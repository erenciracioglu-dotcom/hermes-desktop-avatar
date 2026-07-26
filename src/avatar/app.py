"""Qt application: tray, sprite overlay, chat input, optional console dock.

The app owns the controller, the chat history model, and the console widget
(if the user opens it).  It reacts to controller signals by changing the
overlay state and appending to the history.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .audio_player import AudioPlayer
from .chat_history import ChatHistoryModel
from .chat_panel import ChatPanel
from .console_widget import ConsoleWidget
from .controller import AvatarController, ControllerConfig
from .overlay import OverlayWindow
from .paths import user_config_dir as user_data_dir
from .settings import Settings
from .settings_dialog import SettingsDialog
from .single_instance import release as release_instance
from .single_instance import try_acquire
from .sprites import ensure_placeholder_sprites
from .state_machine import AvatarState


class _PanelFocusFilter(QObject):
    """Hide the chat panel when the user clicks outside it and the avatar.

    All Qt events are routed through QApplication.notify() first.
    Desktop clicks outside the process use chat ActivationChange instead.

    Rules:
      - Click on panel (or child) → keep visible
      - Click on avatar (or child) → keep visible
      - Click on a panel-owned popup (QMenu under Send ▾, etc.) → keep visible
      - Click anywhere else (Settings, tray menus, other windows) → hide panel
    """

    def __init__(self, panel: QWidget, avatar: QWidget, owner: QObject | None = None) -> None:
        super().__init__(owner)
        self._panel = panel
        self._avatar = avatar
        import time as _time
        self._time = _time
        self._show_grace_until: float = 0.0
        self._last_press_logged_at: float = 0.0

    def arm_grace(self, seconds: float = 0.45) -> None:
        """Call when opening chat so the opening click does not immediately hide it."""
        self._show_grace_until = self._time.monotonic() + seconds

    def handle(self, obj: QObject, event: QEvent) -> None:
        """Inspect event; hide panel if needed. Do not call super().notify."""
        if obj is self._panel and event.type() == QEvent.Type.Show:
            self.arm_grace(0.45)
            return
        if event.type() != QEvent.Type.MouseButtonPress:
            return
        if not self._panel.isVisible():
            return
        # Chat panel asks us to ignore outside clicks (tools menu open, etc.)
        try:
            suppress = getattr(self._panel, "should_suppress_outside_hide", None)
            if callable(suppress) and suppress():
                return
        except Exception:
            pass
        in_grace = self._time.monotonic() < self._show_grace_until
        global_pos = self._mouse_pos(event)
        if global_pos is None:
            return

        in_panel = self._hit_widget(self._panel, obj, global_pos)
        in_avatar = self._hit_widget(self._avatar, obj, global_pos)
        in_popup = self._hit_panel_popup(self._panel, obj, global_pos)

        now = self._time.monotonic()
        if now - self._last_press_logged_at > 1.0:
            self._last_press_logged_at = now
            import logging
            logging.getLogger(__name__).info(
                "panel.filter pos=%s in_panel=%s in_avatar=%s in_popup=%s grace=%s obj=%s",
                global_pos, in_panel, in_avatar, in_popup, in_grace, type(obj).__name__,
            )
        if in_grace:
            return
        if in_panel or in_avatar or in_popup:
            return
        self._panel.hide_panel()

    @staticmethod
    def _is_under(widget: QWidget | None, ancestor: QWidget | None) -> bool:
        if widget is None or ancestor is None:
            return False
        w: QWidget | None = widget
        while w is not None:
            if w is ancestor:
                return True
            w = w.parentWidget()
        return False

    @classmethod
    def _hit_panel_popup(cls, panel: QWidget | None, obj: QObject, global_pos) -> bool:
        """True if the click is on a popup (QMenu) owned by the chat panel.

        QMenu is a top-level popup: its geometry is outside the panel frame and
        the event target is often a bare QWindow — so normal hit-tests miss it
        and would hide the panel mid-menu (breaking Attach screenshot).
        """
        if panel is None or not panel.isVisible():
            return False
        try:
            from PySide6.QtWidgets import QApplication, QMenu
        except Exception:
            return False

        # Active popup (the Send ▾ tools menu while open)
        try:
            popup = QApplication.activePopupWidget()
            if popup is not None and (
                cls._is_under(popup, panel) or popup.parentWidget() is panel
            ):
                return True
        except Exception:
            pass

        # Event target is a widget inside a panel-owned menu
        if isinstance(obj, QWidget):
            w: QWidget | None = obj
            while w is not None:
                if isinstance(w, QMenu) and (
                    cls._is_under(w, panel) or w.parentWidget() is panel
                ):
                    return True
                if w is panel:
                    return True
                w = w.parentWidget()

        # Geometry: any visible menu owned by the panel under the cursor
        try:
            for top in QApplication.topLevelWidgets():
                if not isinstance(top, QMenu) or not top.isVisible():
                    continue
                if not (cls._is_under(top, panel) or top.parentWidget() is panel):
                    # Also accept the panel's known tools menu even if reparented
                    known = getattr(panel, "_tools_menu", None)
                    if known is not None and top is not known:
                        continue
                try:
                    if top.frameGeometry().contains(global_pos):
                        return True
                except Exception:
                    continue
            # Explicit tools menu geometry (parent may be cleared while popup)
            known = getattr(panel, "_tools_menu", None)
            if known is not None and known.isVisible():
                try:
                    if known.frameGeometry().contains(global_pos):
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    @staticmethod
    def _hit_widget(widget: QWidget | None, obj: QObject, global_pos) -> bool:
        if widget is None or not widget.isVisible():
            return False
        # Widget hierarchy (reliable for normal child widgets)
        w: QWidget | None
        if isinstance(obj, QWidget):
            w = obj
            while w is not None:
                if w is widget:
                    return True
                w = w.parentWidget()
        # Geometry fallback (frameless / QWindow targets)
        try:
            if widget.frameGeometry().contains(global_pos):
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _mouse_pos(event: QEvent):
        """Extract global mouse position from QEvent, or None."""
        try:
            gp = event.globalPosition() if hasattr(event, "globalPosition") else None
            if gp is not None:
                from PySide6.QtCore import QPoint
                return QPoint(int(gp.x()), int(gp.y()))
        except Exception:
            pass
        try:
            return event.globalPos() if hasattr(event, "globalPos") else None
        except Exception:
            return None

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- helpers
class _FilterApp(QApplication):
    """QApplication that routes all events through the panel focus filter.

    Standard installEventFilter does not see mouse events on other windows.
    """

    def __init__(self, argv):
        super().__init__(argv)
        self._panel_focus_filter: _PanelFocusFilter | None = None

    def install_panel_focus_filter(self, panel: QWidget, avatar: QWidget) -> None:
        self._panel_focus_filter = _PanelFocusFilter(panel, avatar, self)

    def arm_chat_grace(self, seconds: float = 0.45) -> None:
        f = self._panel_focus_filter
        if f is not None:
            f.arm_grace(seconds)

    def notify(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        f = self._panel_focus_filter
        if f is not None:
            f.handle(obj, event)
        return super().notify(obj, event)


# --------------------------------------------------------------------- helpers
def _tray_icon() -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(80, 180, 255))
    p.setPen(QColor(20, 40, 60))
    p.drawEllipse(8, 8, 48, 48)
    p.setBrush(QColor(255, 220, 80))
    p.drawEllipse(28, 4, 8, 8)
    p.end()
    return QIcon(pix)


# --------------------------------------------------------------------- main
def run() -> int:
    log_dir = user_data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "avatar.log"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stderr)
    # On Windows, sys.stderr is often a legacy code page; Unicode log lines
    # can raise UnicodeEncodeError. Use errors='replace' for the console;
    # the file handler still writes full UTF-8.
    if hasattr(sh.stream, "reconfigure"):
        try:
            sh.stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if not try_acquire():
        QMessageBox.warning(
            None,
            "Avatar already running",
            "Another instance of Hermes Desktop Avatar is already open.",
        )
        return 1

    app = _FilterApp(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(_tray_icon())

    settings = Settings()
    ensure_placeholder_sprites()

    # ----- tray menu
    tray = QSystemTrayIcon(_tray_icon(), app)
    tray.setToolTip("Hermes Desktop Avatar")
    menu = QMenu()
    menu.addAction("Chat…", lambda: open_chat())
    menu.addAction("Hermes Console", lambda: open_console())
    menu.addAction("Settings…", lambda: open_settings())
    menu.addSeparator()
    menu.addAction("Quit", app.quit)
    tray.setContextMenu(menu)
    tray.show()

    # ----- chat history + controller
    history = ChatHistoryModel(storage_path=log_dir / "chat_history.json")

    def _controller_config_from_settings() -> ControllerConfig:
        h = settings.get("hermes") or {}
        v = settings.get("voice") or {}
        try:
            timeout_s = float(h.get("startup_timeout_seconds", 60) or 60)
        except (TypeError, ValueError):
            timeout_s = 60.0
        hermes_cmd = h.get("hermes_command")
        if hermes_cmd is not None:
            hermes_cmd = str(hermes_cmd).strip() or None
        api_key = h.get("api_key") or h.get("gateway_key")
        if api_key is not None:
            api_key = str(api_key).strip() or None
        return ControllerConfig(
            gateway_url=h.get("gateway_url") or None,
            session_id=h.get("session_id") or None,
            context_prompt=h.get("persona") or None,
            voice_replies=bool(v.get("enabled", False)),
            # If Hermes tts_tool fails, Edge still speaks the same text.
            edge_fallback=bool(v.get("edge_fallback", True)),
            language=str(settings.get("language") or "en"),
            tts_voice=str(v.get("tts_voice") or "en-US-JennyNeural"),
            tts_rate=str(v.get("rate") or "+0%"),
            auto_start=bool(h.get("auto_start", True)),
            auto_restart=bool(h.get("auto_restart", True)),
            startup_timeout_seconds=timeout_s,
            hermes_command=hermes_cmd,
            api_key=api_key,
            ensure_api_server=bool(h.get("ensure_api_server", True)),
            write_hermes_env=bool(h.get("write_hermes_env", True)),
        )

    controller = AvatarController(_controller_config_from_settings())
    # Gateway ensure logs progress to avatar.log only — no modal status box
    # (a NoButton QMessageBox can stick open and block right-click on the overlay).
    try:
        controller.start()
    except Exception as exc:
        QMessageBox.critical(None, "Could not start Hermes", str(exc))
        return 2

    # Persist API key resolved/generated during ensure (same as Hermes API_SERVER_KEY).
    if controller.config.api_key:
        h = settings.get("hermes") or {}
        if h.get("api_key") != controller.config.api_key:
            h["api_key"] = controller.config.api_key
            settings.set("hermes", h)
            logger.info("saved hermes.api_key for client Authorization")

    # ----- overlay (sprite window) — restore last pos / scale / opacity
    from .character_registry import resolve_character

    disp = settings.get("display") or {}
    try:
        init_scale = float(disp.get("scale", 0.51))
    except (TypeError, ValueError):
        init_scale = 0.51
    overlay = OverlayWindow(scale=init_scale)
    character = resolve_character(settings.get("character_id") or "nora")
    overlay.set_character(character)
    logger.info(
        "active character id=%s label=%s source=%s",
        character.id,
        character.label,
        getattr(character, "source", "?"),
    )
    voice_on = bool((settings.get("voice") or {}).get("enabled", False))
    overlay.set_voice_replies_enabled(voice_on)
    try:
        op = float(disp.get("opacity", 0.95))
    except (TypeError, ValueError):
        op = 0.95
    overlay.set_opacity(op)
    sx, sy = disp.get("x"), disp.get("y")
    if sx is not None and sy is not None:
        try:
            overlay.apply_saved_geometry(int(sx), int(sy), scale=init_scale, opacity=op)
        except (TypeError, ValueError):
            overlay.ensure_default_placement()
    else:
        overlay.ensure_default_placement()
    overlay.show()

    def _save_overlay_geometry() -> None:
        d = settings.get("display") or {}
        d["x"] = overlay.x()
        d["y"] = overlay.y()
        d["scale"] = float(getattr(overlay, "_scale", 0.51))
        d["opacity"] = float(getattr(overlay, "_opacity", 0.95))
        settings.set("display", d)
        logger.info(
            "saved overlay geometry x=%s y=%s scale=%.2f opacity=%.2f",
            d["x"], d["y"], d["scale"], d["opacity"],
        )

    overlay.request_save_geometry.connect(_save_overlay_geometry)
    overlay.request_apply_scale.connect(lambda _s: _save_overlay_geometry())
    overlay.request_apply_opacity.connect(lambda _o: _save_overlay_geometry())

    # Hermes TTS files (MEDIA: paths) → autoplay
    audio_player = AudioPlayer(app)
    audio_player.finished.connect(controller.notify_tts_finished)
    audio_player.error.connect(
        lambda msg: logger.warning("audio player: %s", msg)
    )

    def _on_assistant_audio(paths: list) -> None:
        logger.info("autoplay %d audio file(s)", len(paths or []))
        try:
            audio_player.play_paths(list(paths or []))
        except Exception:
            logger.exception("autoplay failed")

    def _on_interrupted() -> None:
        """User sent a new message mid-turn — stop voice (barge-in).

        Must never block: AudioPlayer.stop() mutes/pauses only and defers
        hard stop. QueuedConnection keeps this off the Send call stack.
        """
        logger.info("interrupted: hard-stop TTS")
        try:
            audio_player.stop(emit_finished=False)
        except Exception:
            logger.exception("interrupt audio stop failed")

    controller.assistant_audio.connect(
        _on_assistant_audio, Qt.ConnectionType.QueuedConnection
    )
    # QueuedConnection: never run multimedia teardown inside Send.
    controller.interrupted.connect(
        _on_interrupted, Qt.ConnectionType.QueuedConnection
    )

    def _on_avatar_state(state: AvatarState) -> None:
        """Map controller lifecycle → overlay animation mode."""
        if state == AvatarState.THINKING:
            overlay.set_state("think")
        elif state == AvatarState.TALKING:
            overlay.set_state("talk")
        else:
            overlay.set_state("idle")

    # QueuedConnection: state is often emitted from the chat worker thread.
    controller.state_changed.connect(
        _on_avatar_state, Qt.ConnectionType.QueuedConnection
    )

    chat_panel: ChatPanel | None = None
    console_window: ConsoleWidget | None = None

    def _save_chat_geometry() -> None:
        if chat_panel is None:
            return
        g = chat_panel.geometry_dict()
        if g.get("w", 0) < 200 or g.get("h", 0) < 120:
            return
        # Merge geometry into existing chat settings (preserve glass_opacity, etc.)
        ch = dict(settings.get("chat") or {})
        ch.update(g)
        settings.set("chat", ch)
        logger.info("saved chat geometry %s", g)

    _chat_geom_timer = QTimer()
    _chat_geom_timer.setSingleShot(True)
    _chat_geom_timer.timeout.connect(_save_chat_geometry)

    def _schedule_save_chat_geometry() -> None:
        _chat_geom_timer.start(350)

    def _ensure_chat_panel() -> ChatPanel:
        nonlocal chat_panel
        if chat_panel is None:
            chat_panel = ChatPanel()
            # Worker threads emit controller signals — always Queued to the UI thread.
            _q = Qt.ConnectionType.QueuedConnection
            chat_panel.request_user_message.connect(controller.send_user_message)

            def _on_user_msg(text: str) -> None:
                chat_panel.add_user_message(text)

            def _on_assistant_msg(text: str) -> None:
                logger.info("panel.add_assistant_message: %s", (text or "")[:60])
                chat_panel.add_assistant_message(text)

            def _on_err(msg: str) -> None:
                chat_panel.add_system_message(f"[error] {msg}")

            controller.user_message.connect(_on_user_msg, _q)
            controller.assistant_message.connect(_on_assistant_msg, _q)
            controller.error.connect(_on_err, _q)
            chat_panel.geometry_changed.connect(_schedule_save_chat_geometry)
            chat_panel.request_close.connect(_save_chat_geometry)
            ch = settings.get("chat") or {}
            if all(ch.get(k) is not None for k in ("x", "y", "w", "h")):
                try:
                    chat_panel.apply_saved_geometry(
                        int(ch["x"]), int(ch["y"]), int(ch["w"]), int(ch["h"])
                    )
                except (TypeError, ValueError):
                    pass
            try:
                chat_panel.set_glass_opacity(ch.get("glass_opacity", 0.88))
            except Exception:
                logger.exception("apply chat glass_opacity failed")
            chat_panel.set_avatar_widget(overlay)
            app.install_panel_focus_filter(chat_panel, overlay)
            logger.info("ChatPanel created and wired")
        return chat_panel

    def open_chat() -> None:
        """Show chat — last geometry if known, else beside avatar."""
        logger.info("open_chat() called")
        panel = _ensure_chat_panel()
        if panel.isVisible():
            panel.raise_()
            panel.activateWindow()
            panel.input.setFocus()
            return

        ch = settings.get("chat") or {}
        restored = False
        if panel.has_user_geometry():
            restored = True
        elif all(ch.get(k) is not None for k in ("x", "y", "w", "h")):
            try:
                panel.apply_saved_geometry(
                    int(ch["x"]), int(ch["y"]), int(ch["w"]), int(ch["h"])
                )
                restored = True
            except (TypeError, ValueError):
                restored = False

        if not restored:
            geom = overlay.geometry()
            screen = QApplication.screenAt(geom.center()) or QApplication.primaryScreen()
            screen_geo = screen.availableGeometry()
            panel.place_near_avatar(geom, screen_geo)
            logger.info("chat placed near avatar → %s", panel.geometry_dict())
        else:
            logger.info("chat restored geometry → %s", panel.geometry_dict())

        panel.input.clear()
        app.arm_chat_grace(0.5)
        panel.show_panel(focus_input=True)
        _save_chat_geometry()

    def open_console():
        nonlocal console_window
        if console_window is None or not console_window.isVisible():
            console_window = ConsoleWidget(log_path=log_file)
            console_window.closed.connect(lambda: None)
        console_window.show()
        console_window.raise_()

    settings_win: SettingsDialog | None = None

    def _apply_settings_from_dialog() -> None:
        cfg = _controller_config_from_settings()
        controller.update_config(cfg)
        overlay.set_voice_replies_enabled(cfg.voice_replies)
        # Switch character if the user picked a different pack / built-in
        try:
            new_char = resolve_character(settings.get("character_id") or "nora")
            current = getattr(overlay, "_preset", None)
            if current is None or getattr(current, "id", None) != new_char.id:
                overlay.set_character(new_char)
                logger.info("character switched → %s", new_char.id)
        except Exception:
            logger.exception("character switch failed")
        # Live-update chat glass if the panel already exists
        if chat_panel is not None:
            try:
                ch = settings.get("chat") or {}
                chat_panel.set_glass_opacity(ch.get("glass_opacity", 0.88))
            except Exception:
                logger.exception("live glass_opacity update failed")
        logger.info("settings applied (non-modal)")

    def open_settings():
        """Open Settings as a non-modal window (avatar + chat stay usable)."""
        nonlocal settings_win
        if settings_win is not None and settings_win.isVisible():
            settings_win.raise_()
            settings_win.activateWindow()
            return

        dlg = SettingsDialog(settings)
        settings_win = dlg

        def _on_finished(_result: int) -> None:
            nonlocal settings_win
            settings_win = None

        dlg.settings_applied.connect(_apply_settings_from_dialog)
        dlg.finished.connect(_on_finished)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _toggle_voice_replies() -> None:
        voice = settings.get("voice") or {}
        new_val = not bool(voice.get("enabled", False))
        voice["enabled"] = new_val
        settings.set("voice", voice)
        controller.set_voice_replies(new_val)
        overlay.set_voice_replies_enabled(new_val)
        logger.info("voice replies → %s", new_val)

    # exit cleanup — controller + single-instance lock release edip app.quit.
    def on_quit():
        try:
            _save_overlay_geometry()
            if chat_panel is not None:
                _save_chat_geometry()
            audio_player.stop()
            controller.stop()
        finally:
            release_instance()
        app.quit()

    overlay.request_quit.connect(on_quit)
    overlay.request_open_chat.connect(open_chat)
    overlay.request_settings.connect(open_settings)
    overlay.request_toggle.connect(
        lambda: overlay.setVisible(not overlay.isVisible())
    )
    overlay.request_toggle_voice.connect(_toggle_voice_replies)

    # Persist turns across restarts.
    controller.user_message.connect(
        lambda text: history.add_turn("user", text)
    )
    controller.assistant_message.connect(
        lambda text: history.add_turn("assistant", text)
    )

    app.aboutToQuit.connect(on_quit)
    tray.activated.connect(
        lambda reason: open_chat()
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick
        else None
    )

    return app.exec()


if __name__ == "__main__":
    sys.exit(run())

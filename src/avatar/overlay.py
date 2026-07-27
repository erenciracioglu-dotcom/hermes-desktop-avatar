"""Always-on-top mascot overlay + speech bubble."""

from __future__ import annotations

import logging
import random

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QMenu, QPushButton, QVBoxLayout, QWidget

from .sprites import ambient_states_available, load_frames_for_preset

logger = logging.getLogger(__name__)


class OverlayWindow(QWidget):
    drag_moved = Signal()
    request_toggle = Signal()
    request_open_chat = Signal()
    request_settings = Signal()
    request_quit = Signal()
    request_apply_scale = Signal(float)
    request_apply_opacity = Signal(float)
    request_save_geometry = Signal()  # position / size after drag or menu
    request_user_message = Signal(str)
    request_toggle_voice = Signal()  # Hermes TTS replies on/off

    def __init__(self, scale: float = 0.51, parent=None) -> None:
        super().__init__(parent)
        # Avoid Qt.Tool — it breaks menus/dialogs on some Windows setups.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool  # no taskbar button
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        # Opaque-ish so user can always see something even if sprites fail
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumSize(96, 96)

        # Character is applied by the app via set_character() after settings load.
        # Placeholder idle until then so the window can still paint.
        from .character_registry import default_character

        self._preset = default_character()
        self._ambient_pool: dict = {}
        self._forced_pools: dict[str, list[str]] = {}
        self._frames: dict = {}
        self._ambient: list[str] = []
        self._mode = "ambient"  # ambient | forced
        self._forced_family: str | None = None  # "talk" | "think" while forced
        self._state = "idle"
        self._frame_i = 0
        self._scale = scale
        self._opacity = 0.95
        self._bubble_text = ""
        self._drag_pos: QPoint | None = None
        self._enabled = True
        self._voice_replies = False

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick_anim)
        # ~24 FPS (source video). Forced think/talk use the same rate.
        self._anim_ms = {
            "idle": 41,
            "walk": 41,
            "talk": 41,
            "think": 41,
            "sleep": 41,
            "attack": 90,
            "dance": 41,
        }
        self._ingest_loaded_frames(load_frames_for_preset(self._preset))
        if self._ambient:
            self._pick_ambient(force_new=True)
        self._anim.start(self._anim_ms.get(self._state, 41))

        # Default placement; app may call apply_saved_geometry / place_corner after.
        self._placed = False
        self._bubble_timer = QTimer(self)
        self._bubble_timer.setSingleShot(True)
        self._bubble_timer.timeout.connect(self.clear_bubble)

        self._relayout()

    def set_scale(self, scale: float) -> None:
        self._scale = max(0.33, min(1.0, scale))
        self._relayout()

    def set_opacity(self, opacity: float) -> None:
        self._opacity = max(0.3, min(1.0, float(opacity)))
        self.setWindowOpacity(self._opacity)

    def apply_saved_geometry(self, x: int, y: int, scale: float | None = None, opacity: float | None = None) -> None:
        """Restore last position / scale / opacity from settings."""
        if scale is not None:
            self._scale = max(0.33, min(1.0, float(scale)))
        if opacity is not None:
            self.set_opacity(opacity)
        self._relayout()
        self.move(int(x), int(y))
        self._placed = True

    def ensure_default_placement(self) -> None:
        if not self._placed:
            self.place_corner("bottom-right", 24)
            self._placed = True

    def set_character_visible(self, visible: bool) -> None:
        if visible:
            self.show()
        else:
            self.hide()

    def set_enabled_label(self, enabled: bool) -> None:
        self._enabled = enabled

    def set_voice_replies_enabled(self, enabled: bool) -> None:
        self._voice_replies = bool(enabled)

    def voice_replies_enabled(self) -> bool:
        return self._voice_replies

    def popup_menu(self) -> None:
        """Build a fresh menu and dispatch by selected action (most reliable on Windows)."""
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #222; color: #eee; padding: 4px; }"
            "QMenu::item:selected { background: #06c; }"
        )
        a_chat = menu.addAction("💬  Chat…")
        a_voice = menu.addAction(
            "🔇  Voice replies: ON" if not self._voice_replies
            else "🔊  Voice replies: OFF"
        )
        a_toggle = menu.addAction(
            "⏸  Hide avatar" if self._enabled else "▶  Show avatar"
        )
        menu.addSeparator()

        size_menu = menu.addMenu("🔍  Size")
        size_menu.setStyleSheet(menu.styleSheet())
        size_presets = [
            ("Mini (smallest)", 0.33),
            ("Very small",      0.41),
            ("Small",           0.51),
            ("Medium",          0.64),
            ("Large",           0.80),
            ("Very large",      1.00),
        ]
        size_actions = []
        for label, scale in size_presets:
            mark = " ✓" if abs(self._scale - scale) < 0.05 else ""
            act = size_menu.addAction(f"{label}{mark}")
            act.setData(scale)
            size_actions.append(act)

        # Opacity submenu — 5 presets, 0.4–1.0
        opacity_menu = menu.addMenu("💧  Opacity")
        opacity_menu.setStyleSheet(menu.styleSheet())
        opacity_presets = [
            ("Ghost",         0.40),
            ("Semi-transparent", 0.60),
            ("Normal",        0.80),
            ("Solid",         0.95),
            ("Opaque",        1.00),
        ]
        opacity_actions = []
        for label, op in opacity_presets:
            mark = " ✓" if abs(self._opacity - op) < 0.05 else ""
            act = opacity_menu.addAction(f"{label}{mark}")
            act.setData(op)
            opacity_actions.append(act)

        menu.addSeparator()
        a_settings = menu.addAction("⚙  Settings…")
        menu.addSeparator()
        a_quit = menu.addAction("✖  Quit")

        # Show near cursor
        from PySide6.QtGui import QCursor

        chosen = menu.exec(QCursor.pos())
        logger.info("menu chosen: %s", chosen.text() if chosen else None)
        print(f"[avatar] menu chosen: {chosen.text() if chosen else None}", flush=True)

        if chosen is a_chat:
            print("[avatar] menu chosen: Chat", flush=True)
            self.request_open_chat.emit()
        elif chosen is a_voice:
            print("[avatar] menu chosen: toggle voice", flush=True)
            self.request_toggle_voice.emit()
        elif chosen is a_toggle:
            self.request_toggle.emit()
        elif chosen is a_settings:
            self.request_settings.emit()
        elif chosen is a_quit:
            self.request_quit.emit()
        elif chosen in size_actions:
            new_scale = float(chosen.data())
            print(f"[avatar] size preset: {new_scale}", flush=True)
            self._apply_scale(new_scale)
        elif chosen in opacity_actions:
            new_opacity = float(chosen.data())
            print(f"[avatar] opacity preset: {new_opacity}", flush=True)
            self._apply_opacity(new_opacity)

    def _apply_scale(self, new_scale: float) -> None:
        """Apply scale 0.33..1.0 and resize, anchoring the bottom-right corner."""
        new_scale = max(0.33, min(1.0, new_scale))
        old_scale = self._scale
        old_w = self.width()
        old_h = self.height()
        old_x = self.x()
        old_y = self.y()
        # Anchor bottom-right while resizing
        old_right = old_x + old_w
        old_bottom = old_y + old_h

        self._scale = new_scale
        self._relayout()

        new_x = old_right - self.width()
        new_y = old_bottom - self.height()
        self.move(new_x, new_y)
        self.update()
        logger.info("scale: %.2f -> %.2f (window: %dx%d, pos: %d,%d)",
                    old_scale, new_scale, self.width(), self.height(), self.x(), self.y())
        self.request_apply_scale.emit(new_scale)
        self.request_save_geometry.emit()

    def _apply_opacity(self, new_opacity: float) -> None:
        """Apply new window opacity 0.3..1.0, save to settings."""
        new_opacity = max(0.3, min(1.0, new_opacity))
        old_opacity = self._opacity
        self._opacity = new_opacity
        self.setWindowOpacity(new_opacity)
        logger.info("opacity: %.2f -> %.2f", old_opacity, new_opacity)
        self.request_apply_opacity.emit(new_opacity)
        self.request_save_geometry.emit()

    def set_character(self, preset) -> None:
        """Switch the active character preset."""
        from .sprites import load_frames_for_preset

        self._preset = preset
        self._ingest_loaded_frames(load_frames_for_preset(preset))
        self._mode = "ambient"
        if self._ambient:
            self._pick_ambient(force_new=True)
        self._frame_i = 0
        self._anim.setInterval(self._anim_ms.get(self._state, 41))
        self._relayout()
        self.update()
        logger.info("Overlay character → %s", preset.id)

    def _ingest_loaded_frames(self, loaded: dict) -> None:
        """Build ambient + forced clip pools from load_frames_for_preset output.

        Multi-clip states (list of sequences) become forced pool keys so
        set_state('talk'|'think') can pick a variant each time.
        """
        from .sprites import _LazySpriteSequence

        self._ambient_pool = {}
        self._forced_pools = {}
        frames: dict = {}

        idle_val = loaded.pop("idle", None)
        if isinstance(idle_val, list):
            if idle_val and isinstance(idle_val[0], QPixmap):
                # A plain list of QPixmaps is one animation sequence, not a
                # list of clip variants. This is the generated-placeholder
                # shape used when a character pack has no usable clips.
                self._ambient_pool["idle_0"] = idle_val
            else:
                for i, seq in enumerate(idle_val):
                    if isinstance(seq, _LazySpriteSequence):
                        key = f"idle_{i}_{seq._path.stem}"
                        self._ambient_pool[key] = seq
                    elif isinstance(seq, list) and seq:
                        key = f"idle_{i}"
                        self._ambient_pool[key] = seq
        elif isinstance(idle_val, _LazySpriteSequence):
            self._ambient_pool[f"idle_0_{idle_val._path.stem}"] = idle_val
        elif idle_val is not None:
            self._ambient_pool["idle_0"] = idle_val

        for state, val in loaded.items():
            # List of sequences (variants) vs single sequence / pixmap list
            is_variant_list = (
                isinstance(val, list)
                and val
                and (
                    isinstance(val[0], _LazySpriteSequence)
                    or (
                        isinstance(val[0], list)
                        and val[0]
                        and hasattr(val[0][0], "width")
                    )
                )
            )
            if is_variant_list:
                keys: list[str] = []
                for i, seq in enumerate(val):
                    if isinstance(seq, _LazySpriteSequence):
                        key = f"{state}_{i}_{seq._path.stem}"
                    else:
                        key = f"{state}_{i}"
                    frames[key] = seq
                    keys.append(key)
                self._forced_pools[state] = keys
                frames[state] = frames[keys[0]]  # logical alias → first
            else:
                frames[state] = val

        frames.update(self._ambient_pool)
        if self._ambient_pool:
            frames["idle"] = next(iter(self._ambient_pool.values()))
        self._frames = frames
        self._ambient = list(self._ambient_pool.keys()) or ["idle"]
        logger.info(
            "sprites: ambient=%d talk=%s think=%s",
            len(self._ambient),
            self._forced_pools.get("talk"),
            self._forced_pools.get("think"),
        )

    def set_state(self, state: str) -> None:
        """App-level animation state.

        - idle  → ambient mode (rarity-weighted idle clips)
        - talk  → forced talking clip (random variant if several)
        - think → forced thinking clip (random variant if several)
        """
        if state == "idle":
            if self._mode != "ambient":
                self._mode = "ambient"
                self._forced_family = None
                self._pick_ambient(force_new=True)
                self.update()
            return

        # Map logical state → concrete frame key (variant pool or direct)
        pool = self._forced_pools.get(state)
        if pool:
            # Prefer a different clip than the one currently showing
            others = [k for k in pool if k != self._state]
            pick = random.choice(others if others else pool)
            frame_key = pick
        elif state in self._frames and self._frames.get(state):
            frame_key = state
        else:
            # Missing assets → stay ambient rather than freeze
            logger.warning("set_state(%s): no frames, staying ambient", state)
            self.set_state("idle")
            return

        base_ms = self._anim_ms.get(state, 41)
        self._mode = "forced"
        self._forced_family = state  # "talk" | "think" | …
        self._state = frame_key
        self._frame_i = 0
        self._anim.setInterval(base_ms)
        self._relayout()
        self.update()
        logger.info("overlay forced state → %s (key=%s)", state, frame_key)

    def _pick_ambient(self, *, force_new: bool = False) -> None:
        choices = list(self._ambient) or ["idle"]
        # Rarity-weighted pick: higher rarity = rarer.
        # weight = 1/rarity → tier a (1) → 1.0, tier b (4) → 0.25, tier c (20) → 0.05
        # so tier-a clips play ~20× more often than tier-c.
        rarity = getattr(self._preset, "idle_rarity", None) if hasattr(self, "_preset") else None
        if len(choices) > 1 and not force_new:
            others = [c for c in choices if c != self._state]
            pool = others if others else choices
        else:
            pool = choices
        if rarity:
            # Pool key "idle_<i>_<stem>" → match rarity by clip stem.
            weights = []
            for key in pool:
                # e.g. "idle_4_nora_idle_b_dancing_serious" → stem after idle_<i>_
                parts = key.split("_", 2)
                stem = parts[2] if len(parts) >= 3 else key
                r = (
                    rarity.get(stem)
                    or rarity.get(key)
                    or rarity.get(f"clips/{stem}.webp")
                    or 1.0
                )
                weights.append(1.0 / max(float(r), 0.001))
            pick = random.choices(pool, weights=weights, k=1)[0]
        else:
            pick = random.choice(pool)
        self._state = pick
        self._frame_i = 0
        self._anim.setInterval(self._anim_ms.get(pick, 41))
        self._relayout()

    def show_bubble(self, text: str, duration_ms: int = 12000) -> None:
        self._bubble_text = (text or "").strip()
        self._relayout()
        self.update()
        if duration_ms > 0:
            self._bubble_timer.start(duration_ms)

    def clear_bubble(self) -> None:
        self._bubble_text = ""
        self._relayout()
        self.update()

    def place_corner(self, corner: str, margin: int = 24) -> None:
        screen = self.screen()
        if screen is None:
            from PySide6.QtGui import QGuiApplication

            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        w, h = self.width(), self.height()
        if corner == "bottom-left":
            self.move(geo.left() + margin, geo.bottom() - h - margin)
        elif corner == "top-right":
            self.move(geo.right() - w - margin, geo.top() + margin)
        elif corner == "top-left":
            self.move(geo.left() + margin, geo.top() + margin)
        else:
            self.move(geo.right() - w - margin, geo.bottom() - h - margin)
        self._placed = True

    def current_pixmap(self) -> QPixmap:
        # Do not scale here — paintEvent smooth-scales to the widget size once.
        frames = self._frames.get(self._state) or self._frames["idle"]
        if frames is None or len(frames) == 0:
            return QPixmap()
        return frames[self._frame_i % len(frames)]


    def _tick_anim(self) -> None:
        frames = self._frames.get(self._state) or self._frames.get("idle") or []
        if not frames:
            return
        self._frame_i += 1
        if self._frame_i >= len(frames):
            self._frame_i = 0
            if self._mode == "ambient":
                self._pick_ambient(force_new=False)
            elif self._forced_family and self._forced_pools.get(self._forced_family):
                # Still thinking/talking: cycle a (possibly different) variant
                self._roll_forced_variant(self._forced_family)
        self.update()

    def _roll_forced_variant(self, family: str) -> None:
        """Pick next forced clip in the same family without leaving forced mode."""
        pool = self._forced_pools.get(family) or []
        if not pool:
            return
        others = [k for k in pool if k != self._state]
        pick = random.choice(others if others else pool)
        if pick != self._state:
            self._state = pick
            self._frame_i = 0
            self._anim.setInterval(self._anim_ms.get(family, 41))
            self._relayout()

    def _bubble_size(self) -> QSize:
        if not self._bubble_text:
            return QSize(0, 0)
        font = QFont("Segoe UI", 10)
        fm = QFontMetrics(font)
        max_w = 240
        br = fm.boundingRect(
            QRect(0, 0, max_w, 500), Qt.TextFlag.TextWordWrap, self._bubble_text
        )
        return QSize(min(max_w, br.width() + 24), br.height() + 20)

    def _relayout(self) -> None:
        # Widget size = sprite × scale + padding. Keep the bottom edge fixed so
        # the mascot does not jump when a speech bubble opens/closes.
        pix = self.current_pixmap()
        bubble = self._bubble_size()
        pad = 8
        gap = 6
        scaled_w = int(pix.width() * self._scale)
        scaled_h = int(pix.height() * self._scale)
        w = max(scaled_w, bubble.width()) + pad * 2
        h = scaled_h + (bubble.height() + gap if bubble.height() else 0) + pad * 2
        new_w, new_h = max(w, 64), max(h, 64)

        old_h = self.height()
        old_y = self.y()
        delta = new_h - old_h  # + taller, - shorter
        self.setFixedSize(new_w, new_h)
        if delta != 0:
            # Grow upward / shrink downward (bottom edge fixed)
            self.move(self.x(), old_y - delta)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        # Antialiasing for shapes + smooth bilinear pixmap scaling
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform,
            True,
        )
        pix = self.current_pixmap()
        bubble = self._bubble_size()
        pad = 8
        gap = 6
        y = pad
        if bubble.height():
            rect = QRect(pad, y, bubble.width(), bubble.height())
            path = QPainterPath()
            path.addRoundedRect(rect, 10, 10)
            painter.fillPath(path, QColor(255, 255, 245, 240))
            painter.setPen(QColor(40, 40, 40, 200))
            painter.drawPath(path)
            painter.setPen(QColor(25, 25, 25))
            painter.setFont(QFont("Segoe UI", 10))
            painter.drawText(
                rect.adjusted(10, 8, -10, -8),
                Qt.TextFlag.TextWordWrap,
                self._bubble_text,
            )
            y += bubble.height() + gap

        # Single-pass smooth scale of the original pixmap into the widget box.
        # NOTE: SmoothTransformation (bilinear) on translucent alpha pixels
        # creates ghost artifacts — neighbouring frames' alpha bleeds into
        # the current frame. FastTransformation (nearest) avoids this at
        # the cost of slightly sharper edges, which is acceptable for
        # mascot sprites.
        target_w = self.width() - pad * 2
        target_h = self.height() - pad * 2 - (bubble.height() + gap if bubble.height() else 0)
        if pix.width() != target_w or pix.height() != target_h:
            scaled_pix = pix.scaled(
                target_w, target_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        else:
            scaled_pix = pix
        # X: center under bubble if any, else left-align
        if bubble.width():
            x = pad + max(0, (self.width() - pad * 2 - scaled_pix.width()) // 2)
        else:
            x = pad
        painter.drawPixmap(x, y, scaled_pix)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self.popup_menu()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        # Double-click opens the chat panel (same as "Chat…").
        if event.button() == Qt.MouseButton.LeftButton:
            print("[avatar] double-click → open chat", flush=True)
            self.request_open_chat.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    # Inline chat input removed; request_open_chat is the single path to ChatPanel.

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            self.drag_moved.emit()
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_pos is not None:
            self._drag_pos = None
            self.request_save_geometry.emit()
        event.accept()

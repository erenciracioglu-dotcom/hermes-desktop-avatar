"""Settings dialog — character, Hermes gateway, context, voice.

Provider/model live on the Hermes gateway. The context field is an
ephemeral system overlay (channel/environment), not a SOUL replacement.

Shown non-modally so the avatar and chat stay clickable/movable.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .character_registry import list_characters
from .context_defaults import DEFAULT_CONTEXT_PROMPT
from .settings import Settings

DEFAULT_GATEWAY = "http://127.0.0.1:8642"
DEFAULT_SESSION = "avatar-nora"

# Default size; actual min is smaller so short displays still fit.
_DIALOG_W = 520
_DIALOG_H = 700

_DIALOG_MIN_W = 420
_DIALOG_MIN_H = 360

_CLI_TIP = (
    "Optional path to the hermes executable used when the avatar "
    "starts or restarts the local gateway.\n\n"
    "Leave empty unless auto-detect fails: the app finds hermes on "
    "PATH, HERMES_CLI, or common install folders.\n\n"
    "Example:\n"
    "C:\\Users\\…\\hermes-agent\\venv\\Scripts\\hermes.exe"
)


def _help_label(html: str) -> QLabel:
    lbl = QLabel(html)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setOpenExternalLinks(False)
    lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    lbl.setStyleSheet("color: #a0a0b0; font-size: 11px;")
    return lbl


def _question_tip(tooltip: str) -> QLabel:
    """Small '?' badge with a hover tooltip."""
    tip = QLabel("?")
    tip.setFixedSize(20, 20)
    tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
    tip.setToolTip(tooltip)
    tip.setCursor(Qt.CursorShape.WhatsThisCursor)
    tip.setStyleSheet(
        "QLabel {"
        "  color: #c8d0e0;"
        "  background-color: rgba(60, 70, 100, 200);"
        "  border: 1px solid rgba(140, 160, 200, 160);"
        "  border-radius: 10px;"
        "  font-weight: 700;"
        "  font-size: 11px;"
        "}"
        "QLabel:hover {"
        "  background-color: rgba(90, 110, 160, 230);"
        "  color: white;"
        "}"
    )
    return tip


class SettingsDialog(QDialog):
    """Non-modal settings window."""

    # Emitted after settings are saved (OK), before the dialog closes.
    settings_applied = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Hermes Desktop Avatar — Settings")
        # Independent top-level window; never modal — avatar/chat stay usable.
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinMaxButtonsHint
        )
        self.setMinimumSize(_DIALOG_MIN_W, _DIALOG_MIN_H)
        self.setMaximumWidth(720)
        self.setSizeGripEnabled(True)

        hermes = settings.get("hermes") or {}
        voice = settings.get("voice") or {}
        chat = settings.get("chat") or {}
        current_character = str(settings.get("character_id") or "nora")

        # Outer layout: scrollable body + fixed footer buttons
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        # ----- Character picker
        char_box = QGroupBox("Character")
        char_lay = QVBoxLayout(char_box)
        char_lay.setSpacing(6)
        char_row = QHBoxLayout()
        char_row.setSpacing(12)

        self.character_preview = QLabel()
        self.character_preview.setFixedSize(64, 64)
        self.character_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.character_preview.setStyleSheet(
            "QLabel {"
            "  background: rgba(30, 34, 48, 220);"
            "  border: 1px solid rgba(120, 140, 180, 120);"
            "  border-radius: 8px;"
            "}"
        )
        self.character_preview.setToolTip("Character preview")

        char_meta = QVBoxLayout()
        char_meta.setSpacing(4)
        self.character_combo = QComboBox()
        self.character_combo.setMinimumWidth(200)
        self._character_ids: list[str] = []
        self._fill_character_combo(current_character)
        self.character_combo.currentIndexChanged.connect(self._on_character_changed)
        self.character_detail = QLabel("")
        self.character_detail.setWordWrap(True)
        self.character_detail.setStyleSheet("color: #a0a0b0; font-size: 11px;")
        char_meta.addWidget(self.character_combo)
        char_meta.addWidget(self.character_detail)

        char_row.addWidget(self.character_preview, 0)
        char_row.addLayout(char_meta, 1)
        char_lay.addLayout(char_row)
        char_lay.addWidget(_help_label(
            "Character packs (<code>.hchar</code> or folder) under "
            "<code>assets/characters</code> or the user characters folder. "
            "Currently Nora only."
        ))
        layout.addWidget(char_box)
        self._on_character_changed(self.character_combo.currentIndex())

        form = QFormLayout()
        form.setSpacing(8)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        self.gateway_url = QLineEdit(hermes.get("gateway_url") or DEFAULT_GATEWAY)
        form.addRow("Gateway URL:", self.gateway_url)

        self.session_id = QLineEdit(hermes.get("session_id") or DEFAULT_SESSION)
        form.addRow("Session id:", self.session_id)

        self.auto_start = QCheckBox("Auto-start local gateway if offline")
        self.auto_start.setChecked(bool(hermes.get("auto_start", True)))
        form.addRow(self.auto_start)

        self.auto_restart = QCheckBox(
            "Restart gateway if port is dead (PID lock, no API)"
        )
        self.auto_restart.setChecked(bool(hermes.get("auto_restart", True)))
        form.addRow(self.auto_restart)

        # Hermes CLI path + ? hover tip
        self.hermes_command = QLineEdit(
            ""
            if hermes.get("hermes_command") in (None, "")
            else str(hermes.get("hermes_command"))
        )
        self.hermes_command.setPlaceholderText(
            "Leave empty to auto-detect (recommended)"
        )
        cli_row = QWidget()
        cli_layout = QHBoxLayout(cli_row)
        cli_layout.setContentsMargins(0, 0, 0, 0)
        cli_layout.setSpacing(6)
        cli_layout.addWidget(self.hermes_command, 1)
        cli_layout.addWidget(_question_tip(_CLI_TIP), 0, Qt.AlignmentFlag.AlignVCenter)
        form.addRow("Hermes CLI path:", cli_row)

        self.context = QPlainTextEdit(
            hermes.get("persona") or DEFAULT_CONTEXT_PROMPT
        )
        self.context.setPlaceholderText(
            "How should Hermes behave in this channel? (context, not identity)"
        )
        self.context.setMinimumHeight(70)
        self.context.setMaximumHeight(130)
        self.context.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        form.addRow("Environment context:", self.context)

        self.tts_enabled = QCheckBox(
            "Voice replies (avatar TTS — same text as chat)"
        )
        self.tts_enabled.setChecked(bool(voice.get("enabled", False)))
        form.addRow(self.tts_enabled)

        self.edge_fallback = QCheckBox(
            "Edge fallback if Hermes tts_tool fails"
        )
        self.edge_fallback.setChecked(bool(voice.get("edge_fallback", True)))
        form.addRow(self.edge_fallback)

        # Chat glass opacity (solidness): 40%–100%
        try:
            glass_f = float(chat.get("glass_opacity", 0.88))
        except (TypeError, ValueError):
            glass_f = 0.88
        glass_pct = int(round(max(0.40, min(1.0, glass_f)) * 100))
        glass_row = QWidget()
        glass_lay = QHBoxLayout(glass_row)
        glass_lay.setContentsMargins(0, 0, 0, 0)
        glass_lay.setSpacing(8)
        self.glass_opacity = QSlider(Qt.Orientation.Horizontal)
        self.glass_opacity.setRange(40, 100)
        self.glass_opacity.setSingleStep(1)
        self.glass_opacity.setPageStep(5)
        self.glass_opacity.setValue(glass_pct)
        self.glass_opacity.setToolTip(
            "How solid the chat panel looks.\n"
            "Lower = more transparent glass; higher = more opaque."
        )
        self.glass_opacity_label = QLabel(f"{glass_pct}%")
        self.glass_opacity_label.setMinimumWidth(40)
        self.glass_opacity_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.glass_opacity.valueChanged.connect(
            lambda v: self.glass_opacity_label.setText(f"{v}%")
        )
        glass_lay.addWidget(self.glass_opacity, 1)
        glass_lay.addWidget(self.glass_opacity_label, 0)
        form.addRow("Chat glass opacity:", glass_row)

        layout.addLayout(form)
        layout.addWidget(_help_label(
            "<b>Environment context</b> does not replace Hermes identity "
            "(SOUL stays). It is sent as an extra system layer each chat turn."
        ))
        layout.addWidget(_help_label(
            "<b>Gateway management</b> (loopback only): if health fails, the "
            "avatar ensures <code>API_SERVER_*</code> and runs "
            "<code>hermes gateway restart</code>."
        ))
        layout.addWidget(_help_label(
            "<b>Voice replies</b>: the model does not call TTS; the avatar "
            "speaks the chat text via Hermes <code>text_to_speech</code> "
            "(or Edge fallback)."
        ))
        layout.addWidget(_help_label(
            "<b>Chat glass opacity</b>: solidness of the chat panel "
            "(glassmorphism). 40% = airy/transparent, 100% = fully solid."
        ))
        layout.addStretch(1)

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # Footer always visible (not inside the scroll area)
        footer = QWidget()
        footer_lay = QVBoxLayout(footer)
        footer_lay.setContentsMargins(14, 8, 14, 12)
        footer_lay.setSpacing(0)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        footer_lay.addWidget(buttons)
        root.addWidget(footer, 0)

        self._fit_to_screen()

    def _fit_to_screen(self) -> None:
        """Size and place the dialog fully inside the available screen area."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(_DIALOG_W, _DIALOG_H)
            return
        avail = screen.availableGeometry()
        # Leave margin so title bar / taskbar never clip the window.
        max_w = max(_DIALOG_MIN_W, avail.width() - 48)
        max_h = max(_DIALOG_MIN_H, avail.height() - 48)
        w = min(_DIALOG_W, max_w)
        h = min(_DIALOG_H, max_h)
        self.resize(w, h)
        # Center on the available desktop (not the full virtual desktop).
        x = avail.x() + (avail.width() - w) // 2
        y = avail.y() + (avail.height() - h) // 2
        self.move(max(avail.x(), x), max(avail.y(), y))

    def _fill_character_combo(self, selected_id: str) -> None:
        self.character_combo.blockSignals(True)
        self.character_combo.clear()
        self._character_ids = []
        chars = list_characters()
        select_idx = 0
        for i, preset in enumerate(chars):
            src = getattr(preset, "source", "builtin") or "builtin"
            ver = getattr(preset, "version", "") or ""
            suffix = "pack" if src == "pack" else "built-in"
            if ver and src == "pack":
                text = f"{preset.label}  ({suffix} v{ver})"
            else:
                text = f"{preset.label}  ({suffix})"
            self.character_combo.addItem(text)
            self._character_ids.append(preset.id)
            if preset.id == selected_id:
                select_idx = i
        if self._character_ids:
            self.character_combo.setCurrentIndex(select_idx)
        self.character_combo.blockSignals(False)

    def _selected_character_id(self) -> str:
        idx = self.character_combo.currentIndex()
        if 0 <= idx < len(self._character_ids):
            return self._character_ids[idx]
        return "nora"

    def _on_character_changed(self, _index: int = 0) -> None:
        cid = self._selected_character_id()
        preset = None
        for p in list_characters():
            if p.id == cid:
                preset = p
                break
        if preset is None:
            self.character_detail.setText("Character not found.")
            self.character_preview.setPixmap(QPixmap())
            self.character_preview.setText("?")
            return

        src = getattr(preset, "source", "builtin") or "builtin"
        bits = [f"id: {preset.id}", f"source: {src}"]
        if getattr(preset, "version", None):
            bits.append(f"v{preset.version}")
        n_idle = len(preset.idle_ambient or [])
        n_talk = len((preset.sprite_map or {}).get("talk") or [])
        n_think = len((preset.sprite_map or {}).get("think") or [])
        bits.append(f"clips: idle×{n_idle}, talk×{n_talk}, think×{n_think}")
        self.character_detail.setText(" · ".join(bits))

        pix = QPixmap()
        preview = getattr(preset, "preview_path", None)
        if preview is not None and Path(preview).is_file():
            pix = QPixmap(str(preview))
        if pix.isNull():
            # First talk/idle webp frame as tiny preview (pack extract / mascot)
            pix = _preview_pixmap_for_preset(preset, 72)
        if pix.isNull():
            self.character_preview.setPixmap(QPixmap())
            self.character_preview.setText(preset.label[:1].upper())
        else:
            self.character_preview.setText("")
            self.character_preview.setPixmap(
                pix.scaled(
                    64,
                    64,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def _on_accept(self) -> None:
        self.settings.set("character_id", self._selected_character_id())

        hermes = self.settings.get("hermes") or {}
        hermes["gateway_url"] = (
            self.gateway_url.text().strip() or DEFAULT_GATEWAY
        ).rstrip("/")
        hermes["session_id"] = self.session_id.text().strip() or DEFAULT_SESSION
        # Config key stays "persona" for compatibility; meaning = environment overlay.
        hermes["persona"] = (
            self.context.toPlainText().strip() or DEFAULT_CONTEXT_PROMPT
        )
        hermes["auto_start"] = self.auto_start.isChecked()
        hermes["auto_restart"] = self.auto_restart.isChecked()
        cmd = self.hermes_command.text().strip()
        hermes["hermes_command"] = cmd or None
        if "startup_timeout_seconds" not in hermes:
            hermes["startup_timeout_seconds"] = 60
        for legacy_key in (
            "port",
            "provider",
            "model",
            "base_url",
            "auto_start_server",
        ):
            hermes.pop(legacy_key, None)
        self.settings.set("hermes", hermes)

        voice = self.settings.get("voice") or {}
        voice["enabled"] = self.tts_enabled.isChecked()
        voice["edge_fallback"] = self.edge_fallback.isChecked()
        # Wake-word UI removed for now; keep any existing config keys untouched.
        self.settings.set("voice", voice)

        chat = self.settings.get("chat") or {}
        if not isinstance(chat, dict):
            chat = {}
        chat["glass_opacity"] = round(self.glass_opacity.value() / 100.0, 2)
        self.settings.set("chat", chat)

        self.settings_applied.emit()
        self.accept()


def _preview_pixmap_for_preset(preset, size: int = 72) -> QPixmap:
    """Best-effort first-frame preview without loading the full character."""
    try:
        from .sprites import _find_sprite_frames_for_preset, _load_webp_frames
    except Exception:
        return QPixmap()

    prefixes: list[str] = []
    sm = preset.sprite_map or {}
    for key in ("talk", "idle", "think"):
        prefixes.extend(sm.get(key) or [])
    prefixes.extend(preset.idle_ambient or [])
    seen: set[str] = set()
    for prefix in prefixes:
        if prefix in seen:
            continue
        seen.add(prefix)
        paths = _find_sprite_frames_for_preset(preset, prefix)
        if not paths:
            continue
        path = paths[0]
        try:
            if path.suffix.lower() == ".webp":
                frames = _load_webp_frames(path, lazy=True)
                if frames:
                    return frames[0]
            else:
                pix = QPixmap(str(path))
                if not pix.isNull():
                    return pix
        except Exception:
            continue
    return QPixmap()

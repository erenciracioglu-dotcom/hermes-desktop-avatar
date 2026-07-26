"""Floating chat panel next to the avatar overlay.

- Opened from overlay menu / double-click / tray.
- User message → request_user_message signal.
- Outside click hides the panel (see app event filter).
- Appears in Alt+Tab (Qt.Window, not Tool).
- Frameless glass panel; resizable via window edges/corners.
- Composer height adjustable (splitter); Send has a tools dropdown.
- Messages render fenced code blocks and local/remote images.
"""
from __future__ import annotations

import html
import logging
import re
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextImageFormat,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# Resize hit-zone width (px) on frameless edges/corners
_RESIZE_MARGIN = 8
_MIN_W, _MIN_H = 340, 260
_MAX_IMAGE_W = 360
_MAX_IMAGE_H = 280
_INPUT_MIN_H = 52
_INPUT_MAX_H = 220
_INPUT_DEFAULT_H = 88

# Glass opacity (0 = very transparent, 1 = solid). Clamped when applied.
_GLASS_OPACITY_MIN = 0.40
_GLASS_OPACITY_MAX = 1.0
_GLASS_OPACITY_DEFAULT = 0.88

# Glass palette
_ACCENT = "#6ea8fe"
_ACCENT_HOVER = "#8bbcff"
_TEXT = "#eef0f6"


def _clamp_glass_opacity(value: float | int | None) -> float:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        v = _GLASS_OPACITY_DEFAULT
    return max(_GLASS_OPACITY_MIN, min(_GLASS_OPACITY_MAX, v))

# Markdown-ish extractors (optional newline after language tag)
_FENCE_RE = re.compile(
    r"```([^\n`]*)\r?\n(.*?)```",
    re.DOTALL,
)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]\(([^)]+)\)",
)
# Bare local image / media paths (Windows + unix)
_BARE_IMAGE_RE = re.compile(
    r"(?P<path>"
    r"(?:[A-Za-z]:[\\/][^\s<>\"']+|(?:~/|/|\\\\)[^\s<>\"']+)"
    r"\.(?:png|jpe?g|gif|webp|bmp|svg)"
    r")",
    re.IGNORECASE,
)
_URL_IMAGE_RE = re.compile(
    r"(?P<url>https?://[^\s<>\"']+\.(?:png|jpe?g|gif|webp|bmp)(?:\?[^\s<>\"']*)?)",
    re.IGNORECASE,
)


def _is_image_path(path: str) -> bool:
    try:
        p = Path(path.strip().strip("\"'")).expanduser()
        if not p.is_file():
            return False
        return p.suffix.lower() in {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
        }
    except Exception:
        return False


def _load_pixmap(path: str, max_w: int = _MAX_IMAGE_W, max_h: int = _MAX_IMAGE_H) -> QPixmap | None:
    try:
        p = Path(path.strip().strip("\"'")).expanduser()
        if not p.is_file():
            return None
        pix = QPixmap(str(p))
        if pix.isNull():
            return None
        if pix.width() > max_w or pix.height() > max_h:
            pix = pix.scaled(
                max_w,
                max_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return pix
    except Exception as exc:
        logger.debug("image load failed %s: %s", path, exc)
        return None


def _glass_edit_qss(*, padding: int = 10, opacity: float = _GLASS_OPACITY_DEFAULT) -> str:
    """Stylesheet for history/input; ``opacity`` = solidness (1 = opaque)."""
    o = _clamp_glass_opacity(opacity)
    # Dark base gets more solid as opacity rises; slight white veil on top.
    bg_a = int(28 + 200 * o)  # ~108..228
    bg_focus_a = min(255, bg_a + 18)
    border_a = int(22 + 50 * o)
    handle_a = int(36 + 50 * o)
    return (
        "QTextEdit {"
        f"  background-color: rgba(20, 22, 36, {bg_a});"
        f"  color: {_TEXT};"
        f"  border: 1px solid rgba(255, 255, 255, {border_a});"
        "  border-radius: 14px;"
        f"  padding: {padding}px;"
        "  selection-background-color: rgba(110, 168, 254, 120);"
        "  selection-color: white;"
        "}"
        "QTextEdit:focus {"
        f"  border: 1px solid rgba(110, 168, 254, {min(200, border_a + 80)});"
        f"  background-color: rgba(22, 24, 40, {bg_focus_a});"
        "}"
        "QScrollBar:vertical {"
        "  background: transparent;"
        "  width: 8px;"
        "  margin: 6px 2px 6px 0;"
        "}"
        "QScrollBar::handle:vertical {"
        f"  background: rgba(255, 255, 255, {handle_a});"
        "  border-radius: 4px;"
        "  min-height: 28px;"
        "}"
        "QScrollBar::handle:vertical:hover {"
        f"  background: rgba(255, 255, 255, {min(255, handle_a + 40)});"
        "}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
        "  height: 0;"
        "}"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {"
        "  background: transparent;"
        "}"
    )


def _glass_card_qss(opacity: float = _GLASS_OPACITY_DEFAULT) -> str:
    o = _clamp_glass_opacity(opacity)
    bg_a = int(10 + 50 * o)
    border_a = int(16 + 30 * o)
    return (
        "#GlassCard {"
        f"  background-color: rgba(255, 255, 255, {bg_a});"
        f"  border: 1px solid rgba(255, 255, 255, {border_a});"
        "  border-radius: 16px;"
        "}"
    )


class _ScrollableHistory(QTextEdit):
    """Selectable history that renders rich HTML / images; open links on click."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setAcceptRichText(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        font = QFont()
        font.setPointSize(10)
        self.setFont(font)
        self.setStyleSheet(_glass_edit_qss(padding=12, opacity=_GLASS_OPACITY_DEFAULT))
        # code_id → source text (for 📋 Copy links)
        self.code_blocks: dict[int, str] = {}
        self._copy_flash_until: float = 0.0

    def apply_glass_opacity(self, opacity: float) -> None:
        self.setStyleSheet(_glass_edit_qss(padding=12, opacity=opacity))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        try:
            pos = event.position().toPoint()
        except Exception:
            pos = event.pos()
        anchor = self.anchorAt(pos)
        if anchor:
            if anchor.startswith("avatar-copy:"):
                self._copy_code(anchor)
                event.accept()
                return
            QDesktopServices.openUrl(QUrl(anchor))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _copy_code(self, anchor: str) -> None:
        try:
            cid = int(anchor.split(":", 1)[1])
        except (IndexError, ValueError):
            return
        code = self.code_blocks.get(cid)
        if code is None:
            return
        QGuiApplication.clipboard().setText(code)
        logger.info("copied code block id=%s (%d chars)", cid, len(code))
        panel = self.window()
        if isinstance(panel, QWidget) and hasattr(panel, "_flash_copied"):
            panel._flash_copied()  # type: ignore[attr-defined]


class _ChatInput(QTextEdit):
    """Multi-line input: Enter sends, Shift+Enter inserts newline; paste images.

    Height is not fixed — the parent splitter / size policy owns sizing.
    """

    submit_requested = Signal()
    image_pasted = Signal(str)  # local path after saving clipboard image

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setPlaceholderText("Message…  Enter to send · Shift+Enter for newline")
        self.setMinimumHeight(_INPUT_MIN_H)
        self.setMaximumHeight(_INPUT_MAX_H)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(_glass_edit_qss(padding=10, opacity=_GLASS_OPACITY_DEFAULT))

    def apply_glass_opacity(self, opacity: float) -> None:
        self.setStyleSheet(_glass_edit_qss(padding=10, opacity=opacity))

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
                return
            self.submit_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        if source is None:
            return
        if source.hasImage():
            img = source.imageData()
            if isinstance(img, QImage) and not img.isNull():
                path = self._save_clipboard_image(img)
                if path:
                    self.image_pasted.emit(path)
                    cursor = self.textCursor()
                    cursor.insertText(f"\n![pasted]({path})\n")
                    return
            if source.hasUrls():
                for url in source.urls():
                    local = url.toLocalFile()
                    if local and _is_image_path(local):
                        cursor = self.textCursor()
                        cursor.insertText(f"\n![image]({local})\n")
                        return
        if source.hasUrls():
            parts = []
            for url in source.urls():
                local = url.toLocalFile() or url.toString()
                if local:
                    parts.append(local)
            if parts:
                self.insertPlainText("\n".join(parts))
                return
        super().insertFromMimeData(source)

    @staticmethod
    def _save_clipboard_image(img: QImage) -> str | None:
        try:
            from .paths import user_config_dir

            out_dir = user_config_dir() / "pasted_images"
            out_dir.mkdir(parents=True, exist_ok=True)
            import time

            path = out_dir / f"paste_{int(time.time() * 1000)}.png"
            if img.save(str(path), "PNG"):
                return str(path)
        except Exception as exc:
            logger.warning("clipboard image save failed: %s", exc)
        return None


class _GlassCard(QFrame):
    """Semi-transparent rounded card used for the composer chrome."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("GlassCard")
        self.setStyleSheet(_glass_card_qss(_GLASS_OPACITY_DEFAULT))

    def apply_glass_opacity(self, opacity: float) -> None:
        self.setStyleSheet(_glass_card_qss(opacity))


class _EdgeResizeFilter(QObject):
    """Forward edge-resize mouse events from children to ChatPanel."""

    def __init__(self, panel: "ChatPanel") -> None:
        super().__init__(panel)
        self._panel = panel

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        et = event.type()
        if et not in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonRelease,
        ):
            return False
        if not isinstance(event, QMouseEvent):
            return False
        # Always track resize drag, even over children
        if self._panel._resizing:
            if et == QEvent.Type.MouseMove:
                self._panel._apply_resize(event.globalPosition().toPoint())
                return True
            if et == QEvent.Type.MouseButtonRelease:
                self._panel.mouseReleaseEvent(event)
                return True
        if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            edges = self._panel._hit_edges(event.globalPosition().toPoint())
            if edges:
                self._panel.mousePressEvent(event)
                return True
        if et == QEvent.Type.MouseMove and not (event.buttons() & Qt.MouseButton.LeftButton):
            edges = self._panel._hit_edges(event.globalPosition().toPoint())
            if edges:
                self._panel.setCursor(self._panel._cursor_for_edges(edges))
            elif obj is self._panel:
                self._panel.unsetCursor()
        return False


class ChatPanel(QWidget):
    """Floating chat panel shown next to the avatar."""

    request_user_message = Signal(str)
    request_close = Signal()
    geometry_changed = Signal()  # move/resize finished — app may persist

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Frameless normal window (not always-on-top — so Settings stays reachable).
        # Edge/corner resize is handled manually below.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
        )
        self.setWindowTitle("Nora — Chat")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.setMaximumSize(16777215, 16777215)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.resize(500, 460)
        self._glass_opacity = _GLASS_OPACITY_DEFAULT

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(10)

        # Header: title (drag) + close
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        self._header_title = "Nora"
        self.header = QLabel(self._header_title)
        self.header.setStyleSheet(
            f"color: {_TEXT}; font-weight: 600; font-size: 13px;"
            "padding: 6px 10px;"
            "background-color: rgba(255, 255, 255, 8);"
            "border: 1px solid rgba(255, 255, 255, 18);"
            "border-radius: 12px;"
            "letter-spacing: 0.3px;"
        )
        self.header.setCursor(Qt.CursorShape.SizeAllCursor)
        self.header.setMouseTracking(True)
        header_row.addWidget(self.header, 1)

        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(32, 32)
        self.close_btn.setToolTip("Close")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: rgba(255, 255, 255, 8);"
            "  color: rgba(255, 220, 220, 200);"
            "  border: 1px solid rgba(255, 255, 255, 16);"
            "  border-radius: 12px;"
            "  font-weight: 700;"
            "  font-size: 12px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(220, 70, 90, 200);"
            "  border-color: rgba(255, 120, 140, 120);"
            "  color: white;"
            "}"
        )
        self.close_btn.clicked.connect(self._on_close_clicked)
        header_row.addWidget(self.close_btn)
        root.addLayout(header_row)

        self._drag_pos: QPoint | None = None
        self.header.mousePressEvent = self._header_mouse_press  # type: ignore[method-assign]
        self.header.mouseMoveEvent = self._header_mouse_move  # type: ignore[method-assign]
        self.header.mouseReleaseEvent = self._header_mouse_release  # type: ignore[method-assign]

        # History + composer in a vertical splitter (drag handle = adjustable input height)
        self.history = _ScrollableHistory(self)
        self.history.setMouseTracking(True)
        self.history.setMinimumHeight(100)

        self._composer = _GlassCard(self)
        self._composer.setMinimumHeight(_INPUT_MIN_H + 58)
        composer_lay = QVBoxLayout(self._composer)
        composer_lay.setContentsMargins(10, 10, 10, 10)
        composer_lay.setSpacing(8)

        self.input = _ChatInput(self._composer)
        self.input.submit_requested.connect(self._on_submit)
        composer_lay.addWidget(self.input, 1)

        actions = QHBoxLayout()
        actions.setSpacing(0)
        actions.addStretch(1)

        # Split control: [ Send | ▾ ] — separate buttons (MenuButtonPopup is flaky
        # with custom stylesheets + our outside-click filter on Windows).
        _send_qss = (
            "QPushButton {"
            f"  background-color: {_ACCENT};"
            "  color: white;"
            "  border: none;"
            "  font-weight: 600;"
            "  font-size: 12px;"
            "}"
            f"QPushButton:hover {{ background-color: {_ACCENT_HOVER}; }}"
            "QPushButton:pressed { background-color: #5a94e8; }"
        )
        self.send_btn = QPushButton("Send", self._composer)
        self.send_btn.setToolTip("Send message (Enter)")
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.setMinimumHeight(38)
        self.send_btn.setMinimumWidth(88)
        self.send_btn.setStyleSheet(
            _send_qss
            + "QPushButton {"
            "  border-top-left-radius: 12px;"
            "  border-bottom-left-radius: 12px;"
            "  border-top-right-radius: 0;"
            "  border-bottom-right-radius: 0;"
            "  padding: 8px 18px;"
            "}"
        )
        self.send_btn.clicked.connect(self._on_submit)

        # ▾ = attach file in one click (no intermediate menu — avoids double-click UX)
        self.menu_btn = QPushButton("▾", self._composer)
        self.menu_btn.setToolTip("Attach file…")
        self.menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_btn.setFixedSize(36, 38)
        self.menu_btn.setStyleSheet(
            _send_qss
            + "QPushButton {"
            "  border-top-left-radius: 0;"
            "  border-bottom-left-radius: 0;"
            "  border-top-right-radius: 12px;"
            "  border-bottom-right-radius: 12px;"
            "  border-left: 1px solid rgba(255, 255, 255, 50);"
            "  padding: 8px 4px;"
            "  font-size: 14px;"
            "}"
        )
        self.menu_btn.clicked.connect(self._on_attach_file)
        self._tools_menu = None  # reserved if multi-tool menu returns later
        self._suppress_outside_hide = False

        actions.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        actions.addWidget(self.menu_btn, 0, Qt.AlignmentFlag.AlignBottom)
        composer_lay.addLayout(actions)

        self._splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._splitter.setObjectName("ChatSplitter")
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(8)
        self._splitter.setStyleSheet(
            "#ChatSplitter::handle {"
            "  background: transparent;"
            "  margin: 2px 40px;"
            "}"
            "#ChatSplitter::handle:hover {"
            "  background: rgba(110, 168, 254, 70);"
            "  border-radius: 3px;"
            "}"
            "#ChatSplitter::handle:pressed {"
            "  background: rgba(110, 168, 254, 120);"
            "  border-radius: 3px;"
            "}"
        )
        self._splitter.addWidget(self.history)
        self._splitter.addWidget(self._composer)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setSizes([320, _INPUT_DEFAULT_H + 56])
        root.addWidget(self._splitter, 1)

        self.hide()
        self._visible_history = False
        self._has_user_geometry = False
        self._img_counter = 0
        self._code_id = 0
        self._avatar_widget: QWidget | None = None

        # Frameless resize state
        self._resize_edges: Qt.Edge | int = 0
        self._resizing = False
        self._resize_origin = QPoint()
        self._resize_geom = QRect()
        self._geom_emit_timer = QTimer(self)
        self._geom_emit_timer.setSingleShot(True)
        self._geom_emit_timer.setInterval(200)
        self._geom_emit_timer.timeout.connect(self._emit_geometry_changed)

        # Catch edge hits even when cursor is over child widgets
        self._edge_filter = _EdgeResizeFilter(self)
        self.installEventFilter(self._edge_filter)
        for child in self.findChildren(QWidget):
            child.installEventFilter(self._edge_filter)
            child.setMouseTracking(True)

        # Hide when user activates another app / clicks the desktop
        self._deactivate_timer = QTimer(self)
        self._deactivate_timer.setSingleShot(True)
        self._deactivate_timer.setInterval(100)
        self._deactivate_timer.timeout.connect(self._hide_if_click_outside)

    def set_glass_opacity(self, opacity: float | int | None) -> None:
        """Set chat panel glass solidness (0.4–1.0). Higher = less transparent."""
        o = _clamp_glass_opacity(opacity)
        prev = getattr(self, "_glass_opacity", None)
        self._glass_opacity = o
        try:
            self.history.apply_glass_opacity(o)
        except Exception:
            pass
        try:
            self.input.apply_glass_opacity(o)
        except Exception:
            pass
        try:
            self._composer.apply_glass_opacity(o)
        except Exception:
            pass
        # Header / close chrome scales with opacity too
        header_bg = int(8 + 40 * o)
        header_border = int(14 + 28 * o)
        self.header.setStyleSheet(
            f"color: {_TEXT}; font-weight: 600; font-size: 13px;"
            "padding: 6px 10px;"
            f"background-color: rgba(255, 255, 255, {header_bg});"
            f"border: 1px solid rgba(255, 255, 255, {header_border});"
            "border-radius: 12px;"
            "letter-spacing: 0.3px;"
        )
        close_bg = int(8 + 30 * o)
        self.close_btn.setStyleSheet(
            "QPushButton {"
            f"  background-color: rgba(255, 255, 255, {close_bg});"
            "  color: rgba(255, 220, 220, 200);"
            f"  border: 1px solid rgba(255, 255, 255, {header_border});"
            "  border-radius: 12px;"
            "  font-weight: 700;"
            "  font-size: 12px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(220, 70, 90, 200);"
            "  border-color: rgba(255, 120, 140, 120);"
            "  color: white;"
            "}"
        )
        self.update()
        if prev is None or abs(float(prev) - o) >= 0.001:
            logger.info("chat glass_opacity → %.2f", o)

    def glass_opacity(self) -> float:
        return float(getattr(self, "_glass_opacity", _GLASS_OPACITY_DEFAULT))

    def paintEvent(self, event) -> None:  # noqa: N802
        """Frosted glass shell; alpha driven by ``_glass_opacity``."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        o = _clamp_glass_opacity(getattr(self, "_glass_opacity", _GLASS_OPACITY_DEFAULT))
        # Body alpha: was ~205–220 at default glass; scale with solidness.
        a_top = int(120 + 135 * o)      # 174..255
        a_mid = int(110 + 145 * o)      # 168..255
        a_bot = int(130 + 125 * o)      # 180..255
        border_a = int(28 + 50 * o)
        shadow_a = int(30 + 40 * o)
        hi_a = int(18 + 28 * o)

        rect = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 18, 18)

        # Soft outer shadow-ish edge
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, shadow_a))
        painter.drawRoundedRect(self.rect().adjusted(3, 4, -1, -1), 18, 18)

        # Main glass body
        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad.setColorAt(0.0, QColor(32, 36, 54, a_top))
        grad.setColorAt(0.45, QColor(22, 24, 38, a_mid))
        grad.setColorAt(1.0, QColor(16, 18, 28, a_bot))
        painter.setBrush(grad)
        painter.setPen(QPen(QColor(255, 255, 255, border_a), 1.0))
        painter.drawPath(path)

        # Top specular highlight (softer when more opaque)
        hi = QRect(rect.left() + 10, rect.top() + 2, rect.width() - 20, 22)
        hi_grad = QLinearGradient(hi.topLeft(), hi.bottomLeft())
        hi_grad.setColorAt(0.0, QColor(255, 255, 255, hi_a))
        hi_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(hi_grad)
        painter.setClipPath(path)
        painter.drawRect(hi)
        painter.setClipping(False)

    # ------------------------------------------------------------- drag handle
    def _header_mouse_press(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            # Don't start move when on resize edge of window
            if self._hit_edges(event.globalPosition().toPoint()):
                event.ignore()
                return
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def _header_mouse_move(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def _header_mouse_release(self, event: QMouseEvent) -> None:
        self._drag_pos = None
        self._has_user_geometry = True
        self.geometry_changed.emit()
        event.accept()

    # ------------------------------------------------------------- frameless resize
    def _hit_edges(self, global_pos: QPoint) -> Qt.Edge:
        """Return which edges are near the cursor (window coords)."""
        geo = self.frameGeometry()
        x, y = global_pos.x(), global_pos.y()
        m = _RESIZE_MARGIN
        edges = Qt.Edge(0)
        if x <= geo.left() + m:
            edges |= Qt.Edge.LeftEdge
        if x >= geo.right() - m:
            edges |= Qt.Edge.RightEdge
        if y <= geo.top() + m:
            edges |= Qt.Edge.TopEdge
        if y >= geo.bottom() - m:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _cursor_for_edges(self, edges: Qt.Edge) -> Qt.CursorShape:
        left = bool(edges & Qt.Edge.LeftEdge)
        right = bool(edges & Qt.Edge.RightEdge)
        top = bool(edges & Qt.Edge.TopEdge)
        bottom = bool(edges & Qt.Edge.BottomEdge)
        if (left and top) or (right and bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if (right and top) or (left and bottom):
            return Qt.CursorShape.SizeBDiagCursor
        if left or right:
            return Qt.CursorShape.SizeHorCursor
        if top or bottom:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            edges = self._hit_edges(event.globalPosition().toPoint())
            if edges:
                self._resizing = True
                self._resize_edges = edges
                self._resize_origin = event.globalPosition().toPoint()
                self._resize_geom = QRect(self.geometry())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        gp = event.globalPosition().toPoint()
        if self._resizing and event.buttons() & Qt.MouseButton.LeftButton:
            self._apply_resize(gp)
            event.accept()
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            edges = self._hit_edges(gp)
            self.setCursor(self._cursor_for_edges(edges))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._resizing:
            self._resizing = False
            self._resize_edges = Qt.Edge(0)
            self._has_user_geometry = True
            self.geometry_changed.emit()
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _apply_resize(self, global_pos: QPoint) -> None:
        geo = QRect(self._resize_geom)
        dx = global_pos.x() - self._resize_origin.x()
        dy = global_pos.y() - self._resize_origin.y()
        edges = self._resize_edges

        if edges & Qt.Edge.LeftEdge:
            new_x = geo.x() + dx
            new_w = geo.width() - dx
            if new_w >= _MIN_W:
                geo.setX(new_x)
                geo.setWidth(new_w)
        if edges & Qt.Edge.RightEdge:
            new_w = geo.width() + dx
            if new_w >= _MIN_W:
                geo.setWidth(new_w)
        if edges & Qt.Edge.TopEdge:
            new_y = geo.y() + dy
            new_h = geo.height() - dy
            if new_h >= _MIN_H:
                geo.setY(new_y)
                geo.setHeight(new_h)
        if edges & Qt.Edge.BottomEdge:
            new_h = geo.height() + dy
            if new_h >= _MIN_H:
                geo.setHeight(new_h)

        self.setGeometry(geo)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.isVisible():
            self._has_user_geometry = True
            self._geom_emit_timer.start()

    def _emit_geometry_changed(self) -> None:
        self.geometry_changed.emit()

    def _on_close_clicked(self) -> None:
        self.hide_panel()
        self.request_close.emit()

    # ------------------------------------------------------------- public API
    def add_user_message(self, text: str) -> None:
        logger.info("panel.add_user_message: %s", (text or "")[:60])
        self._append_message("You", text, "#9ec5ff")
        self._visible_history = True

    def add_assistant_message(self, text: str) -> None:
        self._append_message("Ava", text, "#f0c8a0")
        self._visible_history = True

    def add_system_message(self, text: str) -> None:
        self._append_message("·", text, "#a0a0b0")

    def apply_saved_geometry(self, x: int, y: int, w: int, h: int) -> None:
        """Restore last size/position (double-click reopen)."""
        w = max(self.minimumWidth(), int(w))
        h = max(self.minimumHeight(), int(h))
        self.setGeometry(int(x), int(y), w, h)
        self._has_user_geometry = True

    def place_near_avatar(self, avatar_rect: QRect, screen_geo: QRect) -> None:
        """First-open layout next to avatar when no saved geometry."""
        panel_w = max(self.width(), 480)
        panel_h = max(self.height(), max(280, avatar_rect.height()))
        px = avatar_rect.x() + avatar_rect.width() + 12
        py = avatar_rect.y()
        if px + panel_w > screen_geo.right():
            px = max(screen_geo.left(), avatar_rect.x() - panel_w - 12)
        if py + panel_h > screen_geo.bottom():
            py = max(screen_geo.top(), screen_geo.bottom() - panel_h)
        if py < screen_geo.top():
            py = screen_geo.top()
        self.setGeometry(px, py, panel_w, panel_h)

    def set_avatar_widget(self, avatar: QWidget | None) -> None:
        """Used to ignore outside-click hide when the cursor is on the mascot."""
        self._avatar_widget = avatar

    def show_panel(self, focus_input: bool = True) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        if focus_input:
            self.input.setFocus()

    def hide_panel(self) -> None:
        self._deactivate_timer.stop()
        if self.isVisible():
            self._has_user_geometry = True
            self.geometry_changed.emit()
        self.hide()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        # Clicks on the desktop / other apps never reach QApplication.notify;
        # ActivationChange is how we detect "clicked outside the app".
        # Opening the Send ▾ QMenu also steals activation — ignore that case.
        if event.type() == QEvent.Type.ActivationChange and self.isVisible():
            if not self.isActiveWindow():
                self._deactivate_timer.start()

    def _panel_owns_active_popup(self) -> bool:
        """True while a QMenu (or other popup) parented under this panel is open."""
        try:
            from PySide6.QtWidgets import QApplication, QMenu

            popup = QApplication.activePopupWidget()
            if popup is not None:
                w: QWidget | None = popup
                while w is not None:
                    if w is self:
                        return True
                    w = w.parentWidget()
            for top in QApplication.topLevelWidgets():
                if isinstance(top, QMenu) and top.isVisible():
                    w = top
                    while w is not None:
                        if w is self:
                            return True
                        w = w.parentWidget()
        except Exception:
            pass
        return False

    def _hide_if_click_outside(self) -> None:
        if not self.isVisible():
            return
        if self.isActiveWindow():
            return
        # Tools menu open / just closed — do not auto-hide.
        if self.should_suppress_outside_hide():
            return
        pos = QCursor.pos()
        if self.frameGeometry().contains(pos):
            return
        av = self._avatar_widget
        if av is not None and av.isVisible() and av.frameGeometry().contains(pos):
            return
        logger.info("chat hide: click/focus outside panel and avatar")
        self.hide_panel()
        self.request_close.emit()

    def _flash_copied(self) -> None:
        self.header.setText("Copied ✓")
        QTimer.singleShot(1200, lambda: self.header.setText(self._header_title))

    def has_user_geometry(self) -> bool:
        return self._has_user_geometry

    def geometry_dict(self) -> dict[str, int]:
        g = self.geometry()
        return {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()}

    # ------------------------------------------------------------- rich message
    def _append_message(self, who: str, text: str, color: str) -> None:
        cursor = self.history.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        who_fmt = QTextCharFormat()
        who_fmt.setForeground(QColor(color))
        who_fmt.setFontWeight(QFont.Weight.Bold)
        cursor.setCharFormat(who_fmt)
        cursor.insertText(f"{who}:\n")

        self._insert_rich_body(cursor, text or "")

        cursor.insertBlock()
        cursor.setCharFormat(QTextCharFormat())
        cursor.insertText("")  # spacer between messages

        sb = self.history.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _insert_rich_body(self, cursor: QTextCursor, text: str) -> None:
        """Insert body with fenced code, markdown images, bare paths, plain text."""
        if not text:
            return
        # Normalize newlines (Hermes / Windows may send CRLF)
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        # Split into fenced code vs non-code segments
        pos = 0
        for m in _FENCE_RE.finditer(text):
            if m.start() > pos:
                self._insert_text_with_images(cursor, text[pos : m.start()])
            lang = (m.group(1) or "").strip()
            code = m.group(2)
            if code.endswith("\n"):
                code = code[:-1]
            self._insert_code_block(cursor, code, lang)
            pos = m.end()
        if pos < len(text):
            self._insert_text_with_images(cursor, text[pos:])
        elif pos == 0:
            self._insert_text_with_images(cursor, text)

    def _insert_code_block(self, cursor: QTextCursor, code: str, lang: str = "") -> None:
        """Insert a code box with real newlines + 📋 Copy link.

        QTextEdit's HTML importer collapses whitespace / ignores pre-wrap, so
        we insert each line as its own text block with a dark background.
        """
        code = (code or "").replace("\r\n", "\n").replace("\r", "\n")
        self._code_id += 1
        cid = self._code_id
        self.history.code_blocks[cid] = code

        label = html.escape(lang) if lang else "code"
        # Header row: language + copy action (clickable anchor)
        cursor.insertHtml(
            f'<p style="background-color:#1a1a28; margin-top:6px; margin-bottom:0;">'
            f'<span style="color:#8ab4f8; font-size:9pt;">{label}</span>'
            f'&nbsp;&nbsp;'
            f'<a href="avatar-copy:{cid}" style="color:#9ec5ff; '
            f'text-decoration:none; font-size:9pt;">📋 Copy</a>'
            f"</p>"
        )

        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)

        char_fmt = QTextCharFormat()
        char_fmt.setFont(mono)
        char_fmt.setForeground(QColor("#d4d4d4"))
        char_fmt.setBackground(QColor("#1a1a28"))

        block_fmt = QTextBlockFormat()
        block_fmt.setBackground(QColor("#1a1a28"))
        block_fmt.setLeftMargin(10)
        block_fmt.setRightMargin(10)
        block_fmt.setTopMargin(0)
        block_fmt.setBottomMargin(0)

        lines = code.split("\n")
        if not lines:
            lines = [""]
        for line in lines:
            cursor.insertBlock(block_fmt)
            cursor.setCharFormat(char_fmt)
            # Empty line still needs a character so the block keeps height
            cursor.insertText(line if line else "\u00a0")

        # Reset formatting for following content
        reset_block = QTextBlockFormat()
        cursor.insertBlock(reset_block)
        cursor.setCharFormat(QTextCharFormat())

    def _insert_text_with_images(self, cursor: QTextCursor, text: str) -> None:
        """Plain/inline-code segments + markdown/bare/url images."""
        # Tokenize by markdown images, bare paths, http image urls
        tokens: list[tuple[str, str, str]] = []  # kind, payload, alt

        # Collect all match spans
        spans: list[tuple[int, int, str, str, str]] = []
        for m in _MD_IMAGE_RE.finditer(text):
            spans.append((m.start(), m.end(), "md_img", m.group(2).strip(), m.group(1)))
        for m in _BARE_IMAGE_RE.finditer(text):
            spans.append((m.start(), m.end(), "path", m.group("path"), ""))
        for m in _URL_IMAGE_RE.finditer(text):
            spans.append((m.start(), m.end(), "url", m.group("url"), ""))

        # Drop overlapping (prefer earlier / longer)
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
        kept: list[tuple[int, int, str, str, str]] = []
        last_end = -1
        for s in spans:
            if s[0] >= last_end:
                kept.append(s)
                last_end = s[1]
        kept.sort(key=lambda s: s[0])

        pos = 0
        for start, end, kind, payload, alt in kept:
            if start > pos:
                self._insert_plain_with_inline_code(cursor, text[pos:start])
            if kind in ("md_img", "path"):
                self._insert_image(cursor, payload, alt or payload)
            elif kind == "url":
                # Try as remote: show link; QTextEdit won't fetch remote reliably offline
                self._insert_image_or_link(cursor, payload, alt or payload)
            pos = end
        if pos < len(text):
            self._insert_plain_with_inline_code(cursor, text[pos:])
        elif not kept:
            self._insert_plain_with_inline_code(cursor, text)

    def _insert_plain_with_inline_code(self, cursor: QTextCursor, text: str) -> None:
        if not text:
            return
        pos = 0
        for m in _INLINE_CODE_RE.finditer(text):
            if m.start() > pos:
                self._insert_plain(cursor, text[pos : m.start()])
            self._insert_inline_code(cursor, m.group(1))
            pos = m.end()
        if pos < len(text):
            self._insert_plain(cursor, text[pos:])
        elif pos == 0:
            self._insert_plain(cursor, text)

    def _insert_plain(self, cursor: QTextCursor, text: str) -> None:
        body_fmt = QTextCharFormat()
        body_fmt.setForeground(QColor("#e8e8f0"))
        cursor.setCharFormat(body_fmt)
        cursor.insertText(text)

    def _insert_inline_code(self, cursor: QTextCursor, code: str) -> None:
        escaped = html.escape(code)
        cursor.insertHtml(
            f'<span style="background-color:#2a2a3c; color:#e6db74; '
            f'font-family:Consolas,Courier New,monospace; padding:1px 4px;">'
            f"{escaped}</span>"
        )

    def _insert_image(self, cursor: QTextCursor, path: str, alt: str = "") -> None:
        pix = _load_pixmap(path)
        if pix is None:
            # Fallback: clickable path text
            safe = html.escape(path)
            cursor.insertHtml(
                f'<p style="color:#9ec5ff;"><a href="file:///{html.escape(path.replace(chr(92), "/"))}">'
                f"[image: {safe}]</a></p>"
            )
            return
        self._img_counter += 1
        name = f"img_{self._img_counter}"
        url = QUrl(f"avatar-img:{name}")
        self.history.document().addResource(
            QTextDocument.ResourceType.ImageResource,
            url,
            pix,
        )
        fmt = QTextImageFormat()
        fmt.setName(url.toString())
        fmt.setWidth(pix.width())
        fmt.setHeight(pix.height())
        cursor.insertImage(fmt)
        cursor.insertBlock()
        # Caption / open path
        cap = html.escape(alt or Path(path).name)
        file_url = Path(path).expanduser().resolve().as_uri()
        cursor.insertHtml(
            f'<p style="color:#8888a0; font-size:8pt;">'
            f'<a href="{html.escape(file_url)}">{cap}</a></p>'
        )

    def _insert_image_or_link(self, cursor: QTextCursor, url: str, alt: str = "") -> None:
        # Prefer showing as link (no network fetch in UI thread by default)
        safe = html.escape(url)
        label = html.escape(alt or url)
        cursor.insertHtml(
            f'<p><a href="{safe}" style="color:#8ab4f8;">{label}</a></p>'
        )

    def should_suppress_outside_hide(self) -> bool:
        """Used by the app focus filter while tools menu is open / finishing."""
        return bool(getattr(self, "_suppress_outside_hide", False)) or self._panel_owns_active_popup()

    def _end_outside_hide_suppress(self) -> None:
        self._suppress_outside_hide = False
        if self.isVisible():
            try:
                self.raise_()
                self.activateWindow()
                self.input.setFocus()
            except Exception:
                pass

    def _on_attach_file(self) -> None:
        """Pick local file(s) and insert references into the input (one click from ▾).

        Images → markdown ``![name](path)`` (vision-capable turns).
        Other files → ``[attached file: path]`` so Hermes sees the path in text.
        """
        self._suppress_outside_hide = True
        self._deactivate_timer.stop()
        if not self.isVisible():
            self.show()
            self.raise_()
        try:
            paths, _filter = QFileDialog.getOpenFileNames(
                self,
                "Attach file",
                "",
                "All files (*.*);;"
                "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;"
                "Text (*.txt *.md *.json *.csv *.log *.py *.js *.ts *.html *.css);;"
                "Documents (*.pdf *.doc *.docx)",
            )
        except Exception:
            logger.exception("attach file dialog failed")
            paths = []
        finally:
            QTimer.singleShot(300, self._end_outside_hide_suppress)

        if not paths:
            logger.info("attach file cancelled")
            return

        chunks: list[str] = []
        for raw in paths:
            try:
                p = Path(raw).expanduser().resolve()
            except Exception:
                p = Path(raw)
            if not p.is_file():
                logger.warning("attach skipped (not a file): %s", raw)
                continue
            path_s = str(p)
            if _is_image_path(path_s):
                chunks.append(f"![{p.name}]({path_s})")
            else:
                chunks.append(f"[attached file: {path_s}]")

        if not chunks:
            self.add_system_message("[no valid files selected]")
            return

        cursor = self.input.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        block = "\n" + "\n".join(chunks) + "\n"
        existing = self.input.toPlainText()
        if existing and not existing.endswith("\n"):
            block = "\n" + block
        cursor.insertText(block)
        self.input.setFocus()
        logger.info("attached %d file(s) to input", len(chunks))

    def _on_submit(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self.request_user_message.emit(text)

"""Desktop screenshot capture for the avatar shell.

Hermes gateway may not expose computer-use tools on the API channel.
The desktop client can still grab the screen with Qt and send the image
to Hermes (path in chat + optional multimodal vision payload).
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Strict: user wants a *live* look at the screen (not mere mention of screenshots).
_LIVE_VIEW_INTENT = re.compile(
    r"("
    r"what(?:'s|\s+is)\s+on\s+(?:my\s+)?screen|"
    r"what\s+do\s+you\s+see\s+on\s+(?:my\s+)?screen|"
    r"look\s+at\s+(?:my\s+)?screen|"
    r"see\s+(?:my\s+)?screen|"
    r"capture\s+(?:my\s+)?(?:screen|desktop)|"
    r"take\s+a\s+screenshot"
    r")",
    re.IGNORECASE,
)


def message_wants_live_view(text: str) -> bool:
    """True if the user is asking for a live view of the desktop right now."""
    if not (text or "").strip():
        return False
    return bool(_LIVE_VIEW_INTENT.search(text))


def screenshots_dir() -> Path:
    from .paths import user_config_dir

    d = user_config_dir() / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def capture_desktop(
    *,
    all_monitors: bool = True,
    max_width: int = 1920,
) -> str | None:
    """Grab the desktop and save a PNG. Must run on the Qt GUI thread.

    Returns absolute path, or None on failure.
    """
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPixmap
    except Exception as exc:
        logger.warning("Qt not available for screenshot: %s", exc)
        return None

    app = QGuiApplication.instance()
    if app is None:
        logger.warning("screenshot: no QGuiApplication")
        return None

    screens = QGuiApplication.screens()
    if not screens:
        logger.warning("screenshot: no screens")
        return None

    try:
        if not all_monitors or len(screens) == 1:
            screen = QGuiApplication.primaryScreen() or screens[0]
            pix = screen.grabWindow(0)
        else:
            # Union of all monitor geometries
            from PySide6.QtCore import QRect

            bounds = QRect()
            grabs: list[tuple] = []
            for sc in screens:
                g = sc.geometry()
                bounds = bounds.united(g)
                grabs.append((sc.grabWindow(0), g))
            if bounds.isEmpty():
                return None
            canvas = QPixmap(bounds.width(), bounds.height())
            canvas.fill(Qt.GlobalColor.black)
            painter = QPainter(canvas)
            for grab, g in grabs:
                painter.drawPixmap(g.x() - bounds.x(), g.y() - bounds.y(), grab)
            painter.end()
            pix = canvas

        if pix.isNull():
            logger.warning("screenshot: empty pixmap")
            return None

        if max_width > 0 and pix.width() > max_width:
            pix = pix.scaledToWidth(
                max_width,
                Qt.TransformationMode.SmoothTransformation,
            )

        out = screenshots_dir() / f"desktop_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.png"
        if not pix.save(str(out), "PNG"):
            # Fallback via QImage
            img: QImage = pix.toImage()
            if not img.save(str(out), "PNG"):
                logger.warning("screenshot: save failed %s", out)
                return None
        logger.info("screenshot saved %s (%dx%d)", out, pix.width(), pix.height())
        return str(out.resolve())
    except Exception as exc:
        logger.exception("screenshot capture failed: %s", exc)
        return None


def image_to_data_url(path: str, *, max_side: int = 1600) -> str | None:
    """Load image file → ``data:image/png;base64,...`` for vision chat APIs."""
    import base64
    import io

    try:
        p = Path(path)
        if not p.is_file():
            return None
        # Prefer Pillow if present (resize); else raw file bytes
        try:
            from PIL import Image

            im = Image.open(p)
            im = im.convert("RGB")
            w, h = im.size
            scale = min(1.0, float(max_side) / max(w, h))
            if scale < 1.0:
                im = im.resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    Image.Resampling.LANCZOS,
                )
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
        except ImportError:
            raw = p.read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
            suffix = p.suffix.lower().lstrip(".") or "png"
            if suffix == "jpg":
                suffix = "jpeg"
            return f"data:image/{suffix};base64,{b64}"
    except Exception as exc:
        logger.warning("image_to_data_url failed %s: %s", path, exc)
        return None

"""Video player — wraps OpenCV frame extraction as a Qt-friendly iterator.

Reads a single mp4 file with cv2.VideoCapture and yields frames as QImage
(or QPixmap) so a Qt widget can paint them at a given FPS.

The player is intentionally minimal: no seeking, no audio.  For the
avatar's idle preview we only need to play a clip end-to-end and pick
the next animation when it finishes.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtGui import QImage, QPixmap

logger = logging.getLogger(__name__)


class VideoPlayer:
    """Sequential frame reader for a single video file.

    Usage::

        player = VideoPlayer(path)
        while player.is_open():
            img = player.next_frame()
            if img is None:
                break
            # paint img onto a Qt widget
        player.release()
    """

    def __init__(self, path: Path, target_fps: int = 24) -> None:
        self.path = Path(path)
        self.target_fps = max(1, target_fps)
        self._cap: cv2.VideoCapture | None = None
        self._native_fps: float = 24.0
        self._frame_count: int = 0
        self._frame_idx: int = 0
        self._open()

    def _open(self) -> None:
        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            logger.error("VideoPlayer: cannot open %s", self.path)
            self._cap = None
            return
        self._cap = cap
        self._native_fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
        self._frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    # ------------------------------------------------------------------ accessors
    @property
    def native_fps(self) -> float:
        return self._native_fps

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def frame_step(self) -> int:
        """How many native frames to advance per tick at target_fps.

        Returns 1 (read every frame) if native_fps <= target_fps, else
        the integer stride to roughly match target_fps.
        """
        if self._native_fps <= self.target_fps:
            return 1
        return max(1, round(self._native_fps / self.target_fps))

    # ------------------------------------------------------------------ frame API
    def next_frame(self) -> QImage | None:
        """Advance and decode the next frame.  Returns None on EOF/error."""
        if not self.is_open():
            return None
        step = self.frame_step()
        # skip frames if native fps exceeds target
        if step > 1:
            for _ in range(step - 1):
                if not self._cap.grab():
                    return None
            ok = self._cap.grab()
            if not ok:
                return None
            ok, frame = self._cap.retrieve()
        else:
            ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        self._frame_idx += step
        return self._to_qimage(frame)

    def reset(self) -> None:
        if self._cap is not None:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self._frame_idx = 0

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    # ------------------------------------------------------------------ conversion
    @staticmethod
    def _to_qimage(frame: np.ndarray) -> QImage:
        """cv2 BGR ndarray -> QImage (RGB888).

        The frame is assumed to already be BGR.  We swap channels to RGB
        for Qt.  Width/height are taken from the array shape.
        """
        h, w = frame.shape[:2]
        # BGR -> RGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        bytes_per_line = 3 * w
        return QImage(
            rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888
        ).copy()  # copy so the numpy buffer can be freed

    def next_pixmap(self) -> QPixmap | None:
        img = self.next_frame()
        return QPixmap.fromImage(img) if img is not None else None

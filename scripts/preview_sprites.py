"""Sprite preview — quick viewer for the generated sprite packs.

Runs a small PySide6 window that cycles through the mascot/ frames for
the currently picked IdleAnimation.  Useful for sanity-checking the
weighted random picker's behaviour without launching the full avatar.

Run: ``.venv/Scripts/python.exe scripts/preview_sprites.py``
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

# Allow running directly from scripts/ without installing the package.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from avatar.sprite_packs import build_nora_animator_from_mascot, frames_for

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


class PreviewWindow(QWidget):
    FPS = 24

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Hermes Desktop Avatar — Sprite Preview")
        self.resize(360, 600)

        layout = QVBoxLayout(self)
        self.label = QLabel("(no sprite)")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("background: #1e1e1e;")
        layout.addWidget(self.label)

        self.animator = build_nora_animator_from_mascot()
        self.current_pick = None
        self.current_frames: list[Path] = []
        self.frame_idx = 0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    def _tick(self) -> None:
        if not self.current_frames:
            # pick a new animation
            self.current_pick = self.animator.pick_random()
            self.current_frames = frames_for(self.current_pick.id)
            self.frame_idx = 0
            if self.current_frames:
                print(
                    f"[pick] {self.current_pick.id}  "
                    f"rarity={self.current_pick.rarity}  "
                    f"{len(self.current_frames)} frames"
                )
            self.setWindowTitle(f"Sprite Preview — {self.current_pick.id if self.current_pick else 'idle'}")
            return
        # advance to next frame; when we exhaust, schedule a new pick
        path = self.current_frames[self.frame_idx]
        pix = QPixmap(str(path))
        scaled = pix.scaled(
            self.label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.label.setPixmap(scaled)
        self.frame_idx += 1
        if self.frame_idx >= len(self.current_frames):
            self.current_frames = []  # triggers new pick next tick


def main() -> int:
    app = QApplication(sys.argv)
    win = PreviewWindow()
    win.show()
    win.timer.start(int(1000 / win.FPS))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

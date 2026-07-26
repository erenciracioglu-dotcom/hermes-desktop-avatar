"""Video-based sprite preview — runs the avatar's idle animator directly
against source videos.

This is the "use the videos we have" path: no PNG extraction, no chroma
key.  The avatar's overlay reads from a cv2.VideoCapture stream per
animation and advances frames at 24 fps.  When a clip finishes, the
idle animator picks the next weighted-random animation.

Run:
    .venv/Scripts/python.exe scripts/preview_videos.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QPalette, QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from avatar.idle_animator import IdleAnimator
from avatar.sprite_packs import list_video_animations, video_for
from avatar.video_player import VideoPlayer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("preview")


FPS = 24
WINDOW_W = 360
WINDOW_H = 600


class PreviewWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Hermes Desktop Avatar — Idle Animator Preview")
        self.resize(WINDOW_W, WINDOW_H)

        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
        self.setPalette(pal)
        self.setAutoFillBackground(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.sprite_label = QLabel("(loading)")
        self.sprite_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.sprite_label)

        self.info_label = QLabel("")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.info_label.setFont(font)
        self.info_label.setStyleSheet("color: #cccccc; background: #0a0a0a; padding: 6px;")
        layout.addWidget(self.info_label)

        self.animator: IdleAnimator = list_video_animations()
        logger.info("animator pool size: %d", self.animator.pool_size)
        self._picks = 0
        self._c_picks = 0
        self._t_start = None  # populated on first frame
        self._player: VideoPlayer | None = None
        self._current_anim = None
        # Tier-içi ardışık tekrar engelleme: her tier'ın (a/b/c)
        # son oynayan id'sini ayrı tut. Aradaki başka tier pick'leri
        # sayılmaz — örn. son b-tier b_jumping ise, arada 3 a-tier
        # oynasa bile bir sonraki b-tier b_jumping olamaz.
        self._last_per_tier: dict[float, str] = {}

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------ tick loop
    def _pick_next(self) -> None:
        """Pick a new animation and open its video.

        Tier-aware: enforces "no immediate repeat within the same
        tier".  The last pick from each tier is remembered separately,
        so other-tier picks in between don't reset the no-repeat
        constraint.
        """
        # Beklenen tier: önceki pick'in tier'ı (mevcut davranışı koru).
        # İlk pick'te None → sadece weighted random.
        expected_tier = self._current_anim.rarity if self._current_anim else None
        pick = self.animator.pick_tier_aware(expected_rarity=expected_tier)
        if pick is None:
            pick = self.animator.pick_random()
        self._current_anim = pick
        self._picks += 1
        if pick.rarity >= 20.0:
            self._c_picks += 1
        video_path = video_for(pick.id)
        if video_path is None:
            self.info_label.setText(
                f"#{self._picks}  {pick.id}  rarity={pick.rarity}  [NO VIDEO]"
            )
            self._player = None
            return
        if self._player is not None:
            self._player.release()
        self._player = VideoPlayer(video_path, target_fps=FPS)
        self.setWindowTitle(f"Idle Preview — {pick.id}")
        logger.info(
            "[pick #%d] %s  rarity=%.1f  video=%s  frames=%d",
            self._picks, pick.id, pick.rarity, video_path.name, self._player.frame_count,
        )

    def _tick(self) -> None:
        if self._player is None or not self._player.is_open():
            self._pick_next()
            return
        img = self._player.next_frame()
        if img is None:
            # clip finished → next pick
            self._pick_next()
            return
        pix = QPixmap.fromImage(img).scaled(
            WINDOW_H * 0.85, WINDOW_H * 0.85,  # square-ish area
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.sprite_label.setPixmap(pix)
        # info line
        self.info_label.setText(
            f"#{self._picks:3d}  rarity={self._current_anim.rarity:5.1f}  "
            f"c-tier={self._c_picks}  {self._current_anim.id}"
        )

    # ------------------------------------------------------------------ lifecycle
    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start(int(1000 / FPS))

    def closeEvent(self, event):
        self.timer.stop()
        if self._player is not None:
            self._player.release()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    win = PreviewWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication, QPixmap

from avatar.overlay import OverlayWindow


class OverlayFrameFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QGuiApplication.instance() or QGuiApplication([])

    def test_generated_idle_pixmaps_remain_available(self) -> None:
        overlay = SimpleNamespace()
        idle_frames = [QPixmap(64, 64)]

        OverlayWindow._ingest_loaded_frames(
            overlay,
            {
                "idle": idle_frames,
                "talk": idle_frames,
                "think": idle_frames,
            },
        )

        self.assertEqual(overlay._frames["idle"], idle_frames)
        self.assertTrue(overlay._ambient)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from avatar import paths


class UserConfigDirTests(unittest.TestCase):
    def test_macos_uses_application_support(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            with (
                patch.object(paths.sys, "platform", "darwin"),
                patch.object(paths.Path, "home", return_value=home),
            ):
                result = paths.user_config_dir()

            self.assertEqual(
                result,
                home / "Library" / "Application Support" / "hermes-desktop-avatar",
            )
            self.assertTrue(result.is_dir())

    def test_linux_keeps_dot_config_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            with (
                patch.object(paths.sys, "platform", "linux"),
                patch.object(paths.Path, "home", return_value=home),
            ):
                result = paths.user_config_dir()

            self.assertEqual(result, home / ".config" / "hermes-desktop-avatar")
            self.assertTrue(result.is_dir())

    def test_windows_keeps_appdata_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            appdata = Path(temp_dir) / "AppData" / "Roaming"
            with (
                patch.object(paths.sys, "platform", "win32"),
                patch.dict(os.environ, {"APPDATA": str(appdata)}),
            ):
                result = paths.user_config_dir()

            self.assertEqual(result, appdata / "hermes-desktop-avatar")
            self.assertTrue(result.is_dir())


if __name__ == "__main__":
    unittest.main()

"""Resolve project and user config paths."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def project_root() -> Path:
    """Repo root (contains assets/, config.default.json).

    Under PyInstaller --onefile, use sys._MEIPASS (the extracted temp root);
    ``__file__`` parents[2] points to the wrong place inside ``_MEI…``.
    """
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


def assets_dir() -> Path:
    return project_root() / "assets"


def sprites_dir() -> Path:
    return assets_dir() / "sprites" / "mascot"


def mascot_dir() -> Path:
    """Same as sprites_dir() — kept as a separate name for readability."""
    return sprites_dir()


def mascot_v2_dir() -> Path:
    """Preferred WebP clips for packing (Nora pipeline output)."""
    return assets_dir() / "sprites" / "mascot_v2"


def bundled_characters_dir() -> Path:
    """Shipped ``.hchar`` packs next to the app/repo assets."""
    return assets_dir() / "characters"


def user_characters_dir() -> Path:
    """User-installed character packs (import / download target)."""
    path = user_config_dir() / "characters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_config_path() -> Path:
    return project_root() / "config.default.json"


def user_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path.home() / ".config"
    path = base / "hermes-desktop-avatar"
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_config_path() -> Path:
    return user_config_dir() / "config.json"

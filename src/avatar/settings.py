"""Load / save user settings (JSON)."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from .paths import default_config_path, user_config_path


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Settings:
    """Thread-safe settings holder.

    Single-level access matches dict.get:
        settings.get("language", "tr")
        settings.get("character_id", "nora")

    Nested access uses path():
        settings.path("hermes", "gateway_url", default="")
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self.reload()

    # Keys dropped from the old desktop-commentator / bundled-server era.
    _LEGACY_TOP_LEVEL = frozenset({
        "enabled",
        "autostart",
        "interval_seconds",
        "interval_jitter_seconds",
        "commentator_enabled",
        "sources",
        "output_mode",
        "comment_provider",
        "prompts",
        "session",
        "xai",
        "tts",
        "lmstudio",
        "overlay",
        "capture",
    })
    _LEGACY_HERMES = frozenset({
        "port",
        "provider",
        "model",
        "base_url",
        # old name; migrated to auto_start below
        "auto_start_server",
    })

    def reload(self) -> None:
        with self._lock:
            defaults = self._read_json(default_config_path())
            user_path = user_config_path()
            if user_path.is_file():
                user = self._read_json(user_path)
                self._data = _deep_merge(defaults, user)
            else:
                self._data = defaults
                self.save()
            if self._strip_legacy_keys():
                self.save()

    def _strip_legacy_keys(self) -> bool:
        """Remove obsolete keys; return True if anything changed."""
        changed = False
        for key in list(self._data.keys()):
            if key in self._LEGACY_TOP_LEVEL:
                del self._data[key]
                changed = True
        hermes = self._data.get("hermes")
        if isinstance(hermes, dict):
            # Migrate legacy auto_start_server → auto_start before strip.
            if "auto_start_server" in hermes and "auto_start" not in hermes:
                hermes["auto_start"] = bool(hermes.get("auto_start_server"))
                changed = True
            for key in list(hermes.keys()):
                if key in self._LEGACY_HERMES:
                    del hermes[key]
                    changed = True
            # Migrate old local chat port shape → gateway defaults if missing.
            if not hermes.get("gateway_url"):
                hermes["gateway_url"] = "http://127.0.0.1:8642"
                changed = True
            if not hermes.get("session_id"):
                hermes["session_id"] = "avatar-nora"
                changed = True
            if "auto_start" not in hermes:
                hermes["auto_start"] = True
                changed = True
            if "auto_restart" not in hermes:
                hermes["auto_restart"] = True
                changed = True
            if "startup_timeout_seconds" not in hermes:
                hermes["startup_timeout_seconds"] = 60
                changed = True
            if "ensure_api_server" not in hermes:
                hermes["ensure_api_server"] = True
                changed = True
            if "write_hermes_env" not in hermes:
                hermes["write_hermes_env"] = True
                changed = True
        return changed

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Config must be an object: {path}")
        return data

    def save(self) -> None:
        with self._lock:
            path = user_config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
                f.write("\n")

    def raw(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        """dict.get compatible (top-level keys only)."""
        with self._lock:
            if key not in self._data:
                return default
            val = self._data[key]
            if val is None:
                return default
            return copy.deepcopy(val)

    def path(self, *keys: str, default: Any = None) -> Any:
        """Nested lookup: path('hermes', 'gateway_url', default='')."""
        with self._lock:
            node: Any = self._data
            for key in keys:
                if not isinstance(node, dict) or key not in node:
                    return default
                node = node[key]
            if node is None:
                return default
            return copy.deepcopy(node)

    def get_int(self, key: str, default: int = 0) -> int:
        val = self.get(key, default)
        try:
            return default if val is None else int(val)
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        val = self.get(key, default)
        try:
            return default if val is None else float(val)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        val = self.get(key, default)
        return default if val is None else bool(val)

    def get_str(self, key: str, default: str = "") -> str:
        val = self.get(key, default)
        return default if val is None else str(val)

    def set(self, *keys_and_value: Any, save: bool = True) -> None:
        """set('a', value) or set('a', 'b', value) for nested."""
        if len(keys_and_value) < 2:
            raise ValueError("Need at least one key and a value")
        *keys, value = keys_and_value
        with self._lock:
            node = self._data
            for key in keys[:-1]:
                child = node.setdefault(key, {})
                if not isinstance(child, dict):
                    raise TypeError(f"Cannot descend into non-dict at {key}")
                node = child
            node[keys[-1]] = value
            if save:
                self.save()

    def update_many(self, patch: dict[str, Any], save: bool = True) -> None:
        with self._lock:
            self._data = _deep_merge(self._data, patch)
            if save:
                self.save()

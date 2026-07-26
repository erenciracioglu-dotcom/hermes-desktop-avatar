"""Discover character packs (and optional built-in presets).

Search order (later overrides earlier on same ``id``):
  1. Built-in Python presets (``BUILTIN_PRESETS`` — currently empty)
  2. Bundled ``assets/characters/*.hchar`` and ``assets/characters/*/character.json``
  3. User ``%APPDATA%/hermes-desktop-avatar/characters/`` (same shapes)

Nora is the only shipped character (pack under ``assets/characters/``).
"""
from __future__ import annotations

import logging
from pathlib import Path

from .character_pack import (
    MANIFEST_NAME,
    PACK_EXT,
    CharacterPackError,
    load_preset_from_directory,
    load_preset_from_pack,
)
from .characters import BUILTIN_PRESETS, CharacterPreset
from .paths import bundled_characters_dir, user_characters_dir

logger = logging.getLogger(__name__)

_cache: list[CharacterPreset] | None = None


def _placeholder_preset() -> CharacterPreset:
    """Last-resort stub if no packs are installed (broken install)."""
    return CharacterPreset(
        id="missing",
        label="(no character pack)",
        sprite_map={"idle": [], "talk": [], "think": []},
        idle_ambient=[],
        source="builtin",
        version="0",
    )


def _iter_pack_entries() -> list[Path]:
    """Return pack directories and ``.hchar`` files.

    Order matters: later entries override the same ``id``. Bundled first,
    then user; within a root, directory packs first, then ``.hchar`` so a
    portable zip replaces a dev directory pack when both exist.
    """
    entries: list[Path] = []
    for root in (bundled_characters_dir(), user_characters_dir()):
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / MANIFEST_NAME).is_file():
                entries.append(child)
        entries.extend(sorted(root.glob(f"*{PACK_EXT}")))
    return entries


def scan_characters(*, force: bool = False) -> list[CharacterPreset]:
    """Return all known characters (built-ins + packs). Cached until force."""
    global _cache
    if _cache is not None and not force:
        return list(_cache)

    by_id: dict[str, CharacterPreset] = {}
    for preset in BUILTIN_PRESETS:
        by_id[preset.id] = preset

    for path in _iter_pack_entries():
        try:
            if path.is_dir():
                preset = load_preset_from_directory(path)
                src_name = path.name + "/"
            else:
                preset = load_preset_from_pack(path, force_extract=force)
                src_name = path.name
            by_id[preset.id] = preset
            logger.info(
                "character pack loaded id=%s label=%s from %s",
                preset.id,
                preset.label,
                src_name,
            )
        except CharacterPackError as exc:
            logger.warning("skip pack %s: %s", path, exc)
        except Exception:
            logger.exception("skip pack %s (unexpected error)", path)

    ordered = list(by_id.values())
    # Prefer a stable UX order: nora first if present, then alpha by label
    def _sort_key(p: CharacterPreset) -> tuple:
        return (0 if p.id == "nora" else 1, p.label.lower(), p.id)

    ordered.sort(key=_sort_key)
    _cache = ordered
    return list(ordered)


def invalidate_character_cache() -> None:
    global _cache
    _cache = None


def list_characters(*, force: bool = False) -> list[CharacterPreset]:
    return scan_characters(force=force)


def get_character(character_id: str | None, *, force: bool = False) -> CharacterPreset | None:
    if not character_id:
        return None
    cid = str(character_id).strip()
    for p in scan_characters(force=force):
        if p.id == cid:
            return p
    return None


def default_character() -> CharacterPreset:
    chars = scan_characters()
    for p in chars:
        if p.id == "nora":
            return p
    if chars:
        return chars[0]
    logger.error(
        "no character packs found under %s or %s",
        bundled_characters_dir(),
        user_characters_dir(),
    )
    return _placeholder_preset()


def resolve_character(character_id: str | None) -> CharacterPreset:
    """Resolve id → preset; fall back to default if missing."""
    found = get_character(character_id)
    if found is not None:
        return found
    if character_id:
        logger.warning("character_id=%r not found — using default", character_id)
    return default_character()

"""Character presets — sprite bundle definitions.

Each preset is a visual identity:
- sprite_map: state (idle/think/talk/…) → mascot prefix list
- idle_ambient / idle_rarity: ambient animation selection

Characters ship as packs (``.hchar`` or directory + ``character.json``)
and are discovered by :mod:`avatar.character_registry`. There are no
built-in mascot presets in code right now (Jenny was removed temporarily).

Persona / system prompt live on the Hermes gateway; this module only
holds sprite identity and the Nora pack *recipe* used by
``scripts/pack_character.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# Sprite loader looks for "<asset_root>/<prefix>.webp" (or mascot/ fallback).
# Use a LIST when one state draws from multiple prefixes.
@dataclass
class CharacterPreset:
    id: str
    label: str
    sprite_map: dict[str, list[str]] = field(default_factory=dict)
    # Optional label for docs / future persona wiring (not used by gateway today).
    prompt_entry: str | None = None
    idle_ambient: list[str] = field(default_factory=list)
    # Prefix → rarity weight; lower = more frequent. None = uniform.
    idle_rarity: dict[str, float] | None = None
    # Pack support: when set, frames load from this directory (extracted .hchar).
    asset_root: Path | None = None
    # Additional dirs to search (external_clip_roots / multi-root packs).
    extra_asset_roots: list[Path] | None = None
    pack_path: Path | None = None
    version: str = "1.0.0"
    # "builtin" | "pack"
    source: str = "pack"
    preview_path: Path | None = None


# Intentionally empty — all characters come from packs (Nora, etc.).
# Re-add built-ins here later if needed without a pack file.
BUILTIN_PRESETS: list[CharacterPreset] = []

# Back-compat alias (prefer character_registry.list_characters at runtime).
PRESETS: list[CharacterPreset] = list(BUILTIN_PRESETS)


def get_preset(preset_id: str) -> CharacterPreset | None:
    """Lookup built-in only. Prefer character_registry.get_character()."""
    for p in BUILTIN_PRESETS:
        if p.id == preset_id:
            return p
    return None


def default_preset() -> CharacterPreset | None:
    """Built-in default, or None when only pack characters exist."""
    return BUILTIN_PRESETS[0] if BUILTIN_PRESETS else None


def list_presets() -> list[CharacterPreset]:
    """Built-ins only. Prefer character_registry.list_characters()."""
    return list(BUILTIN_PRESETS)


# ---------------------------------------------------------------------------
# Nora pack source definition (used by scripts/pack_character.py).
# Clip basenames under mascot_v2 / mascot (without .webp).
# ---------------------------------------------------------------------------
NORA_PACK_CLIPS: dict[str, list[str]] = {
    "idle": [
        "nora_idle_a_sigh0",
        "nora_idle_a_sigh1",
        "nora_idle_a_sigh2",
        "nora_idle_a_sigh_3",
    ],
    "talk": ["nora_talking1", "nora_talking2"],
    "think": ["nora_thinking1", "nora_thinking2", "nora_thinking3"],
}

NORA_PACK_AMBIENT: list[str] = [
    "nora_idle_a_sigh0",
    "nora_idle_a_sigh1",
    "nora_idle_a_sigh2",
    "nora_idle_a_sigh_3",
    "nora_idle_b_dancing_serious",
    "nora_idle_b_dancing_serious_2",
    "nora_idle_b_japanese_thank_you",
    "nora_idle_b_jumping",
    "nora_idle_b_turns_around",
    "nora_idle_c_dancing_sexy1",
    "nora_idle_c_impatient",
]

NORA_PACK_RARITY: dict[str, float] = {
    "nora_idle_a_sigh0": 1.0,
    "nora_idle_a_sigh1": 1.0,
    "nora_idle_a_sigh2": 1.0,
    "nora_idle_a_sigh_3": 1.0,
    "nora_idle_b_dancing_serious": 4.0,
    "nora_idle_b_dancing_serious_2": 4.0,
    "nora_idle_b_japanese_thank_you": 4.0,
    "nora_idle_b_jumping": 4.0,
    "nora_idle_b_turns_around": 4.0,
    "nora_idle_c_dancing_sexy1": 20.0,
    "nora_idle_c_impatient": 20.0,
}

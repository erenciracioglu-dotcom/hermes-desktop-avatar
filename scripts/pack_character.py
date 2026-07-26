"""Build a ``.hchar`` character pack from WebP clips on disk.

Examples::

    # Pack Nora (default sources: mascot_v2 then mascot)
    python scripts/pack_character.py nora

    # Custom output
    python scripts/pack_character.py nora -o assets/characters/nora.hchar
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _find_clip(name: str, search_dirs: list[Path]) -> Path | None:
    """``name`` is stem or stem.webp."""
    stem = name[:-5] if name.lower().endswith(".webp") else name
    for d in search_dirs:
        for candidate in (d / f"{stem}.webp", d / "clips" / f"{stem}.webp"):
            if candidate.is_file():
                return candidate
    return None


def build_nora_pack(out_path: Path) -> Path:
    from avatar.character_pack import write_pack
    from avatar.characters import (
        NORA_PACK_AMBIENT,
        NORA_PACK_CLIPS,
        NORA_PACK_RARITY,
    )
    from avatar.paths import mascot_dir

    search = [mascot_dir()]
    files: dict[str, Path] = {}
    states: dict[str, list[str]] = {}
    missing: list[str] = []

    all_stems: set[str] = set()
    for state, stems in NORA_PACK_CLIPS.items():
        arcs: list[str] = []
        for stem in stems:
            all_stems.add(stem)
            src = _find_clip(stem, search)
            if src is None:
                missing.append(stem)
                continue
            arc = f"clips/{stem}.webp"
            files[arc] = src
            arcs.append(arc)
        if arcs:
            states[state] = arcs

    ambient_arcs: list[str] = []
    for stem in NORA_PACK_AMBIENT:
        all_stems.add(stem)
        src = _find_clip(stem, search)
        if src is None:
            missing.append(stem)
            continue
        arc = f"clips/{stem}.webp"
        files[arc] = src
        ambient_arcs.append(arc)

    if missing:
        uniq = sorted(set(missing))
        raise SystemExit(f"Missing clip files ({len(uniq)}): {uniq}")

    rarity = {
        f"clips/{stem}.webp": weight for stem, weight in NORA_PACK_RARITY.items()
    }
    # Fallbacks
    if "idle" in states:
        states.setdefault("dance", list(states["idle"][:1]))
        states.setdefault("sleep", list(states["idle"][:1]))

    manifest = {
        "schema": 1,
        "id": "nora",
        "label": "Nora",
        "version": "1.0.0",
        "author": "hermes-desktop-avatar",
        "license": "user-content",
        "default_fps": 24,
        "states": states,
        "ambient": {
            "pool": ambient_arcs,
            "rarity": rarity,
        },
        "fallbacks": {
            "dance": "idle",
            "sleep": "idle",
        },
    }
    return write_pack(out_path, manifest, files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build .hchar character packs")
    parser.add_argument(
        "character",
        choices=["nora"],
        help="Built-in pack recipe to build",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output .hchar path (default: assets/characters/<id>.hchar)",
    )
    args = parser.parse_args(argv)

    if args.character == "nora":
        out = args.output or (ROOT / "assets" / "characters" / "nora.hchar")
        path = build_nora_pack(out)
        print(f"OK: {path}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Single-file character packs (``.hchar`` = zip + ``character.json``).

Layout inside the archive::

    character.json
    preview.png          (optional)
    clips/
      idle_a_sigh0.webp
      talking1.webp
      …

Runtime extracts to a versioned cache under the user config dir so the
existing WebP / lazy-frame loaders can keep using real filesystem paths.
"""
from __future__ import annotations

import json
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .characters import CharacterPreset
from .paths import user_config_dir

logger = logging.getLogger(__name__)

PACK_EXT = ".hchar"
SCHEMA_VERSION = 1
MANIFEST_NAME = "character.json"


@dataclass
class CharacterPackInfo:
    """Metadata for a discovered pack file (before or after extract)."""

    path: Path
    id: str
    label: str
    version: str = "1.0.0"
    schema: int = SCHEMA_VERSION
    author: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)


class CharacterPackError(Exception):
    """Invalid or unreadable character pack."""


def _read_manifest_from_zip(zf: zipfile.ZipFile) -> dict[str, Any]:
    try:
        raw = zf.read(MANIFEST_NAME)
    except KeyError as exc:
        raise CharacterPackError(
            f"pack missing {MANIFEST_NAME}"
        ) from exc
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CharacterPackError(f"invalid {MANIFEST_NAME}: {exc}") from exc
    if not isinstance(data, dict):
        raise CharacterPackError(f"{MANIFEST_NAME} must be a JSON object")
    return data


def validate_manifest(data: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized manifest or raise CharacterPackError."""
    schema = int(data.get("schema", SCHEMA_VERSION) or SCHEMA_VERSION)
    if schema > SCHEMA_VERSION:
        raise CharacterPackError(
            f"pack schema {schema} requires a newer avatar (supports ≤{SCHEMA_VERSION})"
        )
    cid = str(data.get("id") or "").strip()
    if not cid or any(c in cid for c in r'\/:*?"<>|'):
        raise CharacterPackError("character.json: invalid or missing id")
    label = str(data.get("label") or cid).strip()
    version = str(data.get("version") or "1.0.0").strip()
    states = data.get("states")
    if not isinstance(states, dict) or not states:
        raise CharacterPackError("character.json: states must be a non-empty object")
    # Normalize clip paths
    norm_states: dict[str, list[str]] = {}
    for state, clips in states.items():
        if not isinstance(clips, list) or not clips:
            continue
        paths = [str(c).replace("\\", "/").lstrip("/") for c in clips if c]
        if paths:
            norm_states[str(state)] = paths
    if "idle" not in norm_states and "talk" not in norm_states:
        raise CharacterPackError(
            "character.json: need at least idle or talk clips in states"
        )
    ambient = data.get("ambient") or {}
    if not isinstance(ambient, dict):
        ambient = {}
    pool = ambient.get("pool") or norm_states.get("idle") or []
    if not isinstance(pool, list):
        pool = list(norm_states.get("idle") or [])
    pool = [str(c).replace("\\", "/").lstrip("/") for c in pool if c]
    rarity_in = ambient.get("rarity") or {}
    rarity: dict[str, float] = {}
    if isinstance(rarity_in, dict):
        for k, v in rarity_in.items():
            try:
                rarity[str(k).replace("\\", "/").lstrip("/")] = float(v)
            except (TypeError, ValueError):
                continue
    fallbacks = data.get("fallbacks") or {}
    if not isinstance(fallbacks, dict):
        fallbacks = {}
    return {
        "schema": schema,
        "id": cid,
        "label": label,
        "version": version,
        "author": str(data.get("author") or "").strip(),
        "license": str(data.get("license") or "").strip(),
        "default_fps": int(data.get("default_fps") or 24),
        "states": norm_states,
        "ambient": {"pool": pool, "rarity": rarity},
        "fallbacks": {str(k): str(v) for k, v in fallbacks.items()},
        "preview": str(data.get("preview") or "").replace("\\", "/").lstrip("/"),
    }


def inspect_pack(path: Path) -> CharacterPackInfo:
    """Read pack metadata without full extract."""
    path = Path(path)
    if not path.is_file():
        raise CharacterPackError(f"not a file: {path}")
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if zf.testzip() is not None:
                raise CharacterPackError(f"corrupt zip: {path}")
            data = validate_manifest(_read_manifest_from_zip(zf))
    except zipfile.BadZipFile as exc:
        raise CharacterPackError(f"not a zip/hchar: {path}") from exc
    return CharacterPackInfo(
        path=path.resolve(),
        id=data["id"],
        label=data["label"],
        version=data["version"],
        schema=int(data["schema"]),
        author=data.get("author") or "",
        manifest=data,
    )


def pack_cache_root() -> Path:
    d = user_config_dir() / "character_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def extract_pack(path: Path, *, force: bool = False) -> Path:
    """Extract pack to versioned cache; return directory with character.json."""
    info = inspect_pack(path)
    dest = pack_cache_root() / f"{info.id}_v{info.version}"
    marker = dest / MANIFEST_NAME
    if dest.is_dir() and marker.is_file() and not force:
        return dest
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "r") as zf:
        # Prevent zip-slip
        root = dest.resolve()
        for member in zf.infolist():
            name = member.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            target = (dest / name).resolve()
            if not str(target).startswith(str(root)):
                raise CharacterPackError(f"zip-slip blocked: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
    if not marker.is_file():
        raise CharacterPackError(f"extract failed (no {MANIFEST_NAME}): {path}")
    logger.info("extracted character pack %s → %s", info.id, dest)
    return dest


def _clip_stem(clip_path: str) -> str:
    """``clips/idle_a_sigh0.webp`` → ``idle_a_sigh0``."""
    return Path(clip_path.replace("\\", "/")).stem


def preset_from_extracted(root: Path, *, pack_path: Path | None = None) -> CharacterPreset:
    """Build a CharacterPreset whose asset_root is the extracted pack dir."""
    root = Path(root)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise CharacterPackError(f"missing {MANIFEST_NAME} in {root}")
    with manifest_path.open("r", encoding="utf-8-sig") as f:
        data = validate_manifest(json.load(f))

    states: dict[str, list[str]] = data["states"]
    ambient = data["ambient"]
    pool = list(ambient.get("pool") or [])
    rarity_paths: dict[str, float] = dict(ambient.get("rarity") or {})

    # sprite_map / ambient use file stems; files live under asset_root
    sprite_map: dict[str, list[str]] = {}
    for state, clips in states.items():
        stems = [_clip_stem(c) for c in clips]
        if stems:
            sprite_map[state] = stems

    idle_ambient = [_clip_stem(c) for c in pool] if pool else list(
        sprite_map.get("idle") or []
    )

    # Rarity keyed by stem (and also by full relative path for flexibility)
    idle_rarity: dict[str, float] = {}
    for key, weight in rarity_paths.items():
        idle_rarity[_clip_stem(key)] = weight
        idle_rarity[key] = weight

    # Fallbacks: map missing states to another state's first clip list
    fallbacks = data.get("fallbacks") or {}
    for state, target in fallbacks.items():
        if state not in sprite_map and target in sprite_map:
            sprite_map[state] = list(sprite_map[target])

    preview = data.get("preview") or ""
    preview_path = (root / preview) if preview else None
    if preview_path is not None and not preview_path.is_file():
        preview_path = None

    return CharacterPreset(
        id=data["id"],
        label=data["label"],
        sprite_map=sprite_map,
        prompt_entry=data["id"],
        idle_ambient=idle_ambient,
        idle_rarity=idle_rarity or None,
        asset_root=root,
        pack_path=Path(pack_path).resolve() if pack_path else None,
        version=data["version"],
        source="pack",
        preview_path=preview_path,
    )


def load_preset_from_pack(path: Path, *, force_extract: bool = False) -> CharacterPreset:
    """Inspect + extract + build preset for a ``.hchar`` file."""
    path = Path(path)
    root = extract_pack(path, force=force_extract)
    return preset_from_extracted(root, pack_path=path)


def load_preset_from_directory(root: Path) -> CharacterPreset:
    """Load an unpacked character folder (``character.json`` + optional clips/).

    Supports ``external_clip_roots`` in the manifest: relative directories
    searched for clip files (used by the repo-local Nora pack so we do not
    duplicate multi‑MB WebPs until ``scripts/pack_character.py`` is run).
    """
    root = Path(root).resolve()
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise CharacterPackError(f"missing {MANIFEST_NAME} in {root}")
    with manifest_path.open("r", encoding="utf-8-sig") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise CharacterPackError(f"{MANIFEST_NAME} must be a JSON object")

    external = raw.get("external_clip_roots") or []
    if not isinstance(external, list):
        external = []
    # If external roots are declared, resolve clips from those dirs and set
    # asset_root to the first root that exists (plus pack root for local clips).
    search_roots: list[Path] = []
    for rel in external:
        cand = (root / str(rel)).resolve()
        if cand.is_dir():
            search_roots.append(cand)
    local_clips = root / "clips"
    if local_clips.is_dir():
        search_roots.insert(0, local_clips)
    search_roots.append(root)

    if external:
        # Rewrite manifest paths to bare stems; asset_root becomes multi-search
        # via CharacterPreset.asset_root = pack root and extra roots stored.
        data = validate_manifest(raw)
        states: dict[str, list[str]] = {}
        for state, clips in data["states"].items():
            stems = [_clip_stem(c) for c in clips]
            if stems:
                states[state] = stems
        pool = [_clip_stem(c) for c in data["ambient"]["pool"]]
        rarity: dict[str, float] = {}
        for k, v in (data["ambient"].get("rarity") or {}).items():
            rarity[_clip_stem(k)] = float(v)

        fallbacks = data.get("fallbacks") or {}
        for state, target in fallbacks.items():
            if state not in states and target in states:
                states[state] = list(states[target])

        # Prefer first external root as asset_root; sprites also searches pack root
        asset = search_roots[0] if search_roots else root
        return CharacterPreset(
            id=data["id"],
            label=data["label"],
            sprite_map=states,
            prompt_entry=data["id"],
            idle_ambient=pool or list(states.get("idle") or []),
            idle_rarity=rarity or None,
            asset_root=asset,
            pack_path=root,
            version=data["version"],
            source="pack",
            preview_path=None,
            extra_asset_roots=search_roots[1:] if len(search_roots) > 1 else None,
        )

    return preset_from_extracted(root, pack_path=root)


def write_pack(
    out_path: Path,
    manifest: dict[str, Any],
    files: dict[str, Path],
) -> Path:
    """Create a ``.hchar`` zip.

    ``files`` maps archive-relative paths (e.g. ``clips/foo.webp``) to
    filesystem sources. ``character.json`` is written from ``manifest``.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    norm = validate_manifest(manifest)
    # Ensure referenced clips are in files
    needed: set[str] = set()
    for clips in norm["states"].values():
        needed.update(clips)
    needed.update(norm["ambient"]["pool"])
    preview = norm.get("preview") or ""
    if preview:
        needed.add(preview)
    missing = [c for c in needed if c not in files]
    if missing:
        raise CharacterPackError(f"write_pack missing files: {missing[:5]}")

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr(
                MANIFEST_NAME,
                json.dumps(norm, indent=2, ensure_ascii=False) + "\n",
            )
            for arc, src in sorted(files.items()):
                src = Path(src)
                if not src.is_file():
                    raise CharacterPackError(f"missing source file: {src}")
                zf.write(src, arcname=arc.replace("\\", "/"))
        if out_path.exists():
            out_path.unlink()
        tmp.replace(out_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    logger.info("wrote character pack %s (%s)", out_path, norm["id"])
    return out_path.resolve()

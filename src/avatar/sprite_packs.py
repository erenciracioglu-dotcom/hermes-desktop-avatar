"""Sprite pack loader — maps animation ids to mascot frame paths.

Reads ``mascot/<state>_<idx>.png`` files and groups them by animation id
(id = filename stem without the trailing ``_<idx>``).  Each animation
becomes an IdleAnimation whose rarity is supplied by
:mod:`avatar.idle_animator`.

This is the bridge between the on-disk sprite inventory and the
weighted random picker.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

from .idle_animator import (
    IdleAnimation,
    IdleAnimator,
    build_pool_from_videos,
    parse_rarity_from_filename,
)
from .paths import mascot_dir

logger = logging.getLogger(__name__)


def _group_mascot_frames(mascot: Path, prefix: str) -> dict[str, list[Path]]:
    """Group sprite frame files by animation id.

    Supports both PNG sequence (``<prefix>_<anim>_<idx>.png``) and
    animated WebP (``<prefix>_<anim>.webp``).  WebP is preferred when
    both exist (CorridorKey output path).  When a WebP exists for an
    animation id, the corresponding PNG sequence is ignored entirely.

    Returns a dict mapping animation_id -> sorted list of frame paths
    (WebP counts as one path).
    """
    groups: dict[str, list[Path]] = {}

    # 1. Animated WebP files (one per animation)
    for webp in sorted(mascot.glob(f"{prefix}_*.webp")):
        stem = webp.stem  # e.g. nora_idle_a_sigh0
        groups.setdefault(stem, []).append(webp)

    # 2. PNG sequence — only for animations without WebP.
    #    Stale PNGs left from before the WebP migration are ignored.
    pattern = f"{prefix}_*.png"
    for p in sorted(mascot.glob(pattern)):
        stem = p.stem
        parts = stem.rsplit("_", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        anim_id = parts[0]
        if anim_id in groups:
            # already have an animated WebP for this id; skip PNGs
            continue
        groups.setdefault(anim_id, []).append(p)
    return groups


def build_nora_animator_from_mascot() -> IdleAnimator:
    """Construct an IdleAnimator populated from ``mascot/nora_*.png``.

    Walks the avatar's mascot/ folder, groups frames by animation id,
    and creates an IdleAnimation per group with the rarity derived from
    the original video filename via the a/b/c convention.
    """
    mascot = mascot_dir()
    pool: list[IdleAnimation] = []
    groups = _group_mascot_frames(mascot, prefix="nora")
    # Need the original filename to derive rarity tier.  We store the
    # tier in the animation id itself: animation ids derived from files
    # whose stem contains a/b/c tokens carry that info.  We can re-derive
    # it by reading the animation id pattern.
    for anim_id, frames in groups.items():
        # reconstruct a plausible filename for parse_rarity_from_filename
        fake_name = anim_id.replace("_", "-") + ".mp4"
        rarity = parse_rarity_from_filename(fake_name)
        pool.append(
            IdleAnimation(
                id=anim_id,
                rarity=rarity,
                label=f"{len(frames)} frames",
            )
        )
    animator = IdleAnimator(pool=pool)
    logger.info(
        "built Nora animator: %d animations, %d total frames",
        animator.pool_size,
        sum(len(frames) for frames in groups.values()),
    )
    return animator


def list_animation_ids(prefix: str = "nora") -> list[str]:
    """Return sorted list of animation ids available for a given prefix."""
    mascot = mascot_dir()
    return sorted(_group_mascot_frames(mascot, prefix).keys())


def frames_for(anim_id: str, mascot: Path | None = None) -> list[Path]:
    """Return the frame paths for a single animation id."""
    m = mascot or mascot_dir()
    return _group_mascot_frames(m, prefix=anim_id.split("_")[0]).get(anim_id, [])


# --------------------------------------------------------------------- video source
# When sprite packs aren't yet rendered to PNG, we can drive the avatar
# straight from the source videos.  The video source dir is configured
# via the AVATAR_VIDEO_SOURCE env var, defaulting to the user's Downloads
# nora_new/ folder.

DEFAULT_VIDEO_SOURCE = Path(
    r"C:\Users\2\Downloads\nora_new"
)


def video_source_dir() -> Path:
    """Return the directory where source chroma-keyed videos live.

    Can be overridden by the AVATAR_VIDEO_SOURCE environment variable —
    useful for tests or alternative characters.
    """
    override = os.environ.get("AVATAR_VIDEO_SOURCE")
    return Path(override) if override else DEFAULT_VIDEO_SOURCE


def _video_for_animation(anim_id: str, source: Path) -> Path | None:
    """Resolve an animation id to its source video path.

    Animation ids in the sprite pipeline look like
    ``nora_idle_a_sigh0`` — we map them back to the original video name
    by undoing the underscore normalisation:
      ``nora_idle_a_sigh0`` -> ``nora-idle-a-sigh0.mp4``

    Falls back to case-insensitive search if the exact filename doesn't
    exist (handles ``Nora_Idle-a-sigh1.mp4`` style names).
    """
    # Try the canonical name first
    candidate = source / (anim_id.replace("_", "-") + ".mp4")
    if candidate.is_file():
        return candidate
    # Try alternate normalisations: original-style with spaces,
    # capitalised, etc.
    lower = anim_id.lower()
    for p in source.glob("*.mp4"):
        stem_norm = p.stem.lower().replace(" ", "-").replace("_", "-")
        if stem_norm == lower.replace("_", "-"):
            return p
        # match with underscores too
        stem_underscore = p.stem.lower().replace(" ", "_").replace("-", "_")
        if stem_underscore == anim_id.lower():
            return p
    return None


def video_for(anim_id: str, source: Path | None = None) -> Path | None:
    """Public helper: return the source video path for an animation id.

    Returns None if the video cannot be found (e.g. the sprite set was
    renamed but the source video wasn't moved into the configured dir).
    """
    return _video_for_animation(anim_id, source or video_source_dir())


def list_video_animations(source: Path | None = None, only_idle: bool = True) -> IdleAnimator:
    """Build an IdleAnimator pool from source videos directly.

    Same as ``build_nora_animator_from_mascot`` but reads videos
    instead of rendered PNG frames.  Useful for running the avatar
    before the sprite extraction step is done.

    With ``only_idle=True`` (default), only files whose normalised
    stem contains the token ``idle`` are included.  This keeps
    ``talking``/``thinking`` clips out of the idle ambient pool — those
    are driven by the state machine, not the weighted-random picker.
    """
    src = source or video_source_dir()
    videos = sorted(src.glob("*.mp4"))
    if only_idle:
        videos = [
            v for v in videos
            # match 'idle' as a substring of the normalised stem so that
            # filenames with spaces ("nora idle-c-dancing sexy2.mp4") and
            # capitalisation are still detected as idle clips.
            if "idle" in v.stem.lower().replace("_", "-").replace(" ", "-")
        ]
    pool = build_pool_from_videos(videos)
    return IdleAnimator(pool=pool)

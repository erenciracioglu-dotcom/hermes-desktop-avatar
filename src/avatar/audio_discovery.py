"""Find Hermes-generated audio files when MEDIA: tags are missing.

Telegram delivery intercepts media on the gateway side. The OpenAI-compatible
API only returns the model's *final text*, which often omits MEDIA: even when
TTS (or a voice skill) wrote a file under HERMES_HOME.

Avatar-side recovery: after a chat turn, pick audio files whose mtime falls
inside the request window. No Hermes source changes required.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_AUDIO_SUFFIXES = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".webm"}


def hermes_home_candidates() -> list[Path]:
    """Likely Hermes home directories (env first, then common Windows paths)."""
    out: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path | None) -> None:
        if p is None:
            return
        try:
            key = str(p.expanduser().resolve())
        except Exception:
            key = str(p)
        if key in seen:
            return
        if p.expanduser().is_dir():
            seen.add(key)
            out.append(p.expanduser())

    for env in ("HERMES_HOME", "HERMES_DIR"):
        v = (os.environ.get(env) or "").strip()
        if v:
            _add(Path(v))

    local = os.environ.get("LOCALAPPDATA")
    if local:
        _add(Path(local) / "hermes")
    _add(Path.home() / ".hermes")
    # Legacy / alternate layouts
    _add(Path.home() / "AppData" / "Local" / "hermes")
    return out


def audio_cache_dirs() -> list[Path]:
    dirs: list[Path] = []
    seen: set[str] = set()
    for home in hermes_home_candidates():
        for sub in ("audio_cache", Path("cache") / "audio"):
            p = home / sub
            try:
                key = str(p.resolve())
            except Exception:
                key = str(p)
            if key in seen:
                continue
            if p.is_dir():
                seen.add(key)
                dirs.append(p)
    return dirs


def is_fresh_audio(
    path: str | Path,
    started_at: float,
    *,
    grace_before_s: float = 5.0,
    until: float | None = None,
) -> bool:
    """True if file exists and mtime is inside this chat turn window.

    Hermes often reuses a fixed name (e.g. ``nora_voice.ogg``) in MEDIA:
    tags without regenerating the file. Playing it would repeat the same
    clip for every reply. We only accept files touched during the turn.
    """
    until = until if until is not None else time.time()
    p = Path(path)
    try:
        if not p.is_file():
            return False
        mtime = p.stat().st_mtime
    except OSError:
        return False
    return (started_at - grace_before_s) <= mtime <= (until + 2.0)


def filter_fresh_audio(
    paths: list[str],
    started_at: float,
    *,
    grace_before_s: float = 5.0,
    until: float | None = None,
) -> tuple[list[str], list[str]]:
    """Split paths into (fresh, stale_or_missing)."""
    until = until if until is not None else time.time()
    fresh: list[str] = []
    stale: list[str] = []
    for raw in paths:
        if is_fresh_audio(raw, started_at, grace_before_s=grace_before_s, until=until):
            fresh.append(raw)
        else:
            stale.append(raw)
            try:
                mt = Path(raw).stat().st_mtime if Path(raw).is_file() else None
            except OSError:
                mt = None
            logger.info(
                "reject stale/missing audio (not from this turn): %s mtime=%s "
                "window=[%.0f, %.0f]",
                raw,
                mt,
                started_at - grace_before_s,
                until + 2.0,
            )
    return fresh, stale


def find_audio_created_since(
    started_at: float,
    *,
    grace_before_s: float = 3.0,
    until: float | None = None,
    max_files: int = 3,
) -> list[str]:
    """Return newest audio paths with mtime in [started_at - grace, until].

    ``started_at`` / ``until`` are ``time.time()`` values bracketing the
    chat request. Prefer the most recent files (last TTS in the turn).
    """
    until = until if until is not None else time.time()
    cutoff = started_at - grace_before_s
    candidates: list[tuple[float, Path]] = []

    for d in audio_cache_dirs():
        try:
            entries = list(d.iterdir())
        except OSError as exc:
            logger.debug("cannot list %s: %s", d, exc)
            continue
        for f in entries:
            if not f.is_file():
                continue
            if f.suffix.lower() not in _AUDIO_SUFFIXES:
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if cutoff <= mtime <= until + 1.0:
                candidates.append((mtime, f))

    if not candidates:
        logger.info(
            "audio discovery: no files in %s since %s",
            [str(d) for d in audio_cache_dirs()],
            cutoff,
        )
        return []

    candidates.sort(key=lambda t: t[0], reverse=True)

    # Prefer unique stems: if both .mp3 and .ogg of same TTS, keep ogg first.
    picked: list[Path] = []
    seen_stems: set[str] = set()
    # Sort ogg before mp3 for same mtime bucket
    candidates.sort(
        key=lambda t: (
            -t[0],
            0 if t[1].suffix.lower() in {".ogg", ".opus"} else 1,
        )
    )
    for _mt, path in candidates:
        stem = path.stem
        # voice_nora / voice_nora.ogg vs tts_xxx.mp3+ogg
        if stem in seen_stems:
            continue
        # If we already took stem.ogg, skip stem.mp3
        base = stem
        if base in seen_stems:
            continue
        seen_stems.add(base)
        picked.append(path)
        if len(picked) >= max_files:
            break

    paths = [str(p.resolve()) for p in picked]
    logger.info("audio discovery: %s", paths)
    return paths

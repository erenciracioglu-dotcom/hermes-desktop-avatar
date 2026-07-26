"""Avatar-side TTS fallback (Edge TTS).

Used when voice replies are enabled but Hermes does not return MEDIA: tags
or write a new file under audio_cache (common on api_server: no TTS tool,
or agent routes voice to Telegram instead).
"""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "tr-TR-AhmetNeural"
DEFAULT_RATE = "+0%"
_MAX_CHARS = 1200


def _clean_for_speech(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    # Strip leftover media / paths
    t = re.sub(r"\[\[audio_as_voice\]\]", "", t, flags=re.I)
    t = re.sub(r"MEDIA:\s*\S+", "", t, flags=re.I)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\n{2,}", ". ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > _MAX_CHARS:
        t = t[:_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return t


def synthesize_to_file(
    text: str,
    *,
    voice: str = DEFAULT_VOICE,
    rate: str = DEFAULT_RATE,
    out_dir: Path | None = None,
) -> str | None:
    """Synthesize speech with edge-tts; return absolute path or None."""
    spoken = _clean_for_speech(text)
    if not spoken:
        return None
    voice = (voice or DEFAULT_VOICE).strip() or DEFAULT_VOICE
    rate = (rate or DEFAULT_RATE).strip() or DEFAULT_RATE

    try:
        import edge_tts  # type: ignore
    except ImportError:
        logger.error(
            "edge-tts not installed — run: pip install edge-tts "
            "(avatar voice fallback)"
        )
        return None

    directory = out_dir or Path(tempfile.gettempdir()) / "hermes-desktop-avatar-tts"
    directory.mkdir(parents=True, exist_ok=True)
    out_path = directory / f"tts_{abs(hash(spoken)) % 10**10}.mp3"

    async def _run() -> None:
        communicate = edge_tts.Communicate(spoken, voice, rate=rate)
        await communicate.save(str(out_path))

    try:
        try:
            asyncio.run(_run())
        except RuntimeError:
            # Nested event loop (unlikely in worker thread)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_run())
            finally:
                loop.close()
    except Exception as exc:
        logger.exception("edge-tts failed: %s", exc)
        return None

    if not out_path.is_file() or out_path.stat().st_size == 0:
        logger.error("edge-tts produced empty file")
        return None
    logger.info("edge-tts saved %s (%d bytes, voice=%s)", out_path, out_path.stat().st_size, voice)
    return str(out_path.resolve())

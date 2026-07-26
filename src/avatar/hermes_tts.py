"""Call Hermes Agent's text_to_speech *tool* from the avatar process.

Uses the user's Hermes TTS provider (xAI / MiniMax / Edge / …) from
HERMES_HOME/config.yaml — without going through the LLM.

Language: Hermes often has ``tts.xai.language: en`` (English accent on
Turkish text). For the avatar we temporarily override language (and Edge
voice when needed) for this call only — no permanent Hermes config edit.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_HERMES_ROOTS = (
    os.environ.get("HERMES_AGENT_DIR", "").strip(),
    r"C:\hermes-agent",
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "hermes-agent"),
    str(Path.home() / "hermes-agent"),
)

# Avatar default Edge voices when Hermes provider is edge or for fallbacks.
_EDGE_VOICE_BY_LANG = {
    "en": "en-US-JennyNeural",
}

def guess_speech_language(text: str, preferred: str | None = None) -> str:
    """Return BCP-47-ish code for TTS. Defaults to ``en``."""
    pref = (preferred or "").strip().lower()
    if pref.startswith("en"):
        return "en"
    return pref[:2] if pref else "en"


def _ensure_hermes_home() -> None:
    if os.environ.get("HERMES_HOME"):
        return
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"
    if local.is_dir():
        os.environ["HERMES_HOME"] = str(local)
        return
    home = Path.home() / ".hermes"
    if home.is_dir():
        os.environ["HERMES_HOME"] = str(home)


def _find_hermes_agent_root() -> Optional[Path]:
    for raw in _HERMES_ROOTS:
        if not raw:
            continue
        root = Path(raw)
        if (root / "tools" / "tts_tool.py").is_file():
            return root
    return None


def _apply_language_overrides(cfg: dict[str, Any], language: str) -> dict[str, Any]:
    """Return a deep-copied TTS config tuned for ``language`` (tr/en/…)."""
    out = copy.deepcopy(cfg) if cfg else {}
    lang = (language or "tr").strip().lower()
    if lang.startswith("tr"):
        bcp = "tr"
        edge_voice = _EDGE_VOICE_BY_LANG["tr"]
    elif lang.startswith("en"):
        bcp = "en"
        edge_voice = _EDGE_VOICE_BY_LANG["en"]
    else:
        bcp = lang.split("-")[0] or "tr"
        edge_voice = _EDGE_VOICE_BY_LANG.get(bcp, _EDGE_VOICE_BY_LANG["tr"])

    provider = (out.get("provider") or "edge").lower().strip()

    # xAI Grok TTS: language=en + Turkish text → English accent
    xai = out.setdefault("xai", {})
    if isinstance(xai, dict):
        xai["language"] = bcp
        logger.info("TTS override xai.language → %s (was set for avatar speech)", bcp)

    edge = out.setdefault("edge", {})
    if isinstance(edge, dict):
        # Only force Edge voice when provider is edge; still set for consistency
        if provider == "edge" or not edge.get("voice"):
            edge["voice"] = edge_voice
        elif bcp == "tr" and str(edge.get("voice", "")).startswith("en-"):
            edge["voice"] = edge_voice
            logger.info("TTS override edge.voice → %s (was English)", edge_voice)

    # MiniMax: no standard language field in all versions; leave voice_id as user set
    return out


def synthesize(
    text: str,
    *,
    language: str | None = None,
    preferred_language: str | None = None,
) -> Optional[str]:
    """Synthesize ``text`` via Hermes tts_tool; return absolute path or None.

    ``language``: force BCP code (tr/en). If None, guess from text + preferred.
    ``preferred_language``: avatar settings language (e.g. tr).
    """
    spoken = (text or "").strip()
    if not spoken:
        return None

    lang = language or guess_speech_language(spoken, preferred_language)
    root = _find_hermes_agent_root()
    if root is None:
        logger.warning("Hermes agent install not found (tools/tts_tool.py)")
        return None

    _ensure_hermes_home()
    root_s = str(root.resolve())
    if root_s not in sys.path:
        sys.path.insert(0, root_s)

    try:
        import tools.tts_tool as tts_mod  # type: ignore
        from tools.tts_tool import text_to_speech_tool  # type: ignore
    except Exception as exc:
        logger.warning("cannot import Hermes text_to_speech_tool: %s", exc)
        return None

    orig_load = getattr(tts_mod, "_load_tts_config", None)
    if orig_load is None:
        logger.warning("tts_tool has no _load_tts_config; calling without language override")
        return _call_tool(text_to_speech_tool, spoken)

    def _patched_load() -> dict[str, Any]:
        base = orig_load() or {}
        if not isinstance(base, dict):
            base = {}
        return _apply_language_overrides(base, lang)

    tts_mod._load_tts_config = _patched_load  # type: ignore[attr-defined]
    try:
        logger.info("Hermes TTS synthesize lang=%s chars=%d", lang, len(spoken))
        return _call_tool(text_to_speech_tool, spoken)
    finally:
        tts_mod._load_tts_config = orig_load  # type: ignore[attr-defined]


def _call_tool(text_to_speech_tool, spoken: str) -> Optional[str]:
    try:
        raw = text_to_speech_tool(text=spoken)
    except Exception as exc:
        logger.exception("Hermes text_to_speech_tool failed: %s", exc)
        return None

    if not isinstance(raw, str):
        raw = json.dumps(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("tts_tool non-JSON response: %s", raw[:200])
        return None

    if not data.get("success"):
        logger.warning("tts_tool error: %s", data.get("error") or raw[:200])
        return None

    path = data.get("file_path") or ""
    if not path or not Path(path).is_file():
        tag = data.get("media_tag") or ""
        if "MEDIA:" in tag.upper():
            path = tag.split("MEDIA:", 1)[-1].strip().splitlines()[0].strip()
    if path and Path(path).is_file():
        logger.info(
            "Hermes TTS OK provider=%s path=%s",
            data.get("provider"),
            path,
        )
        return str(Path(path).resolve())

    logger.warning("tts_tool success but file missing: %s", data)
    return None

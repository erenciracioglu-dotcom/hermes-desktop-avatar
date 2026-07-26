"""Parse Hermes-style media tags from assistant text.

Messaging platforms (Telegram, etc.) deliver voice when the agent final
text contains::

    [[audio_as_voice]]
    MEDIA:C:\\Users\\...\\.hermes\\cache\\audio\\tts_....ogg

The desktop avatar uses the same convention: strip tags for the chat
panel, collect local audio paths for autoplay.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Mirrors gateway.platforms.base.BaseAdapter.extract_media (simplified for audio).
_AUDIO_EXT = r"(?:ogg|opus|mp3|wav|m4a|webm)"
_MEDIA_RE = re.compile(
    r"""[`"']?MEDIA:\s*(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|(?:~/|/|[A-Za-z]:\\)\S*?\."""
    + _AUDIO_EXT
    + r"""(?=[\s`"',;:)\]}]|$)|\S+\."""
    + _AUDIO_EXT
    + r""")[`"']?""",
    re.IGNORECASE,
)
_JSON_PATH_RE = re.compile(
    r'"(?:file_path|media_tag)"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.IGNORECASE,
)
_BARE_WIN_RE = re.compile(
    rf'([A-Za-z]:\\[^\s"\'<>|*?]+\.{_AUDIO_EXT})',
    re.IGNORECASE,
)
_BARE_UNIX_RE = re.compile(
    rf'((?:/|~/|\\\\)[^\s"\']+\.{_AUDIO_EXT})',
    re.IGNORECASE,
)


def _normalize_path(raw: str) -> str:
    path = raw.strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
        path = path[1:-1].strip()
    path = path.lstrip("`\"'").rstrip("`\"',.;:)}]")
    # media_tag value may be "[[audio_as_voice]]\nMEDIA:..."
    if "MEDIA:" in path.upper():
        m = _MEDIA_RE.search(path if path.upper().startswith("MEDIA") else f"MEDIA:{path}")
        if not m:
            m = _MEDIA_RE.search(path)
        if m:
            path = m.group("path").strip().strip("`\"'")
    try:
        path = path.encode("utf-8").decode("unicode_escape")
    except Exception:
        pass
    return str(Path(path).expanduser())


def extract_media(content: str) -> tuple[list[str], str]:
    """Return (existing_audio_paths, cleaned_display_text)."""
    if not content:
        return [], ""

    has_voice = "[[audio_as_voice]]" in content
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        try:
            p = _normalize_path(raw)
        except Exception:
            return
        if not p or p in seen:
            return
        # Prefer real files only (Hermes writes under cache/audio or similar)
        if Path(p).is_file():
            seen.add(p)
            found.append(p)

    for m in _MEDIA_RE.finditer(content):
        _add(m.group("path"))

    for m in _JSON_PATH_RE.finditer(content):
        val = m.group(1)
        if re.search(rf"\.{_AUDIO_EXT}$", val, re.I) or "MEDIA:" in val.upper():
            _add(val)

    # Fallback: bare absolute paths (when model pastes tool JSON loosely)
    for m in _BARE_WIN_RE.finditer(content):
        _add(m.group(1))
    for m in _BARE_UNIX_RE.finditer(content):
        _add(m.group(1))

    cleaned = content.replace("[[audio_as_voice]]", "")
    cleaned = _MEDIA_RE.sub("", cleaned)
    # Drop tiny leftover JSON blobs that only restate the path
    cleaned = re.sub(
        r'\{\s*"success"\s*:\s*true[^}]*"file_path"\s*:\s*"[^"]*"\s*[^}]*\}',
        "",
        cleaned,
        flags=re.I | re.S,
    )
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    _ = has_voice  # reserved for future UI (voice bubble vs file)
    return found, cleaned


def extract_media_from_tool_json(blob: str) -> list[str]:
    """If the whole message is a TTS tool JSON object, pull file_path."""
    try:
        data = json.loads(blob)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    paths: list[str] = []
    for key in ("file_path", "path"):
        v = data.get(key)
        if isinstance(v, str) and Path(v).expanduser().is_file():
            paths.append(str(Path(v).expanduser()))
    tag = data.get("media_tag")
    if isinstance(tag, str):
        more, _ = extract_media(tag if "MEDIA:" in tag.upper() else f"MEDIA:{tag}")
        paths.extend(more)
    # dedupe
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

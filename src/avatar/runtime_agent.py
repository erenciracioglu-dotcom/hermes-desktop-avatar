"""Gateway adapter for the desktop avatar.

Talks to the Hermes gateway OpenAI-compatible endpoint
``POST /v1/chat/completions`` (api_server adapter, default port 8642).

Hermes core identity (SOUL.md, tools, memory) stays on the gateway.
Optional environment overlay becomes ephemeral system prompt.

Voice: the *model* is not asked to run TTS. When voice is enabled, the
avatar synthesizes the final reply text itself (Hermes tts_tool config,
then optional Edge) so text and speech always match.

Images: optional local file paths are sent as OpenAI-style ``image_url``
data-URLs so vision-capable models can see desktop screenshots without
Hermes computer-use tools.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("HERMES_GATEWAY_URL", "http://127.0.0.1:8642")
# Prefer HERMES_GATEWAY_KEY, then API_SERVER_KEY (Hermes). No hardcoded secret.
API_KEY = (
    os.environ.get("HERMES_GATEWAY_KEY", "").strip()
    or os.environ.get("API_SERVER_KEY", "").strip()
    or ""
)
AVATAR_SESSION_ID = os.environ.get("HERMES_AVATAR_SESSION", "avatar-nora")
AVATAR_MODEL = os.environ.get("HERMES_AVATAR_MODEL", "hermes-agent")
REQUEST_TIMEOUT = 120

CONTEXT_PROMPT: str = ""
VOICE_REPLIES: bool = False

_CONTEXT_PREAMBLE = """\
[Desktop Avatar channel context — does NOT replace your core identity.]
You are still Hermes (your SOUL / tools / memory from the gateway).
This block only describes *where* you are being used right now and how to behave in this UI.

"""

# Keep short: do NOT ask the model to call text_to_speech (unreliable).
# Avatar owns TTS after the text reply arrives.
_VOICE_NOTE = """\

[Desktop Avatar — voice playback is handled by the desktop client]
- Reply with short text only (1–3 sentences), same language as the user.
- Do NOT call text_to_speech, do NOT attach MEDIA: tags, do NOT send_message voice to Telegram.
- Do NOT discuss runtime_agent, VOICE_INSTRUCTION, or how the avatar plays audio unless the user asks.
- The client will speak your written reply aloud automatically.
"""

# Always injected so the model knows about the client-side screenshot skill.
_SCREENSHOT_SKILL = """\

[Desktop Avatar — REQUIRED screenshot skill]
You have a client-side skill: the desktop app can capture the user's live screen
and send you the image. This is NOT Hermes computer-use; you trigger it with a tag.

MANDATORY when the user wants a live view of their screen right now
(examples: "what's on my screen?", "take a screenshot and tell me"):

Your reply MUST include this exact line (copy/paste, alone on its line):

[[DESKTOP_SCREENSHOT]]

Correct pattern:
  Sure, taking a look.
  [[DESKTOP_SCREENSHOT]]

Wrong patterns (never do these):
  - Saying you cannot see / lack computer-use WITHOUT the tag
  - Asking the user to send another message so a screenshot "will arrive"
  - Claiming a screenshot was taken without emitting the tag
  - Emitting the tag only because screenshots were mentioned in history
    (e.g. talking about an old file) without a live-view request

After you emit the tag, the CLIENT immediately captures and sends the image
to you in an automatic follow-up turn (no user action needed). Then describe
what you see in that image.

If an image is already attached this turn, describe it; do not emit the tag again
unless they ask for a fresh live capture.
"""

# Extra note when images are already attached this turn
_SCREENSHOT_IMAGE_ATTACHED = """\

[Desktop Avatar — image attached this turn]
An image is attached (client capture or user paste). You can see it via vision.
Describe what is in the image. Do not emit [[DESKTOP_SCREENSHOT]] again unless
the user asks for a *new* live capture. Do not claim you cannot see the image.
"""


def configure(
    *,
    gateway_url: Optional[str] = None,
    session_id: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    context_prompt: Optional[str] = None,
    voice_replies: Optional[bool] = None,
) -> None:
    """Apply runtime overrides (from settings / controller)."""
    global GATEWAY_URL, AVATAR_SESSION_ID, API_KEY, AVATAR_MODEL
    global CONTEXT_PROMPT, VOICE_REPLIES
    if gateway_url:
        GATEWAY_URL = gateway_url.rstrip("/")
    if session_id:
        AVATAR_SESSION_ID = session_id
    if api_key:
        API_KEY = api_key
    if model:
        AVATAR_MODEL = model
    if context_prompt is not None:
        CONTEXT_PROMPT = context_prompt.strip()
    if voice_replies is not None:
        VOICE_REPLIES = bool(voice_replies)


def _system_content(*, has_images: bool = False) -> str | None:
    parts: list[str] = []
    if CONTEXT_PROMPT:
        parts.append(_CONTEXT_PREAMBLE + CONTEXT_PROMPT)
    # Screenshot skill is always advertised (agent decides when to use it).
    parts.append(_SCREENSHOT_SKILL.strip())
    if has_images:
        parts.append(_SCREENSHOT_IMAGE_ATTACHED.strip())
    if VOICE_REPLIES:
        parts.append(_VOICE_NOTE.strip())
    if not parts:
        return None
    return "\n".join(parts)


def _user_content(
    message: str,
    image_paths: list[str] | None,
) -> str | list[dict[str, Any]]:
    """Build OpenAI-style user content (plain string or multimodal parts)."""
    paths = [p for p in (image_paths or []) if p]
    if not paths:
        return message

    from .screen_capture import image_to_data_url

    parts: list[dict[str, Any]] = [
        {"type": "text", "text": message},
    ]
    for path in paths:
        data_url = image_to_data_url(path)
        if data_url:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                }
            )
        else:
            parts.append(
                {
                    "type": "text",
                    "text": f"[Image file could not be encoded: {path}]",
                }
            )
    # If no image encoded successfully, fall back to text only
    if len(parts) == 1:
        return message
    return parts


def _build_messages(
    user_message: str,
    image_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    has_images = bool(image_paths)
    messages: list[dict[str, Any]] = []
    system = _system_content(has_images=has_images)
    if system:
        messages.append({"role": "system", "content": system})
    messages.append(
        {
            "role": "user",
            "content": _user_content(user_message, image_paths),
        }
    )
    return messages


def _session() -> requests.Session:
    """HTTP session that ignores HTTP(S)_PROXY for local agent calls."""
    s = requests.Session()
    s.trust_env = False
    return s


class ChatCancelled(Exception):
    """Raised when the in-flight chat request was aborted (user interrupt)."""


def _is_cancel(
    is_cancelled: Any,
    request_holder: dict[str, Any] | None,
) -> bool:
    if callable(is_cancelled) and is_cancelled():
        return True
    if request_holder is not None and request_holder.get("cancel"):
        return True
    return False


def _safe_close(
    response: requests.Response | None,
    session: requests.Session | None,
) -> None:
    """Close HTTP resources without hanging the caller.

    On Windows, ``response.close()`` / ``session.close()`` while another thread
    is mid-read can block for a long time. We prefer socket ``shutdown`` to
    unblock the reader, then close in a short-lived daemon thread.
    """
    import socket
    import threading

    if response is not None:
        # Best-effort: force the peer to see a disconnect (Hermes interrupt).
        try:
            raw = getattr(response, "raw", None)
            sock = None
            if raw is not None:
                # urllib3 HTTPResponse → fp → raw socket (layout varies)
                fp = getattr(raw, "_fp", None) or getattr(raw, "fp", None)
                if fp is not None:
                    inner = getattr(fp, "raw", None) or getattr(fp, "fp", None) or fp
                    sock = getattr(inner, "_sock", None) or getattr(inner, "sock", None)
                if sock is None:
                    conn = getattr(raw, "_connection", None) or getattr(raw, "connection", None)
                    if conn is not None:
                        sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
        except Exception:
            pass

    def _close_blocking() -> None:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    t = threading.Thread(target=_close_blocking, name="avatar-http-close", daemon=True)
    t.start()
    t.join(timeout=0.4)


def _chat_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-Hermes-Session-Id": AVATAR_SESSION_ID,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


def _extract_delta_text(payload: dict[str, Any]) -> str:
    """Pull assistant text from an OpenAI-style chat.completion.chunk."""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    choice0 = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice0.get("delta") or {}
    if isinstance(delta, dict):
        piece = delta.get("content")
        if isinstance(piece, str) and piece:
            return piece
    # Some gateways send full message snapshots mid-stream
    message = choice0.get("message") or {}
    if isinstance(message, dict):
        piece = message.get("content")
        if isinstance(piece, str) and piece:
            return piece
    return ""


def _is_idle_read_timeout(exc: BaseException) -> bool:
    """True if this is a stream-idle timeout (not a refused/down gateway)."""
    if isinstance(exc, (requests.exceptions.ReadTimeout, TimeoutError)):
        return True
    msg = str(exc).lower()
    if "read timed out" in msg or "read timeout" in msg:
        return True
    # urllib3 ReadTimeoutError often wrapped as ConnectionError by requests
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _is_idle_read_timeout(cause)
    return False


def _accumulate_sse_text(response: requests.Response) -> str:
    """Read a full SSE chat.completion stream into assistant text.

    Blocking; intended to run on a dedicated reader thread. Cancel is handled
    by closing ``response`` from the waiter (raises here → ChatCancelled).
    """
    parts: list[str] = []
    buffer = ""
    try:
        for chunk in response.iter_content(chunk_size=1024, decode_unicode=True):
            if chunk is None:
                continue
            if isinstance(chunk, bytes):
                try:
                    chunk = chunk.decode("utf-8", errors="replace")
                except Exception:
                    continue
            if not chunk:
                continue
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r").strip()
                if not line or line.startswith(":"):
                    continue  # keepalive / comment
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    return "".join(parts)
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                err = payload.get("error")
                if isinstance(err, dict) and err.get("message"):
                    logger.warning("stream error chunk: %s", err.get("message"))
                    continue
                piece = _extract_delta_text(payload)
                if piece:
                    parts.append(piece)
    except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError) as exc:
        # Mid-stream disconnect after cancel close — return what we have or re-raise
        if parts:
            logger.info(
                "SSE ended mid-stream after partial reply (%d chars): %s",
                sum(len(p) for p in parts),
                exc,
            )
            return "".join(parts)
        raise
    # Trailing partial line without newline
    tail = buffer.rstrip("\r").strip()
    if tail.startswith("data:"):
        data = tail[5:].strip()
        if data and data != "[DONE]":
            try:
                payload = json.loads(data)
                if isinstance(payload, dict):
                    piece = _extract_delta_text(payload)
                    if piece:
                        parts.append(piece)
            except json.JSONDecodeError:
                pass
    return "".join(parts)


def _post_chat_stream(
    message: str,
    image_paths: list[str] | None = None,
    *,
    is_cancelled: Any = None,
    request_holder: dict[str, Any] | None = None,
) -> str:
    """Streaming POST so Hermes can interrupt on client disconnect.

    Hermes ``agent.interrupt()`` is only wired for SSE disconnects on
    ``/v1/chat/completions``. Short per-chunk read timeouts *break* urllib3
    streams (they surface as ConnectionError and kill the socket) — so we use
    a long read timeout and a cancel-watcher thread that closes the response
    when barge-in is requested. Close runs off the UI thread.
    """
    import threading

    if _is_cancel(is_cancelled, request_holder):
        raise ChatCancelled("cancelled before request")

    connect_t = 15.0
    # Long read: quiet tool stretches + Hermes SSE keepalives (~30s) are normal.
    hard_deadline = float(REQUEST_TIMEOUT + (60 if image_paths else 0))
    read_t = hard_deadline

    session = _session()
    if request_holder is not None:
        request_holder["session"] = session
        request_holder["response"] = None
        request_holder["cancel"] = False

    box: dict[str, Any] = {"response": None}
    done = threading.Event()
    # Close only once from the cancel path / finally
    close_lock = threading.Lock()
    closed = False

    def _close_http() -> None:
        nonlocal closed
        with close_lock:
            if closed:
                return
            closed = True
            resp = box.get("response")
            if request_holder is not None:
                if request_holder.get("response") is resp:
                    request_holder["response"] = None
                if request_holder.get("session") is session:
                    request_holder["session"] = None
            _safe_close(resp if isinstance(resp, requests.Response) else None, session)

    def _reader() -> None:
        try:
            if _is_cancel(is_cancelled, request_holder):
                box["exc"] = ChatCancelled("cancelled before connect")
                return
            try:
                response = session.post(
                    f"{GATEWAY_URL}/v1/chat/completions",
                    headers=_chat_headers(),
                    json={
                        "model": AVATAR_MODEL,
                        "messages": _build_messages(message, image_paths),
                        "stream": True,
                    },
                    stream=True,
                    timeout=(connect_t, read_t),
                )
            except Exception as exc:  # noqa: BLE001 — stored for waiter
                if _is_cancel(is_cancelled, request_holder):
                    box["exc"] = ChatCancelled("cancelled during connect")
                else:
                    box["exc"] = exc
                return

            box["response"] = response
            if request_holder is not None:
                request_holder["response"] = response

            if _is_cancel(is_cancelled, request_holder):
                box["exc"] = ChatCancelled("cancelled after connect")
                return

            if response.status_code >= 400:
                try:
                    _ = response.content
                except Exception:
                    pass
                try:
                    response.raise_for_status()
                except Exception as exc:  # noqa: BLE001
                    box["exc"] = exc
                    return

            try:
                box["text"] = _accumulate_sse_text(response)
            except Exception as exc:  # noqa: BLE001
                if _is_cancel(is_cancelled, request_holder):
                    box["exc"] = ChatCancelled("cancelled while streaming")
                else:
                    box["exc"] = exc
        finally:
            done.set()

    reader = threading.Thread(
        target=_reader,
        name="avatar-sse-reader",
        daemon=True,
    )
    reader.start()

    try:
        while not done.wait(0.1):
            if _is_cancel(is_cancelled, request_holder):
                # Unblock the reader + tell Hermes to interrupt the agent.
                _close_http()
                done.wait(2.0)
                raise ChatCancelled("cancelled while waiting for gateway stream")

        if _is_cancel(is_cancelled, request_holder):
            raise ChatCancelled("cancelled after stream finished")

        exc = box.get("exc")
        if isinstance(exc, ChatCancelled):
            raise exc
        if exc is not None:
            raise exc
        return box.get("text") or ""
    finally:
        _close_http()


def _post_chat_blocking_fallback(
    message: str,
    image_paths: list[str] | None = None,
    *,
    is_cancelled: Any = None,
    request_holder: dict[str, Any] | None = None,
) -> str:
    """Non-streaming fallback if the gateway rejects SSE.

    Still runs on the caller thread so cancel can close the socket from the
    same thread. Less interruptible mid-body than SSE, but better than the
    old abandon-without-close pattern.
    """
    if _is_cancel(is_cancelled, request_holder):
        raise ChatCancelled("cancelled before request")

    timeout = REQUEST_TIMEOUT + (60 if image_paths else 0)
    session = _session()
    response: requests.Response | None = None
    if request_holder is not None:
        request_holder["session"] = session
        request_holder["response"] = None
        request_holder["cancel"] = False

    try:
        # Short connect + long read; cancel is only honored at start/end unless
        # the holder is closed externally.
        response = session.post(
            f"{GATEWAY_URL}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "X-Hermes-Session-Id": AVATAR_SESSION_ID,
                "Content-Type": "application/json",
            },
            json={
                "model": AVATAR_MODEL,
                "messages": _build_messages(message, image_paths),
                "stream": False,
            },
            timeout=timeout,
        )
        if request_holder is not None:
            request_holder["response"] = response
        if _is_cancel(is_cancelled, request_holder):
            raise ChatCancelled("cancelled after response")
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "") or ""
    finally:
        if request_holder is not None:
            if request_holder.get("response") is response:
                request_holder["response"] = None
            if request_holder.get("session") is session:
                request_holder["session"] = None
        _safe_close(response, session)


def _post_chat(
    message: str,
    image_paths: list[str] | None = None,
    *,
    is_cancelled: Any = None,
    request_holder: dict[str, Any] | None = None,
) -> str:
    """POST chat completions, preferring SSE for interruptible barge-in."""
    try:
        return _post_chat_stream(
            message,
            image_paths=image_paths,
            is_cancelled=is_cancelled,
            request_holder=request_holder,
        )
    except ChatCancelled:
        raise
    except requests.exceptions.HTTPError as e:
        # Some older gateways may not stream; fall back once.
        status = e.response.status_code if e.response is not None else 0
        if status in {400, 404, 415, 501} and not _is_cancel(
            is_cancelled, request_holder
        ):
            logger.warning(
                "stream chat failed HTTP %s — trying non-stream fallback",
                status,
            )
            return _post_chat_blocking_fallback(
                message,
                image_paths=image_paths,
                is_cancelled=is_cancelled,
                request_holder=request_holder,
            )
        raise


def chat(
    message: str,
    image_paths: list[str] | None = None,
    *,
    is_cancelled: Any = None,
    request_holder: dict[str, Any] | None = None,
) -> str:
    """Send one user turn to the gateway and return assistant text.

    ``image_paths`` — optional local image files (e.g. desktop screenshots)
    attached as multimodal content for vision.

    ``is_cancelled`` — optional ``() -> bool``; when true, abort and close the
    live HTTP stream so Hermes interrupts the agent (SSE disconnect path).
    ``request_holder`` — optional dict; stores ``session`` / ``response`` and
    accepts ``cancel=True`` from the controller for barge-in.
    """
    try:
        return _post_chat(
            message,
            image_paths=image_paths,
            is_cancelled=is_cancelled,
            request_holder=request_holder,
        )
    except ChatCancelled:
        raise
    except requests.exceptions.HTTPError as e:
        if _is_cancel(is_cancelled, request_holder):
            raise ChatCancelled("cancelled") from e
        # If multimodal fails (gateway lacks vision), retry text-only with path hints
        if image_paths and e.response is not None and e.response.status_code in {
            400, 415, 422, 500,
        }:
            hint = message + "\n\n" + "\n".join(
                f"[Attached local image path: {p}]" for p in image_paths
            )
            try:
                return _post_chat(
                    hint,
                    image_paths=None,
                    is_cancelled=is_cancelled,
                    request_holder=request_holder,
                )
            except ChatCancelled:
                raise
            except Exception:
                pass
        body = ""
        try:
            body = (e.response.text or "")[:200] if e.response is not None else ""
        except Exception:
            body = ""
        code = e.response.status_code if e.response is not None else "?"
        raise RuntimeError(f"Gateway HTTP {code}: {body}") from e
    except requests.exceptions.Timeout as e:
        if _is_cancel(is_cancelled, request_holder):
            raise ChatCancelled("cancelled") from e
        raise RuntimeError(
            f"Gateway timed out at {GATEWAY_URL}: {e}"
        ) from e
    except requests.exceptions.ConnectionError as e:
        if _is_cancel(is_cancelled, request_holder):
            raise ChatCancelled("cancelled") from e
        # requests sometimes wraps stream read timeouts as ConnectionError
        if _is_idle_read_timeout(e):
            raise RuntimeError(
                f"Gateway stream timed out at {GATEWAY_URL}: {e}"
            ) from e
        raise RuntimeError(
            f"Cannot connect to gateway {GATEWAY_URL}. "
            "Is `hermes gateway` running?"
        ) from e
    except TimeoutError as e:
        if _is_cancel(is_cancelled, request_holder):
            raise ChatCancelled("cancelled") from e
        raise RuntimeError(str(e)) from e

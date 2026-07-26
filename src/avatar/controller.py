"""Avatar controller — user message → Hermes gateway → avatar state.

Responsibilities:
- Verify Hermes gateway health on start.
- Own the state machine (idle / thinking / talking).
- Forward user text to the gateway via runtime_agent.
- Parse Hermes MEDIA: tags and emit audio paths for autoplay.
- Emit signals for the UI (history, overlay, errors).
"""
from __future__ import annotations

import logging
import re
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from .hermes_media import extract_media
from .state_machine import AvatarState

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    line = msg if not args else msg % args
    logger.info(line)
    try:
        print(f"[avatar] {line}", flush=True)
    except Exception:
        pass


def _talking_display_ms(text: str) -> int:
    """How long to keep TALKING when there is *no* TTS audio (text-only)."""
    n = len((text or "").strip())
    return min(14_000, max(2_800, 1_200 + n * 55))


# Absolute ceiling while waiting for TTS finished (stuck decoder / missing signal).
_TTS_SAFETY_IDLE_MS = 15 * 60 * 1000


@dataclass
class ControllerConfig:
    gateway_url: str | None = None
    session_id: str | None = None
    context_prompt: str | None = None
    # When True: avatar synthesizes the reply text (Hermes tts_tool config,
    # then optional Edge). Model is not asked to produce MEDIA: / TTS tools.
    voice_replies: bool = False
    edge_fallback: bool = True  # if Hermes tts_tool import/call fails
    # Avatar UI language → TTS language override
    language: str = "en"
    tts_voice: str = "en-US-JennyNeural"
    tts_rate: str = "+0%"
    # Local gateway lifecycle (loopback URLs only). Portable open-source defaults.
    auto_start: bool = True
    auto_restart: bool = True
    startup_timeout_seconds: float = 60.0
    hermes_command: str | None = None
    # Shared with Hermes API_SERVER_KEY (Bearer). Resolved/generated on start.
    api_key: str | None = None
    ensure_api_server: bool = True
    write_hermes_env: bool = True


# Ava requests a live desktop capture by emitting this exact tag.
_SCREENSHOT_TAG_ANY = re.compile(
    r"\[\[\s*DESKTOP[_\s-]*SCREENSHOT\s*\]\]"
    r"|\[\s*DESKTOP[_\s-]*SCREENSHOT\s*\]"
    r"|DESKTOP_SCREENSHOT",
    re.IGNORECASE,
)

# Assistant denied vision / stalled instead of using the skill
_DENIES_VISION = re.compile(
    r"("
    r"can'?t\s+see|"
    r"cannot\s+see|"
    r"do\s+not\s+see|"
    r"don'?t\s+see|"
    r"computer[-\s]?use|"
    r"screenshot\s+didn'?t\s+(come|arrive)|"
    r"next\s+message|"
    r"i'?ll\s+(just\s+)?(take|capture)\s+(it\s+)?(myself|on\s+my\s+own)"
    r")",
    re.IGNORECASE,
)

_LIVE_VIEW_REMINDER = (
    "\n\n[Desktop Avatar client — REQUIRED for this turn]\n"
    "The user is asking for a LIVE view of their screen. "
    "You MUST include this exact line alone in your reply:\n"
    "[[DESKTOP_SCREENSHOT]]\n"
    "Do not say you cannot see the screen. "
    "Do not ask the user to send another message. "
    "The client will capture immediately after the tag."
)


def _strip_screenshot_tag(text: str) -> tuple[str, bool]:
    """Remove screenshot request tags from assistant text. Returns (clean, requested)."""
    if not text:
        return "", False
    requested = bool(_SCREENSHOT_TAG_ANY.search(text))
    if not requested:
        return text, False
    cleaned = _SCREENSHOT_TAG_ANY.sub("", text)
    # Collapse leftover blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, True


def _looks_like_vision_denial(text: str) -> bool:
    return bool(text and _DENIES_VISION.search(text))


class AvatarController(QObject):
    state_changed = Signal(AvatarState)
    assistant_message = Signal(str)  # cleaned text for chat panel
    assistant_audio = Signal(list)  # local audio paths to autoplay
    user_message = Signal(str)
    error = Signal(str)
    # User sent a new message while a turn was in flight — stop TTS / UI
    interrupted = Signal()
    _schedule_idle_ms = Signal(int)
    # Worker thread → GUI thread: capture desktop (Qt requires main thread)
    _request_desktop_capture = Signal()

    def __init__(
        self,
        config: ControllerConfig | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config or ControllerConfig()
        self._state = AvatarState.IDLE
        self._lock = threading.Lock()
        self._busy = False
        self._worker: threading.Thread | None = None
        self._awaiting_audio = False
        # Generation counter: each new send_user_message bumps it; older
        # workers become stale and must not update UI / clear busy.
        self._generation = 0
        self._request_holder: dict = {}
        # Set when the current/previous worker fully exits (HTTP closed).
        # New barge-in turns wait on the previous event so Hermes can finish
        # interrupt before the next /v1/chat/completions hits the same session.
        self._job_done = threading.Event()
        self._job_done.set()  # idle: nothing in flight
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._maybe_idle)
        self._schedule_idle_ms.connect(self._arm_idle_timer)
        self._capture_event = threading.Event()
        self._capture_path: str | None = None
        self._request_desktop_capture.connect(self._on_desktop_capture)

    def start(self, progress=None) -> None:
        """Connect to the gateway API, starting/restarting a *local* one if needed.

        ``progress`` is an optional ``callable(str)`` for UI status lines.
        """
        _log("controller starting")
        try:
            from . import runtime_agent
            from .gateway_manager import GatewayManageOptions, ensure_gateway

            self.apply_runtime_config()
            url = runtime_agent.GATEWAY_URL
            result = ensure_gateway(
                url,
                GatewayManageOptions(
                    auto_start=bool(self.config.auto_start),
                    auto_restart=bool(self.config.auto_restart),
                    startup_timeout_seconds=float(
                        self.config.startup_timeout_seconds or 60.0
                    ),
                    hermes_command=self.config.hermes_command or None,
                    api_key=self.config.api_key or None,
                    ensure_api_server=bool(self.config.ensure_api_server),
                    write_hermes_env=bool(self.config.write_hermes_env),
                    progress=progress,
                ),
            )
            if not result.ok:
                raise RuntimeError(result.detail)

            # Keep avatar Authorization in sync with Hermes API_SERVER_KEY.
            if result.api_key:
                self.config.api_key = result.api_key
                runtime_agent.configure(api_key=result.api_key)

            _log(
                "gateway ready action=%s cli=%s health=%s",
                result.action,
                result.hermes_cli or "-",
                (result.detail or "")[:200],
            )
            _log(
                "voice_replies=%s context_chars=%d",
                runtime_agent.VOICE_REPLIES,
                len(runtime_agent.CONTEXT_PROMPT or ""),
            )
        except Exception as exc:
            from . import runtime_agent

            msg = (
                f"Could not connect to Hermes gateway ({runtime_agent.GATEWAY_URL}): {exc}"
            )
            self.error.emit(msg)
            raise RuntimeError(msg) from exc
        _log("controller ready")

    def apply_runtime_config(self) -> None:
        from . import runtime_agent

        runtime_agent.configure(
            gateway_url=self.config.gateway_url,
            session_id=self.config.session_id,
            api_key=self.config.api_key or None,
            context_prompt=self.config.context_prompt or "",
            voice_replies=self.config.voice_replies,
        )

    def update_config(self, config: ControllerConfig) -> None:
        self.config = config
        self.apply_runtime_config()
        _log("runtime config updated (voice_replies=%s)", config.voice_replies)

    def set_voice_replies(self, enabled: bool) -> None:
        self.config.voice_replies = bool(enabled)
        self.apply_runtime_config()
        _log("voice_replies → %s", self.config.voice_replies)

    def stop(self) -> None:
        _log("controller stopping")
        self._abort_in_flight(reason="controller stop")

    def _is_stale(self, generation: int) -> bool:
        with self._lock:
            return generation != self._generation

    def _abort_in_flight(self, *, reason: str = "interrupt") -> None:
        """Bump generation so workers become stale (do not close HTTP from UI thread)."""
        with self._lock:
            self._generation += 1
            holder = self._request_holder
            if isinstance(holder, dict):
                holder["cancel"] = True
            self._request_holder = {}
            # Leave _job_done alone: the in-flight worker still owns it and
            # will set it in finally after closing the SSE stream.
        _log("aborted in-flight turn (%s)", reason)
        self._capture_event.set()

    @property
    def state(self) -> AvatarState:
        return self._state

    def _set_state(self, new_state: AvatarState) -> None:
        if new_state == self._state:
            return
        _log("state %s -> %s", self._state.value, new_state.value)
        self._state = new_state
        self.state_changed.emit(new_state)

    def send_user_message(self, user_text: str) -> None:
        """Queue a user turn. If a turn is already running/talking, interrupt it."""
        try:
            self._send_user_message_impl(user_text)
        except Exception:
            _log("send_user_message CRASH:\n%s", traceback.format_exc())
            try:
                self.error.emit("Failed to send message (see log).")
            except Exception:
                pass

    def _send_user_message_impl(self, user_text: str) -> None:
        user_text = user_text.strip()
        if not user_text:
            return

        with self._lock:
            # Job finished but TTS still playing counts as active.
            was_active = (
                self._busy
                or self._awaiting_audio
                or self._state
                in (AvatarState.THINKING, AvatarState.TALKING)
            )
            self._generation += 1
            gen = self._generation
            self._busy = True
            self._awaiting_audio = False
            # Mark previous holder cancelled. The worker that owns the HTTP
            # stream closes it on its own thread (Windows OpenSSL-safe) when
            # it sees cancel — do NOT session.close() from the UI thread.
            holder = self._request_holder
            if isinstance(holder, dict):
                holder["cancel"] = True
            self._request_holder = {}
            # Previous worker's completion event; new worker waits on it.
            prev_done = self._job_done
            job_done = threading.Event()
            self._job_done = job_done

        self._capture_event.set()
        self._idle_timer.stop()

        if was_active:
            _log("user interrupt/barge-in → new message: %s", user_text[:80])

        image_paths = self._extract_embedded_images(user_text)

        from .screen_capture import message_wants_live_view

        api_text = user_text
        if message_wants_live_view(user_text) and not image_paths:
            api_text = user_text + _LIVE_VIEW_REMINDER

        # UI + worker first so barge-in never depends on audio teardown.
        self.user_message.emit(user_text)
        self._set_state(AvatarState.THINKING)
        t = threading.Thread(
            target=self._job_main,
            args=(gen, api_text, image_paths, user_text, prev_done, job_done),
            name=f"avatar-chat-{gen}",
            daemon=True,
        )
        self._worker = t
        t.start()

        # Stop TTS after the new turn is queued. Audio stop must never block
        # this method (Windows QMediaPlayer.stop can deadlock the UI thread).
        try:
            self.interrupted.emit()
        except Exception:
            _log("interrupted emit failed:\n%s", traceback.format_exc())

    @staticmethod
    def _extract_embedded_images(user_text: str) -> list[str]:
        """Local image paths already present in the message (markdown / paste)."""
        image_paths: list[str] = []
        for m in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", user_text or ""):
            p = m.group(1).strip().strip("\"'")
            try:
                path = Path(p).expanduser()
                if path.is_file():
                    image_paths.append(str(path.resolve()))
            except Exception:
                continue
        # Dedupe
        seen: set[str] = set()
        out: list[str] = []
        for p in image_paths:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def _on_desktop_capture(self) -> None:
        """GUI-thread slot: grab desktop for a worker waiting on _capture_event."""
        from .screen_capture import capture_desktop

        try:
            self._capture_path = capture_desktop(all_monitors=True)
        except Exception as exc:
            _log("desktop capture slot error: %s", exc)
            self._capture_path = None
        self._capture_event.set()

    def _capture_desktop_blocking(
        self, generation: int, timeout: float = 15.0
    ) -> str | None:
        """Ask the GUI thread to capture; block the worker until done or stale."""
        if self._is_stale(generation):
            return None
        self._capture_path = None
        self._capture_event.clear()
        self._request_desktop_capture.emit()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._is_stale(generation):
                return None
            if self._capture_event.wait(0.2):
                break
        else:
            _log("desktop capture timed out")
            return None
        if self._is_stale(generation):
            return None
        return self._capture_path

    def _job_main(
        self,
        generation: int,
        user_text: str,
        image_paths: list[str] | None = None,
        original_user_text: str | None = None,
        prev_done: threading.Event | None = None,
        job_done: threading.Event | None = None,
    ) -> None:
        """Run chat for ``generation``. Stale generations exit quietly."""
        started = time.time()
        original = original_user_text if original_user_text is not None else user_text
        holder: dict = {}
        with self._lock:
            if generation == self._generation:
                self._request_holder = holder

        def _cancelled() -> bool:
            return self._is_stale(generation)

        try:
            if _cancelled():
                return

            # Let the previous turn close its SSE stream so Hermes receives
            # disconnect → agent.interrupt() before we open a new turn on the
            # same X-Hermes-Session-Id. UI already switched to THINKING.
            if prev_done is not None and not prev_done.is_set():
                _log(
                    "gen=%s waiting for previous turn to release gateway…",
                    generation,
                )
                # Short wait only — do not stall the new turn if the old SSE
                # close is slow. Hermes interrupt is best-effort via disconnect.
                if not prev_done.wait(timeout=1.5):
                    _log(
                        "gen=%s previous turn still winding down after 1.5s — proceeding",
                        generation,
                    )
                if _cancelled():
                    return

            _log(
                "chat -> Hermes gen=%s: %s images=%d",
                generation,
                user_text[:80],
                len(image_paths or []),
            )
            from .runtime_agent import ChatCancelled, chat as avatar_chat
            from .screen_capture import message_wants_live_view

            try:
                response = avatar_chat(
                    user_text,
                    image_paths=image_paths or None,
                    is_cancelled=_cancelled,
                    request_holder=holder,
                )
            except ChatCancelled:
                _log("chat cancelled gen=%s (after/during request)", generation)
                return

            if _cancelled():
                return
            _log(
                "chat <- Hermes gen=%s (%.1fs): %s",
                generation,
                time.time() - started,
                (response or "")[:200],
            )
            if not response:
                self.error.emit("Hermes returned an empty reply.")
                self._set_state(AvatarState.IDLE)
                return

            # Strip accidental MEDIA: from model text (client owns TTS now).
            _ignored_media, cleaned = extract_media(response)
            cleaned = cleaned if cleaned else response
            if _ignored_media:
                _log("ignored model MEDIA tags (avatar owns TTS): %s", _ignored_media)

            # Prefer agent tag; if user clearly wants live view and Ava forgot
            # the tag (or denied vision), capture anyway — do not wait for
            # another user message.
            display, wants_shot = _strip_screenshot_tag(cleaned)
            if (
                not wants_shot
                and not image_paths
                and message_wants_live_view(original)
            ):
                wants_shot = True
                _log(
                    "screenshot fallback: live-view intent, Ava omitted tag "
                    "(denial=%s)",
                    _looks_like_vision_denial(display),
                )
                if _looks_like_vision_denial(display) or not display.strip():
                    # Don't show "I can't see" before we capture for them
                    display = ""

            if wants_shot:
                if _cancelled():
                    return
                if display.strip():
                    self.assistant_message.emit(display)
                else:
                    self.assistant_message.emit("Looking at your screen…")
                _log("desktop screenshot capture starting…")
                path = self._capture_desktop_blocking(generation)
                if _cancelled():
                    return
                if not path:
                    self.error.emit(
                        "Desktop screenshot failed (client capture). "
                        "Try the 📷 button or check display permissions."
                    )
                    if not display.strip():
                        self._set_state(AvatarState.IDLE)
                        return
                    # Fall through to TTS the short ack only
                else:
                    shot_md = f"![desktop screenshot]({path})"
                    self.user_message.emit(shot_md)
                    follow_api = (
                        "[Desktop Avatar] Here is the live desktop screenshot "
                        "(client capture). Describe clearly what you see. "
                        "You can see this image via vision. "
                        "Do not claim you cannot see the screen. "
                        "Do not emit [[DESKTOP_SCREENSHOT]] again for this image."
                    )
                    _log("screenshot follow-up -> Hermes path=%s", path)
                    try:
                        response2 = avatar_chat(
                            follow_api,
                            image_paths=[path],
                            is_cancelled=_cancelled,
                            request_holder=holder,
                        )
                    except ChatCancelled:
                        _log("screenshot follow-up cancelled gen=%s", generation)
                        return
                    if _cancelled():
                        return
                    _log(
                        "chat <- Hermes screenshot follow-up: %s",
                        (response2 or "")[:200],
                    )
                    if not response2:
                        self.error.emit(
                            "Hermes returned an empty reply after screenshot."
                        )
                        self._set_state(AvatarState.IDLE)
                        return
                    _ignored2, cleaned2 = extract_media(response2)
                    display = cleaned2 if cleaned2 else response2
                    display, _again = _strip_screenshot_tag(display)
                    if _again:
                        _log("ignored nested [[DESKTOP_SCREENSHOT]] in follow-up")

            if _cancelled():
                return
            if not (display or "").strip():
                self.error.emit("Hermes returned an empty reply.")
                self._set_state(AvatarState.IDLE)
                return

            paths: list[str] = []
            source = "none"

            # Avatar-owned TTS: always speak the exact display text when voice is on.
            if self.config.voice_replies and display.strip():
                if _cancelled():
                    return
                from .hermes_tts import synthesize as hermes_synthesize
                from .local_tts import synthesize_to_file

                hermes_path = hermes_synthesize(
                    display,
                    preferred_language=self.config.language,
                )
                if _cancelled():
                    return
                if hermes_path:
                    paths = [hermes_path]
                    source = "hermes-tts-tool"
                elif self.config.edge_fallback:
                    edge_voice = self.config.tts_voice
                    lang = (self.config.language or "en").lower()
                    if lang.startswith("tr") and not edge_voice.lower().startswith("tr-"):
                        edge_voice = "tr-TR-EmelNeural"
                    elif lang.startswith("en") and not edge_voice.lower().startswith("en-"):
                        edge_voice = "en-US-JennyNeural"
                    _log(
                        "Hermes tts_tool failed — Edge fallback (voice=%s)",
                        edge_voice,
                    )
                    local = synthesize_to_file(
                        display,
                        voice=edge_voice,
                        rate=self.config.tts_rate,
                    )
                    if local:
                        paths = [local]
                        source = "edge-fallback"
                if not paths and not _cancelled():
                    self.error.emit(
                        "Voice was enabled but TTS failed (Hermes tts_tool + Edge). "
                        "Check HERMES_HOME / hermes-agent setup and the edge-tts package."
                    )

            if _cancelled():
                return

            if paths:
                _log("audio paths (%s): %s", source, paths)

            self._set_state(AvatarState.TALKING)
            self.assistant_message.emit(display)
            if paths:
                # Stay in TALKING until AudioPlayer.finished → notify_tts_finished.
                # Do not use text-length timers here — they end talking while TTS
                # is still playing (and the old 60s safety net did the same for
                # longer replies).
                self._awaiting_audio = True
                self.assistant_audio.emit(paths)
                self._schedule_idle_ms.emit(_TTS_SAFETY_IDLE_MS)
            else:
                self._awaiting_audio = False
                self._schedule_idle_ms.emit(_talking_display_ms(display))
        except Exception as exc:
            if self._is_stale(generation):
                _log("stale gen=%s swallowed error: %s", generation, exc)
                return
            _log("chat CRASH:\n%s", traceback.format_exc())
            self.error.emit(f"Hermes request failed: {exc}")
            self._set_state(AvatarState.IDLE)
        finally:
            # Ensure the stream is closed if the worker exits mid-flight
            # (cancel flag alone is enough when chat() is still in its loop;
            # this covers early returns before chat() / after exceptions).
            try:
                holder["cancel"] = True
            except Exception:
                pass
            with self._lock:
                if generation == self._generation:
                    self._busy = False
                    if self._request_holder is holder:
                        self._request_holder = {}
                # else: a newer generation owns busy
            if job_done is not None:
                job_done.set()

    def _arm_idle_timer(self, ms: int) -> None:
        self._idle_timer.stop()
        self._idle_timer.start(max(500, int(ms)))

    def _maybe_idle(self) -> None:
        """Timer fallback to leave TALKING.

        Normal TTS exit is ``notify_tts_finished``. While ``_awaiting_audio`` is
        set we do *not* use short text timers; the only timer armed is a long
        stuck-playback safety net (see ``_TTS_SAFETY_IDLE_MS``). If that fires
        without a finished signal, force IDLE so the avatar cannot stick forever.
        """
        if self._state != AvatarState.TALKING:
            return
        if self._awaiting_audio:
            _log(
                "TTS safety timeout — forcing IDLE "
                "(AudioPlayer.finished never arrived)"
            )
            self._awaiting_audio = False
        self._set_state(AvatarState.IDLE)

    def notify_tts_finished(self) -> None:
        """App layer: audio queue finished (or skipped). Leave TALKING → IDLE."""
        # Ignore if a newer turn already took over (barge-in).
        if self._state != AvatarState.TALKING:
            self._awaiting_audio = False
            return
        _log("TTS finished → IDLE")
        self._awaiting_audio = False
        self._idle_timer.stop()
        self._set_state(AvatarState.IDLE)

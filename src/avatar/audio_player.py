"""Sequential local audio playback for Hermes-generated voice files.

Windows / Qt Multimedia (FFmpeg) hard-crash note
------------------------------------------------
Calling ``pause()``, ``stop()``, ``setSource()``, or destroying a
``QMediaPlayer`` while it is decoding a long MP3 can kill the whole
process with no Python traceback (native FFmpeg fault).

Barge-in strategy:
  1. Mute the current output (instant silence).
  2. Disconnect signals and **orphan** the player (keep it alive muted;
     never pause/stop/delete it from the UI thread).
  3. Build a fresh player the next time we need to play something.

Orphaned players may finish decoding in the background (inaudible). That
is intentional and far safer than tearing them down mid-stream.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

logger = logging.getLogger(__name__)


class AudioPlayer(QObject):
    """Play a queue of local files; emit finished when the queue is empty."""

    finished = Signal()
    error = Signal(str)
    started = Signal(str)  # path

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue: list[str] = []
        self._stopping = False
        self._player: QMediaPlayer | None = None
        self._audio: QAudioOutput | None = None
        # True while we own an active play session (until finished or stop).
        self._session_active = False
        # True after playback actually reached PlayingState for current item.
        self._item_playing = False
        # Prevent double-advance from EndOfMedia + StoppedState + poll.
        self._advance_pending = False
        # Players muted & abandoned on barge-in — must not be pause/stop/delete'd.
        self._orphans: list[tuple[QMediaPlayer, QAudioOutput]] = []
        self._poll = QTimer(self)
        self._poll.setInterval(250)
        self._poll.timeout.connect(self._on_poll)
        self._build_player()

    def _build_player(self) -> None:
        player = QMediaPlayer(self)
        audio = QAudioOutput(self)
        player.setAudioOutput(audio)
        audio.setVolume(1.0)
        audio.setMuted(False)
        player.mediaStatusChanged.connect(self._on_status)
        player.playbackStateChanged.connect(self._on_playback_state)
        player.errorOccurred.connect(self._on_error)
        self._player = player
        self._audio = audio

    def _disconnect_player(self, player: QMediaPlayer | None) -> None:
        if player is None:
            return
        for signal, slot in (
            (player.mediaStatusChanged, self._on_status),
            (player.playbackStateChanged, self._on_playback_state),
            (player.errorOccurred, self._on_error),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def _orphan_current(self) -> None:
        """Mute and abandon the live player without native teardown."""
        self._poll.stop()
        self._item_playing = False
        self._advance_pending = False
        player, audio = self._player, self._audio
        self._player = None
        self._audio = None
        if player is None and audio is None:
            return
        try:
            if audio is not None:
                audio.setVolume(0.0)
                audio.setMuted(True)
        except Exception:
            logger.exception("orphan mute failed")
        self._disconnect_player(player)
        if player is not None and audio is not None:
            # Keep as QObject children for the process lifetime. Never
            # deleteLater/stop them — destruction mid-decode crashes FFmpeg.
            self._orphans.append((player, audio))
            if len(self._orphans) > 16:
                logger.warning(
                    "audio orphan count=%d (muted decoders left alive on purpose)",
                    len(self._orphans),
                )
        logger.info("audio orphaned for barge-in (muted, no pause/stop)")

    def play_paths(self, paths: list[str]) -> None:
        """Replace queue and start (or no-op if empty)."""
        try:
            self.stop(emit_finished=False)
            if self._player is None or self._audio is None:
                self._build_player()
            valid = [p for p in paths if p and Path(p).is_file()]
            if not valid:
                self._session_active = False
                self.finished.emit()
                return
            self._queue = list(valid)
            self._session_active = True
            self._item_playing = False
            self._advance_pending = False
            try:
                assert self._audio is not None
                self._audio.setMuted(False)
                self._audio.setVolume(1.0)
            except Exception:
                pass
            self._play_next()
        except Exception:
            logger.exception("audio play_paths failed")
            self._session_active = False
            self._poll.stop()
            try:
                self.finished.emit()
            except Exception:
                pass

    def stop(self, *, emit_finished: bool = False) -> None:
        """Silence immediately for barge-in without touching the FFmpeg decoder.

        Never calls ``pause()`` / ``stop()`` / ``setSource()`` / ``deleteLater()``
        on an active decoder — those hard-crash Windows Qt+FFmpeg builds.
        """
        self._stopping = True
        self._session_active = False
        self._queue.clear()
        try:
            self._orphan_current()
        except Exception:
            logger.exception("audio stop/orphan failed")
        finally:
            self._stopping = False
        if emit_finished:
            try:
                self.finished.emit()
            except Exception:
                pass

    def _emit_finished(self) -> None:
        self._poll.stop()
        self._session_active = False
        self._item_playing = False
        self._advance_pending = False
        self.finished.emit()

    def _play_next(self) -> None:
        if self._stopping:
            return
        self._advance_pending = False
        self._item_playing = False
        if not self._queue:
            if self._session_active:
                logger.info("audio queue empty → finished")
                self._emit_finished()
            return
        if self._player is None or self._audio is None:
            try:
                self._build_player()
            except Exception:
                logger.exception("audio rebuild failed")
                self._emit_finished()
                return
        assert self._player is not None
        path = self._queue.pop(0)
        logger.info("audio play: %s", path)
        try:
            self.started.emit(path)
            try:
                assert self._audio is not None
                self._audio.setMuted(False)
                self._audio.setVolume(1.0)
            except Exception:
                pass
            self._player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
            self._player.play()
            self._poll.start()
        except Exception:
            logger.exception("audio play next failed: %s", path)
            self._play_next()

    def _request_advance(self, reason: str) -> None:
        """Move to next queue item once per media item."""
        if self._stopping or not self._session_active:
            return
        if self._advance_pending:
            return
        self._advance_pending = True
        self._item_playing = False
        logger.info("audio advance (%s)", reason)
        self._play_next()

    def _is_near_end(self) -> bool:
        """True if position is at/near duration (real end, not a spurious signal)."""
        player = self._player
        if player is None:
            return True
        try:
            dur = int(player.duration() or 0)
            pos = int(player.position() or 0)
        except Exception:
            return True
        # Duration unknown (0): trust EndOfMedia only after we actually played.
        if dur <= 0:
            return self._item_playing
        # Allow small clock skew near the end; reject "end" at the start.
        return pos >= max(0, dur - 400)

    def _on_status(self, status) -> None:
        if self._stopping or not self._session_active:
            return
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        # Qt sometimes emits EndOfMedia very early (load glitch / metadata).
        if not self._is_near_end():
            try:
                pos = self._player.position() if self._player else -1
                dur = self._player.duration() if self._player else -1
            except Exception:
                pos, dur = -1, -1
            logger.warning(
                "ignoring early EndOfMedia (pos=%s dur=%s); keep/resume playing",
                pos,
                dur,
            )
            self._try_resume()
            return
        self._request_advance("EndOfMedia")

    def _try_resume(self) -> None:
        """If Qt glitched mid-clip, nudge playback without tearing down the player."""
        if self._stopping or not self._session_active or self._player is None:
            return
        try:
            if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                self._player.play()
        except Exception:
            logger.exception("audio resume after early EndOfMedia failed")

    def _on_playback_state(self, state) -> None:
        if self._stopping or not self._session_active:
            return
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._item_playing = True
            return
        if state != QMediaPlayer.PlaybackState.StoppedState:
            return
        # Stopped after real playback + end of media → advance.
        if not self._item_playing:
            return
        try:
            status = (
                self._player.mediaStatus()
                if self._player is not None
                else QMediaPlayer.MediaStatus.NoMedia
            )
        except Exception:
            status = QMediaPlayer.MediaStatus.NoMedia
        if status == QMediaPlayer.MediaStatus.EndOfMedia or self._is_near_end():
            self._request_advance("StoppedState")
            return
        # Spurious stop mid-clip — resume so talking/TTS stay in sync.
        logger.warning("audio StoppedState mid-clip; resuming")
        self._try_resume()

    def _on_poll(self) -> None:
        """Backup completion check when EndOfMedia is late or missing."""
        if self._stopping or not self._session_active or self._player is None:
            return
        if not self._item_playing:
            return
        try:
            state = self._player.playbackState()
            dur = int(self._player.duration() or 0)
            pos = int(self._player.position() or 0)
        except Exception:
            return
        if state != QMediaPlayer.PlaybackState.PlayingState:
            return
        if dur > 0 and pos >= max(0, dur - 80):
            # Natural end; EndOfMedia should follow — if not, advance soon.
            # Wait one extra tick so we prefer the normal signal path.
            if pos >= dur:
                self._request_advance("position>=duration")

    def _on_error(self, *_args) -> None:
        if self._stopping:
            return
        err = ""
        try:
            if self._player is not None:
                err = self._player.errorString() or ""
        except Exception:
            pass
        err = err or "audio playback error"
        logger.warning("audio error: %s", err)
        self.error.emit(err)
        if self._session_active:
            self._request_advance("error")

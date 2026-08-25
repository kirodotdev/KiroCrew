"""Recording session state machine.

Manages the lifecycle of a single recording from start through post-processing.
The state machine enforces valid transitions and keeps a registry of active
sessions so the WebSocket handler and recovery logic can inspect them.

State diagram::

    idle ──start──▶ recording ──pause──▶ paused ──resume──▶ recording
                        │                                       │
                        └──────────────stop─────────────────────┘
                                         │
                                         ▼
                                   processing ──▶ complete | failed
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from kiro_crew.recording.persister import LiveTranscriptPersister
from kiro_crew.recording.writer import WavWriter

logger = logging.getLogger(__name__)


class SessionState(enum.Enum):
    """Recording session states."""

    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"


# Valid transitions: (from_state, action) → to_state
_TRANSITIONS: dict[tuple[SessionState, str], SessionState] = {
    (SessionState.IDLE, "start"): SessionState.RECORDING,
    (SessionState.RECORDING, "pause"): SessionState.PAUSED,
    (SessionState.RECORDING, "stop"): SessionState.PROCESSING,
    (SessionState.PAUSED, "resume"): SessionState.RECORDING,
    (SessionState.PAUSED, "stop"): SessionState.PROCESSING,
    (SessionState.PROCESSING, "complete"): SessionState.COMPLETE,
    (SessionState.PROCESSING, "fail"): SessionState.FAILED,
}


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed."""


@dataclass
class RecordingSession:
    """A single recording session with state machine, WAV writer, and persister.

    Parameters
    ----------
    meeting_id:
        Unique identifier for this recording.  Generated if not provided.
    storage_dir:
        Directory where audio and transcript files will be written.
    title:
        Optional meeting title.
    language:
        BCP 47 language code for transcription.
    """

    meeting_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    storage_dir: Optional[Path] = None
    title: str = ""
    language: str = "en"

    # Internal state
    _state: SessionState = field(default=SessionState.IDLE, init=False)
    _writer: Optional[WavWriter] = field(default=None, init=False, repr=False)
    _persister: Optional[LiveTranscriptPersister] = field(default=None, init=False, repr=False)
    _started_at: Optional[float] = field(default=None, init=False)
    _paused_at: Optional[float] = field(default=None, init=False)
    _total_paused_secs: float = field(default=0.0, init=False)

    @property
    def state(self) -> SessionState:
        """Current session state."""
        return self._state

    @property
    def started_at(self) -> Optional[float]:
        """Monotonic time when recording started, or None."""
        return self._started_at

    @property
    def duration_secs(self) -> float:
        """Elapsed recording time in seconds, excluding pauses."""
        if self._started_at is None:
            return 0.0
        now = time.monotonic()
        elapsed = now - self._started_at - self._total_paused_secs
        if self._state == SessionState.PAUSED and self._paused_at is not None:
            elapsed -= now - self._paused_at
        return max(0.0, elapsed)

    def _transition(self, action: str) -> SessionState:
        """Apply a state transition or raise InvalidTransitionError."""
        key = (self._state, action)
        new_state = _TRANSITIONS.get(key)
        if new_state is None:
            raise InvalidTransitionError(f"Cannot '{action}' in state {self._state.value}")
        self._state = new_state
        return new_state

    async def start(self) -> None:
        """Start recording.  Initializes the WAV writer and persister.

        Raises InvalidTransitionError if not in IDLE state.
        """
        self._transition("start")
        self._started_at = time.monotonic()

        if self.storage_dir is not None:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            audio_path = self.storage_dir / "audio.wav"
            self._writer = WavWriter(audio_path)
            await self._writer.open()

            transcript_path = self.storage_dir / "transcript_local.json"
            self._persister = LiveTranscriptPersister(transcript_path)

    async def pause(self) -> None:
        """Pause recording.  Audio frames received while paused are discarded.

        Raises InvalidTransitionError if not in RECORDING state.
        """
        self._transition("pause")
        self._paused_at = time.monotonic()
        # Flush any pending transcript to disk before pausing
        if self._persister is not None:
            await self._persister.flush()

    async def resume(self) -> None:
        """Resume recording after a pause.

        Raises InvalidTransitionError if not in PAUSED state.
        """
        self._transition("resume")
        if self._paused_at is not None:
            self._total_paused_secs += time.monotonic() - self._paused_at
            self._paused_at = None

    async def stop(self) -> None:
        """Stop recording and finalize files.

        Moves to PROCESSING state. The caller is responsible for calling
        :meth:`mark_complete` or :meth:`mark_failed` once post-processing
        finishes.

        Raises InvalidTransitionError if not in RECORDING or PAUSED state.
        """
        self._transition("stop")
        if self._paused_at is not None:
            self._total_paused_secs += time.monotonic() - self._paused_at
            self._paused_at = None

        if self._writer is not None:
            await self._writer.close()
        if self._persister is not None:
            await self._persister.flush()

    def mark_complete(self) -> None:
        """Mark session as successfully completed.

        Raises InvalidTransitionError if not in PROCESSING state.
        """
        self._transition("complete")

    def mark_failed(self) -> None:
        """Mark session as failed.

        Raises InvalidTransitionError if not in PROCESSING state.
        """
        self._transition("fail")

    async def write_audio(self, pcm_data: bytes) -> None:
        """Write a PCM audio frame.  No-op if paused or not recording.

        The actual I/O is offloaded to the subprocess executor so it never
        blocks the event loop.
        """
        if self._state != SessionState.RECORDING:
            return
        if self._writer is not None:
            await self._writer.write(pcm_data)

    async def add_transcript_segment(self, text: str, *, is_final: bool = False) -> None:
        """Add a transcript segment.  Triggers debounced persistence.

        Parameters
        ----------
        text:
            The transcript text.
        is_final:
            Whether this is a finalized (stable) segment vs an interim partial.
        """
        if self._persister is not None:
            await self._persister.add_segment(text, is_final=is_final)

    async def close(self) -> None:
        """Clean up resources.  Safe to call in any state."""
        if self._writer is not None:
            try:
                await self._writer.close()
            except Exception:
                logger.debug("Error closing WAV writer", exc_info=True)
            self._writer = None
        if self._persister is not None:
            try:
                await self._persister.flush()
            except Exception:
                logger.debug("Error flushing persister", exc_info=True)
            self._persister = None


# ---------------------------------------------------------------------------
# Session registry — tracks active sessions for concurrency limiting and
# crash recovery.
# ---------------------------------------------------------------------------

_active_sessions: dict[str, RecordingSession] = {}
_registry_lock = asyncio.Lock()

# Maximum concurrent recording sessions (design: 1).
MAX_CONCURRENT_SESSIONS = 1


async def register_session(session: RecordingSession) -> bool:
    """Register a session.  Returns False if the concurrency cap is reached."""
    async with _registry_lock:
        if len(_active_sessions) >= MAX_CONCURRENT_SESSIONS:
            return False
        _active_sessions[session.meeting_id] = session
        return True


async def unregister_session(meeting_id: str) -> None:
    """Remove a session from the registry."""
    async with _registry_lock:
        _active_sessions.pop(meeting_id, None)


def get_active_session() -> Optional[RecordingSession]:
    """Return the currently active session, or None."""
    for session in _active_sessions.values():
        return session
    return None


def active_session_count() -> int:
    """Return the number of currently active sessions."""
    return len(_active_sessions)

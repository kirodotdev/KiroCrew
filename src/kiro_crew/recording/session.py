"""Recording session state machine.

Manages one recording from start through WAV finalization.

The state machine enforces start, pause, resume and stop transitions. A small
registry lets the WebSocket enforce its concurrency bound.
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

from kiro_crew.executors import subprocess_executor
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.recording.writer import WavWriter

logger = logging.getLogger(__name__)


class SessionState(enum.Enum):
    """Recording session states."""

    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    PROCESSING = "processing"


# Valid transitions: (from_state, action) → to_state
_TRANSITIONS: dict[tuple[SessionState, str], SessionState] = {
    (SessionState.IDLE, "start"): SessionState.RECORDING,
    (SessionState.RECORDING, "pause"): SessionState.PAUSED,
    (SessionState.RECORDING, "stop"): SessionState.PROCESSING,
    (SessionState.PAUSED, "resume"): SessionState.RECORDING,
    (SessionState.PAUSED, "stop"): SessionState.PROCESSING,
}


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed."""


# Artifact names a session writes into its storage directory. The first
# recording of a meeting is ``audio.wav``; every later one gets its own numbered
# file (``audio-2.wav``, ``audio-3.wav``, ...), because the alternative --
# reopening ``audio.wav`` -- truncated the previous recording to nothing.
AUDIO_FILENAME = "audio.wav"
AUDIO_FILENAME_GLOB = "audio*.wav"
_AUDIO_STEM = "audio"
_AUDIO_SUFFIX = ".wav"


def next_audio_path(storage_dir: Path) -> Path:
    """The path a NEW recording in *storage_dir* should write to.

    ``audio.wav`` when the directory holds no recording yet, otherwise the first
    free ``audio-<n>.wav`` (n from 2). Existence is the only criterion: a
    zero-byte or half-written file from a crashed session is still a file a user
    may want, so it is stepped over rather than reused. The writer additionally
    opens the chosen path exclusively, so a race on the same name fails loudly
    instead of silently overwriting.
    """
    first = storage_dir / AUDIO_FILENAME
    if not first.exists():
        return first
    n = 2
    while True:
        candidate = storage_dir / f"{_AUDIO_STEM}-{n}{_AUDIO_SUFFIX}"
        if not candidate.exists():
            return candidate
        n += 1


@dataclass
class RecordingSession:
    """A single recording session with a state machine and a WAV writer.

    Parameters
    ----------
    meeting_id:
        Unique identifier for this recording.  Generated if not provided.
    storage_dir:
        Directory the audio file is written into.  ``None`` persists nothing
        (dictation, a voice note).
    """

    meeting_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    storage_dir: Optional[Path] = None

    # Internal state
    _state: SessionState = field(default=SessionState.IDLE, init=False)
    _writer: Optional[WavWriter] = field(default=None, init=False, repr=False)
    _audio_path: Optional[Path] = field(default=None, init=False)
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
    def audio_path(self) -> Optional[Path]:
        """The WAV file this session wrote, once started with a storage dir.

        Survives :meth:`close`, so a caller can still name the file afterwards.
        """
        return self._audio_path

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
        """Start recording.  Opens a NEW WAV file in the storage directory.

        A meeting that is recorded more than once (stop then record again, or a
        dropped socket) gets one file per recording:
        see :func:`next_audio_path`. Nothing already on disk is opened.

        Raises InvalidTransitionError if not in IDLE state.
        """
        self._transition("start")
        self._started_at = time.monotonic()

        if self.storage_dir is not None:
            loop = asyncio.get_running_loop()
            audio_path = await loop.run_in_executor(
                subprocess_executor(), next_audio_path, self.storage_dir
            )
            writer = WavWriter(audio_path)
            # Exclusive create: raises rather than truncating an existing file.
            # WavWriter creates the parent on the same executor as the open.
            await writer.open()
            self._writer = writer
            self._audio_path = writer.path

    async def pause(self) -> None:
        """Pause recording.  Audio frames received while paused are discarded.

        Raises InvalidTransitionError if not in RECORDING state.
        """
        self._transition("pause")
        self._paused_at = time.monotonic()

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

        Moves to PROCESSING state, which means capture has ended and the WAV
        header has been finalized.

        Raises InvalidTransitionError if not in RECORDING or PAUSED state.
        """
        self._transition("stop")
        if self._paused_at is not None:
            self._total_paused_secs += time.monotonic() - self._paused_at
            self._paused_at = None

        if self._writer is not None:
            await self._writer.close()

    async def write_audio(self, pcm_data: bytes) -> None:
        """Write a PCM audio frame.  No-op if paused or not recording.

        The actual I/O is offloaded to the subprocess executor so it never
        blocks the event loop.
        """
        if self._state != SessionState.RECORDING:
            return
        if self._writer is not None:
            await self._writer.write(pcm_data)

    async def close(self) -> None:
        """Clean up resources.  Safe to call in any state."""
        if self._writer is not None:
            writer = self._writer
            self._writer = None
            await writer.close()


# ---------------------------------------------------------------------------
# Session registry — tracks active sessions for concurrency limiting.
# ---------------------------------------------------------------------------

_active_sessions: dict[str, RecordingSession] = {}
# One inner lock per running loop: a bare module-global ``asyncio.Lock`` binds
# to the loop it is first used on and raises when a later loop (a second test,
# an in-process gateway restart) acquires it. See ``kiro_crew.loop_lock``.
_registry_lock = LoopBoundLock()

# Maximum concurrent recording sessions (design: 1). The single source of
# truth: the socket handler's pre-upgrade guard imports this rather than
# carrying its own copy.
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


def active_session_count() -> int:
    """Return the number of currently active sessions."""
    return len(_active_sessions)

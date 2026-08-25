"""Recording session management for audio capture and live transcription.

This package provides the state machine, WAV writer, and debounced
transcript persister used by the ``/api/ws/recording`` WebSocket endpoint.
It lives in core (not in the MeetNote app) because audio ingest plus
transcription is not MeetNote-specific — dictation and voice notes reuse
the same socket.

Every blocking step — WAV writes, transcript persistence — is offloaded
via ``run_in_executor(subprocess_executor(), …)`` so nothing blocks the
asyncio event loop.
"""

from __future__ import annotations

from kiro_crew.recording.persister import LiveTranscriptPersister
from kiro_crew.recording.recovery import (
    UnfinishedSession,
    detect_unfinished_sessions,
    discard_session,
    resume_session,
)
from kiro_crew.recording.session import RecordingSession, SessionState
from kiro_crew.recording.writer import WavWriter
from kiro_crew.recording.ws import api_ws_recording

__all__ = [
    "LiveTranscriptPersister",
    "RecordingSession",
    "SessionState",
    "UnfinishedSession",
    "WavWriter",
    "api_ws_recording",
    "detect_unfinished_sessions",
    "discard_session",
    "resume_session",
]

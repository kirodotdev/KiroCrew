"""Recording session management for audio capture.

This package provides the state machine and WAV writer used by the
``/api/ws/recording`` WebSocket endpoint. It lives in core (not in the
Meetings app) because audio ingest is not meetings-specific — dictation and
voice notes reuse the same socket. Transcription is deliberately NOT here: the
browser streams the same audio to ``/api/ws/stt``, and this socket only
persists it.

Every blocking step — the WAV writes — is offloaded via
``run_in_executor(subprocess_executor(), …)`` so nothing blocks the asyncio
event loop.
"""

from __future__ import annotations

from kiro_crew.recording.session import RecordingSession, SessionState
from kiro_crew.recording.writer import WavWriter
from kiro_crew.recording.ws import api_ws_recording

__all__ = [
    "RecordingSession",
    "SessionState",
    "WavWriter",
    "api_ws_recording",
]

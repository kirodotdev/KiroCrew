"""Adapt external speech sessions to the existing audited speech-session driver."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig


async def run_codex_session(
    ws: web.WebSocketResponse,
    cfg: KiroCrewConfig,
    request: web.Request,
    caller: str,
) -> None:
    """Keep the normal caps, redaction, endpointing, and audit trail for Codex."""
    from kiro_crew.dashboard import stt_stream
    from kiro_crew.stt.codex import CODE_UNAVAILABLE, StreamingSession

    with stt_stream._audited_setup(caller):
        session = StreamingSession()
        endpointer = stt_stream._build_endpointer(ws, cfg, request)
    await stt_stream._drive_helper_session(
        ws,
        caller,
        session,
        endpointer,
        start_failure_code=lambda: session.failure.code if session.failure else CODE_UNAVAILABLE,
        label="codex",
    )


async def _run_registered_session(
    ws: web.WebSocketResponse,
    cfg: KiroCrewConfig,
    request: web.Request,
    caller: str,
) -> None:
    """Drive an edition provider with the same redaction, caps, endpointing and audit."""
    from kiro_crew import speech_providers
    from kiro_crew.dashboard import stt_stream

    with stt_stream._audited_setup(caller):
        provider = speech_providers.stt_provider(cfg.stt.provider)
        if provider is None or not speech_providers.streams(provider):
            raise RuntimeError(f"STT provider {cfg.stt.provider!r} cannot stream")
        endpointer = stt_stream._build_endpointer(ws, cfg, request)
        session = provider.open_stream(cfg.stt)
    await stt_stream._drive_helper_session(
        ws,
        caller,
        session,
        endpointer,
        start_failure_code=lambda: stt_stream._CODE_SESSION_FAILED,
        label=f"STT provider {cfg.stt.provider!r}",
    )

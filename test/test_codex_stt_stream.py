"""Exercise the real dictation WebSocket with a synthetic Codex session."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from stt_stream_helpers import _cfg, _make_app, _recv_json, _wait_for_operation

from kiro_crew.dashboard import stt_stream
from kiro_crew.stt import codex


class FakeSession:
    def __init__(self, *, startup_error=False):
        self.queue = asyncio.Queue()
        self.closed = False
        self.failure = (
            codex.CodexError("stt_codex_login_required", "Sign in") if startup_error else None
        )

    async def start(self):
        if self.failure:
            self.closed = True
            return "Sign in"
        return None

    async def feed(self, pcm):
        self.queue.put_nowait({"type": "partial", "text": "private dictation"})
        return True

    async def events(self):
        while (event := await self.queue.get()) is not None:
            yield event

    async def finish(self):
        self.queue.put_nowait({"type": "final", "text": "private dictation"})
        self.queue.put_nowait(None)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_error", [False, True])
async def test_codex_socket_uses_normal_redaction_audit_and_cleanup(monkeypatch, startup_error):
    session = FakeSession(startup_error=startup_error)
    audit = []
    monkeypatch.setattr(codex, "StreamingSession", lambda: session)
    monkeypatch.setattr(
        stt_stream.KiroCrewConfig, "load", classmethod(lambda cls: _cfg(provider="codex"))
    )
    monkeypatch.setattr(stt_stream, "check_origin", lambda request, require: True)
    monkeypatch.setattr(
        stt_stream,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **event: audit.append(event)),
    )
    monkeypatch.setattr(stt_stream, "_build_endpointer", lambda *args: None)
    monkeypatch.setattr(stt_stream, "_redacted", lambda text: text.replace("private", "safe"))
    aws = AsyncMock(side_effect=AssertionError("Codex must never fall back to AWS"))
    monkeypatch.setattr(stt_stream.aws_consent, "authorize", aws)
    async with TestClient(TestServer(_make_app(), host="127.0.0.1")) as client:
        ws = await client.ws_connect("/api/ws/stt")
        first = await _recv_json(ws)
        if startup_error:
            assert first["type"] == "error"
            assert first["code"] == "stt_codex_login_required"
        else:
            assert first["type"] == "ready"
            await ws.send_bytes(b"\0" * 3200)
            assert await _recv_json(ws) == {"type": "partial", "text": "safe dictation"}
            await ws.send_json({"type": "stop"})
            assert await _recv_json(ws) == {"type": "final", "text": "safe dictation"}
        await ws.close()
    await _wait_for_operation(audit, "stt_stream_end")
    assert session.closed
    assert [event["operation"] for event in audit] == ["stt_stream_start", "stt_stream_end"]
    aws.assert_not_awaited()

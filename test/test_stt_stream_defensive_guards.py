"""Failures in the machinery around a stream must not change its outcome.

A raising ``sel()``, a client constructor that raises, a start audit that raises and a
cleanup ``ws.close()`` that raises each leave the handler's status, the closed socket
and the balanced audit pair as they were. The endpoint guards and the provider sessions
live in ``test_stt_stream.py``; the shared socket helpers in ``stt_stream_helpers.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from stt_stream_helpers import transcribe_consented_fixture  # noqa: F401
from stt_stream_helpers import _cfg, _make_app, _wait_for_operation


class TestDefensiveGuards:
    """Failures in the machinery AROUND the stream must not change its outcome.

    Every case here breaks something the handler only uses in passing (the audit
    log, the AWS client constructor, the socket's own ``close()``) and asserts the
    handler still reaches the answer it was going to give: the intended status code,
    a closed WebSocket, and a balanced audit trail.
    """

    @pytest.fixture(autouse=True)
    def _consented(self, transcribe_consented):
        """Every case here drives ``api_ws_stt``, which is consent-gated."""

    @pytest.mark.asyncio
    async def test_guard_audit_sel_failure_preserves_status_code(self, monkeypatch):
        """If sel() raises on a guard rejection, client must still get 403/503, not 500.

        An unwrapped ``sel().log_api_access()`` on the origin, availability or
        concurrency guards would propagate and mask the intended
        HTTPForbidden/HTTPServiceUnavailable.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg()),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: False)
        # sel() itself raises — worst case. _emit_guard_audit must swallow.

        def _raising_sel():
            raise RuntimeError("SEL not initialized")

        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.sel", _raising_sel)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/stt")
            # Must be 403 (from HTTPForbidden), not 500.
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_client_construction_failure_closes_ws_and_emits_end_audit(self, monkeypatch):
        """If TranscribeStreamingClient() raises, WS must close and end audit emit.

        Unwrapped resolver/client construction (an invalid profile, a bad region)
        would leak a prepare()d WebSocket and leave an unmatched stt_stream_start in
        the audit trail. The frame's code says "the session failed", not "something
        is missing": there is nothing for the user to install.
        """
        pytest.importorskip("amazon_transcribe")
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg()),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)
        # Force TranscribeStreamingClient constructor to raise.

        def _raising_client(**kw):
            raise RuntimeError("bad region")

        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.TranscribeStreamingClient",
            _raising_client,
        )
        calls: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: calls.append(kw)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg == {
                "type": "error",
                "message": "failed to create transcription client",
                "code": "stt_session_failed",
            }
            await ws.close()
        # Wait for the end audit instead of assuming the handler already ran:
        # the error frame / client close is not a barrier for it.
        await _wait_for_operation(calls, "stt_stream_end")
        ops = [c["operation"] for c in calls]
        assert (
            "stt_stream_start" in ops and "stt_stream_end" in ops
        ), f"both start and end audit events required; got {ops}"
        end = next(c for c in calls if c["operation"] == "stt_stream_end")
        assert end["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_start_audit_sel_failure_closes_ws_and_emits_end_audit(self, monkeypatch):
        """If the stt_stream_start sel call raises, WS must close and end audit emit.

        An unwrapped ``sel().log_api_access()`` for stt_stream_start would propagate
        after ws.prepare() sent the 101 upgrade, leaking the WebSocket and leaving an
        unmatched start event. This frame precedes the provider branch, so it is the
        one error frame every provider can produce, and it carries a code for the
        same reason theirs do.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg()),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)
        calls: list[dict] = []
        # sel() itself returns an object whose log_api_access raises only for
        # the start operation — guard rejections are unreachable (origin ok,
        # streaming enabled, sessions free), and end-audit must still record.
        fake_sel = MagicMock()

        def _log(**kw):
            calls.append(kw)
            if kw.get("operation") == "stt_stream_start":
                raise RuntimeError("SEL unavailable")

        fake_sel.log_api_access = _log
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg == {
                "type": "error",
                "message": "audit subsystem unavailable",
                "code": "stt_session_failed",
            }
            await ws.close()
        # Wait for the end audit instead of assuming the handler already ran:
        # the error frame / client close is not a barrier for it.
        await _wait_for_operation(calls, "stt_stream_end")
        ops = [c["operation"] for c in calls]
        assert (
            "stt_stream_start" in ops and "stt_stream_end" in ops
        ), f"both start and end audit events required; got {ops}"
        end = next(c for c in calls if c["operation"] == "stt_stream_end")
        assert end["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_cleanup_ws_close_failure_still_emits_end_audit(self, monkeypatch):
        """If the cleanup ws.close() raises on broken transport, end audit still fires.

        An unwrapped ``await ws.close()`` on the normal cleanup path would skip
        _emit_end_audit when the transport is broken, leaving an unmatched
        stt_stream_start in the audit trail.
        """
        pytest.importorskip("amazon_transcribe")
        from amazon_transcribe.handlers import TranscriptResultStreamHandler

        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg()),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)

        # Stub Transcribe happy-path client.
        input_stream = MagicMock()
        input_stream.send_audio_event = AsyncMock()
        results_ended = asyncio.Event()
        input_stream.end_stream = AsyncMock(side_effect=results_ended.set)
        stream = MagicMock(input_stream=input_stream, output_stream=MagicMock())
        client = MagicMock()
        client.start_stream_transcription = AsyncMock(return_value=stream)
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.TranscribeStreamingClient",
            lambda **kw: client,
        )

        async def _results_until_end_stream(self) -> None:
            # flake-ok: the stubbed result stream parks here until the stream ends; moved verbatim
            await results_ended.wait()

        monkeypatch.setattr(
            TranscriptResultStreamHandler, "handle_events", _results_until_end_stream
        )

        # Patch WebSocketResponse.close to raise on the cleanup call.
        from aiohttp import web as _web

        real_close = _web.WebSocketResponse.close
        call_count = {"n": 0}

        async def _raising_close(self, *a, **kw):
            call_count["n"] += 1
            # First close call (cleanup path) raises; later ones (if any) succeed.
            if call_count["n"] == 1:
                raise ConnectionResetError("transport gone")
            return await real_close(self, *a, **kw)

        monkeypatch.setattr(_web.WebSocketResponse, "close", _raising_close)

        calls: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: calls.append(kw)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.sel", lambda: fake_sel)

        async with TestClient(TestServer(_make_app())) as http_client:
            ws = await http_client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_str('{"type":"stop"}')
            await ws.close()
        # Wait for the end audit instead of assuming the handler already ran:
        # the error frame / client close is not a barrier for it.
        await _wait_for_operation(calls, "stt_stream_end")
        ops = [c["operation"] for c in calls]
        assert (
            "stt_stream_start" in ops and "stt_stream_end" in ops
        ), f"both start and end audit events required; got {ops}"

"""Tests for streaming STT WebSocket endpoint (dashboard/stt_stream.py)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig, SttConfig

# Upper bound for _wait_for_operation. Generous because it only ever elapses on
# a genuine regression (the audit never fires); the happy path returns as soon
# as the handler's next step runs, so a large bound costs nothing in wall clock.
_AUDIT_WAIT_TIMEOUT_SECS = 5.0


async def _wait_for_operation(calls: list[dict], operation: str) -> None:
    """Await *operation* appearing in *calls*, or fail with what did arrive.

    The WS error-frame handshake does NOT order the client's assertion after the
    server's audit. Every early-return path in ``api_ws_stt`` runs
    ``send_json(error)`` -> ``ws.close()`` -> ``_emit_end_audit(...)``, so
    ``receive_json()`` returns on the error frame while the handler still has two
    steps to go. Exiting the ``TestClient`` context is not a barrier either: it
    closes the client side and does not await the server handler's coroutine to
    completion. Asserting on ``calls`` right after either point is therefore a
    race that fails whenever the event loop happens not to resume the handler
    first — reproduced at roughly 1-in-8 locally and seen intermittently on CI.

    Polling the real condition removes the guesswork: it returns the instant the
    audit lands and fails with a useful message if it never does.
    """

    async def _poll() -> None:
        while operation not in [c["operation"] for c in calls]:
            # sleep(0) yields to the loop so the pending handler continues; the
            # loop is single-threaded, so a busy-wait without it would hang.
            await asyncio.sleep(0)

    # asyncio.wait_for, not asyncio.timeout: the latter is 3.11+ and this project
    # supports 3.10 (CI runs a 3.10 shard).
    try:
        await asyncio.wait_for(_poll(), timeout=_AUDIT_WAIT_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"{operation!r} audit never emitted within {_AUDIT_WAIT_TIMEOUT_SECS}s; "
            f"got {[c['operation'] for c in calls]}"
        ) from None


def _make_app() -> web.Application:
    from kiro_crew.dashboard import stt_stream

    app = web.Application()
    app.router.add_get("/api/ws/stt", stt_stream.api_ws_stt)
    return app


def _cfg(**kwargs) -> KiroCrewConfig:
    stt = SttConfig(
        enabled=kwargs.pop("enabled", True),
        provider=kwargs.pop("provider", "transcribe"),
        streaming=kwargs.pop("streaming", True),
        **kwargs,
    )
    return KiroCrewConfig(stt=stt)


class TestGuards:
    @pytest.mark.asyncio
    async def test_rejects_when_streaming_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg(streaming=False)),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/stt")
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_rejects_when_provider_is_whisper(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg(provider="whisper")),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/stt")
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_rejects_when_stt_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg(enabled=False)),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/stt")
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_rejects_bad_origin(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg()),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: False)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/stt")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_bad_origin_emits_sel_rejection_audit(self, monkeypatch):
        """403 origin rejection must emit ``stt_stream_rejected`` SEL event.

        Regression: without audit, cross-origin probing attempts leave no
        trace in the audit trail.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg()),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: False)
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/stt")
            assert resp.status == 403
        fake_sel.log_api_access.assert_any_call(
            caller=ANY,
            operation="stt_stream_rejected",
            outcome="forbidden",
            resources="/api/ws/stt",
        )

    @pytest.mark.asyncio
    async def test_disabled_streaming_emits_sel_rejection_audit(self, monkeypatch):
        """503 (streaming not enabled) must emit ``stt_stream_rejected`` SEL event."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg(streaming=False)),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/stt")
            assert resp.status == 503
        fake_sel.log_api_access.assert_any_call(
            caller=ANY,
            operation="stt_stream_rejected",
            outcome="unavailable",
            resources="/api/ws/stt",
        )

    @pytest.mark.asyncio
    async def test_rejects_when_concurrent_cap_reached(self, monkeypatch):
        """New connections rejected with 503 once active sessions hit the cap.

        Regression: without a cap, a single user opening many tabs (or an
        attacker past origin) multiplies Transcribe cost and can exhaust
        the account concurrent-stream quota.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg()),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_CONCURRENT_SESSIONS", 1)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._active_sessions", 1)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/stt")
            assert resp.status == 503


class TestStreamLifecycle:
    """Mock TranscribeStreamingClient to verify lifecycle + redaction."""

    @pytest.fixture(autouse=True)
    def _require_amazon_transcribe(self):
        pytest.importorskip("amazon_transcribe")

    def _install_stubs(self, monkeypatch, *, fail_start=False):
        from amazon_transcribe.handlers import TranscriptResultStreamHandler

        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg()),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)

        # Stub Transcribe client.
        input_stream = MagicMock()
        input_stream.send_audio_event = AsyncMock()
        input_stream.end_stream = AsyncMock()
        stream = MagicMock()
        stream.input_stream = input_stream
        stream.output_stream = MagicMock()

        client = MagicMock()
        if fail_start:
            client.start_stream_transcription = AsyncMock(side_effect=RuntimeError("start failed"))
        else:
            client.start_stream_transcription = AsyncMock(return_value=stream)
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.TranscribeStreamingClient",
            lambda **kw: client,
        )

        # Stub handler so handle_events exits quickly.
        original_init = TranscriptResultStreamHandler.__init__
        monkeypatch.setattr(
            TranscriptResultStreamHandler,
            "__init__",
            lambda self, output_stream: original_init(self, output_stream),
        )
        monkeypatch.setattr(
            TranscriptResultStreamHandler,
            "handle_events",
            AsyncMock(return_value=None),
        )
        return client, input_stream

    @pytest.mark.asyncio
    async def test_ready_then_stop(self, monkeypatch):
        _, input_stream = self._install_stubs(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg == {"type": "ready"}
            await ws.send_bytes(b"\x00\x01" * 16)
            await ws.send_str('{"type":"stop"}')
            await ws.close()
        input_stream.send_audio_event.assert_awaited()
        input_stream.end_stream.assert_awaited()

    @pytest.mark.asyncio
    async def test_start_failure_emits_error(self, monkeypatch):
        self._install_stubs(monkeypatch, fail_start=True)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg["type"] == "error"
            await ws.close()

    @pytest.mark.asyncio
    async def test_start_failure_emits_sel_end_audit(self, monkeypatch):
        """Transcribe start failure must still emit ``stt_stream_end`` SEL audit.

        Regression: early-return paths must not skip the end event —
        the audit trail otherwise shows unmatched start events.
        """
        self._install_stubs(monkeypatch, fail_start=True)
        calls: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: calls.append(kw)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            await ws.receive_json()
            await ws.close()
        # Wait for the end audit instead of assuming the handler already ran:
        # the error frame / client close is not a barrier for it.
        await _wait_for_operation(calls, "stt_stream_end")
        ops = [c["operation"] for c in calls]
        assert "stt_stream_start" in ops and "stt_stream_end" in ops
        end = next(c for c in calls if c["operation"] == "stt_stream_end")
        assert end["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_import_error_emits_sel_end_audit(self, monkeypatch):
        """Missing ``amazon_transcribe`` at module-top-import time falls back to
        ``TranscribeStreamingClient = None``; the handler must still send a
        friendly WS error, close cleanly, and emit the matching end SEL audit.
        Covers the partial-install / stale-env recovery path.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg()),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.TranscribeStreamingClient", None
        )
        calls: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: calls.append(kw)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg == {"type": "error", "message": "amazon-transcribe not installed"}
            await ws.close()
        # Wait for the end audit instead of assuming the handler already ran:
        # the error frame / client close is not a barrier for it.
        await _wait_for_operation(calls, "stt_stream_end")
        ops = [c["operation"] for c in calls]
        assert "stt_stream_start" in ops and "stt_stream_end" in ops
        end = next(c for c in calls if c["operation"] == "stt_stream_end")
        assert end["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_final_transcript_is_redacted(self, monkeypatch):
        """The real _make_handler must redact credentials before emitting final."""
        from kiro_crew.dashboard import stt_stream

        captured: list[dict] = []

        class FakeWS:
            closed = False

            async def send_json(self, data):
                captured.append(data)

        alt = MagicMock(transcript="leaked AKIAIOSFODNN7EXAMPLE secret")
        result = MagicMock(is_partial=False, alternatives=[alt])
        event = MagicMock()
        event.transcript.results = [result]

        fake_ws = FakeWS()
        handler_cls = stt_stream._make_handler(fake_ws)
        h = handler_cls(MagicMock())
        await h.handle_transcript_event(event)

        assert captured and captured[0]["type"] == "final"
        assert "AKIAIOSFODNN7EXAMPLE" not in captured[0]["text"]

    @pytest.mark.asyncio
    async def test_partial_transcript_is_redacted(self, monkeypatch):
        """Partials are now redacted too (security-controls guideline)."""
        from kiro_crew.dashboard import stt_stream

        captured: list[dict] = []

        class FakeWS:
            closed = False

            async def send_json(self, data):
                captured.append(data)

        alt = MagicMock(transcript="partial AKIAIOSFODNN7EXAMPLE text")
        result = MagicMock(is_partial=True, alternatives=[alt])
        event = MagicMock()
        event.transcript.results = [result]

        fake_ws = FakeWS()
        handler_cls = stt_stream._make_handler(fake_ws)
        h = handler_cls(MagicMock())
        await h.handle_transcript_event(event)

        assert captured and captured[0]["type"] == "partial"
        assert "AKIAIOSFODNN7EXAMPLE" not in captured[0]["text"]

    @pytest.mark.asyncio
    async def test_send_audio_failure_still_cleans_up(self, monkeypatch):
        """If send_audio_event raises mid-stream, end_stream still runs."""
        _, input_stream = self._install_stubs(monkeypatch)
        input_stream.send_audio_event = AsyncMock(side_effect=RuntimeError("throttled"))
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_bytes(b"\x00\x01" * 16)
            await ws.close()
        input_stream.end_stream.assert_awaited()

    @pytest.mark.asyncio
    async def test_abrupt_close_without_stop_message(self, monkeypatch):
        """Client closes WS without sending {"type":"stop"} — cleanup must run."""
        _, input_stream = self._install_stubs(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_bytes(b"\x00\x01" * 16)
            await ws.close()  # no stop message
        input_stream.end_stream.assert_awaited()

    @pytest.mark.asyncio
    async def test_handler_task_exception_does_not_crash(self, monkeypatch):
        """handle_events() raising must be logged, not propagated."""
        from amazon_transcribe.handlers import TranscriptResultStreamHandler

        _, input_stream = self._install_stubs(monkeypatch)
        monkeypatch.setattr(
            TranscriptResultStreamHandler,
            "handle_events",
            AsyncMock(side_effect=RuntimeError("connection lost")),
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_str('{"type":"stop"}')
            await ws.close()
        input_stream.end_stream.assert_awaited()

    @pytest.mark.asyncio
    async def test_max_duration_timeout_closes_stream(self, monkeypatch):
        """Session exceeding _MAX_STREAM_DURATION_SECS emits error and cleans up.

        Regression: the deadline must fire on a dedicated task, not on
        per-message checks. An idle-but-alive client (heartbeat pings
        only) must still be torn down after the cap.
        """
        _, input_stream = self._install_stubs(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 0.05
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            # Do NOT send any audio — rely purely on the deadline task.
            msg = await ws.receive_json()
            assert msg == {"type": "error", "message": "max stream duration exceeded"}
            await ws.close()
        input_stream.end_stream.assert_awaited()

    @pytest.mark.asyncio
    async def test_oversized_text_frame_closes_connection(self, monkeypatch):
        """Text frame larger than _MAX_TEXT_FRAME_BYTES terminates the stream."""
        _, input_stream = self._install_stubs(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_str("x" * 300)  # >_MAX_TEXT_FRAME_BYTES (256)
            await ws.close()
        input_stream.end_stream.assert_awaited()


class TestConfig:
    def test_streaming_defaults_false(self):
        cfg = SttConfig()
        assert cfg.streaming is False

    def test_streaming_respects_value(self):
        cfg = SttConfig(streaming=True)
        assert cfg.streaming is True


class TestConfigPutRoundTrip:
    """Verify the STT config PUT handler persists the streaming field."""

    @pytest.mark.asyncio
    async def test_put_persists_streaming(self, tmp_path, monkeypatch):
        # KIROCREW_HOME redirects both config_dir() and config_path() in a
        # way that survives the `from ... import config_path` idiom used by
        # the handler, unlike monkeypatching a module-level name.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.dashboard import handlers

        app = web.Application()
        app.router.add_get("/api/config/stt", handlers.api_stt_config)
        app.router.add_put("/api/config/stt", handlers.api_stt_config)

        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/config/stt", json={"streaming": True, "provider": "transcribe"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["streaming"] is True
            assert data["provider"] == "transcribe"
            # Assert it persisted to disk (survives a fresh load).
            cfg_file = tmp_path / "config.json"
            assert cfg_file.exists()
            import json as _json
            on_disk = _json.loads(cfg_file.read_text(encoding="utf-8"))
            assert on_disk["stt"]["streaming"] is True
            # Assert KiroCrewConfig.load() correctly deserializes — guards
            # against field-name mismatches that would silently break at runtime.
            reloaded = KiroCrewConfig.load()
            assert reloaded.stt.streaming is True

    @pytest.mark.asyncio
    async def test_put_rejects_non_boolean_streaming(self, tmp_path, monkeypatch):
        """Non-boolean ``streaming`` values must be ignored, not coerced.

        Regression: ``bool("false") is True`` silently enabled streaming
        when the client sent a string. The handler now checks
        ``isinstance(body["streaming"], bool)`` and drops anything else.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.dashboard import handlers

        app = web.Application()
        app.router.add_put("/api/config/stt", handlers.api_stt_config)

        async with TestClient(TestServer(app)) as client:
            # "false" string: old bug would coerce to True. Must stay False.
            resp = await client.put(
                "/api/config/stt",
                json={"streaming": "false", "provider": "transcribe"},
            )
            assert resp.status == 200
            assert (await resp.json())["streaming"] is False

            # "true" string: the test that would have caught the bug if
            # default had been True. Must also be ignored (non-bool type).
            resp = await client.put(
                "/api/config/stt",
                json={"streaming": "true", "provider": "transcribe"},
            )
            assert resp.status == 200
            assert (await resp.json())["streaming"] is False

            # Int 1 (truthy): same rule — reject, don't coerce.
            resp = await client.put(
                "/api/config/stt",
                json={"streaming": 1, "provider": "transcribe"},
            )
            assert resp.status == 200
            assert (await resp.json())["streaming"] is False

    @pytest.mark.asyncio
    async def test_get_exposes_transcribe_fields_for_ui(self, tmp_path, monkeypatch):
        """GET response must include transcribe_region, transcribe_profile,
        language_code, and language_codes so the Chat Settings STT section
        can render the current values and a language dropdown.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.dashboard import handlers

        app = web.Application()
        app.router.add_get("/api/config/stt", handlers.api_stt_config)
        app.router.add_put("/api/config/stt", handlers.api_stt_config)

        async with TestClient(TestServer(app)) as client:
            # PUT all three transcribe-specific fields and the provider.
            resp = await client.put(
                "/api/config/stt",
                json={
                    "provider": "transcribe",
                    "transcribe_region": "eu-west-1",
                    "transcribe_profile": "dev-account",
                    "language_code": "fr-FR",
                },
            )
            assert resp.status == 200

            # GET must reflect the persisted values and expose the
            # language-code list the UI picker uses.
            resp = await client.get("/api/config/stt")
            assert resp.status == 200
            data = await resp.json()
            assert data["provider"] == "transcribe"
            assert data["transcribe_region"] == "eu-west-1"
            assert data["transcribe_profile"] == "dev-account"
            assert data["language_code"] == "fr-FR"
            assert isinstance(data["language_codes"], list)
            assert "en-US" in data["language_codes"]
            assert "fr-FR" in data["language_codes"]


class TestDefensiveGuards:
    """Regression tests for review-bot rev 2 findings (posts #9, #10)."""

    @pytest.mark.asyncio
    async def test_guard_audit_sel_failure_preserves_status_code(self, monkeypatch):
        """If sel() raises on a guard rejection, client must still get 403/503, not 500.

        Regression for review-bot post #9: unwrapped sel().log_api_access() on
        the origin/availability/concurrency guards would propagate and mask
        the intended HTTPForbidden/HTTPServiceUnavailable.
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

        Regression for review-bot post #10: unwrapped resolver/client construction
        would leak a prepare()d WebSocket and leave an unmatched
        stt_stream_start in the audit trail.
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
            assert msg == {"type": "error", "message": "failed to create transcription client"}
            await ws.close()
        # Wait for the end audit instead of assuming the handler already ran:
        # the error frame / client close is not a barrier for it.
        await _wait_for_operation(calls, "stt_stream_end")
        ops = [c["operation"] for c in calls]
        assert "stt_stream_start" in ops and "stt_stream_end" in ops, (
            f"both start and end audit events required; got {ops}"
        )
        end = next(c for c in calls if c["operation"] == "stt_stream_end")
        assert end["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_start_audit_sel_failure_closes_ws_and_emits_end_audit(self, monkeypatch):
        """If the stt_stream_start sel call raises, WS must close and end audit emit.

        Regression for review-bot post #13: unwrapped sel().log_api_access() for
        stt_stream_start would propagate after ws.prepare() sent the 101
        upgrade, leaking the WebSocket and leaving an unmatched start event.
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
            assert msg == {"type": "error", "message": "audit subsystem unavailable"}
            await ws.close()
        # Wait for the end audit instead of assuming the handler already ran:
        # the error frame / client close is not a barrier for it.
        await _wait_for_operation(calls, "stt_stream_end")
        ops = [c["operation"] for c in calls]
        assert "stt_stream_start" in ops and "stt_stream_end" in ops, (
            f"both start and end audit events required; got {ops}"
        )
        end = next(c for c in calls if c["operation"] == "stt_stream_end")
        assert end["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_cleanup_ws_close_failure_still_emits_end_audit(self, monkeypatch):
        """If the cleanup ws.close() raises on broken transport, end audit still fires.

        Regression for review-bot post #18: unwrapped await ws.close() on the
        normal cleanup path would skip _emit_end_audit when the transport
        is broken, leaving an unmatched stt_stream_start in the audit trail.
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
        input_stream.end_stream = AsyncMock()
        stream = MagicMock(input_stream=input_stream, output_stream=MagicMock())
        client = MagicMock()
        client.start_stream_transcription = AsyncMock(return_value=stream)
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.TranscribeStreamingClient",
            lambda **kw: client,
        )
        monkeypatch.setattr(
            TranscriptResultStreamHandler,
            "handle_events",
            AsyncMock(return_value=None),
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
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_str('{"type":"stop"}')
            await ws.close()
        # Wait for the end audit instead of assuming the handler already ran:
        # the error frame / client close is not a barrier for it.
        await _wait_for_operation(calls, "stt_stream_end")
        ops = [c["operation"] for c in calls]
        assert "stt_stream_start" in ops and "stt_stream_end" in ops, (
            f"both start and end audit events required; got {ops}"
        )


class TestSttProviderGating:
    """`mlx` is only offered on Apple Silicon, and the check must see through
    Rosetta 2 (KiroCrew's bundled Python reports ``x86_64`` even on arm64)."""

    def test_is_apple_silicon_false_off_darwin(self, monkeypatch):
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        assert core._is_apple_silicon() is False

    def test_is_apple_silicon_true_native_arm64(self, monkeypatch):
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        assert core._is_apple_silicon() is True

    def test_is_apple_silicon_true_under_rosetta(self, monkeypatch):
        """Darwin + ``x86_64`` interpreter, but ``hw.optional.arm64`` == 1."""
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")

        def fake_run(*_a, **_kw):
            return SimpleNamespace(stdout="1\n")

        monkeypatch.setattr("subprocess.run", fake_run)
        assert core._is_apple_silicon() is True

    def test_is_apple_silicon_false_on_intel_mac(self, monkeypatch):
        """Darwin + ``x86_64``; sysctl key absent/0 on a true Intel Mac."""
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")

        def fake_run(*_a, **_kw):
            return SimpleNamespace(stdout="")

        monkeypatch.setattr("subprocess.run", fake_run)
        assert core._is_apple_silicon() is False

    def test_providers_include_mlx_on_apple_silicon(self, monkeypatch):
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_is_apple_silicon", lambda: True)
        assert core._stt_providers() == ["whisper", "mlx", "transcribe"]

    def test_providers_exclude_mlx_off_apple_silicon(self, monkeypatch):
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_is_apple_silicon", lambda: False)
        providers = core._stt_providers()
        assert "mlx" not in providers
        assert providers == ["whisper", "transcribe"]

    def test_mlx_prereqs_empty_when_brew_present(self, monkeypatch):
        """The Install button installs ffmpeg/pipx/mlx-whisper, so when brew is
        present there are no manual prereqs to surface (no duplication)."""
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_is_apple_silicon", lambda: True)
        monkeypatch.setattr(core, "ensure_ffmpeg_in_path", lambda: None)
        monkeypatch.setattr("shutil.which", lambda _name: "/opt/homebrew/bin/brew")
        assert core._stt_prereq_commands("mlx") == []

    def test_mlx_prereqs_only_homebrew_when_brew_absent(self, monkeypatch):
        """Homebrew is the one thing the Install button can't bootstrap."""
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_is_apple_silicon", lambda: True)
        monkeypatch.setattr(core, "ensure_ffmpeg_in_path", lambda: None)
        monkeypatch.setattr("shutil.which", lambda _name: None)
        cmds = core._stt_prereq_commands("mlx")
        assert len(cmds) == 1
        assert "brew" in cmds[0] and "install.sh" in cmds[0]
        # Must NOT duplicate what the Install button already does.
        assert not any("pipx install mlx-whisper" in c for c in cmds)

    def test_mlx_prereqs_empty_off_apple_silicon(self, monkeypatch):
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_is_apple_silicon", lambda: False)
        monkeypatch.setattr(core, "ensure_ffmpeg_in_path", lambda: None)
        assert core._stt_prereq_commands("mlx") == []

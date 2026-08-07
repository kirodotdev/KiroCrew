"""Tests for streaming STT WebSocket endpoint (dashboard/stt_stream.py)."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import platform_compat as pc
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


class TestAppleStreamingSession:
    """The `apple` provider's own WebSocket path (`_run_apple_session`).

    It reuses the endpoint, the event shapes and the endpointer, but has its own
    lifecycle code — so the invariants the AWS path already guards need their own
    coverage here rather than being assumed shared.
    """

    def _install(self, monkeypatch, *, session=None, start_error="", feed_ok=True):
        """Point the endpoint at the apple provider with a stubbed helper session."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg(provider="apple")),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)

        events: asyncio.Queue = asyncio.Queue()
        fed: list[bytes] = []

        class FakeSession:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def start(self):
                return start_error

            async def feed(self, pcm):
                fed.append(pcm)
                return feed_ok

            async def events(self):
                while True:
                    ev = await events.get()
                    if ev is None:
                        return
                    yield ev

            async def finish(self, **kwargs):
                await events.put(None)
                return ""

            async def close(self):
                pass

        fake_module = SimpleNamespace(
            StreamingSession=session or FakeSession,
            STREAM_SAMPLE_RATE_HZ=16000,
        )
        # BOTH, deliberately. `_run_apple_session` does `from kiro_crew import
        # apple_speech`, which resolves the ATTRIBUTE on the already-imported
        # `kiro_crew` package rather than consulting sys.modules — so patching
        # sys.modules alone works when this file runs alone and is silently
        # bypassed once any other test module has imported the real one.
        monkeypatch.setitem(sys.modules, "kiro_crew.apple_speech", fake_module)
        monkeypatch.setattr("kiro_crew.apple_speech", fake_module, raising=False)
        return events, fed

    @pytest.mark.asyncio
    async def test_duration_cap_fires_for_a_client_that_sends_nothing(self, monkeypatch):
        """Regression: the cap MUST run on a dedicated task, not per-message.

        `async for msg in ws` only yields on client data and aiohttp answers
        heartbeat ping/pong internally, so a message-driven deadline never
        evaluates for an idle-but-alive client — leaking the helper process, an OS
        speech session, and one of `_MAX_CONCURRENT_SESSIONS` slots indefinitely.
        """
        self._install(monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 0.05)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            # Deliberately send NO audio — only the deadline task can end this.
            msg = await ws.receive_json()
            assert msg == {"type": "error", "message": "max stream duration exceeded"}
            await ws.close()

    @pytest.mark.asyncio
    async def test_cap_teardown_is_audited_as_a_timeout(self, monkeypatch):
        """A cap-driven teardown must be distinguishable from a clean stop.

        Otherwise `stt_stream_end` reads identically for both, operators cannot
        see cap-driven teardowns, and the audit trail diverges from the AWS path
        for the same event.
        """
        self._install(monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 0.05)
        outcomes: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream._emit_end_audit",
            lambda caller, *, outcome: outcomes.append(outcome),
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.receive_json()  # the cap's error frame
            await ws.close()
        for _ in range(int(_AUDIT_WAIT_TIMEOUT_SECS / 0.02)):
            if outcomes:
                break
            await asyncio.sleep(0.02)
        assert outcomes == ["timeout"], outcomes

    @pytest.mark.asyncio
    async def test_clean_stop_is_not_audited_as_a_timeout(self, monkeypatch):
        """The mirror of the above: `{"type":"stop"}` must not read as a timeout."""
        self._install(monkeypatch)
        outcomes: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream._emit_end_audit",
            lambda caller, *, outcome: outcomes.append(outcome),
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_str('{"type":"stop"}')
            await ws.close()
        for _ in range(int(_AUDIT_WAIT_TIMEOUT_SECS / 0.02)):
            if outcomes:
                break
            await asyncio.sleep(0.02)
        assert outcomes == ["ok"], outcomes

    @pytest.mark.asyncio
    async def test_partials_and_finals_are_relayed_redacted(self, monkeypatch):
        """A partial reaches the browser DOM, so it is an external surface even
        though the next partial replaces it and nothing is persisted."""
        events, _ = self._install(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await events.put({"type": "partial", "text": "  my key is AKIAIOSFODNN7EXAMPLE  "})
            got = await ws.receive_json()
            assert got["type"] == "partial"
            assert "AKIAIOSFODNN7EXAMPLE" not in got["text"]
            # Edge whitespace is stripped: the frontend re-joins finals with a
            # space of its own, so a leading space would double it.
            assert got["text"] == got["text"].strip()
            await ws.send_str('{"type":"stop"}')
            await ws.close()

    @pytest.mark.asyncio
    async def test_a_fatal_helper_error_reaches_the_client(self, monkeypatch):
        """A mid-session helper failure must surface, not go quiet.

        The helper stops producing results after emitting `error`, so dropping the
        event leaves the client on a live socket that will never transcribe again —
        indistinguishable from a silent microphone.
        """
        events, _ = self._install(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await events.put({"type": "error", "message": "result stream failed: boom"})
            msg = await ws.receive_json()
            assert msg["type"] == "error"
            assert "result stream failed" in msg["message"]
            await ws.close()

    @pytest.mark.asyncio
    async def test_helper_death_on_the_write_side_surfaces(self, monkeypatch):
        """A helper that stops ACCEPTING audio must not look like a clean stop.

        Breaking out of the read loop alone audits the session as `ok` and leaves the
        client believing it is still recording, with everything said from then on
        silently dropped — the same failure as swallowing an `error` event, reached
        through the write side instead of the read side.
        """
        self._install(monkeypatch, feed_ok=False)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_bytes(b"\x00\x01" * 32)
            msg = await ws.receive_json()
            assert msg["type"] == "error"
            assert "stopped" in msg["message"]

    @pytest.mark.asyncio
    async def test_helper_start_failure_surfaces_and_closes(self, monkeypatch):
        self._install(monkeypatch, start_error="the Xcode Command Line Tools are required")
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg["type"] == "error"
            assert "Command Line Tools" in msg["message"]
            await ws.close()


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
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.TranscribeStreamingClient", None)
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
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 0.05)
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
        assert (
            "stt_stream_start" in ops and "stt_stream_end" in ops
        ), f"both start and end audit events required; got {ops}"
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
        assert (
            "stt_stream_start" in ops and "stt_stream_end" in ops
        ), f"both start and end audit events required; got {ops}"
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
        assert (
            "stt_stream_start" in ops and "stt_stream_end" in ops
        ), f"both start and end audit events required; got {ops}"


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
        from kiro_crew import apple_speech
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_is_apple_silicon", lambda: True)
        # `apple` has its own gate (macOS 26 + Swift toolchain); pin it off here so
        # this test measures only the Apple-Silicon gate.
        monkeypatch.setattr(
            apple_speech, "availability", lambda: apple_speech.Availability(False, "pinned off")
        )
        assert core._stt_providers() == ["whisper", "mlx", "transcribe"]

    def test_providers_exclude_mlx_off_apple_silicon(self, monkeypatch):
        from kiro_crew import apple_speech
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_is_apple_silicon", lambda: False)
        monkeypatch.setattr(
            apple_speech, "availability", lambda: apple_speech.Availability(False, "pinned off")
        )
        providers = core._stt_providers()
        assert "mlx" not in providers
        assert providers == ["whisper", "transcribe"]

    def test_providers_include_apple_when_supported(self, monkeypatch):
        """`apple` is advertised only where SpeechAnalyzer can actually run."""
        from kiro_crew import apple_speech
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_is_apple_silicon", lambda: True)
        monkeypatch.setattr(apple_speech, "availability", lambda: apple_speech.Availability(True))
        assert core._stt_providers() == ["whisper", "mlx", "apple", "transcribe"]

    def test_providers_exclude_apple_when_toolchain_missing(self, monkeypatch):
        """A host that could run the framework but has no Swift toolchain must not be
        offered the option — picking it would fail at transcription time."""
        from kiro_crew import apple_speech
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_is_apple_silicon", lambda: True)
        monkeypatch.setattr(
            apple_speech,
            "availability",
            lambda: apple_speech.Availability(False, "no toolchain", needs_toolchain=True),
        )
        assert "apple" not in core._stt_providers()

    def test_mlx_prereqs_empty_when_brew_present(self, monkeypatch):
        """The Install button installs ffmpeg/pipx/mlx-whisper, so when brew is
        present there are no manual prereqs to surface (no duplication)."""
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_is_apple_silicon", lambda: True)
        monkeypatch.setattr(core, "ensure_ffmpeg_in_path", lambda: None)
        monkeypatch.setattr(core, "find_brew", lambda: "/opt/homebrew/bin/brew")
        assert core._stt_prereq_commands("mlx") == []

    def test_mlx_prereqs_empty_when_brew_off_path(self, monkeypatch):
        """Homebrew installed but NOT on PATH must not be reported missing.

        A GUI-launched gateway (desktop app / launchd) inherits
        ``/usr/bin:/bin:/usr/sbin:/sbin``, so ``shutil.which("brew")`` returns
        None on a machine that has Homebrew. Resolution goes through
        ``find_brew``, which probes the install prefixes directly — otherwise the
        UI tells a Homebrew user to install Homebrew.
        """
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_is_apple_silicon", lambda: True)
        monkeypatch.setattr(core, "ensure_ffmpeg_in_path", lambda: None)
        monkeypatch.setattr("shutil.which", lambda _name, **_kw: None)
        monkeypatch.setattr("os.path.isfile", lambda p: p == "/opt/homebrew/bin/brew")
        monkeypatch.setattr("os.access", lambda p, _mode: p == "/opt/homebrew/bin/brew")
        assert core._stt_prereq_commands("mlx") == []

    def test_mlx_prereqs_only_homebrew_when_brew_absent(self, monkeypatch):
        """Homebrew is the one thing the Install button can't bootstrap."""
        from kiro_crew.dashboard.handlers import core

        monkeypatch.setattr(core, "_is_apple_silicon", lambda: True)
        monkeypatch.setattr(core, "ensure_ffmpeg_in_path", lambda: None)
        monkeypatch.setattr(core, "find_brew", lambda: None)
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


class TestSttInstallScriptPath:
    """The install script must find Homebrew from a GUI-launched gateway.

    ``bash -c`` is neither a login nor an interactive shell, so the user's
    ``brew shellenv`` line never runs and the script only gets the inherited
    PATH — which for a desktop-app gateway is ``/usr/bin:/bin:/usr/sbin:/sbin``.
    Without a PATH prelude the first ``command -v brew`` check fails and the
    whole install aborts with "ERROR: Homebrew required" on a machine that has it.

    The two tests below that RUN a shell are POSIX-only. On Windows ``bash``
    resolves to ``C:\\Windows\\System32\\bash.exe`` — the WSL launcher stub, which
    exits 1 with "Windows Subsystem for Linux has no installed distributions"
    rather than running the script. That is the same alias-stub hazard
    ``platform_compat.find_python_interpreter`` guards against for Python, and it
    makes the assertion measure the runner's WSL state instead of the prelude.
    Only the shell-executing tests are skipped; the string assertions below run
    everywhere, and Homebrew does not exist on Windows so the prelude's dirs are
    inert there anyway.
    """

    @pytest.mark.parametrize("provider", ["mlx", "whisper"])
    def test_script_prepends_brew_prefixes(self, provider):
        from kiro_crew.dashboard.handlers import core

        script = core._build_stt_install_script(provider)
        assert "/opt/homebrew/bin" in script  # Apple Silicon prefix
        assert "brew shellenv" in script
        # The prelude must run BEFORE the brew probe that gates the install.
        assert script.index("/opt/homebrew/bin") < script.index("command -v brew")

    @pytest.mark.skipif(pc.IS_WINDOWS, reason="bash resolves to the WSL launcher stub")
    @pytest.mark.parametrize("provider", ["mlx", "whisper"])
    def test_script_is_valid_shell(self, provider, tmp_path):
        """Guard the f-string-composed prelude against a syntax regression."""
        import subprocess

        from kiro_crew.dashboard.handlers import core

        p = tmp_path / "install.sh"
        p.write_text(core._build_stt_install_script(provider), encoding="utf-8")
        assert subprocess.run(["bash", "-n", str(p)]).returncode == 0

    @pytest.mark.skipif(pc.IS_WINDOWS, reason="bash resolves to the WSL launcher stub")
    def test_prelude_finds_brew_under_launchd_path(self, tmp_path):
        """End-to-end: the prelude alone recovers brew from a stripped PATH."""
        import subprocess

        from kiro_crew.dashboard.handlers import core

        fake_prefix = tmp_path / "opt" / "homebrew" / "bin"
        fake_prefix.mkdir(parents=True)
        brew = fake_prefix / "brew"
        brew.write_text("#!/bin/sh\necho 'export PATH=\"$PATH\"'\n", encoding="utf-8")
        brew.chmod(0o755)

        prelude = core._stt_install_path_prelude().replace("/opt/homebrew/bin", str(fake_prefix))
        script = prelude + "\ncommand -v brew >/dev/null && echo FOUND || echo MISSING\n"
        out = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path)},
        )
        assert "FOUND" in out.stdout


class TestSttInstallScriptWheels:
    """The pip fallback must never drop into a source build.

    openai-whisper is a pure-Python sdist, but numpy / numba / llvmlite / torch /
    triton / tiktoken ship compiled wheels. On a host whose glibc is older than the
    wheel's tag (Amazon Linux 2 = glibc 2.26, so manylinux_2_17 is the ceiling while
    current numpy publishes manylinux_2_28) pip falls back to the source tarball and
    the failure surfaces as a compiler error naming numpy — "GCC >= 9.3",
    "metadata-generation-failed" — which reads as a numpy bug rather than the
    wheel-compatibility problem it is.
    """

    def _pip_section(self):
        """Return (full script, the pip-fallback section only).

        Scoped deliberately: the brew branch above also mentions
        ``openai-whisper``, so a whole-script index() would measure the wrong
        occurrence and the ordering assertions would silently pass.
        """
        from kiro_crew.dashboard.handlers import core

        script = core._build_stt_install_script("whisper")
        marker = "# Fallback: pip install"
        assert marker in script, "pip fallback section not found"
        return script, script[script.index(marker) :]

    def _pip_commands(self, section):
        """Real pip invocations: continuations joined, comments dropped.

        Each command is truncated at its ``||`` recovery clause — the CPU-torch
        step's fallback ``echo`` mentions openai-whisper, and counting that as an
        install target would inflate the command list.
        """
        joined = section.replace("\\\n", " ")
        cmds = []
        for ln in joined.splitlines():
            stripped = ln.strip()
            if stripped.startswith("#") or "pip install" not in stripped:
                continue
            cmds.append(" ".join(stripped.split()).split("||")[0].strip())
        return cmds

    def test_compiled_deps_are_wheel_only(self):
        """Every pip install of the whisper stack constrains source builds."""
        script, section = self._pip_section()
        cmds = self._pip_commands(section)
        assert cmds, "expected at least one pip install command"
        for cmd in cmds:
            assert "--only-binary" in cmd, f"unconstrained pip install: {cmd}"
        # The named set must cover the deps that actually compile.
        for pkg in ("numpy", "numba", "llvmlite", "torch", "triton", "tiktoken", "regex"):
            assert pkg in section, f"{pkg} missing from the wheel-only set"

    def test_no_hardcoded_version_ceiling(self):
        """Wheel-only is the mechanism; a pinned cap would rot.

        ``--only-binary`` drops sdists from the candidate set, so pip backtracks
        to the newest release that HAS a compatible wheel on its own (glibc 2.26:
        numpy 2.5.1 -> 2.2.6 manylinux_2_17). Pinning a ceiling instead would go
        stale as hosts and wheel tags move, and would hold back a modern host.
        """
        _, section = self._pip_section()
        whisper = [c for c in self._pip_commands(section) if "openai-whisper" in c]
        assert len(whisper) == 1, f"expected one whisper install, got {len(whisper)}"
        assert "numpy<" not in section, "no hardcoded numpy ceiling"
        assert "numpy==" not in section, "no hardcoded numpy pin"

    def test_cpu_torch_installed_first_when_no_gpu(self):
        """A GPU-less Linux host must not pull ~2.5 GB of CUDA wheels."""
        _, section = self._pip_section()
        assert "nvidia-smi" in section, "GPU probe missing"
        assert "download.pytorch.org/whl/cpu" in section
        # --extra-index-url only ADDS a source; pip would still prefer the
        # higher-versioned CUDA build, so the CPU index must be the ONLY index.
        assert "--extra-index-url https://download.pytorch.org/whl/cpu" not in section
        # torch has to land before the whisper resolve, or it is already satisfied
        # by the CUDA build and the CPU step is a no-op.
        assert section.index("download.pytorch.org/whl/cpu") < section.index(
            "Installing openai-whisper"
        )

    def test_cpu_torch_failure_is_not_fatal(self):
        """An unreachable CPU index must not fail an otherwise-fine install."""
        _, section = self._pip_section()
        tail = section[
            section.index("download.pytorch.org/whl/cpu") : section.index(
                "Installing openai-whisper"
            )
        ]
        # The CPU-torch step recovers with `|| echo`, never `exit 1`.
        assert "|| echo" in tail
        assert "exit 1" not in tail

    def test_pip_path_still_targets_system_python(self):
        """Regression guard on a deliberate design choice, not an accident.

        ``--user`` lands in ``~/.local/bin``, which ``transcribe._find_whisper``
        probes explicitly via ``_WHISPER_SEARCH_PATHS`` (``_python3_bin_dir``
        returns the interpreter's OWN prefix bin, not the --user target, so it is
        not what covers this). Redirecting the install into the gateway's venv
        would make the binary undiscoverable at runtime, and ``--user`` is
        rejected outright inside a virtualenv.
        """
        script, section = self._pip_section()
        for cmd in self._pip_commands(section):
            assert "--user" in cmd, f"pip install must stay a --user install: {cmd}"
        assert "sys.executable" not in script

"""Tests for streaming STT WebSocket endpoint (dashboard/stt_stream.py)."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import stt
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
    async def test_rejects_a_provider_that_cannot_stream(self, monkeypatch):
        """A provider outside ``_STREAMING_PROVIDERS`` is refused, not half-served.

        The handler's provider checks are a chain ending in the AWS branch, so a
        value with no streaming implementation of its own does not fail: it falls
        through to Transcribe, which is the one provider that bills. The guard is
        what makes that unreachable. ``whisper`` stands in for such a value because
        it is a retired name a hand-edited or legacy ``config.json`` can still hold;
        ``KiroCrewConfig.load`` degrades it to ``local``, which is exactly why the
        socket needs its own gate rather than trusting the loaded value.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg(provider="whisper")),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/ws/stt")
            assert resp.status == 503

    def test_the_default_provider_can_stream(self):
        """Whatever ships as the default must be admitted by this endpoint.

        Streaming is also on by default, so a default provider missing from
        ``_STREAMING_PROVIDERS`` would 503 every microphone press on a fresh
        install, with the settings panel showing voice input as ready.
        """
        from kiro_crew.dashboard import stt_stream

        assert SttConfig().provider in stt_stream._STREAMING_PROVIDERS
        assert SttConfig().streaming is True

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

        The cap covers all three providers, for two different reasons: on
        ``transcribe`` a single user opening many tabs (or an attacker past origin)
        multiplies cost and can exhaust the account's concurrent-stream quota, and
        on the free on-device providers each session still buffers its whole
        utterance and serialises its decodes onto one resident model.
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

    def _install(self, monkeypatch, *, session=None, start_error="", feed_ok=True, avail=None):
        """Point the endpoint at the apple provider with a stubbed helper session.

        *avail* is what the double reports from ``availability()``, which the error
        path consults to name a start failure. It defaults to a capable host so the
        cases that never fail to start do not depend on the runner's OS.
        """
        from kiro_crew import apple_speech as real_apple_speech

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
            Availability=real_apple_speech.Availability,
            availability=lambda: avail or real_apple_speech.Availability(True),
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

        The frame is pinned whole, `code` included: the dashboard renders `message`
        verbatim into a 12-language UI, so the code is the part a localised string
        can key off and an uncoded frame can only ever be shown in English.
        """
        self._install(monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 0.05)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            # Deliberately send NO audio — only the deadline task can end this.
            msg = await ws.receive_json()
            assert msg == {
                "type": "error",
                "message": "max stream duration exceeded",
                "code": "stt_max_duration_exceeded",
            }
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

        The helper's own prose is forwarded for an operator reading the log, and the
        code is what the localised UI renders: a mid-session helper death is a
        session failure the user can only retry, not an availability problem.
        """
        events, _ = self._install(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await events.put({"type": "error", "message": "result stream failed: boom"})
            msg = await ws.receive_json()
            assert msg["type"] == "error"
            assert "result stream failed" in msg["message"]
            assert msg["code"] == "stt_session_failed"
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
            assert msg["code"] == "stt_session_failed"

    @pytest.mark.asyncio
    async def test_helper_start_failure_surfaces_and_closes(self, monkeypatch):
        """A session that never starts reports WHY in a form the UI can localise.

        `start()` answers in prose, and "install the Xcode Command Line Tools" is
        both the most likely cause and the only one with a user-actionable fix, so
        collapsing it into a generic failure code would throw away the one thing
        worth telling this user. The code is the availability probe's own, which is
        what the settings panel already renders for the same condition.
        """
        from kiro_crew import apple_speech

        self._install(
            monkeypatch,
            start_error="the Xcode Command Line Tools are required",
            avail=apple_speech.Availability(False, "no toolchain", needs_toolchain=True),
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg["type"] == "error"
            assert "Command Line Tools" in msg["message"]
            assert msg["code"] == "stt_apple_needs_toolchain"
            await ws.close()

    @pytest.mark.asyncio
    async def test_start_failure_on_a_capable_host_is_a_session_failure(self, monkeypatch):
        """The mirror: a host that CAN run the framework but still failed to start.

        A helper that would not build, a sandbox that is unavailable or a readiness
        timeout are none of them availability problems, so reporting one of the
        availability codes would send the user to install something they already
        have.
        """
        self._install(monkeypatch, start_error="streaming helper did not become ready")
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg["type"] == "error"
            assert msg["code"] == "stt_session_failed"
            await ws.close()

    @pytest.mark.asyncio
    async def test_cancelled_start_still_closes_the_session(self, monkeypatch):
        """`await session.start()` runs BEFORE the teardown `finally` exists.

        A cancellation landing there (client gone, gateway shutdown) therefore
        has no caller-side owner: without the call-site guard, the helper
        session — and the sandbox launcher it may hold — is never closed, and
        the `stt_stream_start` already emitted gets no matching end audit. The
        guard must close the session, balance the trail, and re-raise.
        """
        started = asyncio.Event()
        closed: list[bool] = []
        outcomes: list[str] = []

        class HangingSession:
            def __init__(self, **kwargs):
                pass

            async def start(self):
                started.set()
                await asyncio.sleep(60)
                return ""

            async def close(self):
                closed.append(True)

        self._install(monkeypatch, session=HangingSession)
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream._emit_end_audit",
            lambda caller, *, outcome: outcomes.append(outcome),
        )
        from kiro_crew.dashboard import stt_stream

        task = asyncio.create_task(
            stt_stream._run_apple_session(
                MagicMock(), _cfg(provider="apple"), MagicMock(), "test-caller"
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]
        assert outcomes == ["error"]


@pytest.fixture()
def transcribe_consented(tmp_path_factory, monkeypatch):
    """Record operator consent for Transcribe in a throwaway data home.

    Streaming Transcribe is a paid service, so ``api_ws_stt`` refuses without a
    grant for the configured profile+region. Every class that drives that handler
    needs this; it is module-level rather than copied per class so the two cannot
    drift. Cases that assert the REFUSAL live in ``test_aws_consent.py``.

    Also stubs the live-account check, which would otherwise spawn the AWS CLI --
    these cases are about the stream, not the identity probe.
    """
    home = tmp_path_factory.mktemp("stt-consent-home")
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    from kiro_crew import aws_consent
    from kiro_crew.config.loader import config_dir

    config_dir().mkdir(parents=True, exist_ok=True)
    cfg = _cfg()
    aws_consent.record_grant(
        aws_consent.SERVICE_TRANSCRIBE,
        profile=cfg.stt.transcribe_profile,
        region=cfg.stt.transcribe_region,
        account="111122223333",
        arn="arn:aws:iam::111122223333:user/test",
        granted_at="2026-08-21T00:00:00+00:00",
    )

    async def _probe(_profile, _region, *, use_cache=True):
        return aws_consent.Identity(ok=True, account="111122223333")

    monkeypatch.setattr(aws_consent, "probe_identity", _probe)


class TestStreamLifecycle:
    """Mock TranscribeStreamingClient to verify lifecycle + redaction."""

    @pytest.fixture(autouse=True)
    def _consented(self, transcribe_consented):
        """Every case here drives ``api_ws_stt``, which is consent-gated."""

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

        The frame carries the availability probe's own missing-extra code rather
        than a socket-specific one, so the socket and the settings panel name the
        same condition and one localised string serves both.
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
            assert msg == {
                "type": "error",
                "message": "amazon-transcribe not installed",
                "code": stt.CODE_EXTRA_MISSING,
            }
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

        The cap's frame carries the same code as the other two providers': one
        condition, one localised string, whichever provider the user is on.
        """
        _, input_stream = self._install_stubs(monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 0.05)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            # Do NOT send any audio — rely purely on the deadline task.
            msg = await ws.receive_json()
            assert msg == {
                "type": "error",
                "message": "max stream duration exceeded",
                "code": "stt_max_duration_exceeded",
            }
            await ws.close()
        input_stream.end_stream.assert_awaited()

    @pytest.mark.asyncio
    async def test_consent_refusal_reaches_the_client_and_is_audited(self, monkeypatch):
        """No recorded grant means no socket, reported over the same error channel.

        Streaming Transcribe bills per audio-second, so the refusal happens before
        the client is constructed and before any audio is read. It carries its own
        code because the fix is an operator action in Settings, not the retry a
        generic session failure invites, and the audit records ``refused`` rather
        than ``error`` so an operator can tell a denied request from a broken one.
        """
        self._install_stubs(monkeypatch)

        async def _refuse(service, *, profile, region):
            return False, "AWS Transcribe needs your consent for profile default"

        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.aws_consent.authorize", _refuse, raising=True
        )
        calls: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: calls.append(kw)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg["type"] == "error"
            assert "consent" in msg["message"]
            assert msg["code"] == "stt_consent_required"
            await ws.close()
        await _wait_for_operation(calls, "stt_stream_end")
        end = next(c for c in calls if c["operation"] == "stt_stream_end")
        assert end["outcome"] == "refused"

    @pytest.mark.asyncio
    async def test_cap_teardown_is_audited_as_a_timeout(self, monkeypatch):
        """A cap-driven teardown must be distinguishable from a clean stop.

        This is the branch where it matters most: a session held open to the cap on
        the metered provider is billed audio, so an operator reading the trail needs
        to see which sessions ended that way. Inferring it from the deadline task's
        own state cannot answer it, because the cap's ``ws.close()`` is what ends the
        read loop, so the cleanup runs while that task is still awaiting the peer's
        acknowledgement and reads as not-yet-done.
        """
        _, input_stream = self._install_stubs(monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 0.05)
        outcomes: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream._emit_end_audit",
            lambda caller, *, outcome: outcomes.append(outcome),
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.receive_json()
            await ws.close()
        for _ in range(int(_AUDIT_WAIT_TIMEOUT_SECS / 0.02)):
            if outcomes:
                break
            await asyncio.sleep(0.02)
        assert outcomes == ["timeout"], outcomes

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
    def test_streaming_defaults_true(self):
        """Streaming is on out of the box, on every provider.

        Recognition is local and free by default, so there is no per-word cost to
        opt into, and words appearing while the user is still speaking is what makes
        dictation feel like dictation. Turning it off is the opt-in now: it buys less
        CPU on the on-device providers and fewer API calls on ``transcribe``.
        """
        assert SttConfig().streaming is True

    def test_streaming_respects_an_explicit_opt_out(self):
        """The field is honoured, not just defaulted.

        Pinned at ``False``, the value that DIFFERS from the default: asserting the
        default's own value here would hold even if the field stopped being read.
        """
        assert SttConfig(streaming=False).streaming is False


class TestConfigPutRoundTrip:
    """Verify the STT config PUT handler persists the streaming field."""

    @pytest.mark.asyncio
    async def test_put_persists_streaming(self, tmp_path, monkeypatch):
        """A saved ``streaming`` choice reaches disk and survives a fresh load.

        Written as an opt-OUT, the value that differs from the default: PUTting
        ``True`` would leave every assertion here true even if the handler dropped
        the field entirely, since a fresh load would answer ``True`` on its own.
        """
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
                "/api/config/stt", json={"streaming": False, "provider": "transcribe"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["streaming"] is False
            assert data["provider"] == "transcribe"
            # Assert it persisted to disk (survives a fresh load).
            cfg_file = tmp_path / "config.json"
            assert cfg_file.exists()
            import json as _json

            on_disk = _json.loads(cfg_file.read_text(encoding="utf-8"))
            assert on_disk["stt"]["streaming"] is False
            # Assert KiroCrewConfig.load() correctly deserializes — guards
            # against field-name mismatches that would silently break at runtime.
            reloaded = KiroCrewConfig.load()
            assert reloaded.stt.streaming is False

    @pytest.mark.asyncio
    async def test_put_rejects_non_boolean_streaming(self, tmp_path, monkeypatch):
        """Non-boolean ``streaming`` values must be ignored, not coerced.

        ``bool()`` of a JSON string is the trap: ``bool("false")`` is ``True`` and
        ``bool("")`` is ``False``, so a coercing handler moves the setting on input
        that expressed no choice. With streaming on by default the damage runs both
        ways (a falsy non-bool would silently turn dictation's live text off, and a
        truthy one would turn it back on under a user who opted out), so both
        directions are pinned here. ``isinstance(body["streaming"], bool)`` is what
        keeps the stored value where the user left it.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.dashboard import handlers

        app = web.Application()
        app.router.add_put("/api/config/stt", handlers.api_stt_config)

        async with TestClient(TestServer(app)) as client:
            # Falsy non-bools, against the default-on setting: none may switch it off.
            for value in ("false", "", 0):
                resp = await client.put(
                    "/api/config/stt",
                    json={"streaming": value, "provider": "transcribe"},
                )
                assert resp.status == 200
                assert (await resp.json())["streaming"] is True, value

            # A real opt-out, so the truthy non-bools below have something to undo.
            resp = await client.put(
                "/api/config/stt",
                json={"streaming": False, "provider": "transcribe"},
            )
            assert resp.status == 200
            assert (await resp.json())["streaming"] is False

            # Truthy non-bools must not switch it back on.
            for value in ("true", 1, "yes"):
                resp = await client.put(
                    "/api/config/stt",
                    json={"streaming": value, "provider": "transcribe"},
                )
                assert resp.status == 200
                assert (await resp.json())["streaming"] is False, value

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


class TestSttLanguageCodes:
    """The language list that drives the Chat Settings STT picker.

    The picker is the only supported way to choose a recognition language, so a
    language missing from this list is unreachable without hand-editing
    config.json — even when the provider (AWS Transcribe) supports it.
    """

    def test_korean_is_offered(self):
        """Korean must be selectable in the picker.

        Regression: `ko-KR` was absent while ja-JP and zh-CN were present, so
        Korean speakers could not choose their language from the dashboard.
        """
        from kiro_crew.dashboard.handlers.core import _STT_LANGUAGE_CODES

        assert "ko-KR" in _STT_LANGUAGE_CODES

    def test_codes_are_unique_and_well_formed(self):
        """Every entry is a distinct `ll-CC` BCP-47 tag.

        A duplicate renders twice in the dropdown, and a malformed tag is
        rejected by Transcribe at stream-start rather than at selection time.
        """
        from kiro_crew.dashboard.handlers.core import _STT_LANGUAGE_CODES

        assert len(set(_STT_LANGUAGE_CODES)) == len(_STT_LANGUAGE_CODES)
        for code in _STT_LANGUAGE_CODES:
            language, _, region = code.partition("-")
            assert language.isalpha() and language.islower() and len(language) == 2, code
            assert region.isalpha() and region.isupper() and len(region) == 2, code

    @pytest.mark.asyncio
    async def test_korean_round_trips_through_the_config_api(self, tmp_path, monkeypatch):
        """Selecting Korean persists and is served back to the UI."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.dashboard import handlers

        app = web.Application()
        app.router.add_get("/api/config/stt", handlers.api_stt_config)
        app.router.add_put("/api/config/stt", handlers.api_stt_config)

        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/config/stt",
                json={"provider": "transcribe", "language_code": "ko-KR"},
            )
            assert resp.status == 200

            resp = await client.get("/api/config/stt")
            assert resp.status == 200
            data = await resp.json()
            assert data["language_code"] == "ko-KR"
            assert "ko-KR" in data["language_codes"]


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


class _FakeLocalSession:
    """Scripted stand-in for ``stt.LocalSession``: no recogniser, no model, no audio.

    Mirrors the real object's contract rather than a convenient subset of it, since
    the transport reads all of it: ``pending_download()`` is asked BEFORE
    ``prepare()`` (a first-run notice cannot wait for the transfer it describes),
    ``feed()`` answers with a list per chunk, ``ended`` latches only when the SESSION
    is over (`finish`/`cancel`, never a detector final -- a session spans many
    utterances), ``has_pending_audio`` reports whether a tail is still worth decoding,
    and ``finish()`` stands for the full-buffer decode.
    """

    def __init__(
        self,
        *,
        pending=None,
        prepare_events=(),
        feed_events=(),
        final_text="",
        final_event=None,
        prepare_gate=None,
    ):
        self.kwargs: dict = {}
        self.fed: list[bytes] = []
        self.prepared = False
        #: Counted, not just flagged: the refusal path starts a detached
        #: transfer, so a test needs to see that prepare was entered even
        #: though the socket closed before it returned.
        self.prepare_calls = 0
        self.finished = False
        self.cancelled = False
        self._pending = pending
        self._prepare_events = list(prepare_events)
        self._feed_events = [list(batch) for batch in feed_events]
        self._final_text = final_text
        #: What ``finish()`` answers when it is not a plain final. The real session
        #: returns an ``error`` event when the whole-buffer decode FAILED, which is a
        #: different outcome from the empty final a silent room produces.
        self._final_event = final_event
        self._prepare_gate = prepare_gate
        self._ended = False
        #: True once audio has been fed that no final has covered. Reset by a final,
        #: exactly as the real session drops a finalised utterance's buffer.
        self._pending_audio = False

    @property
    def ended(self) -> bool:
        return self._ended

    @property
    def has_pending_audio(self) -> bool:
        return self._pending_audio

    def pending_download(self):
        return self._pending

    async def prepare(self) -> list:
        self.prepare_calls += 1
        if self._prepare_gate is not None:
            await self._prepare_gate.wait()
        self.prepared = True
        return list(self._prepare_events)

    async def feed(self, raw_int16: bytes) -> list:
        self.fed.append(raw_int16)
        self._pending_audio = True
        events = self._feed_events.pop(0) if self._feed_events else []
        if any(event.kind == stt.KIND_FINAL for event in events):
            # Finalises the utterance and re-arms. Deliberately does NOT set
            # `_ended`: the session continues, which is what the continuous
            # consumer needs.
            self._pending_audio = False
        return events

    async def finish(self):
        self.finished = True
        self._ended = True
        self._pending_audio = False
        if self._final_event is not None:
            return self._final_event
        return stt.SttEvent(stt.KIND_FINAL, text=self._final_text)

    def cancel(self) -> None:
        self.cancelled = True
        self._ended = True


class _RecordingWS:
    """Minimal ``WebSocketResponse`` stand-in for driving a session function directly.

    For the cases whose subject is a client that has ALREADY gone away: a real test
    client cannot be made to fail a send at a chosen moment, so a socket-level
    version of those tests would be asserting on the scheduler instead of on the
    handler. ``send_fails_after`` raises the error aiohttp raises for a peer that
    reset the connection, and sets *gone* so the fake session can let its download
    finish at that exact point.
    """

    def __init__(self, *, send_fails_after=None, gone=None):
        self.sent: list[dict] = []
        self.closed = False
        self._send_fails_after = send_fails_after
        self._gone = gone

    async def send_json(self, data) -> None:
        if self._send_fails_after is not None and len(self.sent) >= self._send_fails_after:
            if self._gone is not None:
                self._gone.set()
            raise ConnectionResetError("transport gone")
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        raise AssertionError("the read loop must not be reached on this path")


class TestLocalStreamingSession:
    """The default provider's own WebSocket path (``_run_local_session``).

    It reuses the endpoint, the frame shapes and the endpointer, and adds the one
    frame the other two providers never send: a ``status`` report around the
    one-time model download. The lifecycle code is its own, so the invariants the
    other branches already guard need their own coverage here rather than being
    assumed shared.
    """

    def _install(self, monkeypatch, session, **cfg_kwargs):
        """Point the endpoint at ``local`` with *session* standing in for the recogniser."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg(provider="local", **cfg_kwargs)),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)

        def _factory(**kwargs):
            session.kwargs = kwargs
            return session

        monkeypatch.setattr(stt, "LocalSession", _factory)
        return session

    def _record_end_audits(self, monkeypatch) -> list[str]:
        """Collect every ``stt_stream_end`` outcome, in order.

        Patched at the emitter rather than at ``sel()`` because the assertion is a
        COUNT: one end per exit and no more, which a stub that also receives the
        start and rejection events cannot state as plainly.
        """
        outcomes: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream._emit_end_audit",
            lambda caller, *, outcome: outcomes.append(outcome),
        )
        return outcomes

    @pytest.mark.asyncio
    async def test_local_relays_a_partial_then_a_final(self, monkeypatch):
        """The default provider streams behind the same socket as the paid one.

        The frontend cannot tell the three providers apart, so this branch has to
        produce the same ``ready`` / ``partial`` / ``final`` frames from a completely
        different recogniser. The session's config also has to reach it: a language
        or silence window that stops being threaded through leaves recognition
        working and every setting inert.
        """
        session = self._install(
            monkeypatch,
            _FakeLocalSession(
                feed_events=[
                    [stt.SttEvent(stt.KIND_PARTIAL, text="hello")],
                    [stt.SttEvent(stt.KIND_FINAL, text="hello world")],
                ]
            ),
            language_code="fr-FR",
            silence_ms=900,
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_bytes(b"\x00\x01" * 16)
            assert (await ws.receive_json()) == {"type": "partial", "text": "hello"}
            await ws.send_bytes(b"\x00\x01" * 16)
            assert (await ws.receive_json()) == {"type": "final", "text": "hello world"}
            await ws.close()
        # `fr-FR` reduced to the bare language whisper names, not passed through.
        assert session.kwargs["language"] == "fr"
        assert session.kwargs["silence_ms"] == 900
        # The final already went out, so teardown must ABANDON the audio rather than
        # spend a second full-buffer decode on the one shared model.
        assert session.cancelled is True
        assert session.finished is False

    @pytest.mark.asyncio
    async def test_a_stop_frame_yields_the_full_buffer_decode(self, monkeypatch):
        """A user who stops the recording keeps ONE decode of everything heard.

        The partials are fast approximations of successive phrases; the text the
        user keeps is a single decode of the whole utterance, so the transcript has
        the context the model would have had if it had never been streamed. Skipping
        ``finish()`` here would hand them the last partial instead.
        """
        session = self._install(monkeypatch, _FakeLocalSession(final_text="the whole utterance"))
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_bytes(b"\x00\x01" * 16)
            await ws.send_str('{"type":"stop"}')
            assert (await ws.receive_json()) == {
                "type": "final",
                "text": "the whole utterance",
            }
            await ws.close()
        assert session.finished is True

    @pytest.mark.asyncio
    async def test_a_failed_final_decode_reaches_the_client_before_the_close(self, monkeypatch):
        """The teardown decode is the LAST thing a session does, so its failure must
        make it out ahead of the close.

        A dropped empty final and a failed decode used to look identical from here:
        the socket closed with no frame at all, and the client cleared the partial it
        was showing. The frame carries the code because the browser renders localised
        text; the audit records ``error`` because a session that died must not be
        filed as one that finished.
        """
        outcomes = self._record_end_audits(monkeypatch)
        session = self._install(
            monkeypatch,
            _FakeLocalSession(
                final_event=stt.SttEvent(
                    stt.KIND_ERROR,
                    text=stt.DECODE_FAILED_ADVISORY,
                    code=stt.CODE_DECODE_FAILED,
                )
            ),
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_bytes(b"\x00\x01" * 16)
            await ws.send_str('{"type":"stop"}')
            assert (await ws.receive_json()) == {
                "type": "error",
                "message": stt.DECODE_FAILED_ADVISORY,
                "code": stt.CODE_DECODE_FAILED,
            }
            await ws.close()
        assert session.finished is True
        # Waited on rather than asserted straight after the context exits: neither
        # receiving the frame nor leaving `TestClient` orders this after the handler's
        # remaining steps.
        for _ in range(200):
            if outcomes:
                break
            await asyncio.sleep(0.01)
        assert outcomes == ["error"]

    @pytest.mark.asyncio
    async def test_the_vad_ending_an_utterance_emits_final_not_endpoint(self, monkeypatch):
        """The recogniser deciding the speaker stopped is NOT permission to submit.

        ``endpoint`` means "this request looks complete, you may auto-submit" and
        stays governed by ``stt.endpointing`` (off here, as it is by default). The
        detector finalising an utterance is a different event with a different
        consequence: emitting ``endpoint`` for it would auto-send the message box on
        every pause long enough to end a phrase.
        """
        self._install(
            monkeypatch,
            _FakeLocalSession(
                feed_events=[
                    [stt.SttEvent(stt.KIND_FINAL, text="ship it")],
                    [stt.SttEvent(stt.KIND_PARTIAL, text="and again")],
                ]
            ),
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_bytes(b"\x00\x01" * 16)
            assert (await ws.receive_json()) == {"type": "final", "text": "ship it"}
            # The session stays OPEN across the final, so what proves no `endpoint`
            # frame followed is that the next frame is the next utterance's partial.
            # Closing here instead would have ended the Meetings app's transcription
            # on the speaker's first pause.
            await ws.send_bytes(b"\x00\x01" * 16)
            assert (await ws.receive_json()) == {"type": "partial", "text": "and again"}
            await ws.close()

    @pytest.mark.asyncio
    async def test_a_first_run_refuses_rather_than_capturing_speech_it_will_lose(self, monkeypatch):
        """A first run cannot stream, so it must say so instead of listening.

        Waiting for the model here looked reasonable and lost words: the client caps
        its pre-``ready`` buffer at a few seconds and releases the microphone when
        ``ready`` does not arrive, so everything said during a 148 MB fetch was
        captured and then discarded. The socket therefore announces the size, starts
        the transfer in the background so the NEXT attempt works, and refuses with a
        machine-readable code.
        """
        model = stt.resolve_model("base")
        gate = asyncio.Event()
        session = self._install(monkeypatch, _FakeLocalSession(pending=model, prepare_gate=gate))
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {
                "type": "status",
                "stage": "downloading",
                "downloaded_bytes": 0,
                "total_bytes": model.size_bytes,
                "code": "stt_model_missing",
            }
            assert (await ws.receive_json()) == {
                "type": "error",
                "message": "speech model is still downloading",
                "code": "stt_model_missing",
            }
            # No `ready`, so the client never opens the microphone.
            await ws.close()
        # The transfer was still kicked off, which is the whole point of refusing
        # rather than failing: the model lands on disk for the next attempt.
        gate.set()
        await asyncio.sleep(0)
        assert session.prepare_calls >= 1

    @pytest.mark.asyncio
    async def test_no_status_frames_when_the_model_is_already_present(self, monkeypatch):
        """``stage="ready"`` is sent only when a transfer actually ran.

        It tells the panel to stop polling for byte progress and drop the download
        notice, so sending it unconditionally would have every ordinary session
        announce the end of a transfer that never happened.
        """
        self._install(monkeypatch, _FakeLocalSession(pending=None))
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_str('{"type":"stop"}')
            await ws.close()

    @pytest.mark.asyncio
    async def test_download_progress_is_republished_while_the_transfer_runs(self, monkeypatch):
        """One status frame is not enough: the byte count must keep arriving.

        The consumer arms a 20s stall watchdog on the last frame it received and
        RECONNECTS when it fires, so a single frame at the start of a 148 MB fetch
        would have the client tear the socket down and restart the transfer, forever.
        """
        from kiro_crew.dashboard import stt_stream

        model = stt.resolve_model("base")
        gate = asyncio.Event()
        reads = {"n": 0}

        class _Store:
            @property
            def status(self):
                # Advances on every read, and releases prepare() once three reads
                # have happened, so the loop count is fixed by this test rather than
                # by how long a real transfer takes.
                reads["n"] += 1
                if reads["n"] >= 3:
                    gate.set()
                return {
                    "step": "downloading",
                    "downloaded_bytes": reads["n"] * 1_000_000,
                    "total_bytes": model.size_bytes,
                }

        monkeypatch.setattr(stt, "model_store", lambda: _Store())
        # 0, not a shortened interval: the cadence is not what is under test, and a
        # real one would trade wall clock for nothing.
        monkeypatch.setattr(stt_stream, "_MODEL_PROGRESS_INTERVAL_SECS", 0)

        sent: list[dict] = []

        async def _send(frame):
            sent.append(frame)
            return True

        async def _prepare():
            await gate.wait()
            return []

        task = asyncio.create_task(_prepare())
        # Bounded, because the regression this guards against does not fail, it
        # STOPS: a loop that publishes once and then waits leaves prepare() gated
        # forever, and an unbounded await would report that as a hung suite rather
        # than as this assertion. Only ever elapses on that regression.
        try:
            relayed = await asyncio.wait_for(
                stt_stream._relay_download_progress(task, _send),
                timeout=_AUDIT_WAIT_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            raise AssertionError(
                f"progress stopped being republished after {len(sent)} frame(s)"
            ) from None
        assert relayed == []

        assert len(sent) >= 2, sent
        counts = [frame["downloaded_bytes"] for frame in sent]
        # Sorted AND distinct, i.e. strictly growing: a repeated count would leave
        # the panel's progress bar frozen while the transfer ran.
        assert counts == sorted(counts) and len(set(counts)) == len(counts), counts
        assert all(frame["stage"] == "downloading" for frame in sent), sent
        assert all(frame["total_bytes"] == model.size_bytes for frame in sent), sent

    @pytest.mark.asyncio
    async def test_a_failed_progress_send_stops_reporting_not_the_transfer(self, monkeypatch):
        """A peer that went away must not cost the bytes already on the wire.

        The transfer is what the NEXT attempt inherits, so abandoning it when the
        client disappears turns a resumable first run into one that starts from zero
        every time. Reporting stops, the download does not.
        """
        from kiro_crew.dashboard import stt_stream

        gate = asyncio.Event()
        finished: list[bool] = []

        class _Store:
            @property
            def status(self):
                gate.set()
                return {"step": "downloading", "downloaded_bytes": 1, "total_bytes": 2}

        monkeypatch.setattr(stt, "model_store", lambda: _Store())
        monkeypatch.setattr(stt_stream, "_MODEL_PROGRESS_INTERVAL_SECS", 0)

        sent: list[dict] = []

        async def _send(frame):
            sent.append(frame)
            return False

        async def _prepare():
            await gate.wait()
            finished.append(True)
            return []

        task = asyncio.create_task(_prepare())
        assert await stt_stream._relay_download_progress(task, _send) == []
        assert len(sent) == 1, sent
        assert finished == [True]

    @pytest.mark.asyncio
    async def test_an_unavailable_recogniser_keeps_its_own_code(self, monkeypatch):
        """The stt package's availability codes travel through unremapped.

        The settings panel already renders a localised string for each of them, and
        the actions differ ("install the extra", "no wheel for this platform"), so
        collapsing them into a transport-level code here would send the user to fix
        the wrong thing.
        """
        outcomes = self._record_end_audits(monkeypatch)
        self._install(
            monkeypatch,
            _FakeLocalSession(
                prepare_events=[
                    stt.SttEvent(
                        stt.KIND_ERROR,
                        text="local speech needs the voice extra",
                        code=stt.CODE_EXTRA_MISSING,
                    )
                ]
            ),
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {
                "type": "error",
                "message": "local speech needs the voice extra",
                "code": stt.CODE_EXTRA_MISSING,
            }
            await ws.close()
        assert outcomes == ["error"], outcomes

    @pytest.mark.asyncio
    async def test_a_clean_stop_emits_exactly_one_start_and_one_end(self, monkeypatch):
        """The pairing this file exists for, on the branch that is now the default."""
        self._install(monkeypatch, _FakeLocalSession())
        calls: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: calls.append(kw)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_str('{"type":"stop"}')
            await ws.close()
        await _wait_for_operation(calls, "stt_stream_end")
        ops = [c["operation"] for c in calls]
        assert ops.count("stt_stream_start") == 1, ops
        assert ops.count("stt_stream_end") == 1, ops
        end = next(c for c in calls if c["operation"] == "stt_stream_end")
        assert end["outcome"] == "ok"

    @pytest.mark.asyncio
    async def test_a_download_that_outruns_the_ceiling_is_audited_once(self, monkeypatch):
        """Giving up on a slow transfer is an exit path, so it owes an end audit.

        Only this socket gives up: the fetch is shielded and LEFT RUNNING, because
        cancelling it releases the model store's transfer lock while its worker
        thread is still writing the staging file, and the next session would then
        start a second write to that same path. The bytes land on disk for the next
        attempt, which is also why the frame says "still downloading" rather than
        reporting a failure.
        """
        from kiro_crew.dashboard import stt_stream

        outcomes = self._record_end_audits(monkeypatch)
        gate = asyncio.Event()
        session = self._install(
            monkeypatch,
            _FakeLocalSession(pending=stt.resolve_model("base"), prepare_gate=gate),
        )
        # 0 rather than a shortened ceiling: the wait itself is not under test, and
        # `wait_for` treats a non-positive timeout as "already expired".
        monkeypatch.setattr(stt_stream, "_MAX_MODEL_PREPARE_SECS", 0)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json())["stage"] == "downloading"
            assert (await ws.receive_json()) == {
                "type": "error",
                "message": "speech model is still downloading",
                "code": "stt_model_missing",
            }
            await ws.close()
        assert outcomes == ["error"], outcomes
        # The transfer was abandoned by this socket, not cancelled: released now, it
        # still runs to completion.
        gate.set()
        for _ in range(100):
            if session.prepared:
                break
            await asyncio.sleep(0)
        assert session.prepared is True

    @pytest.mark.asyncio
    async def test_a_client_that_disconnects_mid_download_is_audited_once(self, monkeypatch):
        """A closed tab during a first-run fetch still balances the trail.

        This exit is reached with no read loop ever entered and no session teardown
        in scope, so it is the one most likely to be missing its end audit, and an
        unmatched ``stt_stream_start`` is indistinguishable in the trail from a
        session that is still open.
        """
        from kiro_crew.dashboard import stt_stream

        outcomes = self._record_end_audits(monkeypatch)
        model = stt.resolve_model("base")

        class _Store:
            """A transfer in flight, so the loop has progress worth republishing."""

            status = {"step": "downloading", "downloaded_bytes": 1, "total_bytes": model.size_bytes}

        monkeypatch.setattr(stt, "model_store", lambda: _Store())
        monkeypatch.setattr(stt_stream, "_MODEL_PROGRESS_INTERVAL_SECS", 0)
        gone = asyncio.Event()
        session = _FakeLocalSession(pending=model, prepare_gate=gone)
        monkeypatch.setattr(stt, "LocalSession", lambda **kwargs: session)
        # Accepts the opening notice, then fails the way aiohttp fails for a peer
        # that reset the connection, which is also the point the transfer is let
        # finish: the download must outlive the client that asked for it.
        ws = _RecordingWS(send_fails_after=1, gone=gone)

        await stt_stream._run_local_session(ws, _cfg(provider="local"), MagicMock(), "test-caller")

        assert outcomes == ["error"], outcomes
        assert ws.closed is True
        assert session.cancelled is True
        # STARTED, not finished: the fetch is detached on purpose so it outlives the
        # socket that asked for it, which is what makes the next attempt find the
        # weights on disk. Waiting for it here would assert the opposite property.
        #
        # The yield is required, not incidental: `create_task` only schedules, so
        # without giving the loop a turn the count is still zero and the assertion
        # would read as "the transfer was never started".
        await asyncio.sleep(0)
        assert session.prepare_calls >= 1
        gone.set()
        await asyncio.sleep(0)
        assert session.prepared is True, "the detached transfer must still complete"

    @pytest.mark.asyncio
    async def test_a_session_that_cannot_be_built_still_emits_the_end_audit(self, monkeypatch):
        """Constructing the session is the first thing that imports the recogniser.

        So a broken optional dependency raises before any teardown exists, while
        ``stt_stream_start`` has already been logged. Nothing has been created at
        that point, which makes the end audit the whole of the cleanup, and without
        it the trail shows a voice session that never ended.
        """
        from kiro_crew.dashboard import stt_stream

        outcomes = self._record_end_audits(monkeypatch)

        def _boom(**kwargs):
            raise ImportError("numpy is not installed correctly")

        monkeypatch.setattr(stt, "LocalSession", _boom)
        with pytest.raises(ImportError):
            await stt_stream._run_local_session(
                _RecordingWS(), _cfg(provider="local"), MagicMock(), "test-caller"
            )
        assert outcomes == ["error"], outcomes

    @pytest.mark.asyncio
    async def test_a_failing_decode_reports_a_code_and_is_audited_once(self, monkeypatch):
        """A recogniser that raises mid-session is an exit path like any other.

        The client is told the session failed rather than being left on a live socket
        that has silently stopped transcribing, and the trail gets its one matching
        end. Sending nothing here is the failure mode a silent microphone is
        indistinguishable from.
        """
        outcomes = self._record_end_audits(monkeypatch)

        class _Exploding(_FakeLocalSession):
            async def feed(self, raw_int16):
                raise RuntimeError("decode blew up")

        self._install(monkeypatch, _Exploding())
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            await ws.send_bytes(b"\x00\x01" * 16)
            assert (await ws.receive_json()) == {
                "type": "error",
                "message": "transcription failed",
                "code": "stt_session_failed",
            }
            await ws.close()
        assert outcomes == ["error"], outcomes

    @pytest.mark.asyncio
    async def test_cap_teardown_is_audited_as_a_timeout(self, monkeypatch):
        """The duration cap applies here too, and must not read as a clean stop.

        Nothing is metered on this provider, but an abandoned session still
        accumulates buffered audio and holds one of `_MAX_CONCURRENT_SESSIONS` slots,
        so an operator diagnosing "dictation says it is busy" needs the trail to name
        the sessions the cap ended. Each branch claims the cause itself rather than
        inferring it from the deadline task's state, because the cap's own
        `ws.close()` is what ends the read loop: the cleanup runs while that task is
        still awaiting the peer and reads as not-yet-done.
        """
        outcomes = self._record_end_audits(monkeypatch)
        self._install(monkeypatch, _FakeLocalSession())
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 0.05)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json()) == {"type": "ready"}
            # Deliberately send NO audio: only the deadline task can end this.
            assert (await ws.receive_json()) == {
                "type": "error",
                "message": "max stream duration exceeded",
                "code": "stt_max_duration_exceeded",
            }
            await ws.close()
        assert outcomes == ["timeout"], outcomes

    @pytest.mark.asyncio
    async def test_no_progress_frame_while_the_store_reports_no_transfer(self, monkeypatch):
        """Only a running transfer is republished, not whatever the store last said.

        Between ``prepare()`` being called and the store entering its downloading
        step, and again once a transfer has failed, the byte counts are zeros. Sending
        those would leave the panel rendering a progress bar for a transfer that is
        not moving, at the republication cadence, for as long as the wait lasts.
        """
        from kiro_crew.dashboard import stt_stream

        gate = asyncio.Event()

        class _Store:
            @property
            def status(self):
                gate.set()
                return {"step": "failed", "downloaded_bytes": 0, "total_bytes": 0}

        monkeypatch.setattr(stt, "model_store", lambda: _Store())
        monkeypatch.setattr(stt_stream, "_MODEL_PROGRESS_INTERVAL_SECS", 0)

        sent: list[dict] = []

        async def _send(frame):
            sent.append(frame)
            return True

        async def _prepare():
            await gate.wait()
            return []

        task = asyncio.create_task(_prepare())
        assert await stt_stream._relay_download_progress(task, _send) == []
        assert sent == []

"""Tests for streaming STT WebSocket endpoint (dashboard/stt_stream.py).

This file holds the endpoint's guards and the ``apple`` and ``transcribe`` providers'
sessions. The default ``local`` provider's session lives in
``test_stt_stream_local_session.py``, the STT config in ``test_stt_stream_config.py``,
the failures around a stream in ``test_stt_stream_defensive_guards.py``, and the
shared socket helpers in ``stt_stream_helpers.py``.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from stt_stream_helpers import transcribe_consented_fixture  # noqa: F401
from stt_stream_helpers import (
    _arm_cap,
    _await_end_audit,
    _cfg,
    _make_app,
    _recv_json,
    _wait_for_operation,
)

from kiro_crew import stt
from kiro_crew.config.loader import SttConfig
from kiro_crew.testing.wait import await_until, default_timeout


async def _bounded(awaitable):
    """Await a client-side websocket read, failing by name instead of hanging.

    A regression that leaves the server silent would otherwise park the test until
    the 300 s duration cap, past the suite's per-test timeout, and lose the worker.
    """
    return await asyncio.wait_for(awaitable, default_timeout())


class TestGuards:
    @pytest.mark.asyncio
    async def test_rejects_when_streaming_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg(streaming=False)),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            frame = await _recv_json(ws)
            assert frame["type"] == "error"
            assert frame["code"] == "stt_streaming_disabled"
            await ws.close()

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
            ws = await client.ws_connect("/api/ws/stt")
            frame = await _recv_json(ws)
            assert frame["type"] == "error"
            assert frame["code"] == "stt_streaming_disabled"
            await ws.close()

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
            ws = await client.ws_connect("/api/ws/stt")
            frame = await _recv_json(ws)
            assert frame["type"] == "error"
            assert frame["code"] == "stt_streaming_disabled"
            await ws.close()

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
            ws = await client.ws_connect("/api/ws/stt")
            frame = await _recv_json(ws)
            assert frame["type"] == "error"
            assert frame["code"] == "stt_streaming_disabled"
            await ws.close()
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


@pytest.mark.asyncio
async def test_receive_until_takes_its_pending_read_down_with_an_outer_cancellation():
    """A reader cancelled from outside must not leave its `__anext__` in flight.

    That orphan would sit in `receive()` until the close and end unretrieved.
    """
    from kiro_crew.dashboard import stt_stream

    started = asyncio.Event()
    released = asyncio.Event()
    seen: list[str] = []

    class Blocking:
        def __aiter__(self):
            return self

        async def __anext__(self):
            started.set()
            try:
                await released.wait()
            except asyncio.CancelledError:
                seen.append("read cancelled")
                raise
            raise StopAsyncIteration

    async def consume():
        async for _ in stt_stream._receive_until(Blocking(), asyncio.Event()):
            pass

    task = asyncio.create_task(consume())
    await await_until(started, what="the reader starting")
    task.cancel()
    # Joined under the lost-run bound without awaiting the task itself: a reader
    # that swallowed the cancellation would never finish, and this join must fail
    # here by name rather than hang until the suite's global timeout.
    await await_until(
        asyncio.gather(task, return_exceptions=True), what="the cancelled reader finishing"
    )
    assert seen == ["read cancelled"]


class TestAppleStreamingSession:
    """The `apple` provider's own WebSocket path (`_run_apple_session`).

    It reuses the endpoint, the event shapes and the endpointer, but has its own
    lifecycle code — so the invariants the AWS path already guards need their own
    coverage here rather than being assumed shared.
    """

    def _install(
        self,
        monkeypatch,
        *,
        session=None,
        start_error="",
        feed_ok=True,
        avail=None,
        language_code="auto",
    ):
        """Point the endpoint at the apple provider with a stubbed helper session.

        *avail* is what the double reports from ``availability()``, which the error
        path consults to name a start failure. It defaults to a capable host so the
        cases that never fail to start do not depend on the runner's OS.
        """
        from kiro_crew import apple_speech as real_apple_speech

        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(lambda cls: _cfg(provider="apple", language_code=language_code)),
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
    async def test_ready_advertises_the_connection_cap(self, monkeypatch):
        """Same figure, same field, as the other two providers: one contract."""
        self._install(monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 7)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            ready = await _recv_json(ws)
            assert ready["type"] == "ready"
            assert ready["max_duration_ms"] == 7000
            await ws.send_json({"type": "stop"})
            await ws.close()

    @pytest.mark.asyncio
    async def test_the_cap_finishes_the_helper_before_its_error_frame(self, monkeypatch):
        """The cap must reach `session.finish()` while the socket is still open.

        Closing stdin is the helper's cue to finalize, and its trailing final is
        relayed afterwards; a socket already closed by the deadline task has no
        recipient for it. Order on the wire: the helper's final, the cap frame,
        the close.
        """
        # `_install` returns the queue the fake helper's `events()` drains; its
        # `finish()` ends that queue with None. Subclass through the patched
        # module attribute (as `test_stream_uses_effective_locale` does) so
        # finish() first enqueues the trailing final the real helper would emit.
        events, _ = self._install(monkeypatch)
        from kiro_crew import apple_speech

        class Finishing(apple_speech.StreamingSession):
            async def finish(self, **kwargs):
                await events.put({"type": "final", "text": "what was being said"})
                return await super().finish(**kwargs)

        monkeypatch.setattr(apple_speech, "StreamingSession", Finishing)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 0.05)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _recv_json(ws))["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            # Only an OPEN socket can carry this frame: it proves finish() ran
            # before the close, which the old deadline task made impossible.
            assert await _recv_json(ws) == {
                "type": "final",
                "text": "what was being said",
            }
            assert await _recv_json(ws) == {
                "type": "error",
                "message": "max stream duration exceeded",
                "code": "stt_max_duration_exceeded",
            }
            await ws.close()

    @pytest.mark.asyncio
    async def test_a_cap_that_fires_mid_feed_lets_the_frame_finish(self, monkeypatch):
        """The reader stops between frames, never inside one.

        Cancelling the reader task mid-`feed` would cut a frame half-way into the
        helper's stdin. The cap now only ends the loop once the frame in flight
        has been forwarded, and the helper is finished after it — so the frame is
        whole, in order, and the trailing final still precedes the cap frame.
        """
        events, fed = self._install(monkeypatch)
        from kiro_crew import apple_speech

        fire = _arm_cap(monkeypatch)
        feeding = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        class SlowFeed(apple_speech.StreamingSession):
            async def feed(self, pcm):
                feeding.set()
                await await_until(release, what="the test releasing the feed")
                order.append("fed")
                return await super().feed(pcm)

            async def finish(self, **kwargs):
                order.append("finish")
                await events.put({"type": "final", "text": "after the frame"})
                return await super().finish(**kwargs)

        monkeypatch.setattr(apple_speech, "StreamingSession", SlowFeed)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _recv_json(ws))["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            await await_until(feeding, what="the frame reaching feed()")
            fire.set()  # the cap lands while that frame is still being fed
            release.set()
            assert await _recv_json(ws) == {
                "type": "final",
                "text": "after the frame",
            }
            assert (await _recv_json(ws))["code"] == ("stt_max_duration_exceeded")
            await ws.close()
        assert order == ["fed", "finish"]
        assert fed == [b"\x00\x01" * 16]

    @pytest.mark.asyncio
    async def test_a_cap_cancels_a_reader_stuck_inside_a_feed_after_the_grace(self, monkeypatch):
        """A helper that stopped reading its stdin cannot hold the session past the cap.

        The reader stops between frames, but a feed that never returns has no
        next frame; after `_CAP_READER_GRACE_SECS` the cap cancels it after all,
        the helper is finished, and the cap frame still reaches the client.
        """
        _, fed = self._install(monkeypatch)
        from kiro_crew import apple_speech
        from kiro_crew.dashboard import stt_stream

        fire = _arm_cap(monkeypatch)
        monkeypatch.setattr(stt_stream, "_CAP_READER_GRACE_SECS", 0.05)
        feeding = asyncio.Event()
        stuck = asyncio.Event()  # never set: the helper stopped reading

        class Stuck(apple_speech.StreamingSession):
            async def feed(self, pcm):
                feeding.set()
                await stuck.wait()
                return await super().feed(pcm)

        monkeypatch.setattr(apple_speech, "StreamingSession", Stuck)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _recv_json(ws))["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            await await_until(feeding, what="the frame reaching feed()")
            fire.set()
            assert (await _recv_json(ws))["code"] == "stt_max_duration_exceeded"
            await ws.close()
        assert fed == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("language", "expected_locale"),
        [("auto", "en-US"), ("en-US", "en-US"), ("zh-CN", "zh-CN")],
    )
    async def test_stream_uses_effective_locale(self, monkeypatch, language, expected_locale):
        self._install(monkeypatch, language_code=language)
        from kiro_crew import apple_speech

        factory = MagicMock(wraps=apple_speech.StreamingSession)
        monkeypatch.setattr(apple_speech, "StreamingSession", factory)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _recv_json(ws))["type"] == "ready"
            await ws.send_json({"type": "stop"})
            await ws.close()
        assert factory.call_args.kwargs["locale"] == expected_locale

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
            assert (await ws.receive_json())["type"] == "ready"
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
            assert (await ws.receive_json())["type"] == "ready"
            await ws.receive_json()  # the cap's error frame
            await ws.close()
        await _await_end_audit(outcomes)
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
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_str('{"type":"stop"}')
            await ws.close()
        await _await_end_audit(outcomes)
        assert outcomes == ["ok"], outcomes

    @pytest.mark.asyncio
    async def test_partials_and_finals_are_relayed_redacted(self, monkeypatch):
        """A partial reaches the browser DOM, so it is an external surface even
        though the next partial replaces it and nothing is persisted."""
        events, _ = self._install(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json())["type"] == "ready"
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
            assert (await ws.receive_json())["type"] == "ready"
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
            assert (await ws.receive_json())["type"] == "ready"
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


class TestStreamLifecycle:
    """Mock TranscribeStreamingClient to verify lifecycle + redaction."""

    @pytest.fixture(autouse=True)
    def _consented(self, transcribe_consented):
        """Every case here drives ``api_ws_stt``, which is consent-gated."""

    @pytest.fixture(autouse=True)
    def _require_amazon_transcribe(self):
        pytest.importorskip("amazon_transcribe")

    def _install_stubs(
        self,
        monkeypatch,
        *,
        fail_start=False,
        language_code="auto",
        vocabulary="",
        start_exc=None,
        languages=(),
    ):
        from amazon_transcribe.handlers import TranscriptResultStreamHandler

        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.KiroCrewConfig.load",
            classmethod(
                lambda cls: _cfg(
                    language_code=language_code,
                    transcribe_vocabulary=vocabulary,
                    transcribe_languages=list(languages),
                )
            ),
        )
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.check_origin", lambda r, require: True)

        # Stub Transcribe client.
        input_stream = MagicMock()
        input_stream.send_audio_event = AsyncMock()
        # The real result stream ends only once the input stream is ended.
        results_ended = asyncio.Event()
        input_stream.end_stream = AsyncMock(side_effect=results_ended.set)
        stream = MagicMock()
        stream.input_stream = input_stream
        stream.output_stream = MagicMock()

        client = MagicMock()
        if start_exc is not None:
            client.start_stream_transcription = AsyncMock(side_effect=start_exc)
        elif fail_start:
            client.start_stream_transcription = AsyncMock(side_effect=RuntimeError("start failed"))
        else:
            client.start_stream_transcription = AsyncMock(return_value=stream)
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.TranscribeStreamingClient",
            lambda **kw: client,
        )

        original_init = TranscriptResultStreamHandler.__init__
        monkeypatch.setattr(
            TranscriptResultStreamHandler,
            "__init__",
            lambda self, output_stream: original_init(self, output_stream),
        )

        async def _results_until_end_stream(self) -> None:
            await results_ended.wait()

        monkeypatch.setattr(
            TranscriptResultStreamHandler, "handle_events", _results_until_end_stream
        )
        return client, input_stream

    @pytest.mark.asyncio
    async def test_ready_advertises_the_connection_cap(self, monkeypatch):
        """Same figure, same field, as the other two providers: one contract."""
        self._install_stubs(monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 7)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            ready = await _recv_json(ws)
            assert ready["type"] == "ready"
            assert ready["max_duration_ms"] == 7000
            await ws.send_str('{"type":"stop"}')
            await ws.close()

    @pytest.mark.asyncio
    async def test_the_cap_ends_the_input_stream_before_its_error_frame(self, monkeypatch):
        """On the metered provider the trailing final arrives after end_stream().

        A cap that closed the socket first would leave that final decoded, billed
        and thrown away. The input stream is ended and the handler drained while
        the socket is open; the cap frame follows.
        """
        from kiro_crew.dashboard import stt_stream

        _, input_stream = self._install_stubs(monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 0.05)
        order: list[str] = []
        input_stream.end_stream = AsyncMock(side_effect=lambda: order.append("end_stream"))
        original = stt_stream._send_error

        async def _spy(ws, message, code):
            order.append(f"error:{code}")
            await original(ws, message, code)

        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._send_error", _spy)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _recv_json(ws))["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            assert (await _recv_json(ws))["code"] == "stt_max_duration_exceeded"
            await ws.close()
        assert order == ["end_stream", "error:stt_max_duration_exceeded"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("language", "expected_locale"),
        [("auto", "en-US"), ("en-US", "en-US"), ("zh-CN", "zh-CN")],
    )
    async def test_ready_then_stop(self, monkeypatch, language, expected_locale):
        transcribe_client, input_stream = self._install_stubs(monkeypatch, language_code=language)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            await ws.send_str('{"type":"stop"}')
            await ws.close()
        input_stream.send_audio_event.assert_awaited()
        input_stream.end_stream.assert_awaited()
        assert (
            transcribe_client.start_stream_transcription.call_args.kwargs["language_code"]
            == expected_locale
        )

    @pytest.mark.asyncio
    async def test_mixed_languages_open_an_identifying_stream(self, monkeypatch):
        """Two candidates: identification on, no ``LanguageCode`` (Amazon Transcribe
        fails a request carrying both), and the vocabulary as ``VocabularyNames``.
        Everything else on the request is the single-language stream's."""
        transcribe_client, input_stream = self._install_stubs(
            monkeypatch,
            language_code="en-US",
            vocabulary="team-terms",
            languages=["vi-VN", "en-US"],
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _recv_json(ws))["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            await ws.send_str('{"type":"stop"}')
            await ws.close()
        input_stream.send_audio_event.assert_awaited()
        kwargs = transcribe_client.start_stream_transcription.call_args.kwargs
        assert kwargs["language_code"] is None
        assert kwargs["identify_multiple_languages"] is True
        assert kwargs["language_options"] == ["vi-VN", "en-US"]
        assert kwargs["vocabulary_names"] == ["team-terms"]
        assert "vocabulary_name" not in kwargs
        assert kwargs["media_encoding"] == "pcm"
        assert kwargs["partial_results_stability"] == "high"

    @pytest.mark.asyncio
    async def test_a_vocabulary_refusal_keeps_its_code_with_mixed_languages(self, monkeypatch):
        """Under identification the vocabulary rides as ``VocabularyNames``; a refusal
        naming it (for example its language among none of the candidates) still points
        at the vocabulary setting rather than reading as a service fault."""
        from amazon_transcribe.exceptions import BadRequestException

        self._install_stubs(
            monkeypatch,
            vocabulary="team-terms",
            languages=["vi-VN", "fr-FR"],
            start_exc=BadRequestException(
                "None of the languages of the specified custom vocabularies match the "
                "identified language."
            ),
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await _recv_json(ws)
            assert msg["type"] == "error"
            assert msg["code"] == "stt_transcribe_vocabulary_rejected"
            await ws.close()

    @pytest.mark.asyncio
    async def test_one_candidate_keeps_the_single_language_stream(self, monkeypatch):
        transcribe_client, _ = self._install_stubs(
            monkeypatch, language_code="vi-VN", languages=["vi-VN"]
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _recv_json(ws))["type"] == "ready"
            await ws.send_str('{"type":"stop"}')
            await ws.close()
        kwargs = transcribe_client.start_stream_transcription.call_args.kwargs
        assert kwargs["language_code"] == "vi-VN"
        assert kwargs["vocabulary_name"] is None
        assert "identify_multiple_languages" not in kwargs
        assert "language_options" not in kwargs

    @pytest.mark.asyncio
    async def test_an_sdk_too_old_for_mixed_languages_is_refused_before_streaming(
        self, monkeypatch
    ):
        """An SDK without the parameters would raise ``TypeError`` mid-start; the
        socket names the fix instead, before any audio is read, and the audit pair
        stays balanced."""
        transcribe_client, input_stream = self._install_stubs(
            monkeypatch, languages=["vi-VN", "en-US"]
        )
        started: list[dict] = []

        async def start_stream_transcription(
            *, language_code, media_sample_rate_hz, media_encoding, vocabulary_name=None
        ):
            started.append({"language_code": language_code})

        transcribe_client.start_stream_transcription = start_stream_transcription
        calls: list[dict] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream.sel",
            lambda: MagicMock(log_api_access=lambda **kw: calls.append(kw)),
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await _recv_json(ws)
            assert msg["type"] == "error"
            assert msg["code"] == "stt_transcribe_sdk_outdated"
            await _wait_for_operation(calls, "stt_stream_end")
            await ws.close()
        assert started == []
        input_stream.send_audio_event.assert_not_awaited()
        assert [c["outcome"] for c in calls if c["operation"] == "stt_stream_end"] == ["error"]

    @pytest.mark.asyncio
    async def test_start_failure_emits_error(self, monkeypatch):
        self._install_stubs(monkeypatch, fail_start=True)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg["type"] == "error"
            assert msg["code"] == "stt_session_failed"
            await ws.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("vocabulary", "expected"), [("team-terms", "team-terms"), ("", None)])
    async def test_the_configured_vocabulary_rides_on_the_stream(
        self, monkeypatch, vocabulary, expected
    ):
        """None, not ``""``, when unset: the SDK omits the header only for None, and an
        empty name breaks the API's minimum length of 1."""
        transcribe_client, _ = self._install_stubs(monkeypatch, vocabulary=vocabulary)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_str('{"type":"stop"}')
            await ws.close()
        kwargs = transcribe_client.start_stream_transcription.call_args.kwargs
        assert kwargs["vocabulary_name"] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("vocabulary", "service_message", "expected_code"),
        [
            # The refusal this code exists for: retrying cannot help, Settings can.
            # This is the text Amazon Transcribe returned live (us-east-1) when
            # start_stream_transcription named a vocabulary that does not exist.
            (
                "team-terms",
                "The specified vocabulary doesn't exist. Check the name and try your "
                "request again.",
                "stt_transcribe_vocabulary_rejected",
            ),
            # Live text (us-east-1) for a vocabulary still PENDING.
            (
                "team-terms",
                "The specified vocabulary isn't ready for use. Try your request again later.",
                "stt_transcribe_vocabulary_rejected",
            ),
            # Live text (us-east-1) for a READY en-US vocabulary on a fr-FR stream.
            (
                "team-terms",
                "Language used in the vocabulary doesn't match the specified language "
                "code. Correct language code and try again.",
                "stt_transcribe_vocabulary_rejected",
            ),
            # A bad request about something else is not blamed on the vocabulary.
            (
                "team-terms",
                "The language code you specified isn't supported.",
                "stt_session_failed",
            ),
            # With no vocabulary configured, it cannot be the vocabulary.
            ("", "The requested vocabulary couldn't be found.", "stt_session_failed"),
        ],
    )
    async def test_a_vocabulary_refusal_has_its_own_code(
        self, monkeypatch, vocabulary, service_message, expected_code
    ):
        from amazon_transcribe.exceptions import BadRequestException

        self._install_stubs(
            monkeypatch, vocabulary=vocabulary, start_exc=BadRequestException(service_message)
        )
        outcomes: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.stt_stream._emit_end_audit",
            lambda caller, *, outcome: outcomes.append(outcome),
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg["type"] == "error"
            assert msg["code"] == expected_code
            await ws.close()
        await _await_end_audit(outcomes)
        assert outcomes == ["error"]

    def test_only_the_service_refusal_counts_as_a_vocabulary_rejection(self):
        """A transport failure whose text happens to say "vocabulary" is not the
        service refusing it, and with no vocabulary configured nothing is."""
        from amazon_transcribe.exceptions import BadRequestException

        from kiro_crew.dashboard import stt_stream

        refusal = BadRequestException("The requested vocabulary couldn't be found.")
        assert stt_stream._vocabulary_rejected(refusal, "team-terms") is True
        assert stt_stream._vocabulary_rejected(RuntimeError("vocabulary"), "team-terms") is False
        assert stt_stream._vocabulary_rejected(refusal, "") is False

    @pytest.mark.asyncio
    async def test_access_denied_at_start_has_its_own_code(self, monkeypatch):
        """AWS's authorization refusal reaches the browser as its own code.

        Every retry is refused the same way, so the generic session failure, whose
        remedy is to record again, leaves the user stuck. The English stays fixed
        text: AWS's own message names the caller's ARN.
        """
        from amazon_transcribe.exceptions import UnknownServiceException

        denied = UnknownServiceException(
            403,
            "AccessDeniedException",
            "User: arn:aws:sts::111122223333:assumed-role/Test/session is not authorized "
            "to perform: transcribe:StartStreamTranscription",
        )
        self._install_stubs(monkeypatch, start_exc=denied)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            msg = await ws.receive_json()
            assert msg == {
                "type": "error",
                "message": "AWS denied access to transcribe:StartStreamTranscription",
                "code": "stt_aws_access_denied",
            }
            await ws.close()

    def test_other_start_failures_keep_the_generic_code(self):
        """Only the authorization refusal is told apart, not every 403 or SDK error.

        An expired or invalid token is a 403 too, but the consent gate's identity
        probe reports it on the next attempt, and a token IAM has not propagated yet
        works seconds later, so "record again" is the right advice for both.
        """
        from amazon_transcribe.exceptions import BadRequestException, UnknownServiceException

        from kiro_crew.dashboard import stt_stream

        for exc in (
            UnknownServiceException(403, "UnrecognizedClientException", "token is invalid"),
            UnknownServiceException(500, "InternalFailure", "try again"),
            BadRequestException("unsupported language"),
            RuntimeError("No AWS credentials found for profile 'test'"),
        ):
            assert stt_stream._transcribe_start_failure(exc) == (
                "failed to start transcription",
                stt_stream._CODE_SESSION_FAILED,
            ), exc

    def test_access_denied_as_the_sdk_parses_it_keeps_its_code(self):
        """AWS's refusal goes through the SDK's own error parser, not a hand-built exception.

        The parser turns the response's ``x-amzn-errortype`` header into the exception
        the stream start raises. If a release models ``AccessDeniedException`` as a
        class of its own, this test fails, rather than users silently getting the
        generic "record again" advice.
        """
        from amazon_transcribe.deserialize import TranscribeStreamingResponseParser

        from kiro_crew.dashboard import stt_stream

        response = SimpleNamespace(
            status_code=403,
            headers={"x-amzn-errortype": "AccessDeniedException:"},
        )
        body = (
            b'{"Message": "User: arn:aws:sts::111122223333:assumed-role/Test/session is not '
            b'authorized to perform: transcribe:StartStreamTranscription"}'
        )
        exc = TranscribeStreamingResponseParser().parse_exception(response, body)
        assert stt_stream._transcribe_start_failure(exc) == (
            "AWS denied access to transcribe:StartStreamTranscription",
            stt_stream._CODE_AWS_ACCESS_DENIED,
        ), f"the SDK raises {type(exc).__name__} for AccessDeniedException"

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
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            await ws.close()
        input_stream.end_stream.assert_awaited()

    @pytest.mark.asyncio
    async def test_abrupt_close_without_stop_message(self, monkeypatch):
        """Client closes WS without sending {"type":"stop"} — cleanup must run."""
        _, input_stream = self._install_stubs(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            await ws.close()  # no stop message
        input_stream.end_stream.assert_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ending", [RuntimeError("signature mismatch"), None], ids=["raises", "returns"]
    )
    async def test_a_result_stream_that_ends_early_ends_the_session(
        self, monkeypatch, caplog, ending
    ):
        """Transcribe ending its result stream mid-dictation must reach the client.

        Without the frame and the close, the read loop keeps forwarding audio to a
        dead stream and the dashboard keeps showing it is listening. The
        failure itself is logged at teardown, not propagated.
        """
        from amazon_transcribe.handlers import TranscriptResultStreamHandler

        _, input_stream = self._install_stubs(monkeypatch)

        async def _ended(self) -> None:
            if ending is not None:
                raise ending

        monkeypatch.setattr(TranscriptResultStreamHandler, "handle_events", _ended)
        calls: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: calls.append(kw)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.sel", lambda: fake_sel)
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.stt_stream"):
            async with TestClient(TestServer(_make_app())) as client:
                ws = await client.ws_connect("/api/ws/stt")
                assert (await _bounded(ws.receive_json()))["type"] == "ready"
                assert (await _bounded(ws.receive_json())) == {
                    "type": "error",
                    "message": "transcription stream ended",
                    "code": "stt_session_failed",
                }
                assert (await _bounded(ws.receive())).type == WSMsgType.CLOSE
                await ws.close()
            await _wait_for_operation(calls, "stt_stream_end")
        end = next(c for c in calls if c["operation"] == "stt_stream_end")
        assert end["outcome"] == "error"
        input_stream.end_stream.assert_awaited()
        assert "ended the stream before the client stopped" in caplog.text
        assert ("Transcribe handler task failed" in caplog.text) is (ending is not None)

    @pytest.mark.asyncio
    async def test_a_cap_landing_during_an_early_end_sends_no_second_frame(self, monkeypatch):
        """The first cause to claim the session is the only one the client hears.

        The result stream ends first and its frame is held in flight until the
        duration cap has run. A cap that ignored the claim would see the socket
        still open, send a contradictory frame and relabel the audit.
        """
        from amazon_transcribe.handlers import TranscriptResultStreamHandler

        from kiro_crew.dashboard import stt_stream

        self._install_stubs(monkeypatch)

        async def _ended(self) -> None:
            return None

        monkeypatch.setattr(TranscriptResultStreamHandler, "handle_events", _ended)
        monkeypatch.setattr(stt_stream, "_MAX_STREAM_DURATION_SECS", 0.05)
        real_send_error = stt_stream._send_error
        sent_codes: list[str] = []
        cap_ran: list[bool] = []

        async def _held_send_error(ws, message, code):
            sent_codes.append(code)
            if len(sent_codes) == 1:
                cap = next(
                    t
                    for t in asyncio.all_tasks()
                    if getattr(t.get_coro(), "__name__", "") == "_enforce_deadline"
                )
                await asyncio.wait({cap}, timeout=default_timeout())
                cap_ran.append(cap.done())
            await real_send_error(ws, message, code)

        monkeypatch.setattr(stt_stream, "_send_error", _held_send_error)
        calls: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: calls.append(kw)
        monkeypatch.setattr(stt_stream, "sel", lambda: fake_sel)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _bounded(ws.receive_json()))["type"] == "ready"
            assert (await _bounded(ws.receive_json()))["code"] == "stt_session_failed"
            assert (await _bounded(ws.receive())).type == WSMsgType.CLOSE
            await ws.close()
        await _wait_for_operation(calls, "stt_stream_end")
        assert cap_ran == [True]
        assert sent_codes == ["stt_session_failed"]
        end = next(c for c in calls if c["operation"] == "stt_stream_end")
        assert end["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_a_clean_stop_is_not_reported_as_an_early_end(self, monkeypatch):
        """The result stream also ends after a stop; that end is the normal one."""
        self._install_stubs(monkeypatch)
        calls: list[dict] = []
        fake_sel = MagicMock()
        fake_sel.log_api_access = lambda **kw: calls.append(kw)
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream.sel", lambda: fake_sel)

        async def _frames_until_close(ws) -> list[object]:
            return [msg.data async for msg in ws]

        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _bounded(ws.receive_json()))["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            await ws.send_str('{"type":"stop"}')
            frames = await _bounded(_frames_until_close(ws))
        await _wait_for_operation(calls, "stt_stream_end")
        assert frames == []
        end = next(c for c in calls if c["operation"] == "stt_stream_end")
        assert end["outcome"] == "ok"

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
            assert (await ws.receive_json())["type"] == "ready"
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
            assert (await ws.receive_json())["type"] == "ready"
            await ws.receive_json()
            await ws.close()
        await _await_end_audit(outcomes)
        assert outcomes == ["timeout"], outcomes

    @pytest.mark.asyncio
    async def test_oversized_text_frame_closes_connection(self, monkeypatch):
        """Text frame larger than _MAX_TEXT_FRAME_BYTES terminates the stream."""
        _, input_stream = self._install_stubs(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_str("x" * 300)  # >_MAX_TEXT_FRAME_BYTES (256)
            await ws.close()
        input_stream.end_stream.assert_awaited()

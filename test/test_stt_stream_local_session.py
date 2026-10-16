"""The default ``local`` provider's WebSocket path (``_run_local_session``).

The drain under the duration cap, stop and backlog handling, the model download and its
preparing frames, download-progress republication, and one end audit on every exit. The
``apple`` and ``transcribe`` providers' paths live in ``test_stt_stream.py``; the shared
socket helpers in ``stt_stream_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from stt_stream_helpers import (
    _arm_cap,
    _await_end_audit,
    _cfg,
    _make_app,
    _recv_json,
    _wait_for_operation,
)

from kiro_crew import stt
from kiro_crew.testing.wait import await_until


async def _recv(ws):
    """One frame of any type, under the same bound as ``_recv_json``."""
    return await await_until(asyncio.ensure_future(ws.receive()), what="the next frame")


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
        pending_load=False,
        prepare_events=(),
        feed_events=(),
        final_text="",
        final_event=None,
        prepare_gate=None,
    ):
        self.kwargs: dict = {}
        self.fed: list[bytes] = []
        self.prepared = False
        #: Which thread each synchronous model probe was called on. Both resolve the
        #: model, and resolving the CUSTOM selection reads and validates
        #: ``config.json``, so both must be offloaded -- recorded rather than counted
        #: because the defect is WHERE the call ran, not whether it ran.
        self.call_threads: dict[str, int] = {}
        #: Counted, not just flagged: the refusal path starts a detached
        #: transfer, so a test needs to see that prepare was entered even
        #: though the socket closed before it returned.
        self.prepare_calls = 0
        self.finished = False
        self.cancelled = False
        self._pending = pending
        #: Whether ``prepare()`` will block loading the model, so the transport
        #: announces the load with a ``preparing`` status frame first. Defaults off
        #: so the ordinary "model already resident" path stays a plain ``ready``.
        self._pending_load = pending_load
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
        self.call_threads["pending_download"] = threading.get_ident()
        return self._pending

    def pending_load(self) -> bool:
        self.call_threads["pending_load"] = threading.get_ident()
        return self._pending_load

    async def prepare(self) -> list:
        self.prepare_calls += 1
        if self._prepare_gate is not None:
            # flake-ok: the fake prepare parks here until the test opens its gate; moved verbatim
            await self._prepare_gate.wait()
        self.prepared = True
        return list(self._prepare_events)

    async def feed(self, raw_int16: bytes, *, allow_partial=True) -> list:
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

    def stop_partials(self) -> None:
        pass


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
    async def test_ready_advertises_the_connection_cap(self, monkeypatch):
        """A client that outlives one connection needs the cap to plan its rotation.

        The meetings app holds a socket for an hour-long meeting and rotates before
        the cap; without the figure it can only guess. Milliseconds, like the
        sibling `final_timeout_ms`, and derived from the constant rather than
        restated, so a changed cap cannot leave a stale advertisement.
        """
        self._install(monkeypatch, _FakeLocalSession())
        # flake-ok: a real 7 s cap, far above the ready-then-stop exchange it bounds; moved verbatim
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 7)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            ready = await _recv_json(ws)
            assert ready["type"] == "ready"
            assert ready["max_duration_ms"] == 7000
            await ws.send_str('{"type":"stop"}')
            await ws.close()

    def _arm_cap(self, monkeypatch) -> asyncio.Event:
        """Make the duration cap fire on a signal instead of a clock.

        The transport's deadline sleeps `_MAX_STREAM_DURATION_SECS`. Pinning that
        to a sentinel and shimming `asyncio.sleep` (the pattern
        `test_stop_budget_covers_the_active_partial_and_the_final` already uses for
        the drain budget) lets a test fire the cap exactly when the session is in
        the state under test, instead of racing a real timer against loopback I/O:
        a cap that fires before the client's frame has been read, queued and fed
        has nothing pending and legitimately sends no final.
        """
        return _arm_cap(monkeypatch)

    @staticmethod
    def _fed_session(fed: asyncio.Event, **kwargs) -> _FakeLocalSession:
        """A fake session that signals once audio has been accepted."""

        class Fed(_FakeLocalSession):
            async def feed(self, raw_int16, *, allow_partial=True):
                events = await super().feed(raw_int16, allow_partial=allow_partial)
                fed.set()
                return events

        return Fed(**kwargs)

    @pytest.mark.asyncio
    async def test_the_cap_delivers_the_pending_utterance_before_its_error_frame(self, monkeypatch):
        """The cap is a server-initiated stop, not a kill.

        Audio the server has accepted is speech the user already said; discarding
        it at the cap lost up to the utterance backstop (two minutes) per cap in a
        continuous meeting. The trailing final goes out first, then the cap's own
        frame, then the close — the same order a client `stop` produces, plus the
        frame that says why the server ended it.
        """
        outcomes = self._record_end_audits(monkeypatch)
        fire = self._arm_cap(monkeypatch)
        fed = asyncio.Event()
        session = self._install(
            monkeypatch, self._fed_session(fed, final_text="what was being said")
        )
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _recv_json(ws))["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            # The cap fires with that audio accepted and no final yet.
            await await_until(fed, what="the audio being fed")
            fire.set()
            assert (await _recv_json(ws)) == {"type": "final", "text": "what was being said"}
            assert (await _recv_json(ws)) == {
                "type": "error",
                "message": "max stream duration exceeded",
                "code": "stt_max_duration_exceeded",
            }
            closing = await _recv(ws)
            assert closing.type in (
                web.WSMsgType.CLOSE,
                web.WSMsgType.CLOSING,
                web.WSMsgType.CLOSED,
            )
        assert session.finished is True
        assert session.cancelled is False
        await _await_end_audit(outcomes)
        assert outcomes == ["timeout"], outcomes

    @pytest.mark.asyncio
    async def test_a_stop_that_races_the_cap_ends_the_session_once(self, monkeypatch):
        """Both the client and the cap may end the session in the same window.

        Whoever claims first owns the outcome; the other must not add a second
        final or a contradictory frame. The cap is fired once the audio is in,
        with the client's `stop` already sent and in flight: which one the server
        reads first is the loop's choice, and both answers are accepted.
        """
        outcomes = self._record_end_audits(monkeypatch)
        fire = self._arm_cap(monkeypatch)
        fed = asyncio.Event()
        session = self._install(monkeypatch, self._fed_session(fed, final_text="only once"))
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _recv_json(ws))["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            await ws.send_str('{"type":"stop"}')
            await await_until(fed, what="the audio being fed")
            fire.set()
            frames = []
            while True:
                msg = await _recv(ws)
                if msg.type != web.WSMsgType.TEXT:
                    break
                frames.append(json.loads(msg.data))
        assert [f for f in frames if f["type"] == "final"] == [
            {"type": "final", "text": "only once"}
        ]
        assert len([f for f in frames if f["type"] == "error"]) <= 1
        assert session.finished is True
        await _await_end_audit(outcomes)
        assert outcomes in (["ok"], ["timeout"]), outcomes

    @pytest.mark.asyncio
    async def test_a_raise_during_a_cap_drain_reports_the_failure_not_the_cap(self, monkeypatch):
        """One frame, and the one that tells the truth.

        The cap now precedes a drain, so a recogniser that raises while finishing
        reaches the generic `except` with the cap already claimed. Two frames
        (`transcription failed`, then the cap's) contradicted each other; the cap
        frame alone would promise that nothing was lost when the final was. So the
        failure's own code goes out in the cap frame's place, once, and the audit
        still records the cap that ended the session.
        """
        outcomes = self._record_end_audits(monkeypatch)
        fire = self._arm_cap(monkeypatch)
        fed = asyncio.Event()

        class Exploding(_FakeLocalSession):
            async def feed(self, raw_int16, *, allow_partial=True):
                events = await super().feed(raw_int16, allow_partial=allow_partial)
                fed.set()
                return events

            async def finish(self):
                raise RuntimeError("decode blew up while finishing")

        self._install(monkeypatch, Exploding())
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await _recv_json(ws))["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            await await_until(fed, what="the audio being fed")
            fire.set()
            frames = []
            while True:
                msg = await _recv(ws)
                if msg.type != web.WSMsgType.TEXT:
                    break
                frames.append(json.loads(msg.data))
        assert [f["type"] for f in frames] == ["error"], frames
        assert frames[0]["code"] == "stt_session_failed", frames
        await _await_end_audit(outcomes)
        assert outcomes == ["timeout"], outcomes

    @pytest.mark.asyncio
    async def test_pcm_and_stop_are_read_while_a_partial_is_still_decoding(self, monkeypatch):
        from kiro_crew.dashboard import stt_stream

        decoding = asyncio.Event()
        stopped = asyncio.Event()
        allow_partials: list[bool] = []
        frames = [bytes([n, 1]) * 16 for n in range(4)]

        class SlowSession(_FakeLocalSession):
            async def feed(self, raw_int16, *, allow_partial=True):
                allow_partials.append(allow_partial)
                if not self.fed:
                    decoding.set()
                    await await_until(stopped, what="the stop frame being queued")
                return await super().feed(raw_int16, allow_partial=allow_partial)

        class BufferedWS(_RecordingWS):
            async def __aiter__(self):
                yield SimpleNamespace(type=web.WSMsgType.BINARY, data=frames[0])
                await await_until(decoding, what="the decode starting")
                for frame in frames[1:]:
                    yield SimpleNamespace(type=web.WSMsgType.BINARY, data=frame)
                stopped.set()
                yield SimpleNamespace(type=web.WSMsgType.TEXT, data='{"type":"stop"}')
                raise AssertionError("stop must end the input reader")

        session = self._install(monkeypatch, SlowSession(final_text="all four frames"))
        outcomes = self._record_end_audits(monkeypatch)
        ws = BufferedWS()
        await await_until(
            asyncio.ensure_future(
                stt_stream._run_local_session(ws, _cfg(provider="local"), MagicMock(), "test")
            ),
            what="the local session ending",
        )
        assert session.fed == frames, "stop discarded buffered audio"
        assert allow_partials == [True, False, False, False]
        assert ws.sent[-1] == {"type": "final", "text": "all four frames"}
        assert outcomes == ["ok"]

    @pytest.mark.asyncio
    async def test_an_overflowing_pcm_backlog_reports_one_error(self, monkeypatch):
        from kiro_crew.dashboard import stt_stream

        monkeypatch.setattr(stt_stream, "_MAX_LOCAL_BUFFER_BYTES", 2)
        session = self._install(monkeypatch, _FakeLocalSession())
        outcomes = self._record_end_audits(monkeypatch)

        class OverflowWS(_RecordingWS):
            async def __aiter__(self):
                yield SimpleNamespace(type=web.WSMsgType.BINARY, data=b"\x00\x01" * 2)

        ws = OverflowWS()
        await await_until(
            asyncio.ensure_future(
                stt_stream._run_local_session(ws, _cfg(provider="local"), MagicMock(), "test")
            ),
            what="the local session ending",
        )
        assert [frame["type"] for frame in ws.sent] == ["ready", "error"]
        assert ws.sent[-1]["code"] == stt_stream._CODE_SESSION_FAILED
        assert session.fed == [] and session.cancelled
        assert outcomes == ["error"]

    @pytest.mark.asyncio
    async def test_cancelling_the_transport_never_starts_a_final_decode(self, monkeypatch):
        from kiro_crew.dashboard import stt_stream

        decoding = asyncio.Event()
        release = asyncio.Event()

        class SlowSession(_FakeLocalSession):
            async def feed(self, raw_int16, *, allow_partial=True):
                self._pending_audio = True
                decoding.set()
                await await_until(release, what="the test releasing it")
                return []

        class OpenWS(_RecordingWS):
            async def __aiter__(self):
                yield SimpleNamespace(type=web.WSMsgType.BINARY, data=b"\x00\x01")
                await await_until(release, what="the test releasing it")

        session = self._install(monkeypatch, SlowSession())
        outcomes = self._record_end_audits(monkeypatch)
        ws = OpenWS()
        task = asyncio.create_task(
            stt_stream._run_local_session(ws, _cfg(provider="local"), MagicMock(), "test")
        )
        try:
            await await_until(decoding, what="the decode starting")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await await_until(task, what="the cancelled session task ending")
            assert session.cancelled and not session.finished
            assert ws.closed and outcomes == ["error"]
        finally:
            release.set()
            task.cancel()
            # flake-ok: joins the task this finally block cancelled; moved verbatim, a bounded join is tracked for a de-flake pass
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("buffered_seconds", [40, 60])
    async def test_readiness_burst_fits_the_backend_inbox(self, monkeypatch, buffered_seconds):
        from kiro_crew.dashboard import stt_stream

        session = self._install(monkeypatch, _FakeLocalSession(final_text="complete capture"))
        outcomes = self._record_end_audits(monkeypatch)
        # The worklet flushes 100 ms frames in one turn when model readiness
        # arrives; the receiver must admit that burst before inference catches up.
        frame = b"\x00\x01" * (stt_stream.STREAM_SAMPLE_RATE_HZ // 10)
        frames = [frame] * (buffered_seconds * 10)

        class BurstWS(_RecordingWS):
            async def __aiter__(self):
                for audio in frames:
                    yield SimpleNamespace(type=web.WSMsgType.BINARY, data=audio)
                yield SimpleNamespace(type=web.WSMsgType.TEXT, data='{"type":"stop"}')

        ws = BurstWS()
        await await_until(
            asyncio.ensure_future(
                stt_stream._run_local_session(ws, _cfg(provider="local"), MagicMock(), "test")
            ),
            what="the local session ending",
        )
        assert session.fed == frames
        assert session.finished
        assert [message["type"] for message in ws.sent] == ["ready", "final"]
        assert ws.sent[1] == {"type": "final", "text": "complete capture"}
        assert outcomes == ["ok"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phase", ["partial", "final"])
    async def test_stop_budget_covers_the_active_partial_and_the_final(self, monkeypatch, phase):
        from kiro_crew.dashboard import stt_stream

        cfg = _cfg(provider="local", timeout_secs=22)
        budget_started = asyncio.Event()
        budget_expired = asyncio.Event()
        decoding = asyncio.Event()
        interrupted = asyncio.Event()
        release = asyncio.Event()
        original_sleep = asyncio.sleep

        async def controlled_sleep(delay):
            if delay == cfg.stt.timeout_secs:
                budget_started.set()
                await await_until(budget_expired, what="the test expiring the budget")
            else:
                await original_sleep(delay)

        # flake-ok: patches the stdlib asyncio.sleep for this test only, and every delay but the stop budget's still reaches the real sleep; moved verbatim, a module-local shim is tracked for a de-flake pass
        monkeypatch.setattr(stt_stream.asyncio, "sleep", controlled_sleep)

        async def blocked_decode():
            decoding.set()
            try:
                await await_until(release, what="the test releasing it")
            finally:
                interrupted.set()

        class SlowSession(_FakeLocalSession):
            async def feed(self, raw_int16, *, allow_partial=True):
                events = await super().feed(raw_int16, allow_partial=allow_partial)
                if phase == "partial":
                    await blocked_decode()
                return events

            async def finish(self):
                self.finished = True
                await blocked_decode()
                return stt.SttEvent(stt.KIND_FINAL, text="too late")

        class StopWS(_RecordingWS):
            async def __aiter__(self):
                yield SimpleNamespace(type=web.WSMsgType.BINARY, data=b"\x00\x01")
                if phase == "partial":
                    await await_until(decoding, what="the decode starting")
                yield SimpleNamespace(type=web.WSMsgType.TEXT, data='{"type":"stop"}')

        session = self._install(monkeypatch, SlowSession())
        outcomes = self._record_end_audits(monkeypatch)
        ws = StopWS()
        task = asyncio.create_task(stt_stream._run_local_session(ws, cfg, MagicMock(), "test"))
        try:
            await await_until(budget_started, what="the stop budget starting")
            await await_until(decoding, what="the decode starting")
            budget_expired.set()
            await await_until(task, what="the session task ending")
            assert interrupted.is_set() and session.cancelled
            assert session.finished is (phase == "final")
            assert ws.sent[0] == {
                "type": "ready",
                "final_timeout_ms": 42_000,
                "max_duration_ms": 300_000,
            }
            assert [message["type"] for message in ws.sent] == ["ready", "error"]
            assert ws.sent[1]["code"] == stt.CODE_DECODE_FAILED
            assert ws.closed and outcomes == ["timeout"]
        finally:
            release.set()
            budget_expired.set()
            task.cancel()
            # flake-ok: joins the task this finally block cancelled; moved verbatim, a bounded join is tracked for a de-flake pass
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_an_empty_final_retracts_the_partial_without_semantic_endpointing(
        self, monkeypatch
    ):
        from kiro_crew.dashboard import stt_stream

        self._install(
            monkeypatch,
            _FakeLocalSession(
                feed_events=[
                    [stt.SttEvent(stt.KIND_PARTIAL, text="caption hallucination")],
                    [stt.SttEvent(stt.KIND_FINAL, text="")],
                ]
            ),
        )
        endpoint = SimpleNamespace(
            note_partial=MagicMock(),
            note_final=MagicMock(),
            note_audio_final=MagicMock(),
            aclose=AsyncMock(),
        )
        monkeypatch.setattr(stt_stream, "_build_endpointer", lambda *_args, **_kwargs: endpoint)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_bytes(b"\x00\x01")
            assert (await _recv_json(ws))["type"] == "partial"
            await ws.send_bytes(b"\x00\x01")
            assert await _recv_json(ws) == {
                "type": "final",
                "text": "",
            }
            await ws.send_json({"type": "stop"})
            await ws.close()
        endpoint.note_final.assert_not_called()
        endpoint.note_audio_final.assert_called_once_with("")

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
            assert (await ws.receive_json())["type"] == "ready"
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
            assert (await ws.receive_json())["type"] == "ready"
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

        A dropped empty final and a failed decode can look identical from here:
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
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_bytes(b"\x00\x01" * 16)
            await ws.send_str('{"type":"stop"}')
            assert (await ws.receive_json()) == {
                "type": "error",
                "message": stt.DECODE_FAILED_ADVISORY,
                "code": stt.CODE_DECODE_FAILED,
            }
            await ws.close()
        assert session.finished is True
        await _await_end_audit(outcomes)
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
            assert (await ws.receive_json())["type"] == "ready"
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
        # flake-ok: pre-existing zero-delay yield barrier, moved verbatim; tracked for a de-flake pass
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
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_str('{"type":"stop"}')
            await ws.close()

    @pytest.mark.asyncio
    async def test_a_preparing_status_frame_precedes_ready_when_the_model_must_load(
        self, monkeypatch
    ):
        """A present-but-not-resident model gets a ``preparing`` frame before ``ready``.

        Loading the model into memory is otherwise silent on this path:
        ``pending_download`` is None so no download ``status`` goes out, and nothing
        else does until ``ready`` after the load finishes. On a slow host that load
        outruns the client's pre-``ready`` buffer and the mic releases with the socket
        having sent nothing either way — the hung-mic report. The ``preparing`` frame
        is the signal that the load is under way, so it must arrive BEFORE ``ready``.
        """
        self._install(monkeypatch, _FakeLocalSession(pending=None, pending_load=True))
        from kiro_crew.dashboard import stt_stream

        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            first = await ws.receive_json()
            assert first["type"] == "status"
            assert first["stage"] == stt.STAGE_PREPARING
            # The deadline belongs to the side that owns the wait, so the frame
            # states it rather than leaving the client to pick one. A client
            # number shorter than this abandons a load still running here and
            # discards audio the next frame would have transcribed.
            assert (
                first["prepare_timeout_ms"]
                == (stt_stream._MAX_MODEL_PREPARE_SECS + stt_stream._LOCAL_FINAL_WIRE_GRACE_SECS)
                * 1000
            )
            # Strictly longer than this server's own ceiling: the timeout must be
            # reached HERE first, where the reason is known and goes out as a
            # coded error, instead of at a client that can only guess.
            assert first["prepare_timeout_ms"] > stt_stream._MAX_MODEL_PREPARE_SECS * 1000
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_str('{"type":"stop"}')
            await ws.close()

    @pytest.mark.asyncio
    async def test_neither_model_probe_runs_on_the_gateway_event_loop(self, monkeypatch):
        """Both synchronous probes are offloaded, not just the one that was noticed.

        ``pending_download`` and ``pending_load`` each call ``models.resolve``, and
        resolving the ``custom`` selection reads and jsonschema-validates
        ``config.json``. On the single gateway event loop that is the stall this
        feature would otherwise have introduced, and ``pending_download`` returning
        None is exactly the path that goes on to ask ``pending_load`` -- so offloading
        one and leaving the other inline moves the stall rather than removing it.
        Asserted on the thread each probe ran on, because an inline call is
        indistinguishable from an offloaded one by call count.
        """
        loop_thread = threading.get_ident()
        session = self._install(monkeypatch, _FakeLocalSession(pending=None, pending_load=True))
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json())["stage"] == stt.STAGE_PREPARING
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_str('{"type":"stop"}')
            await ws.close()
        assert session.call_threads.keys() == {"pending_download", "pending_load"}
        assert session.call_threads["pending_download"] != loop_thread
        assert session.call_threads["pending_load"] != loop_thread

    @pytest.mark.asyncio
    async def test_no_preparing_frame_when_the_model_is_already_resident(self, monkeypatch):
        """A resident model loads nothing, so it goes straight to ``ready``.

        ``pending_load`` is False when the shared engine already holds this model, so
        the announce is skipped: sending ``preparing`` there would flash a load
        indicator for a session that never loads.
        """
        self._install(monkeypatch, _FakeLocalSession(pending=None, pending_load=False))
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json())["type"] == "ready"
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
            # flake-ok: the stubbed transfer parks here until the stub store's status read opens its gate; moved verbatim
            await gate.wait()
            return []

        task = asyncio.create_task(_prepare())
        # Under the lost-run bound, because the regression this guards against does
        # not fail, it STOPS: a loop that publishes once and then waits leaves
        # prepare() gated forever, and an unbounded await would report that as a
        # hung suite rather than as this assertion. Only ever elapses on that
        # regression.
        relay = asyncio.ensure_future(stt_stream._relay_download_progress(task, _send))
        try:
            relayed = await await_until(relay, what="the relay returning")
        except AssertionError as stalled:
            raise AssertionError(
                f"progress stopped being republished after {len(sent)} frame(s)"
            ) from stalled
        assert relayed == []

        assert len(sent) >= 2, sent
        counts = [frame["downloaded_bytes"] for frame in sent]
        # Sorted AND distinct, i.e. strictly growing: a repeated count would leave
        # the panel's progress bar frozen while the transfer ran.
        assert counts == sorted(counts) and len(set(counts)) == len(counts), counts
        assert all(frame["stage"] == "downloading" for frame in sent), sent
        assert all(frame["total_bytes"] == model.size_bytes for frame in sent), sent
        # Every announcing frame carries the deadline, not just the first one: a
        # client that joins mid-transfer hears one of these as its FIRST word on
        # the subject, and a frame without the figure leaves it holding a budget
        # this side never agreed to.
        expected_ms = (
            stt_stream._MAX_MODEL_PREPARE_SECS + stt_stream._LOCAL_FINAL_WIRE_GRACE_SECS
        ) * 1000
        assert all(frame["prepare_timeout_ms"] == expected_ms for frame in sent), sent

    @pytest.mark.asyncio
    async def test_a_load_that_starts_after_the_pre_check_is_still_announced(self, monkeypatch):
        """An eviction between the pre-check and the load must not silence the wait.

        ``pending_load`` is read without a lock before ``prepare`` starts, so a model
        resident at that instant can be gone by the time the load looks for it. With
        ``stt.idle_evict_secs`` at 0 -- legal, and documented in ``stt.limits`` as the
        right setting on a memory-constrained host -- a model becomes evictable the
        moment a decode finishes, and ``maybe_evict`` is called from the decode
        completion paths, not only from the periodic sweep. So the pre-check's answer
        can flip for an ordinary reason rather than an exotic one.

        Unannounced, the client holds its released utterance on the SHORT budget and
        discards it at sixty seconds while this side is still building the context.
        The announce is therefore owed late, and this pins that it arrives.
        """
        from kiro_crew.dashboard import stt_stream

        released = asyncio.Event()

        class _Store:
            @property
            def status(self):
                # Not a transfer: this is the load half of the wait, the branch
                # that carries no byte count. Counting the spins HERE, not in
                # `pending_load`, so releasing `prepare` does not depend on the
                # announce being asked for -- otherwise removing the announce would
                # hang this test instead of failing its assertion.
                reads["store"] += 1
                if reads["store"] >= 3:
                    released.set()
                return {"step": "loading", "downloaded_bytes": 0, "total_bytes": 0}

        monkeypatch.setattr(stt, "model_store", lambda: _Store())
        monkeypatch.setattr(stt_stream, "_MODEL_PROGRESS_INTERVAL_SECS", 0)

        sent: list[dict] = []

        async def _send(frame):
            sent.append(frame)
            return True

        # False at the pre-check, true afterwards: the eviction the pre-check could
        # not see.
        reads = {"store": 0, "load": 0}

        def _pending_load():
            reads["load"] += 1
            return reads["load"] >= 2

        async def _prepare():
            # flake-ok: the stubbed transfer parks here until the stub store's status read opens its gate; moved verbatim
            await released.wait()
            return []

        assert _pending_load() is False, "pre-check must see the model resident"
        task = asyncio.create_task(_prepare())
        relayed = await await_until(
            asyncio.ensure_future(
                stt_stream._relay_download_progress(task, _send, _pending_load, False)
            ),
            what="the relay returning",
        )
        assert relayed == []

        preparing = [f for f in sent if f.get("stage") == stt.STAGE_PREPARING]
        assert len(preparing) == 1, sent
        assert (
            preparing[0]["prepare_timeout_ms"]
            == (stt_stream._MAX_MODEL_PREPARE_SECS + stt_stream._LOCAL_FINAL_WIRE_GRACE_SECS) * 1000
        )

    @pytest.mark.asyncio
    async def test_a_wait_with_nothing_loading_announces_nothing(self, monkeypatch):
        """A socket with no load under way keeps the short budget.

        The announce is what switches the client from the sixty-second budget to the
        long one, so announcing whenever this branch is reached would hand a backend
        that is doing nothing -- and a socket that is simply dead -- the whole prepare
        budget. The gate is ``pending_load`` being true, not merely arriving here.
        """
        from kiro_crew.dashboard import stt_stream

        released = asyncio.Event()
        reads = {"store": 0}

        class _Store:
            @property
            def status(self):
                # The loop's own read, so the spin count is independent of whether
                # the announce is asked for at all.
                reads["store"] += 1
                if reads["store"] >= 5:
                    released.set()
                return {"step": "loading", "downloaded_bytes": 0, "total_bytes": 0}

        monkeypatch.setattr(stt, "model_store", lambda: _Store())
        monkeypatch.setattr(stt_stream, "_MODEL_PROGRESS_INTERVAL_SECS", 0)

        sent: list[dict] = []

        async def _send(frame):
            sent.append(frame)
            return True

        async def _prepare():
            # flake-ok: the stubbed transfer parks here until the stub store's status read opens its gate; moved verbatim
            await released.wait()
            return []

        task = asyncio.create_task(_prepare())
        relayed = await await_until(
            asyncio.ensure_future(
                stt_stream._relay_download_progress(task, _send, lambda: False, False)
            ),
            what="the relay returning",
        )
        assert relayed == []
        # Several spins really happened, so "nothing was sent" is an observation
        # rather than a race this test won.
        assert reads["store"] >= 5, reads
        assert sent == [], sent

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
            # flake-ok: the stubbed transfer parks here until the stub store's status read opens its gate; moved verbatim
            await gate.wait()
            finished.append(True)
            return []

        task = asyncio.create_task(_prepare())
        # flake-ok: the relay returns once the stub store's status read lets the transfer finish; moved verbatim, a bounded await is tracked for a de-flake pass
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
            assert (await ws.receive_json())["type"] == "ready"
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
            # flake-ok: pre-existing zero-delay yield barrier, moved verbatim; tracked for a de-flake pass
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

        # flake-ok: the session ends once the stubbed socket fails a send and the transfer finishes; moved verbatim, a bounded await is tracked for a de-flake pass
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
        # flake-ok: pre-existing zero-delay yield barrier, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        assert session.prepare_calls >= 1
        gone.set()
        # flake-ok: pre-existing zero-delay yield barrier, moved verbatim; tracked for a de-flake pass
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
    @pytest.mark.parametrize("module", ["kiro_crew.stt.engine", "kiro_crew.stt.vad"])
    async def test_a_deferred_numpy_import_failure_still_emits_the_end_audit(
        self, monkeypatch, module
    ):
        """The numpy-backed helpers are imported inside the session, not at module scope.

        That keeps numpy off the mcp-dashboard import path, but it also means a
        numpy that is installed and fails to load raises here, after
        ``stt_stream_start`` was logged. The import has to sit inside the audited
        setup block, or the trail shows a voice session that never ended.
        """
        from kiro_crew.dashboard import stt_stream

        outcomes = self._record_end_audits(monkeypatch)
        # Scoped so conftest teardowns that import the stt modules still find them.
        with monkeypatch.context() as m:
            m.setitem(sys.modules, module, None)
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
            async def feed(self, raw_int16, *, allow_partial=True):
                raise RuntimeError("decode blew up")

        self._install(monkeypatch, _Exploding())
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json())["type"] == "ready"
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
        # flake-ok: pre-existing real 50 ms duration cap, moved verbatim; a signal-driven cap is tracked for a de-flake pass
        monkeypatch.setattr("kiro_crew.dashboard.stt_stream._MAX_STREAM_DURATION_SECS", 0.05)
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/stt")
            assert (await ws.receive_json())["type"] == "ready"
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
            # flake-ok: the stubbed transfer parks here until the stub store's status read opens its gate; moved verbatim
            await gate.wait()
            return []

        task = asyncio.create_task(_prepare())
        # flake-ok: the relay returns once the stub store's status read lets the transfer finish; moved verbatim, a bounded await is tracked for a de-flake pass
        assert await stt_stream._relay_download_progress(task, _send) == []
        assert sent == []

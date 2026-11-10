"""Connected computers: the session MAC envelope and authenticated liveness.

* ``SessionFrames`` -- a relay cannot change, replay, reorder or reflect a frame,
  and the first bad frame ends the session in that direction for good.
* ``Liveness`` -- heartbeats out, a deadline in, on an injected clock and sleep,
  so no test waits for time to pass.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import pytest

from kiro_crew.connected_computers import authenticated, protocol
from kiro_crew.connected_computers.authenticated import Liveness, SessionFrames
from kiro_crew.testing.wait import (
    DEFAULT_INTERVAL_SECS,
    DEFAULT_PROGRESS_WINDOW_SECS,
    FALLBACK_TIMEOUT_SECS,
    finished,
)

KEY = "d" * 64
BOUND = ("gw-test-0001", "laptop", "b" * 64, "c" * 64)
ANY_OP = frozenset({"ping", "ready", "stop", "admitted"})


def _pair() -> tuple[SessionFrames, SessionFrames]:
    return SessionFrames(KEY, BOUND, gateway=True), SessionFrames(KEY, BOUND, gateway=False)


def _envelope(text: str) -> dict[str, Any]:
    return json.loads(text)


class TestSessionFrames:
    @pytest.mark.parametrize(
        "key,bound",
        [
            ("short", BOUND),
            ("D" * 64, BOUND),
            (KEY, (BOUND[0], BOUND[1], "x", BOUND[3])),
            (KEY, (BOUND[0], BOUND[1], BOUND[2], "")),
        ],
    )
    def test_an_invalid_key_or_nonce_is_refused(self, key, bound):
        with pytest.raises(protocol.ProtocolError):
            SessionFrames(key, bound, gateway=True)

    def test_frames_round_trip_in_order_both_ways(self):
        gateway, client = _pair()
        for _ in range(3):
            assert client.decode(gateway.encode({"op": "ping"}), allowed=ANY_OP) == {"op": "ping"}
        stop = {"op": "stop", "reason": "bye"}
        assert gateway.decode(client.encode(stop), allowed=ANY_OP) == stop
        assert _envelope(gateway.encode({"op": "ping"}))["seq"] == 3

    def test_a_frame_is_never_accepted_back_by_its_sender(self):
        gateway, _client = _pair()
        with pytest.raises(protocol.ProtocolError):
            gateway.decode(gateway.encode({"op": "ping"}), allowed=ANY_OP)

    def test_another_socket_cannot_read_this_one(self):
        gateway, _client = _pair()
        other = SessionFrames(KEY, (*BOUND[:3], "e" * 64), gateway=False)
        with pytest.raises(protocol.ProtocolError):
            other.decode(gateway.encode({"op": "ping"}), allowed=ANY_OP)

    def test_a_replayed_frame_is_refused(self):
        gateway, client = _pair()
        text = gateway.encode({"op": "ping"})
        client.decode(text, allowed=ANY_OP)
        with pytest.raises(protocol.ProtocolError):
            client.decode(text, allowed=ANY_OP)

    def test_a_skipped_frame_is_refused(self):
        gateway, client = _pair()
        gateway.encode({"op": "ping"})
        with pytest.raises(protocol.ProtocolError):
            client.decode(gateway.encode({"op": "ping"}), allowed=ANY_OP)

    def test_a_changed_frame_fails_its_mac(self):
        gateway, client = _pair()
        env = _envelope(gateway.encode({"op": "stop", "reason": "revoked"}))
        env["frame"]["reason"] = "nothing to see"
        with pytest.raises(protocol.ProtocolError):
            client.decode(json.dumps(env), allowed=ANY_OP)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda env: env.pop("mac"),
            lambda env: env.update(extra=1),
            lambda env: env.update(seq=True),
            lambda env: env.update(seq="0"),
            lambda env: env.update(mac="zz"),
            lambda env: env.update(mac="0" * 64),
        ],
        ids=["no-mac", "extra-field", "bool-seq", "str-seq", "short-mac", "wrong-mac"],
    )
    def test_a_malformed_envelope_is_refused(self, mutate):
        gateway, client = _pair()
        env = _envelope(gateway.encode({"op": "ping"}))
        mutate(env)
        with pytest.raises(protocol.ProtocolError):
            client.decode(json.dumps(env), allowed=ANY_OP)

    @pytest.mark.parametrize(
        "text",
        [None, b"{}", "not json", "[1, 2]", "x" * (protocol.MAX_FRAME_BYTES + 1)],
        ids=["none", "bytes", "not-json", "list", "oversize"],
    )
    def test_non_frames_are_refused(self, text):
        _gateway, client = _pair()
        with pytest.raises(protocol.ProtocolError):
            client.decode(text, allowed=ANY_OP)

    def test_an_op_the_receiver_does_not_allow_is_refused(self):
        gateway, client = _pair()
        with pytest.raises(protocol.ProtocolError):
            client.decode(gateway.encode({"op": "ping"}), allowed=frozenset({"ready"}))

    def test_one_bad_frame_ends_the_session_for_good(self):
        gateway, client = _pair()
        with pytest.raises(protocol.ProtocolError):
            client.decode("not json", allowed=ANY_OP)
        # Even the next genuine frame is refused, and nothing more is sent.
        with pytest.raises(protocol.ProtocolError):
            client.decode(gateway.encode({"op": "ping"}), allowed=ANY_OP)
        with pytest.raises(protocol.ProtocolError, match="session ended"):
            client.encode({"op": "ping"})

    def test_an_exhausted_sequence_sends_nothing(self, monkeypatch):
        gateway, _client = _pair()
        monkeypatch.setattr(gateway, "_send_sequence", authenticated._MAX_SEQUENCE + 1)
        with pytest.raises(protocol.ProtocolError, match="session ended"):
            gateway.encode({"op": "ping"})


# -- liveness ---------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _blocking_sleep(
    clock: _Clock, returns_at_once: Callable[[float], bool]
) -> Callable[[float], Awaitable[None]]:
    """A sleep that returns at once for the durations chosen, else parks until cancelled."""

    async def sleep(seconds: float) -> None:
        if returns_at_once(seconds):
            return
        await asyncio.get_running_loop().create_future()

    return sleep


class TestLiveness:
    def test_progress_moves_the_deadline(self):
        clock = _Clock()
        liveness = Liveness(clock=clock, deadline=FALLBACK_TIMEOUT_SECS)
        clock.now += FALLBACK_TIMEOUT_SECS - 1
        assert liveness.stale() is False
        liveness.progress()
        clock.now += FALLBACK_TIMEOUT_SECS - 1
        assert liveness.stale() is False
        clock.now += 1
        assert liveness.stale() is True

    def test_send_timeout_defaults_to_the_deadline(self):
        liveness = Liveness(clock=_Clock(), deadline=FALLBACK_TIMEOUT_SECS)
        assert liveness.send_timeout == FALLBACK_TIMEOUT_SECS
        given = Liveness(
            clock=_Clock(),
            deadline=FALLBACK_TIMEOUT_SECS,
            send_timeout=DEFAULT_PROGRESS_WINDOW_SECS,
        )
        assert given.send_timeout == DEFAULT_PROGRESS_WINDOW_SECS

    @pytest.mark.asyncio
    async def test_the_watchdog_ends_a_silent_connection(self):
        clock = _Clock()

        async def sleep(seconds: float) -> None:
            # Time passes exactly as long as asked, without waiting for it.
            clock.now += seconds

        pings: list[int] = []

        async def send_ping() -> None:
            pings.append(1)

        liveness = Liveness(
            clock=clock,
            sleep=sleep,
            interval=FALLBACK_TIMEOUT_SECS * 2,
            deadline=FALLBACK_TIMEOUT_SECS,
        )
        await finished(liveness.run(send_ping), "the liveness watchdog")
        assert liveness.lost.is_set()
        assert pings == []

    @pytest.mark.asyncio
    async def test_a_failed_ping_loses_liveness(self):
        clock = _Clock()
        sleep = _blocking_sleep(clock, lambda s: s == DEFAULT_INTERVAL_SECS)

        async def send_ping() -> None:
            raise ConnectionResetError("the socket is gone")

        liveness = Liveness(
            clock=clock,
            sleep=sleep,
            interval=DEFAULT_INTERVAL_SECS,
            deadline=FALLBACK_TIMEOUT_SECS,
        )
        await finished(liveness.run(send_ping), "the failed ping")
        assert liveness.lost.is_set()

    @pytest.mark.asyncio
    async def test_a_stalled_ping_loses_liveness_without_holding_the_pinger(self):
        clock = _Clock()
        sleep = _blocking_sleep(
            clock, lambda s: s in (DEFAULT_INTERVAL_SECS, DEFAULT_PROGRESS_WINDOW_SECS)
        )
        started = asyncio.Event()

        async def send_ping() -> None:
            started.set()
            # A relay that stopped reading: the send never finishes.
            await asyncio.get_running_loop().create_future()

        liveness = Liveness(
            clock=clock,
            sleep=sleep,
            interval=DEFAULT_INTERVAL_SECS,
            deadline=FALLBACK_TIMEOUT_SECS,
            send_timeout=DEFAULT_PROGRESS_WINDOW_SECS,
        )
        await finished(liveness.run(send_ping), "the stalled ping")
        assert started.is_set() and liveness.lost.is_set()

    @pytest.mark.asyncio
    async def test_pings_continue_until_liveness_is_lost(self):
        clock = _Clock()
        pings: list[int] = []
        intervals: list[float] = []
        liveness: Liveness

        async def sleep(seconds: float) -> None:
            if seconds == DEFAULT_INTERVAL_SECS:
                intervals.append(seconds)
                if len(intervals) == 3:
                    # Lost while the pinger slept: it stops without a third ping.
                    liveness.lost.set()
                return
            await asyncio.get_running_loop().create_future()

        async def send_ping() -> None:
            pings.append(1)

        liveness = Liveness(
            clock=clock,
            sleep=sleep,
            interval=DEFAULT_INTERVAL_SECS,
            deadline=FALLBACK_TIMEOUT_SECS,
        )
        await finished(liveness.run(send_ping), "the pinger")
        assert pings == [1, 1]

    @pytest.mark.asyncio
    async def test_receive_answers_none_once_lost(self):
        liveness = Liveness(clock=_Clock())
        liveness.lost.set()

        async def never() -> str:
            raise AssertionError("received after liveness was lost")

        assert await finished(liveness.receive(never), "the receive") is None

    @pytest.mark.asyncio
    async def test_receive_returns_the_message(self):
        liveness = Liveness(clock=_Clock())

        async def one() -> str:
            return "frame"

        assert await finished(liveness.receive(one), "the receive") == "frame"

    @pytest.mark.asyncio
    async def test_losing_liveness_cancels_a_pending_receive(self):
        liveness = Liveness(clock=_Clock())
        waiting = asyncio.Event()

        async def forever() -> str:
            waiting.set()
            await asyncio.get_running_loop().create_future()
            return "unreachable"

        receiving = asyncio.ensure_future(liveness.receive(forever))
        await finished(waiting.wait(), "the receive to start")
        liveness.lost.set()
        assert await finished(receiving, "the cancelled receive") is None


@pytest.mark.asyncio
async def test_consume_retrieves_a_finished_outcome():
    loop = asyncio.get_running_loop()
    failed = loop.create_future()
    failed.set_exception(RuntimeError("send failed"))
    authenticated._consume(failed)
    cancelled = loop.create_future()
    cancelled.cancel()
    authenticated._consume(cancelled)
    assert cancelled.cancelled() and isinstance(failed.exception(), RuntimeError)

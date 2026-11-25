"""The tunnel health probe keeps a slow far end's forward up and still tears a dead one down.

A crew that answers every probe a few seconds past the budget is alive, and its
forward works; a forward that relays nothing never answers at all. On the local
socket both are a connect accepted with no bytes back until the far end answers, so
the signal that tells the two apart is the late answer itself: a slow far end sends a
status line after the budget, a dead forward never sends one. So a timed-out probe's
request stays open as long as its answer could still count, and an answer on ANY
open request proves the forward. A forward that answers none of them is torn down after ``threshold``
misses.

The loop is shared by every transport (ssh, ssh through a ``ProxyCommand``, SSM, the
fargate lane) and by ``tunnel_keeper``, so these drive ``_probe_loop`` itself with a
controlled ``_forward_alive``: no sockets, no subprocesses.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew.instances import ssh_tunnel_manager as stm
from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel
from kiro_crew.testing.wait import async_wait_until, await_until


class _Proc:
    """A running child the probe loop never has to signal (its teardown is stubbed)."""

    returncode = None
    pid = 4242
    stderr = None


class _Probes:
    """``_forward_alive`` stand-in: each call parks on its own future the test resolves.

    ``answers`` maps a 1-based call number to what that call answers, and the call it
    answers in: ``{1: (2, True)}`` makes call 1 answer True the moment call 2 starts,
    which is a late answer to an already-missed probe. A call with no entry never
    answers, like a forward that relays nothing; ``cancelled`` records each call the
    loop cancelled, which is how a test sees that no request was left open.
    """

    def __init__(self, answers: dict[int, tuple[int, bool]] | None = None) -> None:
        self.answers = answers or {}
        self.futures: list[asyncio.Future[bool]] = []
        self.cancelled: set[int] = set()

    async def __call__(self) -> bool:
        call = len(self.futures) + 1
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.futures.append(fut)
        for earlier, (at_call, value) in self.answers.items():
            if at_call == call and not self.futures[earlier - 1].done():
                self.futures[earlier - 1].set_result(value)
        try:
            return await fut
        except asyncio.CancelledError:
            self.cancelled.add(call)
            raise


def _connected(probes: _Probes, *, threshold: int = 3) -> tuple[_SshTunnel, list[str], list[str]]:
    tunnel = _SshTunnel("cd-1", "cd-1-alias", 53997, 7777, probe_failure_threshold=threshold)
    tunnel._proc = _Proc()  # type: ignore[assignment]
    tunnel.status.state = TunnelState.CONNECTED
    tunnel._forward_alive = probes  # type: ignore[method-assign]
    healthy: list[str] = []
    tunnel._on_healthy = healthy.append
    torn: list[str] = []

    async def _teardown() -> None:
        torn.append("teardown")

    tunnel._probe_teardown = _teardown  # type: ignore[method-assign]
    return tunnel, healthy, torn


@pytest.fixture
def fast_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short probe timings. The budget stays well above one loop step, so a late
    answer resolved as the next probe starts is read inside that probe's budget."""
    monkeypatch.setattr(stm, "_PROBE_INTERVAL", 0.01)
    monkeypatch.setattr(stm, "_PROBE_HEALTH_TIMEOUT", 2.0)


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_probe")
@pytest.mark.parametrize("answered_at", [2, 3])
async def test_a_late_answer_to_a_missed_probe_keeps_the_tunnel_up(answered_at: int) -> None:
    """Call 1 misses its budget, then answers while a later probe is out: the far
    end is slow, not gone. The tunnel is not torn down, the miss count clears, and
    the answer counts as proven health (it clears the self-heal counter). Answered
    during the LAST probe before the threshold (call 3) is the case a dropped
    request can never see."""
    probes = _Probes({1: (answered_at, True)})
    tunnel, healthy, torn = _connected(probes)
    loop_task = asyncio.ensure_future(tunnel._probe_loop())
    try:
        await async_wait_until(
            lambda: healthy, describe=lambda: (tunnel._probe_failures, len(probes.futures))
        )
        assert torn == []
        assert tunnel._probe_failed is False
        assert tunnel._probe_failures == 0
        assert healthy == ["cd-1"]
    finally:
        tunnel._stopping = True
        tunnel._stop_event.set()
        await await_until(loop_task, what="the probe loop exiting")


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_probe")
async def test_a_forward_that_answers_nothing_is_still_torn_down_at_the_threshold() -> None:
    """A zombie: a forward that accepts and relays nothing never answers any probe,
    so it is torn down after exactly ``threshold`` misses, and none of the requests
    kept open for a late answer outlives the teardown."""
    probes = _Probes()
    tunnel, healthy, torn = _connected(probes)
    await await_until(asyncio.ensure_future(tunnel._probe_loop()), what="the probe loop exiting")
    await async_wait_until(lambda: torn, describe=lambda: tunnel._probe_failures)
    assert tunnel._probe_failed is True
    assert len(probes.futures) == 3
    assert healthy == []
    assert probes.cancelled == {1, 2, 3}
    assert "health probe failed" in tunnel._exit_error(-15)


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_probe")
async def test_a_late_refusal_is_not_an_answer() -> None:
    """Only a completed HTTP response proves the forward. A missed probe that later
    fails (its own request cap ran out, or the far end refused) is still a miss."""
    probes = _Probes({1: (2, False), 2: (3, False)})
    tunnel, healthy, torn = _connected(probes)
    await await_until(asyncio.ensure_future(tunnel._probe_loop()), what="the probe loop exiting")
    await async_wait_until(lambda: torn, describe=lambda: tunnel._probe_failures)
    assert tunnel._probe_failed is True
    assert healthy == []


@pytest.mark.asyncio
async def test_a_refused_probe_is_a_miss_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Far end down: the forward relays the refusal back at once. Nothing is left
    open, so each probe is a miss without waiting out its budget, as before."""
    monkeypatch.setattr(stm, "_PROBE_INTERVAL", 0.01)
    # A budget no probe here may wait out: a refusal must not sit on it.
    monkeypatch.setattr(stm, "_PROBE_HEALTH_TIMEOUT", 3600.0)
    calls: list[int] = []

    async def _refused() -> bool:
        calls.append(1)
        return False

    tunnel, healthy, torn = _connected(_Probes())
    tunnel._forward_alive = _refused  # type: ignore[method-assign]
    await await_until(asyncio.ensure_future(tunnel._probe_loop()), what="the probe loop exiting")
    await async_wait_until(lambda: torn, describe=lambda: tunnel._probe_failures)
    assert len(calls) == 3
    assert healthy == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_probe")
async def test_stop_during_a_probe_cancels_every_open_request() -> None:
    """``stop()`` while probes are out: the loop ends, every request kept open for a
    late answer is cancelled, and nothing is torn down by the probe."""
    probes = _Probes()
    tunnel, _healthy, torn = _connected(probes)
    tunnel._probe_task = asyncio.ensure_future(tunnel._probe_loop())
    await async_wait_until(lambda: tunnel._probe_failures >= 2 and len(probes.futures) >= 3)
    tunnel._proc = None  # stop() has no child to terminate
    await tunnel.stop()
    assert tunnel._probe_task.done()
    assert probes.cancelled == set(range(1, len(probes.futures) + 1))
    assert torn == []
    assert tunnel._probe_failed is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_probe")
async def test_an_answer_after_recovery_resets_the_count_for_the_next_stall() -> None:
    """Slow, recovered, then dead: the late answer clears the count, so the dead
    spell after it needs the full threshold of fresh misses before a teardown."""
    probes = _Probes({1: (2, True)})
    tunnel, healthy, torn = _connected(probes)
    await await_until(asyncio.ensure_future(tunnel._probe_loop()), what="the probe loop exiting")
    await async_wait_until(lambda: torn, describe=lambda: tunnel._probe_failures)
    assert healthy == ["cd-1"]
    # Call 1 missed, then answered while call 2 was out, which was the success;
    # calls 3, 4 and 5 are the three misses of the dead spell.
    assert len(probes.futures) == 5
    assert probes.cancelled == {2, 3, 4, 5}


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast_probe")
async def test_a_request_older_than_a_success_cannot_answer_for_a_later_probe() -> None:
    """Call 1 answers while call 2 is out: that is the success, and call 2 is now
    stale. If call 2 answers later, during a dead spell, it must not count: the
    dead spell still needs three fresh misses, and health is reported once."""
    probes = _Probes({1: (2, True), 2: (4, True)})
    tunnel, healthy, torn = _connected(probes)
    await await_until(asyncio.ensure_future(tunnel._probe_loop()), what="the probe loop exiting")
    await async_wait_until(lambda: torn, describe=lambda: tunnel._probe_failures)
    assert healthy == ["cd-1"]
    assert len(probes.futures) == 5
    assert 2 in probes.cancelled


class _CapturingSession:
    """``aiohttp.ClientSession`` stand-in that records the timeout it was built with."""

    def __init__(self) -> None:
        self.timeout: Any = None

    def __call__(self, *_a: Any, timeout: Any = None, **_kw: Any) -> "_CapturingSession":
        self.timeout = timeout
        return self

    async def __aenter__(self) -> "_CapturingSession":
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False

    def get(self, _url: str, **_kw: Any) -> Any:
        class _Resp:
            status = 200

            async def __aenter__(self) -> "_Resp":
                return self

            async def __aexit__(self, *_a: Any) -> bool:
                return False

        return _Resp()


@pytest.mark.asyncio
async def test_a_probe_request_stays_open_as_long_as_its_answer_can_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request itself must outlive the per-probe budget, or the late answer is
    dropped by the HTTP client before the loop can read it. Its own cap is the
    longest a missed request can wait and still count before the teardown is
    decided, from the tunnel's OWN threshold (an operator can raise it)."""
    session = _CapturingSession()
    monkeypatch.setattr(stm.aiohttp, "ClientSession", session)
    tunnel = _SshTunnel("cd-1", "cd-1-alias", 53997, 7777, probe_failure_threshold=7)
    assert await tunnel._forward_alive() is True
    step = stm._PROBE_INTERVAL + stm._PROBE_HEALTH_TIMEOUT
    assert session.timeout.total == 6 * step + stm._PROBE_HEALTH_TIMEOUT
    assert session.timeout.total > stm._PROBE_HEALTH_TIMEOUT

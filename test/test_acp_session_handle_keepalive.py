"""A keepalive touch for a session defers that session's tool-stall watchdog.

Blocking MCP tools (``wait``, ``spawn_sub_agents``) send no frame while they
run; they ping ``/api/session-keepalive`` instead. On a shared runtime the
tool-idle clock reads only this session's own frames, so the touch has to be
stamped on the session's handle, or a cron run's blocking ``spawn_sub_agents``
is cancelled at the tool-stall window while its sub-agents are still working.

The handle's clock is a fake the polled queue advances by one exact tick per
poll, so every interval below is computed, not measured: scheduler delay
cannot move a result.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import session_handle
from kiro_crew.acp.liveness import VERDICT_DEAD, VERDICT_UNKNOWN, ToolCallState
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import STOP_REASON_TOOL_STALL

# Binary fractions, so the clock's sums are exact.
_TICK = 1 / 64
_WINDOW = 16 * _TICK
_START = 1024.0

# The oracle's reading for spawn_sub_agents polling the gateway: no CPU or IO
# under the MCP subtree, so the verdict is UNKNOWN with no narrowing tag.
_FLAT = (VERDICT_UNKNOWN, "mcp subtree flat (io +0B cpu +0t)")


class _Clock:
    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(session_handle, "time", types.SimpleNamespace(monotonic=fake))
    return fake


class _TickingQueue:
    """Silent queue: each poll advances the clock one tick, then calls *touch*
    (at most *touches* times; None for every poll), then times out."""

    def __init__(
        self, clock: _Clock, touch: Callable[[], None] | None = None, touches: int | None = None
    ) -> None:
        self._clock = clock
        self._touch = touch
        self._left = touches
        self.touched_at: list[float] = []

    async def get(self):
        await asyncio.sleep(0)
        self._clock.now += _TICK
        if self._touch is not None and (self._left is None or self._left > 0):
            self._touch()
            self.touched_at.append(self._clock.now)
            if self._left is not None:
                self._left -= 1
        raise asyncio.TimeoutError

    def qsize(self) -> int:
        return 0


def _settings() -> WatchdogSettings:
    return WatchdogSettings(
        check_after_secs=_TICK / 2,
        tool_stall_suspect_secs=_WINDOW,
        tool_stall_hard_cap_secs=_WINDOW,
    )


def _runtime(clock: _Clock) -> MagicMock:
    rt = MagicMock()
    rt._last_activity = clock.now
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    return rt


def _blocked_in_tool(session_id: str, rt: MagicMock) -> AcpSessionHandle:
    handle = AcpSessionHandle(session_id, asyncio.Queue(), rt, watchdog=_settings())
    handle._turn_done.clear()
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._inflight_tool = ToolCallState(title="spawn_sub_agents", command="{}")

    async def _flat(*, model_wait: bool) -> tuple[str, str]:
        return _FLAT

    handle._consult_oracle_offloaded = _flat  # type: ignore[method-assign]
    return handle


def _record_cancel(rt: MagicMock, clock: _Clock) -> dict[str, float]:
    cancelled_at: dict[str, float] = {}

    async def _note(method, *args, **kwargs):
        if method == "session/cancel":
            cancelled_at.setdefault("t", clock.now)

    rt.send_notification = AsyncMock(side_effect=_note)
    return cancelled_at


async def _drain(handle: AcpSessionHandle, queue: _TickingQueue, timeout: float) -> list:
    handle._queue = queue  # type: ignore[assignment]
    return [ev async for ev in handle._dispatch_events(1, timeout)]


@pytest.mark.asyncio
async def test_a_silent_tool_without_keepalive_is_cancelled_at_the_window(clock):
    """Control: the harness really reaches the tool-stall cancel, on time."""
    rt = _runtime(clock)
    handle = _blocked_in_tool("sA", rt)
    cancelled_at = _record_cancel(rt, clock)

    events = await _drain(handle, _TickingQueue(clock), timeout=_WINDOW * 4)

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert cancelled_at["t"] - _START == _WINDOW + _TICK


@pytest.mark.asyncio
async def test_keepalive_through_the_provider_keeps_a_blocking_tool_alive(clock):
    """The route's call, provider.touch_activity(), defers the tool clock."""
    rt = _runtime(clock)
    handle = _blocked_in_tool("sA", rt)
    provider = AcpSessionProvider(handle, rt)
    cancelled_at = _record_cancel(rt, clock)

    queue = _TickingQueue(clock, provider.touch_activity)
    events = await _drain(handle, queue, timeout=_WINDOW * 4)

    assert queue.touched_at[-1] - _START >= _WINDOW * 3
    assert "t" not in cancelled_at
    assert all(ev.stop_reason != STOP_REASON_TOOL_STALL for ev in events)


@pytest.mark.asyncio
async def test_the_window_runs_again_once_the_pings_stop(clock):
    """A keepalive defers the watchdog; it does not switch it off. The cancel
    lands one window after the last touch, not one window after the start."""
    rt = _runtime(clock)
    handle = _blocked_in_tool("sA", rt)
    cancelled_at = _record_cancel(rt, clock)
    queue = _TickingQueue(clock, handle.note_keepalive, touches=32)

    events = await _drain(handle, queue, timeout=_WINDOW * 8)

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert queue.touched_at[-1] - _START == 32 * _TICK
    assert cancelled_at["t"] - queue.touched_at[-1] == _WINDOW + _TICK


@pytest.mark.asyncio
async def test_a_co_tenant_keepalive_does_not_defer_this_session(clock):
    """Another session's touch on the same runtime is not this tool's progress."""
    rt = _runtime(clock)
    mine = _blocked_in_tool("sA", rt)
    neighbour = AcpSessionProvider(_blocked_in_tool("sB", rt), rt)
    cancelled_at = _record_cancel(rt, clock)

    events = await _drain(mine, _TickingQueue(clock, neighbour.touch_activity), timeout=_WINDOW * 4)

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert cancelled_at["t"] - _START == _WINDOW + _TICK


@pytest.mark.asyncio
async def test_a_keepalive_during_the_oracle_check_prevents_the_cancel(clock):
    """The TOCTOU guard covers keepalives as it covers frames: a touch that
    lands while the oracle is being consulted voids that consult's verdict.

    The first consult blocks until released and answers DEAD, which acts at
    once, so without the guard the cancel would fire on that verdict.
    """
    rt = _runtime(clock)
    handle = _blocked_in_tool("sA", rt)
    cancelled_at = _record_cancel(rt, clock)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = {"n": 0}

    async def _oracle(*, model_wait: bool) -> tuple[str, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            await release.wait()
            return VERDICT_DEAD, "no established backend socket and flat counters"
        return _FLAT

    handle._consult_oracle_offloaded = _oracle  # type: ignore[method-assign]
    drain = asyncio.create_task(_drain(handle, _TickingQueue(clock), timeout=_WINDOW * 4))
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    touched = clock.now
    handle.note_keepalive()
    release.set()
    events = await asyncio.wait_for(drain, timeout=5.0)

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert cancelled_at["t"] - touched == _WINDOW + _TICK


class _ScriptedQueue(_TickingQueue):
    """Ticking queue that runs ``script[n]`` on poll ``n`` (1-based)."""

    def __init__(self, clock: _Clock, script: dict[int, Callable[[], None]]) -> None:
        super().__init__(clock)
        self._script = script
        self._polls = 0

    async def get(self):
        await asyncio.sleep(0)
        self._clock.now += _TICK
        self._polls += 1
        action = self._script.get(self._polls)
        if action is not None:
            action()
        raise asyncio.TimeoutError


@pytest.mark.asyncio
async def test_a_keepalive_during_a_consumer_park_counts_only_the_park_after_it(clock):
    """A touch that lands while the consumer holds an event takes the park so
    far into its baseline, so only the park AFTER the touch is subtracted.

    Park from tick 1 to tick 12, touch at tick 9: three ticks of park follow
    the touch, so the cancel lands at 9 + 3 + window + 1 = tick 29. A baseline
    that left out the open park would subtract all eleven and cancel at 37.
    """
    rt = _runtime(clock)
    handle = _blocked_in_tool("sA", rt)
    cancelled_at = _record_cancel(rt, clock)

    def _park() -> None:
        handle._parked_since = clock.now

    def _unpark() -> None:
        assert handle._parked_since is not None
        handle._parked_total += clock.now - handle._parked_since
        handle._parked_since = None

    queue = _ScriptedQueue(clock, {1: _park, 9: handle.note_keepalive, 12: _unpark})
    events = await _drain(handle, queue, timeout=_WINDOW * 8)

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert cancelled_at["t"] - _START == 29 * _TICK

"""A co-tenant frame landing during the tool-stall oracle await costs one tick.

On a shared kiro-cli runtime an ownerless frame, such as a co-tenant subagent's
``_kiro.dev/subagent/list_update`` roster, is fanned out to every registered
session and marked ``fanout_no_owner``. When one lands while the tool branch
awaits the liveness oracle, the TOCTOU guard voids that consult's verdict,
because the frame has no known owner yet. That must cost one watchdog tick and
keep the idle measured so far: restarting the window instead leaves a lost tool
call next to a talkative subagent hanging for as long as the co-tenant keeps
talking, plus one more full window. A frame of this session's own still
restarts the window, on both delivery paths the guard watches (still queued,
or held by a concurrent ``_wait_for_response``).

The handle's clock is a fake the polled queue advances by one exact tick per
empty poll, so every instant below is computed, not measured.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import types
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import session_handle
from kiro_crew.acp.liveness import EVIDENCE_SHELL_CHILD_ABSENT, VERDICT_UNKNOWN, ToolCallState
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.types import METHOD_SUBAGENT_LIST_UPDATE, STOP_REASON_TOOL_STALL, JsonRpcMessage
from kiro_crew.testing.wait import await_until

# Binary fractions, so the clock's sums are exact.
_TICK = 1 / 64
_WINDOW = 16 * _TICK
_START = 1024.0
#: The turn's own budget: past every cancel below, so a turn that never
#: recovers ends on its deadline instead.
_TURN = 4 * _WINDOW
#: With no frame at all, poll N reads an idle of N ticks and its consult is
#: consult N; poll 17 is the first whose idle exceeds the window.
_ACTING_CONSULT = 17
#: The concurrent command call's request id and budget (never reached here).
_COMMAND_REQ = 77
_COMMAND_BUDGET = 1024.0

# The real oracle's reading for a lost shell call: auto-approved, never
# started, so no child exists. It narrows the UNKNOWN window to
# stale_window_secs.
_LOST_SHELL = (
    VERDICT_UNKNOWN,
    f"{EVIDENCE_SHELL_CHILD_ABSENT}: no shell child started since dispatch",
)

# Set inside the concurrent command call's task only, so the queue double can
# tell its reads from the turn loop's on every Python the suite runs.
_IN_COMMAND_CALL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "in_command_call", default=False
)


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


class _SharedQueue:
    """The session queue, read by the turn loop and by a command call.

    The turn loop gets a queued frame at once; on an empty queue its poll
    advances the clock one tick and times out, which is the watchdog's tick.
    A command call takes the frames handed to it and then blocks, holding
    them in its buffer, without moving the clock.
    """

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self._items: list[JsonRpcMessage] = []
        self.for_command_call: list[JsonRpcMessage] = []
        self.command_call_waiting = asyncio.Event()
        self._never = asyncio.Event()

    async def get(self) -> JsonRpcMessage:
        await asyncio.sleep(0)
        if _IN_COMMAND_CALL.get():
            if self.for_command_call:
                return self.for_command_call.pop(0)
            self.command_call_waiting.set()
            await self._never.wait()
        if self._items:
            return self._items.pop(0)
        self._clock.now += _TICK
        raise asyncio.TimeoutError

    def put_nowait(self, item: JsonRpcMessage) -> None:
        self._items.append(item)

    def qsize(self) -> int:
        return len(self._items)


def _roster(*, co_tenant: bool) -> JsonRpcMessage:
    """A subagent roster frame: fanned out from a co-tenant, or this session's own."""
    return JsonRpcMessage(
        method=METHOD_SUBAGENT_LIST_UPDATE,
        params={"subagents": []},
        fanout_no_owner=co_tenant,
    )


def _settings() -> WatchdogSettings:
    # The suspect window is wider than the narrowed one, so the cancel times
    # below are the narrowed window's.
    return WatchdogSettings(
        check_after_secs=_TICK / 2,
        stale_window_secs=_WINDOW,
        tool_stall_suspect_secs=4 * _WINDOW,
        tool_stall_hard_cap_secs=4 * _WINDOW,
    )


_Script = Callable[[int, AcpSessionHandle, _SharedQueue], Awaitable[None]]


async def _nothing(_n: int, _handle: AcpSessionHandle, _queue: _SharedQueue) -> None:
    return None


class _Turn:
    """One turn whose dispatched shell call is lost, under the fake clock.

    ``script(n, handle, queue)`` runs inside oracle consult ``n``, while the
    turn loop awaits it: whatever it delivers lands during the oracle await.
    """

    def __init__(self, clock: _Clock, script: _Script = _nothing) -> None:
        self._clock = clock
        rt = MagicMock()
        rt._last_activity = clock.now
        rt.pid = None
        rt.is_alive = MagicMock(return_value=True)
        self.cancelled_at: float | None = None

        async def _note(method, *args, **kwargs):
            if method == "session/cancel" and self.cancelled_at is None:
                self.cancelled_at = clock.now - _START

        rt.send_notification = AsyncMock(side_effect=_note)
        self.queue = _SharedQueue(clock)
        handle = AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=_settings())
        handle._queue = self.queue  # type: ignore[assignment]
        handle._turn_done.clear()
        handle._stale_eligible = False
        handle._tool_dispatched = True
        handle._inflight_tool = ToolCallState(
            title="git status --short", command="git status --short", is_shell=True
        )
        self.handle = handle
        self.consults = 0
        self.command_calls: list[asyncio.Future] = []

        async def _consult(*, model_wait: bool) -> tuple[str, str]:
            self.consults += 1
            await script(self.consults, handle, self.queue)
            return _LOST_SHELL

        handle._consult_oracle_offloaded = _consult  # type: ignore[method-assign]

    async def run(self) -> list:
        try:
            return [ev async for ev in self.handle._dispatch_events(1, _TURN)]
        finally:
            for call in self.command_calls:
                call.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await call


async def _command_call_holds(
    turn: _Turn, handle: AcpSessionHandle, queue: _SharedQueue, frame: JsonRpcMessage
) -> None:
    """Start a command call beside the turn that takes *frame* off the queue
    and keeps it buffered while it waits for its own answer."""

    async def _call() -> None:
        _IN_COMMAND_CALL.set(True)
        await handle._wait_for_response(_COMMAND_REQ, _COMMAND_BUDGET)

    queue.for_command_call.append(frame)
    turn.command_calls.append(asyncio.ensure_future(_call()))
    await await_until(queue.command_call_waiting, what="the command call holding the frame")


@pytest.mark.asyncio
async def test_a_lost_shell_call_is_cancelled_one_tick_past_the_window(clock):
    """Control: with no frame at all the cancel lands on the first poll past
    the window."""
    turn = _Turn(clock)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert turn.cancelled_at == _WINDOW + _TICK


@pytest.mark.asyncio
async def test_a_co_tenant_frame_queued_during_the_consult_costs_one_tick(clock):
    """The roster lands in the queue during the consult that would act. That
    verdict is void, and the next consult acts on the idle already measured,
    one tick later, instead of starting the window over."""

    async def script(n: int, handle: AcpSessionHandle, queue: _SharedQueue) -> None:
        if n == _ACTING_CONSULT:
            queue.put_nowait(_roster(co_tenant=True))

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert turn.cancelled_at == _WINDOW + 2 * _TICK


@pytest.mark.asyncio
async def test_co_tenant_frames_in_many_consults_never_restart_the_window(clock):
    """A roster in every other consult, the acting one included: each voids
    only its own consult, so the cancel still lands one tick late, not never."""

    async def script(n: int, handle: AcpSessionHandle, queue: _SharedQueue) -> None:
        if n % 2 == _ACTING_CONSULT % 2:
            queue.put_nowait(_roster(co_tenant=True))

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert turn.cancelled_at == _WINDOW + 2 * _TICK


@pytest.mark.asyncio
async def test_a_co_tenant_frame_a_command_call_holds_costs_one_tick(clock):
    """The same roster, taken off the queue by a concurrent command call and
    still in its buffer when the consult returns: the ingress count voids the
    verdict, and nothing names the frame this session's, so the idle stands."""

    async def script(n: int, handle: AcpSessionHandle, queue: _SharedQueue) -> None:
        if n == _ACTING_CONSULT:
            await _command_call_holds(turn, handle, queue, _roster(co_tenant=True))

    turn = _Turn(clock, script)

    events = await turn.run()

    assert turn.handle._ingress_seq == 1
    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert turn.cancelled_at == _WINDOW + 2 * _TICK


_OWN_FRAME_CONSULT = 12


@pytest.mark.asyncio
async def test_an_own_frame_queued_during_the_consult_restarts_the_window(clock):
    """A roster routed to this session is its own progress: the window runs
    again from the consult it landed in."""

    async def script(n: int, handle: AcpSessionHandle, queue: _SharedQueue) -> None:
        if n == _OWN_FRAME_CONSULT:
            queue.put_nowait(_roster(co_tenant=False))

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert turn.cancelled_at == _OWN_FRAME_CONSULT * _TICK + _WINDOW + _TICK


@pytest.mark.asyncio
async def test_an_own_frame_a_command_call_holds_restarts_the_window(clock):
    """This session's frame held in a command call's buffer never reaches the
    turn loop during this run, so only the own-frame count can tell the guard
    it was progress; the window runs again from that consult."""

    async def script(n: int, handle: AcpSessionHandle, queue: _SharedQueue) -> None:
        if n == _OWN_FRAME_CONSULT:
            await _command_call_holds(turn, handle, queue, _roster(co_tenant=False))

    turn = _Turn(clock, script)

    events = await turn.run()

    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert turn.cancelled_at == _OWN_FRAME_CONSULT * _TICK + _WINDOW + _TICK

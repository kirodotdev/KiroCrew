"""Reading whether a sub-agent run is done runs no code; the write that ends it wakes.

Admission reads ``SubagentInfo.done`` for every registered run on every admission,
tens of millions of times in a wide fan-out, so the read must be served straight
from the instance. The wake-up for a caller waiting on the run (``wait_done``)
therefore rides the WRITE of that one field, which happens once per run, and no
other field's write pays for it.

Nothing here measures time: the tests count the calls a read and a write make.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from types import FrameType
from typing import Any

import pytest

from kiro_crew.subagent import SubagentInfo, wait_done
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until, await_until


def _calls_inside(action: Callable[[], None]) -> list[str]:
    """Every Python or C function called while *action* runs, *action* itself excluded."""
    seen: list[str] = []

    def record(frame: FrameType, event: str, arg: Any) -> None:
        if event == "call" and frame.f_code is not action.__code__:
            seen.append(frame.f_code.co_qualname)
        elif event == "c_call" and arg is not sys.setprofile:
            seen.append(getattr(arg, "__qualname__", repr(arg)))

    previous = sys.getprofile()
    sys.setprofile(record)
    try:
        action()
    finally:
        sys.setprofile(previous)
    return seen


def _waiter(info: SubagentInfo) -> list[str]:
    """Park one counted wake-up on *info*, as ``wait_done`` parks its own."""
    woken: list[str] = []
    info._done_wakers.append(lambda: woken.append(info.id))
    return woken


class TestReadingDoneRunsNoCode:
    def test_a_read_of_done_calls_nothing(self):
        running = SubagentInfo(id="a1", task="t")
        ended = SubagentInfo(id="a2", task="t", done=True)
        values: list[bool] = []

        def read() -> None:
            values[:] = [running.done, ended.done, running.done, ended.done]

        assert _calls_inside(read) == []
        assert values == [False, True, False, True]

    def test_the_counter_sees_a_call_when_there_is_one(self):
        """The check above is not vacuous: a property read on the same record counts."""
        info = SubagentInfo(id="a1", task="t")
        seen: list[str] = []

        def read() -> None:
            seen.append(info.outcome)

        assert any(name.endswith("SubagentInfo.outcome") for name in _calls_inside(read))

    def test_nothing_on_the_class_can_intercept_a_read_of_done(self):
        """A class attribute intercepts a read only through ``__get__``; the write
        hook has none, so the value comes from the instance like any plain field."""
        assert not hasattr(type(vars(SubagentInfo)["done"]), "__get__")
        assert SubagentInfo.__getattribute__ is object.__getattribute__
        assert not hasattr(SubagentInfo, "__getattr__")
        assert vars(SubagentInfo(id="a1", task="t"))["done"] is False

    def test_a_write_of_another_field_calls_nothing(self):
        """The wake-up is on ``done`` alone, not on every write of the record."""
        info = SubagentInfo(id="a1", task="t")

        def write() -> None:
            info.result = "text"
            info.turns = 3
            info.queued = True

        assert _calls_inside(write) == []
        assert SubagentInfo.__setattr__ is object.__setattr__


class TestOnlyTheWriteThatEndsTheRunWakes:
    def test_reads_and_other_writes_wake_nobody_and_the_ending_wakes_once(self):
        info = SubagentInfo(id="a1", task="t")
        woken = _waiter(info)
        assert [info.done, info.done, info.done] == [False, False, False]
        info.result = "partial text"
        info.queued = True
        info.done = False
        assert woken == []
        info.done = True
        assert woken == ["a1"]
        assert [info.done, info.done] == [True, True]
        assert woken == ["a1"]

    def test_a_record_built_already_ended_has_nobody_to_wake(self):
        """``__init__`` assigns ``done`` before the list of waiters exists."""
        info = SubagentInfo(id="a1", task="t", done=True)
        assert info.done is True and info._done_wakers == []

    def test_runs_ended_in_one_pass_each_wake_their_own_waiter_once(self):
        """Orphan recovery and stop-all end many runs in a loop."""
        infos = [SubagentInfo(id=f"a{i}", task="t") for i in range(64)]
        woken = [_waiter(info) for info in infos]
        for info in infos[:40]:
            info.done = True
        assert woken[:40] == [[f"a{i}"] for i in range(40)]
        assert woken[40:] == [[] for _ in range(24)]

    def test_a_continued_run_is_woken_by_its_own_ending_only(self):
        """A continuation is a new run with its own record; the run it continues
        stays ended, and a late write on that one wakes nobody else."""
        ended = SubagentInfo(id="a1", task="t", done=True)
        continued = SubagentInfo(id="a2", task="t", conversation_key="subagent:a1")
        woken = _waiter(continued)
        ended.done = True
        assert woken == []
        continued.done = True
        assert woken == ["a2"]


class TestWaitDone:
    @pytest.mark.asyncio
    async def test_a_run_that_ended_before_the_wait_began_is_answered_at_once(self):
        info = SubagentInfo(id="a1", task="t")
        info.done = True
        assert await wait_done(info, FALLBACK_TIMEOUT_SECS) is True
        assert info._done_wakers == []

    @pytest.mark.asyncio
    async def test_the_wait_is_woken_by_the_write_and_leaves_nothing_behind(self):
        info = SubagentInfo(id="a1", task="t")
        waiting = asyncio.ensure_future(wait_done(info, FALLBACK_TIMEOUT_SECS))
        await async_wait_until(lambda: info._done_wakers)
        info.done = True
        assert await await_until(waiting, what="the wait on the run") is True
        assert info._done_wakers == []

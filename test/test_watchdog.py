"""Tests for the SessionWatchdog dispatcher (CR 1, wrap-only).

The watchdog is a dumb sequential dispatcher: it runs each CleanupHook in
registration order and isolates a failing hook so the rest still run. It does
NOT model decisions/verdicts (that is CR 2).
"""

from __future__ import annotations

import pytest

from kiro_crew.watchdog import CleanupHook, SessionWatchdog


class TestDispatch:
    @pytest.mark.asyncio
    async def test_hooks_run_in_registration_order(self) -> None:
        calls: list[str] = []

        async def a() -> None:
            calls.append("a")

        async def b() -> None:
            calls.append("b")

        async def c() -> None:
            calls.append("c")

        wd = SessionWatchdog(
            [CleanupHook("a", a), CleanupHook("b", b), CleanupHook("c", c)]
        )
        await wd.tick()
        assert calls == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_each_tick_runs_all_hooks_again(self) -> None:
        count = {"n": 0}

        async def inc() -> None:
            count["n"] += 1

        wd = SessionWatchdog([CleanupHook("inc", inc)])
        await wd.tick()
        await wd.tick()
        assert count["n"] == 2

    @pytest.mark.asyncio
    async def test_empty_hook_list_is_noop(self) -> None:
        wd = SessionWatchdog([])
        await wd.tick()  # must not raise


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_one_hook_raising_does_not_block_the_rest(self) -> None:
        calls: list[str] = []

        async def first() -> None:
            calls.append("first")

        async def boom() -> None:
            calls.append("boom")
            raise RuntimeError("hook blew up")

        async def last() -> None:
            calls.append("last")

        wd = SessionWatchdog(
            [CleanupHook("first", first), CleanupHook("boom", boom), CleanupHook("last", last)]
        )
        # tick() must swallow the hook's exception (debug backstop) and keep going.
        await wd.tick()
        assert calls == ["first", "boom", "last"]

    @pytest.mark.asyncio
    async def test_dispatcher_does_not_reraise(self) -> None:
        async def boom() -> None:
            raise ValueError("nope")

        wd = SessionWatchdog([CleanupHook("boom", boom)])
        # No exception should escape tick().
        await wd.tick()


class TestSyncWrapperContract:
    """A sync cleanup function lifted into an async wrapper must be awaitable by
    the dispatcher exactly like a native coroutine hook (the deniedCommands
    case)."""

    @pytest.mark.asyncio
    async def test_sync_wrapped_in_async_is_dispatched(self) -> None:
        ran = {"sync": False}

        def sync_work() -> None:
            ran["sync"] = True

        async def wrapper() -> None:
            sync_work()

        wd = SessionWatchdog([CleanupHook("denied", wrapper)])
        await wd.tick()
        assert ran["sync"] is True

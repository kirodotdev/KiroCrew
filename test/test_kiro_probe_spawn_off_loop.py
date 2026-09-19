"""Tests pinning that the Kiro CLI readiness spawn never runs on the gateway loop.

On native Windows the dashboard's own idle polling forces a ``kiro-cli`` readiness
re-probe about every 30 seconds, and an on-loop spawn there performs
``CreateProcess`` **on the event-loop thread**. CPython leaves no await point in
between -- ``ProactorEventLoop._make_subprocess_transport`` constructs
``_WindowsSubprocessTransport`` before its first ``await waiter``, that
constructor calls ``self._start()``, and ``_start`` calls ``windows_utils.Popen``
-> ``subprocess.Popen._execute_child`` -> ``CreateProcess``. On an image with an
endpoint-protection filter driver that call takes seconds, so the loop advanced
nothing until the loop-stall watchdog exceeded its 25s budget and killed the
gateway. A single forced probe makes SEVERAL of those spawns in sequence, so the
stalls add up against one budget.

``asyncio.to_thread(asyncio.create_subprocess_exec, ...)`` is not the fix, and
``test_the_naive_to_thread_patch_would_spawn_nothing`` pins why: that is a
coroutine FUNCTION, so the worker thread only builds a coroutine object and hands
it back unawaited. Nothing is spawned there, and awaiting it on the gateway loop
performs the identical on-loop ``CreateProcess``. The offload therefore has to be
a whole event loop -- asyncio offers no way to adopt an already-spawned ``Popen``
into a subprocess transport.

These tests pin four contracts:

1. On Windows, ``_run_process`` performs its ``create_subprocess_exec`` on a
   DIFFERENT loop and a DIFFERENT thread than the caller's, and the caller's loop
   keeps ticking while that spawn blocks.
2. On POSIX, there is no hop at all -- the spawn stays on the caller's loop, so
   the platform that never had the defect pays nothing for the fix.
3. The hop re-enters ``_run_process`` exactly ONCE (the private-loop marker
   terminates the recursion) and returns the body's own result unchanged.
4. The hop's pool is NOT ``subprocess_executor()``. The offloaded loop awaits the
   Windows descendant scan, which submits into that pool; sharing one pool would
   let concurrent spawns hold workers while waiting on scans queued behind them.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from typing import Any

import pytest

import kiro_crew.kiro_prerequisite as prerequisite_module
from kiro_crew import platform_compat
from kiro_crew.executors import kiro_spawn_executor, subprocess_executor
from kiro_crew.kiro_prerequisite import _run_process

# Long enough that a caller-loop heartbeat at _TICK_SECS could not plausibly
# advance by _MIN_TICKS if the spawn held the loop, short enough to keep the
# suite fast. Stands in for CreateProcess behind a filter driver.
_BLOCK_SECS = 0.4
_TICK_SECS = 0.01
_MIN_TICKS = 5


class _SpawnRecord:
    """Where a fake spawn actually ran."""

    def __init__(self) -> None:
        self.calls = 0
        self.thread_names: list[str] = []
        self.loop_ids: list[int] = []


def _blocking_spawn(record: _SpawnRecord, *, block: bool):
    """A fake ``create_subprocess_exec`` that records its loop, then fails.

    It raises ``OSError`` on purpose: ``_run_process`` catches that and returns a
    ``ProcessResult``, which stops the body before the Windows tree machinery
    (real ``ctypes`` calls that cannot run on Linux) and keeps these tests about
    the one thing they are for -- WHERE the spawn happened.

    The wait is ``time.sleep``, not ``asyncio.sleep``, because a blocking call is
    exactly what ``CreateProcess`` is. An ``await`` here would let the caller's
    loop advance even with the defect present, and the test would pass on the
    broken code.
    """

    async def _spawn(*_args: str, **_kwargs: Any) -> Any:
        record.calls += 1
        record.thread_names.append(threading.current_thread().name)
        record.loop_ids.append(id(asyncio.get_running_loop()))
        if block:
            time.sleep(_BLOCK_SECS)
        raise OSError("fake spawn refused after recording where it ran")

    return _spawn


def _as_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)


class TestWindowsSpawnLeavesTheGatewayLoop:
    @pytest.mark.asyncio
    async def test_spawn_runs_on_another_loop_and_thread(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        record = _SpawnRecord()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _blocking_spawn(record, block=False))
        _as_windows(monkeypatch)

        caller_loop_id = id(asyncio.get_running_loop())
        caller_thread_name = threading.current_thread().name

        result = await _run_process(
            r"C:\fixed\tool.exe",
            ["--version"],
            env={},
            timeout_secs=1,
        )

        assert result.ok is False
        assert record.calls == 1, "the hop must re-enter the body exactly once"
        assert (
            record.loop_ids[0] != caller_loop_id
        ), "CreateProcess ran on the caller's event loop -- the stall is back"
        assert record.thread_names[0] != caller_thread_name
        assert record.thread_names[0].startswith("mc-kirospawn")

    @pytest.mark.asyncio
    async def test_caller_loop_keeps_ticking_while_the_spawn_blocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        record = _SpawnRecord()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _blocking_spawn(record, block=True))
        _as_windows(monkeypatch)

        ticks = 0
        stop = False

        async def _heartbeat() -> None:
            # Stands in for the gateway's own loop-stall heartbeat: the task the
            # watchdog watches, and the one that stopped advancing.
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(_TICK_SECS)

        beat = asyncio.create_task(_heartbeat())
        try:
            await _run_process(
                r"C:\fixed\tool.exe",
                ["--version"],
                env={},
                timeout_secs=5,
            )
        finally:
            stop = True
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)

        assert record.calls == 1
        assert ticks >= _MIN_TICKS, (
            f"the caller's loop advanced only {ticks} times across a "
            f"{_BLOCK_SECS}s spawn -- it was held by CreateProcess"
        )

    @pytest.mark.asyncio
    async def test_body_result_is_returned_through_the_hop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The private loop is a transport detail: the caller sees the body's result."""
        record = _SpawnRecord()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _blocking_spawn(record, block=False))
        _as_windows(monkeypatch)

        result = await _run_process(
            r"C:\fixed\tool.exe",
            ["--version"],
            env={},
            timeout_secs=1,
        )

        assert result.ok is False
        assert "fake spawn refused" in result.error
        assert result.sandbox_failure is None

    @pytest.mark.asyncio
    async def test_marker_is_what_stops_the_recursion(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Called with the marker already set, the body runs HERE and does not hop."""
        record = _SpawnRecord()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _blocking_spawn(record, block=False))
        _as_windows(monkeypatch)

        hops = 0
        real_hop = prerequisite_module._run_on_private_loop

        async def _counting_hop(make_coro: Any) -> Any:
            nonlocal hops
            hops += 1
            return await real_hop(make_coro)

        monkeypatch.setattr(prerequisite_module, "_run_on_private_loop", _counting_hop)

        caller_loop_id = id(asyncio.get_running_loop())
        await _run_process(
            r"C:\fixed\tool.exe",
            ["--version"],
            env={},
            timeout_secs=1,
            _on_private_loop=True,
        )

        assert hops == 0
        assert record.loop_ids == [caller_loop_id]


class TestPosixIsUnchanged:
    @pytest.mark.asyncio
    async def test_posix_does_not_hop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No thread hop where there was never a stall.

        Asserted on the hop helper rather than on the spawn's loop id: the POSIX
        body runs the sandbox prelude first and may legitimately return before it
        ever spawns, and either way it must not have hopped.
        """
        hops = 0

        async def _counting_hop(_make_coro: Any) -> Any:
            nonlocal hops
            hops += 1
            raise AssertionError("POSIX must not offload the spawn to a private loop")

        monkeypatch.setattr(prerequisite_module, "_run_on_private_loop", _counting_hop)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)

        await _run_process("/usr/bin/tool", ["--version"], env={}, timeout_secs=1)

        assert hops == 0


class TestOffloadPoolIsolation:
    def test_hop_pool_is_not_the_shared_subprocess_pool(self) -> None:
        """Sharing one pool would be a deadlock, not just contention.

        The offloaded loop awaits
        ``platform_compat.descendant_termination_handles_async``, which submits
        into ``subprocess_executor()``. If the hop ran there too, concurrent
        spawns could occupy every worker while each waits on a scan queued behind
        them.
        """
        assert kiro_spawn_executor() is not subprocess_executor()
        assert kiro_spawn_executor()._thread_name_prefix == "mc-kirospawn"

    def test_pool_is_bounded(self) -> None:
        # A started run_in_executor future cannot be cancelled, so this caps how
        # many wedged CreateProcess calls hold threads.
        assert 0 < kiro_spawn_executor()._max_workers <= 4

    @pytest.mark.asyncio
    async def test_worker_is_left_with_no_bound_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pooled thread is reused, so a leaked loop would break the next spawn.

        ``asyncio.Runner.close`` unbinds and closes the loop it made. Without
        that, the second probe on the same worker fails with "Event loop is
        closed" -- which on Windows is every probe after the first.
        """
        record = _SpawnRecord()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _blocking_spawn(record, block=False))
        _as_windows(monkeypatch)

        bound: list[object] = []

        def _read_binding() -> None:
            try:
                bound.append(asyncio.get_event_loop_policy().get_event_loop())
            except RuntimeError:
                bound.append(None)

        caller_loop_id = id(asyncio.get_running_loop())
        for _ in range(3):
            result = await _run_process(r"C:\fixed\tool.exe", ["--version"], env={}, timeout_secs=1)
            assert result.ok is False

        # Three spawns actually happened -- a leaked, already-closed loop would
        # have raised "Event loop is closed" instead of reaching the fake.
        assert record.calls == 3
        # None of them on the caller's loop. Deliberately NOT asserting three
        # DISTINCT loop ids: CPython reuses the address of a freed object, so
        # sequential loops legitimately repeat an id.
        assert caller_loop_id not in record.loop_ids

        await asyncio.get_running_loop().run_in_executor(kiro_spawn_executor(), _read_binding)
        assert bound == [None], "a loop is still bound to the pooled worker thread"


class TestWhyNotToThread:
    @pytest.mark.asyncio
    async def test_the_naive_to_thread_patch_would_spawn_nothing(self) -> None:
        """Pins the trap the fix had to avoid, against the live stdlib.

        Handing ``asyncio.create_subprocess_exec`` itself to ``to_thread`` looks
        like an offload and is not one: the worker thread returns an unawaited
        coroutine, so no child is created there, and awaiting it back on the
        gateway loop performs the very ``CreateProcess`` that stalls it.
        """
        handed_back = await asyncio.to_thread(asyncio.create_subprocess_exec, "/bin/true")
        try:
            assert inspect.iscoroutine(
                handed_back
            ), "create_subprocess_exec is a coroutine function; a thread cannot run it"
        finally:
            handed_back.close()

"""How a Default start, a level write and the cold-start queue order themselves.

A Default start takes its work directory's overlay fence only once it holds a
cold-start permit, and waits for that directory's writes holding neither, so a
pick is never held off by a start that is only queued and a start waiting on a
fence never holds a gateway-wide admission slot.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from effort_helpers import _observe_directory_wait
from test_effort import _await_test

from kiro_crew.acp.runtime import _ColdStartAdmission
from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.providers import acp as acp_provider
from kiro_crew.providers.acp import _write_cli_overlay
from kiro_crew.start_priority import START_QUEUE_ADMISSION, StartPriority
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.usefixtures("no_zero_tool_check")


class TestFenceArrivalOrder:
    """Windows and writes on one fence are served in arrival order."""

    _UNBLOCKED_DEADLINE_SECS = 10.0

    @pytest.mark.asyncio
    async def test_a_window_queued_after_a_write_waits_for_it(self, tmp_path):
        fence = acp_provider._workspace_effort_overlay_fence(
            os.path.realpath(tmp_path / ".kiro" / "settings")
        )
        assert await fence.acquire("window", 0.1) is True
        write_task = asyncio.create_task(fence.acquire("write", self._UNBLOCKED_DEADLINE_SECS))
        await asyncio.sleep(0)
        late_window = asyncio.create_task(fence.acquire("window", self._UNBLOCKED_DEADLINE_SECS))
        await asyncio.sleep(0)
        assert not write_task.done()
        assert not late_window.done()

        fence.release("window")
        assert await _await_test(write_task, "write_task") is True
        assert not late_window.done(), "the late window overtook the queued write"

        fence.release("write")
        assert await _await_test(late_window, "late_window") is True
        fence.release("window")

    @pytest.mark.asyncio
    async def test_window_and_write_queued_behind_a_held_write_resolve_in_order(self, tmp_path):
        # Held write, then a window, then a write: the second write waits for the
        # window, so the window must not wait for it.
        fence = acp_provider._workspace_effort_overlay_fence(
            os.path.realpath(tmp_path / ".kiro" / "settings")
        )
        order: list[str] = []

        async def take(mode: str) -> None:
            assert await fence.acquire(mode, self._UNBLOCKED_DEADLINE_SECS) is True
            order.append(mode)
            await asyncio.sleep(0)
            fence.release(mode)

        assert await fence.acquire("write", 0.1) is True
        window_task = asyncio.create_task(take("window"))
        await asyncio.sleep(0)
        write_task = asyncio.create_task(take("write"))
        await asyncio.sleep(0)
        fence.release("write")

        await asyncio.wait_for(
            asyncio.gather(window_task, write_task), self._UNBLOCKED_DEADLINE_SECS
        )
        assert order == ["window", "write"]

    @pytest.mark.asyncio
    async def test_a_waiter_that_times_out_stops_blocking_a_later_one(self, tmp_path):
        fence = acp_provider._workspace_effort_overlay_fence(
            os.path.realpath(tmp_path / ".kiro" / "settings")
        )
        assert await fence.acquire("window", 0.1) is True
        write_task = asyncio.create_task(fence.acquire("write", 0.01))
        await asyncio.sleep(0)
        assert not write_task.done()

        assert await _await_test(write_task, "write_task") is False

        assert await fence.acquire("window", 0.01) is True
        fence.release("window")
        fence.release("window")


class TestDefaultStartAdmittedWindow:
    """A Default start holds the work-directory fence and its permit in one order only.

    It waits for the directory holding nothing, queues for its cold-start permit
    holding nothing on the fence, and takes the fence only with the permit in
    hand. Each test here pins one half of that: no permit held while it waits,
    and no fence registration held while it queues.
    """

    _MODEL = "claude-opus-4.7"
    _DEADLINE_SECS = 10.0

    @staticmethod
    def _provider(work_dir):
        from kiro_crew.acp.types import ACP_BACKEND_KIRO
        from kiro_crew.providers import acp as acp_provider

        return acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=work_dir,
            model=TestDefaultStartAdmittedWindow._MODEL,
        )

    @staticmethod
    def _fence(work_dir):
        from kiro_crew.providers import acp as acp_provider

        return acp_provider._workspace_effort_overlay_fence(
            os.path.realpath(Path(work_dir) / ".kiro" / "settings")
        )

    @staticmethod
    def _width_one_admission(monkeypatch):
        from kiro_crew.acp import runtime_start as acp_runtime_start_module

        admission = _ColdStartAdmission(limit=1)
        monkeypatch.setattr(acp_runtime_start_module, "_cold_start_admission", lambda: admission)
        return admission

    @pytest.mark.asyncio
    async def test_a_start_waiting_for_a_write_holds_no_admission_permit(
        self, tmp_path, monkeypatch
    ):
        # A start waiting out a fence here holds no cold-start permit, so cold
        # starts in projects that share nothing with this directory never queue
        # behind it.
        from kiro_crew.providers.acp import _write_cli_overlay

        _write_cli_overlay(tmp_path, self._MODEL, "max")
        admission = self._width_one_admission(monkeypatch)
        waiting = _observe_directory_wait(monkeypatch)
        fence = self._fence(tmp_path)
        assert await fence.acquire("write", 0.1) is True

        provider = self._provider(tmp_path)
        entered_start = asyncio.Event()

        async def runtime_start_stub():
            entered_start.set()

        monkeypatch.setattr(provider, "_start_kiro_runtime", runtime_start_stub)
        start_task = asyncio.create_task(provider.start())
        try:
            await await_until(waiting, what="the start waiting for the directory")
            assert admission.active == 0
            assert not entered_start.is_set()

            # The only permit is free while the start waits, so an unrelated cold
            # start is admitted at once.
            await await_until(
                asyncio.ensure_future(admission.acquire(StartPriority.FOREGROUND)),
                what="an unrelated cold start admitted while the start waits",
            )
            admission.release(StartPriority.FOREGROUND)

            fence.release("write")
            await asyncio.wait_for(entered_start.wait(), self._DEADLINE_SECS)
        finally:
            await asyncio.wait_for(start_task, self._DEADLINE_SECS)
        assert admission.active == 0

    @pytest.mark.asyncio
    async def test_a_write_arriving_while_the_start_queues_gets_the_permit_back(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.providers.acp import _write_cli_overlay

        _write_cli_overlay(tmp_path, self._MODEL, "max")
        admission = self._width_one_admission(monkeypatch)
        fence = self._fence(tmp_path)
        await admission.acquire(StartPriority.FOREGROUND)

        provider = self._provider(tmp_path)
        entered_start = asyncio.Event()

        async def runtime_start_stub():
            entered_start.set()

        monkeypatch.setattr(provider, "_start_kiro_runtime", runtime_start_stub)
        start_task = asyncio.create_task(provider.start())
        try:
            # Interval polling, not loop turns: the start resolves its fence key
            # in a worker thread before it reaches the queue.
            await async_wait_until(lambda: admission.queued == 1, timeout=self._DEADLINE_SECS)
            # The write arrives while the start is only queued, so nothing holds
            # it off.
            assert await fence.acquire("write", 0.1) is True

            admission.release(StartPriority.FOREGROUND)
            # The start is granted the permit, finds the write, and hands the
            # permit straight back rather than waiting on the fence with it.
            await async_wait_until(
                lambda: admission.active == 0 and admission.queued == 0,
                timeout=self._DEADLINE_SECS,
            )
            assert not entered_start.is_set()

            fence.release("write")
            await asyncio.wait_for(entered_start.wait(), self._DEADLINE_SECS)
        finally:
            await asyncio.wait_for(start_task, self._DEADLINE_SECS)
        assert admission.active == 0

    @pytest.mark.asyncio
    async def test_a_task_started_inside_the_window_spawns_on_its_own_permit(
        self, tmp_path, monkeypatch
    ):
        # A task created under the window inherits the ContextVar's context, so
        # without the owner-task check it would spawn on this start's permit.
        from types import SimpleNamespace

        from kiro_crew.acp import runtime_spawn_entry
        from kiro_crew.acp import runtime_start as acp_runtime_start_module
        from kiro_crew.acp.types import ACP_BACKEND_KIRO
        from kiro_crew.providers.acp import _write_cli_overlay

        _write_cli_overlay(tmp_path, self._MODEL, "max")
        admission = _ColdStartAdmission(limit=2)
        monkeypatch.setattr(acp_runtime_start_module, "_cold_start_admission", lambda: admission)
        active_inside_child: list[int] = []

        async def child_spawn_admitted(runtime):
            active_inside_child.append(admission.active)

        runtime = SimpleNamespace(
            _process=None,
            _process_tree_confirmed_dead=False,
            _acp_backend=ACP_BACKEND_KIRO,
        )
        runtime._spawn_admitted_rederiving_once = lambda: child_spawn_admitted(runtime)

        active_after_child: list[int] = []

        async def runtime_start_stub():
            child = asyncio.create_task(
                runtime_spawn_entry.spawn(runtime, start_priority=StartPriority.BACKGROUND)
            )
            await asyncio.wait_for(child, self._DEADLINE_SECS)
            active_after_child.append(admission.active)

        provider = self._provider(tmp_path)
        monkeypatch.setattr(provider, "_start_kiro_runtime", runtime_start_stub)

        await asyncio.wait_for(provider.start(), self._DEADLINE_SECS)

        # Two permits held at once: the start's own and the one the child task
        # queued for. One permit would mean the child ran on the start's.
        assert active_inside_child == [2]
        # The child released only its own, so the start still holds the permit it
        # hands to its first real spawn.
        assert active_after_child == [1]
        assert admission.active == 0

    @pytest.mark.asyncio
    async def test_the_fence_ceiling_does_not_count_the_admission_queue_wait(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.providers import acp as acp_provider
        from kiro_crew.providers.acp import _write_cli_overlay

        _write_cli_overlay(tmp_path, self._MODEL, "max")
        admission = self._width_one_admission(monkeypatch)
        ceiling_secs = 0.05
        monkeypatch.setattr(
            acp_provider, "_WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS", ceiling_secs
        )
        await admission.acquire(StartPriority.FOREGROUND)

        provider = self._provider(tmp_path)
        entered_start = asyncio.Event()

        async def runtime_start_stub():
            entered_start.set()

        monkeypatch.setattr(provider, "_start_kiro_runtime", runtime_start_stub)
        start_task = asyncio.create_task(provider.start())
        try:
            await async_wait_until(lambda: admission.queued == 1, timeout=self._DEADLINE_SECS)
            # The queue wait has to outlast the fence ceiling for this to prove
            # anything, so the clock is the condition here, not a synchronization
            # device: no write exists in this directory, so the only thing the
            # start can be waiting for is its permit.
            loop = asyncio.get_running_loop()
            queued_at = loop.time()
            await async_wait_until(
                lambda: loop.time() - queued_at > ceiling_secs * 4,
                timeout=self._DEADLINE_SECS,
            )
            assert not entered_start.is_set()

            admission.release(StartPriority.FOREGROUND)
            await asyncio.wait_for(entered_start.wait(), self._DEADLINE_SECS)
        finally:
            await asyncio.wait_for(start_task, self._DEADLINE_SECS)
        assert admission.active == 0

    @pytest.mark.asyncio
    async def test_a_start_queued_on_cold_start_admission_holds_no_window(
        self, tmp_path, monkeypatch
    ):
        # A start that is only queued for a cold-start permit holds no
        # work-directory window, so a level pick in that directory is not held off
        # by a start that has not begun.
        from kiro_crew.acp import runtime_start as acp_runtime_start_module
        from kiro_crew.start_priority import StartPriority

        _write_cli_overlay(tmp_path, self._MODEL, "max")
        admission = acp_runtime_start_module._cold_start_admission()
        for _ in range(admission.semaphore.limit):
            await admission.acquire(StartPriority.FOREGROUND)

        provider = self._provider(tmp_path)
        entered_start = asyncio.Event()

        async def runtime_start_stub():
            entered_start.set()

        monkeypatch.setattr(provider, "_start_kiro_runtime", runtime_start_stub)
        start_task = asyncio.create_task(provider.start())
        try:
            await async_wait_until(lambda: admission.queued == 1, timeout=self._DEADLINE_SECS)
            assert not entered_start.is_set()

            fence = acp_provider._workspace_effort_overlay_fence(
                os.path.realpath(tmp_path / ".kiro" / "settings")
            )
            # The start is queued for admission, so it holds no window and a
            # gateway write gets straight through.
            assert await fence.acquire("write", 0.1) is True
            fence.release("write")

            admission.release(StartPriority.FOREGROUND)
            await asyncio.wait_for(entered_start.wait(), self._DEADLINE_SECS)
        finally:
            await _await_test(start_task, "start_task")
            for _ in range(admission.semaphore.limit - 1):
                admission.release(StartPriority.FOREGROUND)

    @pytest.mark.asyncio
    async def test_a_start_whose_stub_never_spawns_returns_its_admission_permit(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.acp import runtime_start as acp_runtime_start_module

        _write_cli_overlay(tmp_path, self._MODEL, "max")
        admission = acp_runtime_start_module._cold_start_admission()
        provider = self._provider(tmp_path)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())

        await asyncio.wait_for(provider.start(), self._DEADLINE_SECS)

        assert admission.active == 0

    @pytest.mark.asyncio
    async def test_a_window_wait_that_times_out_returns_its_admission_permit(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.acp import runtime_start as acp_runtime_start_module

        _write_cli_overlay(tmp_path, self._MODEL, "max")
        admission = acp_runtime_start_module._cold_start_admission()
        provider = self._provider(tmp_path)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        fence = acp_provider._workspace_effort_overlay_fence(
            os.path.realpath(tmp_path / ".kiro" / "settings")
        )
        assert await fence.acquire("write", 0.1) is True
        monkeypatch.setattr(
            acp_provider,
            "_WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS",
            0.01,
        )

        with pytest.raises(RuntimeError, match="fence wait timed out"):
            await asyncio.wait_for(provider.start(), self._DEADLINE_SECS)

        assert admission.active == 0
        fence.release("write")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spawn_runtime", [True, False], ids=["spawn", "no-spawn"])
    async def test_handed_down_grant_precedes_start_work_and_is_reported_once(
        self, tmp_path, monkeypatch, spawn_runtime
    ):
        from kiro_crew.acp import runtime_spawn_entry

        _write_cli_overlay(tmp_path, self._MODEL, "max")
        admission = self._width_one_admission(monkeypatch)
        events: list[object] = []

        def queued(queue):
            events.append(("queued", queue))

        def acquired(wait_ms, queue):
            events.append(("acquired", wait_ms, queue))

        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model=self._MODEL,
            on_gate_queued=queued,
            on_gate_acquired=acquired,
        )
        monkeypatch.setattr(
            provider, "_apply_effort_overlay", lambda: events.append("effort-overlay")
        )
        monkeypatch.setattr(
            provider, "_apply_tool_search_overlay", lambda: events.append("tool-search")
        )

        runtime = SimpleNamespace(
            _process=None,
            _process_tree_confirmed_dead=False,
            _acp_backend=ACP_BACKEND_KIRO,
        )

        async def spawned():
            events.append("spawned")

        runtime._spawn_admitted_rederiving_once = spawned

        async def runtime_start_stub():
            events.append("start-runtime")
            if spawn_runtime:
                await runtime_spawn_entry.spawn(
                    runtime,
                    start_priority=StartPriority.BACKGROUND,
                    on_gate_queued=queued,
                    on_gate_acquired=acquired,
                )

        monkeypatch.setattr(provider, "_start_kiro_runtime", runtime_start_stub)

        await asyncio.wait_for(provider.start(), self._DEADLINE_SECS)

        gate_events = [event for event in events if isinstance(event, tuple)]
        assert len(gate_events) == 2
        assert gate_events[0] == ("queued", START_QUEUE_ADMISSION)
        assert gate_events[1][0] == "acquired"
        assert gate_events[1][2] == START_QUEUE_ADMISSION
        assert events.index(gate_events[1]) < events.index("effort-overlay")
        assert events.index(gate_events[1]) < events.index("tool-search")
        assert events.index(gate_events[1]) < events.index("start-runtime")
        assert events.count("spawned") == int(spawn_runtime)
        assert admission.active == 0

    @pytest.mark.asyncio
    async def test_the_first_spawn_of_a_handed_down_start_does_not_queue_again(self):
        # With every other permit held, a spawn that acquired its own would wait
        # for one that this start is already holding, so this bounded wait is the
        # proof: it completes only because the permit was handed down.
        from kiro_crew.acp import runtime_spawn_entry
        from kiro_crew.acp import runtime_start as acp_runtime_start_module
        from kiro_crew.start_priority import StartPriority

        admission = acp_runtime_start_module._cold_start_admission()
        for _ in range(admission.semaphore.limit - 1):
            await admission.acquire(StartPriority.FOREGROUND)

        spawned = asyncio.Event()

        async def spawn_admitted(runtime):
            spawned.set()

        runtime = SimpleNamespace(
            _process=None,
            _process_tree_confirmed_dead=False,
            _acp_backend=ACP_BACKEND_KIRO,
        )
        runtime._spawn_admitted_rederiving_once = lambda: spawn_admitted(runtime)

        acquired_wait_ms: list[float] = []
        async with runtime_spawn_entry.handed_down_cold_start_admission(
            StartPriority.BACKGROUND,
            on_gate_acquired=lambda wait_ms, queue: acquired_wait_ms.append(wait_ms),
        ) as permit:
            assert admission.active == admission.semaphore.limit
            await asyncio.wait_for(
                runtime_spawn_entry.spawn(
                    runtime,
                    start_priority=StartPriority.BACKGROUND,
                    on_gate_acquired=lambda wait_ms, queue: acquired_wait_ms.append(wait_ms),
                ),
                self._DEADLINE_SECS,
            )
            assert spawned.is_set()
            # The lifecycle reported the grant once, and the spawn consumed
            # and released the permit without reporting it again.
            assert acquired_wait_ms == [permit.wait_ms]
            assert admission.active == admission.semaphore.limit - 1

        assert admission.active == admission.semaphore.limit - 1
        for _ in range(admission.semaphore.limit - 1):
            admission.release(StartPriority.FOREGROUND)
        assert admission.active == 0

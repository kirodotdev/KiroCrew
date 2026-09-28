"""Characterize the coordinator boundaries shared by run and reap paths."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.subagent as subagent_module
from kiro_crew.subagent import SubagentInfo, SubagentManager

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


class _ControlledSessions:
    """Session double whose reset boundary is controlled by the test."""

    def __init__(self) -> None:
        self.reset_entered = asyncio.Event()
        self.allow_reset = asyncio.Event()
        self.reset_calls: list[str] = []
        self.release_calls: list[tuple[str, bool]] = []

    def set_continuable_fallback(self, _fallback) -> None:
        pass

    async def reset(self, session_key: str) -> None:
        self.reset_calls.append(session_key)
        self.reset_entered.set()
        await self.allow_reset.wait()

    def release(self, session_key: str, *, cleanup: bool = True) -> None:
        self.release_calls.append((session_key, cleanup))


@pytest.mark.asyncio
async def test_real_run_force_reap_race_reports_once_and_releases_slot_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reap owns each terminal concern once, even if its caller is cancelled."""
    sessions = _ControlledSessions()
    report_entered = asyncio.Event()
    allow_report = asyncio.Event()
    run_entered = asyncio.Event()
    keep_running = asyncio.Event()
    stats = MagicMock()

    async def on_done(_info: SubagentInfo) -> None:
        report_entered.set()
        await allow_report.wait()

    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=MagicMock(),
        on_done=on_done,
        max_concurrent=1,
    )
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    manager._record_cost = MagicMock()
    monkeypatch.setattr(subagent_module, "Stats", lambda: stats)
    monkeypatch.setattr(subagent_module, "sel", lambda: MagicMock())

    info = SubagentInfo(
        id="race0001",
        task="characterize terminal arbitration",
        agent="",
        started=time.time() - 5,
    )
    info._session_sharing = False
    manager._agents[info.id] = info
    manager._running_count = 1

    async def controlled_run_inner(_info: SubagentInfo, _session_key: str) -> None:
        run_entered.set()
        await keep_running.wait()

    monkeypatch.setattr(manager, "_run_inner", controlled_run_inner)

    run_task = asyncio.create_task(manager._run(info))
    manager._tasks[info.id] = run_task
    reap_task: asyncio.Task | None = None
    report_tasks: list[asyncio.Task] = []

    try:
        await asyncio.wait_for(run_entered.wait(), timeout=2)
        reap_task = asyncio.create_task(
            manager._force_reap(info.id, info, elapsed=5.0, reason="deadline")
        )
        await asyncio.wait_for(sessions.reset_entered.wait(), timeout=2)

        sessions.allow_reset.set()
        await asyncio.wait_for(report_entered.wait(), timeout=2)
        report_tasks = list(manager._report_tasks)
        assert len(report_tasks) == 1

        # The finalize claim is already consumed, but cancellation of its caller
        # must not cancel the independently owned terminal delivery.
        reap_task.cancel()
        await asyncio.gather(reap_task, return_exceptions=True)
        assert not report_tasks[0].done()

        allow_report.set()
        await asyncio.wait_for(
            asyncio.gather(run_task, *report_tasks, return_exceptions=True),
            timeout=2,
        )

        done_events = [
            call
            for call in manager._fire_event.await_args_list
            if call.args and call.args[0] == "subagent_done"
        ]
        assert len(done_events) == 1
        assert info._reported_to_parent is True
        assert info.done is True
        assert info.reaped is True
        assert info._finalized is True
        assert info._slot_released is True
        assert manager._running_count == 0
        assert info.id not in manager._tasks
        manager._write_tombstone.assert_called_once_with(info, "deadline")
        manager._record_cost.assert_called_once_with(info)
        stats.inc_subagent_failed.assert_called_once_with()
        assert sessions.reset_calls == ["subagent:race0001"]
        assert sessions.release_calls == [("subagent:race0001", False)]
    finally:
        sessions.allow_reset.set()
        keep_running.set()
        allow_report.set()
        pending = [run_task, *(report_tasks or list(manager._report_tasks))]
        if reap_task is not None:
            pending.append(reap_task)
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

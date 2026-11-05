"""A slow HEARTBEAT.md task must not hold the heartbeat's upkeep.

A heartbeat task turn may run for ``HEARTBEAT_TASK_TIMEOUT_SECS`` (30 min).
When ``_beat`` awaited it inline, the FTS rebuild, the daily prune and idle
consolidation that share the tick all waited behind it. Task processing is now
its own tracked task: a tick schedules it and moves on, a tick that finds it
still running skips the file, and shutdown cancels and awaits it.

Every test drives a fake task that blocks on an event the test owns, so "slow"
is a state the test holds, not a sleep it hopes is long enough.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.heartbeat import HeartbeatService, heartbeat_path
from kiro_crew.testing.wait import async_wait_until, await_until, finished


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("kiro_crew.heartbeat.workspace_dir", lambda: tmp_path / "ws")
    ws = tmp_path / "ws"
    ws.mkdir()
    heartbeat_path().write_text("# Heartbeat Tasks\n\n- slow zibble\n", encoding="utf-8")
    # The loop's default executor is enough for MagicMock maintenance calls.
    monkeypatch.setattr("kiro_crew.heartbeat.maintenance_executor", lambda: None)
    return ws


class _SlowTask:
    """An ``on_task`` that blocks until released, recording each call."""

    def __init__(self, reply: str | None = None) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._reply = reply

    async def __call__(self, text: str, deliver: str) -> str | None:
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return self._reply


def _memory() -> Any:
    mem = MagicMock()
    mem.rebuild_index.return_value = 3
    return mem


def _service(mem: Any, slow: Any, **kwargs: Any) -> HeartbeatService:
    svc = HeartbeatService(mem, on_task=slow, **kwargs)
    svc._memory_backup_started = True  # keep the real backup pass out of the tick
    return svc


async def _beat_without_waiting_for(svc: HeartbeatService, slow: _SlowTask) -> None:
    """Run one tick and require it to return while ``slow`` is still blocked.

    The tick is its own task so a tick that awaits the task pass fails here at
    once, by name, instead of hanging until the lost-run bound.
    """
    beat = asyncio.create_task(svc._beat())
    try:
        await await_until(slow.started, what="the slow task starting")
        assert beat.done(), "the tick is still waiting on the HEARTBEAT.md pass"
        await beat
    finally:
        if not beat.done():
            beat.cancel()


@pytest.mark.asyncio
async def test_upkeep_runs_while_a_task_is_still_running(workspace: Path) -> None:
    slow = _SlowTask()
    mem = _memory()
    consolidator = MagicMock()
    svc = _service(mem, slow, consolidator=consolidator)
    svc._tick = 1
    await _beat_without_waiting_for(svc, slow)

    svc._tick = 15
    await finished(svc._beat(), "tick 15 returning while the task still runs")

    assert not slow.release.is_set()
    assert mem.rebuild_index.call_count == 1
    assert consolidator.check_idle_sessions.call_count == 2

    slow.release.set()
    assert svc._process_task is not None
    await await_until(svc._process_task, what="the task pass finishing")


@pytest.mark.asyncio
async def test_ticks_do_not_overlap_a_running_pass(workspace: Path) -> None:
    slow = _SlowTask(reply="HEARTBEAT_KEEP")
    svc = _service(_memory(), slow)
    svc._tick = 1
    await _beat_without_waiting_for(svc, slow)
    first = svc._process_task

    for tick in (2, 3, 4):
        svc._tick = tick
        await svc._beat()
    assert svc._process_task is first
    assert slow.calls == 1

    slow.release.set()
    assert first is not None
    await await_until(first, what="the first pass finishing")

    # The pass kept the task (HEARTBEAT_KEEP), so the next tick runs it again.
    svc._tick = 5
    await svc._beat()
    assert svc._process_task is not first
    assert svc._process_task is not None
    await await_until(svc._process_task, what="the second pass finishing")
    assert slow.calls == 2


@pytest.mark.asyncio
async def test_the_loop_keeps_ticking_through_a_slow_task(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real ``_loop``: the FTS rebuild lands on tick 15 mid-task."""
    event = asyncio.Event()
    monkeypatch.setattr("kiro_crew.heartbeat.shutdown_event", event)
    slow = _SlowTask()
    mem = _memory()
    svc = _service(mem, slow, interval=0.001)
    loop_task = asyncio.create_task(svc._loop())
    svc._task = loop_task  # so stop() cancels it and wait_closed() awaits it
    try:
        await await_until(slow.started, what="the slow task starting")
        await async_wait_until(
            lambda: mem.rebuild_index.call_count >= 1,
            describe=lambda: f"tick={svc._tick}",
        )
        assert svc._tick >= 15
        assert slow.calls == 1
        assert not slow.release.is_set()
    finally:
        event.set()
        svc.stop()
    await finished(svc.wait_closed(), "heartbeat shutdown")
    assert loop_task.done()


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_the_running_pass(workspace: Path) -> None:
    slow = _SlowTask()
    cycle_ends: list[int] = []

    async def _on_cycle_end() -> None:
        cycle_ends.append(1)

    svc = _service(_memory(), slow, on_cycle_end=_on_cycle_end, interval=3600)
    await svc.start()
    svc._tick = 1
    await _beat_without_waiting_for(svc, slow)
    process_task = svc._process_task
    loop_task = svc._task
    assert process_task is not None and loop_task is not None

    svc.stop()
    await finished(svc.wait_closed(), "heartbeat shutdown")

    assert slow.cancelled.is_set()
    assert process_task.done()
    assert loop_task.done()
    # The pass's finally still ran its cycle-end recycle before shutdown moved on.
    assert cycle_ends == [1]
    # A cancelled pass rewrites nothing: the task stays for the next start.
    assert "slow zibble" in heartbeat_path().read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_failing_pass_is_logged_not_raised(
    workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    svc = _service(_memory(), _SlowTask())

    async def _boom() -> None:
        raise RuntimeError("zibble")

    svc._process_heartbeat_file = _boom  # type: ignore[method-assign]
    svc._tick = 1
    await svc._beat()
    assert svc._process_task is not None
    await await_until(svc._process_task, what="the failing pass finishing")
    assert "Heartbeat task processing failed" in caplog.text


@pytest.mark.asyncio
async def test_wait_closed_without_start_is_a_no_op() -> None:
    svc = HeartbeatService(_memory())
    svc.stop()
    await finished(svc.wait_closed(), "wait_closed with nothing running")

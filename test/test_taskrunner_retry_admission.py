"""Retry admission preserves old results unless its snapshot is acknowledged."""

import asyncio
import json
import threading
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_workflows_snapshot_commit import _fail_io, _private, _runner
from test_workflows_snapshot_commit import world as _world

from kiro_crew import atomic_write as atomic
from kiro_crew.task_models import Task, TaskStatus
from kiro_crew.workflow_memory import TaskSnapshotError

world = _world


async def _seed(world, tmp_path):
    runner = _runner(world, tmp_path / "tasks")
    run = await _private(world, "retry")
    run.name = "retry by name"
    run.status = "failed"
    run.error = "prior run error"
    run.started_at, run.finished_at, run.last_task_time = 11.0, 22.0, 21.0
    run.tasks = [
        Task(
            index=1,
            title="keep",
            description="",
            status=TaskStatus.PASSED,
            result="completed result",
            error="retained note",
            attempts=1,
        ),
        Task(
            index=2,
            title="retry",
            description="",
            status=TaskStatus.FAILED,
            result="partial result",
            error="step error",
            attempts=3,
        ),
        Task(
            index=3,
            title="later",
            description="",
            status=TaskStatus.SKIPPED,
            result="skipped result",
            error="dependency",
            attempts=2,
        ),
    ]
    runner._runs[run.task_id] = run
    runner._agent = "old-agent"
    await runner._apersist_runs()
    return runner, run


def _assert_retained(runner, run, before, tasks):
    assert runner._runs[run.task_id] is run
    assert all(current is prior for current, prior in zip(run.tasks, tasks))
    assert asdict(run) == before
    assert runner._agent == "old-agent"
    assert runner._tasks == {}
    assert runner._start_ids_in_flight == set()


async def _assert_later_persist_retains(runner, world, expected):
    await runner._apersist_runs()
    restored = await asyncio.to_thread(_runner, world, runner._work_dir)
    assert json.loads(restored._serialize_runs()) == expected
    visible = await asyncio.to_thread(runner._runs_path().read_text, encoding="utf-8")
    assert "partial result" in visible
    assert "completed result" in visible


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["write", "replace"])
async def test_retry_snapshot_failure_restores_every_field(world, tmp_path, monkeypatch, phase):
    runner, run = await _seed(world, tmp_path)
    before, tasks = asdict(run), list(run.tasks)
    expected = json.loads(runner._serialize_runs())
    target = runner._runs_path()
    with monkeypatch.context() as patch:
        calls = _fail_io(patch, target, phase)
        with pytest.raises(TaskSnapshotError):
            await runner.retry_from_task(run.name, 2, agent="new-agent")
        assert calls, "Did not reach the real snapshot writer"
    restored = await asyncio.to_thread(_runner, world, runner._work_dir)
    assert json.loads(restored._serialize_runs()) == expected
    _assert_retained(runner, run, before, tasks)
    await _assert_later_persist_retains(runner, world, expected)


@pytest.mark.asyncio
async def test_retry_history_failure_happens_before_reset_and_snapshot(
    world, tmp_path, monkeypatch
):
    runner, run = await _seed(world, tmp_path)
    before, tasks = asdict(run), list(run.tasks)
    expected = json.loads(runner._serialize_runs())
    written = runner._persist_written
    monkeypatch.setattr(
        runner, "_bound_history_key", AsyncMock(side_effect=OSError("history failed"))
    )
    with pytest.raises(OSError, match="history failed"):
        await runner.retry_from_task(run.task_id, 2, agent="new-agent")
    _assert_retained(runner, run, before, tasks)
    assert runner._persist_written == written
    await _assert_later_persist_retains(runner, world, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("write_fails", [False, True])
async def test_cancelled_retry_drains_writer_then_restores_and_releases(
    world, tmp_path, monkeypatch, write_fails
):
    runner, run = await _seed(world, tmp_path)
    before, tasks = asdict(run), list(run.tasks)
    expected = json.loads(runner._serialize_runs())
    target = runner._runs_path()
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    real_write = atomic._write_all
    waits = []

    def held_failure(fd, data, path):
        if Path(path) == target:
            loop.call_soon_threadsafe(entered.set)
            waits.append(release.wait(5))
            if write_fails:
                raise OSError("held write failed")
        return real_write(fd, data, path)

    with monkeypatch.context() as patch:
        patch.setattr(atomic, "_write_all", held_failure)
        pending = asyncio.create_task(runner.retry_from_task(run.task_id, 2, agent="new-agent"))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            assert run.task_id in runner._start_ids_in_flight
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 3)
    assert waits == [True]
    _assert_retained(runner, run, before, tasks)
    restored = await asyncio.to_thread(_runner, world, runner._work_dir)
    if write_fails:
        assert json.loads(restored._serialize_runs()) == expected
    else:
        assert restored._runs[run.task_id].tasks[1].result == ""
    await _assert_later_persist_retains(runner, world, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("competitor", ["retry", "execute"])
async def test_retry_reserves_before_history_await_and_releases_on_cancel(
    world, tmp_path, monkeypatch, competitor
):
    runner, run = await _seed(world, tmp_path)
    before, tasks = asdict(run), list(run.tasks)
    entered = asyncio.Event()
    release = asyncio.Event()
    real_history = runner._bound_history_key

    async def held_history(*args):
        entered.set()
        await release.wait()
        return await real_history(*args)

    monkeypatch.setattr(runner, "_bound_history_key", held_history)
    pending = asyncio.create_task(runner.retry_from_task(run.task_id, 2))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert asdict(run) == before
        with pytest.raises(ValueError, match="already starting"):
            if competitor == "retry":
                await asyncio.wait_for(runner.retry_from_task(run.name, 2), 3)
            else:
                await asyncio.wait_for(runner.execute_plan(run.task_id), 3)
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
    _assert_retained(runner, run, before, tasks)
    monkeypatch.setattr(runner, "_bound_history_key", real_history)
    await _assert_success(runner, run, monkeypatch, tasks)


async def _assert_success(runner, run, monkeypatch, tasks):
    execute = AsyncMock()
    monkeypatch.setattr(runner, "_execute_tasks", execute)
    monkeypatch.setattr(runner, "_watchdog_loop", AsyncMock())
    task_id = await runner.retry_from_task(run.name, 2, agent="new-agent")
    assert runner._start_ids_in_flight == set()
    assert runner._agent == "new-agent"
    assert run.status == "running"
    assert run.error == "" and run.finished_at == 0.0
    assert run.started_at == run.last_task_time and run.started_at > 22.0
    assert all(current is prior for current, prior in zip(run.tasks, tasks))
    assert run.tasks[0].result == "completed result"
    assert run.tasks[0].status == TaskStatus.PASSED
    for task in run.tasks[1:]:
        assert (task.status, task.error, task.result, task.attempts) == (
            TaskStatus.PENDING,
            "",
            "",
            0,
        )
    await asyncio.wait_for(runner._tasks[task_id], 3)
    execute.assert_awaited_once()
    assert run.status == "completed"
    assert runner._tasks == {}


@pytest.mark.asyncio
async def test_successful_retry_keeps_task_identities(world, tmp_path, monkeypatch):
    runner, run = await _seed(world, tmp_path)
    await _assert_success(runner, run, monkeypatch, list(run.tasks))


@pytest.mark.asyncio
async def test_closed_admission_leaves_retry_unchanged(world, tmp_path, monkeypatch):
    runner, run = await _seed(world, tmp_path)
    before, tasks = asdict(run), list(run.tasks)
    monkeypatch.setattr(runner, "_admission_closed", lambda: True)
    with pytest.raises(ValueError, match="admission is closed"):
        await runner.retry_from_task(run.task_id, 2, agent="new-agent")
    _assert_retained(runner, run, before, tasks)
    monkeypatch.setattr(runner, "_admission_closed", lambda: False)
    await _assert_success(runner, run, monkeypatch, tasks)

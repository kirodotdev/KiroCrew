"""Snapshot failures never acknowledge partially persisted task state."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_workflows_private_execution import world as _world

from kiro_crew import atomic_write as atomic
from kiro_crew.task_models import Project
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.workflow_memory import TaskSnapshotError, WorkflowScope
from kiro_crew.workflows.store import WorkflowRunStore

world = _world


def _runner(world, directory):
    return TaskRunner(sessions=world.sessions, context_builder=world.builder, work_dir=directory)


async def _private(world, task_id):
    scope = await WorkflowScope.admit(f"wf_{task_id}", world.builder, "dashboard:alice")
    await scope.prepare(world.builder, f"taskrunner:{task_id}:runtime")
    return Project(
        spec_path="",
        spec_content="PRIVATE_BODY",
        task_id=task_id,
        status="completed",
        execution_context=scope.execution_context,
    )


def _fail_io(monkeypatch, target, phase):
    calls = []
    real_write, real_replace = atomic._write_all, atomic.os.replace

    def write(fd, data, path):
        if Path(path) == target and phase == "write":
            calls.append(phase)
            raise OSError("injected snapshot write failure")
        return real_write(fd, data, path)

    def replace(src, dst, *args, **kwargs):
        if Path(dst) == target and phase == "replace":
            calls.append(phase)
            raise OSError("injected snapshot replace failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(atomic, "_write_all", write)
    monkeypatch.setattr(atomic.os, "replace", replace)
    return calls


@pytest.mark.asyncio
async def test_workflow_save_refuses_an_unavailable_store(world, tmp_path):
    scope = await WorkflowScope.admit("wf_target", world.builder, "dashboard:alice")
    base = tmp_path / "workflows"
    base.write_text("not a directory", encoding="utf-8")
    store = WorkflowRunStore(base_dir=base)
    row = {
        "run_id": scope.run_id,
        "status": "finished",
        "source": "BODY",
        "execution_context": scope.execution_context.to_record(),
    }
    with pytest.raises(OSError):
        await asyncio.to_thread(store.save, scope.run_id, row)
    assert base.read_text(encoding="utf-8") == "not a directory"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["write", "replace"])
@pytest.mark.parametrize("mutation", ["first", "update", "add", "delete"])
async def test_snapshot_failure_and_retry_are_discoverable(
    world, tmp_path, monkeypatch, phase, mutation
):
    directory = tmp_path / "tasks"
    runner = _runner(world, directory)
    private = await _private(world, "private")
    public = Project(spec_path="", spec_content="PUBLIC_OLD", task_id="public", status="completed")
    runner._runs = {public.task_id: public}
    if mutation != "first":
        runner._runs[private.task_id] = private
    await runner._apersist_runs()
    if mutation in {"first", "add"}:
        new = private if mutation == "first" else await _private(world, "new")
        runner._runs[new.task_id] = new
    elif mutation == "update":
        private.spec_content = "PRIVATE_UPDATED"
    else:
        runner._runs.pop(private.task_id)
    public.spec_content = "PUBLIC_NEW"
    expected = {key: run.spec_content for key, run in runner._runs.items()}
    target = runner._runs_path()
    with monkeypatch.context() as patch:
        calls = _fail_io(patch, target, phase)
        with pytest.raises(OSError):
            await runner._apersist_runs()
        assert calls, "The real atomic write never reached the injected failure"
    restarted = await asyncio.to_thread(_runner, world, directory)
    actual = {key: run.spec_content for key, run in restarted._runs.items()}
    old = {"public": "PUBLIC_OLD"}
    if mutation != "first":
        old["private"] = "PRIVATE_BODY"
    assert actual == old
    await runner._apersist_runs()
    await runner._apersist_runs()
    restarted = await asyncio.to_thread(_runner, world, directory)
    assert {key: run.spec_content for key, run in restarted._runs.items()} == expected
    assert json.loads(runner._runs_path().read_text(encoding="utf-8"))
    assert list(directory.glob("*.tmp")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["write", "replace"])
async def test_background_admission_does_not_acknowledge_failed_write(
    world, tmp_path, monkeypatch, phase
):
    runner = _runner(world, tmp_path / "tasks")
    target = runner._runs_path()
    execute = AsyncMock()
    monkeypatch.setattr(runner, "run", execute)
    with monkeypatch.context() as patch:
        calls = _fail_io(patch, target, phase)
        with pytest.raises(OSError):
            await runner.start_background(
                "__inline__:PRIVATE_BODY", source="text", session_key="dashboard:alice"
            )
        assert calls
    assert runner._runs == {}
    assert runner._tasks == {}
    execute.assert_not_awaited()
    restarted = await asyncio.to_thread(_runner, world, runner._work_dir)
    assert restarted._runs == {}


@pytest.mark.asyncio
async def test_failed_delete_remains_retryable(world, tmp_path, monkeypatch):
    runner = _runner(world, tmp_path / "tasks")
    private = await _private(world, "deleted")
    runner._runs[private.task_id] = private
    await runner._apersist_runs()
    target = runner._runs_path()
    with monkeypatch.context() as patch:
        _fail_io(patch, target, "replace")
        with pytest.raises(OSError):
            await runner.delete_run(private.task_id)
    assert private.task_id in runner._runs
    assert await runner.delete_run(private.task_id)
    assert not await runner.delete_run(private.task_id)
    restarted = await asyncio.to_thread(_runner, world, runner._work_dir)
    assert restarted._runs == {}


@pytest.mark.asyncio
async def test_unreadable_row_blocks_snapshot_replacement(world, tmp_path):
    from kiro_crew.workflow_memory import write_task_snapshot

    runner = _runner(world, tmp_path / "tasks")
    member = await _private(world, "bad")
    runner._runs[member.task_id] = member
    rows = json.loads(runner._serialize_runs())
    rows[0]["task_details"] = [{"index": 1, "status": "passed"}]
    await asyncio.to_thread(write_task_snapshot, runner._runs_path(), json.dumps(rows))
    before = runner._runs_path().read_bytes()
    restarted = await asyncio.to_thread(_runner, world, runner._work_dir)
    assert restarted._snapshot_recovery_incomplete
    restarted._runs["public"] = Project("", "PUBLIC", task_id="public", status="completed")
    with pytest.raises(TaskSnapshotError):
        await restarted._apersist_runs()
    assert runner._runs_path().read_bytes() == before


@pytest.mark.asyncio
async def test_cancelled_write_drains_before_new_snapshot(world, tmp_path, monkeypatch):
    import threading

    runner = _runner(world, tmp_path / "tasks")
    private = await _private(world, "cancelled")
    runner._runs[private.task_id] = private
    target = runner._runs_path()
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    real_write = atomic._write_all
    waits = []

    def held_write(fd, data, path):
        if Path(path) == target and not waits:
            loop.call_soon_threadsafe(entered.set)
            waits.append(release.wait(5))
        return real_write(fd, data, path)

    monkeypatch.setattr(atomic, "_write_all", held_write)
    pending = asyncio.create_task(runner._apersist_runs())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done(), "Cancellation released a live snapshot writer"
        private.spec_content = "PRIVATE_NEW"
        newer = asyncio.create_task(runner._apersist_runs())
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
    await asyncio.wait_for(newer, 3)
    assert waits == [True]
    restarted = await asyncio.to_thread(_runner, world, runner._work_dir)
    assert restarted._runs[private.task_id].spec_content == "PRIVATE_NEW"


@pytest.mark.asyncio
async def test_completion_write_failure_never_publishes_success(
    world, tmp_path, monkeypatch, caplog
):
    from kiro_crew import taskrunner as task_module
    from kiro_crew.task_models import Task

    runner = _runner(world, tmp_path / "tasks")
    private = await _private(world, "terminal")
    private.status = "planned"
    private.tasks = [Task(index=1, title="done", description="")]
    runner._runs[private.task_id] = private
    await runner._apersist_runs()
    monkeypatch.setattr(task_module.git_coord, "init_workspace", AsyncMock())
    monkeypatch.setattr(runner, "_bound_history_key", AsyncMock(return_value="test-history"))
    notify = AsyncMock()
    monkeypatch.setattr(runner, "_notify", notify)
    monkeypatch.setattr(
        runner, "_cleanup_run_sessions", AsyncMock(wraps=runner._cleanup_run_sessions)
    )
    stopped = asyncio.Event()

    async def watchdog(_run):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def execute(_run, _history):
        await asyncio.sleep(0)
        _fail_io(monkeypatch, runner._runs_path(), "replace")

    monkeypatch.setattr(runner, "_watchdog_loop", watchdog)
    monkeypatch.setattr(runner, "_execute_tasks", execute)
    task_id = await runner.execute_plan(private.task_id)
    pending = runner._tasks[task_id]
    observed = asyncio.Event()
    pending.add_done_callback(lambda _task: observed.set())
    try:
        await asyncio.wait_for(observed.wait(), 3)
        assert any(
            record.funcName == "_observe_background_completion"
            and record.levelname == "ERROR"
            and record.exc_info is None
            for record in caplog.records
        ), "The owner must retrieve/report a failed background task before anyone awaits it"
    finally:
        # Observing an exception must not turn a caller's await into success.
        with pytest.raises(OSError):
            await asyncio.wait_for(pending, 3)
    await asyncio.wait_for(stopped.wait(), 3)
    assert task_id not in runner._tasks
    assert all("Task completed" not in call.args[0] for call in notify.await_args_list)
    assert private.status == "failed"


@pytest.mark.asyncio
async def test_late_older_snapshot_cannot_overwrite_failed_newer_attempt(
    world, tmp_path, monkeypatch
):
    runner = _runner(world, tmp_path / "tasks")
    private = await _private(world, "ordered")
    runner._runs[private.task_id] = private
    await runner._apersist_runs()
    private.spec_content = "PRIVATE_OLD_QUEUED"
    older_seq, older = runner._next_persist_seq(), runner._serialize_runs()
    private.spec_content = "PRIVATE_NEWER"
    target = runner._runs_path()
    with monkeypatch.context() as patch:
        _fail_io(patch, target, "replace")
        with pytest.raises(OSError):
            await runner._apersist_runs()
    with pytest.raises(OSError):
        await asyncio.to_thread(runner._commit_snapshot, older_seq, older)
    restarted = await asyncio.to_thread(_runner, world, runner._work_dir)
    expected = "PRIVATE_BODY"
    assert restarted._runs[private.task_id].spec_content == expected
    await runner._apersist_runs()
    await asyncio.to_thread(runner._commit_snapshot, older_seq, older)
    restarted = await asyncio.to_thread(_runner, world, runner._work_dir)
    assert restarted._runs[private.task_id].spec_content == "PRIVATE_NEWER"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["write", "replace"])
async def test_plan_admission_failure_rolls_back(world, tmp_path, monkeypatch, phase):
    from kiro_crew.task_models import Task

    runner = _runner(world, tmp_path / "tasks")
    monkeypatch.setattr(
        runner, "_decompose", AsyncMock(return_value=[Task(index=1, title="plan", description="")])
    )
    target = runner._runs_path()
    with monkeypatch.context() as patch:
        calls = _fail_io(patch, target, phase)
        with pytest.raises(OSError):
            await runner.plan("PRIVATE_PLAN", source="text", session_key="dashboard:alice")
        assert calls
    assert runner._runs == {}
    assert runner._start_ids_in_flight == set()
    restarted = await asyncio.to_thread(_runner, world, runner._work_dir)
    assert restarted._runs == {}


@pytest.mark.asyncio
async def test_failed_retry_admission_does_not_leave_a_running_task(world, tmp_path, monkeypatch):
    runner = _runner(world, tmp_path / "tasks")
    private = await _private(world, "retry")
    private.status = "failed"
    runner._runs[private.task_id] = private
    await runner._apersist_runs()
    _fail_io(monkeypatch, runner._runs_path(), "replace")
    with pytest.raises(OSError):
        await runner.retry_from_task(private.task_id, 1)
    assert private.status == "failed"
    assert runner._tasks == {}
    assert runner._start_ids_in_flight == set()


@pytest.mark.asyncio
async def test_unreadable_snapshot_cannot_be_overwritten(world, tmp_path, monkeypatch):
    runner = _runner(world, tmp_path / "tasks")
    member = await _private(world, "unavailable")
    runner._runs[member.task_id] = member
    await runner._apersist_runs()
    before = runner._runs_path().read_bytes()
    from kiro_crew import workflow_memory

    with monkeypatch.context() as patch:
        patch.setattr(
            workflow_memory,
            "read_task_registry",
            lambda path: (_ for _ in ()).throw(OSError("unreadable")),
        )
        restarted = await asyncio.to_thread(_runner, world, runner._work_dir)
    assert restarted._snapshot_recovery_incomplete
    with pytest.raises(TaskSnapshotError):
        await restarted._apersist_runs()
    assert runner._runs_path().read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_background_completion_observer_preserves_await_outcome(caplog, outcome):
    from kiro_crew.taskrunner import _observe_background_completion

    async def complete():
        if outcome == "failure":
            raise RuntimeError("PRIVATE_TASK_EXCEPTION_SENTINEL")

    task = asyncio.create_task(complete())
    task.add_done_callback(_observe_background_completion)
    observed = asyncio.Event()
    task.add_done_callback(lambda _task: observed.set())
    if outcome == "cancelled":
        task.cancel()
    await asyncio.wait_for(observed.wait(), 3)
    if outcome == "failure":
        with pytest.raises(RuntimeError, match="PRIVATE_TASK_EXCEPTION_SENTINEL"):
            await task
        assert "background completion failed (RuntimeError)" in caplog.text
        assert "PRIVATE_TASK_EXCEPTION_SENTINEL" not in caplog.text
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "background completion failed" not in caplog.text
    else:
        await task
        assert "background completion failed" not in caplog.text

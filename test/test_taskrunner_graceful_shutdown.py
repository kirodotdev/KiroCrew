"""A graceful gateway stop ends an active task run the way a crash does.

The gateway stops memory, the MCP broker and every session while a run's step is
still going, so the step fails. That failure is the shutdown, not the run: once
``TaskRunner.begin_shutdown`` has run, or the stop signal (``shutdown_event``) is
set, a run that is still ``running`` is not re-planned, not marked failed, not
settled in the task queue and not followed by ``git worktree remove``. It is
stored as ``running``, and the restart settles it to ``paused`` with its worktree
kept. A run the operator was cancelling or pausing still ends as before.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from taskrunner_helpers import _make_mock_sessions

from kiro_crew import shutdown_event
from kiro_crew.slack.gateway_runtime import exit_path
from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner


def _runner(work_dir: Path) -> TaskRunner:
    return TaskRunner(sessions=_make_mock_sessions(), auto_test=False, work_dir=work_dir)


def _stored_status(work_dir: Path, task_id: str) -> str:
    rows = json.loads((work_dir / "runs.json").read_text(encoding="utf-8"))
    return next(row["status"] for row in rows if row["task_id"] == task_id)


async def _drive(
    tmp_path: Path,
    step: Callable[[TaskRunner, Any, Any], bool],
    *,
    replan_plan: Callable[[TaskRunner], list[Step]] | None = None,
) -> tuple[TaskRunner, TaskRun, dict[str, AsyncMock]]:
    """Run ``TaskRunner.run`` on a one-step spec whose step body is *step*.

    With *replan_plan*, the real ``_try_replan`` runs and its decomposition
    answers what *replan_plan* returns; otherwise the replan is a spy.
    """
    spec = tmp_path / "TASK.md"
    spec.write_text("# Build something", encoding="utf-8")
    runner = _runner(tmp_path)
    plans: list[Callable[[TaskRunner], list[Step]]] = [
        lambda _runner: [Step(index=1, title="A", description="d")]
    ]
    if replan_plan is not None:
        plans.append(replan_plan)

    async def _decompose(spec_text, work_dir, task_id, agent=""):  # type: ignore[no-untyped-def]
        return plans.pop(0)(runner)

    runner._decompose = _decompose  # type: ignore[method-assign]
    spies = {"replan": AsyncMock(return_value=False), "end_run": AsyncMock()}
    if replan_plan is None:
        runner._try_replan = spies["replan"]  # type: ignore[method-assign]
    runner._taskq_end_run = spies["end_run"]  # type: ignore[method-assign]

    async def _step(run, task, history_key="", session_key=""):  # type: ignore[no-untyped-def]
        return step(runner, run, task)

    runner._execute_single_task = _step  # type: ignore[method-assign]

    async def _init(run):  # type: ignore[no-untyped-def]
        run.branch_name = "task/r1"
        run.worktree_path = str(tmp_path / "wt")

    spies["finalize"] = AsyncMock(return_value="task/r1")
    with (
        patch("kiro_crew.git_coord.init_workspace", _init),
        patch("kiro_crew.git_coord.finalize", spies["finalize"]),
        patch("kiro_crew.git_coord.get_state_summary", AsyncMock(return_value="")),
    ):
        run = await runner.run(spec)
    return runner, run, spies


def _broken_by_the_stop(runner: TaskRunner, run: Any, task: Any) -> bool:
    runner.begin_shutdown()
    task.status = StepStatus.FAILED
    task.error = "Memory startup is stopping. Retry after restart."
    return False


@pytest.mark.asyncio
async def test_a_step_the_shutdown_broke_leaves_the_run_resumable(tmp_path: Path) -> None:
    _runner_, run, spies = await _drive(tmp_path, _broken_by_the_stop)
    spies["replan"].assert_not_awaited()
    spies["end_run"].assert_not_awaited()
    spies["finalize"].assert_not_awaited()
    assert run.status == "running"
    assert _stored_status(tmp_path, run.task_id) == "running"


@pytest.mark.asyncio
async def test_the_stop_signal_alone_leaves_the_run_resumable(tmp_path: Path) -> None:
    def _signalled(runner: TaskRunner, run: Any, task: Any) -> bool:
        shutdown_event.set()
        task.status = StepStatus.FAILED
        task.error = "Shutdown"
        return False

    try:
        _runner_, run, spies = await _drive(tmp_path, _signalled)
    finally:
        shutdown_event.clear()
    spies["replan"].assert_not_awaited()
    spies["end_run"].assert_not_awaited()
    spies["finalize"].assert_not_awaited()
    assert _stored_status(tmp_path, run.task_id) == "running"


def _failed_for_real(runner: TaskRunner, run: Any, task: Any) -> bool:
    task.status = StepStatus.FAILED
    task.error = "boom"
    return False


@pytest.mark.asyncio
async def test_a_stop_during_the_replan_leaves_the_run_resumable(tmp_path: Path) -> None:
    def _replan_cut_short(runner: TaskRunner) -> list[Step]:
        # The stop takes the planning session away, and the planner answers no plan.
        runner.begin_shutdown()
        return []

    _runner_, run, spies = await _drive(tmp_path, _failed_for_real, replan_plan=_replan_cut_short)
    spies["end_run"].assert_not_awaited()
    spies["finalize"].assert_not_awaited()
    assert run.status == "running"
    assert _stored_status(tmp_path, run.task_id) == "running"


@pytest.mark.asyncio
async def test_without_a_stop_an_empty_replan_still_fails_the_run(tmp_path: Path) -> None:
    _runner_, run, spies = await _drive(tmp_path, _failed_for_real, replan_plan=lambda _runner: [])
    assert run.status == "failed"
    assert _stored_status(tmp_path, run.task_id) == "failed"
    spies["end_run"].assert_awaited_once()


@pytest.mark.asyncio
async def test_an_operator_cancel_during_the_stop_still_ends_the_run(tmp_path: Path) -> None:
    def _cancelled(runner: TaskRunner, run: Any, task: Any) -> bool:
        run.status = "cancelling"
        runner.begin_shutdown()
        task.status = StepStatus.PASSED
        return True

    _runner_, run, spies = await _drive(tmp_path, _cancelled)
    assert run.status == "cancelled"
    assert _stored_status(tmp_path, run.task_id) == "cancelled"
    spies["end_run"].assert_awaited_once()
    spies["finalize"].assert_awaited_once()


@pytest.mark.asyncio
async def test_a_cancel_during_the_stop_checkpoint_still_ends_the_run(tmp_path: Path) -> None:
    """The operator cancels while the stop's ``running`` checkpoint is being written."""
    cancelled_once: list[bool] = []

    def _broken_then_cancelled(runner: TaskRunner, run: Any, task: Any) -> bool:
        original = runner._apersist_runs

        async def _persist() -> None:
            if run.status == "running" and runner._stopping() and not cancelled_once:
                cancelled_once.append(True)
                await original()
                run.status = "cancelling"  # what ``cancel()`` sets before it cancels
                raise asyncio.CancelledError
            await original()

        runner._apersist_runs = _persist  # type: ignore[method-assign]
        return _broken_by_the_stop(runner, run, task)

    _runner_, run, spies = await _drive(tmp_path, _broken_then_cancelled)
    assert cancelled_once == [True]
    assert run.status == "cancelled"
    assert _stored_status(tmp_path, run.task_id) == "cancelled"
    spies["end_run"].assert_awaited_once()
    spies["finalize"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("ask", "ended"), [("cancel", "cancelled"), ("pause", "paused")])
async def test_a_stop_after_the_checkpoint_is_settled_when_the_operator_asks(
    tmp_path: Path, ask: str, ended: str
) -> None:
    """The driver already exited at the stop; a later cancel or pause still lands."""
    runner, run, spies = await _drive(tmp_path, _broken_by_the_stop)
    assert _stored_status(tmp_path, run.task_id) == "running"
    with patch("kiro_crew.git_coord.finalize", spies["finalize"]):
        if ask == "cancel":
            runner.cancel(run.task_id, exact=True)
            runner.cancel(run.task_id, exact=True)  # a second request must not undo it
            runner.cancel()  # nor a cancel-all
        else:
            runner.pause(run.task_id)
            runner.cancel(run.task_id, exact=True)  # the pause it already took wins
        await runner._stop_settles[run.task_id]
    assert run.status == ended
    assert _stored_status(tmp_path, run.task_id) == ended
    spies["end_run"].assert_awaited_once()
    spies["finalize"].assert_awaited_once()
    restarted = _runner(tmp_path)
    restarted._load_runs()
    assert restarted._runs[run.task_id].status == ended


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("driver", "ended"), [("taskrunner", "cancelled"), ("workflow", "running")]
)
async def test_a_workflow_runs_cancel_settles_a_run_the_stop_froze(
    tmp_path: Path, driver: str, ended: str
) -> None:
    """Workflow Runs cancels through the registry; a TaskRunner run still reaches the settlement."""
    from kiro_crew.workflows.registry import RunHandle
    from kiro_crew.workflows.service import WorkflowService

    runner, run, spies = await _drive(tmp_path, _broken_by_the_stop)
    svc = WorkflowService(sessions=_make_mock_sessions(), persist=False, task_runner=runner)
    finished_driver = asyncio.get_running_loop().create_future()
    finished_driver.set_result(None)
    svc.registry.register(
        RunHandle("wr1", "run", driver=driver, task_id=run.task_id, task=finished_driver),
        persist=False,
    )
    with patch("kiro_crew.git_coord.finalize", spies["finalize"]):
        assert await svc.cancel("wr1") is True
        settle = runner._stop_settles.get(run.task_id)
        if settle is not None:
            await settle
    assert _stored_status(tmp_path, run.task_id) == ended
    restarted = _runner(tmp_path)
    restarted._load_runs()
    if driver == "taskrunner":
        spies["end_run"].assert_awaited_once()
        assert restarted._runs[run.task_id].status == "cancelled"
    else:
        spies["end_run"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cancel_by_a_path_that_sets_no_status_still_ends_the_run(tmp_path: Path) -> None:
    """Workflow Runs cancels the driver task without setting ``cancelling`` first."""

    def _cancelled_by_workflow_runs(runner: TaskRunner, run: Any, task: Any) -> bool:
        runner.begin_shutdown()
        raise asyncio.CancelledError

    _runner_, run, spies = await _drive(tmp_path, _cancelled_by_workflow_runs)
    assert run.status == "cancelled"
    assert _stored_status(tmp_path, run.task_id) == "cancelled"
    spies["end_run"].assert_awaited_once()


@pytest.mark.asyncio
async def test_without_a_stop_the_failed_step_still_fails_the_run(tmp_path: Path) -> None:
    _runner_, run, spies = await _drive(tmp_path, _failed_for_real)
    spies["replan"].assert_awaited_once()
    spies["end_run"].assert_awaited_once()
    spies["finalize"].assert_awaited_once()
    assert run.status == "failed"
    assert _stored_status(tmp_path, run.task_id) == "failed"


def test_the_restart_settles_a_stored_running_run_to_paused(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    run = TaskRun(task_id="r1", spec_path="", spec_content="s", status="running")
    # The step the stop broke is stored FAILED; the resume's ``execute_plan``
    # sets every step that did not pass back to PENDING.
    run.tasks = [Step(index=1, title="A", description="d", status=StepStatus.FAILED)]
    runner._runs[run.task_id] = run
    runner._persist_runs()
    restarted = _runner(tmp_path)
    restarted._load_runs()
    assert restarted._runs["r1"].status == "paused"
    assert restarted._runs["r1"].tasks[0].status == StepStatus.FAILED


@pytest.mark.asyncio
async def test_a_revised_plan_retires_the_original_steps_that_never_ran(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    runner._notify = AsyncMock()  # type: ignore[method-assign]
    run = TaskRun(task_id="r1", spec_path=str(tmp_path / "t.md"), spec_content="s")
    run.status = "running"
    check = Step(index=1, title="check", description="d", status=StepStatus.FAILED, error="x")
    # A parallel sibling that failed too; only ``check`` is handed to the replan.
    lint = Step(index=2, title="lint", description="d", status=StepStatus.FAILED, error="y")
    publish = Step(index=3, title="publish", description="d", depends_on=[1, 2])
    run.tasks = [check, lint, publish]
    revised = [Step(index=1, title="repair", description="d")]
    runner._decompose = AsyncMock(return_value=revised)  # type: ignore[method-assign]

    async def _passes(run, task, history_key="", session_key=""):  # type: ignore[no-untyped-def]
        task.status = StepStatus.PASSED
        return True

    runner._execute_single_task = _passes  # type: ignore[method-assign]
    assert await runner._try_replan(run, check) is True
    assert (publish.status, publish.superseded) == (StepStatus.SKIPPED, True)
    assert (check.status, check.superseded, check.error) == (StepStatus.FAILED, True, "x")
    assert (lint.status, lint.superseded, lint.error) == (StepStatus.FAILED, True, "y")
    assert [t.title for t in run.tasks if t.status == StepStatus.PASSED] == ["repair"]


@pytest.mark.asyncio
async def test_a_resume_after_a_replan_runs_only_the_revised_plan(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    run = TaskRun(task_id="r1", spec_path="", spec_content="s", source="text", status="running")
    run.work_dir = str(tmp_path / "r1")
    run.tasks = [
        Step(index=1, title="check", description="d", status=StepStatus.FAILED, error="x"),
        Step(index=2, title="publish", description="d", status=StepStatus.SKIPPED),
        Step(index=3, title="repair", description="d", status=StepStatus.FAILED),
        Step(index=4, title="publish", description="d", depends_on=[3]),
    ]
    run.tasks[0].superseded = True
    run.tasks[1].superseded = True
    runner._runs[run.task_id] = run
    runner._persist_runs()
    restarted = _runner(tmp_path)
    restarted._load_runs()
    resumed = restarted._runs["r1"]
    assert [t.superseded for t in resumed.tasks] == [True, True, False, False]
    with patch("kiro_crew.taskrunner.git_coord"):
        await restarted.execute_plan("r1")
        driver = restarted._tasks["r1"]
        try:
            # The reset happens before the background run starts.
            assert [t.status for t in resumed.tasks] == [
                StepStatus.FAILED,
                StepStatus.SKIPPED,
                StepStatus.PENDING,
                StepStatus.PENDING,
            ]
            assert resumed.tasks[0].error == "x"
        finally:
            driver.cancel()
            with pytest.raises(asyncio.CancelledError):
                await driver


def test_no_run_is_admitted_after_shutdown_begins(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner._sessions.admission_closed = False
    assert runner._admission_closed() is False
    runner.begin_shutdown()
    assert runner._admission_closed() is True


def test_the_gateway_marks_the_runner_before_it_stops_memory() -> None:
    tree = ast.parse(inspect.getsource(exit_path._shutdown).lstrip())
    lines: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("begin_shutdown", "stop"):
                target = ast.unparse(node.func.value)
                lines.setdefault(f"{target}.{node.func.attr}", node.lineno)
    assert "task_runner.begin_shutdown" in lines
    assert lines["task_runner.begin_shutdown"] < lines["self._memory_startup.stop"]

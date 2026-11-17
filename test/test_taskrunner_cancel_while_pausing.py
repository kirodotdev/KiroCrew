"""A cancel that lands while a task run is still pausing ends the run cancelled.

``pause()`` sets ``pausing`` and cancels the run's driver; the driver's ``finally``
turns ``pausing`` into ``paused`` once the step's sessions are cleaned up. A
cancel sent in that window is the operator's latest request, so it wins: the
run ends ``cancelled`` with its unfinished steps cancelled, the same end state
as a cancel on a running run. A pause on its own still ends ``paused``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from taskrunner_helpers import _make_mock_sessions

from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner
from kiro_crew.testing.wait import await_until

_TASK_ID = "r1"


def _stored_status(work_dir: Path) -> str:
    rows = json.loads((work_dir / "runs.json").read_text(encoding="utf-8"))
    return next(row["status"] for row in rows if row["task_id"] == _TASK_ID)


async def _drive(
    tmp_path: Path, ask: Callable[[TaskRunner], None], *, in_cleanup: bool = False
) -> tuple[TaskRun, AsyncMock]:
    """Start a two-step run whose first step blocks, then call *ask* on it.

    *ask* runs once the first step is in progress or, with *in_cleanup*, from
    inside the driver's session cleanup, after the pause was already handled.
    """
    spec = tmp_path / "TASK.md"
    spec.write_text("# Build something", encoding="utf-8")
    runner = TaskRunner(sessions=_make_mock_sessions(), auto_test=False, work_dir=tmp_path)

    async def _decompose(spec_text, work_dir, task_id, agent=""):  # type: ignore[no-untyped-def]
        return [
            Step(index=1, title="A", description="d"),
            Step(index=2, title="B", description="d"),
        ]

    runner._decompose = _decompose  # type: ignore[method-assign]
    runner._taskq_end_run = AsyncMock()  # type: ignore[method-assign]
    entered = asyncio.Event()

    async def _step(run, task, history_key="", session_key=""):  # type: ignore[no-untyped-def]
        task.status = StepStatus.IN_PROGRESS
        entered.set()
        await asyncio.Event().wait()  # until the pause or cancel cancels it
        return True

    runner._execute_single_task = _step  # type: ignore[method-assign]
    if in_cleanup:
        real_cleanup = runner._cleanup_run_sessions

        async def _cleanup(run):  # type: ignore[no-untyped-def]
            assert run.status == "pausing"
            assert run.tasks[0].status == StepStatus.PENDING  # the pause kept it
            ask(runner)
            await real_cleanup(run)

        runner._cleanup_run_sessions = _cleanup  # type: ignore[method-assign]

    async def _init(run):  # type: ignore[no-untyped-def]
        run.branch_name = "task/r1"
        run.worktree_path = str(tmp_path / "wt")

    finalize = AsyncMock(return_value="task/r1")
    with (
        patch("kiro_crew.git_coord.init_workspace", _init),
        patch("kiro_crew.git_coord.finalize", finalize),
        patch("kiro_crew.git_coord.get_state_summary", AsyncMock(return_value="")),
    ):
        driver = asyncio.create_task(runner.run(spec, task_id=_TASK_ID))
        runner._tasks[_TASK_ID] = driver
        await await_until(entered, what="the first step's start")
        runner.pause(_TASK_ID)
        if not in_cleanup:
            ask(runner)
        run = await await_until(driver, what="the run driver's end")
    return run, finalize


def _cancel_one(runner: TaskRunner) -> None:
    runner.cancel(_TASK_ID, exact=True)


def _cancel_all(runner: TaskRunner) -> None:
    runner.cancel()


def _assert_cancelled(tmp_path: Path, run: Any, finalize: AsyncMock) -> None:
    assert run.status == "cancelled"
    assert _stored_status(tmp_path) == "cancelled"
    assert [t.status for t in run.tasks] == [StepStatus.CANCELLED, StepStatus.CANCELLED]
    finalize.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_pause_alone_still_ends_the_run_paused(tmp_path: Path) -> None:
    run, _finalize = await _drive(tmp_path, lambda _runner: None)
    assert run.status == "paused"
    assert _stored_status(tmp_path) == "paused"
    assert [t.status for t in run.tasks] == [StepStatus.PENDING, StepStatus.PENDING]


@pytest.mark.asyncio
async def test_a_cancel_while_pausing_ends_the_run_cancelled(tmp_path: Path) -> None:
    run, finalize = await _drive(tmp_path, _cancel_one)
    _assert_cancelled(tmp_path, run, finalize)


@pytest.mark.asyncio
async def test_a_cancel_all_while_pausing_ends_the_run_cancelled(tmp_path: Path) -> None:
    run, finalize = await _drive(tmp_path, _cancel_all)
    _assert_cancelled(tmp_path, run, finalize)


@pytest.mark.asyncio
async def test_a_cancel_during_the_pause_cleanup_ends_the_run_cancelled(tmp_path: Path) -> None:
    """The pause already kept the steps pending; the cancel still cancels them."""
    run, finalize = await _drive(tmp_path, _cancel_one, in_cleanup=True)
    _assert_cancelled(tmp_path, run, finalize)

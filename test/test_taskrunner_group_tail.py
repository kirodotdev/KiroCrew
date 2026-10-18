"""What ``_execute_tasks`` does after each dependency group, for both group shapes.

A one-step group and a parallel group end the same way. A failed step goes to
``_try_replan`` and the run's group loop stops there, whatever the replan answered: a
replan that answers yes took over the remaining work and one that answers no has
already failed the run, so a later group of the original plan must not run as well.
When the answer is no and the run is still marked running, the loop fails it with the
step's error redacted; the real ``_try_replan`` marks the run failed before it answers
no, so only a stand-in replan reaches that branch. A group that passes trims each
step's result to ``_RESULT_MEM_CAP`` and persists the registry once.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.taskrunner import _RESULT_MEM_CAP, Step, StepStatus, TaskRun, TaskRunner

# The indices of the first group's steps; a second group of one step depends on all of them.
_FIRST_GROUP = {"one-step": (1,), "parallel": (1, 2)}


def _two_group_run(tmp_path: Path, first: tuple[int, ...]) -> TaskRun:
    run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
    run.tasks = [Step(index=i, title=f"S{i}", description="d") for i in first]
    last = len(first) + 1
    run.tasks.append(Step(index=last, title=f"S{last}", description="d", depends_on=list(first)))
    return run


def _runner(tmp_path: Path) -> TaskRunner:
    sessions = MagicMock()
    sessions.reset = AsyncMock()
    return TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_FIRST_GROUP))
async def test_a_failed_group_ends_the_loop_even_when_the_replan_succeeds(
    tmp_path: Path, shape: str
) -> None:
    run = _two_group_run(tmp_path, _FIRST_GROUP[shape])
    runner = _runner(tmp_path)
    ran: list[int] = []

    async def _execute(_run, step, _history_key, session_key=""):
        ran.append(step.index)
        if step.index == 1:
            step.status = StepStatus.FAILED
            step.error = "broke"
            return False
        step.status = StepStatus.PASSED
        return True

    replan = AsyncMock(return_value=True)
    persist = AsyncMock()
    with (
        patch.object(runner, "_execute_single_task", side_effect=_execute),
        patch.object(runner, "_try_replan", replan),
        patch.object(runner, "_apersist_runs", persist),
        patch.object(runner, "_notify", AsyncMock()),
    ):
        await runner._execute_tasks(run, "hk")

    replan.assert_awaited_once_with(run, run.tasks[0])
    assert sorted(ran) == list(_FIRST_GROUP[shape]), "the dependent group must not run"
    persist.assert_not_awaited()
    assert run.status == "running", "a successful replan's run is not marked failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_FIRST_GROUP))
async def test_a_refused_replan_fails_the_run_with_the_error_redacted(
    tmp_path: Path, shape: str
) -> None:
    run = _two_group_run(tmp_path, _FIRST_GROUP[shape])
    runner = _runner(tmp_path)

    async def _execute(_run, step, _history_key, session_key=""):
        if step.index == 1:
            step.status = StepStatus.FAILED
            step.error = "broke AKIAIOSFODNN7EXAMPLE here"
            return False
        step.status = StepStatus.PASSED
        return True

    with (
        patch.object(runner, "_execute_single_task", side_effect=_execute),
        patch.object(runner, "_try_replan", AsyncMock(return_value=False)),
        patch.object(runner, "_apersist_runs", AsyncMock()),
        patch.object(runner, "_notify", AsyncMock()),
    ):
        await runner._execute_tasks(run, "hk")

    assert run.status == "failed"
    assert run.error.startswith("Task 1 failed: broke ")
    assert "AKIAIOSFODNN7EXAMPLE" not in run.error


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_FIRST_GROUP))
async def test_a_passing_group_trims_each_result_and_persists_once(
    tmp_path: Path, shape: str
) -> None:
    run = _two_group_run(tmp_path, _FIRST_GROUP[shape])
    runner = _runner(tmp_path)

    async def _execute(_run, step, _history_key, session_key=""):
        step.status = StepStatus.PASSED
        step.result = "x" * (_RESULT_MEM_CAP + 10)
        return True

    persist = AsyncMock()
    with (
        patch.object(runner, "_execute_single_task", side_effect=_execute),
        patch.object(runner, "_try_replan", AsyncMock()),
        patch.object(runner, "_apersist_runs", persist),
        patch.object(runner, "_notify", AsyncMock()),
    ):
        await runner._execute_tasks(run, "hk")

    assert [len(t.result) for t in run.tasks] == [_RESULT_MEM_CAP] * len(run.tasks)
    assert persist.await_count == 2, "one persist per group"

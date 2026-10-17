"""The durable task-queue bridge settles a run's row from its status and fails quietly.

Pins the edges of ``kiro_crew.taskrunner_parts.taskq_bridge`` that the queue round trips in
``test_taskrunner_taskq.py`` do not reach, through the runner itself: how a run's container
row is settled for each final status, what an unreadable row state or a refused run row
leaves behind, and that attaching the queue outside an event loop arms no adoption sweep.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.taskq.adapters import runner as r
from kiro_crew.taskq.store import TaskStoreUnavailable
from kiro_crew.taskrunner import PROGRESS_FILE, Project, TaskRunner


def _runner(tmp_path: Path) -> TaskRunner:
    return TaskRunner(sessions=MagicMock(), auto_test=False, work_dir=tmp_path)


def _run(tmp_path: Path, status: str, *, error: str = "") -> Project:
    run = Project(spec_path=str(tmp_path / "spec.md"), spec_content="s", status=status)
    run.task_id = "r1"
    run.work_dir = str(tmp_path)
    run.error = error
    return run


def _handle() -> MagicMock:
    handle = MagicMock()
    handle.done_async = AsyncMock()
    handle.fail_async = AsyncMock()
    handle.cancel_async = AsyncMock()
    return handle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error", "settled"),
    [
        ("failed", "step 2 failed", ("fail_async", ("step 2 failed",), {})),
        ("failed", "", ("fail_async", ("run failed",), {})),
        ("paused", "", ("cancel_async", ("run paused",), {})),
        ("cancelled", "", ("cancel_async", ("run cancelled",), {})),
    ],
)
async def test_a_run_that_did_not_complete_settles_its_row_as_failed_or_cancelled(
    tmp_path: Path, status: str, error: str, settled: tuple[str, tuple[Any, ...], dict[str, Any]]
) -> None:
    runner = _runner(tmp_path)
    handle = _handle()
    runner._run_handles["r1"] = handle
    await runner._taskq_end_run(_run(tmp_path, status, error=error))
    name, args, kwargs = settled
    getattr(handle, name).assert_awaited_once_with(*args, **kwargs)
    others = {"done_async", "fail_async", "cancel_async"} - {name}
    assert [n for n in sorted(others) if getattr(handle, n).await_count] == []
    assert "r1" not in runner._run_handles


@pytest.mark.asyncio
async def test_a_completed_run_settles_its_row_done_with_its_progress_file(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    handle = _handle()
    runner._run_handles["r1"] = handle
    await runner._taskq_end_run(_run(tmp_path, "completed"))
    handle.done_async.assert_awaited_once_with(result_ref=str(tmp_path / PROGRESS_FILE))


@pytest.mark.asyncio
async def test_a_run_without_a_row_settles_nothing(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    await runner._taskq_end_run(_run(tmp_path, "failed"))
    assert runner._run_handles == {}


@pytest.mark.asyncio
async def test_an_unreadable_row_state_reads_as_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner = _runner(tmp_path)
    assert await runner._taskq_row_state("taskrunner:r1") is None

    store = MagicMock()
    store.run = AsyncMock(side_effect=TaskStoreUnavailable("store closed"))
    admission = MagicMock()
    admission.store = store
    runner._task_admission = admission
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.taskrunner"):
        assert await runner._taskq_row_state("taskrunner:r1") is None
    assert [r.getMessage() for r in caplog.records if r.name == "kiro_crew.taskrunner"] == [
        "taskq: state read for taskrunner:r1 failed"
    ]


@pytest.mark.asyncio
async def test_a_refused_run_row_leaves_the_run_without_a_handle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner = _runner(tmp_path)
    admission = MagicMock()
    admission.store = MagicMock()
    admission.accept_async = AsyncMock(side_effect=r.RunnerAdmissionRefused("store refused"))
    admission.claim_only_async = AsyncMock()
    runner._task_admission = admission
    with caplog.at_level(logging.WARNING, logger="kiro_crew.taskrunner"):
        await runner._taskq_begin_run(_run(tmp_path, "running"))
    admission.claim_only_async.assert_not_awaited()
    assert runner._run_handles == {}
    assert [r.getMessage() for r in caplog.records if r.name == "kiro_crew.taskrunner"] == [
        "taskq: run row for r1 not accepted: store refused"
    ]


@pytest.mark.asyncio
async def test_without_a_queue_steps_are_not_admitted_and_nothing_is_adopted(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    assert await runner._taskq_admit_step(_run(tmp_path, "running"), MagicMock()) is None
    assert await runner.adopt_task_rows() is None


def test_attaching_the_queue_outside_an_event_loop_arms_no_adoption_sweep(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    admission = MagicMock()
    admission.store = MagicMock()
    runner.attach_task_admission(admission)
    assert runner.task_admission is admission
    assert runner._adopt_inflight is None

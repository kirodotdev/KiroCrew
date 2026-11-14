"""WorkflowService.cancel on a Task Runner run that is paused or planned."""

from __future__ import annotations

import asyncio

import pytest
from test_workflows_service import FakeSessions

from kiro_crew.workflows.service import WorkflowService

pytestmark = pytest.mark.asyncio


async def test_cancel_reaches_a_paused_task_runner_run() -> None:
    class _RunnerStub:
        def __init__(self, answer: bool) -> None:
            self.answer = answer
            self.asked: list[str] = []

        def cancel(self, task_id: str, exact: bool = False) -> None:
            raise AssertionError("a paused run is ended by cancel_paused")

        async def cancel_paused(self, task_id: str) -> bool:
            self.asked.append(task_id)
            return self.answer

    for answer in (True, False):  # the answer is the runner's, not a literal
        stub = _RunnerStub(answer)
        svc = WorkflowService(sessions=FakeSessions([]), persist=False, task_runner=stub)
        run_id = await svc.begin_host_run(
            name="Plan", source_format="task-plan", task_id="task_123", driver="taskrunner"
        )
        await svc.bind_task(run_id, asyncio.create_task(asyncio.sleep(3600)), task_id="task_123")
        assert await svc.pause(run_id) is True  # waiting on an approval: no driver task

        assert await svc.cancel(run_id) is answer
        assert stub.asked == ["task_123"]


async def test_a_real_service_and_runner_cancel_a_plan_waiting_for_approval(tmp_path) -> None:
    from unittest.mock import MagicMock

    from kiro_crew.taskrunner import Project, Task, TaskRunner

    svc = WorkflowService(sessions=FakeSessions([]), persist=False)
    runner = TaskRunner(
        sessions=MagicMock(), auto_test=False, work_dir=tmp_path, workflow_service=svc
    )
    svc._task_runner = runner
    run_id = await svc.begin_host_run(
        name="Plan", source_format="task-plan", task_id="task_123", driver="taskrunner"
    )
    # How a new plan is left (plan_mode): the run is planned, its Workflows run paused.
    run = Project(spec_path=str(tmp_path / "spec.md"), spec_content="s", status="planned")
    run.task_id = "task_123"
    run.workflow_run_id = run_id
    run.tasks = [Task(index=1, title="one", description="the only step")]
    runner._runs = {"task_123": run}
    assert await svc.pause(run_id) is True

    assert await svc.cancel(run_id) is True
    assert run.status == "cancelled"
    assert svc.status(run_id)["status"] == "cancelled"
    assert await svc.cancel(run_id) is False  # nothing left to cancel

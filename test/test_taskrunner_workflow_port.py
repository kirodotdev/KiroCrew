"""``TaskRunner`` mirrors a run onto its shared workflow run, best-effort once attached.

Pins the workflow-history port (``kiro_crew.taskrunner_parts.workflow_port``) through the
runner itself: each terminal status reaches the service as its own call, a publication
failure is logged and never fails the run, a task-snapshot failure while a run is being
registered is the one error that passes through, and the attachment gate refuses new work
until the service is attached.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.taskrunner import Project, Task, TaskRunner, WorkflowInitializing
from kiro_crew.workflow_memory import TaskSnapshotError


class _Service:
    """A workflow service that records every call and can fail any of them."""

    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.failing = failing

    def _record(self, call: str, /, *args: Any, **kwargs: Any) -> None:
        self.calls.append((call, args, kwargs))
        if call in self.failing:
            raise RuntimeError(f"{call} is unavailable")

    async def begin_host_run(self, **kwargs: Any) -> str:
        self._record("begin_host_run", **kwargs)
        if "begin_host_run_snapshot" in self.failing:
            raise TaskSnapshotError("snapshot refused")
        return "wf-1"

    async def phase(self, run_id: str, title: str) -> None:
        self._record("phase", run_id, title)

    async def set_source(self, run_id: str, source: str, **kwargs: Any) -> bool:
        self._record("set_source", run_id, source, **kwargs)
        return True

    async def pause(self, run_id: str) -> bool:
        self._record("pause", run_id)
        return True

    async def rebind(self, run_id: str, task: Any, *, task_id: str = "") -> bool:
        self._record("rebind", run_id, task_id=task_id)
        return True

    async def finish(self, run_id: str, result: Any) -> None:
        self._record("finish", run_id, result)

    async def fail(self, run_id: str, error: str, *, where: str = "host") -> None:
        self._record("fail", run_id, error, where=where)

    async def cancel_host_run(self, run_id: str, reason: str = "cancelled") -> None:
        self._record("cancel_host_run", run_id, reason)

    async def delete_run(self, run_id: str) -> bool:
        self._record("delete_run", run_id)
        return True

    def status(self, run_id: str) -> dict[str, Any] | None:
        self._record("status", run_id)
        return {"run_id": run_id}


def _runner(tmp_path: Path, service: _Service | None) -> TaskRunner:
    return TaskRunner(
        sessions=MagicMock(), auto_test=False, work_dir=tmp_path, workflow_service=service
    )


def _run(tmp_path: Path, status: str, *, error: str = "") -> Project:
    run = Project(spec_path=str(tmp_path / "spec.md"), spec_content="s", status=status)
    run.task_id = "r1"
    run.workflow_run_id = "wf-1"
    run.error = error
    run.tasks = [Task(index=1, title="one", description="the only step")]
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        ("completed", "", ("finish", ("wf-1", {"task_id": "r1", "status": "completed"}), {})),
        ("paused", "", ("pause", ("wf-1",), {})),
        (
            "cancelled",
            "stopped by operator",
            ("cancel_host_run", ("wf-1", "stopped by operator"), {}),
        ),
        ("cancelled", "", ("cancel_host_run", ("wf-1", "cancelled"), {})),
        ("failed", "step 1 failed", ("fail", ("wf-1", "step 1 failed"), {"where": "taskrunner"})),
        ("failed", "", ("fail", ("wf-1", "TaskRunner failed"), {"where": "taskrunner"})),
    ],
)
async def test_each_terminal_status_reaches_the_service_as_its_own_call(
    tmp_path: Path, status: str, error: str, expected: tuple[str, tuple[Any, ...], dict[str, Any]]
) -> None:
    service = _Service()
    runner = _runner(tmp_path, service)
    await runner._workflow_finalize(_run(tmp_path, status, error=error))
    assert service.calls == [expected]


@pytest.mark.asyncio
async def test_a_status_with_no_terminal_meaning_publishes_nothing(tmp_path: Path) -> None:
    service = _Service()
    await _runner(tmp_path, service)._workflow_finalize(_run(tmp_path, "running"))
    assert service.calls == []


@pytest.mark.asyncio
async def test_a_failed_publication_is_logged_and_never_fails_the_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service = _Service(failing=frozenset({"fail", "set_source", "status", "delete_run"}))
    runner = _runner(tmp_path, service)
    run = _run(tmp_path, "failed", error="boom")
    with caplog.at_level(logging.DEBUG, logger="kiro_crew.taskrunner"):
        await runner._workflow_finalize(run)
        await runner._workflow_set_plan(run)
        await runner._workflow_rebind(run)
        await runner._workflow_delete_link(run)
    messages = [r.getMessage() for r in caplog.records if r.name == "kiro_crew.taskrunner"]
    assert messages == [
        "TaskRunner workflow publication failed to finalize",
        "TaskRunner workflow plan publication failed",
        "TaskRunner workflow publication failed to bind",
        "delete linked workflow run failed for wf-1",
    ]


@pytest.mark.asyncio
async def test_a_failed_registration_is_logged_and_leaves_the_run_unlinked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service = _Service(failing=frozenset({"begin_host_run"}))
    run = _run(tmp_path, "planning")
    run.workflow_run_id = ""
    with caplog.at_level(logging.WARNING, logger="kiro_crew.taskrunner"):
        await _runner(tmp_path, service)._workflow_begin(run)
    assert run.workflow_run_id == ""
    records = [r for r in caplog.records if r.name == "kiro_crew.taskrunner"]
    assert [r.getMessage() for r in records] == ["TaskRunner workflow publication failed to start"]
    assert [str(r.exc_info[1]) for r in records if r.exc_info] == ["begin_host_run is unavailable"]


@pytest.mark.asyncio
async def test_a_snapshot_failure_while_registering_passes_through(tmp_path: Path) -> None:
    service = _Service(failing=frozenset({"begin_host_run_snapshot"}))
    run = _run(tmp_path, "planning")
    run.workflow_run_id = ""
    with pytest.raises(TaskSnapshotError):
        await _runner(tmp_path, service)._workflow_begin(run)


@pytest.mark.asyncio
async def test_a_plan_with_no_steps_publishes_no_source(tmp_path: Path) -> None:
    service = _Service()
    run = _run(tmp_path, "planned")
    run.tasks = []
    await _runner(tmp_path, service)._workflow_set_plan(run, pause=True)
    assert service.calls == []


@pytest.mark.asyncio
async def test_without_a_service_nothing_is_published_and_nothing_fails(tmp_path: Path) -> None:
    runner = _runner(tmp_path, None)
    run = _run(tmp_path, "failed", error="boom")
    await runner._workflow_finalize(run)
    await runner._workflow_set_plan(run)
    await runner._workflow_rebind(run)
    await runner._workflow_delete_link(run)
    assert run.workflow_run_id == "wf-1"


def test_new_work_waits_for_attachment_and_a_failed_host_says_restart(tmp_path: Path) -> None:
    runner = _runner(tmp_path, None)
    runner._require_workflow_ready()

    runner.defer_workflow_attachment()
    with pytest.raises(WorkflowInitializing) as pending:
        runner._require_workflow_ready()
    assert pending.value.code == "workflow_initializing"

    runner.defer_workflow_attachment(failed=True)
    with pytest.raises(WorkflowInitializing) as failed:
        runner._require_workflow_ready()
    assert failed.value.code == "workflow_initialization_failed"
    assert str(failed.value) == "Workflow initialization failed; restart the gateway."

    runner.attach_workflow_service(None)
    runner._require_workflow_ready()

"""Private output remains scoped in fallback slots and TaskRunner persistence."""

import json

import pytest
from test_workflows_inject import _FakeSlot, _FakeState
from test_workflows_private_execution import world as _world

from kiro_crew.dashboard.workflow_inject import inject_bound_workflow_result
from kiro_crew.task_models import Project, Task, TaskStatus
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.workflow_memory import WorkflowScope, task_snapshot_path

world = _world


class State(_FakeState):
    def get_or_create_slot(self, name, **kwargs):
        existing = self.get_slot(name)
        slot = super().get_or_create_slot(name, **kwargs)
        if existing is None:
            slot.agent = kwargs.get("agent", "")
            slot.linked_session_key = kwargs.get("linked_session_key", "")
        return slot


@pytest.mark.asyncio
async def test_private_fallback_is_bound_before_any_output(world):
    scope = await WorkflowScope.admit("wf_fallback", world.builder, "dashboard:alice")
    state = State()
    snapshot = {
        "session_key": scope.origin,
        "run_id": scope.run_id,
        "status": "finished",
        "result": "PRIVATE_RESULT",
    }
    assert await inject_bound_workflow_result(state, scope.run_id, snapshot)
    slot = state.get_slot("workflow-wf_fallback")
    assert slot.agent == "alice"
    assert slot.memory_store == scope.store
    assert slot.linked_session_key == scope.origin
    assert len(slot.messages) == 1
    assert "PRIVATE_RESULT" in slot.messages[0]["content"]
    assert await inject_bound_workflow_result(state, scope.run_id, snapshot)
    assert len(slot.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", ["", "dashboard:bob", "dashboard:alice"])
async def test_private_fallback_never_rebinds_colliding_slot(world, linked):
    scope = await WorkflowScope.admit("wf_collision", world.builder, "dashboard:alice")
    slot = _FakeSlot("workflow-wf_collision")
    slot.agent = "bob"
    slot.memory_store = world.stores["bob"]
    slot.linked_session_key = linked
    state = State({slot.key: slot})
    snapshot = {
        "session_key": scope.origin,
        "run_id": scope.run_id,
        "status": "finished",
        "result": "PRIVATE_RESULT",
    }
    assert not await inject_bound_workflow_result(state, scope.run_id, snapshot)
    assert slot.agent == "bob" and slot.memory_store == world.stores["bob"]
    assert slot.linked_session_key == linked
    assert slot.messages == []


@pytest.mark.asyncio
async def test_private_task_snapshot_has_only_public_reference_and_restores(world, tmp_path):
    scope = await WorkflowScope.admit("wf_task_snapshot", world.builder, "dashboard:alice")
    await scope.prepare(world.builder, "taskrunner:private-task:runtime")
    runner = TaskRunner(
        sessions=world.sessions, context_builder=world.builder, work_dir=tmp_path / "tasks"
    )
    private = Project(
        spec_path="", spec_content="PRIVATE_SOURCE", task_id="private-task", status="completed"
    )
    private.original_input = "PRIVATE_INPUT"
    private.error = "PRIVATE_ERROR"
    private.tasks = [
        Task(
            index=1,
            title="PRIVATE_TITLE",
            description="PRIVATE_DESC",
            status=TaskStatus.PASSED,
            result="PRIVATE_OUTPUT",
        )
    ]
    public = Project(
        spec_path="", spec_content="PUBLIC_SOURCE", task_id="global-task", status="completed"
    )
    runner._runs = {private.task_id: private, public.task_id: public}
    await runner._apersist_runs()
    visible = runner._runs_path().read_text(encoding="utf-8")
    assert "PRIVATE_" not in visible
    assert "PUBLIC_SOURCE" in visible
    assert {"task_id": "private-task", "private_payload": True} in json.loads(visible)
    hidden = task_snapshot_path(runner._runs_path()).read_text(encoding="utf-8")
    for marker in (
        "PRIVATE_SOURCE",
        "PRIVATE_INPUT",
        "PRIVATE_ERROR",
        "PRIVATE_TITLE",
        "PRIVATE_DESC",
        "PRIVATE_OUTPUT",
    ):
        assert marker in hidden
    restored = TaskRunner(
        sessions=world.sessions, context_builder=world.builder, work_dir=tmp_path / "tasks"
    )
    assert restored._runs["private-task"].spec_content == "PRIVATE_SOURCE"
    assert restored._runs["private-task"].tasks[0].result == "PRIVATE_OUTPUT"
    # A public row cannot replace the hidden private content with edited metadata.
    rows = json.loads(visible)
    rows[0] = {
        "task_id": "private-task",
        "spec_content": "FORGED_PUBLIC",
        "status": "completed",
        "spec_path": "",
    }
    runner._runs_path().write_text(json.dumps(rows), encoding="utf-8")
    restored = TaskRunner(
        sessions=world.sessions, context_builder=world.builder, work_dir=tmp_path / "tasks"
    )
    assert restored._runs["private-task"].spec_content == "PRIVATE_SOURCE"
    runner._runs = {public.task_id: public}
    await runner._apersist_runs()
    assert "PRIVATE_SOURCE" in task_snapshot_path(runner._runs_path()).read_text(encoding="utf-8")
    restored = TaskRunner(
        sessions=world.sessions, context_builder=world.builder, work_dir=tmp_path / "tasks"
    )
    assert "private-task" not in restored._runs

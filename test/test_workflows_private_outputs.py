"""Private output remains scoped in fallback slots and TaskRunner persistence."""

import pytest
from test_workflows_inject import _FakeSlot, _FakeState
from test_workflows_private_execution import world as _world

from kiro_crew.dashboard.workflow_inject import inject_bound_workflow_result
from kiro_crew.task_models import Project, Task, TaskStatus
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.workflow_memory import WorkflowScope

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
        "execution_context": scope.execution_context.to_record(),
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
        "execution_context": scope.execution_context.to_record(),
    }
    assert not await inject_bound_workflow_result(state, scope.run_id, snapshot)
    assert slot.agent == "bob" and slot.memory_store == world.stores["bob"]
    assert slot.linked_session_key == linked
    assert slot.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
async def test_task_snapshot_owns_context_and_omits_restricted_bodies(world, tmp_path, mode):
    scope = await WorkflowScope.admit("wf_task_snapshot", world.builder, "dashboard:alice")
    execution = scope.execution_context.with_mode(mode)
    runner = TaskRunner(
        sessions=world.sessions, context_builder=world.builder, work_dir=tmp_path / "tasks"
    )
    member = Project(
        "", "MEMBER_SOURCE", task_id="member-task", status="completed", execution_context=execution
    )
    member.original_input = "MEMBER_INPUT"
    member.error = "MEMBER_ERROR"
    member.tasks = [
        Task(1, "MEMBER_TITLE", "MEMBER_DESC", status=TaskStatus.PASSED, result="MEMBER_OUTPUT")
    ]
    public = Project("", "PUBLIC_SOURCE", task_id="global-task", status="completed")
    runner._runs = {member.task_id: member, public.task_id: public}
    await runner._apersist_runs()
    payload = runner._runs_path().read_text(encoding="utf-8")
    assert "PUBLIC_SOURCE" in payload
    assert "private_payload" not in payload
    assert ("MEMBER_SOURCE" in payload) == (mode == "persistent")
    restored = TaskRunner(
        sessions=world.sessions, context_builder=world.builder, work_dir=tmp_path / "tasks"
    )
    if mode == "persistent":
        assert restored._runs[member.task_id].execution_context == execution
        assert restored._runs[member.task_id].tasks[0].result == "MEMBER_OUTPUT"
    else:
        assert member.task_id not in restored._runs
    assert await runner.delete_run(member.task_id)
    restored = TaskRunner(
        sessions=world.sessions, context_builder=world.builder, work_dir=tmp_path / "tasks"
    )
    assert member.task_id not in restored._runs

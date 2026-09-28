"""Workflow completion keeps its admitted member across delivery races."""

import asyncio

import pytest
from test_workflows_inject import _FakeSlot
from test_workflows_private_outputs import State
from test_workflows_private_outputs import world as _world

from kiro_crew.dashboard.workflow_inject import inject_bound_workflow_result
from kiro_crew.execution_context import bind_session_execution
from kiro_crew.workflow_memory import WorkflowScope

world = _world


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["origin", "fallback"])
async def test_completion_rechecks_target_after_binding(world, monkeypatch, replacement):
    scope = await WorkflowScope.admit("wf_delivery_race", world.builder, "dashboard:alice")
    state = State()
    bound = asyncio.Event()
    release = asyncio.Event()
    original_to_thread = asyncio.to_thread

    async def paused_binding(func, *args, **kwargs):
        result = await original_to_thread(func, *args, **kwargs)
        if func is bind_session_execution:
            bound.set()
            await release.wait()
        return result

    monkeypatch.setattr(asyncio, "to_thread", paused_binding)
    snapshot = {
        "session_key": scope.origin,
        "run_id": scope.run_id,
        "status": "finished",
        "result": "ALICE_RESULT",
        "execution_context": scope.execution_context.to_record(),
    }
    delivery = asyncio.create_task(inject_bound_workflow_result(state, scope.run_id, snapshot))
    try:
        await asyncio.wait_for(bound.wait(), 2)
        target = "alice" if replacement == "origin" else f"workflow-{scope.run_id}"
        slot = _FakeSlot(target)
        slot.agent = "bob"
        slot.memory_store = world.stores["bob"]
        state._slots[target] = slot
    finally:
        release.set()
    assert not await delivery
    assert state.created == []
    assert slot.agent == "bob"
    assert slot.memory_store == world.stores["bob"]
    assert slot.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_restricted_completion_does_not_recreate_closed_parent(world, monkeypatch, mode):
    scope = await WorkflowScope.admit("wf_closed", world.builder, "dashboard:alice")
    state = State()

    def unexpected_binding(*args, **kwargs):
        pytest.fail("A closed restricted session must not be persisted for a fallback")

    monkeypatch.setattr("kiro_crew.execution_context.bind_session_execution", unexpected_binding)
    snapshot = {
        "session_key": scope.origin,
        "run_id": scope.run_id,
        "status": "finished",
        "result": "RESTRICTED_RESULT",
        "execution_context": scope.execution_context.with_mode(mode).to_record(),
    }
    assert not await inject_bound_workflow_result(state, scope.run_id, snapshot)
    assert state.created == []
    assert not state._slots

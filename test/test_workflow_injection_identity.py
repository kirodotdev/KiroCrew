"""Workflow completion keeps its admitted member across delivery races."""

import asyncio

import pytest
from test_workflows_inject import _FakeSlot
from test_workflows_private_outputs import State
from test_workflows_private_outputs import world as _world

from kiro_crew.dashboard.workflow_inject import inject_bound_workflow_result
from kiro_crew.execution_context import bind_session_execution
from kiro_crew.testing.wait import await_until
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
        await await_until(bound, what="the session binding finishing")
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


class _MetadataLog:
    """A conversation log whose metadata read answers one fixed line."""

    def __init__(self, meta, readable):
        self.meta, self.readable, self.keys = meta, readable, []

    def get_metadata_status(self, key):
        self.keys.append(key)
        return self.meta, self.readable


@pytest.mark.asyncio
async def test_fallback_slot_takes_the_closed_sessions_turn_limit(world):
    # A tab closed before its workflow finished had its own limit; the slot the
    # result lands in carries it rather than following the global limit.
    scope = await WorkflowScope.admit("wf_limit", world.builder, "dashboard:alice")
    state = State()
    state.conversation_log = _MetadataLog({"_type": "metadata", "turn_timeout_secs": 28800}, True)
    snapshot = {
        "session_key": scope.origin,
        "run_id": scope.run_id,
        "status": "finished",
        "result": "LIMIT_RESULT",
        "execution_context": scope.execution_context.to_record(),
    }
    assert await inject_bound_workflow_result(state, scope.run_id, snapshot)
    slot = state.get_slot("workflow-wf_limit")
    assert state.conversation_log.keys == [scope.origin]
    assert slot.turn_timeout_secs == 28800
    assert slot._turn_timeout_hydrated is True


@pytest.mark.asyncio
async def test_fallback_slot_defers_an_unreadable_turn_limit(world):
    # Unread, the slot holds no limit and is marked unhydrated, so its saves
    # carry the on-disk limit forward instead of erasing it.
    scope = await WorkflowScope.admit("wf_unread", world.builder, "dashboard:alice")
    state = State()
    state.conversation_log = _MetadataLog({}, False)
    snapshot = {
        "session_key": scope.origin,
        "run_id": scope.run_id,
        "status": "finished",
        "result": "UNREAD_RESULT",
        "execution_context": scope.execution_context.to_record(),
    }
    assert await inject_bound_workflow_result(state, scope.run_id, snapshot)
    slot = state.get_slot("workflow-wf_unread")
    assert slot.turn_timeout_secs is None
    assert slot._turn_timeout_hydrated is False
    # Nothing stored was read, so the defaults apply: the default title, unfiled.
    assert (slot.title, slot.folder_id) == ("Workflow: wf_unread", "")


def _closed_chat_line(**extra):
    return {
        "_type": "metadata",
        "title": "My review chat",
        "title_origin": "user",
        "folder_id": "fold0001",
        **extra,
    }


@pytest.mark.asyncio
async def test_fallback_slot_keeps_the_closed_chats_name_folder_and_binding(world):
    # A chat its person named and filed, closed before its workflow finished:
    # the result's slot writes that chat's line, so it takes the stored name,
    # folder, workspace and conductor instead of saving the defaults over them.
    scope = await WorkflowScope.admit("wf_line", world.builder, "dashboard:alice")
    state = State()
    state._folders = [{"id": "fold0001"}]
    state.conversation_log = _MetadataLog(
        _closed_chat_line(workspace="ws-a", crew_mode_agent="kirocrew-conductor"), True
    )
    snapshot = {
        "session_key": scope.origin,
        "run_id": scope.run_id,
        "name": "review",
        "status": "finished",
        "result": "LINE_RESULT",
        "execution_context": scope.execution_context.to_record(),
    }
    assert await inject_bound_workflow_result(state, scope.run_id, snapshot)
    slot = state.get_slot("workflow-wf_line")
    assert (slot.title, slot._titled, slot._title_origin) == ("My review chat", True, "user")
    assert slot.folder_id == "fold0001"
    assert (slot.workspace, slot.crew_mode_agent) == ("ws-a", "kirocrew-conductor")
    # The namespace pairs with the agent the slot was given: alice's member name.
    assert (slot.agent, slot.agent_kind) == ("alice", "member")


@pytest.mark.asyncio
async def test_fallback_slot_takes_the_runs_own_title_and_folder_over_the_stored_ones(world):
    scope = await WorkflowScope.admit("wf_args", world.builder, "dashboard:alice")
    state = State()
    state._folders = [{"id": "fold0001"}, {"id": "fold0009"}]
    state.conversation_log = _MetadataLog(_closed_chat_line(), True)
    snapshot = {
        "session_key": scope.origin,
        "run_id": scope.run_id,
        "status": "finished",
        "result": "ARGS_RESULT",
        "fallback_title": "MR !1234 review",
        "fallback_folder_id": "fold0009",
        "execution_context": scope.execution_context.to_record(),
    }
    assert await inject_bound_workflow_result(state, scope.run_id, snapshot)
    slot = state.get_slot("workflow-wf_args")
    assert (slot.title, slot.folder_id) == ("MR !1234 review", "fold0009")


@pytest.mark.asyncio
async def test_refire_leaves_the_fallback_slots_title_and_folder(world):
    # Only the delivery that creates the slot titles and files it: a re-fire
    # must not put the default title back over a name the slot already has.
    scope = await WorkflowScope.admit("wf_refire", world.builder, "dashboard:alice")
    state = State()
    state.conversation_log = _MetadataLog({"_type": "metadata"}, True)
    snapshot = {
        "session_key": scope.origin,
        "run_id": scope.run_id,
        "status": "finished",
        "result": "REFIRE_RESULT",
        "execution_context": scope.execution_context.to_record(),
    }
    assert await inject_bound_workflow_result(state, scope.run_id, snapshot)
    slot = state.get_slot("workflow-wf_refire")
    assert (slot.title, slot.folder_id) == ("Workflow: wf_refire", "")
    slot.title, slot.folder_id = "Renamed after delivery", "fold0002"
    assert await inject_bound_workflow_result(state, scope.run_id, snapshot)
    assert (slot.title, slot.folder_id) == ("Renamed after delivery", "fold0002")

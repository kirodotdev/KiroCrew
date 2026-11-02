"""A workflow result whose chat is gone tests the live session ceiling.

Every other slot allocator refuses at ``MAX_LIVE_SLOTS``. The ``workflow-<run_id>``
fallback is an allocation too, so at the ceiling it creates nothing, logs one
warning and returns False; below it, and on a re-fire into an existing fallback
slot, delivery is unchanged.
"""

import logging

import pytest
from test_workflows_inject import _FakeSlot, _FakeState
from test_workflows_private_outputs import State
from test_workflows_private_outputs import world as _world

from kiro_crew.dashboard import workflow_inject
from kiro_crew.dashboard.workflow_inject import (
    inject_bound_workflow_result,
    inject_workflow_result,
)
from kiro_crew.workflow_memory import WorkflowScope

world = _world

_CAP = 3


@pytest.fixture(autouse=True)
def _small_ceiling(_floor_monkeypatch):
    # raising=False keeps the tests runnable with the fix reverted, so they
    # fail on the missing ceiling test rather than erroring in setup.
    _floor_monkeypatch.setattr(workflow_inject, "MAX_LIVE_SLOTS", _CAP, raising=False)


def _full(state_cls=_FakeState):
    return state_cls({f"chat-{i}": _FakeSlot(f"chat-{i}") for i in range(_CAP)})


def _snapshot(run_id="wf_cap", **extra):
    snap = {
        "session_key": "dashboard:gone",
        "run_id": run_id,
        "name": "nightly",
        "status": "finished",
        "result": "CAP_RESULT",
    }
    snap.update(extra)
    return snap


def _cap_warnings(caplog):
    return [r for r in caplog.records if "live session ceiling" in r.getMessage()]


def test_fallback_at_ceiling_creates_nothing_and_warns_once(caplog):
    state = _full()
    with caplog.at_level(logging.WARNING, logger=workflow_inject.__name__):
        assert inject_workflow_result(state, "wf_cap", _snapshot()) is False
    assert state.created == []
    assert "workflow-wf_cap" not in state._slots
    assert len(_cap_warnings(caplog)) == 1
    assert "wf_cap" in _cap_warnings(caplog)[0].getMessage()


def test_fallback_below_ceiling_still_creates_titled_slot():
    state = _FakeState({"chat-0": _FakeSlot("chat-0")})
    assert inject_workflow_result(state, "wf_cap", _snapshot(fallback_title="Run A"))
    slot = state._slots["workflow-wf_cap"]
    assert slot.title == "Run A"
    assert "CAP_RESULT" in slot.messages[0]["content"]


def test_refire_into_existing_fallback_slot_needs_no_new_allocation():
    state = _full()
    state._slots.pop("chat-0")
    state._slots["workflow-wf_cap"] = _FakeSlot("workflow-wf_cap")
    assert state.live_slot_count() == _CAP
    assert inject_workflow_result(state, "wf_cap", _snapshot())
    assert len(state._slots["workflow-wf_cap"].messages) == 1


def test_live_originating_chat_is_delivered_at_ceiling():
    state = _full()
    snap = _snapshot(session_key="chat-1")
    assert inject_workflow_result(state, "wf_cap", snap)
    assert state.created == []
    assert len(state._slots["chat-1"].messages) == 1


@pytest.mark.asyncio
async def test_bound_fallback_at_ceiling_creates_and_binds_nothing(world, monkeypatch, caplog):
    scope = await WorkflowScope.admit("wf_bound_cap", world.builder, "dashboard:alice")
    state = _full(State)

    def unexpected_binding(*args, **kwargs):
        pytest.fail("A fallback refused at the ceiling must not bind an identity")

    monkeypatch.setattr("kiro_crew.execution_context.bind_session_execution", unexpected_binding)
    snapshot = {
        "session_key": scope.origin,
        "run_id": scope.run_id,
        "status": "finished",
        "result": "BOUND_CAP_RESULT",
        "execution_context": scope.execution_context.to_record(),
    }
    with caplog.at_level(logging.WARNING, logger=workflow_inject.__name__):
        assert await inject_bound_workflow_result(state, scope.run_id, snapshot) is False
    assert state.created == []
    assert f"workflow-{scope.run_id}" not in state._slots
    assert len(_cap_warnings(caplog)) == 1

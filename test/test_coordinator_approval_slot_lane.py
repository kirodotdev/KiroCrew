"""A coordinator (sub-agent / spawn) approval lights the owning slot's lane.

``ApprovalCoordinator.request`` parks its future on the STATE-level registry and
records the owning ``slot`` key; ``slot._approval_futures`` never sees it. The
slot projection must still report ``pending_approval`` / ``pending_approval_info``
for that slot, and the coordinator must push a slots update on both edges so
the Needs Approval lane, the command palette and Crew Companion's session watch
recompute without waiting for an unrelated push.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog

_WAIT_SECS = 30.0
_POLL_SECS = 0.01


def _record(approval_id: str, slot: str, *, tool: str = "spawn_run(t)") -> dict:
    return {
        "id": approval_id,
        "source": "subagent",
        "tool": tool,
        "tool_input": "task text",
        "tool_purpose": "",
        "slot": slot,
        "ts": 1.0,
    }


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    return state


async def _register(state: DashboardState, approval_id: str, slot_key: str) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(
        state.request_approval(approval_id, "subagent", "spawn_run(t)", slot=slot_key)
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT_SECS
    while approval_id not in state._pending_approvals:
        assert loop.time() < deadline, f"approval {approval_id!r} never registered"
        await asyncio.sleep(_POLL_SECS)
    return task


# --------------------------------------------------------------------------- #
# Projection: the slot dict reads coordinator records for its own key.
# --------------------------------------------------------------------------- #


def test_projection_reads_coordinator_record_for_this_slot() -> None:
    slot = _ChatSlot("parent")
    slot._coordinator_approvals = lambda key: [_record("spawn:a1", key)]

    payload = slot.to_dict()

    assert payload["pending_approval"] is True
    assert payload["pending_approval_info"] == {
        "tool": "spawn_run(t)",
        "tool_input": "task text",
        "tool_kind": "spawn",
        "request_id": "spawn:a1",
    }
    # A parked slot is not "waiting for input": the user has a decision, not a turn.
    assert payload["waiting_for_input"] is False


def test_projection_without_coordinator_records_is_unchanged() -> None:
    slot = _ChatSlot("parent")
    slot._coordinator_approvals = lambda key: []

    payload = slot.to_dict()

    assert payload["pending_approval"] is False
    assert payload["pending_approval_info"] is None


def test_projection_tolerates_an_unwired_slot() -> None:
    """A slot built outside DashboardState (tests, probes) has no callback."""
    slot = _ChatSlot("bare")

    payload = slot.to_dict()

    assert payload["pending_approval"] is False
    assert payload["pending_approval_info"] is None


def test_non_spawn_coordinator_record_has_empty_tool_kind() -> None:
    slot = _ChatSlot("parent")
    slot._coordinator_approvals = lambda key: [_record("req-7", key, tool="fs_write")]

    info = slot.to_dict()["pending_approval_info"]

    assert info["tool"] == "fs_write"
    assert info["tool_kind"] == ""
    assert info["request_id"] == "req-7"


def test_slot_registry_future_still_supplies_the_card_first() -> None:
    """The registry that owns a live future describes the card; the coordinator
    record is only the fallback for a slot whose own registry is empty."""
    slot = _ChatSlot("parent")
    meta = json.dumps({"tool_input": "ls", "tool_kind": "bash", "request_id": "r1"})
    slot.messages.append({"role": "permission", "content": "shell", "cls": meta, "ts": "t1"})
    loop = asyncio.new_event_loop()
    try:
        slot._approval_futures["r1"] = loop.create_future()
        slot._coordinator_approvals = lambda key: [_record("spawn:a1", key)]

        info = slot.to_dict()["pending_approval_info"]
    finally:
        loop.close()

    assert info["request_id"] == "r1"
    assert info["tool"] == "shell"


def test_stale_permission_row_does_not_describe_a_coordinator_approval() -> None:
    """A coordinator approval writes no transcript row, so an old unresolved
    row (from a turn whose future is long gone) must not be mistaken for it."""
    slot = _ChatSlot("parent")
    stale = json.dumps({"tool_input": "rm -rf x", "tool_kind": "bash", "request_id": "old"})
    slot.messages.append({"role": "permission", "content": "shell", "cls": stale, "ts": "t1"})
    slot._coordinator_approvals = lambda key: [_record("spawn:a1", key)]

    info = slot.to_dict()["pending_approval_info"]

    assert info["request_id"] == "spawn:a1"
    assert info["tool"] == "spawn_run(t)"


def test_projection_reads_only_the_oldest_record() -> None:
    slot = _ChatSlot("parent")
    slot._coordinator_approvals = lambda key: [
        _record("spawn:first", key, tool="spawn_run(one)"),
        _record("spawn:second", key, tool="spawn_run(two)"),
    ]

    info = slot.to_dict()["pending_approval_info"]

    assert info["request_id"] == "spawn:first"


# --------------------------------------------------------------------------- #
# State lookup: which records count for which slot.
# --------------------------------------------------------------------------- #


def test_pending_coordinator_approvals_filters_by_slot_and_liveness(tmp_path) -> None:
    state = _make_state(tmp_path)
    loop = asyncio.new_event_loop()
    try:
        live = loop.create_future()
        done = loop.create_future()
        done.set_result(True)
        state._approval_futures.update({"live": live, "done": done, "other": loop.create_future()})
        state._pending_approvals.update(
            {
                "live": _record("live", "parent"),
                "done": _record("done", "parent"),
                "other": _record("other", "sibling"),
                "orphan": _record("orphan", "parent"),  # record without a future
                "unowned": _record("unowned", ""),
            }
        )

        assert [r["id"] for r in state.pending_coordinator_approvals("parent")] == ["live"]
        assert [r["id"] for r in state.pending_coordinator_approvals("sibling")] == ["other"]
        assert state.pending_coordinator_approvals("") == []
        assert state.pending_coordinator_approvals("nobody") == []
    finally:
        loop.close()


def test_state_created_slot_is_wired_to_the_lookup(tmp_path) -> None:
    state = _make_state(tmp_path)

    slot = state.get_or_create_slot("parent")

    assert slot._coordinator_approvals == state.pending_coordinator_approvals


# --------------------------------------------------------------------------- #
# End to end through the coordinator: request, resolve, expire.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_spawn_approval_lights_only_the_owning_slot_and_clears_on_resolve(tmp_path):
    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")
    sibling = state.get_or_create_slot("sibling")
    state.push_slots_update.reset_mock()  # slot creation pushes; count from here
    task = await _register(state, "spawn:a1", parent.key)

    lit = state.serialize_slot(parent)
    assert lit["pending_approval"] is True
    assert lit["pending_approval_info"]["request_id"] == "spawn:a1"
    assert state.serialize_slot(sibling)["pending_approval"] is False
    # Registration itself pushed the lane recompute -- the request edge.
    assert state.push_slots_update.call_count == 1

    assert state.resolve_state_approval("spawn:a1", True) is True
    # The future is decided: the lane clears even before the record is popped.
    assert state.serialize_slot(parent)["pending_approval"] is False
    assert await task is True
    assert "spawn:a1" not in state._pending_approvals
    # The resolve edge pushed too, after the record was retired.
    assert state.push_slots_update.call_count == 2
    assert state.serialize_slot(parent)["pending_approval_info"] is None


@pytest.mark.asyncio
async def test_expired_spawn_approval_clears_the_lane_and_pushes(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")
    monkeypatch.setattr(state, "_APPROVAL_TIMEOUT", 0.01)
    state.push_slots_update.reset_mock()

    result = await state.request_approval("spawn:a2", "subagent", "spawn_run(t)", slot=parent.key)

    assert result is False
    assert state.serialize_slot(parent)["pending_approval"] is False
    assert state.push_slots_update.call_count == 2


@pytest.mark.asyncio
async def test_unowned_approval_lights_no_slot(tmp_path):
    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")
    task = await _register(state, "req-bg", "")

    assert state.serialize_slot(parent)["pending_approval"] is False

    assert state.resolve_state_approval("req-bg", False) is True
    assert await task is False


@pytest.mark.asyncio
async def test_push_failure_never_breaks_the_approval(tmp_path):
    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")
    state.push_slots_update = MagicMock(side_effect=RuntimeError("socket gone"))
    task = await _register(state, "spawn:a3", parent.key)

    assert state.serialize_slot(parent)["pending_approval"] is True
    assert ApprovalCoordinator.resolve_state(state, "spawn:a3", True) is True
    assert await task is True

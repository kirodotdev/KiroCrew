"""Per-chat agent-queue priority: the slot field, its route, its persistence,
and the lane priority source the subagent dispatcher reads.

The dispatcher side (strict tiers over the weighted round-robin) is covered in
``test_fairness_lanes``; this file covers how a chat's choice gets there.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard import chat_handlers as ch
from kiro_crew.dashboard.chat import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
    restore_recent_sessions,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog

ROUTE = "/api/chat/slots/{slot}/queue-priority"


def _make_state(tmp_path, subagents=None) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
        subagents=subagents,
    )


def _app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_patch(ROUTE, ch.api_chat_slot_queue_priority)
    return as_owner(app)


async def _patch(state, name, payload, headers=None):
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.patch(
            f"/api/chat/slots/{name}/queue-priority", json=payload, headers=headers or {}
        )
        return resp.status, await resp.json()


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


# ── the slot field ────────────────────────────────────────────────────────────


def test_new_slot_defaults_to_medium_and_projects_it() -> None:
    slot = _ChatSlot("fresh")
    assert slot.queue_priority == "medium"


# ── PATCH /api/chat/slots/{slot}/queue-priority ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("priority", ["low", "medium", "high"])
async def test_owner_sets_each_priority(state, priority) -> None:
    slot = state.get_or_create_slot("s1")
    slot.queue_priority = "low" if priority != "low" else "high"
    status, body = await _patch(state, "s1", {"priority": priority})
    assert status == 200, body
    assert body == {"ok": True, "queue_priority": priority, "changed": True}
    assert slot.queue_priority == priority
    assert slot._dirty is False


@pytest.mark.asyncio
async def test_same_value_reports_no_change_and_logs_allowed(state, monkeypatch) -> None:
    state.get_or_create_slot("s1")
    audit = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: audit)

    status, body = await _patch(state, "s1", {"priority": "medium"})

    assert status == 200 and body["changed"] is False
    audit.log_api_access.assert_called_once_with(
        caller="local-app",
        operation="chat.slot_queue_priority",
        outcome="allowed",
        source="dashboard",
        resources="slot=s1 priority=medium",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["urgent", "HIGH", "", None, 2, True, ["high"]])
async def test_unknown_values_are_rejected_and_nothing_changes(state, bad) -> None:
    slot = state.get_or_create_slot("s1")
    status, body = await _patch(state, "s1", {"priority": bad})
    assert status == 400 and body["code"] == "invalid_queue_priority"
    assert slot.queue_priority == "medium"


@pytest.mark.asyncio
async def test_unknown_slot_is_404(state) -> None:
    status, body = await _patch(state, "missing", {"priority": "high"})
    assert status == 404
    assert body["code"] == "slot_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [{"X-Test-App": "some-app"}, {"X-Test-User": "someone-else"}],
    ids=["app-token", "non-owner"],
)
@pytest.mark.parametrize("name", ["s1", "missing"], ids=["existing-slot", "absent-slot"])
async def test_only_the_owner_may_change_priority(state, headers, name) -> None:
    slot = state.get_or_create_slot("s1") if name == "s1" else None
    status, body = await _patch(state, name, {"priority": "high"}, headers)
    assert status == 403 and body["code"] == "owner_only"
    if slot is not None:
        assert slot.queue_priority == "medium"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["s1", "missing"], ids=["existing-slot", "absent-slot"])
async def test_crew_member_caller_is_refused(state, name) -> None:
    from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

    @web.middleware
    async def member(request, handler):
        request[MEMBER_CHAT_PRINCIPAL_KEY] = "member:crew-a"
        return await handler(request)

    slot = state.get_or_create_slot("s1") if name == "s1" else None
    app = _app(state)
    app.middlewares.append(member)
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch(
            f"/api/chat/slots/{name}/queue-priority", json={"priority": "high"}
        )
        body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "owner_only"
    if slot is not None:
        assert slot.queue_priority == "medium"


@pytest.mark.asyncio
async def test_patch_persists_priority_before_response(tmp_path, state) -> None:
    slot = state.get_or_create_slot("durable")
    slot.append("user", "hello")
    slot.drain()
    _save_slot_to_history(state, slot)

    status, body = await _patch(state, "durable", {"priority": "high"})

    assert status == 200, body
    assert _meta(tmp_path, "durable")["queue_priority"] == "high"


@pytest.mark.asyncio
async def test_refused_save_rolls_priority_back(state, monkeypatch) -> None:
    slot = state.get_or_create_slot("s1")
    slot.queue_priority = "low"
    monkeypatch.setattr(ch, "save_slot_off_loop", AsyncMock(return_value=False))

    status, body = await _patch(state, "s1", {"priority": "high"})

    assert status == 409
    assert body["code"] == "session_gone"
    assert slot.queue_priority == "low"
    assert slot._dirty is True


@pytest.mark.asyncio
async def test_failed_save_rolls_priority_back_without_publishing(state, monkeypatch) -> None:
    slot = state.get_or_create_slot("s1")
    slot.queue_priority = "low"
    slot._dirty = False
    monkeypatch.setattr(ch, "save_slot_off_loop", AsyncMock(side_effect=OSError("disk full")))
    publish = MagicMock()
    monkeypatch.setattr(state, "push_slot_patch", publish)

    status, body = await _patch(state, "s1", {"priority": "high"})

    assert status == 503
    assert body == {"error": "failed to save agent-queue priority", "code": "save_failed"}
    assert slot.queue_priority == "low"
    assert slot._dirty is True
    publish.assert_not_called()


@pytest.mark.asyncio
async def test_patch_synchronizes_slots_sharing_a_session(state, monkeypatch) -> None:
    first = state.get_or_create_slot("first")
    second = state.get_or_create_slot("second")
    first.linked_session_key = second.linked_session_key = "slack:C1:171"
    publish = MagicMock()
    monkeypatch.setattr(state, "push_slot_patch", publish)

    status, body = await _patch(state, "first", {"priority": "high"})

    assert status == 200, body
    assert first.queue_priority == second.queue_priority == "high"
    assert second._dirty is True
    assert state.lane_queue_priorities() == {"slack:C1:171": "high"}
    assert publish.call_args_list == [
        (("first", ("queue_priority",)),),
        (("second", ("queue_priority",)),),
    ]


# ── persistence ───────────────────────────────────────────────────────────────


def _meta(tmp_path, key: str) -> dict:
    path = tmp_path / f"dashboard_{key}.jsonl"
    return json.loads(path.read_text(encoding="utf-8").split("\n")[0])


def test_non_default_priority_is_saved_and_restored(tmp_path, state) -> None:
    slot = state.get_or_create_slot("prio")
    slot.queue_priority = "high"
    slot.append("user", "hello")
    slot.drain()
    _save_slot_to_history(state, slot)
    assert _meta(tmp_path, "prio")["queue_priority"] == "high"

    fresh = _make_state(tmp_path)
    assert restore_recent_sessions(fresh, window_minutes=60) == 1
    assert fresh._slots["prio"].queue_priority == "high"

    other = _make_state(tmp_path)
    restored = _rehydrate_slot_from_history(other, "prio")
    assert restored is not None
    assert restored.queue_priority == "high"


def test_default_priority_writes_no_key(tmp_path, state) -> None:
    slot = state.get_or_create_slot("plain")
    slot.append("user", "hello")
    slot.drain()
    _save_slot_to_history(state, slot)
    assert "queue_priority" not in _meta(tmp_path, "plain")


def test_tampered_priority_on_disk_restores_as_medium(tmp_path, state) -> None:
    path = tmp_path / "dashboard_bad.jsonl"
    meta = {"_type": "metadata", "created_at": "2026-03-23T10:00:00", "queue_priority": "max"}
    row = {"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}
    path.write_text(json.dumps(meta) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    fresh = _make_state(tmp_path)
    restored = _rehydrate_slot_from_history(fresh, "bad")
    assert restored is not None
    assert restored.queue_priority == "medium"


# ── the dispatcher's lane priority source ─────────────────────────────────────


def test_state_installs_itself_as_the_lane_priority_source(tmp_path) -> None:
    subagents = MagicMock()
    st = _make_state(tmp_path, subagents=subagents)
    subagents.set_lane_priority_source.assert_called_once_with(st.lane_queue_priorities)


def test_lane_priorities_list_only_non_default_slots_by_session_key(state) -> None:
    state.get_or_create_slot("a").queue_priority = "high"
    state.get_or_create_slot("b").queue_priority = "low"
    state.get_or_create_slot("c")  # medium: omitted
    linked = state.get_or_create_slot("d")
    linked.linked_session_key = "slack:C1:171"
    linked.queue_priority = "high"
    assert state.lane_queue_priorities() == {
        "dashboard:a": "high",
        "dashboard:b": "low",
        "slack:C1:171": "high",
    }


def test_lane_priority_collision_keeps_the_highest_tier(state) -> None:
    high = state.get_or_create_slot("high")
    low = state.get_or_create_slot("low")
    high.linked_session_key = low.linked_session_key = "slack:C1:171"
    high.queue_priority = "high"
    low.queue_priority = "low"

    assert state.lane_queue_priorities() == {"slack:C1:171": "high"}

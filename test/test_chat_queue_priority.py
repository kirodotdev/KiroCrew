"""Per-chat agent-queue priority: the per-lane store on DashboardState, its
route, what slots serialize, and the lane priority source the subagent
dispatcher reads.

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
SESSION = "slack:C1:171"


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


def _serialized(state: DashboardState, slot) -> object:
    return state.serialize_slot(slot)["queue_priority"]


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


# ── the stored tier ───────────────────────────────────────────────────────────


def test_the_slot_carries_no_tier_of_its_own() -> None:
    assert not hasattr(_ChatSlot("fresh"), "queue_priority")
    assert "queue_priority" not in _ChatSlot.__slots__


def test_a_new_slot_serializes_medium(state) -> None:
    slot = state.get_or_create_slot("fresh")
    assert state.queue_priority_of(slot) == "medium"
    assert _serialized(state, slot) == "medium"


def test_a_state_built_without_init_reads_medium() -> None:
    bare = DashboardState.__new__(DashboardState)
    bare._slots = {}
    assert bare.lane_queue_priorities() == {}
    assert bare.queue_priority_of(_ChatSlot("x")) == "medium"


# ── PATCH /api/chat/slots/{slot}/queue-priority ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("priority", ["low", "medium", "high"])
async def test_owner_sets_each_priority(state, priority) -> None:
    slot = state.get_or_create_slot("s1")
    state._session_queue_priority["dashboard:s1"] = "low" if priority != "low" else "high"
    status, body = await _patch(state, "s1", {"priority": priority})
    assert status == 200, body
    assert body == {"ok": True, "queue_priority": priority, "changed": True}
    assert state.queue_priority_of(slot) == priority
    assert _serialized(state, slot) == priority
    assert slot._dirty is False


@pytest.mark.asyncio
async def test_medium_leaves_no_entry(state) -> None:
    state.get_or_create_slot("s1")
    state._session_queue_priority["dashboard:s1"] = "high"
    status, body = await _patch(state, "s1", {"priority": "medium"})
    assert status == 200 and body["changed"] is True
    assert state._session_queue_priority == {}


@pytest.mark.asyncio
async def test_same_value_reports_no_change_and_logs_allowed(state, monkeypatch) -> None:
    state.get_or_create_slot("s1")
    audit = MagicMock()
    publish = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: audit)
    monkeypatch.setattr(state, "push_slot_patch", publish)

    status, body = await _patch(state, "s1", {"priority": "medium"})

    assert status == 200 and body == {"ok": True, "queue_priority": "medium", "changed": False}
    publish.assert_not_called()
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
    assert state.queue_priority_of(slot) == "medium"
    assert state._session_queue_priority == {}


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
    if name == "s1":
        state.get_or_create_slot("s1")
    status, body = await _patch(state, name, {"priority": "high"}, headers)
    assert status == 403 and body["code"] == "owner_only"
    assert state._session_queue_priority == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["s1", "missing"], ids=["existing-slot", "absent-slot"])
async def test_crew_member_caller_is_refused(state, name) -> None:
    from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

    @web.middleware
    async def member(request, handler):
        request[MEMBER_CHAT_PRINCIPAL_KEY] = "member:crew-a"
        return await handler(request)

    if name == "s1":
        state.get_or_create_slot("s1")
    app = _app(state)
    app.middlewares.append(member)
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch(
            f"/api/chat/slots/{name}/queue-priority", json={"priority": "high"}
        )
        body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "owner_only"
    assert state._session_queue_priority == {}


@pytest.mark.asyncio
async def test_patch_saves_nothing_and_reaches_the_dispatcher_at_once(
    tmp_path, state, monkeypatch
) -> None:
    """The tier is memory only, so the PATCH never writes the transcript."""
    slot = state.get_or_create_slot("durable")
    slot.append("user", "hello")
    slot.drain()
    _save_slot_to_history(state, slot)
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(ch, "save_slot_off_loop", save)

    status, body = await _patch(state, "durable", {"priority": "low"})

    assert status == 200, body
    save.assert_not_called()
    assert "queue_priority" not in _meta(tmp_path, "durable")
    assert state.lane_queue_priorities() == {"dashboard:durable": "low"}


@pytest.mark.asyncio
async def test_a_pick_applies_to_every_slot_on_the_session(state, monkeypatch) -> None:
    first = state.get_or_create_slot("first")
    second = state.get_or_create_slot("second")
    other = state.get_or_create_slot("other")
    first.linked_session_key = second.linked_session_key = SESSION
    publish = MagicMock()
    monkeypatch.setattr(state, "push_slot_patch", publish)

    status, body = await _patch(state, "first", {"priority": "high"})

    assert status == 200, body
    assert state.queue_priority_of(first) == state.queue_priority_of(second) == "high"
    assert _serialized(state, second) == "high"
    assert state.queue_priority_of(other) == "medium"
    assert state.lane_queue_priorities() == {SESSION: "high"}
    assert publish.call_args_list == [
        (("first", ("queue_priority",)),),
        (("second", ("queue_priority",)),),
    ]


@pytest.mark.asyncio
async def test_a_cron_bound_chat_has_no_tier(state, monkeypatch) -> None:
    slot = state.get_or_create_slot("cronchat", linked_session_key="cron:job1")
    slot.linked_session_key = "cron:job1"
    publish = MagicMock()
    monkeypatch.setattr(state, "push_slot_patch", publish)

    status, body = await _patch(state, "cronchat", {"priority": "high"})

    assert status == 409 and body["code"] == "queue_priority_system_lane"
    assert state._session_queue_priority == {}
    publish.assert_not_called()
    assert state.queue_priority_of(slot) is None
    assert _serialized(state, slot) is None


# ── a slot joining a session after a pick ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("picked", ["low", "high"])
async def test_a_slot_minted_onto_the_session_later_reports_its_tier(state, picked) -> None:
    owner = state.get_or_create_slot("owner")
    owner.linked_session_key = SESSION
    assert (await _patch(state, "owner", {"priority": picked}))[0] == 200

    later = state.get_or_create_slot("later", linked_session_key=SESSION)
    later.linked_session_key = SESSION

    assert state.queue_priority_of(later) == picked
    assert _serialized(state, later) == picked


@pytest.mark.asyncio
@pytest.mark.parametrize("picked", ["low", "high"])
async def test_a_slot_linked_afterwards_reports_its_tier(state, picked) -> None:
    owner = state.get_or_create_slot("owner")
    owner.linked_session_key = SESSION
    assert (await _patch(state, "owner", {"priority": picked}))[0] == 200
    later = state.get_or_create_slot("later")
    assert state.queue_priority_of(later) == "medium"

    later.linked_session_key = SESSION

    assert state.queue_priority_of(later) == picked


def test_a_slot_rehydrated_onto_a_live_session_reports_its_tier(tmp_path) -> None:
    path = tmp_path / "dashboard_restored.jsonl"
    meta = {
        "_type": "metadata",
        "created_at": "2026-03-23T10:00:00",
        "linked_session_key": SESSION,
    }
    row = {"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}
    path.write_text(json.dumps(meta) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    st = _make_state(tmp_path)
    live = st.get_or_create_slot("live")
    live.linked_session_key = SESSION
    st._session_queue_priority[SESSION] = "high"

    restored = _rehydrate_slot_from_history(st, "restored")

    assert restored is not None
    assert restored.linked_session_key == SESSION
    assert st.queue_priority_of(restored) == "high"


# ── persistence ───────────────────────────────────────────────────────────────


def _meta(tmp_path, key: str) -> dict:
    path = tmp_path / f"dashboard_{key}.jsonl"
    return json.loads(path.read_text(encoding="utf-8").split("\n")[0])


@pytest.mark.parametrize("priority", ["low", "high"])
def test_no_tier_is_saved_and_a_restart_starts_at_medium(tmp_path, state, priority) -> None:
    slot = state.get_or_create_slot("prio")
    state._session_queue_priority["dashboard:prio"] = priority
    slot.append("user", "hello")
    slot.drain()
    _save_slot_to_history(state, slot)
    assert "queue_priority" not in _meta(tmp_path, "prio")
    assert state.queue_priority_of(slot) == priority

    fresh = _make_state(tmp_path)
    assert restore_recent_sessions(fresh, window_minutes=60) == 1
    assert fresh.queue_priority_of(fresh._slots["prio"]) == "medium"

    other = _make_state(tmp_path)
    restored = _rehydrate_slot_from_history(other, "prio")
    assert restored is not None
    assert other.queue_priority_of(restored) == "medium"


@pytest.mark.parametrize("written", ["low", "medium", "high", "max"])
def test_a_tier_written_into_the_transcript_is_ignored(tmp_path, written) -> None:
    """Transcript metadata is agent-writable, so no tier is ever read back from it.

    An agent that rewrites an owner's ``low`` to ``medium`` (or plants ``high``)
    changes nothing: every restore starts at Medium.
    """
    path = tmp_path / "dashboard_forged.jsonl"
    meta = {"_type": "metadata", "created_at": "2026-03-23T10:00:00", "queue_priority": written}
    row = {"role": "user", "content": "hi", "ts": "2026-03-23T10:00:00"}
    path.write_text(json.dumps(meta) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    st = _make_state(tmp_path)
    restored = _rehydrate_slot_from_history(st, "forged")
    assert restored is not None
    assert st.queue_priority_of(restored) == "medium"
    assert st._session_queue_priority == {}


# ── the dispatcher's lane priority source ─────────────────────────────────────


def test_state_installs_itself_as_the_lane_priority_source(tmp_path) -> None:
    subagents = MagicMock()
    st = _make_state(tmp_path, subagents=subagents)
    subagents.set_lane_priority_source.assert_called_once_with(st.lane_queue_priorities)


def test_lane_priorities_report_only_lanes_with_a_live_slot(state) -> None:
    state.get_or_create_slot("a")
    linked = state.get_or_create_slot("d")
    linked.linked_session_key = SESSION
    state._session_queue_priority.update(
        {"dashboard:a": "high", SESSION: "low", "dashboard:closed": "high"}
    )

    assert state.lane_queue_priorities() == {"dashboard:a": "high", SESSION: "low"}
    # Side-effect free: the stale entry is filtered, never deleted, from here.
    assert "dashboard:closed" in state._session_queue_priority

    linked.linked_session_key = ""
    assert state.lane_queue_priorities() == {"dashboard:a": "high"}


@pytest.mark.asyncio
async def test_a_pick_drops_the_tiers_of_closed_chats(state) -> None:
    """The map is bounded by the live slots: a PATCH prunes lanes none maps to."""
    state.get_or_create_slot("a")
    state.get_or_create_slot("b")
    state._session_queue_priority.update(
        {"dashboard:a": "low", "dashboard:closed1": "high", "dashboard:closed2": "low"}
    )
    status, body = await _patch(state, "b", {"priority": "high"})
    assert status == 200, body
    assert state._session_queue_priority == {"dashboard:a": "low", "dashboard:b": "high"}

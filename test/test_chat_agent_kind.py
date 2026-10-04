"""An explicit selection namespace decides between a same-name member and template."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes, drain_background_tasks
from dashboard_owner_helpers import as_owner
from test_chat_agent_selection import TEMPLATE, _template_chat, _turn_state

from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.execution_context import read_session_execution
from kiro_crew.member_memory_auth import read_private_session_store


async def _same_name_state(tmp_path, monkeypatch):
    """A private member named exactly like an installed shared template."""
    state, _slot, private_store = await _template_chat(tmp_path, monkeypatch, first_turn=False)
    cfg = KiroCrewConfig.load()
    assert cfg.agents[TEMPLATE].memory_store == private_store
    return state, private_store


@pytest.mark.asyncio
async def test_explicit_template_create_never_pins_the_same_name_member(tmp_path, monkeypatch):
    state, private_store = await _same_name_state(tmp_path, monkeypatch)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots",
            json={"name": "kind-template", "agent": TEMPLATE, "agent_kind": "template"},
        )
        assert response.status == 200, await response.text()
        body = await response.json()
    assert body["agent"] == TEMPLATE
    assert body["agent_kind"] == "template"
    slot = state._slots["kind-template"]
    assert slot.agent_kind == "template"
    assert slot.memory_store != private_store
    assert read_private_session_store("dashboard:kind-template") is None
    execution = read_session_execution("dashboard:kind-template")
    assert execution is not None
    assert execution.selection_kind == "template"
    assert execution.member_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["persistent", "incognito"])
async def test_agentless_template_kind_create_keeps_shared_memory_through_the_first_send(
    tmp_path, monkeypatch, memory_mode
):
    """A stated template kind on the default name is a template pick, not the member."""
    from kiro_crew.execution_context import _LIVE_EXECUTIONS, _live_key

    state, private_store = await _same_name_state(tmp_path, monkeypatch)
    cfg = KiroCrewConfig.load()
    cfg.default_agent = TEMPLATE
    cfg.save()
    monkeypatch.setattr(live, "snapshot", KiroCrewConfig.load)
    monkeypatch.setattr(chat_handlers, "_maybe_auto_title", AsyncMock())
    monkeypatch.setattr(chat_handlers, "maybe_auto_tag", AsyncMock())
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **kw: None)
    app = _make_app_with_agent_routes(state)
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    key = "dashboard:kind-default"
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots",
            json={
                "name": "kind-default",
                "agent_kind": "template",
                "memory_mode": memory_mode,
            },
        )
        assert response.status == 200, await response.text()
        slot = state._slots["kind-default"]
        assert slot.agent == TEMPLATE
        if memory_mode == "incognito":
            _LIVE_EXECUTIONS.pop(_live_key(key), None)
            assert read_session_execution(key) is None
        response = await client.post(
            "/api/chat?ws=1", json={"slot": "kind-default", "message": "hello"}
        )
        assert response.status == 200, await response.text()
        await asyncio.wait_for(slot.task, timeout=5)
        await asyncio.wait_for(drain_background_tasks(state), timeout=5)
    assert slot.memory_store != private_store
    assert read_private_session_store(key) is None
    execution = read_session_execution(key)
    assert execution is not None
    assert execution.selection_kind == "template"
    assert execution.member_id is None


@pytest.mark.asyncio
async def test_explicit_member_create_binds_the_member_on_the_first_send(tmp_path, monkeypatch):
    state, private_store = await _same_name_state(tmp_path, monkeypatch)
    monkeypatch.setattr(live, "snapshot", KiroCrewConfig.load)
    monkeypatch.setattr(chat_handlers, "_run_chat", AsyncMock())
    monkeypatch.setattr(chat_handlers, "_maybe_auto_title", AsyncMock())
    monkeypatch.setattr(chat_handlers, "maybe_auto_tag", AsyncMock())
    app = _make_app_with_agent_routes(state)
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots",
            json={"name": "kind-member", "agent": TEMPLATE, "agent_kind": "member"},
        )
        assert response.status == 200, await response.text()
        body = await response.json()
        assert read_session_execution("dashboard:kind-member") is None
        response = await client.post(
            "/api/chat?ws=1", json={"slot": "kind-member", "message": "hello"}
        )
        assert response.status == 200, await response.text()
        await asyncio.wait_for(drain_background_tasks(state), timeout=5)
    assert body["agent_kind"] == "member"
    assert state._slots["kind-member"].agent_kind == "member"
    execution = read_session_execution("dashboard:kind-member")
    assert execution is not None
    assert execution.selection_kind == "member"
    assert execution.store.store_id == private_store


@pytest.mark.asyncio
async def test_explicit_template_switch_on_empty_chat_keeps_shared_memory(tmp_path, monkeypatch):
    state, private_store = await _same_name_state(tmp_path, monkeypatch)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json={"name": "switch-template"})
        assert response.status == 200, await response.text()
        response = await client.post(
            "/api/chat/slots/switch-template/agent",
            json={"agent": TEMPLATE, "agent_kind": "template"},
        )
        assert response.status == 200, await response.text()
        body = await response.json()
    assert body["agent"] == TEMPLATE
    assert body["agent_kind"] == "template"
    slot = state._slots["switch-template"]
    assert slot.agent == TEMPLATE
    assert slot.memory_store != private_store
    assert read_private_session_store("dashboard:switch-template") is None
    execution = read_session_execution("dashboard:switch-template")
    assert execution is not None and execution.selection_kind == "template"


@pytest.mark.asyncio
async def test_explicit_member_switch_on_empty_chat_leaves_it_unbound(tmp_path, monkeypatch):
    state, private_store = await _same_name_state(tmp_path, monkeypatch)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json={"name": "switch-member"})
        assert response.status == 200, await response.text()
        response = await client.post(
            "/api/chat/slots/switch-member/agent",
            json={"agent": TEMPLATE, "agent_kind": "member"},
        )
        assert response.status == 200, await response.text()
        assert (await response.json())["agent_kind"] == "member"
    assert state._slots["switch-member"].memory_store != private_store
    assert read_session_execution("dashboard:switch-member") is None


@pytest.mark.asyncio
async def test_member_switch_after_a_template_pick_binds_the_member_after_a_restart(
    tmp_path, monkeypatch
):
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history, restore_open_slots
    from kiro_crew.dashboard.chat_utils import slot_history_key

    state, private_store = await _same_name_state(tmp_path, monkeypatch)
    key = "dashboard:rekind"
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **kw: None)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await client.post("/api/chat/slots", json={"name": "rekind"})
        assert response.status == 200, await response.text()
        response = await client.post(
            "/api/chat/slots/rekind/agent", json={"agent": TEMPLATE, "agent_kind": "template"}
        )
        assert response.status == 200, await response.text()
        slot = state._slots["rekind"]
        _save_slot_to_history(state, slot, force=True)
        history = slot_history_key(slot)
        assert state.conversation_log.get_metadata(history).get("agent_kind") == "template"
        response = await client.post(
            "/api/chat/slots/rekind/agent", json={"agent": TEMPLATE, "agent_kind": "member"}
        )
        assert response.status == 200, await response.text()
        assert (await response.json())["agent_kind"] == "member"
    assert read_session_execution(key) is None
    assert state.conversation_log.get_metadata(history).get("agent_kind") == "member"
    state._persist_open_slots()

    restarted = _turn_state(tmp_path, monkeypatch)
    assert restore_open_slots(restarted) == 1
    slot = restarted._slots["rekind"]
    assert slot.agent_kind == "member"
    monkeypatch.setattr(live, "snapshot", KiroCrewConfig.load)
    monkeypatch.setattr(chat_handlers, "_maybe_auto_title", AsyncMock())
    monkeypatch.setattr(chat_handlers, "maybe_auto_tag", AsyncMock())
    app = _make_app_with_agent_routes(restarted)
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat?ws=1", json={"slot": "rekind", "message": "hello"})
        assert response.status == 200, await response.text()
        await asyncio.wait_for(slot.task, timeout=5)
        await asyncio.wait_for(drain_background_tasks(restarted), timeout=5)
    assert read_private_session_store(key) == private_store
    execution = read_session_execution(key)
    assert execution is not None and execution.selection_kind == "member"


@pytest.mark.asyncio
async def test_member_recreate_after_a_template_pick_binds_the_member_after_a_restart(
    tmp_path, monkeypatch
):
    from kiro_crew.dashboard.chat_persistence import restore_open_slots
    from kiro_crew.dashboard.chat_utils import slot_history_key

    state, private_store = await _same_name_state(tmp_path, monkeypatch)
    key = "dashboard:recreated-kind"
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **kw: None)
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await client.post(
            "/api/chat/slots",
            json={
                "name": "recreated-kind",
                "agent": TEMPLATE,
                "agent_kind": "template",
            },
        )
        assert response.status == 200, await response.text()
        stale = state._slots.pop("recreated-kind")
        history = slot_history_key(stale)
        await asyncio.to_thread(
            state.conversation_log.update_metadata,
            history,
            {"agent": TEMPLATE, "agent_kind": "template"},
        )
        assert state.conversation_log.get_metadata(history).get("agent_kind") == "template"
        assert read_session_execution(key).selection_kind == "template"
        response = await client.post(
            "/api/chat/slots",
            json={
                "name": "recreated-kind",
                "agent": TEMPLATE,
                "agent_kind": "member",
            },
        )
        assert response.status == 200, await response.text()
        assert (await response.json())["agent_kind"] == "member"
    assert read_session_execution(key) is None
    assert state.conversation_log.get_metadata(history).get("agent_kind") == "member"
    state._persist_open_slots()

    restarted = _turn_state(tmp_path, monkeypatch)
    assert restore_open_slots(restarted) == 1
    slot = restarted._slots["recreated-kind"]
    assert slot.agent_kind == "member"
    monkeypatch.setattr(live, "snapshot", KiroCrewConfig.load)
    monkeypatch.setattr(chat_handlers, "_maybe_auto_title", AsyncMock())
    monkeypatch.setattr(chat_handlers, "maybe_auto_tag", AsyncMock())
    app = _make_app_with_agent_routes(restarted)
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat?ws=1", json={"slot": "recreated-kind", "message": "hello"}
        )
        assert response.status == 200, await response.text()
        await asyncio.wait_for(slot.task, timeout=5)
        await asyncio.wait_for(drain_background_tasks(restarted), timeout=5)
    assert read_private_session_store(key) == private_store
    execution = read_session_execution(key)
    assert execution is not None and execution.selection_kind == "member"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["create", "switch"])
async def test_stated_kind_that_does_not_resolve_is_refused(tmp_path, monkeypatch, route):
    """A stated namespace never silently falls back to the default agent."""
    state = _turn_state(tmp_path, monkeypatch)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        if route == "create":
            response = await client.post(
                "/api/chat/slots",
                json={"name": "missing-kind", "agent": "not-installed", "agent_kind": "member"},
            )
            # Refused BEFORE the mint: a refused create leaves no phantom slot
            # behind for the next slots frame to advertise.
            assert "missing-kind" not in state._slots
        else:
            created = await client.post("/api/chat/slots", json={"name": "missing-kind"})
            assert created.status == 200, await created.text()
            prior = state._slots["missing-kind"].agent
            response = await client.post(
                "/api/chat/slots/missing-kind/agent",
                json={"agent": "not-installed", "agent_kind": "template"},
            )
            assert state._slots["missing-kind"].agent == prior
        assert response.status == 409, await response.text()
        assert (await response.json())["code"] == "agent_choice_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["create", "switch"])
async def test_unknown_kind_is_rejected_before_any_mutation(tmp_path, monkeypatch, route):
    state = _turn_state(tmp_path, monkeypatch)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        if route == "create":
            response = await client.post(
                "/api/chat/slots", json={"name": "bad-kind", "agent_kind": "crew"}
            )
            assert "bad-kind" not in state._slots
        else:
            created = await client.post("/api/chat/slots", json={"name": "bad-kind"})
            assert created.status == 200, await created.text()
            response = await client.post(
                "/api/chat/slots/bad-kind/agent", json={"agent": "x", "agent_kind": "crew"}
            )
        assert response.status == 400
        assert (await response.json())["code"] == "invalid_agent_kind"


@pytest.mark.asyncio
async def test_member_thread_refuses_the_same_name_template_kind(tmp_path, monkeypatch):
    """The member pin covers the namespace, not just the name.

    A same-name switch on a member DM thread is an allowed session reset -- in
    the MEMBER namespace. Picked as a template, the same name would run the
    shared template and detach the thread from the member's memory, which is a
    re-bind by another spelling and is refused like any other re-bind.
    """
    from kiro_crew.members import DM_SLOT_MODE, write_dm_binding

    state, private_store = await _same_name_state(tmp_path, monkeypatch)
    key = f"member-{TEMPLATE}"
    write_dm_binding(TEMPLATE, member=TEMPLATE, slot_key=key)
    slot = state.get_or_create_slot(key, agent=TEMPLATE, mode=DM_SLOT_MODE)
    slot.memory_store = private_store
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            f"/api/chat/slots/{key}/agent", json={"agent": TEMPLATE, "agent_kind": "template"}
        )
        assert response.status == 409, await response.text()
        assert (await response.json())["code"] == "member_thread_agent_pinned"
    # The pin held: nothing rebound the thread or its memory.
    assert slot.agent == TEMPLATE
    assert slot.agent_kind == ""
    assert slot.memory_store == private_store


@pytest.mark.asyncio
async def test_slot_projection_carries_the_selection_namespace(tmp_path, monkeypatch):
    state, _private_store = await _same_name_state(tmp_path, monkeypatch)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots",
            json={"name": "projected", "agent": TEMPLATE, "agent_kind": "template"},
        )
        assert response.status == 200, await response.text()
        listing = await client.get("/api/chat/slots")
        assert listing.status == 200
        rows = await listing.json()
    slots = rows if isinstance(rows, list) else rows.get("slots", rows)
    row = next(s for s in slots if s["key"] == "projected")
    assert row["agent_kind"] == "template"

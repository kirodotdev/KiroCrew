"""An explicit selection namespace decides between a same-name member and template."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes
from dashboard_owner_helpers import as_owner
from test_chat_agent_selection import TEMPLATE, _template_chat, _turn_state

from kiro_crew.config.loader import KiroCrewConfig
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
async def test_explicit_member_create_pins_the_member(tmp_path, monkeypatch):
    state, private_store = await _same_name_state(tmp_path, monkeypatch)
    app = _make_app_with_agent_routes(state)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots",
            json={"name": "kind-member", "agent": TEMPLATE, "agent_kind": "member"},
        )
        assert response.status == 200, await response.text()
        body = await response.json()
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
async def test_explicit_member_switch_on_empty_chat_pins_the_member(tmp_path, monkeypatch):
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
    execution = read_session_execution("dashboard:switch-member")
    assert execution is not None
    assert execution.selection_kind == "member"
    assert execution.store.store_id == private_store


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

"""Crew Mode workers never ask the person; the conductor does, and a worker's tool
approval is decided on its conductor's approval card (edge review R1: S2, S4, and
owner decisions A and B).

Every case names the rule it guards and fails when that rule is removed.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import crew_mode
from kiro_crew.dashboard.chat_handlers import api_chat_mode, api_chat_slot_approve
from kiro_crew.dashboard.state import SlotOrigin


@pytest.fixture(autouse=True)
def _routing_live(_floor_monkeypatch):
    # The routing is off on main until the Crew panel turns it on; these tests pin
    # the rules as they run then.
    from kiro_crew import crew_mode

    _floor_monkeypatch.setattr(crew_mode, "APPROVAL_ROUTING_LIVE", True)


@pytest.fixture(autouse=True)
def _clean(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    _floor_monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    yield


def _live(key: str, agent: str, created_by: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        agent=agent,
        agent_kind="template",
        crew_mode_agent="",
        mode="",
        memory_mode="persistent",
        _created_by=created_by,
        _saved_created_by="",
        _app="",
        instance_id="",
    )


# ── S2: a worker's ask_question goes to its conductor ────────────────────────


def test_S2_a_worker_question_card_is_refused_with_the_way_to_ask_its_conductor():
    from kiro_crew.dashboard.handlers import ask_question as aq

    state = SimpleNamespace(
        _slots={
            "root": _live("root", "kirocrew-conductor"),
            "w": _live("w", "kirocrew-worker", created_by="root"),
            "plain": _live("plain", "kirocrew"),
        }
    )
    refused = aq._crew_worker_refusal(state, "w", "dashboard:w")
    assert refused is not None and refused.status == 409
    body = json.loads(refused.text)
    assert body["code"] == "crew_worker_asks_conductor"
    assert "work_report" in body["error"] and "question" in body["error"]
    # The conductor and a plain chat still ask the person.
    assert aq._crew_worker_refusal(state, "root", "dashboard:root") is None
    assert aq._crew_worker_refusal(state, "plain", "dashboard:plain") is None


def test_F2_a_worker_of_a_closed_conductor_is_told_why_no_answer_comes_yet(monkeypatch):
    from kiro_crew.dashboard.handlers import ask_question as aq

    # The root's tab is closed: the stamp survives (known roots) but no slot.
    monkeypatch.setattr(crew_mode, "_known_roots", {"root"})
    state = SimpleNamespace(_slots={"w": _live("w", "kirocrew-worker", created_by="root")})
    refused = aq._crew_worker_refusal(state, "w", "dashboard:w")
    # Owner decision: a worker never asks the person, even with its root closed.
    assert refused is not None and refused.status == 409
    body = json.loads(refused.text)
    assert body["conductor_open"] is False
    assert body["error"] == aq.CREW_WORKER_ASK_CLOSED_ERROR
    assert "closed" in body["error"] and "work_report" in body["error"]
    # With the root live the usual instruction stands.
    state._slots["root"] = _live("root", "kirocrew-conductor")
    body = json.loads(aq._crew_worker_refusal(state, "w", "dashboard:w").text)
    assert body["conductor_open"] is True and body["error"] == aq.CREW_WORKER_ASK_ERROR


def test_S2_both_question_routes_refuse_a_worker_before_any_card_opens():
    from kiro_crew.dashboard.handlers import ask_question as aq

    source = open(aq.__file__, encoding="utf-8").read()
    mcp_open = source.split("async def api_agent_ask_open", 1)[1]
    assert mcp_open.index("_crew_worker_refusal(") < mcp_open.index("state.open_agent_ask(")
    browser = source.split("async def api_ask_question(", 1)[1].split("async def ", 1)[0]
    assert "_crew_worker_refusal(" in browser


def test_S2_the_tool_hands_the_worker_the_gateways_instruction(monkeypatch):
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import control

    monkeypatch.setattr(
        mcp_core,
        "_post",
        lambda *a, **k: {
            "error": "Call work_report with status 'question'.",
            "code": "crew_worker_asks_conductor",
        },
    )
    out = control._ask_question_blocking("dashboard:w", [{"question": "q"}])
    assert out == "Call work_report with status 'question'."
    assert "plain text" not in out


# ── B / S4: the approval card ───────────────────────────────────────────────


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


@web.middleware
async def _owner(request: web.Request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


@web.middleware
async def _app_token(request: web.Request, handler):
    request["app"] = "some-app"
    request["user"] = "local-app"
    return await handler(request)


def _server(state, middleware) -> TestServer:
    from kiro_crew.dashboard.handlers.crew_workers import api_crew_worker_approve

    app = web.Application(middlewares=[middleware])
    app["state"] = state
    app.router.add_post("/api/chat/mode", api_chat_mode)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_post(
        "/api/chat/slots/{slot}/crew-workers/{worker}/approve", api_crew_worker_approve
    )
    return TestServer(app)


def _crew(state, request_id: str = "rid-1", grantable: bool = True):
    root = state.get_or_create_slot("root", origin=SlotOrigin.USER)
    root.agent = "kirocrew-conductor"
    worker = state.get_or_create_slot("w", origin=SlotOrigin.USER)
    worker.agent = "kirocrew-worker"
    worker._created_by = "root"
    other = state.get_or_create_slot("other-root", origin=SlotOrigin.USER)
    other.agent = "kirocrew-conductor"
    fut = asyncio.get_running_loop().create_future()
    meta = {"request_id": request_id, **({"trust_grantable": "1"} if grantable else {})}
    row = worker.append("permission", "Shell", json.dumps(meta))
    worker.register_approval(request_id, fut, row)
    return worker, fut, worker.approval_instance(request_id)


@pytest.mark.asyncio
async def test_B_approve_resolves_the_workers_request_once_pinned_to_the_card(state):
    worker, fut, mid = _crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "approve", "request_id": "rid-1", "request_mid": mid},
        )
        assert resp.status == 200, await resp.text()
    assert fut.done() and fut.result() == "approved"
    assert not worker._trust


@pytest.mark.asyncio
async def test_B_reject_denies_it_once(state):
    _worker, fut, mid = _crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "reject", "request_id": "rid-1", "request_mid": mid},
        )
        assert resp.status == 200, await resp.text()
    assert fut.done() and fut.result() == "rejected_once"


@pytest.mark.asyncio
async def test_B_trust_this_worker_trusts_the_worker_not_its_conductor(state):
    worker, fut, _mid = _crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "trust", "request_id": "rid-1"},
        )
        assert resp.status == 200, await resp.text()
    assert fut.done() and fut.result() in ("approved", "trust")
    assert worker._trust is True
    assert not state._slots["root"]._trust


@pytest.mark.asyncio
async def test_F1_card_trust_does_not_pass_to_the_sessions_the_worker_creates(state):
    from kiro_crew.dashboard.approval_posture import trust_carries

    worker, _fut, _mid = _crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "trust", "request_id": "rid-1"},
        )
        assert resp.status == 200, await resp.text()
    assert worker._trust is True and worker._trust_card_scoped is True
    # A session this worker creates later is born asking.
    assert trust_carries(worker) is False


@pytest.mark.asyncio
async def test_F1_a_directly_trusted_session_still_passes_its_trust_on(state):
    from kiro_crew.dashboard.approval_posture import trust_carries

    worker, _fut, _mid = _crew(state)
    root = state._slots["root"]
    # A conductor the person trusted directly: its workers inherit, as before.
    root._trust = True
    assert trust_carries(root) is True
    # A trust pressed on the slot's own approval (not the card) also carries.
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/w/approve", json={"action": "trust", "request_id": "rid-1"}
        )
        assert resp.status == 200, await resp.text()
    assert worker._trust is True and worker._trust_card_scoped is False
    assert trust_carries(worker) is True


@pytest.mark.asyncio
async def test_G1_card_trust_stores_auto_so_subagents_are_trusted(state):
    from kiro_crew.dashboard.chat_turn.tool_approval import (
        _persistable_session_policy,
        _slot_is_trusted,
    )
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key

    worker, _fut, _mid = _crew(state)
    key = effective_session_key(worker)
    state.sessions.set_approval_policy.reset_mock()
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "trust", "request_id": "rid-1"},
        )
        assert resp.status == 200, await resp.text()
    assert worker._trust is True and worker._trust_card_scoped is True
    # The stored policy is what spawn_run's parent_trusted and every subagent's
    # own approval policy read: a card trust covers the worker's subagents.
    calls = [c.args for c in state.sessions.set_approval_policy.call_args_list]
    assert calls[-1] == (key, "auto")
    # The per-turn rewrite (turn_admission) writes the same value.
    assert _persistable_session_policy(worker, False) == "auto"
    assert _slot_is_trusted(worker)
    # Revoking the worker's trust stops it reaching a subagent spawned after.
    state.sessions.set_approval_policy.reset_mock()
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "w"})
        assert resp.status == 200, await resp.text()
    calls = [c.args for c in state.sessions.set_approval_policy.call_args_list]
    assert (key, "auto") not in calls and (key, "") in calls
    assert _persistable_session_policy(worker, False) == ""
    # Yolo stays process-wide.
    assert _persistable_session_policy(worker, True) == "auto"


@pytest.mark.asyncio
async def test_G1_direct_trust_still_stores_auto_for_inheritance(state):
    from kiro_crew.dashboard.chat_turn.tool_approval import _persistable_session_policy
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key

    worker, _fut, _mid = _crew(state)
    state.sessions.set_approval_policy.reset_mock()
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/w/approve", json={"action": "trust", "request_id": "rid-1"}
        )
        assert resp.status == 200, await resp.text()
    calls = [c.args for c in state.sessions.set_approval_policy.call_args_list]
    assert (effective_session_key(worker), "auto") in calls
    assert _persistable_session_policy(worker, False) == "auto"
    root = state._slots["root"]
    root._trust = True
    assert _persistable_session_policy(root, False) == "auto"


def _coordinator(state, approval_id: str = "spawn:abc", instance: str = "a1b2c3"):
    fut = asyncio.get_running_loop().create_future()
    state._approval_futures[approval_id] = fut
    state._pending_approvals[approval_id] = {
        "id": approval_id,
        "instance": instance,
        "slot": "w",
        "tool": "spawn_run",
        "tool_input": "",
    }
    return fut


@pytest.mark.asyncio
async def test_F7_a_coordinator_approval_from_the_card_is_pinned_to_its_instance(state):
    _crew(state)
    fut = _coordinator(state)
    async with TestClient(_server(state, _owner)) as client:
        base = "/api/chat/slots/root/crew-workers/w/approve"
        # No pin: refused, nothing decided.
        resp = await client.post(base, json={"action": "approve", "request_id": "spawn:abc"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "approval_target_required"
        # A stale card's instance: refused, nothing decided.
        resp = await client.post(
            base,
            json={"action": "approve", "request_id": "spawn:abc", "request_instance": "ffff"},
        )
        assert resp.status == 404
        assert not fut.done()
        # Trust is not a card decision for a coordinator request.
        resp = await client.post(
            base,
            json={"action": "trust", "request_id": "spawn:abc", "request_instance": "a1b2c3"},
        )
        assert resp.status == 400
        assert not fut.done()
        resp = await client.post(
            base,
            json={"action": "approve", "request_id": "spawn:abc", "request_instance": "a1b2c3"},
        )
        assert resp.status == 200, await resp.text()
    assert fut.done() and fut.result() is True


@pytest.mark.asyncio
async def test_F7_a_native_card_decision_without_a_mid_is_refused(state):
    _worker, fut, _mid = _crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "approve", "request_id": "rid-1"},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "approval_target_required"
    assert not fut.done()


def test_F7_the_coordinator_card_carries_its_record_instance():
    from kiro_crew.dashboard.handlers import crew_workers

    row = {
        "pending_approval": True,
        "pending_approval_info": {
            "origin": "coordinator",
            "request_id": "spawn:abc",
            "request_instance": "a1b2c3",
            "tool": "spawn_run",
        },
    }
    card = crew_workers._approval_of(row)
    assert card is not None and card["request_instance"] == "a1b2c3"
    row["pending_approval_info"]["request_instance"] = "<not hex>"
    assert crew_workers._approval_of(row)["request_instance"] == ""


@pytest.mark.asyncio
async def test_B_a_stale_card_cannot_pick_a_request_that_replaced_it(state):
    _worker, fut, _mid = _crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "approve", "request_id": "rid-1", "request_mid": "stale-mid"},
        )
        assert resp.status == 404
    assert not fut.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body", "status", "code"),
    [
        # Another root's card cannot reach this root's worker.
        (
            "/api/chat/slots/other-root/crew-workers/w/approve",
            {"action": "approve"},
            404,
            "not_a_worker",
        ),
        # Nothing wider than the card's three decisions.
        ("/api/chat/slots/root/crew-workers/w/approve", {"action": "yolo"}, 400, "invalid_action"),
        (
            "/api/chat/slots/root/crew-workers/w/approve",
            {"action": "trust_command"},
            400,
            "invalid_action",
        ),
    ],
)
async def test_B_the_card_route_refuses_another_root_and_wider_grants(
    state, path, body, status, code
):
    _worker, fut, _mid = _crew(state)
    async with TestClient(_server(state, _owner)) as client:
        resp = await client.post(path, json={"request_id": "rid-1", **body})
        assert resp.status == status
        assert (await resp.json())["code"] == code
    assert not fut.done()


@pytest.mark.asyncio
async def test_B_an_app_token_is_refused(state):
    _worker, fut, mid = _crew(state)
    async with TestClient(_server(state, _app_token)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "approve", "request_id": "rid-1", "request_mid": mid},
        )
        assert resp.status in (403, 404)
    assert not fut.done()


def test_B_the_card_shows_the_request_redacted_and_cut():
    from kiro_crew.dashboard.handlers import crew_workers

    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    row = {
        "pending_approval": True,
        "pending_approval_info": {
            "origin": "native",
            "request_id": "rid",
            "request_mid": "mid",
            "tool": "Shell",
            "tool_input": f"echo {secret} " + "x" * 5000,
        },
    }
    card = crew_workers._approval_of(row)
    assert card is not None
    assert secret not in card["tool_input"]
    assert len(card["tool_input"]) == crew_workers.APPROVAL_INPUT_MAX_CHARS
    assert card["tool_input_cut"] == "1"
    assert crew_workers._approval_of({"pending_approval": False}) is None

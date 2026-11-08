"""A Crew Mode card trust reaches the worker's subagents, not its sessions (R4 H1, H3)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import crew_mode
from kiro_crew.dashboard.approval_posture import trust_carries, trust_reaches_subagents
from kiro_crew.dashboard.chat_handlers import api_chat_mode, api_chat_slot_approve
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
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
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)


@web.middleware
async def _owner(request: web.Request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


@pytest.mark.asyncio
async def test_H1_card_trust_reaches_subagents_not_sessions_on_any_path(tmp_path):
    from kiro_crew.dashboard.chat_turn.tool_approval import _persistable_session_policy
    from kiro_crew.dashboard.handlers.crew_workers import api_crew_worker_approve
    from kiro_crew.dashboard.server import (
        _clear_override_derived_trust,
        _suspend_override_derived_trust,
    )

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.owner_id = ""
    root = state.get_or_create_slot("root", origin=SlotOrigin.USER)
    root.agent = "kirocrew-conductor"
    worker = state.get_or_create_slot("w", origin=SlotOrigin.USER)
    worker.agent = "kirocrew-worker"
    worker._created_by = "root"
    state.get_or_create_slot("plain", origin=SlotOrigin.USER).agent = "kirocrew"
    fut = asyncio.get_running_loop().create_future()
    row = worker.append(
        "permission", "Shell", json.dumps({"request_id": "r", "trust_grantable": "1"})
    )
    worker.register_approval("r", fut, row)
    key = effective_session_key(worker)
    policy = state.sessions.set_approval_policy

    def stored() -> str:
        # What spawn_run's parent_trusted and each subagent's own policy read.
        writes = [c.args[1] for c in policy.call_args_list if c.args[0] == key]
        return writes[-1] if writes else ""

    def children_ask() -> None:
        # Subagents: the spawn gate and each subagent's policy read the stored value.
        assert stored() == "auto"
        assert _persistable_session_policy(worker, False) == "auto"
        assert trust_reaches_subagents(worker) is True
        # Sessions and forks the worker opens are born asking.
        assert trust_carries(worker) is False

    app = web.Application(middlewares=[_owner])
    app["state"] = state
    app.router.add_post("/api/chat/mode", api_chat_mode)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_post(
        "/api/chat/slots/{slot}/crew-workers/{worker}/approve", api_crew_worker_approve
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/slots/root/crew-workers/w/approve",
            json={"action": "trust", "request_id": "r"},
        )
        assert resp.status == 200, await resp.text()
        assert worker._trust is True and worker._trust_card_scoped is True
        children_ask()
        # A mode change on ANOTHER slot, every mode.
        for mode in ("normal", "trust_reads", "trust"):
            resp = await client.post("/api/chat/mode", json={"mode": mode, "slot": "plain"})
            assert resp.status == 200, await resp.text()
            children_ask()
    # The safety override's expiry and suspension.
    policy(key, "auto")  # as a yolo arm writes it
    # A slot holding only the override's inherited "auto", so suspend/restore run.
    plain_key = effective_session_key(state._slots["plain"])
    state._slots["plain"]._trust = False
    state._slots["plain"]._trust_reads = False
    with patch("kiro_crew.messaging.session_trust.clear_trusted_sessions"):
        _clear_override_derived_trust(state, "test")
    children_ask()
    state.sessions.get_approval_policy = MagicMock(return_value="auto")
    restore = _suspend_override_derived_trust(state)
    assert restore is not None  # the plain slot was suspended
    assert policy.call_args_list[-1].args == (plain_key, "")
    children_ask()
    state.sessions.get_approval_policy = MagicMock(return_value="")
    restore()
    assert policy.call_args_list[-1].args == (plain_key, "auto")
    children_ask()
    # Revoking the worker's trust: a subagent spawned after it asks again.
    app2 = web.Application(middlewares=[_owner])
    app2["state"] = state
    app2.router.add_post("/api/chat/mode", api_chat_mode)
    async with TestClient(TestServer(app2)) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "w"})
        assert resp.status == 200, await resp.text()
    assert stored() == ""
    assert _persistable_session_policy(worker, False) == ""
    assert trust_reaches_subagents(worker) is False
    # A trust the person sets on the conductor itself keeps its inheritance.
    root._trust = True
    assert trust_carries(root) and _persistable_session_policy(root, False) == "auto"


@pytest.mark.asyncio
async def test_H3_a_workers_spawn_approval_sends_no_slack_buttons():
    from kiro_crew.llm_helpers import LLMEvent
    from kiro_crew.slack.gateway import GatewayOrchestrator

    def live(key: str, agent: str, created_by: str = "") -> SimpleNamespace:
        return SimpleNamespace(
            key=key, agent=agent, agent_kind="template", crew_mode_agent="", mode="",
            memory_mode="persistent", _created_by=created_by, _saved_created_by="",
            _app="", instance_id="", _trust=False, _trust_scope="",
        )  # fmt: skip

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock(get_channel=MagicMock(return_value=None))
    gw.slack = MagicMock(open_dm=AsyncMock(return_value="D1"), post_blocks=AsyncMock())
    gw._owner_id = "U1"
    gw.dashboard_state = MagicMock(request_approval=AsyncMock(return_value=True))
    gw.dashboard_state.dashboard_user_ws_count = MagicMock(return_value=1)
    gw._cfg = MagicMock()
    gw._cfg.hooks.get = MagicMock(return_value=[])
    gw._approval_mode = None
    event = LLMEvent(kind="permission_request", request_id="spawn:a", title="spawn_run")
    with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
        gw.dashboard_state._slots = {
            "root": live("root", "kirocrew-conductor"),
            "w": live("w", "kirocrew-worker", "root"),
        }
        assert await gw._interactive_approval("subagent")(event, "dashboard:w") is True
        gw.slack.post_blocks.assert_not_awaited()  # the conductor's card only
        # A plain session still gets the DM (a failing post falls back at once).
        gw.dashboard_state._slots = {"plain": live("plain", "kirocrew")}
        gw.slack.post_blocks = AsyncMock(side_effect=RuntimeError("down"))
        await gw._interactive_approval("subagent")(event, "dashboard:plain")
        gw.slack.post_blocks.assert_awaited_once()

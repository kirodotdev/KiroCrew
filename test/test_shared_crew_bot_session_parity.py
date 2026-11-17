"""A relay teammate's Bot turns use the same approval policy as session turns.

The runner harness opens as a chat and changes mode before the permission event,
so it exercises the real approval rungs without provisioning a Bot's memory store.
The roster route tests cover admission and approval seeding through relay ingress.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_runner_coverage_helpers import (
    _async_iter,
    _complete,
    _drive,
    _permission,
    _runner_state,
    _slot,
)

from kiro_crew.dashboard import card_send, chat_runner
from kiro_crew.dashboard.approval_posture import trust_reaches_subagents
from kiro_crew.shared_crew import attribution
from kiro_crew.shared_crew.contracts import (
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.identity import KIRO_ISSUER, PROVIDER_KIRO

MATE = Principal(
    id=principal_id_for(PROVIDER_KIRO, KIRO_ISSUER, "kiro-user-ana"),
    kind=PrincipalKind.HUMAN,
    provider=PROVIDER_KIRO,
    issuer=KIRO_ISSUER,
    subject="kiro-user-ana",
    provenance=Provenance.KIRO_RELAY,
    display=DisplaySnapshot(name="Ana"),
)
_BOT = "dashboard:member-code-reviewer"
_SELF_WRITE_ROUTES = (
    "/api/member/self/notes",
    "/api/member/self/update",
    "/api/member/self/goals/report",
    "/api/member/self/goals/key-result",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chat", "member"])
@pytest.mark.parametrize(
    "grant", ["trust", "trust_reads", "trusted_pattern", "scope", "yolo", "none"]
)
async def test_a_teammates_bot_and_session_turn_apply_the_same_grants(tmp_path, mode, grant):
    state, client = _runner_state(tmp_path)
    slot = _slot("member-code-reviewer")
    slot._trust = grant == "trust"
    slot._trust_reads = grant == "trust_reads"
    if grant == "trusted_pattern":
        slot._trusted_patterns.add("ls*")
    if grant == "scope":
        slot._trust_scope = "scope-1"
    state.is_yolo_active = MagicMock(return_value=grant == "yolo")
    calls = 0

    async def events():
        slot.mode = mode
        yield _permission(tool_input=json.dumps({"command": "ls -la"}))
        yield _complete()

    def stream(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return events() if calls == 1 else _async_iter([_complete()])

    client.stream = MagicMock(side_effect=stream)
    client.stream_command = MagicMock(side_effect=stream)
    attribution.bind_request_author(
        ActorContext(principal=MATE, request_id="r", transport=Transport.HTTP)
    )
    with (
        patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        patch.object(chat_runner, "safety_override") as override,
        patch.object(chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=None)),
        patch.object(chat_runner, "_name_grant_refusal_for", new=AsyncMock(return_value=None)),
    ):
        override.return_value.is_scope_active.return_value = grant == "scope"
        override.return_value.is_cli_launch = False
        await _drive(state, slot)

    if grant == "none":
        client.approve_tool.assert_not_awaited()
        assert len([m for m in slot.messages if m.get("role") == "permission"]) == 1
    else:
        client.approve_tool.assert_awaited_once_with("req-cov-1")
    assert trust_reaches_subagents(slot) is (grant == "trust")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _SELF_WRITE_ROUTES)
async def test_an_owner_card_turn_keeps_the_bots_own_record_writes(path):
    @web.middleware
    async def internal(request, handler):
        request["internal_auth"] = True
        return await handler(request)

    async def ok(_request):
        return web.json_response({"ok": True})

    app = web.Application(
        middlewares=[
            card_send.card_turn_arrival_middleware,
            internal,
            card_send.card_turn_delegation_middleware,
        ]
    )
    for route in _SELF_WRITE_ROUTES:
        app.router.add_post(route, ok)
    app.router.add_post("/api/session-control/send", ok)
    with patch("kiro_crew.dashboard.chat_handlers.sel"), card_send.card_turn(_BOT):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(path, json={}, headers={"X-Session-Key": _BOT})
            body = await resp.json()
            held = await client.post("/api/session-control/send", headers={"X-Session-Key": _BOT})
            held_body = await held.json()
    assert resp.status == 200, body
    assert held.status == 403, held_body
    assert held_body["code"] == "card_turn_delegation_refused"


def test_an_owner_card_turn_self_write_in_flight_after_stop_is_allowed():
    class Request(dict):
        headers = {"X-Session-Key": _BOT}
        method = "POST"
        path = "/api/member/self/notes"

    req = Request(internal_auth=True)
    reached = []

    async def record(_request):
        reached.append(True)
        return None

    async def arrival(_request):
        return None

    async def arrive_then_stop():
        with card_send.card_turn(_BOT):
            await card_send.card_turn_arrival_middleware(req, arrival)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            return await card_send.card_turn_delegation_middleware(req, record)

    response = asyncio.run(arrive_then_stop())
    assert req[card_send.CARD_TURN_AT_ARRIVAL] is True
    assert response is None
    assert reached == [True]

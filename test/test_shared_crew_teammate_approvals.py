"""A Shared Crew teammate decides a tool approval in a session they can run turns in.

The relay cases are driven over a real loopback listener with the relay ingress,
the client-sent strip and token auth chained as the gateway orders them, so the
caller is exactly the ``relay:<Kiro user ID>`` teammate the relay hands over. A
teammate approves or rejects a session's own card, in their own session or the
owner's, and the decision names them as the responder. Changing the approval mode
(Trust, Trust reads, a command grant, YOLO) and a deny-rule override stay with the
owner, and a non-owner dashboard session that no sharing path admitted is still
refused.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from shared_crew_host_isolation import host_isolation  # noqa: F401
from token_auth_helpers import clear_nonces  # noqa: F401 -- the autouse fixture registers by import

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import api_chat_mode, api_chat_slot_approve
from kiro_crew.dashboard.deny_override import DENY_OVERRIDE_ONCE
from kiro_crew.dashboard.kiro_relay.caller import CrewKind, HostBinding
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.relay_headers import relay_header_strip_middleware
from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.shared_crew import owner, people
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.identity import KIRO_ISSUER, PROVIDER_KIRO

pytestmark = pytest.mark.usefixtures("host_isolation")

OWNER = "U0OWNER"
ANA = "kiro-user.teammate-ana-0002"
ANA_ID = principal_id_for(PROVIDER_KIRO, KIRO_ISSUER, ANA)
SLOT = "ana-session"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, _floor_monkeypatch: pytest.MonkeyPatch):
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    people.reset_for_tests()
    yield
    people.reset_for_tests()


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = OWNER
    owner.set_owner_subject(OWNER)
    return st


@pytest.fixture
def sel_log():
    log = MagicMock()
    with patch.object(chat_handlers, "sel", return_value=log):
        yield log


def _routes(app: web.Application) -> web.Application:
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_post("/api/chat/mode", api_chat_mode)
    return app


def _relay_app(state) -> web.Application:
    ingress = RelayIngress(enabled=True)
    ingress.bind_host(HostBinding(crew_kind=CrewKind.TEAM, owner_user_id=""))
    app = web.Application(
        middlewares=[
            relay_ingress_middleware(strict_internal_paths=_STRICT_INTERNAL_API_PATHS),
            relay_header_strip_middleware,
            token_auth_middleware(port=5476),
        ]
    )
    app["state"] = state
    app["allowed_origins"] = set()
    app[RELAY_INGRESS_APP_KEY] = ingress
    return _routes(app)


def _teammate(client: TestClient) -> Any:
    return client.server.app[RELAY_INGRESS_APP_KEY].stamp_headers([], relay_user=ANA)


def _dashboard_app(state, user: str, principal: Principal | None) -> web.Application:
    """A non-owner dashboard session as token auth publishes it, with *principal* as actor."""

    @web.middleware
    async def middleware(request: web.Request, handler):
        request["app"] = ""
        request["user"] = user
        request["is_dashboard_user"] = True
        if principal is not None:
            request["auth_tailnet_direct"] = True
            request[ACTOR_REQUEST_KEY] = ActorContext(
                principal=principal, request_id="req", transport=Transport.HTTP
            )
        return await handler(request)

    app = web.Application(middlewares=[middleware])
    app["state"] = state
    return _routes(app)


def _ana_author() -> dict[str, Any]:
    return {
        "id": ANA_ID,
        "kind": PrincipalKind.HUMAN.value,
        "provider": PROVIDER_KIRO,
        "provenance": Provenance.KIRO_RELAY.value,
        "display": {"name": "Ana", "login": ""},
    }


def _pending(state, request_id: str, slot_key: str = SLOT, author: dict | None = None):
    """A session waiting on a tool approval its latest turn raised."""
    slot = state.get_or_create_slot(slot_key, origin=SlotOrigin.USER)
    slot.append("user", "run it", "msg msg-u", meta={"author": author or _ana_author()})
    fut = asyncio.get_running_loop().create_future()
    row = slot.append("permission", "Tool", f'{{"request_id": "{request_id}"}}')
    slot.register_approval(request_id, fut, row)
    return slot, fut


def _card_body(slot, request_id: str, action: str, response_id: str = "r-ana") -> dict:
    """The body the dashboard's approval card sends."""
    return {
        "action": action,
        "request_id": request_id,
        "response_id": response_id,
        "origin": "native",
        "request_mid": slot.approval_instance(request_id),
    }


def _sel_outcomes(sel_log) -> list[tuple[str, str, str]]:
    return [
        (c.kwargs.get("operation"), c.kwargs.get("outcome"), c.kwargs.get("caller"))
        for c in sel_log.log_api_access.call_args_list
        if c.kwargs.get("source") == "shared_crew_teammate"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approved", "rejected", "rejected_once"])
async def test_a_relay_teammate_decides_an_approval_in_their_own_session(
    state, sel_log, action
) -> None:
    slot, fut = _pending(state, "rid-1")
    async with TestClient(TestServer(_relay_app(state))) as client:
        resp = await client.post(
            f"/api/chat/slots/{SLOT}/approve",
            json=_card_body(slot, "rid-1", action),
            headers=_teammate(client),
        )
        body = await resp.json()
    assert resp.status == 200, body
    assert fut.result() == action
    assert body["decision"]["responder"]["id"] == ANA_ID
    assert body["decision"]["settled_by"] == "person"
    assert (f"tool_approval:{action}", "allowed", f"relay:{ANA}") in _sel_outcomes(sel_log)


@pytest.mark.asyncio
async def test_a_relay_teammate_decides_an_approval_in_the_owners_session(state, sel_log) -> None:
    owner_author = {"id": "p_owner", "kind": "human", "display": {"name": "Owner"}}
    slot, fut = _pending(state, "rid-2", slot_key="owner-session", author=owner_author)
    async with TestClient(TestServer(_relay_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/owner-session/approve",
            json=_card_body(slot, "rid-2", "approved"),
            headers=_teammate(client),
        )
        body = await resp.json()
    assert resp.status == 200, body
    assert fut.result() == "approved"
    assert body["decision"]["responder"]["id"] == ANA_ID


@pytest.mark.asyncio
async def test_a_second_teammate_click_gets_the_first_decision(state, sel_log) -> None:
    slot, fut = _pending(state, "rid-3")
    body = _card_body(slot, "rid-3", "approved")
    async with TestClient(TestServer(_relay_app(state))) as client:
        first = await client.post(
            f"/api/chat/slots/{SLOT}/approve", json=body, headers=_teammate(client)
        )
        late = await client.post(
            f"/api/chat/slots/{SLOT}/approve",
            json={**body, "action": "rejected", "response_id": "r-other"},
            headers=_teammate(client),
        )
        late_body = await late.json()
    assert first.status == 200
    assert late.status == 409 and late_body["code"] == "already_resolved"
    assert fut.result() == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["trust", "trust_reads", "trust_command", "trust_base", "yolo", DENY_OVERRIDE_ONCE]
)
async def test_a_teammate_cannot_change_the_approval_mode_from_a_card(
    state, sel_log, action
) -> None:
    slot, fut = _pending(state, "rid-4")
    async with TestClient(TestServer(_relay_app(state))) as client:
        resp = await client.post(
            f"/api/chat/slots/{SLOT}/approve",
            json={"action": action, "request_id": "rid-4", "response_id": "r-ana"},
            headers=_teammate(client),
        )
        body = await resp.json()
    assert resp.status == 403, body
    assert body["code"] == "owner_only"
    assert not fut.done()
    assert not slot._trust and not slot._trust_reads
    assert (f"tool_approval:{action}", "denied", f"relay:{ANA}") in _sel_outcomes(sel_log)


@pytest.mark.asyncio
async def test_a_teammate_still_cannot_set_the_session_approval_mode(state, sel_log) -> None:
    slot, _fut = _pending(state, "rid-5")
    async with TestClient(TestServer(_relay_app(state))) as client:
        resp = await client.post(
            "/api/chat/mode", json={"mode": "trust", "slot": SLOT}, headers=_teammate(client)
        )
    assert resp.status == 403
    assert not slot._trust


@pytest.mark.asyncio
async def test_a_tailscale_collaborator_decides_an_approval(state, sel_log) -> None:
    carol = Principal(
        id=principal_id_for("tailscale", "tailnet", "carol@example.test"),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject="carol@example.test",
        provenance=Provenance.TAILSCALE_LISTENER,
        display=DisplaySnapshot(name="Carol", login="carol@example.test"),
    )
    slot, fut = _pending(state, "rid-6")
    app = _dashboard_app(state, "tailnet:uid:42", carol)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/chat/slots/{SLOT}/approve", json=_card_body(slot, "rid-6", "approved", "r-c")
        )
        body = await resp.json()
    assert resp.status == 200, body
    assert fut.result() == "approved"
    assert body["decision"]["responder"]["id"] == carol.id


@pytest.mark.asyncio
async def test_a_teammate_decide_must_name_the_card_it_was_clicked_on(state, sel_log) -> None:
    _slot, fut = _pending(state, "rid-8")
    async with TestClient(TestServer(_relay_app(state))) as client:
        resp = await client.post(
            f"/api/chat/slots/{SLOT}/approve",
            json={"action": "approved", "request_id": "rid-8", "response_id": "r-ana"},
            headers=_teammate(client),
        )
        body = await resp.json()
    assert resp.status == 404, body
    assert body["code"] == "approval_target_required"
    assert not fut.done()


@pytest.mark.asyncio
async def test_a_relay_teammate_is_refused_with_the_shared_crew_preview_off(
    state, sel_log, _floor_monkeypatch
) -> None:
    _floor_monkeypatch.delenv("KIROCREW_SHARED_CREW_PREVIEW", raising=False)
    _floor_monkeypatch.setattr("kiro_crew.shared_crew.preview.snapshot_at_start", lambda: False)
    slot, fut = _pending(state, "rid-9")
    async with TestClient(TestServer(_relay_app(state))) as client:
        resp = await client.post(
            f"/api/chat/slots/{SLOT}/approve",
            json=_card_body(slot, "rid-9", "approved"),
            headers=_teammate(client),
        )
    assert resp.status == 403
    assert not fut.done()
    assert _sel_outcomes(sel_log) == []


@pytest.mark.asyncio
async def test_a_dashboard_session_no_sharing_path_admitted_is_still_refused(
    state, sel_log
) -> None:
    slot, fut = _pending(state, "rid-7")
    app = _dashboard_app(state, "slack:U0OTHER", None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/chat/slots/{SLOT}/approve", json=_card_body(slot, "rid-7", "approved")
        )
    assert resp.status == 403
    assert not fut.done()
    assert _sel_outcomes(sel_log) == []

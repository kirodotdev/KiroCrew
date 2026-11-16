"""An SSM invite guest passes every owner gate except the Terminal's.

The rule lives in one place (``shared_crew.ssm_guest_access``, read by
``source_providers.is_owner_dashboard_request``), so these tests pin the predicate,
the Terminal exclusion, that every registered Terminal route is excluded, and a
few owner routes end to end: the Bots roster, Trust and YOLO, the app list.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_state
from shared_crew_host_isolation import host_isolation  # noqa: F401
from token_auth_helpers import clear_nonces  # noqa: F401 -- the autouse fixture registers by import

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import api_chat_mode, api_chat_slot_approve
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.shared_crew import owner, people
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    SSM_GUEST_REQUEST_KEY,
    SSM_REQUEST_KEY,
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    SsmGuest,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.identity import ssm_principal
from kiro_crew.shared_crew.ssm_guest_access import (
    TERMINAL_PATH_PREFIXES,
    is_ssm_guest_request,
    is_terminal_request,
)

pytestmark = pytest.mark.usefixtures("host_isolation")

OWNER = "U0OWNER"
INVITE = "b0ae764d1587"
GUEST_USER = f"ssm:{INVITE}"
SLOT = "guest-session"
SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, _floor_monkeypatch: pytest.MonkeyPatch):
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    people.reset_for_tests()
    owner.set_owner_subject(OWNER)
    yield
    people.reset_for_tests()


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = OWNER
    return st


@pytest.fixture
def sel_log():
    log = MagicMock()
    with patch.object(chat_handlers, "sel", return_value=log):
        yield log


def _tailscale_principal() -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", "carol@example.test"),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject="carol@example.test",
        provenance=Provenance.TAILSCALE_LISTENER,
        display=DisplaySnapshot(name="Carol", login="carol@example.test"),
    )


def _publish(request: Any, who: str) -> None:
    """The claims the SSM gate and token auth publish for *who*."""
    request["app"] = ""
    request["is_dashboard_user"] = True
    if who == "ssm":
        request["user"] = GUEST_USER
        request[SSM_REQUEST_KEY] = True
        request[SSM_GUEST_REQUEST_KEY] = SsmGuest(invite_id=INVITE, alias="Sam")
        principal = ssm_principal(INVITE, "Sam")
    elif who == "tailscale":
        request["user"] = "tailnet:uid:42"
        request["auth_tailnet_direct"] = True
        principal = _tailscale_principal()
    elif who == "owner":
        request["user"] = OWNER
        return
    else:
        request["user"] = "slack:U0OTHER"
        return
    request[ACTOR_REQUEST_KEY] = ActorContext(
        principal=principal, request_id="req", transport=Transport.HTTP
    )


def _request(who: str, path: str = "/api/members", method: str = "GET") -> Any:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id=OWNER)
    request = make_mocked_request(method, path, app=app)
    _publish(request, who)
    return request


# -- the predicate --------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/members",
        "/api/members/reviewer/dashboard",
        "/api/chat/mode",
        "/api/apps",
        "/api/apps/install",
        "/api/update",
        "/api/update/channel",
        "/api/settings",
        "/api/terminals-are-not-this",
    ],
)
def test_an_ssm_guest_passes_the_owner_gate(path: str) -> None:
    request = _request("ssm", path)
    assert is_ssm_guest_request(request)
    assert is_owner_dashboard_request(request)


@pytest.mark.parametrize(
    "path",
    [
        "/api/terminal",
        "/api/terminal/sessions",
        "/api/terminal/sessions/s1",
        "/api/terminal/external",
        "/api/terminal/external-grant",
        "/api/terminal/complete",
        "/api/terminal/redact",
        "/api/ws/terminal/s1",
    ],
)
def test_an_ssm_guest_never_passes_the_owner_gate_on_a_terminal_route(path: str) -> None:
    request = _request("ssm", path, "POST")
    assert is_terminal_request(request)
    assert not is_owner_dashboard_request(request)


def test_every_registered_terminal_route_is_excluded() -> None:
    """A Terminal route added or renamed must stay under the excluded prefixes."""
    paths: set[str] = set()
    for source in (SRC / "dashboard" / "routes").glob("*.py"):
        paths |= set(re.findall(r'"(/api/[^"]*terminal[^"]*)"', source.read_text()))
    assert paths, "found no terminal routes to check"
    for path in sorted(paths):
        concrete = path.replace("{session_id}", "s1")
        assert is_terminal_request(SimpleNamespace(path=concrete)), path
        assert concrete.startswith(TERMINAL_PATH_PREFIXES), path


def test_the_owner_still_passes_and_other_sessions_still_do_not() -> None:
    assert is_owner_dashboard_request(_request("owner"))
    assert is_owner_dashboard_request(_request("owner", "/api/terminal/sessions"))
    assert not is_owner_dashboard_request(_request("tailscale"))
    assert not is_owner_dashboard_request(_request("other"))


def test_nobody_is_an_ssm_guest_with_the_shared_crew_preview_off(_floor_monkeypatch) -> None:
    _floor_monkeypatch.delenv("KIROCREW_SHARED_CREW_PREVIEW", raising=False)
    _floor_monkeypatch.setattr("kiro_crew.shared_crew.preview.snapshot_at_start", lambda: False)
    request = _request("ssm")
    assert not is_ssm_guest_request(request)
    assert not is_owner_dashboard_request(request)


def test_an_ssm_claim_whose_actor_names_another_invite_is_not_a_guest() -> None:
    request = _request("ssm")
    request[ACTOR_REQUEST_KEY] = ActorContext(
        principal=ssm_principal("another-invite", "Mallory"),
        request_id="req",
        transport=Transport.HTTP,
    )
    assert not is_ssm_guest_request(request)
    assert not is_owner_dashboard_request(request)


def test_an_app_token_on_the_ssm_listener_is_not_a_guest() -> None:
    request = _request("ssm")
    request["app"] = "some-app"
    assert not is_ssm_guest_request(request)


# -- end to end ----------------------------------------------------------------


def _app(state, who: str) -> web.Application:
    from kiro_crew.apps.routes_listing import handle_list_apps
    from kiro_crew.dashboard.handlers import members as m
    from kiro_crew.dashboard.handlers.terminal_routes import api_terminal_create, api_terminal_list

    @web.middleware
    async def middleware(request: web.Request, handler):
        _publish(request, who)
        return await handler(request)

    app = web.Application(middlewares=[middleware])
    app["state"] = state
    app.router.add_get("/api/members", m.api_members)
    app.router.add_get("/api/apps", handle_list_apps)
    app.router.add_post("/api/chat/mode", api_chat_mode)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_get("/api/terminal/sessions", api_terminal_list)
    app.router.add_post("/api/terminal/sessions", api_terminal_create)
    return app


@pytest.mark.asyncio
async def test_an_ssm_guest_reads_the_owners_bot_roster(state) -> None:
    async with TestClient(TestServer(_app(state, "ssm"))) as client:
        resp = await client.get("/api/members")
        body = await resp.json()
    assert resp.status == 200, body
    assert "members" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["trust", "trust_reads", "normal"])
async def test_an_ssm_guest_sets_a_sessions_approval_mode(state, sel_log, mode) -> None:
    slot = state.get_or_create_slot(SLOT, origin=SlotOrigin.USER)
    async with TestClient(TestServer(_app(state, "ssm"))) as client:
        resp = await client.post("/api/chat/mode", json={"mode": mode, "slot": SLOT})
        body = await resp.json()
    assert resp.status == 200, body
    assert slot._trust is (mode == "trust")
    assert slot._trust_reads is (mode == "trust_reads")


@pytest.mark.asyncio
async def test_an_ssm_guest_turns_yolo_on_and_off(state, sel_log) -> None:
    from kiro_crew.safety_override import safety_override

    async with TestClient(TestServer(_app(state, "ssm"))) as client:
        try:
            resp = await client.post("/api/chat/mode", json={"mode": "yolo"})
            body = await resp.json()
            assert resp.status == 200, body
            assert safety_override().is_active()
        finally:
            off = await client.post("/api/chat/mode", json={"mode": "normal"})
            assert off.status == 200
    assert not safety_override().is_active()


@pytest.mark.asyncio
async def test_an_ssm_guest_grants_trust_from_an_approval_card(state, sel_log) -> None:
    slot = state.get_or_create_slot(SLOT, origin=SlotOrigin.USER)
    slot.append("user", "run it", "msg msg-u")
    fut = asyncio.get_running_loop().create_future()
    row = slot.append(
        "permission", "Tool", json.dumps({"request_id": "rid-1", "trust_grantable": "1"})
    )
    slot.register_approval("rid-1", fut, row)
    async with TestClient(TestServer(_app(state, "ssm"))) as client:
        resp = await client.post(
            f"/api/chat/slots/{SLOT}/approve",
            json={
                "action": "trust",
                "request_id": "rid-1",
                "response_id": "r-sam",
                "origin": "native",
                "request_mid": slot.approval_instance("rid-1"),
            },
        )
        body = await resp.json()
    assert resp.status == 200, body
    assert fut.done() and slot._trust


@pytest.mark.asyncio
async def test_a_tailscale_collaborator_still_cannot_set_the_approval_mode(state, sel_log) -> None:
    slot = state.get_or_create_slot(SLOT, origin=SlotOrigin.USER)
    async with TestClient(TestServer(_app(state, "tailscale"))) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": SLOT})
    assert resp.status == 403
    assert not slot._trust


@pytest.mark.asyncio
async def test_an_ssm_guests_terminal_probe_reads_disabled_and_create_is_refused(state) -> None:
    async with TestClient(TestServer(_app(state, "ssm"))) as client:
        probe = await client.get("/api/terminal/sessions")
        probe_body = await probe.json()
        create = await client.post("/api/terminal/sessions", json={})
    assert probe.status == 200
    assert probe_body == {"enabled": False, "sessions": []}
    assert create.status in (401, 403)


@pytest.mark.asyncio
async def test_an_ssm_guest_gets_the_app_list(state, monkeypatch) -> None:
    from kiro_crew.apps import routes_listing

    walked: list[bool] = []

    def _walk() -> list[Any]:
        walked.append(True)
        return []

    # The installed-app walk runs only past the SSM early return that used to
    # hand every SSM request an empty list.
    monkeypatch.setattr(routes_listing, "_listed_apps_with_trust", _walk)
    monkeypatch.setattr(routes_listing, "list_app_processes", lambda: [])
    async with TestClient(TestServer(_app(state, "ssm"))) as client:
        resp = await client.get("/api/apps")
    assert resp.status == 200
    assert walked == [True]

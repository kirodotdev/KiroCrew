"""A Shared Crew teammate passes every owner gate except the Terminal's.

The rule lives in one place (``shared_crew.teammate_access``, read by
``source_providers.is_owner_dashboard_request``), so these tests pin the predicate
for each sharing path (an SSM invite guest, a Tailscale collaborator, a relay
teammate), the Terminal exclusion, that every registered Terminal route is excluded,
and a few owner routes end to end: the Bots roster, Trust and YOLO, the app list.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
from kiro_crew.dashboard.handlers.source_providers import (
    is_host_owner_dashboard_request,
    is_owner_dashboard_request,
)
from kiro_crew.dashboard.kiro_relay.caller import RELAY_CALLER_KEY, RelayCaller, RelayCallerKind
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
from kiro_crew.shared_crew.identity import KIRO_ISSUER, PROVIDER_KIRO, ssm_principal
from kiro_crew.shared_crew.teammate_access import (
    TERMINAL_PATH_PREFIXES,
    is_admission_config_key,
    is_shared_crew_teammate,
    is_ssm_guest_request,
    is_terminal_request,
)

pytestmark = pytest.mark.usefixtures("host_isolation")

OWNER = "U0OWNER"
INVITE = "b0ae764d1587"
GUEST_USER = f"ssm:{INVITE}"
SLOT = "guest-session"
ANA = "kiro-user.teammate-ana-0002"
TEAMMATES = ("ssm", "tailscale", "relay")
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


def _relay_principal() -> Principal:
    return Principal(
        id=principal_id_for(PROVIDER_KIRO, KIRO_ISSUER, ANA),
        kind=PrincipalKind.HUMAN,
        provider=PROVIDER_KIRO,
        issuer=KIRO_ISSUER,
        subject=ANA,
        provenance=Provenance.KIRO_RELAY,
        display=DisplaySnapshot(name="Ana", login=""),
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
    elif who in ("tailscale_direct", "tailscale_serve"):
        # A widened-bind peer (attribution only) and the allowlisted serve path.
        request["user"] = "tailnet:uid:42"
        provenance = (
            Provenance.TAILSCALE_DIRECT if who == "tailscale_direct" else Provenance.TAILSCALE_SERVE
        )
        principal = dataclasses.replace(_tailscale_principal(), provenance=provenance)
    elif who == "relay":
        request["user"] = f"relay:{ANA}"
        request[RELAY_CALLER_KEY] = RelayCaller(kiro_user_id=ANA, kind=RelayCallerKind.TEAMMATE)
        principal = _relay_principal()
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
@pytest.mark.parametrize("who", TEAMMATES)
def test_a_teammate_passes_the_owner_gate(path: str, who: str) -> None:
    request = _request(who, path)
    assert is_shared_crew_teammate(request)
    assert is_owner_dashboard_request(request)
    # They pass as themselves: the machine owner is still told apart.
    assert not is_host_owner_dashboard_request(request)
    assert request["user"] != OWNER


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
@pytest.mark.parametrize("who", TEAMMATES)
def test_a_teammate_never_passes_the_owner_gate_on_a_terminal_route(path: str, who: str) -> None:
    request = _request(who, path, "POST")
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
    assert not is_owner_dashboard_request(_request("other"))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/webhooks/tokens"),
        ("DELETE", "/api/webhooks/tokens/t1"),
        ("POST", "/api/apps/register"),
        ("POST", "/api/apps/notes/token"),
        ("GET", "/api/secrets"),
        ("PUT", "/api/secrets/GITHUB_TOKEN"),
        ("POST", "/api/token/local"),
        ("POST", "/api/auth/mobile-link"),
        ("POST", "/api/relay-viewer/token"),
        ("PUT", "/api/slack/config"),
        ("POST", "/api/telegram/config"),
        ("POST", "/api/channels"),
        ("DELETE", "/api/channels/c1"),
        ("POST", "/api/channels/whatsapp/qr/start"),
        ("POST", "/api/instances/i1/connect"),
        ("POST", "/api/instances/i1/refresh-token"),
        ("POST", "/api/instances/i1/embed-token"),
        ("PUT", "/api/crons/j1/secrets"),
        ("POST", "/api/shutdown"),
    ],
)
@pytest.mark.parametrize("who", TEAMMATES)
def test_a_credential_that_outlives_access_stays_the_owners(
    who: str, method: str, path: str
) -> None:
    """A webhook bearer, an app secret, the vault or a channel's token would survive the
    teammate's access being revoked."""
    assert not is_owner_dashboard_request(_request(who, path, method))
    assert is_owner_dashboard_request(_request("owner", path, method))


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api/slack/config"), ("GET", "/api/channels"), ("POST", "/api/channels/c1/messages")],
)
def test_a_teammate_reads_and_uses_a_channel(method: str, path: str) -> None:
    assert is_owner_dashboard_request(_request("relay", path, method))


@pytest.mark.parametrize("method", ["PUT", "POST", "DELETE"])
@pytest.mark.parametrize("who", TEAMMATES)
def test_turning_remote_access_off_stays_the_owners(who: str, method: str) -> None:
    """Turning it back on takes the host machine, so a teammate could shut every
    person out of the Crew and not undo it."""
    assert not is_owner_dashboard_request(_request(who, "/api/remote-control", method))
    assert is_owner_dashboard_request(_request("owner", "/api/remote-control", method))


@pytest.mark.parametrize("who", TEAMMATES)
def test_a_teammate_reads_the_remote_access_state(who: str) -> None:
    assert is_owner_dashboard_request(_request(who, "/api/remote-control", "GET"))


@pytest.mark.parametrize(
    "key",
    [
        "dashboard.shared_crew.ssm",
        "dashboard.shared_crew.tailnet_direct",
        "dashboard.tailscale.trust_identity",
        "dashboard.tailscale.allowed_logins",
        "tunnel.relay_enabled",
        "instances.enabled",
        "slack.allowed_users",
        "telegram.accounts.main.allowed_user_ids",
        "webex.allow_group_rooms",
        "whatsapp.dm_policy",
    ],
)
def test_the_keys_that_decide_who_reaches_the_crew_are_admission_keys(key: str) -> None:
    assert is_admission_config_key(key)


@pytest.mark.parametrize(
    "key", ["agent.model", "agent.sandbox_allow_no_isolation", "memory.ollama_allow_remote"]
)
def test_an_ordinary_setting_is_not_an_admission_key(key: str) -> None:
    assert not is_admission_config_key(key)


def test_every_allow_list_in_the_config_schema_is_an_admission_key() -> None:
    """A channel or allow-list added to the schema is covered by name, with no table to update."""
    from kiro_crew.dashboard.handlers import config_all_settings

    entries = config_all_settings._entries()
    paths = [
        getattr(e, "path", "") for e in (entries.values() if isinstance(entries, dict) else entries)
    ]
    allow_lists = [
        p
        for p in paths
        if p and any(seg.startswith("allowed_") or seg == "dm_policy" for seg in p.split("."))
    ]
    assert allow_lists, "found no allow-lists in the schema to check"
    assert [p for p in allow_lists if not is_admission_config_key(p)] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("dashboard.shared_crew.ssm", "only the Crew owner can change who may reach this Crew"),
        ("instances.enabled", "only the Crew owner can manage remote crews"),
    ],
)
@pytest.mark.parametrize("who", TEAMMATES)
async def test_a_teammate_cannot_patch_an_admission_key(
    state, who: str, key: str, message: str
) -> None:
    from kiro_crew.dashboard.handlers import core

    @web.middleware
    async def as_who(request: web.Request, handler):
        _publish(request, who)
        return await handler(request)

    app = web.Application(middlewares=[as_who])
    app["state"] = state
    app.router.add_patch("/api/config/kirocrew", core.api_kirocrew_config_patch)
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch("/api/config/kirocrew", json={"path": key, "value": True})
        body = await resp.json()
    assert resp.status == 403, body
    assert body["code"] == "owner_only"
    assert body["error"] == message


@pytest.mark.parametrize("who", ["tailscale", "tailscale_serve"])
def test_the_machine_owner_on_a_tailnet_path_is_never_a_teammate(who: str) -> None:
    """The owner keeps the Terminal: a tailnet actor does not make them a teammate."""
    request = _request(who, "/api/terminal/sessions")
    request["user"] = OWNER
    assert is_host_owner_dashboard_request(request)
    assert not is_shared_crew_teammate(request)
    assert is_owner_dashboard_request(request)


@pytest.mark.parametrize("who", ["other", "tailscale_direct"])
def test_a_session_no_sharing_path_admitted_is_not_a_teammate(who: str) -> None:
    """A messaging allowlist link and a widened-bind tailnet peer (attribution only)."""
    request = _request(who)
    assert not is_shared_crew_teammate(request)
    assert not is_owner_dashboard_request(request)


def test_the_tailscale_serve_path_is_a_teammate() -> None:
    request = _request("tailscale_serve")
    assert is_shared_crew_teammate(request)
    assert is_owner_dashboard_request(request)


def test_a_direct_listener_principal_without_the_listeners_mark_is_not_a_teammate() -> None:
    request = _request("tailscale")
    request["auth_tailnet_direct"] = False
    assert not is_shared_crew_teammate(request)


def test_a_relay_principal_without_the_ingress_caller_is_not_a_teammate() -> None:
    request = _request("relay")
    del request[RELAY_CALLER_KEY]
    assert not is_shared_crew_teammate(request)


def test_nobody_is_an_ssm_guest_with_the_shared_crew_preview_off(_floor_monkeypatch) -> None:
    _floor_monkeypatch.delenv("KIROCREW_SHARED_CREW_PREVIEW", raising=False)
    _floor_monkeypatch.setattr("kiro_crew.shared_crew.preview.snapshot_at_start", lambda: False)
    request = _request("ssm")
    assert not is_ssm_guest_request(request)
    assert not is_owner_dashboard_request(request)
    # The Tailscale paths are preview features too; the relay is the team Crew's
    # own access path, granted by the control plane, and does not wait on it.
    assert not is_owner_dashboard_request(_request("tailscale"))
    assert is_owner_dashboard_request(_request("relay"))


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
@pytest.mark.parametrize("who", TEAMMATES)
async def test_a_teammate_reads_the_owners_bot_roster(state, who: str) -> None:
    async with TestClient(TestServer(_app(state, who))) as client:
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
async def test_a_tailscale_collaborator_sets_the_approval_mode(state, sel_log) -> None:
    slot = state.get_or_create_slot(SLOT, origin=SlotOrigin.USER)
    async with TestClient(TestServer(_app(state, "tailscale"))) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": SLOT})
    assert resp.status == 200
    assert slot._trust


@pytest.mark.asyncio
@pytest.mark.parametrize("who", TEAMMATES)
async def test_a_teammates_terminal_probe_reads_disabled_and_create_is_refused(
    state, who: str
) -> None:
    async with TestClient(TestServer(_app(state, who))) as client:
        probe = await client.get("/api/terminal/sessions")
        probe_body = await probe.json()
        create = await client.post("/api/terminal/sessions", json={})
    assert probe.status == 200
    assert probe_body == {"enabled": False, "sessions": []}
    assert create.status in (401, 403)


@pytest.mark.asyncio
async def test_the_owner_on_a_tailnet_path_gets_the_terminal_list(state, monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import terminal, terminal_routes

    # Through the facade, whose patch reaches every module of the family.
    monkeypatch.setattr(terminal, "_is_enabled", lambda _request: True)

    @web.middleware
    async def owner_on_tailnet(request: web.Request, handler):
        _publish(request, "tailscale")
        request["user"] = OWNER
        return await handler(request)

    app = web.Application(middlewares=[owner_on_tailnet])
    app["state"] = state
    app.router.add_get("/api/terminal/sessions", terminal_routes.api_terminal_list)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/terminal/sessions")
        body = await resp.json()
    assert resp.status == 200
    assert body.get("enabled") is not False


@pytest.mark.parametrize("who", TEAMMATES)
def test_a_teammate_is_refused_the_mobile_setup(who: str) -> None:
    """Publishing to a phone is managing who may reach the Crew, and the QR is an
    owner-subject credential."""
    from kiro_crew.dashboard.handlers.tailnet_mobile import _guard

    refused = _guard(_request(who, "/api/tailnet/mobile/qr", "POST"))
    assert refused is not None
    assert refused.status == 403


@pytest.mark.asyncio
async def test_an_ssm_guest_gets_the_app_list(state, monkeypatch) -> None:
    from kiro_crew.apps import routes_listing

    walked: list[bool] = []

    def _walk() -> list[Any]:
        walked.append(True)
        return []

    # The installed-app walk runs only past the early return that hands an SSM
    # request that is not a guest's an empty list.
    monkeypatch.setattr(routes_listing, "_listed_apps_with_trust", _walk)
    from kiro_crew.apps import routes as apps_routes

    monkeypatch.setattr(apps_routes, "list_app_processes", lambda: [])
    async with TestClient(TestServer(_app(state, "ssm"))) as client:
        resp = await client.get("/api/apps")
    assert resp.status == 200
    assert walked == [True]


# -- the machine owner's own Team Crew notes ----------------------------------------
#
# A ``system.relay`` note names the Team Crews the machine owner's own sign-in can
# open, so it stays the machine owner's on every transport even though a teammate
# otherwise acts as the owner (``ws_event_scope.OWNER_ONLY_CHANNELS``).

_RELAY_NOTE = {
    "ts": "2",
    "source": "system",
    "channel": "system.relay",
    "title": "You've been added to Build box",
    "acked": False,
}
_AGENT_NOTE = {
    "ts": "1",
    "source": "system",
    "channel": "system.agent",
    "body": "done",
    "acked": False,
}


@pytest.fixture
def owner_only_withheld(monkeypatch) -> list[str]:
    """The event types withheld as owner-only, as their SEL rows record them."""
    from kiro_crew.dashboard import ws_event_scope

    rows: list[str] = []

    def record(app: str, event_type: str, reason: str) -> None:
        if reason == ws_event_scope.OWNER_ONLY_NOTE:
            rows.append(event_type)

    monkeypatch.setattr(ws_event_scope, "_audit_deny", record)
    return rows


def _notes_app(state, who: str) -> web.Application:
    from kiro_crew.dashboard.handlers import messaging as msg
    from kiro_crew.dashboard.handlers import updates

    @web.middleware
    async def middleware(request: web.Request, handler):
        _publish(request, who)
        return await handler(request)

    app = web.Application(middlewares=[middleware])
    app["state"] = state
    app.router.add_get("/api/notifications", msg.api_notifications)
    app.router.add_post("/api/notifications/ack", msg.api_notification_ack)
    app.router.add_delete("/api/notifications", msg.api_notification_delete)
    app.router.add_get("/api/stream", updates.api_stream)
    return app


class _OwnerSocket:
    """An owner socket; a teammate's carries ``TEAMMATE_WS_FLAG`` (``ws.py`` sets it)."""

    def __init__(self, teammate: bool) -> None:
        from kiro_crew.shared_crew.teammate_access import TEAMMATE_WS_FLAG

        self.closed = False
        self._flags = {"_is_dashboard_user": True, TEAMMATE_WS_FLAG: teammate}

    def get(self, key, default=None):
        return self._flags.get(key, default)


def test_a_team_crew_note_reaches_only_the_machine_owners_socket(
    state, monkeypatch, owner_only_withheld
) -> None:
    machine_owner, teammate = _OwnerSocket(teammate=False), _OwnerSocket(teammate=True)
    state.register_ws(machine_owner, owner=True)
    state.register_ws(teammate, owner=True)
    sent: list = []
    monkeypatch.setattr(state, "_spawn_ws_send", lambda ws, _payload: sent.append(ws))
    state._send_ws_all("notification", dict(_RELAY_NOTE), "{}")
    assert machine_owner in sent
    assert teammate not in sent
    assert owner_only_withheld == ["notification"]
    # The Crew's own notes still reach the teammate as the owner.
    sent.clear()
    state._send_ws_all("notification", dict(_AGENT_NOTE), "{}")
    assert machine_owner in sent and teammate in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("who", (*TEAMMATES, "owner"))
async def test_the_sse_stream_keeps_team_crew_notes_for_the_machine_owner_alone(
    state, monkeypatch, owner_only_withheld, who: str
) -> None:
    from kiro_crew.dashboard.handlers import updates
    from kiro_crew.testing.wait import finished

    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    monkeypatch.setattr(state, "status_snapshot", lambda **_kwargs: {})
    try:
        async with TestClient(TestServer(_notes_app(state, who))) as client:
            resp = await client.get("/api/stream")
            assert resp.status == 200
            first = await finished(resp.content.readuntil(b"\n\n"), "the first SSE frame")
            assert first.startswith(b"event: dashboard")
            state._broadcast(dict(_RELAY_NOTE))
            state._broadcast({"source": "system", "body": "fence"})
            blocks: list[bytes] = []
            while b"fence" not in (blocks[-1] if blocks else b""):
                blocks.append(await finished(resp.content.readuntil(b"\n\n"), "an SSE frame"))
            resp.close()
    finally:
        stopped.set()
    got = [block for block in blocks if b"Build box" in block]
    if who == "owner":
        assert got and owner_only_withheld == []
    else:
        assert not got
        assert owner_only_withheld == ["notification"]


@pytest.mark.asyncio
@pytest.mark.parametrize("who", (*TEAMMATES, "owner"))
async def test_the_notification_list_keeps_team_crew_notes_for_the_machine_owner_alone(
    state, owner_only_withheld, who: str
) -> None:
    state._notification_log = [dict(_AGENT_NOTE), dict(_RELAY_NOTE)]
    async with TestClient(TestServer(_notes_app(state, who))) as client:
        resp = await client.get("/api/notifications")
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    channels = [note["channel"] for note in body["notifications"]]
    if who == "owner":
        assert channels == ["system.agent", "system.relay"]
        assert body["unread"] == 2
    else:
        assert channels == ["system.agent"]
        assert body["unread"] == 1
        assert owner_only_withheld == ["notification"]


@pytest.mark.asyncio
@pytest.mark.parametrize("who", TEAMMATES)
@pytest.mark.parametrize(("method", "operation"), [("POST", "ack"), ("DELETE", "delete")])
async def test_a_teammate_is_refused_a_team_crew_note_by_its_timestamp(
    state, owner_only_withheld, who: str, method: str, operation: str
) -> None:
    state._notification_log = [dict(_AGENT_NOTE), dict(_RELAY_NOTE)]
    path = "/api/notifications/ack" if operation == "ack" else "/api/notifications"
    async with TestClient(TestServer(_notes_app(state, who))) as client:
        resp = await client.request(method, path, json={"ts": "2"})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "owner_only"
        # The Crew's own note is still theirs to change.
        ok = await client.request(method, path, json={"ts": "1"})
        assert ok.status == 200, await ok.text()
    relay = [note for note in state._notification_log if note["ts"] == "2"]
    assert relay and relay[0]["acked"] is False
    assert owner_only_withheld == [f"notifications.{operation}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "operation"), [("POST", "ack"), ("DELETE", "delete")])
async def test_the_machine_owner_changes_a_team_crew_note(
    state, monkeypatch, method: str, operation: str
) -> None:
    state._notification_log = [dict(_AGENT_NOTE), dict(_RELAY_NOTE)]
    path = "/api/notifications/ack" if operation == "ack" else "/api/notifications"
    async with TestClient(TestServer(_notes_app(state, "owner"))) as client:
        resp = await client.request(method, path, json={"ts": "2"})
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["ok"] is True

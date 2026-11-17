"""The Shared Crew reads for a teammate the Kiro relay verified.

``GET /api/shared-crew/people``, ``/sessions`` and ``/mode`` admit a team Crew
teammate the relay handed over, as they admit a person the tailnet or SSM
listener admitted. Driven over a real loopback listener with the relay ingress,
the client-sent strip and token auth chained as both gateway chains order them.
The relay marker is the caller the ingress publishes under a request key, so a
request that only carries relay headers, or an actor that only looks relayed, is
refused. Invite management and the name-and-picture edit stay owner-only.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from shared_crew_host_isolation import host_isolation  # noqa: F401
from token_auth_helpers import clear_nonces  # noqa: F401 -- the autouse fixture registers by import

from kiro_crew.dashboard.kiro_relay.caller import (
    RELAY_CALLER_KEY,
    RELAY_HANDOVER_HEADER,
    RELAY_USER_HEADER,
    CrewKind,
    HostBinding,
    RelayCaller,
    RelayCallerKind,
)
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.relay_headers import relay_header_strip_middleware
from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware
from kiro_crew.history import ConversationLog
from kiro_crew.shared_crew import owner, people, routes_identity, routes_invites, routes_people
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.identity import relay_principal

pytestmark = pytest.mark.usefixtures("host_isolation")

OWNER = "kiro-user.owner-0001"
ANA = "kiro-user.teammate-ana-0002"
READS = (routes_identity.PEOPLE_PATH, routes_identity.SESSIONS_PATH, routes_identity.MODE_PATH)


@pytest.fixture(autouse=True)
def _fresh(_floor_monkeypatch: pytest.MonkeyPatch):
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    people.reset_for_tests()
    yield
    people.reset_for_tests()


def _register(app: web.Application) -> None:
    routes_identity.register(app)
    routes_people.register(app)
    app.router.add_get(routes_invites.STATUS_PATH, routes_invites.api_status)
    app.router.add_post(routes_invites.STATUS_PATH, routes_invites.api_create)


def _relay_app(tmp_path, kind: CrewKind) -> web.Application:
    ingress = RelayIngress(enabled=True)
    ingress.bind_host(
        HostBinding(crew_kind=kind, owner_user_id=OWNER if kind is CrewKind.PERSONAL else "")
    )
    app = web.Application(
        middlewares=[
            relay_ingress_middleware(strict_internal_paths=_STRICT_INTERNAL_API_PATHS),
            relay_header_strip_middleware,
            token_auth_middleware(port=5476),
        ]
    )
    base = tmp_path / "sessions"
    base.mkdir(exist_ok=True)
    app["state"] = SimpleNamespace(owner_id="", conversation_log=ConversationLog(base))
    app["allowed_origins"] = set()
    app[RELAY_INGRESS_APP_KEY] = ingress
    _register(app)
    return app


def _stamped(client: TestClient, user: str) -> Any:
    return client.server.app[RELAY_INGRESS_APP_KEY].stamp_headers([], relay_user=user)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", READS)
async def test_a_relayed_teammate_reads_people_sessions_and_mode(tmp_path, path: str) -> None:
    async with TestClient(TestServer(_relay_app(tmp_path, CrewKind.TEAM))) as client:
        resp = await client.get(path, headers=_stamped(client, ANA))
        assert resp.status == 200, await resp.text()


@pytest.mark.asyncio
async def test_the_people_read_lists_the_teammate_as_the_viewer(tmp_path) -> None:
    async with TestClient(TestServer(_relay_app(tmp_path, CrewKind.TEAM))) as client:
        resp = await client.get(routes_identity.SESSIONS_PATH, headers=_stamped(client, ANA))
        body = await resp.json()
    assert body["viewer"]["id"] == principal_id_for("kiro", "kiro-relay", ANA)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", READS)
async def test_relay_headers_without_the_handover_are_not_a_relay_teammate(
    tmp_path, path: str
) -> None:
    # A client that writes the relay's user header itself: the strip drops it and
    # the request is an ordinary one with no credential.
    async with TestClient(TestServer(_relay_app(tmp_path, CrewKind.TEAM))) as client:
        resp = await client.get(path, headers={RELAY_USER_HEADER: ANA})
        assert resp.status == 403
        assert (await resp.json())["error"] == "Token required"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", READS)
async def test_a_non_owner_token_with_relay_headers_is_still_refused(tmp_path, path: str) -> None:
    # A dashboard token for someone who is not the owner, carrying the relay's user
    # header: the strip drops the header and the owner gate refuses the token.
    app = _relay_app(tmp_path, CrewKind.TEAM)
    app["state"].owner_id = "U0OWNER"
    owner.set_owner_subject("U0OWNER")
    async with TestClient(TestServer(app)) as client:
        token = generate_token("U0SOMEONE")
        resp = await client.get(path, params={"token": token}, headers={RELAY_USER_HEADER: ANA})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "owner_only"


@pytest.mark.asyncio
async def test_a_forged_handover_is_refused_before_any_route(tmp_path) -> None:
    async with TestClient(TestServer(_relay_app(tmp_path, CrewKind.TEAM))) as client:
        resp = await client.get(
            routes_identity.PEOPLE_PATH,
            headers={RELAY_USER_HEADER: ANA, RELAY_HANDOVER_HEADER: "not-the-credential"},
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "relay_handover_invalid"


@pytest.mark.asyncio
async def test_a_relayed_stranger_on_a_personal_crew_is_refused(tmp_path) -> None:
    async with TestClient(TestServer(_relay_app(tmp_path, CrewKind.PERSONAL))) as client:
        resp = await client.get(routes_identity.PEOPLE_PATH, headers=_stamped(client, ANA))
        assert resp.status == 403
        assert (await resp.json())["code"] == "relay_not_owner"


@pytest.mark.asyncio
async def test_invite_management_stays_owner_only_for_a_relayed_teammate(tmp_path) -> None:
    async with TestClient(TestServer(_relay_app(tmp_path, CrewKind.TEAM))) as client:
        headers = _stamped(client, ANA)
        listed = await client.get(routes_invites.STATUS_PATH, headers=headers)
        created = await client.post(
            routes_invites.STATUS_PATH, headers=headers, json={"emails": ["b@example.com"]}
        )
        renamed = await client.put(
            routes_people.DISPLAY_PATH.format(user_id="u1"),
            headers=headers,
            json={"alias": "Bea"},
        )
    assert listed.status == 403
    assert created.status == 403
    assert renamed.status == 403


# -- the marker is the ingress-published caller, never the actor alone --


def _teammate() -> RelayCaller:
    return RelayCaller(kiro_user_id=ANA, kind=RelayCallerKind.TEAMMATE)


async def _forged_client(tmp_path, *, caller: object) -> TestClient:
    """A dashboard user whose actor is a relayed teammate's, with *caller* published."""

    @web.middleware
    async def fake_auth(request: web.Request, handler: Any) -> web.StreamResponse:
        request["is_dashboard_user"] = True
        request["app"] = ""
        request["user"] = f"relay:{ANA}"
        if caller is not None:
            request[RELAY_CALLER_KEY] = caller
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=relay_principal(_teammate(), ""),
            request_id="r",
            transport=Transport.REMOTE,
        )
        return await handler(request)

    app = web.Application(middlewares=[fake_auth])
    base = tmp_path / "sessions"
    base.mkdir(exist_ok=True)
    app["state"] = SimpleNamespace(owner_id="", conversation_log=ConversationLog(base))
    routes_identity.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", [None, {"kiro_user_id": ANA}, f"relay:{ANA}"])
async def test_a_relayed_looking_actor_without_the_published_caller_is_refused(
    tmp_path, forged: object
) -> None:
    client = await _forged_client(tmp_path, caller=forged)
    try:
        for path in READS:
            assert (await client.get(path)).status == 403, path
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_published_relayed_owner_is_not_admitted_as_a_teammate(tmp_path) -> None:
    owner_caller = RelayCaller(kiro_user_id=OWNER, kind=RelayCallerKind.OWNER)
    client = await _forged_client(tmp_path, caller=owner_caller)
    try:
        # The relayed owner goes through the owner gate on the owner's own subject;
        # under a teammate's subject that gate refuses.
        assert (await client.get(routes_identity.PEOPLE_PATH)).status == 403
    finally:
        await client.close()

"""A relayed page load of the bare root goes to /chat; everything else is unchanged.

The control plane hands out a Crew's relay root as its address, and the dashboard's
home is ``/chat``. The relay ingress answers a verified ``GET`` or ``HEAD`` of the
bare ``/`` with a relative ``302`` to ``/chat``. Every neighbouring input is pinned
to its old behaviour: deep links, the API, static assets, a query on ``/``, other
methods, a WebSocket upgrade, a direct (non-relay) root, and a request the ingress
refuses.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from token_auth_helpers import clear_nonces  # noqa: F401 -- the autouse fixture registers by import

from kiro_crew.dashboard.kiro_relay.caller import (
    RELAY_HANDOVER_HEADER,
    RELAY_USER_HEADER,
    CrewKind,
    HostBinding,
    published_relay_caller,
)
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.relay_headers import relay_header_strip_middleware
from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.relay_client.ingress import LoopbackIngress, RelayedRequest
from kiro_crew.relay_client.protocol import WS_MESSAGE_MAX_BYTES

pytestmark = pytest.mark.asyncio

OWNER = "kiro-user.owner-0001"
TEAMMATE = "kiro-user.teammate-0002"


async def _served(request: web.Request) -> web.Response:
    """Stands in for every route: says what was served and whether it was relayed."""
    return web.json_response(
        {"path": request.path_qs, "relayed": published_relay_caller(request) is not None}
    )


def _app(ingress: RelayIngress | None) -> web.Application:
    """The relay layers and token auth in both gateway chains' order."""
    app = web.Application(
        middlewares=[
            relay_ingress_middleware(strict_internal_paths=_STRICT_INTERNAL_API_PATHS),
            relay_header_strip_middleware,
            token_auth_middleware(port=5476),
        ]
    )
    app["state"] = SimpleNamespace(owner_id="")
    app["allowed_origins"] = set()
    if ingress is not None:
        app[RELAY_INGRESS_APP_KEY] = ingress
    app.router.add_route("*", "/{tail:.*}", _served)
    return app


def _ingress(kind: CrewKind = CrewKind.PERSONAL) -> RelayIngress:
    ingress = RelayIngress(enabled=True)
    owner = OWNER if kind is CrewKind.PERSONAL else ""
    ingress.bind_host(HostBinding(crew_kind=kind, owner_user_id=owner))
    return ingress


def _stamped(ingress: RelayIngress, user: str = OWNER, **extra: str):
    return ingress.stamp_headers(list(extra.items()), relay_user=user)


@pytest.mark.parametrize(("kind", "user"), [(CrewKind.PERSONAL, OWNER), (CrewKind.TEAM, TEAMMATE)])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_a_relayed_root_page_load_redirects_to_chat(
    kind: CrewKind, user: str, method: str
) -> None:
    ingress = _ingress(kind)
    async with TestClient(TestServer(_app(ingress))) as client:
        resp = await client.request(
            method, "/", headers=_stamped(ingress, user), allow_redirects=False
        )
        assert resp.status == 302
        # Relative, so the browser stays on the relay host it dialled.
        assert resp.headers["Location"] == "/chat"


@pytest.mark.parametrize(
    "path",
    [
        "/chat",
        "/chat/",
        "/chat/some-session",
        "/chat?x=1",
        "/api/status",
        "/api/sessions",
        "/assets/index-abc123.js",
        "/logo.png",
        "/favicon.ico",
        "/settings",
        "/?token=opaque",
        "/?next=/settings",
    ],
)
async def test_every_other_relayed_get_is_served_as_before(path: str) -> None:
    ingress = _ingress()
    async with TestClient(TestServer(_app(ingress))) as client:
        resp = await client.get(path, headers=_stamped(ingress), allow_redirects=False)
        assert resp.status == 200, path
        body = await resp.json()
        assert body == {"path": path, "relayed": True}


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS"])
async def test_a_relayed_root_request_of_another_method_is_not_redirected(method: str) -> None:
    ingress = _ingress()
    async with TestClient(TestServer(_app(ingress))) as client:
        resp = await client.request(
            method,
            "/",
            headers=_stamped(ingress, Origin=str(client.make_url(""))),
            allow_redirects=False,
        )
        assert resp.status != 302
        assert "Location" not in resp.headers


async def test_a_relayed_websocket_upgrade_on_root_is_not_redirected() -> None:
    ingress = _ingress()
    async with TestClient(TestServer(_app(ingress))) as client:
        resp = await client.get(
            "/",
            headers=_stamped(ingress, Upgrade="websocket", Connection="Upgrade"),
            allow_redirects=False,
        )
        assert resp.status == 200
        assert "Location" not in resp.headers


@pytest.mark.parametrize("relay_enabled", [True, False])
async def test_a_direct_root_request_is_served_as_before(relay_enabled: bool) -> None:
    """Off the relay, ``/`` is untouched, even with a client-sent caller header."""
    ingress = _ingress() if relay_enabled else RelayIngress(enabled=False)
    async with TestClient(TestServer(_app(ingress))) as client:
        for headers in ({}, {RELAY_USER_HEADER: OWNER}):
            resp = await client.get("/", headers=headers, allow_redirects=False)
            assert resp.status != 302
            assert "Location" not in resp.headers


async def test_a_refused_relayed_root_request_is_refused_not_redirected() -> None:
    """The redirect comes after verification: a bad handover or a non-owner still gets 403."""
    ingress = _ingress()
    async with TestClient(TestServer(_app(ingress))) as client:
        forged = await client.get(
            "/",
            headers={RELAY_HANDOVER_HEADER: "not-the-credential", RELAY_USER_HEADER: OWNER},
            allow_redirects=False,
        )
        assert forged.status == 403
        assert (await forged.json())["code"] == "relay_handover_invalid"

        stranger = await client.get("/", headers=_stamped(ingress, TEAMMATE), allow_redirects=False)
        assert stranger.status == 403
        assert (await stranger.json())["code"] == "relay_not_owner"

        # The one remote limit a bare / can meet: a local credential header.
        limited = await client.get(
            "/",
            headers=_stamped(ingress, **{"X-Internal-Secret": "x"}),
            allow_redirects=False,
        )
        assert limited.status == 403
        assert (await limited.json())["code"] == "relay_local_only"


async def test_a_relayed_root_request_with_an_unreadable_header_is_refused_not_redirected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The header-rebuild refusal answers ``/`` before the redirect, as on every path."""

    def _unencodable(*_args: object, **_kwargs: object) -> web.Request:
        raise UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogates not allowed")

    ingress = _ingress()
    async with TestClient(TestServer(_app(ingress))) as client:
        stamped = _stamped(ingress)
        monkeypatch.setattr(web.Request, "clone", _unencodable)
        resp = await client.get("/", headers=stamped, allow_redirects=False)
        assert resp.status == 400
        assert (await resp.json())["code"] == "relay_header_invalid"


async def test_the_redirect_reaches_the_relay_through_the_loopback_hand_off() -> None:
    """The relay client hands the 302 and its Location back, never following it."""
    gateway = _ingress()
    server = TestServer(_app(gateway), host="127.0.0.1", port=0)
    await server.start_server()
    handoff = LoopbackIngress(
        "127.0.0.1",
        server.port or 0,
        gateway=gateway,
        max_ws_message_bytes=WS_MESSAGE_MAX_BYTES,
    )
    try:
        for path, query, status in (("/", "", 302), ("/", "a=b", 200), ("/chat", "", 200)):
            response = await handoff.open_http(
                RelayedRequest(method="GET", path=path, query=query, headers=(), user=OWNER),
                None,
            )
            try:
                assert response.status == status, (path, query)
                location = dict((k.lower(), v) for k, v in response.headers).get("location")
                assert location == ("/chat" if status == 302 else None)
            finally:
                await response.aclose()
    finally:
        await handoff.aclose()
        await server.close()

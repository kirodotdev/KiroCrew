"""The Kiro relay's caller identity, the relay-path header rule and the remote limits.

Driven end to end over a real loopback TCP listener (``aiohttp.test_utils``), with
the ingress middleware, the client-sent relay-header strip and token auth chained
the way both gateway chains order them, and a stub standing in for the relay client:
it calls the seam the relay client will call, :meth:`RelayIngress.stamp_headers`,
and sends what it returns.

The four cases the work item names are grouped below:

* the header is ignored on a local request and on a tailnet request;
* a teammate is never treated as the owner;
* each remote limit refuses with its own code;
* the owner check on a personal Crew.

Beside them: the strip just inside the ingress cannot undo the identity the ingress
set, and both layers read one prefix definition.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from token_auth_helpers import clear_nonces  # noqa: F401 -- the autouse fixture registers by import

from kiro_crew.apps.proxy_auth import PRINCIPAL_NONE, PRINCIPAL_OWNER_SESSION
from kiro_crew.apps.routes_proxy import _proxy_principal
from kiro_crew.dashboard import relay_headers
from kiro_crew.dashboard import server as dashboard_server
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.kiro_relay import caller as relay_caller
from kiro_crew.dashboard.kiro_relay import ingress as relay_ingress
from kiro_crew.dashboard.kiro_relay import limits
from kiro_crew.dashboard.kiro_relay.caller import (
    RELAY_HANDOVER_HEADER,
    RELAY_REMOTE,
    RELAY_USER_HEADER,
    CrewKind,
    HostBinding,
    Provenance,
    RelayCaller,
    RelayCallerKind,
    Transport,
    published_relay_caller,
)
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    RelayIngressError,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.origin import (
    check_origin,
    is_direct_local_request,
    is_https_request,
    is_proxied_request,
)
from kiro_crew.dashboard.relay_headers import (
    relay_header_strip_middleware,
    strip_client_relay_headers,
)
from kiro_crew.dashboard.server import audit_actor
from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import (
    generate_token,
    token_auth_middleware,
)
from kiro_crew.shared_crew.contracts import PrincipalKind
from kiro_crew.shared_crew.contracts import Provenance as ContractProvenance
from kiro_crew.shared_crew.identity import actor_of

OWNER = "kiro-user.owner-0001"
TEAMMATE = "kiro-user.teammate-0002"


async def _echo(request: web.Request) -> web.Response:
    """What the handler saw: identity, relay attribution and locality."""
    caller = published_relay_caller(request)
    kind, owner_id = _proxy_principal(request)
    return web.json_response(
        {
            "user": request.get("user", ""),
            "app": request.get("app"),
            "auth_token": request.get("auth_token"),
            "is_dashboard_user": request.get("is_dashboard_user"),
            "owner": is_owner_dashboard_request(request),
            "principal_kind": kind,
            "principal_owner": owner_id,
            "audit_actor": audit_actor(request, "dashboard_user"),
            "https": is_https_request(request),
            "relay_kind": caller.kind.value if caller else None,
            "provenance": caller.provenance.value if caller else None,
            "transport": caller.transport.value if caller else None,
            "relay_headers": sorted(
                name.lower() for name in request.headers if name.lower().startswith("x-kiro-relay-")
            ),
            "actor_kind": actor_of(request).principal.kind.value,
            "actor_provenance": actor_of(request).principal.provenance.value,
            "remote": request.remote,
            "proxied": is_proxied_request(request),
            "direct_local": is_direct_local_request(request),
            "no_origin_csrf_ok": check_origin(request),
        }
    )


def _app(
    ingress: RelayIngress | None,
    *,
    owner_id: str = "",
    mixed_internal_paths: frozenset[str] = frozenset(),
    between: tuple[object, ...] = (),
) -> web.Application:
    """The relay layers and token auth in both gateway chains' order.

    *between* goes between the ingress and the client-sent strip, to observe what the
    ingress hands on.
    """
    app = web.Application(
        middlewares=[
            relay_ingress_middleware(strict_internal_paths=_STRICT_INTERNAL_API_PATHS),
            *between,
            relay_header_strip_middleware,
            token_auth_middleware(port=5476, mixed_internal_paths=mixed_internal_paths),
        ]
    )
    app["state"] = SimpleNamespace(owner_id=owner_id)
    app["allowed_origins"] = set()
    if ingress is not None:
        app[RELAY_INGRESS_APP_KEY] = ingress
    app.router.add_route("*", "/{tail:.*}", _echo)
    return app


def _personal() -> RelayIngress:
    ingress = RelayIngress(enabled=True)
    ingress.bind_host(HostBinding(crew_kind=CrewKind.PERSONAL, owner_user_id=OWNER))
    return ingress


def _team() -> RelayIngress:
    ingress = RelayIngress(enabled=True)
    ingress.bind_host(HostBinding(crew_kind=CrewKind.TEAM))
    return ingress


def _relayed(ingress: RelayIngress, user: str, extra: dict[str, str] | None = None):
    """The stub relay client: the person's headers, stamped through the seam."""
    client_headers = list((extra or {}).items())
    return ingress.stamp_headers(client_headers, relay_user=user)


# ---------------------------------------------------------------------------
# The header is ignored on a local request and on a tailnet request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_header_ignored_on_a_local_request(enabled: bool) -> None:
    """A local caller naming the owner in x-kiro-relay-user is still a local caller.

    With a token it is served as its own token's subject, with the relay headers
    gone before the handler. Without one it is refused like any tokenless request,
    never admitted as the person it named.
    """
    ingress = _personal() if enabled else RelayIngress(enabled=False)
    spoof = {RELAY_USER_HEADER: OWNER, "X-Kiro-Relay-Region": "us-east-1"}
    async with TestClient(TestServer(_app(ingress))) as client:
        token = generate_token("local-app", ttl_seconds=300)
        resp = await client.get("/api/status", params={"token": token}, headers=spoof)
        body = await resp.json()
        assert resp.status == 200
        assert body["user"] == "local-app"
        assert body["relay_kind"] is None
        assert body["relay_headers"] == []
        assert body["remote"] == "127.0.0.1"

        # Drop the session cookie the link exchange set, so the next request
        # carries no Crew credential at all.
        client.session.cookie_jar.clear()
        resp = await client.get("/api/status", headers=spoof)
        assert resp.status == 403
        assert (await resp.json())["error"] == "Token required"


@pytest.mark.asyncio
async def test_header_ignored_on_a_tailnet_request() -> None:
    """A request through ``tailscale serve`` naming someone in the header is not relayed.

    It reaches the gateway from loopback with the forwarding and Tailscale identity
    headers the serve proxy adds; the relay header is stripped all the same, and
    without a token it is refused rather than admitted as the named person.
    """
    tailnet = {
        "X-Forwarded-For": "100.64.0.5",
        "Tailscale-User-Login": "you@example.com",
        RELAY_USER_HEADER: OWNER,
    }
    async with TestClient(TestServer(_app(_personal()))) as client:
        resp = await client.get("/api/status", headers=tailnet)
        assert resp.status == 403
        assert (await resp.json())["error"] == "Token required"

        token = generate_token("local-app", ttl_seconds=300)
        resp = await client.get("/api/status", params={"token": token}, headers=tailnet)
        body = await resp.json()
        assert resp.status == 200
        assert body["relay_kind"] is None
        assert body["relay_headers"] == []


@pytest.mark.asyncio
async def test_a_forged_handover_is_refused_not_admitted() -> None:
    """A local process guessing the handover credential is refused, typed."""
    forged = {RELAY_USER_HEADER: OWNER, RELAY_HANDOVER_HEADER: "guess"}
    async with TestClient(TestServer(_app(_personal()))) as client:
        resp = await client.get("/api/status", headers=forged)
        assert resp.status == 403
        assert (await resp.json())["code"] == "relay_handover_invalid"


@pytest.mark.asyncio
async def test_the_relay_off_honors_nothing() -> None:
    """An ingress built off refuses the relay client's calls and stamps nothing."""
    ingress = RelayIngress(enabled=False)
    with pytest.raises(RelayIngressError):
        ingress.bind_host(HostBinding(crew_kind=CrewKind.TEAM))
    with pytest.raises(RelayIngressError):
        ingress.stamp_headers([], relay_user=OWNER)
    assert ingress.handed_over("") is False


def test_the_gateway_builds_its_ingress_from_the_relay_switch() -> None:
    """The dashboard app's ingress is built from ``tunnel.relay_enabled``, nothing else.

    Only the JSON boolean ``true`` reads as on, and an enterprise build ignores that
    local switch with the rest of the ``tunnel`` control.
    """
    tree = ast.parse(Path(inspect.getfile(dashboard_server)).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "RelayIngress"
    ]
    assert len(calls) == 1
    (enabled,) = [kw.value for kw in calls[0].keywords if kw.arg == "enabled"]
    assert ast.unparse(enabled) == (
        "_cfg.tunnel.relay_enabled is True and (not local_enable_locked('tunnel'))"
    )


# ---------------------------------------------------------------------------
# The stamp: the relay's own value always wins
# ---------------------------------------------------------------------------


def test_the_stamp_overwrites_every_client_relay_header() -> None:
    ingress = _personal()
    stamped = ingress.stamp_headers(
        [
            ("X-Kiro-Relay-User", "someone-else"),
            ("x-kiro-relay-handover", "guess"),
            ("X-KIRO-RELAY-ANYTHING", "x"),
            ("Accept", "application/json"),
        ],
        relay_user=OWNER,
    )
    assert stamped.getall(RELAY_USER_HEADER) == [OWNER]
    assert len(stamped.getall(RELAY_HANDOVER_HEADER)) == 1
    assert "X-Kiro-Relay-Anything" not in stamped
    assert stamped["Accept"] == "application/json"


def test_the_stamp_needs_a_bound_host_and_a_valid_user() -> None:
    ingress = RelayIngress(enabled=True)
    with pytest.raises(RelayIngressError):
        ingress.stamp_headers([], relay_user=OWNER)
    ingress.bind_host(HostBinding(crew_kind=CrewKind.TEAM))
    for bad in ("", "has space", "a,b", "x" * 257, "ünï"):
        with pytest.raises(ValueError):
            ingress.stamp_headers([], relay_user=bad)


@pytest.mark.asyncio
async def test_unbinding_retires_every_earlier_stamp() -> None:
    ingress = _personal()
    stale = _relayed(ingress, OWNER)
    ingress.unbind_host()
    ingress.bind_host(HostBinding(crew_kind=CrewKind.PERSONAL, owner_user_id=OWNER))
    async with TestClient(TestServer(_app(ingress))) as client:
        resp = await client.get("/api/status", headers=stale)
        assert resp.status == 403
        assert (await resp.json())["code"] == "relay_handover_invalid"
        resp = await client.get("/api/status", headers=_relayed(ingress, OWNER))
        assert resp.status == 200


@pytest.mark.asyncio
async def test_a_handover_off_loopback_tcp_is_refused() -> None:
    """The credential is honored only on the loopback TCP listener the client dials."""
    ingress = _personal()
    app = _app(ingress)
    request = make_mocked_request("GET", "/api/status", headers=_relayed(ingress, OWNER), app=app)
    request = request.clone(remote="10.0.0.5")

    async def _never(_request: web.Request) -> web.StreamResponse:
        raise AssertionError("a refused handover reached the handler")

    resp = await relay_ingress_middleware(strict_internal_paths=frozenset())(request, _never)
    assert resp.status == 403
    assert resp.text is not None and "relay_handover_transport" in resp.text


@pytest.mark.asyncio
async def test_a_handover_beside_an_unreadable_header_is_refused_not_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a handover's headers can't be rebuilt without the relay header, nothing is served.

    aiohttp hands a header byte that is not valid UTF-8 to the app as a lone
    surrogate, which the clone cannot re-encode; the clone's failure is forced here
    because a test request cannot be built from such a header in the first place.
    """

    def _unencodable(*_args: object, **_kwargs: object) -> web.Request:
        raise UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogates not allowed")

    async with TestClient(TestServer(_app(_personal()))) as client:
        stamped = _relayed(_personal_like(client), OWNER)
        monkeypatch.setattr(web.Request, "clone", _unencodable)
        resp = await client.get("/api/status", headers=stamped)
        assert resp.status == 400
        assert (await resp.json())["code"] == "relay_header_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "with_handover"),
    [("absent", False), ("absent", True), ("off", True), ("on", False)],
)
async def test_a_request_without_a_handover_goes_on_unchanged_to_the_strip(
    state: str, with_handover: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """The rule handles only a request presenting the handover credential to a live ingress.

    Any other request, and any request on an app whose ingress is off or absent (the
    headless API server builds none), is handed on as the very same object: the
    client-sent strip just inside is the one place a client-sent relay header is
    dropped and logged.
    """
    app = web.Application()
    if state == "off":
        app[RELAY_INGRESS_APP_KEY] = RelayIngress(enabled=False)
    elif state == "on":
        app[RELAY_INGRESS_APP_KEY] = _personal()
    headers = {RELAY_USER_HEADER: OWNER}
    if with_handover:
        headers[RELAY_HANDOVER_HEADER] = "client-sent"
    request = make_mocked_request("GET", "/api/status", headers=headers, app=app)
    handed_on: list[web.Request] = []

    async def _handler(seen: web.Request) -> web.StreamResponse:
        handed_on.append(seen)
        return web.Response(text="ok")

    caplog.set_level("WARNING", logger=relay_ingress.__name__)
    resp = await relay_ingress_middleware(strict_internal_paths=frozenset())(request, _handler)
    assert resp.status == 200
    assert len(handed_on) == 1 and handed_on[0] is request
    assert [r for r in caplog.records if r.name == relay_ingress.__name__] == []


@pytest.mark.asyncio
async def test_a_handover_without_a_bound_host_is_refused() -> None:
    ingress = _personal()
    stamped = _relayed(ingress, OWNER)
    ingress._binding = None  # the connection dropped between stamp and arrival
    async with TestClient(TestServer(_app(ingress))) as client:
        resp = await client.get("/api/status", headers=stamped)
        assert resp.status == 403
        assert (await resp.json())["code"] == "relay_not_connected"


# ---------------------------------------------------------------------------
# The relayed caller: provenance, remote transport, never local
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_relayed_request_carries_relay_provenance_and_never_reads_as_local() -> None:
    async with TestClient(TestServer(_app(_personal()))) as client:
        resp = await client.get("/api/status", headers=_relayed(_personal_like(client), OWNER))
        body = await resp.json()
    assert resp.status == 200
    assert body["provenance"] == Provenance.KIRO_RELAY.value == "kiro_relay"
    assert body["transport"] == Transport.REMOTE.value == "remote"
    assert body["remote"] == RELAY_REMOTE
    assert body["relay_headers"] == []
    assert body["proxied"] is True
    # The audit trail names the person the relay verified.
    assert body["audit_actor"] == f"relay:{OWNER}_via_proxy"
    # Every loopback-trusting gate is closed to it: the direct-local gates and the
    # no-Origin CSRF branch that trusts local processes.
    assert body["direct_local"] is False
    assert body["no_origin_csrf_ok"] is False
    # Token auth returns before publishing a shared-crew actor, so the request is
    # attributed Unknown there, never as the local operator.
    assert body["actor_kind"] == PrincipalKind.UNKNOWN.value
    assert body["actor_provenance"] != ContractProvenance.LOCAL_OPERATOR.value


def _personal_like(client: TestClient) -> RelayIngress:
    """The ingress the client's server was built with."""
    return client.server.app[RELAY_INGRESS_APP_KEY]


# ---------------------------------------------------------------------------
# The client-sent strip just inside the ingress
# ---------------------------------------------------------------------------


def test_the_ingress_and_the_strip_read_one_prefix_definition() -> None:
    """Both relay layers and the app proxy name relay headers by one definition.

    So the strip, the proxy and the ingress can never disagree about which header is
    the relay's, and the two headers the relay client stamps fall under it.
    """
    assert relay_caller.RELAY_HEADER_PREFIX is relay_headers.RELAY_HEADER_PREFIX
    assert relay_ingress.without_relay_headers is relay_headers.without_relay_headers
    assert not hasattr(relay_ingress, "RELAY_HEADER_PREFIX")
    for name in (RELAY_USER_HEADER, RELAY_HANDOVER_HEADER):
        assert relay_headers.is_relay_header(name), name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build", "user", "kind"),
    [
        (_personal, OWNER, RelayCallerKind.OWNER),
        (_team, TEAMMATE, RelayCallerKind.TEAMMATE),
    ],
)
async def test_the_strip_inside_the_ingress_keeps_a_verified_handovers_identity(
    build, user: str, kind: RelayCallerKind, caplog: pytest.LogCaptureFixture
) -> None:
    """The client-sent strip cannot remove or override what the ingress established.

    The ingress hands on a request with every relay header already gone, so the strip
    just inside it has nothing to drop and passes the very same request through. A
    clone there would matter: aiohttp resets ``request.remote`` to the socket's peer
    on a clone, which would turn a relayed request back into a loopback one.
    """
    handed_on: list[web.Request] = []

    @web.middleware
    async def _observe(request: web.Request, handler):
        handed_on.append(request)
        return await handler(request)

    # A client-sent relay header beside the stamp is dropped by the stamp itself.
    extra = {"X-Kiro-Relay-Region": "us-east-1"}
    caplog.set_level("WARNING", logger=relay_headers.__name__)
    async with TestClient(TestServer(_app(build(), between=(_observe,)))) as client:
        resp = await client.get(
            "/api/status", headers=_relayed(_personal_like(client), user, extra)
        )
        body = await resp.json()

    assert resp.status == 200
    assert body["relay_kind"] == kind.value
    assert body["remote"] == RELAY_REMOTE
    assert body["relay_headers"] == []
    assert body["audit_actor"] == f"relay:{user}_via_proxy"
    assert body["direct_local"] is False
    # The strip dropped nothing, so it logged nothing and cloned nothing.
    assert [r for r in caplog.records if r.name == relay_headers.__name__] == []
    (request,) = handed_on
    assert strip_client_relay_headers(request) is request
    published = published_relay_caller(request)
    assert published is not None and published.kiro_user_id == user
    assert published.kind is kind
    assert request.remote == RELAY_REMOTE


# ---------------------------------------------------------------------------
# The owner check on a personal Crew
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_id", ["", "U0OWNER"])
async def test_the_owner_of_a_personal_crew_is_the_owner(owner_id: str) -> None:
    async with TestClient(TestServer(_app(_personal(), owner_id=owner_id))) as client:
        resp = await client.get("/api/status", headers=_relayed(_personal_like(client), OWNER))
        body = await resp.json()
    assert resp.status == 200
    assert body["relay_kind"] == RelayCallerKind.OWNER.value
    assert body["user"] == (owner_id or "local-app")
    assert body["owner"] is True
    # The app proxy's signed claim names the owner (learning L6).
    assert body["principal_kind"] == PRINCIPAL_OWNER_SESSION
    assert body["principal_owner"] == (owner_id or "local-app")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "named",
    [
        TEAMMATE,
        # Same person, other sign-in provider: a separate Kiro user, so refused.
        "github.kiro-user.owner-0001",
        # The comparison is exact: no case folding.
        OWNER.upper(),
    ],
)
async def test_a_personal_crew_refuses_anyone_but_its_owner(named: str) -> None:
    async with TestClient(TestServer(_app(_personal()))) as client:
        resp = await client.get("/api/status", headers=_relayed(_personal_like(client), named))
        assert resp.status == 403
        assert (await resp.json())["code"] == "relay_not_owner"


def test_a_personal_binding_needs_an_owner_and_a_team_binding_has_none() -> None:
    with pytest.raises(ValueError):
        HostBinding(crew_kind=CrewKind.PERSONAL)
    with pytest.raises(ValueError):
        HostBinding(crew_kind=CrewKind.TEAM, owner_user_id=OWNER)


# ---------------------------------------------------------------------------
# A teammate is never treated as the owner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_id", ["", "U0OWNER"])
async def test_a_teammate_is_never_the_owner(owner_id: str) -> None:
    async with TestClient(TestServer(_app(_team(), owner_id=owner_id))) as client:
        resp = await client.get("/api/status", headers=_relayed(_personal_like(client), TEAMMATE))
        body = await resp.json()
    assert resp.status == 200
    assert body["relay_kind"] == RelayCallerKind.TEAMMATE.value
    assert body["user"] == f"relay:{TEAMMATE}"
    assert body["is_dashboard_user"] is True
    assert body["owner"] is False
    # The app proxy's claim for a teammate names no owner.
    assert body["principal_kind"] == PRINCIPAL_NONE
    assert body["principal_owner"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "teammate"])
@pytest.mark.parametrize("where", ["query", "cookie"])
@pytest.mark.parametrize("kind", ["dashboard", "app"])
async def test_a_crew_credential_over_the_relay_is_never_honored(
    who: str, where: str, kind: str
) -> None:
    """A Crew token a relayed request carries is never read, and none is issued.

    The relay authenticates a relayed request, so the person is admitted as the
    relay named them whatever Crew token rides along: a teammate holding the
    owner's link stays the teammate, and an app token grants no app scope.
    """
    ingress = _personal() if who == "owner" else _team()
    user = OWNER if who == "owner" else TEAMMATE
    token = (
        generate_token("local-app", ttl_seconds=300)
        if kind == "dashboard"
        else generate_token("notes", app="notes")
    )
    async with TestClient(TestServer(_app(ingress))) as client:
        params = {}
        if where == "query":
            params = {"token": token}
        else:
            client.session.cookie_jar.update_cookies({f"mc_token_{client.port}": token})
        resp = await client.get(
            "/api/sessions", params=params, headers=_relayed(_personal_like(client), user)
        )
        assert resp.status == 200
        body = await resp.json()
        assert not [c for c in resp.cookies if c.startswith(("mc_token_", "mc_refresh_"))]
    assert body["user"] == ("local-app" if who == "owner" else f"relay:{TEAMMATE}")
    assert body["owner"] is (who == "owner")
    assert body["app"] == ""
    assert body["auth_token"] == ""


# ---------------------------------------------------------------------------
# A refusal from each remote limit
# ---------------------------------------------------------------------------

_LIMIT_CASES = [
    # (crew, method, path, extra client headers, expected limit)
    ("personal", "POST", "/api/send-message", {}, limits.LOCAL_ONLY),
    ("personal", "GET", "/api/status", {"X-Internal-Secret": "x"}, limits.LOCAL_ONLY),
    ("personal", "GET", "/api/status", {"X-Local-Secret": "x"}, limits.LOCAL_ONLY),
    ("personal", "GET", "/api/instances", {}, limits.INSTANCES_OFF),
    ("team", "POST", "/api/instances/abc/connect", {}, limits.INSTANCES_OFF),
    ("personal", "POST", "/api/token/local", {}, limits.LOCAL_CREDENTIAL),
    ("personal", "POST", "/api/auth/mobile-link", {}, limits.LOCAL_CREDENTIAL),
    ("personal", "POST", "/api/tailnet/mobile/qr", {}, limits.LOCAL_CREDENTIAL),
    ("personal", "POST", "/api/auth/refresh", {}, limits.LOCAL_CREDENTIAL),
    ("personal", "POST", "/api/apps/notes/token", {}, limits.LOCAL_CREDENTIAL),
    ("personal", "POST", "/api/apps/register", {}, limits.LOCAL_CREDENTIAL),
    ("personal", "POST", "/api/apps/register/", {}, limits.LOCAL_CREDENTIAL),
    ("personal", "POST", "/api/apps/dev-fleet/pod-token", {}, limits.LOCAL_CREDENTIAL),
    ("personal", "POST", "/api/webhooks/tokens", {}, limits.LOCAL_CREDENTIAL),
    ("personal", "PATCH", "/api/webhooks/tokens/tok_1", {}, limits.LOCAL_CREDENTIAL),
    ("personal", "POST", "/apps/dev-fleet/api/pod/token", {}, limits.LOCAL_CREDENTIAL),
    ("personal", "POST", "/apps/dev-fleet/api//pod/token", {}, limits.NONCANONICAL_PATH),
    ("personal", "POST", "/apps/dev-fleet/api/pod/%2574oken", {}, limits.NONCANONICAL_PATH),
    ("personal", "GET", "/api/secrets", {}, limits.LOCAL_CREDENTIAL),
    ("team", "DELETE", "/api/secrets/GITHUB_TOKEN", {}, limits.LOCAL_CREDENTIAL),
    ("personal", "POST", "/api/shutdown", {}, limits.SHUTDOWN),
    ("team", "GET", "/apps/notes/ui/index.html", {}, limits.APPS_OFF_FOR_TEAMMATES),
    ("team", "POST", "/apps/notes/api/save", {}, limits.APPS_OFF_FOR_TEAMMATES),
    ("team", "GET", "/api/apps", {}, limits.APPS_OFF_FOR_TEAMMATES),
    ("team", "GET", "/api/apps/issue-radar/repos", {}, limits.APPS_OFF_FOR_TEAMMATES),
    ("team", "GET", "/api/apps/issue-radar/issues", {}, limits.APPS_OFF_FOR_TEAMMATES),
    ("team", "POST", "/api/apps/notes/enable", {}, limits.APPS_OFF_FOR_TEAMMATES),
    ("team", "GET", "/api/publish-providers", {}, limits.APPS_OFF_FOR_TEAMMATES),
    ("team", "POST", "/api/app-store/refresh", {}, limits.APPS_OFF_FOR_TEAMMATES),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("crew", "method", "path", "extra", "limit"), _LIMIT_CASES)
async def test_each_remote_limit_refuses_with_its_code(
    crew: str, method: str, path: str, extra: dict[str, str], limit: limits.RemoteLimit
) -> None:
    ingress = _personal() if crew == "personal" else _team()
    user = OWNER if crew == "personal" else TEAMMATE
    async with TestClient(TestServer(_app(ingress))) as client:
        resp = await client.request(method, path, headers=_relayed(ingress, user, extra))
        assert resp.status == 403
        assert (await resp.json())["code"] == limit.code


def test_every_limit_has_a_case() -> None:
    assert {case[-1].code for case in _LIMIT_CASES} == {lim.code for lim in limits.ALL_LIMITS}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # Encoded once more than the router decodes: the app proxy re-encodes its
        # capture, so the backend would read the inner form.
        ("/apps/dev-fleet/api/pod/%74oken", limits.NONCANONICAL_PATH),
        ("/api/%2573ecrets", limits.NONCANONICAL_PATH),
        ("/api/sessions/%2541", limits.NONCANONICAL_PATH),
        ("/api/sessions/../token/local", limits.NONCANONICAL_PATH),
        ("/api/sessions/./x", limits.NONCANONICAL_PATH),
        ("/api/sessions/..", limits.NONCANONICAL_PATH),
        ("/api//shutdown", limits.NONCANONICAL_PATH),
        # Canonical paths go on to the route limits; one trailing slash is canonical.
        ("/api/sessions", None),
        ("/api/sessions/", None),
        ("/api/sessions/.well-known", None),
        ("/", None),
        ("/api/secrets/", limits.LOCAL_CREDENTIAL),
    ],
)
def test_a_path_not_in_canonical_form_is_refused(path, expected) -> None:
    owner = RelayCaller(kiro_user_id=OWNER, kind=RelayCallerKind.OWNER)
    got = limits.remote_limit_for(path, {}, owner, strict_internal_paths=())
    assert got == expected


@pytest.mark.asyncio
async def test_the_owner_keeps_apps_and_ordinary_routes() -> None:
    """The app limit is a teammate's only, and ordinary routes stay reachable."""
    async with TestClient(TestServer(_app(_personal()))) as client:
        ingress = _personal_like(client)
        for path in (
            "/apps/notes/ui/index.html",
            "/api/apps/issue-radar/repos",
            "/api/publish-providers",
            "/api/sessions",
            "/api/instancesx",
            "/api/apps/dev-fleet/pod-tokens",
            "/api/webhooks",
        ):
            resp = await client.get(path, headers=_relayed(ingress, OWNER))
            assert resp.status == 200, path

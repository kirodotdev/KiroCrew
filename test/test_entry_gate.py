"""The entry port: what a request that arrived on it may reach (``dashboard/entry_gate``).

The gateway fixture serves the real Crew API route table behind the relay-path rule,
the relay-header strip, the entry gate and token auth, in that order and without the
barriers that sit between the entry gate and token auth in the real chain, on two
loopback sockets of one runner, as ``entry_gate.start`` binds them: the gateway's own
port and the entry port. Each test sends the same request to one or both.
"""

from __future__ import annotations

import errno
import json
import logging
import socket
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from multidict import CIMultiDict
from test_app_web_origin import _ALLOWED, _HOST, _Backend
from test_crew_api_app_credential import (  # noqa: F401 -- fixtures register by import
    _grant,
    audit,
    clock,
    store,
)
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _session_token,
    clear_nonces,
)

from kiro_crew import platform_compat
from kiro_crew.apps import app_web_origin as awo
from kiro_crew.config.loader_builders import _build_dashboard_config
from kiro_crew.config.validation import validate_config_data
from kiro_crew.dashboard import crew_api_models, entry_gate, server, tailnet
from kiro_crew.dashboard import token_auth as ta
from kiro_crew.dashboard.crew_api_errors import is_owner_path
from kiro_crew.dashboard.kiro_relay.ingress import relay_ingress_middleware
from kiro_crew.dashboard.kiro_relay.limits import LOCAL_CREDENTIAL_HEADERS
from kiro_crew.dashboard.origin import (
    ENTRY_REQUEST_KEY,
    is_direct_local_request,
    is_host_forwarded_request,
    is_proxied_request,
)
from kiro_crew.dashboard.relay_headers import relay_header_strip_middleware
from kiro_crew.dashboard.routes import crew_api
from kiro_crew.dashboard.tailnet import TailnetTrust
from kiro_crew.dashboard.token_auth import token_auth_middleware

MODELS = "/v1/models"
PROBE = "/v1/crew/probe"
OWNER = "owner"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _probe(request: web.Request) -> web.Response:
    return web.json_response(
        {"direct_local": is_direct_local_request(request), "forwarded": is_proxied_request(request)}
    )


async def _reached(request: web.Request) -> web.Response:
    return web.Response(text="reached")


class _Gateway:
    """A client that sends to the gateway's own port or to the entry port."""

    def __init__(self, client: TestClient, entry_port: int) -> None:
        self.client = client
        self.entry_port = entry_port

    def url(self, path: str, *, entry: bool) -> str:
        port = self.entry_port if entry else self.client.port
        return f"http://127.0.0.1:{port}{path}"

    async def send(self, path: str, *, entry: bool, method: str = "GET", headers=None):
        return await self.client.session.request(
            method, self.url(path, entry=entry), headers=headers, allow_redirects=False
        )


@pytest_asyncio.fixture
async def gateway(monkeypatch):
    async for served in _two_ports(monkeypatch, token_auth_middleware()):
        yield served


async def _two_ports(monkeypatch, auth, *, ipv6=False):
    """The front of the chain with *auth* as its token auth, on both ports."""
    monkeypatch.setattr(crew_api_models, "usable_models", lambda: [])
    ref = entry_gate.GateRef()
    app = web.Application(
        middlewares=[
            relay_ingress_middleware(strict_internal_paths=frozenset()),
            relay_header_strip_middleware,
            entry_gate.entry_gate_middleware,
            auth,
        ]
    )
    app["state"] = SimpleNamespace(owner_id=OWNER)
    app[entry_gate.ENTRY_GATE_APP_KEY] = ref
    crew_api.register(app)
    app.router.add_get(PROBE, _probe)
    app.router.add_get("/api/health", _reached)
    app.router.add_post("/v1/chat/completions", _reached)
    with ExitStack() as stack:
        kwargs = {}
        if ipv6:
            own = stack.enter_context(_reserved_loopback_sockets(ipv6=True))
            kwargs["socket_factory"] = lambda host, port, family: own[host]
        client = TestClient(TestServer(app, host="127.0.0.1", **kwargs))
        try:
            await client.start_server()
            runner = client.server.runner
            if ipv6:
                entry = stack.enter_context(_reserved_loopback_sockets(ipv6=True))
                entry_port = entry["127.0.0.1"].getsockname()[1]
                for sock in [own["::1"], *entry.values()]:
                    await web.SockSite(runner, sock).start()
            else:
                await web.TCPSite(runner, entry_gate.BIND_ADDRESS, 0).start()
                entry_port = next(addr[1] for addr in runner.addresses if addr[1] != client.port)
            ref.gate = entry_gate.EntryGate(port=entry_port)
            yield _Gateway(client, entry_port)
        finally:
            await client.close()


async def _error(resp) -> tuple[int, str, str]:
    error = (await resp.json())["error"]
    assert error["request_id"] == resp.headers["X-Request-Id"]
    return resp.status, error["type"], error["code"]


# ── the mark comes from the socket ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_same_request_is_local_on_the_gateway_port_and_forwarded_on_the_entry_port(
    gateway,
) -> None:
    """No forwarding header on either: only the port the request arrived on differs."""
    owner = _bearer(_session_token(OWNER))
    own = await gateway.send(PROBE, entry=False, headers=owner)
    entry = await gateway.send(PROBE, entry=True, headers=owner)
    assert await own.json() == {"direct_local": True, "forwarded": False}
    assert await entry.json() == {"direct_local": False, "forwarded": True}


def test_a_marked_request_is_forwarded_and_never_local() -> None:
    request = _make_request(path=MODELS)
    request.get = lambda key, default=None: True if key == ENTRY_REQUEST_KEY else default
    assert is_direct_local_request(request) is False
    assert is_host_forwarded_request(request) is False
    assert is_proxied_request(request) is True


# ── no tailnet identity on the entry port ────────────────────────────────────

ALICE, MALLORY = "100.64.0.11", "100.64.0.13"
TAILNET = TailnetTrust(
    trust_identity=True, allowed_logins=("alice@example.com",), pin_scope="login"
)


def _tailnet_double(*, entry: bool, forwarded_for: str):
    """What the tailnet gates read: the peer, the headers and the entry mark."""
    return SimpleNamespace(
        remote="127.0.0.1",
        headers=CIMultiDict({"X-Forwarded-For": forwarded_for}),
        get=lambda key, default=None: entry if key == ENTRY_REQUEST_KEY else default,
    )


@pytest_asyncio.fixture
async def tailnet_gateway(_floor_monkeypatch, monkeypatch):
    """Both ports with tailnet identity trust on and the Shared Crew preview off.

    The daemon's ``whois`` names Alice (allowlisted) and Mallory (not). Each first
    use of a bearer that binds it, and each auth audit row, is recorded.
    """
    names = {ALICE: "alice@example.com", MALLORY: "mallory@example.com"}

    def _whois(argv: list[str]):
        login = names.get(argv[-1])
        if login is None:
            return None, False
        return {"Node": {"Name": f"{login}.ts.net."}, "UserProfile": {"LoginName": login}}, False

    _floor_monkeypatch.delenv("KIROCREW_SHARED_CREW_PREVIEW", raising=False)
    _floor_monkeypatch.setattr(tailnet, "IS_POSIX", True)
    whois = MagicMock(side_effect=_whois)
    _floor_monkeypatch.setattr(tailnet, "_run_json_detail", whois)
    tailnet._whois_cache.clear()
    ta._state.clear_all()
    bound: list = []
    logged: list = []
    bind, log = ta.bind_token_peer, ta._log_auth

    def _bind(*args, **kwargs):
        bound.append((args, kwargs))
        return bind(*args, **kwargs)

    def _log(request, user_id, outcome, error):
        logged.append((user_id, outcome, error))
        return log(request, user_id, outcome, error)

    monkeypatch.setattr(ta, "bind_token_peer", _bind)
    monkeypatch.setattr(ta, "_log_auth", _log)
    try:
        async for served in _two_ports(monkeypatch, token_auth_middleware(tailnet_trust=TAILNET)):
            yield SimpleNamespace(gateway=served, whois=whois, bound=bound, logged=logged)
    finally:
        tailnet._whois_cache.clear()
        ta._state.clear_all()


def _expiry(token: str) -> float:
    return json.loads(ta._b64url_decode(token.split(".")[0]))["session_exp"]


def test_an_entry_request_has_no_forwarded_tailnet_candidate() -> None:
    marked = _tailnet_double(entry=True, forwarded_for=ALICE)
    unmarked = _tailnet_double(entry=False, forwarded_for=ALICE)
    assert tailnet._forwarded_peer_candidate(marked, TAILNET) is None
    assert tailnet._forwarded_peer_candidate(unmarked, TAILNET) == ALICE


def test_an_entry_request_is_never_a_forwarded_tailnet_request() -> None:
    marked = _tailnet_double(entry=True, forwarded_for=f"{ALICE}, {MALLORY}")
    unmarked = _tailnet_double(entry=False, forwarded_for=f"{ALICE}, {MALLORY}")
    assert tailnet.is_forwarded_tailnet_request(marked, TAILNET) is False
    assert tailnet.is_forwarded_tailnet_request(unmarked, TAILNET) is True


@pytest.mark.asyncio
async def test_an_entry_request_is_never_refused_for_the_allowlist(_floor_monkeypatch) -> None:
    """Not forwarded, so ``forwarded_peer_refusal`` answers ``None`` without asking."""
    whois = MagicMock(side_effect=AssertionError("the daemon was asked"))
    _floor_monkeypatch.setattr(tailnet, "_run_json_detail", whois)
    tailnet._whois_cache.clear()
    marked = _tailnet_double(entry=True, forwarded_for=MALLORY)
    assert await tailnet.forwarded_peer_refusal(marked, TAILNET, None) is None
    assert await tailnet.resolve_forwarded_peer(marked, TAILNET) is None
    assert whois.call_count == 0


@pytest.mark.asyncio
async def test_an_allowlisted_forwarded_address_names_nobody_on_the_entry_port(
    tailnet_gateway,
) -> None:
    token = _session_token(OWNER)
    headers = {**_bearer(token), "X-Forwarded-For": ALICE}
    resp = await tailnet_gateway.gateway.send(MODELS, entry=True, headers=headers)
    assert resp.status == 200
    assert tailnet_gateway.whois.call_count == 0
    assert tailnet_gateway.logged == [(OWNER, "ok", "")]
    # Pinned to the proxy's address as a shared pin, never to a tailnet device.
    assert tailnet_gateway.bound == [((token, "ip:127.0.0.1", _expiry(token)), {"proxied": True})]


@pytest.mark.asyncio
async def test_an_off_allowlist_forwarded_address_is_not_refused_on_the_entry_port(
    tailnet_gateway,
) -> None:
    token = _session_token(OWNER)
    headers = {**_bearer(token), "X-Forwarded-For": MALLORY}
    resp = await tailnet_gateway.gateway.send(MODELS, entry=True, headers=headers)
    assert resp.status == 200
    assert tailnet_gateway.whois.call_count == 0
    assert tailnet_gateway.logged == [(OWNER, "ok", "")]


@pytest.mark.asyncio
async def test_an_allowlisted_forwarded_address_names_its_login_on_the_gateway_port(
    tailnet_gateway,
) -> None:
    token = _session_token(OWNER)
    headers = {**_bearer(token), "X-Forwarded-For": ALICE}
    resp = await tailnet_gateway.gateway.send(MODELS, entry=False, headers=headers)
    assert resp.status == 200
    assert tailnet_gateway.whois.call_count == 1
    assert tailnet_gateway.logged == [("alice@example.com", "ok", "")]
    assert tailnet_gateway.bound == [
        ((token, "ts:login:alice@example.com", _expiry(token)), {"proxied": False})
    ]


@pytest.mark.asyncio
async def test_an_off_allowlist_forwarded_address_is_refused_on_the_gateway_port(
    tailnet_gateway,
) -> None:
    headers = {**_bearer(_session_token(OWNER)), "X-Forwarded-For": MALLORY}
    resp = await tailnet_gateway.gateway.send(MODELS, entry=False, headers=headers)
    status, _, _ = await _error(resp)
    assert status == 401
    assert (await resp.json())["error"]["message"].startswith("tailnet login not allowed")
    assert tailnet_gateway.logged == [
        ("mallory@example.com", "denied", "tailnet login not allowed")
    ]
    assert tailnet_gateway.bound == []


# ── what the entry port serves ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_owners_bearer_reaches_a_public_route_through_the_entry_port(gateway) -> None:
    resp = await gateway.send(MODELS, entry=True, headers=_bearer(_session_token(OWNER)))
    assert resp.status == 200
    assert await resp.json() == {"object": "list", "data": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [False, True], ids=["gateway-port", "entry-port"])
async def test_a_header_naming_a_person_changes_no_identity_on_either_port(gateway, entry) -> None:
    """X-Kiro-Entry-User changes nothing: no credential stays 401, the owner's bearer is served."""
    named = await gateway.send(MODELS, entry=entry, headers={"X-Kiro-Entry-User": OWNER})
    assert await _error(named) == (401, "authentication_error", "invalid_token")
    owner = {"X-Kiro-Entry-User": "mallory", **_bearer(_session_token(OWNER))}
    assert (await gateway.send(MODELS, entry=entry, headers=owner)).status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_first", [False, True], ids=["direct-first", "entry-first"])
async def test_an_entry_request_marks_an_existing_address_pin_shared_without_rebinding(
    gateway, entry_first
) -> None:
    bearer = _bearer(_session_token(OWNER))
    assert ta.proxied_pin_observed() is None
    first = await gateway.send(MODELS, entry=entry_first, headers=bearer)
    assert first.status == 200
    assert ta.proxied_pin_observed() is entry_first
    with ta._state._lock:
        before = dict(ta._state._peer_bindings)
    again = await gateway.send(MODELS, entry=not entry_first, headers=bearer)
    assert again.status == 200
    assert ta.proxied_pin_observed() is True
    with ta._state._lock:
        assert ta._state._peer_bindings == {
            key: (peer_key, expiry, True) for key, (peer_key, expiry, _) in before.items()
        }


@pytest.mark.asyncio
async def test_an_app_credential_served_on_the_gateway_port_is_401_and_owner_403_on_the_entry_port(
    gateway, store, clock, audit  # noqa: F811 -- fixtures imported from main's tests
) -> None:
    """An app credential is taken from this machine alone, and the entry port forwards."""
    app = _bearer(_grant(store, clock)["access_token"])
    own = await gateway.send(MODELS, entry=False, headers=app)
    assert own.status == 200
    assert audit.log_api_access.call_args.kwargs["error"] == ""
    models = await gateway.send(MODELS, entry=True, headers=app)
    assert await _error(models) == (401, "authentication_error", "invalid_token")
    row = audit.log_api_access.call_args.kwargs
    assert (row["outcome"], row["error"], row["forwarded"]) == (
        "denied",
        "not-direct-local",
        True,
    )
    owner_route = await gateway.send("/v1/crew/owner/bots", entry=True, headers=app)
    assert await _error(owner_route) == (403, "permission_error", "owner_only")


@pytest.mark.asyncio
async def test_every_owner_route_is_403_on_the_entry_port(gateway) -> None:
    """Read from the registered route table, so a new Owner route is covered unlisted."""
    owner = _bearer(_session_token(OWNER))
    checked = []
    for route in gateway.client.app.router.routes():
        canonical = route.resource.canonical
        if route.method in ("HEAD", "*") or not is_owner_path(canonical):
            continue
        path = canonical.replace("{", "").replace("}", "")
        resp = await gateway.send(path, entry=True, method=route.method, headers=owner)
        assert await _error(resp) == (403, "permission_error", "owner_only"), canonical
        checked.append(canonical)
    assert len(checked) >= 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/health"),
        ("GET", "/"),
        ("GET", "/oauth/authorize"),
        ("POST", "/v1/chat/completions"),
    ],
)
async def test_a_path_outside_the_crew_api_is_404_on_the_entry_port(gateway, method, path) -> None:
    owner = _bearer(_session_token(OWNER))
    resp = await gateway.send(path, entry=True, method=method, headers=owner)
    assert await _error(resp) == (404, "not_found_error", "not_found")


@pytest.mark.asyncio
async def test_a_cookie_is_no_credential_on_the_entry_port(gateway) -> None:
    session = _session_token(OWNER)
    cookie = {
        "Cookie": f"mc_token_{gateway.client.port}={session}; mc_token_{gateway.entry_port}={session}"
    }
    assert (await gateway.send(MODELS, entry=False, headers=cookie)).status == 200
    resp = await gateway.send(MODELS, entry=True, headers=cookie)
    assert await _error(resp) == (401, "authentication_error", "invalid_token")
    assert resp.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'


@pytest_asyncio.fixture
async def mixed_gateway(monkeypatch):
    """The two ports with the gateway's own mixed internal paths in token auth."""
    from kiro_crew.dashboard.server_internal_paths import _MIXED_INTERNAL_API_PATHS

    auth = token_auth_middleware(mixed_internal_paths=_MIXED_INTERNAL_API_PATHS)
    async for served in _two_ports(monkeypatch, auth):
        yield served


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/chat/completions/x", "/v1/crew/no-such-route"])
async def test_a_path_no_route_serves_is_404_from_the_gate_on_the_entry_port(
    mixed_gateway, monkeypatch, path
) -> None:
    """A cookie or ``?token=`` never reaches token auth's cookie branches there."""
    rows = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: rows)
    session = _session_token(OWNER)
    ports = (mixed_gateway.client.port, mixed_gateway.entry_port, 5476)
    # Any bearer passes the gate's bearer check; the cookie branch would ignore it.
    cookie = {
        "Cookie": "; ".join(f"mc_token_{p}={session}" for p in ports),
        **_bearer("not-a-credential"),
    }
    resp = await mixed_gateway.send(f"{path}?token={session}", entry=True, headers=cookie)
    assert await _error(resp) == (404, "not_found_error", "not_found")
    assert rows.log_api_access.call_args.kwargs["operation"] == "entry_gate"


def test_no_registered_route_sits_under_an_internal_v1_path() -> None:
    """The gate lets a wrong method on a routed path through, so no routed path may be
    one that token auth treats as internal and authenticates by cookie or query token."""
    from kiro_crew.dashboard.crew_api_errors import _FROZEN_PATHS
    from kiro_crew.dashboard.routes import register_all
    from kiro_crew.dashboard.server_internal_paths import (
        _MIXED_INTERNAL_API_PATHS,
        _STRICT_INTERNAL_API_PATHS,
    )

    internal_v1 = {
        p for p in _MIXED_INTERNAL_API_PATHS | _STRICT_INTERNAL_API_PATHS if p.startswith("/v1/")
    }
    app = web.Application()
    register_all(app)
    routed = {route.resource.canonical for route in app.router.routes()}
    under = {p for p in routed if ta.internal_path_matches(p, internal_v1)}
    assert MODELS in routed
    assert under <= _FROZEN_PATHS, under - _FROZEN_PATHS


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), [("GET", "/v1/responses"), ("POST", MODELS)])
async def test_a_wrong_method_on_a_crew_api_route_is_405_on_both_ports(
    gateway, method, path
) -> None:
    owner = _bearer(_session_token(OWNER))
    own = await gateway.send(path, entry=False, method=method, headers=owner)
    entry = await gateway.send(path, entry=True, method=method, headers=owner)
    assert own.status == entry.status == 405
    # Without a bearer the gate's own 401 comes first.
    bare = await gateway.send(path, entry=True, method=method)
    assert await _error(bare) == (401, "authentication_error", "invalid_token")


@pytest.mark.asyncio
async def test_a_wrong_method_on_an_owner_route_is_403_on_the_entry_port(gateway) -> None:
    owner = _bearer(_session_token(OWNER))
    resp = await gateway.send("/v1/crew/owner/bots", entry=True, method="DELETE", headers=owner)
    assert await _error(resp) == (403, "permission_error", "owner_only")


@pytest.mark.asyncio
async def test_an_unregistered_owner_path_is_404_on_the_entry_port(gateway) -> None:
    owner = _bearer(_session_token(OWNER))
    resp = await gateway.send("/v1/crew/owner/nope", entry=True, headers=owner)
    assert await _error(resp) == (404, "not_found_error", "not_found")


@pytest.mark.asyncio
async def test_a_request_whose_connection_is_gone_is_refused_while_an_entry_gate_is_set(
    monkeypatch,
) -> None:
    """Its socket cannot show it did not arrive on the entry port."""
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())
    app = web.Application()
    app[entry_gate.ENTRY_GATE_APP_KEY] = entry_gate.GateRef(entry_gate.EntryGate(port=8443))
    request = make_mocked_request("GET", MODELS, app=app)
    # What aiohttp's protocol does when the peer's connection is lost.
    request.protocol.transport = None
    assert request.transport is None
    handler = MagicMock()
    resp = await entry_gate.entry_gate_middleware(request, handler)
    assert resp.status == 400
    assert json.loads(resp.body)["error"]["code"] == "invalid_request"
    handler.assert_not_called()


def _on_socket(monkeypatch, sockname):
    """A request double whose transport reports *sockname* (or raises it)."""
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())
    app = web.Application()
    app[entry_gate.ENTRY_GATE_APP_KEY] = entry_gate.GateRef(entry_gate.EntryGate(port=8443))
    request = make_mocked_request("GET", MODELS, app=app)
    transport = MagicMock()
    if isinstance(sockname, BaseException):
        transport.get_extra_info.side_effect = sockname
    else:
        transport.get_extra_info.side_effect = lambda key: sockname if key == "sockname" else None
    request.protocol.transport = transport
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("sockname", [OSError("unreadable"), None, ("127.0.0.1",)])
async def test_an_unreadable_socket_address_is_refused_while_an_entry_gate_is_set(
    monkeypatch, sockname
) -> None:
    request = _on_socket(monkeypatch, sockname)
    handler = MagicMock()
    resp = await entry_gate.entry_gate_middleware(request, handler)
    assert resp.status == 400
    assert json.loads(resp.body)["error"]["code"] == "invalid_request"
    handler.assert_not_called()
    assert ENTRY_REQUEST_KEY not in request


@pytest.mark.asyncio
@pytest.mark.parametrize("sockname", ["/run/kirocrew/dashboard.sock", ("127.0.0.1", 5476)])
async def test_a_readable_socket_off_the_entry_port_is_served_unmarked(
    monkeypatch, sockname
) -> None:
    request = _on_socket(monkeypatch, sockname)
    served = web.Response(text="served")

    async def handler(req):
        return served

    assert await entry_gate.entry_gate_middleware(request, handler) is served
    assert ENTRY_REQUEST_KEY not in request


@pytest.mark.asyncio
async def test_a_request_on_the_entry_port_socket_is_marked(monkeypatch) -> None:
    request = _on_socket(monkeypatch, ("127.0.0.1", 8443))
    await entry_gate.entry_gate_middleware(request, MagicMock())
    assert request[ENTRY_REQUEST_KEY] is True


@pytest.mark.asyncio
async def test_a_forged_bearer_is_401_on_the_entry_port(gateway) -> None:
    token = _session_token(OWNER)
    forged = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    resp = await gateway.send(MODELS, entry=True, headers=_bearer(forged))
    assert await _error(resp) == (401, "authentication_error", "invalid_token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host", ["file-explorer.apps.localhost", "example.com", "::1", "[::2]", "[::1", ""]
)
async def test_a_host_that_does_not_name_loopback_is_400_on_the_entry_port(gateway, host) -> None:
    headers = {**_bearer(_session_token(OWNER)), "Host": host}
    resp = await gateway.send(MODELS, entry=True, headers=headers)
    assert await _error(resp) == (400, "invalid_request_error", "invalid_request")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host", ["127.0.0.1", "localhost", "LOCALHOST:8443", "[::1]", "[::1]:8443"]
)
async def test_a_host_that_names_loopback_passes_on_the_entry_port(gateway, host) -> None:
    """A proxy that dials ``::1`` sends ``Host: [::1]``."""
    headers = {**_bearer(_session_token(OWNER)), "Host": host}
    assert (await gateway.send(MODELS, entry=True, headers=headers)).status == 200


@pytest.mark.asyncio
async def test_a_query_token_is_no_credential_on_the_entry_port(gateway) -> None:
    resp = await gateway.send(f"{MODELS}?token={_session_token(OWNER)}", entry=True)
    assert await _error(resp) == (401, "authentication_error", "invalid_token")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", LOCAL_CREDENTIAL_HEADERS)
@pytest.mark.parametrize("path", [MODELS, "/api/health"])
async def test_a_local_credential_header_is_400_on_the_entry_port(gateway, name, path) -> None:
    """Over the gateway's own list of local credentials, on a Crew API path and off one."""
    headers = {**_bearer(_session_token(OWNER)), name: "anything"}
    resp = await gateway.send(path, entry=True, headers=headers)
    assert await _error(resp) == (400, "invalid_request_error", "local_credential_refused")
    assert LOCAL_CREDENTIAL_HEADERS


@pytest.mark.asyncio
async def test_a_local_credential_header_is_not_refused_on_the_gateway_port(gateway) -> None:
    headers = {**_bearer(_session_token(OWNER)), LOCAL_CREDENTIAL_HEADERS[0]: "anything"}
    assert (await gateway.send(MODELS, entry=False, headers=headers)).status == 200


@pytest.mark.asyncio
async def test_a_host_naming_an_app_web_origin_reaches_no_app_backend_through_the_entry_port(
    monkeypatch,
) -> None:
    """The app web origin router sits inside the entry gate and routes by ``Host`` alone."""
    backend = _Backend()
    await backend.start()

    async def _port(name: str) -> int | None:
        return backend.port if name == "demo" else None

    monkeypatch.setattr(awo, "_backend_port", _port)
    ref = entry_gate.GateRef()
    app = web.Application(
        middlewares=[entry_gate.entry_gate_middleware, awo.build_app_web_origin_middleware(None)]
    )
    app["allowed_origins"] = set(_ALLOWED)
    app[entry_gate.ENTRY_GATE_APP_KEY] = ref
    app.router.add_route("*", "/{path:.*}", _reached)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    try:
        runner = client.server.runner
        await web.TCPSite(runner, entry_gate.BIND_ADDRESS, 0).start()
        entry_port = next(addr[1] for addr in runner.addresses if addr[1] != client.port)
        ref.gate = entry_gate.EntryGate(port=entry_port)
        sent = {**_bearer(_session_token(OWNER)), "Host": _HOST}
        own = await client.session.get(f"http://127.0.0.1:{client.port}{MODELS}", headers=sent)
        assert own.status == 200
        assert len(backend.seen) == 1, "on the gateway's own port the app's backend answers"
        entry = await client.session.get(f"http://127.0.0.1:{entry_port}{MODELS}", headers=sent)
        assert await _error(entry) == (400, "invalid_request_error", "invalid_request")
        assert len(backend.seen) == 1
    finally:
        await client.close()
        await backend.stop()


# ── the port's value and its bind ────────────────────────────────────────────


@pytest.fixture
def not_windows(monkeypatch):
    """The value rules apart from the Windows refusal; reads no OS state."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)


#: The tests below bind the entry port for real; on Windows a non-zero port is refused.
_binds = pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="the entry port is refused")


@pytest.mark.parametrize(("value", "port"), [(0, 0), (1024, 1024), (8443, 8443), (65535, 65535)])
def test_a_port_in_range_is_taken_as_written(not_windows, value, port) -> None:
    assert entry_gate.configured_port(value, 5476) == port


def test_the_ssm_port_is_refused_only_while_the_ssm_listener_runs(not_windows) -> None:
    """server.py passes the SSM port only when that listener is configured to run."""
    with pytest.raises(entry_gate.EntryPortError, match="ssm_port"):
        entry_gate.configured_port(8443, 5476, ssm_port=8443)
    assert entry_gate.configured_port(8443, 5476, ssm_port=None) == 8443
    assert entry_gate.configured_port(8443, 5476, ssm_port=9443) == 8443


@pytest.mark.parametrize("value", [1024, 8443, 65535, 80, 65536])
def test_any_non_zero_port_is_refused_on_windows(monkeypatch, value) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    with pytest.raises(entry_gate.EntryPortError, match="Linux and macOS"):
        entry_gate.configured_port(value, 5476)
    assert entry_gate.configured_port(0, 5476) == 0


@pytest.mark.parametrize("value", [-1, 1, 80, 1023, 65536, 5476, True, "8443", 8443.0, None])
def test_a_wrong_port_is_refused_not_rounded(value) -> None:
    with pytest.raises(entry_gate.EntryPortError):
        entry_gate.configured_port(value, 5476)


@pytest.mark.parametrize(
    ("stored", "read"),
    [
        ({}, 0),
        ({"entry_port": 8443}, 8443),
        ({"entry_port": "8443"}, 8443),
        ({"entry_port": 8443.0}, 8443),
        *[({"entry_port": port}, port) for port in (-1, 1, 80, 1023, 65536, 5476)],
    ],
)
def test_the_config_reads_the_port_without_clamping_it(not_windows, stored, read) -> None:
    data = validate_config_data({"dashboard": stored})
    port = _build_dashboard_config(set(), data["dashboard"]).entry_port
    assert type(port) is int
    assert port == read
    if port and (not 1024 <= port <= 65535 or port == 5476):
        with pytest.raises(entry_gate.EntryPortError):
            entry_gate.configured_port(port, 5476)
    else:
        assert entry_gate.configured_port(port, 5476) == port


@pytest.mark.parametrize("value", [8443.5, "abc", True, None])
def test_a_wrong_typed_config_port_warns_and_stays_off(value, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        data = validate_config_data({"dashboard": {"entry_port": value}})
    port = _build_dashboard_config(set(), data["dashboard"]).entry_port
    assert type(port) is int
    assert port == 0
    assert entry_gate.configured_port(port, 5476) == 0
    assert any(
        record.name == "kiro_crew.config.loader"
        and record.levelno == logging.WARNING
        and record.message.startswith("Config: type mismatch at 'dashboard.entry_port': ")
        and record.message.endswith("; using default")
        for record in caplog.records
    )


@pytest_asyncio.fixture
async def runner():
    built = web.AppRunner(web.Application())
    await built.setup()
    try:
        yield built
    finally:
        await built.cleanup()


def _has_ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


_needs_ipv6 = pytest.mark.skipif(not _has_ipv6_loopback(), reason="no IPv6 loopback here")


@contextmanager
def _reserved_loopback_sockets(*, ipv6):
    """Reserve one port on both addresses before handing either socket to aiohttp."""
    for _ in range(50):
        with ExitStack() as stack:
            v4 = stack.enter_context(server._bind_once("127.0.0.1", 0))
            port = v4.getsockname()[1]
            socks = {"127.0.0.1": v4}
            if ipv6:
                try:
                    socks["::1"] = stack.enter_context(server._bind_once("::1", port))
                except OSError as exc:
                    if exc.errno != errno.EADDRINUSE:
                        raise
                    continue
            yield socks
            return
    raise AssertionError("no port free on both loopback addresses")


@_needs_ipv6
def test_loopback_reservation_retries_an_ipv6_port_held_by_another_socket(monkeypatch) -> None:
    real = server._bind_once
    with _reserved_loopback_sockets(ipv6=True) as occupied:
        first = occupied["127.0.0.1"]
        port = first.getsockname()[1]
        asked = []

        def _bind(host, requested):
            asked.append((host, requested))
            if len(asked) == 1:
                return first
            return real(host, requested)

        monkeypatch.setattr(server, "_bind_once", _bind)
        with _reserved_loopback_sockets(ipv6=True) as socks:
            assert asked[:2] == [("127.0.0.1", 0), ("::1", port)]
            assert socks["127.0.0.1"].getsockname()[1] != port
            assert socks["::1"].getsockname()[1] == socks["127.0.0.1"].getsockname()[1]
            assert first.fileno() == -1


def test_loopback_reservation_propagates_an_unexpected_bind_error(monkeypatch) -> None:
    real = server._bind_once
    failure = OSError(errno.EACCES, "permission denied")
    opened = []

    def _bind(host, port):
        if host == "::1":
            raise failure
        sock = real(host, port)
        opened.append(sock)
        return sock

    monkeypatch.setattr(server, "_bind_once", _bind)
    with pytest.raises(OSError) as taken, _reserved_loopback_sockets(ipv6=True):
        pytest.fail("unexpected bind error was swallowed")
    assert taken.value is failure
    assert all(sock.fileno() == -1 for sock in opened)


@pytest_asyncio.fixture
async def reserved_sockets(runner):
    """Keep sockets bound; stop their sites before closing the reservations."""
    with _reserved_loopback_sockets(ipv6=_has_ipv6_loopback()) as socks:
        try:
            yield socks
        finally:
            # aiohttp must unregister the sockets before closing the reservations.
            await runner.cleanup()


def _assert_nothing_left(runner, ref, port: int) -> None:
    """No gate, no site on the runner, and the port free again on 127.0.0.1."""
    assert ref.gate is None
    assert runner.addresses == []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as again:
        again.bind(("127.0.0.1", port))


@pytest.mark.asyncio
@pytest.mark.parametrize("windows", [False, True])
async def test_off_binds_nothing(runner, monkeypatch, windows) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
    ref = entry_gate.GateRef()
    assert await entry_gate.start(ref, runner, 0, 5476) == []
    assert ref.gate is None
    assert runner.addresses == []


@pytest.mark.asyncio
async def test_a_set_port_on_windows_stops_the_start_before_any_bind(runner, monkeypatch) -> None:
    """Faked on any host: the refusal comes before the bind helper is reached."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

    def _no_bind(host, port):
        raise AssertionError("bound on Windows")

    monkeypatch.setattr(server, "_bind_once", _no_bind)
    ref = entry_gate.GateRef()
    with pytest.raises(entry_gate.EntryPortError, match="Linux and macOS"):
        await entry_gate.start(ref, runner, 8443, 5476)
    assert ref.gate is None
    assert runner.addresses == []


@_binds
@pytest.mark.asyncio
async def test_the_ssm_port_stops_the_start_and_binds_nothing(
    runner, monkeypatch, reserved_sockets
) -> None:
    port = reserved_sockets["127.0.0.1"].getsockname()[1]
    asked = _record_binds(monkeypatch, reserved_sockets)
    ref = entry_gate.GateRef()
    with pytest.raises(entry_gate.EntryPortError, match="ssm_port"):
        await entry_gate.start(ref, runner, port, 5476, ssm_port=port)
    assert asked == []
    for sock in reserved_sockets.values():
        sock.close()
    _assert_nothing_left(runner, ref, port)


@_binds
@pytest.mark.asyncio
async def test_a_port_already_in_use_stops_the_start_and_leaves_no_gate(runner) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        ref = entry_gate.GateRef()
        with pytest.raises(entry_gate.EntryPortError):
            await entry_gate.start(ref, runner, held.getsockname()[1], 5476)
    assert ref.gate is None
    assert runner.addresses == []


def _record_binds(monkeypatch, reserved_sockets) -> list[tuple[str, int]]:
    """Record bind requests and hand over sockets that have stayed bound for real."""
    asked: list[tuple[str, int]] = []

    def _bind(host, port):
        asked.append((host, port))
        sock = reserved_sockets[host]
        assert sock.getsockname()[1] == port
        return sock

    monkeypatch.setattr(server, "_bind_once", _bind)
    return asked


def test_bound_port_reads_the_gate_the_start_set() -> None:
    app = web.Application()
    assert entry_gate.bound_port(app) == 0
    ref = app[entry_gate.ENTRY_GATE_APP_KEY] = entry_gate.GateRef()
    assert entry_gate.bound_port(app) == 0
    ref.gate = entry_gate.EntryGate(port=8443)
    assert entry_gate.bound_port(app) == 8443
    assert entry_gate.bound_port(None) == 0
    assert entry_gate.bound_port(MagicMock()) == 0


def test_the_host_hint_names_every_host_the_entry_port_accepts() -> None:
    for host in ("127.0.0.1", "[::1]", "localhost"):
        assert host in entry_gate.HOST_HINT
        request = make_mocked_request("GET", MODELS, headers={"Host": f"{host}:8443"})
        assert entry_gate._names_loopback(request)
    assert len(entry_gate._LOOPBACK_HOSTS) == 3


@_binds
@pytest.mark.asyncio
async def test_the_log_line_of_an_open_port_carries_the_host_hint(
    runner, monkeypatch, reserved_sockets, caplog
) -> None:
    _record_binds(monkeypatch, reserved_sockets)

    async def _no_ipv6_loopback(bind_once, port):
        return None

    # One listener is enough for the log line, on a machine with or without ``::1``.
    monkeypatch.setattr(entry_gate, "_bind_ipv6_loopback", _no_ipv6_loopback)
    port = reserved_sockets["127.0.0.1"].getsockname()[1]
    app = web.Application()
    ref = app[entry_gate.ENTRY_GATE_APP_KEY] = entry_gate.GateRef()
    with caplog.at_level(logging.INFO, logger="kiro_crew.dashboard.entry_gate"):
        await entry_gate.start(ref, runner, port, 5476)
    assert entry_gate.bound_port(app) == port
    opened = [r.getMessage() for r in caplog.records if r.getMessage().startswith("entry port")]
    assert len(opened) == 1
    assert opened[0] == f"entry port {port} on 127.0.0.1 (Crew API only); {entry_gate.HOST_HINT}"


@_binds
@_needs_ipv6
@pytest.mark.asyncio
async def test_both_loopback_addresses_are_bound(runner, monkeypatch, reserved_sockets) -> None:
    asked = _record_binds(monkeypatch, reserved_sockets)
    port = reserved_sockets["127.0.0.1"].getsockname()[1]
    ref = entry_gate.GateRef()
    sites = await entry_gate.start(ref, runner, port, 5476)
    assert len(sites) == 2
    assert asked == [("127.0.0.1", port), ("::1", port)]
    assert sorted((addr[0], addr[1]) for addr in runner.addresses) == [
        ("127.0.0.1", port),
        ("::1", port),
    ]
    assert ref.gate == entry_gate.EntryGate(port=port)
    with (
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as other,
        pytest.raises(OSError) as taken,
    ):
        other.bind(("::1", port))
    assert taken.value.errno == errno.EADDRINUSE


@_binds
@_needs_ipv6
@pytest.mark.asyncio
async def test_ipv6_loopback_already_held_stops_the_start_and_binds_nothing(
    runner, monkeypatch, reserved_sockets
) -> None:
    port = reserved_sockets["127.0.0.1"].getsockname()[1]
    real = server._bind_once

    def _bind(host, port_):
        if host == "::1":
            # The fixture still holds ::1, so the real bind must fail with EADDRINUSE.
            return real(host, port_)
        assert host == "127.0.0.1" and port_ == port
        return reserved_sockets[host]

    monkeypatch.setattr(server, "_bind_once", _bind)
    ref = entry_gate.GateRef()
    with pytest.raises(entry_gate.EntryPortError) as taken:
        await entry_gate.start(ref, runner, port, 5476)
    assert taken.value.__cause__.errno == errno.EADDRINUSE
    _assert_nothing_left(runner, ref, port)


def _ipv6_bind_raises(monkeypatch, exc: OSError) -> None:
    real = server._bind_once

    def _bind(host, port):
        if host == "::1":
            raise exc
        return real(host, port)

    monkeypatch.setattr(server, "_bind_once", _bind)


@_binds
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address"),
        OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol"),
        socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
    ],
    ids=["EADDRNOTAVAIL", "EAFNOSUPPORT", "gaierror"],
)
async def test_without_ipv6_loopback_the_port_starts_on_127_0_0_1(
    runner, monkeypatch, exc, reserved_sockets
) -> None:
    _record_binds(monkeypatch, reserved_sockets)
    _ipv6_bind_raises(monkeypatch, exc)
    port = reserved_sockets["127.0.0.1"].getsockname()[1]
    ref = entry_gate.GateRef()
    sites = await entry_gate.start(ref, runner, port, 5476)
    assert len(sites) == 1
    assert [(addr[0], addr[1]) for addr in runner.addresses] == [("127.0.0.1", port)]
    assert ref.gate == entry_gate.EntryGate(port=port)


@_binds
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", [errno.EADDRINUSE, errno.EACCES, errno.EINVAL], ids=["EADDRINUSE", "EACCES", "EINVAL"]
)
async def test_any_other_ipv6_bind_error_stops_the_start_and_binds_nothing(
    runner, monkeypatch, code, reserved_sockets
) -> None:
    """Fail closed: only a missing IPv6 loopback lets the start go on without ``::1``."""
    _record_binds(monkeypatch, reserved_sockets)
    _ipv6_bind_raises(monkeypatch, OSError(code, "refused"))
    port = reserved_sockets["127.0.0.1"].getsockname()[1]
    ref = entry_gate.GateRef()
    with pytest.raises(entry_gate.EntryPortError):
        await entry_gate.start(ref, runner, port, 5476)
    _assert_nothing_left(runner, ref, port)


@_binds
@pytest.mark.asyncio
async def test_a_site_that_fails_to_start_leaves_nothing_bound(
    runner, monkeypatch, reserved_sockets
) -> None:
    port = reserved_sockets["127.0.0.1"].getsockname()[1]
    real_start = web.SockSite.start
    started: list = []

    async def _start(self):
        if started:
            raise OSError(errno.EINVAL, "second site refused")
        started.append(self)
        await real_start(self)

    monkeypatch.setattr(web.SockSite, "start", _start)
    real = server._bind_once
    socks: list[socket.socket] = []

    def _two_v4(host, port_):
        # Two IPv4 sockets on distinct ports stand in for the two loopback addresses.
        if not socks:
            assert host == "127.0.0.1" and port_ == port
            sock = reserved_sockets[host]
        else:
            sock = real("127.0.0.1", 0)
        socks.append(sock)
        return sock

    monkeypatch.setattr(server, "_bind_once", _two_v4)
    ref = entry_gate.GateRef()
    with pytest.raises(entry_gate.EntryPortError):
        await entry_gate.start(ref, runner, port, 5476)
    _assert_nothing_left(runner, ref, port)
    assert all(sock.fileno() == -1 for sock in socks)


def test_the_gate_owns_a_request_on_either_loopback_address() -> None:
    gate = entry_gate.EntryGate(port=8443)

    def _on(sockname):
        return SimpleNamespace(
            transport=SimpleNamespace(
                get_extra_info=lambda key: sockname if key == "sockname" else None
            )
        )

    assert gate.owns(_on(("127.0.0.1", 8443)))
    assert gate.owns(_on(("::1", 8443, 0, 0)))
    assert not gate.owns(_on(("::1", 5476, 0, 0)))
    assert not gate.owns(_on(("192.0.2.1", 8443)))


@pytest_asyncio.fixture
async def ipv6_gateway(monkeypatch):
    async for served in _two_ports(monkeypatch, token_auth_middleware(), ipv6=True):
        yield served


async def _send_over_ipv6(gateway: _Gateway, port: int, headers: dict[str, str]):
    return await gateway.client.session.get(f"http://[{IPV6}]:{port}{MODELS}", headers=headers)


IPV6 = entry_gate.IPV6_BIND_ADDRESS


@_needs_ipv6
@pytest.mark.asyncio
async def test_a_bearer_pinned_through_the_entry_port_is_served_over_either_loopback_address(
    ipv6_gateway,
) -> None:
    """A proxy that dials ``localhost`` reaches the entry port over both addresses."""
    gateway = ipv6_gateway
    bearer = _bearer(_session_token(OWNER))
    first = await gateway.send(MODELS, entry=True, headers=bearer)
    assert first.status == 200
    again = await _send_over_ipv6(gateway, gateway.entry_port, bearer)
    assert again.status == 200


@_needs_ipv6
@pytest.mark.asyncio
async def test_a_bearer_pinned_on_the_gateway_port_is_refused_from_the_other_loopback_address(
    ipv6_gateway,
) -> None:
    """Off the entry port the pin stays the caller's own address."""
    gateway = ipv6_gateway
    bearer = _bearer(_session_token(OWNER))
    first = await gateway.send(MODELS, entry=False, headers=bearer)
    assert first.status == 200
    again = await _send_over_ipv6(gateway, gateway.client.port, bearer)
    assert again.status == 401
    assert (await again.json())["error"]["message"].startswith("IP mismatch")

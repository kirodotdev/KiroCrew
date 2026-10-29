"""The direct tailnet listener admits only people the daemon names on the allowlist.

These tests drive the real auth middleware with a :class:`DirectGate`. The
socket addresses of a connection are what decide that a request is on the
direct listener, so the test server's loopback sockets are relabelled through
``tailnet_direct._sockaddr``; the daemon's ``whois`` is a fake. Everything else
(the gate, the allowlist rule, the token flow it falls back to) is real.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew.config.sections_shared_crew import SharedCrewConfig, shared_crew_config_from
from kiro_crew.dashboard import tailnet_direct, tailnet_localapi
from kiro_crew.dashboard.origin import check_origin
from kiro_crew.dashboard.tailnet import ForwardedPeer
from kiro_crew.dashboard.tailnet_direct import (
    DirectGate,
    SelfNode,
    admission_refusal,
    parse_status,
    parse_whois,
)
from kiro_crew.dashboard.tailnet_localapi import Answer
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.shared_crew import access, capabilities, people, wiring
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, PrincipalKind, Provenance
from kiro_crew.shared_crew.identity import direct_principal

OWNER_IP = "100.64.0.1"
PORT = 5476
KEY_A = "nodekey:" + "a" * 64
KEY_B = "nodekey:" + "b" * 64
KEY_C = "nodekey:" + "c" * 64
KEY_T = "nodekey:" + "d" * 64

OWNER = SelfNode(
    addresses=(OWNER_IP, "fd7a:115c:a1e0::1"),
    login="owner@example.com",
    user_id="11",
    dns_name="crew.tail1234.ts.net",
)


def _whois_body(
    login: str,
    uid: int,
    name: str,
    key: str,
    *,
    display: str = "",
    node: dict[str, Any] | None = None,
) -> dict[str, Any]:
    node_body: dict[str, Any] = {"Name": f"{name}.tail1234.ts.net.", "Key": key, **(node or {})}
    return {
        "Node": node_body,
        "UserProfile": {"ID": uid, "LoginName": login, "DisplayName": display},
    }


PEERS: dict[str, dict[str, Any]] = {
    "100.64.0.3": _whois_body(
        "owner@example.com", 11, "owner-phone", KEY_A, display="Owner Person"
    ),
    "100.64.0.5": _whois_body("alice@example.com", 21, "alice-laptop", KEY_B, display="Alice A"),
    "100.64.0.7": _whois_body("mallory@example.com", 31, "mallory", KEY_C),
    "100.64.0.9": {
        "Node": {"Name": "ci-runner.tail1234.ts.net.", "Key": KEY_T, "Tags": ["tag:ci"]},
        "UserProfile": {"ID": 99, "LoginName": "tagged-devices"},
    },
}


class FakeWhois:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    def __call__(self, addr: str) -> Answer:
        self.calls.append(addr)
        if self.fail:
            return Answer(None, tailnet_localapi.METHOD_NONE)
        ip = addr.rsplit(":", 1)[0].strip("[]")
        body = PEERS.get(ip)
        return Answer(body, tailnet_localapi.METHOD_LOCALAPI_UNIX) if body else Answer(None, "none")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    import kiro_crew.dashboard.revocation_gen as rg
    import kiro_crew.dashboard.token_auth as ta

    monkeypatch = _floor_monkeypatch
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(rg, "_gen", 0)
    monkeypatch.setattr(ta, "_revoked_store_singleton", None)
    ta._state.clear_all()
    people.reset_for_tests()

    real = tailnet_direct._sockaddr

    def relabelled(request: web.Request, name: str) -> tuple[str, int] | None:
        header = "X-Test-Local" if name == "sockname" else "X-Test-Peer"
        raw = request.headers.get(header)
        if raw is None:
            return real(request, name)
        host, _, port = raw.rpartition(":")
        return host.strip("[]"), int(port)

    monkeypatch.setattr(tailnet_direct, "_sockaddr", relabelled)
    yield
    ta._state.clear_all()
    people.reset_for_tests()


async def _whoami(request: web.Request) -> web.Response:
    actor = request.get(ACTOR_REQUEST_KEY)
    p = actor.principal if actor is not None else None
    return web.json_response(
        {
            "user": request.get("user"),
            "id": p.id if p else None,
            "login": p.display.login if p else None,
            "name": p.display.name if p else None,
            "kind": p.kind.value if p else None,
            "provenance": p.provenance.value if p else None,
        }
    )


def _gate(allowed: tuple[str, ...] = (), whois: FakeWhois | None = None, **kw: Any) -> DirectGate:
    return DirectGate(
        owner=OWNER, port=PORT, allowed_logins=allowed, whois_fn=whois or FakeWhois(), **kw
    )


async def _client(gate: DirectGate | None) -> TestClient:
    app = web.Application(
        middlewares=[
            token_auth_middleware(
                tailnet_direct=gate,
                internal_paths=frozenset({"/api/internal/x"}),
            )
        ]
    )
    app.router.add_get("/api/whoami", _whoami)
    app.router.add_get("/api/internal/x", _whoami)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    return client


def _direct(peer_ip: str, port: int = 50123, **extra: str) -> dict[str, str]:
    return {"X-Test-Local": f"{OWNER_IP}:{PORT}", "X-Test-Peer": f"{peer_ip}:{port}", **extra}


async def _get(
    client: TestClient, headers: dict[str, str], path: str = "/api/whoami"
) -> tuple[int, Any]:
    resp = await client.get(path, headers=headers)
    text = await resp.text()
    try:
        return resp.status, json.loads(text)
    except ValueError:
        return resp.status, text


# -- whois and status parsing ------------------------------------------------- #


def test_whois_names_the_owner_by_stable_user_id_and_never_the_sharer():
    body = _whois_body("alice@example.com", 21, "alice-laptop", KEY_B, node={"Sharer": 77})
    peer = parse_whois(body)
    assert peer is not None
    assert (peer.user_id, peer.login) == ("21", "alice@example.com")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["UserProfile"].pop("ID"),
        lambda b: b["UserProfile"].update(ID=0),
        lambda b: b["Node"].update(Key="not-a-key"),
        lambda b: b["UserProfile"].update(LoginName="bad login|x"),
        lambda b: b.pop("Node"),
    ],
    ids=["no-id", "zero-id", "bad-key", "bad-login", "no-node"],
)
def test_whois_without_a_stable_identity_names_nobody(mutate):
    body = _whois_body("alice@example.com", 21, "alice-laptop", KEY_B)
    mutate(body)
    assert parse_whois(body) is None


def test_status_keeps_only_tailnet_addresses_and_the_owner_login():
    status = {
        "BackendState": "Running",
        "Self": {
            "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1", "192.168.1.5", "0.0.0.0"],
            "UserID": 11,
            "DNSName": "crew.tail1234.ts.net.",
        },
        "User": {"11": {"LoginName": "owner@example.com"}},
        "CurrentTailnet": {"MagicDNSSuffix": "tail1234.ts.net"},
    }
    node = parse_status(status)
    assert node == SelfNode(
        addresses=("100.64.0.1", "fd7a:115c:a1e0::1"),
        login="owner@example.com",
        user_id="11",
        dns_name="crew.tail1234.ts.net",
    )
    assert parse_status({**status, "BackendState": "Stopped"}) is None


# -- the admission rule ------------------------------------------------------ #


def _peer(ip: str):
    peer = parse_whois(PEERS[ip])
    assert peer is not None
    return peer


def test_empty_allowlist_admits_only_the_owner():
    assert admission_refusal(_peer("100.64.0.3"), allowed_logins=(), owner=OWNER) is None
    assert (
        admission_refusal(_peer("100.64.0.5"), allowed_logins=(), owner=OWNER)
        == "login_not_allowed"
    )


def test_owner_needs_both_the_stable_id_and_the_login():
    impostor = parse_whois(_whois_body("owner@example.com", 12, "x", KEY_C))
    assert impostor is not None
    assert admission_refusal(impostor, allowed_logins=(), owner=OWNER) == "login_not_allowed"


def test_allowlisted_login_is_admitted_case_insensitively():
    assert (
        admission_refusal(_peer("100.64.0.5"), allowed_logins=("Alice@Example.com",), owner=OWNER)
        is None
    )
    assert (
        admission_refusal(_peer("100.64.0.7"), allowed_logins=("alice@example.com",), owner=OWNER)
        == "login_not_allowed"
    )


def test_tagged_node_is_refused_unless_its_tag_is_listed():
    tagged = _peer("100.64.0.9")
    assert (
        admission_refusal(tagged, allowed_logins=("alice@example.com",), owner=OWNER)
        == "tagged_node_not_allowed"
    )
    assert (
        admission_refusal(tagged, allowed_logins=("tagged-devices",), owner=OWNER)
        == "tagged_node_not_allowed"
    )
    assert admission_refusal(tagged, allowed_logins=("tag:ci",), owner=OWNER) is None


def test_unreadable_allowlist_admits_nobody_not_even_the_owner():
    assert (
        admission_refusal(
            _peer("100.64.0.3"), allowed_logins=(), owner=OWNER, identity_unknown=True
        )
        == "allowlist_unreadable"
    )


# -- the principal ----------------------------------------------------------- #


def test_one_user_on_two_nodes_is_one_principal_named_by_display_name():
    laptop = _peer("100.64.0.5")
    phone = parse_whois(_whois_body("alice@example.com", 21, "alice-phone", KEY_C))
    assert phone is not None
    a, b = direct_principal(laptop), direct_principal(phone)
    assert a.id == b.id
    assert a.subject == "alice@example.com"
    assert a.provenance is Provenance.TAILSCALE_LISTENER
    assert a.display.name == "Alice A" and a.display.login == "alice@example.com"
    renamed = parse_whois(
        _whois_body("alice@example.com", 21, "alice-laptop", KEY_B, display="Other")
    )
    assert renamed is not None and direct_principal(renamed).id == a.id


def test_serve_and_the_listener_give_one_person_one_principal_and_budget():
    from kiro_crew.shared_crew.access_set import principal_ids_for
    from kiro_crew.shared_crew.identity import derive_principal

    serve = derive_principal(
        internal=False,
        app="",
        token_subject="",
        serve_peer=ForwardedPeer(login="Alice@Example.com", node="alice-laptop", address="x"),
        direct_peer=None,
        remote="127.0.0.1",
        forwarded=True,
    )
    listener = direct_principal(_peer("100.64.0.5"))
    assert serve.id == listener.id
    assert principal_ids_for("alice@example.com", frozenset({"21"})) == frozenset({serve.id})


def test_tagged_node_is_a_machine_keyed_by_node():
    principal = direct_principal(_peer("100.64.0.9"))
    assert principal.kind is PrincipalKind.MACHINE
    assert principal.subject == "node:ci-runner.tail1234.ts.net"


# -- the middleware ---------------------------------------------------------- #


@pytest.mark.asyncio
async def test_owner_is_admitted_without_a_token_with_owner_rights():
    client = await _client(_gate())
    try:
        status, body = await _get(client, _direct("100.64.0.3"))
    finally:
        await client.close()
    assert status == 200
    # whois proved the owner, so the request carries the owner's dashboard subject.
    assert body["user"] == "local-app"
    assert (body["login"], body["name"], body["provenance"]) == (
        "owner@example.com",
        "Owner Person",
        "tailscale_listener",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("own_ip", [OWNER_IP, "fd7a:115c:a1e0::1"])
async def test_a_local_process_dialling_its_own_tailnet_address_is_refused(
    own_ip: str, monkeypatch: pytest.MonkeyPatch
):
    """whois of the machine's own address names the owner, so it is never asked."""
    monkeypatch.setitem(PEERS, own_ip, PEERS["100.64.0.3"])
    whois = FakeWhois()
    client = await _client(_gate(whois=whois))
    try:
        status, body = await _get(client, _direct(own_ip))
    finally:
        await client.close()
    assert status == 403
    assert body["reason"] == tailnet_direct.REFUSE_SELF_CONNECTION
    assert whois.calls == []


@pytest.mark.asyncio
async def test_empty_allowlist_refuses_a_teammate():
    client = await _client(_gate())
    try:
        status, body = await _get(client, _direct("100.64.0.5"))
    finally:
        await client.close()
    assert status == 403
    assert body["code"] == "tailnet_direct_refused" and body["reason"] == "login_not_allowed"


@pytest.mark.asyncio
async def test_non_allowlisted_login_is_refused_and_allowlisted_admitted():
    client = await _client(_gate(("alice@example.com",)))
    try:
        ok_status, ok_body = await _get(client, _direct("100.64.0.5"))
        bad_status, bad_body = await _get(client, _direct("100.64.0.7"))
    finally:
        await client.close()
    assert ok_status == 200 and ok_body["login"] == "alice@example.com"
    assert bad_status == 403 and bad_body["reason"] == "login_not_allowed"


@pytest.mark.asyncio
async def test_the_chain_reads_a_gate_filled_in_after_it_was_built():
    """The gateway fills the holder after the bind; the chain reads it per request."""
    ref = tailnet_direct.GateRef()
    client = await _client(ref)  # type: ignore[arg-type]
    try:
        before, _ = await _get(client, _direct("100.64.0.5"))
        ref.gate = _gate(("alice@example.com",))
        after, body = await _get(client, _direct("100.64.0.5"))
    finally:
        await client.close()
    # Empty, the request runs the token flow, which has no token to accept.
    assert before in (401, 403)
    assert after == 200 and body["login"] == "alice@example.com"


@pytest.mark.asyncio
async def test_tagged_node_is_refused():
    client = await _client(_gate(("alice@example.com",)))
    try:
        status, body = await _get(client, _direct("100.64.0.9"))
    finally:
        await client.close()
    assert status == 403 and body["reason"] == "tagged_node_not_allowed"


@pytest.mark.asyncio
async def test_forged_tailscale_user_headers_are_ignored():
    client = await _client(_gate(("alice@example.com",)))
    forged = {
        "Tailscale-User-Login": "owner@example.com",
        "Tailscale-User-Name": "Owner Person",
    }
    try:
        status, body = await _get(client, _direct("100.64.0.5", **forged))
        refused, refused_body = await _get(client, _direct("100.64.0.7", **forged))
    finally:
        await client.close()
    assert status == 200 and body["login"] == "alice@example.com"
    assert refused == 403 and refused_body["reason"] == "login_not_allowed"


@pytest.mark.asyncio
async def test_a_forged_relay_header_never_reaches_a_direct_listener_handler():
    """The direct listener serves the dashboard chain, whose relay-header strip runs first."""
    from kiro_crew.dashboard.relay_headers import is_relay_header, relay_header_strip_middleware

    seen: list[list[str]] = []

    async def relay_names(request: web.Request) -> web.Response:
        seen.append([name for name in request.headers if is_relay_header(name)])
        return await _whoami(request)

    app = web.Application(
        middlewares=[
            relay_header_strip_middleware,
            token_auth_middleware(tailnet_direct=_gate(("alice@example.com",))),
        ]
    )
    app.router.add_get("/api/whoami", relay_names)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    forged = {"X-Kiro-Relay-User": "owner@example.com", "x-kiro-relay-foo": "x"}
    try:
        status, body = await _get(client, _direct("100.64.0.5", **forged))
    finally:
        await client.close()
    assert status == 200 and body["login"] == "alice@example.com"
    assert seen == [[]]


@pytest.mark.asyncio
async def test_direct_sites_serve_the_runner_they_are_given(monkeypatch):
    """Every direct site serves the dashboard's own runner, so its app and its chain."""
    runners: list[Any] = []

    class FakeSite:
        def __init__(self, runner: Any, host: str, port: int, **_kw: Any) -> None:
            runners.append(runner)
            self.name = f"http://{host}:{port}"

        async def start(self) -> None:
            return None

    monkeypatch.setattr(web, "TCPSite", FakeSite)
    runner = object()
    sites = await tailnet_direct.start_sites(runner, PORT, _gate())  # type: ignore[arg-type]
    assert len(sites) == 2
    assert runners == [runner, runner]


@pytest.mark.asyncio
@pytest.mark.parametrize(("os_name", "reuse"), [("posix", True), ("nt", False)])
async def test_direct_sites_rebind_through_time_wait_on_posix_only(monkeypatch, os_name, reuse):
    """A restart inside TIME_WAIT still binds on POSIX; Windows never shares a live port."""
    reuses: list[Any] = []

    class FakeSite:
        def __init__(self, _runner: Any, host: str, port: int, **kw: Any) -> None:
            reuses.append(kw.get("reuse_address"))
            self.name = f"http://{host}:{port}"

        async def start(self) -> None:
            return None

    monkeypatch.setattr(web, "TCPSite", FakeSite)
    monkeypatch.setattr(tailnet_direct.os, "name", os_name)
    await tailnet_direct.start_sites(object(), PORT, _gate())  # type: ignore[arg-type]
    assert reuses == [reuse, reuse]


@pytest.mark.asyncio
async def test_whois_error_rejects_and_never_admits_unattributed():
    whois = FakeWhois()
    whois.fail = True
    client = await _client(_gate(("alice@example.com",), whois))
    try:
        status, body = await _get(client, _direct("100.64.0.5"))
    finally:
        await client.close()
    assert status == 403 and body["reason"] == "whois_failed"


@pytest.mark.asyncio
async def test_a_forwarded_header_on_the_direct_listener_is_refused():
    client = await _client(_gate(("alice@example.com",)))
    try:
        status, body = await _get(
            client, _direct("100.64.0.5", **{"X-Forwarded-For": "100.64.0.1"})
        )
    finally:
        await client.close()
    assert status == 403 and body["reason"] == "forwarded_header"


@pytest.mark.asyncio
async def test_a_peer_outside_the_tailnet_ranges_is_refused():
    client = await _client(_gate(("alice@example.com",)))
    try:
        status, body = await _get(client, _direct("192.168.1.20"))
    finally:
        await client.close()
    assert status == 403 and body["reason"] == "not_tailnet_peer"


@pytest.mark.asyncio
async def test_strict_internal_paths_stay_loopback_only():
    client = await _client(_gate())
    try:
        status, _ = await _get(client, _direct("100.64.0.3"), "/api/internal/x")
    finally:
        await client.close()
    assert status == 403


@pytest.mark.asyncio
async def test_browser_refusal_is_a_readable_page():
    client = await _client(_gate())
    try:
        status, body = await _get(client, {**_direct("100.64.0.5"), "Accept": "text/html"})
    finally:
        await client.close()
    assert status == 403
    assert "invited or approved" in body and "login_not_allowed" in body


@pytest.mark.asyncio
async def test_loopback_without_a_token_is_still_refused():
    client = await _client(_gate())
    try:
        status, body = await _get(client, {})
    finally:
        await client.close()
    assert status == 403 and body["error"] == "Token required"


@pytest.mark.asyncio
async def test_serve_path_is_unchanged_forwarded_loopback_still_needs_a_token():
    client = await _client(_gate())
    try:
        status, body = await _get(
            client,
            {"X-Forwarded-For": "100.64.0.1", "Tailscale-User-Login": "owner@example.com"},
        )
    finally:
        await client.close()
    assert status == 403 and body["error"] == "Token required"


@pytest.mark.asyncio
async def test_no_gate_means_no_direct_path():
    client = await _client(None)
    try:
        status, body = await _get(client, _direct("100.64.0.3"))
    finally:
        await client.close()
    assert status == 403 and body["error"] == "Token required"


@pytest.mark.asyncio
async def test_whois_runs_once_per_connection_within_the_ttl():
    whois = FakeWhois()
    client = await _client(_gate(whois=whois))
    try:
        for _ in range(3):
            status, _ = await _get(client, _direct("100.64.0.3", port=40000))
            assert status == 200
        await _get(client, _direct("100.64.0.3", port=40001))
    finally:
        await client.close()
    assert whois.calls == ["100.64.0.3:40000", "100.64.0.3:40001"]


def test_a_different_node_key_on_the_same_connection_is_refused():
    whois = FakeWhois()
    clock = [0.0]
    gate = _gate(("alice@example.com",), whois, clock=lambda: clock[0])
    peer, reason = gate._lookup("100.64.0.5", 40000, fresh=False)
    assert peer is not None and reason == ""
    PEERS["100.64.0.5"]["Node"]["Key"] = KEY_T
    try:
        clock[0] = 100.0
        peer, reason = gate._lookup("100.64.0.5", 40000, fresh=False)
    finally:
        PEERS["100.64.0.5"]["Node"]["Key"] = KEY_B
    assert peer is None and reason == "node_changed"


def test_whois_cache_is_bounded():
    gate = _gate(whois=FakeWhois())
    for port in range(tailnet_direct.WHOIS_CACHE_MAX_ENTRIES + 20):
        gate._lookup("100.64.0.3", port, fresh=False)
    assert len(gate._cache) == tailnet_direct.WHOIS_CACHE_MAX_ENTRIES


@pytest.mark.asyncio
async def test_open_websocket_is_closed_when_the_peer_stops_verifying():
    whois = FakeWhois()
    gate = _gate(whois=whois)
    request = make_mocked_request("GET", "/ws", headers=_direct("100.64.0.3"))
    closed = asyncio.Event()
    request.transport.close.side_effect = lambda: closed.set()  # type: ignore[union-attr]
    peer, reason = await gate.verify(request)
    assert peer is not None and reason == ""

    async def handler(_req: web.Request) -> str:
        whois.fail = True
        await asyncio.wait_for(closed.wait(), 30)
        return "closed"

    assert (
        await tailnet_direct.run_with_reverify(request, gate, peer, handler, interval=0.01)
        == "closed"
    )


@pytest.mark.asyncio
async def test_an_event_stream_on_the_direct_listener_is_reverified_too(monkeypatch):
    """A removed person's SSE stream is closed like their WebSocket."""
    wrapped: list[str] = []
    real = tailnet_direct.run_with_reverify

    async def spy(request: web.Request, *a: Any, **kw: Any) -> Any:
        wrapped.append(request.path)
        return await real(request, *a, **kw)

    monkeypatch.setattr("kiro_crew.dashboard.token_auth.run_with_reverify", spy)
    client = await _client(_gate(("alice@example.com",)))
    try:
        status, _ = await _get(client, _direct("100.64.0.5"))
    finally:
        await client.close()
    assert status == 200
    assert wrapped == ["/api/whoami"]


@pytest.mark.asyncio
async def test_direct_session_cannot_mint_a_bearer_link():
    from kiro_crew.dashboard.handlers._shared import _caller_bounds

    request = make_mocked_request("POST", "/api/auth/mobile-link")
    request["auth_tailnet_direct"] = True
    request["auth_token"] = ""
    assert _caller_bounds(request)[1] == 0


# -- binding ----------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sites_bind_only_tailnet_addresses(monkeypatch):
    bound: list[tuple[str, int]] = []

    class FakeSite:
        def __init__(self, runner: Any, host: str, port: int, **_kw: Any) -> None:
            self.host, self.port = host, port
            self.name = f"http://{host}:{port}"

        async def start(self) -> None:
            bound.append((self.host, self.port))

    monkeypatch.setattr(web, "TCPSite", FakeSite)
    owner = SelfNode(
        addresses=("100.64.0.1", "fd7a:115c:a1e0::1", "0.0.0.0", "192.168.1.5", "::"),
        login="owner@example.com",
        user_id="11",
    )
    gate = DirectGate(owner=owner, port=PORT)
    sites = await tailnet_direct.start_sites(object(), PORT, gate)  # type: ignore[arg-type]
    assert bound == [("100.64.0.1", PORT), ("fd7a:115c:a1e0::1", PORT)]
    assert len(sites) == 2


def _status_answer() -> Answer:
    return Answer(
        {
            "BackendState": "Running",
            "Self": {"TailscaleIPs": [OWNER_IP], "UserID": 11, "DNSName": ""},
            "User": {"11": {"LoginName": "owner@example.com"}},
        },
        tailnet_localapi.METHOD_LOCALAPI_UNIX,
    )


def test_resolve_reads_the_daemon_once_and_returns_an_active_gate():
    calls: list[int] = []

    def status() -> Answer:
        calls.append(1)
        return _status_answer()

    on = tailnet_direct.resolve(
        port=PORT,
        allowed_logins=(),
        identity_unknown=False,
        governance_pinned=False,
        status_fn=status,
    )
    assert on.state == "active" and on.gate is not None and on.method == "localapi_unix"
    assert calls == [1]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (Answer(None, "none"), "tailscale_not_running"),
        (
            Answer({"BackendState": "Running", "Self": {"TailscaleIPs": []}}, "cli"),
            "no_tailnet_address",
        ),
        (
            Answer(
                {"BackendState": "Running", "Self": {"TailscaleIPs": [OWNER_IP], "UserID": 11}},
                "cli",
            ),
            "owner_login_unknown",
        ),
    ],
)
def test_unavailable_daemon_reports_a_reason(answer, reason):
    out = tailnet_direct.resolve(
        port=PORT,
        allowed_logins=(),
        identity_unknown=False,
        governance_pinned=False,
        status_fn=lambda: answer,
    )
    assert (out.state, out.reason, out.gate) == ("unavailable", reason, None)


@pytest.mark.asyncio
async def test_turning_the_flag_off_binds_nothing(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard import tailnet_direct_listener

    started: list[str] = []

    async def fake_start_sites(*_a: Any, **_k: Any) -> list[Any]:
        started.append("bound")
        return []

    monkeypatch.setattr(tailnet_direct, "start_sites", fake_start_sites)
    monkeypatch.setattr(tailnet_direct.tailnet_localapi, "status", _status_answer)
    cfg = KiroCrewConfig()
    cfg.dashboard.shared_crew.tailnet_direct = False
    resolution = await tailnet_direct_listener.resolve(cfg, PORT, True)
    assert resolution.state == "off" and resolution.gate is None
    app = web.Application()
    app["allowed_origins"] = {"http://127.0.0.1:5476"}
    assert await tailnet_direct_listener.start(app, object(), PORT, resolution) == []  # type: ignore[arg-type]
    assert started == []
    assert app["allowed_origins"] == {"http://127.0.0.1:5476"}
    assert access.tailnet_direct().state == "off"


@pytest.mark.asyncio
async def test_flag_off_still_publishes_the_owner_so_phone_setup_admits_them(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard import tailnet, tailnet_direct_listener

    monkeypatch.setattr(tailnet_direct.tailnet_localapi, "status", _status_answer)
    cfg = KiroCrewConfig()
    cfg.dashboard.shared_crew.tailnet_direct = False
    cfg.dashboard.tailscale.enabled = True
    resolution = await tailnet_direct_listener.resolve(cfg, PORT, True)
    await tailnet_direct_listener.start(web.Application(), object(), PORT, resolution)  # type: ignore[arg-type]
    assert tailnet.login_allowed(OWNER.login, ())


@pytest.mark.asyncio
async def test_a_widened_bind_does_not_start_the_direct_listener():
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard import tailnet_direct_listener

    cfg = KiroCrewConfig()
    cfg = dataclasses.replace(
        cfg,
        dashboard=dataclasses.replace(
            cfg.dashboard,
            shared_crew=dataclasses.replace(cfg.dashboard.shared_crew, tailnet_direct=True),
        ),
    )
    resolution = await tailnet_direct_listener.resolve(cfg, PORT, False)
    assert (resolution.state, resolution.reason) == ("unavailable", "bind_not_loopback")


@pytest.mark.asyncio
async def test_the_default_config_binds_no_direct_listener():
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard import tailnet_direct_listener

    resolution = await tailnet_direct_listener.resolve(KiroCrewConfig(), PORT, True)
    assert resolution.gate is None
    assert resolution.state != tailnet_direct.STATE_ACTIVE


# -- CSRF / origin ------------------------------------------------------------ #


def test_direct_origins_pass_csrf_and_foreign_origins_do_not():
    gate = _gate()
    app = web.Application()
    app["allowed_origins"] = {"http://127.0.0.1:5476"} | tailnet_direct.direct_origins(gate, PORT)

    def origin_ok(origin: str) -> bool:
        req = make_mocked_request("POST", "/api/x", headers={"Origin": origin}, app=app)
        return check_origin(req)

    assert origin_ok("http://crew.tail1234.ts.net:5476")
    assert origin_ok("http://100.64.0.1:5476")
    assert origin_ok("http://[fd7a:115c:a1e0::1]:5476")
    assert not origin_ok("http://evil.example:5476")
    assert not origin_ok("http://crew.tail1234.ts.net:9999")


def test_share_urls_carry_no_token():
    urls = tailnet_direct.direct_urls(_gate(), PORT)
    assert urls[0] == "http://crew.tail1234.ts.net:5476/"
    assert all("token" not in u for u in urls)


# -- config ------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, False),
        ({"tailnet_direct": True}, True),
        ({"tailnet_direct": False}, False),
        ({"tailnet_direct": "true"}, False),
        ({"tailnet_direct": 1}, False),
    ],
)
def test_config_default_is_off_and_a_non_boolean_reads_off(raw, expected):
    assert SharedCrewConfig().tailnet_direct is False
    assert shared_crew_config_from(raw).tailnet_direct is expected


# -- capabilities ------------------------------------------------------------- #


@pytest.fixture()
def _caps():
    access._reset_setup_paths_for_tests()
    capabilities._reset_capability_sources_for_tests()
    wiring.install()
    yield
    wiring.uninstall()
    capabilities._reset_capability_sources_for_tests()
    access._reset_setup_paths_for_tests()


def test_active_direct_listener_makes_attribution_available(_caps):
    access.note_tailnet(
        enabled=True,
        host="h",
        identity_trust=True,
        loopback_only=True,
        proof_gap="platform_unprovable",
    )
    body = capabilities.aggregate_capabilities().to_dict()
    assert body["attribution"] == "unavailable"
    access.note_tailnet_direct(
        access.DirectTailnet(
            state="active", reason="", method="cli_app_bundle", urls=("http://crew:5476/",)
        )
    )
    body = capabilities.aggregate_capabilities().to_dict()
    assert body["attribution"] == "available"
    assert body["attribution_via"] == "tailscale_direct"
    assert body["tailnet_direct"]["method"] == "cli_app_bundle"
    assert body["setup_paths"]["tailscale"]["state"] == "available"


# -- the transport -------------------------------------------------------------- #


def test_a_path_cli_outside_the_vetted_locations_is_never_run(monkeypatch, tmp_path):
    planted = tmp_path / "tailscale"
    planted.write_text("#!/bin/sh\n")
    planted.chmod(0o755)
    monkeypatch.setattr(tailnet_localapi.shutil, "which", lambda _name: str(planted))
    from kiro_crew.dashboard import tailnet

    monkeypatch.setattr(tailnet, "_CLI_CANDIDATE_PATHS", ())
    assert tailnet_localapi.cli_candidates() == []


def test_a_socket_the_gateway_user_owns_is_not_trusted(tmp_path):
    fake = tmp_path / "tailscaled.sock"
    fake.write_text("")
    assert tailnet_localapi._root_owned_and_sealed(str(fake)) is False


def test_every_transport_failing_answers_nothing(monkeypatch):
    monkeypatch.setattr(tailnet_localapi, "_unix_socket_paths", lambda: ())
    monkeypatch.setattr(tailnet_localapi, "_localapi_macsys", lambda _p: None)
    monkeypatch.setattr(tailnet_localapi, "cli_candidates", lambda: [("/x/tailscale", "cli")])
    monkeypatch.setattr(tailnet_localapi, "_run_cli", lambda _c, _a: (None, True))
    answer = tailnet_localapi.whois("100.64.0.5:1")
    assert answer.data is None and answer.method == "none" and answer.transient


def test_cli_fallback_names_its_method(monkeypatch):
    monkeypatch.setattr(tailnet_localapi, "_unix_socket_paths", lambda: ())
    monkeypatch.setattr(tailnet_localapi, "_localapi_macsys", lambda _p: None)
    monkeypatch.setattr(
        tailnet_localapi,
        "cli_candidates",
        lambda: [
            ("/usr/local/bin/tailscale", "cli_path"),
            (tailnet_localapi.MACOS_APP_CLI, "cli_app_bundle"),
        ],
    )
    seen: list[tuple[str, list[str]]] = []

    def run(cli: str, args: list[str]) -> tuple[Any, bool]:
        seen.append((cli, args))
        return ({"ok": 1}, False) if cli == tailnet_localapi.MACOS_APP_CLI else (None, False)

    monkeypatch.setattr(tailnet_localapi, "_run_cli", run)
    answer = tailnet_localapi.whois("100.64.0.5:1")
    assert answer.method == "cli_app_bundle" and answer.data == {"ok": 1}
    assert seen[0][1] == ["whois", "--json", "100.64.0.5:1"]


def test_macsys_localapi_reads_a_root_owned_port_and_token(monkeypatch):
    monkeypatch.setattr(tailnet_localapi, "IS_MACOS", True)
    monkeypatch.setattr(tailnet_localapi, "_macsys_port_and_token", lambda: (41112, "tok"))
    seen: dict[str, Any] = {}

    def fake_get(conn: Any, path: str, headers: dict[str, str]) -> Answer:
        seen.update(host=conn.host, port=conn.port, path=path, auth=headers.get("Authorization"))
        return Answer({"Self": {}}, "none")

    monkeypatch.setattr(tailnet_localapi, "_http_get", fake_get)
    answer = tailnet_localapi._localapi_macsys("/localapi/v0/status")
    assert answer is not None and answer.method == "localapi_macsys"
    assert (seen["host"], seen["port"]) == ("127.0.0.1", 41112)
    assert seen["auth"] == "Basic OnRvaw=="


# -- in-Crew invites (the live allowlist overlay) ------------------------------ #


@pytest.fixture()
def _live():
    from kiro_crew.dashboard import tailnet_live_allowlist

    tailnet_live_allowlist.reset_for_tests()
    yield tailnet_live_allowlist
    tailnet_live_allowlist.reset_for_tests()


@pytest.mark.asyncio
async def test_an_invited_login_is_admitted_token_free_without_a_restart(_live):
    client = await _client(_gate())
    try:
        before, _ = await _get(client, _direct("100.64.0.5", port=41000))
        _live.grant("alice@example.com")
        after, body = await _get(client, _direct("100.64.0.5", port=41000))
    finally:
        await client.close()
    assert before == 403
    assert after == 200 and body["login"] == "alice@example.com"


@pytest.mark.asyncio
async def test_a_revoked_login_is_refused_on_its_next_request_inside_the_cache_window(_live):
    whois = FakeWhois()
    client = await _client(_gate(("alice@example.com",), whois))
    try:
        ok, _ = await _get(client, _direct("100.64.0.5", port=41001))
        _live.revoke("alice@example.com")
        refused, body = await _get(client, _direct("100.64.0.5", port=41001))
    finally:
        await client.close()
    assert ok == 200
    assert refused == 403 and body["reason"] == "login_not_allowed"
    assert whois.calls == ["100.64.0.5:41001"]


def test_a_live_grant_never_admits_a_tagged_node(_live):
    _live.grant("tagged-devices")
    assert (
        admission_refusal(_peer("100.64.0.9"), allowed_logins=(), owner=OWNER)
        == "tagged_node_not_allowed"
    )

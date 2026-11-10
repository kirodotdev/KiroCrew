"""Crew-owned OAuth MVP: the end-to-end flows behind the unit checks.

The card's Connect flow through the mint row, the token endpoint and the revoke
call against local stand-ins, the in-session bridge's transport to the gateway,
and the proxy route's handler module. Companion of ``test_crew_oauth_mvp.py``.
"""

from __future__ import annotations

import asyncio
import io
import json
import types
import urllib.error
import urllib.request

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.connections import crew_oauth
from kiro_crew.connections.crew_oauth import CrewOAuthError, Tokens, TokenStore
from kiro_crew.secrets import SecretVault
from kiro_crew.testing.wait import async_wait_until


@pytest.fixture(autouse=True)
def _pinned_modules(_floor_monkeypatch):
    """The code under test imports ``crew_oauth`` at call time; pin it to this file's."""
    import sys

    import kiro_crew.connections as connections_pkg

    _floor_monkeypatch.setitem(sys.modules, "kiro_crew.connections.crew_oauth", crew_oauth)
    _floor_monkeypatch.setattr(connections_pkg, "crew_oauth", crew_oauth, raising=False)
    # Projection runs in the gateway unless a test says otherwise.
    _floor_monkeypatch.setenv("KIROCREW_BOUND_PORT", "8765")


def _client_ns(**over):
    return types.SimpleNamespace(**{"client_id": "Iv1.abc", "client_secret": "sec", **over})


# --- config and backend -----------------------------------------------------


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("kas", True),
        ("kiro", False),
        ("", False),
        ("claude", True),
        ("strands", True),
        ("goose", True),
        ("opencode", False),
        ("deepseek", True),
    ],
)
def test_the_chat_backend_decides_custody(monkeypatch, backend, expected):
    import kiro_crew.config as config

    monkeypatch.setattr(
        config.KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: types.SimpleNamespace(agent=types.SimpleNamespace(acp_backend=backend))
        ),
    )
    assert crew_oauth._chat_backend_bridged() is expected


def test_an_unreadable_config_keeps_todays_custody(monkeypatch):
    import kiro_crew.config as config

    def _boom(cls):
        raise OSError("unreadable")

    monkeypatch.setattr(config.KiroCrewConfig, "load", classmethod(_boom))
    assert crew_oauth._chat_backend_bridged() is False

    import kiro_crew.config.loader as loader

    def _bad():
        raise ValueError("bad json")

    monkeypatch.setattr(loader, "read_config_for_update", _bad)
    assert crew_oauth.enabled() is False


def test_an_unreadable_vault_is_unknown_not_absent(monkeypatch):
    """Disconnect still tries the cleanup when the vault cannot be read."""

    class _Broken:
        def present(self, slug):
            raise OSError("vault")

    monkeypatch.setattr(crew_oauth, "TokenStore", _Broken)
    assert crew_oauth.holds_grant("github") is None
    assert crew_oauth.holds_grant("slack") is False


def test_the_sign_in_uses_the_products_one_pkce_implementation():
    from kiro_crew.auth.login import portal

    assert not hasattr(crew_oauth, "pkce_pair")
    import asyncio

    async def _make():
        return crew_oauth.SignIn("github")

    sign_in = asyncio.run(_make())
    assert portal.generate_code_challenge(sign_in._verifier) == sign_in._challenge


# --- stored shape -----------------------------------------------------------


@pytest.mark.parametrize("raw", ["not json", "[]", '{"access": ""}', '{"refresh": "r"}'])
def test_a_malformed_stored_pair_reads_as_nothing(raw):
    assert Tokens.from_json(raw) is None


def test_a_stored_pair_round_trips_and_the_default_store_uses_the_data_home(tmp_path, monkeypatch):
    import kiro_crew.config.paths as paths

    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    store = TokenStore()
    tokens = Tokens("a", "r", 123.0, "repo")
    store.replace("github", tokens)
    held = store.read("github")
    assert held is not None and held[0] == tokens and store.present("github")
    store.delete("github")
    assert store.read("github") is None and not store.present("github")


# --- operator client --------------------------------------------------------


def test_the_operator_client_needs_a_secret(tmp_path, monkeypatch):
    import kiro_crew.config.loader as loader
    import kiro_crew.config.paths as paths
    import kiro_crew.connections.oauth_clients as oauth_clients

    monkeypatch.setattr(loader, "read_config_for_update", lambda: {})
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(oauth_clients, "resolve_oauth_client", lambda *a, **k: _client_ns())
    assert crew_oauth._client("github").client_secret == "sec"
    monkeypatch.setattr(
        oauth_clients, "resolve_oauth_client", lambda *a, **k: _client_ns(client_secret=None)
    )
    with pytest.raises(CrewOAuthError) as err:
        crew_oauth._client("github")
    assert err.value.code == "client_not_configured"


# --- token endpoint and revoke against local stand-ins ----------------------


def _vendor(answers: dict, seen: list) -> web.Application:
    async def _token(request):
        seen.append(("token", dict(await request.post())))
        status, body = answers["token"]
        return web.Response(status=status, text=body, content_type="application/json")

    async def _revoke(request):
        seen.append(("revoke", request.headers.get("Authorization", ""), await request.json()))
        return web.Response(status=answers["revoke"])

    app = web.Application()
    app.router.add_post("/token", _token)
    app.router.add_delete("/applications/{client_id}/token", _revoke)
    return app


async def _with_vendor(monkeypatch, answers, seen):
    server = TestServer(_vendor(answers, seen))
    await server.start_server()
    base = str(server.make_url(""))
    monkeypatch.setitem(
        crew_oauth._ENDPOINTS,
        "github",
        {
            **crew_oauth._ENDPOINTS["github"],
            "token": f"{base}/token",
            "revoke": f"{base}/applications/{{client_id}}/token",
        },
    )
    return server


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ((400, "{}"), "token_http_400"),
        ((200, "not json"), "token_bad_answer"),
        ((200, "[1]"), "token_bad_answer"),
    ],
)
async def test_token_endpoint_failures_are_coded(monkeypatch, answer, code):
    seen: list = []
    server = await _with_vendor(monkeypatch, {"token": answer, "revoke": 204}, seen)
    try:
        with pytest.raises(CrewOAuthError) as err:
            await crew_oauth._post_token("github", {"grant_type": "refresh_token"})
        assert err.value.code == code
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_disconnect_revokes_the_token_and_deletes_the_pair(tmp_path, monkeypatch):
    seen: list = []
    server = await _with_vendor(monkeypatch, {"token": (200, "{}"), "revoke": 204}, seen)
    monkeypatch.setattr(crew_oauth, "_client", lambda slug: _client_ns())
    store = TokenStore(SecretVault(tmp_path))
    store.replace("github", Tokens("a1", "r1", None, ""))
    try:
        assert await crew_oauth.disconnect("github", store=store) == {
            "removed": True,
            "revoked": True,
        }
        assert await crew_oauth.disconnect("github", store=store) == {
            "removed": False,
            "revoked": False,
        }
    finally:
        await server.close()
    assert seen[0][0] == "revoke" and seen[0][1].startswith("Basic ")
    assert seen[0][2] == {"access_token": "a1"}
    assert store.read("github") is None


@pytest.mark.asyncio
async def test_disconnect_deletes_an_entry_it_cannot_read(tmp_path):
    """A hand-written, unparseable entry is never used, and Disconnect still removes it."""
    vault = SecretVault(tmp_path)
    store = TokenStore(vault)
    vault.set_sync(crew_oauth.token_entry_name("github"), "not json")
    assert store.read("github") is None and store.present("github")
    result = await crew_oauth.disconnect("github", store=store)
    assert result == {"removed": True, "revoked": False}
    assert not store.present("github")


@pytest.mark.asyncio
async def test_a_failed_revoke_still_deletes_the_pair(tmp_path, monkeypatch):
    seen: list = []
    server = await _with_vendor(monkeypatch, {"token": (200, "{}"), "revoke": 500}, seen)
    monkeypatch.setattr(crew_oauth, "_client", lambda slug: _client_ns())
    store = TokenStore(SecretVault(tmp_path))
    store.replace("github", Tokens("a1", None, None, ""))
    try:
        assert await crew_oauth.disconnect("github", store=store) == {
            "removed": True,
            "revoked": False,
        }
    finally:
        await server.close()
    assert store.read("github") is None


# --- the card's Connect flow through the mint row ----------------------------


@pytest.fixture
def github_on_port_zero(monkeypatch):
    from kiro_crew.connections import registry

    provider = dict(registry.get_provider("github"))
    provider["auth"] = {**provider["auth"], "redirect_port": 0}
    monkeypatch.setattr(registry, "get_provider", lambda slug: provider)
    monkeypatch.setattr(crew_oauth, "_client", lambda slug: _client_ns())
    from kiro_crew.connections import mint

    monkeypatch.setattr(mint, "_mints", {})
    monkeypatch.setattr(crew_oauth, "_startup_locks", {})
    return mint


@pytest.mark.asyncio
async def test_connect_runs_through_the_mint_row_to_granted(
    tmp_path, monkeypatch, github_on_port_zero
):
    import aiohttp

    mint = github_on_port_zero
    store = TokenStore(SecretVault(tmp_path))
    monkeypatch.setattr(crew_oauth, "TokenStore", lambda: store)

    async def _post(slug, form):
        return {"access_token": "a1", "refresh_token": "r1", "expires_in": 28800}

    monkeypatch.setattr(crew_oauth, "_post_token", _post)
    token = await crew_oauth.start_mint_row("github")
    row = mint._mints["github"]
    assert row["token"] == token and row["state"] == "waiting"
    sign_in = row["client"]
    url = row["oauth_url"]
    state = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))["state"]
    async with aiohttp.ClientSession() as http:
        callback = f"http://127.0.0.1:{sign_in.bound_port}/callback"
        async with http.get(callback, params={"state": state, "code": "c0de"}) as resp:
            assert resp.status == 200
    await async_wait_until(lambda: mint._mints["github"].get("state") == "granted")
    assert "client" not in mint._mints["github"] and "oauth_url" not in mint._mints["github"]
    assert store.read("github")[0].access == "a1"


@pytest.mark.asyncio
@pytest.mark.parametrize("draw", ["_state", "_challenge"])
async def test_a_draw_shaped_like_a_key_id_still_reaches_waiting(
    monkeypatch, github_on_port_zero, draw
):
    """``state`` and the PKCE challenge are random draws; the gate reads the rest of the link."""
    mint = github_on_port_zero
    # "akia" + 16 alphanumerics: the key-id shape the gate matches regardless of case.
    drawn = "akia" + "q" * 16 + "x" * 23

    class _Drawn(crew_oauth.SignIn):
        def __init__(self, slug, *args):
            super().__init__(slug, *args)
            setattr(self, draw, drawn)

    monkeypatch.setattr(crew_oauth, "SignIn", _Drawn)
    await crew_oauth.start_mint_row("github")
    row = mint._mints["github"]
    assert row["state"] == "waiting"
    assert drawn in row["oauth_url"]
    await row["client"].shutdown()


@pytest.mark.asyncio
async def test_a_client_id_carrying_a_key_id_fails_the_row(monkeypatch, github_on_port_zero):
    mint = github_on_port_zero
    key_id = "AKIA" + "IOSFODNN7EXAMPLE"
    monkeypatch.setattr(crew_oauth, "_client", lambda slug: _client_ns(client_id=key_id))
    await crew_oauth.start_mint_row("github")
    row = mint._mints["github"]
    assert row["state"] == "failed"
    assert row["reason"] == "mint_url_rejected"


@pytest.mark.asyncio
async def test_a_declined_consent_fails_the_row(monkeypatch, github_on_port_zero):
    import aiohttp

    mint = github_on_port_zero
    await crew_oauth.start_mint_row("github")
    row = mint._mints["github"]
    sign_in = row["client"]
    state = dict(p.split("=", 1) for p in row["oauth_url"].split("?", 1)[1].split("&"))["state"]
    async with aiohttp.ClientSession() as http:
        callback = f"http://127.0.0.1:{sign_in.bound_port}/callback"
        async with http.get(callback, params={"state": state}) as resp:
            assert resp.status == 200
    await async_wait_until(lambda: mint._mints["github"].get("state") == "failed")
    assert mint._mints["github"]["reason"] == "crew_oauth_consent_declined"


@pytest.mark.asyncio
async def test_a_non_ascii_state_is_a_400_not_a_500(monkeypatch, github_on_port_zero):
    import aiohttp

    mint = github_on_port_zero
    await crew_oauth.start_mint_row("github")
    sign_in = mint._mints["github"]["client"]
    async with aiohttp.ClientSession() as http:
        callback = f"http://127.0.0.1:{sign_in.bound_port}/callback"
        async with http.get(callback, params={"state": "é", "code": "c"}) as resp:
            assert resp.status == 400
    await sign_in.shutdown()


@pytest.mark.asyncio
async def test_a_busy_callback_port_fails_the_row(monkeypatch, github_on_port_zero):
    mint = github_on_port_zero

    async def _busy(self):
        raise CrewOAuthError("callback_port_busy")

    monkeypatch.setattr(crew_oauth.SignIn, "start", _busy)
    await crew_oauth.start_mint_row("github")
    assert mint._mints["github"]["state"] == "failed"
    assert mint._mints["github"]["reason"] == "crew_oauth_callback_port_busy"


@pytest.mark.asyncio
async def test_a_superseding_connect_waits_for_the_first_listener(monkeypatch, github_on_port_zero):
    from kiro_crew import security

    mint = github_on_port_zero
    gate = asyncio.Event()
    scans: list[str] = []

    def _scan(url):
        scans.append(url)
        if len(scans) == 1:
            asyncio.run_coroutine_threadsafe(gate.wait(), loop).result()
        return False

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(security, "oauth_url_contains_credential", _scan)
    first = asyncio.create_task(crew_oauth.start_mint_row("github"))
    await async_wait_until(lambda: len(scans) == 1)
    second = asyncio.create_task(crew_oauth.start_mint_row("github"))
    lock = crew_oauth._startup_locks["github"]
    await async_wait_until(lambda: len(getattr(lock, "_waiters", None) or ()) == 1)
    assert len(scans) == 1  # the second start is parked on the lock, not scanning
    gate.set()
    await first
    token = await second
    row = mint._mints["github"]
    assert row["token"] == token and row["state"] == "waiting"
    await row["client"].shutdown()


@pytest.mark.asyncio
async def test_a_superseded_row_is_left_alone(monkeypatch, github_on_port_zero):
    mint = github_on_port_zero
    assert await crew_oauth._set_row("github", "nobody", {"state": "granted"}) is False
    mint._mints["github"] = {"state": "waiting", "token": "t2"}
    assert await crew_oauth._set_row("github", "t1", {"state": "granted"}) is False
    assert mint._mints["github"]["state"] == "waiting"


# --- the in-session bridge's transport --------------------------------------


def _no_tcp(*_a, **_k):
    raise AssertionError("POSIX never sends to the proxy over TCP")


def test_the_bridge_talks_to_the_gateway_socket_only(monkeypatch, tmp_path):
    import kiro_crew.config.loader_locations as loader_locations
    import kiro_crew.config.paths as paths
    import kiro_crew.loopback_http as loopback_http
    from kiro_crew import kas_connection_bridge, platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(loader_locations, "read_local_secret", lambda port: "s3cret")
    calls: list = []

    def _urlopen(request, timeout, *, socket_path, verify_peer):
        calls.append((request.full_url, dict(request.header_items()), str(socket_path)))
        return io.BytesIO(b"")

    monkeypatch.setattr(loopback_http, "unix_socket_urlopen", _urlopen)
    # No socket file exists under this home: still the socket, never TCP.
    monkeypatch.setattr(loopback_http, "tcp_verified_urlopen", _no_tcp)
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    doc = {"port": 8123, "version": "v1", "bridge_token": "tok", "connection": "github"}
    bridge = kas_connection_bridge.build_bridge(
        doc, server="github", out=io.BytesIO(), err=io.StringIO()
    )
    bridge._opener.open(
        __import__("urllib.request").request.Request("http://127.0.0.1:8123/api/connection-proxy"),
        5,
    )
    url, _headers, path = calls[0]
    assert url.endswith("/api/connection-proxy") and path == str(tmp_path / "dashboard-8123.sock")
    sent = bridge._request_headers()
    assert sent["X-Crew-Bridge"] == "v1" and sent["X-Crew-Bridge-Token"] == "tok"
    assert sent["X-Internal-Secret"] == "s3cret" and "Authorization" not in sent


def test_the_bridge_on_windows_talks_to_the_gateway_over_verified_tcp(monkeypatch):
    import kiro_crew.cli_server_peer as cli_server_peer
    import kiro_crew.loopback_http as loopback_http
    from kiro_crew import kas_connection_bridge, platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(loopback_http, "unix_socket_urlopen", _no_tcp)
    calls: list = []

    def _tcp(request, timeout, *, verify_peer):
        calls.append(request.full_url)
        return io.BytesIO(b"")

    monkeypatch.setattr(loopback_http, "tcp_verified_urlopen", _tcp)
    opener = kas_connection_bridge._GatewayOpener(8123)
    opener.open(urllib.request.Request("http://127.0.0.1:8123/api/connection-proxy"), 5)
    assert calls == ["http://127.0.0.1:8123/api/connection-proxy"]
    verdict = cli_server_peer._ConnectedPeerVerdict
    denied: list = []
    monkeypatch.setattr(cli_server_peer, "_audit_peer_denied", lambda *a: denied.append(a))
    monkeypatch.setattr(cli_server_peer, "_connected_peer_verdict", lambda sock: verdict.MISMATCH)
    with pytest.raises(PermissionError):
        opener._verify_peer(object())
    assert [a[:2] for a in denied] == [("connection_bridge", 8123)]
    monkeypatch.setattr(cli_server_peer, "_connected_peer_verdict", lambda sock: verdict.MATCH)
    opener._verify_peer(object())
    assert len(denied) == 1


def test_the_bridge_refuses_a_socket_not_answered_by_the_gateway(monkeypatch):
    import kiro_crew.cli_server_peer as cli_server_peer
    import kiro_crew.mcp_gateway.socketsec as socketsec
    from kiro_crew import kas_connection_bridge, platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    opener = kas_connection_bridge._GatewayOpener(8123)
    monkeypatch.setattr(
        socketsec, "check_peer_is_self", lambda sock: socketsec.PeerCredResult.MISMATCH
    )
    with pytest.raises(PermissionError):
        opener._verify_peer(object())
    monkeypatch.setattr(
        socketsec, "check_peer_is_self", lambda sock: socketsec.PeerCredResult.MATCH
    )
    monkeypatch.setattr(
        cli_server_peer, "_connected_unix_peer_is_recorded_gateway", lambda sock, port: True
    )
    opener._verify_peer(object())


def test_the_bridge_serves_stdio_until_it_closes(monkeypatch):
    import sys

    from kiro_crew import kas_connection_bridge

    built: list = []

    class _Fake:
        def serve(self, stdin):
            built.append(stdin)
            return 0

    monkeypatch.setattr(kas_connection_bridge, "build_bridge", lambda doc, **k: _Fake())
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"")))
    assert kas_connection_bridge.run_connection_bridge({}, server="github") == 0
    assert built


def test_an_unparsable_gateway_refusal_still_names_its_status():
    import urllib.error

    from kiro_crew.kas_connection_bridge import ConnectionBridge

    bridge = ConnectionBridge("http://x/", {}, server="github", out=io.BytesIO(), err=io.StringIO())
    exc = urllib.error.HTTPError(
        "http://x/",
        502,
        "Bad",
        {"X-Crew-Proxy-Refusal": "provider_unreachable"},
        io.BytesIO(b"<html>"),
    )
    assert bridge._http_error_reason(exc) == "was refused by Kiro Crew (HTTP 502)"


# --- the route's handler module ---------------------------------------------


@pytest.mark.asyncio
async def test_the_route_handler_refuses_a_non_internal_caller():
    from kiro_crew.dashboard.handlers.connection_proxy import api_connection_proxy

    app = web.Application()
    app.router.add_post("/api/connection-proxy", api_connection_proxy)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/connection-proxy", data=json.dumps({}))
        assert resp.status == 403
        assert (await resp.json())["code"] == "internal_unix_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("windows", [False, True])
async def test_an_internal_tcp_caller_is_taken_on_windows_only(monkeypatch, windows):
    """POSIX keeps the Unix socket as the only way in; Windows takes loopback TCP,
    with the peer pid from the kernel table, and pins it like the Unix arm."""
    import os

    import kiro_crew.member_memory_auth as member_memory_auth
    from kiro_crew import platform_compat
    from kiro_crew.connections import crew_oauth_proxy

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
    monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: os.getpid())
    monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-me")
    monkeypatch.setattr(platform_compat, "process_owner_sid", lambda pid: "S-1-5-21-me")
    monkeypatch.setattr(crew_oauth, "manages", lambda slug: False)
    monkeypatch.setattr(crew_oauth, "custody_on", lambda slug: False)
    monkeypatch.setattr(crew_oauth_proxy, "_bridges", {})
    crew_oauth_proxy.register_bridge(
        version="v1",
        token="tok",
        session_key="s",
        server="github",
        slug="github",
        mcp_url="http://127.0.0.1:1/mcp",
    )

    @web.middleware
    async def _auth(request, handler):
        request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app.router.add_post(crew_oauth_proxy.PROXY_PATH, crew_oauth_proxy.handle)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            crew_oauth_proxy.PROXY_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                crew_oauth_proxy.BRIDGE_HEADER: "v1",
                crew_oauth_proxy.BRIDGE_TOKEN_HEADER: "tok",
            },
        )
        # Admitted on Windows (then stopped by the switched-off Connection).
        expected = "crew_oauth_off" if windows else "internal_unix_required"
        assert (await resp.json())["code"] == expected
    if windows:
        assert crew_oauth_proxy._bridges["v1"].pid == os.getpid()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("me", "owner"),
    [("S-1-5-21-me", "S-1-5-21-other"), (None, None), ("S-1-5-21-me", None)],
)
async def test_a_windows_tcp_peer_this_user_does_not_own_is_refused(monkeypatch, me, owner):
    """The Windows TCP arm admits a peer only when its owner SID is this user's;
    another account's peer, or an unreadable owner, is refused and never pinned."""
    import os

    import kiro_crew.member_memory_auth as member_memory_auth
    from kiro_crew import platform_compat
    from kiro_crew.connections import crew_oauth_proxy

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: os.getpid())
    monkeypatch.setattr(platform_compat, "current_user_sid", lambda: me)
    monkeypatch.setattr(platform_compat, "process_owner_sid", lambda pid: owner)
    monkeypatch.setattr(crew_oauth, "manages", lambda slug: True)
    monkeypatch.setattr(crew_oauth, "custody_on", lambda slug: True)
    monkeypatch.setattr(crew_oauth_proxy, "_bridges", {})
    crew_oauth_proxy.register_bridge(
        version="v1",
        token="tok",
        session_key="s",
        server="github",
        slug="github",
        mcp_url="http://127.0.0.1:1/mcp",
    )

    @web.middleware
    async def _auth(request, handler):
        request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app.router.add_post(crew_oauth_proxy.PROXY_PATH, crew_oauth_proxy.handle)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            crew_oauth_proxy.PROXY_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                crew_oauth_proxy.BRIDGE_HEADER: "v1",
                crew_oauth_proxy.BRIDGE_TOKEN_HEADER: "tok",
            },
        )
        assert resp.status == 403
        assert (await resp.json())["code"] == "bridge_unregistered"
    assert crew_oauth_proxy._bridges["v1"].pid is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status", "refusal"),
    [
        ("needs_auth", 409, "needs_auth"),
        ("token_unreachable", 502, "refresh_unavailable"),
        ("token_http_503", 502, "refresh_rejected"),
        ("incorrect_client_credentials", 502, "refresh_rejected"),
        ("token_bad_answer", 502, "refresh_rejected"),
        ("client_not_configured", 409, "client_not_configured"),
    ],
)
async def test_only_a_dead_grant_asks_the_user_to_reconnect(monkeypatch, code, status, refusal):
    """A provider outage during refresh is a temporary failure, not a lost sign-in."""
    import os

    import kiro_crew.dashboard.origin as origin
    import kiro_crew.mcp_gateway.socketsec as socketsec
    from kiro_crew.connections import crew_oauth_proxy

    monkeypatch.setattr(origin, "request_is_unix_socket", lambda request: True)
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda sock: os.getpid())
    monkeypatch.setattr(crew_oauth, "manages", lambda slug: True)
    monkeypatch.setattr(crew_oauth, "custody_on", lambda slug: True)

    async def _token(slug, *, force=False, store=None):
        raise CrewOAuthError(code)

    monkeypatch.setattr(crew_oauth, "access_token", _token)
    monkeypatch.setattr(crew_oauth_proxy, "_bridges", {})
    crew_oauth_proxy.register_bridge(
        version="v1",
        token="tok",
        session_key="s",
        server="github",
        slug="github",
        mcp_url="http://127.0.0.1:1/mcp",
    )

    @web.middleware
    async def _auth(request, handler):
        request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app.router.add_post(crew_oauth_proxy.PROXY_PATH, crew_oauth_proxy.handle)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            crew_oauth_proxy.PROXY_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                crew_oauth_proxy.BRIDGE_HEADER: "v1",
                crew_oauth_proxy.BRIDGE_TOKEN_HEADER: "tok",
            },
        )
        assert resp.status == status
        assert resp.headers[crew_oauth_proxy.REFUSAL_HEADER] == refusal


@pytest.mark.asyncio
async def test_disconnect_revokes_a_pair_that_replaced_the_one_it_read(tmp_path, monkeypatch):
    """A rotation landing during the revoke is re-read and revoked, never dropped."""
    store = TokenStore(SecretVault(tmp_path))
    store.replace("github", Tokens("a1", "r1", None, ""))
    revoked: list[str] = []

    async def _revoke(slug, tokens):
        revoked.append(tokens.access)
        if tokens.access == "a1":
            # A writer that took no part in this Disconnect's lock, e.g. a second
            # gateway on the same data home, rotates the pair mid-revoke.
            store.replace("github", Tokens("a2", "r2", None, ""))
        return True

    monkeypatch.setattr(crew_oauth, "_revoke", _revoke)
    assert await crew_oauth.disconnect("github", store=store) == {
        "removed": True,
        "revoked": True,
    }
    assert revoked == ["a1", "a2"] and store.read("github") is None


@pytest.mark.asyncio
async def test_disconnect_waits_for_a_refresh_holding_the_lock(tmp_path, monkeypatch):
    """Refresh and Disconnect take the same per-connection lock."""
    import asyncio

    store = TokenStore(SecretVault(tmp_path))
    store.replace("github", Tokens("a1", "r1", None, ""))
    order: list[str] = []

    async def _revoke(slug, tokens):
        order.append(f"revoke:{tokens.access}")
        return True

    monkeypatch.setattr(crew_oauth, "_revoke", _revoke)
    monkeypatch.setattr(crew_oauth, "_refresh_locks", {})
    lock = crew_oauth._refresh_locks.setdefault("github", asyncio.Lock())
    await lock.acquire()
    task = asyncio.create_task(crew_oauth.disconnect("github", store=store))
    await async_wait_until(lambda: len(getattr(lock, "_waiters", None) or ()) == 1)
    store.replace("github", Tokens("a2", "r2", None, ""))  # the refresh's commit
    order.append("refresh-committed")
    lock.release()
    assert (await task)["removed"] is True
    assert order == ["refresh-committed", "revoke:a2"] and store.read("github") is None


@pytest.mark.parametrize(
    ("headers", "routed"),
    [({"Authorization": "Bearer ghp-pat"}, False), ({"User-Agent": "x"}, True), ({}, True)],
)
def test_test_is_refused_only_for_an_entry_routed_through_crew(monkeypatch, headers, routed):
    """A real PAT entry is tested as before; only a Crew-routed entry gets the 409."""
    import kiro_crew.mcp_discovery as mcp_discovery
    from kiro_crew.dashboard.handlers import connections

    monkeypatch.setattr(crew_oauth, "manages", lambda slug: True)
    monkeypatch.setattr(crew_oauth, "custody_on", lambda slug: True)
    server = types.SimpleNamespace(
        name="github", url="https://api.githubcopilot.com/mcp/", headers=headers
    )
    monkeypatch.setattr(mcp_discovery, "list_servers", lambda: [server])
    assert connections._routed_through_crew_oauth("github") is routed
    monkeypatch.setattr(mcp_discovery, "list_servers", lambda: [])
    assert connections._routed_through_crew_oauth("github") is False


@pytest.mark.parametrize(
    "headers", [{"Authorization": "Bearer ghp-pat"}, {"X-Api-Key": "k"}, {"User-Agent": "x"}, {}]
)
def test_status_custody_matches_routing(monkeypatch, headers):
    """The card reads the grant the bridge will use: Crew's only for a Crew-routed entry."""
    import kiro_crew.mcp_discovery as mcp_discovery
    from kiro_crew.acp import kas_env_launch
    from kiro_crew.connections import registry, status

    monkeypatch.setattr(crew_oauth, "manages", lambda slug: True)
    monkeypatch.setattr(crew_oauth, "custody_on", lambda slug: True)
    url = "https://api.githubcopilot.com/mcp/"
    server = types.SimpleNamespace(name="github", url=url, headers=headers)
    monkeypatch.setattr(mcp_discovery, "list_servers", lambda: [server])
    routed = kas_env_launch.crew_connection_slug("github", {"url": url, "headers": headers}, "kas")
    crew = status._crew_managed_slugs([registry.get_provider("github")])
    assert ("github" in crew) is bool(routed)


@pytest.mark.asyncio
async def test_the_card_shows_no_sharing_hint_for_a_crew_held_grant(tmp_path, monkeypatch):
    """Option B: a Crew-held grant is never described as shared or kept."""
    from kiro_crew.connections import ownership, status

    # A temporary data home: never the developer's real config or MCP inventory.
    data_home = tmp_path / "crew-home"
    data_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    called: list = []

    async def _sharers(present, _dirs):
        called.append([slug for slug, _url in present])
        return {}

    monkeypatch.setattr(ownership, "grant_sharers_map", _sharers)
    monkeypatch.setattr(status, "_crew_managed_slugs", lambda providers: frozenset({"github"}))
    monkeypatch.setattr(crew_oauth, "holds_grant", lambda slug: True)
    monkeypatch.setattr(status, "_provider_grant_presence", lambda url: True)
    monkeypatch.setattr(status, "reconcile_connected_since", lambda statuses, now: {})
    statuses = await status.collect_connection_statuses()
    gh = next(s for s in statuses if s["slug"] == "github")
    assert gh["grantPresent"] is True and "grantSharedWith" not in gh
    assert all("github" not in slugs for slugs in called)
    # The card hides Test (kiro-cli's grant, refused 409) only for this row.
    assert gh["crewHeld"] is True
    assert all("crewHeld" not in s for s in statuses if s["slug"] != "github")


@pytest.mark.asyncio
async def test_a_native_grant_is_not_marked_crew_held(tmp_path, monkeypatch):
    """Flag off (or kiro-cli backend): no row carries ``crewHeld``, so Test stays."""
    from kiro_crew.connections import status

    data_home = tmp_path / "crew-home"
    data_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    monkeypatch.setattr(crew_oauth, "manages", lambda slug: False)
    monkeypatch.setattr(crew_oauth, "custody_on", lambda slug: False)
    monkeypatch.setattr(status, "_provider_grant_presence", lambda url: False)
    monkeypatch.setattr(status, "reconcile_connected_since", lambda statuses, now: {})
    statuses = await status.collect_connection_statuses()
    assert statuses and all("crewHeld" not in s for s in statuses)

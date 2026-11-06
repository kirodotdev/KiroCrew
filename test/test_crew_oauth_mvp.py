"""Crew-owned OAuth MVP: GitHub on KAS behind ``connections.crew_oauth`` (default off).

What must hold (RFC "Crew-owned OAuth for Connections", phase 1):

* flag off -> the KAS projection is exactly what it was;
* flag on -> the GitHub entry becomes a stdio bridge to the gateway; no url,
  header, oauth block or token on the wire, and every GitHub call must ask;
* refresh is single-flight and commits the rotated pair before it is used; a
  failed refresh never deletes the stored pair;
* the proxy forwards a ``tools/call`` only against an approval Crew's gate
  granted, pins one bridge process, injects the bearer itself and never answers
  401 toward the harness;
* Windows: the flag reads as off and the launcher refuses.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import types
import urllib.error
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from kas_agents_helpers import _spec

from kiro_crew import kas_env_shim, mediated_request_capability, platform_compat
from kiro_crew.acp import kas_agents, kas_env_launch
from kiro_crew.connections import crew_oauth, crew_oauth_proxy
from kiro_crew.connections.crew_oauth import CrewOAuthError, Tokens, TokenStore
from kiro_crew.kas_connection_bridge import ConnectionBridge
from kiro_crew.secrets import SecretVault
from kiro_crew.testing.wait import async_wait_until

GITHUB_URL = "https://api.githubcopilot.com/mcp/"
SESSION = "dashboard_chat-crew-oauth-1"
CANARY_SECRET = "gh-client-secret-canary-7731"


@pytest.fixture(autouse=True)
def _clean_proxy_state(_floor_monkeypatch):
    # The code under test imports these modules at call time; pin them to the
    # objects this file patches, whatever another test rebound before.
    import sys

    import kiro_crew.connections as connections_pkg

    for name, module in (("crew_oauth", crew_oauth), ("crew_oauth_proxy", crew_oauth_proxy)):
        _floor_monkeypatch.setitem(sys.modules, f"kiro_crew.connections.{name}", module)
        _floor_monkeypatch.setattr(connections_pkg, name, module, raising=False)
    crew_oauth_proxy._bridges.clear()
    crew_oauth_proxy._approvals.clear()
    crew_oauth._refresh_locks.clear()
    # Projection runs in the gateway unless a test says otherwise.
    _floor_monkeypatch.setenv("KIROCREW_BOUND_PORT", "8765")
    yield
    crew_oauth_proxy._bridges.clear()
    crew_oauth_proxy._approvals.clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    data_home = tmp_path / "crew-home"
    data_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    return data_home


def _github_spec() -> dict:
    return _spec(
        tools=["@github"],
        allowedTools=["@github"],
        mcpServers={
            "github": {
                "url": GITHUB_URL,
                "oauth": {"clientId": "Iv1.abc", "clientSecret": CANARY_SECRET},
            }
        },
    )


def _project(monkeypatch, *, on: bool) -> dict:
    monkeypatch.setattr(crew_oauth, "enabled", lambda: on)
    monkeypatch.setattr(crew_oauth, "_chat_backend_is_kas", lambda: True)
    monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")
    return kas_agents.to_client_custom_agent("kirocrew", _github_spec(), "p", session_key=SESSION)


# --- vault compare-and-set --------------------------------------------------


def test_set_if_revision_writes_only_over_the_revision_it_read(tmp_path):
    vault = SecretVault(tmp_path)
    first = vault.set_if_revision_sync("T", "one", None)
    assert first is not None
    assert vault.set_if_revision_sync("T", "again", None) is None  # not absent any more
    second = vault.set_if_revision_sync("T", "two", first)
    assert second is not None and second != first
    assert vault.set_if_revision_sync("T", "stale", first) is None
    assert vault.get("T").reveal() == "two"


# --- flag and platform ------------------------------------------------------


def test_the_flag_is_off_unless_config_says_true(monkeypatch):
    import kiro_crew.config.loader as loader

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(loader, "read_config_for_update", lambda: {})
    assert crew_oauth.enabled() is False
    monkeypatch.setattr(
        loader, "read_config_for_update", lambda: {"connections": {"crew_oauth": "yes"}}
    )
    assert crew_oauth.enabled() is False
    monkeypatch.setattr(
        loader, "read_config_for_update", lambda: {"connections": {"crew_oauth": True}}
    )
    assert crew_oauth.enabled() is True
    assert crew_oauth.manages("asana") is False


def test_windows_reads_the_flag_as_off(monkeypatch):
    import kiro_crew.config.loader as loader

    monkeypatch.setattr(
        loader, "read_config_for_update", lambda: {"connections": {"crew_oauth": True}}
    )
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert crew_oauth.enabled() is False


# --- KAS projection ---------------------------------------------------------


def test_flag_off_projection_is_unchanged(home, monkeypatch):
    monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")
    monkeypatch.setattr(crew_oauth, "enabled", lambda: False)
    off = kas_agents.to_client_custom_agent("kirocrew", _github_spec(), "p", session_key=SESSION)
    # main's projection of this spec, written out: the url entry and its allow,
    # no bridge, no forced ask.
    assert off["mcpServers"] == {"github": {"url": GITHUB_URL}}
    assert off["permissions"] == {
        "rules": [{"capability": "mcp", "effect": "allow", "match": ["github/*"]}]
    }
    assert not crew_oauth_proxy._bridges


def test_flag_on_projects_a_gateway_bridge_and_forces_ask(home, monkeypatch):
    out = _project(monkeypatch, on=True)
    entry = out["mcpServers"]["github"]
    wire = json.dumps(out)
    assert "url" not in entry and "oauth" not in entry
    assert kas_env_shim.SHIM_MODULE in entry["args"]
    assert CANARY_SECRET not in wire and GITHUB_URL not in wire
    asks = [r for r in out["permissions"]["rules"] if r["effect"] == "ask"]
    assert {"capability": "mcp", "match": ["github/*"], "effect": "ask"} in asks
    names = kas_env_launch.batch_snapshot_names([out])
    doc = json.loads((home / "kas-mcp-launch" / names[0]).read_text())
    assert doc["connection"] == "github" and doc["port"] == 8123
    assert set(doc) >= {"bridge_token", "version", "session"}
    assert CANARY_SECRET not in json.dumps(doc) and GITHUB_URL not in json.dumps(doc)
    assert crew_oauth_proxy.is_connection_server(SESSION, "github")


def test_a_hand_authored_github_name_elsewhere_is_not_bridged(home, monkeypatch):
    monkeypatch.setattr(crew_oauth, "enabled", lambda: True)
    entry = {"url": "https://example.invalid/mcp"}
    assert kas_env_launch.crew_connection_slug("github", entry) == ""


# --- tokens and refresh -----------------------------------------------------


def _store(tmp_path, tokens: Tokens) -> TokenStore:
    store = TokenStore(SecretVault(tmp_path))
    store.replace("github", tokens)
    return store


@pytest.mark.asyncio
async def test_a_fresh_token_is_returned_without_refresh(tmp_path, monkeypatch):
    store = _store(tmp_path, Tokens("a1", "r1", time.time() + 3600, ""))

    async def _never(*_a):
        raise AssertionError("no refresh expected")

    monkeypatch.setattr(crew_oauth, "_post_token", _never)
    assert await crew_oauth.access_token("github", store=store) == "a1"


@pytest.mark.asyncio
async def test_refresh_is_single_flight_and_committed_before_use(tmp_path, monkeypatch):
    store = _store(tmp_path, Tokens("a1", "r1", time.time() - 10, ""))
    monkeypatch.setattr(
        crew_oauth,
        "_client",
        lambda slug: types.SimpleNamespace(client_id="id", client_secret="sec"),
    )
    calls: list[str] = []
    release = asyncio.Event()

    async def _post(slug, form):
        calls.append(form["refresh_token"])
        # The vendor answers only once every caller is queued on the lock.
        await release.wait()
        return {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}

    monkeypatch.setattr(crew_oauth, "_post_token", _post)
    callers = [
        asyncio.create_task(crew_oauth.access_token("github", store=store)) for _ in range(5)
    ]
    await async_wait_until(lambda: calls == ["r1"])
    lock = crew_oauth._refresh_locks["github"]
    await async_wait_until(lambda: len(getattr(lock, "_waiters", None) or ()) == 4)
    release.set()
    got = await asyncio.gather(*callers)
    assert got == ["a2"] * 5
    assert calls == ["r1"]  # one single-use refresh token spent once
    held = store.read("github")
    assert held is not None and held[0].refresh == "r2"


@pytest.mark.asyncio
async def test_a_dead_refresh_token_keeps_the_stored_pair(tmp_path, monkeypatch):
    store = _store(tmp_path, Tokens("a1", "r1", time.time() - 10, ""))
    monkeypatch.setattr(
        crew_oauth,
        "_client",
        lambda slug: types.SimpleNamespace(client_id="id", client_secret="sec"),
    )

    async def _post(slug, form):
        return {"error": "bad_refresh_token"}

    monkeypatch.setattr(crew_oauth, "_post_token", _post)
    with pytest.raises(CrewOAuthError) as err:
        await crew_oauth.access_token("github", store=store)
    assert err.value.code == "needs_auth"
    assert store.read("github") is not None  # never deleted on a refresh failure


@pytest.mark.asyncio
async def test_a_rotation_by_another_writer_wins_over_a_failed_refresh(tmp_path, monkeypatch):
    store = _store(tmp_path, Tokens("a1", "r1", time.time() - 10, ""))
    monkeypatch.setattr(
        crew_oauth,
        "_client",
        lambda slug: types.SimpleNamespace(client_id="id", client_secret="sec"),
    )

    async def _post(slug, form):
        # A second gateway rotated r1 first; ours is now spent.
        store.replace("github", Tokens("a9", "r9", time.time() + 3600, ""))
        return {"error": "bad_refresh_token"}

    monkeypatch.setattr(crew_oauth, "_post_token", _post)
    assert await crew_oauth.access_token("github", store=store) == "a9"


def test_token_endpoint_errors_carry_no_value():
    with pytest.raises(CrewOAuthError) as err:
        crew_oauth.tokens_from_response({"error": "bad_verification_code"}, 0.0)
    assert err.value.code == "bad_verification_code"


# --- sign-in listener -------------------------------------------------------


@pytest.mark.asyncio
async def test_sign_in_checks_state_swaps_the_code_and_stores_the_pair(tmp_path, monkeypatch):
    import aiohttp

    from kiro_crew.connections import registry

    provider = dict(registry.get_provider("github"))
    # Port 0: the listener itself takes a free port, so no other process can race it.
    provider["auth"] = {**provider["auth"], "redirect_port": 0}
    monkeypatch.setattr(registry, "get_provider", lambda slug: provider)
    monkeypatch.setattr(
        crew_oauth,
        "_client",
        lambda slug: types.SimpleNamespace(client_id="Iv1.abc", client_secret=CANARY_SECRET),
    )
    sent: list[dict] = []

    async def _post(slug, form):
        sent.append(form)
        return {"access_token": "a1", "refresh_token": "r1", "expires_in": 28800}

    monkeypatch.setattr(crew_oauth, "_post_token", _post)
    sign_in = crew_oauth.SignIn("github")
    url = await sign_in.start()
    port = sign_in.bound_port
    assert port
    assert url.startswith("https://github.com/login/oauth/authorize?")
    # A distinctive canary: random base64 in the URL can spell any short word.
    assert "code_challenge_method=S256" in url and CANARY_SECRET not in url
    state = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))["state"]
    store = TokenStore(SecretVault(tmp_path))
    finish = asyncio.create_task(sign_in.finish(store))
    async with aiohttp.ClientSession() as http:
        callback = f"http://127.0.0.1:{port}/callback"
        async with http.get(callback, params={"state": "forged", "code": "c"}) as resp:
            assert resp.status == 400
        async with http.get(callback, params={"state": state, "code": "c0de"}) as resp:
            assert resp.status == 200
    await finish
    await sign_in.shutdown()
    assert sent[0]["code"] == "c0de" and sent[0]["client_secret"] == CANARY_SECRET
    assert sent[0]["code_verifier"]
    assert store.read("github")[0].access == "a1"
    assert sign_in._runner is None  # listener closed


# --- approvals --------------------------------------------------------------


def _event(**over):
    base = dict(
        mcp_server_name="github",
        tool_name="get_me",
        tool_call_id="call-1",
        mcp_identity_trusted=True,
        raw_params_trusted=True,
        raw_tool_params={"a": 1},
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _register(session: str = SESSION, version: str = "v1", token: str = "tok") -> None:
    crew_oauth_proxy.register_bridge(
        version=version,
        token=token,
        session_key=session,
        server="github",
        slug="github",
        mcp_url="http://127.0.0.1:1/mcp",
    )


def test_an_approval_mints_one_single_use_call_for_its_arguments():
    _register()
    mint = mediated_request_capability.ApprovalMint(SESSION, _event())
    assert mint.applies
    mint.mint()
    mint.settle(sent=True)
    assert not crew_oauth_proxy.consume_approval(SESSION, "github", "get_me", {"a": 2})
    assert crew_oauth_proxy.consume_approval(SESSION, "github", "get_me", {"a": 1})
    assert not crew_oauth_proxy.consume_approval(SESSION, "github", "get_me", {"a": 1})


def test_an_unsent_approval_and_a_cancelled_turn_leave_nothing():
    _register()
    mint = mediated_request_capability.ApprovalMint(SESSION, _event())
    mint.mint()
    mint.settle(sent=False)
    assert not crew_oauth_proxy.consume_approval(SESSION, "github", "get_me", {"a": 1})
    mint = mediated_request_capability.ApprovalMint(SESSION, _event())
    mint.mint()
    mint.settle(sent=True)
    mediated_request_capability.invalidate_session(SESSION)
    assert not crew_oauth_proxy.consume_approval(SESSION, "github", "get_me", {"a": 1})


def test_no_approval_is_minted_without_a_bridge_or_trusted_identity():
    assert not mediated_request_capability.ApprovalMint(SESSION, _event()).applies
    _register()
    assert crew_oauth_proxy.note_approval(SESSION, _event(mcp_identity_trusted=False)) == ""
    assert crew_oauth_proxy.note_approval("other-session", _event()) == ""


# --- bridge admission -------------------------------------------------------


def test_admission_needs_the_registered_secret_and_pins_one_process():
    _register()
    assert crew_oauth_proxy.admit_bridge("v1", "wrong", 100)[0] is None
    assert crew_oauth_proxy.admit_bridge("nope", "tok", 100)[0] is None
    me = os.getpid()
    assert crew_oauth_proxy.admit_bridge("v1", "tok", me)[0] is not None
    bridge, reason = crew_oauth_proxy.admit_bridge("v1", "tok", me + 1)
    assert bridge is None and "pinned" in reason


def test_a_pin_moves_only_after_the_pinned_process_is_gone(monkeypatch):
    _register()
    assert crew_oauth_proxy.admit_bridge("v1", "tok", 4242)[0] is not None
    monkeypatch.setattr(crew_oauth_proxy, "_pid_alive", lambda pid: False)
    assert crew_oauth_proxy.admit_bridge("v1", "tok", 4343)[0] is not None


def test_disconnect_drops_every_bridge_of_the_slug():
    _register()
    crew_oauth_proxy.drop_slug("github")
    assert crew_oauth_proxy.admit_bridge("v1", "tok", os.getpid())[0] is None
    assert not crew_oauth_proxy.is_connection_server(SESSION, "github")


# --- the proxy route --------------------------------------------------------


def _upstream_app(seen: list, statuses: list[int]) -> web.Application:
    async def _mcp(request):
        seen.append(dict(request.headers))
        status = statuses.pop(0) if statuses else 200
        if status != 200:
            return web.Response(status=status)
        body = await request.json()
        return web.json_response(
            {"jsonrpc": "2.0", "id": body["id"], "result": {"ok": True}},
            headers={"Mcp-Session-Id": "s-1"},
        )

    app = web.Application()
    app.router.add_post("/mcp", _mcp)
    return app


def _proxy_app(internal: bool = True) -> web.Application:
    @web.middleware
    async def _auth(request, handler):
        request["internal_auth"] = internal
        return await handler(request)

    # The dashboard app allows 60 MB; give this stand-in room above the proxy cap.
    app = web.Application(middlewares=[_auth], client_max_size=8 * 1024 * 1024)
    app.router.add_post(crew_oauth_proxy.PROXY_PATH, crew_oauth_proxy.handle)
    return app


@pytest.fixture
def proxy_env(monkeypatch):
    import kiro_crew.dashboard.origin as origin
    import kiro_crew.mcp_gateway.socketsec as socketsec

    monkeypatch.setattr(origin, "request_is_unix_socket", lambda request: True)
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda sock: os.getpid())
    monkeypatch.setattr(crew_oauth, "manages", lambda slug: True)
    forced: list[bool] = []

    async def _token(slug, *, force=False, store=None):
        forced.append(force)
        return "AT-2" if force else "AT-1"

    monkeypatch.setattr(crew_oauth, "access_token", _token)
    return forced


async def _post(client, body, token="tok"):
    return await client.post(
        crew_oauth_proxy.PROXY_PATH,
        json=body,
        headers={crew_oauth_proxy.BRIDGE_HEADER: "v1", crew_oauth_proxy.BRIDGE_TOKEN_HEADER: token},
    )


@pytest.mark.asyncio
async def test_the_proxy_injects_the_bearer_and_gates_tool_calls(proxy_env):
    seen: list = []
    async with TestServer(_upstream_app(seen, [])) as upstream:
        crew_oauth_proxy.register_bridge(
            version="v1",
            token="tok",
            session_key=SESSION,
            server="github",
            slug="github",
            mcp_url=str(upstream.make_url("/mcp")),
        )
        async with TestClient(TestServer(_proxy_app())) as client:
            resp = await _post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert resp.status == 200 and resp.headers["Mcp-Session-Id"] == "s-1"
            assert seen[-1]["Authorization"] == "Bearer AT-1"
            call = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_me", "arguments": {"a": 1}},
            }
            refused = await (await _post(client, call)).json()
            assert refused["error"]["code"] == -32000 and len(seen) == 1
            crew_oauth_proxy.note_approval(SESSION, _event())
            ok = await (await _post(client, call)).json()
            assert ok["result"] == {"ok": True} and len(seen) == 2
            denied = await _post(client, call, token="bad")
            assert denied.status == 403
    assert "Authorization" not in json.dumps(dict(resp.headers))


@pytest.mark.asyncio
async def test_a_vendor_401_refreshes_once_and_never_reaches_the_harness(proxy_env):
    seen: list = []
    async with TestServer(_upstream_app(seen, [401, 401])) as upstream:
        crew_oauth_proxy.register_bridge(
            version="v1",
            token="tok",
            session_key=SESSION,
            server="github",
            slug="github",
            mcp_url=str(upstream.make_url("/mcp")),
        )
        async with TestClient(TestServer(_proxy_app())) as client:
            resp = await _post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert resp.status == 409
            assert resp.headers[crew_oauth_proxy.REFUSAL_HEADER] == "needs_auth"
    assert proxy_env == [False, True]
    assert [h["Authorization"] for h in seen] == ["Bearer AT-1", "Bearer AT-2"]


@pytest.mark.asyncio
async def test_the_proxy_refuses_a_caller_without_internal_authority(proxy_env):
    _register()
    async with TestClient(TestServer(_proxy_app(internal=False))) as client:
        resp = await _post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert resp.status == 403


# --- the in-session bridge and launcher -------------------------------------


def test_the_bridge_reports_the_gateway_refusal_text():
    import io

    bridge = ConnectionBridge("http://x/", {}, server="github", out=io.BytesIO(), err=io.StringIO())
    refusal = {crew_oauth_proxy.REFUSAL_HEADER: "needs_auth"}
    exc = urllib.error.HTTPError(
        "http://x/", 409, "Conflict", refusal, io.BytesIO(b'{"error": "not connected"}')
    )
    assert bridge._http_error_reason(exc) == "was refused by Kiro Crew: not connected"


def test_a_vendor_status_is_reported_as_the_providers_and_never_quoted():
    import io

    from kiro_crew import kas_connection_bridge

    assert kas_connection_bridge.REFUSAL_HEADER == crew_oauth_proxy.REFUSAL_HEADER
    bridge = ConnectionBridge("http://x/", {}, server="github", out=io.BytesIO(), err=io.StringIO())
    for status in (403, 429, 503):
        exc = urllib.error.HTTPError(
            "http://x/", status, "x", {}, io.BytesIO(b'{"error": "vendor text"}')
        )
        reason = bridge._http_error_reason(exc)
        assert reason == f"answered HTTP {status} from the provider"
        assert "Kiro Crew" not in reason and "vendor text" not in reason


def _connection_snapshot(home: Path, monkeypatch) -> list[str]:
    monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")
    entry = kas_env_launch.relay_crew_connection(
        {"url": GITHUB_URL},
        session_key=SESSION,
        agent_id="kirocrew",
        server="github",
        slug="github",
        port="8123",
    )
    return entry["args"][entry["args"].index(kas_env_shim.SHIM_MODULE) + 1 :]


@pytest.mark.skipif(os.name == "nt", reason="POSIX arm")
def test_the_launcher_runs_the_gateway_bridge_on_posix(home, monkeypatch):
    argv = _connection_snapshot(home, monkeypatch)
    ran: list[dict] = []
    import kiro_crew.kas_connection_bridge as bridge_mod

    monkeypatch.setattr(
        bridge_mod, "run_connection_bridge", lambda doc, server: ran.append(doc) or 0
    )
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    with pytest.raises(SystemExit) as done:
        kas_env_shim.main(argv)
    assert done.value.code == 0 and ran[0]["connection"] == "github"


def test_the_launcher_refuses_a_connection_on_windows(home, monkeypatch, capsys):
    argv = _connection_snapshot(home, monkeypatch)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(kas_env_shim.os, "name", "nt")
    with pytest.raises(SystemExit) as done:
        kas_env_shim.main(argv)
    assert done.value.code == 78
    assert "Unix socket" in capsys.readouterr().err


# --- custody, warm process, gate edges --------------------------------------


def test_crew_custody_needs_kas_as_the_chat_backend(monkeypatch):
    """On kiro-cli the native grant keeps working, so Crew does not take custody."""
    monkeypatch.setattr(crew_oauth, "enabled", lambda: True)
    monkeypatch.setattr(crew_oauth, "_chat_backend_is_kas", lambda: False)
    assert crew_oauth.manages("github") is False
    monkeypatch.setattr(crew_oauth, "_chat_backend_is_kas", lambda: True)
    assert crew_oauth.manages("github") is True


def test_a_stored_pair_is_found_whatever_the_flag_says(tmp_path, monkeypatch):
    """Disconnect cleans a pair stored while the flag was on, after it is turned off."""
    vault = SecretVault(tmp_path)
    monkeypatch.setattr(crew_oauth, "TokenStore", lambda: TokenStore(vault))
    monkeypatch.setattr(crew_oauth, "enabled", lambda: False)
    assert crew_oauth.holds_grant("github") is False
    TokenStore(vault).replace("github", Tokens("a", "r", None, ""))
    assert crew_oauth.holds_grant("github") is True
    assert crew_oauth.holds_grant("asana") is False


def test_the_warm_universe_leaves_a_crew_owned_provider_out(monkeypatch):
    """The shared native process must not hold the callback port Crew binds."""
    from kiro_crew.connections import warm

    monkeypatch.setattr(warm, "_disabled_provider_slugs", lambda: set())
    monkeypatch.setattr(warm, "_warm_mintable_entry", lambda provider, _x: {})
    monkeypatch.setattr(crew_oauth, "manages", lambda slug: True)
    assert "github" not in {p["slug"] for p in warm.warm_spec_providers()}
    monkeypatch.setattr(crew_oauth, "manages", lambda slug: False)
    assert "github" in {p["slug"] for p in warm.warm_spec_providers()}


def test_a_full_table_refuses_a_new_bridge_and_keeps_the_live_one(monkeypatch):
    monkeypatch.setattr(crew_oauth_proxy, "_MAX_BRIDGES", 1)
    _register(session="s-1", version="v1")
    _register(session="s-2", version="v2")
    assert crew_oauth_proxy.is_connection_server("s-1", "github")
    assert not crew_oauth_proxy.is_connection_server("s-2", "github")


def test_a_full_table_makes_the_projected_bridge_refuse_to_start(home, monkeypatch):
    monkeypatch.setattr(crew_oauth_proxy, "_MAX_BRIDGES", 0)
    entry = kas_env_launch.relay_crew_connection(
        {"url": GITHUB_URL},
        session_key=SESSION,
        agent_id="kirocrew",
        server="github",
        slug="github",
        port="8123",
    )
    assert "unwritten" in entry["args"]
    leaf = home / "kas-mcp-launch"
    assert not leaf.exists() or not list(leaf.iterdir())


def test_discarding_a_batch_unregisters_its_bridges(home, monkeypatch):
    out = _project(monkeypatch, on=True)
    assert crew_oauth_proxy.is_connection_server(SESSION, "github")
    kas_env_launch.discard_batch([out])
    assert not crew_oauth_proxy.is_connection_server(SESSION, "github")


def test_a_refused_batch_spends_no_approval():
    """An approved call beside a refused method: nothing is sent, so nothing is spent."""
    _register()
    crew_oauth_proxy.note_approval(SESSION, _event())
    bridge = crew_oauth_proxy._bridges["v1"]
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "get_me", "arguments": {"a": 1}},
    }
    refused, _errors = crew_oauth_proxy.gate_calls(
        bridge, [call, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}]
    )
    assert refused and len(crew_oauth_proxy._approvals) == 1
    assert crew_oauth_proxy.gate_calls(bridge, call) == (False, [])
    assert not crew_oauth_proxy._approvals


def test_disconnect_revokes_only_crews_own_token(tmp_path, monkeypatch):
    assert crew_oauth._ENDPOINTS["github"]["revoke"].endswith("/applications/{client_id}/token")


@pytest.mark.asyncio
async def test_an_unreachable_token_endpoint_is_a_coded_failure(monkeypatch):
    import aiohttp

    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise aiohttp.ClientConnectionError("down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(aiohttp, "ClientSession", _Boom)
    with pytest.raises(CrewOAuthError) as err:
        await crew_oauth._post_token("github", {})
    assert err.value.code == "token_unreachable"


@pytest.mark.asyncio
async def test_a_refused_call_without_an_id_is_never_forwarded(proxy_env):
    seen: list = []
    async with TestServer(_upstream_app(seen, [])) as upstream:
        crew_oauth_proxy.register_bridge(
            version="v1",
            token="tok",
            session_key=SESSION,
            server="github",
            slug="github",
            mcp_url=str(upstream.make_url("/mcp")),
        )
        async with TestClient(TestServer(_proxy_app())) as client:
            for body in (
                {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "get_me"}},
                {"jsonrpc": "2.0", "id": None, "method": "tools/call", "params": {"name": "x"}},
            ):
                resp = await _post(client, body)
                assert resp.status == 202
    assert seen == []


@pytest.mark.asyncio
async def test_a_large_approved_call_is_read_whole_before_it_is_judged(proxy_env):
    seen: list = []
    big = {"blob": "x" * (3 * 1024 * 1024)}
    upstream_app = _upstream_app(seen, [])
    upstream_app._client_max_size = 8 * 1024 * 1024
    async with TestServer(upstream_app) as upstream:
        crew_oauth_proxy.register_bridge(
            version="v1",
            token="tok",
            session_key=SESSION,
            server="github",
            slug="github",
            mcp_url=str(upstream.make_url("/mcp")),
        )
        crew_oauth_proxy.note_approval(SESSION, _event(raw_tool_params=big))
        async with TestClient(TestServer(_proxy_app())) as client:
            call = {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "get_me", "arguments": big},
            }
            answer = await (await _post(client, call)).json()
    assert answer["result"] == {"ok": True} and len(seen) == 1


def test_only_setup_and_listing_pass_without_an_approval():
    """Deny by default: resources, prompts and completion never reach the vendor."""
    _register()
    bridge = crew_oauth_proxy._bridges["v1"]
    for method in ("initialize", "ping", "tools/list", "notifications/initialized"):
        assert crew_oauth_proxy.gate_calls(
            bridge, {"jsonrpc": "2.0", "id": 1, "method": method}
        ) == (
            False,
            [],
        )
    assert crew_oauth_proxy.gate_calls(bridge, {"jsonrpc": "2.0", "id": 1, "result": {}}) == (
        False,
        [],
    )
    for method in ("resources/read", "prompts/get", "completion/complete", "resources/list"):
        refused, errors = crew_oauth_proxy.gate_calls(
            bridge, {"jsonrpc": "2.0", "id": 9, "method": method, "params": {}}
        )
        assert refused and errors[0]["id"] == 9


def test_a_tool_search_envelope_binds_the_inner_arguments():
    _register()
    envelope = {"tool_id": "github::get_me", "arguments": {"a": 1}}
    crew_oauth_proxy.note_approval(SESSION, _event(raw_tool_params=envelope))
    assert crew_oauth_proxy.consume_approval(SESSION, "github", "get_me", {"a": 1})


def test_an_envelope_naming_another_tool_is_not_unwrapped():
    _register()
    envelope = {"tool_id": "github::delete_repo", "arguments": {"a": 1}}
    crew_oauth_proxy.note_approval(SESSION, _event(raw_tool_params=envelope))
    assert not crew_oauth_proxy.consume_approval(SESSION, "github", "get_me", {"a": 1})


@pytest.mark.asyncio
async def test_shutdown_waits_for_a_token_write_already_started(tmp_path, monkeypatch):
    """A Disconnect that ends the sign-in reads the vault only after the write lands."""
    import threading

    gate = threading.Event()
    stored: list[str] = []

    class _SlowStore:
        def replace(self, slug, tokens):
            gate.wait(10)
            stored.append(tokens.access)

    monkeypatch.setattr(
        crew_oauth,
        "_client",
        lambda slug: types.SimpleNamespace(client_id="id", client_secret="sec"),
    )

    async def _post(slug, form):
        return {"access_token": "a1", "refresh_token": "r1"}

    monkeypatch.setattr(crew_oauth, "_post_token", _post)
    sign_in = crew_oauth.SignIn("github")
    sign_in._client = types.SimpleNamespace(client_id="id", client_secret="sec")
    sign_in._redirect = "http://127.0.0.1:1/callback"
    sign_in._done.set_result("code")
    finish = asyncio.create_task(sign_in.finish(_SlowStore()))
    await async_wait_until(lambda: sign_in._write is not None)
    finish.cancel()
    closing = asyncio.create_task(sign_in.shutdown())
    await asyncio.sleep(0)
    assert not closing.done()  # still draining the write
    gate.set()
    await closing
    assert stored == ["a1"]


@pytest.mark.parametrize(
    "raw",
    [
        {"a": 1, "_meta": {"kiro": {"validation": "x"}}},
        {"tool_id": "github::get_me", "arguments": {"a": 1}, "_meta": {"k": 1}},
        {"tool_id": "github::get_me", "arguments": {"a": 1, "_meta": {"k": 1}}},
    ],
)
def test_kas_transport_metadata_never_breaks_an_approved_call(raw):
    """KAS ``_meta`` rides beside the arguments; the forwarded call carries only them."""
    _register()
    crew_oauth_proxy.note_approval(SESSION, _event(raw_tool_params=raw))
    assert crew_oauth_proxy.consume_approval(SESSION, "github", "get_me", {"a": 1})


def test_an_entry_with_its_own_auth_header_keeps_its_header_bridge(home, monkeypatch):
    """A personal-access-token GitHub entry never borrows Crew's sign-in."""
    monkeypatch.setattr(crew_oauth, "enabled", lambda: True)
    monkeypatch.setattr(crew_oauth, "_chat_backend_is_kas", lambda: True)
    monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")
    spec = _github_spec()
    spec["mcpServers"]["github"]["headers"] = {"Authorization": "Bearer ghp-canary-pat"}
    out = kas_agents.to_client_custom_agent("kirocrew", spec, "p", session_key=SESSION)
    assert kas_env_launch.crew_connection_slug("github", spec["mcpServers"]["github"]) == ""
    assert not crew_oauth_proxy._bridges
    asks = [r for r in (out.get("permissions") or {}).get("rules", []) if r["effect"] == "ask"]
    assert {"capability": "mcp", "match": ["github/*"], "effect": "ask"} not in asks
    names = kas_env_launch.batch_snapshot_names([out])
    doc = json.loads((home / "kas-mcp-launch" / names[0]).read_text())
    assert doc["url"] == GITHUB_URL and doc["headers"]["Authorization"].endswith("canary-pat")
    assert "connection" not in doc


def test_an_approval_without_trusted_arguments_authorizes_no_arguments_at_all():
    """Fail closed: an untrusted-argument approval is no wildcard for the tool."""
    _register()
    crew_oauth_proxy.note_approval(
        SESSION, _event(tool_name="create_issue", raw_params_trusted=False, raw_tool_params=None)
    )
    assert not crew_oauth_proxy.consume_approval(
        SESSION, "github", "create_issue", {"title": "not what was approved"}
    )
    assert crew_oauth_proxy.consume_approval(SESSION, "github", "create_issue", None)
    assert not crew_oauth_proxy.consume_approval(SESSION, "github", "create_issue", None)


@pytest.mark.parametrize(
    ("headers", "crew"),
    [
        ({"User-Agent": "my-client"}, True),
        ({"Accept-Language": "en"}, True),
        ({"Authorization": "Bearer ghp-x"}, False),
        ({"X-Api-Key": "k"}, False),
        ({"X-GitHub-Token": "t"}, False),
        ({"Cookie": "s=1"}, False),
    ],
)
def test_only_a_credential_header_keeps_an_entry_off_crew_auth(monkeypatch, headers, crew):
    monkeypatch.setattr(crew_oauth, "enabled", lambda: True)
    monkeypatch.setattr(crew_oauth, "_chat_backend_is_kas", lambda: True)
    entry = {"url": GITHUB_URL, "headers": headers}
    assert (kas_env_launch.crew_connection_slug("github", entry) == "github") is crew


def test_a_session_outside_the_gateway_keeps_the_native_path(monkeypatch):
    """``kirocrew chat`` projects in its own process: no proxy there, so no bridge."""
    monkeypatch.setattr(crew_oauth, "manages", lambda slug: True)
    entry = {"url": GITHUB_URL}
    monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
    assert kas_env_launch.crew_connection_slug("github", entry) == ""
    monkeypatch.setenv("KIROCREW_BOUND_PORT", "8765")
    assert kas_env_launch.crew_connection_slug("github", entry) == "github"


@pytest.mark.asyncio
async def test_the_proxy_forwards_the_message_the_gate_judged(proxy_env):
    """Duplicate keys: the vendor gets the parsed message, never the raw bytes."""
    raw: list[bytes] = []

    async def _mcp(request):
        raw.append(await request.read())
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": {}})

    upstream_app = web.Application()
    upstream_app.router.add_post("/mcp", _mcp)
    async with TestServer(upstream_app) as upstream:
        crew_oauth_proxy.register_bridge(
            version="v1",
            token="tok",
            session_key=SESSION,
            server="github",
            slug="github",
            mcp_url=str(upstream.make_url("/mcp")),
        )
        body = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","method":"tools/list"}'
        async with TestClient(TestServer(_proxy_app())) as client:
            resp = await client.post(
                crew_oauth_proxy.PROXY_PATH,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    crew_oauth_proxy.BRIDGE_HEADER: "v1",
                    crew_oauth_proxy.BRIDGE_TOKEN_HEADER: "tok",
                },
            )
            assert resp.status == 200
    assert raw and b"tools/call" not in raw[0]


def test_a_non_auth_header_entry_is_bridged_and_keeps_its_header(home, monkeypatch):
    """A User-Agent-only entry still gets Crew's bearer; its header rides the gateway."""
    monkeypatch.setattr(crew_oauth, "enabled", lambda: True)
    monkeypatch.setattr(crew_oauth, "_chat_backend_is_kas", lambda: True)
    monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")
    spec = _github_spec()
    spec["mcpServers"]["github"]["headers"] = {"User-Agent": "my-client"}
    out = kas_agents.to_client_custom_agent("kirocrew", spec, "p", session_key=SESSION)
    names = kas_env_launch.batch_snapshot_names([out])
    doc = json.loads((home / "kas-mcp-launch" / names[0]).read_text())
    assert doc["connection"] == "github" and "headers" not in doc
    (bridge,) = crew_oauth_proxy._bridges.values()
    assert bridge.extra_headers == {"User-Agent": "my-client"}


@pytest.mark.asyncio
async def test_the_proxy_sends_the_entry_header_beside_crews_bearer(proxy_env):
    seen: list = []
    async with TestServer(_upstream_app(seen, [])) as upstream:
        crew_oauth_proxy.register_bridge(
            version="v1",
            token="tok",
            session_key=SESSION,
            server="github",
            slug="github",
            mcp_url=str(upstream.make_url("/mcp")),
            headers={"User-Agent": "my-client", "Authorization": "Bearer spoof"},
        )
        async with TestClient(TestServer(_proxy_app())) as client:
            await _post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert seen[-1]["User-Agent"] == "my-client"
    assert seen[-1]["Authorization"] == "Bearer AT-1", "Crew's bearer always wins"

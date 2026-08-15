"""Tests for the MCP Apps UI→tool callback path.

Gateway side (:mod:`kiro_crew.mcp_gateway.app_call`) is tested against the
REAL ``test/fake_mcp_app_server.py`` child process pooled in a real
:class:`BackendPool` — proving spool-token gating, app-visibility enforcement
and the ephemeral-stub forward on live pipes.

Dashboard side (:mod:`kiro_crew.dashboard.handlers.mcp_apps`) is tested with
``aiohttp``'s test client against a scripted unix-socket fake gateway.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import mcp_apps as mcp_apps_handlers
from kiro_crew.mcp_apps_render import load_spool
from kiro_crew.mcp_gateway import apps
from kiro_crew.mcp_gateway.app_call import handle_app_call
from kiro_crew.mcp_gateway.apps import write_spool
from kiro_crew.mcp_gateway.backend import MCP_APPS_ENV_FLAG, Backend
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey

SERVER_PATH = Path(__file__).parent / "fake_mcp_app_server.py"

pytestmark = pytest.mark.asyncio

_UNIX_SOCKET_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the scripted fake gatewayd needs AF_UNIX sockets (gatewayd itself is unix-only)",
)


@pytest.fixture
def spool_tmp(tmp_path, monkeypatch):
    d = tmp_path / "mcp-apps"
    monkeypatch.setenv(apps.SPOOL_ENV, str(d))
    return d


@pytest.fixture
def apps_flag_on(monkeypatch):
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")


def _pool_key(server: str = "fake-mcp-app") -> PoolKey:
    return PoolKey(
        server_name=server,
        agent_name="test-agent",
        command_args_hash="abc123",
        effective_env_hash="def456",
        work_dir="/tmp/test",
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="ghi789",
        approval_mode="reads",
        trust_all_tools=False,
        config_snapshot_hash="jkl012",
    )


def _spool_record(server: str = "fake-mcp-app", pool_digest: str | None = None) -> str:
    """Write a plausible spool record (as interception would) and return its id."""
    if pool_digest is None:
        pool_digest = _pool_key(server).stable_hash()
    return write_spool({
        "server": server,
        "tool": "draw",
        "session_key": "dashboard:sess-cb",
        "pool_digest": pool_digest,
        "html": "<html>app</html>",
        "csp": None,
        "permissions": None,
        "structured_content": None,
    })


def _cbs(spool_id: str):
    """The record's #418 callback capability (minted by write_spool)."""
    rec = load_spool(spool_id) or {}
    return rec.get("callback_secret")


class _LivePool:
    """Real fake-server subprocess pooled in a real BackendPool, handshaken."""

    def __init__(self, pool: BackendPool, backend: Backend, process, pump):
        self.pool = pool
        self.backend = backend
        self.process = process
        self.pump = pump

    async def aclose(self):
        self.pump.cancel()
        try:
            await self.pump
        except (asyncio.CancelledError, Exception):
            pass
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()


async def _spawn_pooled_server() -> _LivePool:
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(SERVER_PATH),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    now = time.monotonic()
    key = _pool_key()
    backend = Backend(
        pool_key=key,
        process=process,
        stdin=process.stdin,
        stdout=process.stdout,
        created_at=now,
        last_used_at=now,
    )
    pump = asyncio.get_running_loop().create_task(backend.run_stdout_pump())
    pool = BackendPool(max_backends=4)
    await pool.add(key, backend)

    # Handshake once through a normal stub so _init_state is "ready" — the
    # state a pooled backend that already served an app is guaranteed to be in.
    inbox = await backend.attach_stub("chat-stub")
    await backend.forward_from_stub("chat-stub", {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "kiro-cli", "version": "0"}},
    })
    await asyncio.wait_for(inbox.get(), timeout=10)
    return _LivePool(pool, backend, process, pump)


# --------------------------------------------------------------------------
# Gateway side: handle_app_call against the real fake server
# --------------------------------------------------------------------------

async def test_app_call_happy_path(apps_flag_on, spool_tmp):
    """save_state (visibility ["app"]) is callable via a valid spool token."""
    live = await _spawn_pooled_server()
    try:
        spool_id = _spool_record()
        reply = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
            "tool": "save_state", "arguments": {},
        })
        assert reply["type"] == "app-result", reply
        assert reply["result"]["content"][0]["text"] == "state saved"
    finally:
        await live.aclose()


async def test_app_call_cancellation_is_audited(apps_flag_on, spool_tmp, monkeypatch):
    """GPT 5.6 finding: a cancellation AFTER the tool is dispatched (e.g.
    SIGTERM during a callback that outlives the shutdown drain) must still
    leave a SEL record. The CancelledError is audited "cancelled" and
    re-raised — never silently swallowed (it is a BaseException, so the
    handler's `except Exception` never catches it)."""
    import kiro_crew.mcp_gateway.app_call as app_call_mod

    audits: list[tuple[str, str]] = []
    monkeypatch.setattr(
        app_call_mod, "_audit",
        lambda outcome, reason, **kw: audits.append((outcome, reason)),
    )

    # Cancel ONLY the forwarded tools/call; let the preamble tools/list
    # roundtrip proceed against the real pooled server so we reach the guarded
    # dispatch path (both use the module-level _roundtrip).
    orig_roundtrip = app_call_mod._roundtrip

    async def _cancel(backend, frame, *a, **k):
        if isinstance(frame, dict) and frame.get("method") == "tools/call":
            raise asyncio.CancelledError()
        return await orig_roundtrip(backend, frame, *a, **k)

    monkeypatch.setattr(app_call_mod, "_roundtrip", _cancel)

    live = await _spawn_pooled_server()
    try:
        spool_id = _spool_record()
        with pytest.raises(asyncio.CancelledError):
            await handle_app_call(live.pool, {
                "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
                "tool": "save_state", "arguments": {},
            })
        assert any(o == "cancelled" for o, _ in audits), audits
    finally:
        await live.aclose()


async def test_app_call_model_and_app_tool_allowed(apps_flag_on, spool_tmp):
    """draw (visibility ["model","app"]) is also app-callable."""
    live = await _spawn_pooled_server()
    try:
        spool_id = _spool_record()
        reply = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
            "tool": "draw", "arguments": {},
        })
        assert reply["type"] == "app-result", reply
        assert "drew a thing" in reply["result"]["content"][0]["text"]
    finally:
        await live.aclose()


async def test_app_call_denies_non_app_tool(apps_flag_on, spool_tmp):
    """A tool absent from tools/list (or without app visibility) is refused
    without ever reaching the backend's tools/call."""
    live = await _spawn_pooled_server()
    try:
        spool_id = _spool_record()
        reply = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
            "tool": "no_such_tool", "arguments": {},
        })
        assert reply["type"] == "app-call-rejected"
        assert reply["reason"] == "tool not app-visible"
    finally:
        await live.aclose()


async def test_app_call_rejects_schema_violating_arguments(apps_flag_on, spool_tmp):
    """Iframe-controlled arguments are validated against the tool's declared
    inputSchema BEFORE forwarding (fail-closed). The fake server's tools
    declare `{"type": "object", "properties": {}}`, so any unexpected key
    must be rejected gateway-side — the backend never sees the call."""
    live = await _spawn_pooled_server()
    try:
        spool_id = _spool_record()
        reply = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
            "tool": "save_state", "arguments": {"smuggled": "../../etc/passwd"},
        })
        assert reply["type"] == "app-call-rejected"
        assert "schema validation" in reply["reason"]
        # The compliant empty call still works on the same backend.
        ok = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
            "tool": "save_state", "arguments": {},
        })
        assert ok["type"] == "app-result"
    finally:
        await live.aclose()


async def test_app_call_denied_by_governance_mcp_scope(apps_flag_on, spool_tmp, tmp_path, monkeypatch):
    """An enterprise `mcp`-scope deny on @server/tool binds the app-originated
    path exactly like the model path (single ceiling across both invocation
    authorities). Policy loaded via the real KIROCREW_SECURITY_POLICY seam."""
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "version": 1,
        "boot": {"fail_closed": True},
        "mcp": {"mode": "deny", "deny": ["@fake-mcp-app/save_state"]},
    }))
    monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(policy))
    live = await _spawn_pooled_server()
    try:
        spool_id = _spool_record()
        reply = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
            "tool": "save_state", "arguments": {},
        })
        assert reply["type"] == "app-call-rejected"
        assert "governance" in reply["reason"]
        # A tool the policy does not deny still works under the same ceiling.
        ok = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
            "tool": "draw", "arguments": {},
        })
        assert ok["type"] == "app-result"
    finally:
        await live.aclose()


async def test_app_call_denied_when_the_reloaded_policy_signature_is_bad(
    apps_flag_on, spool_tmp, tmp_path, monkeypatch
):
    """A policy TAMPERED after boot must not widen an app callback.

    This path re-reads the policy per call for freshness, so boot's verification of
    the original bytes says nothing about what it loads now. Under a genuine
    ``require_policy_signature`` opt-in a non-verified reload denies.
    """
    from kiro_crew.platform.admission import hmac_signature
    from kiro_crew.platform.governance import policy_signing_payload

    adm = tmp_path / "admission_policy.json"
    adm.write_text(json.dumps({
        "require_policy_signature": True, "trust_keys": {"fleet": "k"},
    }))
    monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
    body = {
        "version": 1,
        "boot": {"fail_closed": True},
        "mcp": {"mode": "deny", "deny": []},
        "identity": {"issuer": "fleet"},
    }
    body["identity"]["signature"] = hmac_signature("k", policy_signing_payload(body))
    body["mcp"]["deny"].append("@never/matched")  # tampered AFTER signing
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(body))
    monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(policy))
    live = await _spawn_pooled_server()
    try:
        spool_id = _spool_record()
        reply = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
            # A tool the (tampered) policy does NOT deny — so a pass here would be a
            # real bypass, not an incidental allowlist miss.
            "tool": "draw", "arguments": {},
        })
        assert reply["type"] == "app-call-rejected"
        assert "signature" in reply["reason"]
    finally:
        await live.aclose()


async def test_app_call_is_not_denied_when_no_policy_is_readable_here(
    apps_flag_on, spool_tmp, tmp_path, monkeypatch
):
    """A bundle-only enterprise host must not have every callback denied.

    gatewayd is not the composition process — it never runs ``boot_platform``, so it
    loads the policy with no ``bundled_loader`` and cannot see a companion-bundled
    ceiling. ``None`` here is the NORMAL result on such a host, not evidence the
    policy is gone, so the signature gate must not fire on it.
    """
    adm = tmp_path / "admission_policy.json"
    adm.write_text(json.dumps({
        "require_policy_signature": True, "trust_keys": {"fleet": "k"},
    }))
    monkeypatch.setenv("KIROCREW_ADMISSION_POLICY", str(adm))
    monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
    monkeypatch.setattr(
        "kiro_crew.platform.governance._policy_home_path", lambda: tmp_path / "nope.json"
    )
    live = await _spawn_pooled_server()
    try:
        spool_id = _spool_record()
        reply = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
            "tool": "draw", "arguments": {},
        })
        assert reply["type"] == "app-result"
    finally:
        await live.aclose()


async def test_app_call_governance_evaluation_error_fails_closed(apps_flag_on, spool_tmp, tmp_path, monkeypatch):
    """A broken governance load DENIES the app call (opposite polarity from
    Plane A's soft fail-open — this path has no always-on deny floor)."""
    policy = tmp_path / "broken.json"
    policy.write_text("{not json")
    monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(policy))
    live = await _spawn_pooled_server()
    try:
        spool_id = _spool_record()
        reply = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
            "tool": "save_state", "arguments": {},
        })
        assert reply["type"] == "app-call-rejected"
        assert "governance" in reply["reason"]
    finally:
        await live.aclose()


async def test_app_call_rejects_unknown_spool_id(apps_flag_on, spool_tmp):
    """No spool record — the capability token is the whole gate."""
    live = await _spawn_pooled_server()
    try:
        reply = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": "0" * 32,
            "tool": "save_state", "arguments": {},
        })
        assert reply["type"] == "app-call-rejected"
        assert "unknown or expired" in reply["reason"]
    finally:
        await live.aclose()


async def test_app_call_rejects_when_no_backend(apps_flag_on, spool_tmp):
    """A valid token whose PRODUCING backend is gone is refused — no fallback
    to another backend for the same server."""
    pool = BackendPool(max_backends=2)
    spool_id = _spool_record(server="gone-server")
    reply = await handle_app_call(pool, {
        "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
        "tool": "save_state", "arguments": {},
    })
    assert reply["type"] == "app-call-rejected"
    assert "producing backend no longer available" in reply["reason"]


async def test_app_call_rejects_record_without_digest(apps_flag_on, spool_tmp):
    """A record lacking the pool_digest binding (pre-binding or tampered) is a
    plain deny — never resolved by server name."""
    live = await _spawn_pooled_server()
    try:
        spool_id = write_spool({
            "server": "fake-mcp-app", "tool": "draw",
            "session_key": "dashboard:sess-cb",
            "html": "<html>app</html>", "csp": None,
            "permissions": None, "structured_content": None,
        })
        reply = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
            "tool": "save_state", "arguments": {},
        })
        assert reply["type"] == "app-call-rejected"
        assert "no backend binding" in reply["reason"]
    finally:
        await live.aclose()


async def test_app_call_rejects_malformed_frames(apps_flag_on, spool_tmp):
    pool = BackendPool(max_backends=2)
    for frame, want in (
        ({"type": "app-call"}, "missing spool_id"),
        ({"type": "app-call", "spool_id": "a" * 32}, "missing tool"),
        ({"type": "app-call", "spool_id": "a" * 32, "tool": "t",
          "arguments": "nope"}, "arguments must be an object"),
    ):
        reply = await handle_app_call(pool, frame)
        assert reply["type"] == "app-call-rejected"
        assert reply["reason"] == want


async def test_app_call_allows_tool_with_no_declared_visibility(
    apps_flag_on, spool_tmp
):
    """The bug this PR fixes, against the real fake server.

    ``visibility`` is optional and SEP-1865 defaults it to ``["model", "app"]``,
    so a server that declares nothing must still accept an app callback. The old
    gate denied on absence, which broke the spec's Interactive Updates pattern
    (a rendered app calling a tool to refresh itself) for every default-visibility
    server — apps rendered but their controls were inert.
    """
    live = await _spawn_pooled_server()
    try:
        spool_id = _spool_record()
        frame = {"type": "app-call", "spool_id": spool_id,
                 "callback_secret": _cbs(spool_id),
                 "tool": "refresh", "arguments": {}}
        reply = await handle_app_call(live.pool, frame)
        assert reply["type"] == "app-result", reply
        assert reply["result"]["content"][0]["text"] == "refreshed"
    finally:
        await live.aclose()


async def test_app_call_still_denies_model_only_tool(apps_flag_on, spool_tmp):
    """Loosening the DEFAULT must not loosen an explicit exclusion."""
    live = await _spawn_pooled_server()
    try:
        from kiro_crew.mcp_gateway import app_call as app_call_mod

        async def _model_only(backend, **_):
            return {"secret": {"name": "secret",
                               "inputSchema": {"type": "object", "properties": {}},
                               "_meta": {"ui": {"visibility": ["model"]}}}}

        spool_id = _spool_record()
        frame = {"type": "app-call", "spool_id": spool_id,
                 "callback_secret": _cbs(spool_id),
                 "tool": "secret", "arguments": {}}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(app_call_mod, "_tools_by_name", _model_only)
            reply = await handle_app_call(live.pool, frame)
        assert reply["type"] == "app-call-rejected"
        assert reply["reason"] == "tool not app-visible"
    finally:
        await live.aclose()


async def test_app_call_denies_a_malformed_declaration(apps_flag_on, spool_tmp):
    """An unreadable declaration must deny on the APP path too.

    The parser bucketed this correctly all along, but nothing drove a malformed
    declaration through ``handle_app_call``, so the DELEGATION was unpinned: a
    fail-open of the shape ``v.allowed or v.unreadable`` passed the whole suite.
    A container this host cannot read may be hiding an exclusion, so admitting
    it would authorize a call the server may have meant to refuse.
    """
    live = await _spawn_pooled_server()
    try:
        from kiro_crew.mcp_gateway import app_call as app_call_mod

        async def _unreadable(backend, **_):
            return {"secret": {"name": "secret",
                               "inputSchema": {"type": "object", "properties": {}},
                               "_meta": {"ui": "bad"}}}

        spool_id = _spool_record()
        frame = {"type": "app-call", "spool_id": spool_id,
                 "callback_secret": _cbs(spool_id),
                 "tool": "secret", "arguments": {}}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(app_call_mod, "_tools_by_name", _unreadable)
            reply = await handle_app_call(live.pool, frame)
        assert reply["type"] == "app-call-rejected"
        assert reply["reason"] == "tool not app-visible"
    finally:
        await live.aclose()


async def test_app_call_tools_list_is_fetched_fresh_per_call(apps_flag_on, spool_tmp, monkeypatch):
    """Authorization input is never cached: each app call re-fetches
    tools/list, so a server-side visibility revocation takes effect on the
    very next call (no stale-authorization window)."""
    from kiro_crew.mcp_gateway import app_call as app_call_mod

    live = await _spawn_pooled_server()
    try:
        spool_id = _spool_record()
        frame = {"type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
                 "tool": "save_state", "arguments": {}}
        r1 = await handle_app_call(live.pool, frame)
        assert r1["type"] == "app-result"
        # Simulate the server revoking app visibility. The revocation must be an
        # explicit declaration that EXCLUDES "app" — merely dropping the
        # visibility key is not a revocation, because SEP-1865 defaults an
        # undeclared tool to ["model", "app"].

        async def _revoked(backend, **_):
            return {"save_state": {"name": "save_state",
                                   "inputSchema": {"type": "object", "properties": {}},
                                   "_meta": {"ui": {"visibility": ["model"]}}}}

        monkeypatch.setattr(app_call_mod, "_tools_by_name", _revoked)
        r2 = await handle_app_call(live.pool, frame)
        assert r2["type"] == "app-call-rejected"
        assert r2["reason"] == "tool not app-visible"
    finally:
        await live.aclose()


# --------------------------------------------------------------------------
# Dashboard side: POST /api/mcp-apps/call relay
# --------------------------------------------------------------------------

async def _fake_gateway(sock_path: Path, reply: dict):
    """One-shot unix-socket server that records the received frame."""
    received: list[dict] = []

    async def _handle(reader, writer):
        line = await reader.readline()
        received.append(json.loads(line.decode("utf-8")))
        writer.write((json.dumps(reply) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(_handle, path=str(sock_path))
    return server, received


#: The session the test spool records are bound to (see _spool_record) — the
#: relay endpoint refuses callers that do not present the owning session.
_OWNER_HDR = {"X-Session-Key": "dashboard:sess-cb"}


async def test_relay_rejects_non_owner_and_app_tokens(relay_env, spool_tmp):
    """The endpoint's load-bearing gate is the AUTHENTICATED caller identity:
    app-scoped tokens and non-owner subjects are refused regardless of any
    client-set header (GPT 5.6 finding: X-Session-Key alone is spoofable)."""
    make_client, _sock = relay_env
    spool_id = _spool_record()
    async with make_client() as client:
        for hdrs in (
            {"X-Test-User": "someone-else", **_OWNER_HDR},   # non-owner subject
            {"X-Test-App": "some-app", **_OWNER_HDR},        # app-scoped token
        ):
            resp = await client.post(
                "/api/mcp-apps/call", headers=hdrs,
                json={"spool_id": spool_id, "tool": "save_state", "arguments": {}},
            )
            assert resp.status == 403
            body = await resp.json()
            assert "owner authorization" in body["error"]


async def test_relay_rejects_restricted_session(relay_env, spool_tmp):
    """Incognito/guest sessions cannot drive app tool execution."""
    make_client, _sock = relay_env
    spool_id = _spool_record()
    async with make_client() as client:
        client.app["state"]._restricted_keys = {"dashboard:incog-1"}
        resp = await client.post(
            "/api/mcp-apps/call",
            headers={"X-Session-Key": "dashboard:incog-1"},
            json={"spool_id": spool_id, "tool": "save_state", "arguments": {}},
        )
        assert resp.status == 403
        body = await resp.json()
        assert "not available" in body["error"]


async def test_relay_rejects_session_mismatch(relay_env, spool_tmp):
    """A caller relaying on behalf of a DIFFERENT session than the one the
    spool record names (leaked spool id) is refused before the gateway."""
    make_client, _sock = relay_env
    spool_id = _spool_record()  # bound to dashboard:sess-cb
    async with make_client() as client:
        resp = await client.post(
            "/api/mcp-apps/call",
            headers={"X-Session-Key": "dashboard:other"},
            json={"spool_id": spool_id, "tool": "save_state", "arguments": {}},
        )
        assert resp.status == 403
        body = await resp.json()
        assert "another session" in body["error"]


@pytest.fixture
def relay_env(short_sock_dir, monkeypatch):
    """Client factory for an app exposing ONLY the relay route, with the
    gateway socket redirected at a tmp path via the config override hook.

    Returns ``(make_client, sock)``; open the client with ``async with``
    inside each test — an async-gen fixture breaks under the repo's pinned
    pytest-asyncio (its wrapper reads ``fixturedef.unittest``, removed in
    pytest 8.1), so the suite avoids ``@pytest_asyncio.fixture`` by
    convention (see test_denied_commands_api.py).

    Uses ``short_sock_dir`` rather than ``tmp_path``: an AF_UNIX path is capped
    at ~104 bytes by ``sockaddr_un.sun_path``, and pytest's ``tmp_path`` under
    macOS's ``/private/var/folders/...`` temp root already exceeds that before
    the filename is appended.
    """
    sock = short_sock_dir / "gateway.sock"
    monkeypatch.setattr(mcp_apps_handlers, "_socket_path", lambda: str(sock))

    @web.middleware
    async def _identity(request, handler):
        # Stand-in for the token-auth middleware: an OWNER dashboard caller
        # (subject in _LOCAL_DASHBOARD_OWNER_SUBJECTS, no app scope). Tests
        # override via the X-Test-User / X-Test-App headers.
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])

    class _FakeState:
        _restricted_keys: set = set()
        _slots: dict = {}
        owner_id = ""

    app["state"] = _FakeState()
    app.router.add_post("/api/mcp-apps/call", mcp_apps_handlers.api_mcp_apps_call)

    def make_client() -> TestClient:
        return TestClient(TestServer(app))

    return make_client, sock


@_UNIX_SOCKET_ONLY
async def test_relay_success(relay_env, spool_tmp):
    make_client, sock = relay_env
    spool_id = _spool_record()
    server, received = await _fake_gateway(
        sock, {"type": "app-result", "result": {"ok": True}}
    )
    try:
        async with make_client() as client:
            resp = await client.post("/api/mcp-apps/call", headers=_OWNER_HDR, json={
                "spool_id": spool_id, "callback_secret": _cbs(spool_id),
                "tool": "save_state", "arguments": {"a": 1},
            })
            assert resp.status == 200
            assert (await resp.json()) == {"result": {"ok": True}}
            assert received[0] == {
                "type": "app-call", "spool_id": spool_id, "callback_secret": _cbs(spool_id),
                "tool": "save_state", "arguments": {"a": 1},
            }
    finally:
        server.close()
        await server.wait_closed()


@_UNIX_SOCKET_ONLY
async def test_relay_policy_rejection_is_403(relay_env, spool_tmp):
    make_client, sock = relay_env
    spool_id = _spool_record()
    server, _ = await _fake_gateway(
        sock, {"type": "app-call-rejected", "reason": "tool not app-visible"}
    )
    try:
        async with make_client() as client:
            resp = await client.post("/api/mcp-apps/call", headers=_OWNER_HDR, json={
                "spool_id": spool_id, "tool": "secret", "arguments": {},
            })
            assert resp.status == 403
            assert (await resp.json())["error"] == "tool not app-visible"
    finally:
        server.close()
        await server.wait_closed()


@_UNIX_SOCKET_ONLY
async def test_relay_backend_error_passthrough(relay_env, spool_tmp):
    make_client, sock = relay_env
    spool_id = _spool_record()
    err = {"code": -32602, "message": "bad args"}
    server, _ = await _fake_gateway(sock, {"type": "app-error", "error": err})
    try:
        async with make_client() as client:
            resp = await client.post("/api/mcp-apps/call", headers=_OWNER_HDR, json={
                "spool_id": spool_id, "tool": "save_state", "arguments": {},
            })
            assert resp.status == 200
            assert (await resp.json()) == {"error": err}
    finally:
        server.close()
        await server.wait_closed()


async def test_relay_unknown_spool_is_404_without_gateway(relay_env, spool_tmp):
    """The local pre-check answers 404 before any socket dial (no fake
    gateway is even listening here)."""
    make_client, _sock = relay_env
    async with make_client() as client:
        resp = await client.post("/api/mcp-apps/call", headers=_OWNER_HDR, json={
            "spool_id": "f" * 32, "tool": "save_state", "arguments": {},
        })
        assert resp.status == 404


@_UNIX_SOCKET_ONLY
async def test_relay_gateway_down_is_503(relay_env, spool_tmp):
    make_client, _sock = relay_env
    spool_id = _spool_record()
    async with make_client() as client:
        resp = await client.post("/api/mcp-apps/call", headers=_OWNER_HDR, json={
            "spool_id": spool_id, "tool": "save_state", "arguments": {},
        })
        assert resp.status == 503


async def test_relay_validates_body(relay_env, spool_tmp):
    make_client, _sock = relay_env
    async with make_client() as client:
        for body, status in (
            ("not json", 400),
            ({"tool": "x"}, 400),
            ({"spool_id": "a" * 32}, 400),
            ({"spool_id": "a" * 32, "tool": "x", "arguments": []}, 400),
        ):
            if isinstance(body, str):
                resp = await client.post(
                    "/api/mcp-apps/call", data=body,
                    headers={"Content-Type": "application/json"},
                )
            else:
                resp = await client.post("/api/mcp-apps/call", json=body)
            assert resp.status == status


async def test_app_call_requires_callback_secret(apps_flag_on, spool_tmp):
    """#418: the model-visible spool_id authorizes NOTHING — a call with a
    wrong or missing callback_secret is denied even for a live record."""
    live = await _spawn_pooled_server()
    try:
        spool_id = _spool_record()
        wrong = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id, "callback_secret": "nope",
            "tool": "save_state", "arguments": {},
        })
        assert wrong["type"] == "app-call-rejected"
        assert "callback capability" in wrong["reason"]
        missing = await handle_app_call(live.pool, {
            "type": "app-call", "spool_id": spool_id,
            "tool": "save_state", "arguments": {},
        })
        assert missing["type"] == "app-call-rejected"
        assert "callback capability" in missing["reason"]
    finally:
        await live.aclose()

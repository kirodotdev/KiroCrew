"""The agent socket's capability store and gate (dashboard/agent_ipc.py).

RFC: docs/request-for-change/rfc-sandbox-ipc-capability.md, phase 1. The store is
driven with injected process-tree readers; the gate with a real unix listener, so
the peer credentials it reads are the kernel's.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest
from aiohttp import web

from kiro_crew.agent_ipc_wire import CAPABILITY_HEADER, REDEEM_PATH
from kiro_crew.dashboard import agent_ipc
from kiro_crew.loopback_http import build_loopback_opener, unix_socket_urlopen
from kiro_crew.testing.wait import default_timeout

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX transport")

# A fake process tree: 500 is the runtime root, 501 the harness, 502 the server,
# 600 an unrelated process.
_PARENTS = {502: 501, 501: 500, 500: 1, 600: 1}


def _store(starts: dict[int, str] | None = None) -> agent_ipc.CapabilityStore:
    table = {502: "s502", 501: "s501", 600: "s600"} if starts is None else starts
    return agent_ipc.CapabilityStore(
        ppid_fn=lambda pid: _PARENTS.get(pid, 1), start_fn=lambda pid: table.get(pid, "")
    )


class _Owner:
    pass


def test_redeem_admits_a_descendant_of_the_runtime_and_binds_its_pid() -> None:
    store = _store()
    pickup = store.issue(server="kirocrew-core", session_token="tok", runtime_pid=lambda: 500)
    cap, code = store.redeem(pickup, 502)
    assert code == "" and len(cap) == 64
    binding, code = store.verify(cap, 502)
    assert binding == agent_ipc.Binding("kirocrew-core", "tok", 502) and code == ""


def test_redeem_refuses_an_unknown_pickup_and_a_process_outside_the_session() -> None:
    store = _store()
    pickup = store.issue(server="kirocrew-core", session_token="tok", runtime_pid=lambda: 500)
    assert store.redeem("not-a-pickup", 502) == ("", agent_ipc.REFUSE_PICKUP_UNKNOWN)
    assert store.redeem(pickup, 600) == ("", agent_ipc.REFUSE_NOT_IN_SESSION)
    # The runtime root itself is not its own descendant.
    assert store.redeem(pickup, 500) == ("", agent_ipc.REFUSE_NOT_IN_SESSION)


def test_redeem_refuses_when_the_runtime_pid_is_unknown() -> None:
    store = _store()
    pickup = store.issue(server="kirocrew-core", session_token="tok", runtime_pid=lambda: None)
    assert store.redeem(pickup, 502) == ("", agent_ipc.REFUSE_NOT_IN_SESSION)


def test_a_second_redemption_is_refused_while_the_first_holder_lives() -> None:
    store = _store()
    pickup = store.issue(server="kirocrew-core", session_token="tok", runtime_pid=lambda: 500)
    cap, _ = store.redeem(pickup, 502)
    # 501 descends from the runtime too (the harness), but 502 still holds the pickup.
    assert store.redeem(pickup, 501) == ("", agent_ipc.REFUSE_PICKUP_HELD)
    assert store.verify(cap, 502)[0] is not None


def test_a_restarted_server_may_redeem_once_the_previous_holder_is_gone() -> None:
    starts = {502: "s502", 501: "s501"}
    store = _store(starts)
    pickup = store.issue(server="kirocrew-core", session_token="tok", runtime_pid=lambda: 500)
    old_cap, _ = store.redeem(pickup, 502)
    del starts[502]  # the first server exited
    new_cap, code = store.redeem(pickup, 501)
    assert code == "" and new_cap
    assert store.verify(old_cap, 502) == (None, agent_ipc.REFUSE_CAPABILITY)
    assert store.verify(new_cap, 501)[0] is not None


def test_a_capability_is_refused_from_any_other_process_or_a_recycled_pid() -> None:
    starts = {502: "s502", 501: "s501", 600: "s600"}
    store = _store(starts)
    pickup = store.issue(server="kirocrew-core", session_token="tok", runtime_pid=lambda: 500)
    cap, _ = store.redeem(pickup, 502)
    assert store.verify(cap, 600) == (None, agent_ipc.REFUSE_CAPABILITY)
    assert store.verify(cap, 501) == (None, agent_ipc.REFUSE_CAPABILITY)
    starts[502] = "recycled"
    assert store.verify(cap, 502) == (None, agent_ipc.REFUSE_CAPABILITY)
    assert store.verify("", 502) == (None, agent_ipc.REFUSE_CAPABILITY)


def test_a_peer_with_no_start_token_cannot_redeem() -> None:
    store = _store({})
    pickup = store.issue(server="kirocrew-core", session_token="tok", runtime_pid=lambda: 500)
    assert store.redeem(pickup, 502) == ("", agent_ipc.REFUSE_PEER_UNVERIFIED)


def test_pickups_die_with_their_owner() -> None:
    store = _store()
    owner = _Owner()
    pickup = store.issue(
        server="kirocrew-core", session_token="tok", runtime_pid=lambda: 500, owner=owner
    )
    cap, _ = store.redeem(pickup, 502)
    del owner
    gc.collect()
    assert store.verify(cap, 502) == (None, agent_ipc.REFUSE_CAPABILITY)
    assert store.redeem(pickup, 502) == ("", agent_ipc.REFUSE_PICKUP_UNKNOWN)


def test_an_owner_collected_while_the_store_lock_is_held_does_not_hang() -> None:
    # The cyclic GC can run the owner's finalizer on a thread that already holds the
    # store's lock (inside issue, or redeem's locked liveness check). The finalizer
    # must not take that lock, and the next locked call drops the pickup.
    store = _store()
    issued: dict[str, str] = {}

    def collect_under_the_lock() -> None:
        owner = _Owner()
        issued["pickup"] = store.issue(
            server="kirocrew-core", session_token="tok", runtime_pid=lambda: 500, owner=owner
        )
        issued["cap"] = store.redeem(issued["pickup"], 502)[0]
        with store._lock:
            del owner
            gc.collect()

    worker = threading.Thread(target=collect_under_the_lock, daemon=True)
    worker.start()
    worker.join(timeout=default_timeout())
    if worker.is_alive():
        store._lock.release()  # unwind the hung finalizer so the thread can exit
        pytest.fail("the owner's finalizer blocked on the store lock its own thread held")
    assert store.verify(issued["cap"], 502) == (None, agent_ipc.REFUSE_CAPABILITY)
    assert store.redeem(issued["pickup"], 502) == ("", agent_ipc.REFUSE_PICKUP_UNKNOWN)


def test_the_store_keeps_digests_not_values() -> None:
    store = _store()
    pickup = store.issue(server="kirocrew-core", session_token="tok", runtime_pid=lambda: 500)
    cap, _ = store.redeem(pickup, 502)
    assert pickup.encode() not in repr(store._pickups).encode()
    assert cap not in repr(store._caps) and pickup not in repr(store._pickups)


def test_issue_requires_a_server_and_a_token() -> None:
    with pytest.raises(ValueError):
        _store().issue(server="", session_token="tok", runtime_pid=lambda: 500)
    with pytest.raises(ValueError):
        _store().issue(server="kirocrew-core", session_token="", runtime_pid=lambda: 500)


# --- the gate, over a real unix socket ------------------------------------------------


def _post(path: str, sock: str, *, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1{path}",
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with unix_socket_urlopen(req, timeout=default_timeout(), socket_path=sock) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def gate_app(short_sock_dir, monkeypatch):
    """A gateway-shaped app: the gate as middleware, one internal and one other route."""
    sock = short_sock_dir / "ipc" / "agent-1.sock"
    monkeypatch.setattr(agent_ipc, "agent_socket_path", lambda port: sock)
    monkeypatch.setattr(agent_ipc, "STORE", agent_ipc.CapabilityStore())
    monkeypatch.setattr(
        agent_ipc, "verify_session_token", lambda tok: {"tok": "chat-1"}.get(tok, "")
    )

    @web.middleware
    async def gate(request, handler):
        if agent_ipc.request_on_agent_socket(request):
            refused = await agent_ipc.agent_socket_gate(
                request, internal=request.path.startswith("/api/internal")
            )
            if refused is not None:
                return refused
        return await handler(request)

    async def internal(request):
        binding = request.get(agent_ipc.GRANT_KEY)
        return web.json_response({"server": binding.server if binding else None})

    async def other(request):
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[gate])
    app.router.add_get("/api/internal/thing", internal)
    app.router.add_get("/api/other", other)
    return app, str(sock)


async def _serve(app, call):
    runner = web.AppRunner(app)
    await runner.setup()
    path = await agent_ipc.start_agent_site(runner, 1)
    assert path is not None and agent_ipc.agent_socket_bound()
    try:
        return await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, call), 30)
    finally:
        agent_ipc._BOUND_PATHS.discard(str(path))
        await runner.cleanup()


def _issue(runtime_pid: int) -> str:
    return agent_ipc.issue_pickup(
        server="kirocrew-core", session_token="tok", runtime_pid=lambda: runtime_pid
    )


@pytest.mark.asyncio
async def test_gate_redeems_then_admits_the_capability_for_its_own_session(gate_app):
    app, sock = gate_app
    # This test process is the peer; its parent stands in for the runtime root.
    pickup = _issue(os.getppid())

    def call():
        status, body = _post(REDEEM_PATH, sock, body={"pickup": pickup})
        assert status == 200, body
        cap = body["capability"]
        ok = _post(
            "/api/internal/thing", sock, headers={CAPABILITY_HEADER: cap, "X-Session-Key": "chat-1"}
        )
        other_session = _post(
            "/api/internal/thing", sock, headers={CAPABILITY_HEADER: cap, "X-Session-Key": "chat-2"}
        )
        no_route = _post(
            "/api/other", sock, headers={CAPABILITY_HEADER: cap, "X-Session-Key": "chat-1"}
        )
        no_cap = _post("/api/internal/thing", sock, headers={"X-Session-Key": "chat-1"})
        secret = _post(
            "/api/internal/thing",
            sock,
            headers={CAPABILITY_HEADER: cap, "X-Session-Key": "chat-1", "X-Internal-Secret": "x"},
        )
        return ok, other_session, no_route, no_cap, secret

    ok, other_session, no_route, no_cap, secret = await _serve(app, call)
    assert ok == (200, {"server": "kirocrew-core"})
    assert other_session[0] == 403 and other_session[1]["code"] == agent_ipc.REFUSE_SESSION_MISMATCH
    assert no_route[0] == 403 and no_route[1]["code"] == agent_ipc.REFUSE_ROUTE
    assert no_cap[0] == 403 and no_cap[1]["code"] == agent_ipc.REFUSE_CAPABILITY
    assert secret[0] == 403 and secret[1]["code"] == agent_ipc.REFUSE_SECRET


@pytest.mark.asyncio
async def test_a_connection_bridge_capability_serves_its_proxy_route_only(gate_app):
    """A bridge's capability is no key to the gateway's other internal routes."""
    from kiro_crew.agent_ipc_wire import BRIDGE_PICKUP_PREFIX

    app, sock = gate_app
    pickup = agent_ipc.issue_pickup(
        server=f"{BRIDGE_PICKUP_PREFIX}github", session_token="tok", runtime_pid=os.getppid
    )

    def call():
        cap = _post(REDEEM_PATH, sock, body={"pickup": pickup})[1]["capability"]
        return _post(
            "/api/internal/thing", sock, headers={CAPABILITY_HEADER: cap, "X-Session-Key": "chat-1"}
        )

    status, body = await _serve(app, call)
    assert status == 403 and body["code"] == agent_ipc.REFUSE_ROUTE


@pytest.mark.asyncio
async def test_gate_refuses_a_redemption_from_outside_the_session(gate_app):
    app, sock = gate_app
    # The runtime root is this process itself, so this process is not its descendant.
    pickup = _issue(os.getpid())

    def call():
        return _post(REDEEM_PATH, sock, body={"pickup": pickup})

    status, body = await _serve(app, call)
    assert status == 403 and body["code"] == agent_ipc.REFUSE_NOT_IN_SESSION


@pytest.mark.asyncio
async def test_gate_refuses_a_session_token_that_names_no_session(gate_app, monkeypatch):
    app, sock = gate_app
    monkeypatch.setattr(agent_ipc, "verify_session_token", lambda tok: "")
    pickup = _issue(os.getppid())

    def call():
        _status, body = _post(REDEEM_PATH, sock, body={"pickup": pickup})
        return _post(
            "/api/internal/thing",
            sock,
            headers={CAPABILITY_HEADER: body["capability"], "X-Session-Key": "chat-1"},
        )

    status, body = await _serve(app, call)
    assert status == 403 and body["code"] == agent_ipc.REFUSE_SESSION_UNBOUND


@pytest.mark.asyncio
async def test_the_dashboard_tcp_path_is_untouched_by_the_gate(gate_app):
    """A request NOT on the agent socket never enters the gate."""
    app, _sock = gate_app
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    def call():
        opener = build_loopback_opener()
        with opener.open(
            f"http://127.0.0.1:{port}/api/internal/thing", timeout=default_timeout()
        ) as r:
            return json.loads(r.read())

    try:
        body = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, call), 30)
    finally:
        await runner.cleanup()
    assert body == {"server": None}


# --- through the real token_auth middleware --------------------------------------------

_SECRET = "s" * 32


@pytest.fixture
def token_auth_app(gate_app):
    """The same socket, with the gateway's real token_auth deciding every request."""
    from kiro_crew.dashboard import token_auth as ta

    _app, sock = gate_app

    async def thing(request):
        return web.json_response(
            {
                "internal_auth": bool(request.get("internal_auth")),
                "server": getattr(request.get(agent_ipc.GRANT_KEY), "server", None),
            }
        )

    app = web.Application(
        middlewares=[
            ta.token_auth_middleware(
                internal_paths={"/api/internal/thing"}, internal_secret=_SECRET
            )
        ]
    )
    app.router.add_get("/api/internal/thing", thing)
    app.router.add_get("/api/other", thing)
    return app, sock


@pytest.mark.asyncio
async def test_token_auth_grants_the_internal_arm_to_a_verified_capability(token_auth_app):
    app, sock = token_auth_app
    pickup = _issue(os.getppid())

    def call():
        _status, body = _post(REDEEM_PATH, sock, body={"pickup": pickup})
        cap = body["capability"]
        session = {"X-Session-Key": "chat-1"}
        return (
            _post("/api/internal/thing", sock, headers={CAPABILITY_HEADER: cap, **session}),
            # The internal secret is not a credential on this listener, alone or beside a
            # capability: it authenticates by connection only.
            _post("/api/internal/thing", sock, headers={"X-Internal-Secret": _SECRET, **session}),
            _post(
                "/api/internal/thing",
                sock,
                headers={CAPABILITY_HEADER: cap, "X-Internal-Secret": _SECRET, **session},
            ),
            # Non-internal routes are not served, capability or not.
            _post("/api/other", sock, headers={CAPABILITY_HEADER: cap, **session}),
            # A capability for another session is refused before any grant.
            _post(
                "/api/internal/thing", sock, headers={CAPABILITY_HEADER: cap, "X-Session-Key": "x"}
            ),
        )

    ok, secret_only, both, other, mismatch = await _serve(app, call)
    assert ok == (200, {"internal_auth": True, "server": "kirocrew-core"})
    assert secret_only[0] == 403 and secret_only[1]["code"] == agent_ipc.REFUSE_SECRET
    assert both[0] == 403 and both[1]["code"] == agent_ipc.REFUSE_SECRET
    assert other[0] == 403 and other[1]["code"] == agent_ipc.REFUSE_ROUTE
    assert mismatch[0] == 403 and mismatch[1]["code"] == agent_ipc.REFUSE_SESSION_MISMATCH


@pytest.mark.asyncio
async def test_token_auth_without_the_agent_socket_still_requires_the_secret(token_auth_app):
    """Over loopback TCP nothing changes: the secret grants, its absence refuses."""
    app, _sock = token_auth_app
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    opener = build_loopback_opener()

    def get(headers):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/internal/thing", headers=headers)
        try:
            with opener.open(req, timeout=default_timeout()) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def call():
        return (
            get({"X-Internal-Secret": _SECRET}),
            get({"X-Internal-Secret": ""}),
            # A capability header on TCP is not a credential either.
            get({CAPABILITY_HEADER: "a" * 64, "X-Internal-Secret": ""}),
        )

    try:
        granted, missing, cap_on_tcp = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, call), 30
        )
    finally:
        await runner.cleanup()
    assert granted == (200, {"internal_auth": True, "server": None})
    assert missing[0] == 403 and missing[1]["code"] == "internal_secret_missing"
    assert cap_on_tcp[0] == 403 and cap_on_tcp[1]["code"] == "internal_secret_missing"


# --- the socket's place in the data home -------------------------------------------------


@pytest.mark.parametrize("backend", ["codex", "opencode", "goose", "pi"])
def test_an_enforced_harness_child_can_reach_the_agent_socket_but_not_the_secret(backend):
    """The agent socket is the enforced child's way in, so its leaf must not be masked.

    The same mask keeps hiding ``run/`` and ``.local_secret``: this path is added
    beside the secret, never by exposing it.
    """
    from kiro_crew.agent_ipc_wire import AGENT_IPC_LEAF
    from kiro_crew.agent_sdk.tool_gate import adapter_hidden_credential_dirs
    from kiro_crew.config.loader import config_dir

    hidden = {os.path.normpath(p) for p in adapter_hidden_credential_dirs(backend)}
    home = config_dir()
    agent_dir = os.path.normpath(str(home / AGENT_IPC_LEAF))
    assert hidden, "the backend is expected to be enforced"
    assert not any(agent_dir == h or agent_dir.startswith(h + os.sep) for h in hidden)
    assert os.path.normpath(str(home / "run")) in hidden
    assert os.path.normpath(str(home / ".local_secret")) in hidden


@pytest.mark.asyncio
async def test_the_session_query_names_only_the_holders_own_session(gate_app):
    """A server under the mask asks which session it acts for; only a holder is told."""
    from kiro_crew.agent_ipc_wire import SESSION_PATH

    app, sock = gate_app
    pickup = _issue(os.getppid())

    def call():
        _status, body = _post(REDEEM_PATH, sock, body={"pickup": pickup})
        cap = body["capability"]
        return (
            _post(SESSION_PATH, sock, headers={CAPABILITY_HEADER: cap}),
            _post(SESSION_PATH, sock),
            _post(SESSION_PATH, sock, headers={CAPABILITY_HEADER: "f" * 64}),
            _post(SESSION_PATH, sock, body={}, headers={CAPABILITY_HEADER: cap}),
        )

    ok, no_cap, wrong_cap, post = await _serve(app, call)
    assert ok == (200, {"session_key": "chat-1"})
    assert no_cap[0] == 403 and no_cap[1]["code"] == agent_ipc.REFUSE_CAPABILITY
    assert wrong_cap[0] == 403 and wrong_cap[1]["code"] == agent_ipc.REFUSE_CAPABILITY
    assert post[0] == 403 and post[1]["code"] == agent_ipc.REFUSE_ROUTE


@pytest.mark.asyncio
async def test_a_granted_session_query_writes_one_granted_sel_row(gate_app, monkeypatch):
    """The session query is a capability grant, so the operator's SEL records it."""
    from kiro_crew.agent_ipc_wire import SESSION_PATH

    app, sock = gate_app
    rows: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        agent_ipc,
        "_log_auth",
        lambda request, user_id, outcome, error: rows.append((request.path, user_id, outcome)),
    )
    pickup = _issue(os.getppid())

    def call():
        _status, body = _post(REDEEM_PATH, sock, body={"pickup": pickup})
        return _post(SESSION_PATH, sock, headers={CAPABILITY_HEADER: body["capability"]})

    assert (await _serve(app, call))[0] == 200
    assert rows == [(SESSION_PATH, "agent-ipc", "granted")]

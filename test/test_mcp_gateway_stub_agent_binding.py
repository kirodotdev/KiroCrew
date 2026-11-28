"""The declaring agent of a stub comes from the gateway's agent binding.

gatewayd serves a stub's backend with the declared env of one agent, and stamps
that agent on its app renders. These tests drive the real ``_handle_connection``
over a real unix socket and check that the agent is the one the owning gateway
bound to the stub's session token (an ``agent-binding`` frame), not whatever the
Register frame names:

* a Register naming another agent than the binding gets no declared env and an
  empty declaring agent,
* a Register naming the bound agent gets its declared env, as before,
* a session whose stubs register before any claim (a warm-pool session) is
  served its agent, and the claim that follows names its caller without moving
  the agent,
* a binding from a sender that is not the daemon's owner is not recorded,
* a resource-subscription holder registering from the gateway keeps the agent
  the daemon resolved, and
* the gateway side reads the stub agents off ``session/new`` params and sends
  the binding again with every claim.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew.mcp_gateway import claim as claim_mod
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import launch_approval
from kiro_crew.mcp_gateway.daemon import connection as connection_mod
from kiro_crew.mcp_gateway.daemon import control, session_describe
from kiro_crew.mcp_gateway.hashing import hash_effective_env
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.mcp_gateway.rewriter_overlay import (
    env_sidecar_dir,
    env_sidecar_name,
    resolve_overlay_dir,
)
from kiro_crew.testing.ids import unallocatable_pids
from kiro_crew.testing.wait import finished, until_parked

pytestmark = [
    pytest.mark.xdist_group("mcp_gateway"),
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="the daemon under test listens on a Unix socket (gatewayd is unix-only)",
    ),
]

#: The approval gate is stubbed by ``_clean_globals`` below, on purpose and visibly.
ENFORCE_LAUNCH_APPROVAL = True

OWN_AGENT = "own-agent"
OTHER_AGENT = "other-agent"
SERVER = "gh-mcp"
TOKEN = "t" * 64
#: The runtime pid the binding names, and the host chain the peer walk reports.
RUNTIME_PID, OTHER_RUNTIME_PID = unallocatable_pids(2)


@pytest.fixture(autouse=True)
def _clean_globals(_floor_monkeypatch, tmp_path):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    for table in (gw._CONN_INDEX, gw._STUB_PROBES, gw._TOKEN_BINDINGS, gw._AGENT_BINDINGS):
        table.clear()
    claim_mod._BOUND_AGENTS.clear()
    session_describe._LIVE_CONNS.clear()
    # The test process is the gateway that owns the daemon.
    _floor_monkeypatch.setattr(gw, "_OWNER_PID", os.getpid())
    # The kernel-attested host chain of every registering peer: under RUNTIME_PID.
    _floor_monkeypatch.setattr(gw, "_resolve_peer_identity", lambda _pid: ("", [RUNTIME_PID]))
    _floor_monkeypatch.setattr(launch_approval, "launch_approved", lambda *_a, **_k: True)
    yield
    for table in (gw._CONN_INDEX, gw._STUB_PROBES, gw._TOKEN_BINDINGS, gw._AGENT_BINDINGS):
        table.clear()
    claim_mod._BOUND_AGENTS.clear()
    session_describe._LIVE_CONNS.clear()


def _write_sidecar(agent: str) -> dict[str, str]:
    pairs = {"GITHUB_TOKEN": f"secret://{agent}-token", "REGION": "us-west-2"}
    d = env_sidecar_dir(resolve_overlay_dir())
    d.mkdir(parents=True, exist_ok=True)
    (d / env_sidecar_name(agent, SERVER)).write_text(json.dumps(pairs), encoding="utf-8")
    return pairs


def _register(agent: str, pairs: dict[str, str], **extra: Any) -> dict[str, Any]:
    frame = {
        "type": "register",
        "stub_uuid": str(uuid.uuid4()),
        "server_name": SERVER,
        "agent_name": agent,
        "command_args_hash": "c" * 16,
        "effective_env_hash": hash_effective_env(pairs, identity_keys=()),
        "work_dir": "/tmp",
        "binary_version": "1",
        "os_uid": 1000,
        "stub_session_token": TOKEN,
        "ancestor_pids": [RUNTIME_PID],
    }
    frame.update(extra)
    return frame


class _Daemon:
    def __init__(self, sock: Path, acquired: list[dict[str, Any]], backend: MagicMock) -> None:
        self.sock = sock
        self.acquired = acquired
        self.backend = backend
        self._writers: list[asyncio.StreamWriter] = []

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_unix_connection(str(self.sock))
        self._writers.append(writer)
        return reader, writer

    async def close_clients(self) -> None:
        """Close every client connection so each handler reaches EOF and returns.

        The daemon's own ``wait_closed`` only completes once its handler tasks
        end, and a registered stub's handler runs until its socket does.
        """
        for writer in self._writers:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def control(self, frame: dict[str, Any]) -> dict[str, Any]:
        reader, writer = await self._open()
        writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
        return json.loads(await finished(reader.readline(), "the daemon's reply"))

    async def register_and_ensure(self, frame: dict[str, Any]) -> tuple[Any, Any]:
        reader, writer = await self._open()
        writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
        registered = json.loads(await finished(reader.readline(), "the daemon's reply"))
        assert registered["type"] == "registered", registered
        writer.write(b'{"type":"ensure_backend"}\n')
        await writer.drain()
        ready = json.loads(await finished(reader.readline(), "the daemon's reply"))
        assert ready["type"] == "ready", ready
        return reader, writer


@pytest_asyncio.fixture
async def daemon(monkeypatch) -> AsyncIterator[_Daemon]:
    acquired: list[dict[str, Any]] = []
    backend = MagicMock()
    backend.attach_stub = AsyncMock(side_effect=lambda *_a, **_k: asyncio.Queue())
    backend.detach_stub = AsyncMock(return_value=0)
    backend.cancel_in_flight_for_stub = AsyncMock(return_value=[])
    backend.recycle_if_idle = AsyncMock()
    backend.quarantined = False
    backend._pending_requests = {}

    async def _acquire(pool, pool_key, resolver, **kw):
        acquired.append({"pool_key": pool_key, **kw})
        return backend, True

    monkeypatch.setattr(gw, "_acquire_backend", _acquire)
    # A private backend with no binding yet waits up to one keepalive interval
    # for a late one (connection._await_declaring_agent); shrink it so the
    # fail-closed tests do not sit on the real 5s.
    monkeypatch.setattr(
        connection_mod, "QUEUED_KEEPALIVE_SECS", 0.3
    )  # flake-ok: bounds the fail-closed agent-binding wait in the test
    pool = MagicMock()
    pool.get = AsyncMock(return_value=None)
    pool.release_exclusive = AsyncMock(return_value=None)
    pool.unreserve = MagicMock()

    def _resolver(pk: PoolKey):
        return "true", [], {}, pk.work_dir

    handlers: set[asyncio.Task[Any]] = set()

    async def _serve(reader, writer):
        task = asyncio.current_task()
        if task is not None:
            handlers.add(task)
        try:
            await gw._handle_connection(reader, writer, pool, _resolver, sock)
        finally:
            if task is not None:
                handlers.discard(task)

    # In the run's short root, not tmp_path: a unix socket path has a short length limit.
    with tempfile.TemporaryDirectory(
        prefix=SHORT_TMP_PREFIX + "agent-binding-", dir=short_tmp_base()
    ) as sock_dir:
        sock = Path(sock_dir) / "gw.sock"
        server = await asyncio.start_unix_server(_serve, path=str(sock))
        daemon_obj = _Daemon(sock, acquired, backend)
        try:
            yield daemon_obj
        finally:
            # Close the clients so each handler reaches EOF and returns, then
            # await the handler tasks directly. ``Server.wait_closed`` is not
            # used: on CPython 3.12 it never returns once the server has served
            # a connection (the close waiter is never set).
            await daemon_obj.close_clients()
            server.close()
            if handlers:
                await asyncio.gather(*handlers, return_exceptions=True)


def _binding(agents: list[str]) -> dict[str, Any]:
    return {
        "type": "agent-binding",
        "stub_session_token": TOKEN,
        "agents": agents,
        "pid": RUNTIME_PID,
        "pid_start_id": None,
    }


def _stubs_are_not_the_owner(monkeypatch) -> None:
    """From here on, connections come from a process other than the daemon's owner.

    The test process sends both the binding and the Register, so a stub's
    Register would otherwise pass as the gateway's own.
    """
    monkeypatch.setattr(gw, "_OWNER_PID", os.getpid() + 100000)
    monkeypatch.setattr(control.platform_compat, "is_exec_supervisor_of", lambda *_a: False)


async def _settle(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_a_register_naming_another_agent_gets_no_declared_env(daemon, monkeypatch) -> None:
    _write_sidecar(OTHER_AGENT)
    pairs = _write_sidecar(OWN_AGENT)
    assert (await daemon.control(_binding([OWN_AGENT])))["type"] == "agent-bound"
    _stubs_are_not_the_owner(monkeypatch)

    _reader, writer = await daemon.register_and_ensure(_register(OTHER_AGENT, pairs))
    await _settle(writer)

    (seen,) = daemon.acquired
    assert seen["exclusive_stub_uuid"], "premise: a private backend"
    assert seen["declaring_agent"] == ""
    assert gw._declared_env_for_private_backend(seen["pool_key"], declaring_agent="") == {}
    daemon.backend.attach_stub.assert_awaited_once()
    assert daemon.backend.attach_stub.await_args.kwargs["agent"] == ""


@pytest.mark.asyncio
async def test_a_register_naming_the_bound_agent_gets_its_declared_env(daemon, monkeypatch) -> None:
    pairs = _write_sidecar(OWN_AGENT)
    assert (await daemon.control(_binding([OWN_AGENT])))["type"] == "agent-bound"
    _stubs_are_not_the_owner(monkeypatch)

    _reader, writer = await daemon.register_and_ensure(_register(OWN_AGENT, pairs))
    await _settle(writer)

    (seen,) = daemon.acquired
    assert seen["declaring_agent"] == OWN_AGENT
    declared = gw._declared_env_for_private_backend(
        seen["pool_key"], declaring_agent=seen["declaring_agent"]
    )
    assert declared == pairs
    assert daemon.backend.attach_stub.await_args.kwargs["agent"] == OWN_AGENT


@pytest.mark.asyncio
async def test_a_stub_registering_before_its_claim_keeps_its_agent(daemon, monkeypatch) -> None:
    """A warm-pool session: binding, then register with no caller, then the claim."""
    pairs = _write_sidecar(OWN_AGENT)
    assert (await daemon.control(_binding([OWN_AGENT])))["type"] == "agent-bound"
    _stubs_are_not_the_owner(monkeypatch)

    _reader, writer = await daemon.register_and_ensure(_register(OWN_AGENT, pairs))
    (conn,) = gw._CONN_INDEX[RUNTIME_PID]
    assert conn.caller is None, "premise: no claim has named the token yet"
    (seen,) = daemon.acquired
    assert seen["declaring_agent"] == OWN_AGENT
    assert (
        gw._declared_env_for_private_backend(seen["pool_key"], declaring_agent=OWN_AGENT) == pairs
    )

    claim = claim_mod.build_claim_frame(RUNTIME_PID, "dashboard:warm", None, TOKEN)
    claim["pid_start_id"] = None
    reply = await daemon.control(claim)
    assert reply["type"] == "claimed", reply
    assert conn.caller is not None and conn.caller.session_key == "dashboard:warm"
    assert conn.bound_agents == frozenset({OWN_AGENT})
    await _settle(writer)


@pytest.mark.asyncio
async def test_a_binding_from_a_sender_that_is_not_the_owner_is_ignored(
    daemon, monkeypatch
) -> None:
    pairs = _write_sidecar(OWN_AGENT)
    monkeypatch.setattr(gw, "_OWNER_PID", os.getpid() + 100000)
    monkeypatch.setattr(control.platform_compat, "is_exec_supervisor_of", lambda *_a: False)

    reply = await daemon.control(_binding([OWN_AGENT]))
    assert reply["type"] == "agent-binding-rejected"
    assert "neither owner pid" in reply["reason"]
    assert TOKEN not in gw._AGENT_BINDINGS

    _reader, writer = await daemon.register_and_ensure(_register(OWN_AGENT, pairs))
    await _settle(writer)
    (seen,) = daemon.acquired
    assert seen["declaring_agent"] == ""


@pytest.mark.asyncio
async def test_an_unowned_daemon_records_no_binding(daemon, monkeypatch) -> None:
    monkeypatch.setattr(gw, "_OWNER_PID", 0)
    reply = await daemon.control(_binding([OWN_AGENT]))
    assert reply["type"] == "agent-binding-rejected"
    assert "--owner-pid" in reply["reason"]
    assert TOKEN not in gw._AGENT_BINDINGS


@pytest.mark.asyncio
async def test_a_binding_for_another_runtime_does_not_reach_this_stub(daemon, monkeypatch) -> None:
    pairs = _write_sidecar(OWN_AGENT)
    frame = _binding([OWN_AGENT])
    frame["pid"] = OTHER_RUNTIME_PID
    assert (await daemon.control(frame))["type"] == "agent-bound"
    _stubs_are_not_the_owner(monkeypatch)

    _reader, writer = await daemon.register_and_ensure(_register(OWN_AGENT, pairs))
    await _settle(writer)
    (seen,) = daemon.acquired
    assert seen["declaring_agent"] == ""


@pytest.mark.asyncio
async def test_a_holder_registered_by_the_gateway_keeps_the_resolved_agent(
    daemon, monkeypatch
) -> None:
    """The kept Register names the agent the daemon resolved, not the frame's."""
    pairs = _write_sidecar(OWN_AGENT)
    assert (await daemon.control(_binding([OWN_AGENT])))["type"] == "agent-bound"
    with monkeypatch.context() as stub_side:
        _stubs_are_not_the_owner(stub_side)
        _reader, writer = await daemon.register_and_ensure(
            _register(OTHER_AGENT, pairs, session_key="dashboard:s", session_type="dashboard")
        )
    (conn,) = list(session_describe._LIVE_CONNS)
    assert conn.register is not None and conn.register["agent_name"] == ""
    await _settle(writer)
    assert daemon.acquired[-1]["declaring_agent"] == ""

    # A Register sent from the owning gateway itself, with no token, as a holder's is.
    holder = _register(OWN_AGENT, pairs)
    holder.pop("stub_session_token")
    _reader, writer = await daemon.register_and_ensure(holder)
    await _settle(writer)
    assert daemon.acquired[-1]["declaring_agent"] == OWN_AGENT


def test_stub_entry_agents_reads_every_stub_entry(tmp_path) -> None:
    """A marked kiro-agent-JSON entry and an ACP entry that runs the stub module
    both bind their ``--agent``; an unrelated server's own flags do not."""
    from kiro_crew.mcp_gateway.rewriter_spec import _refusing_stub_entry

    stub_x = _refusing_stub_entry(server_name="a", agent_name="x", work_dir=tmp_path)
    stub_y = _refusing_stub_entry(server_name="b", agent_name="y", work_dir=tmp_path)
    params = {
        "mcpServers": [
            # ACP array element: the marker is dropped, the stub module stays.
            {"name": "a", "command": stub_x["command"], "args": stub_x["args"]},
            # A non-stub server carrying --server / --agent flags of its own.
            {"name": "plain", "command": "node", "args": ["--server", "x", "--agent", "victim"]},
        ],
        # kiro-agent-JSON entry, keyed by name, carrying the wrapper marker.
        "_meta": {"kiro": {"customAgents": [{"mcpServers": {"b": stub_y}}]}},
    }
    assert claim_mod.stub_entry_agents(params) == frozenset({"x", "y"})
    assert claim_mod.stub_entry_agents({"mcpServers": []}) == frozenset()


def test_stub_entry_agents_ignores_a_non_stub_servers_argv() -> None:
    """A plain agent-spec server carrying ``--server x --agent victim`` is not a
    gateway stub (no wrapper marker, does not run the stub module), so it binds
    no agent -- the gateway never takes a declaring agent from arbitrary argv."""
    params = {
        "mcpServers": [
            {"name": "plain", "command": "node", "args": ["--server", "x", "--agent", "victim"]},
        ]
    }
    assert claim_mod.stub_entry_agents(params) == frozenset()


@pytest.mark.asyncio
async def test_every_claim_sends_the_binding_again(monkeypatch) -> None:
    sent: list[tuple[str, Any]] = []

    async def _claim(*args: Any) -> bool:
        sent.append(("claim", args))
        return True

    async def _inner(socket_path, pid, token, agents, pid_start_id, confined=False):
        sent.append(("bind", (pid, token, agents)))
        return True

    monkeypatch.setattr(claim_mod, "send_claim", _claim)
    monkeypatch.setattr(claim_mod, "_send_agent_binding_inner", _inner)

    assert await claim_mod.send_agent_binding("/s", RUNTIME_PID, TOKEN, frozenset({"x"}))
    sent.clear()
    await claim_mod._claim_and_rebind("/s", RUNTIME_PID, "dashboard:a", None, TOKEN)
    assert [kind for kind, _ in sent] == ["claim", "bind"]
    assert sent[1][1] == (RUNTIME_PID, TOKEN, frozenset({"x"}))

    sent.clear()
    await claim_mod._claim_and_rebind("/s", RUNTIME_PID, "dashboard:a", None, "u" * 64)
    assert [kind for kind, _ in sent] == ["claim"]


def test_stub_entry_agents_reads_a_rewritten_stub_entry(tmp_path) -> None:
    """The rewriter packs ``--server`` / ``--agent`` into the stub-flags envelope."""
    from kiro_crew.mcp_gateway.rewriter_spec import _refusing_stub_entry

    entry = _refusing_stub_entry(server_name=SERVER, agent_name=OWN_AGENT, work_dir=tmp_path)
    assert "--agent" not in entry["args"], "premise: the flags are inside the envelope"
    params = {"mcpServers": [{"name": SERVER, "command": entry["command"], "args": entry["args"]}]}
    assert claim_mod.stub_entry_agents(params) == frozenset({OWN_AGENT})


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["session/new", "session/load", "session/resume"])
async def test_the_binding_is_sent_before_the_request_that_starts_the_stubs(
    monkeypatch, method, tmp_path
) -> None:
    from types import SimpleNamespace

    from kiro_crew.acp import client_transport
    from kiro_crew.mcp_gateway.rewriter_spec import _refusing_stub_entry

    order: list[str] = []

    async def _bind(socket_path, pid, token, agents, **_kw):
        order.append(f"bind {pid} {sorted(agents)}")
        return True

    class _Written(Exception):
        pass

    def _next_req_id() -> int:
        order.append("write")
        raise _Written

    monkeypatch.setattr(client_transport, "send_agent_binding", _bind)
    client = SimpleNamespace(
        # No project-file backend, so main's hold_session_params is a no-op here.
        backend="",
        _agent=None,
        _process=SimpleNamespace(pid=RUNTIME_PID, stdin=object()),
        _native_skill_projection=None,
        _stub_session_token=TOKEN,
        _mcp_gateway_socket="/s",
        _next_req_id=_next_req_id,
    )
    stub = _refusing_stub_entry(server_name="a", agent_name=OWN_AGENT, work_dir=tmp_path)
    params = {"mcpServers": [{"name": "a", "command": stub["command"], "args": stub["args"]}]}
    with pytest.raises(_Written):
        await client_transport._send_request(client, method, params)
    assert order == [f"bind {RUNTIME_PID} {[OWN_AGENT]}", "write"]


@pytest.mark.asyncio
async def test_a_binding_after_register_reaches_the_live_connection(daemon, monkeypatch) -> None:
    """A stub that reconnected before the binding was sent again picks it up."""
    pairs = _write_sidecar(OWN_AGENT)
    with monkeypatch.context() as stub_side:
        _stubs_are_not_the_owner(stub_side)
        _reader, writer = await daemon.register_and_ensure(_register(OWN_AGENT, pairs))
    assert daemon.acquired[-1]["declaring_agent"] == ""
    (conn,) = gw._CONN_INDEX[RUNTIME_PID]
    relabel = AsyncMock()
    monkeypatch.setattr(connection_mod, "_relabel_stub", relabel)
    respawn = AsyncMock(return_value=None)
    monkeypatch.setattr(gw, "_respawn_backend_for_stub", respawn)

    reply = await daemon.control(_binding([OWN_AGENT]))
    assert reply == {"type": "agent-bound", "agents": 1, "connections": 1}
    assert conn.bound_agents == frozenset({OWN_AGENT})

    writer.write(b'{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n')
    await writer.drain()
    answer = json.loads(await finished(_reader.readline(), "the answer to the forwarded request"))
    assert answer["id"] == 7
    relabel.assert_awaited_once_with(daemon.backend, conn.stub_uuid, OWN_AGENT)
    respawn.assert_awaited_once()
    assert conn.register is not None and conn.register["agent_name"] == OWN_AGENT
    await _settle(writer)


@pytest.mark.asyncio
async def test_a_non_owner_binding_leaves_the_stub_with_no_declared_env(
    monkeypatch, caplog
) -> None:
    """A CLI-local process is not the daemon's owner, so its stub binding is refused.

    ``kirocrew chat`` / ``run`` / ``eval`` / knowledge consolidate route through
    the daemon like the dashboard, but only the owning gateway may bind a stub's
    agent, and only for a connection that owner descends. On an owned daemon (the
    dashboard owns it), a non-owner session's stub -- the owner pid is not in its
    attested host chain -- can never receive a binding, so it resolves to an empty
    declaring agent AT ONCE, without waiting out the keepalive, and one WARNING
    names the server. Fail-closed by design.
    """
    # The dashboard owns the daemon; this stub is not descended from it.
    monkeypatch.setattr(gw, "_OWNER_PID", os.getpid())

    def _must_not_wait() -> Any:
        raise AssertionError("entered the wait: a non-owner binding can never arrive")

    # Deterministic stand-in for "without waiting" (no wall-clock bound, per the
    # Determinism contract): the wait loop's first step is `_agent_binding_event`,
    # so if the guard fails to return early this fires and fails the test.
    monkeypatch.setattr(gw, "_agent_binding_event", _must_not_wait)
    monkeypatch.setattr(connection_mod.socketsec, "get_peer_pid", lambda _w: 4321)
    audited: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        connection_mod,
        "_audit_declaring_agent_denied",
        lambda *a: audited.append(a),
    )

    conn = connection_mod._StubConn(
        stub_uuid="s-cli-local",
        ancestor_pids=[RUNTIME_PID],
        pool_label="",
        caller=None,
        stub_session_token="tok-cli-local",
    )
    conn.attested_pids = (RUNTIME_PID,)  # the owner pid (os.getpid()) is NOT here
    assert not conn.bound_agents
    with caplog.at_level("WARNING", logger=connection_mod.logger.name):
        resolved = await connection_mod._await_declaring_agent(
            OWN_AGENT, "", conn, "s-cli-local", None, MagicMock(), server_name=SERVER
        )
    assert resolved == ""
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert repr(SERVER) in warnings[0]
    assert "dashboard" in warnings[0]
    # One SEL audit event for the refusal, with the stub uuid and the framed
    # agent, and never the bearer token.
    assert len(audited) == 1
    peer_pid, stub_uuid, frame_agent, reason = audited[0]
    assert (peer_pid, stub_uuid, frame_agent) == (4321, "s-cli-local", OWN_AGENT)
    assert reason and "tok-cli-local" not in reason


@pytest.mark.asyncio
async def test_a_binding_that_lands_during_the_owner_check_is_not_refused(monkeypatch) -> None:
    """The owner check hops to a thread; a replayed binding can land meanwhile.

    The stub is not the owner, so ``_declaring_agent`` awaits the owner check.
    The genuine binding arrives during that await. The framed agent is then in
    the bound set and must be taken, not refused as forged.
    """
    conn = connection_mod._StubConn(
        stub_uuid="s-race",
        ancestor_pids=[RUNTIME_PID],
        pool_label="",
        caller=None,
        stub_session_token=TOKEN,
    )

    def _not_a_successor_while_the_binding_lands(_owner, _peer):
        # Runs on the owner check's thread hop, where a replayed binding lands.
        conn.bound_agents = frozenset({OWN_AGENT})
        return False

    monkeypatch.setattr(gw, "_OWNER_PID", os.getpid() + 100000)
    monkeypatch.setattr(control.socketsec, "get_peer_pid", lambda _w: RUNTIME_PID)
    monkeypatch.setattr(
        control.platform_compat, "is_exec_supervisor_of", _not_a_successor_while_the_binding_lands
    )
    assert await connection_mod._declaring_agent(OWN_AGENT, conn, MagicMock()) == OWN_AGENT


@pytest.mark.asyncio
async def test_a_pooled_register_naming_an_unbound_agent_is_audited(daemon, monkeypatch) -> None:
    """A pooled stub never enters the private wait, so its refusal is audited at register."""
    audited: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        connection_mod, "_audit_declaring_agent_denied", lambda *a: audited.append(a)
    )
    pairs = _write_sidecar(OTHER_AGENT)
    with monkeypatch.context() as stub_side:
        _stubs_are_not_the_owner(stub_side)
        frame = _register(OTHER_AGENT, pairs, poolable=True)
        _reader, writer = await daemon.register_and_ensure(frame)
    assert daemon.acquired[-1]["declaring_agent"] == ""
    assert len(audited) == 1
    _peer_pid, stub_uuid, frame_agent, reason = audited[0]
    assert (stub_uuid, frame_agent) == (frame["stub_uuid"], OTHER_AGENT)
    assert TOKEN not in reason
    await _settle(writer)


@pytest.mark.asyncio
async def test_a_daemon_slower_than_five_seconds_still_gets_its_bindings_replayed(
    monkeypatch,
) -> None:
    """The replay waits as long as the startup path does, not just the 5s fast poll.

    The daemon's socket comes up 5.5s after the spawn: a replay that gave up at
    the 5s fast poll would leave its reconnecting stubs with no declared env.
    """
    from types import SimpleNamespace

    from kiro_crew.mcp_gateway import manager as manager_mod

    ready_at = manager_mod._SOCKET_READY_TIMEOUT_SECS + 0.5
    waits: list[float] = []

    async def _wait_for_socket(_path, timeout):
        waits.append(timeout)
        return timeout >= ready_at

    replayed: list[str] = []

    async def _replay(socket_path):
        replayed.append(socket_path)
        return 1

    monkeypatch.setattr(claim_mod, "replay_agent_bindings", _replay)
    mgr = manager_mod.GatewayManager.__new__(manager_mod.GatewayManager)
    mgr._spec = SimpleNamespace(socket_path=Path("/s"))
    monkeypatch.setattr(mgr, "_wait_for_socket", _wait_for_socket)
    await mgr._replay_bindings_when_ready()
    assert waits == [manager_mod._SOCKET_READY_SLOW_START_SECS]
    assert replayed == [str(Path("/s"))]


def test_the_daemon_binding_table_evicts_dead_sessions_before_a_live_one(monkeypatch) -> None:
    """Over the cap, only bindings whose runtime pid is gone are dropped.

    The oldest binding belongs to a live session, so it survives newer dead ones,
    as the gateway's own cache keeps it (``claim._evict_dead_bindings_over_cap``).
    """
    from kiro_crew.mcp_gateway.daemon import identity as identity_mod

    live, dead = RUNTIME_PID, OTHER_RUNTIME_PID
    monkeypatch.setattr(identity_mod, "_MAX_TOKEN_BINDINGS", 2)
    monkeypatch.setattr(identity_mod.platform_compat, "pid_exists", lambda pid: pid == live)
    gw._AGENT_BINDINGS.clear()
    gw._bind_agents("tok-live-old", frozenset({OWN_AGENT}), live)
    gw._bind_agents("tok-dead-1", frozenset({OWN_AGENT}), dead)
    gw._bind_agents("tok-dead-2", frozenset({OWN_AGENT}), dead)
    gw._bind_agents("tok-live-new", frozenset({OWN_AGENT}), live)
    assert list(gw._AGENT_BINDINGS) == ["tok-live-old", "tok-live-new"]


def test_the_daemon_binding_table_drops_the_oldest_live_one_with_a_warning(
    monkeypatch, caplog
) -> None:
    """When every entry over the cap is live, the oldest goes and a WARNING says so."""
    from kiro_crew.mcp_gateway.daemon import identity as identity_mod

    monkeypatch.setattr(identity_mod, "_MAX_TOKEN_BINDINGS", 2)
    monkeypatch.setattr(identity_mod.platform_compat, "pid_exists", lambda _pid: True)
    gw._AGENT_BINDINGS.clear()
    for token in ("tok-a", "tok-b", "tok-c"):
        gw._bind_agents(token, frozenset({OWN_AGENT}), RUNTIME_PID)
    caplog.clear()  # tok-c's bind already evicted tok-a
    with caplog.at_level("WARNING"):
        gw._bind_agents("tok-d", frozenset({OWN_AGENT}), RUNTIME_PID)
    assert list(gw._AGENT_BINDINGS) == ["tok-c", "tok-d"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert identity_mod._mount_key("tok-b")[:16] in warnings[0]
    assert "tok-b" not in warnings[0], "the bearer token itself is never logged"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (b'{"type":"agent-bound","agents":1}\n', True),
        (b'{"type":"agent-binding-rejected"}\n', False),
    ],
)
async def test_the_binding_send_reads_the_daemons_ack(monkeypatch, reply, expected) -> None:
    """The real frame goes out on the socket; only an ``agent-bound`` reply is success."""
    sent: list[bytes] = []
    reader = asyncio.StreamReader()
    reader.feed_data(reply)
    reader.feed_eof()
    writer = MagicMock()
    writer.write = lambda data: sent.append(data)
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock(side_effect=OSError("closed"))

    async def _connect(_path):
        return reader, writer

    monkeypatch.setattr(claim_mod.transport, "connect", _connect)
    ok = await claim_mod._send_agent_binding_inner(
        "/s", RUNTIME_PID, TOKEN, frozenset({OWN_AGENT}), "start-1"
    )
    assert ok is expected
    (frame,) = [json.loads(line) for line in sent]
    assert frame["type"] == "agent-binding"
    assert frame["agents"] == [OWN_AGENT] and frame["pid"] == RUNTIME_PID
    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_a_binding_send_that_cannot_connect_reports_failure(monkeypatch) -> None:
    """A daemon that is down answers False, and the binding is still remembered for replay."""

    async def _down(*_a, **_k):
        raise OSError("no daemon")

    monkeypatch.setattr(claim_mod, "_send_agent_binding_inner", _down)
    claim_mod._BOUND_AGENTS.pop(TOKEN, None)
    assert not await claim_mod.send_agent_binding(
        "/s", RUNTIME_PID, TOKEN, frozenset({OWN_AGENT}), pid_start_id=None
    )
    assert TOKEN in claim_mod._BOUND_AGENTS


@pytest.mark.asyncio
async def test_a_late_binding_keeps_the_server_with_a_valid_initialize(daemon, monkeypatch) -> None:
    """A late binding replaces the agent-less backend just before the replayed
    initialize, and the session keeps the server.

    After a gatewayd restart the stub's backend was acquired agent-less; the
    owner's binding then lands, and the next frame is the replayed initialize.
    The replacement is primed from that very initialize, so the stub is answered
    from its cached handshake, not the generic BackendGone retry error -- which
    ``_replay_initialize`` would treat as terminal and drop the server over.
    """
    pairs = _write_sidecar(OWN_AGENT)
    with monkeypatch.context() as stub_side:
        _stubs_are_not_the_owner(stub_side)
        _reader, writer = await daemon.register_and_ensure(_register(OWN_AGENT, pairs))
    assert daemon.acquired[-1]["declaring_agent"] == ""
    (conn,) = gw._CONN_INDEX[RUNTIME_PID]

    monkeypatch.setattr(connection_mod, "_relabel_stub", AsyncMock())
    replacement = MagicMock()
    replacement.forward_from_stub = AsyncMock()
    dummy_writer_task = asyncio.ensure_future(asyncio.Event().wait())
    recovered = (replacement, asyncio.Queue(), dummy_writer_task)
    monkeypatch.setattr(gw, "_respawn_backend_for_stub", AsyncMock(return_value=recovered))

    reply = await daemon.control(_binding([OWN_AGENT]))
    assert reply == {"type": "agent-bound", "agents": 1, "connections": 1}

    # The replayed initialize: the binding is picked up this frame, the backend
    # is relabeled, restart_for_agent fires, and the replacement answers the
    # handshake rather than a retry error.
    writer.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    await writer.drain()
    # The connection stays open and sends NO error for the initialize: the next
    # socket frame is the pong, not a jsonrpc error for id 1. (The handshake
    # itself is delivered through the replacement's inbox, not this socket.)
    writer.write(b'{"type":"ping"}\n')
    await writer.drain()
    frame = json.loads(await finished(_reader.readline(), "the next daemon frame"))
    assert frame == {"type": "pong"}, frame
    replacement.forward_from_stub.assert_awaited_once()
    assert replacement.forward_from_stub.await_args.args[1].get("method") == "initialize"

    dummy_writer_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await dummy_writer_task
    await _settle(writer)


@pytest.mark.asyncio
async def test_launch_mounted_stubs_are_bound_from_the_launch_roster(monkeypatch, tmp_path) -> None:
    """A launch harness mounts its stubs off the wire array, so the roster binds them.

    Strands, pi and claude send an empty ``session/new`` ``mcpServers`` and put
    their stubs in ``_launch_mcp_servers``; the binding reads them from there.
    """
    from types import SimpleNamespace

    from kiro_crew.acp import client_transport
    from kiro_crew.mcp_gateway.rewriter_spec import _refusing_stub_entry

    sent: list[frozenset[str]] = []

    async def _bind(socket_path, pid, token, agents, **_kw):
        sent.append(agents)
        return True

    monkeypatch.setattr(client_transport, "send_agent_binding", _bind)
    entry = _refusing_stub_entry(server_name=SERVER, agent_name=OWN_AGENT, work_dir=tmp_path)
    stub = {"name": SERVER, "command": entry["command"], "args": entry["args"]}
    client = SimpleNamespace(
        _process=SimpleNamespace(pid=RUNTIME_PID),
        _stub_session_token=TOKEN,
        _mcp_gateway_socket="/s",
        _launch_mcp_servers=[stub],
    )
    # The wire array is empty, as a launch harness sends it.
    await client_transport._bind_stub_agents(client, {"mcpServers": []})
    assert sent == [frozenset({OWN_AGENT})]


@pytest.mark.asyncio
async def test_a_private_backend_waits_for_a_late_binding_then_gets_its_env(
    daemon, monkeypatch
) -> None:
    """A daemon-restart race: the stub registers before its binding is replayed.

    The private backend must not spawn with an empty declared env; it waits for
    the binding, then reads the agent's sidecar. The binding lands mid-wait the
    way ``_apply_agent_binding`` lands it: record it, refresh the live
    connection, wake the waiter.
    """
    pairs = _write_sidecar(OWN_AGENT)
    _stubs_are_not_the_owner(monkeypatch)
    # Owner-descended: the owning gateway's pid is in the stub's attested host
    # chain ([RUNTIME_PID] here), so a late binding can still arrive and the
    # private backend waits for it (the stub's own Register still does not vouch
    # as the owner -- the socket peer is the test process, not RUNTIME_PID).
    monkeypatch.setattr(gw, "_OWNER_PID", RUNTIME_PID)
    held: dict[str, Any] = {}

    async def _do_register() -> None:
        _reader, writer = await daemon.register_and_ensure(_register(OWN_AGENT, pairs))
        held["writer"] = writer

    task = asyncio.ensure_future(_do_register())
    try:
        # Wait until the register is parked in _await_declaring_agent, then land
        # the binding, so the test proves the wait and not a lucky ordering.
        await until_parked(gw._agent_binding_event(), count=1)
        gw._bind_agents(TOKEN, frozenset({OWN_AGENT}), RUNTIME_PID, None)
        gw._refresh_bound_agents(TOKEN, RUNTIME_PID)
        gw._wake_agent_binding_waiters()
        await finished(task, "the waiting register completes")

        (seen,) = daemon.acquired
        assert seen["declaring_agent"] == OWN_AGENT
        assert (
            gw._declared_env_for_private_backend(seen["pool_key"], declaring_agent=OWN_AGENT)
            == pairs
        )
        # The retained Register a resource-subscription holder is built from now
        # carries the resolved agent, so the holder's own private backend reads
        # the declared env too (session_describe -> holder_register_frame).
        (conn,) = [c for c in session_describe._LIVE_CONNS if c.stub_session_token == TOKEN]
        assert conn.register is not None and conn.register["agent_name"] == OWN_AGENT
    finally:
        if not task.done():
            task.cancel()
        if "writer" in held:
            await _settle(held["writer"])


@pytest.mark.asyncio
async def test_the_wait_times_out_fail_closed_when_no_binding_arrives(daemon, monkeypatch) -> None:
    """No binding ever arrives: the backend starts fail-closed with no agent."""
    pairs = _write_sidecar(OWN_AGENT)
    _stubs_are_not_the_owner(monkeypatch)
    _reader, writer = await daemon.register_and_ensure(_register(OWN_AGENT, pairs))
    await _settle(writer)

    (seen,) = daemon.acquired
    assert seen["declaring_agent"] == ""
    assert gw._declared_env_for_private_backend(seen["pool_key"], declaring_agent="") == {}


@pytest.mark.asyncio
async def test_replay_resends_every_live_binding(monkeypatch) -> None:
    """The gateway re-sends every remembered binding to a restarted daemon."""
    sent: list[tuple[int, str, frozenset[str]]] = []

    async def _inner(socket_path, pid, token, agents, pid_start_id, confined=False):
        sent.append((pid, token, agents))
        return True

    monkeypatch.setattr(claim_mod, "_send_agent_binding_inner", _inner)
    claim_mod._BOUND_AGENTS.clear()
    assert await claim_mod.send_agent_binding("/s", RUNTIME_PID, TOKEN, frozenset({OWN_AGENT}))
    assert await claim_mod.send_agent_binding(
        "/s", OTHER_RUNTIME_PID, "u" * 64, frozenset({OTHER_AGENT})
    )
    sent.clear()

    count = await claim_mod.replay_agent_bindings("/s")
    assert count == 2
    assert (RUNTIME_PID, TOKEN, frozenset({OWN_AGENT})) in sent
    assert (OTHER_RUNTIME_PID, "u" * 64, frozenset({OTHER_AGENT})) in sent
    assert await claim_mod.replay_agent_bindings(None) == 0


@pytest.mark.asyncio
async def test_replay_carries_the_original_process_generation(monkeypatch) -> None:
    """A replay sends the start token cached at first send, not a fresh read.

    So a recycled pid is never re-authorized as the original runtime.
    """
    start_ids: list[Any] = []

    async def _inner(socket_path, pid, token, agents, pid_start_id, confined=False):
        start_ids.append(pid_start_id)
        return True

    monkeypatch.setattr(claim_mod, "_send_agent_binding_inner", _inner)
    # First send reads the start token once; pin it to a known generation.
    monkeypatch.setattr(claim_mod.platform_compat, "get_process_start_id", lambda _pid: "gen-1")
    claim_mod._BOUND_AGENTS.clear()
    assert await claim_mod.send_agent_binding("/s", RUNTIME_PID, TOKEN, frozenset({OWN_AGENT}))
    assert start_ids == ["gen-1"]

    # The pid is recycled: a fresh read would now return a different generation.
    monkeypatch.setattr(claim_mod.platform_compat, "get_process_start_id", lambda _pid: "gen-2")
    start_ids.clear()
    assert await claim_mod.replay_agent_bindings("/s") == 1
    assert start_ids == ["gen-1"], "replay must carry the cached generation, not re-read it"


def _agent_binding_audits(sink: MagicMock) -> list[dict[str, Any]]:
    """The kwargs of each ``mcp-gateway.agent_binding`` audit row, in order."""
    return [
        e.kwargs
        for e in sink.log_api_access.call_args_list
        if e.kwargs.get("operation") == "mcp-gateway.agent_binding"
    ]


@pytest.mark.asyncio
async def test_an_accepted_binding_writes_one_audit_row_without_the_token(monkeypatch) -> None:
    sink = MagicMock()
    monkeypatch.setattr(gw, "SecurityEventLog", lambda: sink)
    monkeypatch.setattr(gw.socketsec, "get_peer_pid", lambda _w: os.getpid())
    monkeypatch.setattr(gw, "_OWNER_PID", os.getpid())

    reply = await gw._apply_agent_binding(_binding([OWN_AGENT]), MagicMock())
    assert reply["type"] == "agent-bound"

    rows = _agent_binding_audits(sink)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "allowed"
    assert OWN_AGENT in rows[0]["resources"]
    assert TOKEN not in str(rows[0])


@pytest.mark.asyncio
async def test_a_refused_binding_writes_one_audit_row_without_the_token(monkeypatch) -> None:
    sink = MagicMock()
    monkeypatch.setattr(gw, "SecurityEventLog", lambda: sink)
    monkeypatch.setattr(gw.socketsec, "get_peer_pid", lambda _w: os.getpid() + 100000)
    monkeypatch.setattr(gw, "_OWNER_PID", os.getpid())
    monkeypatch.setattr(control.platform_compat, "is_exec_supervisor_of", lambda *_a: False)

    reply = await gw._apply_agent_binding(_binding([OWN_AGENT]), MagicMock())
    assert reply["type"] == "agent-binding-rejected"

    rows = _agent_binding_audits(sink)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "denied"
    assert TOKEN not in str(rows[0])


@pytest.mark.asyncio
async def test_a_malformed_binding_writes_one_audit_row_without_the_token(monkeypatch) -> None:
    sink = MagicMock()
    monkeypatch.setattr(gw, "SecurityEventLog", lambda: sink)
    monkeypatch.setattr(gw.socketsec, "get_peer_pid", lambda _w: os.getpid())

    frame = _binding([OWN_AGENT])
    frame["agents"] = "not-a-list"  # malformed
    reply = await gw._apply_agent_binding(frame, MagicMock())
    assert reply["type"] == "agent-binding-rejected"

    rows = _agent_binding_audits(sink)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "denied"
    assert TOKEN not in str(rows[0])


@pytest.mark.asyncio
async def test_a_live_session_binding_survives_the_backstop_and_is_replayed(monkeypatch) -> None:
    """Past the cap, a live session's binding is kept and still replayed.

    The backstop evicts only entries whose runtime process is gone, never a
    live one, so a long-lived session does not lose its declared env after a
    daemon restart just because many newer sessions bound since.
    """
    sent: list[int] = []

    async def _inner(socket_path, pid, token, agents, pid_start_id, confined=False):
        sent.append(pid)
        return True

    monkeypatch.setattr(claim_mod, "_send_agent_binding_inner", _inner)
    # The oldest session is live; the rest are gone. Liveness is the eviction
    # signal, pinned here so the test does not depend on real pid lifetimes.
    live_pid, dead_pid = 1000, 1001
    monkeypatch.setattr(claim_mod.platform_compat, "pid_exists", lambda pid: pid == live_pid)
    claim_mod._BOUND_AGENTS.clear()

    assert await claim_mod.send_agent_binding(
        "/s", live_pid, "live-token", frozenset({OWN_AGENT}), pid_start_id="gen"
    )
    for i in range(claim_mod.MAX_SESSION_BINDINGS + 2):
        await claim_mod.send_agent_binding(
            "/s", dead_pid, f"dead-{i}", frozenset({OTHER_AGENT}), pid_start_id="gen"
        )

    assert "live-token" in claim_mod._BOUND_AGENTS, "the live session must not be evicted"
    assert len(claim_mod._BOUND_AGENTS) <= claim_mod.MAX_SESSION_BINDINGS

    sent.clear()
    await claim_mod.replay_agent_bindings("/s")
    assert live_pid in sent, "the live session's binding is replayed after a restart"


def test_forget_agent_binding_drops_only_that_session(monkeypatch) -> None:
    claim_mod._BOUND_AGENTS.clear()
    claim_mod._BOUND_AGENTS["a"] = (1, "g", frozenset({OWN_AGENT}))
    claim_mod._BOUND_AGENTS["b"] = (2, "g", frozenset({OTHER_AGENT}))
    claim_mod.forget_agent_binding("a")
    assert "a" not in claim_mod._BOUND_AGENTS
    assert "b" in claim_mod._BOUND_AGENTS
    claim_mod.forget_agent_binding("")  # no-op, no raise

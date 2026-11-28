"""gatewayd refuses a caller served from inside a confined runtime.

A runtime fenced tighter than any backend gatewayd starts (a stricter sandbox
tier, or an enforced harness under the credential mask) says so in the claim
its owning gateway pushes. These tests drive the real ``_handle_connection``
over a real unix socket and check that:

* a Register from under that runtime, with its own token, no token, or a token
  another claim named, is rejected with a fallback class and never reaches a
  backend,
* the flag is taken only from the daemon's owner, and a later claim without it
  does not clear it,
* a stub that registered before the confining claim is dropped on its next frame,
* a recycled runtime pid is not taken for the confined runtime, and
* the gateway sets the flag from the runtime's own tier.
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

from kiro_crew import sandbox
from kiro_crew.mcp_gateway import claim as claim_mod
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import launch_approval, runtime_confinement
from kiro_crew.mcp_gateway.daemon import control, session_describe
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.testing.ids import unallocatable_pids
from kiro_crew.testing.wait import finished

pytestmark = [
    pytest.mark.xdist_group("mcp_gateway"),
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="the daemon under test listens on a Unix socket (gatewayd is unix-only)",
    ),
]

#: The approval gate is stubbed by ``_clean_globals`` below, on purpose and visibly.
ENFORCE_LAUNCH_APPROVAL = True

TOKEN = "c" * 64
OTHER_TOKEN = "d" * 64
RUNTIME_PID, OTHER_RUNTIME_PID = unallocatable_pids(2)


def _tables() -> tuple[Any, ...]:
    return (
        gw._CONN_INDEX,
        gw._STUB_PROBES,
        gw._TOKEN_BINDINGS,
        gw._AGENT_BINDINGS,
        gw._CONFINED_RUNTIMES,
        gw._CONFINED_TOKENS,
    )


@pytest.fixture(autouse=True)
def _clean_globals(_floor_monkeypatch, tmp_path):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    for table in _tables():
        table.clear()
    session_describe._LIVE_CONNS.clear()
    # The test process is the gateway that owns the daemon.
    _floor_monkeypatch.setattr(gw, "_OWNER_PID", os.getpid())
    # The kernel-attested host chain of every registering peer: under RUNTIME_PID.
    _floor_monkeypatch.setattr(gw, "_resolve_peer_identity", lambda _pid: ("", [RUNTIME_PID]))
    _floor_monkeypatch.setattr(launch_approval, "launch_approved", lambda *_a, **_k: True)
    yield
    for table in _tables():
        table.clear()
    session_describe._LIVE_CONNS.clear()


def _register(token: str = TOKEN) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "register",
        "stub_uuid": str(uuid.uuid4()),
        "server_name": "memory",
        "command_args_hash": "c" * 16,
        "effective_env_hash": "e" * 16,
        "work_dir": "/tmp",
        "binary_version": "1",
        "os_uid": 1000,
        "poolable": True,
        "ancestor_pids": [RUNTIME_PID],
    }
    if token:
        frame["stub_session_token"] = token
    return frame


def _claim(pid: int = RUNTIME_PID, token: str = TOKEN, confined: bool = True) -> dict[str, Any]:
    frame = claim_mod.build_claim_frame(pid, "dashboard:strict", None, token)
    frame["pid_start_id"] = None
    if confined:
        frame["confined"] = True
    return frame


def _not_the_owner(monkeypatch) -> None:
    """From here on, connections come from a process other than the daemon's owner."""
    monkeypatch.setattr(gw, "_OWNER_PID", os.getpid() + 100000)
    monkeypatch.setattr(control.platform_compat, "is_exec_supervisor_of", lambda *_a: False)


class _Daemon:
    def __init__(self, sock: Path, acquired: list[dict[str, Any]]) -> None:
        self.sock = sock
        self.acquired = acquired
        self._writers: list[asyncio.StreamWriter] = []

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_unix_connection(str(self.sock))
        self._writers.append(writer)
        return reader, writer

    async def close_clients(self) -> None:
        for writer in self._writers:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def send(self, frame: dict[str, Any]) -> tuple[Any, Any, dict[str, Any]]:
        reader, writer = await self._open()
        writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
        reply = json.loads(await finished(reader.readline(), "the daemon's reply"))
        return reader, writer, reply


@pytest_asyncio.fixture
async def daemon(monkeypatch) -> AsyncIterator[_Daemon]:
    acquired: list[dict[str, Any]] = []
    backend = MagicMock()
    backend.attach_stub = AsyncMock(side_effect=lambda *_a, **_k: asyncio.Queue())
    backend.detach_stub = AsyncMock(return_value=0)
    backend.cancel_in_flight_for_stub = AsyncMock(return_value=[])
    backend.recycle_if_idle = AsyncMock()
    backend.forward_from_stub = AsyncMock()
    backend.quarantined = False
    backend._pending_requests = {}

    async def _acquire(pool, pool_key, resolver, **kw):
        acquired.append({"pool_key": pool_key, **kw})
        return backend, True

    monkeypatch.setattr(gw, "_acquire_backend", _acquire)
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
            # As ``run_gatewayd``'s own handler does once a connection is over.
            writer.close()

    with tempfile.TemporaryDirectory(
        prefix=SHORT_TMP_PREFIX + "confine-", dir=short_tmp_base()
    ) as sock_dir:
        sock = Path(sock_dir) / "gw.sock"
        server = await asyncio.start_unix_server(_serve, path=str(sock))
        daemon_obj = _Daemon(sock, acquired)
        try:
            yield daemon_obj
        finally:
            await daemon_obj.close_clients()
            server.close()
            if handlers:
                await asyncio.gather(*handlers, return_exceptions=True)


def _assert_refused(reply: dict[str, Any], daemon: _Daemon) -> None:
    assert reply["type"] == "rejected", reply
    assert reply["class"] == "isolation" and reply["fallback"] is True, reply
    assert daemon.acquired == []
    assert RUNTIME_PID not in gw._CONN_INDEX


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [TOKEN, "", OTHER_TOKEN], ids=["own", "none", "other"])
async def test_a_register_from_a_confined_runtime_is_refused(daemon, monkeypatch, token) -> None:
    assert (await daemon.send(_claim()))[2]["type"] == "claim-noop"
    if token == OTHER_TOKEN:
        # Another session's token, which a claim named for a runtime this peer
        # is NOT under: the runtime the peer is under still decides.
        other = _claim(OTHER_RUNTIME_PID, OTHER_TOKEN, confined=False)
        assert (await daemon.send(other))[2]["type"] == "claim-noop"
    _not_the_owner(monkeypatch)

    _r, _w, reply = await daemon.send(_register(token))

    _assert_refused(reply, daemon)


@pytest.mark.asyncio
async def test_a_confined_token_is_refused_from_any_runtime(daemon, monkeypatch) -> None:
    assert (await daemon.send(_claim(OTHER_RUNTIME_PID)))[2]["type"] == "claim-noop"
    _not_the_owner(monkeypatch)

    _r, _w, reply = await daemon.send(_register(TOKEN))

    _assert_refused(reply, daemon)


@pytest.mark.asyncio
async def test_the_flag_from_a_sender_that_is_not_the_owner_is_ignored(daemon, monkeypatch) -> None:
    _not_the_owner(monkeypatch)
    assert (await daemon.send(_claim()))[2]["type"] == "claim-noop"
    assert not gw._CONFINED_RUNTIMES and not gw._CONFINED_TOKENS

    _r, _w, reply = await daemon.send(_register())

    assert reply["type"] == "registered", reply


@pytest.mark.asyncio
async def test_a_later_claim_without_the_flag_does_not_clear_it(daemon, monkeypatch) -> None:
    assert (await daemon.send(_claim()))[2]["type"] == "claim-noop"
    assert (await daemon.send(_claim(confined=False)))[2]["type"] == "claim-noop"
    _not_the_owner(monkeypatch)
    assert (await daemon.send(_claim(confined=False)))[2]["type"] == "claim-noop"

    _r, _w, reply = await daemon.send(_register())

    _assert_refused(reply, daemon)


@pytest.mark.asyncio
async def test_a_stub_registered_before_the_confining_claim_is_dropped(daemon, monkeypatch) -> None:
    reader, writer, reply = await daemon.send(_register())
    assert reply["type"] == "registered", reply

    assert (await daemon.send(_claim()))[2]["type"] == "claimed"
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    writer.write((json.dumps(request) + "\n").encode())
    await writer.drain()

    assert await finished(reader.readline(), "the daemon closing the connection") == b""
    assert daemon.acquired == []


@pytest.mark.asyncio
async def test_a_recycled_runtime_pid_is_not_the_confined_runtime(daemon, monkeypatch) -> None:
    claim = _claim()
    claim["pid_start_id"] = "start-of-the-confined-runtime"
    assert (await daemon.send(claim))[2]["type"] == "claim-noop"
    monkeypatch.setattr(gw, "_get_process_start_id", lambda _pid: "start-of-a-new-process")
    _not_the_owner(monkeypatch)

    # No token: only the runtime pid could tie this peer to the confined claim.
    _r, _w, reply = await daemon.send(_register(""))

    assert reply["type"] == "registered", reply


@pytest.mark.parametrize(
    ("acp_backend", "mode", "confined"),
    [
        ("kas", "strict", True),
        ("kas", "cc", True),
        ("kas", "standard", False),
        # An enforced harness whose credential mask applies at its tier.
        ("codex", "standard", True),
        ("", "", False),
    ],
)
def test_the_claim_frame_carries_the_runtime_tier(monkeypatch, acp_backend, mode, confined) -> None:
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr("kiro_crew.sandbox_spawn.credential_mask_applies", lambda _mode: True)

    frame = claim_mod.build_claim_frame(
        RUNTIME_PID, "dashboard:x", None, TOKEN, acp_backend=acp_backend, sandbox_mode=mode
    )

    assert frame.get("confined", False) is confined


def test_an_unreadable_tier_is_confined(monkeypatch) -> None:
    def _boom(_mode: str) -> str:
        raise RuntimeError("governance profile unreadable")

    monkeypatch.setattr(sandbox, "effective_sandbox_mode", _boom)

    assert runtime_confinement.runtime_confined("kas", "standard") is True


def test_over_the_cap_only_gone_runtimes_are_forgotten(monkeypatch) -> None:
    from kiro_crew.mcp_gateway.daemon import identity

    live_pid, gone_pid, new_pid = 101, 102, 103
    monkeypatch.setattr(identity, "_MAX_TOKEN_BINDINGS", 2)
    monkeypatch.setattr(identity.platform_compat, "pid_exists", lambda pid: pid != gone_pid)
    identity._confine("live", live_pid, None)
    identity._confine("gone", gone_pid, None)

    identity._confine("new", new_pid, None)

    assert list(identity._CONFINED_RUNTIMES) == [live_pid, new_pid]
    assert list(identity._CONFINED_TOKENS) == ["live", "new"]
    # A live runtime is kept even past the cap.
    identity._confine("newer", 104, None)
    assert len(identity._CONFINED_RUNTIMES) == 3


def _binding(confined: bool) -> dict[str, Any]:
    frame = claim_mod.build_agent_binding_frame(
        RUNTIME_PID, TOKEN, frozenset(), None, confined=confined
    )
    return frame


@pytest.mark.asyncio
async def test_an_owner_binding_confines_before_any_stub_registers(daemon, monkeypatch) -> None:
    """The binding is awaited before ``session/new``, which a claim is not on every path."""
    assert (await daemon.send(_binding(confined=True)))[2]["type"] == "agent-bound"
    _not_the_owner(monkeypatch)

    _r, _w, reply = await daemon.send(_register(""))

    _assert_refused(reply, daemon)


@pytest.mark.asyncio
async def test_an_unconfined_binding_confines_nothing(daemon, monkeypatch) -> None:
    assert (await daemon.send(_binding(confined=False)))[2]["type"] == "agent-bound"
    _not_the_owner(monkeypatch)

    _r, _w, reply = await daemon.send(_register())

    assert reply["type"] == "registered", reply


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "sent"), [("strict", True), ("standard", False)])
async def test_a_confined_runtime_is_named_even_with_no_stub_agents(
    monkeypatch, mode, sent
) -> None:
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    frames: list[bool] = []

    async def _inner(socket_path, pid, token, agents, pid_start_id, confined=False):
        frames.append(confined)
        return True

    monkeypatch.setattr(claim_mod, "_send_agent_binding_inner", _inner)

    ok = await claim_mod.send_agent_binding(
        "/s", RUNTIME_PID, TOKEN, frozenset(), acp_backend="kas", sandbox_mode=mode
    )

    assert ok is sent
    assert frames == ([True] if sent else [])
    if sent:
        # A replay, which carries no tier, still names the runtime confined.
        assert await claim_mod.replay_agent_bindings("/s") == 1
        assert frames == [True, True]

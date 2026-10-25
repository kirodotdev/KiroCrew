"""A pooled Kiro Crew MCP backend authenticates against the gateway that owns it.

The reported failure: in a Codex-backed dashboard session, ``kirocrew-core`` refused
``memory_recall`` with ``resolution_failed (cause: URLError)`` and
``kirocrew-dashboard`` answered ``internal_auth_mismatch``. Both servers read the
gateway port from their own environment and pair it with the credential published
for that port, so the port decides which INSTANCE they authenticate against.

A pooled control-plane backend is spawned by the MCP gateway daemon, from the
daemon's environment, and the daemon is started during gateway boot BEFORE the
dashboard binds and exports ``KIROCREW_BOUND_PORT``. It never inherits the bound
port; it does inherit any ``KIROCREW_PORT`` the gateway's shell carried. A gateway
started from another gateway's agent shell therefore pointed every pooled
backend at THAT gateway.

The fix: the owning gateway announces the port its listener bound over the
daemon's control socket (``owner-port``), and the daemon binds every pooled
control-plane backend to it. Covered here:

* who may announce, and which ports (the frame's acceptance rules), on an owned
  daemon and on an unowned one, where only the port's listener may;
* the bind itself, with and without an announced port;
* an acquisition before the announce waits and then binds, and past its budget is
  a ``capacity`` refusal that never tells the stub to exec the server itself;
* the prewarm pass skips a control-plane key until the announce;
* a new port retires the control-plane backends bound to the old one, and the
  pool hands none of them out again;
* the manager announces, re-announces, and puts the port on a respawn's argv;
* the reported topology, end to end through the real resolver chain, and that the
  bound backend sends instance A's credential to instance A.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import socket
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_mcp_gateway_claim import _register
from test_mcp_gateway_spawn_gate import peer_ok  # noqa: F401 - a fixture the wire tests use
from test_mcp_gateway_spawn_gate import (
    _admission,
    _await_state,
    _fake_backend,
    _fake_pool,
    _FakeWriter,
    _register_frame,
    _ScriptedReader,
    _within_ceiling,
)

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew import code_fingerprint as cf
from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import launch_approval
from kiro_crew.mcp_gateway import manager as mgr
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.daemon import launch as daemon_launch
from kiro_crew.mcp_gateway.daemon import warm_pool
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey
from kiro_crew.port_resolution import resolve_client_port_src
from kiro_crew.testing.wait import DEFAULT_INTERVAL_SECS, FALLBACK_TIMEOUT_SECS

OWNER_PORT = 47211  # instance A: the gateway that owns the daemon
FOREIGN_PORT = 47299  # instance B: the gateway whose shell started A
MOVED_PORT = 47233  # instance A again, after an in-app restart under --port auto
SUCCESSOR_PID = 424242  # the gateway an in-app restart through a launcher left running

#: Launch approval is not this module's subject, but the resolver tests below spawn
#: through ``env_target_resolver``, so the approval they rely on is granted
#: explicitly (:func:`approved_launch`) rather than inherited from the autouse
#: fixture. A test that does not ask for it runs against the real, empty store.
ENFORCE_LAUNCH_APPROVAL = True


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def owner_port_state(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts on a daemon with no owner, no port and no warned server.

    Patched through ``_floor_monkeypatch`` (D11) so a test that calls
    ``monkeypatch.undo()`` cannot lift this reset. ``raising=False`` so that, with
    the fix reverted, each test fails on its own assertion instead of erroring here.
    """
    _floor_monkeypatch.setattr(gw, "_OWNER_PID", 0)
    _floor_monkeypatch.setattr(gw, "_OWNER_PORT", 0, raising=False)
    _floor_monkeypatch.setattr(gw, "_OWNER_PORT_EVENT", None, raising=False)
    _floor_monkeypatch.setattr(daemon_launch, "_OWNER_PORT_DECLINES_WARNED", set(), raising=False)


@pytest.fixture
def approved_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat the resolved target as operator-approved, for the binding tests only.

    Both readers: a direct resolver call loads the store itself, and an acquisition
    hands the resolver its own snapshot.
    """
    monkeypatch.setattr(launch_approval, "launch_approved", lambda *_a, **_k: True)
    monkeypatch.setattr(launch_approval.LaunchApprovals, "admits_launch", lambda *_a, **_k: True)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data home with a ``run/`` dir, and no inherited port variables."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    for name in ("KIROCREW_PORT", "KIROCREW_BOUND_PORT", "KIROCREW_BOUND_HOST"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "run").mkdir(mode=0o700)
    return tmp_path


def _publish_secret(home: Path, port: int, value: str) -> None:
    """The per-port credential a gateway publishes beside its marker (synthetic)."""
    (home / "run" / f"gateway-{port}.secret").write_text(value, encoding="ascii")


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> int:
    """This test process plays the owning gateway."""
    pid = os.getpid()
    monkeypatch.setattr(gw, "_OWNER_PID", pid)
    return pid


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A recording SEL behind the daemon's audit helpers."""
    sink = MagicMock()
    monkeypatch.setattr(gw, "SecurityEventLog", lambda: sink)
    return sink


def _sender(monkeypatch: pytest.MonkeyPatch, pid: int | None, *, successor_of: int = 0) -> list:
    """The kernel reports *pid* on the control connection; *successor_of* supervises it."""
    asked: list[tuple[int, int]] = []

    def supervises(supervisor: int, descendant: int) -> bool:
        asked.append((supervisor, descendant))
        return supervisor == successor_of and descendant == pid

    monkeypatch.setattr(gw.socketsec, "get_peer_pid", lambda _w: pid)
    monkeypatch.setattr(platform_compat, "is_exec_supervisor_of", supervises)
    return asked


def _control_plane_key(name: str = "kirocrew-core") -> PoolKey:
    frame = _register("dashboard:chat-1-1")
    frame["server_name"] = name
    return PoolKey.from_register(frame)


def _resolve_in(env: dict[str, str]) -> tuple[int, str]:
    """Which port a backend spawned with *env* dials, through the real chain."""
    with patch.dict(os.environ, env, clear=True):
        return resolve_client_port_src(None)


def _owner_port_frame(port: Any) -> dict[str, Any]:
    return {"type": "owner-port", "port": port}


async def _announce(port: Any, pool: BackendPool | None = None) -> dict[str, Any]:
    """Serve one ``owner-port`` frame (the sender is whatever :func:`_sender` set)."""
    return await gw._apply_owner_port(
        _owner_port_frame(port), object(), pool if pool is not None else BackendPool(2)
    )


def _listener(monkeypatch: pytest.MonkeyPatch, answer: bool | None) -> list:
    """The listener lookup answers *answer* for every ``(pid, port)`` it is asked."""
    asked: list[tuple[int, int]] = []

    def owns(pid: int, port: int) -> bool | None:
        asked.append((pid, port))
        return answer

    monkeypatch.setattr(platform_compat, "process_owns_loopback_listener", owns)
    return asked


# ── who may announce, and what ─────────────────────────────────────────────


class TestOwnerPortFrame:
    @pytest.mark.asyncio
    async def test_the_owner_sets_the_port(self, owner, monkeypatch, audit):
        _sender(monkeypatch, owner)
        looked_up = _listener(monkeypatch, False)
        reply = await _announce(OWNER_PORT)
        assert reply == {"type": "owner-port-set", "port": OWNER_PORT}
        assert gw._OWNER_PORT == OWNER_PORT
        assert looked_up == [], "an owned daemon keeps its owner rule, not the listener's"
        (event,) = audit.log_api_access.call_args_list
        assert event.kwargs["operation"] == "mcp-gateway.owner_port"
        assert event.kwargs["outcome"] == "allowed"

    @pytest.mark.asyncio
    async def test_the_exec_successor_of_the_owner_sets_the_port(self, owner, monkeypatch, audit):
        """After an in-app restart through a supervising launcher the owner pid is
        the launcher, and the gateway that announces is its child."""
        asked = _sender(monkeypatch, SUCCESSOR_PID, successor_of=owner)
        reply = await _announce(OWNER_PORT)
        assert reply == {"type": "owner-port-set", "port": OWNER_PORT}
        assert asked == [(owner, SUCCESSOR_PID)]

    @pytest.mark.asyncio
    async def test_an_unrelated_same_uid_peer_is_refused(self, owner, monkeypatch, audit, caplog):
        """Listening on the port does not make a peer the owner of an owned daemon."""
        _sender(monkeypatch, SUCCESSOR_PID, successor_of=0)
        _listener(monkeypatch, True)
        with caplog.at_level(logging.WARNING, logger=gw.logger.name):
            reply = await _announce(FOREIGN_PORT)
        assert reply["type"] == "owner-port-rejected"
        assert str(SUCCESSOR_PID) in reply["reason"]
        assert gw._OWNER_PORT == 0
        assert any("owner-port announce refused" in r.getMessage() for r in caplog.records)
        assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("owned", [True, False], ids=["owned", "unowned"])
    async def test_a_sender_whose_pid_cannot_be_read_is_refused(self, monkeypatch, audit, owned):
        monkeypatch.setattr(gw, "_OWNER_PID", os.getpid() if owned else 0)
        asked = _sender(monkeypatch, None, successor_of=os.getpid())
        looked_up = _listener(monkeypatch, True)
        reply = await _announce(OWNER_PORT)
        assert reply["type"] == "owner-port-rejected"
        assert gw._OWNER_PORT == 0
        assert asked == [] and looked_up == [], "nothing to look up without a pid"

    @pytest.mark.asyncio
    async def test_an_unowned_daemon_takes_the_port_from_its_listener(self, monkeypatch, audit):
        """A gateway adopts an unowned daemon without being its owner. It proves
        the port by holding the listener the bound backends will dial."""
        _sender(monkeypatch, SUCCESSOR_PID)
        looked_up = _listener(monkeypatch, True)
        reply = await _announce(OWNER_PORT)
        assert reply == {"type": "owner-port-set", "port": OWNER_PORT}
        assert gw._OWNER_PORT == OWNER_PORT
        assert looked_up == [(SUCCESSOR_PID, OWNER_PORT)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer,said",
        [(False, "does not listen"), (None, "cannot tell")],
        ids=["not-the-listener", "host-cannot-answer"],
    )
    async def test_an_unowned_daemon_refuses_a_sender_it_cannot_see_listening(
        self, monkeypatch, audit, answer, said
    ):
        _sender(monkeypatch, SUCCESSOR_PID)
        _listener(monkeypatch, answer)
        reply = await _announce(FOREIGN_PORT)
        assert reply["type"] == "owner-port-rejected"
        assert said in reply["reason"]
        assert gw._OWNER_PORT == 0
        assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_an_unowned_daemon_reads_the_real_listener_table(self, monkeypatch, audit):
        """The real lookup: this process listens on a loopback port, so its
        announce of that port is taken. With the socket closed this process does
        not listen there, and the same announce is refused."""
        _sender(monkeypatch, os.getpid())
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            if platform_compat.process_owns_loopback_listener(os.getpid(), port) is None:
                pytest.skip("this host cannot report which process listens on a port")
            assert await _announce(port) == {"type": "owner-port-set", "port": port}
        finally:
            server.close()
        monkeypatch.setattr(gw, "_OWNER_PORT", 0)
        assert (await _announce(port))["type"] == "owner-port-rejected"
        assert gw._OWNER_PORT == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("owned", [True, False], ids=["owned", "unowned"])
    @pytest.mark.parametrize("port", [0, 65536, -1, "123", True, None, 1.5, [OWNER_PORT]])
    async def test_a_value_that_is_not_a_port_is_refused(self, monkeypatch, audit, port, owned):
        monkeypatch.setattr(gw, "_OWNER_PID", os.getpid() if owned else 0)
        _sender(monkeypatch, os.getpid())
        looked_up = _listener(monkeypatch, True)
        reply = await _announce(port)
        assert reply["type"] == "owner-port-rejected"
        assert gw._OWNER_PORT == 0
        assert looked_up == []

    @pytest.mark.asyncio
    async def test_a_later_announce_replaces_the_earlier_one(
        self, owner, monkeypatch, audit, caplog
    ):
        _sender(monkeypatch, owner)
        await _announce(OWNER_PORT)
        with caplog.at_level(logging.INFO, logger=gw.logger.name):
            reply = await _announce(FOREIGN_PORT)
        assert reply == {"type": "owner-port-set", "port": FOREIGN_PORT}
        assert gw._OWNER_PORT == FOREIGN_PORT
        assert any(f"from {OWNER_PORT} to {FOREIGN_PORT}" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_frame_is_served_on_the_control_socket(self, owner, monkeypatch, audit):
        _sender(monkeypatch, owner)
        writer = _FakeWriter()
        served = await gw._serve_control_frame(_owner_port_frame(OWNER_PORT), writer, MagicMock())
        assert served is True
        assert writer.frames() == [{"type": "owner-port-set", "port": OWNER_PORT}]

    def test_the_pong_reports_the_port(self, owner, monkeypatch):
        assert gw._pong_payload()["owner_port"] == 0
        monkeypatch.setattr(gw, "_OWNER_PORT", OWNER_PORT)
        assert gw._pong_payload()["owner_port"] == OWNER_PORT


# ── the bind ───────────────────────────────────────────────────────────────


class TestBindControlPlaneToOwner:
    def test_the_daemon_env_alone_dials_the_foreign_instance(self):
        """The defect, stated through the real chain: what a pooled backend
        inherited before the fix. No bound port (the daemon predates the bind) and
        the shell's foreign port first in line."""
        assert _resolve_in({"KIROCREW_PORT": str(FOREIGN_PORT)}) == (FOREIGN_PORT, "env")

    @pytest.mark.parametrize("owned", [True, False], ids=["owned", "unowned"])
    def test_a_known_port_binds_and_drops_a_foreign_inherited_port(self, monkeypatch, owned):
        monkeypatch.setattr(gw, "_OWNER_PID", os.getpid() if owned else 0)
        monkeypatch.setattr(gw, "_OWNER_PORT", OWNER_PORT)
        env = {"KIROCREW_PORT": str(FOREIGN_PORT), "KIROCREW_BOUND_PORT": str(FOREIGN_PORT)}
        assert daemon_launch.bind_control_plane_to_owner(env, "kirocrew-core") == OWNER_PORT
        assert env == {"KIROCREW_BOUND_PORT": str(OWNER_PORT)}
        assert _resolve_in(env) == (OWNER_PORT, "bound")

    def test_the_operators_own_port_is_kept(self, owner, monkeypatch):
        monkeypatch.setattr(gw, "_OWNER_PORT", OWNER_PORT)
        env = {"KIROCREW_PORT": str(OWNER_PORT)}
        daemon_launch.bind_control_plane_to_owner(env)
        assert env == {"KIROCREW_PORT": str(OWNER_PORT), "KIROCREW_BOUND_PORT": str(OWNER_PORT)}

    @pytest.mark.parametrize("owned", [True, False], ids=["owned", "unowned"])
    @pytest.mark.parametrize(
        "inherited",
        [{}, {"KIROCREW_PORT": str(FOREIGN_PORT)}, {"KIROCREW_BOUND_PORT": str(FOREIGN_PORT)}],
    )
    def test_a_daemon_with_no_port_refuses_whatever_was_inherited(
        self, monkeypatch, inherited, owned
    ):
        """With nothing inherited the client chain still picks a port (a lone live
        marker, else the default), and that can be a sibling gateway. An unowned
        daemon is no exception: a gateway that adopted it may be another one."""
        monkeypatch.setattr(gw, "_OWNER_PID", os.getpid() if owned else 0)
        env = dict(inherited)
        with pytest.raises(daemon_launch.OwnerPortUnproven):
            daemon_launch.bind_control_plane_to_owner(env, "kirocrew-core")
        assert env == inherited

    def test_the_resolver_refuses_rather_than_answering_no_target(
        self, home, owner, monkeypatch, approved_launch
    ):
        """``None`` is the "no target" answer, which tells the stub to exec the
        server itself; an unknown owner port must be a capacity refusal instead."""
        monkeypatch.setenv("MC_MCP_TARGET_KIROCREW_CORE", "kirocrew mcp-core")
        with pytest.raises(daemon_launch.OwnerPortUnproven):
            gw.env_target_resolver(_control_plane_key())

    def test_a_third_party_backend_env_is_untouched(
        self, home, owner, monkeypatch, approved_launch
    ):
        """Only Crew's own servers read the gateway port; nothing else is rewritten,
        and nothing else waits."""
        monkeypatch.setenv("KIROCREW_PORT", str(FOREIGN_PORT))
        monkeypatch.setenv("MC_MCP_TARGET_ECHO_MCP", "echo-mcp --stdio")
        resolved = gw.env_target_resolver(PoolKey.from_register(_register("dashboard:chat-1-1")))
        assert resolved is not None
        assert resolved[2]["KIROCREW_PORT"] == str(FOREIGN_PORT)
        assert "KIROCREW_BOUND_PORT" not in resolved[2]
        assert not gw._owner_port_pending("echo-mcp")


# ── an acquisition before the announce ─────────────────────────────────────


def _recording_resolver(calls: list[dict[str, str]]):
    def resolve(pool_key: PoolKey):
        target = gw.env_target_resolver(pool_key)
        if target is not None:
            calls.append(dict(target[2]))
        return target

    return resolve


class TestAcquisitionBeforeTheAnnounce:
    @pytest.mark.asyncio
    async def test_it_waits_with_keepalives_and_then_proceeds(self, owner):
        queued: list[dict[str, Any]] = []

        async def on_queued(position) -> None:
            queued.append(position.frame())
            if len(queued) == 2:
                gw._set_owner_port(OWNER_PORT)

        await _within_ceiling(
            gw._await_owner_port(
                _control_plane_key(),
                admission=_admission(capacity=3),
                # Unbounded: the announce ends this wait; _within_ceiling bounds the test.
                deadline=None,
                on_queued=on_queued,
                keepalive_secs=DEFAULT_INTERVAL_SECS,
            ),
            "the wait ending on the announce",
        )
        assert len(queued) == 2
        assert all(f["type"] == "queued" and f["position"] == 0 for f in queued)
        assert queued[0]["capacity"] == 3

    @pytest.mark.asyncio
    async def test_past_its_deadline_it_is_refused(self, owner):
        with pytest.raises(daemon_launch.OwnerPortUnproven):
            await _within_ceiling(
                gw._await_owner_port(
                    _control_plane_key(),
                    admission=None,
                    # A monotonic deadline of 0 is long past: the expired path.
                    deadline=0,
                    on_queued=None,
                ),
                "the wait ending on its deadline",
            )

    @pytest.mark.asyncio
    async def test_a_third_party_server_never_waits(self, owner, monkeypatch):
        # A deadline of 0 is long past, so any wait would refuse at once.
        await gw._await_owner_port(
            _control_plane_key("echo-mcp"), admission=None, deadline=0, on_queued=None
        )
        monkeypatch.setattr(gw, "_OWNER_PID", 0)
        await gw._await_owner_port(
            _control_plane_key("echo-mcp"), admission=None, deadline=0, on_queued=None
        )

    @pytest.mark.asyncio
    async def test_an_unowned_daemon_waits_too_and_proceeds_on_the_announce(self):
        """The adopted unowned daemon: no announce yet means wait, then refuse, the
        same as an owned daemon. Its inherited port is never used."""
        with pytest.raises(daemon_launch.OwnerPortUnproven, match="unowned daemon"):
            await gw._await_owner_port(
                _control_plane_key(), admission=None, deadline=0, on_queued=None
            )

        async def announce_soon() -> None:
            await asyncio.sleep(0)
            gw._set_owner_port(OWNER_PORT)

        announcer = asyncio.create_task(announce_soon())
        await _within_ceiling(
            gw._await_owner_port(
                _control_plane_key(),
                admission=None,
                # Unbounded: the announce ends this wait; _within_ceiling bounds the test.
                deadline=None,
                on_queued=None,
            ),
            "the unowned daemon's wait ending on the announce",
        )
        await announcer

    @pytest.mark.asyncio
    async def test_the_first_wait_per_server_warns_and_later_ones_do_not(self, owner, caplog):
        with caplog.at_level(logging.DEBUG, logger=gw.logger.name):
            for name in ("kirocrew-core", "kirocrew-core", "kirocrew-dashboard"):
                with pytest.raises(daemon_launch.OwnerPortUnproven):
                    await gw._await_owner_port(
                        _control_plane_key(name),
                        admission=None,
                        deadline=0,
                        on_queued=None,
                    )
        waits = [r for r in caplog.records if "waits for owner pid" in r.getMessage()]
        assert len(waits) == 3
        assert [r.levelno for r in waits] == [logging.WARNING, logging.DEBUG, logging.WARNING]

    @pytest.mark.asyncio
    async def test_a_prewarm_never_waits(self, owner):
        resolver = MagicMock()
        with pytest.raises(daemon_launch.OwnerPortUnproven):
            await _within_ceiling(
                gw._acquire_backend(
                    _fake_pool(), _control_plane_key(), resolver, admission=None, prewarm=True
                ),
                "a prewarm refused at once",
            )
        resolver.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("owned", [True, False], ids=["owned", "unowned"])
    async def test_a_stub_that_registers_first_waits_then_gets_a_bound_backend(
        self, home, monkeypatch, approved_launch, peer_ok, owned  # noqa: F811
    ):
        """The boot window on the wire: the stub's ``ensure_backend`` is held, the
        resolver is not run, and once the owner announces, the backend it gets is
        bound to the owner's port, not the inherited foreign one. A daemon that a
        gateway adopted unowned waits the same way."""
        monkeypatch.setattr(gw, "_OWNER_PID", os.getpid() if owned else 0)
        monkeypatch.setenv("KIROCREW_PORT", str(FOREIGN_PORT))
        monkeypatch.setenv("MC_MCP_TARGET_KIROCREW_CORE", "kirocrew mcp-core")
        resolved: list[dict[str, str]] = []
        pool = _fake_pool()
        pool.get_or_create = AsyncMock(return_value=_fake_backend())
        hangup = asyncio.Event()
        writer = _FakeWriter()
        reader = _ScriptedReader(
            _register_frame(server_name="kirocrew-core"),
            {"type": "ensure_backend", "wait_budget_secs": 600},
            hangup,
        )
        handler = asyncio.create_task(
            gw._handle_connection(
                reader,
                writer,
                pool,
                _recording_resolver(resolved),
                Path("/tmp/adm.sock"),
                None,
                admission=_admission(),
            )
        )
        try:
            await _await_state(
                lambda: "kirocrew-core" in daemon_launch._OWNER_PORT_DECLINES_WARNED,
                "the acquisition waiting for the owner's port",
            )
            assert resolved == [], "nothing is resolved before the port is known"
            assert [f["type"] for f in writer.frames()] == ["registered"]
            gw._set_owner_port(OWNER_PORT)
            await _await_state(
                lambda: any(f["type"] == "ready" for f in writer.frames()),
                "ready after the announce",
            )
        finally:
            hangup.set()
            await _within_ceiling(handler, "the handler ending on the hangup")
        assert [f["type"] for f in writer.frames()] == ["registered", "ready"]
        (env,) = resolved
        assert env["KIROCREW_BOUND_PORT"] == str(OWNER_PORT)
        assert "KIROCREW_PORT" not in env
        assert _resolve_in(env) == (OWNER_PORT, "bound")

    @pytest.mark.asyncio
    async def test_past_the_budget_a_stub_is_refused_capacity_with_no_fallback(
        self, home, owner, monkeypatch, approved_launch, peer_ok  # noqa: F811
    ):
        """The finding this rework fixes: an unknown port must never reach the stub
        as a fallback-authorising rejection. The stub's exec would run the server
        inside the session's sandbox, where its gateway tools fail."""
        monkeypatch.setenv("MC_MCP_TARGET_KIROCREW_CORE", "kirocrew mcp-core")
        resolver = MagicMock(side_effect=AssertionError("resolved without a port"))
        writer = _FakeWriter()
        await _within_ceiling(
            gw._handle_connection(
                _ScriptedReader(
                    _register_frame(server_name="kirocrew-core"),
                    # 0.4 s asked; the daemon gives up first, after 0.2 s.
                    {"type": "ensure_backend", "wait_budget_secs": 0.4},
                    asyncio.Event(),  # the stub keeps its socket open
                ),
                writer,
                _fake_pool(),
                resolver,
                Path("/tmp/adm.sock"),
                None,
                admission=_admission(),
            ),
            "the capacity refusal",
        )
        frames = writer.frames()
        assert [f["type"] for f in frames] == ["registered", "rejected"]
        assert frames[1]["class"] == "capacity"
        assert frames[1]["retry_after_secs"] == gw._CAPACITY_RETRY_AFTER_SECS
        assert not any("fallback" in f for f in frames)
        resolver.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_legacy_lazy_spawn_refusal_carries_no_fallback_either(self):
        writer = _FakeWriter()
        await gw._refuse_lazy_spawn(
            daemon_launch.OwnerPortUnproven("'kirocrew-core': no port"),
            writer,
            caller=None,
            pool_key=_control_plane_key(),
        )
        (frame,) = writer.frames()
        assert frame["class"] == "capacity" and "fallback" not in frame


class TestOwnerPortRefusal:
    """``OwnerPortUnproven`` is a ``capacity`` failure on every path.

    ``test_mcp_gateway_spawn_gate.py`` holds ``_CAPACITY_FAILURES`` equal to its
    table and leaves this member to these tests.
    """

    @pytest.mark.parametrize("exclusive", [False, True], ids=["pooled", "private"])
    def test_the_classifier_never_authorises_an_exec(self, exclusive):
        verdict = gw._classify_rejection(
            gw.OwnerPortUnproven("'kirocrew-core': no port"), exclusive=exclusive
        )
        assert verdict is not None
        assert (verdict.cls, verdict.fallback) == ("capacity", False)
        assert verdict.retry_after_secs == gw._CAPACITY_RETRY_AFTER_SECS
        assert "fallback" not in verdict.frame("r")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ensure",
        [{"type": "ensure_backend"}, {"type": "ensure_backend", "wait_budget_secs": 600}],
        ids=["old-stub", "queue-aware-stub"],
    )
    async def test_both_wire_shapes_get_capacity_with_no_fallback(
        self, monkeypatch, peer_ok, ensure  # noqa: F811
    ):
        """A pre-``spawn_queue`` stub execs on ``fallback: true`` after closing its
        socket, uncharged; a queue-aware one execs on it too. Neither may see it."""
        monkeypatch.setattr(
            gw, "_acquire_backend", AsyncMock(side_effect=gw.OwnerPortUnproven("no port"))
        )
        admission = _admission()
        writer = _FakeWriter()
        await _within_ceiling(
            gw._handle_connection(
                _ScriptedReader(_register_frame(server_name="kirocrew-core"), ensure),
                writer,
                _fake_pool(),
                MagicMock(),
                Path("/tmp/adm.sock"),
                None,
                admission=admission,
            ),
            "the refusal",
        )
        frames = writer.frames()
        assert [f["type"] for f in frames] == ["registered", "rejected"]
        assert frames[1]["class"] == "capacity"
        assert frames[1]["retry_after_secs"] == gw._CAPACITY_RETRY_AFTER_SECS
        assert not any("fallback" in f for f in frames)
        assert admission.budget.snapshot()["by_kind"] == {}, "no fallback was charged"


# ── prewarm ────────────────────────────────────────────────────────────────


class TestPrewarm:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("owned", [True, False], ids=["owned", "unowned"])
    async def test_a_control_plane_key_is_skipped_until_the_announce(self, monkeypatch, owned):
        monkeypatch.setattr(gw, "_OWNER_PID", os.getpid() if owned else 0)
        payloads = [
            _register_frame(server_name="kirocrew-core", stub_uuid="warm-core"),
            _register_frame(server_name="echo-mcp", stub_uuid="warm-echo"),
        ]
        hot_keys = SimpleNamespace(top_register_payloads=lambda n: payloads[:n])
        warmed: list[str] = []

        async def acquire(pool, pool_key, resolver, **kwargs):
            assert kwargs["prewarm"] is True
            warmed.append(pool_key.server_name)
            return SimpleNamespace(pinned=False), True

        monkeypatch.setattr(gw, "_acquire_backend", acquire)
        monkeypatch.setattr(gw, "_audit_prewarm_spawn", lambda *_a: None)
        pool = _fake_pool()
        pool.snapshot = AsyncMock(return_value=[])
        prewarmer = warm_pool._Prewarmer(pool, MagicMock(), _admission(), hot_keys, 2)

        await prewarmer.run_pass()
        assert warmed == ["echo-mcp"]

        gw._set_owner_port(OWNER_PORT)
        await prewarmer.run_pass()
        assert warmed == ["echo-mcp", "kirocrew-core", "echo-mcp"]


# ── a change of owner port ─────────────────────────────────────────────────


def _pooled(pool_key: PoolKey, owner_port: int = 0) -> Backend:
    """A live fake backend for *pool_key*, spawned for *owner_port*."""
    backend = _fake_backend()
    backend.pool_key = pool_key
    backend.owner_port = owner_port
    backend.shutdown = AsyncMock()  # type: ignore[method-assign]
    return backend


def _demo_resolver(pool_key: PoolKey) -> tuple[str, list[str], dict[str, str], str]:
    """A target whose environment inherited another gateway's port."""
    return "demo-mcp-server", [], {"KIROCREW_PORT": str(FOREIGN_PORT)}, pool_key.work_dir


class _Spawner:
    """Stands in for ``spawn_backend``: records each backend and the env it got.

    ``during_first`` runs inside the first spawn, before it returns.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Backend, dict[str, str]]] = []
        self.during_first: Any = None

    async def __call__(self, **kwargs: Any) -> Backend:
        backend = _pooled(kwargs["pool_key"])
        self.calls.append((backend, dict(kwargs["env"])))
        if self.during_first is not None and len(self.calls) == 1:
            await self.during_first()
        return backend


@pytest.fixture
def spawner(monkeypatch: pytest.MonkeyPatch) -> _Spawner:
    """``_acquire_backend``'s real spawn path, with no process and no config reads."""
    fake = _Spawner()
    monkeypatch.setattr(gw, "spawn_backend", fake)
    monkeypatch.setattr(gw, "_declared_env_to_forward", lambda _k: {})
    monkeypatch.setattr(gw, "resolve_secret_uris", lambda env, _home: (env, []))
    monkeypatch.setattr(gw, "_spawns_own_control_plane", lambda *_a, **_k: False)
    return fake


class TestOwnerPortChange:
    """The owner's listener moves (an in-app restart under ``--port auto`` keeps the
    daemon), so it announces a new port. Nothing bound to the old one is handed out
    again."""

    @pytest.mark.asyncio
    async def test_a_new_port_retires_old_port_control_plane_backends_only(
        self, owner, monkeypatch, audit, spawner
    ):
        _sender(monkeypatch, owner)
        pool = BackendPool(4)
        core, echo = _control_plane_key(), _control_plane_key("echo-mcp")
        gw._set_owner_port(OWNER_PORT)
        first, _ = await gw._acquire_backend(pool, core, _demo_resolver)
        third_party, _ = await gw._acquire_backend(pool, echo, _demo_resolver)
        pool.unreserve(core)
        pool.unreserve(echo)
        assert (first.owner_port, third_party.owner_port) == (OWNER_PORT, 0)

        writer = _FakeWriter()
        assert await gw._serve_control_frame(_owner_port_frame(MOVED_PORT), writer, pool)
        assert writer.frames() == [{"type": "owner-port-set", "port": MOVED_PORT}]
        assert first not in pool.live_pooled_backends()
        assert first in pool.all_backends(), "draining, so its calls in flight finish"
        first.shutdown.assert_not_awaited()

        second, core_spawned = await gw._acquire_backend(pool, core, _demo_resolver)
        same, echo_spawned = await gw._acquire_backend(pool, echo, _demo_resolver)
        assert core_spawned and second is not first and second.owner_port == MOVED_PORT
        assert same is third_party and not echo_spawned
        envs = [env for _backend, env in spawner.calls]
        assert [env.get("KIROCREW_BOUND_PORT") for env in envs] == [
            str(OWNER_PORT),
            None,
            str(MOVED_PORT),
        ]
        assert envs[1]["KIROCREW_PORT"] == str(FOREIGN_PORT), "a third-party env is untouched"
        assert _resolve_in(envs[2]) == (MOVED_PORT, "bound")

    @pytest.mark.asyncio
    async def test_a_spawn_bound_before_the_change_is_not_handed_out(
        self, owner, monkeypatch, audit, spawner
    ):
        """The spawn was bound to the old port, and the announce lands while it
        starts. The pool refuses it, and the respawn is bound to the new port."""
        _sender(monkeypatch, owner)
        pool = BackendPool(4)
        gw._set_owner_port(OWNER_PORT)

        async def announce_during_spawn() -> None:
            assert (await _announce(MOVED_PORT, pool))["type"] == "owner-port-set"

        spawner.during_first = announce_during_spawn
        got, _ = await gw._acquire_backend(pool, _control_plane_key(), _demo_resolver)
        (stale, stale_env), (fresh, fresh_env) = spawner.calls
        assert stale_env["KIROCREW_BOUND_PORT"] == str(OWNER_PORT)
        assert fresh_env["KIROCREW_BOUND_PORT"] == str(MOVED_PORT)
        assert got is fresh and fresh.owner_port == MOVED_PORT
        stale.shutdown.assert_awaited()
        assert stale not in pool.all_backends()

    @pytest.mark.asyncio
    async def test_a_third_party_spawn_in_flight_across_the_change_is_kept(
        self, owner, monkeypatch, audit, spawner
    ):
        """Unlike a credential cutover, the retirement is no drain-epoch bump."""
        _sender(monkeypatch, owner)
        pool = BackendPool(4)
        gw._set_owner_port(OWNER_PORT)

        async def announce_during_spawn() -> None:
            await _announce(MOVED_PORT, pool)

        spawner.during_first = announce_during_spawn
        got, _ = await gw._acquire_backend(pool, _control_plane_key("echo-mcp"), _demo_resolver)
        ((only, _env),) = spawner.calls
        assert got is only and only in pool.live_pooled_backends()
        only.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reuse_refuses_a_pooled_backend_bound_to_another_port(self):
        """The acquire path's own check, for an entry that is still pooled."""
        pool = BackendPool(4)
        core, echo = _control_plane_key(), _control_plane_key("echo-mcp")
        stale, third_party = _pooled(core, OWNER_PORT), _pooled(echo)
        await pool.add(core, stale)
        await pool.add(echo, third_party)
        gw._set_owner_port(MOVED_PORT)
        fresh = _pooled(core, MOVED_PORT)
        spawn = AsyncMock(return_value=fresh)

        assert await pool.get_or_create(core, spawn, reusable=gw.bound_to_owner_port) is fresh
        kept = await pool.get_or_create(echo, AsyncMock(), reusable=gw.bound_to_owner_port)
        assert kept is third_party
        assert spawn.await_count == 1
        assert stale in pool.all_backends() and stale not in pool.live_pooled_backends()
        stale.shutdown.assert_not_awaited()


# ── the manager ────────────────────────────────────────────────────────────


def _manager(tmp_path: Path) -> mgr.GatewayManager:
    return mgr.GatewayManager(mgr.GatewaySpec(socket_path=tmp_path / "gw.sock"))


def _acks(sent: list[dict[str, Any]]):
    async def roundtrip(frame: dict[str, Any]) -> dict[str, Any]:
        sent.append(frame)
        return {"type": "owner-port-set", "port": frame["port"]}

    return roundtrip


class TestManagerAnnounce:
    @pytest.mark.asyncio
    async def test_the_announce_sends_the_port_and_reports_the_ack(self, tmp_path, monkeypatch):
        m = _manager(tmp_path)
        sent: list[dict[str, Any]] = []
        monkeypatch.setattr(m, "_control_roundtrip", _acks(sent))
        assert await m.announce_owner_port(OWNER_PORT) is True
        assert sent == [{"type": "owner-port", "port": OWNER_PORT}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("port", [0, 65536, True, "47211"])
    async def test_a_value_that_is_not_a_port_is_neither_kept_nor_sent(
        self, tmp_path, monkeypatch, port
    ):
        m = _manager(tmp_path)
        roundtrip = AsyncMock()
        monkeypatch.setattr(m, "_control_roundtrip", roundtrip)
        assert await m.announce_owner_port(port) is False
        assert m._owner_port is None
        roundtrip.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_refused_announce_warns_once_and_never_raises(
        self, tmp_path, monkeypatch, caplog
    ):
        m = _manager(tmp_path)
        replies = [None, {"type": "owner-port-rejected", "reason": "no"}]
        monkeypatch.setattr(m, "_control_roundtrip", AsyncMock(side_effect=replies))
        with caplog.at_level(logging.DEBUG, logger=mgr.logger.name):
            assert await m.announce_owner_port(OWNER_PORT) is False
            assert await m._send_owner_port() is False
        misses = [r for r in caplog.records if "did not take owner port" in r.getMessage()]
        assert [r.levelno for r in misses] == [logging.WARNING, logging.DEBUG]
        assert m._owner_port == OWNER_PORT, "kept for the watchdog's retry"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("adopt", [True, False], ids=["adoption", "spawn"])
    async def test_a_start_announces_the_stored_port(self, tmp_path, monkeypatch, adopt):
        """Re-announced after the election either way: an adopted daemon holds its
        previous owner's port, and a spawn's argv cannot replace an announce the
        adopted daemon never got."""
        m = _manager(tmp_path)
        m.set_owner_port(OWNER_PORT)
        sent: list[dict[str, Any]] = []
        monkeypatch.setattr(m, "_control_roundtrip", _acks(sent))
        fit = {"type": "pong", "targets": [], "fingerprint": cf.code_fingerprint(), "owner_pid": 0}
        monkeypatch.setattr(m, "_ping_payload", AsyncMock(return_value=fit if adopt else None))

        class _Proc:
            pid = 12345
            returncode = None

        async def spawn() -> dict[str, Any]:
            m._process = _Proc()  # type: ignore[assignment]
            return {**fit, "owner_pid": os.getpid()}

        monkeypatch.setattr(m, "_spawn_and_confirm", spawn)
        monkeypatch.setattr(m, "_run_watchdog", AsyncMock())
        try:
            assert await m.start() is True
            assert m._adopted is adopt
        finally:
            if m._watchdog is not None:
                m._watchdog.cancel()
                await asyncio.gather(m._watchdog, return_exceptions=True)
        assert sent == [{"type": "owner-port", "port": OWNER_PORT}]

    @pytest.mark.asyncio
    async def test_a_start_before_the_bind_sends_nothing(self, tmp_path, monkeypatch):
        m = _manager(tmp_path)
        monkeypatch.setattr(m, "_start_locked", AsyncMock(return_value=True))
        roundtrip = AsyncMock()
        monkeypatch.setattr(m, "_control_roundtrip", roundtrip)
        assert await m.start() is True
        roundtrip.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "pong,resent",
        [
            ({"type": "pong", "owner_port": 0}, True),
            ({"type": "pong", "owner_port": FOREIGN_PORT}, True),
            ({"type": "pong", "owner_port": OWNER_PORT}, False),
            ({"type": "pong"}, False),  # a daemon that predates the frame
        ],
    )
    async def test_the_reconcile_resends_only_to_a_daemon_that_lacks_the_port(
        self, tmp_path, monkeypatch, pong, resent
    ):
        m = _manager(tmp_path)
        m.set_owner_port(OWNER_PORT)
        sent: list[dict[str, Any]] = []
        monkeypatch.setattr(m, "_control_roundtrip", _acks(sent))
        await m._reconcile_owner_port(pong)
        assert bool(sent) is resent

    @pytest.mark.asyncio
    async def test_the_owned_watchdog_reconciles_on_its_liveness_pong(self, tmp_path, monkeypatch):
        m = _manager(tmp_path)
        m.set_owner_port(OWNER_PORT)
        monkeypatch.setattr(mgr, "_LIVENESS_PING_INTERVAL_SECS", 0)
        monkeypatch.setattr(
            m, "_ping_with_escalation", AsyncMock(return_value={"type": "pong", "owner_port": 0})
        )
        sent: list[dict[str, Any]] = []
        acks = _acks(sent)

        async def roundtrip(frame):
            m._stopping = True  # one cycle is enough
            return await acks(frame)

        monkeypatch.setattr(m, "_control_roundtrip", roundtrip)
        assert await _within_ceiling(m._liveness_probe_loop(), "one probe cycle") == "stopping"
        assert sent == [{"type": "owner-port", "port": OWNER_PORT}]

    @pytest.mark.asyncio
    async def test_the_adopted_watchdog_reconciles_on_its_liveness_pong(
        self, tmp_path, monkeypatch
    ):
        """Covers re-adoption by the watchdog too: it re-enters this branch."""
        m = _manager(tmp_path)
        m.set_owner_port(OWNER_PORT)
        m._adopted = True
        monkeypatch.setattr(
            m, "_ping_with_escalation", AsyncMock(return_value={"type": "pong", "owner_port": 0})
        )
        sent: list[dict[str, Any]] = []
        monkeypatch.setattr(m, "_control_roundtrip", _acks(sent))

        async def reconcile_adopted(_pong) -> bool:
            m._stopping = True
            return True

        monkeypatch.setattr(m, "_reconcile_adopted", reconcile_adopted)
        await _within_ceiling(m._run_watchdog(), "one adopted-branch cycle")
        assert sent == [{"type": "owner-port", "port": OWNER_PORT}]


async def _spawn_argv(monkeypatch, tmp_path, m: mgr.GatewayManager) -> list[str]:
    """Run ``_spawn_once`` against a fake launcher; return the argv it launched."""
    launched: list[list[str]] = []

    async def launch(*argv, **_kwargs):
        launched.append(list(argv))
        return SimpleNamespace(returncode=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(m, "_gatewayd_log_path", lambda: tmp_path / "logs" / "mcp-gatewayd.stdout")
    monkeypatch.setattr(m, "_host_available_mb", lambda: 2048.0)
    monkeypatch.setattr(m, "_credential_watch_paths", lambda: [])
    await m._spawn_once()
    (argv,) = launched
    return argv


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == flag]


class TestManagerArgv:
    @pytest.mark.asyncio
    async def test_a_spawn_after_the_bind_passes_the_port(self, tmp_path, monkeypatch):
        m = _manager(tmp_path)
        m.set_owner_port(OWNER_PORT)
        argv = await _spawn_argv(monkeypatch, tmp_path, m)
        assert _flag_values(argv, "--owner-port") == [str(OWNER_PORT)]

    @pytest.mark.asyncio
    async def test_the_port_never_comes_from_the_environment_read_before_the_bind(
        self, tmp_path, monkeypatch
    ):
        """The gateway's own inherited variables can be another gateway's."""
        monkeypatch.setenv("KIROCREW_PORT", str(FOREIGN_PORT))
        monkeypatch.setenv("KIROCREW_BOUND_PORT", str(FOREIGN_PORT))
        m = _manager(tmp_path)
        assert m._owner_port is None
        argv = await _spawn_argv(monkeypatch, tmp_path, m)
        assert "--owner-port" not in argv

    def test_the_daemon_cli_takes_the_flag(self):
        args = gw._build_argparser().parse_args(["--owner-pid", "7", "--owner-port", "47211"])
        assert args.owner_port == OWNER_PORT
        assert gw._build_argparser().parse_args([]).owner_port == 0


# ── the reported topology, end to end ──────────────────────────────────────


class TestTheReportedTopology:
    @pytest.mark.asyncio
    async def test_an_inherited_foreign_port_loses_to_the_announced_one(
        self, home, owner, monkeypatch, approved_launch, audit
    ):
        """Gateway A was started from gateway B's agent shell, so A's daemon
        inherited B's ``KIROCREW_PORT``. A announces its own port; every pooled
        control-plane backend A's daemon resolves is bound to A."""
        monkeypatch.setenv("KIROCREW_PORT", str(FOREIGN_PORT))
        monkeypatch.setenv("MC_MCP_TARGET_KIROCREW_CORE", "kirocrew mcp-core")
        monkeypatch.setenv("MC_MCP_TARGET_KIROCREW_DASHBOARD", "kirocrew mcp-dashboard")
        _sender(monkeypatch, owner)
        assert (await _announce(OWNER_PORT))["type"] == "owner-port-set"
        for name in ("kirocrew-core", "kirocrew-dashboard"):
            resolved = gw.env_target_resolver(_control_plane_key(name))
            assert resolved is not None, name
            assert "KIROCREW_PORT" not in resolved[2], name
            assert _resolve_in(resolved[2]) == (OWNER_PORT, "bound"), name

    @pytest.mark.asyncio
    async def test_a_daemon_respawned_with_the_port_needs_no_announce(
        self, short_sock_dir, owner, monkeypatch
    ):
        """``--owner-port`` reaches the facade before anything can connect."""
        monkeypatch.setattr(gw, "_OWN_START_TIME", gw._OWN_START_TIME)
        stop = asyncio.Event()
        stop.set()  # serve nothing; only the start-up writes are under test
        await _within_ceiling(
            gw.run_gatewayd(
                short_sock_dir / "port.sock",
                max_backends=2,
                idle_timeout_secs=FALLBACK_TIMEOUT_SECS,
                stop_event=stop,
                target_resolver=MagicMock(return_value=None),
                owner_pid=owner,
                owner_port=OWNER_PORT,
            ),
            "a daemon that starts and stops",
        )
        assert gw._OWNER_PORT == OWNER_PORT


# ── per-call identity: instance A never authenticates against instance B ─────


def _policy_response(exclude: list[str]) -> MagicMock:
    body = MagicMock()
    body.read.return_value = json.dumps({"exclude": exclude}).encode("utf-8")
    body.__enter__ = MagicMock(return_value=body)
    body.__exit__ = MagicMock(return_value=False)
    return body


@pytest.fixture
def policy_state(monkeypatch: pytest.MonkeyPatch):
    """Fresh resolver state, a recording audit sink, and a declared session key."""
    for name, value in (
        ("_last_failure_time", 0.0),
        ("_last_startup_race_time", 0.0),
        ("_last_startup_race_key", ""),
        ("_failure_count", 0),
        ("_last_failure_class", ""),
    ):
        monkeypatch.setattr(mcp_shared, name, value)
    monkeypatch.setattr(mcp_shared, "_excluded_tools_by_session", {})
    monkeypatch.setattr(mcp_shared, "_included_tools_by_session", {})
    audit = MagicMock()
    monkeypatch.setattr(mcp_shared, "sel", lambda: audit)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-1-1")
    return audit


class TestInstanceIsolation:
    def test_the_bound_backend_sends_instance_As_credential_to_instance_A(
        self, home, owner, policy_state, monkeypatch
    ):
        monkeypatch.setattr(gw, "_OWNER_PORT", OWNER_PORT)
        _publish_secret(home, OWNER_PORT, "synthetic-credential-A")
        _publish_secret(home, FOREIGN_PORT, "synthetic-credential-B")
        env = {"KIROCREW_HOME": str(home), "KIROCREW_PORT": str(FOREIGN_PORT)}
        daemon_launch.bind_control_plane_to_owner(env, "kirocrew-core")
        urlopen = MagicMock(return_value=_policy_response(["blocked"]))
        monkeypatch.setattr(mcp_shared, "loopback_urlopen", urlopen)

        with patch.dict(os.environ, {**env, "KIROCREW_SESSION_KEY": "dashboard:chat-1-1"}):
            policy = mcp_shared._resolve_tool_policy()

        assert policy.unresolved == "" and policy.excluded == {"blocked"}
        request = urlopen.call_args.args[0]
        assert request.full_url == f"http://127.0.0.1:{OWNER_PORT}/api/session-tool-policy"
        assert request.get_header("X-internal-secret") == "synthetic-credential-A"
        assert all(str(FOREIGN_PORT) not in c.args[0].full_url for c in urlopen.call_args_list)

    def test_a_credential_rotation_and_restart_on_the_same_port_is_read_fresh(
        self, home, owner, policy_state, monkeypatch
    ):
        """The secret is re-read per call, so a restarted gateway's new credential
        is the one the next read sends. A refused read is never cached as an
        authorization result."""
        monkeypatch.setenv("KIROCREW_BOUND_PORT", str(OWNER_PORT))
        _publish_secret(home, OWNER_PORT, "synthetic-generation-1")
        forbidden = urllib.error.HTTPError(
            url="http://127.0.0.1/",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"Forbidden","code":"internal_auth_mismatch"}'),
        )
        urlopen = MagicMock(side_effect=[forbidden, _policy_response(["blocked"])])
        monkeypatch.setattr(mcp_shared, "loopback_urlopen", urlopen)

        first = mcp_shared._resolve_tool_policy()
        # The gateway restarts on the same port and publishes a new credential.
        _publish_secret(home, OWNER_PORT, "synthetic-generation-2")
        second = mcp_shared._resolve_tool_policy()

        assert first.unresolved == "policy_forbidden"
        assert second.unresolved == "" and second.excluded == {"blocked"}
        sent = [c.args[0].get_header("X-internal-secret") for c in urlopen.call_args_list]
        assert sent == ["synthetic-generation-1", "synthetic-generation-2"]

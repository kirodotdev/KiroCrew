"""No tunnel is started once a gateway restart has claimed the process.

The restart's pre-exec seam stops every forwarder, then the HTTP server keeps
serving until the exec. The exec keeps the pid, so a forwarder started in that
window is parented to the new image, which never reclaims it. While
``state._gateway_restart_in_progress`` is set, the connect route answers 409
``restarting`` before the manager starts anything, and the startup revive stops
at the next instance. ``only_if_connected`` starts nothing and is answered as
usual.

Driven through the real handler and the real ``SshTunnelManager``; the last
class uses a real child process as the forwarder, so the proof is that no
process was started, not that a stub was not called.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import handlers_instances as handlers
from kiro_crew.dashboard import server
from kiro_crew.instances.registry import InstancesRegistry
from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, TunnelState, TunnelStatus
from kiro_crew.pre_exec_stops import register_pre_exec_stop, run_pre_exec_stops

#: Bound on reaping a test's own child after a signal. It only names a hang: a
#: child that ignores SIGTERM is killed at it, and one that outlives the kill
#: fails the test at that line instead of reaching the worker-killing timeout.
_REAP_TIMEOUT_SECS = 30.0


class _Tunnel:
    """Fake forwarder: starts CONNECTED and records that it was built."""

    built: list[str] = []

    def __init__(self, iid, ssh_host, lp, rp, *, transport="ssh", **_kwargs):
        _Tunnel.built.append(iid)
        self.pid = None
        self.transport = transport
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)

    async def start(self):
        self.status.state = TunnelState.CONNECTED
        return True

    async def stop(self):
        self.status.state = TunnelState.STOPPED


async def _mint(host, **_kwargs):
    return "TOK"


class _Req:
    def __init__(self, state, *, match=None, query=None, user="owner"):
        self.app = {"state": state}
        self.headers: dict = {}
        self.match_info = match or {}
        self.query = query or {}
        self._attrs = {"app": ""}
        if user is not None:
            self._attrs["user"] = user

    def get(self, key, default=None):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]


def _state(registry, manager, *, restarting: bool) -> SimpleNamespace:
    return SimpleNamespace(
        owner_id="owner",
        instances_registry=registry,
        instances_manager=manager,
        _gateway_restart_in_progress=restarting,
    )


def _body(resp):
    return json.loads(resp.body.decode())


async def _connect(state, **query):
    return await handlers.api_instances_connect(_Req(state, match={"id": "cd-1"}, query=query))


@pytest.fixture
def env(tmp_path: Path, _floor_monkeypatch):
    import kiro_crew.instances.port_allocator as pa
    import kiro_crew.instances.ssh_tunnel_manager as stm
    from kiro_crew.config import loader

    # Hermetic ports: both namespaces that probe must answer "free".
    _floor_monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)
    _floor_monkeypatch.setattr(pa, "_is_port_free", lambda port, host="127.0.0.1": True)
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"instances": {"enabled": True}}))
    loader._invalidate_config_cache()
    _Tunnel.built = []
    reg = InstancesRegistry(path=tmp_path / "instances.json")
    reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
    mgr = SshTunnelManager(reg, base_port=53800, mint_token=_mint, tunnel_factory=_Tunnel)
    return reg, mgr


class TestTheConnectRoute:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", [{}, {"rebuild": "1"}])
    async def test_a_restart_in_progress_answers_409_and_starts_nothing(self, env, query):
        reg, mgr = env

        resp = await _connect(_state(reg, mgr, restarting=True), **query)

        assert resp.status == 409
        assert _body(resp)["code"] == "restarting"
        assert _Tunnel.built == []
        assert mgr.status("cd-1") is None

    @pytest.mark.asyncio
    async def test_without_a_restart_the_connect_works_as_before(self, env):
        reg, mgr = env

        resp = await _connect(_state(reg, mgr, restarting=False))

        assert resp.status == 200
        assert _body(resp)["state"] == "connected"
        assert _Tunnel.built == ["cd-1"]
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_a_connect_after_the_restart_is_refused_again_once_it_ends(self, env):
        reg, mgr = env
        state = _state(reg, mgr, restarting=True)
        assert (await _connect(state)).status == 409

        state._gateway_restart_in_progress = False

        assert (await _connect(state)).status == 200
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_only_if_connected_is_answered_during_a_restart(self, env):
        reg, mgr = env
        state = _state(reg, mgr, restarting=True)

        declined = await _connect(state, only_if_connected="1")

        # Declined, not refused: the 200 the auto-warm reads as `warm-declined`.
        assert declined.status == 200
        assert _body(declined)["code"] == "instance_not_connected"
        assert _Tunnel.built == []

    @pytest.mark.asyncio
    async def test_only_if_connected_still_reports_a_live_tunnel_during_a_restart(self, env):
        reg, mgr = env
        await mgr.connect("cd-1")
        state = _state(reg, mgr, restarting=True)

        warm = await _connect(state, only_if_connected="1")

        assert warm.status == 200
        assert _body(warm)["state"] == "connected"
        assert _Tunnel.built == ["cd-1"]
        await mgr.shutdown()


class TestTheStartupRevive:
    @pytest.mark.asyncio
    async def test_a_restart_in_progress_revives_nothing(self, env):
        reg, mgr = env
        reg.update("cd-1", was_connected=True)

        await server._revive_intended_instances(reg, mgr, _state(reg, mgr, restarting=True))

        assert _Tunnel.built == []
        assert reg.get("cd-1").was_connected is True, "the intent survives for the next image"

    @pytest.mark.asyncio
    async def test_without_a_restart_it_revives_the_instance(self, env):
        reg, mgr = env
        reg.update("cd-1", was_connected=True)

        await server._revive_intended_instances(reg, mgr, _state(reg, mgr, restarting=False))

        assert _Tunnel.built == ["cd-1"]
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_a_restart_claimed_mid_revive_stops_at_the_next_instance(self, env):
        reg, mgr = env
        reg.add(name="B", ssh_host="b-alias", instance_id="b", remote_port=7778)
        reg.update("cd-1", was_connected=True)
        reg.update("b", was_connected=True)
        state = _state(reg, mgr, restarting=False)
        real_connect = mgr.connect

        async def _connect_then_claim(instance_id, *args, **kwargs):
            status = await real_connect(instance_id, *args, **kwargs)
            state._gateway_restart_in_progress = True
            return status

        mgr.connect = _connect_then_claim

        await server._revive_intended_instances(reg, mgr, state)

        assert _Tunnel.built == ["cd-1"]
        await mgr.shutdown()


def _child_tunnel(cwd):
    """A forwarder whose ``start`` spawns a real long-lived child process."""

    class _ChildTunnel(_Tunnel):
        proc: asyncio.subprocess.Process | None = None

        async def start(self):
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(300)", cwd=str(cwd)
            )
            self.pid = self.proc.pid
            return await super().start()

        async def stop(self):
            if self.proc is not None and self.proc.returncode is None:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=_REAP_TIMEOUT_SECS)
                except asyncio.TimeoutError:
                    self.proc.kill()
                    await asyncio.wait_for(self.proc.wait(), timeout=_REAP_TIMEOUT_SECS)
            await super().stop()

    return _ChildTunnel


class TestNoForwarderStartsBetweenTheSeamAndTheExec:
    @pytest.fixture(autouse=True)
    def _free_ports(self, _floor_monkeypatch):
        import kiro_crew.instances.port_allocator as pa
        import kiro_crew.instances.ssh_tunnel_manager as stm

        _floor_monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)
        _floor_monkeypatch.setattr(pa, "_is_port_free", lambda port, host="127.0.0.1": True)

    @pytest.mark.asyncio
    async def test_a_connect_after_the_seam_starts_no_process(self, env, tmp_path):
        reg, _ = env
        mgr = SshTunnelManager(
            reg, base_port=53820, mint_token=_mint, tunnel_factory=_child_tunnel(tmp_path)
        )
        state = _state(reg, mgr, restarting=True)
        await mgr.connect("cd-1")
        first = mgr._tunnels["cd-1"].proc
        assert first is not None and first.returncode is None
        register_pre_exec_stop("Instance tunnels", mgr.shutdown)
        try:
            await run_pre_exec_stops()
            assert first.returncode is not None

            resp = await _connect(state)

            assert resp.status == 409
            assert mgr._tunnels == {}, "a forwarder was started after the seam"
        finally:
            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_the_same_connect_without_the_claim_starts_one(self, env, tmp_path):
        """The control: the gate, not the seam, is what keeps the process away."""
        reg, _ = env
        mgr = SshTunnelManager(
            reg, base_port=53830, mint_token=_mint, tunnel_factory=_child_tunnel(tmp_path)
        )
        state = _state(reg, mgr, restarting=False)
        await mgr.connect("cd-1")
        register_pre_exec_stop("Instance tunnels", mgr.shutdown)
        await run_pre_exec_stops()

        resp = await _connect(state)

        try:
            assert resp.status == 200
            proc = mgr._tunnels["cd-1"].proc
            assert proc is not None and proc.returncode is None
        finally:
            await mgr.shutdown()

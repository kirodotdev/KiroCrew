"""``SshTunnelManager.connect`` state across failures and restarts.

The startup hooks that create the manager and revive intended instances, the
local port a (re)connect picks, and the last error a failed connect retains.
"""

from __future__ import annotations

import asyncio

import pytest
from instances_helpers import _enable, _FakeTunnel, _patch_port_probe

from kiro_crew.testing.wait import await_until


class TestInstancesStartupHooks:
    """The startup hooks must register before aiohttp freezes its signal lists.

    The instances startup/cleanup hooks must be registered on the aiohttp app
    BEFORE ``runner.setup()`` freezes its signal lists. If registered after,
    ``on_startup.append`` raises ``RuntimeError`` and the startup signal (which
    fires during setup) would never run the hook anyway.
    """

    def _state(self):
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.state import DashboardState

        return DashboardState(
            sessions=MagicMock(), crons=MagicMock(), lessons=MagicMock(), start_time=0.0
        )

    def test_register_then_freeze_then_startup_creates_manager(self, tmp_path, monkeypatch):
        from aiohttp import web

        from kiro_crew.dashboard.server import _register_instances_hooks

        _enable(tmp_path, monkeypatch, enabled=True)
        app = web.Application()
        state = self._state()

        # Register before freeze (mirrors start_dashboard ordering), then freeze
        # the app exactly as ``runner.setup()`` does. Neither step must raise.
        _register_instances_hooks(app, state, port=7777)
        app.freeze()

        # on_startup fires during setup; empty registry => the hook creates the
        # manager and returns early (no real ssh / no last-active instance).
        asyncio.run(app.on_startup.send(app))
        assert state.instances_manager is not None
        assert state.instances_registry is not None

        # on_cleanup must shut the manager down through the same frozen-safe path.
        called = {}

        async def _fake_shutdown():
            called["shutdown"] = True

        state.instances_manager.shutdown = _fake_shutdown
        asyncio.run(app.on_cleanup.send(app))
        assert called.get("shutdown") is True

    def test_disabled_skips_manager_creation(self, tmp_path, monkeypatch):
        from aiohttp import web

        from kiro_crew.dashboard.server import _register_instances_hooks

        _enable(tmp_path, monkeypatch, enabled=False)
        app = web.Application()
        state = self._state()

        _register_instances_hooks(app, state, port=7777)
        app.freeze()
        asyncio.run(app.on_startup.send(app))

        # Flag off => no registry/manager created, and cleanup is a safe no-op.
        assert state.instances_manager is None
        asyncio.run(app.on_cleanup.send(app))


class TestPortMirror:
    """CSE SEC-016: the SSH tunnel's local port mirrors the remote (configured)
    port so the embedded dashboard's Origin (http://127.0.0.1:<port>) matches the
    remote gateway's trusted port. Each connected instance must use a distinct
    remote port; a local bind conflict hard-fails (no dynamic fallback)."""

    @staticmethod
    def _mgr(reg, factory, monkeypatch, *, port_free=True):
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        # Allocation must still succeed when ``port_free`` is False: that case
        # models the TOCTOU loss where the port is taken between allocating it
        # and the manager's re-probe.
        _patch_port_probe(monkeypatch, manager_free=port_free)

        async def ok_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            return "TOK"

        return SshTunnelManager(reg, mint_token=ok_mint, tunnel_factory=factory)

    @pytest.mark.asyncio
    async def test_local_port_allocated_not_mirrored(self, tmp_path, monkeypatch):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        captured: dict = {}

        def factory(iid, ssh_host, lp, rp, **k):
            captured["lp"], captured["rp"] = lp, rp
            return _FakeTunnel(iid, ssh_host, lp, rp, **k)

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=7900)
        mgr = self._mgr(reg, factory, monkeypatch)

        status = await mgr.connect("cd-1")
        assert status.state == TunnelState.CONNECTED
        # The forward still points AT the remote's port...
        assert captured["rp"] == 7900
        # ...but the local end is allocated from the tunnel base, NOT mirrored.
        assert captured["lp"] != 7900
        assert captured["lp"] >= mgr._allocator.base_port
        assert reg.get("cd-1").local_port == captured["lp"]

    @pytest.mark.asyncio
    async def test_two_instances_share_one_remote_port(self, tmp_path, monkeypatch):
        """Two stock installs both reporting the SAME remote port connect.

        This is the case mirroring made impossible — and the shipped defaults put
        every stock pair in it.
        """
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        seen: dict[str, int] = {}

        def factory(iid, ssh_host, lp, rp, **k):
            seen[iid] = lp
            return _FakeTunnel(iid, ssh_host, lp, rp, **k)

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        # Both remotes are stock: same remote port, which is also the port a
        # stock hub would itself be holding.
        reg.add(name="A", ssh_host="host-a", instance_id="cd-a", remote_port=5476)
        reg.add(name="B", ssh_host="host-b", instance_id="cd-b", remote_port=5476)
        mgr = self._mgr(reg, factory, monkeypatch)

        assert (await mgr.connect("cd-a")).state == TunnelState.CONNECTED
        assert (await mgr.connect("cd-b")).state == TunnelState.CONNECTED
        # Distinct local ports, neither of them the shared remote port.
        assert seen["cd-a"] != seen["cd-b"]
        assert 5476 not in seen.values()
        assert reg.get("cd-a").local_port != reg.get("cd-b").local_port

    @pytest.mark.asyncio
    async def test_reconnect_prefers_its_own_recorded_port(self, tmp_path, monkeypatch):
        """A recorded port IS preferred across a restart.

        ``shutdown`` documents that it "Leaves registry hints intact", so a
        recorded ``local_port`` survives a gateway RESTART. The loopback port is
        the browser ORIGIN of the pane's iframe (``http://<host>:<local_port>``),
        and origin-keyed client state — ``localStorage`` UI preferences — is lost
        the moment that origin moves; re-minting the token and reloading the pane
        does nothing for state the browser keys by origin, and first-free
        allocation moves the origin whenever another instance holds the lower
        port. connect() therefore PREFERS the recorded port: here the base port
        is free and lower, yet the recorded one is returned.
        """
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        captured: dict = {}

        def factory(iid, ssh_host, lp, rp, **k):
            captured["lp"] = lp
            return _FakeTunnel(iid, ssh_host, lp, rp, **k)

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=7900)
        mgr = self._mgr(reg, factory, monkeypatch)
        base = mgr._allocator.base_port
        reg.update("cd-1", local_port=base + 2)  # survivor of a restart

        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        # The recorded port is preferred even though the lower base port is free.
        assert captured["lp"] == base + 2
        assert reg.get("cd-1").local_port == base + 2

    @pytest.mark.asyncio
    async def test_reconnect_does_not_reuse_a_recorded_port_under_a_live_hop_lease(
        self, tmp_path, monkeypatch
    ):
        """A leased recorded port yields to a fresh port, not the preference.

        A live hop lease means a chained credential this gateway minted still
        routes a bearer token to that port number. The origin-stability
        preference must never override that: binding this crew's forward on a
        leased port would deliver another crew's token here — the confused-deputy
        the lease exists to close. So a recorded port carrying a live lease stays
        reserved and unpreferred, and the crew takes a different port.
        """
        import time

        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        captured: dict = {}

        def factory(iid, ssh_host, lp, rp, **k):
            captured["lp"] = lp
            return _FakeTunnel(iid, ssh_host, lp, rp, **k)

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=7900)
        mgr = self._mgr(reg, factory, monkeypatch)
        base = mgr._allocator.base_port
        recorded = base + 2
        reg.update("cd-1", local_port=recorded)  # survivor of a restart
        # A chained credential is still valid against the recorded port.
        reg.lend_hop(recorded, time.time() + 3600)
        assert recorded in reg.live_hop_leases()

        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        # The leased port is NOT reused; a different, safe port is chosen.
        assert captured["lp"] != recorded
        assert captured["lp"] >= base
        assert reg.get("cd-1").local_port != recorded
        # The lease still stands — reconnecting did not free the guarded port.
        assert recorded in reg.live_hop_leases()

    @pytest.mark.asyncio
    async def test_reconnect_does_not_reuse_a_recorded_port_another_row_records(
        self, tmp_path, monkeypatch
    ):
        """A recorded port another row still records yields to a fresh port.

        Two registry rows can record the SAME port number (a duplicate hint left
        by an earlier allocation). ``_reserved_ports`` withholds that number on
        behalf of every row that records it, so dropping it from the exclude set
        for this crew alone would unreserve it for the other row too — binding
        this crew's forward on a port a hub still forwards another crew's bearer
        token to, the confused-deputy the reservation exists to close. So a
        recorded port another row records stays reserved and unpreferred, and the
        crew takes a different port.
        """
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        captured: dict = {}

        def factory(iid, ssh_host, lp, rp, **k):
            captured["lp"] = lp
            return _FakeTunnel(iid, ssh_host, lp, rp, **k)

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=7900)
        reg.add(name="CE", ssh_host="ce-1-alias", instance_id="ce-1", remote_port=7901)
        mgr = self._mgr(reg, factory, monkeypatch)
        base = mgr._allocator.base_port
        shared = base + 2
        reg.update("cd-1", local_port=shared)  # survivor of a restart
        reg.update("ce-1", local_port=shared)  # a second row records the same port

        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        # The shared port is NOT reused; a different, safe port is chosen.
        assert captured["lp"] != shared
        assert captured["lp"] >= base
        assert reg.get("cd-1").local_port != shared
        # The other row's claim on the port is untouched.
        assert reg.get("ce-1").local_port == shared

    @pytest.mark.asyncio
    async def test_rebuild_never_prefers_the_recorded_port(self, tmp_path, monkeypatch):
        """A rebuild passes no port preference, gated on the flag.

        A rebuild wants a DIFFERENT port — the field evidence puts every stall on
        the first-allocated port, so a cause bound to the port itself must be
        escaped. When a rebuild finds no live tunnel to tear down, the recorded
        ``local_port`` is still on the registry row (nothing zeroed it) and the
        freed-port value is absent — so an exclusion gated on the freed port
        would let the recorded port be preferred on a rebuild, aiming straight
        back at the very port the rebuild is escaping. Gating on the rebuild flag
        keeps the recorded port unpreferred regardless.
        """
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=7900)
        mgr = self._mgr(reg, _FakeTunnel, monkeypatch)
        base = mgr._allocator.base_port
        recorded = base + 2
        # A recorded port survives on the row, but no live tunnel is tracked — a
        # rebuild here skips teardown, so the row's port is not zeroed.
        reg.update("cd-1", local_port=recorded, was_connected=True)
        assert "cd-1" not in mgr._tunnels

        real_allocate = mgr._allocator.allocate
        preferred_seen: list[int] = []

        def spy_allocate(*, exclude, preferred=0):
            preferred_seen.append(preferred)
            return real_allocate(exclude=exclude, preferred=preferred)

        monkeypatch.setattr(mgr._allocator, "allocate", spy_allocate)

        rebuilt = await mgr.connect("cd-1", rebuild=True)
        assert rebuilt.state == TunnelState.CONNECTED
        # The allocation on the rebuild path is asked for NO preference, even
        # though the recorded port is present and free.
        assert preferred_seen == [0], "a rebuild must pass preferred=0, never the recorded port"
        assert rebuilt.local_port != recorded, "rebuild must not land back on the recorded port"
        assert rebuilt.local_port >= base

    @pytest.mark.asyncio
    async def test_port_conflict_hard_fails(self, tmp_path, monkeypatch):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        captured: dict = {}

        def factory(iid, ssh_host, lp, rp, **k):
            captured["called"] = True
            return _FakeTunnel(iid, ssh_host, lp, rp, **k)

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=7900)
        mgr = self._mgr(reg, factory, monkeypatch, port_free=False)

        status = await mgr.connect("cd-1")
        assert status.state == TunnelState.ERROR
        assert "was taken while connecting" in (status.error or "")
        # We fail before opening the tunnel — factory never invoked.
        assert "called" not in captured


class TestLastError:
    """Retained last-error: a failed connect remembers *why* so a sticky tab
    whose tunnel is down can show its error instead of a bare "disconnected"."""

    @pytest.fixture(autouse=True)
    # flake-ok: pre-existing autouse port-probe stub on the shared monkeypatch, moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _free_ports(self, monkeypatch):
        _patch_port_probe(monkeypatch)

    def _mgr(self, tmp_path, *, mint=None, factory=_FakeTunnel):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            return "SECRET_TOK"

        return reg, SshTunnelManager(
            reg, base_port=53400, mint_token=mint or ok_mint, tunnel_factory=factory
        )

    @pytest.mark.asyncio
    async def test_retained_on_validation_failure(self, tmp_path):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="Bad", ssh_host="-obadhost", instance_id="bad")
        await mgr.connect("bad")
        assert mgr.status("bad") is None  # no live tunnel was created
        assert "invalid ssh settings" in (mgr.last_error("bad") or "")

    @pytest.mark.asyncio
    async def test_retained_on_tunnel_start_failure(self, tmp_path):
        def failing(*a, **k):
            t = _FakeTunnel(*a, **k)
            t.start_result = False
            return t

        reg, mgr = self._mgr(tmp_path, factory=failing)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        await mgr.connect("cd-1")
        # _FakeTunnel.start sets status.error="boom" on failure.
        assert mgr.status("cd-1") is None  # failed tunnel popped, not left lingering
        assert mgr.last_error("cd-1") == "boom"

    @pytest.mark.asyncio
    async def test_retained_on_mint_failure_after_teardown(self, tmp_path):
        from kiro_crew.instances.token_mint import TokenMintError

        async def bad_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            raise TokenMintError("nope")

        reg, mgr = self._mgr(tmp_path, mint=bad_mint)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        await mgr.connect("cd-1")
        assert mgr.status("cd-1") is None  # tunnel popped on mint failure
        assert "token mint failed" in (mgr.last_error("cd-1") or "")

    @pytest.mark.asyncio
    async def test_cleared_on_successful_connect(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState
        from kiro_crew.instances.token_mint import TokenMintError

        calls = {"n": 0}

        async def flaky_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TokenMintError("first attempt fails")
            return "SECRET_TOK"

        reg, mgr = self._mgr(tmp_path, mint=flaky_mint)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        await mgr.connect("cd-1")
        assert mgr.last_error("cd-1")  # set after the first failure
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        assert mgr.last_error("cd-1") is None  # cleared on the clean connect
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_cleared_on_explicit_disconnect(self, tmp_path):
        from kiro_crew.instances.token_mint import TokenMintError

        async def bad_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            raise TokenMintError("nope")

        reg, mgr = self._mgr(tmp_path, mint=bad_mint)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        await mgr.connect("cd-1")
        assert mgr.last_error("cd-1")
        await mgr.disconnect("cd-1")
        assert mgr.last_error("cd-1") is None


class TestStatusForRetainedError:
    """_status_for must surface a retained error (state="error") when no live
    tunnel exists, and fall back to "disconnected" only when there is none."""

    @pytest.fixture(autouse=True)
    # flake-ok: pre-existing autouse port-probe stub on the shared monkeypatch, moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _free_ports(self, monkeypatch):
        _patch_port_probe(monkeypatch)

    def _mgr(self, tmp_path, *, mint=None, factory=_FakeTunnel):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            return "SECRET_TOK"

        return reg, SshTunnelManager(
            reg, base_port=53400, mint_token=mint or ok_mint, tunnel_factory=factory
        )

    @pytest.mark.asyncio
    async def test_surfaces_error_when_no_live_tunnel(self, tmp_path):
        import types

        from kiro_crew.dashboard.handlers_instances import _status_for
        from kiro_crew.instances.token_mint import TokenMintError

        async def bad_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            raise TokenMintError("nope")

        reg, mgr = self._mgr(tmp_path, mint=bad_mint)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        await mgr.connect("cd-1")  # fails -> no live tunnel, last_error retained

        state = types.SimpleNamespace(instances_manager=mgr)
        d = _status_for(state, "cd-1")
        assert d["state"] == "error"
        assert "token mint failed" in d["error"]

    @pytest.mark.asyncio
    async def test_disconnected_when_no_tunnel_and_no_error(self, tmp_path):
        import types

        from kiro_crew.dashboard.handlers_instances import _status_for

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        state = types.SimpleNamespace(instances_manager=mgr)
        d = _status_for(state, "cd-1")
        assert d == {"instance_id": "cd-1", "state": "disconnected"}

    @pytest.mark.asyncio
    async def test_live_tunnel_status_wins(self, tmp_path):
        import types

        from kiro_crew.dashboard.handlers_instances import _status_for

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        await mgr.connect("cd-1")
        state = types.SimpleNamespace(instances_manager=mgr)
        d = _status_for(state, "cd-1")
        assert d["state"] == "connected"
        await mgr.shutdown()


class TestStartupRevive:
    """_revive_intended_instances: reconnect every was_connected instance on
    startup and isolate per-instance failures. No credential-staleness gate —
    a failed reconnect simply leaves a sticky error tab to retry."""

    @pytest.fixture(autouse=True)
    # flake-ok: pre-existing autouse port-probe stub on the shared monkeypatch, moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _free_ports(self, monkeypatch):
        _patch_port_probe(monkeypatch)

    def _mgr(self, tmp_path, *, mint=None, factory=_FakeTunnel):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(path=tmp_path / "instances.json")

        async def ok_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            return "SECRET_TOK"

        return reg, SshTunnelManager(
            reg, base_port=53400, mint_token=mint or ok_mint, tunnel_factory=factory
        )

    @pytest.mark.asyncio
    async def test_revives_all_was_connected(self, tmp_path):
        import kiro_crew.dashboard.server as server
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="A", ssh_host="host-a", instance_id="a", remote_port=7777)
        reg.add(name="B", ssh_host="host-b", instance_id="b", remote_port=7778)
        reg.add(name="C", ssh_host="host-c", instance_id="c", remote_port=7779)
        reg.update("a", was_connected=True)
        reg.update("b", was_connected=True)  # c was never connected

        await server._revive_intended_instances(reg, mgr)

        assert mgr.status("a").state == TunnelState.CONNECTED
        assert mgr.status("b").state == TunnelState.CONNECTED
        assert mgr.status("c") is None  # not intended -> not revived
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_some_fail_isolated_and_intent_preserved(self, tmp_path):
        import kiro_crew.dashboard.server as server
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState
        from kiro_crew.instances.token_mint import TokenMintError

        async def mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            if "bad" in host:
                raise TokenMintError("unreachable")
            return "SECRET_TOK"

        reg, mgr = self._mgr(tmp_path, mint=mint)
        reg.add(name="Good", ssh_host="host-good", instance_id="good", remote_port=7777)
        reg.add(name="Bad", ssh_host="host-bad", instance_id="bad", remote_port=7778)
        reg.update("good", was_connected=True)
        reg.update("bad", was_connected=True)

        # One unreachable host must NOT abort the rest or raise.
        await server._revive_intended_instances(reg, mgr)

        assert mgr.status("good").state == TunnelState.CONNECTED
        assert mgr.status("bad") is None  # mint failed -> no live tunnel
        # Intent preserved so the tab persists; retained error explains why.
        assert reg.get("bad").was_connected is True
        assert "token mint failed" in (mgr.last_error("bad") or "")
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_noop_when_none_intended(self, tmp_path):
        import kiro_crew.dashboard.server as server

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="A", ssh_host="host-a", instance_id="a")  # was_connected False

        await server._revive_intended_instances(reg, mgr)  # returns early, no raise
        assert mgr.status("a") is None

    @pytest.mark.asyncio
    async def test_instances_startup_schedules_revive_in_background(self, monkeypatch):
        """_instances_startup must NOT await the reconnect.

        on_startup handlers run during runner.setup(), before the HTTP port
        binds, so awaiting serial SSH-tunnel connects (each of which can hang
        for its full timeout when the network is down) delays the port bind
        past the desktop app's 30s gateway-wait window. Revive must be a
        tracked background task so the handler returns promptly.
        """
        import asyncio
        import types

        from aiohttp import web

        import kiro_crew.dashboard.server as server

        cfg = types.SimpleNamespace(
            instances=types.SimpleNamespace(
                enabled=True,
                tunnel_base_port=53400,
                ssh_compression=False,
                connect_timeout_secs=15.0,
                mint_timeout_secs=30.0,
                max_recovery_attempts=8,
                recover_backoff_max_secs=30.0,
                probe_failure_threshold=3,
            )
        )
        monkeypatch.setattr(server, "KiroCrewConfig", types.SimpleNamespace(load=lambda: cfg))
        monkeypatch.setattr(server, "InstancesRegistry", lambda: object())
        # The manager double carries `sync_hop_holds` because startup genuinely calls
        # it: a lent hop's lease is persisted and its listening socket is not, so the
        # restart has to re-take those ports. Recorded rather than ignored so the
        # ordering below can be asserted.
        armed: list[str] = []

        class _ManagerDouble:
            def sync_hop_holds(self):
                armed.append("armed")
                return set()

            async def shutdown(self):  # registered with the pre-exec seam
                pass

        monkeypatch.setattr(server, "SshTunnelManager", lambda *a, **k: _ManagerDouble())

        started = asyncio.Event()
        release = asyncio.Event()

        async def _blocking_revive(registry, manager, state=None):
            started.set()
            # flake-ok: pre-existing park on the test's own release event, set before the bounded task drain, moved verbatim
            await release.wait()  # simulate a hung SSH connect that never returns

        monkeypatch.setattr(server, "_revive_intended_instances", _blocking_revive)
        # The hook schedules the sequencer, which re-takes the hop holds off the
        # boot path and THEN awaits the revive above.

        app = web.Application()
        state = types.SimpleNamespace(
            _background_tasks=set(), instances_registry=None, instances_manager=None
        )
        server._register_instances_hooks(app, state, 5476)
        startup_handler = list(app.on_startup)[-1]

        # Must return promptly even though revive never completes.
        # `_instances_startup` has no await points by design -- it schedules the
        # revive as a tracked background task and returns. Drive it one step with no
        # loop turn: a coroutine that completes raises StopIteration, so the
        # background task gets no turn (the `armed == []` ordering below needs that).
        # A regression that awaits the sequencer inline instead yields a value here
        # and fails by name at once rather than hanging the xdist worker (D8).
        startup_coro = startup_handler(app)
        try:
            with pytest.raises(StopIteration):
                startup_coro.send(None)
        finally:
            startup_coro.close()

        # NOT on the boot path. `on_startup` runs inside `runner.setup()`, before the
        # HTTP port is bound, and re-taking the holds costs a registry read plus one
        # bind per live lease -- data-scaled work the desktop app's gateway-wait window
        # measures. So by the time the handler returns it must NOT have run yet.
        assert armed == [], "the hop re-take ran on the boot path"

        # Revive was scheduled as a tracked background task, not awaited.
        assert len(state._background_tasks) == 1
        await await_until(started, what="the background revive starting")  # it did start in the bg

        # ...but it ran BEFORE the revive, which is the ordering that matters: revive
        # reconnects instances that allocate ports, and a lease whose hold is not yet
        # taken is a port the allocator avoids and nothing owns.
        assert armed == ["armed"], "revive started before the hop holds were re-taken"

        # Cleanup: release the hung revive and drain the task.
        release.set()
        for t in list(state._background_tasks):
            await await_until(t, what="the background task finishing")

"""``SshTunnelManager`` self-heal: refresh, rebuild and restart a failing tunnel.

Includes ``reconfigure``, which must serialize against an in-flight recovery.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from instances_helpers import _FakeTunnel, _patch_port_probe

from kiro_crew.testing.wait import await_until


class TestReconfigureAtomicity:
    """`reconfigure` must serialize against everything else taking the lock."""

    def _manager(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        reg = InstancesRegistry(tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        return SshTunnelManager(reg), reg

    def test_reconfigure_holds_the_lock_across_teardown_and_persist(self, tmp_path):
        """`connect` takes the same lock, so a reconfiguration in progress must
        block it — that mutual exclusion is what removes the window where a
        connect could read the pre-edit coordinates."""
        mgr, _reg = self._manager(tmp_path)
        applied: list[str] = []

        async def scenario():
            # Stand in for a connect that already holds the lock.
            async with mgr._lock:
                task = asyncio.create_task(
                    mgr.reconfigure("cd-1", lambda: applied.append("persisted"))
                )
                # Yield generously: while the lock is held, nothing may persist.
                for _ in range(5):
                    # flake-ok: pre-existing five-yield barrier before the lock assertion, moved verbatim; tracked for a de-flake pass
                    await asyncio.sleep(0)
                assert applied == [], "reconfigure wrote without holding the lock"
            # flake-ok: pre-existing unbounded await of the reconfigure task once the held lock is released, moved verbatim
            await task
            assert applied == ["persisted"]

        asyncio.run(scenario())

    def test_reconfigure_cancels_this_instances_in_flight_recovery(self, tmp_path):
        """Self-heal reads the record BEFORE it takes the lock, so a recovery
        already in flight carries the pre-edit coordinates. It must be cancelled
        and awaited, or it reinstalls a tunnel to the old machine after the edit —
        and `connect()` being idempotent would then hand that tunnel out for the
        new settings. Another instance's recovery must be left alone."""
        mgr, _reg = self._manager(tmp_path)
        started = asyncio.Event()
        outcome: list[str] = []

        async def scenario():
            async def stale_recovery():
                started.set()
                try:
                    # flake-ok: pre-existing 30 s sleep stands in for a recovery that is always cancelled, moved verbatim
                    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep shape, moved verbatim; tracked for a de-flake pass
                    await asyncio.sleep(30)
                    outcome.append("reinstalled-old-coordinates")
                except asyncio.CancelledError:
                    outcome.append("cancelled")
                    raise

            async def other_instance_recovery():
                try:
                    # flake-ok: pre-existing 30 s sleep stands in for another instance's recovery the test cancels, moved verbatim
                    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep shape, moved verbatim; tracked for a de-flake pass
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    outcome.append("other-cancelled")
                    raise

            mgr._track_recovery("cd-1", asyncio.create_task(stale_recovery()))
            other = asyncio.create_task(other_instance_recovery())
            mgr._track_recovery("cd-2", other)
            # flake-ok: pre-existing unbounded wait for the stale recovery task to signal it started, moved verbatim
            await started.wait()

            await mgr.reconfigure("cd-1", lambda: outcome.append("persisted"))
            # The stale recovery is finished (not merely signalled) before the
            # coordinates are written.
            # flake-ok: pre-existing order assertion the product's cancel-then-write sequence defines, moved verbatim
            assert outcome == ["cancelled", "persisted"], outcome
            assert not other.done(), "another instance's recovery was cancelled"
            other.cancel()
            # flake-ok: pre-existing await of a just-cancelled task that re-raises its cancellation, moved verbatim
            await asyncio.gather(other, return_exceptions=True)

        asyncio.run(scenario())

    def test_an_in_flight_token_mint_cannot_outlive_the_reconfiguration(self, tmp_path):
        """Both background writers must be stopped AND unwound before the move.

        Self-heal rebuilds a tunnel and token refresh mints a credential, each from
        the record it read. A refresh left running would finish after the edit and
        store a token minted from the pre-edit coordinates against the rebuilt
        tunnel — the embedded dashboard would be handed a credential the new remote
        never issued. Signalling a cancel is not enough; it has to be awaited.
        """
        mgr, _reg = self._manager(tmp_path)
        events: list[str] = []

        async def slow_mint():
            try:
                # flake-ok: pre-existing 30 s sleep stands in for a token mint that reconfigure always cancels, moved verbatim
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep shape, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(30)
                events.append("stored-stale-token")
            except asyncio.CancelledError:
                events.append("refresh-unwound")
                raise

        async def scenario():
            mgr._refresh_tasks["cd-1"] = asyncio.create_task(slow_mint())
            # flake-ok: pre-existing one-yield barrier lets the mint task reach its await before reconfigure, moved verbatim
            await asyncio.sleep(0)  # let it reach its await

            await mgr.reconfigure("cd-1", lambda: events.append("persisted"))

            # Unwound BEFORE the write, not merely signalled.
            # flake-ok: pre-existing order assertion the product's unwind-then-write sequence defines, moved verbatim
            assert events == ["refresh-unwound", "persisted"], events
            assert "cd-1" not in mgr._refresh_tasks
            # A refresh cannot be restarted while the barrier is up either.
            mgr._reconfiguring.add("cd-1")
            mgr._schedule_token_refresh("cd-1")
            assert "cd-1" not in mgr._refresh_tasks
            mgr._reconfiguring.discard("cd-1")

        asyncio.run(scenario())

    def test_a_recovery_scheduled_mid_reconfigure_is_refused(self, tmp_path):
        """Cancelling the recoveries in flight is not enough on its own.

        Self-heal reads the record before it takes the lock, and the cancellation
        itself awaits — so a tunnel exiting during that await schedules a FRESH
        recovery holding pre-edit coordinates. The barrier raised at the start of
        a reconfiguration is what makes that new attempt refuse to run. This drives
        the exact window: the tunnel dies while the cancellation is in flight.
        """
        mgr, _reg = self._manager(tmp_path)
        applied: list[str] = []
        original = mgr._cancel_recovery

        async def cancel_then_the_tunnel_dies(instance_id: str) -> None:
            await original(instance_id)
            # Barrier is up, lock not yet taken: the scheduling seam must refuse.
            mgr._on_tunnel_exit(instance_id)

        mgr._cancel_recovery = cancel_then_the_tunnel_dies  # type: ignore[method-assign]

        async def scenario():
            await mgr.reconfigure("cd-1", lambda: applied.append("persisted"))
            assert applied == ["persisted"]
            # No recovery was scheduled for this instance while the barrier held.
            assert not mgr._recovery_by_instance.get("cd-1")
            # And the barrier is down afterwards, so normal self-heal resumes.
            assert "cd-1" not in mgr._reconfiguring

        asyncio.run(scenario())

    def test_reconfigure_aborts_and_keeps_the_tunnel_when_stop_fails(self, tmp_path):
        """A failed stop must not orphan the forward or advance the record.

        Nothing is discarded unless the stop succeeded: the tunnel keeps its place
        in ``_tunnels`` AND its token and refresh task, because a live forward with
        no credential is not usable (session transfer reports
        ``transfer_no_credential``). Nothing is persisted either.
        """
        mgr, _reg = self._manager(tmp_path)
        applied: list[str] = []

        class _StubbornTunnel:
            async def stop(self):
                raise OSError("ssh process will not die")

        mgr._tunnels["cd-1"] = _StubbornTunnel()  # type: ignore[assignment]
        mgr._tokens["cd-1"] = "live-token"

        async def idle_refresh():
            # flake-ok: pre-existing 30 s sleep stands in for an idle refresh loop the test cancels, moved verbatim
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep shape, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(30)

        async def scenario():
            mgr._refresh_tasks["cd-1"] = asyncio.create_task(idle_refresh())
            # flake-ok: pre-existing one-yield barrier lets the refresh task start before reconfigure, moved verbatim
            await asyncio.sleep(0)

            with pytest.raises(OSError):
                await mgr.reconfigure("cd-1", lambda: applied.append("persisted"))
            assert applied == [], "coordinates were written over a live tunnel"
            assert "cd-1" in mgr._tunnels, "the forward was left untracked"
            assert mgr._tokens.get("cd-1") == "live-token", "a live tunnel lost its token"
            # The refresh loop keeps the live tunnel's credential fresh, so a
            # REJECTED edit must not have torn it down either.
            task = mgr._refresh_tasks.get("cd-1")
            assert task is not None and not task.done(), "a live tunnel lost its refresh"
            assert "cd-1" not in mgr._reconfiguring, "barrier not cleared"
            task.cancel()
            # flake-ok: pre-existing await of the just-cancelled refresh task, which re-raises its cancellation, moved verbatim
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(scenario())

    def test_a_cancelled_request_does_not_release_the_lock_mid_write(self, tmp_path):
        """Cancelling the caller must not open the critical section.

        The registry write runs on a worker thread. If the awaiting task is
        cancelled (the client hung up), an unshielded await would unwind the
        ``async with`` and free the lock while that write was still in flight — a
        concurrent connect could then read the pre-edit coordinates. The write is
        awaited out under the lock, and only then does the cancellation land.
        """
        mgr, _reg = self._manager(tmp_path)
        started = threading.Event()
        finished = threading.Event()

        def slow_write():
            started.set()
            # flake-ok: pre-existing 0.3 s sleep keeps the registry write in flight while the test cancels the request, moved verbatim
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep shape, moved verbatim; tracked for a de-flake pass
            time.sleep(0.3)
            finished.set()
            return "persisted"

        async def scenario():
            task = asyncio.create_task(mgr.reconfigure("cd-1", slow_write))
            # flake-ok: pre-existing 5 s wait for the write thread to start; a miss surfaces at the finished assert, moved verbatim
            await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                # flake-ok: pre-existing unbounded await of the cancelled reconfigure task after its 0.3 s write, moved verbatim
                await task
            # The write ran to completion before the lock was surrendered.
            assert finished.is_set(), "the write was abandoned mid-flight"
            assert not mgr._lock.locked(), "the lock was not released"

        asyncio.run(scenario())


class _ResilTunnel:
    """Controllable fake tunnel for self-heal tests."""

    def __init__(
        self,
        iid,
        ssh_host,
        lp,
        rp,
        *,
        connect_timeout_secs=0,
        compression=True,
        probe_failure_threshold=0,
        on_exit=None,
        transport="ssh",
        ssm_target="",
        aws_profile="",
        aws_region="",
    ):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        self._S = TunnelState
        self.transport = transport
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)
        self.start_result = True
        # Mirrors _SshTunnel.pid; _mark_recovered persists it after a rebuild.
        self.pid = None

    async def start(self):
        self.status.state = self._S.CONNECTED if self.start_result else self._S.ERROR
        return self.start_result

    async def stop(self):
        self.status.state = self._S.STOPPED


class TestSelfHealRefreshRestart:
    @pytest.fixture(autouse=True)
    # flake-ok: pre-existing class autouse fixture patches the port probe on the shared monkeypatch, moved verbatim
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _free_ports(self, monkeypatch):
        _patch_port_probe(monkeypatch)

    def _mgr(self, tmp_path, *, mint=None, factory=_ResilTunnel):
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
            return "TOK"

        return reg, SshTunnelManager(
            reg, base_port=53900, mint_token=mint or ok_mint, tunnel_factory=factory
        )

    @pytest.mark.asyncio
    async def test_recover_tier1_then_tier2(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        # Tier 1 success
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        await mgr._recover("cd-1")
        assert mgr.status("cd-1").state == TunnelState.CONNECTED
        # The rebuild only re-binds the local port, so it leaves the attempt
        # counter at the value this recovery raised it to (1). The counter is
        # cleared later by a proven end-to-end probe, which
        # test_rebuild_does_not_reset_the_counter_until_a_probe_proves_the_forward_live
        # pins directly.
        assert mgr._recover_attempts.get("cd-1", 0) == 1

    @pytest.mark.asyncio
    async def test_rebuild_does_not_reset_the_counter_until_a_probe_proves_the_forward_live(
        self, tmp_path
    ):
        """A rebuild's start() waits only for the LOCAL bind, so it is not proof
        the far end answers — the attempt counter is NOT reset by the rebuild.
        Only a proven end-to-end probe success (the _on_tunnel_healthy seam the
        probe loop fires) clears it. This keeps a forward whose remote gateway
        is down from resetting its budget every rebuild and churning forever,
        while a single slow post-rebuild probe cannot ratchet the budget down
        permanently — the next good probe clears it."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        mgr._recover_attempts["cd-1"] = 2

        await mgr._recover("cd-1")
        # start() reported success (local bind), so the tunnel is CONNECTED...
        assert mgr.status("cd-1").state == TunnelState.CONNECTED
        # ...but the rebuild alone did NOT reset the counter (it climbed by one).
        assert mgr._recover_attempts.get("cd-1", 0) == 3

        # A proven end-to-end probe success — what the probe loop fires — clears it.
        mgr._on_tunnel_healthy("cd-1")
        assert mgr._recover_attempts.get("cd-1", 0) == 0

    @pytest.mark.asyncio
    async def test_on_tunnel_healthy_clears_the_attempt_counter_idempotently(self, tmp_path):
        """The probe-success seam is idempotent: clearing an absent key is a
        no-op, so a healthy tunnel that never needed recovery costs nothing."""
        reg, mgr = self._mgr(tmp_path)
        mgr._recover_attempts["cd-1"] = 5
        mgr._on_tunnel_healthy("cd-1")
        assert "cd-1" not in mgr._recover_attempts
        mgr._on_tunnel_healthy("cd-1")  # absent key -> no-op, no raise
        assert "cd-1" not in mgr._recover_attempts

    @pytest.mark.asyncio
    async def test_probe_loop_fires_on_healthy_on_a_successful_probe(self, tmp_path):
        """Wiring guard: a successful _forward_alive in the probe loop must fire
        the _on_healthy seam so the manager can clear the attempt counter."""
        from kiro_crew.instances import ssh_tunnel_manager as stm
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        stm_interval = stm._PROBE_INTERVAL
        healthy: list[str] = []
        t = _SshTunnel("cd-1", "h", 7778, 7777)
        t.status.state = TunnelState.CONNECTED
        t._on_healthy = healthy.append

        async def _alive():
            # Answer once, then stop the loop so the test does not spin.
            t._stopping = True
            return True

        t._forward_alive = _alive  # type: ignore[assignment]

        async def main():
            import asyncio as _aio

            stm._PROBE_INTERVAL = 0.01
            try:
                await await_until(
                    _aio.ensure_future(t._probe_loop()), what="the probe loop exiting"
                )
            finally:
                stm._PROBE_INTERVAL = stm_interval

        await main()
        assert healthy == ["cd-1"]

    def test_recover_releases_lock_during_io(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        started = asyncio.Event()
        release = asyncio.Event()

        class SlowTunnel(_ResilTunnel):
            async def start(self):
                started.set()
                # flake-ok: pre-existing park on the test's own release event, moved verbatim; tracked for a de-flake pass
                await release.wait()  # block the "slow" rebuild I/O
                self.status.state = self._S.CONNECTED
                return True

        reg, mgr = self._mgr(tmp_path, factory=SlowTunnel)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # seed a live ERROR tunnel so _recover proceeds to a (slow) tier-1 rebuild
        mgr._tunnels["cd-1"] = _ResilTunnel("cd-1", "cd-1-alias", 53999, 7777)
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR

        async def main():
            task = asyncio.create_task(mgr._recover("cd-1"))
            await await_until(started, what="the slow rebuild starting")
            # Slow rebuild is in flight — the manager lock must NOT be held (the fix).
            assert not mgr._lock.locked()
            release.set()
            await await_until(task, what="_recover finishing")
            assert mgr.status("cd-1").state == TunnelState.CONNECTED
            # The rebuild only re-binds locally, so the counter stands at the
            # value this recovery raised it to (1), not reset by the rebuild.
            assert mgr._recover_attempts.get("cd-1", 0) == 1

        asyncio.run(main())

    @pytest.mark.asyncio
    async def test_recover_attempt_cap_then_diagnose(self, tmp_path, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        def failing(*a, **k):
            t = _ResilTunnel(*a, **k)
            t.start_result = False
            return t

        reg, mgr = self._mgr(tmp_path, factory=failing)
        mgr._max_recovery = 2
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # seed a live ERROR tunnel
        mgr._tunnels["cd-1"] = _ResilTunnel("cd-1", "cd-1-alias", 53999, 7777)
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        diag_calls = []
        monkeypatch.setattr(mgr, "_schedule_diagnosis", lambda i: diag_calls.append(i))
        for _ in range(mgr._max_recovery + 1):
            mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
            await mgr._recover("cd-1")
        assert diag_calls == ["cd-1"], diag_calls  # diagnosis scheduled once cap exceeded

    # ── respawn-loop fix (orphaned port-holder) ──────────────────────────────

    @pytest.mark.asyncio
    async def test_rebuild_stops_old_before_replace(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        old = _ResilTunnel("cd-1", "cd-1-alias", 53999, 7777)
        old.status.state = TunnelState.ERROR
        mgr._tunnels["cd-1"] = old
        inst = reg.get("cd-1")
        # _rebuild takes resolved transport params (not a bare ssh host) so the
        # same code path serves both the ssh and ssm transports.
        ok = await mgr._rebuild(inst, mgr._resolve_transport(inst), 53999, expected_epoch=0)
        assert ok is not None
        # The old tunnel's child must be stopped (port freed) before the replace,
        # else it orphans and holds the forward port -> respawn loop.
        assert old.status.state == TunnelState.STOPPED
        assert mgr._tunnels["cd-1"] is not old

    @pytest.mark.asyncio
    async def test_connect_stops_stale_tunnel_before_replace(self, tmp_path):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        # Use the _FakeTunnel (tracks .stopped) for this manager.
        reg, mgr = self._mgr(tmp_path, factory=_FakeTunnel)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        stale = _FakeTunnel("cd-1", "cd-1-alias", 53910, 7777)
        stale.status.state = TunnelState.ERROR  # tracked but not CONNECTED
        mgr._tunnels["cd-1"] = stale
        st = await mgr.connect("cd-1")
        assert st.state == TunnelState.CONNECTED
        assert stale.stopped is True  # stale tunnel terminated before replacement
        assert mgr._tunnels["cd-1"] is not stale

    @pytest.mark.asyncio
    async def test_wait_until_ready_rejects_child_that_dies_during_probe(self):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        class _Proc:
            def __init__(self):
                self.returncode = None
                self.stderr = self

            async def read(self):
                return b"bind [127.0.0.1]:53991: Address already in use\r\n"

        t = _SshTunnel("cd-1", "cd-1-alias", 53991, 7777, connect_timeout_secs=1.0)
        t._proc = _Proc()

        async def reachable():
            # Simulate a stale holder answering while OUR child dies the same tick.
            t._proc.returncode = 255
            return True

        t._port_reachable = reachable  # type: ignore[assignment]
        assert await t._wait_until_ready() is False
        assert t.status.state == TunnelState.ERROR
        assert "in use" in t.status.error.lower()
        assert "post-quantum" not in t.status.error.lower()

    @staticmethod
    async def _timeout_error_for_live_child(*, tunable=True, **kwargs):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        class _LiveProc:
            returncode = None  # never exits: still connecting at the deadline

        t = _SshTunnel("cd-1", "cd-1-alias", 53992, 7777, connect_timeout_secs=0.05, **kwargs)
        t._timeout_tunable = tunable
        t._proc = _LiveProc()

        async def never_reachable():
            return False

        t._port_reachable = never_reachable  # type: ignore[assignment]
        assert await t._wait_until_ready() is False
        return t.status.error.lower()

    @pytest.mark.asyncio
    async def test_wait_until_ready_timeout_with_live_child_names_the_tunable(self):
        # A live ssh child at the deadline may be a slow proxy handshake or an
        # unreachable host: the message names both and the timeout tunable.
        err = await self._timeout_error_for_live_child()
        assert "timed out after 0.05s waiting for the ssh forward" in err
        assert "proxy handshake" in err
        assert "unreachable" in err
        assert "instances.connect_timeout_secs" in err
        assert "ssm" not in err

    @pytest.mark.asyncio
    async def test_wait_until_ready_timeout_on_ssm_names_the_ssm_session(self):
        # The SSM transport has no ssh proxy: its live-child timeout speaks of
        # the SSM session and agent, and still names the timeout tunable.
        err = await self._timeout_error_for_live_child(
            transport="ssm", ssm_target="i-0123456789abcdef0"
        )
        assert "timed out after 0.05s waiting for the ssm port forward" in err
        assert "ssm agent" in err
        assert "instances.connect_timeout_secs" in err
        assert "ssh" not in err
        assert "proxy" not in err

    def test_ready_timeout_at_ceiling_does_not_suggest_raising_it(self):
        # A budget already at the ceiling cannot be raised: the message points
        # at reachability and omits the tunable.
        from kiro_crew.instances.constants import CONNECT_TIMEOUT_CEILING_SECS
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        class _LiveProc:
            returncode = None

        t = _SshTunnel(
            "cd-1", "cd-1-alias", 53994, 7777, connect_timeout_secs=CONNECT_TIMEOUT_CEILING_SECS
        )
        t._timeout_tunable = True
        t._proc = _LiveProc()
        err = t._ready_timeout_error().lower()
        assert "already at its maximum" in err
        assert "reachable" in err
        assert "instances.connect_timeout_secs" not in err

    @pytest.mark.parametrize("kwargs", [{}, {"transport": "ssm", "ssm_target": "i-0abc"}])
    @pytest.mark.asyncio
    async def test_ready_timeout_on_an_untunable_tunnel_names_no_setting(self, kwargs):
        # A tunnel whose budget does not come from instances.connect_timeout_secs
        # (the desktop tunnel keeper's) keeps the cause text but never tells the
        # user to raise a setting that would change nothing.
        err = await self._timeout_error_for_live_child(tunable=False, **kwargs)
        assert "timed out after 0.05s" in err
        assert "reachable" in err
        assert "instances.connect_timeout_secs" not in err
        assert "kirocrew config set" not in err

    @pytest.mark.asyncio
    async def test_a_bare_tunnel_is_untunable_and_the_manager_marks_its_own(self, tmp_path):
        # The default is off, so a tunnel built outside the manager (tunnel_keeper's
        # make_tunnel) names no setting. The manager reads the setting into the
        # budget, so connect() and a self-heal rebuild both turn it on.
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        assert _SshTunnel("cd-1", "cd-1-alias", 53995, 7777)._timeout_tunable is False
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        assert mgr._tunnels["cd-1"]._timeout_tunable is True
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        await mgr._recover("cd-1")
        assert mgr._tunnels["cd-1"]._timeout_tunable is True

    @pytest.mark.asyncio
    async def test_wait_until_ready_timeout_with_dead_child_stays_generic(self):
        # A child that exited in the same tick the loop gave up (too late for
        # _failed_on_child_exit to classify) keeps the generic wording rather
        # than guessing the proxy-handshake cause.
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        class _DeadProc:
            returncode = 255  # already gone by the deadline

        t = _SshTunnel("cd-1", "cd-1-alias", 53993, 7777, connect_timeout_secs=0.05)
        t._proc = _DeadProc()

        async def never_reachable():
            return False

        async def never_classified():
            # Keep the loop from ending early so it reaches the timeout path,
            # where _proc.returncode is already set (child gone).
            return False

        t._port_reachable = never_reachable  # type: ignore[assignment]
        t._failed_on_child_exit = never_classified  # type: ignore[assignment]
        assert await t._wait_until_ready() is False
        err = t.status.error.lower()
        assert "timed out after 0.05s waiting for forward" in err
        assert "instances.connect_timeout_secs" not in err

    def test_exit_error_strips_post_quantum_noise(self):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        t = _SshTunnel("cd-1", "h", 1, 2)
        t._stderr_buf = (
            "** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
            '** This session may be vulnerable to "store now, decrypt later" attacks.\n'
            "** The server may need to be upgraded. See https://openssh.com/pq.html\n"
            "bind [127.0.0.1]:7778: Address already in use\n"
        )
        err = t._exit_error(255)
        assert "post-quantum" not in err.lower()
        assert "already in use" in err.lower()
        # Pure-noise stderr falls back to the bare exit code (no false detail).
        t._stderr_buf = (
            "** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
        )
        assert t._exit_error(255) == "ssh exited with code 255"

    def test_exit_error_classifies_wssh_transport_vs_auth(self):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        t = _SshTunnel("cd-1", "h", 1, 2)

        # WSSH transport drop carrying ANSI + passthrough auth-prompt prose is
        # NOT an auth verdict — it classifies as a transport drop, and the ANSI is
        # stripped from the surfaced detail (never reflected raw).
        t._stderr_buf = (
            "\x1b[1G\x1b[31m[Message from WSSH Proxy Service] "
            "Your SSH session ended unexpectedly. Re-authenticate if your session expired.\x1b[0m\n"
        )
        err = t._exit_error(255)
        assert "transport drop" in err.lower()
        assert "auth failed" not in err.lower()
        assert "\x1b" not in err  # ANSI stripped

        # Banner-exchange timeout (the re-mint failure case) is also transport.
        t._stderr_buf = "Connection timed out during banner exchange\n"
        assert "transport drop" in t._exit_error(255).lower()

        # A genuine ssh auth failure IS reported as auth.
        t._stderr_buf = "host: Permission denied (publickey).\n"
        auth = t._exit_error(255)
        assert "auth failed" in auth.lower()
        assert "transport drop" not in auth.lower()

        # A real certificate-expiry message stays an auth verdict.
        t._stderr_buf = "Certificate has expired\n"
        assert "auth failed" in t._exit_error(255).lower()

    def test_first_connect_transport_drop_points_at_ssh_host(self):
        """A drop before the forward ever came up most often means the
        instance's ``ssh_host`` names a host that does not exist under that
        name (rebuilt, renamed): the proxy closes the socket before the ssh
        handshake and ssh prints only "Connection closed by UNKNOWN". The
        first-connect classification must name ``ssh_host``; a drop on a tunnel
        that WAS up must not, because that host was reachable."""
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        t = _SshTunnel("cd-1", "h", 1, 2)
        t._stderr_buf = (
            "kex_exchange_identification: Connection closed by remote host\r\n"
            "Connection closed by UNKNOWN port 65535\r\n"
        )
        first = t._exit_error(255, first_connect=True)
        assert first.startswith("ssh tunnel transport drop (")
        assert "ssh_host" in first
        assert "Connection closed by UNKNOWN port 65535" in first
        # Steady-state wording is unchanged: same classification, no hint.
        steady = t._exit_error(255)
        assert steady.startswith("ssh tunnel transport drop:")
        assert "ssh_host" not in steady
        # The hint never leaks into the other classes.
        t._stderr_buf = "host: Permission denied (publickey).\n"
        assert "ssh_host" not in t._exit_error(255, first_connect=True)
        t._stderr_buf = "bind [127.0.0.1]:7778: Address already in use\n"
        assert "ssh_host" not in t._exit_error(255, first_connect=True)

    @pytest.mark.asyncio
    async def test_wait_until_ready_child_death_is_a_first_connect_error(self):
        """``_wait_until_ready`` is the first-connect path, so a child that dies
        there with a transport drop surfaces the ``ssh_host`` hint in
        ``status.error`` -- the string the dashboard shows on Connect."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        class _Proc:
            def __init__(self):
                self.returncode = 255
                self.stderr = self

            async def read(self):
                return b"Connection closed by UNKNOWN port 65535\r\nConnection reset by peer\r\n"

        t = _SshTunnel("cd-1", "stale-alias", 53991, 7777, connect_timeout_secs=1.0)
        t._proc = _Proc()
        assert await t._wait_until_ready() is False
        assert t.status.state == TunnelState.ERROR
        assert "ssh_host" in t.status.error
        assert t.status.error.startswith("ssh tunnel transport drop (")

    def test_recover_backoff_grows_and_caps(self):
        from kiro_crew.instances.ssh_tunnel_manager import (
            _RECOVER_BACKOFF_MAX_SECS,
            _recover_backoff_secs,
        )

        assert _recover_backoff_secs(1) < _recover_backoff_secs(2) < _recover_backoff_secs(3)
        assert _recover_backoff_secs(99) == _RECOVER_BACKOFF_MAX_SECS

    @pytest.mark.asyncio
    async def test_refresh_token_once(self, tmp_path):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", ttl="20h")
        await mgr.connect("cd-1")
        assert mgr._refresh_tasks.get("cd-1") is not None
        ttl0 = mgr.token_ttl_remaining("cd-1")
        # flake-ok: pre-existing ttl check reads the real clock with a 1000 s margin, far past the test timeout, moved verbatim
        assert ttl0 is not None and ttl0 > 71000
        ok = await mgr._refresh_token_once("cd-1")
        assert ok and mgr.get_token("cd-1") == "TOK"
        # disconnect cancels refresh + clears ttl
        await mgr.disconnect("cd-1")
        assert "cd-1" not in mgr._refresh_tasks
        assert mgr.token_ttl_remaining("cd-1") is None

    @pytest.mark.asyncio
    async def test_a_token_minted_for_a_replaced_tunnel_is_discarded(self, tmp_path):
        """A mint runs WITHOUT the manager lock, so the tunnel it was minted for can
        be torn down and replaced while it is in flight — and `instance_id in
        self._tunnels` is true again for the REPLACEMENT, so it cannot tell the two
        apart. The request-driven `refresh_token()` the embedded dashboard calls is
        not a task in `_refresh_tasks`, so it cannot be cancelled by name either;
        the generation stamp is what makes the write refuse itself.
        """
        # Only the mint under test blocks; connect's own mints must not.
        arm = asyncio.Event()
        started = asyncio.Event()
        release = asyncio.Event()
        minted = 0

        async def slow_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            nonlocal minted
            minted += 1
            if arm.is_set():
                arm.clear()
                started.set()
                # flake-ok: pre-existing park of the armed mint on the test's own release event, moved verbatim
                await release.wait()
                return "TOK-STALE"
            return f"TOK-{minted}"

        reg, mgr = self._mgr(tmp_path, mint=slow_mint)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        first_epoch = mgr._tunnel_epoch["cd-1"]

        # A refresh begins against the CURRENT tunnel and blocks inside its mint.
        arm.set()
        refresh = asyncio.create_task(mgr._refresh_token_once("cd-1"))
        await await_until(started, what="the rebuild starting")
        # Meanwhile the tunnel is replaced (an edit + reconnect, or a self-heal).
        # flake-ok: pre-existing await of disconnect while the mint is parked; teardown never awaits that task, moved verbatim
        await mgr.disconnect("cd-1")
        # flake-ok: pre-existing await of connect while the mint is parked without the lock, so it returns, moved verbatim
        await mgr.connect("cd-1")
        assert mgr._tunnel_epoch["cd-1"] > first_epoch
        good = mgr.get_token("cd-1")

        release.set()
        # flake-ok: pre-existing unbounded await of the refresh task right after the test sets release, moved verbatim
        assert await refresh is False, "a token for a superseded tunnel must not be stored"
        # The valid token of the CURRENT tunnel is untouched.
        assert mgr.get_token("cd-1") == good

    def _tier2_rig(self, tmp_path):
        """Manager whose tunnel starts can be failed on demand (drives tier 1
        to fail so a recovery reaches tier 2) and whose mint can be parked on
        an Event (only while armed; connect's own mints run through)."""
        arm = asyncio.Event()
        started = asyncio.Event()
        release = asyncio.Event()
        fail_next = {"n": 0}
        minted = {"n": 0}

        def factory(*a, **k):
            t = _ResilTunnel(*a, **k)
            if fail_next["n"] > 0:
                fail_next["n"] -= 1
                t.start_result = False
            return t

        async def mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            minted["n"] += 1
            if arm.is_set():
                arm.clear()
                started.set()
                # flake-ok: pre-existing park of the armed tier-2 mint on the test's own release event, moved verbatim
                await release.wait()
                return "TOK-STALE"
            return f"TOK-{minted['n']}"

        reg, mgr = self._mgr(tmp_path, mint=mint, factory=factory)
        return reg, mgr, arm, started, release, fail_next

    @pytest.mark.asyncio
    async def test_a_tier2_remint_for_a_replaced_tunnel_is_discarded(self, tmp_path):
        """The self-heal tier-2 re-mint runs without the lock, so the operator
        can disconnect + reconnect while it is in flight; membership is then
        satisfied by the NEW generation and only the epoch stamp can refuse the
        stale store. Without the stamp check the current tunnel's token, mint
        timestamp and ttl would be overwritten by a mint it never requested,
        and the stale rebuild would replace its live tunnel.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr, arm, started, release, fail_next = self._tier2_rig(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")

        # A tunnel dies; tier 1's rebuild fails; tier 2 parks inside its mint.
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        fail_next["n"] = 1
        arm.set()
        recovery = asyncio.create_task(mgr._recover("cd-1"))
        await await_until(started, what="the rebuild starting")

        # The ordinary operator reaction: reconnect. connect() replaces the
        # (ERROR) tunnel left by the failed tier-1 rebuild and bumps the epoch.
        # flake-ok: pre-existing await of connect while the tier-2 mint is parked without the lock, so it returns, moved verbatim
        await mgr.connect("cd-1")
        good = mgr.get_token("cd-1")
        good_minted_at = mgr._token_minted_at["cd-1"]
        good_ttl = mgr._token_ttl_secs["cd-1"]
        good_tunnel = mgr._tunnels["cd-1"]
        good_refresh = mgr._refresh_tasks["cd-1"]
        good_epoch = mgr._tunnel_epoch["cd-1"]

        release.set()
        await await_until(recovery, what="the recovery task finishing")
        # The stale mint was refused whole: token, mint bookkeeping, the live
        # tunnel (no stale rebuild) and the refresh schedule are all untouched.
        assert mgr._tokens["cd-1"] == good != "TOK-STALE"
        assert mgr._token_minted_at["cd-1"] == good_minted_at
        assert mgr._token_ttl_secs["cd-1"] == good_ttl
        assert mgr._tunnels["cd-1"] is good_tunnel
        assert mgr._refresh_tasks["cd-1"] is good_refresh
        assert mgr._tunnel_epoch["cd-1"] == good_epoch

    @pytest.mark.asyncio
    async def test_tier2_without_interleaving_still_stores_and_rebuilds(self, tmp_path):
        """The guard must not be over-eager: an undisturbed tier 2 stores its
        mint and rebuilds. This also pins the +1 in the store's compare — a
        failed tier-1 rebuild installs (and bumps the stamp for) its
        replacement before start() reports failure, so an undisturbed tier 2
        always sees the Phase 1 stamp plus exactly one.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr, _arm, _started, _release, fail_next = self._tier2_rig(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        first_token = mgr.get_token("cd-1")

        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        fail_next["n"] = 1  # tier 1 fails, tier 2's own rebuild succeeds
        await mgr._recover("cd-1")

        assert mgr.status("cd-1").state == TunnelState.CONNECTED
        assert mgr.get_token("cd-1") not in (None, first_token)  # re-mint stored
        # The rebuild only re-binds locally, so the counter stands at the value
        # this recovery raised it to (1); a proven probe clears it later.
        assert mgr._recover_attempts.get("cd-1", 0) == 1

    @pytest.mark.asyncio
    async def test_a_tier2_recovery_resets_the_counter_only_on_a_proven_probe(self, tmp_path):
        """Pin the counter-reset invariant at the tier-2 site, through the
        production path. A tier-2 recovery (tier 1 fails, tier 2 re-mints and
        rebuilds) raises the attempt counter and leaves it raised — the rebuild
        is a local bind, not proof the far end answers. The recovery wires the
        rebuilt tunnel's `_on_healthy` seam to the manager's `_on_tunnel_healthy`
        (what the probe loop fires on a proven end-to-end round trip); firing it
        is the ONLY thing that clears the counter. Asserting the reset after that
        seam runs — not after a manual pop — pins the real production reset: were
        the reset deleted from `_on_tunnel_healthy`, the final assert reds."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr, _arm, _started, _release, fail_next = self._tier2_rig(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")

        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        fail_next["n"] = 1  # tier 1 fails, tier 2's own rebuild succeeds
        await mgr._recover("cd-1")

        assert mgr.status("cd-1").state == TunnelState.CONNECTED
        # Tier 2 rebuilt the forward but has not proven the far end answers, so
        # the counter stands at the value this recovery raised it to.
        assert mgr._recover_attempts.get("cd-1", 0) == 1
        # The recovery wired the rebuilt tunnel's probe-success seam to the
        # manager's reset (production wiring, not a test hook).
        rebuilt = mgr._tunnels["cd-1"]
        assert rebuilt._on_healthy == mgr._on_tunnel_healthy
        # Fire it exactly as the probe loop does on a proven end-to-end probe —
        # this runs the real _on_tunnel_healthy reset, clearing the counter.
        rebuilt._on_healthy(rebuilt.status.instance_id)
        assert mgr._recover_attempts.get("cd-1", 0) == 0

    @pytest.mark.asyncio
    async def test_a_disconnect_during_tier1_rebuild_is_not_overwritten(self, tmp_path):
        """The rebuild's slow awaits run without the lock, so a user disconnect
        can land inside its old.stop(). An unguarded rebuild would reinstall a
        tunnel and record the recovery, persisting was_connected=True straight
        over the disconnect's False — reviving, across restarts, an instance
        the user turned off. The teardown's epoch bump makes the recovery's
        expected generation stale, so the gated install refuses and the
        recovery stands down.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        arm = asyncio.Event()
        parked = asyncio.Event()
        release = asyncio.Event()

        class _StopParkTunnel(_ResilTunnel):
            async def stop(self):
                if arm.is_set():
                    arm.clear()
                    parked.set()
                    # flake-ok: pre-existing park of old.stop() on the test's own release event, moved verbatim
                    await release.wait()
                await super().stop()

        reg, mgr = self._mgr(tmp_path, factory=_StopParkTunnel)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        assert reg.get("cd-1").was_connected is True

        # A tunnel dies; tier 1's rebuild parks inside old.stop().
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        arm.set()
        recovery = asyncio.create_task(mgr._recover("cd-1"))
        await await_until(parked, what="the rebuild parking")

        # The user turns the instance off while the rebuild is parked.
        # flake-ok: pre-existing await of disconnect while the rebuild is parked; teardown never drains self-heal, moved verbatim
        assert await mgr.disconnect("cd-1") is True
        assert reg.get("cd-1").was_connected is False

        release.set()
        await await_until(recovery, what="the recovery task finishing")

        # The recovery stood down: nothing tracked, nothing recorded.
        assert "cd-1" not in mgr._tunnels
        assert reg.get("cd-1").was_connected is False

    @pytest.mark.asyncio
    async def test_a_connect_during_tier1_rebuild_keeps_its_live_tunnel(self, tmp_path):
        """A connect() landing inside tier 1's old.stop() installs a live
        replacement. An ungated rebuild would overwrite that replacement
        without stopping it — an orphaned child holding its port, untracked —
        and the recovery's mismatch handling would then drop the instance
        entirely. The install gate refuses instead: the recovery stands down
        and connect's tunnel, token and record stay exactly as connect left
        them.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        arm = asyncio.Event()
        parked = asyncio.Event()
        release = asyncio.Event()

        class _StopParkTunnel(_ResilTunnel):
            async def stop(self):
                if arm.is_set():
                    arm.clear()
                    parked.set()
                    # flake-ok: pre-existing park of old.stop() on the test's own release event, moved verbatim
                    await release.wait()
                await super().stop()

        reg, mgr = self._mgr(tmp_path, factory=_StopParkTunnel)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")

        # A tunnel dies; tier 1's rebuild parks inside old.stop().
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        arm.set()
        recovery = asyncio.create_task(mgr._recover("cd-1"))
        await await_until(parked, what="the rebuild parking")

        # The user reconnects while the rebuild is parked.
        # flake-ok: pre-existing await of connect while the rebuild is parked without the lock, so it returns, moved verbatim
        await mgr.connect("cd-1")
        fresh_tunnel = mgr._tunnels["cd-1"]
        fresh_token = mgr.get_token("cd-1")
        fresh_epoch = mgr._tunnel_epoch["cd-1"]

        release.set()
        await await_until(recovery, what="the recovery task finishing")

        # connect's live tunnel was not overwritten, orphaned, or dropped.
        assert mgr._tunnels["cd-1"] is fresh_tunnel
        assert fresh_tunnel.status.state == TunnelState.CONNECTED
        assert mgr.get_token("cd-1") == fresh_token
        assert mgr._tunnel_epoch["cd-1"] == fresh_epoch
        assert reg.get("cd-1").was_connected is True

    @pytest.mark.asyncio
    async def test_a_disconnect_during_the_rebuilt_tunnels_start_reaps_it(self, tmp_path):
        """start() is an unlocked await with a window before the child spawns:
        a disconnect landing there stops a tunnel that has no process yet (a
        no-op) and untracks it, so the spawn would land afterwards with the
        rebuild holding the only live handle. The post-start revalidation
        reaps the rebuild's own tunnel and stands the recovery down instead of
        leaving an untracked forwarder holding its port.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        arm = asyncio.Event()
        parked = asyncio.Event()
        release = asyncio.Event()

        class _StartParkTunnel(_ResilTunnel):
            async def start(self):
                if arm.is_set():
                    arm.clear()
                    parked.set()
                    # flake-ok: pre-existing park of the rebuilt tunnel's start() on the test's own release event, moved verbatim
                    await release.wait()
                return await super().start()

        reg, mgr = self._mgr(tmp_path, factory=_StartParkTunnel)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")

        # A tunnel dies; tier 1's rebuild installs its replacement and parks
        # inside that replacement's start().
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        arm.set()
        recovery = asyncio.create_task(mgr._recover("cd-1"))
        await await_until(parked, what="the rebuild parking")
        rebuilt = mgr._tunnels["cd-1"]

        # The user turns the instance off while the start is parked.
        # flake-ok: pre-existing await of disconnect while start() is parked; teardown never drains a self-heal, moved verbatim
        assert await mgr.disconnect("cd-1") is True

        release.set()
        await await_until(recovery, what="the recovery task finishing")

        # The rebuild reaped its own tunnel: nothing tracked, nothing running,
        # and the disconnect's record stands.
        assert "cd-1" not in mgr._tunnels
        assert rebuilt.status.state == TunnelState.STOPPED
        assert reg.get("cd-1").was_connected is False

    @pytest.mark.asyncio
    async def test_shutdown_ends_the_generation_so_a_surviving_recovery_reinstalls_nothing(
        self, tmp_path
    ):
        """shutdown() cancels in-flight recoveries, but a cancellation landing
        inside a tunnel stop can be swallowed there. Such a survivor must find
        the generation stamp moved — shutdown bumps every tracked instance's
        epoch before its stop awaits — so its gated install refuses and no
        child is spawned after cleanup.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        arm = asyncio.Event()
        parked = asyncio.Event()
        release = asyncio.Event()

        class _StopParkTunnel(_ResilTunnel):
            async def stop(self):
                if arm.is_set():
                    arm.clear()
                    parked.set()
                    # A swallowed cancellation: absorb it and keep going, the
                    # way _SshTunnel.stop() suppresses CancelledError around
                    # its child-task awaits.
                    while True:
                        try:
                            # flake-ok: pre-existing cancel-absorbing park on the test's own release event, moved verbatim
                            await release.wait()
                            break
                        except asyncio.CancelledError:
                            continue
                await super().stop()

        reg, mgr = self._mgr(tmp_path, factory=_StopParkTunnel)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")

        # A tunnel dies; tier 1's rebuild parks inside old.stop(), tracked the
        # way _on_tunnel_exit tracks it so shutdown's cancel reaches it.
        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        arm.set()
        recovery = asyncio.create_task(mgr._recover("cd-1"))
        mgr._track_recovery("cd-1", recovery)
        await await_until(parked, what="the rebuild parking")

        # flake-ok: pre-existing await of shutdown, which cancels the parked recovery without awaiting it; moved verbatim
        await mgr.shutdown()
        release.set()
        # The recovery survived its cancellation (swallowed in stop()) but its
        # expected epoch is stale: the gated install refuses.
        await await_until(
            asyncio.gather(recovery, return_exceptions=True), what="the recovery settling"
        )

        assert "cd-1" not in mgr._tunnels

    @pytest.mark.asyncio
    async def test_a_recovery_surviving_a_disconnect_stores_and_rebuilds_nothing(self, tmp_path):
        """Teardown deliberately does not drain a parked self-heal (see
        _teardown_locked: the recovery's cancellation can be swallowed inside
        _SshTunnel.stop(), so awaiting it under the lock could deadlock). This
        pins the property that decision rests on: a recovery whose mint
        returns after the disconnect stores no token and reinstalls no tunnel.
        """
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr, arm, started, release, fail_next = self._tier2_rig(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")

        mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
        fail_next["n"] = 1
        arm.set()
        recovery = asyncio.create_task(mgr._recover("cd-1"))
        mgr._track_recovery("cd-1", recovery)  # as _on_tunnel_exit would
        await await_until(started, what="the rebuild starting")

        assert (
            await await_until(
                asyncio.ensure_future(mgr.disconnect("cd-1")),
                what="the disconnect finishing",
            )
            is True
        )
        release.set()
        await await_until(recovery, what="the recovery task finishing")

        assert "cd-1" not in mgr._tokens
        assert "cd-1" not in mgr._tunnels
        assert "cd-1" not in mgr._refresh_tasks

    @pytest.mark.asyncio
    async def test_a_refresh_refuses_to_start_while_the_instance_is_being_edited(self, tmp_path):
        """The barrier is up precisely because the coordinates are about to move, so
        a mint started now could only produce a token for the machine the user is
        leaving. Refusing is what lets the client retry after the edit instead of
        being handed a credential the new remote never issued.
        """
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        await mgr.connect("cd-1")
        mgr._reconfiguring.add("cd-1")
        try:
            assert await mgr._refresh_token_once("cd-1") is False
            assert await mgr.refresh_token("cd-1") is None
        finally:
            mgr._reconfiguring.discard("cd-1")
        assert await mgr._refresh_token_once("cd-1") is True

    @pytest.mark.asyncio
    async def test_refresh_passes_instance_remote_port(self, tmp_path):
        # F1 regression: connect AND proactive re-mint must target the instance's
        # actual remote_port (not the default 7777), or a non-default-port
        # instance gets an invalid re-minted token.
        seen: list = []

        async def capturing_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            seen.append(remote_port)
            return "TOK"

        reg, mgr = self._mgr(tmp_path, mint=capturing_mint)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1", remote_port=9001)
        await mgr.connect("cd-1")
        assert seen == [9001]  # initial mint targets the right port
        assert await mgr._refresh_token_once("cd-1") is True
        assert seen[-1] == 9001  # proactive refresh re-mints with the same port

    def test_restart_remote(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as stm

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        reg.add(name="Bad", ssh_host="-obadhost", instance_id="bad")
        calls = {}

        async def fake_run(
            host,
            sub,
            *,
            remote_bin="",
            marker_port=None,
            timeout_secs=60.0,
            connect_timeout_secs=10.0,
            stdout_tail=False,
        ):
            calls["a"] = (host, sub, marker_port, connect_timeout_secs)
            calls["stdout_tail"] = stdout_tail
            return (0, "")

        monkeypatch.setattr(stm, "run_remote_kirocrew", fake_run)
        r = asyncio.run(mgr.restart_remote("cd-1"))
        # remote_port defaults to 5476 → threaded so restart uses the marker resolver.
        # connect_timeout_secs comes from the configured mint budget (unset here,
        # so the ssh default from constants.DEFAULT_MINT_TIMEOUT_SECS), not the
        # 10s ssh-exec fail-fast fallback -- a restart
        # on a slow-proxy host must reuse the same budget the mint itself gets.
        assert r["ok"] and calls["a"] == ("cd-1-alias", "restart", 5476, 30.0)
        assert calls["stdout_tail"] is True
        # validation failure
        r = asyncio.run(mgr.restart_remote("bad"))
        assert not r["ok"] and "invalid ssh settings" in r["message"]
        # unknown
        r = asyncio.run(mgr.restart_remote("ghost"))
        assert not r["ok"]

    def test_diagnose_caps_connect_timeout_at_the_diagnostics_ceiling(self, tmp_path, monkeypatch):
        """A user who raised instances.connect_timeout_secs for a
        genuinely slow proxy still wants a diagnosis to resolve in well
        under a minute, not silently inherit the full tunable -- diagnose()
        must cap what it forwards, not pass the configured value straight
        through."""
        from kiro_crew.instances import ssh_tunnel_manager as stm

        reg, mgr = self._mgr(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        mgr._connect_timeout = 90.0  # well above the diagnostics cap
        captured = {}

        async def fake_diagnose(ssh_host, remote_port, local_port, connect_timeout_secs=10.0):
            captured["connect_timeout_secs"] = connect_timeout_secs
            from kiro_crew.instances.diagnostics import OK, DiagnosisResult

            return DiagnosisResult(OK, "ok", [])

        monkeypatch.setattr(stm, "diagnose_instance", fake_diagnose)
        result = asyncio.run(mgr.diagnose("cd-1"))
        assert result is not None
        assert captured["connect_timeout_secs"] == stm._DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS
        assert captured["connect_timeout_secs"] < 90.0

    def test_probe_loop_tears_down_after_threshold(self, tmp_path, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as stm
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        monkeypatch.setattr(stm, "_PROBE_INTERVAL", 0.01)

        class FakeProc:
            def __init__(self):
                self._rc = None

            @property
            def returncode(self):
                return self._rc

            def terminate(self):
                self._rc = -15

            def kill(self):
                self._rc = -9

            async def wait(self):
                return self._rc if self._rc is not None else 0

            stderr = None

        async def main():
            t = _SshTunnel("cd-1", "h", 7778, 7777, probe_failure_threshold=2)
            t._proc = FakeProc()
            t.status.state = TunnelState.CONNECTED

            async def _unreachable():
                return False

            t._forward_alive = _unreachable
            await await_until(asyncio.ensure_future(t._probe_loop()), what="the probe loop exiting")
            # flake-ok: pre-existing 50 ms sleep after the probe loop already exited, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep shape, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.05)
            assert t._probe_failed is True
            assert "health probe failed" in t._exit_error(-15)

        asyncio.run(main())

    def test_sshtunnel_start_success_then_stop(self, monkeypatch):
        from kiro_crew.instances import ssh_tunnel_manager as stm
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

        monkeypatch.setattr(stm, "_PROBE_INTERVAL", 0)  # no probe-loop task

        class FakeProc:
            def __init__(self):
                self.returncode = None
                self._exited = asyncio.Event()

            async def wait(self):
                # flake-ok: pre-existing fake process wait parks until terminate or kill sets its exit event, moved verbatim
                await self._exited.wait()
                return self.returncode

            def terminate(self):
                self.returncode = -15
                self._exited.set()

            def kill(self):
                self.returncode = -9
                self._exited.set()

            # A real asyncio Process always exposes both stream attributes, and
            # they are None for a stream that was not piped.
            stdout = None
            stderr = None

        async def fake_exec(*a, **k):
            return FakeProc()

        # flake-ok: pre-existing test-scoped patch of the asyncio spawn function the tunnel calls, moved verbatim
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        async def main():
            t = _SshTunnel("cd-1", "cd-1-alias", 7778, 7777)

            async def _reachable():
                return True

            t._port_reachable = _reachable  # forward comes up immediately
            ok = await t.start()
            assert ok and t.status.state == TunnelState.CONNECTED
            assert t.status.connected_at > 0
            # second start while CONNECTED is a no-op
            assert await t.start() is True
            await t.stop()
            assert t.status.state == TunnelState.STOPPED

        asyncio.run(main())

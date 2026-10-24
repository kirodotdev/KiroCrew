"""SessionManager warm-pool internals and periodic upkeep.

Among them: ``_fill_warm_pool``, ``_claim_from_pool`` and ``_drain_and_claim``, pool PIDs, the
pool-health and cleanup loops (and the cleanup loop surviving an ``_expire_idle`` crash), the
non-blocking ``start_pool``, ``set_active_dashboard_slots``, ``_schedule_replenish``, and
``_expire_idle`` reaping timed-out sessions and orphaned dashboard slots.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from kiro_crew.session import BACKGROUND_KEY, SessionManager
from kiro_crew.testing.wait import await_until


class TestOrphanedDashboardSessions:
    """Tests for orphaned dashboard session detection in _expire_idle."""

    @pytest.mark.asyncio
    async def test_expire_idle_reaps_orphaned_dashboard_session(self, cfg):
        """Dashboard session whose slot does not exist is reaped immediately."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")
        mgr.release("dashboard:tab1")
        # Mark tab2 as the only active slot — tab1 is orphaned
        mgr.set_active_dashboard_slots({"dashboard:tab2"})
        await mgr._expire_idle(9999)  # high timeout so idle doesn't trigger

        assert "dashboard:tab1" not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_expire_idle_skips_uninitialized_slots(self, cfg):
        """When _active_dashboard_slots is None, no orphan reaping occurs."""
        import time

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")
        mgr.release("dashboard:tab1")
        # Don't call set_active_dashboard_slots — stays None
        mgr._sessions["dashboard:tab1"].last_used = time.monotonic()
        await mgr._expire_idle(9999)

        assert "dashboard:tab1" in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_expire_idle_preserves_active_dashboard_session(self, cfg):
        """Dashboard session whose slot still exists is NOT reaped."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")
        mgr.release("dashboard:tab1")
        mgr.set_active_dashboard_slots({"dashboard:tab1"})
        await mgr._expire_idle(9999)

        assert "dashboard:tab1" in mgr._sessions
        await mgr.close_all()


class TestWarmPoolInternals:
    """Tests for _fill_warm_pool, _claim_from_pool, _drain_and_claim."""

    @pytest.mark.asyncio
    async def test_fill_warm_pool_spawns_to_size(self, cfg):
        cfg.session.pool_size = 2
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        await mgr._fill_warm_pool()
        assert mgr._warm_pool.qsize() == 2
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_fill_warm_pool_no_factory(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._provider_factory = None
        await mgr._fill_warm_pool()  # should not raise
        assert mgr._warm_pool.qsize() == 0

    @pytest.mark.asyncio
    async def test_fill_warm_pool_zero_size(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 0
        await mgr._fill_warm_pool()
        assert mgr._warm_pool.qsize() == 0

    @pytest.mark.asyncio
    async def test_fill_warm_pool_stops_on_failure(self, cfg):
        call_count = 0

        def failing_factory(session_key=None, agent=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise RuntimeError("spawn failed")
            m = AsyncMock()
            m.start = AsyncMock()
            return m

        mgr = SessionManager(cfg, provider_factory=failing_factory)
        mgr._pool_size = 3
        await mgr._fill_warm_pool()
        # Only 1 succeeded before failure stopped the loop
        assert mgr._warm_pool.qsize() == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_claim_from_pool_matching_agent(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = "kirocrew"
        mock_p = AsyncMock()
        mgr._warm_pool.put_nowait((mock_p, 100.0))
        result = mgr._claim_from_pool("kirocrew")
        assert result == (mock_p, 100.0)

    @pytest.mark.asyncio
    async def test_claim_from_pool_mismatched_agent(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = "kirocrew"
        mock_p = AsyncMock()
        mgr._warm_pool.put_nowait((mock_p, 100.0))
        result = mgr._claim_from_pool("different-agent")
        assert result is None

    @pytest.mark.asyncio
    async def test_claim_from_pool_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        result = mgr._claim_from_pool(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_drain_and_claim_healthy(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = ""
        mock_p = AsyncMock()
        mock_p.is_process_alive = lambda: True
        mgr._warm_pool.put_nowait((mock_p, time.monotonic()))
        result = await mgr._drain_and_claim(None)
        assert result is mock_p
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_drain_and_claim_dead_provider_discarded(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = ""
        dead_p = AsyncMock()
        dead_p.is_process_alive = lambda: False
        dead_p.exit_code = 1
        mgr._warm_pool.put_nowait((dead_p, time.monotonic()))
        result = await mgr._drain_and_claim(None)
        assert result is None
        dead_p.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_drain_and_claim_stale_ttl_discarded(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = ""
        mgr._pool_ttl_secs = 60
        stale_p = AsyncMock()
        stale_p.is_process_alive = lambda: True

        # Spawned 120s ago — exceeds 60s TTL
        mgr._warm_pool.put_nowait((stale_p, time.monotonic() - 120))
        result = await mgr._drain_and_claim(None)
        assert result is None
        stale_p.shutdown.assert_awaited_once()
        await mgr.close_all()


class TestPoolHealthLoop:
    """Tests for _pool_health_loop periodic sweep."""

    @pytest.mark.asyncio
    async def test_health_loop_removes_dead_providers(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        mgr._pool_ttl_secs = 0  # no TTL

        dead_p = AsyncMock()
        dead_p.is_process_alive = lambda: False
        dead_p.exit_code = 1
        dead_p.client = AsyncMock()
        dead_p.client._pid = 111

        mgr._warm_pool.put_nowait((dead_p, time.monotonic()))

        # Run one iteration then cancel
        call_count = 0
        original_sleep = asyncio.sleep

        async def one_pass_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            await original_sleep(0)

        # flake-ok: asyncio.sleep patched in this with-block so the health loop runs one pass and its second sleep raises; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=one_pass_sleep):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 0
        dead_p.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_health_loop_keeps_healthy_providers(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        mgr._pool_ttl_secs = 0

        healthy_p = AsyncMock()
        healthy_p.is_process_alive = lambda: True
        healthy_p.client = AsyncMock()
        healthy_p.client._pid = 222

        mgr._warm_pool.put_nowait((healthy_p, time.monotonic()))

        call_count = 0

        async def one_pass_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            # instant return for first sleep
            return

        # flake-ok: asyncio.sleep patched in this with-block so the health loop runs one pass and its second sleep raises; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=one_pass_sleep):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 1
        healthy_p.shutdown.assert_not_awaited()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_health_loop_ttl_expiry(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        mgr._pool_ttl_secs = 60

        stale_p = AsyncMock()
        stale_p.is_process_alive = lambda: True
        stale_p.client = AsyncMock()
        stale_p.client._pid = 333

        mgr._warm_pool.put_nowait((stale_p, time.monotonic() - 120))

        call_count = 0

        async def one_pass_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            return

        # flake-ok: asyncio.sleep patched in this with-block so the health loop runs one pass and its second sleep raises; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=one_pass_sleep):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 0
        stale_p.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_health_loop_empty_pool_skips(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2

        call_count = 0

        async def one_pass_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            return

        # flake-ok: asyncio.sleep patched in this with-block so the health loop runs one pass and its second sleep raises; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=one_pass_sleep):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()
        # No crash, just skipped
        await mgr.close_all()


class TestCleanupLoop:
    """Tests for _cleanup_loop periodic maintenance.

    Every sweep the loop dispatches is stubbed: these tests pin the LOOP's
    wiring (which sweeps run, with what args, and when), not sweep behavior —
    each sweep has its own tests in its own module. Leaving a sweep unstubbed
    (notably ``find_orphan_mcp_candidates``, a full process-table scan, and
    ``cleanup_orphaned_session_roots``, which reads the operator's real
    ``~/.kirocrew`` PID file) made each test take ~10-20s of wall-clock and
    probe live system state — both banned by testing-conventions.md.
    """

    @pytest.mark.asyncio
    async def test_cleanup_loop_calls_expire_idle(self, cfg):
        cfg.session.timeout_secs = 120
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with (
            patch.object(mgr, "_expire_idle", new_callable=AsyncMock) as mock_expire,
            patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0),
            patch("kiro_crew.session.cleanup_stale_sandbox_profiles", return_value=0),
            patch("kiro_crew.session._collect_active_pids", return_value=({}, True)),
            patch("kiro_crew.session._periodic_pid_sweep", return_value=([], [])),
            patch("kiro_crew.session._kill_confirmed_and_writeback", return_value=0),
            patch("kiro_crew.session.cleanup_orphaned_session_roots", return_value=0),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
            patch("kiro_crew.session.shutdown_event") as mock_event,
        ):
            # First wait_for returns TimeoutError (normal wakeup), second signals shutdown
            mock_event.is_set = lambda: mock_expire.await_count >= 1
            mock_event.wait = AsyncMock(side_effect=asyncio.TimeoutError)
            await mgr._cleanup_loop()

        mock_expire.assert_awaited_once_with(120)
        assert mgr._idle_sweep_enabled is True
        assert mgr._idle_timeout == 120
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_disabled_idle_sweep(self, cfg, caplog):
        cfg.session.timeout_secs = 0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with (
            patch.object(mgr, "_expire_idle", new_callable=AsyncMock) as mock_expire,
            patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0),
            patch("kiro_crew.session.cleanup_stale_sandbox_profiles", return_value=0),
            patch("kiro_crew.session._collect_active_pids", return_value=({}, True)),
            patch("kiro_crew.session._periodic_pid_sweep", return_value=([], [])),
            patch("kiro_crew.session._kill_confirmed_and_writeback", return_value=0),
            patch("kiro_crew.session.cleanup_orphaned_session_roots", return_value=0),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
            patch("kiro_crew.session.shutdown_event") as mock_event,
        ):
            call_count = [0]

            async def one_pass(*a, **kw):
                call_count[0] += 1
                raise asyncio.TimeoutError

            mock_event.is_set = lambda: call_count[0] >= 1
            mock_event.wait = AsyncMock(side_effect=one_pass)
            with caplog.at_level(logging.INFO, logger="kiro_crew.session"):
                await mgr._cleanup_loop()

        assert call_count == [1], "the loop still ticked once"
        mock_expire.assert_not_awaited()
        assert mgr._idle_sweep_enabled is False
        assert any(m.startswith("Idle session sweep disabled") for m in caplog.messages)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_clamps_low_timeout(self, cfg, caplog):
        cfg.session.timeout_secs = 30  # below 60 minimum
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with (
            patch.object(mgr, "_expire_idle", new_callable=AsyncMock) as mock_expire,
            patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0),
            patch("kiro_crew.session.cleanup_stale_sandbox_profiles", return_value=0),
            patch("kiro_crew.session._collect_active_pids", return_value=({}, True)),
            patch("kiro_crew.session._periodic_pid_sweep", return_value=([], [])),
            patch("kiro_crew.session._kill_confirmed_and_writeback", return_value=0),
            patch("kiro_crew.session.cleanup_orphaned_session_roots", return_value=0),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
            patch("kiro_crew.session.shutdown_event") as mock_event,
        ):
            mock_event.is_set = lambda: mock_expire.await_count >= 1
            mock_event.wait = AsyncMock(side_effect=asyncio.TimeoutError)
            with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
                await mgr._cleanup_loop()

        mock_expire.assert_awaited_once_with(60)
        assert mgr._idle_timeout == 60
        assert "session.timeout_secs=30 is below minimum 60; clamping to 60" in caplog.messages
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_shutdown_signal(self, cfg):
        cfg.session.timeout_secs = 120
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with patch("kiro_crew.session.shutdown_event") as mock_event:
            mock_event.is_set = lambda: True
            mock_event.wait = AsyncMock(return_value=None)
            await mgr._cleanup_loop()
        mock_event.wait.assert_not_awaited()
        assert mgr._idle_timeout == 120, "the policy is still adopted before the first tick"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_runs_sandbox_sweep_via_executor(self, cfg, caplog):
        """Sandbox sweep is invoked through run_in_executor on maintenance_executor.

        Asserts the offload specifically (the blocking-call fix): the sweep
        must execute on a maintenance-executor worker thread, NOT the event
        loop thread, so its blocking os.listdir/os.kill/os.remove I/O cannot
        freeze the loop.
        """
        cfg.session.timeout_secs = 120
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        sweep_threads: list[str] = []
        sweep_homes: list[object] = []

        def _fake_sweep(*, data_home=None) -> int:
            # The deps hand the sweep the data home the manager resolved on ITS
            # thread (the pool thread must not resolve it itself); record it too.
            sweep_threads.append(threading.current_thread().name)
            sweep_homes.append(data_home)
            return 3

        with (
            patch.object(mgr, "_expire_idle", new_callable=AsyncMock),
            patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0),
            patch(
                "kiro_crew.session.cleanup_stale_sandbox_profiles", side_effect=_fake_sweep
            ) as mock_sweep,
            patch("kiro_crew.session._collect_active_pids", return_value=({}, True)),
            patch("kiro_crew.session._periodic_pid_sweep", return_value=([], [])),
            patch("kiro_crew.session._kill_confirmed_and_writeback", return_value=0),
            patch("kiro_crew.session.cleanup_orphaned_session_roots", return_value=0),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
            patch("kiro_crew.session.shutdown_event") as mock_event,
        ):
            mock_event.is_set = lambda: mock_sweep.call_count >= 1
            mock_event.wait = AsyncMock(side_effect=asyncio.TimeoutError)
            with caplog.at_level(logging.INFO, logger="kiro_crew.session"):
                await mgr._cleanup_loop()

        # Verify: sweep was called (production wiring). At least once, not
        # exactly once: the loop now also dispatches one reclaim pass at START
        # (a host whose runtime tmpfs is out of inodes cannot spawn at all, so
        # that pass must not wait out an interval), so a run can legitimately
        # record the boot pass, the tick pass, or both.
        assert mock_sweep.call_count >= 1
        # Verify the offload: EVERY call ran on a maintenance-executor worker
        # thread, not the event loop thread (run_in_executor path).
        assert sweep_threads, "sweep never executed"
        assert all(name != threading.main_thread().name for name in sweep_threads)
        assert sweep_threads[0].startswith("mc-maint")
        # The home reached the pool thread pre-resolved and pinned: a sweep that
        # resolved config_dir() for itself, after the queuing test's pin was gone,
        # walked the operator's real ~/.kiro/crew (third side-effect audit).
        assert sweep_homes and all(
            h is not None and Path(h).resolve() == Path(os.environ["KIROCREW_HOME"]).resolve()
            for h in sweep_homes
        ), sweep_homes
        assert "removed 3 stale sandbox artifacts" in caplog.text
        await mgr.close_all()


class TestPoolPids:
    """Tests for _pool_pids non-destructive peek."""

    @pytest.mark.asyncio
    async def test_pool_pids_extracts_pids(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_p = AsyncMock()
        mock_p.client = AsyncMock()
        mock_p.client._pid = 42
        mgr._warm_pool.put_nowait((mock_p, time.monotonic()))
        pids = mgr._pool_pids()
        assert 42 in pids
        # Non-destructive — item still in pool
        assert mgr._warm_pool.qsize() == 1

    @pytest.mark.asyncio
    async def test_pool_pids_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr._pool_pids() == set()

    @pytest.mark.asyncio
    async def test_pool_pids_includes_sweep_pids(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_sweep_pids.add(999)
        pids = mgr._pool_pids()
        assert 999 in pids


class TestSetActiveDashboardSlots:
    """Test set_active_dashboard_slots."""

    def test_sets_slots(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_active_dashboard_slots({"dashboard:tab1", "dashboard:tab2"})
        assert mgr._active_dashboard_slots == {"dashboard:tab1", "dashboard:tab2"}


class TestStartPoolNonBlocking:
    """Tests for start_pool non-blocking path."""

    @pytest.mark.asyncio
    async def test_start_pool_non_blocking(self, cfg, monkeypatch):
        cfg.session.pool_size = 1
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 1
        # A backend without a shared runtime, so the boot start is observable.
        monkeypatch.setattr(mgr, "_bg_backend_supports_runtime", lambda: False)
        await mgr.start_pool(blocking=False)
        # Let background tasks run
        # flake-ok: fixed 0.1 s barrier for the non-blocking start task to spawn its session; a loaded host can miss it; moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- fixed wait for the non-blocking start task to spawn its session before the assert; moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.1)
        assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_start_pool_no_factory(self, cfg):
        mgr = SessionManager(cfg, provider_factory=None)
        await mgr.start_pool()  # should be no-op
        assert mgr.count == 0

    @pytest.mark.asyncio
    async def test_ensure_background_already_exists(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()
        await mgr._ensure_background()
        assert mgr.count == 1  # still just the one bg session
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_ensure_background_factory_failure(self, cfg):
        def failing_factory(session_key=None, **kwargs):
            raise RuntimeError("spawn failed")

        mgr = SessionManager(cfg, provider_factory=failing_factory)
        await mgr._ensure_background()
        assert BACKGROUND_KEY not in mgr._sessions


class TestScheduleReplenish:
    """Tests for _schedule_replenish fire-and-forget pool refill."""

    @pytest.mark.asyncio
    async def test_schedule_replenish_creates_task(self, cfg):
        cfg.session.pool_size = 2
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        mgr._schedule_replenish()
        # flake-ok: fixed 0.1 s barrier for the replenish task to fill the pool; a loaded host can miss it; moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- fixed wait for the replenish task to fill the pool before the qsize assert; moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.1)  # let task run
        assert mgr._warm_pool.qsize() >= 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_schedule_replenish_noop_when_pool_disabled(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 0
        mgr._schedule_replenish()  # should not create task
        assert len(mgr._background_tasks) == 0


class TestExpireIdleOrphans:
    """Tests for _expire_idle orphaned dashboard slot detection."""

    @pytest.mark.asyncio
    async def test_orphaned_dashboard_slot_expired(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:slot5")
        mgr.release("dashboard:slot5")
        # Set active slots to NOT include slot5
        mgr.set_active_dashboard_slots({"dashboard:slot0"})
        await mgr._expire_idle(timeout_secs=9999)  # not idle, but orphaned
        assert not mgr.has_session("dashboard:slot5")

    @pytest.mark.asyncio
    async def test_active_slot_not_expired(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:slot0")
        mgr.release("dashboard:slot0")
        mgr.set_active_dashboard_slots({"dashboard:slot0"})
        await mgr._expire_idle(timeout_secs=9999)
        assert mgr.has_session("dashboard:slot0")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_channel_session_never_expired(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("channel:C123")
        mgr.release("channel:C123")
        # Backdate to make it idle
        async with mgr._lock:
            mgr._sessions["channel:C123"].last_used = time.monotonic() - 9999
        await mgr._expire_idle(timeout_secs=1)
        assert mgr.has_session("channel:C123")
        await mgr.close_all()


class TestSessionTimeout:
    @pytest.mark.asyncio
    async def test_session_expires_after_timeout(self, cfg):
        cfg.session.timeout_secs = 1
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p, is_new, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        # Manually backdate last_used
        async with mgr._lock:
            mgr._sessions["thread1"].last_used = time.monotonic() - 10
        await mgr._expire_idle(timeout_secs=1)
        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_active_session_not_expired(self, cfg):
        cfg.session.timeout_secs = 10
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p, is_new, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        await mgr._expire_idle(timeout_secs=10)
        assert mgr.count == 1
        await mgr.close_all()


class TestCleanupLoopResilience:
    """Tests that _cleanup_loop survives _expire_idle exceptions."""

    @pytest.mark.asyncio
    async def test_cleanup_loop_continues_after_expire_idle_crash(self, cfg):
        """If _expire_idle raises, the loop keeps running."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cfg.session.timeout_secs = 360
        call_count = 0

        # Collapse the loop's inter-sweep sleep to ~zero WITHOUT busy-spinning.
        # The loop sleeps via ``asyncio.wait_for(shutdown_event.wait(), timeout=interval)``
        # (interval >= 60s). We shrink only THAT call to a tiny real timeout so
        # the wait actually runs: it returns immediately once shutdown_event is
        # set, and otherwise times out in ~1ms. Raising
        # TimeoutError WITHOUT awaiting the wait() would turn the loop into an
        # unbounded busy-spin — if _expire_idle's shutdown_event.set() landed on
        # a cross-loop-rebound event (after an earlier asyncio test in the same
        # process), the top-of-loop is_set() check could miss it and the test
        # would hang until its own outer deadline. Letting the real wait() run
        # makes the stop deterministic regardless of prior event-loop binding.
        # The ``timeout >= 60`` discriminator clamps ONLY the loop's own inter-sweep
        # wait; the outer guard below is an ``await_until`` on the loop task, which
        # never routes through this patched ``asyncio.wait_for``.
        real_wait_for = asyncio.wait_for

        async def _fast_wait_for(coro, *, timeout):
            if timeout >= 60:
                # flake-ok: 1 ms real timeout for only the loop's inter-sweep wait of 60 s or more; expiry runs the next sweep and await_until bounds the loop; moved verbatim
                return await real_wait_for(coro, timeout=0.001)
            return await real_wait_for(coro, timeout=timeout)

        async def _expire_then_stop(timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated crash")
            # Force loop exit on second call
            from kiro_crew import shutdown_event

            shutdown_event.set()

        import kiro_crew

        kiro_crew.shutdown_event.clear()
        with (
            # flake-ok: asyncio.wait_for patched in this with-block; only 60 s or longer waits shrink to 1 ms; await_until bounds the loop; moved verbatim; tracked for a de-flake pass
            patch("asyncio.wait_for", side_effect=_fast_wait_for),
            patch.object(mgr, "_expire_idle", side_effect=_expire_then_stop),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
        ):
            await await_until(
                asyncio.ensure_future(mgr._cleanup_loop()),
                what="the cleanup loop exiting after shutdown_event is set",
            )

        assert call_count >= 2
        kiro_crew.shutdown_event.clear()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_logs_expire_idle_exception(self, cfg, caplog):
        """Exception in _expire_idle is logged at ERROR level."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cfg.session.timeout_secs = 360

        # Shrink only the loop's inter-sweep sleep to a tiny REAL timeout (not a
        # coro.close()+raise) so the loop observes shutdown_event deterministically
        # instead of busy-spinning — see the note in
        # test_cleanup_loop_continues_after_expire_idle_crash.
        real_wait_for = asyncio.wait_for

        async def _fast_wait_for(coro, *, timeout):
            if timeout >= 60:
                # flake-ok: 1 ms real timeout for only the loop's inter-sweep wait of 60 s or more; expiry runs the next sweep and await_until bounds the loop; moved verbatim
                return await real_wait_for(coro, timeout=0.001)
            return await real_wait_for(coro, timeout=timeout)

        async def _crash_and_stop(timeout):
            from kiro_crew import shutdown_event

            shutdown_event.set()
            raise ValueError("boom")

        import kiro_crew

        kiro_crew.shutdown_event.clear()
        with (
            # flake-ok: asyncio.wait_for patched in this with-block; only 60 s or longer waits shrink to 1 ms; await_until bounds the loop; moved verbatim; tracked for a de-flake pass
            patch("asyncio.wait_for", side_effect=_fast_wait_for),
            patch.object(mgr, "_expire_idle", side_effect=_crash_and_stop),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
        ):
            with caplog.at_level(logging.ERROR):
                await await_until(
                    asyncio.ensure_future(mgr._cleanup_loop()),
                    what="the cleanup loop exiting after shutdown_event is set",
                )

        assert "_expire_idle crashed" in caplog.text
        kiro_crew.shutdown_event.clear()
        await mgr.close_all()

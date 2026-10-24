"""How a provider leaves the warm pool, and how its OS process is made to die.

The health sweep drops dead and TTL-expired providers and schedules a refill
when the pool is short of its target; ``_pool_pids`` reports every pooled
provider's PID, the ones the sweep holds out of the queue included. A discard
escalates from a bounded graceful shutdown to a hard kill when the process
survives, never signals a mock or sentinel PID, never runs the kill inline once
the executor is down, and keeps going when one kill in a batch fails. The
shared fixture and stand-ins live in ``session_pool_helpers.py``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_pool_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolate_config_dir,
    _make_manager,
    _make_provider,
)

from kiro_crew import platform_compat
from kiro_crew.testing.wait import async_wait_until, finished

# ---------------------------------------------------------------------------
# _pool_health_loop
# ---------------------------------------------------------------------------


class TestPoolHealthLoop:
    @pytest.mark.asyncio
    async def test_removes_dead_provider_and_replenishes(self):
        """Dead provider is removed during health sweep, replenish triggered."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        dead = _make_provider()
        dead.is_process_alive.return_value = False
        dead.exit_code = 1
        mgr._warm_pool.put_nowait((dead, time.monotonic()))
        mgr._schedule_replenish = MagicMock()

        # Run one iteration by patching sleep to raise after first call
        call_count = 0

        async def _sleep_once(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=_sleep_once):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.empty()
        dead.shutdown.assert_awaited_once()
        mgr._schedule_replenish.assert_called_once()

    @pytest.mark.asyncio
    async def test_removes_expired_provider(self):
        """TTL-expired provider is removed during health sweep."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=60)
        stale = _make_provider()
        mgr._warm_pool.put_nowait((stale, time.monotonic() - 120))
        mgr._schedule_replenish = MagicMock()

        call_count = 0

        async def _sleep_once(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=_sleep_once):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.empty()
        stale.shutdown.assert_awaited_once()
        mgr._schedule_replenish.assert_called_once()

    @pytest.mark.asyncio
    async def test_keeps_healthy_provider(self):
        """Healthy provider at target survives health sweep with no churn."""
        mgr, _ = _make_manager(pool_size=1, pool_agent="kirocrew")
        healthy = _make_provider()
        mgr._warm_pool.put_nowait((healthy, time.monotonic()))
        mgr._schedule_replenish = MagicMock()

        call_count = 0

        async def _sleep_once(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=_sleep_once):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 1
        healthy.shutdown.assert_not_awaited()
        mgr._schedule_replenish.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_pool_schedules_replenish(self):
        """Empty pool self-heals: sweep schedules a refill instead of skipping."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        mgr._schedule_replenish = MagicMock()

        call_count = 0

        async def _sleep_once(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=_sleep_once):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        mgr._schedule_replenish.assert_called_once()

    @pytest.mark.asyncio
    async def test_under_target_pool_schedules_replenish(self):
        """A short-but-healthy pool is a deficit: sweep schedules a refill."""
        mgr, _ = _make_manager(pool_size=3, pool_agent="kirocrew")
        healthy = _make_provider()
        mgr._warm_pool.put_nowait((healthy, time.monotonic()))
        mgr._schedule_replenish = MagicMock()

        await mgr._sweep_warm_pool_once()

        assert mgr._warm_pool.qsize() == 1
        healthy.shutdown.assert_not_awaited()
        mgr._schedule_replenish.assert_called_once()

    @pytest.mark.asyncio
    async def test_at_target_pool_does_not_replenish(self):
        """A full healthy pool has no deficit: sweep schedules nothing."""
        mgr, _ = _make_manager(pool_size=2, pool_agent="kirocrew")
        for _ in range(2):
            mgr._warm_pool.put_nowait((_make_provider(), time.monotonic()))
        mgr._schedule_replenish = MagicMock()

        await mgr._sweep_warm_pool_once()

        assert mgr._warm_pool.qsize() == 2
        mgr._schedule_replenish.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_pool_sweep_is_noop(self):
        """pool_size=0 keeps the sweep a no-op: no refill for a disabled pool."""
        mgr, _ = _make_manager(pool_size=0)
        mgr._schedule_replenish = MagicMock()
        expired = _make_provider()
        mgr._warm_pool.put_nowait((expired, time.monotonic() - 10_000))

        await mgr._sweep_warm_pool_once()

        mgr._schedule_replenish.assert_not_called()
        assert mgr._warm_pool.qsize() == 1, "a disabled pool's sweep reads nothing"
        expired.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mixed_healthy_and_dead(self):
        """Only dead providers removed; healthy ones re-enqueued in order."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        healthy1 = _make_provider()
        dead = _make_provider()
        dead.is_process_alive.return_value = False
        healthy2 = _make_provider()
        mgr._warm_pool.put_nowait((healthy1, time.monotonic()))
        mgr._warm_pool.put_nowait((dead, time.monotonic()))
        mgr._warm_pool.put_nowait((healthy2, time.monotonic()))
        mgr._schedule_replenish = MagicMock()

        call_count = 0

        async def _sleep_once(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep, scoped to this with-block; moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=_sleep_once):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 2
        dead.shutdown.assert_awaited_once()
        healthy1.shutdown.assert_not_awaited()
        healthy2.shutdown.assert_not_awaited()
        mgr._schedule_replenish.assert_called_once()


# ---------------------------------------------------------------------------
# _pool_pids
# ---------------------------------------------------------------------------


class TestPoolPids:
    def test_returns_pids_from_pool(self):
        """Extracts PIDs from pooled providers."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        p1 = _make_provider()
        p1.client = MagicMock()
        p1.client._pid = 1234
        p2 = _make_provider()
        p2.client = MagicMock()
        p2.client._pid = 5678
        mgr._warm_pool.put_nowait((p1, time.monotonic()))
        mgr._warm_pool.put_nowait((p2, time.monotonic()))

        pids = mgr._pool_pids()

        assert pids == {1234, 5678}
        # Non-destructive: queue still has both entries
        assert mgr._warm_pool.qsize() == 2

    def test_empty_pool_returns_empty_set(self):
        mgr, _ = _make_manager(pool_agent="kirocrew")

        assert mgr._pool_pids() == set()

    def test_skips_provider_without_client(self):
        """Provider with no client attr is skipped, not crashed."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        p = _make_provider()
        del p.client  # no client attribute
        mgr._warm_pool.put_nowait((p, time.monotonic()))

        pids = mgr._pool_pids()

        assert pids == set()
        assert mgr._warm_pool.qsize() == 1

    def test_skips_non_int_pid(self):
        """Provider with non-int PID is skipped."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        p = _make_provider()
        p.client = MagicMock()
        p.client._pid = None
        mgr._warm_pool.put_nowait((p, time.monotonic()))

        pids = mgr._pool_pids()

        assert pids == set()
        assert mgr._warm_pool.qsize() == 1

    def test_includes_sweep_pids_during_health_check(self):
        """PIDs temporarily out of queue during health sweep are still visible."""
        mgr, _ = _make_manager(pool_agent="kirocrew")
        # Simulate health loop having drained providers
        mgr._pool_sweep_pids = {1111, 2222}

        pids = mgr._pool_pids()

        assert {1111, 2222} <= pids


# ---------------------------------------------------------------------------
# Discard reaping — a discarded provider's OS process must actually die
# ---------------------------------------------------------------------------


class TestDiscardReaping:
    """A discard removes the provider from all pool bookkeeping, so the
    discard path is the last chance to signal the process. These tests pin
    the escalation contract: bounded graceful shutdown, hard-kill fallback
    on failure, and post-shutdown liveness verification.
    """

    @staticmethod
    def _expired_entry(mgr, provider):
        mgr._warm_pool.put_nowait((provider, time.monotonic() - 10_000))

    @pytest.mark.asyncio
    async def test_survivor_after_noop_shutdown_is_hard_killed(self):
        """Graceful shutdown returning cleanly is not proof the process died —
        a still-alive process must be hard-killed."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
        survivor = _make_provider()
        survivor.is_process_alive = MagicMock(return_value=True)
        self._expired_entry(mgr, survivor)

        with patch("kiro_crew.session._sync_kill_provider") as mock_kill:
            pooled = await mgr._drain_and_claim("kirocrew")

        assert pooled is None
        survivor.shutdown.assert_awaited_once()
        mock_kill.assert_called_once_with(survivor)

    @pytest.mark.asyncio
    async def test_exited_process_is_not_hard_killed(self):
        """No hard kill when the process actually exited — its PID may already
        be recycled by an unrelated process."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
        clean = _make_provider()
        clean.is_process_alive = MagicMock(return_value=False)
        self._expired_entry(mgr, clean)

        with patch("kiro_crew.session._sync_kill_provider") as mock_kill:
            result = await mgr._drain_and_claim("kirocrew")

        clean.shutdown.assert_awaited_once()
        mock_kill.assert_not_called()
        assert result is None, "an expired provider is discarded, not claimed"
        assert mgr._warm_pool.empty()

    @pytest.mark.asyncio
    async def test_shutdown_failure_falls_back_to_hard_kill(self):
        """A raising shutdown must not be swallowed into a leak."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
        broken = _make_provider()
        broken.is_process_alive = MagicMock(return_value=True)
        broken.shutdown = AsyncMock(side_effect=RuntimeError("protocol close failed"))
        self._expired_entry(mgr, broken)

        with patch("kiro_crew.session._sync_kill_provider") as mock_kill:
            pooled = await mgr._drain_and_claim("kirocrew")

        assert pooled is None
        mock_kill.assert_called_once_with(broken)

    @pytest.mark.asyncio
    async def test_wedged_shutdown_is_bounded_and_hard_killed(self, monkeypatch):
        """A shutdown that never returns must not stall the discard path
        (the health sweep is a single task — a wedge would disable TTL
        enforcement for the whole pool)."""
        from kiro_crew.session import SessionManager

        monkeypatch.setattr(SessionManager, "_POOL_DISCARD_TIMEOUT", 0.05)
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
        wedged = _make_provider()
        wedged.is_process_alive = MagicMock(return_value=True)

        async def _never_returns() -> None:
            # flake-ok: pre-existing never-returning shutdown stand-in, moved verbatim; the discard path's own timeout ends it
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the stub shutdown that never returns, moved verbatim; the test asserts the discard timeout bounds it
            await asyncio.sleep(3600)

        wedged.shutdown = _never_returns
        self._expired_entry(mgr, wedged)

        # Give the hard-kill offload a PRIVATE executor. Production dispatches it
        # to `subprocess_executor()`, a process-wide 8-worker singleton shared by
        # every test in this xdist worker, and a started run_in_executor future
        # cannot be cancelled — so a sibling test holding those threads (a wedged
        # PTY close, a real `taskkill` on Windows) makes this offload queue behind
        # them. That queue wait is unbounded and is NOT covered by
        # `_POOL_DISCARD_TIMEOUT`, so it could consume the bounded wait below and
        # fail naming the wedged shutdown — the one thing this
        # test had already bounded, to 0.05s. A dedicated executor keeps the
        # assertion about escalation ordering instead of about the shared pool's
        # spare capacity.
        with (
            ThreadPoolExecutor(max_workers=1) as private_executor,
            patch("kiro_crew.session._sync_kill_provider") as mock_kill,
            patch("kiro_crew.session.subprocess_executor", return_value=private_executor),
        ):
            pooled = await finished(mgr._drain_and_claim("kirocrew"), "the drain and claim")

        assert pooled is None
        mock_kill.assert_called_once_with(wedged)

    @pytest.mark.asyncio
    async def test_health_sweep_hard_kills_expired_survivor(self):
        """The periodic sweep applies the same escalation as the claim path."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
        survivor = _make_provider()
        survivor.is_process_alive = MagicMock(return_value=True)
        self._expired_entry(mgr, survivor)

        with patch("kiro_crew.session._sync_kill_provider") as mock_kill:
            await mgr._sweep_warm_pool_once()

        survivor.shutdown.assert_awaited_once()
        mock_kill.assert_called_once_with(survivor)
        assert mgr._warm_pool.qsize() == 0

    @pytest.mark.asyncio
    async def test_sweep_ttl_discard_logs_info_dead_provider_stays_warning(self, caplog):
        """A scheduled TTL recycle of a healthy provider is the pool
        working as designed, so its discard line is INFO. Both anomalies keep
        WARNING: a provider that died before aging out (TTL line, dead process)
        and the dead-provider branch below. All three are still reaped."""
        import logging

        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=60)
        stale = _make_provider()
        stale.is_process_alive = MagicMock(return_value=True)
        stale_dead = _make_provider()
        stale_dead.is_process_alive = MagicMock(return_value=False)
        dead = _make_provider()
        dead.is_process_alive = MagicMock(return_value=False)
        dead.exit_code = 9
        mgr._warm_pool.put_nowait((stale, time.monotonic() - 120))
        mgr._warm_pool.put_nowait((stale_dead, time.monotonic() - 120))
        mgr._warm_pool.put_nowait((dead, time.monotonic()))

        with patch("kiro_crew.session._sync_kill_provider"):
            with caplog.at_level(logging.INFO, logger="kiro_crew.session"):
                await mgr._sweep_warm_pool_once()

        ttl_records = [
            r for r in caplog.records if str(r.msg).startswith("Pool health: %.0fs old provider")
        ]
        dead_records = [
            r for r in caplog.records if str(r.msg).startswith("Pool health: dead provider")
        ]
        # FIFO drain order: healthy-stale first (INFO), dead-stale second (WARNING).
        assert [r.levelname for r in ttl_records] == ["INFO", "WARNING"]
        assert [r.levelname for r in dead_records] == ["WARNING"]
        # Severity-only: all three entries were still discarded and shut down.
        stale.shutdown.assert_awaited_once()
        stale_dead.shutdown.assert_awaited_once()
        dead.shutdown.assert_awaited_once()
        assert mgr._warm_pool.qsize() == 0

    @pytest.mark.asyncio
    async def test_hard_kill_never_signals_mock_or_sentinel_pids(self):
        """A provider stand-in whose pid resolves to a non-int (Mock coerces
        to 1 via __index__) or to pid<=1 must never be signaled — an unguarded
        kill would SIGTERM init / the CI container entrypoint."""
        from kiro_crew.session_pid import _sync_kill_provider

        mock_provider = _make_provider()  # _client._pid auto-resolves to a Mock
        pid_one = _make_provider()
        pid_one._client = SimpleNamespace(_pid=1)

        with patch("kiro_crew.session_pid.platform_compat.kill_pid") as mock_kill:
            _sync_kill_provider(mock_provider)
            _sync_kill_provider(pid_one)

        mock_kill.assert_not_called()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX signal semantics")
    @pytest.mark.asyncio
    async def test_survivor_reaped_even_when_provider_bookkeeping_says_dead(self):
        """The ACP provider's is_process_alive() self-reports dead once its
        kill path has run — even when signal delivery silently failed and the
        OS process survived. Verification must probe the OS via the tracked
        PID, not trust provider bookkeeping."""
        proc = subprocess.Popen(["sleep", "300"])
        try:
            provider = _make_provider()
            # Model a real ACP provider: the tracked PID lives at
            # provider._client._pid AND the client records the pid's start
            # identity, exactly as AcpClient does after start
            # (client_start.py: self._start_time = get_process_start_id(self._pid)).
            # _sync_kill_provider verifies that recorded id against the live one
            # before signalling the root; a stand-in without it is refused as
            # unverifiable and the survivor would leak, which is not the
            # production shape this test exists to exercise.
            provider._client = SimpleNamespace(
                _pid=proc.pid, _start_time=platform_compat.get_process_start_id(proc.pid)
            )
            # Bookkeeping lies: claims dead while the OS process is alive
            provider.is_process_alive = MagicMock(return_value=False)
            provider.shutdown = AsyncMock()  # "ran" but killed nothing

            mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
            self._expired_entry(mgr, provider)

            await mgr._drain_and_claim("kirocrew")

            await async_wait_until(
                lambda: proc.poll() is not None,
                describe=lambda: "survivor leaked behind lying bookkeeping",
            )
        finally:
            if proc.poll() is None:
                proc.kill()
            # flake-ok: reaps the child after the kill just above, moved verbatim; tracked for a de-flake pass
            proc.wait()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX signal semantics")
    @pytest.mark.asyncio
    async def test_discard_kills_real_process_when_graceful_shutdown_is_noop(self):
        """End-to-end: the OS process behind a discarded provider is gone even
        when the graceful shutdown does nothing (child ignores the protocol
        close). Exercises the real hard-kill fallback, not a mock."""
        proc = subprocess.Popen(["sleep", "300"])
        try:
            provider = _make_provider()
            # Mimic an ACP provider: the tracked PID lives at
            # provider._client._pid, and the client records that pid's start
            # identity the way AcpClient does after start
            # (client_start.py: self._start_time = get_process_start_id(self._pid)).
            # _sync_kill_provider re-verifies it before signalling the root.
            provider._client = SimpleNamespace(
                _pid=proc.pid, _start_time=platform_compat.get_process_start_id(proc.pid)
            )
            provider.is_process_alive = MagicMock(side_effect=lambda: proc.poll() is None)
            provider.shutdown = AsyncMock()  # graceful close that kills nothing

            mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
            self._expired_entry(mgr, provider)

            pooled = await mgr._drain_and_claim("kirocrew")
            assert pooled is None

            await async_wait_until(
                lambda: proc.poll() is not None,
                describe=lambda: "discarded provider process leaked",
            )
        finally:
            if proc.poll() is None:
                proc.kill()
            # flake-ok: reaps the child after the kill just above, moved verbatim; tracked for a de-flake pass
            proc.wait()

    @pytest.mark.asyncio
    async def test_dispatch_hard_kill_never_runs_inline_when_executor_down(self):
        """When the subprocess executor is already shut down (gateway
        teardown), the fallback must carry the kill on a dedicated thread —
        never inline on the event loop, where ``_sync_kill_provider`` blocks
        (``os.waitpid`` / ``taskkill``) and can trip the loop watchdog."""
        from kiro_crew.session import SessionManager

        dead_executor = ThreadPoolExecutor(max_workers=1)
        dead_executor.shutdown(wait=True)

        called_on: list[threading.Thread] = []
        done = threading.Event()

        def _record_thread(_provider) -> None:
            called_on.append(threading.current_thread())
            done.set()

        provider = _make_provider()
        with (
            patch("kiro_crew.session.subprocess_executor", return_value=dead_executor),
            patch("kiro_crew.session._sync_kill_provider", side_effect=_record_thread),
        ):
            SessionManager._dispatch_hard_kill(provider)

        assert done.wait(timeout=5), "fallback kill was never dispatched"
        assert (
            called_on[0] is not threading.main_thread()
        ), "fallback kill ran inline on the event-loop thread"

    @pytest.mark.asyncio
    async def test_one_failing_hard_kill_does_not_abort_batch_discard(self):
        """The health sweep discards several providers in one pass — a
        hard-kill failure for one provider must not escape and skip the
        discard of the remaining providers (that would re-leak them)."""
        mgr, _ = _make_manager(pool_agent="kirocrew", pool_ttl_secs=1)
        first = _make_provider()
        first.is_process_alive = MagicMock(return_value=True)
        second = _make_provider()
        second.is_process_alive = MagicMock(return_value=True)
        self._expired_entry(mgr, first)
        self._expired_entry(mgr, second)

        attempted: list[object] = []

        def _kill(provider) -> None:
            attempted.append(provider)
            if provider is first:
                raise RuntimeError("kill blew up")

        with patch("kiro_crew.session._sync_kill_provider", side_effect=_kill):
            await mgr._sweep_warm_pool_once()

        assert (
            first in attempted and second in attempted
        ), "a failing hard kill aborted the batch and leaked later providers"
        assert mgr._warm_pool.qsize() == 0

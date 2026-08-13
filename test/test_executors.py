"""Tests for the bounded maintenance/cron thread pools.

The key invariant: cron execution and the periodic orphan-reaping sweeps use
*separate* pools, so a burst of long-running concurrent cron jobs can never
occupy all the maintenance threads and starve the sweeps that reap leaked
kiro-cli/MCP children (the documented root cause of the loop wedge).
"""

from __future__ import annotations

import kiro_crew.executors as ex


def teardown_function() -> None:
    # Don't leak pools between tests (the module memoizes them process-wide).
    ex.shutdown_maintenance_executor()


def test_maintenance_and_cron_are_distinct_pools() -> None:
    maint = ex.maintenance_executor()
    cron = ex.cron_executor()
    assert maint is not cron, "cron must not share the maintenance pool"


def test_subprocess_pool_is_distinct_from_maintenance_and_cron() -> None:
    # The whole point of the subprocess pool: a teardown call that blocks on a
    # wedged kernel resource (PTY os.close, hung ps/pgrep) must NOT share workers
    # with the orphan-reaping sweep that is the recovery action for the wedge.
    subproc = ex.subprocess_executor()
    assert subproc is not ex.maintenance_executor()
    assert subproc is not ex.cron_executor()


def test_pools_are_memoized() -> None:
    assert ex.maintenance_executor() is ex.maintenance_executor()
    assert ex.subprocess_executor() is ex.subprocess_executor()
    assert ex.cron_executor() is ex.cron_executor()


def test_thread_name_prefixes_distinguish_pools() -> None:
    # Watchdog stack dumps identify the offending pool by thread name.
    assert ex.maintenance_executor()._thread_name_prefix == "mc-maint"
    assert ex.subprocess_executor()._thread_name_prefix == "mc-subproc"
    assert ex.cron_executor()._thread_name_prefix == "mc-cron"


def test_pools_are_bounded() -> None:
    # Bounded so a flood of concurrent work queues rather than spawning
    # unbounded threads.
    assert ex.cron_executor()._max_workers == ex._MAX_CRON_WORKERS
    assert ex.maintenance_executor()._max_workers == ex._MAX_MAINT_WORKERS
    assert ex.subprocess_executor()._max_workers == ex._MAX_SUBPROCESS_WORKERS


def test_shutdown_is_idempotent_and_resets() -> None:
    first = ex.maintenance_executor()
    first_subproc = ex.subprocess_executor()
    first_cron = ex.cron_executor()
    ex.shutdown_maintenance_executor()
    ex.shutdown_maintenance_executor()  # second call must not raise
    # After shutdown a fresh pool is created on next use.
    assert ex.maintenance_executor() is not first
    assert ex.subprocess_executor() is not first_subproc
    assert ex.cron_executor() is not first_cron


def test_pools_execute_work() -> None:
    assert ex.maintenance_executor().submit(lambda: 1 + 1).result(timeout=5) == 2
    assert ex.subprocess_executor().submit(lambda: 4 + 4).result(timeout=5) == 8
    assert ex.cron_executor().submit(lambda: 2 + 3).result(timeout=5) == 5


def test_governance_pool_is_isolated_bounded_and_reset() -> None:
    # GPT round-7 pass 3: the governance pool (externally-paced inbound channels
    # gate + dashboard governance GETs) must be a DISTINCT, bounded, shutdown-
    # resettable pool so a remote message burst can't occupy the maintenance
    # workers the orphan sweeps need.
    gov = ex.governance_executor()
    # Distinct from every other pool.
    assert gov is not ex.maintenance_executor()
    assert gov is not ex.subprocess_executor()
    assert gov is not ex.cron_executor()
    assert gov is not ex.discovery_executor()
    assert gov is not ex.embed_executor()
    # Memoized, named, bounded.
    assert ex.governance_executor() is gov
    assert gov._thread_name_prefix == "mc-gov"
    assert gov._max_workers == ex._MAX_GOVERNANCE_WORKERS
    # Executes work.
    assert gov.submit(lambda: 7 * 6).result(timeout=5) == 42
    # Shutdown resets it (fresh pool next use).
    ex.shutdown_maintenance_executor()
    assert ex.governance_executor() is not gov

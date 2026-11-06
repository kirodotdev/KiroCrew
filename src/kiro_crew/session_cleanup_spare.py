"""Warm-pool spare half of the RSS ceiling tick.

The 5-minute RSS tick in :mod:`kiro_crew.session_cleanup` measures each session
runtime's tree and recycles one over ``session.watchdog_rss_max_mb``. A warm-pool
spare is a runtime no session owns, so it is absent from that pass's candidate
set; this module measures it against the same ceiling and, on a hit, takes it out
of the pool and tears it down through the pool's own discard path.

The functions take the :class:`~kiro_crew.session_cleanup.SessionCleanup` instance
as ``cleanup`` and read ``cleanup._owner``, ``cleanup._deps`` and ``cleanup.state``.
Every runtime pid is read inside the pool that owns the spare
(``_spare_pid_providers`` and ``_remove_pooled_providers_by_pid`` return
``(pid, provider)`` pairs), so this module reads none of its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.session_cleanup import SessionCleanup


async def build_rss_measure(
    cleanup: SessionCleanup, loop: asyncio.AbstractEventLoop
) -> Callable[[int], int]:
    """One per-tick tree-RSS reader for ``_rss_threshold_check``.

    ``_rss_threshold_check`` builds it once and passes it both to its own
    session-candidate loop and to :func:`recycle_over_ceiling_spares`, so a tick
    with sessions and spares reads one tree snapshot, not two.

    On Linux a single immutable ``/proc`` snapshot is built once and closed
    over, because rebuilding it per process is expensive; on Windows a raw
    parent map is unsafe (stale PPIDs + recycled PIDs would attach an
    unrelated subtree), so each tree is read through the lineage-validating
    route instead.
    """
    measure: Callable[[int], int]
    if cleanup._deps.is_windows():

        def measure(pid: int) -> int:
            return cleanup._deps.get_session_rss_mb(pid)

    else:
        child_map = await loop.run_in_executor(
            cleanup._deps.get_maintenance_executor(),
            cleanup._deps.build_child_map,
        )

        def measure(pid: int) -> int:
            return cleanup._deps.rss_mb_from_tree(pid, child_map)

    return measure


def spare_providers_over_ceiling_eligible(cleanup: SessionCleanup) -> list[tuple[int, Any]]:
    """The warm-pool spares to measure this tick as ``(pid, provider)``.

    Empty whenever the ceiling is off, so a disabled ceiling measures nothing
    here either -- the spare pass mirrors the session pass's own ``rss_max_mb``
    guard. The pid is read inside the pool, which owns the spare's runtime; this
    pass never reads a runtime pid of its own. The snapshot does not consume the
    pool: a spare it lists but does not recycle stays claimable.
    """
    if not cleanup.state.rss_max_mb:
        return []
    try:
        return list(cleanup._owner._spare_pid_providers())
    except Exception:
        # Reading the pool must never take the whole RSS tick down; the session
        # pass has already run.
        cleanup._deps.logger.debug("warm-pool snapshot for RSS tick failed", exc_info=True)
        return []


async def recycle_over_ceiling_spares(
    cleanup: SessionCleanup,
    spares: list[tuple[int, Any]],
    measure: Callable[[int], int],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Measure each spare runtime's tree and discard any over the ceiling.

    *spares* arrives as ``(pid, provider)`` pairs the pool read, so this pass
    reads no runtime pid of its own. The tree is measured once per distinct pid,
    cached only within the tick. A pid over the ceiling is then taken OUT of the
    pool before it is torn down: the removal is synchronous with its own
    membership check, so a spare a session claimed during the measurement is
    simply not in the queue at removal time and is left alone -- the session
    pass measures it next tick as a session runtime. A removed provider is
    discarded through the pool's own path, so a hit does exactly what the health
    sweep already does; the log line matches the session recycle's ``tree
    rss=%dMB exceeds %dMB``. A spare belongs to no session, so there is no
    sub-agent, injection or background-launch guard to consult -- nobody's work
    is on it.
    """
    deps = cleanup._deps
    rss_by_pid: dict[int, int] = {}
    over_ceiling: set[int] = set()
    for pid, _provider in spares:
        if pid in rss_by_pid:
            continue
        rss = await loop.run_in_executor(deps.get_maintenance_executor(), measure, pid)
        rss_by_pid[pid] = rss
        if rss > cleanup.state.rss_max_mb:
            over_ceiling.add(pid)
    if not over_ceiling:
        return

    # Take the over-ceiling spares out of the pool and tear them down. The
    # removal is synchronous and returns only the entries still queued, so a
    # spare claimed since it was measured is never found here and never killed
    # under its new session.
    removed = cleanup._owner._remove_pooled_providers_by_pid(over_ceiling)
    for pid, provider in removed:
        deps.logger.warning(
            "RSS recycle: spare runtime pid=%d tree rss=%dMB exceeds %dMB",
            pid,
            rss_by_pid.get(pid, 0),
            cleanup.state.rss_max_mb,
        )
        try:
            await cleanup._owner._discard_pool_provider(
                provider, "RSS recycle (warm-pool spare over the memory ceiling)"
            )
            deps.stats_factory().inc_session_cleaned()
        except Exception:
            # One spare cannot suppress the rest of this tick.
            deps.logger.exception("RSS recycle failed for spare runtime pid=%d", pid)
        finally:
            # The sweep shield the removal set for this pid is dropped once its
            # process is torn down; only this pid, so a concurrent health sweep's
            # own shields survive.
            cleanup._owner._pool_sweep_pids.discard(pid)

    # Refill through the same helper the health sweep uses after a removal, so
    # the pool is not left a spare short until the next sweep. A no-op when the
    # pool is disabled; it only schedules a task, no loop work.
    if removed:
        cleanup._owner._schedule_replenish()

"""Keeping the warm pool warm and current: prewarm passes, top-ups, flushes, rotation.

The hot-key tally is persisted on a cadence, the hottest keys are warmed at
startup and topped up periodically through the same acquire path live stubs use
(yielding to any stub queued at the spawn gate), and a credential rotation drains
every pooled backend blue-green before re-warming.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Callable, Optional

from kiro_crew.mcp_gateway.admission import Admission
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.daemon.launch import TargetResolver
from kiro_crew.mcp_gateway.pool import DRAIN_DEADLINE_SECS, BackendPool, PoolKey
from kiro_crew.mcp_gateway.prewarm import HotKeyStore, prewarm_from_payloads

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
else:
    from kiro_crew.mcp_gateway.daemon import facade


# Interval between credential-file change probes when one or more
# ``--credential-watch-path`` flags were supplied. On a content change,
# backends spawned with the stale credential are drained (blue-green
# cutover) so they respawn with the refreshed credential. The probe is a
# cheap stat (plus a hash only when mtime moved), so 30s keeps rotation
# latency low without measurable overhead. No flag ⇒ no watcher task.
_CREDENTIAL_WATCH_INTERVAL_SECS = 30.0


# Interval between hot-key persistence flushes when prewarming is enabled.
# Recording a register hit is O(1) in-memory; the actual disk write is
# batched onto this cadence and run via ``asyncio.to_thread`` so the event
# loop never blocks on IO. 30s bounds data loss on a hard kill to one
# interval of observation while keeping write volume negligible.
_HOT_KEYS_FLUSH_INTERVAL_SECS = 30.0


# Interval between warm-pool top-up passes when prewarming is enabled. A
# prewarmed backend can be lost between passes (it died, or was reclaimed under
# capacity pressure despite pinning if the cap was genuinely exhausted), so a
# periodic re-warm restores the hot set without waiting for the next restart.
# The pass is idempotent — a still-present backend is reused, not respawned —
# so this cadence only pays for backends that actually need re-warming. Set
# above the idle timeout so a healthy warm set is not needlessly re-checked too
# often, while still recovering a lost backend well within a few minutes.
_PREWARM_TOPUP_INTERVAL_SECS = 120.0


# A prewarm's own wait at the spawn gate. Nobody is waiting for a warm backend,
# and the gate is strict FIFO with no priority lane, so a prewarm that has queued
# is ahead of every stub that arrives after it: bounding the wait is what caps how
# long that can last. It is also what keeps a pass from parking under
# ``_prewarm_lock``, which the credential-rotation re-warm needs to take. Kept
# well under ``_PREWARM_TOPUP_INTERVAL_SECS`` so a pass that stood down is retried
# by the sweeper rather than overlapping it.
_PREWARM_SPAWN_WAIT_SECS = 10.0


class _PrewarmStoodDown(RuntimeError):
    """A live stub is queued at the spawn gate, so this key is not warmed.

    Raised per key rather than checked once per pass, and carried as an
    exception because that is the one signal ``prewarm_from_payloads`` already
    treats as "log it and keep going" — a warm backend nobody asked for is never
    worth a place in front of a session that did."""


async def _hot_keys_flush_sweeper(
    hot_keys: HotKeyStore,
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    """Persist the hot-key tally once per ``interval`` until ``stop_event``
    is set. The write runs via :func:`asyncio.to_thread` so the blocking
    file IO never stalls the event loop — the on-loop path only ever
    mutates an in-memory dict. A flush that writes nothing (no new hits) is
    a cheap no-op inside :meth:`HotKeyStore.flush`.
    """
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly (final flush at shutdown)
            except asyncio.TimeoutError:
                pass
            try:
                wrote = await asyncio.to_thread(hot_keys.flush)
                if wrote:
                    logger.debug("hot-keys: flushed to %s", hot_keys.path)
            except Exception:  # pragma: no cover — defensive
                logger.exception("hot-keys flush failed; continuing")
    except asyncio.CancelledError:
        pass


async def _prewarm_topup_sweeper(
    schedule_prewarm: Callable[[], None],
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    """Re-warm the hot set once per ``interval`` until ``stop_event`` is set.

    Calls ``schedule_prewarm`` (a fire-and-forget scheduler), which runs an
    idempotent pass: a hot key whose backend is still pooled is reused at no
    cost, and one whose backend has died or been reclaimed is respawned. This
    keeps the warm set populated for the daemon's whole lifetime instead of
    only at startup. The scheduler itself is non-blocking, so the sweeper just
    sleeps between triggers.
    """
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly
            except asyncio.TimeoutError:
                pass
            try:
                schedule_prewarm()
            except Exception:  # pragma: no cover — defensive
                logger.exception("prewarm top-up scheduling failed; continuing")
    except asyncio.CancelledError:
        pass


async def _drain_and_rewarm_on_credential_change(
    pool: BackendPool,
    schedule_prewarm: Callable[[], None],
) -> None:
    """Handle a credential rotation via blue-green cutover: move ALL
    active backends (including in-use, refcount>0) to the draining list,
    then re-warm fresh backends with the new credential.

    Draining backends continue serving in-flight requests but are invisible
    to new acquires. The heartbeat sweeper reaps them when refcount drops to
    0 or the deadline expires, whichever first. New requests immediately cut
    over to fresh backends spawned with the rotated credential.

    If the drain itself raises, we deliberately skip the re-warm: stale
    backends may still be pooled, and re-warming would reuse + PIN them,
    making them harder to evict next cycle. Skipping leaves recovery to the
    next credential change or the top-up sweeper once they idle out.
    """
    try:
        # First evict truly idle backends (refcount==0) immediately — they
        # have no in-flight work and can be killed outright.
        idle_drained = await pool.evict_idle(0.0, include_pinned=True)
        # Move in-use backends (refcount>0) to the draining list for
        # blue-green cutover — they finish in-flight work then get reaped.
        moved = await pool.drain_all_to_bluegreen()
        logger.info(
            "credential file changed: blue-green cutover — evicted %d idle, "
            "moved %d in-use to draining (deadline=%ds)",
            idle_drained,
            moved,
            int(DRAIN_DEADLINE_SECS),
        )
    except Exception:
        logger.exception("credential-change blue-green cutover failed; skipping re-warm")
        return
    schedule_prewarm()


class _Prewarmer:
    """The warm-pool pass and the tasks running it, for one ``run_gatewayd``.

    The warm set is kept ready by three triggers, all routed through the same
    idempotent pass (a backend already in the pool is reused by the acquire path,
    so re-running is cheap and self-healing):

    * (a) once at startup,
    * (b) a periodic top-up sweeper that re-warms any hot key whose backend has
      since died or been reclaimed under capacity pressure, and
    * (c) after a credential-cookie refresh, so a freshly-rotated credential is
      baked into the warm backends before the next chat attaches.

    Disabled (``hot_keys`` is ``None``) means :meth:`schedule` creates no task and
    the record/IO paths are no-ops.
    """

    def __init__(
        self,
        pool: BackendPool,
        resolver: TargetResolver,
        admission: Admission,
        hot_keys: Optional[HotKeyStore],
        prewarm_count: int,
    ) -> None:
        self.pool = pool
        self.resolver = resolver
        self.admission = admission
        self.hot_keys = hot_keys
        self.prewarm_count = prewarm_count
        # Serializes passes so the unpin loop sees the latest state.
        self._prewarm_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    async def run_pass(self, *, initial: bool = False) -> None:
        # Warm the top-N hottest keys through the same acquire path live stubs
        # use. Fully best-effort: any failure leaves the daemon serving lazily.
        #
        # Disk is loaded ONLY on the initial startup pass. Re-loading on every
        # top-up / cookie-rewarm would overwrite the live in-memory tally with
        # the last-flushed snapshot -- regressing hit/miss counters and any keys
        # observed since the last flush (up to one flush interval of loss). The
        # running store already holds the freshest observations, so subsequent
        # passes read straight from memory.
        #
        # Serialized via _prewarm_lock so overlapping passes (startup vs top-up
        # vs cookie-refresh) never race on pin/unpin -- the unpin loop always
        # reflects the most recently warmed set.
        hot_keys = self.hot_keys
        pool = self.pool
        admission = self.admission
        assert hot_keys is not None  # guarded by the caller
        async with self._prewarm_lock:
            try:
                if initial:
                    await asyncio.to_thread(hot_keys.load)
                # Prewarm yields to every stub: a live session waiting in the
                # gate's queue would only be delayed by warming a key nobody
                # has asked for. Checked here AND before each key below,
                # because a pass takes as long as its spawns and a stub that
                # arrives during one would otherwise queue behind the rest of
                # it. The top-up sweeper runs this pass again later.
                if admission.gate.queued > 0:
                    logger.info(
                        "prewarm: %d stub(s) queued at the spawn gate — skipping this pass",
                        admission.gate.queued,
                    )
                    return
                payloads = hot_keys.top_register_payloads(self.prewarm_count)
                if not payloads:
                    logger.info("prewarm: no hot keys yet — nothing to warm")
                    return

                async def _acquire(pool_key: PoolKey) -> Backend:
                    # Audit only a REAL spawn (not a pool reuse) so the SEL log
                    # reports actual out-of-handshake subprocess creations 1:1.
                    #
                    # Gate on ``was_spawned`` — set inside the pool's per-key
                    # create lock — NOT a racy ``pool.get()`` pre-check. A
                    # pooled backend can die or be evicted (idle/LRU/heartbeat
                    # sweep, capacity pressure) between a pre-check and the
                    # acquire, turning a "reuse" into a real spawn whose audit
                    # a pre-check would silently skip.
                    #
                    # ``prewarm=True``: the permit settles NEUTRAL the moment
                    # the spawn returns. Nothing sends this backend an
                    # ``initialize`` until a stub attaches, so waiting for
                    # one would time out every unused warm backend and read
                    # the daemon's own prewarming as congestion.
                    #
                    # Re-read per key, not once per pass: the queue can gain a
                    # live stub while an earlier key is being spawned, and the
                    # gate admits in strict arrival order, so a prewarm that
                    # enqueues after that stub arrives is served BEFORE it.
                    # ``prewarm_from_payloads`` logs the stand-down and moves
                    # on, leaving the daemon to serve lazily.
                    if admission.gate.queued > 0:
                        raise _PrewarmStoodDown(
                            f"{admission.gate.queued} stub(s) queued at the spawn gate"
                        )
                    # ...and bounded, because the gate has no priority lane: a
                    # prewarm already queued cannot be overtaken, so the wait
                    # is what caps how long a stub can sit behind it.
                    backend, was_spawned = await facade._acquire_backend(
                        pool,
                        pool_key,
                        self.resolver,
                        admission=admission,
                        prewarm=True,
                        wait_deadline=time.monotonic() + _PREWARM_SPAWN_WAIT_SECS,
                    )
                    if was_spawned:
                        facade._audit_prewarm_spawn(pool_key.human_readable())
                    return backend

                await prewarm_from_payloads(
                    payloads,
                    _acquire,
                    limit=self.prewarm_count,
                    unreserve=pool.unreserve,
                )

                # Unpin backends whose key fell out of the current top-N so
                # the idle sweeper can reclaim them. Prevents unbounded pin
                # accumulation across hot-set drift and config_snapshot_hash
                # changes (only the CURRENT top-N stays pinned).
                current_top_digests = {
                    PoolKey.from_register(p).stable_hash() for p in payloads[: self.prewarm_count]
                }
                for pool_key, backend in await pool.snapshot():
                    if (
                        getattr(backend, "pinned", False)
                        and pool_key.stable_hash() not in current_top_digests
                    ):
                        backend.pinned = False
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover -- defensive
                logger.exception("prewarm pass failed; serving lazily")

    def schedule(self, *, initial: bool = False) -> None:
        """Fire-and-forget one warm pass, tracked so shutdown can cancel it.
        ``initial=True`` loads persisted hot keys from disk (startup only).
        No-op when prewarming is disabled."""
        if self.hot_keys is None:
            return
        task = asyncio.create_task(self.run_pass(initial=initial), name="mcp-gateway-prewarm")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def cancel_all(self) -> None:
        """Cancel every in-flight pass (startup / top-up / credential-triggered) so a
        slow handshake cannot stall shutdown."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

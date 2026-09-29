"""The daemon's periodic liveness and reclamation sweeps.

Idle eviction, backend temp reclamation, the two self-exit watches (the listening
socket's path and the owning gateway's PID), and the heartbeat that probes stub
transports and pooled backends. Each sweep runs until the daemon's
``stop_event`` is set or ``run_gatewayd`` cancels it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from kiro_crew.mcp_gateway import hazards
from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.pool import BackendPool

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
else:
    from kiro_crew.mcp_gateway.daemon import facade


# Interval between per-backend heartbeat sweeps. A backend
# that is gone, or wedged with an in-flight request outstanding past
# ``backend.HEARTBEAT_TIMEOUT_SECS``, is recycled on the next sweep. 60s
# balances recovery latency against ping overhead; the first sweep fires one
# interval after startup so short-lived runs (tests) never trigger it.
_HEARTBEAT_SWEEP_INTERVAL_SECS = 60.0


async def _idle_sweeper(
    pool: BackendPool,
    idle_timeout_secs: int,
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    """Periodically drop idle backends from ``pool`` until ``stop_event``
    is set. One sweep per ``interval`` seconds; sweeps themselves are
    non-blocking.
    """
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly
            except asyncio.TimeoutError:
                pass
            try:
                evicted = await pool.evict_idle(idle_timeout_secs)
                if evicted:
                    logger.debug("idle sweep evicted %d backends", evicted)
            except Exception:  # pragma: no cover — defensive
                logger.exception("idle sweep failed; continuing")
    except asyncio.CancelledError:
        pass


async def _backend_tmp_sweeper() -> None:
    """Reclaim per-process backend temp dirs whose owner is dead AND whose content is idle.

    Deletion deliberately lives ONLY here, never on a shutdown path, because a
    launcher's exit is not proof its process tree is gone (see ``backend_tmp``).
    The first pass runs at task start (the same boot posture as the spool sweep),
    then hourly; offloaded and best-effort.
    """
    while True:
        try:
            await asyncio.to_thread(facade.sweep_all_backend_tmp)
        except Exception:
            logger.debug("backend-tmp: sweep failed", exc_info=True)
        await asyncio.sleep(3600)


#: Consecutive missing-socket observations required before the daemon
#: self-exits. A single stat is not trusted: a transient tmpfs/NFS hiccup
#: must not kill a healthy daemon, so only an uninterrupted run of misses
#: counts as proof of unreachability.
_SOCKET_LIVENESS_MISSES = 3

#: How often the daemon confirms its owning gateway is still the process
#: that spawned it. Coarse on purpose: a stat of one /proc entry, and a
#: dead owner costs nothing but idle pooled backends until the next probe.
_OWNER_LIVENESS_INTERVAL_SECS = 15.0

#: Consecutive owner-gone observations before self-exit. Two, not one: the
#: start-time read and the pid-exists read are separate syscalls, and a
#: transient EACCES/EIO between them must not end a serving daemon.
_OWNER_LIVENESS_MISSES = 2


async def _owner_liveness_sweeper(
    owner_pid: int,
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    """Self-exit when the gateway that spawned this daemon is gone.

    ``owner_pid`` is the PID the launcher passed on argv. Its start time is
    read ONCE at arm time and compared on every probe: a PID number is
    recycled by the kernel, so ``pid_exists`` alone would let a daemon keep
    running for whatever unrelated process later took its owner's number.

    Fail-safe rules mirror :func:`_socket_liveness_sweeper`: a start time that
    is unreadable *while the owner still exists* disables the check (nothing to
    compare against, and refusing to serve would be worse than serving one
    generation too long); an owner that is conclusively **gone** at arm time
    takes the graceful drain path immediately, because "unreadable" then means
    gone rather than unknown; a probe that cannot read the start time is
    inconclusive and neither counts nor resets;
    :data:`_OWNER_LIVENESS_MISSES` consecutive conclusive misses set
    ``stop_event``, which is the graceful drain path.
    """
    baseline = await asyncio.to_thread(facade._process_start_time, owner_pid)
    if baseline is None:
        # No baseline has two causes that need OPPOSITE answers, and treating
        # them alike is what let a daemon outlive its owner. The listening
        # socket is bound and advertised before this task first runs, so on a
        # contended host the owner can die inside the gap between the bind and
        # this read. Disabling the check there retired the only mechanism that
        # would ever have ended this daemon, so it served an owner that no
        # longer existed until something killed it from outside.
        # ``pid_exists`` separates the two: it reports False only on
        # ProcessLookupError, and True when the process exists but cannot be
        # signalled -- so False here is a conclusion, not a read failure.
        if not await asyncio.to_thread(facade._pid_exists, owner_pid):
            logger.warning(
                "gatewayd: owning gateway pid %d was already gone when the "
                "owner-liveness check armed; this daemon serves no live gateway "
                "-- initiating graceful self-shutdown",
                owner_pid,
            )
            stop_event.set()
            return
        logger.warning(
            "gatewayd: owner pid %d is alive but has no readable start time; the "
            "owner-liveness check is disabled for this daemon",
            owner_pid,
        )
        return
    misses = 0
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            alive = await asyncio.to_thread(facade._pid_exists, owner_pid)
            if alive:
                now = await asyncio.to_thread(facade._process_start_time, owner_pid)
                if now is None:
                    continue  # inconclusive: neither a miss nor a reset
                if now == baseline:
                    misses = 0
                    continue
            misses += 1
            if misses >= _OWNER_LIVENESS_MISSES:
                logger.warning(
                    "gatewayd: owning gateway pid %d is gone (%s); this daemon serves "
                    "no live gateway and would only be adopted by a newer one running "
                    "different code -- initiating graceful self-shutdown",
                    owner_pid,
                    "pid recycled" if alive else "process exited",
                )
                stop_event.set()
                break
    except asyncio.CancelledError:
        raise


async def _socket_liveness_sweeper(
    socket_path: Path,
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    """Self-exit when the daemon's own listening socket path disappears.

    The daemon is spawned with ``start_new_session=True``, making it a
    session and process-group leader: when its launcher dies without
    signalling it, no ``killpg`` from the launcher's tree can reach it and it
    stays resident forever. The one unreachability signal observable from
    inside is the listening socket path this daemon created at bind — once
    that path is gone, no stub can ever connect again, so the process is
    provably useless regardless of who launched it. Exiting through
    ``stop_event`` takes exactly the graceful drain path SIGTERM takes:
    in-flight work drains and every pooled backend is shut down.

    Fail-closed rules:

    * The caller arms this task only AFTER a successful bind — before that,
      an absent path is a startup race, not unreachability.
    * Only ``FileNotFoundError`` (ENOENT) counts as a miss. Any other stat
      failure (EACCES, EIO, …) is inconclusive: it neither counts toward
      exit nor resets an in-progress miss streak.
    * :data:`_SOCKET_LIVENESS_MISSES` CONSECUTIVE misses are required; a
      successful stat resets the streak.
    * POSIX-only — a Windows named pipe has no directory entry to observe,
      so the caller never creates this task there.
    """
    misses = 0
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(os.stat, socket_path)
                misses = 0
            except FileNotFoundError:
                misses += 1
                if misses >= _SOCKET_LIVENESS_MISSES:
                    logger.warning(
                        "gatewayd socket %s missing for %d consecutive checks — "
                        "no stub can reach this daemon again; initiating "
                        "graceful self-shutdown",
                        socket_path,
                        misses,
                    )
                    stop_event.set()
                    break
            except OSError:
                logger.debug(
                    "socket liveness probe inconclusive for %s",
                    socket_path,
                    exc_info=True,
                )
    except asyncio.CancelledError:
        pass


async def _heartbeat_sweeper(
    pool: BackendPool,
    interval: float,
    stop_event: asyncio.Event,
    backends_pidfile: Optional[Path] = None,
) -> None:
    """Probe stub transports and every pooled backend once per ``interval``,
    until ``stop_event`` is set.

    Two independent responsibilities, in this order:

    1. **Stub transports** (:func:`_probe_stub_transports`) -- write a keepalive
       to every live stub connection. A half-open transport is invisible to the
       parked reader and surfaces only on a write, so without this probe a stub
       that died mid-session never detaches and its backend's refcount never
       reaches 0 -- putting it permanently out of reach of the idle sweep. A
       failed write cancels that stub's handler, whose teardown detaches it.
    2. **Backends** -- :meth:`Backend._heartbeat_once` classifies each one:

    * ``"gone"`` / ``"wedged"`` -- the classify call has already errored every
      attached stub (via ``_broadcast_backend_gone``); the sweeper evicts the
      backend from the pool, shuts it down, and records the death against the
      circuit breaker so a crash loop trips it.
    * ``"alive"`` -- record a healthy signal that closes any OPEN breaker for
      the server.
    * ``"idle"`` -- left untouched; the idle sweeper owns eviction.

    The first sweep fires one full ``interval`` after startup, so short-lived
    runs (tests) never trigger the periodic logic.
    """
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly
            except asyncio.TimeoutError:
                pass
            try:
                now = time.monotonic()
                # Probe stub transports FIRST. A dead stub detected here
                # detaches on this same sweep, so the backend sweep below and
                # the idle sweep see the corrected refcount immediately rather
                # than one interval late.
                try:
                    await facade._probe_stub_transports()
                except Exception:  # pragma: no cover — defensive
                    logger.exception("stub transport probe crashed")
                for key, backend in await pool.snapshot():
                    try:
                        state = await backend._heartbeat_once(now)
                    except Exception:  # pragma: no cover — defensive
                        logger.exception("heartbeat probe crashed for %s", key.human_readable())
                        continue
                    if state in ("gone", "wedged"):
                        pool.note_backend_death(key.stable_hash(), now - backend.created_at)
                        evicted = await pool.evict(key, expected=backend)
                        if evicted is not None:
                            with contextlib.suppress(Exception):
                                await evicted.shutdown(timeout=2.0)
                        logger.warning(
                            "heartbeat recycled %s backend pool=%s",
                            state,
                            key.human_readable(),
                        )
                    elif state == "alive":
                        pool.note_backend_healthy(key.stable_hash())
                # Reap draining backends (blue-green cutover) whose refcount
                # hit 0 or whose deadline expired.
                reaped = await pool.reap_draining()
                for backend in reaped:
                    logger.info(
                        "heartbeat reaped draining backend server=%s pid=%s "
                        "refcount=%d (credential-rotation cutover)",
                        backend.pool_key.server_name,
                        backend.pid,
                        backend.refcount,
                    )
                # Persist live backend pids out-of-band so the supervising
                # manager can killpg them if it must SIGKILL a wedged gatewayd
                # (which then never runs pool.shutdown_all()).
                if backends_pidfile is not None:
                    # Offload the file write: it is otherwise a synchronous
                    # open+write+close on the event loop (every other write in
                    # the daemon — _write_diagnostic, hot_keys.flush, socket
                    # probes — is offloaded via to_thread for the same reason).
                    pids = "\n".join(str(p) for p in pool.live_backend_pids())
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(backends_pidfile.write_text, pids)
                # Persist any per-client behaviour observed since the last
                # sweep. Offloaded for the same reason as the pidfile write,
                # and cheap when nothing was observed (the flush is a no-op
                # unless the in-memory ledger is dirty).
                await asyncio.to_thread(hazards.flush_sink)
            except Exception:  # pragma: no cover — defensive
                logger.exception("heartbeat sweep failed; continuing")
    except asyncio.CancelledError:
        pass
    finally:
        # The periodic flush above only persists what was observed before the
        # last tick. A clean shutdown cancels this task, so anything observed in
        # the final interval would be lost — and a hazard is the strongest
        # evidence the system has, so losing one means a server that misbehaved
        # keeps its recommendation until it misbehaves again. Shutdown is the
        # ordinary path here, not the exceptional one, so it flushes as well.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(hazards.flush_sink)

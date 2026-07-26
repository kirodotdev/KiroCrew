"""Shared thread-pool executors for blocking maintenance work.

The asyncio event loop's *default* executor is also used by the loop itself
for ``getaddrinfo`` (DNS) and by every ``run_in_executor(None, ...)`` caller.
When long-running maintenance work (orphan-PID sweeps, cron subprocesses,
agent-overlay rewrites) piles onto that default pool it can saturate it and
starve the loop's own DNS resolution -- which is exactly the failure mode that
turns a brief network blip into a multi-second event-loop stall.

This module provides *separate*, bounded pools for that blocking work so it
can never contend with the loop's default executor.

Three pools, deliberately split:

* :func:`maintenance_executor` -- the fast periodic sweeps (orphaned MCP/PID
  cleanup) and agent-overlay rewrites.  These are short and MUST stay
  responsive, because the orphan sweeps are what reap the leaked kiro-cli/MCP
  children whose mass death is the documented root cause of the loop wedge.
* :func:`subprocess_executor` -- per-process teardown work that can block on a
  *wedged kernel resource*: ``os.close`` on a PTY master fd whose far-end shell
  is in uninterruptible sleep, and the ``ps``/``pgrep`` spawns + ``os.kill``
  sweep in :mod:`kiro_crew.acp.client`.  These can hang indefinitely, so they
  get their OWN pool: a storm of wedged teardowns can occupy every worker here
  WITHOUT starving the :func:`maintenance_executor` orphan sweep that is the
  recovery action for the wedge (the bug the wedge fix would otherwise create
  by coupling the recovery mechanism to the same pool as the work that wedges).
* :func:`cron_executor` -- user cron command/script execution, which can run
  for minutes (``job.timeout`` defaults to 300s) and, because the scheduler
  dispatches every due job concurrently, can have several jobs in flight at
  once.  Keeping cron on its OWN pool means a burst of long-running cron jobs
  can never occupy all the maintenance threads and starve the sweeps.
* :func:`discovery_executor` -- browser-triggerable, read-only filesystem
  discovery for dashboard list endpoints (``GET /api/skills``,
  ``/api/agents/installed``, ``/api/prompts``): ``os.walk`` of skill/agent/SOP
  trees, per-file frontmatter reads, ``~/.aim`` globs.  A large catalog makes
  one such scan take seconds, and a ``run_in_executor`` future cannot be
  cancelled once started, so N concurrent dashboard tabs would each pin a
  worker for the full scan.  This gets its OWN pool so those user-triggerable
  scans can never occupy the :func:`maintenance_executor` workers the orphan
  sweeps need to recover from a wedge.

Long-term direction: this blocking work should move into a dedicated
supervised process (the VS Code extension-host model), so a wedge there cannot
touch the dashboard event loop at all.  These bounded pools are the in-process
containment until that process split lands.
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")

__all__ = [
    "maintenance_executor",
    "subprocess_executor",
    "cron_executor",
    "discovery_executor",
    "embed_executor",
    "governance_executor",
    "run_in_embed_pool",
    "shutdown_maintenance_executor",
]

# Bounded so a burst of maintenance work cannot spawn unbounded threads.  Four
# is enough for the periodic sweeps + agent-overlay rewrites while leaving the
# default executor entirely free for the loop's own I/O.
_MAX_MAINT_WORKERS = 4

# Subprocess/PTY teardown can block on a wedged kernel resource and a single
# mass shutdown (close_all over the warm pool + active sessions) enqueues
# several tasks per client.  Size this above the maintenance pool so a burst of
# wedged teardowns queues among ITSELF rather than head-of-line-blocking the
# orphan sweep on the separate maintenance pool.  Still bounded: a started
# run_in_executor future cannot be cancelled, so this caps how many wedged
# closes/spawns we hold threads for; excess work queues here.
_MAX_SUBPROCESS_WORKERS = 8

# Cron jobs can be long (default 300s timeout) and several can be due in the
# same tick (the scheduler fires each as an independent task).  Give them their
# own bounded pool so they queue among THEMSELVES rather than evicting the fast
# maintenance sweeps.  A run_in_executor future cannot be cancelled once its
# thread starts, so this also caps how many concurrent cron subprocesses we hold
# threads for; excess jobs queue here instead of starving anything else.
_MAX_CRON_WORKERS = 4

# Dashboard discovery scans (skills/agents/SOP listing) are read-only but can
# take seconds on a large catalog, and each is browser-triggerable so several
# can be in flight at once (multiple tabs / pollers).  A started
# run_in_executor future cannot be cancelled, so bound this at a handful of
# workers: concurrent scans queue among THEMSELVES here rather than occupying
# the maintenance pool's orphan-sweep workers.
_MAX_DISCOVERY_WORKERS = 4

# Ollama embed/probe offloads (consolidation lesson writes, memory import,
# context-preview, and every build_message call — its episodic recall embeds
# the query) block on network I/O for up to embedding_timeout_secs per call —
# and against a HUNG endpoint every call eats the full timeout, since failures
# are deliberately not cached.  Give them their own bounded pool so a wedged
# Ollama parks mc-embed threads and queues further embed work behind ITSELF,
# instead of exhausting asyncio's default executor (which the loop shares for
# DNS resolution and every other asyncio.to_thread user).  Sized above the
# other pools because build_message runs on every new session across all
# surfaces (dashboard + Slack + cron + heartbeat + subagent can land
# concurrently after a restart); with a healthy endpoint each call is fast,
# so the cap only bites — deliberately — when Ollama is wedged.
_MAX_EMBED_WORKERS = 8

# Governance checks for EXTERNALLY-triggered surfaces: the per-inbound-message
# channels gate (every Slack/Discord/Telegram/Webex/WeCom message + approval
# callback)
# and the dashboard governance GETs.  These walk the ProfileStore (filesystem)
# and may do a synchronous SEL write, and their rate is driven by REMOTE senders
# — a burst of inbound messages could otherwise occupy every mc-maint worker and
# starve the orphan sweeps.  Own bounded pool so externally-paced governance I/O
# queues among ITSELF, never evicting the maintenance sweeps.
_MAX_GOVERNANCE_WORKERS = 4

_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None
_subprocess_pool: ThreadPoolExecutor | None = None
_cron_pool: ThreadPoolExecutor | None = None
_discovery_pool: ThreadPoolExecutor | None = None
_embed_pool: ThreadPoolExecutor | None = None
_governance_pool: ThreadPoolExecutor | None = None


def maintenance_executor() -> ThreadPoolExecutor:
    """Return the process-wide maintenance thread pool, creating it on first use.

    Threads are named ``mc-maint`` for easy identification in watchdog stack
    dumps.  Distinct from asyncio's default executor so blocking maintenance
    work cannot starve the event loop's DNS resolution.  Reserved for the fast
    periodic sweeps + overlay rewrites -- cron uses :func:`cron_executor`.
    """
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(
                    max_workers=_MAX_MAINT_WORKERS,
                    thread_name_prefix="mc-maint",
                )
                atexit.register(shutdown_maintenance_executor)
    return _pool


def subprocess_executor() -> ThreadPoolExecutor:
    """Return the process-wide subprocess/PTY-teardown pool, creating it on first use.

    Threads are named ``mc-subproc``.  Separate from :func:`maintenance_executor`
    so a teardown call that blocks on a wedged kernel resource (PTY ``os.close``
    on an uninterruptible-sleep shell, a hung ``ps``/``pgrep`` spawn) cannot
    occupy the workers the orphan-reaping sweep needs to recover from the wedge.
    """
    global _subprocess_pool
    if _subprocess_pool is None:
        with _lock:
            if _subprocess_pool is None:
                _subprocess_pool = ThreadPoolExecutor(
                    max_workers=_MAX_SUBPROCESS_WORKERS,
                    thread_name_prefix="mc-subproc",
                )
                atexit.register(shutdown_maintenance_executor)
    return _subprocess_pool


def cron_executor() -> ThreadPoolExecutor:
    """Return the process-wide cron execution thread pool, creating it on first use.

    Threads are named ``mc-cron``.  Separate from :func:`maintenance_executor`
    so long-running, concurrent cron command/script jobs queue among themselves
    instead of occupying the maintenance threads the orphan-reaping sweeps need.
    """
    global _cron_pool
    if _cron_pool is None:
        with _lock:
            if _cron_pool is None:
                _cron_pool = ThreadPoolExecutor(
                    max_workers=_MAX_CRON_WORKERS,
                    thread_name_prefix="mc-cron",
                )
                atexit.register(shutdown_maintenance_executor)
    return _cron_pool


def discovery_executor() -> ThreadPoolExecutor:
    """Return the process-wide dashboard-discovery pool, creating it on first use.

    Threads are named ``mc-discovery``.  Separate from :func:`maintenance_executor`
    so browser-triggerable, seconds-long read-only filesystem scans (skills /
    agents / SOP listing for the dashboard) can never occupy the workers the
    orphan-reaping sweeps need to recover from an event-loop wedge.
    """
    global _discovery_pool
    if _discovery_pool is None:
        with _lock:
            if _discovery_pool is None:
                _discovery_pool = ThreadPoolExecutor(
                    max_workers=_MAX_DISCOVERY_WORKERS,
                    thread_name_prefix="mc-discovery",
                )
                atexit.register(shutdown_maintenance_executor)
    return _discovery_pool


def embed_executor() -> ThreadPoolExecutor:
    """Return the process-wide Ollama embed/probe pool, creating it on first use.

    Threads are named ``mc-embed``.  Separate from asyncio's default executor
    so a hung embedding endpoint (every call eats the full
    ``embedding_timeout_secs``; failures are deliberately not cached) parks
    only these workers — embed work queues behind ITSELF instead of starving
    the loop's DNS resolution and every other ``asyncio.to_thread`` user.
    """
    global _embed_pool
    if _embed_pool is None:
        with _lock:
            if _embed_pool is None:
                _embed_pool = ThreadPoolExecutor(
                    max_workers=_MAX_EMBED_WORKERS,
                    thread_name_prefix="mc-embed",
                )
                atexit.register(shutdown_maintenance_executor)
    return _embed_pool


def governance_executor() -> ThreadPoolExecutor:
    """Return the process-wide governance-check pool, creating it on first use.

    Threads are named ``mc-gov``.  Separate from :func:`maintenance_executor` so
    the externally-paced per-inbound-message channels gate (and the dashboard
    governance GETs) — which walk the ProfileStore and may do a synchronous SEL
    write — can never occupy the workers the orphan-reaping sweeps need.  A remote
    burst of inbound messages queues among ITSELF here instead of starving the
    maintenance sweeps.
    """
    global _governance_pool
    if _governance_pool is None:
        with _lock:
            if _governance_pool is None:
                _governance_pool = ThreadPoolExecutor(
                    max_workers=_MAX_GOVERNANCE_WORKERS,
                    thread_name_prefix="mc-gov",
                )
                atexit.register(shutdown_maintenance_executor)
    return _governance_pool


async def run_in_embed_pool(func: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Run a blocking Ollama embed/probe callable on :func:`embed_executor`.

    Drop-in replacement for ``asyncio.to_thread`` at embed call sites: same
    signature, but the work lands on the bounded ``mc-embed`` bulkhead pool
    instead of asyncio's shared default executor.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(embed_executor(), functools.partial(func, *args, **kwargs))


def shutdown_maintenance_executor() -> None:
    """Shut down all maintenance pools if they were created.  Idempotent."""
    global _pool, _subprocess_pool, _cron_pool, _discovery_pool, _embed_pool
    global _governance_pool
    with _lock:
        pool, _pool = _pool, None
        subprocess_pool, _subprocess_pool = _subprocess_pool, None
        cron_pool, _cron_pool = _cron_pool, None
        discovery_pool, _discovery_pool = _discovery_pool, None
        embed_pool, _embed_pool = _embed_pool, None
        governance_pool, _governance_pool = _governance_pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
    if subprocess_pool is not None:
        subprocess_pool.shutdown(wait=False, cancel_futures=True)
    if cron_pool is not None:
        cron_pool.shutdown(wait=False, cancel_futures=True)
    if discovery_pool is not None:
        discovery_pool.shutdown(wait=False, cancel_futures=True)
    if embed_pool is not None:
        embed_pool.shutdown(wait=False, cancel_futures=True)
    if governance_pool is not None:
        governance_pool.shutdown(wait=False, cancel_futures=True)

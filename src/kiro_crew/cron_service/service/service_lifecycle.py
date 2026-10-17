"""Start and stop the cron service: the async factory, the timer's start and the shutdown.

:meth:`~kiro_crew.cron.CronService.create` builds the service off the event loop and
moves an unreadable store aside under the store lock; ``start`` loads the jobs, runs
the loop-stall breaker and arms the timer; ``stop`` cancels the reaper, the timer and
every run, and saves the run records the store lock refused.

Each function here is a :class:`~kiro_crew.cron.CronService` member: the class binds
it by name, so it reads and writes the service's own state on the instance, and a
patch of the member on the class or on an instance reaches every caller. A name
tests patch on :mod:`kiro_crew.cron`, or one that module defines, is read through
it on each call (``seams``).

Instance fields written: ``_loop``, ``_reaper_task``, ``_running``, ``_timer_task``,
``quarantined_store``.

Instance fields read: ``_held_run_records``, ``_history``, ``_jobs``, ``_load_failed``,
``_path``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from kiro_crew.cron_service.model import CronJob
from kiro_crew.cron_service.store import CronStoreBusy, quarantine_unreadable_store

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


# ── Lifecycle ──


async def create(
    cls: type[CronService],
    base_dir: Path | None = None,
    on_job: Callable[[CronJob], Awaitable[str | None]] | None = None,
    *,
    quarantine_unreadable: bool = False,
) -> "CronService":
    """Async factory for event-loop contexts (the gateway).

    Equivalent to ``CronService(...)`` but SAFE to call from a running
    event loop: the plain constructor performs its initial ``_load()`` —
    a whole-file ``read_bytes()`` + blake2b hash of ``crons.json`` —
    synchronously, which would block the sole gateway loop (chat, WS,
    timers, heartbeat) during async startup. This factory constructs with
    ``_defer_initial_load=True`` (so the constructor does no store I/O) and
    then runs that initial ``_load()`` in a worker thread via
    ``asyncio.to_thread``. ``_running`` is still ``False`` at this point, so
    ``_load()`` arms no timer — running it off-loop is safe.

    Genuinely-sync, loop-less processes (CLI, MCP server, apps SDK, tests)
    must keep using the plain constructor, which loads inline.

    ``quarantine_unreadable`` is for gateway startup. A store that failed to
    load refuses every write, and startup writes to it (app cron cleanup), so
    a corrupt file stopped the gateway from serving at all. With the flag, an
    unparseable file is renamed aside -- never rewritten or deleted -- and the
    store reloads as missing: empty and writable. :attr:`quarantined_store`
    then names the copy so the caller can tell the user where the jobs are.
    """
    self = cls(base_dir=base_dir, on_job=on_job, _defer_initial_load=True)
    # Bind to the gateway loop so off-loop mutation paths (async mutators'
    # worker cores, app-hook/SDK calls offloaded via asyncio.to_thread) can
    # re-arm the timer thread-safely — see _arm_timer / __init__ _loop.
    self._loop = asyncio.get_running_loop()
    await asyncio.to_thread(self._load)
    if quarantine_unreadable and self._load_failed:
        self.quarantined_store = await asyncio.to_thread(self._quarantine_unreadable_locked)
    # Resolve history usability off the loop too (deferred in __init__).
    await asyncio.to_thread(self._history.prepare)
    return self


def _quarantine_unreadable_locked(self: CronService) -> Path | None:
    """Rename an unparseable store aside under the store lock and reload."""
    try:
        with self._file_lock():
            moved = quarantine_unreadable_store(self._path)
            if moved is not None:
                self._load()
    except (CronStoreBusy, OSError):
        # The startup this exists to rescue must not fail on the lock itself
        # (busy, or an unwritable lock file): keep the refusing store as-is.
        logger.warning("Could not lock the cron store; leaving the unreadable store in place")
        return None
    if moved is not None:
        logger.warning(
            "Cron store %s could not be parsed; moved it to %s and started with an "
            "empty schedule. Run `kirocrew doctor` for how to restore it.",
            self._path,
            moved,
        )
    return moved


async def start(self: CronService) -> None:
    """Load jobs and start the timer loop.

    ``_load()`` is offloaded to a worker thread (``asyncio.to_thread``):
    ``start()`` is always awaited on the gateway event loop, and the load
    does a whole-file read+hash of ``crons.json`` — synchronous filesystem
    I/O that must never run on the loop. ``_running`` is still ``False``
    here, so the load arms no timer; ``_arm_timer()`` is called explicitly
    on the loop afterwards.
    """
    # Bind to the running loop (idempotent if create() already did) so any
    # off-loop re-arm during this service's lifetime self-heals to it.
    self._loop = asyncio.get_running_loop()
    await asyncio.to_thread(self._load)
    self._running = True
    await self._history.rotate_all()
    # BEFORE the timer is armed: a job the previous gateway died running
    # has no last_run_ts for that run, so it is due again the moment the
    # timer fires. The breaker must have paused it by then or the boot
    # re-runs the crash.
    await asyncio.to_thread(self._apply_loop_stall_breaker)
    self._arm_timer()
    logger.info("Cron service started with %d jobs", len(self._jobs))


async def stop(self: CronService) -> None:
    """Stop the timer loop and cancel running jobs."""
    self._running = False
    if self._reaper_task:
        self._reaper_task.cancel()
        try:
            await self._reaper_task
        except (asyncio.CancelledError, Exception):
            pass
        self._reaper_task = None
    if self._timer_task:
        self._timer_task.cancel()
        self._timer_task = None
    tasks = [claim.task for claim in self._claims.values() if claim.task is not None]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    self._claims.clear()
    # A record the store lock refused is held in memory only; save it before
    # this process goes, or the next one finds the job due again.
    if self._held_run_records:
        await asyncio.to_thread(self._flush_held_run_records)

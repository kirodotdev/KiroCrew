"""When the scheduler wakes, and what a wake fires.

The timer is armed for the next due job, capped at the poll interval; a tick
refreshes the store under the lock, drains deferred removals and held run records,
and starts every due job as its own task.

Each function here is a :class:`~kiro_crew.cron.CronService` member: the class binds
it by name, so it reads and writes the service's own state on the instance, and a
patch of the member on the class or on an instance reaches every caller. A name
tests patch on :mod:`kiro_crew.cron`, or one that module defines, is read through
it on each call (``seams``).

Instance fields written: ``_admission_deferring``, ``_admission_last_log``,
``_on_timer_running``, ``_timer_task``.

Instance fields read: ``_jobs``, ``_loop``, ``_pending_removals``, ``_running``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew import host_timezone, shutdown_event
from kiro_crew.cron_service.model import CronJob
from kiro_crew.cron_service.schedule import _TIMER_POLL_SECS, next_wake_secs
from kiro_crew.cron_service.store import CronStoreBusy

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


# ── Timer ──


def _next_wake_secs(self: CronService) -> float | None:
    """Compute seconds until the next job should fire (:func:`~kiro_crew.cron_service.schedule.next_wake_secs`)."""
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    return next_wake_secs(self._jobs, self._claims, seams.time.time())


def _effective_delay(self: CronService) -> float:
    """Compute the actual timer delay, capped at poll interval.

    Ensures the timer always wakes within _TIMER_POLL_SECS to _sync()
    externally-added jobs, even when the next job is far in the future.
    """
    delay = self._next_wake_secs()
    if delay is None:
        return _TIMER_POLL_SECS
    if self._admission_deferring and delay < _TIMER_POLL_SECS:
        # A critical-posture episode leaves deferred ``every``/``at``
        # jobs overdue (``last_run_ts`` untouched by design), which
        # would otherwise re-arm the timer at zero delay — a busy loop
        # of scans and admission probes on a host already under memory
        # pressure. Back off to the poll cadence: the episode is
        # re-evaluated (and deferred jobs fire) within one poll of
        # posture recovery.
        return _TIMER_POLL_SECS
    return min(delay, _TIMER_POLL_SECS)


def _arm_timer(self: CronService) -> None:
    # Creating or cancelling asyncio tasks is legal only on the loop thread.
    # Off the loop (a to_thread worker, or a CLI/test context) a task create
    # raises and a lone cancel would halt every job, so hand the arm to the
    # loop bound in create()/start() via call_soon_threadsafe. A loop-less
    # process (self._loop is None) has no scheduler to arm.
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        bound = self._loop
        if bound is not None and not bound.is_closed():
            bound.call_soon_threadsafe(self._arm_timer)
        return
    current = asyncio.current_task()
    # Never cancel the timer task from inside itself: the tick's `finally`
    # re-arms while it still runs, and a cancel would abort due jobs not yet
    # spawned. Skip the cancel and just create the replacement.
    #
    # A job's completion handler (_run_job_isolated) also re-arms, from its
    # own task, possibly while _on_timer is mid-sweep. Cancelling would abort
    # the sweep and a second task would double-fire, so drop this arm: the
    # tick's `finally` re-arms once the sweep is done.
    if self._on_timer_running and current is not self._timer_task:
        return
    if self._timer_task and not self._timer_task.done() and self._timer_task is not current:
        self._timer_task.cancel()
    if not self._running:
        return
    delay = self._effective_delay()

    logger.debug("Cron: next timer in %.1fs", delay)

    async def _tick() -> None:
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
            return  # shutdown signaled
        except asyncio.TimeoutError:
            pass  # normal wake-up
        if self._running:
            try:
                await self._on_timer()
            except Exception:
                logger.exception("Cron timer error — will re-arm")
            finally:
                # Always re-arm, even after errors
                if self._running:
                    self._arm_timer()

    self._timer_task = asyncio.create_task(_tick())


def _tick_scan_locked(self: CronService) -> list[CronJob]:
    """Locked store refresh + deferred-removal drain + snapshot. WORKER-THREAD ONLY.

    The tick's blocking work (lock spin, whole-file read + hash, deferred
    one-shot delete + save) runs here in a ``to_thread`` worker; the loop then
    runs the due-scan on the returned snapshot.

    Drains deferred removals BEFORE the snapshot so a finished
    ``delete_after_run`` job (see :meth:`defer_removal`) can never look due,
    even if ``_sync`` re-read it as enabled. On a busy store it returns the
    in-memory snapshot undrained; the next tick retries.
    """
    # A laptop that travels changes zone under a running gateway; this re-read
    # is what a "local" schedule follows. Here because this is the tick's one
    # worker-thread hop: the read is filesystem (or registry) I/O.
    host_timezone.refresh_host_timezone()
    drained: list[str] = []
    try:
        with self._file_lock():
            self._sync()
            drained = self._drain_pending_removals_locked()
            self._save_held_run_records_locked()
    except CronStoreBusy:
        logger.debug("Cron timer tick: store busy, using in-memory snapshot")
    except OSError as exc:
        logger.warning("Cron timer tick: store write failed, using in-memory snapshot: %s", exc)
    # Post-lock on purpose: the emit must never extend the store-lock hold
    # (see audit_one_shot_removal). Still on this worker thread, so the
    # queue append cannot block the event loop either.
    for jid in drained:
        self.audit_one_shot_removal(jid, "cron_deferred_drain")
    return list(self._jobs)


async def _on_timer(self: CronService) -> None:
    """Fire due jobs as independent tasks (non-blocking).

    The locked store refresh + deferred-removal drain + snapshot is
    offloaded to a worker thread (:meth:`_tick_scan_locked`) so a large or
    slow ``crons.json`` can never freeze the gateway loop with the
    ``_sync()`` ``read_bytes()`` + blake2b hash on every tick (the
    ``no-blocking-call-on-event-loop`` rule). The mutation-free due-scan —
    which reads loop-owned ``self._claims`` — then runs on the loop
    against the returned snapshot.

    Brackets the whole body with ``self._on_timer_running`` so a job
    completing during either ``to_thread`` await below (the scan, and the
    admission check) cannot have its ``_arm_timer()`` call cancel
    ``self._timer_task`` out from under this sweep — see ``_arm_timer``.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    self._on_timer_running = True
    try:
        snapshot = await asyncio.to_thread(self._tick_scan_locked)
        now = seams.time.time()
        due = [
            j for j in snapshot if j.enabled and j.id not in self._claims and self._is_due(j, now)
        ]

        # An empty due-scan can only end the tick when no deferral episode is
        # in progress: the recovery log (below) must still fire on a quiet
        # tick, otherwise an episode that ends during a lull is never closed.
        if not due and not self._admission_deferring:
            return

        # While host memory is CRITICAL, defer this tick's ``every``/``at``
        # firings (verdict computed off-loop). Deferral is stateless: those
        # kinds stay due, so they fire on the first admitted tick. A
        # cron-expression job is due only in its minute and would be lost, so
        # it runs anyway. Manual runs skip this scan. Fails open on unknown
        # posture; the INFO log fires once per episode, then every 15 minutes.
        decision = await asyncio.to_thread(seams.admission_check)

        # The await yielded the loop: a manual run may have claimed or finished
        # a job, and a job may have been edited or disabled. Rebuild the due
        # list from the LIVE jobs and re-check before the deferral split, so a
        # stale schedule kind or definition is never used and a finished manual
        # run is not fired twice. Reuse the scan-time ``now`` so a minute
        # boundary crossed during the await cannot drop a due cron occurrence.
        live_by_id = {j.id: j for j in self._jobs if j.enabled}
        due = [
            live_by_id[j.id]
            for j in due
            if j.id in live_by_id
            and j.id not in self._claims
            and j.id not in self._pending_removals
            and self._is_due(live_by_id[j.id], now)
        ]

        if not decision.admitted:
            deferred = [j for j in due if j.schedule.kind != "cron"]
            due = [j for j in due if j.schedule.kind == "cron"]
            now_mono = seams.time.monotonic()
            if not self._admission_deferring:
                self._admission_deferring = True
                self._admission_last_log = now_mono
                logger.info(
                    "Cron: deferring interval/one-shot firings (%d deferred "
                    "this tick; cron-expression jobs run normally) — %s "
                    "(re-logged every 15 min while the episode lasts)",
                    len(deferred),
                    decision.reason,
                )
            elif now_mono - self._admission_last_log >= 900.0:
                self._admission_last_log = now_mono
                logger.info(
                    "Cron: STILL deferring interval/one-shot firings (%d "
                    "deferred this tick) — %s",
                    len(deferred),
                    decision.reason,
                )
            else:
                logger.debug("Cron: still deferring %d scheduled job(s)", len(deferred))
        elif self._admission_deferring:
            self._admission_deferring = False
            logger.info("Cron: memory posture recovered — resuming scheduled firings")

        if not due:
            return

        # Fire each job independently — one hung job never blocks others.
        # The claim taken here is handed to the task rather than read back
        # by it: see _run_job_isolated.
        for j in due:
            claim = self._claim_run(j.id, "scheduled")
            claim.task = asyncio.create_task(self._run_job_isolated(j, claim))
    finally:
        self._on_timer_running = False

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

from kiro_crew import shutdown_event
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
    # Re-arming creates/cancels asyncio tasks, which is only legal on the
    # event loop thread. When this is reached OFF the loop — a locked core
    # running in an asyncio.to_thread worker whose _sync()->_load() wants to
    # re-arm, an app-hook/SDK mutation offloaded via asyncio.to_thread, or a
    # purely synchronous (CLI/test) context — there is no running loop here.
    # Creating a task would raise RuntimeError, and a blind cancel could
    # stop the existing timer WITHOUT rearming it, silently halting every
    # scheduled job. So off-loop we cancel/create nothing and instead hand
    # the arm back to the bound event loop (captured in create()/start())
    # via loop.call_soon_threadsafe(self._arm_timer): the arm then runs ON
    # the loop and (re)arms for real. Arming is thus owned by the service —
    # no caller has to remember a drain step. In a genuinely loop-less
    # process (self._loop is None) there is no scheduler to arm.
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
    # Never cancel the timer task if we ARE that task. The tick's own
    # `finally` re-arms while the tick coroutine is still executing, so a
    # blind `self._timer_task.cancel()` there fires a CancelledError back
    # into the running tick — aborting the in-flight `_on_timer` dispatch
    # (dropping any due jobs not yet spawned) and leaving a half-processed
    # sweep. We skip the cancel in that self-referential case and simply
    # create the replacement task below; the finishing tick exits normally.
    # A *different* caller rescheduling while the tick merely waits on
    # shutdown_event still cancels correctly (current is not the timer task).
    #
    # A DIFFERENT hazard, same root cause, needs a second guard: a job's
    # own completion handler calls _arm_timer() (see _run_job_isolated) to
    # re-arm promptly instead of waiting out the rest of the poll cap, but
    # that call runs on the JOB's task, not the timer's — so `current is
    # not self._timer_task` above is true even while _on_timer is still
    # mid-sweep (yielded at its own to_thread scan). Cancelling here would
    # abort that sweep exactly as the self-referential case above
    # describes, just reached from a different caller. Neither cancelling
    # NOR creating a replacement task is safe in that window (creating one
    # too would leave two timer tasks alive and double-fire the next
    # tick), so this arm is dropped entirely: _on_timer's own tick already
    # unconditionally re-arms in its `finally` once the sweep completes
    # (by which point the completed job has released its claim),
    # so the delay this caller wanted still gets picked up, just a moment
    # later rather than being computed twice.
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

    The timer tick's blocking work — the bounded ``_file_lock`` spin, the
    ``_sync()`` that ``read_bytes()`` + blake2b-hashes the WHOLE
    ``crons.json``, and the deferred one-shot delete+save — runs here so it
    can be offloaded off the event loop via ``asyncio.to_thread`` (see
    :meth:`_on_timer`). Returns a snapshot of the current jobs; the loop
    then runs the mutation-free, claim-aware due-scan against it.

    Drains deferred removals BEFORE snapshotting so a completed
    ``delete_after_run`` job whose immediate removal hit a busy store (see
    :meth:`defer_removal`) is deleted here and can never appear due — even
    though the ``_sync`` above may have re-derived it as enabled. If the
    store is too contended to lock this tick, degrades to the in-memory
    snapshot without draining (the next tick retries; ``defer_removal``'s
    in-memory disable keeps a completed one-shot from re-firing meanwhile).
    A worker-thread ``_sync()`` reload may reach ``_arm_timer``, which hands
    the (re)arm back to the bound event loop thread-safely (see
    :meth:`_arm_timer`) — no caller-side drain is required.
    """
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

        # Posture-gated admission: while host memory is CRITICAL, defer this
        # tick's ``every``/``at`` firings instead of admitting more work onto
        # a host that cannot absorb it. The verdict is computed off-loop
        # (config + procfs reads must not stall the event loop). Deferral is
        # deliberately STATELESS and only applies to schedule kinds that stay
        # due on their own (``last_run_ts`` untouched, so a deferred job fires
        # on the first admitted tick). A cron-expression job is only due while
        # its expression matches the current minute, so it cannot be deferred
        # statelessly: an in-memory catch-up marker loses the occurrence on
        # gateway restart, and dropping it silently loses the occurrence
        # outright — so cron-expression jobs run normally even under critical
        # posture (persisted deferral markers are a possible follow-up).
        # Manual runs (run_job / cron_trigger) never pass through this scan
        # and are not deferred. Fails open on unknown posture. The INFO log
        # fires once per deferral episode and re-fires every 15 minutes so a
        # long suspension stays diagnosable.
        decision = await asyncio.to_thread(seams.admission_check)

        # The admission await yielded the loop, so the due snapshot may be
        # stale: a manual run (run_job / cron_trigger) can have claimed — or
        # even completed — a job meanwhile, and a job can have been edited,
        # disabled, or queued for removal. Rebuild the due list from the LIVE
        # job objects and re-run the due check BEFORE the deferral partition
        # below: classifying by the stale snapshot's schedule kind would let
        # an interval job edited into a matching cron expression during the
        # await be deferred-and-dropped (its occurrence lost), and dispatching
        # the snapshot object would execute a stale definition. An id-only
        # check would double-fire a job whose manual run already finished.
        # The re-check deliberately reuses the scan-time ``now``: a live
        # ``last_run_ts`` advanced by a finished manual run still fails it
        # (interval math for ``every``/``at``, the same-minute guard for cron
        # expressions), while a minute boundary crossed during the await
        # cannot drop a cron-expression occurrence that was genuinely due at
        # scan time.
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

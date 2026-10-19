"""Run one cron job: its jitter wait, its deadline, the callback, and the merge of its result.

The deadline is the job's wake budget plus the pool-queue, gate and vet
allowances; a run that outlives it is cancelled and recorded as timed out.

Each function here is a :class:`~kiro_crew.cron.CronService` member: the class binds
it by name, so it reads and writes the service's own state on the instance, and a
patch of the member on the class or on an instance reaches every caller. A name
tests patch on :mod:`kiro_crew.cron`, or one that module defines, is read through
it on each call (``seams``).

Instance fields written: ``_history``, ``_timeout_alerts``.

Instance fields read: ``_dir``, ``_on_job``, ``_push_refresh``, ``_running``, ``_runs``,
``_timeout_alert``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew import cron_inflight
from kiro_crew.cron_history import CronRunRecord
from kiro_crew.cron_service.claims import _RunClaim
from kiro_crew.cron_service.execution import close_run, interruption_note, run_parks_one_shot
from kiro_crew.cron_service.model import CronJob
from kiro_crew.cron_service.store import CronStoreBusy
from kiro_crew.metrics.events import CRON_FIRES

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


async def _run_job_isolated(self: CronService, job: CronJob, claim: _RunClaim) -> None:
    """Execute a single job and merge results back to disk.

    ``claim`` is the claim the dispatcher took for this run -- the object it
    stored in ``_claims`` and tracked this task on (``_on_timer``,
    ``_run_claimed_manual``) -- handed in rather than read back here. This
    first step runs at least one loop iteration after ``create_task``, and
    a ``cancel()`` in that gap takes the claim, marks the cancellation for
    it, and awaits the process kill BEFORE it cancels the task, so the task
    starts inside ``cancel()``: a claim read back from the store there would
    be one this run does not own, the finally's flag read would miss,
    and the run would be filed as a failure beside the cancelled row
    ``cancel()`` writes.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    if not self._runs.holds(job.id, claim):
        # Taken before this run's first step: cancel() took the claim,
        # wrote the run's terminal row and is about to cancel this task.
        # Run nothing -- a stamp set now would sit on a claim that is no
        # longer this run's to release, and that finally would double the
        # row already written. The flags cancel() and the reaper set live
        # on this claim and go with it.
        return
    # This run's generation, drawn while it verifiably holds the claim;
    # the finally stamps it on the record it merges (see CronJob).
    claim.generation = self._runs.next_generation(job)
    started_at = claim.claimed_at
    trigger = claim.trigger
    # Provisional; refined once the jitter sleep completes. A run stop()
    # cancels during jitter still reaches the history path, and its
    # duration is then measured from this stamp.
    exec_started_at = started_at
    # The last completed run's outcome, which a run stop() cancels hands
    # back to the job (close_run) instead of a reset or half-written one.
    prior_status, prior_error = job.last_status, job.last_error
    being_cancelled = False
    marker_write: "asyncio.Future[None] | None" = None
    # Everything the finally below releases is claimed INSIDE the try. The
    # caller (_on_timer, run_job) has already stored the claim and tracked
    # this task on it, and the timer path never awaits the task, so that
    # finally is the only release the claim ever gets. Bookkeeping ahead of
    # the try -- the generation, the fire counter -- is outside that
    # protection: an exception there ends the task with the claim still
    # stored and nothing on this path left to release it; the job then
    # reads as running until the reaper sweep, the manual-run route or
    # cancel() meets the finished task (discard_finished_run), and every
    # scheduled fire and every manual run in between is skipped or refused
    # with 409.
    try:
        # Stamped here rather than derived from claimed_at: the two clocks
        # share no epoch, so the reaper's deadline is only meaningful
        # against a stamp taken on its own clock.
        claim.started_monotonic = seams.time.monotonic()
        # The object this run executes on, kept on the claim for the reaper:
        # a store reload replaces the registry's job objects, and the marker
        # that says the payload never started stays on this one.
        claim.job = job
        # The pause this run starts from, carried on its record: the merge
        # and the reaper compare it with the store's, so a resume issued
        # while the run is in flight is not written back over.
        job.auto_paused_at_start = job.auto_paused
        # One increment per execution, before the jitter sleep so a run
        # cancelled during jitter still counts as fired. ``kind`` is the
        # dispatch shape -- ``script`` and ``command`` bypass the model
        # entirely, so this is the split between jobs that cost tokens and
        # jobs that cost none.
        if job.script:
            kind = "script"
        elif job.command:
            kind = "command"
        else:
            kind = "agent"
        seams.emit_counter(CRON_FIRES, {"kind": kind, "trigger": trigger})
        # Apply jitter to spread execution unless strict_schedule is set or manual
        jitter = self._compute_jitter(job) if trigger != "manual" else 0
        claim.jitter = jitter
        # ``last_result`` is a cross-run context-carry field for AGENT jobs
        # (see build_cron_session_context): result-less runs leave the
        # previous value in place so the next run's prompt keeps its dedup
        # context. Command and script jobs have theirs cleared once in the
        # finally below, because the prompt built for them is never
        # dispatched. The history recorder in the finally block must NOT
        # attribute that carried-over value to THIS run, so clear the
        # freshness marker here; executor callbacks set it via
        # CronJob.set_run_result() when the run actually produces a result.
        # (String identity/equality can't stand in for the marker: CPython
        # interns equal literals and caches single-char strings, so a run
        # re-producing the previous text looks identical to one that
        # produced nothing.)
        job.result_produced = False
        # The jitter sleep MUST live inside this try: hourly/daily jobs
        # sleep up to 59 min here, and a user cancel() during that window
        # raises CancelledError at the sleep — if that happened BEFORE the
        # try, the finally below would never run, leaking the rest of this
        # run's bookkeeping (the in-flight marker, the claim release).
        if jitter > 0:
            logger.debug("Cron: applying %.0fs jitter to job '%s'", jitter, job.name)
            await seams._sleep_out_jitter(jitter)
        exec_started_at = seams.time.time()
        # The record a hard exit leaves behind. Every other trace of this
        # run (last_run_ts, the history row, status) is written in the
        # finally below, which an os._exit from the loop-stall watchdog
        # never reaches -- so without this file the store would show the
        # job as never fired, it would be due again on the next boot, and
        # nothing could say which job the dying gateway was running. Off
        # the loop like every other write on this path; best-effort. The
        # write is kept as a task so the finally below can wait for it: a
        # cancellation that lands mid-write must not let clear_marker run
        # before the worker publishes, or the marker it leaves behind would
        # read as an abandoned run on the next boot.
        # The in-flight marker is one file per RUN, named by the claim's
        # token, so the clear in the finally can only remove this run's own
        # marker -- never the one a replacement run wrote while this run's
        # cancellation was still unwinding (see cron_inflight.clear_marker).
        marker_write = asyncio.ensure_future(
            asyncio.to_thread(
                cron_inflight.write_marker,
                self._dir,
                job.id,
                job.name,
                exec_started_at,
                run=claim.marker_run,
            )
        )
        await asyncio.shield(marker_write)
        # Notify dashboard that the job has started executing so the live
        # is_running badge appears without a manual reload.
        try:
            if self._push_refresh:
                self._push_refresh("crons")
        except Exception:
            logger.debug("push_refresh failed on job start", exc_info=True)
        await self._execute_with_timeout(job, claim)
    except asyncio.CancelledError:
        # stop() cancels this task WITHOUT setting claim.cancelled, so the
        # finally must know not to clear the last completed run's result.
        being_cancelled = True
        raise
    finally:
        finished_at = seams.time.time()
        # The run ended by a path that runs finally, so it is not in
        # flight whatever its outcome. A marker that survives this is what a
        # hard exit looks like, so clear it first and unconditionally --
        # after the write that may still be publishing it, or a
        # cancellation mid-write would unlink nothing and leave the marker.
        try:
            if marker_write is not None and not marker_write.done():
                await asyncio.shield(marker_write)
        except (asyncio.CancelledError, Exception):
            pass  # the write is best-effort; the clear below still runs
        try:
            # This run's file only (the token is in its name): a replacement
            # run accepted while this cancellation was unwinding -- a whole
            # session teardown for an agent job -- has its own marker, and
            # a clear by job id would have taken it, leaving a hard exit
            # during that run with no evidence for the breaker.
            await asyncio.to_thread(cron_inflight.clear_marker, self._dir, job.id, claim.marker_run)
        except Exception:
            logger.debug("in-flight marker not cleared for %s", job.id, exc_info=True)
        # Read only THIS run's flags, off its own claim. cancel() and the
        # reaper flag the claim they took; a marker keyed by job id alone
        # would let this finalizer, stalled in the clear above while a
        # replacement run was accepted and cancelled, eat that run's
        # marker -- its finalizer then finds none and appends a failure
        # row after the cancelled row cancel() already wrote.
        reaped = claim.reaped
        cancelled = claim.cancelled
        # This run's terminal record, taken BEFORE the release below. The
        # job object is shared by every run of the job, and the release
        # lets a replacement start while the merge and history append are
        # still pending: that run's `_execute` resets last_status and the
        # result-produced flag on the same object, so a record read after
        # the release would file this run as a failure with no summary and
        # persist the replacement's blank status as this run's. The every-
        # job stamp move and the carried-result clear are this run's own
        # terminal writes, so they land here too, inside the claim.
        terminal: CronJob | None = None
        status = ""
        run_result: str | None = None
        if not reaped and not cancelled:
            terminal, status, run_result = close_run(
                job,
                started_at=started_at,
                generation=claim.generation,
                being_cancelled=being_cancelled,
                prior_status=prior_status,
                prior_error=prior_error,
                dispatched=claim.outcome_reset,
            )
        # Release the claim only while it is still THIS run's (identity,
        # see RunClaims.holds). cancel() and the reaper take the run they
        # found before anything else can claim the job, and the marker
        # clear above is a full executor round trip: a manual Run accepted
        # in that gap holds a claim of its own that is not this run's to
        # remove. Popping it anyway drops that run at its own claim re-check
        # after the route answered "started", or, past the re-check, leaves
        # it running with Cancel answering 409 and a further Run accepted
        # beside it. A run that is still the claim holder (a normal
        # completion, or stop()'s cancel, which takes nothing) releases
        # everything here -- the one pop covers every field.
        # Hold this run's record BEFORE the release below. Released, the job
        # is free for the next tick's due check, and a reload a worker thread
        # finished meanwhile has listed the store's copy, which never got
        # this run: held first, every reload applies it, so the listed job
        # is either claimed or carries the run (see _hold_run_record).
        if terminal is not None:
            self._hold_run_record(job, terminal)
        self._runs.release(job.id, claim)
        # Notify dashboard that the job has finished (clears the badge).
        try:
            if self._push_refresh:
                self._push_refresh("crons")
        except Exception:
            logger.debug("push_refresh failed on job end", exc_info=True)
        if terminal is not None:
            try:
                # Offload the lock+sync+save merge to a worker thread:
                # _merge_job_result enters the bounded sync _file_lock,
                # whose spin does time.sleep(poll) for up to
                # _FILE_LOCK_TIMEOUT_SECS under contention. Calling it
                # directly here — on the gateway event loop, since
                # _run_job_isolated is a loop task — would park the whole
                # loop (chat, heartbeat, timer) for that window. to_thread
                # is safe for the same reason the batch-remove path uses it
                # (flock on separate fds mutually excludes in-process too,
                # and the self._jobs reassignment is an atomic reference
                # swap). CronStoreBusy (a TimeoutError) on sustained
                # contention is caught below and logged — the merge is
                # best-effort and the next run / reaper re-persists.
                await asyncio.to_thread(self._merge_job_result, terminal)
            except CronStoreBusy as exc:
                # The lock was never won, so nothing was merged and the
                # one-shot consume did not run. A delete_after_run at-job
                # is still enabled (the consume is what stops it), so
                # without the deferral it is due again on the next tick.
                # Same owed rule as the consume in _merge_job_result. A
                # FAILED run owes a park, not a delete: its auto-pause is in
                # the run record held above, which every reload re-applies
                # and the next save under the lock persists.
                logger.warning("Result merge for job '%s' skipped: %s", job.name, exc)
                if (
                    terminal.delete_after_run
                    and terminal.last_status != "error"
                    and not (terminal.fire_time_denied or terminal.run_never_started)
                ):
                    self.defer_removal(terminal.id)
            except Exception:
                logger.exception("Failed to merge result for job '%s'", job.name)
            # Record history
            try:
                # The row's cause is this run's own. A stopped run with no
                # outcome carries the previous run's last_error (close_run),
                # so it gets the interruption note instead.
                row_error = terminal.last_error or ""
                if being_cancelled and status == "cancelled":
                    row_error = interruption_note(dispatched=claim.outcome_reset)
                record = CronRunRecord(
                    job_id=job.id,
                    trigger=trigger,
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=int((finished_at - exec_started_at) * 1000),
                    status=status,
                    # Uncut on purpose: CronHistoryStore.append is the one
                    # truncation site, applying the configured cap through
                    # truncate_summary, which keeps URLs and the outcome
                    # line. A slice here would cut ahead of both.
                    summary=run_result or row_error,
                    trace=run_result or "",
                    error=row_error,
                )
                await self._history.append(record)
                if self._push_refresh:
                    self._push_refresh("cron_history")
            except Exception:
                logger.exception("Failed to record history for job '%s'", job.name)
        # Re-arm now rather than waiting for whatever wake was already
        # armed: a job that ran for most of its interval was invisible to
        # every _next_wake_secs() computed while self._claims held it
        # (see _next_wake_secs), so the armed delay can be stale by up to
        # _TIMER_POLL_SECS by the time this job becomes due again. Placed
        # at the very end, after last_run_ts/history are settled, so the
        # delay this computes reflects this run's actual outcome. Safe to
        # call unconditionally (also when reaped/cancelled, or when
        # nothing changed): _arm_timer() itself no-ops when the service
        # isn't running, and the self._on_timer_running guard there
        # covers the one case where this job's own completion happens to
        # race an in-flight dispatch sweep.
        if self._running:
            self._arm_timer()


async def _execute_with_timeout(
    self: CronService, job: CronJob, claim: _RunClaim | None = None
) -> None:
    """Execute a job with a timeout guard.

    ``claim`` is the run's own claim, handed down so ``_execute`` can ask
    whether THIS run was cancelled (the markers are keyed by run, and
    ``cancel()`` has taken the stored claim by the time that question is
    asked); a direct call without one matches no marker.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    timeout = seams.effective_wake_budget(job)
    # The cron pool's QUEUE WAIT happens inside this deadline, so the wake
    # budget has to cover it as well as the execution.  Excluding queue wait
    # from the per-call `timeout=` kwarg (see run_in_cron_pool) is not enough
    # on its own: without the term below, a job still sitting in the pool
    # queue is killed here and reported as an execution overrun, which is the
    # exact misdiagnosis this whole change exists to remove.  Worse, a thread
    # cannot be interrupted -- so if a worker claimed the call as this
    # deadline fired, the subprocess runs on while the overlap guards clear
    # and the next wake duplicates its side effects.  That is the hazard
    # _SUBPROC_CLEANUP_ALLOWANCE_SECS was written for, and the queue wait is
    # a second term it never accounted for, and the fire-time gate's own bound
    # is a third -- it is awaited before the dispatch and inside this same
    # deadline, so _gate_budget_allowance covers it.  The CLAIM-time vet is a
    # fourth: the same vet again, inside the worker, ahead of the subprocess
    # it authorises -- so _vet_allowance covers it, and without that term a
    # widened inner backstop in the gateway is simply pre-empted here.
    # All three allowances are
    # shared with the reaper so the two deadlines cannot drift and pre-empt
    # one another.  Only command/script jobs go through the pool, so a
    # message job's budget is left exactly as set.
    deadline = (
        timeout
        + seams._pool_queue_allowance(job)
        + seams._gate_budget_allowance(job)
        + seams._vet_allowance(job)
    )
    # Fresh run: no failure counted yet. The timeout handler below reads
    # this to avoid double-counting a run that already recorded its
    # failure and then overran the deadline during cleanup.
    job.failure_recorded = False
    try:
        await asyncio.wait_for(self._execute(job, claim), timeout=deadline)
    except asyncio.TimeoutError:
        # NB: Timeout bypasses _cron_callback's except block entirely, and
        # with it every failure alert that block sends; _alert_timeout below
        # is the one that reports this run.
        # Clear failure dedup state so a subsequent real error isn't
        # suppressed as a dup of the pre-timeout failure, and count the
        # timeout toward the auto-pause threshold for a run that actually
        # DISPATCHED: a job that times out on every run must eventually
        # auto-pause instead of running forever with zero user signal.
        job.last_status = "error"
        job.last_error = f"Timed out after {deadline}s"
        job.last_run_ts = seams.time.time()
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        # Skip the count when this run already recorded its failure (a
        # delivery-path exception followed by cleanup overrunning the
        # deadline): one failed run is one failure, whichever handler
        # observes it last.
        #
        # Skip it too when the payload NEVER STARTED. A stall that pushes
        # wall clock past the wake deadline while the fire-time gate is
        # still awaited cancels this coroutine AT that await, so no handler
        # inside the gate runs and the marker set before it survives -- and
        # the timeout lands here on a run that dispatched nothing. Counting
        # it would auto-pause at _AUTO_PAUSE_THRESHOLD, and a paused job
        # never fires again, so repeated event-loop saturation durably
        # disables a job that has not run a line. This is the same
        # discriminator the starvation, gate-deny and vet-overrun paths
        # already use: a state that PREVENTED the run is not a defect OF
        # the run. A genuine execution overrun still counts, because
        # _execute resets the marker to False before invoking the callback.
        counted = not job.failure_recorded and not job.run_never_started
        if counted:
            job.record_failure()
        # The deadline cancelled _execute before its one-shot disable, so it
        # is done here (the result merge persists it). A delete_after_run
        # one-shot is a failed run: the finally below auto-pauses it.
        if run_parks_one_shot(job):
            job.enabled = False
        logger.error("Cron job '%s' timed out after %ds", job.name, deadline)
        # Alerted on the count's own terms: a run that already recorded its
        # failure was reported by the handler that recorded it, and a run whose
        # payload never started is not a failure of the job.
        if counted:
            self._alert_timeout(job, job.last_error)
    finally:
        # A one-shot whose run FAILED (error or timeout) is auto-paused, not
        # consumed: the pause is execution-owned, so apply_run_record persists
        # it under the generation fence and the doctor reports it. In a finally
        # so a cancel after the failure still parks it. A denied or
        # never-started run is not a failed run and keeps its own handling.
        if job.delete_after_run and job.last_status == "error" and not job.auto_paused:
            if not (job.fire_time_denied or job.run_never_started):
                job.enabled, job.auto_paused = False, True
                job._audit_pause_change("auto_paused")


def _alert_timeout(self: CronService, job: CronJob, detail: str) -> None:
    """Start the timeout alert for *job*, whose deadline ended its run, and return.

    Both arms that end a run on its deadline call this: the ``wait_for`` here
    and the reaper's force-reap. The alert is the gateway's (see
    ``set_timeout_alert``): it dedups, honours ``silent`` and reaches the bell
    and the job's Slack or channel destination. It runs as its own task, never
    awaited, so a slow delivery cannot hold the run's claim past its deadline
    and into the reaper's. No alert set (the CLI, MCP, tests) is a no-op.
    """
    alert = self._timeout_alert
    if alert is None:
        return
    try:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(alert(job, detail))
    except Exception:
        logger.warning("Cron '%s': could not start the timeout alert", job.name, exc_info=True)
        return
    self._timeout_alerts.add(task)
    task.add_done_callback(self._timeout_alerts.discard)


async def _execute(self: CronService, job: CronJob, claim: _RunClaim | None = None) -> None:
    """Run the job callback and update runtime fields (last_run_ts, last_status).

    ``claim`` is this run's claim (see ``_execute_with_timeout``).
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    logger.info("Cron: executing '%s' (%s)", job.name, job.id)
    # Reset status for this run so a prior run's "error" can't leak into an
    # "ok" decision below, and its error text can't be read as this run's.
    # Same for the fire-time denial marker.
    job.last_status = None
    job.last_error = None
    if claim is not None:
        claim.outcome_reset = True
    job.fire_time_denied = False
    job.run_never_started = False
    job.refusal_cannot_clear = False
    # Transient retries the callback took this run. The gateway callback only
    # INCREMENTS `_transient_attempts` (a runtime attribute on the live job);
    # this method is the one owner of reading it, clearing it and persisting
    # it -- see the stamp after `last_run_ts` below. The read-and-clear sits
    # in a `finally` so a cancelled or timed-out run (CancelledError skips
    # everything after the await) cannot leak a half-spent budget into the
    # next run's retry allowance.
    retries = 0
    try:
        if self._on_job:
            try:
                await self._on_job(job)
            finally:
                retries = int(getattr(job, "_transient_attempts", 0) or 0)
                job._transient_attempts = 0  # type: ignore[attr-defined]
        # Only mark "ok" if the callback did not itself report failure. The
        # command/script paths return NORMALLY and signal failure by mutating
        # the shared job (last_status="error"); only the LLM path raises.
        # Overwriting unconditionally with "ok" destroyed that error before
        # the history recorder and _merge_job_result read it, mis-reporting
        # failed command/script runs as successful on the dashboard and in
        # cron_list.
        if job.last_status != "error":
            job.last_status = "ok"
            job.last_error = None
            # Reset the auto-pause budget: without this, CronService-run
            # jobs count failures monotonically (record_failure fires on
            # the error/timeout paths but nothing ever reset the counter
            # here), so any job accumulating _AUTO_PAUSE_THRESHOLD
            # transient failures over its LIFETIME — successes in
            # between notwithstanding — silently auto-paused. Guarded by
            # the "error" check above so the deliberately-neutral paths
            # (governance/fire-time denials, which set last_status =
            # "error" without counting a failure) stay neutral: a policy
            # denial neither spends nor refills the budget. Callback
            # paths that already called record_success() are unaffected
            # (resetting 0 to 0 is idempotent). The claim.cancelled
            # check closes a cancel race: cancel() kills the sandboxed
            # subprocess BEFORE task.cancel(), and the gateway's
            # cancelled branch returns None without setting last_status,
            # so a callback returning in that window would otherwise
            # reach this branch — and cancel() documents that it leaves
            # consecutive_failures untouched. Read off THIS run's claim:
            # cancel() has taken the stored claim by now, and a flag set
            # on another run of the job is not this run's.
            if claim is None or not claim.cancelled:
                job.record_success()
    except Exception as exc:
        job.last_status = "error"
        job.last_error = str(exc)
        logger.error("Cron job '%s' failed: %s", job.name, exc)

    job.last_run_ts = seams.time.time()
    # Retry telemetry is stamped HERE, with the `last_run_ts` it describes,
    # so the two can never disagree for a run that completed. Stamping it
    # anywhere inside the callback would bind it to the PREVIOUS run's
    # `last_run_ts` (this line has not run yet while the callback is
    # executing), and the Schedule page -- which shows the count only when
    # the two stamps match -- would never show it. A cancelled run never
    # reaches this line, so its own `last_run_ts` write leaves the pair
    # mismatched and the page shows no count rather than a stale one.
    job.last_retry_count = retries
    job.last_retry_run_ts = job.last_run_ts

    # One-shot "at" jobs: disable after the run. A fire-time-DENIED at-job
    # is disabled too — its due time has passed, so leaving it enabled
    # would make it due on EVERY timer tick (a zero-delay refire loop that
    # floods audit/history until resource exhaustion). Parking it disabled
    # (instead of deleting — including the delete_after_run shape, which
    # the merge below retains) keeps it discoverable so an operator can
    # re-enable it after a policy loosening. Recurring jobs are untouched:
    # they simply wait for their next scheduled slot and resume on their
    # own when policy loosens. A fire that never started is not parked
    # (see run_parks_one_shot): nothing ran, so it stays due and retries.
    if run_parks_one_shot(job):
        job.enabled = False

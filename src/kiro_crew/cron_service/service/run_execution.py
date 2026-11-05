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
from kiro_crew.cron_service.execution import (
    auto_pause_failed_one_shot,
    close_run,
    interruption_note,
    run_parks_one_shot,
)
from kiro_crew.cron_service.model import CronJob
from kiro_crew.cron_service.store import CronStoreBusy
from kiro_crew.metrics.events import CRON_FIRES

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


async def _run_job_isolated(self: CronService, job: CronJob, claim: _RunClaim) -> None:
    """Execute a single job and merge results back to disk.

    ``claim`` is the claim the dispatcher stored and tracked this task on, handed
    in rather than read back: a ``cancel()`` between ``create_task`` and this
    first step takes the stored claim, so a claim read back here would not be
    this run's, and the run would be filed as a failure beside the cancelled row.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    if not self._runs.holds(job.id, claim):
        # cancel() took the claim and wrote the terminal row before this
        # first step. Run nothing, or the finally would write a second row.
        return
    # This run's generation, drawn while it verifiably holds the claim;
    # the finally stamps it on the record it merges (see CronJob).
    claim.generation = self._runs.next_generation(job)
    started_at = claim.claimed_at
    trigger = claim.trigger
    # Provisional; refined after the jitter sleep. A run cancelled during
    # jitter measures its duration from this stamp.
    exec_started_at = started_at
    # The last completed run's outcome, which a stopped run hands back (close_run).
    prior_status, prior_error = job.last_status, job.last_error
    being_cancelled = False
    marker_write: "asyncio.Future[None] | None" = None
    # The finally below is the claim's only release (the timer path never
    # awaits this task). An exception raised before the try leaves the claim
    # stored, and the job reads as running until something meets the finished
    # task (discard_finished_run).
    try:
        # A monotonic stamp, not claimed_at: the reaper's deadline needs its own clock.
        claim.started_monotonic = seams.time.monotonic()
        # Kept for the reaper: a store reload replaces the registry's job
        # objects, and the never-started marker stays on this one.
        claim.job = job
        # Compared with the store's by the merge and the reaper, so a resume
        # issued mid-run is not written back over.
        job.auto_paused_at_start = job.auto_paused
        # Counted before the jitter sleep, so a run cancelled in jitter still
        # counts. ``script`` and ``command`` cost no tokens; ``agent`` does.
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
        # An agent job's ``last_result`` carries over result-less runs as the
        # next prompt's context (cron_prior_result_context), so history must
        # not file it as this run's. CronJob.set_run_result() sets the flag
        # when a result is produced; comparing strings cannot, because a run
        # may reproduce the previous text.
        job.result_produced = False
        # Inside the try: a cancel() during a jitter sleep of up to 59 min
        # must still reach the finally.
        if jitter > 0:
            logger.debug("Cron: applying %.0fs jitter to job '%s'", jitter, job.name)
            await seams._sleep_out_jitter(jitter)
        exec_started_at = seams.time.time()
        # The record a hard exit leaves behind: an os._exit from the loop-stall
        # watchdog skips the finally, so without it the job would look never
        # fired and nothing could say what the gateway was running. Kept as a
        # task so the finally can wait for it before clearing. One file per run,
        # named by the claim's token, so the clear never removes a replacement
        # run's marker (cron_inflight.clear_marker).
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
        # Shows the dashboard's running badge without a reload.
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
        # Only a hard exit may leave the marker, so clear it first, after the
        # write that may still be publishing it.
        try:
            if marker_write is not None and not marker_write.done():
                await asyncio.shield(marker_write)
        except (asyncio.CancelledError, Exception):
            pass  # the write is best-effort; the clear below still runs
        try:
            # This run's file only: a replacement run accepted while this one
            # unwound has its own marker, which the breaker needs.
            await asyncio.to_thread(cron_inflight.clear_marker, self._dir, job.id, claim.marker_run)
        except Exception:
            logger.debug("in-flight marker not cleared for %s", job.id, exc_info=True)
        # Off this run's own claim: cancel() and the reaper flag the claim
        # they took, never another run's.
        reaped = claim.reaped
        cancelled = claim.cancelled
        # Taken BEFORE the release: the job object is shared, and a
        # replacement run started after the release resets last_status and
        # the result flag on it. This run's own terminal writes land here too.
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
        # The release removes the claim only while it is still this run's
        # (RunClaims.holds): a manual Run accepted after cancel() or the reaper
        # took this one holds a claim of its own.
        # The record is held BEFORE the release, so every store reload applies
        # it: the listed job is either claimed or carries the run (_hold_run_record).
        if terminal is not None:
            self._hold_run_record(job, terminal)
        self._runs.release(job.id, claim)
        try:
            if self._push_refresh:
                self._push_refresh("crons")
        except Exception:
            logger.debug("push_refresh failed on job end", exc_info=True)
        if terminal is not None:
            try:
                # In a thread: _file_lock sleeps for up to _FILE_LOCK_TIMEOUT_SECS
                # under contention, which would park the gateway loop. flock on
                # separate fds also excludes in-process, and the self._jobs swap
                # is atomic. CronStoreBusy is logged below; the next run re-persists.
                await asyncio.to_thread(self._merge_job_result, terminal)
            except CronStoreBusy as exc:
                # Nothing merged, so a delete_after_run at-job is still enabled
                # and would be due on the next tick: defer its removal (same
                # rule as _merge_job_result). A FAILED run is parked instead,
                # by the auto-pause in the held run record.
                logger.warning("Result merge for job '%s' skipped: %s", job.name, exc)
                if (
                    terminal.delete_after_run
                    and terminal.last_status != "error"
                    and not (terminal.fire_time_denied or terminal.run_never_started)
                ):
                    self.defer_removal(terminal.id)
            except Exception:
                logger.exception("Failed to merge result for job '%s'", job.name)
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
                    # Uncut: CronHistoryStore.append truncates once, keeping URLs
                    # and the outcome line.
                    summary=run_result or row_error,
                    trace=run_result or "",
                    error=row_error,
                )
                await self._history.append(record)
                if self._push_refresh:
                    self._push_refresh("cron_history")
            except Exception:
                logger.exception("Failed to record history for job '%s'", job.name)
        # Re-arm last, after this run's outcome is settled: _next_wake_secs()
        # skipped this job while it was claimed, so the armed delay may be
        # stale by up to _TIMER_POLL_SECS. _arm_timer() guards a concurrent sweep.
        if self._running:
            self._arm_timer()


async def _execute_with_timeout(
    self: CronService, job: CronJob, claim: _RunClaim | None = None
) -> None:
    """Execute a job with a timeout guard.

    ``claim`` is the run's own claim, so ``_execute`` can ask whether THIS run
    was cancelled after ``cancel()`` took the stored one; without it, no match.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    timeout = seams.effective_wake_budget(job)
    # The deadline also covers the pool queue wait, the fire-time gate and the
    # claim-time vet, all awaited inside it. Without them a queued job is
    # killed as an execution overrun, and a worker thread that already took the
    # call runs on, so the next wake duplicates its side effects. The reaper
    # shares these allowances, so the two deadlines cannot pre-empt each other.
    # Only command/script jobs use the pool; a message job's budget is as set.
    deadline = (
        timeout
        + seams._pool_queue_allowance(job)
        + seams._gate_budget_allowance(job)
        + seams._vet_allowance(job)
    )
    # Read by the timeout handler, so a failure is not counted twice.
    job.failure_recorded = False
    try:
        await asyncio.wait_for(self._execute(job, claim), timeout=deadline)
    except asyncio.TimeoutError:
        # A timeout skips _cron_callback's alerts; _alert_timeout reports it.
        # Dedup state is cleared so a later real error is not dropped, and a
        # dispatched run's timeout counts toward auto-pause.
        job.last_status = "error"
        job.last_error = f"Timed out after {deadline}s"
        job.last_run_ts = seams.time.time()
        job.last_failure_hash = ""
        job.last_failure_at = 0.0
        # Not counted when the run already recorded its failure (one run, one
        # failure), nor when the payload never started: a loop stall during
        # the fire-time gate would otherwise auto-pause a job that ran nothing.
        # _execute clears that marker before the callback, so a real overrun counts.
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
        # A failed one-shot is auto-paused, not consumed; in a finally so a
        # cancel after the failure still parks it (``freeze_terminal_record``
        # applies the same rule on cancel or reap).
        auto_pause_failed_one_shot(job, job.last_status)


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
    # The callback only increments `_transient_attempts`; this method reads,
    # clears and persists it, in a `finally` so a cancelled run cannot leak a
    # half-spent retry budget into the next run.
    retries = 0
    try:
        if self._on_job:
            try:
                await self._on_job(job)
            finally:
                retries = int(getattr(job, "_transient_attempts", 0) or 0)
                job._transient_attempts = 0  # type: ignore[attr-defined]
        # Command/script paths return normally and report failure by setting
        # last_status="error"; only the LLM path raises. Keep that error.
        if job.last_status != "error":
            job.last_status = "ok"
            job.last_error = None
            # A success refills the auto-pause budget, so only consecutive
            # failures pause a job. A policy denial sets "error" and so neither
            # spends nor refills it. A cancelled run is skipped: cancel() kills
            # the subprocess before task.cancel(), so the callback can return
            # normally here, and cancel() leaves consecutive_failures untouched.
            if claim is None or not claim.cancelled:
                job.record_success()
    except Exception as exc:
        job.last_status = "error"
        job.last_error = str(exc)
        logger.error("Cron job '%s' failed: %s", job.name, exc)

    job.last_run_ts = seams.time.time()
    # Stamped with the `last_run_ts` it describes: the Schedule page shows the
    # retry count only when the two match, so a cancelled run shows none.
    job.last_retry_count = retries
    job.last_retry_run_ts = job.last_run_ts

    # A one-shot "at" job is disabled after its run. A fire-time-denied one
    # too, or it would refire on every tick; it is parked, not deleted, so an
    # operator can re-enable it. A fire that never started stays due
    # (run_parks_one_shot).
    if run_parks_one_shot(job):
        job.enabled = False

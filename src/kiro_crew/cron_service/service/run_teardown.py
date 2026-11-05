"""End a cron run that must stop: the reaper's timeout sweep and the user's cancel.

The reaper wakes every ``_REAPER_INTERVAL`` seconds and force-reaps a run that has
outlived its wake budget; :meth:`~kiro_crew.cron.CronService.cancel` ends a run the
user stopped. Both fence the run's session keys, end its sessions through
:mod:`~kiro_crew.cron_service.service.run_kill`, and record the terminal state
(a cancelled one-shot is parked) under the store lock.

Each function here is a :class:`~kiro_crew.cron.CronService` member: the class binds
it by name, so it reads and writes the service's own state on the instance, and a
patch of the member on the class or on an instance reaches every caller. A name
tests patch on :mod:`kiro_crew.cron`, or one that module defines, is read through
it on each call (``seams``).

Instance fields written: ``_history``, ``_reaper_task``, ``_sessions``.

Instance fields read: ``_held_run_records``, ``_held_run_records_guard``,
``_held_settlements``, ``_jobs``, ``_push_refresh``, ``_runs``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import ExitStack
from typing import TYPE_CHECKING

from kiro_crew import cron_script
from kiro_crew.cron_history import CronRunRecord
from kiro_crew.cron_service.claims import _RunClaim
from kiro_crew.cron_service.execution import run_parks_one_shot
from kiro_crew.cron_service.model import _JOB_TIMEOUT_SECS
from kiro_crew.executors import subprocess_executor
from kiro_crew.process_identity import failure_name, with_kill_failure

if TYPE_CHECKING:
    from kiro_crew.cron import CronPauseNotSaved, CronService
    from kiro_crew.session import SessionManager

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


# ── Reaper ──

#: How often the reaper also removes stale per-run transcripts of stateless jobs.
_RESIDUE_PRUNE_INTERVAL_SECS = 3600.0


async def _prune_run_residue(service: CronService, now: float) -> None:
    """Remove stale per-run transcripts off the loop; never fail the reaper."""
    from kiro_crew.cron_service.run_residue import prune_stale_run_transcripts

    active = service.active_session_keys()
    try:
        await asyncio.to_thread(prune_stale_run_transcripts, active_keys=active, now=now)
    except Exception:
        logger.debug("Reaper: cron run transcript prune failed", exc_info=True)


def start_reaper(self: CronService, sessions: SessionManager) -> None:
    """Start the periodic reaper loop.  Call once after the event loop is running."""
    self._sessions = sessions
    if self._reaper_task is None:
        self._reaper_task = asyncio.create_task(self._reaper_loop())


async def _reaper_loop(self: CronService) -> None:
    """Periodically force-kill cron jobs that exceed the timeout.

    Defense-in-depth: catches cases where ``asyncio.wait_for`` in
    ``_execute_with_timeout`` fails to fire (event-loop saturation,
    orphaned tasks).
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    # The first residue pass waits a full interval, so it never adds to the
    # startup burst of a gateway restoring its sessions.
    next_residue_prune = seams.time.monotonic() + _RESIDUE_PRUNE_INTERVAL_SECS
    while True:
        await asyncio.sleep(seams._REAPER_INTERVAL)
        now = seams.time.time()
        now_mono = seams.time.monotonic()
        if now_mono >= next_residue_prune:
            next_residue_prune = now_mono + _RESIDUE_PRUNE_INTERVAL_SECS
            await _prune_run_residue(self, now)
        # Snapshot the job list CACHE-ONLY — no store lock, no _sync, no
        # disk I/O on the loop (same rationale as list_jobs/get_job). The
        # batch-remove worker (remove_jobs → asyncio.to_thread) builds a
        # NEW list and swaps self._jobs by an atomic reference assignment,
        # so this comprehension iterates one coherent list object (either
        # the pre- or post-swap list, never a half-rebuilt one) and can
        # never tear. The reaper only needs the in-memory view to map
        # running task ids → timeouts; cross-process freshness is
        # irrelevant to force-killing a locally-running task.
        jobs_by_id = {j.id: j for j in self._jobs}
        for job_id, claim in list(self._claims.items()):
            # The list is a snapshot for iteration order only. An earlier reap
            # in this sweep awaited, and meanwhile this run can end and a
            # replacement claim the job; the snapshot's stale stamps would put
            # it past its deadline. Reap only the stored claim; a replacement
            # is measured on its own stamps by the next sweep.
            if self._claims.get(job_id) is not claim:
                continue
            # A claim cancel() or an earlier reap has taken is theirs to
            # finish, so a sweep inside their kill awaits never reaps it twice.
            if claim.taken:
                continue
            # A done() task whose claim is still here never reached
            # _run_job_isolated's finally, so the job still reads as running.
            # Release it on THIS sweep: the deadline can be a day away, and
            # every scheduled fire until then would be skipped.
            if self.discard_finished_run(job_id):
                continue
            elapsed = now - claim.claimed_at
            # Decide and report on the monotonic clock, so a backward
            # wall-clock step never logs or persists a negative duration. A
            # claim with no monotonic stamp (a manual run still in its store
            # refresh) falls back to wall-clock elapsed, so reaping never stops.
            started_mono = claim.started_monotonic
            elapsed_mono = now_mono - started_mono if started_mono is not None else elapsed
            job = jobs_by_id.get(job_id)
            deadline = (
                max(min(job.timeout_secs, 86400), seams._JOB_TIMEOUT_SECS)
                if job
                else seams._JOB_TIMEOUT_SECS
            ) + (
                seams._pool_queue_allowance(job)
                + seams._gate_budget_allowance(job)
                + seams._vet_allowance(job)
            )
            jitter_allowance = claim.jitter or 0.0
            if elapsed_mono <= deadline + jitter_allowance:
                continue
            logger.warning(
                "Reaper: cron job %s exceeded %ds (ran %.0fs), force-killing",
                job_id,
                deadline,
                elapsed_mono,
            )
            try:
                # The reap takes the measured claim or nothing, so a claim
                # that changed under this sweep is never killed.
                await self._force_reap(job_id, elapsed_mono, deadline, claim=claim)
            except Exception:
                logger.exception("Reaper: failed to reap cron job %s", job_id)


async def _force_reap(
    self: CronService,
    job_id: str,
    elapsed: float,
    deadline: int = _JOB_TIMEOUT_SECS,
    *,
    claim: _RunClaim,
) -> None:
    """Kill the run ``claim`` stands for: its session process and its task.

    ``claim`` is the run the sweep measured over its deadline. The reap takes
    that claim or nothing (by identity): a replacement claim, a claim that is
    gone, or one already taken by another teardown is not killed, and the
    job's session is left alone. There is no reap by job id alone.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    taken = self._runs.take(job_id, expected=claim)
    if taken is None:
        # Taken by another teardown (a second "Reaped" row would draw a
        # generation that could outrank a replacement run's), gone, or
        # replaced. The sweep skips all three; this covers a store that
        # changed under the sweep's awaits.
        return
    # Flag the run this reap took: its finalizer reads the flag off its
    # own claim, so no other run of the job can see it.
    taken.reaped = True
    reap_started_at = taken.claimed_at
    reap_trigger = taken.trigger
    # The kill, then the finish in a ``finally``: ``taken`` has no other
    # owner-death recovery, so a kill that raised (a cancellation at
    # shutdown) would leave the claim taken for the life of the gateway and
    # every manual run would 409. ``_sigkill_session`` REPORTS what it could
    # not kill instead of raising, so a live process group is never recorded
    # as reaped: a raised or reported failure goes into the terminal record
    # (the run's finally writes none for a taken run), is audited as
    # ``failed``, and a raised one is re-raised after the record.
    #
    # EVERY key the run registered is ended (``_run_session_keys``): a
    # sequential job keeps an earlier agent's session alive for its pending
    # sub-agents. Each key is FENCED until the record and audit are written
    # (``SessionManager.ending_key``, see ``_end_run_processes``), so a claim
    # or cold start meets a key being ended or already recorded, never
    # neither. The fences lift however the block ends.
    with ExitStack() as fences:
        keys = self._fence_run_keys(job_id, taken, fences)
        # The run's newest key names the record and the audit; the audit's
        # metadata lists what was actually ENDED -- filled only by the kill
        # passes, so a service without a session manager and a kill that
        # raised report none.
        session_key = keys[0]
        ended: list[str] = []
        kill_failure: BaseException | None = None
        sigkill_failure: str | None = None
        try:
            # Kill the session processes first: reset each key and kill what
            # the reset could not stop -- for every session that lands under
            # it, bounded (see ``_end_run_processes``).
            if self._sessions:
                sigkill_failure, ended = await self._end_run_sessions(
                    job_id, taken, keys, fences, who="Reaper"
                )
        except (Exception, asyncio.CancelledError) as exc:
            # CancelledError too: the reaper task is cancelled at shutdown, and
            # this reap still owes the finish and the record before it lets
            # the cancellation through. GeneratorExit and the interpreter-exit
            # signals are not caught (awaiting after them is an error); the
            # finally still finishes the claim.
            kill_failure = exc
        finally:
            # Cancel the asyncio task and release the claim directly. Don't rely on
            # _run_job_isolated's finally — the reaper exists for cases where the
            # normal path is stuck (idempotent with finally).
            self._runs.finish_taken(job_id)
        # One name for the record and the audit: a failure that escaped the
        # kill (re-raised below) or one the SIGKILL reported.
        kill_failed = failure_name(kill_failure) if kill_failure is not None else sigkill_failure

        # Update job state and persist. The persist goes through the locked
        # worker-thread merge helper (offloaded via asyncio.to_thread) — NOT a
        # bare on-loop self._save() — so it re-syncs under the store lock and
        # cannot clobber a concurrent add/update worker's just-written job
        # list, and its bounded lock spin never parks the event loop this
        # coroutine runs on. See _merge_terminal_state_locked.
        job = next((j for j in self._jobs if j.id == job_id), None)
        if job:
            last_error = f"Reaped after {int(elapsed)}s (exceeded {deadline}s deadline)"
            if kill_failed is not None:
                # Bounded at retention (``MAX_ERROR_DETAIL_LEN``): the kill's
                # reason is not this code's to size -- a Windows tree drain that
                # failed carries one line per process, several handles' failures
                # are joined -- and ``last_error`` is persisted and shown as is.
                last_error = with_kill_failure(last_error, kill_failed)
            last_run_ts = seams.time.time()
            # Drawn here, in the same loop step as the release above (no await
            # between them), so a replacement claim always draws a higher one
            # and this record can never land over that run's.
            generation = self._runs.next_generation(job)
            # Reflect into the in-memory snapshot for the history record below
            # and any immediate reader; the authoritative persist is the locked
            # merge, which re-derives the disk copy after _sync().
            job.last_status = "error"
            job.last_error = last_error
            job.last_run_ts = last_run_ts
            # A reaped one-shot is parked like a timed-out one, because the
            # finalizer skips the result merge and _execute's disable never
            # runs; the merge below saves the pause. A delete_after_run one is
            # kept and auto-paused as failed. A run whose payload never started
            # parks nothing and stays due. Read that off ``taken.job``,
            # not ``job``: a reload may have replaced ``job``, and
            # ``run_never_started`` and the other park inputs are runtime-only.
            ran = taken.job if taken.job is not None else job
            park_one_shot = taken.started_monotonic is not None and run_parks_one_shot(ran)
            if park_one_shot:
                job.enabled = False
            # A reaped run that started counts toward auto-pause, like the
            # wait_for timeout arm; one that never started does not. The same
            # test decides whether the user is told (below).
            counted = (
                taken.started_monotonic is not None
                and not job.failure_recorded
                and not ran.run_never_started
            )
            try:
                await self._merge_terminal_state_with_retry(
                    job,
                    job_id,
                    scheduled=taken.trigger == "scheduled",
                    last_status="error",
                    last_error=last_error,
                    last_run_ts=last_run_ts,
                    run_generation=generation,
                    result_produced=taken.started_monotonic is not None and job.result_produced,
                    count_failure=counted,
                    park_one_shot=park_one_shot,
                    # Off the object the run executed on, never ``job``: a
                    # reload may have made that a store copy whose stamp,
                    # if any, is not this run's.
                    auto_paused_at_start=(
                        taken.job.auto_paused_at_start if taken.job is not None else None
                    ),
                    dispatched=taken.outcome_reset,
                    ran=ran,
                )
            except Exception:
                logger.exception("Reaper: failed to persist state for cron %s", job_id)
            # Record timeout in history
            try:
                record = CronRunRecord(
                    job_id=job_id,
                    trigger=reap_trigger,
                    started_at=reap_started_at,
                    finished_at=seams.time.time(),
                    duration_ms=int(elapsed * 1000),
                    status="timeout",
                    summary=job.last_error or "",
                    error=job.last_error or "",
                )
                await self._history.append(record)
                if self._push_refresh:
                    self._push_refresh("cron_history")
            except Exception:
                logger.exception("Reaper: failed to record history for cron %s", job_id)
            if counted:
                self._alert_timeout(job, last_error)

        # SEL audit.
        try:
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key=session_key,
                source="cron",
                tool_name="reaper_force_kill",
                # Never ``reaped`` for a process group the kill left alive:
                # the reap ended the run's record, not its processes.
                outcome="reaped" if kill_failed is None else "failed",
                metadata={
                    "job_id": job_id,
                    "session_key": session_key,
                    # The keys this reap actually ended, newest first --
                    # only those every session of which was answered; a key
                    # whose kill was refused or failed is not listed (it
                    # stays registered, named in last_error), and none is
                    # when the passes never ran or raised.
                    "session_keys": ended,
                    "elapsed": int(elapsed),
                },
            )
        except Exception:
            logger.exception("Reaper: SEL audit failed for cron %s", job_id)
    if kill_failure is not None:
        raise kill_failure


# ── User-initiated cancellation ──


async def cancel(self: CronService, job_id: str) -> bool:
    """Cancel a running cron execution (user-initiated).

    Kills the sandboxed subprocess (script/command crons) or the kiro-cli
    session (agent crons), cancels the asyncio task, records a
    ``cancelled`` history entry, and leaves ``consecutive_failures``
    untouched. Returns True when a running execution was found. A kill
    that raises still cancels the task, releases the run's claim and
    records the entry (its error names the failure), then propagates.
    A cancelled one-shot whose pause could not be saved raises
    :class:`CronPauseNotSaved` after the entry and the audit, unless nothing
    is left to pause: a job removed from the store meanwhile, or one paused
    by whatever wrote the store meanwhile, is noted in the entry and the
    cancel returns True. The entry and the message describe the job as the
    cancel last saw it (:func:`_cancelled_park_report`).
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    # A finished task is not a running execution, whatever the claim says.
    # Trusting the claim here would kill nothing, answer True, record a
    # "Cancelled by user after Ns" row for a run that ended long ago.
    # Release the leftovers and answer "not running" instead; a live
    # task is untouched and cancels below.
    self.discard_finished_run(job_id)
    if job_id not in self._claims:
        return False
    claim = self._runs.take(job_id)
    if claim is None:
        # The stored claim is already taken: another cancel() (a
        # double-clicked Cancel) or the reaper is inside its kill awaits
        # and owns this run's teardown. Carrying on would pop ITS claim at
        # step 3, ending the job's occupancy before that kill finished,
        # and write a second terminal row whose generation could outrank
        # a replacement run's. Nothing here is this caller's to cancel:
        # answer "not running", as the route does for an idle job.
        return False
    logger.info("Cancel: user-initiated cancellation of cron job %s", job_id)
    # Flag the run this cancel took (_RunClaim.cancelled): the flag lives
    # on this run's claim, so only this run's finalizer can read it.
    claim.cancelled = True
    started_at = claim.claimed_at
    trigger = claim.trigger
    elapsed = seams.time.time() - started_at

    job = next((j for j in self._jobs if j.id == job_id), None)
    is_agent_job = job is None or not (job.script or job.command)

    # Steps 1-2 are the kill awaits; step 3 finishes the claim in a
    # ``finally``, as in _force_reap, so a raised kill never leaves the claim
    # taken until restart. What can raise: step 1's executor hop (RuntimeError
    # from a shut-down pool) and a cancellation when the client disconnects.
    # A raised or reported failure goes into the terminal record, is audited
    # as ``failed``, and a raised one is re-raised after the record.
    #
    # EVERY key the run registered is ended and FENCED until step 4's record
    # and the audit are written, as in _force_reap. A script or command job's
    # key names no session, so the fence gates nothing for it.
    killed_proc = False
    with ExitStack() as fences:
        keys = self._fence_run_keys(job_id, claim, fences)
        # The run's newest key names the record and the audit; the audit's
        # metadata lists what was actually ENDED -- filled only by step 2, so
        # a script or command job (whose key names no session and whose
        # step 2 is skipped) and a kill that raised report none.
        session_key = keys[0]
        ended: list[str] = []
        kill_failure: BaseException | None = None
        sigkill_failure: str | None = None
        try:
            # 1. Script/command crons: SIGTERM the sandboxed subprocess group.
            # Offloaded: kill_running_process performs blocking kernel calls.
            killed_proc = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), cron_script.kill_running_process, job_id
            )

            # 2. Agent crons: kill the kiro-cli sessions (mirrors _force_reap):
            # reset each of the run's keys and kill what the reset could not
            # stop, for every session that lands under it, bounded
            # (``_end_run_sessions``).
            if self._sessions and is_agent_job and not killed_proc:
                sigkill_failure, ended = await self._end_run_sessions(
                    job_id, claim, keys, fences, who="Cancel"
                )
        except (Exception, asyncio.CancelledError) as exc:
            # CancelledError too: aiohttp cancels the route's handler when the
            # client disconnects mid-cancel, and this coroutine still owes the
            # finish and the record before it lets the cancellation through.
            # GeneratorExit and the interpreter-exit signals are not caught
            # (awaiting after them is an error); the finally still finishes.
            kill_failure = exc
        finally:
            # 3. Cancel the asyncio task and release the claim directly
            # (idempotent with _run_job_isolated's finally).
            self._runs.finish_taken(job_id)
        # One name for the record and the audit: a failure that escaped the
        # kill (re-raised below) or one the SIGKILL reported.
        kill_failed = failure_name(kill_failure) if kill_failure is not None else sigkill_failure

        # 4. Update job state, persist, and record history. The persist goes
        # through the locked worker-thread merge helper (offloaded via
        # asyncio.to_thread) — NOT a bare on-loop self._save() — so it re-syncs
        # under the store lock and cannot clobber a concurrent add/update
        # worker; the bounded spin never parks this loop-side coroutine.
        pause_not_saved: CronPauseNotSaved | None = None
        if job:
            last_error = f"Cancelled by user after {int(elapsed)}s"
            if kill_failed is not None:
                # Same bound as the reaper's (``with_kill_failure``).
                last_error = with_kill_failure(last_error, kill_failed)
            last_run_ts = seams.time.time()
            # Drawn here, in the same loop step as the release in step 3 (no
            # await between them), so a replacement claim always draws a higher
            # one and this record can never land over that run's.
            generation = self._runs.next_generation(job)
            # In-memory snapshot for the history record / immediate readers;
            # the locked merge is authoritative.
            job.last_status = "error"
            job.last_error = last_error
            job.last_run_ts = last_run_ts
            # A cancelled one-shot is parked, not left due: its run never
            # reaches _execute's one-shot disable. The hold is provisional:
            # the job leaves the schedule in this process at once, so the next
            # tick does not run it while the save below is pending, but
            # ``user_paused`` is set only by the save that lands the park. A
            # delete_after_run one-shot is parked too: _execute_with_timeout's
            # failed-run auto-pause only reaches a run whose payload started,
            # so a cancel that lands before that would leave it due.
            park_one_shot = job.schedule.kind == "at"
            if park_one_shot:
                job.enabled = False
            # Why the park was not saved, or None once a store write that
            # holds it has returned (the merge's own answer).
            park_failure: str | None = None
            try:
                park_failure = await self._merge_terminal_state_with_retry(
                    job,
                    job_id,
                    scheduled=claim.trigger == "scheduled",
                    last_status="error",
                    last_error=last_error,
                    last_run_ts=last_run_ts,
                    run_generation=generation,
                    result_produced=claim.started_monotonic is not None and job.result_produced,
                    park_one_shot=park_one_shot,
                    # Off the object the run executed on, as the reaper reads it, so
                    # an auto-pause the run started from is never taken as its own.
                    auto_paused_at_start=(
                        claim.job.auto_paused_at_start if claim.job is not None else None
                    ),
                    dispatched=claim.outcome_reset,
                    ran=claim.job,
                )
            except Exception as exc:
                logger.exception("Cancel: failed to persist state for cron %s", job_id)
                park_failure = failure_name(exc)
            # A scheduled terminal record that did not land is retried by the
            # held-record owner, but the history entry and caller still report
            # this attempt's failure, from the job as it is now and with the
            # cancel's own text (another run can replace ``job.last_error``).
            # When the merge did not run or the job was reloaded, apply its
            # rules here: a newer run's record or a non-one-shot schedule takes
            # the hold off; a one-shot the record never reached stays held. A
            # failed park answers success only when the SAVED store shows the
            # job cannot run again; in-memory flags are not proof.
            live = self.get_job(job_id)
            saved: str | None = None
            with self._held_run_records_guard:
                held = self._held_run_records.get(job_id)
                # Only this cancel's own settlement is a pause hold. A newer
                # run held in its place decides whether the job runs again,
                # so the saved store is read below instead.
                settlement_pending = False
                if (
                    held is not None
                    and self._held_settlements.get(job_id) is held
                    and held.run_generation == generation
                    and live is not None
                ):
                    settlement_pending = live.user_pause_generation <= held.user_pause_generation
                    # The user resumed the job after this cancel's record was
                    # frozen, and the merge did not say so (a busy store raises
                    # before it runs): no newer run was recorded, so report the
                    # resume, as the merge would have. A pause meanwhile is left
                    # to the saved store below, which alone shows it landed.
                    if (
                        park_one_shot
                        and park_failure is not None
                        and not settlement_pending
                        and live.enabled
                    ):
                        park_failure = seams._PARK_NEWER_CHOICE
            if park_one_shot and park_failure is not None:
                if live is not None and (
                    live.run_generation > generation or live.schedule.kind != "at"
                ):
                    live.enabled = not (live.user_paused or live.auto_paused)
                elif live is not None and live.run_generation < generation:
                    live.enabled = False
                # Read even while this cancel's settlement is pending: a pause
                # saved before the cancel froze its record is in the store, and
                # the saved store's "paused" answers success over the hold.
                if live is None or live.user_paused or live.auto_paused:
                    saved = await asyncio.to_thread(self._saved_job_state, job_id)
            note, message = seams._cancelled_park_report(
                live, park_failure, saved, held=settlement_pending
            )
            history_error = f"{last_error}{note}" if park_one_shot else last_error
            if park_one_shot and park_failure is not None:
                logger.warning(
                    "Cancel: the pause of cancelled one-shot cron %s was not saved (%s)",
                    job_id,
                    park_failure,
                )
            if park_one_shot and message is not None:
                pause_not_saved = seams.CronPauseNotSaved(message)
            try:
                record = CronRunRecord(
                    job_id=job_id,
                    trigger=trigger,
                    started_at=started_at,
                    finished_at=seams.time.time(),
                    duration_ms=int(elapsed * 1000),
                    status="cancelled",
                    summary=history_error,
                    error=history_error,
                )
                await self._history.append(record)
                if self._push_refresh:
                    self._push_refresh("cron_history")
            except Exception:
                logger.exception("Cancel: failed to record history for cron %s", job_id)
        if self._push_refresh:
            self._push_refresh("crons")

        # SEL audit.
        try:
            seams.sel.sel().log_tool_invocation(
                session_key=session_key,
                source="cron",
                tool_name="cron_cancel",
                # Never ``cancelled`` for a process group the kill left alive.
                outcome="cancelled" if kill_failed is None else "failed",
                metadata={
                    "job_id": job_id,
                    "session_key": session_key,
                    # The keys this cancel actually ended, newest first --
                    # only those every session of which was answered; a key
                    # whose kill was refused or failed is not listed (it
                    # stays registered, named in last_error). None for a
                    # script or command job, whose sessions step 2 never
                    # touches, or when step 2 raised.
                    "session_keys": ended,
                    "elapsed": int(elapsed),
                    # Named for what the return now MEANS, not for what it used
                    # to. kill_running_process returns True either because it
                    # signalled a live child OR because it recorded the cancel
                    # against a spawn still in flight, where there is no child to
                    # signal yet. Auditing that second case as
                    # "killed_subprocess" asserted a kill that never happened.
                    "cancellation_accepted": killed_proc,
                },
            )
        except Exception:
            logger.exception("Cancel: SEL audit failed for cron %s", job_id)
    if kill_failure is not None:
        raise kill_failure
    if pause_not_saved is not None:
        raise pause_not_saved
    return True

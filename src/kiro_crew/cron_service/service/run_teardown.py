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
            # The list is a snapshot for iteration order only; the job's
            # run is whatever the store holds NOW. An earlier job's reap in
            # this same sweep awaited (its session reset, the locked
            # persist, the history append), and in that window this job's
            # run can end and a replacement claim the job: the snapshot's
            # claim then holds a run that is gone, with stamps that put the
            # job far past its deadline. Measure and reap only the stored
            # claim -- a replacement is a different object, measured on its
            # own stamps by the next sweep. (The six-dict shape this
            # replaces read the monotonic and jitter stamps live per
            # iteration, so a replacement's fresh stamp made the sweep
            # continue; the claim gives the same guarantee by identity.)
            if self._claims.get(job_id) is not claim:
                continue
            # A claim cancel() or an earlier reap has taken is theirs to
            # finish: they popped the run's stamps, in the shape this
            # replaces, exactly so a sweep landing inside their kill awaits
            # would not reap the run a second time.
            if claim.taken:
                continue
            # A task that is done() while its claim is still here never
            # reached _run_job_isolated's finally (a run that ends normally
            # releases the claim there, before its task finishes), so the
            # job still reads as running to the due-scan, _next_wake_secs
            # and run_job. Release it on THIS sweep, not once the run's
            # deadline passes: that is at least _JOB_TIMEOUT_SECS and up to
            # a day away, and every scheduled fire until then is skipped.
            # A live task or no tracked task falls through to the timeout
            # backstop below unchanged.
            if self.discard_finished_run(job_id):
                continue
            elapsed = now - claim.claimed_at
            # DECIDE and REPORT on the monotonic clock. A claim with no
            # monotonic stamp (a manual run still parked in its store
            # refresh, or a test that seeds only the epoch stamp) falls back
            # to the wall-clock elapsed, so the backstop never stops reaping
            # — it just cannot tell suspend time apart for that run.
            #
            # The reported duration is monotonic too, not wall-clock: a
            # backward wall-clock step during a >=30-min run would otherwise
            # render a negative "ran -Ns"/"Reaped after -Ns" in the log and
            # the persisted history. Monotonic elapsed is equally legible
            # ("seconds since start") and cannot go negative.
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
                # Named for the claim measured above: the reap takes that
                # claim or nothing (``RunClaims.take``'s ``expected``), so a
                # claim that changed under this sweep is never killed on
                # the snapshot's stamps.
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

    ``claim`` is the run the caller measured over its deadline -- the
    sweep, the only caller, passes the claim it iterated. The reap takes
    that claim or nothing (identity): a stored claim that is a different
    object is a replacement run the caller never measured, a claim that is
    gone was a run that ended on its own, and a claim already taken is a
    teardown someone else owns -- none is killed, and the job's session,
    which is the replacement's now, is left alone. There is no reap by
    job id alone: a kill needs a run to answer for, and the run is the
    claim.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    taken = self._runs.take(job_id, expected=claim)
    if taken is None:
        # Either the stored claim is taken -- cancel() or an earlier reap
        # owns this run's teardown and is inside its kill awaits; its
        # claim is not this reap's to pop below, and its terminal row is
        # the only one owed (a second "Reaped" row here would draw a
        # generation that could outrank a replacement run's) -- or the
        # measured run is gone, or a replacement this reap was not named
        # for stands in its place. The sweep skips all three itself; this
        # covers a store that changed under the sweep's awaits.
        return
    # Flag the run this reap took: its finalizer reads the flag off its
    # own claim, so no other run of the job can see it.
    taken.reaped = True
    reap_started_at = taken.claimed_at
    reap_trigger = taken.trigger
    # The kill, then the finish -- in a ``finally``, so the claim this reap
    # took is finished however the kill ends. ``taken`` is a lock with no
    # other owner-death recovery: from the take on, by design, the run's
    # own fences fail, the sweep skips the claim and discard_finished_run
    # refuses it. A kill that raised would otherwise leave the claim taken
    # for the life of the gateway -- the sweep swallows this coroutine's
    # exception and skips the claim on every later sweep, and every manual
    # run of the job 409s. What can raise here is a cancellation (the
    # reaper task cancelled at shutdown); the ``Exception`` arm of the
    # handler is a net for a reset failure the inner handlers let through,
    # and today none does: they catch every ``Exception`` the reset raises,
    # and ``_sigkill_session`` raises nothing. What the SIGKILL could not
    # do it REPORTS instead -- the guard's refusal of the pid, an error
    # the kill raised, an error ahead of the signal -- as its result, so
    # a process group it left alive is never recorded as reaped: the
    # failure, raised or reported, is carried into the terminal record
    # below (the run's finally writes none for a taken run) and audited as
    # ``failed``; a raised one is re-raised after the record, so the sweep
    # still logs it.
    #
    # EVERY key the run registered is ended (``_run_session_keys``): a
    # sequential job holds one key per agent, and an earlier agent's session
    # is kept alive for its pending sub-agents while a later agent runs --
    # a reap that ends only the newest key records ``reaped`` while that
    # earlier session and its sub-agents survive. Each key is FENCED from
    # before its first pass until the record and the audit are written
    # (``SessionManager.ending_key``, see ``_end_run_processes``): a claim
    # or cold start under it meets a key that is either being ended --
    # held at the door -- or recorded, never one that is neither. The
    # fences lift however the block ends; a kill failure that escaped is
    # re-raised after the lift.
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
            # A reaped one-shot is parked the way a timed-out one is: both are
            # the system ending a run it judged stuck, and the finalizer skips
            # the result merge for a reaped run, so _execute's own disable
            # never runs. Held off the schedule here at once, with the pause
            # saved by the merge below. A delete_after_run one is kept and
            # auto-paused as a failed run; a run whose payload never started
            # parks nothing and stays due. Whether the payload ever started is
            # read off the object the run executed on (``taken.job``), not
            # ``job``: a store reload during the run or the kill awaits made
            # ``job`` a different object, and ``run_never_started`` (like
            # ``fire_time_denied`` and ``refusal_cannot_clear``, which the park
            # decision reads) is runtime-only and does not survive a reload.
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
    # ``finally``, so it runs however they end. ``taken`` is a lock with no
    # other owner-death recovery: from the take on, by design, the run's
    # own fences fail, the reaper sweep skips the claim and
    # discard_finished_run refuses it. A kill that raised would otherwise
    # leave the claim taken until restart: every manual run of the job 409s
    # and every scheduled fire is skipped. What can raise here: step 1's
    # executor hop (a pool shut down under it, a thread refused at
    # interpreter exit -- RuntimeError either way) and a cancellation of
    # this handler when the client disconnects. Step 2 raises nothing but
    # a cancellation: its inner handlers catch every ``Exception`` the
    # reset raises, and ``_sigkill_session`` raises nothing -- it REPORTS
    # a refused pid or a failed kill as its result instead, so a process
    # group it left alive is never recorded as cancelled. The failure,
    # raised or reported, is carried into the terminal record (the run's
    # finally writes none for a taken run) and audited as ``failed``; a
    # raised one is re-raised after the record, so the caller still learns
    # the kill failed.
    #
    # EVERY key the run registered is ended (``_run_session_keys``; a
    # sequential job holds one per agent, an earlier agent's kept alive for
    # its pending sub-agents), each FENCED from before step 1 until step 4's
    # record and the audit are written (``SessionManager.ending_key``, see
    # ``_end_run_processes``): a claim or cold start under it meets a key
    # that is either being ended -- held at the door -- or recorded, never
    # one that is neither. A script or command job's key names no session,
    # so the fence gates nothing for it. The fences lift however the block
    # ends; a kill failure that escaped is re-raised after the lift.
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
            # A scheduled terminal record that did not land is retried by
            # the existing held-record owner. The history entry and caller still
            # report this attempt's failure because no durable write returned.
            # The held-record merge preserves choices made since. The caller
            # (below) say so, from the job as it is now: the cancel's own text
            # first, because another run of the job can replace
            # ``job.last_error`` while the merge is awaited. The merge's own
            # rules first, for one that did not run (a busy store raises before
            # it) or whose job a reload replaced since: a newer run's record, or
            # a schedule that is not a one-shot, takes the hold off as a reload
            # derives it; a one-shot the cancel's record never reached is held.
            # A failed park answers success only when the saved store shows the
            # job cannot run again: flags a failed save left in memory are not
            # proof, so a job gone or paused here is read from the store.
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

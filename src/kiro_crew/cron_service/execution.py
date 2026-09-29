"""How long a cron run may take, and what its end writes back.

The run's deadline terms: the per-wake execution budget (:func:`effective_wake_budget`)
and the allowances a command or script job spends inside it before its own code
runs. The service sums the same terms twice -- for the execution guard
(:meth:`CronService._execute_with_timeout`) and for the reaper's defence-in-depth
sweep (:meth:`CronService._reaper_loop`) -- so the two deadlines cannot drift and
pre-empt one another.

A run's terminal record: the fields its finalizer freezes before the claim is
released (:func:`close_run`) and the fields the locked merge copies from that
record onto the store's copy of the job (:func:`apply_run_record`).

The service's run lifecycle -- claim, jitter, marker, execute, finalize -- calls
these; nothing here holds state.
"""

from __future__ import annotations

import copy
import math
from typing import TYPE_CHECKING

from kiro_crew.executors import _CRON_QUEUE_WAIT_SECS, cron_gate_budget

if TYPE_CHECKING:
    from kiro_crew.cron_service.model import CronJob

# Margin the per-wake budget must leave above a command/script subprocess
# timeout: the wake deadline cancels only the executor FUTURE (threads are
# not interruptible), so a budget shorter than the subprocess bound leaves
# the subprocess running while the guards clear and later wakes duplicate it.
_SUBPROC_CLEANUP_ALLOWANCE_SECS = 5


def _pool_queue_allowance(job: CronJob | None) -> int:
    """Queue budget a command/script job may spend before its own code runs.

    Single-sourced deliberately. TWO deadlines bound one run -- the execution
    guard in :meth:`CronService._execute_with_timeout` and the reaper's
    defence-in-depth sweep -- and the cron pool's queue wait happens inside both.
    If only one of them accounts for that wait, the other pre-empts it, and the
    two failures are opposite and both silent: a reaper that does not account for
    it cancels a job that never executed (a skipped run), while an execution
    deadline that does not account for it kills a job still sitting in the pool
    queue and reports it as an overrun (the misdiagnosis this change exists to
    remove, which also lets a claimed subprocess run on while the overlap guards
    clear). Deriving both from this one function is what keeps them from drifting.

    Only command and script jobs dispatch through the pool to EXECUTE, so only
    they need the allowance; a message job gets none and neither deadline is
    widened for it. That scoping holds only because the one piece of pool work
    every job kind shares -- the fire-time governance gate -- is dispatched to
    the GOVERNANCE pool rather than this one. Gating on the cron pool would put
    a message job's gate behind job-duration work while its deadline was already
    armed, spending an execution budget it has no allowance to cover; the fix
    for that belongs at the gate's dispatch, not in a wider deadline here, since
    widening would also delay the wedged-delivery backstop for runs that never
    queue at all. See the gate sites in slack/gateway.py.
    """
    if job is None:
        return 0
    return _CRON_QUEUE_WAIT_SECS if (job.command or job.script) else 0


def _gate_budget_allowance(job: CronJob | None) -> int:
    """Seconds the fire-time gate may consume inside a pool-dispatching job's deadline.

    A third term of the same shape as :func:`_pool_queue_allowance`.  The gate is
    awaited BEFORE the pool dispatch and inside the deadline armed for the whole
    run, so a gate that spends its full bound leaves the subprocess that much
    less -- and a thread cannot be interrupted, so when the deadline then fires
    with a worker already claimed, the overlap guards clear while the subprocess
    runs on and the next wake duplicates its side effects.  That is the hazard
    ``_SUBPROC_CLEANUP_ALLOWANCE_SECS`` exists for; the queue wait was a second
    term it did not account for, and the gate bound is a third.

    Scoped to command/script for the same reason the queue allowance is: only
    those dispatch through the pool to EXECUTE, so only they carry the
    claimed-worker hazard.  A message job's budget is left exactly as set -- its
    protection is that :func:`cron_gate_budget` lands strictly below the wake
    deadline and is the gate's TOTAL across both its phases, so the gate's own
    bound fires first and the run is retained.

    Rounded UP to an int: the value is added to a deadline that reaches the
    operator as ``Timed out after {deadline}s``, and a float would render there
    as ``2.0s``.  Up is the safe direction -- it can only add headroom.
    """
    if job is None or not (job.command or job.script):
        return 0
    return math.ceil(cron_gate_budget(effective_wake_budget(job)))


def _vet_allowance(job: CronJob | None) -> int:
    """Seconds the CLAIM-TIME vet may consume inside a pool-dispatching job's deadline.

    A FOURTH term of the same shape as the three above, and it exists for the
    same reason :func:`_gate_budget_allowance` does.  The fire-time gate runs
    ``vet_job_at_fire_time`` BEFORE the pool dispatch; the claim-time vet runs
    the SAME function again INSIDE the worker, ahead of the subprocess it
    authorises -- so it too is spent inside the deadline armed for the whole run,
    and a vet that spends its bound leaves the subprocess that much less.  A
    thread cannot be interrupted, so when the deadline then fires with the
    subprocess already started, the overlap guards clear while it runs on and the
    next wake duplicates its side effects.  That is the hazard
    :data:`_SUBPROC_CLEANUP_ALLOWANCE_SECS` exists for; the queue wait was a
    second term it did not account for, the gate bound a third, and this a
    fourth.

    Sized from :func:`_gate_budget_allowance` rather than from a second copy of
    its expression: it is the same work under the same bound, and two copies
    would drift.  The direction of drift matters -- an allowance SMALLER than the
    bound the vet is actually held to is exactly the unaccounted margin this
    closes.

    Scoped to command/script for the reason the other two are: only those
    dispatch through the pool to EXECUTE, so only they carry the claimed-worker
    hazard.  A message job never reaches ``_vet_at_claim_then`` at all.
    """
    return _gate_budget_allowance(job)


def effective_wake_budget(job: CronJob) -> int:
    """Seconds :meth:`CronService._execute_with_timeout` will allow this run.

    Extracted so the fire-time gate can cap its own bound against the same
    number rather than re-deriving the rule.  A second copy would drift, and the
    direction it drifts matters: a gate bound that exceeded the real wake budget
    would let the wake deadline fire first, which is the state where starvation
    is indistinguishable from an overrun and a one-shot gets consumed by a run
    that never dispatched.

    Returns an int: the value reaches the operator through
    ``last_error = f"Timed out after {deadline}s"``, and a float would render
    there as ``2.0s``.

    A non-numeric ``timeout_secs`` falls back to the default rather than raising.
    ``_execute_with_timeout`` was the only caller when this rule lived inline, so
    a duck-typed job never reached the comparison; the fire-time gate now derives
    its own bound from this and runs on every job kind, so the rule has to
    tolerate a store entry (or a test double) whose field is not a number.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    raw = getattr(job, "timeout_secs", None)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return seams._JOB_TIMEOUT_SECS
    return int(raw) if 1 <= raw <= 86400 else seams._JOB_TIMEOUT_SECS


def close_run(
    job: CronJob, *, started_at: float, generation: int, being_cancelled: bool
) -> tuple[CronJob, str, str | None]:
    """Write a finished run's own terminal fields on ``job``; return its frozen record.

    Returns ``(terminal, status, run_result)``: the shallow copy the locked merge
    applies, the history row's status, and the result this run produced (None
    when it produced none). The caller takes it while the run still holds its
    claim -- see :meth:`CronService._run_job_isolated`.
    """
    # For 'every' jobs, use started_at to prevent cumulative drift.
    # `_execute` bound this run's retry count to the `last_run_ts` it
    # stamped; moving that stamp has to move the binding with it, or
    # the pair disagrees for every completed `every` run and the
    # Schedule page never shows a count. Only a bound pair moves: a
    # run that timed out never reached `_execute`'s stamp, so its
    # pair is (previous run, this run) and stays mismatched.
    if job.schedule.kind == "every":
        if job.last_retry_run_ts == job.last_run_ts:
            job.last_retry_run_ts = started_at
        job.last_run_ts = started_at
    # One clear per result-less run. Scattering it over exit sites is
    # what let the fire-time deny and script Skip paths keep a result.
    if (job.command or job.script) and not being_cancelled:
        job.clear_carried_result()
    status = "success" if job.last_status == "ok" else "failure"
    # Attribute last_result to this run only if the run actually
    # produced it (set_run_result sets the marker). Reading it
    # unconditionally recorded the PREVIOUS run's result as this
    # run's summary/trace whenever the run ended without producing
    # one (observed in the wild: a timed-out run's history row
    # carried the prior success's summary verbatim -- fabricated
    # history on a status=failure record).
    run_result = job.last_result if job.result_produced else None
    # The merge copies this run's fields to the disk copy field by
    # field; a shallow copy freezes them as this run left them.
    terminal = copy.copy(job)
    # The record's generation, which the merge compares against the
    # one the store holds and stamps on it. Set on the copy, not
    # the shared job -- only a merge moves the store's generation,
    # so a run that has merely STARTED behind this one does not
    # discard this record; a run whose record already landed does.
    terminal.run_generation = generation
    return terminal, status, run_result


def apply_run_record(target: CronJob, run: CronJob) -> None:
    """Copy the fields a completed run persists from its record ``run`` onto ``target``.

    ``target`` is the store's copy of the job, freshly reloaded under the lock:
    a different object than the run's, so every field the run produces is
    copied explicitly. The caller has already checked the record's generation.
    """
    target.run_generation = run.run_generation
    target.last_run_ts = run.last_run_ts
    target.last_status = run.last_status
    target.last_error = run.last_error
    # Only propagate enabled=False for one-shot at-jobs that fired.
    # Never overwrite enabled for recurring jobs — user_paused is the
    # sole authority for user-controlled pause/resume state.
    # Propagate the fired/parked disable for at-jobs — including a
    # fire-time-DENIED one (parked disabled instead of deleted so
    # it cannot refire every tick yet stays re-enableable).
    if run.schedule.kind == "at" and (not run.delete_after_run or run.fire_time_denied):
        target.enabled = run.enabled
        target.user_paused = not run.enabled
    # auto_paused is execution-owned (repeated-failure auto-pause and
    # its reset on success), so propagate it for every job — unlike
    # `enabled`, which must not be clobbered for recurring jobs. Also
    # reflect it into the disk copy's derived `enabled` so the next
    # reader sees the pause before a reload re-derives it.
    target.auto_paused = run.auto_paused
    if run.auto_paused and not target.user_paused:
        target.enabled = False
    target.last_result = run.last_result
    # Both stamp fields travel WITH last_result. The merge's _sync()
    # replaced the job list with the disk copies, so target is a
    # different object than `run` and every field a run produces
    # has to be copied explicitly. Omitting these persisted the new
    # result under the PREVIOUS run's stamp, so after a reload
    # /to-chat rendered a header the executor never wrote and
    # append_if_absent duplicated the row instead of collapsing it.
    target.last_result_ts = run.last_result_ts
    target.last_result_stamp = run.last_result_stamp
    target.last_posted_hash = run.last_posted_hash
    target.consecutive_dupes = run.consecutive_dupes
    target.last_posted_at = run.last_posted_at
    target.last_failure_hash = run.last_failure_hash
    target.last_failure_at = run.last_failure_at
    target.consecutive_failures = run.consecutive_failures
    # Same shape as the other runtime->disk copies on this call: a
    # field `_execute` sets on the in-memory `run` is invisible after
    # reload unless copied here explicitly. A cancelled run never
    # reached the stamp in `_execute`, so on it these still hold the
    # last completed run's values and the copy changes nothing.
    target.last_retry_count = run.last_retry_count
    target.last_retry_run_ts = run.last_retry_run_ts

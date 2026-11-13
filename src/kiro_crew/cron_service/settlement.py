"""A scheduled cancel or reap's terminal record, held until a store write lands it.

A cancel and a reap release the run's claim before their terminal store write
returns. For a scheduled run, a write that did not land (a busy store, a write
error, an unreadable store) leaves the occurrence due again on disk, so the
service holds the frozen record as a *settlement* in its held-record owner
(``CronService._hold_run_record``) and the next timer tick's save retries it.

This module owns the two rules that record carries: how it is frozen
(:func:`freeze_terminal_record`) and how it is applied to a job the store hands
back (:func:`apply_settlement_record`). Nothing here holds state.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from kiro_crew.cron_service.execution import apply_run_record, auto_pause_failed_one_shot

if TYPE_CHECKING:
    from kiro_crew.cron_service.claims import _RunClaim
    from kiro_crew.cron_service.model import CronJob


class TerminalSettlementPending(RuntimeError):
    """A scheduled terminal record still needs a durable store write."""

    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


def freeze_terminal_record(
    job: CronJob,
    *,
    ran: CronJob | None,
    last_status: str,
    last_error: str,
    last_run_ts: float,
    run_generation: int,
    result_produced: bool,
    count_failure: bool,
    park_one_shot: bool,
    auto_paused_at_start: bool | None,
    dispatched: bool = False,
) -> CronJob:
    """Freeze the one terminal record used by the immediate merge and any hold.

    A cancel or reap can freeze this record before the cancelled task reaches
    ``_execute_with_timeout``'s finally, so a dispatched run first applies that
    finally's failed one-shot auto-pause to ``ran``, the object the finally
    pauses, and the record carries it (audited once: the finally then finds the
    run paused). Whether the run was denied or never started is
    read off ``ran``, the object the run executed on: those flags are
    runtime-only, and a store reload during the run makes ``job`` a disk copy
    that has lost them.

    A ``park_one_shot`` request writes the park onto the record itself
    (``enabled`` False, ``user_paused`` True), so a held record carries the
    cancel's own decision rather than one re-derived from the record later.
    """
    origin = ran if ran is not None else job
    paused_here = False
    if dispatched and not (origin.fire_time_denied or origin.run_never_started):
        # On the run's object, as the finally does, so the transition is audited
        # once even when a reload made ``job`` a different object.
        auto_pause_failed_one_shot(origin, last_status)
        paused_here = (
            origin is not job
            and origin.delete_after_run
            and last_status == "error"
            and origin.auto_paused
            and not origin.auto_paused_at_start
        )
    record = copy.copy(job)
    if paused_here:
        # ``job`` is the store's copy: the record carries the run's pause, and the
        # settlement's own rules decide whether it lands on ``job``.
        record.enabled, record.auto_paused = False, True
    record.run_generation = run_generation
    record.last_status = last_status
    record.last_error = last_error
    record.last_run_ts = last_run_ts
    record.auto_paused_at_start = auto_paused_at_start
    if park_one_shot and record.schedule.kind == "at":
        record.enabled = False
        record.user_paused = True
    # The record counts this run's failure but does not decide the auto-pause: its
    # limit is the one the run started with, and a limit saved during the run wins.
    # The job the store hands back decides it (:func:`pause_at_stored_limit`).
    record.settlement_failure_counted = count_failure and record.count_failure()
    record.settlement_pause_audited = False
    if (record.command or record.script) and not result_produced:
        record.last_result = ""
    return record


def pause_at_stored_limit(target: CronJob, record: CronJob) -> None:
    """Auto-pause ``target`` when the failure ``record`` counted reaches ``target``'s limit.

    ``target`` carries the limit as stored, so a limit saved during the run decides
    in both directions: 0 or a raised limit keeps the job running, a lowered one
    pauses it. A record that counted no failure never pauses a job. The caller
    derives ``enabled``; the audit row waits for the save (:func:`audit_landed_pause`).
    """
    if record.settlement_failure_counted and target.failure_limit_reached():
        target.auto_paused = True


def audit_landed_pause(saved: CronJob, record: CronJob) -> None:
    """Write the one ``cron_auto_pause`` row once a save returned with ``record``'s pause.

    ``saved`` is the job as that save wrote it. The pause is the record's when the
    record counted the failure, did not carry a pause of its own (a failed one-shot's
    is audited at the freeze), and ``saved`` holds the record's count at or past its
    stored limit. The record's marker keeps a later save of the same record (a held
    retry, or the immediate merge after a tick landed it) from writing a second row.
    """
    if record.settlement_pause_audited or not record.settlement_failure_counted:
        return
    if record.auto_paused or not saved.auto_paused:
        return
    limit = saved.auto_pause_limit()
    if saved.consecutive_failures != record.consecutive_failures or limit is None:
        return
    if saved.consecutive_failures < limit:
        return
    record.settlement_pause_audited = True
    saved._audit_pause_change("auto_paused")


def newer_run_owns(claim: _RunClaim | None, target: CronJob, record: CronJob) -> bool:
    """Whether ``claim`` is a run claimed after ``record`` that executes on ``target``.

    Such a run owns ``target``'s pause state and failure count: the record was
    applied to the listed job before that run could claim it, so the run builds
    its count on the record's, and the record's copy would erase what the run
    added. The job's ``run_generation`` cannot tell this, because a run stamps its
    generation only on its frozen copy; the claim's generation can.

    A timer tick or a reload calls this on a worker thread, while the claims are
    loop-owned. It is a read of two attributes, and both are stamped at the run's
    first step, before the run can move the count, so a read that misses them sees
    a count the run has not moved yet.
    """
    return (
        claim is not None
        and claim.job is target
        and claim.generation is not None
        and claim.generation > record.run_generation
    )


def apply_settlement_record(
    target: CronJob, record: CronJob, *, land_park: bool = False, newer_run: bool = False
) -> None:
    """Apply a held settlement ``record`` to ``target``, the job the store handed back.

    A ``target`` at an older generation takes the run's fields
    (``apply_run_record``). One at the record's own generation takes only the
    record's pause, because a save of the list can carry the generation before
    the park reaches the store. A newer generation is a newer run and is left
    alone.

    The pause then follows three rules. A ``target`` with a newer
    ``user_pause_generation`` was paused or resumed after the record was
    frozen, and keeps ``enabled``, ``user_paused``, ``auto_paused`` and the
    failure count. Otherwise the record's auto-pause and failure count land,
    and ``target``'s own limit decides the pause that count causes
    (:func:`pause_at_stored_limit`). A ``target`` whose schedule changed keeps
    ``user_paused``, because the record's park belonged to the old schedule;
    on any other the record's park holds the job off the schedule (``enabled``
    False). ``user_paused`` stays as persisted until
    a save lands the park: only the held save's copy passes ``land_park``, so
    nothing reports a park the store did not take. The record never re-enables
    a job.

    ``newer_run`` (:func:`newer_run_owns`) says a run claimed after the record
    executes on ``target``. That run owns the pause state and the failure count,
    so neither lands; the park and ``enabled`` follow the rules above.
    """
    if target.run_generation > record.run_generation:
        return
    enabled, user_paused = target.enabled, target.user_paused
    auto_paused, failures = target.auto_paused, target.consecutive_failures
    if target.run_generation < record.run_generation:
        apply_run_record(target, record)
    if target.user_pause_generation > record.user_pause_generation:
        target.enabled, target.user_paused = enabled, user_paused
        target.auto_paused, target.consecutive_failures = auto_paused, failures
        return
    started = record.auto_paused_at_start
    owns = not newer_run and (started is None or auto_paused == started)
    if target.schedule != record.schedule:
        # The park belonged to the old schedule, so it does not land. The failure
        # the record counted still does, and the stored limit decides its pause.
        if owns:
            target.auto_paused = record.auto_paused
            target.consecutive_failures = record.consecutive_failures
            pause_at_stored_limit(target, record)
        target.user_paused = user_paused
        target.enabled = enabled and not target.auto_paused
        return
    if owns:
        target.auto_paused = record.auto_paused
        target.consecutive_failures = record.consecutive_failures
        pause_at_stored_limit(target, record)
    target.user_paused = user_paused or (land_park and record.user_paused)
    target.enabled = enabled and not (
        record.user_paused or target.user_paused or target.auto_paused
    )

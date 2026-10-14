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
    from kiro_crew.cron_service.model import CronJob


class TerminalSettlementPending(RuntimeError):
    """A scheduled terminal record still needs a durable store write."""

    def __init__(
        self,
        message: str,
        record: CronJob | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.record = record
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
    finally's failed one-shot auto-pause to ``job`` (audited once: the finally
    then finds the job paused). Whether the run was denied or never started is
    read off ``ran``, the object the run executed on: those flags are
    runtime-only, and a store reload during the run makes ``job`` a disk copy
    that has lost them.

    A ``park_one_shot`` request writes the park onto the record itself
    (``enabled`` False, ``user_paused`` True), so a held record carries the
    cancel's own decision rather than one re-derived from the record later.
    """
    origin = ran if ran is not None else job
    if dispatched and not (origin.fire_time_denied or origin.run_never_started):
        auto_pause_failed_one_shot(job, last_status)
    record = copy.copy(job)
    record.run_generation = run_generation
    record.last_status = last_status
    record.last_error = last_error
    record.last_run_ts = last_run_ts
    record.auto_paused_at_start = auto_paused_at_start
    if park_one_shot and record.schedule.kind == "at":
        record.enabled = False
        record.user_paused = True
    if count_failure:
        record.record_failure()
    if (record.command or record.script) and not result_produced:
        record.last_result = ""
    return record


def apply_settlement_record(target: CronJob, record: CronJob, *, land_park: bool = False) -> None:
    """Apply a held settlement ``record`` to ``target``, the job the store handed back.

    A ``target`` at an older generation takes the run's fields
    (``apply_run_record``). One at the record's own generation takes only the
    record's pause, because a save of the list can carry the generation before
    the park reaches the store. A newer generation is a newer run and is left
    alone.

    The pause then follows three rules. A ``target`` with a newer
    ``user_pause_generation`` was paused or resumed after the record was
    frozen, and keeps ``enabled``, ``user_paused``, ``auto_paused`` and the
    failure count. A ``target`` whose schedule changed keeps ``user_paused``.
    Otherwise the record's auto-pause lands, and its park holds the job off
    the schedule (``enabled`` False). ``user_paused`` stays as persisted until
    a save lands the park: only the held save's copy passes ``land_park``, so
    nothing reports a park the store did not take. The record never re-enables
    a job.
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
    if target.schedule != record.schedule:
        target.user_paused = user_paused
        target.enabled = enabled and not target.auto_paused
        return
    started = record.auto_paused_at_start
    if started is None or auto_paused == started:
        target.auto_paused = record.auto_paused
        target.consecutive_failures = record.consecutive_failures
    target.user_paused = user_paused or (land_park and record.user_paused)
    target.enabled = enabled and not (
        record.user_paused or target.user_paused or target.auto_paused
    )

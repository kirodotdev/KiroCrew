"""The loop-stall breaker: pause the job the previous gateway died running.

At start the service reads the last loop-stall dump's attribution
(:mod:`kiro_crew.stall_attribution`); when it names one cron job on the cron surface,
that job is paused before the timer can fire it again. Nothing here can fail the
service's start.

Each function here is a :class:`~kiro_crew.cron.CronService` member: the class binds
it by name, so it reads and writes the service's own state on the instance, and a
patch of the member on the class or on an instance reaches every caller. A name
tests patch on :mod:`kiro_crew.cron`, or one that module defines, is read through
it on each call (``seams``).

Instance fields read: ``_dir``, ``_dumps_dir``, ``_history``, ``_jobs``, ``_load_failed``,
``_loop``, ``quarantined_store``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew import cron_inflight, stall_attribution
from kiro_crew.cron_history import CronRunRecord
from kiro_crew.cron_service.execution import interruption_note
from kiro_crew.cron_service.store import CronStoreUnreadable

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


# ── Loop-stall breaker ──


def _apply_loop_stall_breaker(self: CronService) -> str | None:
    """Pause the job the previous gateway died running. WORKER-THREAD ONLY.

    The loop-stall watchdog hard-exits the gateway; the run in flight left an
    in-flight marker (:mod:`kiro_crew.cron_inflight`) and the dump names the
    PID. When the newest dump's wedged stack is a cron turn AND exactly one
    abandoned marker carries that PID, that job is the one whose input
    stalled the loop -- and left enabled it is due again as soon as the
    timer arms, which is the hourly crash loop a user reported. It is parked
    ``auto_paused`` with a ``last_error`` that says why and how to resume,
    audited like the failure-count auto-pause. Ambiguous evidence (several
    runs in flight, no marker, a non-cron surface) pauses nothing; the doctor
    prints the same attribution so the operator can decide.

    What the markers said is recorded (``cron_inflight.record_attribution``)
    BEFORE they are swept, because the doctor and the restart notification
    read the same evidence afterwards: an ambiguous verdict the breaker
    declined to act on must still reach the operator who can act on it. Each
    run the newest dump does not explain is filed instead
    (:meth:`_file_cut_run`) before its marker goes: with no dump at all, or
    with a dump an earlier boot settled (and swept its own markers for), that
    is every run a marker names.

    A dump is CLAIMED, and its markers swept, only once the breaker has
    reached a verdict that survives a restart. A pause the store refused to
    persist leaves the job enabled and still due, so claiming it would let the
    next boot -- the one whose store is readable again -- skip the job and
    re-run the crash the breaker exists to stop. That boot is the only one
    that retries, so it keeps both the claim and the evidence intact.
    Returns the paused job id, or None.

    Every failure here is swallowed. This is a safety net that runs BEFORE
    the timer arms, so a fault in it must cost at most the net: letting one
    propagate would fail ``start()`` and leave the operator with no scheduler
    at all, which is strictly worse than the crash loop it is trying to stop.
    """
    try:
        return self._loop_stall_breaker_verdict()
    except Exception:
        logger.warning("loop-stall breaker skipped after an unexpected failure", exc_info=True)
        return None


def _loop_stall_breaker_verdict(self: CronService) -> str | None:
    """The breaker's body. See :meth:`_apply_loop_stall_breaker`, which owns
    the promise that nothing in here can fail the cron service's start."""
    try:
        attribution = stall_attribution.attribute_latest_stall(self._dir, self._dumps_dir)
    except Exception:
        logger.debug("loop-stall attribution failed; breaker skipped", exc_info=True)
        return None
    paused: str | None = None
    # Only a dump no earlier boot claimed gets a verdict: a claimed one's
    # evidence has been recorded, and re-pausing a job the operator resumed is
    # what the claim prevents.
    if attribution is not None and cron_inflight.read_claim(self._dir) != attribution.dump.name:
        recorded = True
        if attribution.candidates or attribution.unrelated_abandoned:
            recorded = cron_inflight.record_attribution(
                self._dir,
                attribution.dump.name,
                attribution.candidates,
                attribution.unrelated_abandoned,
            )
        # A host-wide pause ended the gateway whatever job was in flight, so the
        # job is not what the breaker exists to stop.
        if (
            attribution.is_cron
            and attribution.job is not None
            and attribution.host_pause_secs is None
        ):
            settled, paused = self._pause_for_loop_stall(attribution)
            if not settled:
                return None
        # Touch markers only behind a written claim AND a readable record. With
        # the claim missing, the next boot would re-derive this verdict from the
        # retained markers (and the record), and the pause it reaches is
        # idempotent: the job's own ``last_error`` names the dump, which settles
        # it even after the operator has resumed the job. With the record
        # missing, the markers are the only copy of what the doctor has to show.
        if not (recorded and cron_inflight.write_claim(self._dir, attribution.dump.name)):
            return paused
    # The dump's own runs are its candidates, and its verdict has consumed them.
    # Every other abandoned marker names a run cut short by an exit that left no
    # dump of its own (a crash, a kill, a power loss): a dump stays on disk long
    # after its stall, so the newest one can be days older than the exit that
    # cut these. Each such run is filed before its marker goes.
    explained = {m.path for m in attribution.candidates} if attribution is not None else set()
    for marker in cron_inflight.abandoned_markers(self._dir):
        if marker.path in explained or self._file_cut_run(marker):
            cron_inflight.remove_markers([marker])
    return paused


def _pause_for_loop_stall(
    self: CronService, attribution: "stall_attribution.StallAttribution"
) -> tuple[bool, str | None]:
    """``(settled, paused job id)`` for the job *attribution* names.

    *settled* is False ONLY when the verdict could not be recorded -- an
    unreadable or unwritable store -- which is the one case a later boot must
    retry. A job that is already paused, or gone from the store, is settled
    with nothing paused: there is no action left for any boot to take.
    """
    marker = attribution.job
    if marker is None:  # pragma: no cover - the caller checks
        return (True, None)
    with self._file_lock():
        self._sync()
        if self._load_failed:
            logger.warning("loop-stall auto-pause deferred: cron store not readable")
            return (False, None)
        job = next((j for j in self._jobs if j.id == marker.job_id), None)
        if job is None or job.auto_paused or job.user_paused:
            return (True, None)
        if job.last_error and attribution.dump.name in job.last_error:
            # Paused for THIS dump on an earlier boot and since resumed by
            # the operator (resume keeps ``last_error``): the verdict was
            # recorded in the store itself, so a lost claim file cannot
            # turn a resume into a second pause.
            return (True, None)
        before = (
            job.enabled,
            job.auto_paused,
            job.last_status,
            job.last_run_ts,
            job.last_error,
        )
        job.enabled = False
        job.auto_paused = True
        job.last_status = "error"
        job.last_run_ts = marker.started_at
        job.last_error = (
            "Paused: the gateway was terminated by the loop-stall watchdog while this "
            f"job was running (crash dump {attribution.dump.name}). Inspect the command "
            "the run was about to execute, then resume with "
            f"`kirocrew cron resume {job.id}`."
        )
        try:
            self._save()
        except (OSError, CronStoreUnreadable) as exc:
            # The two faults ``_save`` raises for a store it could not WRITE:
            # ``CronStoreUnreadable`` (a ``ValueError`` subclass) when the store
            # went unreadable, and a bare ``OSError`` (ENOSPC/EROFS/EIO out of
            # ``atomic_write``) when the write itself failed. The original
            # ``except CronStoreUnreadable`` missed the OSError: the in-memory
            # pause fields stayed mutated while disk kept the job enabled, and
            # the breaker went on to claim the dump and sweep the markers, so
            # the next boot (seeing the claim) would skip the job the pause
            # never persisted for. Caught as exactly this pair rather than a
            # bare ``ValueError`` so an unrelated ``ValueError`` (a serialization
            # bug in ``encode_store``, say) is NOT reported as an unwritable
            # store, and rather than ``BaseException`` so a
            # ``KeyboardInterrupt``/``SystemExit``/``CancelledError`` propagates
            # (the re-raising sibling cores let it through too). The in-memory
            # job must match the store it could not write: still enabled, still
            # due, so this session schedules it as the disk says and the next
            # boot retries the pause (not settled).
            (
                job.enabled,
                job.auto_paused,
                job.last_status,
                job.last_run_ts,
                job.last_error,
            ) = before
            logger.warning("loop-stall auto-pause not persisted: %s", exc)
            return (False, None)
        job._audit_pause_change("auto_paused_loop_stall")
    logger.error(
        "Cron job '%s' (%s) auto-paused: the previous gateway was hard-exited by the "
        "loop-stall watchdog while running it (%s). Resume with `kirocrew cron resume %s` "
        "once the cause is fixed.",
        job.name,
        job.id,
        attribution.dump.name,
        job.id,
    )
    return (True, job.id)


def _file_cut_run(self: CronService, marker: cron_inflight.RunningMarker) -> bool:
    """File the run *marker* names, which a hard exit cut short. WORKER-THREAD ONLY.

    As ``stop()`` files a run it cut (``close_run``): an ``every`` job's
    ``last_run_ts`` moves forward to the run's start, so it waits for its next
    slot, then a ``cancelled`` history row with the interruption note, and the
    interrupt notice, which honours ``silent``. The gateway died at a time
    nothing recorded, so the row ends where it began. The row carries the
    marker's run token, as every row a run writes does, so a run that
    already has its row is never filed twice. True once the marker can go;
    False keeps it as the evidence for the next boot: the store that would
    name the job is unreadable or was moved aside on this boot, the job's
    history is unreadable or cannot be written, or the stamp or the row was
    not written.
    """
    run = marker.run_token
    with self._file_lock():
        self._sync()
        # A store moved aside on this boot reloads empty: its jobs only look removed.
        if self._load_failed or self.quarantined_store is not None:
            return False
        job = next((j for j in self._jobs if j.id == marker.job_id), None)
        if job is None:
            # Nothing to file into: a removed job's history went with it.
            return True
        if run is not None:
            try:
                if self._history.has_run_sync(job.id, run):
                    return True
            except OSError as exc:
                logger.warning("cut cron run not filed: %s", exc)
                return False
        if job.schedule.kind == "every" and (job.last_run_ts or 0.0) < marker.started_at:
            previous = job.last_run_ts
            job.last_run_ts = marker.started_at
            try:
                self._save()
            except (OSError, CronStoreUnreadable) as exc:
                # The pair ``_save`` raises for a store it could not write
                # (see ``_pause_for_loop_stall``).
                job.last_run_ts = previous
                logger.warning("cut cron run not filed: %s", exc)
                return False
    note = interruption_note(dispatched=True)
    record = CronRunRecord(
        job_id=job.id,
        started_at=marker.started_at,
        finished_at=marker.started_at,
        status="cancelled",
        summary=note,
        error=note,
    )
    if run is not None:
        record.run_id = run
    if not self._history.append_sync(record):
        return False
    if self._loop is not None:
        self._loop.call_soon_threadsafe(self._alert_interrupt, job, note)
    return True

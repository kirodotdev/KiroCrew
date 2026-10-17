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

Instance fields read: ``_dir``, ``_dumps_dir``, ``_jobs``, ``_load_failed``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew import cron_inflight, stall_attribution
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
    declined to act on must still reach the operator who can act on it.

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
    if attribution is None:
        cron_inflight.sweep_abandoned_markers(self._dir)
        return None
    if cron_inflight.read_claim(self._dir) == attribution.dump.name:
        # Settled on an earlier boot: its evidence has been recorded, and
        # re-pausing a job the operator resumed is what the claim prevents.
        cron_inflight.sweep_abandoned_markers(self._dir)
        return None
    recorded = True
    if attribution.candidates or attribution.unrelated_abandoned:
        recorded = cron_inflight.record_attribution(
            self._dir,
            attribution.dump.name,
            attribution.candidates,
            attribution.unrelated_abandoned,
        )
    paused: str | None = None
    if attribution.is_cron and attribution.job is not None:
        settled, paused = self._pause_for_loop_stall(attribution)
        if not settled:
            return None
    # Sweep only behind a written claim AND a readable record. With the claim
    # missing, the next boot would re-derive this verdict from the retained
    # markers (and the record), and the pause it reaches is idempotent: the
    # job's own ``last_error`` names the dump, which settles it even after
    # the operator has resumed the job. With the record missing, the
    # markers are the only copy of what the doctor has to show.
    if recorded and cron_inflight.write_claim(self._dir, attribution.dump.name):
        cron_inflight.sweep_abandoned_markers(self._dir)
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

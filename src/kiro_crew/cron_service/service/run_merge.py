"""Land a finished run's result in the cron store.

The merge reloads the store under the lock and copies the run's record onto the
stored job; a record the merge could not save is held and re-applied across
reloads until a save persists it, and a terminal state (timeout, reap, cancel) is
merged with the reason it could not park a one-shot.

Each function here is a :class:`~kiro_crew.cron.CronService` member: the class binds
it by name, so it reads and writes the service's own state on the instance, and a
patch of the member on the class or on an instance reaches every caller. A name
tests patch on :mod:`kiro_crew.cron`, or one that module defines, is read through
it on each call (``seams``).

Instance fields written: ``_held_run_records``, ``_pending_removals``.

Instance fields read: ``_held_run_records_guard``, ``_jobs``, ``_load_failed``,
``_path``.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING

from kiro_crew.cron_service.execution import apply_run_record, pause_changed_since_start
from kiro_crew.cron_service.model import CronJob
from kiro_crew.cron_service.store import CronStoreBusy, CronStoreUnreadable, _read_job_records

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


def _hold_run_record(self: CronService, job: CronJob, record: CronJob) -> None:
    """Keep a completed run's record until a save under the lock persists it.

    Loop-side; the finalizer calls it before the result merge. The merge
    is best-effort: a lock that stays contended raises
    :class:`CronStoreBusy` before anything is merged, and the record then
    lives only in memory. A reload of a store another writer changed
    replaces the job with its disk copy, which never received the run, so
    an ``every`` job is due again early and a fired one-shot is enabled
    again. Every reload re-applies a held record
    (:meth:`_reapply_held_run_records`), and the next save under the lock
    (the merge, the timer tick, :meth:`stop`) persists and releases it.

    Holds of one job arrive in run order (the next run cannot start before
    this finalizer gets here), so the newest record replaces a held one.
    ``job`` is the object the run wrote its fields on. The record is applied
    here to the job listed under its id, whether that is ``job`` itself or
    the disk copy a reload swapped in during the run, which adds the
    generation and the persisted spellings (``user_paused`` for a fired
    one-shot), so any save of it lands the record and a re-apply skips it.
    A timer tick the store lock also refuses works from this list, so the
    listed job must carry the run before this finalizer yields, or that
    tick finds it due and runs it again.

    A reload on a worker thread can replace the list at any point here. The
    record is registered first, and the lookup and the apply run under the
    same guard as a reload's re-apply: a reload that re-applies after the
    registration applies the record to its own copies, and one that
    re-applied before it had already swapped its copies into the list
    (``_load`` replaces the list before it re-applies), so the lookup finds
    them. Under the one guard, the two applies to a job never interleave.
    """
    with self._held_run_records_guard:
        self._held_run_records[record.id] = record
        listed = next((j for j in self._jobs if j.id == record.id), None)
        if listed is not None and listed.run_generation < record.run_generation:
            self._apply_held_run_record(listed, record)


def _apply_held_run_record(target: CronJob, record: CronJob) -> None:
    """Apply a held record to ``target``; a schedule edited since keeps its pause state.

    ``apply_run_record`` derives a fired one-shot's disable from the record's
    schedule. When ``target`` carries a different schedule from the one that
    fired (another writer made the one-shot recurring, or moved it to a later
    time), that disable belongs to the old schedule: the run's result and
    generation are taken, the current schedule's ``enabled`` and
    ``user_paused`` are kept.
    """
    enabled, user_paused = target.enabled, target.user_paused
    apply_run_record(target, record)
    if target.schedule != record.schedule:
        target.user_paused = user_paused
        target.enabled = enabled and not target.auto_paused


def _release_held_run_record(self: CronService, record: CronJob) -> None:
    """Drop ``record`` after a save persisted it, unless a newer one is held."""
    with self._held_run_records_guard:
        if self._held_run_records.get(record.id) is record:
            del self._held_run_records[record.id]


def _reapply_held_run_records(self: CronService) -> None:
    """Apply held run records to the job list. Under the store lock.

    Same fence as :meth:`_merge_job_result`: a record applies only to a job
    whose generation is older, so it never overwrites a newer run another
    writer stored, nor a job that already carries it. The lookup and the
    applies run under the guard, as the hold's do (:meth:`_hold_run_record`).
    """
    with self._held_run_records_guard:
        self._apply_held_run_records_to(self._jobs)


def _apply_held_run_records_to(self: CronService, jobs: list[CronJob]) -> None:
    """Apply every held run record to its job in ``jobs``. The caller holds the guard."""
    by_id = {j.id: j for j in jobs}
    for record in self._held_run_records.values():
        target = by_id.get(record.id)
        if target is not None and target.run_generation < record.run_generation:
            self._apply_held_run_record(target, record)


def _save_held_run_records_locked(self: CronService) -> None:
    """Persist and release the held run records. MUST hold the store lock, after ``_sync``.

    A job whose generation equals its record's was given the record, but a
    run that started since resets its status in place (``_execute``) without
    a new generation. So the save writes a copy of such a job with the record
    applied, and the running job itself is left as it is. The copy keeps the
    live job's pause state and failure count: the job already carries the
    record's values for them, so a difference is a user edit made since the
    hold (a re-enable, a resume), and the edit wins.
    """
    with self._held_run_records_guard:
        held = list(self._held_run_records.values())
    if not held or self._load_failed:
        return
    self._reapply_held_run_records()
    by_id = {record.id: record for record in held}
    jobs: list[CronJob] = []
    for job in self._jobs:
        record = by_id.get(job.id)
        if record is not None and job.run_generation == record.run_generation:
            live = job
            job = copy.copy(job)
            apply_run_record(job, record)
            job.enabled = live.enabled
            job.user_paused = live.user_paused
            job.auto_paused = live.auto_paused
            job.consecutive_failures = live.consecutive_failures
        jobs.append(job)
    self._save(jobs)
    for record in held:
        self._release_held_run_record(record)


def _flush_held_run_records(self: CronService) -> None:
    """Persist the held run records for :meth:`stop`. WORKER-THREAD ONLY.

    Best-effort: a store still contended or unwritable leaves them unsaved.
    """
    try:
        with self._file_lock():
            self._sync()
            self._save_held_run_records_locked()
    except (CronStoreBusy, OSError) as exc:
        logger.warning("Cron: held run records not saved at stop: %s", exc)


def _merge_job_result(self: CronService, job: CronJob) -> None:
    """Merge a single job's runtime state back to disk.

    Enters the bounded sync :meth:`_file_lock` (which spins with
    ``time.sleep`` under contention) and may raise :class:`CronStoreBusy`.
    MUST NOT be called directly on the gateway event loop — its sole
    loop-side caller, :meth:`_run_job_isolated`, offloads it via
    ``asyncio.to_thread`` so the spin never parks the loop. Sync/CLI
    contexts with no running loop may call it directly.

    Fenced by run generation (``run_generation``, see :class:`CronJob`):
    the record's fields are applied only while its generation is not
    below the one the store holds, compared under the lock after the
    ``_sync()``. The finalizer calls this behind the release of its claim,
    so a replacement run can be accepted, complete and merge while this
    call is still waiting for the lock; an older record landing after it
    would revert status, error, result and the failure counter to a run
    that is not the last one. ONLY the field copies are fenced: the
    one-shot consume below is owed by this run's completion whatever
    landed since -- a replacement's terminal merge writes status fields and
    retires nothing -- and a stale record that skipped it would leave a
    consumed ``delete_after_run`` at-job enabled on disk, due again on
    every tick. The consume already tolerates the row being gone.
    """
    with self._file_lock():
        self._sync()
        by_id = {j.id: j for j in self._jobs}
        if job.id in by_id and job.run_generation < by_id[job.id].run_generation:
            logger.debug(
                "Cron: not applying the record of an older run of '%s' (generation %d);"
                " the store already holds generation %d",
                job.name,
                job.run_generation,
                by_id[job.id].run_generation,
            )
        elif job.id in by_id:
            apply_run_record(by_id[job.id], job)
        # A fire-time-DENIED run is a policy refusal, not a completed run:
        # deleting the one-shot here would make the documented
        # resume-on-policy-loosening semantic impossible for at-jobs.
        # A run that never STARTED is the same story for a different reason --
        # every pool worker was busy for the whole queue budget -- so consuming
        # the one-shot would destroy scheduled work that never got a chance to
        # run. Only the delete is suppressed: unlike a policy denial this needs
        # no operator action, so the job stays enabled and simply retries.
        # TWO signals, because the queue and the audit ask different
        # questions and a corrupt store answers them differently.
        #   delete_owed      -- is a consume OWED by this path at all?
        #   removed_one_shot -- did this path actually remove a PRESENT job?
        # Deriving both from presence conflated them: `_load` degrades an
        # unreadable store to an empty job list WITHOUT raising, so presence
        # is exactly what a corrupt store destroys, and the deferred queue
        # below then never fired for a delete that was still owed on disk.
        # A failed run is parked, not consumed. Its auto-pause is in the run
        # record the finalizer holds until a save lands it, so a save fault
        # leaves the park to the next save under the lock, as the busy path
        # does.
        delete_owed = (
            job.delete_after_run
            and job.last_status != "error"
            and not (job.fire_time_denied or job.run_never_started)
        )
        removed_one_shot = False
        restore: list[tuple[CronJob, str]] = []
        consumed_row = False
        if delete_owed:
            # Presence check keeps the audit honest: a Done-script one-shot
            # already removed by the gateway path leaves nothing to delete
            # here, and that path owns the audit record.
            removed_one_shot = job.id in by_id
            # BACKGROUND writer: a failed epoch bump must not crash the
            # run path, but the delete is skipped — the deferred drain
            # retries once the epoch state heals, never deleting a
            # still-live grant record. The job has ALREADY RUN, so the
            # held delete needs the same two-layer guard as
            # `defer_removal`: the run path deliberately leaves `enabled`
            # untouched for a delete_after_run at-job (the delete is what
            # stops it), so without a persisted pause the save below
            # writes it back live and every tick re-fires it until the
            # bump succeeds; and without the queue entry no later pass
            # ever retries the delete. `user_paused` is the persisted
            # spelling `enabled` is re-derived from on reload.
            try:
                self._bump_grant_epochs_for({job.id})
            except (OSError, ValueError):
                logger.warning(
                    "One-shot delete held for %s: grant-epoch bump failed",
                    job.id,
                    exc_info=True,
                )
                removed_one_shot = False
                if job.id in by_id:
                    by_id[job.id].enabled = False
                    by_id[job.id].user_paused = True
                self._pending_removals.add(job.id)
            else:
                # Consuming the one-shot retires its principal cron:<job id>,
                # so a child that job created is released in the SAME save --
                # the fifth removal core alongside the four locked cores.
                # _remove_job_rows filters the row out before releasing so
                # the job cannot release itself; rolled back below if the
                # save fails.
                restore = self._remove_job_rows({job.id})
                consumed_row = True
        # BACKGROUND writer: a job has already run, so an unreadable store
        # must not surface as a job-runner crash. The run result is lost,
        # which is strictly better than clobbering the store.
        try:
            self._save()
        except BaseException as exc:
            # EVERY save failure rolls back the child release, not just
            # CronStoreUnreadable: the release lives in memory only until the
            # save lands it, so a bare OSError (ENOSPC/EROFS/EIO out of
            # atomic_write) that left the cleared owners in place while disk
            # still named the old owner would let the next successful save
            # persist a release nothing asked for, from a consume that never
            # reached disk.
            if consumed_row:
                for child, previous_owner in restore:
                    child.session_key = previous_owner
                # The consumed row is still filtered out of self._jobs and
                # only a reload can put it back, so drop the fingerprint to
                # force one -- otherwise the next _save skips the reload and
                # persists the removal this path was told had failed,
                # completing the parent's removal while its children keep the
                # ownership just rolled back.
                self._reset_fingerprint()
            # Queue the consume for EVERY save failure, not just the
            # store-unreadable one: the fingerprint reset above forces a
            # reload, and the save never landed, so the disk copy is still
            # enabled. Without a queue entry the reloaded one-shot comes back
            # enabled and runs a second time on the next tick. Keyed on
            # delete_owed, NOT presence: an absent id proves nothing about
            # whether the delete is owed, and the drain intersects the queue
            # with what is present and drops the rest.
            if delete_owed:
                self._pending_removals.add(job.id)
            if isinstance(exc, CronStoreUnreadable):
                # Return WITHOUT auditing: the emit below records only a SAVED
                # removal, and nothing was saved. Auditing here would file a
                # removal record for a delete that never reached disk. The
                # drain retries the queued consume once the store is readable.
                logger.warning("Cron job result not persisted: %s", exc)
                return
            # Anything else is a real write fault, not the tolerated
            # store-unreadable case: surface it rather than reporting a quiet
            # no-op after the disk refused the write.
            raise
        self._release_held_run_record(job)
    if removed_one_shot:
        # The delete_after_run consume is an automated removal with no
        # handler-level caller, so the emit lives with the removal.
        # AFTER the lock: only a saved removal is recorded, and
        # the sel call never extends the store-lock hold.
        self.audit_one_shot_removal(job.id, "cron_run_complete")


def _merge_terminal_state_locked(
    self: CronService,
    job_id: str,
    *,
    last_status: str,
    last_error: str,
    last_run_ts: float,
    run_generation: int,
    result_produced: bool = False,
    count_failure: bool = False,
    park_one_shot: bool = False,
    auto_paused_at_start: bool | None = None,
) -> str | None:
    """Persist a job's terminal runtime state under the store lock.

    Used for the reaper timeout (:meth:`_force_reap`) and user cancel
    (:meth:`cancel`) paths. Mutating the in-memory job and calling a bare,
    unlocked ``self._save()`` directly on the event loop would open a
    lost-update race: between a concurrent
    ``add_job_async``/``update_job_async`` worker's ``_sync`` and its
    ``_save``, the unlocked save would re-serialize a stale ``self._jobs``
    and silently drop the just-added/updated job from ``crons.json``.

    WORKER-THREAD ONLY. Mirrors :meth:`_merge_job_result`: enters the
    bounded sync :meth:`_file_lock` (whose spin does ``time.sleep`` and may
    raise :class:`CronStoreBusy`), ``_sync()``s FIRST so any concurrent
    worker's persisted job list is reloaded, then applies the terminal
    fields to the disk copy and ``_save()``s — the whole read-modify-write
    is one lock transaction. Both loop-side callers offload it via
    ``asyncio.to_thread`` so the spin never parks the gateway loop. A
    missing id (removed meanwhile) is a no-op.

    Fenced by run generation like :meth:`_merge_job_result`:
    ``run_generation`` is the number the caller drew for the run this
    record is for, and the record is skipped (debug log, nothing saved)
    when the store already holds a higher one. Both callers release the
    run's claim before they offload this call, so a Run
    accepted in that gap can complete and merge first; applying the older
    record would persist that run's success as the cancellation or timeout
    that came before it. Returning early here skips nothing owed: unlike
    ``_merge_job_result`` this helper writes only the three status fields
    (plus clearing a command/script job's carried result, the failure
    count when ``count_failure``, and the one-shot park when
    ``park_one_shot``), and the save after them has nothing to
    record once they are skipped.

    Returns None only once a ``_save()`` holding this record has returned,
    with the park in it when ``park_one_shot`` asks for one. Every other
    path returns why it was not saved, so a caller never reports a park the
    store did not take: the job missing from the store (removed, or an
    unreadable store loaded as empty), a newer run's record, a disk copy whose
    schedule is not a one-shot, or an unreadable store refusing the save (logged,
    not raised). A busy store (``CronStoreBusy``) and any other write error
    raise, after a failed save has taken the park back off the disk copy.
    Nothing saves a park later. A park request also leaves the job as a
    reload would show it: at a newer run's record, and for a disk copy whose
    schedule is not a one-shot, the caller's provisional hold comes off, and a
    failed save restores the hold only onto a one-shot. The caller words every
    end from that job (:func:`_cancelled_park_report`).
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    with self._file_lock():
        self._sync()
        by_id = {j.id: j for j in self._jobs}
        target = by_id.get(job_id)
        if target is None:
            if self._load_failed:
                return "the cron store could not be read"
            return seams._PARK_JOB_REMOVED
        if run_generation < target.run_generation:
            logger.debug(
                "Cron: not applying the terminal record of an older run of '%s'"
                " (generation %d); the store already holds generation %d",
                target.name,
                run_generation,
                target.run_generation,
            )
            if park_one_shot:
                # The newer record decides whether the job runs again, so the
                # caller's provisional hold comes off as a reload derives it.
                target.enabled = not (target.user_paused or target.auto_paused)
            return seams._PARK_NEWER_RUN
        target.run_generation = run_generation
        target.last_status = last_status
        target.last_error = last_error
        target.last_run_ts = last_run_ts
        # A one-shot whose run the user cancelled is parked like a
        # fire-time-denied one: its due time has passed, so leaving it
        # enabled would run it again on the next tick. Disabled, so the user
        # can re-enable it. Written to the disk copy for the save below; a
        # failed save takes ``user_paused`` back off, so a park the store
        # did not take is never reported, and leaves the caller's
        # provisional hold (``enabled`` False) in place. The reaper parks a
        # plain one-shot it kills the same way.
        parking = park_one_shot and target.schedule.kind == "at"
        if park_one_shot and not parking:
            # Recurring in the store: it keeps that schedule, so the caller's
            # provisional hold comes off as a reload derives it, before the
            # snapshot below, so a failed save does not put it back.
            target.enabled = not (target.user_paused or target.auto_paused)
        # Snapshotted before the park and the failure count, so a failed
        # save leaves the job as the disk says.
        counted = (target.enabled, target.auto_paused, target.consecutive_failures)
        paused_before = target.user_paused
        if parking:
            target.enabled = False
            target.user_paused = True
        # Counted on the disk copy under the lock, so the failure count and
        # any auto-pause it triggers persist with the terminal record. Not
        # when the store's pause changed since the run started
        # (``auto_paused_at_start``): a resume issued during the run gave
        # the job fresh attempts, and a run from before it does not spend one.
        if count_failure and not pause_changed_since_start(target, auto_paused_at_start):
            target.record_failure()
        # A command/script run that produced nothing must not show the
        # previous run's result beside this error. The caller passes the
        # flag because a reload in _sync() drops the runtime-only marker.
        # Agent jobs keep theirs on purpose as dedup context.
        if (target.command or target.script) and not result_produced:
            target.last_result = ""
        # BACKGROUND writer: reached from the reaper timeout and user
        # cancel. An unreadable store must not abort the reaper loop.
        try:
            self._save()
        except Exception as exc:
            # Keep scheduling as the disk says (like the loop-stall breaker):
            # a pause the store did not take must not stop the job in memory.
            target.enabled, target.auto_paused, target.consecutive_failures = counted
            target.user_paused = paused_before
            if parking:
                target.enabled = False  # the caller's provisional hold
            if not isinstance(exc, CronStoreUnreadable):
                raise
            logger.warning("Cron terminal state not persisted: %s", exc)
            return "the cron store could not be read"
    if park_one_shot and not parking:
        return seams._PARK_NOT_ONE_SHOT
    return None


def _saved_job_state(self: CronService, job_id: str) -> str | None:
    """``"absent"`` or ``"paused"`` when the store as saved shows the job cannot run
    again, read under the store lock; None when it can, or when the store is busy or
    unreadable. Worker-thread only: the lock spins."""
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    try:
        with self._file_lock():
            records, readable = _read_job_records(self._path)
    except Exception:
        return None
    record = next((j for j in records if j.get("id") == job_id), None)
    if not readable or (record is not None and seams._record_is_enabled(record)):
        return None
    return "absent" if record is None else "paused"

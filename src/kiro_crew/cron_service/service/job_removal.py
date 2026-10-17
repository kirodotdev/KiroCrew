"""Remove cron jobs: one, a batch, every job an owner holds, or a one-shot queued at fire.

Every removal runs under one store lock, kills the removed jobs' secret grants,
releases the jobs they own, deletes their run history and is audited after the
lock; a one-shot that ran is queued by :meth:`~kiro_crew.cron.CronService.defer_removal`
and removed by the next timer tick's drain.

Each function here is a :class:`~kiro_crew.cron.CronService` member: the class binds
it by name, so it reads and writes the service's own state on the instance, and a
patch of the member on the class or on an instance reaches every caller. A name
tests patch on :mod:`kiro_crew.cron`, or one that module defines, is read through
it on each call (``seams``).

Instance fields written: ``_jobs``, ``_pending_removals``.

Instance fields read: ``_load_failed``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

from kiro_crew import cron_script
from kiro_crew.cron_service.model import CronJob
from kiro_crew.cron_service.store import CronStoreUnreadable

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


def remove_job(
    self: CronService,
    job_id: str,
    *,
    actor: str,
    source: str,
    one_shot_path: str | None = None,
) -> bool:
    """Remove a job by ID.

    ``actor`` and ``source`` are required so every caller-requested
    removal is attributable at this mutation seam. Automated one-shot
    callers additionally provide ``one_shot_path`` to retain their
    distinct audit outcome and path discriminator.

    Raises :class:`CronStoreBusy` on lock contention; see
    :meth:`remove_job_async` for the event-loop-safe variant.

    A caller-requested removal also deletes the job's run history, whichever
    surface asked for it. An automated one-shot removal keeps it: the run
    path appends that run's record after the removal.
    """
    ok = self._remove_job_locked(job_id)
    if ok and one_shot_path is None:
        self._delete_history_of([job_id])
    self._audit_requested_removal(
        job_id,
        removed=ok,
        actor=actor,
        source=source,
        one_shot_path=one_shot_path,
    )
    if ok:
        self._arm_timer()
    return ok


async def remove_job_async(
    self: CronService,
    job_id: str,
    *,
    actor: str,
    source: str,
    one_shot_path: str | None = None,
) -> bool:
    """Event-loop-safe :meth:`remove_job`: the lock+save runs off the loop.

    Raises :class:`CronStoreBusy` (retryable) on sustained contention.
    """
    ok = await asyncio.to_thread(self._remove_job_locked, job_id)
    if ok and one_shot_path is None:
        await asyncio.to_thread(self._delete_history_of, [job_id])
    self._audit_requested_removal(
        job_id,
        removed=ok,
        actor=actor,
        source=source,
        one_shot_path=one_shot_path,
    )
    if ok:
        self._arm_timer()
    return ok


async def remove_job_if_async(
    self: CronService,
    job_id: str,
    *,
    expected_revision: str,
    revision_of: Callable[["CronJob"], str],
    actor: str,
    source: str,
) -> str:
    """Compare-and-delete: remove *job_id* only while ``revision_of(job)`` still
    equals *expected_revision*, read and deleted under one store lock off the
    loop. Returns ``"removed"``, ``"not_found"`` or ``"changed"``; raises
    :class:`CronStoreBusy` on sustained contention.

    A removal here is caller-requested, so it deletes the job's run history
    like :meth:`remove_job_async` does.
    """
    outcome = await asyncio.to_thread(
        self._remove_job_if_locked, job_id, expected_revision, revision_of
    )
    if outcome == "removed":
        await asyncio.to_thread(self._delete_history_of, [job_id])
    if outcome == "changed":
        self._audit_refused_removal(job_id, actor=actor, source=source)
    else:
        self._audit_requested_removal(
            job_id, removed=outcome == "removed", actor=actor, source=source
        )
    if outcome == "removed":
        self._arm_timer()
    return outcome


def _audit_refused_removal(self: CronService, job_id: str, *, actor: str, source: str) -> None:
    """Audit a compare-and-delete the live job's fingerprint refused."""
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    try:
        seams.sel.sel().log_api_access(
            caller=actor,
            operation="cron.remove",
            outcome="denied",
            source=source,
            resources=f"job_id={job_id} reason=changed_since_apply",
        )
    except Exception:
        logger.warning("SEL audit for cron removal failed (job %s)", job_id, exc_info=True)


def defer_removal(self: CronService, job_id: str) -> None:
    """Queue a one-shot job for removal on the next timer tick.

    Called on the event loop when an immediate :meth:`remove_job_async` for
    a completed ``delete_after_run`` / Done job raised :class:`CronStoreBusy`
    (the store lock stayed contended past the timeout). There is otherwise
    no caller to retry a fire-and-forget removal, so without this the
    finished job would linger ENABLED with its recurring schedule and
    re-fire on the next tick — duplicate execution and a duplicate
    user-visible notification.

    Two-layer guarantee:

    * **Immediate** — the job is disabled IN MEMORY right now so the very
      next :meth:`_on_timer` due-scan skips it (covers the window where the
      store is unchanged and ``_sync`` does not reload).
    * **Durable** — the id is recorded so :meth:`_drain_pending_removals_locked`,
      invoked from the timer tick's worker-thread transaction while it
      already holds the store lock (:meth:`_tick_scan_locked`), deletes it
      from disk. The drain runs BEFORE the due-scan, so even a
      ``_sync`` reload that re-enables the job (``enabled`` is derived from
      persisted pause flags, not the removal intent) cannot let it fire.

    Idempotent and cheap; safe to call for an id already queued.
    """
    for job in self._jobs:
        if job.id == job_id:
            job.enabled = False
            break
    self._pending_removals.add(job_id)


def _drain_pending_removals_locked(self: CronService) -> list[str]:
    """Delete jobs queued via :meth:`defer_removal`. MUST hold the store lock.

    Returns the ids actually removed (sorted, empty when nothing drained)
    so the caller can SEL-audit them after releasing the store lock.

    Called from :meth:`_tick_scan_locked` (the timer tick's worker-thread
    transaction) inside its ``_file_lock`` block, so the delete+save is
    serialized against every other
    mutator exactly like the other locked cores. Removes only the queued
    ids still present after the tick's ``_sync``; saves once iff something
    was actually removed (an all-missing queue never rewrites the file).
    An id absent from the reloaded store was already removed elsewhere, so dropping it
    is correct -- but ONLY when the load succeeded. Under ``_load_failed``
    the list is unknown rather than empty, so this returns before claiming
    (see the guard below) instead of intersecting against nothing.

    Cross-thread safety: this drain runs in the timer tick's WORKER thread
    while :meth:`defer_removal` adds ids from the EVENT-LOOP thread. The
    queue is claimed with a single-bytecode tuple swap
    (``pending, self._pending_removals = self._pending_removals, set()``),
    which is atomic under the GIL. A concurrent ``defer_removal`` add
    therefore lands EITHER in ``pending`` (drained now) OR in the fresh
    replacement set (drained next tick) — it can never fall into the gap
    between a read and a reset and be silently erased. ``present`` is
    computed AFTER the swap so the intersection sees the post-swap job
    list, and the in-memory disable performed by ``defer_removal`` keeps
    even an id deferred to the next tick from re-firing meanwhile.
    """
    if not self._pending_removals:
        return []
    if self._load_failed:
        # Return WITHOUT claiming. The claim below is a reset, and `present`
        # is built from `self._jobs`, which a failed load has emptied -- so
        # the intersection would be empty and the early return below would
        # drop the whole queue before ever reaching the `_save` its requeue
        # arm guards. Absence from an unloaded list means "unknown", not
        # "already removed", and dropping the intent lets the repaired store
        # re-run a completed one-shot and notify a second time.
        logger.warning("Deferred cron removals held: store unreadable, retrying next tick")
        return []
    # Atomic claim-and-reset (see docstring) — do NOT split into a read
    # (``& present``) followed by ``.clear()``; an id added between those
    # two steps would be erased without ever being deleted from disk, so
    # the completed one-shot would re-fire and re-notify.
    pending, self._pending_removals = self._pending_removals, set()
    present = {j.id for j in self._jobs}
    to_remove = pending & present
    if not to_remove:
        return []
    # BACKGROUND tick: a failed epoch bump must not crash the scan, but
    # it must also not let the delete proceed (the saved grant record
    # would be replayable once the epoch state heals). Requeue exactly
    # like the store-unreadable case and retry next tick.
    try:
        self._bump_grant_epochs_for(to_remove)
    except (OSError, ValueError):
        logger.warning("Deferred cron removals held: grant-epoch bump failed", exc_info=True)
        self._pending_removals |= pending
        return []
    # A Done()/delete_after_run job that self-removes retires its principal
    # just as a CLI remove does, so its children are released in the SAME
    # save (see _remove_job_rows).
    restore = self._remove_job_rows(to_remove)
    # BACKGROUND writer: this runs inside the due-scan, so an unreadable
    # store must not abort the tick and stop every other job. The deferred
    # delete simply stays pending until the store is readable again.
    try:
        self._save()
    except BaseException as exc:
        # EVERY save failure rolls back, not just CronStoreUnreadable. _save
        # also raises bare OSError (ENOSPC/EROFS/EIO out of atomic_write),
        # which a narrow `except CronStoreUnreadable` let past this block
        # entirely: the child owners stayed cleared in memory while disk still
        # named the old owner, the queue stayed empty, and the fingerprint
        # still matched the untouched file so no _sync would ever reload the
        # truth back. The next successful save then persisted the cleared
        # owners -- a silent release nothing asked for, from a removal that
        # never happened.
        for child, previous_owner in restore:
            child.session_key = previous_owner
        # REQUEUE, or the intent is lost outright. The claim above already
        # emptied the queue, so the comment's promise that the delete "stays
        # pending until the store is readable again" only holds if it is put
        # back: the next _sync reloads the job from the file that still holds
        # it, and a completed one-shot would run and notify a SECOND time.
        # Union rather than assignment -- a concurrent defer_removal may have
        # added to the fresh replacement set since the swap.
        self._pending_removals |= to_remove
        # self._jobs still has the removed rows filtered out, and only a
        # reload can put them back -- so drop the fingerprint to force one.
        # Without it the retry above is a promise nothing can keep: the next
        # drain intersects the requeued ids against a _jobs that omits them,
        # finds nothing to remove, and silently drops the intent.
        self._reset_fingerprint()
        if isinstance(exc, CronStoreUnreadable):
            logger.warning("Deferred cron removal not persisted: %s", exc)
            # Empty list, not a bare return: the caller SEL-audits what came
            # back, and nothing was durably removed.
            return []
        # Anything else is a real write fault, not the tolerated
        # store-unreadable case: surface it rather than reporting a quiet
        # no-op tick after the disk refused the write.
        raise
    for jid in to_remove:
        logger.info("Removed deferred one-shot cron job %s", jid)
    # SEL audit is the CALLER's job (post-lock): this method runs inside
    # the caller's ``_file_lock`` transaction, and the first ``sel()`` of a
    # process constructs the log (trust-dir + HMAC key read), which must
    # never extend the store-lock hold past the CronStoreBusy timeout.
    return sorted(to_remove)


def _bump_grant_epochs_for(self: CronService, removed_ids: set[str]) -> None:
    """Kill the secret grants of jobs about to be deleted from the store.

    A deleted job's record (mapping + active pin) survives as
    agent-readable history, and the store file is agent-writable:
    without an epoch bump, re-creating the job from the saved record
    would let the runner verify the old pin and inject the secret
    again. Bumping BEFORE the store swap keeps the revoke fence's
    fail-closed direction — and a FAILED bump (unwritable/corrupt epoch
    state) raises so the caller ABORTS the delete: deleting while the
    old epoch is still live would leave the saved record replayable the
    moment the epoch state heals. Owner-driven removal paths propagate
    the error; background ticks catch it and requeue/skip the delete
    instead of crashing the scan.
    """
    # An id with a LIVE epoch entry must bump even when the record no
    # longer carries grant fields: the store is agent-writable, so an
    # agent can CLEAR the fields, delete the job, and replay the saved
    # mapping+pin into a re-created job — the pin was minted under the
    # still-committed epoch. An id with neither grant fields nor an
    # epoch entry never had an active pin minted (pins are HMAC-keyed
    # and only the approval path commits entries), so skipping it is
    # safe and keeps the epoch file bounded across one-shot job churn.
    epoch_ids = cron_script.grant_epoch_ids() if removed_ids else set()
    for j in self._jobs:
        if j.id in removed_ids and (j.secret_env or j.secret_env_pin or j.id in epoch_ids):
            cron_script.bump_grant_epoch(j.id)


def _remove_job_rows(self: CronService, removed_ids: set[str]) -> list[tuple[CronJob, str]]:
    """Filter ``removed_ids`` out of ``self._jobs`` and cascade the release.

    The ONLY sanctioned spelling of a structural remove. Every site that
    drops a job's row from ``self._jobs`` must route through this helper so
    the removal and the release of the removed jobs' children land in the
    SAME save -- a bare list-comprehension filter compiles and passes tests
    while silently skipping the cascade, stranding every child the removed
    cron owns. ``test_cron_remove_rows_structural.py`` fails any filter
    site that bypasses this helper.

    Same contract as :meth:`_release_children_of_removed`: IN-LOCK ONLY,
    after ``_sync_for_write()``; the caller must ``_save()`` afterwards and
    on save failure restore the returned ``(job, previous_owner)`` pairs
    (and reset the fingerprint where its path requires it). Grant-epoch
    bumps read the live rows, so callers bump BEFORE calling this.
    """
    self._jobs = [j for j in self._jobs if j.id not in removed_ids]
    return self._release_children_of_removed(removed_ids)


def _release_children_of_removed(
    self: CronService, removed_ids: set[str]
) -> list[tuple[CronJob, str]]:
    """Clear ownership on jobs whose cron principal is among ``removed_ids``.

    IN-LOCK ONLY: callers must already hold :meth:`_file_lock`, must have
    reloaded through ``_sync_for_write()``, must have filtered the removed
    rows out of ``self._jobs`` first (so a removed job cannot release
    itself), and must ``_save()`` afterwards — the point is that a removal
    and the release it implies land in ONE atomic write, never as two
    transactions a crash could split.

    Removing a cron retires its principal: ``cron:<job id>`` is presented
    only by runs of that job, so once the row is gone no session can ever
    present the key again and any job it created is manageable by nobody —
    ``cron_list`` omits it, ``cron_update``/``cron_remove`` answer "job not
    found", and it keeps firing. That is the same dead-owner state the
    history-delete funnel exists to prevent, and the funnel cannot cover it:
    it deliberately SKIPS a live cron principal, so a transcript deleted
    before the cron is removed leaves nothing behind to notice later.

    Released, not deleted or re-parented — the same semantics the delete
    funnel uses. A child is the user's own scheduled work; only its owner is
    gone, so it drops to the documented ownerless state the CLI and the
    Schedule page manage rather than a third state or a guessed new parent.

    Matches through :func:`cron_owner_matches`, the one matcher every release
    path shares, so a child stamped under a longer spelling
    (``cron:<parent>:<run id>``, ``cron:<parent>:<agent>``) is caught too.
    Returns ``(job, previous_owner)`` pairs so the caller can roll the cache
    back if its ``_save()`` fails.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    if not removed_ids:
        return []
    targets = {f"cron:{job_id}" for job_id in removed_ids if job_id}
    restore: list[tuple[CronJob, str]] = []
    for job in self._jobs:
        if not job.session_key:
            continue
        if not any(seams.cron_owner_matches(job.session_key, target) for target in targets):
            continue
        restore.append((job, job.session_key))
        job.session_key = ""
    if restore:
        logger.info(
            "Released %d cron job(s) whose owning cron was removed: %s",
            len(restore),
            ", ".join(sorted(job.id for job, _ in restore)),
        )
    return restore


def _remove_jobs_locked(self: CronService, job_ids: list[str]) -> tuple[list[str], list[str]]:
    """Sync core of :meth:`remove_jobs` — lock/reload/mutate/save only.

    Deliberately does NO timer work so it is safe to run in an executor
    thread (``_arm_timer`` needs the event loop). Cross-thread safety:
    every other store mutation also takes ``_file_lock`` — flock on
    separate fds mutually excludes within the process too — so a
    concurrent loop-side mutation blocks until this completes.
    """
    removed: list[str] = []
    missing: list[str] = []
    with self._file_lock():
        self._sync_for_write()
        present = {j.id for j in self._jobs}
        targets = set()
        for jid in job_ids:
            if jid in present:
                removed.append(jid)
                targets.add(jid)
            else:
                missing.append(jid)
        if targets:
            self._bump_grant_epochs_for(targets)
            restore = self._remove_job_rows(targets)
            try:
                self._save()
            except BaseException:
                for child, previous_owner in restore:
                    child.session_key = previous_owner
                # The removed rows are still filtered out of self._jobs and only
                # a reload can put them back, so drop the fingerprint to force
                # one. Without it the cache is missing rows that are still on
                # disk while the fingerprint says it agrees with disk: the next
                # _save() skips the reload and persists the removal this caller
                # was told had failed, completing the parent's removal while its
                # children keep the ownership just rolled back.
                self._reset_fingerprint()
                raise
            logger.info("Removed %d cron job(s) in batch", len(targets))
    return removed, missing


async def remove_jobs(
    self: CronService, job_ids: list[str], *, actor: str, source: str
) -> tuple[list[str], list[str]]:
    """Remove many jobs under ONE lock/reload/save, off the event loop.

    ``actor`` and ``source`` are required so the completed batch is
    audited here after persistence, outside the store lock.

    Returns ``(removed_ids, missing_ids)`` preserving input order. Looping
    :meth:`remove_job` per id would pay the file-lock + reload +
    full-serialize + atomic-write cost PER id on the event loop — with up to
    500 ids that starves every other gateway task (and on slow/network
    storage even one save can stall). The disk work
    runs in a worker thread; only ``_arm_timer`` (asyncio.create_task)
    runs back on the loop, and only when something was actually removed.
    """
    requested = list(job_ids)
    removed, missing = await asyncio.to_thread(self._remove_jobs_locked, requested)
    if removed:
        await asyncio.to_thread(self._delete_history_of, removed)
    self._audit_requested_batch_removal(requested, removed, missing, actor=actor, source=source)
    if removed:
        self._arm_timer()
    return removed, missing


def _audit_requested_batch_removal(
    self: CronService,
    requested: list[str],
    removed: list[str],
    missing: list[str],
    *,
    actor: str,
    source: str,
) -> None:
    """Audit one caller-requested batch after persistence and off-lock."""
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    try:
        seams.sel.sel().log_api_access(
            caller=actor,
            operation="cron.batch_delete",
            outcome="ok" if removed else "failed",
            source=source,
            resources=f"requested={requested} deleted={removed} failed={missing}",
        )
    except Exception:
        logger.warning("SEL audit for cron batch removal failed", exc_info=True)


def remove_jobs_sync(
    self: CronService, job_ids: list[str], *, actor: str, source: str
) -> tuple[list[str], list[str]]:
    """Synchronous sibling of :meth:`remove_jobs` — ONE atomic locked batch.

    Removes every id in ``job_ids`` under a SINGLE :meth:`_remove_jobs_locked`
    lock/reload/save transaction (not a per-id loop), so a contended store
    either removes them all or removes none and raises :class:`CronStoreBusy`
    — there is no partial-removal state that could leave some jobs orphaned
    and still enabled. Returns ``(removed_ids, missing_ids)``.

    Synchronous: only for loop-less callers / the offloaded ``CronSDK``
    facade (the ``_file_lock`` loop-safety guard rejects it on a running
    loop). On the loop use :meth:`remove_jobs`. Like :meth:`remove_jobs`, it
    deletes the removed jobs' run history.
    """
    requested = list(job_ids)
    removed, missing = self._remove_jobs_locked(requested)
    if removed:
        self._delete_history_of(removed)
    self._audit_requested_batch_removal(requested, removed, missing, actor=actor, source=source)
    if removed:
        self._arm_timer()
    return removed, missing


def _remove_jobs_by_owner_locked(self: CronService, owner_prefix: str) -> list[str]:
    """Select AND remove every job owned by ``owner_prefix`` under ONE lock.

    Sync core of :meth:`remove_jobs_by_owner` — lock/reload/select/mutate/
    save only, no timer work (so it is safe in an executor thread;
    ``_arm_timer`` needs the event loop). The critical property over
    passing in a pre-computed id list: the ownership SELECTION happens
    AFTER the in-lock ``_sync()`` reload, against the authoritative on-disk
    state — not against a possibly-stale in-memory/cache snapshot taken
    before the lock. A job created by this owner in another process since
    the last cache refresh is therefore still seen and removed, closing the
    cross-process orphan window where a cache-only ``list_jobs()`` id
    snapshot would miss it and leave it ENABLED after the app is deleted.

    All-or-nothing within the single ``_file_lock`` transaction: a contended
    store raises :class:`CronStoreBusy` before any mutation. Returns the
    list of removed ids.

    Selects through :meth:`_sync_for_write`, not ``_sync()``, because an EMPTY
    owned set is not an authoritative one. ``_load`` degrades an unreadable
    store to an empty job list, so the selection below would answer zero for a
    reason unrelated to ownership, skip the ``if removed`` branch, never reach
    ``_save()`` -- the only raiser on this path -- and return ``[]``. Uninstall
    reads that as "this app owned nothing" and deletes the app while its
    still-ENABLED jobs remain on disk to resume once the store parses again.
    ``_sync_for_write`` refuses first. A missing or honestly empty store leaves
    ``_load_failed`` clear, so a fresh install still tears down silently.
    """
    removed: list[str] = []
    with self._file_lock():
        self._sync_for_write()
        removed = [j.id for j in self._jobs if getattr(j, "created_by", "") == owner_prefix]
        if removed:
            targets = set(removed)
            self._bump_grant_epochs_for(targets)
            restore = self._remove_job_rows(targets)
            try:
                self._save()
            except BaseException:
                for child, previous_owner in restore:
                    child.session_key = previous_owner
                # The removed rows are still filtered out of self._jobs and only
                # a reload can put them back, so drop the fingerprint to force
                # one. Without it the cache is missing rows that are still on
                # disk while the fingerprint says it agrees with disk: the next
                # _save() skips the reload and persists the removal this caller
                # was told had failed, completing the parent's removal while its
                # children keep the ownership just rolled back.
                self._reset_fingerprint()
                raise
            logger.info("Removed %d cron job(s) owned by %s", len(removed), owner_prefix)
    return removed


async def remove_jobs_by_owner(self: CronService, owner_prefix: str) -> list[str]:
    """Remove every job owned by ``owner_prefix`` under ONE lock, off-loop.

    Selects and removes in a SINGLE :meth:`_remove_jobs_by_owner_locked`
    lock/reload/select/save transaction — the owner scan runs against the
    in-lock reloaded on-disk state, so a job another process created for
    this owner since the last cache refresh is still removed (no
    cross-process orphan window). All-or-nothing; propagates
    :class:`CronStoreBusy` on a contended store. The disk work runs in a
    worker thread; only ``_arm_timer`` runs back on the loop, and only when
    something was actually removed. Returns the removed ids.
    """
    removed = await asyncio.to_thread(self._remove_jobs_by_owner_locked, owner_prefix)
    if removed:
        self._arm_timer()
    return removed


def remove_jobs_by_owner_sync(self: CronService, owner_prefix: str) -> list[str]:
    """Synchronous sibling of :meth:`remove_jobs_by_owner` — ONE atomic
    locked select+remove batch.

    Selects and removes every job whose ``created_by == owner_prefix``
    under a SINGLE :meth:`_remove_jobs_by_owner_locked` transaction (the
    owner scan runs on the in-lock reloaded state, so a cross-process
    creation is still caught). All-or-nothing; raises
    :class:`CronStoreBusy` on a contended store.

    Synchronous: only for loop-less callers / the offloaded ``CronSDK``
    facade (the ``_file_lock`` loop-safety guard rejects it on a running
    loop). On the loop use :meth:`remove_jobs_by_owner`. Returns the removed
    ids.
    """
    removed = self._remove_jobs_by_owner_locked(owner_prefix)
    if removed:
        self._arm_timer()
    return removed

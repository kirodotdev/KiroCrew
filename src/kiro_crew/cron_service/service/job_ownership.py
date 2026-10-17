"""Who owns a cron job: adopting a job into a chat session and releasing a retired owner's jobs.

Also the strict on-disk reads of every owner key and chat folder id the store holds.

Each function here is a :class:`~kiro_crew.cron.CronService` member: the class binds
it by name, so it reads and writes the service's own state on the instance, and a
patch of the member on the class or on an instance reaches every caller. A name
tests patch on :mod:`kiro_crew.cron`, or one that module defines, is read through
it on each call (``seams``).

Instance fields read: ``_jobs``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Collection

from kiro_crew.cron_service.identity import cron_session_key_is_stable
from kiro_crew.cron_service.model import CronJob

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


def adopt_job(self: CronService, job_id: str, session_key: str) -> bool:
    """Point ``job_id`` at ``session_key`` as its originating chat session.

    ``session_key`` names the chat session a job belongs to, and it is ONE
    field with ONE meaning: every consumer reads it as the delivery target
    (``session="origin"`` resolution and script-result injection both strip
    the ``dashboard:`` prefix off it to get a slot). So adopting a job also
    makes its output arrive in that session -- that is what being the
    originating session IS, not a side effect the caller has to be warned
    about separately. Pass ``""`` to release the job back to the operator
    surfaces (CLI and the dashboard Schedule page), which is the state every
    job created outside a chat legitimately starts in.

    Deliberately NOT a branch in :meth:`_update_job_locked`: that path is
    reachable from MCP ``cron_update`` and the dashboard ``PATCH``, and a
    ``session_key`` branch there would hand both surfaces the power to
    repoint where any job delivers. Ownership is asserted by the operator,
    so the CLI -- the one surface that is not a session -- is its only
    writer.

    Returns ``False`` when the id is absent. Raises :class:`CronStoreBusy`
    on lock contention. Synchronous only: the CLI is its sole caller and has
    no event loop, so an async sibling would be dead code.
    """
    ok = self._adopt_job_locked(job_id, session_key)
    if ok:
        self._arm_timer()
    return ok


def _adopt_job_locked(self: CronService, job_id: str, session_key: str) -> bool:
    """Lock/reload/mutate/save core of :meth:`adopt_job` (no timer work)."""
    with self._file_lock():
        self._sync_for_write()
        for job in self._jobs:
            if job.id == job_id:
                before = job.session_key
                job.session_key = session_key
                # Roll back a failed save: the mutated object is the one
                # ``list_jobs`` already handed out, so an owner the store
                # rejected must not stay in the live cache (every ownership
                # decision in this process reads it) and be persisted by the
                # next save that succeeds. The sibling batch release
                # (:meth:`_release_jobs_owned_by_locked`) restores the same
                # field the same way.
                try:
                    self._save()
                except BaseException:
                    job.session_key = before
                    raise
                return True
    return False


def _release_jobs_owned_by_locked(
    self: CronService,
    owner_keys: Collection[str],
) -> list[str]:
    """Select AND release every job owned by ``owner_keys`` under ONE lock.

    Sync core of :meth:`release_jobs_owned_by` — lock/reload/select/mutate/
    save only, no timer work (so it is safe in an executor thread;
    ``_arm_timer`` needs the event loop). Same shape, and the same critical
    property, as :meth:`_remove_jobs_by_owner_locked`: BOTH decisions this
    makes — which owner keys name a retired principal, and which jobs still
    carry one of them — happen AFTER the in-lock ``_sync_for_write()``
    reload, against the authoritative on-disk state rather than a snapshot
    taken before the lock.

    That is the whole reason this exists instead of a caller-side
    ``list_jobs()`` loop over :meth:`adopt_job`. ``list_jobs`` is cache-only
    with up to one timer-poll interval of cross-process staleness, and both
    decisions are wrong when read from it:

    * OWNER staleness — between a pre-lock snapshot and a per-id write,
      another surface (the CLI's ``cron adopt``, a cron-injected slot
      re-stamping its key) can hand the job to a DIFFERENT owner, and an
      unconditional per-id release would clear that new owner, silently
      unbinding a job from a session that legitimately owns it.
    * PRINCIPAL staleness — a ``cron:<job id>`` owner is only live while that
      job exists, so its jobs must be kept owned; but a CLI ``cron remove``
      inside the staleness window leaves the cache still listing the job, and
      a cached liveness check would call the dead principal live and skip
      releasing its children. Nothing re-runs the delete funnel, so those
      jobs strand permanently.

    Conditioning both on the reloaded state makes the release a
    compare-and-clear against current truth: a re-adopted job keeps its new
    owner, a job whose cron principal is still scheduled keeps its owner, and
    either is simply absent from the returned ids.

    Ownership is matched through :func:`cron_owner_matches`, not ``==``,
    because one cron principal is stamped under several spellings
    (``cron:<job id>``, ``cron:<job id>:<run id>``,
    ``cron:<job id>:<agent>``) depending on which run created the job. An
    exact comparison misses a child stamped with a longer spelling than the
    caller holds, and a match MISS is not a release failure, so nothing warns.

    The active exact-key registry is snapshotted only AFTER this transaction
    acquires ``_file_lock`` and reloads the store. The short in-process
    ``_active_session_lock`` makes that worker-thread read coherent with loop
    registrations. A run that registered and persisted a child before this
    lock was acquired is therefore visible in both snapshots; a run that
    registers later cannot persist its child until this transaction releases
    the same store lock. This closes the snapshot-to-lock TOCTOU window while
    keeping every live stateless run out of principal-wide matching.

    All-or-nothing within the single ``_file_lock`` transaction: a contended
    store raises :class:`CronStoreBusy` before any mutation, and a save that
    fails after the mutation puts every touched owner back before re-raising,
    so the cache never reports a release that is not on disk. Returns the ids
    actually released.

    Selects through :meth:`_sync_for_write`, not ``_sync()``, for the reason
    spelled out on :meth:`_remove_jobs_by_owner_locked`: ``_load`` degrades an
    unreadable store to an empty job list, so an ownership scan over it would
    answer "this session owned nothing", skip the save, and report a clean
    release while the still-stamped jobs sit on disk. ``_sync_for_write``
    refuses first, so the caller learns the release did not happen. It also
    fails the liveness read closed, which matters in the opposite direction:
    an empty job list would call every cron principal dead and release jobs a
    live cron still owns.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    owners = {k for k in owner_keys if k}
    if not owners:
        return []
    released: list[str] = []
    with self._file_lock():
        self._sync_for_write()
        active_keys = self.active_session_keys()
        # Principal liveness, decided on the state the reload just brought
        # in. A cron whose row is still here AND whose key is stable across
        # runs presents that key again on every future run, so releasing the
        # jobs it created would leave a LIVE owner unable to list, update or
        # remove its own work.
        #
        # Existence alone is NOT enough. A stateless job mints
        # ``cron:<job id>:<uuid4>`` fresh per fire, so the key its last run
        # stamped on a child is already unpresentable even though the parent
        # row is still scheduled -- retaining that ownership strands the child
        # exactly as a removed parent would. ``cron_session_key_is_stable``
        # is the predicate that lives beside the mint sites, so this cannot
        # drift from what those sites actually produce; inferring it from the
        # key's SHAPE is wrong, because a durable sequential-agent key and an
        # ephemeral per-run key are both three segments.
        live_by_id = {job.id: job for job in self._jobs}
        targets: set[str] = set()
        for key in owners:
            principal = seams.cron_job_id_from_session_key(key)
            parent = live_by_id.get(principal) if principal else None
            if parent is not None and cron_session_key_is_stable(parent):
                continue
            targets.add(key)
        if not targets:
            return []
        # Previous owners are recorded so the cache can be put BACK if the
        # save fails. ``_save`` serializes ``self._jobs``, so the mutation
        # has to precede persistence -- but an unwritable store (ENOSPC, a
        # read-only volume) would then leave memory saying "ownerless" while
        # disk still names the old owner: every ownership decision in this
        # process reads the released state, the delete reports success, and
        # the old owner resurrects on the next restart. Rolling back keeps
        # the two agreeing on the only outcome that actually happened.
        restore: list[tuple[CronJob, str]] = []
        for job in self._jobs:
            if not job.session_key:
                continue
            if isinstance(job.session_key, str) and job.session_key in active_keys:
                continue
            if not any(seams.cron_owner_matches(job.session_key, target) for target in targets):
                continue
            restore.append((job, job.session_key))
            job.session_key = ""
            released.append(job.id)
        if released:
            try:
                self._save()
            except BaseException:
                for job, previous_owner in restore:
                    job.session_key = previous_owner
                raise
            logger.info(
                "Released %d cron job(s) owned by deleted session(s) %s",
                len(released),
                ", ".join(sorted(targets)),
            )
    return released


async def release_jobs_owned_by(self: CronService, owner_keys: Collection[str]) -> list[str]:
    """Clear ``session_key`` on every job owned by a RETIRED ``owner_keys`` key.

    The batch, owner-conditioned sibling of ``adopt_job(job_id, "")``: it
    releases jobs back to the operator surfaces (CLI and the dashboard
    Schedule page) the way a single ``--release`` does, but resolves both
    principal liveness and current ownership INSIDE the store lock, on the
    freshly reloaded state — so neither decision can be made from a stale
    cache (see :meth:`_release_jobs_owned_by_locked`). Callers pass every
    candidate owner key and do no filtering of their own.

    One lock/reload/select/save transaction, all-or-nothing; propagates
    :class:`CronStoreBusy` on a contended store so the caller can retry
    rather than silently dropping the release. The worker snapshots the
    complete ordered set of active and deferred per-run keys only after it
    holds the store lock, so a newer stateless run cannot persist a child in
    the snapshot-to-lock gap and have that live owner released. Only
    ``_arm_timer`` runs back on the loop, and only when something was actually
    released. Returns the released ids.
    """
    released = await asyncio.to_thread(
        self._release_jobs_owned_by_locked,
        owner_keys,
    )
    if released:
        self._arm_timer()
    return released


def _owner_keys_locked(self: CronService) -> set[str]:
    """Every non-empty ``session_key`` on disk, read under ONE lock. STRICT.

    The READ half of :meth:`_release_jobs_owned_by_locked`, with the same
    freshness guarantee and the same failure contract, for the caller that
    must decide WHICH owner keys to release before it can call the release at
    all — the history delete's store-side owner sweep, which recovers the
    exact owner key of a job whose transcript never recorded it.

    Deliberately NOT ``list_jobs``/``list_jobs_async``. Both answer a job list
    that is EMPTY or STALE in exactly the two states this scan has to
    distinguish from "nobody owns anything", and neither raises:

    * ``list_jobs`` is cache-only, up to one timer-poll interval behind a
      cross-process write — and the cross-process job is precisely what the
      sweep exists to find.
    * ``list_jobs_async`` locks and syncs, but degrades on BOTH store
      failures: :meth:`_synced_snapshot` swallows :class:`CronStoreBusy` and
      returns the cache, and it syncs through ``_sync()``, whose ``_load``
      turns an unreadable store into an empty job list. A contended or
      corrupt store therefore reads as "no owners" — indistinguishable from
      an honestly ownerless store, and the wrong answer for a caller about to
      destroy the last record of an ownership it could not see.

    So this raises instead of degrading: :class:`CronStoreBusy` out of
    :meth:`_file_lock` on sustained contention, :class:`CronStoreUnreadable`
    out of :meth:`_sync_for_write` on a store ``_load`` could not parse. The
    caller is expected to fail CLOSED on either — an unknown job set is not an
    empty one. ``_sync_for_write`` is the same reload the release uses and is
    chosen for the same reason spelled out there: ``_sync()`` alone would let
    an unreadable store answer "this session owned nothing".

    Read-only: no mutation, no ``_save``, so nothing to roll back. Returns
    owner keys, not jobs, because the scan's only question is which owner
    spellings exist — the release re-resolves ownership and liveness per job
    inside its own lock, and handing out ``CronJob`` objects would invite a
    caller-side decision on state that is stale the moment the lock drops.
    Includes jobs that are disabled or auto-paused: a paused job's owner is
    still stamped on disk and still strands when the session goes.
    """
    with self._file_lock():
        self._sync_for_write()
        return {owner for job in self._jobs if (owner := job.session_key)}


def _chat_folder_ids_locked(self: CronService) -> set[str]:
    """Every non-empty ``chat_folder_id`` on disk, read under ONE lock. STRICT.

    For the folder cleanup, which must not delete a folder a saved job files
    its tab into: the job stores the folder's id, so a folder recreated by
    name would not bring the filing back. Same failure contract as
    :meth:`_owner_keys_locked` and for the same reason -- an unreadable or
    contended store is an unknown job set, not an empty one, so this raises
    :class:`CronStoreBusy` or :class:`CronStoreUnreadable` instead of
    answering "no folders". Includes disabled and auto-paused jobs.
    """
    with self._file_lock():
        self._sync_for_write()
        return {fid for job in self._jobs if (fid := job.chat_folder_id)}


async def chat_folder_ids_async(self: CronService) -> set[str]:
    """Event-loop-safe :meth:`_chat_folder_ids_locked`."""
    return await asyncio.to_thread(self._chat_folder_ids_locked)


async def owner_keys_async(self: CronService) -> set[str]:
    """Event-loop-safe :meth:`_owner_keys_locked` — the lock+read runs off the loop.

    Propagates :class:`CronStoreBusy` (retryable) and
    :class:`CronStoreUnreadable` (not) rather than degrading to a partial
    answer; see :meth:`_owner_keys_locked` for why a read this one is used for
    must fail loudly. No timer work: nothing is mutated, so there is no
    schedule change to arm.
    """
    return await asyncio.to_thread(self._owner_keys_locked)

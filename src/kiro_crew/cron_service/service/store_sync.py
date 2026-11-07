"""The service's copy of ``crons.json``: the store lock, the fingerprint, load, sync and save.

A transaction takes the cross-process store lock (never on a running event loop),
reloads when the file's digest changed, refuses a mutation while the store is
unreadable, and saves atomically, keeping what a newer build added to the document.
The job snapshots callers read are taken here too.

Each function here is a :class:`~kiro_crew.cron.CronService` member: the class binds
it by name, so it reads and writes the service's own state on the instance, and a
patch of the member on the class or on an instance reaches every caller. A name
tests patch on :mod:`kiro_crew.cron`, or one that module defines, is read through
it on each call (``seams``).

Instance fields written: ``_jobs``, ``_last_digest``, ``_last_mtime``,
``_last_mtime_ns``, ``_last_size``, ``_load_failed``.

Instance fields read: ``_dir``, ``_held_run_records_guard``, ``_path``, ``_running``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterator

from kiro_crew.cron_service.model import CronJob
from kiro_crew.cron_service.readers import enabled_count_from_disk
from kiro_crew.cron_service.store import (
    _FILE_LOCK_POLL_SECS,
    _FILE_LOCK_TIMEOUT_SECS,
    _STORE_VERSION,
    CronStoreBusy,
    CronStoreUnreadable,
    StoreDocument,
    cron_store_lock,
    decode_store,
    encode_store,
)
from kiro_crew.cron_service.zone_default_migration import (
    DEFERRED_STORE_VERSION,
    store_extra_for_save,
)

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


def list_jobs(self: CronService, include_disabled: bool = False) -> list[CronJob]:
    """List jobs from the in-memory snapshot — CACHE-ONLY, never touches disk.

    A hot path called on the gateway event loop, so it does no file I/O
    (``no-blocking-call-on-event-loop``); ``list(self._jobs)`` is never torn.
    The timer tick and every mutator refresh the snapshot off the loop, so an
    external write shows up within ``_TIMER_POLL_SECS``. For an immediate
    cross-process read use :meth:`list_jobs_async`.
    """
    return self._snapshot(list(self._jobs), include_disabled)


def count_enabled_from_disk(self: CronService) -> int:
    """Count enabled jobs by reading ``crons.json`` directly — thread-safe.

    It only parses the file and never touches ``self._jobs`` or the timer, so a
    worker thread may call it. The dashboard status count uses it to see a job
    the CLI or MCP just added, before the next timer tick.

    Enabled means neither user-paused nor auto-paused (``_record_is_enabled``).
    The ``_read_job_records`` guards keep a corrupt store from killing the
    status pusher; it counts 0 there. The ``loadable`` half of
    :func:`enabled_count_from_disk` is dropped: the pusher needs a number.
    """
    return enabled_count_from_disk(self._path)[0]


def get_job(self: CronService, job_id: str) -> CronJob | None:
    """Find a job by its id in the in-memory snapshot — CACHE-ONLY, no disk I/O.

    See :meth:`list_jobs` for the cache-only rationale and the
    off-loop freshness contract. Use :meth:`get_job_async` when a
    guaranteed cross-process-fresh read is required.
    """
    for job in self._jobs:
        if job.id == job_id:
            return job
    return None


def _snapshot(jobs: list[CronJob], include_disabled: bool) -> list[CronJob]:
    """Filter a job snapshot by the ``include_disabled`` flag."""
    if include_disabled:
        return jobs
    return [j for j in jobs if j.enabled]


def _synced_snapshot(self: CronService, include_disabled: bool) -> list[CronJob]:
    """Refresh from disk under the store lock, then snapshot. WORKER-THREAD ONLY.

    Runs the blocking read/hash/parse + bounded lock spin OFF the event
    loop (via :meth:`list_jobs_async` / :meth:`get_job_async` /
    :meth:`run_job` -> ``asyncio.to_thread``). Degrades to the current
    in-memory snapshot if the store is too contended to lock, so a read
    never raises :class:`CronStoreBusy` into a caller. A worker-thread
    ``_sync()`` may reach ``_arm_timer``, which hands the (re)arm back to
    the bound event loop thread-safely (see :meth:`_arm_timer`), so no
    caller-side drain is required.
    """
    try:
        with self._file_lock():
            self._sync()
    except CronStoreBusy:
        pass  # too contended for a guaranteed-fresh read — use the cache
    return self._snapshot(list(self._jobs), include_disabled)


async def list_jobs_async(self: CronService, include_disabled: bool = False) -> list[CronJob]:
    """Freshness-guaranteed :meth:`list_jobs`: offloads a locked sync to a worker.

    For the rare loop-side caller that must observe a write made by another
    process (CLI/MCP) *right now* rather than within the ≤``_TIMER_POLL_SECS``
    timer refresh. The read/hash/parse and the bounded lock spin run in an
    ``asyncio.to_thread`` worker so the event loop is never blocked; the
    deferred timer arm (if the worker's ``_sync()`` reloaded an external
    change) is drained back on the loop.
    """
    jobs = await asyncio.to_thread(self._synced_snapshot, include_disabled)
    return jobs


async def get_job_async(self: CronService, job_id: str) -> CronJob | None:
    """Freshness-guaranteed :meth:`get_job` — see :meth:`list_jobs_async`."""
    jobs = await asyncio.to_thread(self._synced_snapshot, True)
    for job in jobs:
        if job.id == job_id:
            return job
    return None


def status(self: CronService) -> dict[str, Any]:
    """Service status summary."""
    return {
        "running": self._running,
        "jobs": len(self._jobs),
        "enabled": sum(1 for j in self._jobs if j.enabled),
    }


@contextmanager
def _file_lock(
    self: CronService,
    *,
    timeout: float = _FILE_LOCK_TIMEOUT_SECS,
    poll: float = _FILE_LOCK_POLL_SECS,
) -> Iterator[None]:
    """Cross-process advisory lock on the cron store.

    A bounded non-blocking spin, not a blocking ``flock``: a blocking wait
    behind one slow holder would park the thread with no way out. The spin
    sleeps between tries (freeing the GIL) and raises :class:`CronStoreBusy`
    (a :class:`TimeoutError`) after ``timeout``. ``flock`` on separate open
    descriptions also excludes within one process, so worker threads serialize.

    Loop callers use the ``*_async`` mutators, which run this in a worker and
    turn :class:`CronStoreBusy` into a retryable error; the sync path serves
    the loop-less CLI/MCP processes. :meth:`_guard_off_event_loop` raises
    :class:`CronLoopSafetyError` (strict) or warns if entered on a running loop.
    """
    self._guard_off_event_loop()
    with cron_store_lock(self._dir, timeout=timeout, poll=poll):
        yield


def _record_fingerprint(self: CronService) -> None:
    """Snapshot the store file's fingerprint as the last-loaded state.

    Called after a successful load and after a save so :meth:`_sync` treats
    the current on-disk contents as already in memory and only reloads on a
    genuine external change. Records a content digest (authoritative) plus
    the (mtime_ns, size) tuple (diagnostic) from the bytes now on disk.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    try:
        st = self._path.stat()
        raw = self._path.read_bytes()
    except OSError:
        self._reset_fingerprint()
        return
    self._last_mtime = st.st_mtime
    self._last_mtime_ns = st.st_mtime_ns
    self._last_size = st.st_size
    self._last_digest = seams.store_digest(raw)


def _keep_store_document(self: CronService, document: StoreDocument) -> None:
    """Hold what a newer build added to the store just loaded, and the records no job
    could be built from, for :meth:`_save` to write back."""
    self._store_version = document.version
    self._store_extra = MappingProxyType(dict(document.extra))
    self._unknown_fields = MappingProxyType(dict(document.unknown_fields))
    self._skipped_records = document.skipped


def _forget_store_document(self: CronService) -> None:
    """Back to this build's own empty document (no file, or one that did not load)."""
    self._store_version = _STORE_VERSION
    self._store_extra = MappingProxyType({})
    self._unknown_fields = MappingProxyType({})
    self._skipped_records = ()


def _reset_fingerprint(self: CronService) -> None:
    """Clear the fingerprint so the next :meth:`_sync` forces a reload."""
    self._last_mtime = 0.0
    self._last_mtime_ns = 0
    self._last_size = -1
    self._last_digest = b""


def _sync(self: CronService) -> None:
    """Reload from disk if the file changed externally.

    Compares a content DIGEST rather than only ``(mtime_ns, size)``: an
    external atomic write can preserve both the coarse timestamp and the
    byte length while changing content (e.g. renaming a job to an
    equal-length name), which an mtime/size fingerprint misses — the stale
    in-memory state would then be re-saved over the external change, losing
    it. The bytes read here are the same bytes :meth:`_load` parses when a
    reload is needed, so the file is read at most once per changed sync.

    INVARIANT: no ``_sync()`` or whole-file read + hash runs on the gateway
    loop (``test_cron_locking_regression.py::TestReadPathsLocked``). Callers:
    the ``*_locked`` mutators (reached via ``*_async`` -> ``to_thread``, or a
    loop-less CLI/MCP process), ``_synced_snapshot``, ``_tick_scan_locked`` and
    ``_merge_job_result`` (all in workers). The gateway's first ``_load()``
    runs in a worker via ``CronService.create()`` / ``start()``; the plain
    constructor loads inline only in loop-less processes. ``list_jobs``,
    ``get_job`` and the reaper's ``jobs_by_id`` read the cache and do no I/O.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    if not self._path.exists():
        # A GONE store is not an unreadable one: clear the refusal latch, or
        # "move the unreadable file aside" never unblocks a live gateway.
        # Not a call to _load(): it empties _jobs, losing a reaper update made
        # in memory but not yet saved (test_reaper_persists_state). Empty the
        # list and forget the store document (its kept records and newer-build
        # keys) only when the latch was set: they came from the store that was
        # moved aside, and writing them back could resurrect an external removal.
        # The fingerprint was already reset by that failed load.
        if self._load_failed:
            self._jobs = []
            self._forget_store_document()
        self._load_failed = False
        return
    try:
        raw = self._path.read_bytes()
    except OSError:
        # Latch, as _load's failure paths do, so _save() will not overwrite a
        # store that went unreadable after a good load (EIO, EACCES). _jobs is
        # kept: it may hold an unsaved reaper update. Reset the fingerprint with
        # the latch, or an unchanged file never reloads and the latch never
        # clears once the store is healthy again.
        self._reset_fingerprint()
        self._load_failed = True
        return
    if seams.store_digest(raw) != self._last_digest:
        logger.info("Cron file changed externally, reloading")
        self._load(_preread=raw)


def _keep_unzoned_jobs_on_utc(
    jobs: list[CronJob], extra: Mapping[str, Any], skipped: tuple[Any, ...]
) -> bool:
    """Stamp UTC on an older store's unzoned jobs that fired in UTC (``zone_default_migration``).

    The *skipped* records the save keeps get the same stamp: they are written under
    the new version too, so a record fixed by hand later keeps its wall clock.

    ``False`` when the config cannot be read: the zone those jobs inherited is
    unknown, so nothing is stamped, they run in UTC in memory meanwhile, and the
    caller keeps the store's older version for the next load to settle. A job an
    earlier such save listed as following the default (*extra*) is never one of
    them. It never makes the store unreadable.
    """
    from kiro_crew.cron_service.zone_default_migration import (
        configured_default_zone,
        follows_default_ids,
        hold_jobs_on_utc_in_memory,
        pin_jobs_read_in_utc,
        pin_records_read_in_utc,
    )

    follows_default = follows_default_ids(extra)

    try:
        zone = configured_default_zone()
    except Exception:
        logger.warning(
            "Cron store upgrade deferred: config unreadable, jobs with no zone are not "
            "stamped until it can be read",
            exc_info=True,
        )
        hold_jobs_on_utc_in_memory(jobs, follows_default)
        return False
    pin_jobs_read_in_utc(jobs, zone, follows_default)
    pin_records_read_in_utc(skipped, zone, follows_default)
    return True


def _load(self: CronService, _preread: bytes | None = None) -> None:
    """Deserialize jobs from crons.json and record the fingerprint.

    ``_preread`` lets :meth:`_sync` hand in the bytes it already read for
    the change check so the file is not read twice for one reload.

    Clears :attr:`_load_failed` on entry and re-raises it only on the paths
    that could not read the store, so a load that DOES resolve — including
    a missing file and an honestly empty one — leaves the store writable,
    and a store repaired between two loads heals itself.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    self._load_failed = False
    self._forget_store_document()
    if not self._path.exists():
        self._jobs = []
        self._reset_fingerprint()
        return
    try:
        st = self._path.stat()
        raw = _preread if _preread is not None else self._path.read_bytes()
        document = decode_store(raw)
        if document is None:
            # Parses but holds no jobs list ([], a scalar, {"jobs": null}):
            # nothing to salvage, and building entries would raise into startup.
            logger.warning("Failed to load cron store: document is not an object with a jobs list")
            self._jobs = []
            self._reset_fingerprint()
            self._load_failed = True
            return
        settled = not document.predates_local_default or _keep_unzoned_jobs_on_utc(
            document.jobs, document.extra, document.skipped
        )
        # decode_store skips a malformed record with a warning and keeps an
        # object a snapshot restore accepts for _save to write back; only a file
        # with nothing parseable resets the whole store.
        # Fingerprint from the stat taken BEFORE the read: a writer racing in
        # between costs one redundant reload, never a lost update. The digest
        # is of the exact bytes parsed, so _sync compares like for like.
        self._last_mtime = st.st_mtime
        self._last_mtime_ns = st.st_mtime_ns
        self._last_size = st.st_size
        self._last_digest = seams.store_digest(raw)
        # The disk copies never received a run whose merge the lock refused.
        # They become the job list only once they carry it: a reader of the
        # list while this reload runs (the timer's due check on the loop,
        # with the reload on a worker thread) never sees a copy without it.
        # Under the guard, as the hold's lookup and apply are.
        with self._held_run_records_guard:
            self._apply_held_run_records_to(document.jobs)
            self._jobs = document.jobs
        self._keep_store_document(document)
        if not settled:
            # Saves keep the older version, so a load that can read the config
            # still settles these jobs (zone_default_migration).
            self._store_version = DEFERRED_STORE_VERSION
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        # Same class set as _read_job_records' guard: ValueError covers
        # UnicodeDecodeError, RecursionError is deeply nested JSON, and OSError
        # covers stat()/read_bytes() at construction, which _sync does not guard.
        logger.warning("Failed to load cron store: %s", exc)
        self._jobs = []
        self._reset_fingerprint()
        self._load_failed = True

    # Restore timers for active jobs loaded from disk
    if self._running:
        restored = sum(1 for j in self._jobs if j.enabled)
        if restored:
            self._arm_timer()
            logger.info("Restored %d cron timer(s) from disk", restored)


def _unreadable_error(self: CronService) -> CronStoreUnreadable:
    """The one wording for a refusal caused by an unreadable store.

    Three guards raise it (:meth:`_sync_for_write`, :meth:`_save`,
    :meth:`raise_if_store_unreadable`), and the CLI, dashboard, MCP and Slack
    show the message verbatim, so it is built in one place.
    """
    return CronStoreUnreadable(
        f"refusing to write cron store: the last load could not read {self._path}, "
        "so the in-memory job list is empty for that reason rather than because the "
        "store is empty. Move the unreadable file aside to start fresh."
    )


def raise_if_store_unreadable(self: CronService) -> None:
    """Refuse if the last load could not read the store. NO I/O of its own.

    For a caller that compares loaded jobs with a desired state before writing:
    an unreadable store loads as an EMPTY list without raising, so no write is
    tried and the caller would report a no-op over a corrupt file.

    Reads only the latch, so it is safe on the loop. Call it AFTER a
    :meth:`list_jobs_async`, which refreshes the latch, or it is one poll stale.
    """
    if self._load_failed:
        raise self._unreadable_error()


def _sync_for_write(self: CronService) -> None:
    """:meth:`_sync` for a MUTATING transaction — refuse BEFORE the mutation.

    A mutator edits ``self._jobs`` before ``_save()``, so a refusal only at the
    disk boundary would leave the change in memory (a resumed job the timer can
    still fire, via :meth:`_tick_scan_locked`'s cache fallback). ``_save()``
    cannot roll back fields it never saw, so the ten user-facing writers refuse
    here first; the check follows ``_sync()``, which sets ``_load_failed``.

    ``_save()`` keeps its own guard as the backstop: the three BACKGROUND
    writers wrap only their ``_save()`` call, so raising earlier would abort
    the reaper and the tick instead of degrading them.
    """
    self._sync()
    if self._load_failed:
        raise self._unreadable_error()


def _save(self: CronService, jobs: list[CronJob] | None = None) -> None:
    """Atomic write (tmp → rename) and update mtime tracking.

    ``jobs`` is the list to write, ``self._jobs`` by default; the held-record
    save passes copies so a running job is written without being changed.

    RAISES :exc:`CronStoreUnreadable` when the last :meth:`_load` could not
    read the store: that load left an empty list, and writing it would wipe a
    store that still holds records. Raising, not returning, keeps the caller
    from reporting success for a write that never happened. The three
    BACKGROUND writers catch it and degrade. A missing or honestly empty store
    is not a failure, so a fresh install still writes.

    WRITE-PATH AUDIT — every ``_save()`` call site and every structural
    ``self._jobs`` mutation. INVARIANT: every writer holds :meth:`_file_lock`
    and is reached from the gateway loop ONLY via ``asyncio.to_thread`` (or
    runs in a loop-less CLI/MCP process). Keep this table in sync when adding
    a writer.

    ==============================  ==========  ======================================
    Writer (method)                 Locked?     Loop entry
    ==============================  ==========  ======================================
    _persist_add_locked             _file_lock  add_job_async → to_thread; sync CLI/MCP
    _persist_add_if_absent_locked   _file_lock  add_job_if_absent_async → to_thread; sync
    _update_job_locked              _file_lock  update_job_async → to_thread; sync CLI/MCP
    _remove_job_locked              _file_lock  remove_job_async → to_thread; sync CLI/MCP
    _remove_jobs_locked             _file_lock  remove_jobs → to_thread
    _remove_jobs_by_owner_locked    _file_lock  app/owner teardown → to_thread; sync
    _adopt_job_locked               _file_lock  adopt path → to_thread; sync
    _enable_job_locked              _file_lock  enable_job_async → to_thread; sync CLI/MCP
    _ack_job_locked                 _file_lock  ack_job_async → to_thread; sync
    _unack_job_locked               _file_lock  unack_job_async → to_thread; sync
    _merge_job_result               _file_lock  _run_job_isolated → to_thread; BACKGROUND
    _merge_terminal_state_locked    _file_lock  _force_reap / cancel → to_thread; BACKGROUND
    _drain_pending_removals_locked    (caller)  _tick_scan_locked holds _file_lock; BACKGROUND
    _load (self._jobs = …)            (caller)  _sync() under _file_lock; else construction/start
    ==============================  ==========  ======================================

    In-memory-only job field writes that DON'T call ``_save()`` and are
    persisted later under lock: ``defer_removal`` (sets ``enabled=False``
    so the next due-scan skips it; the durable delete happens in the locked
    ``_drain_pending_removals_locked``), and the pre-persist snapshot writes
    in ``_force_reap``/``cancel`` (authoritative persist is the offloaded
    ``_merge_terminal_state_locked``).
    """
    if self._load_failed:
        raise self._unreadable_error()
    self._dir.mkdir(parents=True, exist_ok=True)
    written = self._jobs if jobs is None else jobs
    document = encode_store(
        written,
        version=self._store_version,
        extra=store_extra_for_save(
            self._store_extra,
            written,
            deferred=self._store_version == DEFERRED_STORE_VERSION,
            skipped=self._skipped_records,
        ),
        unknown_fields=self._unknown_fields,
        skipped=self._skipped_records,
    )
    # Atomic write: unique tmp → fsync → rename, then sync the directory so
    # the rename itself survives a sudden host restart. Without both syncs a
    # save that returned can come back as an empty or missing crons.json.
    # Deferred import to avoid a circular dependency.
    from kiro_crew.atomic_write import atomic_write, fsync_dir

    atomic_write(self._path, document, fsync=True)
    # Best-effort: the replace has already published the new store, so a
    # filesystem that refuses a directory fsync must not turn a committed
    # save into a reported failure.
    fsync_dir(self._path.parent, best_effort=True)
    # Refresh the (mtime_ns, size) fingerprint so _sync recognizes this as
    # our own write and does not reload it back over the in-memory state.
    self._record_fingerprint()

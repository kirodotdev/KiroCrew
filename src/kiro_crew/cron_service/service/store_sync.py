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

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


def list_jobs(self: CronService, include_disabled: bool = False) -> list[CronJob]:
    """List jobs from the in-memory snapshot — CACHE-ONLY, never touches disk.

    This is a hot path: it is called directly on the gateway event loop by
    the dashboard WebSocket status push, the dashboard REST handlers, the
    Slack handlers, the apps SDK, and MCP tools. It performs NO filesystem
    I/O — no lock-file open, no ``read_bytes()``, no digest hash — so a
    large ``crons.json`` can never freeze the loop with synchronous I/O
    (the ``no-blocking-call-on-event-loop`` rule). ``list(self._jobs)`` is
    never torn: CPython swaps the list reference atomically.

    Cross-process freshness is maintained OFF the loop: the timer tick
    (``_on_timer``, every ≤``_TIMER_POLL_SECS``) and every mutator
    ``_sync()`` the in-memory snapshot under the store lock, so an external
    write is picked up within one poll interval. Callers that need to
    observe a cross-process write *immediately* use :meth:`list_jobs_async`,
    which offloads a locked ``_sync()`` + snapshot to a worker thread.
    """
    return self._snapshot(list(self._jobs), include_disabled)


def count_enabled_from_disk(self: CronService) -> int:
    """Count enabled jobs by reading ``crons.json`` directly — thread-safe.

    Unlike :meth:`list_jobs`, which serves the in-memory snapshot that the
    loop-side timer refreshes, this performs ONLY a read-only file parse. It
    never mutates loop-owned state (``self._jobs``, ``self._last_mtime``) and
    never touches the asyncio timer, so it is safe to invoke from a worker
    thread via ``asyncio.to_thread``.

    This exists specifically for the dashboard status count refresh, which
    needs an enabled-job count off the event loop that is current across
    processes: a job added by the CLI or an MCP tool reaches the snapshot
    only on the next timer tick, while this read sees it at once.

    Enabled semantics come from the shared ``_record_is_enabled`` predicate
    (the single owner used by ``_load`` too): a job is enabled when it is
    neither user-paused nor auto-paused (with the legacy ``!enabled``
    fallback for stores written before those fields existed). A slightly stale count is
    acceptable here — the caller caches it and the atomic tmp→rename write
    in ``_save`` guarantees a concurrent read sees a whole file, never a
    partial one.

    The read, parse and shape guards are :func:`_read_job_records`, shared
    with the other two direct readers. Routing through it is what keeps the
    WS status pusher alive on a corrupt store: catching only
    ``(OSError, json.JSONDecodeError)`` here would let invalid UTF-8
    (``UnicodeDecodeError``, from a bare locale-dependent ``read_text()``)
    and deeply nested JSON (``RecursionError``, a ``RuntimeError``) escape
    and kill the pusher. A count of 0 is the correct degrade: an
    unreadable store has no jobs anyone can schedule.

    The reduction itself lives in :func:`enabled_count_from_disk`, whose
    ``loadable`` half this method deliberately discards — the status pusher
    wants a number it can always render, not a fault to handle.
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

    Acquires the lock with a NON-BLOCKING ``try_acquire_lock`` in a bounded
    spin instead of a blocking ``fcntl.flock(LOCK_EX)``. A blocking flock
    parks the calling thread in an uninterruptible kernel wait for as long
    as another holder keeps the lock — and every store *mutator*
    (:meth:`add_job`, :meth:`update_job`, :meth:`remove_job`,
    :meth:`enable_job`, …) takes this lock directly on the gateway's
    asyncio event loop. A single slow holder (a large atomic save on
    network storage, the CLI process, or the off-loop batch-remove worker)
    would therefore freeze the ENTIRE event loop — every unrelated session,
    timer, and reaper — until it released.

    The non-blocking spin polls with a short ``time.sleep`` between
    attempts (releasing the GIL so worker threads make progress) and raises
    :class:`CronStoreBusy` (a :class:`TimeoutError` subclass) after
    ``timeout`` rather than blocking indefinitely. flock on separate open
    descriptions mutually excludes within a single process too, so this
    still serializes the loop-side mutators against the ``asyncio.to_thread``
    batch-remove and mutator workers.

    The loop-resident mutator boundaries do NOT call this directly on the
    event loop — they use the ``*_async`` mutator variants (``add_job_async``
    et al.), which offload this lock+save to a worker thread and translate a
    raised :class:`CronStoreBusy` into a clean retryable error. The bounded
    sync path here still serves the CLI/MCP server processes (no event loop
    to park) and remains a strict improvement over the old unbounded flock.

    The ``no-blocking-call-on-event-loop`` invariant is MACHINE-ENFORCED:
    :meth:`_guard_off_event_loop` raises :class:`CronLoopSafetyError` (strict
    mode) or warns (default) if this is entered on a thread with a running
    asyncio loop — so a future writer that calls a sync mutator on the loop
    is caught rather than silently re-freezing it.
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
    """Hold what a newer build added to the store just loaded, for :meth:`_save`."""
    self._store_version = document.version
    self._store_extra = MappingProxyType(dict(document.extra))
    self._unknown_fields = MappingProxyType(dict(document.unknown_fields))


def _forget_store_document(self: CronService) -> None:
    """Back to this build's own empty document (no file, or one that did not load)."""
    self._store_version = _STORE_VERSION
    self._store_extra = MappingProxyType({})
    self._unknown_fields = MappingProxyType({})


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

    ─────────────────────────────────────────────────────────────────────
    EXHAUSTIVE AUDIT — every ``_sync()`` caller and raw store-``read``
    site in this module, classified by whether it can run on the gateway
    event loop. INVARIANT: **no ``_sync()`` / whole-file ``read_bytes()`` +
    hash ever runs on the loop.** All blocking store I/O is either in a
    worker thread (``asyncio.to_thread``) or in a loop-less process
    (CLI / MCP). Enforced mechanically by
    ``test_cron_locking_regression.py::TestReadPathsLocked`` (on-loop reads
    AND the timer tick must not touch the store on the loop).

    ``_sync()`` callers
      • _persist_add_locked / _update_job_locked_kw / _remove_job_locked /
        _remove_jobs_locked / _enable_job_locked / _ack_job_locked /
        _unack_job_locked  → OFF-LOOP: reached from the loop only via their
        ``*_async`` wrappers, which ``await asyncio.to_thread(...)``; also
        called directly by loop-less CLI/MCP/app-SDK processes.
      • _synced_snapshot  → OFF-LOOP (worker): the body of
        list_jobs_async / get_job_async / run_job's offloaded refresh.
      • _tick_scan_locked  → OFF-LOOP (worker): the timer tick's
        (``_on_timer``) offloaded lock+sync+drain+snapshot transaction.
      • _merge_job_result  → OFF-LOOP on the gateway (``_run_job_isolated``
        calls it via ``asyncio.to_thread``); loop-less CLI/MCP may call it
        directly.
      • run_job  → now OFF-LOOP: its former on-loop ``_sync()`` moved into
        the ``_synced_snapshot`` offload; the claim stays on
        the loop and does NO store I/O.

    Raw store ``read_bytes()`` sites
      • _record_fingerprint (post-load/save) / _sync / _load  → all reached
        only through the OFF-LOOP ``_sync()`` callers above (or a loop-less
        process). None on the loop.
      • initial ``_load()`` (construction / ``start()``)  → the plain
        constructor loads INLINE (loop-less CLI/MCP/apps-SDK/tests only —
        no loop to park). Loop contexts (the gateway) build via the async
        factory ``CronService.create()``, which sets
        ``_defer_initial_load=True`` and runs ``_load()`` via
        ``asyncio.to_thread``; ``start()`` likewise offloads its ``_load()``.
        ``_running`` is False during both, so neither arms a timer off-loop.
        OFF-LOOP on the gateway.

    Cache-only (NO store I/O at all — never lock, read, or hash)
      • list_jobs / get_job  → on-loop hot paths; return the atomically-
        swapped in-memory snapshot.
      • _reaper_loop's ``jobs_by_id`` snapshot  → on-loop; cache-only,
        same atomic-reference-swap rationale as list_jobs.
    ─────────────────────────────────────────────────────────────────────
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    if not self._path.exists():
        # Clear the refusal latch: a store that is GONE is not an unreadable
        # one, and _load's docstring already promises that a load which
        # resolves -- "including a missing file" -- leaves the store writable.
        # Returning bare left the latch set, so the one remediation this
        # refusal PRINTS (move the unreadable file aside) did nothing on a
        # live gateway: every later write kept failing until a restart. A
        # fresh CLI/MCP process was unaffected because it reconstructs.
        #
        # Deliberately NOT a call to _load(), even though its missing-file
        # branch clears this same flag: that branch also replaces _jobs with
        # an empty list, which discards in-memory jobs not yet persisted --
        # the reaper mutates a job and only then saves, so wiping first loses
        # the update and the save never happens (test_cron_reaper's
        # test_reaper_persists_state catches exactly that). Clearing the flag
        # is the whole of the defect; emptying the list is a separate
        # behaviour change and not one this needs.
        #
        # The fingerprint is left alone on purpose: the failed load that set
        # this latch already reset it, so a file that reappears mismatches the
        # cleared digest below and reloads normally.
        #
        # Clearing the latch re-opens _save(), so the snapshot it would write
        # has to be trustworthy. When the latch was SET, _jobs came from a
        # load that could not read the store -- it may predate an external
        # removal, and writing it back resurrects whatever that writer
        # deleted. _load's own missing-file branch empties the list for the
        # same reason; this branch bypasses _load, so it must do it too.
        # Conditioned on the latch, NOT unconditional: with the latch clear
        # this is the ordinary no-store path, where the reaper's in-memory
        # mutation is still waiting to be saved and wiping it would lose the
        # update (test_cron_reaper's test_reaper_persists_state).
        if self._load_failed:
            self._jobs = []
        self._load_failed = False
        return
    try:
        raw = self._path.read_bytes()
    except OSError:
        # LATCH, the same as _load's two failure paths do. This is the THIRD
        # way a read of the store can fail and the only one that never reaches
        # _load, so returning bare left _save()'s guard -- the one thing
        # standing between stale memory and the file -- open: a store that went
        # unreadable AFTER a good load (EIO, EACCES, a botched restore) was
        # overwritten from memory, discarding whatever it had come to hold.
        #
        # Still deliberately NOT un-latched: the store is unreadable here, so
        # keeping an existing refusal is correct and clearing it would suppress
        # a live fault rather than report it.
        #
        # _jobs is deliberately NOT emptied, unlike _load's paths: the
        # missing-file rationale above applies unchanged -- wiping the list
        # discards a mutation the reaper has made but not yet saved.
        #
        # The fingerprint has to be cleared WITH the latch, exactly as both
        # _load failure paths pair them. Left alone, a fault over an UNCHANGED
        # store leaves the tracked digest still matching the file, so the next
        # _sync sees no change, skips the _load that is the only thing that
        # clears this latch, and a store that is now perfectly healthy refuses
        # every write until the process restarts.
        self._reset_fingerprint()
        self._load_failed = True
        return
    if seams.store_digest(raw) != self._last_digest:
        logger.info("Cron file changed externally, reloading")
        self._load(_preread=raw)


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
            # A document that parses but is not an object holding a jobs
            # LIST (top-level [], a scalar, {"jobs": null}) cannot yield
            # any job — same salvage story as unparseable JSON (there is
            # nothing to keep), and without this guard the per-entry build
            # would raise an uncaught AttributeError/TypeError into _sync
            # and gateway startup.
            logger.warning("Failed to load cron store: document is not an object with a jobs list")
            self._jobs = []
            self._reset_fingerprint()
            self._load_failed = True
            return
        # One malformed or legacy record is warned about and skipped by
        # decode_jobs; every well-formed job survives, and the whole-store
        # reset below is reserved for a file that yields nothing parseable
        # at all, where there is nothing to salvage.
        # Fingerprint from the stat taken BEFORE the read: if a writer
        # replaced the file between our stat and read we may have loaded the
        # newer content under an older fingerprint, which only costs one
        # redundant reload on the next _sync — never a lost update. The
        # digest is taken from the exact bytes we parsed so _sync compares
        # like for like.
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
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        # Same class set as _read_job_records' json.loads guard, kept
        # spelled identically so the two cannot drift. A decode-error-only
        # handler here let two classes escape into _sync and gateway
        # startup: UnicodeDecodeError (invalid UTF-8 — a SIBLING subclass
        # of ValueError, not an ancestor of json.JSONDecodeError) and
        # RecursionError (deeply nested JSON — a RuntimeError, outside the
        # ValueError tree entirely). OSError covers the stat()/read_bytes()
        # above, which _sync already guards but the constructor's
        # _load() — and so gateway startup — does not. A genuinely absent
        # file never reaches here: the exists() check returns early, so a
        # fresh install still loads silently rather than warning.
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

    Built in a single place because THREE guards raise it — :meth:`_sync_for_write`
    before a mutation, :meth:`_save` at the disk boundary, and
    :meth:`raise_if_store_unreadable` for a caller that must refuse without
    attempting a write at all — and the message names the path plus the
    remediation that the CLI, dashboard, MCP and Slack boundaries surface
    verbatim. Two copies of that sentence would drift.
    """
    return CronStoreUnreadable(
        f"refusing to write cron store: the last load could not read {self._path}, "
        "so the in-memory job list is empty for that reason rather than because the "
        "store is empty. Move the unreadable file aside to start fresh."
    )


def raise_if_store_unreadable(self: CronService) -> None:
    """Refuse if the last load could not read the store. NO I/O of its own.

    Exists because every other guard is on a WRITE, and a caller that decides
    whether to write by first comparing the loaded jobs against a desired state
    never gets that far: an unreadable store loads as an EMPTY list — :meth:`_load`
    warns, empties, latches ``_load_failed`` and RETURNS rather than raising, and
    :meth:`_synced_snapshot` only translates :class:`CronStoreBusy` — so there is
    no job to diverge, no mutation is attempted, and such a caller reports a
    successful no-op over a corrupt file. That is the quiet-versus-broken
    conflation, and it is invisible to :meth:`_sync_for_write`.

    Reads the latch only, so it is safe on the event loop and adds no second read
    after a :meth:`list_jobs_async` — which has just refreshed the latch under the
    store lock. Call it AFTER that read, or the answer is one poll stale.
    """
    if self._load_failed:
        raise self._unreadable_error()


def _sync_for_write(self: CronService) -> None:
    """:meth:`_sync` for a MUTATING transaction — refuse BEFORE the mutation.

    Every user-facing mutator edits ``self._jobs`` and only then reaches
    ``_save()``, so refusing at the disk boundary alone left the caller told
    "rejected" while the mutation stayed in the in-memory list — a resumed job
    the timer can still fire, an ack already consumed, a removal already gone
    from the cache. All TEN user-facing writers in ``_save``'s audit table now
    route through here; the three BACKGROUND ones deliberately do not (below).
    That is reachable, not theoretical:
    :meth:`_tick_scan_locked` documents an in-memory-snapshot fallback for a
    contended lock — it skips ``_sync()`` and returns ``list(self._jobs)`` — so
    the due-scan could hand a refused job to the runner.

    Refusing up front rather than undoing afterwards is what makes that
    unrepresentable. ``_save()`` cannot roll back a mutation it never saw: its
    write-path audit lists roughly a dozen writers, each touching different
    fields, so a generic rollback there has nothing to key on and a partial one
    would be a fresh defect. The check sits after ``_sync()`` because
    ``_sync()`` is what sets ``_load_failed``.

    ``_save()`` keeps its own guard rather than delegating to this one: it is
    the backstop for any writer that does not come through here, and the three
    BACKGROUND writers depend on it firing at the disk boundary — each wraps
    only its ``_save()`` call, so an earlier raise would abort the reaper and
    the tick instead of degrading them.
    """
    self._sync()
    if self._load_failed:
        raise self._unreadable_error()


def _save(self: CronService, jobs: list[CronJob] | None = None) -> None:
    """Atomic write (tmp → rename) and update mtime tracking.

    ``jobs`` is the list to write, ``self._jobs`` by default; the held-record
    save passes copies so a running job is written without being changed.

    RAISES :exc:`CronStoreUnreadable` when the last :meth:`_load` could not
    read the store, instead of writing. ``_load`` degrades an unreadable
    store to an empty job list, which is indistinguishable HERE from an
    honestly empty one — and this method serialises ``self._jobs``
    wholesale, so one mutation after a failed load would persist that empty
    list over a store still holding records. Measured on the base handler
    alone (a ``json.JSONDecodeError`` store plus one ``add_job``), so this
    is not a hazard the widened ``_load`` guard introduced.

    It RAISES rather than returning quietly because a silent refusal is the
    same silence-shaped failure this change exists to break: the mutator
    would return success to the dashboard/CLI/MCP caller for a write that
    never happened. Every writer below reaches disk through this one
    method, so the single check covers all of them; the three BACKGROUND
    writers catch the error and degrade so a corrupt store cannot take down
    the reaper, the tick scan or the job runner. A missing store and an
    honestly empty one are NOT failures (``_load`` clears the flag for
    both), so a fresh install still writes.

    WRITE-PATH AUDIT — every ``_save()`` call site and every structural
    ``self._jobs`` mutation, each classified locked/unlocked and
    on-loop/off-loop. INVARIANT: every writer holds :meth:`_file_lock` and
    is reached from the gateway event loop ONLY via ``asyncio.to_thread``
    (or runs in a genuinely loop-less CLI/MCP process). No bare on-loop
    ``_save()`` remains. Keep this table in sync when adding a writer.
    Counted by EXECUTABLE call site: three other lines in this file mention
    ``self._save()`` in a comment or docstring and are not calls.

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
    document = encode_store(
        self._jobs if jobs is None else jobs,
        version=self._store_version,
        extra=self._store_extra,
        unknown_fields=self._unknown_fields,
    )
    # Atomic write: unique tmp → fsync → rename, then sync the directory so
    # the rename itself survives a sudden host restart. Without both syncs a
    # save that returned can come back as an empty or missing crons.json.
    # Deferred import to avoid circular dependency (pre-existing)
    from kiro_crew.atomic_write import atomic_write, fsync_dir

    atomic_write(self._path, document, fsync=True)
    # Best-effort: the replace has already published the new store, so a
    # filesystem that refuses a directory fsync must not turn a committed
    # save into a reported failure.
    fsync_dir(self._path.parent, best_effort=True)
    # Refresh the (mtime_ns, size) fingerprint so _sync recognizes this as
    # our own write and does not reload it back over the in-memory state.
    self._record_fingerprint()

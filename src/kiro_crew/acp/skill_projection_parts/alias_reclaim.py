"""Reclaiming this home's unused views and their residue, and the censuses that count them.

The bounded prune walk with its reclaim, work and time budgets, run per spawn, at
projection end (the eager prune and the lease finalizer) and at boot; the
ownership-proved per-candidate reclaim; the orphan sidecar and alias-lock sweeps; the
gateway-boot drain that first migrates legacy side files out of the agents directory
and then prunes in batches; and the read-only, bounded censuses ``kirocrew doctor``
reports, counted by the prune's own rules.

:mod:`kiro_crew.acp.skill_projection` stays the import path and the patch surface; see
:mod:`kiro_crew.acp.skill_projection_parts`.
"""

from __future__ import annotations

import functools
import gc
import hashlib
import json
import logging
import os
import queue
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.acp.skill_projection_parts.native_projection import _active_aliases
from kiro_crew.acp.skill_projection_parts.side_files import (
    _LEGACY_ALIAS_NAME_RE,
    _PINNED_SIDE_DIR,
    _PROJECTION_LEASE_MAX_BYTES,
    _PROJECTION_LEASE_RECORD_SUFFIX,
    _PROJECTION_LEASE_SCAN_LIMIT,
    _PROJECTION_LEASE_SCAN_MAX_SECONDS,
    _PROJECTION_LOCK_TIMEOUT_SECS,
    _ensure_projection_metadata_directory,
    _hold_legacy_publication_lock,
    _legacy_lease_dir,
    _legacy_lock_path,
    _legacy_metadata_dir,
    _metadata_dirs,
    _path_exists,
    _per_spawn_prune_due,
    _projection_alias_lock,
    _projection_lease_dir,
    _prune_budget_within,
    _read_lease_record,
    _scan_legacy_lease_dir,
    _scan_projection_leases,
    _unlink_alias_if_unchanged,
    _unlink_projection_lease_if_unchanged,
)
from kiro_crew.acp.skill_projection_parts.view_ownership import (
    _MANAGED_AGENT,
    _MANAGED_ALIAS_SHA256,
    _MANAGED_CREW_HOME,
    _MANAGED_SOURCE,
    _home_tag,
    _is_legacy_projected_view,
    _managed_marker,
    _managed_metadata_for_alias,
    _parsed_spec,
    _same_crew_home,
    _sidecar_record,
    _sidecar_record_in,
    _view_home_tag,
)
from kiro_crew.acp.skill_projection_parts.view_sources import (
    _VIEW_LEDGER_NAME,
    _admissible_source_agent,
    _import_legacy_ledger,
    _read_json_regular_file,
    _read_view_ledger,
    _ViewLedgerWrites,
)
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX, volatile_env_keys
from kiro_crew.atomic_write import atomic_write, on_event_loop
from kiro_crew.config.paths import data_home, kiro_agents_dir
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.acp.skill_projection")


# Candidate work is bounded independently of successful reclamation. Retained
# aliases must not turn a prune under the publication lock into an unbounded scan.
# It is the unit of the ENUMERATION ceiling, the part of the section the time
# budget below does not cover: one call yields at most this many stale candidates
# and walks at most three times this many directory entries (one limit for stale
# candidates, one for ordinary authored entries, one of retained-alias credit)
# before the budgeted classification starts (see _projection_prune_candidates).
_PROJECTION_PRUNE_WORK_LIMIT = 4096
# A CEILING on reclaims per run, never a floor: the time budget below can end a
# call having reclaimed none at all. It is headroom over the aliases one run
# publishes, so a call that does reach them covers the steady-state orphan rate
# as well as some backlog. It bounds no part of the critical section: a candidate
# that is unreclaimable costs a full classification and never increments it,
# which is why the section carries its own budget below.
_PRUNE_MAX_RECLAIMS_PER_RUN = 64
# The boot drain runs the per-spawn prune in a loop, so each batch holds the
# publication lock at most as long as one spawn's prune does. The pause between
# batches lets a waiting spawn take the lock: a blocked acquire polls with
# backoff up to the lock's poll cap, so the pause exceeds that cap (asserted by
# test). One batch reclaiming nothing can be a budget spent on kept or leased
# entries before its random start offset reached a stale one, so the drain
# stops only after this many CONSECUTIVE idle batches, and the batch count
# bounds it even if another writer keeps refilling the directory.
_DRAIN_MAX_BATCHES = 1000
_DRAIN_IDLE_BATCHES = 3
_DRAIN_BATCH_PAUSE_SECS = platform_compat._LOCK_POLL_MAX_SECS * 2
# The ONE window the re-preparation contract does not cover, and the only thing
# this age excludes. A publisher from a build that predates the lease holds no
# lease, so between its write and kiro-cli reading `--agent` its alias looks
# exactly like backlog -- and that process will NOT re-prepare, because it
# already did, so a deletion there is a failed spawn rather than an eviction.
# This is deliberately NOT a liveness proxy (the reason an age cut-off was
# rejected for the recorded path): it only has to exceed publish-to-spawn, which
# is milliseconds, and the real backlog is hours to days old.
_LEGACY_RECLAIM_MIN_AGE_SECS = 600.0


# The budget for one call's classification work, and a BETWEEN-candidate one: it
# bounds how many candidates are walked, not how long any single one takes, and
# the directory enumeration that precedes the walk is outside it. Derived from the
# lock ceiling, so retuning the ceiling moves the budget and keeps the reserve.
_PRUNE_MAX_SECONDS_PER_RUN = _prune_budget_within(_PROJECTION_LOCK_TIMEOUT_SECS)


# Ownership sidecars one prune may reclaim when their alias is already gone.
# Metadata only -- no kiro-cli cost rides on it -- so a modest ceiling that the
# boot drain multiplies by its batch count is enough to retire a backlog.
_SIDECAR_SWEEP_MAX_PER_RUN = 256


def _eager_prune(directory: Path, crew_home_id: str, *, force: bool = False) -> None:
    """Prune this home's unleased views once, right after a projection ended.

    The per-spawn prune reclaims a session's views only when the NEXT spawn
    comes, so on a host that is winding down they sit in the agents directory
    (and in the Kiro IDE's agent list) until then. This runs the same bounded
    prune under the same lock as soon as the lease is gone.

    A per-spawn eager prune (``force=False``) never waits for this process's own
    queue -- a start that holds the in-process lock runs its own prune, so a busy
    queue means the work is covered, not skipped -- and honours the
    ``_per_spawn_prune_due`` spacing.

    An END (``force=True``: a release or a drop) is a terminal event with no
    other party covering it, so it both bypasses the spacing AND QUEUES for the
    in-process lock rather than skipping when it is busy. That matters when
    sessions end together -- a gateway stop runs each end on its own
    ``kirocrew-skill-view-prune`` thread -- or when one ends inside the spacing
    window a recent spawn opened: a zero-wait end would fail the lock the first
    end holds and leave its own view on disk until the next spawn or boot drain,
    the very "gateway stop" case this reclaim exists for. Ends reach here through
    :func:`_coalesced_end_prune`, so at most one end per target queues here at a
    time, and because each end releases its lease before it asks, the walk that
    runs after the wait already sees this end's lease gone.
    Every other gate still holds: the cross-process lock ceiling, the lease scan
    and the time budget. Never raises (the queue timeout is an ``OSError``).
    """
    try:
        with _projection_alias_lock(directory, in_process_wait=None if force else 0.0):
            if force or _per_spawn_prune_due(directory):
                _prune_stale_managed_aliases(directory, crew_home_id, keep=set())
    except OSError as exc:
        logger.debug("skill projection: eager prune skipped; lock or I/O error: %s", exc)
    except Exception:
        logger.warning("skill projection: eager prune failed", exc_info=True)


def _eager_prune_in(
    side_dir: Path, directory: Path, crew_home_id: str, *, force: bool = False
) -> None:
    """:func:`_eager_prune` against *side_dir*, the side directory the projection used."""
    _PINNED_SIDE_DIR.path = side_dir
    try:
        _eager_prune(directory, crew_home_id, force=force)
    finally:
        _PINNED_SIDE_DIR.path = None


# The thread running a cyclic collection right now, or None. A finalizer the
# collector runs executes on whichever thread happened to allocate, at that
# point in its code -- possibly inside a non-reentrant lock such as a stdlib
# executor's ``submit`` lock. The end of a projection takes locks and calls into
# the subprocess pool, whose bounded request goes through such an executor, so
# run there it can wait on a lock its own thread already holds, forever. A
# collection never nests and runs on one thread, so one slot is enough.
_COLLECTING_THREAD: int | None = None


def _note_collection(phase: str, _info: dict[str, int]) -> None:
    """``gc.callbacks`` hook: record which thread is collecting. Takes no lock."""
    global _COLLECTING_THREAD
    _COLLECTING_THREAD = threading.get_ident() if phase == "start" else None


gc.callbacks.append(_note_collection)

# Ends of projections the cyclic collector freed, run in order by the reaper
# thread. ``SimpleQueue.put`` is reentrant: safe from inside a collection.
_COLLECTED_ENDS: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
_REAPER_LOCK = threading.Lock()
_REAPER: threading.Thread | None = None


def _ensure_projection_reaper() -> None:
    """Start the thread that ends collected projections, if it is not running.

    Called where a lease finalizer is armed -- ordinary code, never a collection,
    which must not start a thread. A fork leaves the parent's thread dead in the
    child, so the next arm there starts a new one; one that cannot start
    (interpreter shutdown) leaves the queue to the next arm.
    """
    global _REAPER
    with _REAPER_LOCK:
        if _REAPER is not None and _REAPER.is_alive():
            return
        reaper = threading.Thread(
            target=_reap_collected_projections, name="kirocrew-skill-view-reaper", daemon=True
        )
        try:
            reaper.start()
        except RuntimeError:
            logger.debug("skill projection: reaper thread not started", exc_info=True)
            return
        _REAPER = reaper


def _reap_collected_projections() -> None:
    """Run each collected projection's end, one at a time, forever."""
    while True:
        end = _COLLECTED_ENDS.get()
        try:
            end()
        except Exception:
            logger.warning("skill projection: ending a collected projection failed", exc_info=True)


def _end_projection(
    lease_stack: ExitStack, directory: Path, crew_home_id: str, side_dir: Path
) -> None:
    """The lease finalizer: release the lease, then prune eagerly.

    Called by :meth:`NativeSkillProjection.release` (the client's shutdown path
    calls that on the event-loop thread) or, for a projection that was merely
    dropped, when its last reference goes or the cyclic collector frees it.
    Inside a collection nothing runs here: the end is queued for the reaper
    thread (:data:`_COLLECTING_THREAD` says why). Otherwise, off the loop the
    whole sequence runs inline. On the loop ALL of it goes to a daemon
    thread: the prune waits on a file lock (up to the publication lock's
    ceiling), and the lease release itself unlocks and closes the holder, then
    stats, opens and unlinks two files -- blocking calls whose cost on a slow or
    network-homed data home would stall every session the gateway serves. A
    thread that cannot be started (interpreter shutdown) releases the lease
    inline, the two unlinks the finalizer always did, and drops the prune: the
    next spawn's prune covers it.
    """
    if _COLLECTING_THREAD == threading.get_ident():
        _COLLECTED_ENDS.put(
            functools.partial(
                _end_projection_off_loop, lease_stack, side_dir, directory, crew_home_id
            )
        )
        return
    if on_event_loop():
        try:
            threading.Thread(
                target=_end_projection_off_loop,
                args=(lease_stack, side_dir, directory, crew_home_id),
                name="kirocrew-skill-view-prune",
                daemon=True,
            ).start()
            return
        except RuntimeError:
            logger.debug("skill projection: lease release thread not started", exc_info=True)
        lease_stack.close()
        return
    _end_projection_off_loop(lease_stack, side_dir, directory, crew_home_id)


def _end_projection_off_loop(
    lease_stack: ExitStack, side_dir: Path, directory: Path, crew_home_id: str
) -> None:
    """:func:`_end_projection`'s work, on a thread that is not the event loop.

    This runs for a release or a drop -- both terminal -- so the prune is forced
    past the per-spawn spacing: a burst of ends must reclaim every home, not just
    the first within the spacing window (see :func:`_eager_prune`).
    """
    try:
        lease_stack.close()
    finally:
        _coalesced_end_prune(side_dir, directory, crew_home_id)


# Ends whose prune is already covered by one running for the same target. An end
# releases its lease BEFORE it asks for a prune, so any prune pass that STARTS
# after that request sees the lease gone and covers it. Without coalescing, a
# subagent fan-out queues one forced prune per ended projection on the same
# in-process lock the session starts wait on, each spending its full walk budget,
# and the starts behind them run out their in-process wait and are refused.
_END_PRUNE_GUARD = threading.Lock()
_END_PRUNE_RUNNING: set[tuple[str, str, str]] = set()
_END_PRUNE_REQUESTED: set[tuple[str, str, str]] = set()


def _coalesced_end_prune(side_dir: Path, directory: Path, crew_home_id: str) -> None:
    """Run the forced end prune, or hand it to the one already running for this target.

    At most one end prune per (side directory, agents directory, home) runs or
    waits on the alias lock at a time. An end that arrives while one runs records
    a request and returns. The end that ran the first pass runs it on its own
    thread, as before; if requests arrived meanwhile, the follow-up passes go to
    one daemon thread, which repeats while requests keep arriving, so the caller
    is not held for the length of a wave. Every end is still followed by a prune
    that started after its lease was released. Never raises.
    """
    key = (side_dir.as_posix(), directory.absolute().as_posix(), crew_home_id)
    with _END_PRUNE_GUARD:
        if key in _END_PRUNE_RUNNING:
            _END_PRUNE_REQUESTED.add(key)
            return
        _END_PRUNE_RUNNING.add(key)
    try:
        _eager_prune_in(side_dir, directory, crew_home_id, force=True)
        if not _end_prune_more_requested(key):
            return
        try:
            threading.Thread(
                target=_run_requested_end_prunes,
                args=(key, side_dir, directory, crew_home_id),
                name="kirocrew-skill-view-prune",
                daemon=True,
            ).start()
            return
        except RuntimeError:
            logger.debug("skill projection: follow-up prune thread not started", exc_info=True)
        _run_requested_end_prunes(key, side_dir, directory, crew_home_id)
    except BaseException:
        with _END_PRUNE_GUARD:
            _END_PRUNE_RUNNING.discard(key)
            _END_PRUNE_REQUESTED.discard(key)
        raise


def _end_prune_more_requested(key: tuple[str, str, str]) -> bool:
    """Consume a pending request for *key*, or end its run when there is none."""
    with _END_PRUNE_GUARD:
        if key in _END_PRUNE_REQUESTED:
            _END_PRUNE_REQUESTED.discard(key)
            return True
        _END_PRUNE_RUNNING.discard(key)
        return False


def _run_requested_end_prunes(
    key: tuple[str, str, str], side_dir: Path, directory: Path, crew_home_id: str
) -> None:
    """The follow-up passes of one coalesced run, after its first request was consumed."""
    try:
        while True:
            _eager_prune_in(side_dir, directory, crew_home_id, force=True)
            if not _end_prune_more_requested(key):
                return
    except BaseException:
        with _END_PRUNE_GUARD:
            _END_PRUNE_RUNNING.discard(key)
            _END_PRUNE_REQUESTED.discard(key)
        raise


def _sweep_orphan_sidecars(
    directory: Path, crew_home_id: str, *, skip: set[str], deadline: float
) -> int:
    """Remove this home's ownership sidecars whose alias is already gone.

    A sidecar outlived its alias whenever something other than the recorded
    pair removal took the alias: an older build's unrecorded path unlinked the
    alias alone, and so does an operator deleting ``kirocrew-skill-view-*.json``
    by hand. Each one is metadata only, but the directory is walked by
    kiro-cli's own directory scans, so the backlog is not free.

    A sidecar is removed only when its stem has the shape the projection derives,
    nothing this call keeps or any live lease names it (*skip*), no alias by
    that name exists (any doubt reads as present), and the record carries the
    marker AND names THIS data home -- another home's record is that home's to
    retire. Runs under the publication lock, so no publisher can be mid-pair.
    Bounded by the same entry-walk limit as the alias walk, a random start
    offset, :data:`_SIDECAR_SWEEP_MAX_PER_RUN` and *deadline*. Returns how many
    it removed, over this home's records directory and then the legacy one in
    *directory* (:func:`_metadata_dirs`), each under the same bounds.
    """
    removed = 0
    for metadata_dir in _metadata_dirs(directory):
        if time.monotonic() >= deadline:
            break
        removed += _sweep_orphan_sidecars_in(
            directory, metadata_dir, crew_home_id, skip=skip, deadline=deadline
        )
    return removed


def _sweep_orphan_sidecars_in(
    directory: Path, metadata_dir: Path, crew_home_id: str, *, skip: set[str], deadline: float
) -> int:
    """:func:`_sweep_orphan_sidecars` over ONE records directory."""
    info = pinned_fs.lstat_by_name(metadata_dir)
    if (
        info is None
        or platform_compat.is_link_or_junction(metadata_dir)
        or not stat.S_ISDIR(info.st_mode)
    ):
        return 0
    stems: list[str] = []
    walked = 0
    try:
        with os.scandir(metadata_dir) as entries:
            for entry in entries:
                if walked >= _PROJECTION_PRUNE_WORK_LIMIT:
                    break
                walked += 1
                if not entry.name.endswith(".json"):
                    continue
                stem = entry.name[: -len(".json")]
                if _LEGACY_ALIAS_NAME_RE.fullmatch(stem) and stem not in skip:
                    stems.append(stem)
    except OSError:
        logger.debug("skill projection: cannot scan %s for sidecars", metadata_dir, exc_info=True)
        return 0
    offset = _prune_start_offset(len(stems))
    doomed: list[tuple[str, dict[str, Any], Path, tuple[int, int]]] = []
    for stem in stems[offset:] + stems[:offset]:
        if len(doomed) >= _SIDECAR_SWEEP_MAX_PER_RUN or time.monotonic() >= deadline:
            break
        if _path_exists(directory / f"{stem}.json"):
            continue
        record, path, identity = _sidecar_record_in(metadata_dir, stem)
        if (
            record is None
            or identity is None
            or not _same_crew_home(record.get(_MANAGED_CREW_HOME), crew_home_id)
        ):
            continue
        doomed.append((stem, record, path, identity))
    # The sidecar is the last record of which agent a view with no alias was
    # built from, and a conversation may still name that view. Its mapping goes
    # into the ledger first, in one write for the whole batch; a batch the
    # ledger cannot take is kept for a later run.
    ledger = _ViewLedgerWrites(directory, crew_home_id)
    views = {
        stem: str(record.get(_MANAGED_AGENT))
        for stem, record, _path, _identity in doomed
        if _admissible_source_agent(record.get(_MANAGED_AGENT))
    }
    if views and not ledger.retain_many(views):
        logger.debug("skill projection: view ledger unwritable; ownership sidecars kept")
        return 0
    removed = 0
    for stem, _record, path, identity in doomed:
        if time.monotonic() >= deadline:
            break  # the rest is recorded already and goes on a later run
        if _path_exists(directory / f"{stem}.json"):
            continue
        if _unlink_projection_lease_if_unchanged(path, identity, what="ownership sidecar"):
            removed += 1
    if removed:
        logger.info("skill projection: removed %d ownership sidecar(s) with no alias", removed)
    return removed


_ALIAS_LOCK_SUFFIX = ".lock"


def _unlink_unheld_lock_file(path: Path, identity: tuple[int, int]) -> bool:
    """Remove the lock file at *path* only while nobody holds it, and only that inode.

    A launcher may still hold (or re-take) an old ``<stem>.lock``; unlinking it then
    would let the next launcher lock a fresh inode while the first still holds the
    old one, and their rewrites would race. So the file is opened without
    following links, its identity checked, and an exclusive lock taken without
    waiting: a held lock is left alone, and the unlink happens while ours is held.

    The open is :func:`platform_compat.open_lock_file_for_sweep`, not a bare
    ``os.open``: on Windows a CRT descriptor omits ``FILE_SHARE_DELETE``, so our
    own verification handle would block the very ``unlink`` below with a sharing
    violation and the sweep would leave genuine residue behind. The helper opens
    with delete-sharing on Windows so the unlink lands while the handle (and its
    lock) is still held, matching POSIX, where an open fd never blocks an unlink.
    """
    try:
        fd = platform_compat.open_lock_file_for_sweep(path)
    except OSError:
        return False
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != identity or opened.st_size != 0:
            return False
        if not platform_compat.try_acquire_lock(fd, exclusive=True):
            return False
        return _unlink_projection_lease_if_unchanged(path, identity, what="orphaned alias lock")
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _sweep_orphan_alias_locks(directory: Path, *, deadline: float) -> int:
    """Remove empty ``<alias>.lock`` files left beside aliases that are gone.

    This module never creates one. A launcher that rewrites every spec in the
    agents directory takes a per-file lock named ``<stem>.lock`` next to each
    spec and leaves it behind (one host: 1,661 of them beside 23 aliases). For an
    authored agent that is the launcher's file; for a skill-view alias the name is
    in the projection's namespace and outlives the alias it guarded, so each prune of
    an alias strands one. Only that residue is removed: the exact derived name
    shape, a regular EMPTY file, no ``<stem>.json`` beside it, and older than the
    legacy reclaim age so a launcher pass that is still running is never raced.
    The same walk limit, random start, per-run ceiling and *deadline* as the
    sidecar sweep bound it. Returns how many it removed.
    """
    names: list[str] = []
    walked = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if walked >= _PROJECTION_PRUNE_WORK_LIMIT:
                    break
                walked += 1
                if entry.name.endswith(_ALIAS_LOCK_SUFFIX) and _LEGACY_ALIAS_NAME_RE.fullmatch(
                    entry.name[: -len(_ALIAS_LOCK_SUFFIX)]
                ):
                    names.append(entry.name)
    except OSError:
        logger.debug("skill projection: cannot scan %s for alias locks", directory, exc_info=True)
        return 0
    offset = _prune_start_offset(len(names))
    removed = 0
    for name in names[offset:] + names[:offset]:
        if removed >= _SIDECAR_SWEEP_MAX_PER_RUN or time.monotonic() >= deadline:
            break
        path = directory / name
        spec = directory / f"{name[: -len(_ALIAS_LOCK_SUFFIX)]}.json"
        info = pinned_fs.lstat_by_name(path)
        if (
            info is None
            or platform_compat.is_link_or_junction(path)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size != 0
            or time.time() - info.st_mtime < _LEGACY_RECLAIM_MIN_AGE_SECS
            or _path_exists(spec)
        ):
            continue
        if _unlink_unheld_lock_file(path, (info.st_dev, info.st_ino)):
            removed += 1
    if removed:
        logger.info("skill projection: removed %d orphaned alias lock file(s)", removed)
    return removed


def _prune_start_offset(count: int) -> int:
    """Where this call begins its bounded walk over *count* candidates.

    A time-budgeted walk from a fixed start examines the same prefix every call,
    so unreclaimable candidates at the front would hide the rest of the list
    behind the budget permanently. Moving the start makes every entry OF THIS LIST reachable
    across calls. The list itself is the bounded window
    :func:`_projection_prune_candidates` yields, so an alias beyond that entry
    ceiling in a stably padded directory is not promised a turn: the rotation
    bounds lock time within the window, not eventual drain of the whole
    directory. It cannot be a cursor in memory: the workload this bound exists
    for spawns a fresh process per cron run, so a process-local cursor restarts at
    zero every time and rotates nothing.
    """
    if count <= 0:
        return 0
    return secrets.randbelow(count)


def _projection_prune_candidates(directory: Path, skip: set[str]) -> Iterator[Path]:
    """Yield bounded stale candidates without letting retained aliases starve them.

    Retained aliases do not consume the stale-work budget. Total directory
    traversal is bounded separately: one work-limit for stale candidates, one
    for ordinary authored entries, and at most one work-limit of retained-alias
    credit. A larger skip set therefore cannot turn membership into an unbounded
    traversal under the publication lock.
    """
    work_limit = _PROJECTION_PRUNE_WORK_LIMIT
    stale_work = 0
    walked = 0
    walk_ceiling = (2 * work_limit) + min(len(skip), work_limit)
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if walked >= walk_ceiling or stale_work >= work_limit:
                    # INFO, like the lease-scan ceiling: past this window an alias
                    # is not promised a turn, so the deferral must be visible.
                    logger.info(
                        "skill projection: deferred remaining alias pruning after %d stale "
                        "candidate(s) across %d walked entr(ies)",
                        stale_work,
                        walked,
                    )
                    return
                # Count every entry before filtering. Otherwise authored files or
                # arbitrary padding can bypass the traversal ceiling just as
                # non-record files once bypassed the lease-scan ceiling.
                walked += 1
                if not (
                    entry.name.startswith(NATIVE_SKILL_ALIAS_PREFIX)
                    and entry.name.endswith(".json")
                ):
                    continue
                stem = entry.name[: -len(".json")]
                if stem in skip:
                    continue
                stale_work += 1
                yield directory / entry.name
    except OSError:
        logger.debug("skill projection: cannot scan %s to prune aliases", directory, exc_info=True)


def _prune_stale_managed_aliases(
    directory: Path, crew_home_id: str, *, keep: set[str], sweep_sidecars: bool = True
) -> int:
    """Remove aliases owned by this Kiro Crew data home that no projection uses.

    Runs while the publication lock is held, so a deletion cannot land on an alias
    a publisher that takes that lock is writing; a build predating the lease takes
    no part in it, and the minimum age is what covers that one. A time budget keeps
    the held lock down to a slice of the walk rather than all of it. An alias is
    kept when this run publishes it, a projection in this process holds it, or a
    held lease in any process names it. Everything else this data home recorded
    is a cache entry for a projection that has ended: every consumer re-prepares
    before it sends an alias, so removing one costs the next spawn of that agent
    one rewrite and nothing else. Aliases are keyed on the agent's view, so a new
    one appears when an agent's spec changes, not once per run.

    With *sweep_sidecars* (the default) the rest of the same time budget then
    retires this home's ownership sidecars whose alias is gone
    (:func:`_sweep_orphan_sidecars`) and the empty ``<alias>.lock`` residue an
    external rewriter left beside gone aliases (:func:`_sweep_orphan_alias_locks`);
    the boot drain sweeps on its own instead, so it can count them. Returns how
    many aliases it removed.

    An older build sharing *directory* publishes under a lock of its own, in
    *directory*; this prune holds that lock too, from the lease scan through the
    last reclaim (:func:`_hold_legacy_publication_lock`), so no lease an older
    build takes can slip in behind the scan. A lock it cannot take is
    uncertainty, and the run removes nothing.
    """
    with ExitStack() as exclusion:
        if not _hold_legacy_publication_lock(directory, exclusion):
            logger.debug(
                "skill projection: an older build holds the publication lock in %s; "
                "pruning nothing this run",
                directory,
            )
            return 0
        return _prune_excluding_older_publishers(
            directory, crew_home_id, keep=keep, sweep_sidecars=sweep_sidecars
        )


def _prune_excluding_older_publishers(
    directory: Path, crew_home_id: str, *, keep: set[str], sweep_sidecars: bool
) -> int:
    """:func:`_prune_stale_managed_aliases` with the older build's lock already held."""
    # ONE bounded lease scan per prune, under the held publication lock, rather
    # than one full scan per candidate (which multiplied the candidate cap by the
    # lease-scan cap). It also reclaims crash/finalizer lease residue as a side
    # effect. Any uncertainty is fail-closed: authorize no deletion this run and
    # let a later spawn re-scan, because a candidate whose covering lease the scan
    # could not read must be treated as live.
    live_aliases, lease_uncertain = _scan_projection_leases(directory, legacy_locked=True)
    if lease_uncertain:
        return 0
    active = _active_aliases()
    # Kept, active, and live-lease aliases are never reclaimable. The candidate
    # scan skips them WITHOUT charging them to the stale-work budget, so a run's
    # own just-published aliases (which sort early) cannot starve the backlog of
    # examination -- the order-dependent defect this reconciliation closes. The
    # scan still bounds total traversal at the class level plus this bounded skip
    # set, so the skip is not an unbounded bypass.
    skip = keep | active | live_aliases
    # Materialized so the time-budgeted walk below can start at a rotated offset.
    # The entry-walk limit inside _projection_prune_candidates bounds this list,
    # so the enumeration that precedes the budget is bounded too; rotation then
    # spreads a budgeted walk across that bounded window on successive spawns.
    candidates = list(_projection_prune_candidates(directory, skip))
    # The reclaim ceiling is expressed over the same skip accounting: each run
    # publishes len(keep) aliases and leaves that many behind when it ends, so
    # the cap covers that steady-state rate plus a bounded backlog drain. The
    # candidate scan already excludes the skip set, so every yielded candidate is
    # unretained -- it may still prove foreign, malformed or unowned -- and the
    # two ceilings account for distinct work.
    cap = _PRUNE_MAX_RECLAIMS_PER_RUN + len(keep)
    offset = _prune_start_offset(len(candidates))
    candidates = candidates[offset:] + candidates[:offset]
    deadline = time.monotonic() + _PRUNE_MAX_SECONDS_PER_RUN
    reclaimed = 0
    examined = 0
    ledger = _ViewLedgerWrites(directory, crew_home_id)
    for index, path in enumerate(candidates):
        if reclaimed >= cap:
            logger.debug(
                "skill projection: reclaim cap reached (%d); the rest drains on later spawns",
                cap,
            )
            break
        if time.monotonic() >= deadline:
            logger.debug(
                "skill projection: prune budget spent after %d candidate(s); the rest drains on later spawns",
                examined,
            )
            break
        # Counted for EVERY candidate, not only the reclaimed ones: what the budget
        # has to cover is the classification, which an unreclaimable entry pays in
        # full. The candidate scan already excluded kept, active and leased names.
        examined += 1
        ledger.look_ahead(candidates, index)
        if _reclaim_prune_candidate(directory, path, crew_home_id, ledger):
            reclaimed += 1
    if reclaimed > 0:
        logger.info("skill projection: reclaimed %d unused alias(es)", reclaimed)
    if sweep_sidecars:
        _sweep_orphan_sidecars(directory, crew_home_id, skip=skip, deadline=deadline)
        _sweep_orphan_alias_locks(directory, deadline=deadline)
    return reclaimed


def _move_sidecar(source: Path, identity: tuple[int, int], target: Path) -> bool:
    """Move one ownership sidecar out of the legacy directory; ``True`` when *source* is gone.

    A rename when the two directories share a filesystem; otherwise a copy of
    the bytes already read and validated, then the identity-checked unlink the
    prune uses. An existing *target* (this home already republished the view
    under the new layout) is kept and the legacy copy only unlinked.
    """
    if _path_exists(target):
        return _unlink_projection_lease_if_unchanged(source, identity, what="ownership sidecar")
    try:
        os.replace(source, target)
        return True
    except OSError:
        pass
    try:
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            raw = os.read(fd, _PROJECTION_LEASE_MAX_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        return False
    if (opened.st_dev, opened.st_ino) != identity or len(raw) > _PROJECTION_LEASE_MAX_BYTES:
        return False
    try:
        atomic_write(target, raw.decode("utf-8"), restrict_to_owner=True)
    except (OSError, UnicodeDecodeError):
        return False
    return _unlink_projection_lease_if_unchanged(source, identity, what="ownership sidecar")


def _migrate_legacy_side_files(directory: Path, crew_home_id: str) -> None:
    """Carry this home's side files out of the agents directory, once, at boot.

    Builds before the side-file layout wrote the ownership sidecars, the view
    ledger, the lease records and the publication lock INTO *directory*, where
    the Kiro IDE lists them as agents. This moves what is provably this home's
    and leaves everything else:

    * a sidecar in the legacy records directory whose record names THIS data
      home moves to :func:`_projection_metadata_dir` (bounded per boot; the
      rest moves next boot, and every reader consults both directories meanwhile);
      another home's sidecar stays, it is that home's to move;
    * the legacy view ledger's entries are merged into this home's ledger (they
      are alias-to-agent names, harmless to copy); the legacy file itself is
      shared by every home that published here and names no home, so it stays
      while it holds any entry or cannot be read -- another home's stored view
      names resolve through it until that home's own migration copies them --
      and only an empty, readable one goes, and the directory only after it;
    * lease records are never moved -- a live one is lock-held by its process and
      a stale one is reclaimed by the lease scan -- the directory is removed only
      once it is empty;
    * the legacy lock file goes last, only when both legacy directories are gone
      (no older build is publishing here) and nobody holds it.

    Idempotent: every step checks what is on disk. Nothing is deleted whose name
    and content do not prove the projection wrote it. Runs under this home's
    publication lock, so no publisher of this home is mid-pair.
    """
    legacy_metadata = _legacy_metadata_dir(directory)
    info = pinned_fs.lstat_by_name(legacy_metadata)
    if (
        info is not None
        and not platform_compat.is_link_or_junction(legacy_metadata)
        and stat.S_ISDIR(info.st_mode)
    ):
        stems: list[str] = []
        walked = 0
        others = 0
        with os.scandir(legacy_metadata) as entries:
            for entry in entries:
                if walked >= _PROJECTION_PRUNE_WORK_LIMIT:
                    others += 1  # unexamined: the directory is not empty either way
                    break
                walked += 1
                if entry.name == _VIEW_LEDGER_NAME:
                    continue
                stem = entry.name[: -len(".json")] if entry.name.endswith(".json") else ""
                if _LEGACY_ALIAS_NAME_RE.fullmatch(stem):
                    stems.append(stem)
                else:
                    others += 1
        moved = 0
        if stems:
            target_dir = _ensure_projection_metadata_directory()
            for stem in stems:
                record, source, identity = _sidecar_record_in(legacy_metadata, stem)
                if (
                    record is None
                    or identity is None
                    or not _same_crew_home(record.get(_MANAGED_CREW_HOME), crew_home_id)
                ):
                    others += 1
                    continue
                if _move_sidecar(source, identity, target_dir / f"{stem}.json"):
                    moved += 1
                else:
                    others += 1
        ledger_path = legacy_metadata / _VIEW_LEDGER_NAME
        ledger_info = pinned_fs.lstat_by_name(ledger_path)
        if ledger_info is not None:
            legacy_ledger = _read_view_ledger(legacy_metadata)
            if legacy_ledger:
                try:
                    _import_legacy_ledger(_ensure_projection_metadata_directory(), legacy_ledger)
                except OSError:
                    logger.debug("skill projection: legacy view ledger not merged", exc_info=True)
            # The legacy ledger is SHARED: every home that ever published into
            # this agents directory wrote its view names there, and an entry
            # carries no home. Another home's stored view names resolve through
            # it until THAT home's own migration copies them, so this home may
            # not remove it while it holds any entry, or while it cannot be
            # read; only an empty, readable ledger goes. The directory, and the
            # lock after it, stay with it.
            raw_ledger = _read_json_regular_file(ledger_path)
            if (
                not isinstance(raw_ledger, dict)
                or raw_ledger
                or not stat.S_ISREG(ledger_info.st_mode)
            ):
                others += 1
            elif not others:
                _unlink_projection_lease_if_unchanged(
                    ledger_path, (ledger_info.st_dev, ledger_info.st_ino), what="view ledger"
                )
        if moved:
            logger.info(
                "skill projection: moved %d ownership sidecar(s) out of %s", moved, legacy_metadata
            )
        if not others:
            try:
                os.rmdir(legacy_metadata)
            except OSError:
                pass  # something arrived meanwhile; next boot looks again
    legacy_leases = _legacy_lease_dir(directory)
    lease_info = pinned_fs.lstat_by_name(legacy_leases)
    if (
        lease_info is not None
        and not platform_compat.is_link_or_junction(legacy_leases)
        and stat.S_ISDIR(lease_info.st_mode)
    ):
        # Reclaims crash residue the way every prune does; a live lease stays.
        _scan_legacy_lease_dir(directory, time.monotonic() + _PROJECTION_LEASE_SCAN_MAX_SECONDS)
        try:
            os.rmdir(legacy_leases)
        except OSError:
            pass  # not empty: an older build still holds a lease here
    if pinned_fs.lstat_by_name(legacy_metadata) is None and (
        pinned_fs.lstat_by_name(legacy_leases) is None
    ):
        lock_path = _legacy_lock_path(directory)
        lock_info = pinned_fs.lstat_by_name(lock_path)
        if (
            lock_info is not None
            and not platform_compat.is_link_or_junction(lock_path)
            and stat.S_ISREG(lock_info.st_mode)
            and lock_info.st_size == 0
        ):
            _unlink_unheld_lock_file(lock_path, (lock_info.st_dev, lock_info.st_ino))


def drain_stale_aliases() -> int:
    """Best-effort boot drain of unused aliases this data home owns; never raises.

    The per-spawn prune is capped, so a backlog of thousands takes hundreds of
    spawns to clear, and every spawn in between still lists the backlog in
    kiro-cli's subagent tool. This runs once at gateway boot and repeats that
    same prune, under the publication lock, until three batches in a row
    reclaim nothing or the batch limit is reached, so some unused aliases can
    remain for later spawns. Returns how many aliases it removed.
    """
    try:
        directory = kiro_agents_dir()
        crew_home_id = data_home().absolute().as_posix()
    except (OSError, ValueError, RuntimeError):
        logger.debug("skill projection: cannot resolve directories to drain", exc_info=True)
        return 0
    if pinned_fs.lstat_by_name(directory) is None:
        return 0
    total = 0
    sidecars = 0
    idle_batches = 0
    migrated = False
    for batch in range(_DRAIN_MAX_BATCHES):
        if batch:
            time.sleep(_DRAIN_BATCH_PAUSE_SECS)
        swept = 0
        try:
            with _projection_alias_lock(directory):
                if not migrated:
                    # Once per boot, under the first lock this drain takes: the
                    # side files an older build left in the agents directory
                    # come over to this home before anything is pruned by them.
                    migrated = True
                    try:
                        _migrate_legacy_side_files(directory, crew_home_id)
                    except OSError:
                        logger.debug(
                            "skill projection: legacy side-file migration skipped", exc_info=True
                        )
                reclaimed = _prune_stale_managed_aliases(
                    directory, crew_home_id, keep=set(), sweep_sidecars=False
                )
                # Its own budget, the size of one prune walk's: a batch holds
                # the lock for at most two walks' worth, both bounded.
                sweep_deadline = time.monotonic() + _PRUNE_MAX_SECONDS_PER_RUN
                swept = _sweep_orphan_sidecars(
                    directory, crew_home_id, skip=_active_aliases(), deadline=sweep_deadline
                ) + _sweep_orphan_alias_locks(directory, deadline=sweep_deadline)
        except OSError as exc:
            # A concurrent spawn holding the lock, or a lock-file fault; either
            # way an idle batch, so a fault ends the drain at the idle bound.
            logger.debug("skill projection: drain batch skipped; lock or I/O error: %s", exc)
            reclaimed = 0
        except Exception:
            logger.warning("skill projection: drain failed", exc_info=True)
            break
        total += reclaimed
        sidecars += swept
        idle_batches = 0 if reclaimed or swept else idle_batches + 1
        if idle_batches >= _DRAIN_IDLE_BATCHES:
            break
    else:
        logger.warning(
            "skill projection: drain stopped at its batch bound (%d) after %d alias(es); "
            "the rest drains on later spawns or the next boot",
            _DRAIN_MAX_BATCHES,
            total,
        )
    if total > 0 or sidecars > 0:
        logger.info(
            "skill projection: boot drain removed %d unused alias(es) and %d orphaned "
            "ownership sidecar(s) or alias lock file(s)",
            total,
            sidecars,
        )
    return total


def _reclaim_prune_candidate(
    directory: Path, path: Path, crew_home_id: str, ledger: _ViewLedgerWrites
) -> bool:
    """Classify one stale candidate and remove it when ownership proves it is ours.

    The per-candidate step of :func:`_prune_stale_managed_aliases`, which charges
    each call against its time budget. The sidecar goes with its alias only once
    *ledger* holds the view's source agent (see :class:`_ViewLedgerWrites`);
    when the ledger cannot take it, the alias still goes and the sidecar, which
    then answers for the view on its own, stays for a later sweep. Returns
    whether the alias was removed.
    """
    candidate = pinned_fs.lstat_by_name(path)
    if candidate is None:
        return False
    identity = (candidate.st_dev, candidate.st_ino)
    try:
        raw = safe_read_file_bytes(str(path))
    except FileTooLargeError:
        return False
    if raw is None:
        return False
    managed = _managed_metadata_for_alias(directory, path, raw)
    if managed is None:
        record, sidecar_path, sidecar_identity = _sidecar_record(directory, path.stem)
        if sidecar_identity is not None:
            # A sidecar IS there; it just does not bind these bytes. That is not
            # the pre-lifecycle case below, whose premise is that no record
            # exists: a launcher re-serialized an alias this home
            # published before its record carried the view digest. Every build
            # that writes a sidecar also holds a lease for as long as it uses the
            # alias, so the caller's lease gate already covers the publish-to-
            # spawn window the legacy age exists for, and the pair goes together
            # -- but only when the record names THIS home and the file is still
            # a projected view. Anything else (another home's record, a record
            # that is not provably ours) keeps the alias.
            if (
                record is None
                or not _same_crew_home(record.get(_MANAGED_CREW_HOME), crew_home_id)
                or not _is_legacy_projected_view(path, raw)
            ):
                return False
            current = pinned_fs.lstat_by_name(path)
            if current is None or (current.st_dev, current.st_ino) != identity:
                return False
            try:
                current_raw = safe_read_file_bytes(str(path))
            except FileTooLargeError:
                return False
            if current_raw != raw:
                return False
            if _sidecar_record(directory, path.stem) != (record, sidecar_path, sidecar_identity):
                return False
            recorded = ledger.retain(path.stem, record)
            if _unlink_alias_if_unchanged(path, identity):
                if recorded:
                    _unlink_projection_lease_if_unchanged(
                        sidecar_path, sidecar_identity, what="ownership sidecar"
                    )
                logger.debug("skill projection: pruned re-serialized alias %s", path.name)
                return True
            return False
        # No ownership record at all. Either a pre-lifecycle build wrote this, so
        # the recorded-pair proof is unavailable and the re-preparation
        # contract carries the removal instead (see _is_legacy_projected_view);
        # or ANOTHER data home sharing this agents directory wrote it, with its
        # record under its own data home where this process cannot look. The
        # view's own description tells the two apart (_view_home_tag): a tag
        # that is not this home's is that home's live view and is left alone,
        # exactly as a foreign sidecar is. A tag that IS this home's -- its
        # record lost -- takes the legacy path below with every one of its gates.
        # Every gate the caller applied still holds: it is not in this run's set,
        # no live projection claims it, and no held lease names it.
        home_tag = _view_home_tag(_parsed_spec(raw))
        if home_tag is not None and home_tag != _home_tag(crew_home_id):
            return False
        if _is_legacy_projected_view(path, raw):
            if time.time() - candidate.st_mtime < _LEGACY_RECLAIM_MIN_AGE_SECS:
                # Possibly mid-publish by a build that holds no lease. A
                # negative age (clock moved) lands here too, which is the
                # safe side.
                return False
            current = pinned_fs.lstat_by_name(path)
            if current is None or (current.st_dev, current.st_ino) != identity:
                return False
            try:
                current_raw = safe_read_file_bytes(str(path))
            except FileTooLargeError:
                return False
            if current_raw != raw or not _is_legacy_projected_view(path, current_raw):
                return False
            if _managed_metadata_for_alias(directory, path, current_raw) is not None:
                # A concurrent preparation republished it WITH a record
                # between the two reads; that owner decides its lifetime.
                return False
            if _unlink_alias_if_unchanged(path, identity):
                logger.debug("skill projection: pruned unrecorded legacy alias %s", path.name)
                return True
            logger.debug("skill projection: legacy alias changed before removal: %s", path)
        return False
    metadata, metadata_path, metadata_identity, metadata_raw = managed
    if not _same_crew_home(metadata.get(_MANAGED_CREW_HOME), crew_home_id):
        return False

    # Re-open and revalidate the exact alias and ownership sidecar at
    # deletion time. A sidecar digest binds the ownership record to these
    # projected bytes; any replacement or uncertainty keeps both files.
    current = pinned_fs.lstat_by_name(path)
    if current is None or (current.st_dev, current.st_ino) != identity:
        return False
    try:
        current_raw = safe_read_file_bytes(str(path))
    except FileTooLargeError:
        return False
    if current_raw != raw:
        return False
    current_managed = _managed_metadata_for_alias(directory, path, current_raw)
    if current_managed is None:
        return False
    current_metadata, current_metadata_path, current_metadata_identity, current_metadata_raw = (
        current_managed
    )
    if (
        current_metadata != metadata
        or current_metadata_path != metadata_path
        or current_metadata_identity != metadata_identity
        or current_metadata_raw != metadata_raw
        or not _same_crew_home(current_metadata.get(_MANAGED_CREW_HOME), crew_home_id)
    ):
        return False
    recorded = ledger.retain(path.stem, current_metadata)
    if _unlink_alias_if_unchanged(path, identity):
        if recorded and metadata_path is not None and metadata_identity is not None:
            _unlink_projection_lease_if_unchanged(metadata_path, metadata_identity)
        logger.debug("skill projection: pruned unused managed alias %s", path.name)
        return True
    logger.debug("skill projection: unused alias changed before removal: %s", path)
    return False


# Bounds on what the census RETAINS, not on what it counts: every retained
# collection has a ceiling and the result says when one was hit. The alias
# ceiling is the diagnostic's own memory budget, far above the backlogs that
# motivated the census (28k on one host) and comfortably below what a doctor run
# may hold in memory. The lease walk has no ceiling of its own: it is bounded by
# the reclaim scan's :data:`_PROJECTION_LEASE_SCAN_LIMIT`, charged per directory
# entry exactly as that scan charges it, so ONE constant decides both where the
# census stops and where every prune defers. Past either bound the diagnostic
# cannot say what a later reclaim pass will do and reports that instead of
# guessing.
_CENSUS_MAX_ALIASES = 65536


def census_projected_aliases(directory: Path) -> dict[str, int]:
    """Count the projected aliases in *directory* without touching any of them.

    A read-only census for diagnostics, in this module so the lease-record and
    sidecar shapes it reads are the ones :func:`_prune_stale_managed_aliases`
    reclaims by. Returns plain counts:

    ``total``
        regular ``<prefix>*.json`` files directly in *directory*;
    ``leased``
        of those, how many a lease record names. The record is read with the
        reclaim's own parse and NO lock is probed -- a probe would reclaim
        residue as a side effect, which a census must not do -- so this is
        "published by some projection", not "held right now": a crash-stale
        record counts here until the next spawn's probe reclaims it;
    ``foreign_home`` / ``foreign_leased``
        of the unreferenced and of the lease-named aliases respectively, how
        many an ownership sidecar attributes to a Kiro Crew data home other
        than this process's own -- spelled exactly as the publisher records it,
        so a caller cannot pass a differently normalised id. Two homes share
        one agents directory whenever they share ``~/.kiro``, and this
        gateway's reclaim skips the other home's aliases unconditionally, so
        neither bucket drains here and the leased one is not "held or
        crash-stale" from this gateway's point of view either;
    ``unreadable_leases``
        lease records the reclaim reads as uncertainty. When one is unreadable,
        :func:`_scan_projection_leases` reports uncertainty for the pass and
        nothing is reclaimed, so a diagnostic must not promise a drain;
    ``truncated``
        1 when the alias retention bound (:data:`_CENSUS_MAX_ALIASES`) was hit,
        so the other counts are floors, or when the lease directory holds more
        entries than the reclaim scan's :data:`_PROJECTION_LEASE_SCAN_LIMIT` --
        counted over every entry, not just records -- so every prune would
        defer and no drain is promised.

    Every read failure counts toward the side that claims less: an unreadable
    directory is an empty census, an unreadable sidecar is not foreign.
    """
    counts = {
        "total": 0,
        "leased": 0,
        "foreign_home": 0,
        "foreign_leased": 0,
        "unreadable_leases": 0,
        "truncated": 0,
    }
    crew_home_id = data_home().absolute().as_posix()
    stems: set[str] = set()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not (
                    entry.name.startswith(NATIVE_SKILL_ALIAS_PREFIX)
                    and entry.name.endswith(".json")
                    and entry.is_file(follow_symlinks=False)
                ):
                    continue
                if len(stems) >= _CENSUS_MAX_ALIASES:
                    counts["truncated"] = 1
                    break
                stems.add(entry.name[: -len(".json")])
    except OSError:
        return counts
    counts["total"] = len(stems)
    if not stems:
        return counts

    named: set[str] = set()
    # Both lease directories the reclaim scans (this home's own and the legacy
    # one in the agents directory), each charged against the reclaim scan's
    # per-directory ceiling.
    for lease_dir in (_projection_lease_dir(), _legacy_lease_dir(directory)):
        lease_entries = 0
        try:
            with os.scandir(lease_dir) as entries:
                for entry in entries:
                    # Charge EVERY entry against the reclaim scan's own ceiling
                    # before any suffix filter, exactly as _scan_projection_leases
                    # does: a directory that caps that scan (records plus their
                    # ``.hold`` sidecars and any padding) makes every prune defer,
                    # so the census must report truncated there, not promise a drain.
                    if lease_entries >= _PROJECTION_LEASE_SCAN_LIMIT:
                        counts["truncated"] = 1
                        break
                    lease_entries += 1
                    if not (
                        entry.name.endswith(_PROJECTION_LEASE_RECORD_SUFFIX)
                        and entry.is_file(follow_symlinks=False)
                    ):
                        continue
                    listed = _read_lease_record(Path(entry.path))
                    if listed is None:
                        counts["unreadable_leases"] += 1
                        continue
                    # Only stems this census retained: bounded by the alias ceiling.
                    named.update(stems.intersection(listed))
        except FileNotFoundError:
            pass
        except OSError:
            counts["unreadable_leases"] += 1
    counts["leased"] = len(named)

    # Plain bounded reads, like the lease records: the hardened reader audits
    # every call, and a backlog is tens of thousands of sidecars. The first
    # records directory that has an entry for the stem answers for it.
    metadata_dirs = _metadata_dirs(directory)
    this_home_tag = _home_tag(crew_home_id)
    for stem in stems:
        metadata: Any = None
        recorded = False
        for metadata_dir in metadata_dirs:
            try:
                fd = os.open(
                    metadata_dir / f"{stem}.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
            except FileNotFoundError:
                continue
            except OSError:
                recorded = True
                break
            recorded = True
            try:
                try:
                    raw = os.read(fd, _PROJECTION_LEASE_MAX_BYTES + 1)
                finally:
                    os.close(fd)
                if len(raw) > _PROJECTION_LEASE_MAX_BYTES:
                    break
                metadata = json.loads(raw)
            except (OSError, ValueError, TypeError, RecursionError):
                pass
            break
        if (
            isinstance(metadata, dict)
            and _managed_marker(metadata)
            and isinstance(metadata.get(_MANAGED_CREW_HOME), str)
            and not _same_crew_home(metadata[_MANAGED_CREW_HOME], crew_home_id)
        ):
            counts["foreign_leased" if stem in named else "foreign_home"] += 1
        elif not recorded:
            # No record in either directory: a home that keeps its sidecars under
            # its own data home leaves none here for another home's view, and the
            # prune keeps such a view by the home tag in its description. Count it
            # the way the prune treats it.
            tag = _view_home_tag(_read_alias_spec_bounded(directory / f"{stem}.json"))
            if tag is not None and tag != this_home_tag:
                counts["foreign_leased" if stem in named else "foreign_home"] += 1
    return counts


def _read_alias_spec_bounded(path: Path) -> dict[str, Any] | None:
    """The parsed alias at *path* when it is a small regular file; ``None`` otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw = os.read(fd, _PROJECTION_LEASE_MAX_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        return None
    return _parsed_spec(raw) if len(raw) <= _PROJECTION_LEASE_MAX_BYTES else None


def census_projection_residue(
    directory: Path, churning: dict[tuple[str, str], dict[str, set[str]]] | None = None
) -> dict[str, int]:
    """Count what the projection leaves around its aliases, touching nothing.

    The companion of :func:`census_projected_aliases` for the files that are
    not aliases, read-only and bounded by the same retention ceiling. Returns:

    ``sidecars`` / ``orphan_sidecars``
        ownership sidecars in the metadata directory, and of those how many
        name an alias that is not on disk;
    ``alias_locks``
        empty ``<alias>.lock`` files beside the aliases -- never written by this
        module; an external rewriter leaves one per spec it locked;
    ``rewritten``
        aliases whose bytes differ from the digest their own sidecar recorded:
        another program is rewriting the agents directory. That is the
        precondition of both the unbounded alias growth and the "not installed"
        failure, so a non-zero count is worth seeing before a user reports it;
    ``truncated``
        1 when a walk hit :data:`_CENSUS_MAX_ALIASES`, making the rest floors.
    """
    counts = {
        "sidecars": 0,
        "orphan_sidecars": 0,
        "alias_locks": 0,
        "rewritten": 0,
        "truncated": 0,
    }
    aliases: set[str] = set()
    lock_names: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if len(aliases) + len(lock_names) >= _CENSUS_MAX_ALIASES:
                    counts["truncated"] = 1
                    break
                if not entry.name.startswith(NATIVE_SKILL_ALIAS_PREFIX):
                    continue
                if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                    aliases.add(entry.name[: -len(".json")])
                elif entry.name.endswith(_ALIAS_LOCK_SUFFIX) and entry.is_file(
                    follow_symlinks=False
                ):
                    lock_names.append(entry.name)
    except OSError:
        return counts
    # Residue by the sweeper's own rule (_sweep_orphan_alias_locks): empty, older
    # than the legacy age, and no alias beside it. A live or non-empty lock is not.
    for name in lock_names:
        info = pinned_fs.lstat_by_name(directory / name)
        if (
            info is not None
            and stat.S_ISREG(info.st_mode)
            and info.st_size == 0
            and time.time() - info.st_mtime >= _LEGACY_RECLAIM_MIN_AGE_SECS
            and name[: -len(_ALIAS_LOCK_SUFFIX)] not in aliases
        ):
            counts["alias_locks"] += 1
    # Both records directories the prune reads: this home's own and the legacy
    # one in the agents directory, under one retention bound.
    for metadata_dir in _metadata_dirs(directory):
        if counts["truncated"]:
            break
        try:
            with os.scandir(metadata_dir) as entries:
                for entry in entries:
                    if counts["sidecars"] >= _CENSUS_MAX_ALIASES:
                        counts["truncated"] = 1
                        break
                    if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                        continue
                    stem = entry.name[: -len(".json")]
                    if not _LEGACY_ALIAS_NAME_RE.fullmatch(stem):
                        # Not a sidecar: the view ledger (_VIEW_LEDGER_NAME) lives here too.
                        continue
                    counts["sidecars"] += 1
                    if stem not in aliases:
                        if not counts["truncated"]:
                            counts["orphan_sidecars"] += 1
                        continue
                    try:
                        fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                        try:
                            raw = os.read(fd, _PROJECTION_LEASE_MAX_BYTES + 1)
                        finally:
                            os.close(fd)
                        alias_fd = os.open(
                            directory / f"{stem}.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                        )
                        try:
                            alias_raw = os.read(alias_fd, _CENSUS_ALIAS_MAX_BYTES + 1)
                        finally:
                            os.close(alias_fd)
                    except OSError:
                        continue
                    record = _parsed_spec(raw) if len(raw) <= _PROJECTION_LEASE_MAX_BYTES else None
                    recorded = (
                        record.get(_MANAGED_ALIAS_SHA256)
                        if record is not None and _managed_marker(record)
                        else None
                    )
                    if len(alias_raw) > _CENSUS_ALIAS_MAX_BYTES:
                        # Not read whole, so its digest proves nothing either way.
                        continue
                    if (
                        isinstance(recorded, str)
                        and recorded != hashlib.sha256(alias_raw).hexdigest()
                    ):
                        counts["rewritten"] += 1
                    if record is not None and _managed_marker(record) and churning is not None:
                        _note_env_values(
                            churning,
                            (
                                str(record.get(_MANAGED_AGENT))[:_CHURNING_LABEL_MAX_CHARS],
                                str(record.get(_MANAGED_SOURCE))[:_CHURNING_LABEL_MAX_CHARS],
                            ),
                            _parsed_spec(alias_raw),
                        )
        except OSError:
            pass
    return counts


# The largest alias the census hashes whole; a bigger one is left unjudged.
_CENSUS_ALIAS_MAX_BYTES = 8 * 1024 * 1024


def _note_env_values(
    churning: dict[tuple[str, str], dict[str, set[str]]],
    group: tuple[str, str],
    view: dict[str, Any] | None,
) -> None:
    """Record, per agent and source, a digest of each non-volatile MCP env value."""
    servers = view.get("mcpServers") if isinstance(view, dict) else None
    if not isinstance(servers, dict):
        return
    volatile = volatile_env_keys()
    if group not in churning and len(churning) >= _CHURNING_GROUPS_MAX:
        return
    seen = churning.setdefault(group, {})
    for server, entry in servers.items():
        env = entry.get("env") if isinstance(entry, dict) else None
        if not isinstance(env, dict):
            continue
        for key, value in env.items():
            if str(key) in volatile:
                continue
            # Bounded at insertion: a label is cut to a fixed length, and a group
            # keeps a fixed number of them, so a spec with a huge env map costs a
            # bounded amount. Rendering escapes it; see the doctor line.
            label = f"{server}.{key}"[:_CHURNING_LABEL_MAX_CHARS]
            if label not in seen and len(seen) >= _CHURNING_KEYS_PER_GROUP_MAX:
                continue
            values = seen.setdefault(label, set())
            if len(values) < 2:
                values.add(hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest())


def census_churning_env_keys(directory: Path) -> list[str]:
    """``<server>.<ENV_KEY>`` names whose value differs across one agent's aliases.

    Read-only and bounded like :func:`census_projection_residue`. A key a launcher
    re-stamps on every launch but that is not in :func:`volatile_env_keys` mints a
    new alias per launch; this is how an operator finds it and adds it to
    ``KIROCREW_SKILL_VIEW_VOLATILE_ENV``. A real edit to a value shows up once too.
    """
    churning: dict[tuple[str, str], dict[str, set[str]]] = {}
    census_projection_residue(directory, churning)
    names = {key for keys in churning.values() for key, values in keys.items() if len(values) > 1}
    return sorted(names)[:_CHURNING_ENV_KEYS_MAX]


_CHURNING_ENV_KEYS_MAX = 20
_CHURNING_GROUPS_MAX = 256
_CHURNING_KEYS_PER_GROUP_MAX = 64
_CHURNING_LABEL_MAX_CHARS = 120

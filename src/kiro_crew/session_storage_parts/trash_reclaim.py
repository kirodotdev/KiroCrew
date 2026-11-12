"""Moving sessions into the trash: :func:`move_to_trash`.

An owner module of :mod:`kiro_crew.session_storage`, the one import path: callers,
tests and documents reach every name here as ``kiro_crew.session_storage.<name>``.

Under the mutation lock, the stores are re-scanned and the session index and co-tenant
maps re-read, and each selected session is staged whole or not at all: its halves,
attachments and reply threads into one batch directory with its manifest entry, and its
crew logs under the same batch id in the crew-log tree. Its search-index copy is purged
first, under its transcript locks. A session whose copy will not purge, or that is
resumed or written to since the reclaim began, is left in place and what already moved is
put back; when that cannot all go back, the move stops with an error naming the batch
that records what stayed staged.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack, nullcontext
from pathlib import Path
from typing import Any

from kiro_crew.chat_attachments import attachments_dir
from kiro_crew.config.paths import data_home
from kiro_crew.history import ConversationLog
from kiro_crew.history_index import INDEX_FILENAME, SessionSearchIndex
from kiro_crew.session_storage_parts.batch_manifest import (
    _append_entry,
    _fsync_tree,
    _levels_between,
    _sync_batch,
    _write_header,
)
from kiro_crew.session_storage_parts.batch_removal import _remove_emptied_batch
from kiro_crew.session_storage_parts.crew_staging import (
    _crew_logs_left_staged,
    _has_staged_crew,
    _put_back_crew_logs,
    _stage_crew_logs,
    _sync_batch_or_warn,
)
from kiro_crew.session_storage_parts.file_moves import (
    _batch_dir,
    _file_stamp,
    _identity_of,
    _move_file,
    _mutation_lock,
    _rollback,
    _unlisted_files,
)
from kiro_crew.session_storage_parts.store_inventory import (
    _cli_index,
    _scan_units,
    _unit_paths,
    cotenant_sids,
    invalidate_scan_cache,
    reclaim_block_reason,
)
from kiro_crew.session_storage_parts.store_layout import (
    MANIFEST_NAME,
    MIN_RECLAIM_AGE_DAYS,
    SessionIndex,
    SessionStorageError,
    TrashBatch,
    _archive_index,
    _crew_sessions_dir,
    _validate_unit_id,
    logger,
)


def _purge_search_index(stems: list[str]) -> bool:
    """Remove the search index's copy of *stems*' message text; report the verdict.

    The index keeps each session's text by transcript stem, and that copy stays readable
    on disk after the transcript goes, so a later empty-trash would report a purge it
    did not complete. Returns ``False`` rather than raising: the caller refuses and
    reports one session at a time.

    The caller holds the session's ``ConversationLog._locked`` across this call and its
    file moves; the background indexer takes the same lock, so it cannot write the row
    back in between. A missing index is not created (no copy to remove); one that
    exists but will not open is a refusal, since unprovable removal is not removal.
    """
    if not stems:
        return True
    db_path = _crew_sessions_dir() / ".index" / INDEX_FILENAME
    if not db_path.exists():
        return True
    index = SessionSearchIndex(db_path)
    try:
        if not index.available:
            return False
        return index.drop(stems)
    finally:
        index.close()


def move_to_trash(
    uids: list[str],
    *,
    reason: str,
    index: SessionIndex,
    now: float | None = None,
    refresh: Callable[[], SessionIndex] | None = None,
    activation: "tuple[AbstractContextManager[Any], Callable[[], SessionIndex]] | None" = None,
) -> TrashBatch:
    """Stage whole sessions for deletion and return the batch that can undo it.

    Every half of each session moves together — replay log, transcript and rotated
    archive segments. Leaving one behind would produce a session that lists but
    cannot resume, or resumes with no history.

    Refuses a session that is still mapped, and one touched within
    :data:`MIN_RECLAIM_AGE_DAYS`: a mapped session is resumable, and a fresh one
    may be running under a subsystem that never registered it.

    *activation* is ``(lock, live_index)``: the lock a resume holds while it maps a
    session, and a read of the index that lock guards. A session with crew logs has
    that index re-read and its crew logs staged inside that one hold, so a resume --
    including one that only reads its transcript and whose mapping has not reached the
    file *refresh* reads -- either lands first and is seen, or waits until the units
    have moved and finds the session gone.

    *refresh* re-reads the caller's index INSIDE the lock: once before anything moves,
    and again before every session the loop reaches, because a session can be resumed
    while a large store is scanned or moved. The active sets are UNIONED, never
    replaced, so a re-read can only add protection.

    It is called up to once per selected session, so it must be cheap when nothing has
    changed: return the same :class:`SessionIndex` object to say "nothing has moved"
    (one identity comparison). A fresh index per call is correct but slow.

    A resume that WRITES is caught by mtime, since each file is stat'd for the manifest
    anyway. A resume that only READS writes nothing and is caught by the re-read, as it
    is mapped. Either way the session stays in place and is named in ``revived``.

    Serialized against other mutations, because two interleaved reclaims can put
    one half of a session in each batch and leave neither able to restore it.
    """
    with _mutation_lock():
        try:
            return _move_to_trash_locked(
                uids, reason=reason, index=index, now=now, refresh=refresh, activation=activation
            )
        finally:
            # In a finally: a partial batch has already moved files, so a raised
            # refusal still leaves the cached pass stale.
            invalidate_scan_cache()


def _move_to_trash_locked(
    uids: list[str],
    *,
    reason: str,
    index: SessionIndex,
    now: float | None = None,
    refresh: Callable[[], SessionIndex] | None = None,
    activation: "tuple[AbstractContextManager[Any], Callable[[], SessionIndex]] | None" = None,
) -> TrashBatch:
    """Stage whole sessions for deletion and return the batch that can undo it.

    Every half of each session moves together — replay log, transcript and rotated
    archive segments. Leaving one behind would produce a session that lists but
    cannot resume, or resumes with no history.

    Refuses any session that is still mapped: it is one the product can resume,
    and moving its files out from under a live slot breaks it with no error a user
    would connect to this action.

    The checks run before the move loop; the loop closes the remaining window per
    session. A source file newer than an instant taken before the checks was written
    since they certified it, and a session the re-read reports as mapped is resumable
    however old its files are (a read-only resume writes nothing). Either way the whole
    session stays in place and is named in ``TrashBatch.revived``.

    Each session is recorded as it lands, so an interrupted batch stays restorable.
    """
    clock = time.time() if now is None else now
    # Anchor for the loop's revival check, taken BEFORE the scan and every check below:
    # a file written after it may postdate the read that certified it. Stamping later
    # would let a session resumed during the scan carry an older mtime and be staged.
    # No false positives: a candidate is untouched for MIN_RECLAIM_AGE_DAYS anyway.
    # Real wall-clock, not *clock*: it is compared with a kernel mtime, and ``now``
    # may be injected.
    validated_at = time.time()
    blocked_reason = reclaim_block_reason()
    if blocked_reason:
        raise SessionStorageError(blocked_reason)
    requested = [_validate_unit_id(uid) for uid in uids]
    if not requested:
        raise SessionStorageError("no sessions selected")

    by_uid = {u.uid: u for u in _scan_units(index, strict=True)}

    # The authority check runs AFTER the slow scan, so a session continued during it
    # is not judged retired. Sets are UNIONED, so a re-read only adds protection; the
    # loop re-reads per session too. *seen* lets the loop spot an unchanged view by
    # identity.
    seen: SessionIndex | None = None
    if refresh is not None:
        try:
            latest = refresh()
        except Exception:
            logger.warning("could not re-read the session index", exc_info=True)
            raise SessionStorageError(
                "could not confirm which sessions are live; nothing was moved"
            )
        seen = latest
        live_sids = index.active_sids | latest.active_sids
        live_stems = index.active_stems | latest.active_stems
    else:
        live_sids = index.active_sids
        live_stems = index.active_stems

    # Co-tenant ownership is re-read after the scan for the same reason: a co-tenant
    # that adopted an OLD replay log meanwhile is not covered by the freshness floor.
    # An instance that truly shares this store refuses the move outright, since it can
    # resume a session part-way through the loop, which no snapshot covers.
    cotenant_now, refusals = cotenant_sids()
    if refusals:
        name, why = refusals[0]
        # name!r, not plain interpolation: the directory name is agent-influenced
        # and passes no identifier gate, so a newline or ANSI payload in it would
        # forge a second record the moment a caller logs str(exc) (the
        # log-forgery class).
        raise SessionStorageError(
            f"{len(refusals)} instance(s) sharing this session store make reclaiming "
            f"unsafe ({name!r} — {why}); nothing was moved"
        )
    live_sids = live_sids | cotenant_now

    def is_live(uid: str) -> bool:
        unit = by_uid.get(uid)
        if unit is None:
            return False
        if unit.active or (unit.sid and unit.sid in live_sids):
            return True
        if any(crew in live_sids for crew in unit.crew_units):
            return True
        return any(stem in live_stems for stem in unit.stems)

    blocked = [uid for uid in requested if is_live(uid)]
    if blocked:
        raise SessionStorageError(
            f"refusing to move {len(blocked)} session(s) still in use: {blocked[0]}"
        )
    # Freshness is checked HERE, the chokepoint, so a caller passing ids directly
    # cannot take a running session that was never mapped.
    fresh = [
        uid
        for uid in requested
        if uid in by_uid and by_uid[uid].age_days(clock) < MIN_RECLAIM_AGE_DAYS
    ]
    if fresh:
        raise SessionStorageError(
            f"refusing to move {len(fresh)} session(s) touched in the last "
            f"{MIN_RECLAIM_AGE_DAYS:g} day(s): {fresh[0]}"
        )

    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(clock))
    batch_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
    target = _batch_dir(batch_id)
    target.mkdir(parents=True, exist_ok=False)
    # Recorded HERE, at creation and under the mutation lock, because it is the strongest
    # anchor this path will ever have: the directory was made by this call, so nothing has
    # had a window to substitute anything for it yet. The cleanup below removes this batch
    # by descriptor and compares that descriptor against these numbers, so a swapped
    # ancestor reaching a different directory - however convincingly it is dressed up - is
    # refused rather than emptied.
    created_identity = _identity_of(target)
    # The batch entry must be ON DISK before files are renamed into it, or a crash with
    # the mkdir still cached loses the directory and the files with it. Every level
    # from the data home is synced, since the first batch also creates `trash/` and
    # `trash/sessions/`; relative_to raises rather than climb past the data home.
    _fsync_tree(data_home(), target.parent)
    archives = _archive_index()

    moved_bytes = 0
    moved_sessions = 0
    revived: list[str] = []
    # Sessions left in place because the search index kept its copy of their text;
    # reported like ``revived``, since doing less than asked silently is a defect.
    refused_index: list[str] = []
    refresh_failed = False
    staged_dirs: set[Path] = set()
    source_dirs: set[Path] = set()
    cli_files = _cli_index()
    with (target / MANIFEST_NAME).open("w", encoding="utf-8") as manifest:
        _write_header(manifest, batch_id, clock, reason)
        # The indexer serializes on ConversationLog's per-session lock, so the
        # reclaim borrows the same lock rather than inventing a second one.
        log = ConversationLog(base_dir=_crew_sessions_dir())
        for uid in requested:
            unit = by_uid.get(uid)
            if unit is None:
                continue
            if refresh is not None:
                # Re-read before EVERY unit: the mtime check sees writes only, and a
                # resume that merely READS an old transcript shows up only in the
                # index. Cheap, because an unchanged view is the same object.
                try:
                    latest = refresh()
                except Exception:
                    # Degraded, not fatal: a re-read only WIDENS the live sets, so
                    # losing one keeps the pre-loop guard, and abandoning a batch that
                    # already moved files is worse. A refresh broken from the start
                    # still fails closed in the pre-loop call.
                    if not refresh_failed:
                        refresh_failed = True
                        logger.warning(
                            "could not re-read the session index while staging; "
                            "continuing with the last view read",
                            exc_info=True,
                        )
                else:
                    if latest is not seen:
                        seen = latest
                        live_sids = live_sids | latest.active_sids
                        live_stems = live_stems | latest.active_stems
            if is_live(uid):
                # Mapped since the checks; nothing has moved yet, so it is simply
                # not taken and reported as revived.
                revived.append(uid)
                continue
            # Purge and move under the per-session lock the indexer also takes, so it
            # cannot re-index a still-present transcript between them. Sorted order
            # keeps two multi-stem reclaims from deadlocking.
            with ExitStack() as unit_locks:
                for stem in sorted(unit.stems):
                    unit_locks.enter_context(log._locked(stem))
                if not _purge_search_index(list(unit.stems)):
                    logger.warning(
                        "could not remove the search index copy for %r; not moving it", uid
                    )
                    refused_index.append(uid)
                    continue
                # Crew logs go FIRST: a held unit refuses the rename (session in use)
                # before anything else has moved. Every rollback below returns them.
                # Under *activation* the last liveness read and the staging share one
                # hold of the lock a resume maps under.
                resumed = False
                crew_ok, crew_bytes = True, 0
                hold = activation[0] if activation and unit.crew_units else nullcontext()
                with hold:
                    if activation and unit.crew_units:
                        try:
                            now_index = activation[1]()
                        except Exception:
                            logger.warning(
                                "could not read the live session index for %r; not moving it",
                                uid,
                                exc_info=True,
                            )
                            resumed = True
                        else:
                            live_sids = live_sids | now_index.active_sids
                            live_stems = live_stems | now_index.active_stems
                            resumed = is_live(uid)
                    if not resumed:
                        crew_ok, crew_bytes = _stage_crew_logs(batch_id, uid, unit.crew_units)
                if resumed:
                    revived.append(uid)
                    continue
                if not crew_ok:
                    if not _put_back_crew_logs(manifest, batch_id, uid):
                        _sync_batch_or_warn(target, staged_dirs, manifest, source_dirs)
                        raise _crew_logs_left_staged(uid, target)
                    revived.append(uid)
                    continue
                files: list[dict[str, Any]] = []
                done: list[tuple[Path, Path]] = []
                failed = False
                woke = False
                try:
                    unit_files = _unit_paths(unit.sid, unit.stems, archives, cli_files)
                except OSError:
                    # A half would not enumerate; taking the rest would split the
                    # session, so it is left whole.
                    logger.warning("could not enumerate session %r for staging", uid, exc_info=True)
                    unit_files = []
                    failed = True
                for src, rel in unit_files:
                    try:
                        size, mtime = _file_stamp(src)
                    except OSError:
                        # Unsized means unrecorded, and restore cannot put back an
                        # unrecorded file; skipping it would split the session.
                        logger.warning("could not stat %s for staging", src, exc_info=True)
                        failed = True
                        break
                    if mtime > validated_at:
                        # Written since it was certified idle, so it was resumed during
                        # the move (the stat is free: the manifest needs it). Leave the
                        # whole session; staging half would leave the live half behind.
                        woke = True
                        break
                    dst = target / rel
                    if dst.parent not in staged_dirs:
                        try:
                            dst.parent.mkdir(parents=True, exist_ok=True)
                        except OSError:
                            # Crew logs are already staged; an escape would leave them
                            # with no entry, so roll back like a file that would not move.
                            logger.warning(
                                "could not create the staging directory %s",
                                dst.parent,
                                exc_info=True,
                            )
                            failed = True
                            break
                        # Sync the new directory chain before anything is renamed into
                        # it: if the rename reached disk without the mkdir, the file
                        # would have no reachable name. Once per directory, not per
                        # file; :func:`_sync_batch` forces the renamed entries later.
                        try:
                            _fsync_tree(target, dst.parent)
                        except OSError:
                            # Treated like a file that would not move: roll this session
                            # back and keep the rest of the batch.
                            logger.warning(
                                "could not sync the staging directory %s", dst.parent, exc_info=True
                            )
                            failed = True
                            break
                        # Recorded only once durable, so a later session retries the sync.
                        staged_dirs.update(_levels_between(target, dst.parent))
                    try:
                        _move_file(src, dst)
                    except OSError:
                        logger.warning("could not move %s into the trash", src, exc_info=True)
                        failed = True
                        break
                    # Synced once at the end, not per file (see :func:`_sync_batch`).
                    source_dirs.add(src.parent)
                    done.append((dst, src))
                    files.append({"rel": rel, "origin": str(src), "bytes": size})
            if woke:
                # Put back what moved, so the session stays resumable.
                if not _rollback(done):
                    # Most likely the resume recreated an origin, and put-back never
                    # overwrites. First, record what is still staged: restore and
                    # empty see only the manifest, and restore also never overwrites,
                    # so it will not clobber the live session with this stale copy.
                    stranded = [
                        record for record, (landed, _origin) in zip(files, done) if landed.exists()
                    ]
                    if stranded:
                        try:
                            _append_entry(manifest, {"uid": uid, "files": stranded})
                        except OSError:
                            logger.warning(
                                "could not record the stranded half of %r; it is in %s",
                                uid,
                                target,
                                exc_info=True,
                            )
                    # Second, raise rather than report a revival ("left in place" would
                    # be false), naming the batch. Sync first so "can be restored" is
                    # true; a sync failure is only logged, so the OSError does not
                    # replace the error that points at the fragment.
                    _put_back_crew_logs(manifest, batch_id, uid)
                    try:
                        _sync_batch(target, staged_dirs, manifest, source_dirs)
                    except OSError:
                        logger.warning(
                            "could not sync %s while recording the stranded half of %r; "
                            "the fragment is on disk but may not survive a power loss",
                            target,
                            uid,
                            exc_info=True,
                        )
                    raise SessionStorageError(
                        f"session {uid!r} was resumed while being staged and could "
                        f"not be fully put back; what is still staged is recorded in "
                        f"{target} and can be restored"
                    )
                if not _put_back_crew_logs(manifest, batch_id, uid):
                    _sync_batch_or_warn(target, staged_dirs, manifest, source_dirs)
                    raise _crew_logs_left_staged(uid, target)
                revived.append(uid)
                continue
            if failed:
                # A partly moved session is a hidden split that emptying would destroy
                # half of; put back what moved and leave it alone.
                _rollback(done)
                if not _put_back_crew_logs(manifest, batch_id, uid):
                    _sync_batch_or_warn(target, staged_dirs, manifest, source_dirs)
                    raise _crew_logs_left_staged(uid, target)
                continue
            if files:
                # Moved but unrecorded files can be neither restored nor resumed, so
                # rewind the partial line and put the files back.
                mark = manifest.tell()
                try:
                    _append_entry(manifest, {"uid": uid, "files": files})
                except OSError:
                    logger.warning("could not record session %r in the manifest; rolling back", uid)
                    try:
                        manifest.truncate(mark)
                        manifest.seek(mark)
                    except OSError:
                        logger.warning("could not rewind the manifest", exc_info=True)
                    _rollback(done)
                    if not _put_back_crew_logs(manifest, batch_id, uid):
                        _sync_batch_or_warn(target, staged_dirs, manifest, source_dirs)
                        raise _crew_logs_left_staged(uid, target)
                    continue
                moved_sessions += 1
                moved_bytes += sum(int(record["bytes"]) for record in files) + crew_bytes
                # Remove the drained, empty attachments dirs (restore recreates them)
                # under the transcript locks: a resumed writer creates the dir and its
                # first image under that lock, so an unlocked ``rmdir`` could cost it
                # the image. ``rmdir`` refuses a non-empty dir. Best effort.
                try:
                    with ExitStack() as shell_locks:
                        for stem in sorted(unit.stems):
                            shell_locks.enter_context(log._locked(stem))
                        for stem in unit.stems:
                            try:
                                os.rmdir(attachments_dir(_crew_sessions_dir(), stem))
                            except OSError:
                                pass
                except Exception:
                    logger.debug("could not take the lock to remove an empty attachments dir")
            elif _has_staged_crew(batch_id, uid):
                # A session made only of crew logs still needs its manifest entry: it
                # is how restore finds the uid whose units to put back.
                try:
                    _append_entry(manifest, {"uid": uid, "files": []})
                except OSError:
                    logger.warning("could not record session %r; putting it back", uid)
                    if not _put_back_crew_logs(manifest, batch_id, uid):
                        _sync_batch_or_warn(target, staged_dirs, manifest, source_dirs)
                        raise _crew_logs_left_staged(uid, target)
                    continue
                moved_sessions += 1
                moved_bytes += crew_bytes
        # Inside the handle's scope, so the manifest can still be synced through the
        # descriptor that wrote it. Closing only flushes to the OS.
        _sync_batch(target, staged_dirs, manifest, source_dirs)

    if revived:
        # Reported, not just skipped: the endpoint's own rule is that doing less
        # than the user asked without saying so is a defect. uid!r because a unit
        # id reaching here has passed _validate_unit_id, but the log line is read
        # by people who also read agent-influenced names, so keep it quoted.
        logger.warning(
            "left %d session(s) in place: resumed while staging (first: %r)",
            len(revived),
            revived[0],
        )

    if refused_index:
        logger.warning(
            "left %d session(s) in place: the search index would not release its copy "
            "of their text (first: %r)",
            len(refused_index),
            refused_index[0],
        )

    if not moved_sessions:
        # Leave no empty batch behind — but a rollback that itself failed can have
        # left staged files here, and those are the only copy.
        if _unlisted_files(target):
            raise SessionStorageError(
                "no sessions were staged, and some files could not be put back; " f"see {target}"
            )
        _remove_emptied_batch(target, "the empty batch", expect=created_identity)
        if revived:
            # Not "not found": every session is still in place and resumable.
            raise SessionStorageError(
                f"all {len(revived)} selected session(s) were resumed while being "
                "staged; nothing was moved"
            )
        if refused_index:
            raise SessionStorageError(
                f"the search index would not release its copy of {len(refused_index)} "
                "selected session(s)' text; nothing was moved"
            )
        raise SessionStorageError("none of the selected sessions were found on disk")

    return TrashBatch(
        batch_id=batch_id,
        created_at=clock,
        reason=reason,
        sessions=moved_sessions,
        bytes=moved_bytes,
        revived=tuple(revived),
    )

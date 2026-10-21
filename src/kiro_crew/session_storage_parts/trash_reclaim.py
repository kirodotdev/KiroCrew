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

    The index stores each indexed session's message text, keyed by transcript stem.
    A session leaving live storage must not leave that copy behind: search cannot
    reach it once the transcript is gone, but it stays READABLE on disk, so a later
    empty-trash would report a purge it did not complete.

    Returns ``False`` rather than raising, because the caller refuses ONE session at
    a time and reports it: a session whose copy cannot be removed is left in place,
    exactly where the caller left it, and named in the warning the move logs.

    The caller holds that session's ``ConversationLog._locked`` across this call AND
    its file moves. That is what makes the removal stick: the background indexer
    takes the same per-session lock, so it cannot re-read a still-present transcript
    and write the row back in between.

    An index that does not exist is not created here -- a reclaim must not bring one
    into being, and absent means there is no copy to remove. An index that exists but
    cannot be opened is a refusal: the copy may be in it and nothing here can remove
    it, so unprovable removal is not removal.
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

    *refresh* re-reads the caller's index INSIDE the lock: once immediately before
    anything moves, and again before every session the loop reaches. Scanning a
    large store takes long enough that a session can be resumed and mapped while
    the selection is being computed, and the move loop after that is not instant
    either. The active sets are UNIONED, never replaced, so a re-read can only ever
    add protection.

    Called up to once per selected session, so it must be CHEAP when nothing has
    changed. Return the same :class:`SessionIndex` object as last time to say
    "nothing has moved" — an identical object is recognised by identity and costs
    one comparison. Returning a freshly built index every call is correct but pays
    for a full re-read per session, which at a six-figure selection is minutes.

    Two shapes of mid-staging resume are caught, and both are needed. A resume
    that WRITES is caught by mtime: each file is already stat'd for the manifest,
    so a source modified since the reclaim began fails the check before it moves.
    A resume that only READS an old transcript writes nothing, so it is invisible
    to mtime and is caught by the re-read instead — it is mapped. Either way the
    session is left in place and named in the returned batch's ``revived``: it was
    asked for and not taken.

    Serialized against other mutations, because two interleaved reclaims can put
    one half of a session in each batch and leave neither able to restore it.
    """
    with _mutation_lock():
        try:
            return _move_to_trash_locked(
                uids, reason=reason, index=index, now=now, refresh=refresh, activation=activation
            )
        finally:
            # In a finally, not after a success: a partially-completed batch has
            # already moved files, so a raised refusal still leaves any cached
            # pass describing sessions that are not where it says any more.
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

    Every such check runs before the move loop, so each describes the instant it
    ran. The loop closes the remaining window itself, with two per-session signals.
    A source file whose mtime is newer than an instant taken before any of those
    checks has been written to since the reads that certified it, which an idle
    session's file cannot be. And a session the re-read now reports as mapped is
    one the product can resume, however old its files are — the case mtime cannot
    see, because a resume that only reads a transcript writes nothing. Either way
    the whole session is left in place and named in ``TrashBatch.revived``.

    Each session is recorded as it lands, so an interruption leaves a manifest
    describing exactly what moved — a partial batch stays restorable instead of
    becoming orphaned files nothing points at.
    """
    clock = time.time() if now is None else now
    # Anchor for the move loop's revival check, taken BEFORE the scan and before
    # every authority check below — not after them. Everything this function is
    # about to reason over is read after this instant, so a source file written
    # later than it may have been written after the read that certified it, and
    # the loop must not trust that file. Stamping after the checks would leave
    # exactly that gap: a session resumed during the scan, or between the index
    # re-read and the loop, would carry an mtime older than the stamp and be
    # staged.
    #
    # Taking it this early costs nothing in false positives. A candidate has to be
    # untouched for MIN_RECLAIM_AGE_DAYS to qualify at all, so a legitimate one's
    # mtime is days older than this stamp however early it is taken.
    #
    # Deliberately real wall-clock rather than *clock*: a caller may inject
    # ``now`` (tests do), and this is compared against an mtime the kernel writes,
    # so it has to measure elapsed reality.
    validated_at = time.time()
    blocked_reason = reclaim_block_reason()
    if blocked_reason:
        raise SessionStorageError(blocked_reason)
    requested = [_validate_unit_id(uid) for uid in uids]
    if not requested:
        raise SessionStorageError("no sessions selected")

    by_uid = {u.uid: u for u in _scan_units(index, strict=True)}

    # The authority check runs AFTER the scan, not before it. Enumerating a
    # six-figure store is the slow part of this function, so an index read before
    # it is already stale by the time anything moves — a session continued in that
    # interval would look retired. Re-reading here is what the checks below get to
    # judge against, and the sets are UNIONED so a re-read can only ever add
    # protection. It is not the last word: the move loop re-reads per session, for
    # the stretch this one cannot describe.
    #
    # *seen* keeps the index this returned, so the loop can tell an unchanged view
    # apart from a new one by identity and skip re-deriving the sets for it.
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

    # Co-tenant ownership is re-read here for exactly the reason *refresh* exists,
    # and must not be left out of it: the scan above is the slow part, so the
    # co-tenant view taken during it is seconds stale by the time anything moves.
    # Refreshing only the local map would leave a co-tenant that adopted a
    # PRE-EXISTING replay log in that window unprotected — and the freshness floor
    # does not cover that case, because the session it adopted is old.
    #
    # An instance that genuinely shares this store refuses the move outright rather
    # than being handled per session: it can seed and resume a session at any
    # moment, including part-way through the loop below, which a snapshot taken
    # here cannot cover however fresh it is.
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
    # Freshness is checked HERE, not only in the selection helper, because this is
    # the chokepoint: a caller passing ids directly would otherwise bypass the
    # floor and take a session that is running right now but was never mapped.
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
    # And the entry itself has to exist ON DISK before anything is moved in: the batch
    # directory is about to become the only home of these files, so a crash with the
    # mkdir still in the page cache would come back to no batch directory at all — and
    # the files were renamed INTO it, so they would be gone with it.
    #
    # Every level down from the data home, not just target.parent: on the FIRST batch
    # this machine ever stages, parents=True creates `trash/` and `trash/sessions/`
    # too, and an unsynced `trash/sessions` entry takes the batch inside it. Bounded
    # by relative_to, which raises rather than climbing past the data home if the
    # trash root is ever moved out from under it (_batch_dir already refuses that).
    _fsync_tree(data_home(), target.parent)
    archives = _archive_index()

    moved_bytes = 0
    moved_sessions = 0
    revived: list[str] = []
    # Sessions left in place because the search index would not give up its copy
    # of their text. Reported like ``revived``: doing less than the caller asked
    # without saying so is a defect, and this refusal protects deleted content.
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
                # Re-read before EVERY unit, not once before the loop. The mtime
                # check below is the only other per-unit signal and it sees writes
                # only, so a resume that merely READS an old transcript to rebuild
                # history — recording the turn that follows under a newly mapped
                # sid — leaves every mtime days old and slips past it. The
                # index is where that resume IS visible, so it has to be consulted
                # at the same cadence.
                #
                # Affordable because an unchanged view costs one identity
                # comparison: *refresh* may return the same object it returned last
                # time to say "nothing has moved", and the dashboard's does exactly
                # that behind a stat of the session map. A caller that returns a
                # fresh index every time is not wrong, only slower — the sets are
                # re-derived and the answer is identical.
                try:
                    latest = refresh()
                except Exception:
                    # Degraded, not fatal. Every re-read only ever WIDENS the live
                    # sets, so losing one leaves the guard exactly as strong as the
                    # pre-loop read already made it — it cannot make a session
                    # reclaimable that the checks before this loop protected. A
                    # batch part-way through has already moved files, and
                    # abandoning it over the loss of an additive signal would be
                    # the worse trade. A refresh broken from the start still fails
                    # closed: the pre-loop call above raises.
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
                # Mapped since the checks that certified it. Nothing of this
                # session has moved yet, so there is nothing to roll back — it is
                # simply not taken, and reported the same way a write-shaped
                # revival is.
                revived.append(uid)
                continue
            # The index's copy of this session's text goes under the SAME lock that
            # holds its files, and stays held across the move. A purge that merely
            # preceded the move left a window: the background indexer re-reads a
            # transcript that is still in place, so its write lands after the purge
            # and the text is back in the index once the files are gone. The
            # indexer takes this same per-session lock, so holding it here is what
            # excludes it. Locks are taken in sorted order so two multi-stem
            # reclaims cannot deadlock against each other.
            with ExitStack() as unit_locks:
                for stem in sorted(unit.stems):
                    unit_locks.enter_context(log._locked(stem))
                if not _purge_search_index(list(unit.stems)):
                    logger.warning(
                        "could not remove the search index copy for %r; not moving it", uid
                    )
                    refused_index.append(uid)
                    continue
                # The crew logs go FIRST: renaming a unit is refused while a writer
                # holds it, which means the session is in use, and nothing else of the
                # session has moved yet to put back. Every rollback below returns them.
                # Under *activation* the final liveness read and the staging are one
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
                    # A half this session owns could not be enumerated (an
                    # attachments directory that exists but will not list). Taking
                    # the halves that did enumerate is the split this loop's
                    # rollback exists to prevent, so the session is left whole.
                    logger.warning("could not enumerate session %r for staging", uid, exc_info=True)
                    unit_files = []
                    failed = True
                for src, rel in unit_files:
                    try:
                        size, mtime = _file_stamp(src)
                    except OSError:
                        # A file that cannot be sized cannot be recorded, and a file
                        # the manifest does not record is one restore cannot put back.
                        # Skipping it here while moving the rest is precisely the split
                        # this loop's rollback exists to prevent, so it is a failure.
                        logger.warning("could not stat %s for staging", src, exc_info=True)
                        failed = True
                        break
                    if mtime > validated_at:
                        # Every authority check ran before the loop, and moving a
                        # six-figure store is not instant, so a session can be resumed
                        # between being certified retired and being reached here. Its
                        # replay log is then written to, and this is the one signal of
                        # that which costs nothing: the stat is already being taken for
                        # the manifest.
                        #
                        # A candidate qualified by being untouched for
                        # MIN_RECLAIM_AGE_DAYS, so an mtime newer than the instant we
                        # certified it cannot be the same idle file — something has it
                        # open. Leave the whole session alone rather than any part of
                        # it: staging half is the split the rollback below exists to
                        # prevent, and the half left behind would be the live half.
                        woke = True
                        break
                    dst = target / rel
                    if dst.parent not in staged_dirs:
                        try:
                            dst.parent.mkdir(parents=True, exist_ok=True)
                        except OSError:
                            # The crew logs are already staged, so an escape here would
                            # leave them in the trash with no entry that restores them.
                            # A failure instead, rolled back like a file that would not move.
                            logger.warning(
                                "could not create the staging directory %s",
                                dst.parent,
                                exc_info=True,
                            )
                            failed = True
                            break
                        # Before anything moves INTO it, not with the batch at the end.
                        # The same-filesystem move is a rename, which removes the file's
                        # only other name in the same atomic step that creates this one:
                        # if the rename reaches disk while the mkdir that created its
                        # parent does not, the file has no reachable name left. Syncing
                        # the chain here orders the two — the directory that will hold
                        # the file is durable before the source entry can be given up.
                        #
                        # Cheap in the way the per-file case would not be: once per new
                        # directory, not once per file, so the hot path stays a bare
                        # rename (see :func:`_sync_batch` for the end-of-batch sync that
                        # forces the entries those renames then add).
                        try:
                            _fsync_tree(target, dst.parent)
                        except OSError:
                            # Same treatment as a file that would not move: nothing of
                            # this session has entered this directory yet, so rolling it
                            # back and leaving the rest of the batch to be recorded and
                            # synced properly is strictly better than abandoning a batch
                            # whose manifest has not been forced out.
                            logger.warning(
                                "could not sync the staging directory %s", dst.parent, exc_info=True
                            )
                            failed = True
                            break
                        # Recorded only once the chain is durable. On the failure above
                        # nothing was staged under it, so there is nothing for the
                        # end-of-batch sync to force; a later session needing the same
                        # directory re-runs the mkdir and retries the sync.
                        staged_dirs.update(_levels_between(target, dst.parent))
                    try:
                        _move_file(src, dst)
                    except OSError:
                        logger.warning("could not move %s into the trash", src, exc_info=True)
                        failed = True
                        break
                    # The live-store directory this file just left. Synced once at the end
                    # rather than per file, because the same-filesystem path is a bare
                    # rename and this is the hot path (see :func:`_sync_batch`).
                    source_dirs.add(src.parent)
                    done.append((dst, src))
                    files.append({"rel": rel, "origin": str(src), "bytes": size})
            if woke:
                # Put back whatever already moved for this session: the point of
                # leaving it alone is that it stays resumable, and half a session
                # is not.
                if not _rollback(done):
                    # The rollback could not finish — most plausibly because the
                    # resume that triggered this recreated an origin, and putting a
                    # file back is deliberately non-overwriting. Two things then
                    # have to be true before this returns.
                    #
                    # First, whatever is still staged must be IN the manifest.
                    # Unlisted files in a batch are reachable by nothing: restore
                    # enumerates the manifest, and so does empty. Recording them is
                    # safe because restore moves exclusively too, so a later restore
                    # declines the origin the resume recreated rather than
                    # overwriting the live session with this stale copy.
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
                    # Second, this must not be reported as a revival. "Left in
                    # place" would be false, so raise instead — matching how this
                    # module already treats a file it could not put back — and name
                    # the batch so the fragment can be found.
                    #
                    # The message promises the fragment "can be restored", and that
                    # is only true of a manifest that reached disk: sync before
                    # raising, because nothing after this point will.
                    #
                    # A sync failure here is logged rather than raised, like every
                    # other sync in this module that runs just before a raise
                    # (:func:`_sync_batch_or_warn`). This path is ALREADY failing,
                    # with an error that names which session was resumed and
                    # where its fragment is; replacing that with an OSError would cost
                    # the operator the only pointer to the fragment in exchange for
                    # information the log line already carries.
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
                # A session that moved only partly is the broken state this module
                # exists to prevent, and it would be invisible: the manifest would
                # list the staged half while the rest stayed in place, so emptying
                # the trash would destroy one half of a session nobody knew was
                # split. Put back whatever moved and leave the session alone.
                _rollback(done)
                if not _put_back_crew_logs(manifest, batch_id, uid):
                    _sync_batch_or_warn(target, staged_dirs, manifest, source_dirs)
                    raise _crew_logs_left_staged(uid, target)
                continue
            if files:
                # The manifest is what makes a move reversible, so a session that
                # moved but could not be recorded is worse than one that never
                # moved: its files are gone from live storage and no entry names
                # them, so they can neither be restored nor resumed. Rewind the
                # partial line and put the files back.
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
                # The attachments directories this unit drained are now empty
                # shells; a restore recreates one on demand. Removed under the
                # unit's transcript locks, re-taken for this step: a writer that
                # resumes this session creates the directory and lands its first
                # image under that same lock, so an unlocked ``rmdir`` could take
                # the directory between its ``mkdir`` and the file arriving and
                # cost that row its image. ``rmdir`` refuses a non-empty
                # directory, so anything this code did not stage (and so did not
                # move), or a resumed writer's image that landed while the lock
                # was free, keeps its home. Best-effort throughout: an empty shell
                # left behind is a cosmetic leftover, not a broken session.
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
            # A distinct message from "not found": every selected session is still
            # exactly where the caller left it, and it is still resumable. Saying
            # "none were found" here would send someone looking for lost files.
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

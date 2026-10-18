"""Putting a batch's sessions back where they came from: :func:`restore`.

An owner module of :mod:`kiro_crew.session_storage`, the one import path: callers,
tests and documents reach every name here as ``kiro_crew.session_storage.<name>``.

Under the mutation lock, each listed session's files go back to the origin derived from
their staged path, a session that cannot go back whole is rolled back, the manifest is
rewritten to what stays staged, and a batch with no session left is removed, unless
anything but its manifest remains in it or it cannot be shown to be the batch the restore
read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.history import (
    THREADS_DIR_NAME,
    THREADS_SIDECAR_SUFFIX,
    ConversationLog,
    HistoryLockTimeout,
    transcript_lock_stems,
)
from kiro_crew.session_storage_parts.batch_manifest import _read_manifest, _rewrite_manifest
from kiro_crew.session_storage_parts.batch_removal import _discard_restored_batch
from kiro_crew.session_storage_parts.crew_staging import (
    _has_staged_crew,
    _release_crew_holds,
    _release_restored_holds,
    _restore_crew_logs,
    _return_crew_logs,
)
from kiro_crew.session_storage_parts.file_moves import (
    _batch_dir,
    _canonical_origin,
    _identity_of,
    _move_file_exclusive,
    _move_sidecar_pinned,
    _mutation_lock,
    _origin_path,
    _rollback,
    _staged_path,
)
from kiro_crew.session_storage_parts.store_inventory import invalidate_scan_cache
from kiro_crew.session_storage_parts.store_layout import (
    _TRANSCRIPT_SUFFIX,
    SessionStorageError,
    _crew_sessions_dir,
    _validate_unit_id,
    logger,
)


def restore(batch_id: str, uids: list[str] | None = None) -> int:
    """Put staged sessions back where they came from; return sessions restored.

    Serialized against other mutations so a concurrent reclaim cannot move a
    session's other half while this one is putting it back.
    """
    with _mutation_lock():
        try:
            return _restore_locked(batch_id, uids)
        finally:
            invalidate_scan_cache()


def _restore_locked(batch_id: str, uids: list[str] | None = None) -> int:
    """Put staged sessions back where they came from; return sessions restored.

    *uids* restores only those sessions, which is what makes a large
    policy-driven batch usable: a user remembers the one conversation they want,
    not the batch it happened to land in.

    A session is restored only when EVERY one of its files can go back. Restoring
    part of one would recreate the half-session this module exists to avoid, so a
    blocked file leaves the whole session staged.
    """
    batch = _batch_dir(batch_id)
    # BEFORE the first read of anything inside the batch, which is the point. This identity is
    # what the cleanup at the bottom binds its removal to, and every check between here and
    # there is by path: an ancestor swapped at ANY point during the restore is caught, because
    # the pinned root the cleanup opens will not be the inode recorded here. Captured later -
    # after the manifest read, or after the files have moved - the swap that landed in between
    # would be recorded as though it were the batch, and the cleanup would then verify
    # consistently against a directory the caller was never looking at.
    identity = _identity_of(batch)
    parsed = _read_manifest(batch)
    if parsed is None:
        raise SessionStorageError(f"no restorable batch {batch_id!r}")
    header, entries = parsed
    wanted = None if uids is None else {_validate_unit_id(uid) for uid in uids}

    remaining: list[dict[str, Any]] = []
    restored = 0
    for entry in entries:
        uid = str(entry.get("uid") or "")
        if wanted is not None and uid not in wanted:
            remaining.append(entry)
            continue
        files = entry.get("files")
        files = files if isinstance(files, list) else []
        planned: list[tuple[Path, Path]] = []
        blocked = False
        for record in files:
            if not isinstance(record, dict):
                # A record we cannot read is a file we cannot place. Restoring the
                # rest would leave the session split with that file staged and
                # unreferenced, so the whole session stays put.
                logger.warning("manifest record for %r is not an object", uid)
                blocked = True
                break
            rel = str(record.get("rel") or "")
            src = _staged_path(batch, rel)
            recorded = _origin_path(str(record.get("origin") or ""))
            origin = _canonical_origin(rel)
            if src is None or origin is None or recorded is None:
                logger.warning("refusing a manifest record outside the session stores")
                blocked = True
                break
            if recorded.resolve() != origin.resolve():
                # Both are inside a session store, so neither check alone catches
                # this: the recorded origin names a DIFFERENT session's file.
                logger.warning(
                    "refusing a manifest record whose origin does not match its " "staged path"
                )
                blocked = True
                break
            # An occupied origin means the session came back on its own; the
            # occupant is newer, and undoing a deletion must not cause one.
            # is_file() FOLLOWS links, so a staged symlink would pass as a file and
            # restore would put the link back where the session's data belongs.
            if platform_compat.is_link_or_junction(src) or not src.is_file() or origin.exists():
                blocked = True
                break
            planned.append((src, origin))
        if not blocked and not files and _has_staged_crew(batch_id, uid):
            crew_only = _restore_crew_logs(batch_id, uid)
            if crew_only is None:
                remaining.append(entry)
            else:
                _release_restored_holds(crew_only)
                _release_crew_holds(batch_id, uid)
                restored += 1
            continue
        if blocked or not planned:
            remaining.append(entry)
            continue
        # The crew logs go back FIRST and are staged again if anything below fails,
        # so a session is restored with all of its halves or left staged with all.
        crew_back = _restore_crew_logs(batch_id, uid)
        if crew_back is None:
            remaining.append(entry)
            continue
        crew_sessions = _crew_sessions_dir()
        threads_dir = crew_sessions / THREADS_DIR_NAME
        live_transcripts: list[tuple[Path, Path]] = []
        prelock_files: list[tuple[Path, Path]] = []
        for item in planned:
            _src, origin = item
            if origin.parent == crew_sessions and origin.suffix == _TRANSCRIPT_SUFFIX:
                live_transcripts.append(item)
            elif origin.parent == threads_dir and origin.suffix == THREADS_SIDECAR_SUFFIX:
                # The reply-thread sidecar is written under the transcript's lock
                # (``append_thread_reply``), so it is published and rolled back
                # under that same lock, beside the transcript: a reply a recreated
                # chat commits between publish and a lost-race rollback would
                # otherwise ride the sidecar back to trash.
                live_transcripts.append(item)
            else:
                prelock_files.append(item)
        if any(
            platform_compat.is_link_or_junction(origin.parent)
            for _src, origin in live_transcripts
            if origin.parent == threads_dir and origin.parent.exists()
        ):
            # A link where the sidecar directory should be would carry the
            # restore outside the session store; leave the batch staged.
            _return_crew_logs(batch_id, uid, crew_back)
            remaining.append(entry)
            continue
        done_prelock: list[tuple[Path, Path]] = []
        done_transcripts: list[tuple[Path, Path]] = []
        try:
            lost_race = False
            # Replay logs and archive segments can require unbounded
            # cross-filesystem copies. Restore them first, with no history lock
            # held; every failure rolls them back before the live transcript is
            # published.
            for src, origin in prelock_files:
                origin.parent.mkdir(parents=True, exist_ok=True)
                if not _move_file_exclusive(src, origin):
                    logger.warning(
                        "session %r was recreated while being restored; leaving it staged",
                        uid,
                    )
                    lost_race = True
                    break
                done_prelock.append((origin, src))
            if lost_race:
                _rollback(done_prelock)
                _return_crew_logs(batch_id, uid, crew_back)
                remaining.append(entry)
                continue

            transcript_keys: set[str] = set()
            for _src, origin in live_transcripts:
                # A sidecar's stem IS its transcript's stem, so it takes the
                # same lock aliases.
                transcript_keys.update(transcript_lock_stems(origin.stem))
            transcript_log = ConversationLog(base_dir=crew_sessions)
            with transcript_log.locked_stems(transcript_keys):
                # The original preflight ran before replay restoration and lock
                # acquisition. Recheck every physical alias of each live
                # transcript here; prelock origins now exist because this
                # transaction restored them and are tracked separately for
                # rollback. A sidecar takes the same recheck on ITS stem: its
                # replies belong to the transcript that was deleted with it, so a
                # transcript recreated under that stem since is a different chat,
                # and the old thread must not be attached to it.
                if any(
                    (crew_sessions / f"{stem}{_TRANSCRIPT_SUFFIX}").exists()
                    for _src, origin in live_transcripts
                    for stem in transcript_lock_stems(origin.stem)
                ):
                    lost_race = True
                else:
                    try:
                        for src, origin in live_transcripts:
                            origin.parent.mkdir(parents=True, exist_ok=True)
                            if origin.parent == threads_dir:
                                # The sidecar is published relative to the pinned
                                # `.threads` descriptor: a link swapped in for the
                                # directory after preflight is refused by the open
                                # (O_NOFOLLOW) and cannot redirect the publish.
                                moved = _move_sidecar_pinned(src, origin)
                            else:
                                moved = _move_file_exclusive(src, origin)
                            if not moved:
                                logger.warning(
                                    "session %r was recreated while being restored; "
                                    "leaving it staged",
                                    uid,
                                )
                                lost_race = True
                                break
                            done_transcripts.append((origin, src))
                    except OSError:
                        # Put any published live transcript back while its alias
                        # locks are still held. Large replay files roll back only
                        # after this scope exits.
                        _rollback(done_transcripts)
                        done_transcripts.clear()
                        raise
                if lost_race:
                    _rollback(done_transcripts)
                    done_transcripts.clear()
            if lost_race:
                _rollback(done_prelock)
                _return_crew_logs(batch_id, uid, crew_back)
                remaining.append(entry)
                continue
        except (OSError, HistoryLockTimeout):
            logger.warning("could not fully restore session %r", uid, exc_info=True)
            # Live transcript rollback happens under its lock above. Replay and
            # archive rollback stays outside that lock because it can copy
            # unbounded files across filesystems.
            _rollback(done_prelock)
            _return_crew_logs(batch_id, uid, crew_back)
            remaining.append(entry)
            continue
        _release_restored_holds(crew_back)
        _release_crew_holds(batch_id, uid)
        restored += 1

    if remaining:
        _rewrite_manifest(batch, header, remaining)
    else:
        # NO write on this branch, and that is a decided residual rather than an oversight.
        # Every entry below the header describes a file this restore moved back OUT, so a
        # batch the cleanup then declines to remove still lists sessions that are not in it.
        # Clearing them needs a write to `batch`, and there is nowhere safe to put one:
        # AFTER the cleanup the descriptor is closed and every refusal is itself evidence
        # the path may be redirected, and BEFORE it this branch has no write at all today,
        # so adding one hands `atomic_write` - which REPLACES its destination - a path that
        # a batch swapped to a link points wherever an actor chose, including another
        # batch's manifest, whose sessions would become unlistable. A stale listing is
        # visible and reversible; that is not. `empty_trash` clears the batch on the user's
        # own say-so.
        _discard_restored_batch(batch, identity)
    return restored

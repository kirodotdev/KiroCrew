"""Removing a whole unit, holding it back from retention, and staging it in the trash.

``remove_unit`` is the one spelling of deletion (``REMOVE_*`` outcomes). It works under the
unit's lease, removes segments first and the lease last (``_remove_unit_contents``), runs a
caller's companion cleanup inside that hold (``_run_in_hold``) and pauses the eager folder
meanwhile (``_eager_folder_paused``). The session trash keeps a unit out of retention with a
hold mark (``hold_unit``, ``hold_staged_unit``, ``release_unit_hold``, ``is_trash_held``,
``TRASH_HOLD_FILE``), and moves it out of and back into the live tree with durable renames
(``stage_unit``, ``restore_staged_unit``, ``_durable_rename``, ``crew_log_trash_root``).
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from kiro_crew.config.paths import data_home
from kiro_crew.crew_log.errors import CODE_ALREADY_OWNED, CODE_BAD_ROOT, CrewLogError
from kiro_crew.crew_log.lease import LEASE_FILE
from kiro_crew.crew_log.lease import acquire as acquire_lease
from kiro_crew.crew_log.lease import release as release_lease
from kiro_crew.crew_log.schema import KIND_SESSION, require_kind
from kiro_crew.crew_log.store_paths import (
    _ROOT_LEAF,
    _checked_crew_log_root,
    _is_segment_name,
    _mkdir_private,
    crew_log_dir,
    log_exception_text,
    logger,
    segment_paths,
)
from kiro_crew.crew_log.store_segment_reads import read_head
from kiro_crew.crew_log.store_units import _proved_header
from kiro_crew.session_ledger import _store_name, is_link, unlink_lock_in_hold

#: :func:`remove_unit` outcomes. Stable strings, like ``resolve``'s, because a
#: caller has to tell them apart to report honestly: only ``removed`` may be
#: counted as a removal, and only ``failed`` is a problem.
REMOVE_REMOVED = "removed"
#: A writer owns the unit -- another process, or another handle in this one. The
#: unit is LIVE, so it is skipped; a later pass gets it once its writer is gone.
REMOVE_OWNED = "owned"
#: There was nothing to remove: no directory, or one that no id addresses. PROVEN
#: absence -- a caller may act on the unit's name being free, because nothing here
#: stands in for a directory this function declined to look inside.
REMOVE_ABSENT = "absent"
#: The unit's own name is a LINK, so nothing was removed and nothing was inspected.
#: Separate from ``absent`` because the two invite opposite actions: absence says the
#: name is free, while this says a directory exists and reaching it means following a
#: link into a unit that is very likely another member's live one. A caller acting on
#: this as absence does its own cleanup at the RESOLVED path and corrupts that unit;
#: what the condition actually needs is a person to remove the link.
REMOVE_LINKED = "linked"
#: Something in the unit survived. Reported, never counted as removed, and the
#: unit is left identifiable so the next pass can aim at it again.
REMOVE_FAILED = "failed"


def _run_in_hold(action: "Callable[[], bool]", kind: str, unit_id: str) -> bool:
    """Run a caller's companion cleanup and report whether it finished.

    The answer decides whether the removal proceeds, so a failure is NOT swallowed.
    A caller passes a companion because something outside the unit belongs to the
    same deletion, and the gate that stops it being read again can live INSIDE the
    unit -- so a companion left behind while the unit goes is worse than nothing
    removed at all: the surviving data becomes readable again with its gate gone.
    Reporting the failure is what lets the caller keep both.

    An exception is the same answer as a refusal, and its traceback is rendered to
    text rather than attached as ``exc_info``: the action is the CALLER's callable,
    so the retained frames would be ones this package cannot vet.
    """
    try:
        return bool(action())
    except Exception:
        log_exception_text(
            logger,
            logging.WARNING,
            "crew log retention: companion cleanup for %s log %r failed",
            kind,
            unit_id,
        )
        return False


def remove_unit(
    kind: str,
    unit_id: str,
    *,
    guard: "Callable[[Path], bool]",
    in_hold: "Callable[[], bool] | None" = None,
) -> str:
    """Remove one unit's crew log entirely. Returns one of the ``REMOVE_*`` statuses.

    The ONE spelling of deletion in this package, called by the retention sweep
    and by each permanent-delete funnel that reaches it -- a session's, and the
    dashboard's crew-member route -- for the reason the work crew log's
    ``purge_matching`` docstring gives: two callers deleting the same tree two
    ways is two chances to get the order wrong, and the order is the whole
    correctness argument.

    **Ownership first.** Removal goes THROUGH the lease, taken non-blocking and
    ``sole`` so it is shared with nothing: ownership is what stands between this
    and unlinking the segments a live writer is appending to. ``sole`` is not a
    detail -- the lease is refcounted per process, so a plain claim against a
    unit this process is already writing would SUCCEED by joining that count and
    prove nothing (see :func:`lease.acquire`). Either kind of contention answers
    ``owned``: this pass does nothing, and nothing is written either, so a unit
    whose writer outlives the sweep is simply collected by a later one.

    **Then the caller re-decides, INSIDE the hold.** *guard* is REQUIRED and is
    called as ``guard(directory)`` once ownership is held; the unit is removed
    only if it answers true. The lease alone is not enough, and the gap is the
    same one ``purge_matching`` documents: a selection made outside it is a
    SNAPSHOT, and ownership deliberately ends BETWEEN turns, so a session can be
    revived, append, finish its turn and release the lease in the window between
    the decision and the delete -- after which the removal would take a live
    conversation's log while contending with nobody. Re-reading under the hold is
    what makes the decision current, which is why the guard is a callback rather
    than a filter the caller applies first, and why there is no default that
    skips the re-decision. A caller whose precondition is not a property of the
    file passes ``lambda _dir: True`` and says at the call site what does decide.

    **A caller with a companion file OUTSIDE the unit passes ``in_hold``, and it
    runs while this lease is still held, BEFORE anything here is destroyed.** Some of
    a unit's meaning lives outside its directory -- a member's pre-log activity source
    is the case -- and a caller that removed such a file after this function RETURNED
    would do it in the window between the release here and its own next line, where
    another PROCESS can create the unit afresh and fold that source back in. The lease
    is taken ``sole`` and so cannot be shared, which is why the caller cannot simply
    hold it itself; a hook inside the hold closes that window without a second
    spelling of deletion. Optional, so a caller with nothing outside the unit passes
    nothing and is unaffected.

    It runs FIRST, and a false answer stops the removal with ``failed`` and nothing
    here touched. The gate that stops such a companion being read again can live
    inside THIS unit -- a fold marker does -- so a unit destroyed while its companion
    survives is not a partial success but an armed one: the survivor becomes readable
    again with its gate gone, and nothing revisits the unit to notice. In this order a
    refusal leaves both in place and both still gated, and the unit stays
    identifiable for a later pass to aim at again.

    **Then order, with IDENTITY LAST.** Segments carry the header, so they are the
    history and they go first; the per-append lock file next; then any other
    entry, none of them followed if it is a link. The ``.lease`` file is removed
    LAST and only by its holder, which is what keeps ``lease``'s inode check a
    fact about this code: while it exists, the lease path names the file whose
    lock proves ownership, and a lease unlinked before the segments would let a
    second remover take a lock on a fresh inode and unlink the same files
    concurrently. Windows refuses an in-hold unlink and gets it after release
    instead, which is safe there precisely because it fails while any handle is
    open -- the same asymmetry :func:`session_ledger.unlink_lock_in_hold`
    documents, so that function is reused rather than copied.

    **Nothing is written to a crew log that is about to go.** No "pruned" entry, no
    tombstone: removal is not rotation and not a format change, and a reader
    holding a citation into the unit already has its answer from
    ``Ref.resolve``, which reports ``gone`` for a pointer into a unit that has no
    crew log at all.

    Failures are COUNTED, not ignored, and reported for what they are. A unit
    that could not be fully removed answers ``failed`` rather than being reported
    as collected, and stays addressable so the next pass can aim at it again --
    but ``failed`` does not promise its history survived. Segments go first, so
    the ordinary partial removal is history already gone with something else left
    standing; the log line says which of the two happened rather than claiming
    the segments were kept.
    """
    require_kind(kind)
    # The name as WRITTEN, checked before the resolution below follows it.
    # ``crew_log_dir`` returns the RESOLVED path, so a unit directory that is a link
    # to another unit resolves inside the root, passes containment, and hands this
    # function the TARGET -- which is not a link, so checking the resolved path
    # would prove nothing and the removal would delete the other unit's history
    # while reporting this one's id. Refused rather than followed, the same stance
    # ``session_ledger.purge_matching`` takes on a linked store. The CHECKED root,
    # so this and the ``crew_log_dir`` below read the same directory: a linked kind
    # root would otherwise be refused only on the second read, after this one had
    # already followed it.
    #
    # Answered as its OWN status rather than as absence. Absence says the name is
    # free, and a caller acting on that does its own cleanup at the resolved path --
    # which is where this link points, so it would reach into a unit that is very
    # likely another member's live one. The two conditions invite opposite actions,
    # so they cannot share a value.
    named = _checked_crew_log_root(kind) / _store_name(unit_id)
    if is_link(named):
        logger.warning(
            "crew log retention: %s log %r is a link; refusing to remove what it names",
            kind,
            unit_id,
        )
        return REMOVE_LINKED
    directory = crew_log_dir(kind, unit_id)
    if not directory.is_dir():
        return REMOVE_ABSENT
    lease_path = directory / LEASE_FILE
    try:
        lease_key = acquire_lease(lease_path, kind=kind, unit_id=unit_id, sole=True)
    except CrewLogError as exc:
        if exc.code == CODE_ALREADY_OWNED:
            return REMOVE_OWNED
        raise
    except OSError:
        # Failing to open the lease file is not evidence that someone owns the
        # unit, and it is not a removal either.
        logger.warning("crew log retention: cannot lease %s log %r", kind, unit_id, exc_info=True)
        return REMOVE_FAILED
    lease_gone = False
    try:
        if not guard(directory):
            # The unit came back to life, or the caller's reason stopped holding,
            # between the decision and this hold. Not a failure: nothing was
            # removed and nothing was written.
            return REMOVE_OWNED
        if in_hold is not None and not _run_in_hold(in_hold, kind, unit_id):
            # FIRST, and the removal stops here when it refuses. The caller's
            # companion sits outside the unit while the gate that stops it being
            # read again can sit inside -- so destroying this unit before the
            # companion is gone is what arms that gate against a survivor. Taken in
            # this order a refusal costs nothing: both are still here, both are
            # still gated, and the unit stays identifiable for the next pass.
            logger.warning(
                "crew log retention: %s log %r not removed; its companion would not go",
                kind,
                unit_id,
            )
            return REMOVE_FAILED
        # With this process's eager folder held between batches, so no fold has one of
        # these files open: Windows refuses to unlink a file any handle holds, and a fold
        # reads segments without the lease this removal holds.
        with _eager_folder_paused():
            failures, history_gone = _remove_unit_contents(directory)
        if failures:
            # Say WHICH of the two failures this is. Segments go first, so a
            # failure after some of them went is a PARTIAL removal -- that
            # history is already gone and reporting it as kept would send a
            # reader looking for a record this pass destroyed.
            if history_gone:
                logger.warning(
                    "crew log retention: %s log %r only PARTLY removed; %d history file(s) "
                    "already gone and %d entr(ies) would not go",
                    kind,
                    unit_id,
                    history_gone,
                    failures,
                )
                if kind == KIND_SESSION:
                    # Some of this unit's history is gone, which is why the in-memory
                    # edge is suspect -- but "some" is not "the opening record", and the
                    # two ways it can be wrong call for different answers. The disk
                    # decides, by the same reading a fresh scan of this unit would make.
                    # Same import-here reason as the removed path below.
                    from kiro_crew.crew_log.session_tree import opened_record
                    from kiro_crew.crew_log.session_tree_projection import (
                        forget_unit,
                        reconcile_unit_edge,
                        retract_unit_parent,
                    )

                    surviving = None
                    try:
                        ordered = segment_paths(kind, unit_id)
                        if ordered:
                            header, entry, _announced = read_head(ordered[0])
                            surviving = opened_record(directory, header, entry)
                    except OSError:
                        # Cannot read it, so cannot prove anything survives. Treated as
                        # gone, which is the conservative direction: a dropped edge
                        # comes back on the next seed, a kept one that names nothing
                        # readable stays wrong until the process restarts.
                        surviving = None
                    if surviving is None:
                        # Nothing here still yields a record at all.
                        forget_unit(unit_id)
                    else:
                        if surviving.parent_slot is None:
                            # The CREATING segment went while later ones survive, so a
                            # scan now contributes this slot with NO parent. Dropping the
                            # whole record instead would orphan this unit's CHILDREN,
                            # which cite its slot: a slot with no record reads as a
                            # creator that never existed, rather than one whose own
                            # creator is unknown.
                            retract_unit_parent(unit_id)
                        # The citation above is only the FIRST segment's contribution. A
                        # decision is appended later, so it can be in any segment and is
                        # therefore losable by this same pass whether or not the creating
                        # one survived -- which is why this is not inside the arm above.
                        # Re-read from what is left, because nothing else will: a
                        # decision is otherwise only re-derived by a cold rebuild, and
                        # until then the tree would assert a takeover with a deleted
                        # segment behind it and checkpoint that claim.
                        reconcile_unit_edge(unit_id, surviving.slot)
            else:
                logger.warning(
                    "crew log retention: %s log %r not removed; its history is intact",
                    kind,
                    unit_id,
                )
            if history_gone and in_hold is not None:
                # The companion is already gone -- it ran before any of this -- so a
                # partial removal strands nothing. Recorded because the two facts
                # read together: this unit's history is gone AND so is the companion
                # whose gate lived here, which is why nothing revisiting it can
                # re-import anything.
                logger.debug(
                    "crew log retention: %s log %r partly removed with its companion "
                    "already gone",
                    kind,
                    unit_id,
                )
            return REMOVE_FAILED
        # Before the identity unlink below, not after: while the lease FILE exists it
        # names the inode whose lock proves ownership, and once it is gone a second
        # remover can lock a fresh one -- which would put another process inside the
        # hold this removal, and the companion step it already ran, were given alone.
        lease_gone = unlink_lock_in_hold(lease_path)
    finally:
        release_lease(lease_key)
    if not lease_gone:
        # Windows: the OS refused the in-hold unlink, and the late one is safe
        # there because it fails while any handle is open.
        try:
            lease_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("crew log retention: lease file still held; leaving it")
            return REMOVE_FAILED
    try:
        directory.rmdir()
    except OSError:
        # Every file this owns is gone. A directory that will not go is residue,
        # not retained history, so the removal still counts -- but say so.
        logger.debug("crew log retention: %s log %r directory not removed", kind, unit_id)
    if kind == KIND_SESSION:
        # The session tree projection holds this unit's lineage edge in memory, and
        # the disk it was folded from has stopped holding it. Dropped HERE, at the one
        # point the removal is established, rather than in the sweep: ``remove_unit``
        # is also reached by a direct delete, and a projection updated only by the
        # retention pass would keep serving an edge into a unit that is gone.
        #
        # Deliberately after the ``rmdir``, which is allowed to fail: what makes the
        # record wrong is that the unit's SEGMENTS are gone, and an empty directory
        # left standing is residue that answers no record either way. Imported here
        # because the projection imports the store, and so this owner, for the root and
        # the replay.
        from kiro_crew.crew_log.session_tree_projection import forget_unit

        forget_unit(unit_id)
    return REMOVE_REMOVED


#: A file inside a unit directory that keeps the unit out of retention. The session
#: trash writes it when a restore that failed could not stage a unit it had already put
#: back: the unit is then live while the rest of its session is still in the trash, and
#: the sweep must not expire a unit whose session the user can still restore. Only the
#: trash removes it, when that session is restored or its batch is emptied.
TRASH_HOLD_FILE = ".trash-hold"


def _write_hold(directory: Path) -> None:
    """Create *directory*'s hold mark and make it durable. Raises ``OSError`` if it is not.

    The mark is created without following a link, then the file and its directory are
    synced, so a power loss cannot come back to a unit the sweep reads as unheld.
    """
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(directory / TRASH_HOLD_FILE, flags, 0o600)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_hold_dir(directory)


def _sync_hold_dir(directory: Path) -> None:
    """Sync the directory entry of a new hold mark."""
    from kiro_crew.atomic_write import fsync_dir

    fsync_dir(directory)


def hold_unit(kind: str, unit_id: str) -> bool:
    """Keep *unit_id* out of retention until :func:`release_unit_hold`. False when not held.

    The unit is already live, so there is nothing to withhold when the mark cannot be
    made durable: a mark that exists is still what the sweep reads, and the failed
    sync is logged. False only when no mark could be created at all.
    """
    require_kind(kind)
    try:
        named = _checked_crew_log_root(kind) / _store_name(unit_id)
        if is_link(named):
            return False
        directory = crew_log_dir(kind, unit_id)
        if not directory.is_dir():
            return False
    except (CrewLogError, OSError):
        log_exception_text(
            logger, logging.ERROR, "crew log trash: could not hold %s log %r", kind, unit_id
        )
        return False
    try:
        _write_hold(directory)
    except OSError:
        if not _is_held(directory):
            log_exception_text(
                logger, logging.ERROR, "crew log trash: could not hold %s log %r", kind, unit_id
            )
            return False
        log_exception_text(
            logger, logging.WARNING, "crew log trash: could not sync the hold on %r", unit_id
        )
    return True


def hold_staged_unit(staged: Path) -> bool:
    """Mark a unit still in the trash as held, so it is held the moment it is published.

    A restore publishes a session's units before its transcript, and a sweep running in
    between would otherwise read a closed, expired unit and delete it while the rest of
    the session is still coming back. The mark rides the rename into the live tree, and
    it is durable before this answers True, so the caller publishes only a unit whose
    hold survives a power loss. False when the staged directory is not a real directory
    under the trash root, or when the mark could not be made durable.
    """
    trash = crew_log_trash_root()
    try:
        if is_link(trash) or is_link(staged) or not staged.is_relative_to(trash):
            return False
        if not staged.is_dir():
            return False
        _write_hold(staged)
    except OSError:
        log_exception_text(
            logger, logging.WARNING, "crew log trash: could not hold staged %s", staged.name
        )
        return False
    return True


def release_unit_hold(kind: str, unit_id: str) -> None:
    """Let *unit_id* age out again; a unit with no hold is left as it is."""
    require_kind(kind)
    try:
        named = _checked_crew_log_root(kind) / _store_name(unit_id)
        if is_link(named):
            return
        (crew_log_dir(kind, unit_id) / TRASH_HOLD_FILE).unlink()
    except FileNotFoundError:
        return
    except (CrewLogError, OSError):
        log_exception_text(
            logger, logging.WARNING, "crew log trash: could not release the hold on %r", unit_id
        )


def is_trash_held(directory: Path) -> bool:
    """Whether the unit in *directory* belongs to a session still in the trash."""
    return _is_held(directory)


def _is_held(directory: Path) -> bool:
    """Whether *directory* carries a trash hold; an unreadable answer counts as held."""
    try:
        (directory / TRASH_HOLD_FILE).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def crew_log_trash_root() -> Path:
    """Where session trash stages crew logs: ``<data home>/crew-log/trash``.

    Inside the crew-log tree rather than beside the transcripts in the session trash,
    because that tree is the one the sandbox hides from agents. A staged crew log is
    later RESTORED into the live tree, so a copy an agent could edit while it waits
    would be a way to write entries the gateway then reads as its own.
    """
    return data_home() / _ROOT_LEAF / "trash"


#: Whether a unit directory can be renamed while this process holds its lease. POSIX
#: renames a directory whatever is open inside it; Windows refuses a directory holding
#: an open handle, and the held lease IS one. See :func:`stage_unit`.
_RENAME_UNDER_LEASE = os.name != "nt"


def _sync_down(top: Path, leaf: Path) -> None:
    """Sync *leaf* and every directory above it up to *top*, so a new name survives a crash."""
    from kiro_crew.atomic_write import fsync_dir

    level = leaf
    while True:
        fsync_dir(level)
        if level == top or top not in level.parents:
            return
        level = level.parent


def _durable_rename(source: Path, destination: Path) -> bool:
    """Rename *source* to *destination* and make both names durable. False when it did not.

    The rename alone is not a move a caller may build on: a power loss before the
    two parent directories are synced can come back to the unit under neither name,
    or under both. The destination's own chain is synced BEFORE the rename, so the
    directory it lands in exists on disk first; both parents are synced after it. A
    sync that fails after the rename puts the unit back and syncs that too, so a False
    answer means the unit is where it started. If even the rollback cannot be synced,
    a power loss may still recover the unit under *destination*; that is logged, and it
    loses nothing -- a crew log left in the trash with no manifest entry is kept there
    by the session trash, which refuses to delete staging its manifest does not list.
    """
    top = data_home() / _ROOT_LEAF
    _mkdir_private(destination.parent)
    _sync_down(top, destination.parent)
    os.rename(source, destination)
    try:
        _sync_down(top, destination.parent)
        _sync_down(top, source.parent)
    except OSError:
        log_exception_text(
            logger, logging.WARNING, "crew log: could not sync the move of %s", source.name
        )
        try:
            os.rename(destination, source)
        except OSError:
            log_exception_text(
                logger, logging.ERROR, "crew log: %s is at %s and not synced", source, destination
            )
            return True
        try:
            _sync_down(top, source.parent)
            _sync_down(top, destination.parent)
        except OSError:
            log_exception_text(
                logger,
                logging.ERROR,
                "crew log: %s was put back at %s but that is not synced; after a power "
                "loss it may be found at %s instead",
                source.name,
                source,
                destination,
            )
        return False
    return True


def stage_unit(kind: str, unit_id: str, destination: Path) -> str:
    """Move one unit's directory to *destination*, under its lease. A ``REMOVE_*`` status.

    The session trash's counterpart of :func:`remove_unit`: the same sole lease stands
    between the move and a live writer, so a unit some process is appending to answers
    ``owned`` and stays where it is. ``removed`` means the unit left the live tree and
    both names are on disk. *destination* must not exist; it is created under
    :func:`crew_log_trash_root`.

    On Windows the lease is released before the rename rather than held across it,
    because Windows refuses to rename a directory that holds an open handle and the
    lease is one. That refusal is what stands in for the hold: a writer that takes the
    lease in the gap has its lease file open inside the directory, so the rename fails
    and the unit stays, exactly as ``owned`` would have left it.
    """
    require_kind(kind)
    named = _checked_crew_log_root(kind) / _store_name(unit_id)
    if is_link(named):
        return REMOVE_ABSENT
    directory = crew_log_dir(kind, unit_id)
    if not directory.is_dir():
        return REMOVE_ABSENT
    trash = crew_log_trash_root()
    if is_link(trash) or not destination.is_relative_to(trash):
        raise CrewLogError(
            f"refusing to stage a crew log outside {trash}: {destination}", code=CODE_BAD_ROOT
        )
    try:
        lease_key = acquire_lease(directory / LEASE_FILE, kind=kind, unit_id=unit_id, sole=True)
    except CrewLogError as exc:
        if exc.code == CODE_ALREADY_OWNED:
            return REMOVE_OWNED
        raise
    except OSError:
        log_exception_text(
            logger, logging.WARNING, "crew log trash: cannot lease %s log %r", kind, unit_id
        )
        return REMOVE_FAILED
    held = True
    try:
        if not _RENAME_UNDER_LEASE:
            release_lease(lease_key)
            held = False
        moved = _durable_rename(directory, destination)
    except OSError:
        log_exception_text(
            logger, logging.WARNING, "crew log trash: could not stage %s log %r", kind, unit_id
        )
        return REMOVE_FAILED
    finally:
        if held:
            release_lease(lease_key)
    # The session-tree projection keeps this unit's record: a staged unit comes back on
    # restore, and nothing re-derives a record for an unchanged root. A reader that
    # meets the unit while it is staged gets ``gone``, as it would for a removed one.
    return REMOVE_REMOVED if moved else REMOVE_FAILED


def restore_staged_unit(kind: str, staged: Path) -> "str | None":
    """Put a unit :func:`stage_unit` moved back into the live tree; its id, or None.

    The unit's own header decides where it goes: its id must fold to the staged
    directory's name, the same proof :func:`unit_header_slot` requires. An occupied
    destination is left alone -- a unit recreated since is newer than this copy. The
    move is synced like the stage (:func:`_durable_rename`).
    """
    require_kind(kind)
    header = None if is_link(staged) else _proved_header(staged)
    if header is None:
        return None
    root = _checked_crew_log_root(kind)
    target = root / staged.name
    if is_link(target) or target.exists():
        return None
    try:
        moved = _durable_rename(staged, target)
    except OSError:
        log_exception_text(
            logger, logging.WARNING, "crew log trash: could not restore %s", staged.name
        )
        return None
    return str(header["id"]) if moved else None


def _eager_folder_paused() -> "AbstractContextManager[bool]":
    """:func:`eager.paused`, imported late: the eager folder imports this module."""
    from kiro_crew.crew_log import eager

    return eager.paused()


def _remove_unit_contents(directory: Path) -> "tuple[int, int]":
    """Delete everything in *directory* except the lease. ``(failures, history_gone)``.

    Ordered, and every failure counted: ``rmtree(ignore_errors=True)`` reports
    success over a subtree it left standing, and on Windows a sharing violation
    on one held file is exactly that case. Segments go first because they are the
    history; the lease stays for the caller to remove last, under its own hold.
    Call while holding the lease.

    Both numbers are returned because a failure alone does not say what the unit
    still holds, and the caller has to be able to say. Segments going first means
    a late failure is the ordinary shape of a partial removal: the history is
    already gone and something else would not go, which is not the same event as
    a removal that got nowhere.
    """
    failures = 0
    history_gone = 0
    try:
        children = list(directory.iterdir())
    except OSError:
        # Nothing was reached, so nothing of the history is gone either.
        return (1, 0)
    ordered = sorted(
        (child for child in children if child.name != LEASE_FILE),
        key=lambda child: 0 if _is_segment_name(child.name) else 1,
    )
    for child in ordered:
        was_history = _is_segment_name(child.name)
        # A linked entry is unlinked as a NAME, never followed: ``is_dir`` is true
        # through a link to a directory, and walking it would delete the target.
        if child.is_dir() and not is_link(child):
            # ``ignore_errors`` rather than a callback: ``onerror`` is deprecated
            # since 3.12 and slated for removal, and ``onexc`` does not exist on
            # every version this runs on. Neither is needed, because the
            # correctness here does not come from the callback -- it comes from the
            # existence check below. That is the same reason this cannot simply
            # trust ``rmtree``: it reports success over a subtree it left standing,
            # which on Windows is exactly what a sharing violation produces.
            shutil.rmtree(child, ignore_errors=True)
            if child.exists():
                failures += 1
            continue
        try:
            child.unlink()
        except OSError:
            failures += 1
        else:
            if was_history:
                history_gone += 1
    return (failures, history_gone)

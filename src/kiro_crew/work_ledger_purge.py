"""Telling a finished conductor work ledger from a live one, and removing it.

The item census and header damage a sweep judges a ledger by, the newest activity it
records, and ``purge_conductor``, which removes a finished ledger under every lock it holds
and leaves the lock shells for last. :mod:`kiro_crew.work_ledger` binds every name defined
here, so each one reads, imports and patches as ``kiro_crew.work_ledger.<name>``.
"""

from __future__ import annotations

import json
import os
import shutil
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.atomic_write import read_bytes_with_retry
from kiro_crew.platform_compat import open_lock_file, release_lock, try_acquire_lock
from kiro_crew.session_ledger import is_link, unlink_lock_in_hold
from kiro_crew.work_ledger_records import (
    CODE_LEDGER_NOT_FINISHED,
    ITEM_STATES,
    MAX_RECORD_BYTES,
    TERMINAL_ITEM_STATES,
    WorkLedgerError,
    logger,
)
from kiro_crew.work_ledger_rules import _parse_iso
from kiro_crew.work_ledger_store import (
    _CONDUCTOR_FILE,
    _ITEM_ID_RE,
    _ITEMS_DIR,
    _KEY_FILE,
    _LOCK_FILE,
    _read_json_record,
    conductor_dir,
    conductor_lock,
)

# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ItemCensus:
    """What one pass over a conductor's item files found.

    ``damage`` is set when the items DIRECTORY could not be enumerated -- its own
    answer, never folded into the counts, because an unreadable directory is the
    one case where the counts and the truth are unrelated: read as "zero items" it
    selects an ordinary purge, which removes the header and leaves behind the
    item data it could not see. An ABSENT directory is not damage: a conductor
    that never created an item has none.
    """

    open_items: int = 0
    closed: int = 0
    unreadable: int = 0
    newest_closed_at: str = ""
    damage: str = ""
    #: The newest mtime of any non-lock file under ``items/`` -- records, event
    #: logs, and a torn or misnamed file the census could not read. A ledger's
    #: age must see EVERY write to it: an unreadable item carries no stamp the
    #: census can read, and ``_create_item`` writes it without touching the
    #: header, so without this a crash-torn write into an old conductor would be
    #: invisible to the retention window and deletable under ``allow_unreadable``
    #: the moment it landed. Lock files are excluded because the purge itself
    #: touches them (``_hold_every_item_lock``), and a reading that moved under
    #: the reader would refuse forever.
    newest_write_at: datetime | None = None


def header_damage(directory: Path) -> str:
    """Why *directory*'s ``conductor.json`` cannot be trusted, or ``""`` when it reads.

    Presence is not readability. An absent, torn, oversized or non-object header
    all mean the same thing for a DELETE decision -- this store's own account of
    itself is missing -- and treating only absence as damage let a malformed
    header read as a finished ledger. The one check both the sweep's scanner and
    :func:`purge_conductor`'s locked recheck use, so a header that tears between
    the report and the purge is refused by the store exactly as the scanner would
    have refused to list it as finished.
    """
    header = directory / _CONDUCTOR_FILE
    try:
        size = header.stat().st_size
    except FileNotFoundError:
        return "no conductor record"
    except OSError as exc:
        return f"conductor record unreadable ({exc.strerror or exc})"
    if size > MAX_RECORD_BYTES:
        return "conductor record over the size ceiling"
    try:
        raw = json.loads(read_bytes_with_retry(header).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return "conductor record is not readable JSON"
    if not isinstance(raw, dict):
        return "conductor record is not a JSON object"
    return ""


def census_items(directory: Path) -> ItemCensus:
    """Classify every item file under *directory* for a DELETE decision.

    The one census both the sweep's scanner and :func:`purge_conductor` use, so
    the classification rule lives once. It reads the files directly rather than
    through :func:`list_work_items`, which SKIPS an unreadable item: right for a
    listing, wrong here, because a torn record would then be invisible and the
    ledger would look finished. An item whose stored ``state`` is not one of
    :data:`ITEM_STATES` counts as unreadable rather than closed -- an
    unrecognised disposition is not evidence of closure. The newest ``closed_at``
    is chosen as an INSTANT: the stamps carry the local offset at write time, and
    across a DST change two of them sort lexically in the wrong order.

    Lock-free. Under the conductor lock it is authoritative; outside it, a
    snapshot the caller must re-take under the lock before acting on it.
    """
    open_items = closed = unreadable = 0
    newest = ""
    newest_at: datetime | None = None
    items_dir_path = directory / _ITEMS_DIR
    if is_link(items_dir_path):
        # A linked ``items/`` names another directory's files as this store's
        # items. Counting them would let a purge of THIS store delete THOSE
        # files; this is damage, and the purge refuses it under its own lock.
        return ItemCensus(damage="items directory is a link, not a directory")
    try:
        with os.scandir(items_dir_path) as scan:
            names = sorted(entry.name for entry in scan if entry.is_file())
    except FileNotFoundError:
        return ItemCensus()
    except OSError as exc:
        return ItemCensus(damage=f"items directory could not be read ({exc.strerror or exc})")
    newest_write: datetime | None = None
    for name in names:
        if not name.endswith(".lock"):
            try:
                written = datetime.fromtimestamp((items_dir_path / name).stat().st_mtime)
            except OSError:
                pass
            else:
                written = written.astimezone()
                if newest_write is None or written > newest_write:
                    newest_write = written
        if not name.endswith(".json"):
            continue
        if not _ITEM_ID_RE.match(name[: -len(".json")]):
            # Item-shaped, but its stem is not an id the store ever minted: a
            # renamed or hand-moved record. Skipping it would let a plain purge
            # delete it with the rest of the contents; for a delete decision it
            # is unreadable, like a record whose stored id disagrees with its
            # file. Event logs (``.jsonl``) and atomic-write temps (``.tmp``)
            # are not item-shaped and are not counted.
            unreadable += 1
            continue
        raw = _read_json_record(items_dir_path / name)
        if not isinstance(raw, dict):
            unreadable += 1
            continue
        stored_id = raw.get("item_id")
        if isinstance(stored_id, str) and stored_id and stored_id != name[: -len(".json")]:
            # The reader's own rule (``read_work_item``): a record whose stored
            # id names a different item than the file it sits in is a misnamed
            # or hand-moved file and reads as absent. For a DELETE decision that
            # is unreadable, not closed -- counting its terminal ``state`` would
            # let a plain purge remove a record the store itself will not read.
            unreadable += 1
            continue
        state = raw.get("state")
        if not isinstance(state, str) or state not in ITEM_STATES:
            unreadable += 1
        elif state in TERMINAL_ITEM_STATES:
            closed += 1
            closed_at = raw.get("closed_at")
            parsed = _parse_iso(closed_at) if isinstance(closed_at, str) else None
            if parsed is not None and parsed.tzinfo is None:
                # A naive stamp beside an offset-bearing one would make the ``>``
                # below raise TypeError out of a function that must not.
                parsed = parsed.astimezone()
            if (
                isinstance(closed_at, str)
                and parsed is not None
                and (newest_at is None or parsed > newest_at)
            ):
                newest_at, newest = parsed, closed_at
        else:
            open_items += 1
    return ItemCensus(
        open_items=open_items,
        closed=closed,
        unreadable=unreadable,
        newest_closed_at=newest,
        newest_write_at=newest_write,
    )


def _newest_activity(directory: Path, census: ItemCensus) -> datetime | None:
    """The instant this ledger last changed, by either of its two writers.

    The newest item ``closed_at`` alone is NOT the ledger's age: the conductor's
    own ``goal`` action rewrites ``conductor.json`` with no item involved, and a
    conductor that just bumped its round on a set of old closed items is a live
    conductor about to create. So age is the newer of the newest close and the
    header's mtime, and it is what :func:`purge_conductor` re-checks under the
    lock against the window the caller passes.
    """
    candidates: list[datetime] = []
    parsed = _parse_iso(census.newest_closed_at) if census.newest_closed_at else None
    if parsed is not None:
        candidates.append(parsed.astimezone() if parsed.tzinfo is None else parsed)
    # The newest write under ``items/`` by mtime, readable or not: the one
    # reading that sees a crash-torn item the census could not stamp. See
    # ``ItemCensus.newest_write_at``.
    if census.newest_write_at is not None:
        candidates.append(census.newest_write_at)
    # The header's mtime, or the DIRECTORY's when the header cannot be statted:
    # ``atomic_write`` renames into this directory, so a store whose header is
    # gone or unreadable still shows when it was last written to. Dropping the
    # reading would make the newest close the only age, and a store touched a
    # minute ago would then pass a window it should refuse. The same fallback
    # the sweep's scanner uses.
    for candidate_path in (directory / _CONDUCTOR_FILE, directory):
        try:
            mtime = candidate_path.stat().st_mtime
        except OSError:
            continue
        candidates.append(datetime.fromtimestamp(mtime).astimezone())
        break
    return max(candidates) if candidates else None


@contextmanager
def _hold_every_item_lock(directory: Path) -> Iterator[None]:
    """Hold every item lock under *directory*, or refuse. Call under the conductor lock.

    The census reads item FILES, and a file another writer is replacing at that
    moment reads as unreadable -- on Windows a read of a file another handle holds
    open raises, on POSIX a half-renamed record is torn. So a census alone cannot
    tell "damaged" from "being written", and ``allow_unreadable`` would let an
    operator remove an item whose worker holds its lock right now. The item lock
    is the one thing that CAN tell: a writer holds it for the whole of its
    read-modify-write. Taking every item lock NON-BLOCKING before the census, and
    holding them through the removal, makes "someone is writing" a refusal rather
    than a misclassification. A lock that cannot be taken is a live writer, and a
    live writer is not finished, whatever its file looks like.

    Non-blocking on purpose: a blocking acquire would make the purge wait for a
    worker's write to finish and then delete the item it just wrote. Taken in
    sorted name order, which is the same relative order every other path uses,
    so this cannot deadlock against a writer holding one item lock and waiting on
    another -- no writer holds two. Lock files are created for items that have
    none yet, exactly as :func:`item_lock` would; they go with the store.
    """
    items_dir_path = directory / _ITEMS_DIR
    try:
        with os.scandir(items_dir_path) as scan:
            stems = sorted(
                entry.name[: -len(".json")]
                for entry in scan
                if entry.is_file()
                and entry.name.endswith(".json")
                and _ITEM_ID_RE.match(entry.name[: -len(".json")])
            )
    except OSError:
        # Absent: a conductor with no items has no locks to take. Unreadable:
        # nothing can be enumerated, so nothing can be locked either -- the
        # census reports that same failure as damage and the purge refuses on it
        # (or, with allow_unreadable, the removal counts its own failure and
        # keeps the identity files). Either way this is not the place to decide.
        stems = []
    with ExitStack() as stack:
        for stem in stems:
            lock_path = items_dir_path / f"{stem}.lock"
            fd = stack.enter_context(open_lock_file(lock_path))
            if not try_acquire_lock(fd, exclusive=True):
                raise WorkLedgerError(
                    f"item {stem!r} is being written right now; refusing to purge the ledger",
                    code=CODE_LEDGER_NOT_FINISHED,
                    field="state",
                )
            # Registered after the open, so the unwind releases each lock before
            # closing its descriptor, newest first.
            stack.callback(release_lock, fd)
        yield


def purge_conductor(slot_key: str, *, allow_unreadable: bool, idle_for: timedelta) -> bool:
    """Delete one conductor's whole ledger directory. Returns whether it went.

    Both keywords are REQUIRED, with no defaults: this is an irreversible delete
    with one production caller (``ledger_sweep.purge``), and a default would let
    a future caller skip the retention window or the unreadable-record refusal
    by omission. "No window" is spelled ``idle_for=timedelta(0)``, visibly.

    An EXPLICIT maintenance primitive, like ``session_ledger.purge_matching``:
    nothing in the request path calls it. The caller owns the POLICY decision -- which
    ledgers are old enough, which an operator asked about -- but not the
    correctness one: this function re-establishes for itself, under the lock,
    that the ledger is actually finished.

    IT IS AIMED BY KEY, NOT BY PATH, and resolves the directory itself through
    :func:`conductor_dir`. A caller that found a store by WALKING the root must
    therefore check that the directory it looked at is the one this key resolves
    to before calling: a copied or hand-made store carrying another ledger's
    ``slot_key`` breadcrumb otherwise sends this function at the canonical
    ledger, which the caller never listed and an operator never saw.

    Serialised by :func:`conductor_lock`, FIRST in the lock order, then EVERY
    item lock taken non-blocking in sorted order (:func:`_hold_every_item_lock`)
    -- the documented conductor -> item order, so no writer can be waiting the
    other way -- and the re-check happens INSIDE both holds. An item lock that
    cannot be taken is a worker mid-write, and the purge refuses: a file being
    replaced reads as unreadable to the census, so without the item locks
    ``allow_unreadable`` would let an operator remove an item its worker is
    writing to this instant. It refuses -- ``WorkLedgerError`` with
    code :data:`CODE_LEDGER_NOT_FINISHED` -- when any item is non-terminal, when
    the items directory cannot be enumerated, when any item record is unreadable
    unless *allow_unreadable* says the operator asked for that too, and -- when
    *idle_for* is POSITIVE -- when the
    ledger's newest activity by EITHER writer (an item close, a header rewrite or
    any write under ``items/`` -- see :func:`_newest_activity`) is younger than
    *idle_for*. A non-positive *idle_for* is a caller declining a retention
    window, and age is then not consulted at all; every other refusal above still
    applies, so "no window" buys no unreadable record, no open item and no live
    writer. The caller's scan measured age outside the lock; a
    ``goal`` round-bump between that scan and this hold is a live conductor, and
    the recheck is what catches it. ``_create_item`` and ``_write_goal`` take
    this same lock across their whole transaction, so neither can land while the
    re-check and the removal run.

    REMOVAL IS ORDERED, AND THE IDENTITY FILES GO LAST. Items and any other
    content are removed first, every failure is counted rather than ignored, and
    only once everything else is gone do ``conductor.json`` and then the
    ``slot_key`` breadcrumb go -- header first, breadcrumb after, so a header
    unlink that fails leaves a store that still NAMES itself and can be purged
    again, rather than one no key can reach. On any failure the store keeps
    whatever identity it still has and reads as ``unreadable`` to the next
    sweep instead of as a half-destroyed ledger. The return value is whether the
    directory is gone.

    THE LOCK INODES. Every lock file -- the conductor's and each item's -- is
    unlinked INSIDE the holds on POSIX (``unlink_lock_in_hold``), so a writer
    queued on any of them acquires a detached inode, finds its path gone and
    refuses; nothing is ever handed a second inode while a first is held.
    Windows refuses an unlink under an open handle and gets them after release
    (:func:`_remove_lock_shell`), which is safe there because that unlink fails
    whenever a writer still holds a handle -- the OS keeps the identity stable. Removing
    a store necessarily removes the inode its lock is taken on, so a writer that
    was blocked on the OLD inode is not serialised against a later writer that
    creates a NEW one. No ordering fixes that -- unlinking inside the hold has the
    same effect -- and renaming the store away first is refused on Windows while
    this function holds its own handle inside it. Every path-based advisory lock
    has this property the moment its store is deleted. What makes it
    harmless here is that every writer checks, inside its hold, that the inode it
    acquired is still the one at the lock's path (``require_lock_inode`` in
    ``_open_lock``): a writer that was queued behind this purge refuses with the
    same ``OSError`` a held lock produces, and ``_create_item`` additionally
    refuses when it finds no header. A queued writer therefore cannot publish
    into the removed store; the worst outcome is one refused write.
    """
    directory = conductor_dir(slot_key)
    if is_link(directory) or is_link(directory / _ITEMS_DIR):
        # A linked store, or a linked ``items/`` inside it, names files that are
        # not this ledger's; a removal would land on them. Refused outright,
        # before any lock is taken, whatever the caller passed: this is not a
        # damaged record an operator can ask to clear, it is a delete aimed
        # somewhere else.
        raise WorkLedgerError(
            "conductor ledger directory (or its items/) is a link, not a directory; "
            "refusing to purge through it",
            code=CODE_LEDGER_NOT_FINISHED,
            field="state",
        )
    try:
        lock_cm = conductor_lock(slot_key, create=False)
        lock_cm.__enter__()
    except FileNotFoundError:
        # Already gone -- removed by a concurrent sweep, or never there. Nothing
        # to do, and nothing must be created in its place.
        return False
    try:
        shell = _purge_conductor_locked(
            directory, allow_unreadable=allow_unreadable, idle_for=idle_for
        )
    finally:
        lock_cm.__exit__(None, None, None)
    if shell is None:
        return False
    # AFTER the conductor lock is released, never inside it. The shell's job is
    # the lock files the holds could not unlink, and on Windows the conductor's
    # own is one of them: its handle is still open until the line above, so an
    # unlink here while it was held raised, the directory stayed non-empty, and
    # the purge reported False over a store it had already emptied. The session
    # half (``session_ledger.purge_matching``) releases before its shell for the
    # same reason.
    conductor_lock_gone, item_locks_left = shell
    _remove_lock_shell(
        directory, conductor_lock_gone=conductor_lock_gone, item_locks_left=item_locks_left
    )
    return not directory.exists()


def _purge_conductor_locked(
    directory: Path, *, allow_unreadable: bool, idle_for: timedelta
) -> tuple[bool, list[Path]] | None:
    """The body of :func:`purge_conductor`, under the conductor lock it acquired.

    Returns what :func:`_remove_lock_shell` needs once that lock is released --
    whether the conductor lock went inside the hold, and the item lock paths that
    did not -- or ``None`` when the store was kept.
    """
    with _hold_every_item_lock(directory):
        census = census_items(directory)
        if census.open_items:
            raise WorkLedgerError(
                f"conductor ledger has {census.open_items} open item(s); refusing to purge it",
                code=CODE_LEDGER_NOT_FINISHED,
                field="state",
            )
        if census.damage and not allow_unreadable:
            raise WorkLedgerError(
                f"conductor ledger's {census.damage}; refusing to purge it",
                code=CODE_LEDGER_NOT_FINISHED,
                field="state",
            )
        if census.unreadable and not allow_unreadable:
            raise WorkLedgerError(
                f"conductor ledger has {census.unreadable} unreadable item record(s); "
                "refusing to purge it",
                code=CODE_LEDGER_NOT_FINISHED,
                field="state",
            )
        if (
            not census.closed
            and not census.unreadable
            and not census.damage
            and (directory / _CONDUCTOR_FILE).exists()
        ):
            # No items at all, and a header: finished-LOOKING, not finished. The
            # same rule the sweep's scanner applies, re-applied here because
            # this is the binding decision -- a caller-built report, or a store
            # whose items vanished between the scan and this hold, must not turn
            # an empty conductor into a purgeable one, torn header or not. A
            # directory with NEITHER header nor items is not that case: it is
            # the residue a purge leaves when a lock file could not be unlinked
            # -- on Windows, a writer queued on the conductor lock holds its
            # handle through the post-release unlink -- and it falls through to
            # the header check below as damage, removable with allow_unreadable.
            raise WorkLedgerError(
                "conductor ledger has no items; an empty conductor is not finished, "
                "refusing to purge it",
                code=CODE_LEDGER_NOT_FINISHED,
                field="state",
            )
        # The header is re-read under the lock too. The scanner lists a store
        # whose header does not read as ``unreadable``, which a plain purge skips;
        # a header that tore between the report and this hold must meet the same
        # refusal here, or the report's "finished" would be trusted over the
        # store's own account of itself.
        damaged_header = header_damage(directory)
        if damaged_header and not allow_unreadable:
            raise WorkLedgerError(
                f"conductor ledger's {damaged_header}; refusing to purge it",
                code=CODE_LEDGER_NOT_FINISHED,
                field="state",
            )
        latest = _newest_activity(directory, census)
        # A NON-POSITIVE WINDOW HAS NOTHING TO CHECK, and asking anyway is a bug.
        # ``idle_for=timedelta(0)`` is the caller saying "do not hold this store
        # back on age grounds", so an age refusal under it is wrong whatever the
        # store's timestamps read. The subtraction alone does not honour that:
        # ``_newest_activity`` sources a candidate from ``st_mtime``, a file
        # timestamp can order AHEAD of a later ``datetime.now()`` reading where
        # the filesystem's resolution is finer than the clock's advance, and the
        # elapsed time is then a small negative timedelta -- which is less than a
        # zero window, so the guard refuses a purge that asked for no window. The
        # window's sign is therefore the first thing consulted.
        #
        # A POSITIVE window still refuses on that same future reading, and must:
        # there the caller did ask for an age judgement, a store whose newest
        # write reads ahead of the clock has just been written to, and refusing is
        # the conservative half of an irreversible delete. The sweep's scanner
        # reaches the same verdict by clamping elapsed time at zero before
        # comparing (``ledger_sweep._age_of``), so both halves agree.
        if (
            latest is not None
            and idle_for > timedelta(0)
            and datetime.now().astimezone() - latest < idle_for
        ):
            raise WorkLedgerError(
                "conductor ledger changed within the retention window; refusing to purge it",
                code=CODE_LEDGER_NOT_FINISHED,
                field="state",
            )
        failures = _remove_contents_locked(directory)
        if failures:
            logger.warning(
                "work ledger purge: %d entr(y/ies) could not be removed; the header and "
                "breadcrumb are kept so the store stays identifiable",
                failures,
            )
            return None
        # Header first, breadcrumb second: a header that will not unlink leaves a
        # store that still names itself, so a later purge can still be aimed at it.
        for name in (_CONDUCTOR_FILE, _KEY_FILE):
            try:
                (directory / name).unlink(missing_ok=True)
            except OSError:
                logger.warning("work ledger purge: %s could not be removed; store kept", name)
                return None
        # Every lock inode goes INSIDE the holds where the OS allows it, so a
        # writer queued on any of them finds its path gone on acquire and
        # refuses; see ``session_ledger.unlink_lock_in_hold``. Windows refuses
        # these and gets them after release from ``_remove_lock_shell``.
        held_item_locks = _unlink_item_locks_in_hold(directory)
        conductor_lock_gone = unlink_lock_in_hold(directory / _LOCK_FILE)
    return conductor_lock_gone, held_item_locks


def _unlink_item_locks_in_hold(directory: Path) -> list[Path]:
    """Unlink every ``items/<stem>.lock`` while :func:`_hold_every_item_lock` holds it.

    Returns the lock paths the OS REFUSED to unlink under the hold (Windows), which
    are the only ones :func:`_remove_lock_shell` may touch afterwards.
    """
    try:
        entries = list((directory / _ITEMS_DIR).iterdir())
    except OSError:
        return []
    return [
        entry
        for entry in entries
        if entry.name.endswith(".lock") and not unlink_lock_in_hold(entry)
    ]


#: The files that make a store a ledger and let a purge NAME it. Removed last,
#: header before breadcrumb, and only when everything else is already gone.
_IDENTITY_FILES = frozenset({_CONDUCTOR_FILE, _KEY_FILE})


def _remove_contents_locked(directory: Path) -> int:
    """Remove *directory*'s contents except the identity files and EVERY lock file.

    Returns how many entries could NOT be removed. Failures are counted, never
    ignored: ``shutil.rmtree(ignore_errors=True)`` would report success over a
    subtree it silently left standing, and the caller's decision to remove the
    header depends on this count being honest.

    Lock files -- the conductor's and every ``items/<stem>.lock`` -- are LEFT
    STANDING by this pass; they are not content, and they are not counted. What
    happens to them next differs by platform. On POSIX the caller unlinks each
    one INSIDE its hold (:func:`_unlink_item_locks_in_hold`,
    ``unlink_lock_in_hold``), so a queued writer acquires a detached inode and
    refuses. On Windows a handle opened with ``os.open`` has no
    ``FILE_SHARE_DELETE``, unlinking a held lock raises, and those paths are
    handed to :func:`_remove_lock_shell` to unlink after every hold is released.
    Either way this pass must not try: counting a refused lock unlink as a
    content failure would keep the header on every Windows purge, and the
    work-half purge could never succeed there. Because of that the ``items/``
    directory is walked file by file rather than ``rmtree``'d: the tree cannot
    go while the locks in it stay. Call under the conductor lock.
    """
    failures = 0

    def _count(_fn: Any, _path: Any, _exc: Any) -> None:
        nonlocal failures
        failures += 1

    try:
        children = list(directory.iterdir())
    except OSError:
        return 1
    for child in children:
        if child.name == _LOCK_FILE or child.name in _IDENTITY_FILES:
            continue
        # A linked entry is unlinked as a NAME, never followed: ``is_dir`` is
        # true through a link to a directory, and walking it would delete the
        # target's files. The caller refuses a linked ``items/`` before it gets
        # here; this is the same rule for every other entry.
        if child.name == _ITEMS_DIR and child.is_dir() and not is_link(child):
            try:
                entries = list(child.iterdir())
            except OSError:
                failures += 1
                continue
            for entry in entries:
                if entry.name.endswith(".lock"):
                    continue
                if entry.is_dir() and not is_link(entry):
                    shutil.rmtree(entry, onerror=_count)
                else:
                    try:
                        entry.unlink()
                    except OSError:
                        failures += 1
            continue
        if child.is_dir() and not is_link(child):
            shutil.rmtree(child, onerror=_count)
        else:
            try:
                child.unlink()
            except OSError:
                failures += 1
    return failures


def _remove_lock_shell(
    directory: Path, *, conductor_lock_gone: bool, item_locks_left: list[Path]
) -> None:
    """Remove the empty directories, and ONLY the lock files the holds could not.

    Call AFTER releasing. A lock path that was unlinked inside its hold is never
    touched again here: a writer that refused on the detached inode may already
    have retried, recreated the store and taken a FRESH lock at that path, and a
    second unlink would detach that fresh inode under its holder -- the next writer
    would take a third, un-serialised against the second. So this function
    receives exactly the paths the OS refused to unlink under the hold (Windows)
    and unlinks those alone, which is safe there because a late unlink fails
    whenever any writer holds a handle. The ``rmdir``s simply fail if a writer
    has rebuilt the store, and that is the correct outcome.
    """
    for lock_path in item_locks_left:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("work ledger purge: item lock still held; leaving it")
    try:
        (directory / _ITEMS_DIR).rmdir()
    except OSError:
        logger.debug("work ledger purge: items directory not fully removed")
    if not conductor_lock_gone:
        try:
            (directory / _LOCK_FILE).unlink(missing_ok=True)
        except OSError:
            logger.debug("work ledger purge: conductor lock still held; leaving it")
    try:
        directory.rmdir()
    except OSError:
        logger.debug("work ledger purge: ledger directory not fully removed")

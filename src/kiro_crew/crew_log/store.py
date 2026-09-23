"""The append-only crew log store: one file per unit, the gateway the only writer.

Layout, resolved against the live data home on every call (never captured at
import, so pod isolation and test isolation both keep working)::

    <data home>/crew-log/crews/<store name>/log.jsonl
    <data home>/crew-log/sessions/<store name>/log.jsonl
    <data home>/crew-log/members/<store name>/log.jsonl

``<store name>`` is the readable-plus-digest fold of the unit id that
``session_ledger`` and ``work_ledger`` already use, and the raw id lives in the
header (see :func:`crew_log_dir` for why the id is not the directory name). Both
files carry a ``.lock`` sibling in the same directory.

One dedicated ``crew-log`` root, holding every kind, is what carries the
protection: the leaf is hidden from a sandboxed process by the OS and fenced from
the agent's own file tools, and both fences are stated per leaf, so every kind
under this root inherits them and a new kind cannot be left out by omission. A
record a conductor is meant to trust as authority must not be forgeable by
anything that can call ``open()``, and a tool gate does not answer a subprocess --
which is why the session half does NOT live under the ``sessions`` transcript
root it would otherwise have shared.

Three properties are the whole design.

**Append only.** A line, once written, is never rewritten. There is exactly ONE
mutation: on ``open``, trailing bytes that are not a complete line are dropped.
Everything else -- a damaged interior line, an unknown envelope key, a type from
a newer writer -- is handled on the READ side by skipping or ignoring, never by
repairing the file. So two readers of the same bytes always agree, and a reader
is never the thing that changes history.

**Torn is decided by termination, not by taste.** Every append writes
``line + "\\n"`` and fsyncs, so a file that does not end in a newline was
interrupted mid-write. Those trailing bytes are the crash artifact and are
truncated -- unless they happen to parse whole, in which case only the newline
was lost: the record is kept and the next append re-supplies the separator. A
line that IS newline-terminated but does not parse is damage *inside* history,
so it is skipped on read and left on disk. Nothing else can be torn, which is
why this rule needs no heuristics.

**Seq comes from the file, under the lock.** ``seq`` is read back from the tail
inside the critical section on every append rather than trusted from the
in-process cache, so two writers cannot both believe they own the same number.
The read is a bounded window at the end of the file (``_TAIL_WINDOW``), not a
scan, so it costs the same on a crew log with ten lines and one with a million.
``.last_seq`` serves the cached value for callers that only want to look.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import traceback
import weakref
from collections import deque
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.crew_log.entry_types import validate_data
from kiro_crew.crew_log.errors import (
    CODE_ALREADY_EXISTS,
    CODE_ALREADY_OWNED,
    CODE_BAD_DATA,
    CODE_BAD_HEADER,
    CODE_BAD_ROOT,
    CODE_BAD_SEGMENT,
    CODE_BAD_THREAD,
    CODE_INVALID_ID,
    CODE_NO_LEDGER,
    CODE_SEGMENT_GAP,
    CODE_UNKNOWN_ENTRY_TYPE,
    CrewLogError,
    IndeterminateAppend,
)
from kiro_crew.crew_log.lease import LEASE_FILE
from kiro_crew.crew_log.lease import acquire as acquire_lease
from kiro_crew.crew_log.lease import release as release_lease
from kiro_crew.crew_log.schema import (
    KIND_CREW,
    KIND_MEMBER,
    KIND_SESSION,
    MAX_ENTRY_BYTES,
    Entry,
    Header,
    Ref,
    build_header,
    check_ownership,
    parse_header,
    require_data,
    require_entry_line,
    require_kind,
    require_unit_id,
    serialize,
)
from kiro_crew.jsonl_util import (
    UnreadableRecord,
    bounded_raw_records,
    strict_raw_records,
)
from kiro_crew.platform_compat import file_lock, restrict_dir_to_owner
from kiro_crew.session_ledger import _store_name, is_link, resolved_within, unlink_lock_in_hold

logger = logging.getLogger(__name__)

LOG_FILE = "log.jsonl"

#: How a segment past the first is named and found. ``log.jsonl`` holds seq 1
#: onward; ``log.<first_seq>.jsonl`` is a later segment.
_SEGMENT_STEM = "log"
_SEGMENT_SUFFIX = ".jsonl"
_SEGMENT_GLOB = f"{_SEGMENT_STEM}.*{_SEGMENT_SUFFIX}"
_LOCK_FILE = ".lock"

#: The one data-home leaf that holds every crew log, of every kind.
#:
#: A dedicated root rather than a subdirectory of each unit's existing home,
#: because the protection is what makes the record an authority: this leaf is
#: hidden from a sandboxed process by the OS (``sandbox._CREW_HIDDEN_LEAVES``)
#: and fenced from the agent's own file tools (``security.paths``), and both are
#: stated per LEAF. The ``sessions`` transcript root carries NEITHER entry, so a
#: session log placed there is protected by the tool gate alone and a
#: sandboxed subprocess can open it directly and forge the history a conductor
#: reads as fact. One root also means one entry per fence instead of one per
#: kind, so a third unit kind inherits the protection rather than needing a
#: reviewer to notice it was left out.
_ROOT_LEAF = "crew-log"

#: Directory under :data:`_ROOT_LEAF` that holds each kind's units.
_ROOT_DIR: dict[str, str] = {
    KIND_CREW: "crews",
    KIND_SESSION: "sessions",
    KIND_MEMBER: "members",
}

#: How much of the file's end a tail read covers. One maximum-size entry plus
#: slack, so the newest complete line is inside the window even when it is the
#: largest line the format allows.
_TAIL_WINDOW = 64 * 1024 + 8 * 1024

#: Largest page a single read may materialize.
MAX_PAGE_LIMIT = 500
DEFAULT_PAGE_LIMIT = 50

#: ``resolve`` outcomes. There is no ``forbidden``: this layer claims no
#: authorization, so it has none to deny (see :meth:`CrewLog.resolve`).
STATUS_OK = "ok"
STATUS_GONE = "gone"
#: The cited span reaches BELOW the oldest surviving segment: retention removed
#: it. A normal answer -- the citing entry stays honest about having pointed there.
STATUS_PRUNED = "pruned"
#: The cited span lies inside a segment that still exists, but lines in it could
#: not be read. That is DAMAGE, not retention, and it must never be reported as
#: either ``gone`` or ``pruned``: a reader told "retention" stops looking, while a
#: reader told "corrupt" knows the file it still has is not intact.
STATUS_CORRUPT = "corrupt"


def now_ms() -> int:
    """Epoch milliseconds, the clock every ``time`` and ``createdAt`` uses."""
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def crew_log_root(kind: str) -> Path:
    """Root directory holding every crew log of *kind*."""
    return data_home() / _ROOT_LEAF / _ROOT_DIR[require_kind(kind)]


def _checked_crew_log_root(kind: str) -> Path:
    """:func:`crew_log_root` for *kind*, refused when the directory is not the real one.

    Containment (:func:`resolved_within`) resolves its BASE first and then checks
    only that the child stays under the resolved base. That is the right rule for
    a child, and it is why the base itself has to be established separately: a
    linked kind root makes the link's target the containment root, so every path
    beneath it passes the check while living outside the tree whose protections
    the data home establishes -- readable, and writable by whoever owns the
    target.

    Two conditions, both cheap and both checked on every call rather than cached,
    because the directory can be replaced between calls by anything that can
    write the parent:

    * the kind directory is not itself a link -- ``lstat`` rather than ``stat``,
      so the answer is about the name and not about what it points at;
    * it resolves to exactly the canonical spelling under the data home's own
      ``crew-log`` tree, with both sides resolved so one symlinked ancestor cannot
      make two spellings of the same directory disagree.

    An absent directory is fine and is not an error: the callers that write
    create it, and the ones that read report their own absence. Only a directory
    that EXISTS and is wrong is refused.
    """
    root = crew_log_root(kind)
    try:
        is_link = root.is_symlink()
    except OSError as exc:  # pragma: no cover -- a stat fault on the parent
        raise CrewLogError(
            f"cannot establish the crew log root {root}: {exc}",
            code=CODE_BAD_ROOT,
        ) from exc
    if is_link:
        raise CrewLogError(
            f"refusing a linked crew log root: {root} is a symbolic link",
            code=CODE_BAD_ROOT,
        )
    if not root.exists():
        return root
    try:
        resolved = root.resolve()
        # Anchored at the data home, NOT at ``<home>/crew-log`` resolved: resolving
        # the parent would accept a linked ``crew-log`` -- both sides would then
        # spell the link's target and agree, which is the same escape one level up.
        canonical = data_home().resolve() / _ROOT_LEAF / _ROOT_DIR[require_kind(kind)]
    except (OSError, RuntimeError) as exc:
        raise CrewLogError(
            f"cannot resolve the crew log root {root}: {exc}",
            code=CODE_BAD_ROOT,
        ) from exc
    if resolved != canonical:
        raise CrewLogError(
            f"refusing a crew log root outside the data home: {root} resolves to {resolved}",
            code=CODE_BAD_ROOT,
        )
    return root


def crew_log_dir(kind: str, unit_id: str) -> Path:
    """The validated directory for one unit's crew log. Does not create it.

    The directory is named with the readable-plus-digest fold
    (``session_ledger._store_name``) rather than the raw id, and the raw id is
    persisted in the header instead. Two reasons, and the second is the load
    bearing one:

    * A legitimate id is not always a legitimate FILENAME. A channel session key
      carries a colon (``slack:1712793600.123``), which POSIX accepts and Windows
      refuses, so the raw id as a directory name turns a sanctioned id into an
      ``OSError`` on a supported platform.
    * Identity is the DIGEST over the exact id, so ``Foo`` and ``foo`` get
      distinct directories on a case-insensitive filesystem and two long ids
      sharing a prefix cannot land in one file. The readable half is a
      convenience for a human reading the directory listing and is capped for
      filesystem name limits; it is not the identity, and the fold is not
      reversible -- ``open`` proves it reached the right unit by checking the id
      the header stores.

    Containment is what makes the path safe regardless: the shape gate refuses a
    separator or a NUL in the raw id, and ``resolved_within`` re-checks
    symlink-safely that the resolved path stays under the root.
    """
    require_unit_id(unit_id)
    resolved = resolved_within(_checked_crew_log_root(kind), _store_name(unit_id))
    if resolved is None:
        raise CrewLogError(
            f"path traversal blocked for crew log id: {unit_id!r}",
            code=CODE_INVALID_ID,
            field="id",
        )
    return resolved


def crew_log_path(kind: str, unit_id: str) -> Path:
    """The crew log file for one unit."""
    return crew_log_dir(kind, unit_id) / LOG_FILE


def _segment_first_seq(path: Path) -> int | None:
    """The first seq declared by a canonical segment filename, or ``None``."""
    if path.name == LOG_FILE:
        return 1
    prefix = f"{_SEGMENT_STEM}."
    if not path.name.startswith(prefix) or not path.name.endswith(_SEGMENT_SUFFIX):
        return None
    middle = path.name[len(prefix) : -len(_SEGMENT_SUFFIX)]
    if not middle.isdigit() or int(middle) <= 1:
        return None
    return int(middle)


def segment_paths(kind: str, unit_id: str) -> list[Path]:
    """Every segment of one unit's crew log, in ascending first-seq order.

    Retention is deleting whole segments off the FRONT, which is why the format
    is segments rather than one growing file: dropping the oldest lines out of a
    single file would rewrite it, and this store's whole guarantee is that a
    written line is never rewritten. Deleting a segment leaves every remaining
    line byte-identical, so retention costs a reader the old entries and costs
    the format nothing.

    ``log.jsonl`` is the segment that starts at seq 1 and is the only one a
    writer creates today; a later segment is ``log.<first_seq>.jsonl``. The
    first-seq is IN the name so ordering needs no file read, and so a reader can
    tell a gap at the front (retention) from a gap in the middle (damage).
    """
    directory = crew_log_dir(kind, unit_id)
    found: list[tuple[int, Path]] = []
    head = directory / LOG_FILE
    if head.is_file():
        found.append((1, head))
    for candidate in directory.glob(_SEGMENT_GLOB):
        first = _segment_first_seq(candidate)
        if first is None:
            # Not a segment: a neighbour that merely shares the prefix. Ignored
            # rather than refused, so an editor's backup file cannot make a
            # readable crew log unreadable.
            continue
        found.append((first, candidate))
    found.sort(key=lambda pair: pair[0])
    return [path for _first, path in found]


def segment_first_seqs(kind: str, unit_id: str) -> list[int]:
    """The first seq of every surviving segment, ascending.

    The companion to :func:`segment_paths`, and the reason the first-seq is in the
    file NAME: telling a gap at the front from a gap in the middle needs only the
    directory listing, so the decision costs no file read and stays correct on a
    crew log too large to scan.
    """
    return [
        first
        for path in segment_paths(kind, unit_id)
        if (first := _segment_first_seq(path)) is not None
    ]


def _lock_path(kind: str, unit_id: str) -> Path:
    return crew_log_dir(kind, unit_id) / _LOCK_FILE


# --------------------------------------------------------------------------- #
# Removal
# --------------------------------------------------------------------------- #

#: :func:`remove_unit` outcomes. Stable strings, like ``resolve``'s, because a
#: caller has to tell them apart to report honestly: only ``removed`` may be
#: counted as a removal, and only ``failed`` is a problem.
REMOVE_REMOVED = "removed"
#: A writer owns the unit -- another process, or another handle in this one. The
#: unit is LIVE, so it is skipped; a later pass gets it once its writer is gone.
REMOVE_OWNED = "owned"
#: There was nothing to remove: no directory, or one that no id addresses.
REMOVE_ABSENT = "absent"
#: Something in the unit survived. Reported, never counted as removed, and the
#: unit is left identifiable so the next pass can aim at it again.
REMOVE_FAILED = "failed"


def remove_unit(kind: str, unit_id: str, *, guard: "Callable[[Path], bool]") -> str:
    """Remove one unit's crew log entirely. Returns one of the ``REMOVE_*`` statuses.

    The ONE spelling of deletion in this package, called by the retention sweep
    and by the permanent-delete funnel alike, for the reason the work crew log's
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
    named = _checked_crew_log_root(kind) / _store_name(unit_id)
    if is_link(named):
        logger.warning(
            "crew log retention: %s log %r is a link; refusing to remove what it names",
            kind,
            unit_id,
        )
        return REMOVE_ABSENT
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
            return REMOVE_FAILED
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
        # because the projection imports this module for the root and the replay.
        from kiro_crew.crew_log.session_tree_projection import forget_unit

        forget_unit(unit_id)
    return REMOVE_REMOVED


def _unit_header_object(kind: str, unit_id: str) -> "dict[str, Any] | None":
    """*unit_id*'s header line, parsed FROM DISK, or None when it cannot be proved.

    An independent second answer to "whose crew log is this", for a caller that
    reached the unit id through a channel it does not fully trust. The header is
    written once by the emitter at creation and never rewritten, and it is inside
    the fenced crew log tree, so it does not move when a mapping does.

    None for every reason a caller must not proceed on: no directory, no segment,
    an unreadable or unparseable header, or a header whose own id does not fold
    back to this directory (the same refusal the sweep makes, since a directory
    carrying another unit's id would answer for that other unit). None is "cannot
    prove", never "that field is absent", so a reader that requires a match
    refuses rather than guessing.

    Read-only: it takes no lease and writes nothing. A concurrent append cannot
    change a header, so what it reports stands for as long as that file lives.
    """
    require_kind(kind)
    try:
        named = _checked_crew_log_root(kind) / _store_name(unit_id)
        if is_link(named):
            return None
        directory = crew_log_dir(kind, unit_id)
    except (CrewLogError, OSError):
        return None
    return _proved_header(directory)


def _proved_header(directory: Path) -> "dict[str, Any] | None":
    """*directory*'s header, but only when the header's own id folds back to it.

    The half of :func:`unit_header_slot` that a caller which reached a store by
    PATH can use -- the slot scan walks the kind root and never learns an id until
    it reads one. Every reason to refuse is the same: a linked directory (it names
    somewhere else), a store with no segment, an unreadable or unparseable header,
    or a header whose ``id`` does not fold to this directory's name, which would
    make this store answer for a different unit.

    ``None`` is "cannot prove", never "no such field", so a caller that needs a
    value refuses rather than guessing. Read-only: it takes no lease.
    """
    try:
        if is_link(directory):
            return None
        segments = [
            (first, child)
            for child in directory.iterdir()
            if (first := _segment_first_seq(child)) is not None
        ]
    except OSError:
        return None
    if not segments:
        return None
    segments.sort(key=lambda pair: pair[0])
    try:
        raw_header = _read_header_line(segments[0][1])
        if raw_header is None:
            return None
        parsed = _parses_to_object(raw_header)
    except (OSError, ValueError):
        return None
    if not parsed:
        return None
    own_id = parsed.get("id")
    if not isinstance(own_id, str) or not own_id or _store_name(own_id) != directory.name:
        return None
    return parsed


def unit_header_slot(kind: str, unit_id: str) -> "str | None":
    """The slot recorded in *unit_id*'s HEADER, or None when it cannot be proved."""
    parsed = _unit_header_object(kind, unit_id)
    if parsed is None:
        return None
    slot = parsed.get("slot")
    return slot if isinstance(slot, str) and slot else None


def unprovable_session_units() -> int:
    """How many session-kind units under the root have a header that cannot be proved.

    A slot-keyed fold reaches its units through their headers, so a unit this
    cannot read is a unit no fold will see; a caller that must know its fold was
    complete asks this first.
    """
    try:
        root = _checked_crew_log_root(KIND_SESSION)
        names = sorted(child.name for child in root.iterdir())
    except (CrewLogError, OSError):
        return 0
    return sum(1 for name in names if _proved_header(root / name) is None)


#: The cached slot map, the root identity it was built from, and the children that
#: scan could NOT prove. Replaced WHOLE, so a reader loads one reference and sees
#: either the old triple or the new one; two threads racing rebuild it twice, which
#: costs a scan and cannot produce a wrong answer. ``None`` means nothing is cached.
_slot_index: "tuple[tuple[Any, ...], dict[str, tuple[str, ...]], tuple[str, ...]] | None" = None


def _slot_root_fingerprint(root: Path, names: "list[str]") -> "tuple[Any, ...]":
    """What must be unchanged for a cached slot map to still be current.

    A unit's slot NEVER changes -- the header is written once and never rewritten
    -- so the only thing that can invalidate the map is a unit appearing or
    disappearing, and both rename an entry in this directory.

    The NAMES are in the key, not just how many there are. A count plus an mtime
    cannot tell one set of children from another: a purge and a create landing inside
    one mtime granularity tick leave the count equal and the mtime unmoved, and the
    replacement unit's record would then stay invisible for as long as the directory
    sat still. The names are already read to do the scan, so carrying them costs the
    comparison and nothing else. The root's device and inode are in the key too, so a
    different data home (a pod, a test) never reads another one's map.
    """
    try:
        stat = root.stat()
    except OSError:
        return (str(root), None, None, tuple(names))
    return (str(root), stat.st_dev, stat.st_ino, stat.st_mtime_ns, tuple(names))


def session_units_for_slot(slot: str, *, strict: bool = False) -> "tuple[str, ...]":
    """Every session crew log whose HEADER names *slot*, oldest unit first.

    The slot-keyed read path. A slot owns one ACP session id AT A TIME rather than
    for its whole life -- a reset, an agent or model switch and a provider swap all
    cold-start a new id -- so one slot's history is spread across a unit per id it
    ran under, and a fold that answers for the SLOT has to join them. The header is
    what says which slot a unit belongs to: written once at create, never
    rewritten, and inside the fenced tree, so it does not move when a mapping does.
    A unit whose header cannot be PROVED to be its own is left out rather than
    attributed to a slot it may not belong to (see :func:`_proved_header`).

    *strict* is for a caller that VALIDATES against the listing rather than reading
    it, and it refuses on both ways the listing can come back incomplete: a scan that
    could not be made at all, and a child that cannot be proved while already holding
    entries (:func:`_unproven_holding_content`). Without it a read takes the shorter
    listing, which is the right answer for a read -- it says nothing false about what
    it could see -- and the wrong one for a write, which would validate against a
    record missing whatever that unit recorded.

    Ordered by the header's ``createdAt``, then by unit id so a tie is stable.
    That is the order the units were opened in, and therefore the order their
    entries happened in -- a fold applies a later update over an earlier one, so
    reversing it would let a retired session's state win over the live one.

    The per-unit header reads are CACHED against the root's identity, so a reader
    that folds on every loop cycle pays one scan per change to the set of units
    rather than one per read.
    """
    if not slot:
        return ()
    return session_units_by_slot(strict=strict).get(slot, ())


def session_units_by_slot(*, strict: bool = False) -> "dict[str, tuple[str, ...]]":
    """Every session crew log with a provable slot-naming header, grouped by slot.

    The index :func:`session_units_for_slot` looks one slot up in; a caller that
    must look ACROSS slots (a rebuild searching every other slot's units for entries
    naming its board) reads the whole map once instead of scanning per slot. Same
    order within a slot, same cache, same treatment of unprovable children.

    *strict* carries the validating caller's contract down to the scan that decides
    it, because the refusal belongs where the incompleteness is seen rather than
    where the listing is used. It means the same thing either way in: a scan that
    could not be made at all, and a child that cannot be proved while already
    holding entries, are both refused instead of being answered with a listing that
    is quietly short.
    """
    global _slot_index
    try:
        root = _checked_crew_log_root(KIND_SESSION)
        names = sorted(child.name for child in root.iterdir())
    except (CrewLogError, OSError):
        # No store, or one that could not be scanned. A read takes the empty listing;
        # a caller that would VALIDATE against the listing passes ``strict`` and gets
        # the failure instead, because an empty listing taken for a scan that failed
        # would let it validate against a record that is not there.
        if strict:
            raise
        return {}
    fingerprint = _slot_root_fingerprint(root, names)
    cached = _slot_index
    if cached is not None and cached[0] == fingerprint and not _any_now_provable(root, cached[2]):
        if strict:
            _refuse_unprovable_unit(root, cached[2])
        return cached[1]
    rows: "dict[str, list[tuple[int, str]]]" = {}
    unproven: list[str] = []
    for name in names:
        parsed = _proved_header(root / name)
        if parsed is None:
            # Kept, not forgotten. ``create`` makes the directory and PUBLISHES the
            # header as a second step, so a scan landing inside that window sees a
            # store with nothing to prove -- and the header then arrives INSIDE the
            # directory, which does not touch the root's mtime or child count. The
            # fingerprint alone would therefore stay valid over a map that is
            # missing a real unit, and the miss would outlive the window until some
            # unrelated child churn happened to invalidate it. Naming the
            # unprovable children is what bounds that: a cache hit re-checks just
            # those, which is nothing at all in the ordinary case.
            unproven.append(name)
            continue
        unit_slot = parsed.get("slot")
        unit_id = parsed.get("id")
        if not isinstance(unit_slot, str) or not unit_slot or not isinstance(unit_id, str):
            # Provable as a store but not attributable to a slot -- a session that
            # never ran on one. It is not a pending unit and cannot become one: the
            # header is written once. So it is not re-checked.
            continue
        created = parsed.get("createdAt")
        # A header with no usable ``createdAt`` still belongs to the slot, and
        # dropping it would silently lose that unit's updates. It sorts FIRST, the
        # position that lets any dated unit's later state win over it.
        order = created if isinstance(created, int) and not isinstance(created, bool) else 0
        rows.setdefault(unit_slot, []).append((order, unit_id))
    by_slot = {key: tuple(unit for _order, unit in sorted(found)) for key, found in rows.items()}
    _slot_index = (fingerprint, by_slot, tuple(unproven))
    if strict:
        _refuse_unprovable_unit(root, tuple(unproven))
    return by_slot


def _unproven_holding_content(root: Path, unproven: "tuple[str, ...]") -> "str | None":
    """The first child that cannot be proved AND already holds log content.

    What the strict listing needs beyond the scan. A child ``_proved_header`` could
    not prove is one of two different things, and only one of them is safe to leave
    out of the listing:

    * ``create`` has made the directory and not yet published the header. It holds no
      entries at all, so a listing without it is missing nothing that could be folded.
    * an established unit whose header will not read right now -- a transient
      ``OSError``, a link at the name, a segment that will not parse. It may hold any
      number of entries, so a caller that VALIDATES against the listing (the crew
      ledger's one-editor rule) would pass against a record missing them.

    "Holds content" is the same test ``create`` itself applies: the header is
    published atomically, so a partial one never reaches the name, and a zero-byte
    file "carries no header and no entries". A directory that will not answer at all
    is reported rather than guessed about -- whether it holds entries is exactly what
    could not be established.
    """
    for name in unproven:
        directory = root / name
        if is_link(directory):
            return name
        try:
            segments = [
                child for child in directory.iterdir() if _segment_first_seq(child) is not None
            ]
        except OSError:
            return name
        if any(_has_content(segment) for segment in segments):
            return name
    return None


def _refuse_unprovable_unit(root: Path, unproven: "tuple[str, ...]") -> None:
    """Raise when a strict listing cannot account for a child that holds entries."""
    blocked = _unproven_holding_content(root, unproven)
    if blocked is None:
        return
    raise CrewLogError(
        f"session crew log {blocked!r} holds entries its header cannot prove, "
        "so the listing is incomplete",
        code=CODE_BAD_HEADER,
        field="id",
    )


def _any_now_provable(root: Path, unproven: "tuple[str, ...]") -> bool:
    """Whether any child the last scan could not prove has since become a unit.

    The cache's second condition. The root fingerprint sees a child appear or
    disappear; it does NOT see a header published inside a directory that already
    existed, which is exactly what ``create`` does after its ``mkdir``. Re-checking
    the named children closes that window with work proportional to how many were
    unprovable -- normally none, so a cache hit stays a dictionary lookup.

    A child that is permanently unprovable (a stray directory, a link) is re-checked
    on every hit and never proves, which costs one header read per hit and is the
    price of never serving a stale map.
    """
    return any(_proved_header(root / name) is not None for name in unproven)


def unit_dir_for(kind: str, unit_id: str) -> "Path | None":
    """The unit directory for *unit_id* under *kind*'s root, or None.

    One ``stat``, no read. For the one reader that must find NAMED units ahead
    of the store's order (the session tree admits the live sessions' logs first,
    :mod:`kiro_crew.crew_log.session_tree`). None for an absent directory, a linked entry
    (it answers for a directory outside the tree), a root the store refuses
    (:func:`_checked_crew_log_root`) and an id that cannot be named; a caller
    reads None as "nothing to read here", never as an error.
    """
    require_kind(kind)
    try:
        named = _checked_crew_log_root(kind) / _store_name(unit_id)
        if is_link(named) or not named.is_dir():
            return None
        return named
    except (CrewLogError, OSError):
        return None


def unit_dirs(
    kind: str, *, limit: int, exclude: "Collection[str]" = ()
) -> tuple[list[Path], bool, bool]:
    """Up to *limit* unit directories under *kind*'s root, in the directory's own
    order, whether at least one more exists, and whether the listing FAILED.
    ``([], False, False)`` when there are none. A directory whose name is in
    *exclude* is neither listed nor counted: the caller already holds it.

    The enumeration for the one reader that looks ACROSS units (the session
    tree, :mod:`kiro_crew.crew_log.session_tree`). Bounded in WORK, not only in what it
    retains: the listing stops as soon as *limit* candidates are in hand and one
    more has been seen, so a root holding a million unit directories costs the
    caller *limit* + 1 candidate checks and never a walk of the million. The
    price is that nothing is said about how many more there are -- only THAT
    there are -- and that the admitted set follows the directory's iteration
    order rather than the names: for the tree that is the right trade, since
    the live sessions' logs are admitted by name ahead of this listing and the
    closed sessions' logs it lists do not decide anything a row on screen shows.

    An absent root lists nothing and is NOT a fault: a store with no root for
    this kind holds no units, and an answer without them is the whole truth. A
    root the store refuses, and one whose listing raises, both set the third
    value: those roots hold units this call cannot prove, and the difference
    between "none" and "none I could read" is the difference between a complete
    answer and a confident wrong one. The fault is reported from the read that
    failed rather than left to a caller's second look, which cannot see a
    failure that surfaced PART WAY through -- ``iterdir`` yields as it goes, so
    an error after the first entry escapes any probe that draws one entry and
    stops. Whatever was already in hand is returned WITH the fault rather than
    discarded: those directories were read, and the flag says the answer is
    short. A linked entry is skipped for the reason :func:`unit_header_slot`
    skips one -- it answers for a directory outside the tree.
    """
    excluded = frozenset(exclude)
    kept: list[Path] = []
    try:
        root = _checked_crew_log_root(kind)
        if not root.is_dir():
            return [], False, False
        for child in root.iterdir():
            if child.name in excluded or is_link(child) or not child.is_dir():
                continue
            if len(kept) >= max(0, limit):
                # One past the limit is all the caller needs to know.
                return kept, True, False
            kept.append(child)
    except (CrewLogError, OSError):
        return kept, False, True
    return kept, False, False


def oldest_segment(directory: Path) -> Path | None:
    """The surviving segment of *directory* with the lowest first seq, or ``None``.

    Where a unit's history starts TODAY: ``log.jsonl`` while it survives,
    otherwise the lowest-numbered later segment retention left behind. One
    ``stat`` in the common case: the head segment is checked by name before the
    directory is listed.
    """
    head = directory / LOG_FILE
    try:
        if head.is_file():
            return head
        found = [
            (first, child)
            for child in directory.iterdir()
            if (first := _segment_first_seq(child)) is not None
        ]
    except OSError:
        return None
    if not found:
        return None
    found.sort(key=lambda pair: pair[0])
    return found[0][1]


def newest_segment(directory: Path) -> Path | None:
    """The surviving segment of *directory* with the HIGHEST first seq, or ``None``.

    Where a unit's history ENDS today, which is the half :func:`oldest_segment`
    cannot answer: a decision recorded after the session opened lands at the end of
    the newest segment, not behind the header of the oldest one.

    No name shortcut. ``log.jsonl`` is the oldest segment while it survives, so the
    newest is only knowable from the listing -- and a unit that has never been
    rotated has exactly one segment, which the listing finds in the same call.

    Raises what the listing raises. ``None`` therefore means one thing -- no surviving
    segment -- rather than standing for an unreadable directory as well, which is the
    distinction the tree-edge caller decides on: a unit whose listing failed has not
    told anyone it records no decision, and reported as though it had, its stale
    checkpoint edge survives as the tree's answer.
    """
    found = [
        (first, child)
        for child in directory.iterdir()
        if (first := _segment_first_seq(child)) is not None
    ]
    if not found:
        return None
    found.sort(key=lambda pair: pair[0])
    return found[-1][1]


def read_head(path: Path) -> "tuple[dict[str, Any] | None, Entry | None, bool]":
    """Line 1 of *path* parsed as its header object, line 2 as its first entry,
    and whether a line 2 EXISTS at all.

    Header and entry are ``None`` when absent or when they cannot be delivered
    intact -- the abort posture of :func:`_read_header_line`, for the same
    reason: skipping a damaged line 1 would hand back an ENTRY as the header, a
    wrong answer rather than a missing one. The third value is what lets a
    caller tell two ``None`` entries apart: a header with NO line behind it is a
    normal transient (the emitter creates the file and appends
    ``session/opened`` in two writes, and a read can land between them), while
    a line that is there and does not parse is damage, and a caller that cached
    nothing for it would read it again forever.

    A line 2 with no terminator that does not parse is the transient, not the
    damage: the read landed inside the append, and the same bytes complete on
    the same inode a moment later. Reporting it as "no line behind the header
    yet" keeps a caller from caching a refusal it would otherwise hold for as
    long as the file exists. A terminated line that does not parse, and an
    unterminated one that does parse (the write landed short of its newline),
    are both final and reported as such.

    A file that cannot be opened or read raises the :class:`OSError` as is.
    Every ``None`` above is a verdict on bytes this function saw; a read that
    failed saw none, and reporting it as damage would let a caller cache a
    permanent refusal for a moment's I/O fault (or for a unit retention removed
    between the caller's ``stat`` and this open). The caller decides whether
    that is a retry next pass or a unit that is gone.
    """
    with open(path, "rb") as source:
        records = iter(strict_raw_records(source, path, cap=MAX_ENTRY_BYTES))
        try:
            raw_header = next(records, b"").strip()
        except UnreadableRecord:
            return None, None, False
        header = _parses_to_object(raw_header) if raw_header else None
        if not header:
            return None, None, False
        try:
            raw_entry = next(records, None)
        except UnreadableRecord:
            return header, None, True
    if raw_entry is None:
        return header, None, False
    parsed = _parses_to_object(raw_entry.strip())
    if parsed is None and not raw_entry.endswith((b"\n", b"\r")):
        # Mid-append: nothing final to report yet.
        return header, None, False
    entry = None if parsed is None else Entry.from_dict(parsed)
    return header, entry, True


def unit_header_created_at(kind: str, unit_id: str) -> "int | None":
    """*unit_id*'s creation stamp, read from the header ON DISK, or None.

    The stamp is written once at create and never rewritten, so it separates a
    RECREATED log from the one a reader started on -- including a recreation that
    landed on the freed inode, where device and inode alone report the two files as
    one. A caller holding an open handle cannot get this from the handle itself:
    that header was parsed when the handle was opened, so it describes the file
    that existed then and keeps answering for it after the file is replaced.

    None for a header that cannot be proved, and for a stamp that is absent or not
    an integer. None never matches a recorded identity, so an unprovable header
    costs a rebuild instead of licensing a reuse.
    """
    parsed = _unit_header_object(kind, unit_id)
    if parsed is None:
        return None
    created_at = parsed.get("createdAt")
    if not isinstance(created_at, int) or isinstance(created_at, bool):
        return None
    return created_at


def unit_ids(kind: str) -> list[str]:
    """Every unit id of *kind* that proves its own identity, sorted.

    The directory name is the readable-plus-digest fold and the fold is not
    reversible, so the id cannot be read off the listing -- it comes from each
    unit's HEADER, and only when that header's id folds back to the directory it
    was found in. That is the same refusal :func:`unit_header_slot` makes, for the
    same reason: a directory carrying another unit's id would otherwise be
    enumerated as that other unit.

    A directory that cannot be proved is SKIPPED rather than raising, because a
    caller listing units wants the ones it can act on -- one unreadable unit must
    not make the roster unlistable. An absent root is an empty list, not an error.
    """
    require_kind(kind)
    try:
        root = _checked_crew_log_root(kind)
        children = sorted(root.iterdir())
    except (CrewLogError, OSError):
        return []
    out: list[str] = []
    for child in children:
        try:
            if is_link(child) or not child.is_dir():
                continue
            segments = [
                (first, grandchild)
                for grandchild in child.iterdir()
                if (first := _segment_first_seq(grandchild)) is not None
            ]
            if not segments:
                continue
            segments.sort(key=lambda pair: pair[0])
            raw_header = _read_header_line(segments[0][1])
            if raw_header is None:
                continue
            parsed = _parses_to_object(raw_header)
        except (OSError, ValueError):
            continue
        if not parsed:
            continue
        own_id = parsed.get("id")
        if not isinstance(own_id, str) or not own_id:
            continue
        if _store_name(own_id) != child.name:
            continue
        out.append(own_id)
    return sorted(out)


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


def _is_segment_name(name: str) -> bool:
    """Whether *name* is segment-SHAPED, for ordering a removal.

    Deliberately broader than :func:`_segment_first_seq`, which answers the
    different question of whether a file is a canonical segment a reader may
    trust: that one rejects ``log.1.jsonl`` and ``log.0.jsonl``, because no
    real segment starts below seq 2. Removal must not inherit that rule. It
    deletes every file in the unit's directory whatever its name, and this
    predicate only decides what goes FIRST, so a stray numbered file is better
    grouped with the segments than left to the tail of the pass -- it is history
    by shape, and the ordering exists so the identity files outlive the history.
    """
    if name == LOG_FILE:
        return True
    if not (name.startswith(f"{_SEGMENT_STEM}.") and name.endswith(_SEGMENT_SUFFIX)):
        return False
    return name[len(_SEGMENT_STEM) + 1 : -len(_SEGMENT_SUFFIX)].isdigit()


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #

#: The teardown marker. A unit whose newest lifecycle entry is not this one is
#: OPEN, whatever its age.
_TYPE_SESSION_CLOSED = "session/closed"
#: The revival marker. It is written on create AND on a re-attach to an existing
#: conversation, so one appearing after a close means the session came back.
_TYPE_SESSION_OPENED = "session/opened"
#: The two entries that move a session between open and closed. Everything else --
#: a turn, a tool, an in-flight closer landing after a teardown -- says nothing
#: about which state the unit is in.
_LIFECYCLE_TYPES = frozenset({_TYPE_SESSION_OPENED, _TYPE_SESSION_CLOSED})

#: The ONE close reason that is positive proof the session's ACP id can never be
#: resumed: ``destroy`` deletes that id's mapping outright
#: (``_session_map.delete``), unconditionally, inside the registry lock and before
#: this entry is written. An id absent from the map cannot be resumed by anything.
#:
#: ``reset`` is deliberately NOT here, and the reason is worth stating because the
#: opposite reading is intuitive: a reset does cold-start its successor on a new
#: id, but its own ``clear_sid`` is guarded by ``if clear_conversation and session
#: is not None``, so a reset that keeps the conversation emits
#: ``session/closed {reset}`` while LEAVING the old id mapped -- still resumable,
#: and its log still needed. ``discarded`` clears the sid unconditionally and would
#: qualify on that test, but no path writes that reason into a crew log today, so
#: admitting it would be a rule about a file nothing produces. Every other reason --
#: a shutdown, a crash, an eviction, or a spelling this build does not know -- ends
#: the gateway's SERVICE of the session without ending the id's life.
#:
#: This reason is the whole authorization for deleting a unit, and it is read from
#: the crew log rather than from anything outside it. The obvious alternative -- ask
#: which sessions are still mapped in ``session_map.json`` and keep those -- was
#: implemented and then removed, because that file is agent-WRITABLE while this tree
#: is bind-masked, and a VALID EMPTY map is not a failed read: it reads as "nothing
#: is revivable" and hands a trusted sweep a positive answer that authorizes
#: deleting a fenced unit the writer of that file cannot touch directly. Absence of
#: protection must never be authorization.
_TERMINAL_CLOSE_REASONS = frozenset({"destroyed"})


def _is_terminal_close(entry: "Entry") -> bool:
    """Whether *entry*'s reason proves this session's id can never be resumed."""
    data = entry.data
    reason = data.get("reason") if isinstance(data, dict) else None
    return isinstance(reason, str) and reason in _TERMINAL_CLOSE_REASONS


def sweep_expired(retention_days: int, *, now: float | None = None) -> "tuple[int, int]":
    """Remove CLOSED session logs older than *retention_days*. ``(removed, failed)``.

    The same switch and the same sweep that already expire transcript archives:
    ``history._cleanup_old_archives`` calls this with the retention it resolved
    from ``session.archive_retention_days``, inside the hourly throttle it
    already has. A negative value disables both halves, so a user who turned
    archive expiry off has turned this off too and there is no second setting to
    find. Bodies live in these files now, which is why one switch has to govern
    both: expiring the transcript while its crew log grew forever is the gap this
    closes.

    A missing root, or one holding no unit directories, costs a directory listing
    and answers ``(0, 0)``. That is also what makes this de facto gated by
    ``KIROCREW_CREW_LOG`` without reading it: only the emitter creates
    session units, and the emitter is inert without the flag. Reading the flag
    HERE would be worse than not reading it -- turning it off would strand every
    crew log already written, permanently, since nothing else collects them.

    Crew logs are out of scope and are never scanned: this walks
    ``crew-log/sessions`` alone. They have no writer yet, and no ``session/closed``
    to age from, so a rule invented for them now would be a guess applied to
    files nothing produces.

    A unit is collectable only when its own log PROVES the session is finished:
    the newest lifecycle entry is a ``session/closed`` whose reason is one of
    :data:`_TERMINAL_CLOSE_REASONS`, meaning the gateway cleared that ACP id's
    mapping during the teardown so it can never be resumed. Nothing outside the
    crew log tree takes part in that decision -- see that constant for why the
    session map was tried for it and removed.
    """
    if retention_days < 0:
        return (0, 0)
    try:
        # The CHECKED root, the same one ``crew_log_dir`` resolves under. A linked or
        # out-of-home kind directory is refused here rather than one unit at a
        # time: the removal itself is already refused downstream, but only after
        # this walk had read a header and a tail from every file under whatever the
        # link named. One refusal reads nothing.
        children = list(_checked_crew_log_root(KIND_SESSION).iterdir())
    except CrewLogError:
        # A root that EXISTS and is wrong. Not a unit failure -- there is no
        # legitimate unit here to have failed -- so it is reported and the pass
        # ends rather than being counted as work.
        logger.warning("crew log retention: refusing to sweep the session log root", exc_info=True)
        return (0, 0)
    except OSError:
        # Includes the ordinary case of a root that was never created.
        return (0, 0)
    cutoff_ms = int(((time.time() if now is None else now) - retention_days * 86400) * 1000)
    removed = 0
    failed = 0
    for child in children:
        try:
            if is_link(child) or not child.is_dir():
                continue
            unit_id = _expired_unit_id(child, cutoff_ms)
            if unit_id is None:
                continue
            # Re-read the SAME decision inside the removal's own lease hold. The
            # scan above is a snapshot, and ownership ends between turns by
            # design, so a session can be revived, append its ``session/opened``,
            # finish its turn and release the lease in the window between the two
            # -- after which an unguarded removal would take a live conversation's
            # log while contending with nobody.
            status = remove_unit(
                KIND_SESSION, unit_id, guard=partial(_still_expired, cutoff_ms, unit_id)
            )
        except (CrewLogError, OSError):
            # One unreadable unit must not stop the pass over the others, and it
            # is not a removal: the unit keeps its history and the next pass sees
            # it again.
            logger.debug("crew log retention: skipped %s", child.name, exc_info=True)
            failed += 1
            continue
        if status == REMOVE_REMOVED:
            removed += 1
        elif status == REMOVE_FAILED:
            failed += 1
    if removed or failed:
        logger.info(
            "crew log retention: removed %d expired session log(s) (>%dd), %d could not be removed",
            removed,
            retention_days,
            failed,
        )
    return (removed, failed)


def _still_expired(cutoff_ms: int, expected: str, directory: Path) -> bool:
    """The sweep's re-decision, re-derived from the unit as it stands NOW.

    Bound to its first two arguments and handed to :func:`remove_unit` as the
    ``guard`` it calls under the lease. Re-deriving the ID as well as the age is
    deliberate: it refuses a directory that changed identity in the same window,
    so the removal can only proceed against the unit the scan actually chose.
    """
    return _expired_unit_id(directory, cutoff_ms) == expected


def _expired_unit_id(directory: Path, cutoff_ms: int) -> "str | None":
    """The raw id of the unit in *directory* when it is closed and expired.

    ``None`` means leave it alone, and every path to ``None`` is deliberate:

    * **No segment, or a header this directory does not answer to.** The id is
      read from the OLDEST surviving segment, which is where ``CrewLog.open``
      resolves it, and it is accepted only if it folds BACK to this directory's
      own name. A directory no id addresses is not removed, because the removal
      would be aimed by id and would resolve somewhere else.
    * **A torn tail.** Unterminated trailing bytes are what
      ``open(repair=True)`` truncates, and the sweep cannot tell a dead writer's
      crash artifact from an append that has not yet reached its fsync -- the
      bytes are identical. Deleting the unit would destroy the history the repair
      exists to recover, so the repair gets it and retention does not.
    * **The newest lifecycle entry is not a ``session/closed``.** An OPEN session
      is never touched, whatever its age, and this is the check that decides it.
      The pair ``session/opened`` / ``session/closed`` is what moves a unit
      between the two states, so the newest of the PAIR is the answer -- not the
      newest close on its own. A resumed session appends to the crew log it already
      had, so ``... closed ... opened ...`` is a legitimate file whose session is
      running right now. The read is bounded, so the deciding entry could in
      principle sit further back than the window -- but only entries written after
      it can push it out, and once a unit is closed the emitter writes nothing but
      a handful of in-flight closers unless the session was revived, which appends
      its own ``session/opened``. So a lifecycle entry outside the window means a
      live session, which is exactly the unit that must be kept: the bound fails
      closed rather than needing a full scan of every crew log on every pass.
    * **A segment that vanished mid-read.** Another remover -- the delete funnel,
      or a second pass -- got there first, which is the outcome this sweep wanted.
      It reads as "nothing to do" rather than as a failure, because the caller's
      failure tally is what an operator reads as "these units still hold
      history", and counting a race that already collected one would make that
      number a lie.

    The age comes from the LAST ``session/closed`` entry's own ``time``, falling
    back to the newest segment's mtime only when that field is unusable. The
    entry wins because it is the writer's own record of when the session ended
    and nothing rewrites it, while mtime is metadata a copy, a restore or a
    backup tool resets -- a restored tree would read as freshly closed and never
    expire. The fallback is kept rather than skipping the unit because a damaged
    ``time`` still proves the session ended, and mtime is then the best available
    bound on when writing stopped; it can only be at or after the real close, so
    it errs toward keeping the file.
    """
    try:
        segments = [
            (first, child)
            for child in directory.iterdir()
            if (first := _segment_first_seq(child)) is not None
        ]
    except OSError:
        return None
    if not segments:
        return None
    segments.sort(key=lambda pair: pair[0])
    oldest = segments[0][1]
    newest = segments[-1][1]
    try:
        raw_header = _read_header_line(oldest)
        if raw_header is None:
            return None
        parsed = _parses_to_object(raw_header)
        unit_id = parsed.get("id") if parsed else None
        if not isinstance(unit_id, str) or not unit_id:
            return None
        if _store_name(unit_id) != directory.name:
            return None
        tail = _scan_tail(newest)
    except FileNotFoundError:
        # A segment listed a moment ago is gone: another remover -- the delete
        # funnel, or a second pass -- got there first. That is the outcome this
        # sweep wanted, so it reads as "nothing to do" and is deliberately NOT
        # counted as a failure; the caller's ``failed`` tally is what an operator
        # reads as "these units still hold history", and a race that already
        # collected one would make that number a lie.
        return None
    except (OSError, ValueError):
        return None
    if tail.empty or tail.torn_offset is not None:
        return None
    closed = _last_lifecycle_entry(tail)
    if closed is None or closed.type != _TYPE_SESSION_CLOSED:
        return None
    if not _is_terminal_close(closed):
        return None
    closed_ms = closed.time
    if not isinstance(closed_ms, int) or closed_ms <= 0:
        try:
            closed_ms = int(newest.stat().st_mtime * 1000)
        except OSError:
            return None
    return unit_id if closed_ms < cutoff_ms else None


def _last_lifecycle_entry(tail: _Tail) -> "Entry | None":
    """The newest ``session/opened`` or ``session/closed`` in *tail*'s window.

    Whether a unit is closed is decided by the newest of the PAIR, not by the
    newest close on its own. A resumed session appends to the crew log it already
    had, so a file legitimately reads ``... closed ... opened ...`` -- one close
    followed by the revival that outlived it -- and a scan that stopped at the
    close would call a session that is running right now expired and delete a
    live conversation's log.

    :data:`_LIFECYCLE_TYPES` holds those two types and no others, and nothing that
    moves a session in the TREE belongs in it: this answer authorizes a DELETION,
    so a type added here makes a unit whose newest such entry is not a close
    immortal in retention. The tree's own types are read by
    :func:`read_last_tree_edge`, which shares the window walk below and keeps its
    own set.
    """
    return _last_entry_of_types(tail, _LIFECYCLE_TYPES)


#: The two entry types that move a session in the TREE, read from a unit's tail.
#: Deliberately NOT in :data:`_LIFECYCLE_TYPES`: that set decides open versus
#: closed and therefore authorizes retention's delete, so an adoption sitting at
#: the end of a log would keep that log forever.
_TREE_EDGE_TYPES = frozenset({"session/adopted", "session/released"})


def read_last_tree_edge(segment: Path) -> "Entry | None":
    """The newest tree-edge entry in *segment*'s tail window, or ``None``.

    An adoption or a release lands after the session opened, so a reader of a
    unit's HEAD cannot see it. This is the counterpart read, and it is the same
    bounded window retention already reads -- one open, at most ``_TAIL_WINDOW``
    bytes, no walk of the file.

    BOUNDED, and that bound is why this is not the whole answer: only entries written
    AFTER a decision can push it out of the window, and a session that keeps working
    keeps appending. :func:`find_last_tree_edge` is the complete read a cold rebuild
    needs; this one is the cheap first look it starts from, and the one a warm scan of a
    live unit uses because a live unit's decision is at its end.

    Raises whatever the read raises, like :func:`read_head`: a caller distinguishes
    "this unit has no decision" from "its bytes were not seen", and only the first
    is something to cache.
    """
    tail = _scan_tail(segment)
    if tail.empty:
        return None
    return _last_entry_of_types(tail, _TREE_EDGE_TYPES)


def tree_edge_scan_identity(directory: Path) -> "tuple[int, int, int, int] | None":
    """What must be unchanged for an earlier COMPLETE tree-edge read of this unit to
    still be its answer, or ``None`` when the unit has no segments.

    :func:`find_last_tree_edge` is the only honest read for a rebuild and it is
    proportional to the unit's whole log, because a unit that records no decision is
    only proved to record none by reading all of it. Paying that on every process start
    for every unit is what this exists to avoid: the verdict is a pure function of the
    bytes, so it can be cached across boots as long as something can say the bytes are
    the same ones.

    Four numbers, each closing a different way the answer could go stale:

    * the directory's own mtime, which moves when a segment is ADDED or REMOVED -- a
      rotation, or retention taking the oldest;
    * the number of segments, because two changes inside one mtime granularity can leave
      the directory looking untouched while its contents are not;
    * the newest segment's size and mtime, which move on an APPEND -- the one change that
      does not touch the directory at all, and the one that can add a decision.

    Deliberately NOT a content hash: this runs per unit on a boot path, and the whole
    point is to be cheaper than reading the bytes. It is a staleness check, not proof of
    identity -- so it is used only to skip re-deriving a NEGATIVE verdict, where being
    wrong costs a decision that is re-read on the next change, and never to admit an edge
    that no read produced.

    Raises what the stats raise. A caller that cannot get an identity must do the read.
    """
    newest: tuple[int, Path] | None = None
    count = 0
    for child in directory.iterdir():
        first = _segment_first_seq(child)
        if first is None:
            continue
        count += 1
        if newest is None or first > newest[0]:
            newest = (first, child)
    if newest is None:
        return None
    stat = newest[1].stat()
    return (directory.stat().st_mtime_ns, count, stat.st_size, stat.st_mtime_ns)


def find_last_tree_edge(directory: Path) -> "Entry | None":
    """The newest tree-edge entry in the WHOLE of *directory*'s surviving log, or
    ``None``.

    What a cold rebuild needs, and the reason it cannot use the tail window alone: a
    decision is silently lost when the log grew past that window after it was written,
    and the consequence of losing one is not a missing edge but a WRONG one -- the
    sidebar restores the creating edge and shows a session under a parent that gave it
    up, with nothing to correct it. A bounded read is the right cost for a live unit and
    the wrong answer for a rebuild that has no checkpoint to fall back on.

    Segments are searched NEWEST FIRST, and the first one that answers wins: a decision
    in a newer segment supersedes anything an older one holds, so the walk stops at the
    first hit rather than reading the rest. A unit that has never rotated therefore
    costs exactly what the tail read costs, which is the ordinary case; a rotated unit
    costs one read per segment until its newest decision is found, and a unit that
    records none costs one read per segment once.

    Inside a segment the search is still the windowed one for the LAST window, then the
    whole file when the window did not reach its start -- so a decision anywhere in a
    rotated log is found, and the common case pays the cheap read first.

    Raises what the reads raise, for the reason :func:`read_head` does: "no decision"
    and "the bytes were not seen" are different answers and only the first may be
    cached. Narrowed to ``OSError`` and ``ValueError``, which is what every caller here
    guards -- a reader exception outside those two would escape all of them and reach
    the projection's boot guard, which seeds no records at all and then refuses lineage
    for the rest of the process.
    """
    # NOT guarded. A listing that fails has not established that this unit records no
    # decision -- it has established nothing -- and the difference matters because both
    # callers CACHE the answer: the projection's seed would install the creating edge for
    # a session that was moved, and the scanner would store that as its verdict for the
    # segment's whole stat identity, so one moment's fault becomes the tree's standing
    # answer. Each caller already catches ``OSError`` and marks its scan incomplete,
    # which is the honest reading and the one the docstring above promises.
    found = [
        (first, child)
        for child in directory.iterdir()
        if (first := _segment_first_seq(child)) is not None
    ]
    if not found:
        return None
    found.sort(key=lambda pair: pair[0], reverse=True)
    for _first, segment in found:
        entry = read_last_tree_edge(segment)
        if entry is not None:
            return entry
        # The window did not answer. When it did not reach the start of the file there
        # are earlier lines in THIS segment it never saw, so they are read before moving
        # on to an older segment -- otherwise a decision in the middle of a busy log is
        # exactly what goes missing.
        entry = _scan_whole_for_types(segment, _TREE_EDGE_TYPES)
        if entry is not None:
            return entry
    return None


def _scan_whole_for_types(segment: Path, types: "frozenset[str]") -> "Entry | None":
    """The newest entry of *types* anywhere in *segment*, or ``None``.

    Only reached when the bounded window did not answer and could not prove it saw the
    whole file. It streams the records rather than holding the file, and keeps the last
    match instead of stopping at the first: the newest one is the answer, and a forward
    stream meets it last.

    Raises what the stat and the read raise, as ``OSError`` or ``ValueError`` and nothing
    else -- byte damage from the framing reader is converted below, because the callers
    that guard this one guard exactly those two. ``None`` therefore means the file holds
    no such entry, never that it could not be looked at -- the distinction the tree-edge
    caller turns into a CACHED verdict, which nothing afterwards re-reads or corrects.
    The ``open`` below already propagates, so guarding only the stat made one function
    answer two different ways about the same file.
    """
    size = segment.stat().st_size
    if size == 0:
        return None
    if size <= _TAIL_WINDOW:
        # The window already covered this whole file, so there is nothing new to read.
        return None
    newest: Entry | None = None
    with open(segment, "rb") as handle:
        try:
            for raw in strict_raw_records(handle, segment, cap=MAX_ENTRY_BYTES):
                parsed = _parses_to_object(raw.strip())
                if parsed is None:
                    continue
                entry = Entry.from_dict(parsed)
                if entry is not None and entry.type in types:
                    newest = entry
        except UnreadableRecord as exc:
            # Byte damage -- an over-cap record is the reachable case, and the framing
            # reader ABORTS on it, so everything after it in this file is unseen.
            # Reported as a failed READ rather than returned as ``newest``, because the
            # unseen part is where a later decision would be: answering with the newest
            # readable one would be a WRONG edge served as complete, which is the whole
            # failure this function exists to prevent.
            #
            # Converted to ``ValueError`` rather than propagated as-is. Every caller of
            # this file's tree-edge reads guards ``(OSError, ValueError)`` and turns a
            # raise into "incomplete"; ``UnreadableRecord`` is neither, so it escaped all
            # of them into the projection's own boot guard, which marks the projection
            # seeded with no records at all and refuses lineage for the process lifetime.
            # One conversion here, rather than a class added to three separate tuples
            # that would then have to stay in step.
            raise ValueError(f"crew log segment {segment.name} holds an unreadable record") from exc
    return newest


def _last_entry_of_types(tail: _Tail, types: "frozenset[str]") -> "Entry | None":
    """The newest entry in *tail*'s window whose type is in *types*.

    Searched from the END, so a file with many turns costs one comparison per
    trailing entry rather than a parse of the whole window. Entries outside *types*
    are skipped, and each caller brings its own set: the question "is this unit
    closed" and the question "where does this slot hang" are answered from the same
    bytes and must not share a vocabulary, because the first one authorizes a
    delete.

    Reuses the window the tail scan already read, so this costs no second read.
    """
    lines = tail.window.split(b"\n")
    if not tail.at_start:
        # The window may begin mid-line; that first fragment is not a record.
        lines = lines[1:]
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        parsed = _parses_to_object(stripped)
        if parsed is None:
            continue
        entry = Entry.from_dict(parsed)
        if entry is not None and entry.type in types:
            return entry
    return None


@contextmanager
def _open_lock(path: Path) -> Iterator[None]:
    """Hold the advisory lock on *path*, creating the lock file if absent.

    ``"r+"`` -- writable but NOT truncating -- for the reason ``work_ledger``
    documents: ``msvcrt.locking`` needs a writable handle, so ``"r"`` is out,
    while ``"w"`` truncates, and on Windows a truncating open of a file another
    process already holds locked raises a sharing violation instead of waiting.
    That would make the second contender crash before it reached the lock,
    defeating the serialization the lock exists for. ``file_lock`` itself fails
    closed, which is why nothing here has a lock-less fallback.
    """
    _mkdir_private(path.parent)
    path.touch(exist_ok=True)
    with open(path, "r+") as handle:
        with file_lock(handle.fileno(), exclusive=True):
            yield


# --------------------------------------------------------------------------- #
# Tail scan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Tail:
    """What a bounded read of the file's end says about its state.

    ``torn_offset`` and ``needs_newline`` are mutually exclusive: the trailing
    unterminated bytes either parse (the newline was lost, keep the record) or
    they do not (a crash artifact, drop it).

    ``window`` is the bytes that were read, already stripped of any torn tail,
    and ``at_start`` says whether they reach the beginning of the file. Both are
    carried so a caller that needs a SECOND answer about the tail -- does this
    seq exist -- can have it without a second read, and without this function
    parsing the whole window for a caller that does not ask.
    """

    last_seq: int
    torn_offset: int | None
    needs_newline: bool
    empty: bool
    window: bytes = b""
    at_start: bool = True


def _parses_to_object(blob: bytes) -> dict[str, Any] | None:
    """*blob* as a JSON object, or ``None``. Never raises."""
    try:
        parsed = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _seq_of(blob: bytes) -> int | None:
    """The ``seq`` of the entry *blob* encodes, or ``None`` when it has none."""
    parsed = _parses_to_object(blob)
    if parsed is None:
        return None
    entry = Entry.from_dict(parsed)
    return None if entry is None else entry.seq


def _scan_tail(path: Path) -> _Tail:
    """Read the end of *path* and report seq, torn bytes, and emptiness."""
    size = path.stat().st_size
    if size == 0:
        return _Tail(last_seq=0, torn_offset=None, needs_newline=False, empty=True)
    window = min(size, _TAIL_WINDOW)
    with open(path, "rb") as handle:
        handle.seek(size - window)
        blob = handle.read(window)
    at_start = window == size

    torn_offset: int | None = None
    needs_newline = False
    if not blob.endswith(b"\n"):
        cut = blob.rfind(b"\n")
        trailing = blob[cut + 1 :]
        if _parses_to_object(trailing) is not None:
            needs_newline = True
        else:
            torn_offset = size - len(trailing)
            blob = blob[: cut + 1]

    segments = blob.split(b"\n")
    if not at_start:
        # The window may begin mid-line; that first fragment is not a record.
        segments = segments[1:]
    last_seq = 0
    for segment in reversed(segments):
        stripped = segment.strip()
        if not stripped:
            continue
        seq = _seq_of(stripped)
        if seq is not None:
            last_seq = seq
            break
    if last_seq == 0 and not at_start:
        # Every line in the window was the header-less kind or damaged; only a
        # full scan can answer, and it is the rare path by construction.
        last_seq = _full_scan_last_seq(path)
    return _Tail(
        last_seq=last_seq,
        torn_offset=torn_offset,
        needs_newline=needs_newline,
        empty=False,
        window=blob,
        at_start=at_start,
    )


def _anchor_exists(path: Path, seq: int, tail: _Tail) -> bool:
    """Whether *seq* names a PARSEABLE entry in *path*.

    A thread pointing at a line no reader can parse is a pointer to nothing, so
    the range check ``1 <= seq <= last_seq`` is not enough on its own: a damaged
    interior line at exactly that seq is skipped by every reader, and the group
    would hang off an anchor that never appears.

    Bounded, in three steps, so proving this costs an ordinary append nothing:

    1. The window is already in memory, so an anchor inside it is free. A thread
       anchor is normally recent, which is exactly where that lands.
    2. If the window reached the start of the file, its answer is complete -- the
       whole file was examined.
    3. Only an anchor OLDER than the window falls back to a scan, and that scan
       stops AT the anchor instead of reading to the end.

    An append that passes no ``thread`` never calls this, so the bounded-tail
    cost of the ordinary write path is unchanged.
    """
    segments = tail.window.split(b"\n")
    if not tail.at_start:
        segments = segments[1:]
    for segment in segments:
        stripped = segment.strip()
        if stripped and _seq_of(stripped) == seq:
            return True
    if tail.at_start:
        return False
    for entry in _iter_entries(path):
        if entry.seq == seq:
            return True
        if entry.seq > seq:
            return False
    return False


def _full_scan_last_seq(path: Path) -> int:
    """The highest seq any parseable line carries. The fallback path only."""
    highest = 0
    for entry in _iter_entries(path):
        if entry.seq > highest:
            highest = entry.seq
    return highest


def _has_content(path: Path) -> bool:
    """Whether *path* holds any bytes. A zero-byte file reads as absent.

    ``create`` and ``open`` need the same answer: an empty file is not a crew log
    and never was one, so treating it as existing would strand a unit on a file
    that carries nothing to protect.
    """
    try:
        return path.stat().st_size > 0
    except FileNotFoundError:
        return False


def _iter_entries(path: Path) -> Iterator[Entry]:
    """Every parseable entry in *path*, oldest first, header excluded.

    A malformed interior line is SKIPPED, not raised on: the log is append-only
    and one damaged line must not hide the history in front of it. The file is
    streamed line by line, so a large crew log costs one line of memory, not its
    size.

    Decoding is STRICT and per line. Replacement-decoding would be the wrong
    kind of tolerance here: invalid bytes inside a JSON string can decode into
    still-valid JSON, so a damaged line would be yielded with silently altered
    values instead of skipped -- handing a consumer corrupted data as authority,
    which is precisely what a record that calls itself the authority must never
    do. Byte damage is this machinery's expected adversary, so an undecodable
    line is damage and is skipped exactly like unparseable JSON.

    The file is read in BINARY mode and framed by
    :func:`jsonl_util.bounded_raw_records`, so no universal-newline translation
    can rewrite the bytes on the way in, and one planted line cannot cost more
    than :data:`MAX_ENTRY_BYTES` of memory however long it is. That cap is the
    format's own write limit, so a longer line is not something this writer
    produced: it is damage, and the skip posture already applies to damage.
    """
    try:
        with open(path, "rb") as source:
            for index, raw in enumerate(
                bounded_raw_records(source, path, cap=MAX_ENTRY_BYTES, label="crew log")
            ):
                if index == 0:
                    continue  # the header
                stripped = raw.strip()
                if not stripped:
                    continue
                parsed = _parses_to_object(stripped)
                if parsed is None:
                    continue
                entry = Entry.from_dict(parsed)
                if entry is not None:
                    yield entry
    except FileNotFoundError:
        return


def _read_header_line(path: Path) -> bytes | None:
    """Line 1 of *path* as raw bytes, or ``None`` when there is no usable one.

    Bytes, not text: the caller decodes strictly and reports a header it cannot
    decode as ``bad_header`` rather than accepting a replacement-decoded one.

    Framed with the ABORT posture (:func:`jsonl_util.strict_raw_records`) where
    the entry read below uses the skip posture, and the difference is the point.
    Skipping an over-cap line 1 would hand back line 2 -- an ENTRY -- as though it
    were the header, which is a wrong answer rather than a missing one. Raising
    instead becomes ``None`` here, and ``None`` is what the caller reports as a
    bad header, so an unreadable line 1 fails closed on the identity of the unit.
    """
    try:
        with open(path, "rb") as source:
            for raw in strict_raw_records(source, path, cap=MAX_ENTRY_BYTES):
                return raw.strip()
    except FileNotFoundError:
        return None
    except UnreadableRecord:
        return None
    return None


def _read_first_entry_seq(path: Path) -> int | None:
    """Seq on the physical first entry line, or ``None`` when unreadable.

    Filename provenance applies only when that line carries a usable entry. A
    damaged first record has no sequence claim to compare and remains subject to
    the same per-record skip posture as any later damaged record.
    """
    try:
        with open(path, "rb") as source:
            records = iter(strict_raw_records(source, path, cap=MAX_ENTRY_BYTES))
            next(records, None)  # header
            raw = next(records, b"").strip()
    except FileNotFoundError:
        return None
    except UnreadableRecord:
        return None
    parsed = _parses_to_object(raw)
    entry = None if parsed is None else Entry.from_dict(parsed)
    return None if entry is None else entry.seq


# --------------------------------------------------------------------------- #
# Read results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Page:
    """One page of entries, NEWEST first.

    ``next_before`` is the cursor for the following page, or ``None`` when this
    page reached the oldest entry. It is set only when an older entry actually
    exists, so paging never hands back a phantom empty page at the end.
    """

    entries: tuple[Entry, ...]
    next_before: int | None


@dataclass(frozen=True)
class Resolution:
    """The outcome of following a :class:`~kiro_crew.crew_log.schema.Ref`."""

    status: str
    entries: tuple[Entry, ...]

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def _as_ref(ref: Ref | dict[str, Any]) -> Ref:
    """*ref* as a :class:`Ref`, accepting either spelling a caller may hand in.

    Every entry point that takes a ref accepts both a built ``Ref`` and its wire
    form, and coerces here, so the two spellings cannot disagree about what a
    valid ref is: ``Ref.from_dict`` runs the same ``__post_init__`` checks a
    direct constructor call goes through.
    """
    return ref if isinstance(ref, Ref) else Ref.from_dict(ref)


def _covers_span(found: tuple[Entry, ...], from_seq: int, last_seq: int) -> bool:
    """Whether *found* covers ``from_seq..last_seq`` whole, each seq exactly once.

    *found* MUST already be drawn from that span -- the caller's walk bounds it --
    so seq being contiguous inside a file makes the two counts settle it, and this
    does not re-check membership.

    The test is COVERAGE of the span, and a duplicate is damage in its own right.
    Seq is unique under the append lock, so two lines claiming one seq cannot both
    be the writer's -- and a tally of LINES would let that duplicate fill the place
    of a line that is gone, reading a span with a hole in it as intact. Counting
    distinct seqs catches that substitution; requiring the two counts to agree
    catches the duplicate even when nothing is missing, which is a file a reader
    must not be told is intact.
    """
    covered = {entry.seq for entry in found}
    return len(covered) >= max(0, last_seq - from_seq + 1) and len(covered) == len(found)


# --------------------------------------------------------------------------- #
# The crew log
# --------------------------------------------------------------------------- #


class CrewLog:
    """One unit's append-only crew log.

    Construct through :meth:`create` or :meth:`open`, never directly: both do
    the file-level work (existence, header, torn-tail repair, seq recovery) that
    the instance then assumes has happened.
    """

    __slots__ = (
        "_kind",
        "_id",
        "_path",
        "_header",
        "_last_seq",
        "_needs_newline",
        "_lease_key",
        # Weak-referenceable so this handle being dropped is what releases its
        # write ownership. See :meth:`_claim`.
        "__weakref__",
    )

    def __init__(
        self,
        *,
        kind: str,
        unit_id: str,
        path: Path,
        header: Header,
        last_seq: int,
        needs_newline: bool,
        lease_key: str | None = None,
    ) -> None:
        self._kind = kind
        self._id = unit_id
        self._path = path
        self._header = header
        self._last_seq = last_seq
        self._needs_newline = needs_newline
        self._lease_key: str | None = None
        if lease_key is not None:
            # A reference already taken on this handle's behalf -- the repair an
            # ``open`` ran before the instance existed. Adopting it here is what
            # binds its release to this handle's lifetime.
            self._adopt_lease(lease_key)

    # -- identity ----------------------------------------------------------- #

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def id(self) -> str:
        return self._id

    @property
    def path(self) -> Path:
        return self._path

    @property
    def header(self) -> Header:
        return self._header

    @property
    def last_seq(self) -> int:
        """The newest seq this instance knows of. Authoritative only for its own
        appends -- the file is re-read under the lock on every write."""
        return self._last_seq

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"CrewLog(kind={self._kind!r}, id={self._id!r}, last_seq={self._last_seq})"

    # -- lifecycle ---------------------------------------------------------- #

    @classmethod
    def exists(cls, kind: str, unit_id: str) -> bool:
        """Whether *unit_id* has a crew log with content. Raises on an invalid id.

        Empty means absent, the same answer ``create`` and ``open`` give, so all
        three agree about a file that carries nothing.
        """
        return any(_has_content(p) for p in segment_paths(kind, unit_id))

    @classmethod
    def create(cls, kind: str, unit_id: str, **header_fields: Any) -> CrewLog:
        """Write a new crew log's header. Refuses if the file already exists.

        The header is PUBLISHED atomically -- temp file, fsync, rename -- while
        every later line is a plain append. Creation is the one moment the file
        holds no history, so nothing is lost by writing it whole, and an
        interrupted create must not be able to leave a partial header behind: a
        short write or ENOSPC mid-header would otherwise wedge the unit forever,
        since ``open`` would read the fragment as a torn tail, truncate it to an
        empty file, and refuse -- while a retried ``create`` refused the very
        file it had just produced. After the rename the file is append-only for
        the rest of its life.

        A ZERO-BYTE file counts as absent for the same reason: it carries no
        header and no entries, so there is nothing to protect and refusing would
        only strand the unit. Anything longer is real content and is refused.

        Existence is checked twice, the second time under the lock: the first
        check is the cheap answer for the ordinary caller, the second is what
        makes "create refuses an existing crew log" true when two processes race.
        """
        kind = require_kind(kind)
        path = crew_log_path(kind, unit_id)
        if _has_content(path):
            raise CrewLogError(
                f"{kind} crew log {unit_id!r} already exists", code=CODE_ALREADY_EXISTS, field="id"
            )
        header = build_header(kind, unit_id, now_ms(), header_fields)
        line = require_entry_line(serialize(header.to_dict()))
        with _open_lock(_lock_path(kind, unit_id)):
            if _has_content(path):
                raise CrewLogError(
                    f"{kind} crew log {unit_id!r} already exists",
                    code=CODE_ALREADY_EXISTS,
                    field="id",
                )
            _mkdir_private(path.parent)
            atomic_write(path, f"{line}\n", fsync=True, newline="\n")
        return cls(
            kind=kind,
            unit_id=unit_id,
            path=path,
            header=header,
            last_seq=0,
            needs_newline=False,
        )

    @classmethod
    def open(
        cls,
        kind: str,
        unit_id: str,
        *,
        repair: bool = False,
        child_gone: "Callable[[str], bool] | None" = None,
    ) -> CrewLog:
        """Open an existing crew log, repairing a torn tail if there is one.

        ``child_gone`` narrows what ``repair`` is allowed to conclude, and is the
        only thing that lets it close an unmatched ``subagent/spawned``. A child
        outlives its asking turn by design, so an unmatched opener does NOT mean
        the child is finished -- a writer torn down inside a live process can leave
        one still running and still able to file its own real terminal, which would
        put two outcomes for one ``agent_id`` in a file nothing rewrites. No fact
        derivable from the file answers this, and no flag does either: what settles
        it is the live subagent registry of the process doing the repair, so the
        caller that holds one passes a predicate over ``agent_id``. Omitted, no
        child is closed -- the direction that leaves a reader behind rather than
        wrong.

        A zero-byte file answers ``no_ledger``, the same answer ``create`` gives
        it. One meaning for an empty file across both paths is what keeps them
        from disagreeing: a create that could not finish leaves nothing behind,
        and a retry succeeds instead of being refused by the fragment.

        ``repair`` is what closes an INTERRUPTED TURN, and it is opt-in because
        the two callers of ``open`` want opposite things. A RESUME -- the gateway
        finding a session log whose writer is gone -- wants the open turn
        closed, and passes ``repair=True``. A live writer RECONNECTING to its own
        crew log must not: its turn is still running, and closing it would append a
        ``turn/completed {interrupted}`` in the middle of a turn that then keeps
        writing, so the record would claim an outcome the turn never had. A
        reconnect happens for reasons that have nothing to do with the writer's
        health -- a handle evicted from a bounded cache is enough -- so repair
        cannot be inferred from the fact that an ``open`` is happening at all. See
        :func:`_close_interrupted_tail`; the torn-tail truncation below is
        unconditional because trailing bytes that are not a complete line are not
        a record, so nothing can be reading them.
        """
        kind = require_kind(kind)
        # The NEWEST segment is the one a writer appends to, and with nothing
        # rotating yet that is ``log.jsonl`` in every crew log that exists. Taking
        # it from the segment list rather than the fixed name is what lets a log
        # whose oldest segment was pruned still open: retention removes files off
        # the front, and the header travels on each segment so the survivor carries
        # it.
        segments = [
            candidate for candidate in segment_paths(kind, unit_id) if _has_content(candidate)
        ]
        if not segments:
            raise CrewLogError(
                f"no {kind} crew log for {unit_id!r}", code=CODE_NO_LEDGER, field="id"
            )
        path = segments[-1]
        header_path = segments[0]
        with _open_lock(_lock_path(kind, unit_id)):
            tail = _scan_tail(path)
            if tail.torn_offset is not None:
                dropped = path.stat().st_size - tail.torn_offset
                _truncate(path, tail.torn_offset)
                logger.warning(
                    "dropped %d torn trailing byte(s) from %s crew log %r",
                    dropped,
                    kind,
                    unit_id,
                )
            raw = _read_header_line(header_path)
        parsed = None if not raw else _parses_to_object(raw)
        if parsed is None:
            raise CrewLogError(
                f"{kind} crew log {unit_id!r} has no readable header line",
                code=CODE_BAD_HEADER,
                field="type",
            )
        header = parse_header(parsed, kind=kind, unit_id=unit_id)
        lease_key: str | None = None
        if repair:
            # Ownership BEFORE the closers, and the reason repair is the one thing
            # ``open`` claims it for: the closers are appends, and a repair running
            # while a writer in ANOTHER process still holds the turn is what makes
            # the file state two outcomes for one turn. Refused here, this open
            # raises and nothing is written -- the live writer's log is left as it
            # stands.
            _mkdir_private(path.parent)
            lease_key = acquire_lease(path.parent / LEASE_FILE, kind=kind, unit_id=unit_id)
            try:
                if _close_interrupted_tail(kind, unit_id, path, child_gone=child_gone):
                    # The closers moved the tail, so this object's cached seq has to
                    # be re-read or its first append would collide with them.
                    tail = _scan_tail(path)
            except BaseException:
                release_lease(lease_key)
                raise
        return cls(
            kind=kind,
            unit_id=unit_id,
            path=path,
            header=header,
            last_seq=tail.last_seq,
            needs_newline=tail.needs_newline,
            lease_key=lease_key,
        )

    def repair_interrupted_turn(self, *, child_gone: "Callable[[str], bool] | None" = None) -> int:
        """Close an open turn on this crew log. Returns how many closers landed.

        The method form of ``open(repair=True)``, for a caller that already holds
        a handle. Same rule: only a resume calls it, never a live writer. And the
        same ``child_gone`` meaning: without a predicate an unmatched
        ``subagent/spawned`` is left open, because nothing else can rule out a live
        child that is still about to report its own outcome.
        """
        # The closers are appends, so this takes write ownership exactly as one
        # does, and is refused the same way when another process holds the log.
        self._claim()
        written = _close_interrupted_tail(self._kind, self._id, self._path, child_gone=child_gone)
        # Refreshed unconditionally: the repair also TRUNCATES an unreachable chunk
        # group, which changes the tail without writing a closer, and a cached
        # ``last_seq`` past the end of the file would be served to a reader that only
        # wants to look.
        tail = _scan_tail(self._path)
        self._last_seq = tail.last_seq
        self._needs_newline = tail.needs_newline
        return written

    # -- write ownership ---------------------------------------------------- #

    def _claim(self) -> None:
        """Take write ownership of this unit before the first write of this handle.

        Lazy, and never taken by ``open`` itself, because ``open`` also serves
        READERS: ``iter_from``, ``page`` and ``resolve`` need no ownership, and
        making a reader contend with the writer would be a regression in exchange
        for nothing. The first write is the moment ownership starts to mean
        something, so that is where it is claimed.

        Raises the ``already_owned`` refusal when another PROCESS owns the log.
        Another handle in THIS process is not contention -- it shares the same
        kernel lock through the reference count -- which is what lets the emitter's
        cached handle and a session claim's handle overlap.
        """
        if self._lease_key is not None:
            return
        # The directory of the segment this handle appends to, not one resolved
        # from the data home again: a handle writes the file it was opened on, and
        # the lock has to name that file even if the home was repointed since.
        _mkdir_private(self._path.parent)
        self._adopt_lease(
            acquire_lease(self._path.parent / LEASE_FILE, kind=self._kind, unit_id=self._id)
        )

    def _adopt_lease(self, key: str) -> None:
        """Bind an acquired lease reference to this handle's lifetime.

        Released when this object is dropped, so a holder keeps ownership for
        exactly as long as it can still append through it. That places one
        requirement on whatever holds a handle: it must not drop one belonging to
        a LIVE turn, or ownership ends while that turn is still producing entries
        and a successor's repair closes a turn that completes for real a moment
        later. Both of the emitter's release paths honour it -- capacity eviction
        and session teardown each skip a session with a live turn -- so ownership
        ends between turns, where there is no live turn for a repair to damage.

        Binding release to the object rather than to an explicit call is also what
        keeps a queued write that still holds the handle inside the ownership it
        needs: an eager release at eviction would take it out from under a job
        that is about to append.
        """
        self._lease_key = key
        weakref.finalize(self, release_lease, key)

    # -- write -------------------------------------------------------------- #

    def append(
        self,
        type: str,
        data: dict[str, Any],
        *,
        src: str,
        thread: int | None = None,
        ref: Ref | dict[str, Any] | None = None,
        ignorable: bool = False,
    ) -> Entry:
        """Append one entry and return it, with ``seq`` and ``time`` filled in.

        Every refusal happens before any byte is written, so a rejected append
        leaves the file identical. ``thread`` is checked against the seq read
        back under the lock, which is also what assigns this entry's own seq.

        ``ignorable`` promises that nothing later in the file depends on this
        entry being understood, so a reader that does not know the type may skip
        it instead of stopping. Only the writer can make that promise -- it is
        the one that knows whether the entry is a sample or a fact -- which is
        why it lives on the append and not on the read.
        """
        require_data(data)
        check_ownership(self._kind, type, src)
        validate_data(self._kind, type, data)
        pointer = None if ref is None else _as_ref(ref)
        if thread is not None and (
            not isinstance(thread, int) or isinstance(thread, bool) or thread < 1
        ):
            raise CrewLogError(
                f"thread must be a positive seq in this crew log: {thread!r}",
                code=CODE_BAD_THREAD,
                field="thread",
            )
        # After the format checks, so a malformed entry is refused without this
        # process claiming anything, and before the write lock, so ownership is
        # settled before any byte is at stake.
        self._claim()
        with _open_lock(_lock_path(self._kind, self._id)):
            tail = _scan_tail(self._path)
            if tail.empty:
                raise CrewLogError(
                    f"no {self._kind} crew log for {self._id!r}",
                    code=CODE_NO_LEDGER,
                    field="id",
                )
            if tail.torn_offset is not None:
                _truncate(self._path, tail.torn_offset)
            if thread is not None and (
                thread > tail.last_seq or not _anchor_exists(self._path, thread, tail)
            ):
                raise CrewLogError(
                    f"thread {thread} names no parseable entry in this crew log "
                    f"(newest seq is {tail.last_seq})",
                    code=CODE_BAD_THREAD,
                    field="thread",
                )
            entry = Entry(
                type=type,
                seq=tail.last_seq + 1,
                time=now_ms(),
                src=src,
                data=data,
                thread=thread,
                ref=pointer,
                ignorable=bool(ignorable),
            )
            line = require_entry_line(serialize(entry.to_dict()))
            _append_line(self._path, line, needs_newline=tail.needs_newline)
        self._last_seq = entry.seq
        self._needs_newline = False
        return entry

    def append_many(
        self,
        items: "list[dict[str, Any]]",
        *,
        src: str,
        cite: "Callable[[list[int]], dict[str, Any]] | None" = None,
    ) -> "list[Entry]":
        """Append a GROUP of entries as one write, and return them.

        For a group whose members are meaningless apart: a body too large for one
        line becomes several ``message/chunk`` entries plus the entry that CITES
        their seqs, and appending those one at a time leaves a window where a hard
        kill puts the chunks on disk with nothing pointing at them -- the body is
        stored and unreachable, and the citing entry that would have explained it
        never exists. Written together, a crash leaves either the whole group or a
        torn tail, and the tail is what the next append truncates.

        *cite* is how a citation is made unable to disagree with the allocation. The
        caller passes ONLY the cited entries in *items* and a callable; this method
        calls it INSIDE the lock, with the seqs it has just allocated for those
        entries, and appends what it returns as the group's last member. Allocating
        outside the lock and citing the result cannot be made correct: the lock is a
        cross-process one, so between a caller's read of the tail and this method's
        read another handle -- a resumed session, a second process -- can append and
        shift the whole run, after which the citation names seqs belonging to the
        intruder. Nothing detects that later: the seqs exist and parse.

        Every refusal still happens before any byte is written -- each item is
        validated up front, and a *cite* result is validated before the write it
        joins -- so a rejected group leaves the file identical. One lock, one
        ``write()``, one ``fsync``.

        ``thread`` is deliberately not accepted here. A group is self-contained by
        construction, and an anchor check per member would have to re-read the tail
        it is being allocated from.
        """
        if not items:
            return []
        for item in items:
            # ``item["data"]`` itself, not ``item.get("data") or {}``: the fallback
            # made a MISSING key and a falsey non-dict both validate as an empty
            # object, so a caller that forgot the body -- or passed None -- would
            # have its entry written with no data at all. A group is refused whole,
            # and this is where that promise is kept.
            require_data(item.get("data"))
            check_ownership(self._kind, str(item.get("type") or ""), src)
            validate_data(self._kind, str(item.get("type") or ""), item.get("data"))
        self._claim()
        with _open_lock(_lock_path(self._kind, self._id)):
            tail = _scan_tail(self._path)
            if tail.empty:
                raise CrewLogError(
                    f"no {self._kind} crew log for {self._id!r}",
                    code=CODE_NO_LEDGER,
                    field="id",
                )
            if tail.torn_offset is not None:
                _truncate(self._path, tail.torn_offset)
                tail = _scan_tail(self._path)
            members = items
            if cite is not None:
                # The seqs the cited entries are about to get, from the allocation
                # this call is committing to -- not from a read the caller made
                # earlier and hoped still held.
                allocated = [tail.last_seq + 1 + offset for offset in range(len(items))]
                citing = cite(allocated)
                # Refused, not raised through. An entry shape this cannot use is a
                # deterministic caller bug, and letting it surface as a TypeError or
                # an AttributeError would put it in the "might clear" class the
                # write-behind RETRIES -- spending the whole attempt budget, and
                # holding every later entry of this session behind it, on a verdict
                # that cannot change. A refusal is a loss now, which is the honest
                # outcome and the one the retention policy already handles.
                if not isinstance(citing, dict):
                    raise CrewLogError(
                        f"cite must return an entry mapping, got {type(citing).__name__}",
                        code=CODE_BAD_DATA,
                        field="cite",
                    )
                require_data(citing.get("data"))
                check_ownership(self._kind, str(citing.get("type") or ""), src)
                validate_data(self._kind, str(citing.get("type") or ""), citing.get("data"))
                # LAST, so a torn tail can only cost the citing entry -- the orphan
                # shape the repair drops -- never a chunk it already named.
                members = [*items, citing]
            stamped = now_ms()
            entries: "list[Entry]" = []
            for offset, item in enumerate(members):
                pointer = item.get("ref")
                entries.append(
                    Entry(
                        type=str(item["type"]),
                        seq=tail.last_seq + 1 + offset,
                        time=stamped,
                        src=src,
                        data=item["data"],
                        thread=None,
                        ref=None if pointer is None else _as_ref(pointer),
                        ignorable=bool(item.get("ignorable")),
                    )
                )
            lines = [require_entry_line(serialize(entry.to_dict())) for entry in entries]
            _append_lines(self._path, lines, needs_newline=tail.needs_newline)
        self._last_seq = entries[-1].seq
        self._needs_newline = False
        return entries

    # -- read --------------------------------------------------------------- #

    def raw_prefix_digest(self, records: int) -> tuple[str, int]:
        """SHA-256 of the first *records* raw entry records, and count hashed.

        Segments are walked oldest first with the store's normal binary framing.
        Each segment's header is excluded: log identity is checked separately,
        while this digest proves that the entry bytes already consumed by a fold
        have not changed. No JSON is decoded on this path.

        *records* is a RAW record count and must come from
        :meth:`raw_records_through`, never from a fold's entry span. The two differ
        by every blank or unparseable interior line, and an entry span therefore
        stops this walk short of the records the fold actually consumed.

        Never raises. An invalid count, unreadable segment, framing failure, or
        prefix shorter than requested returns a count other than *records*, which
        makes a savepoint comparison fail closed.
        """
        if not isinstance(records, int) or isinstance(records, bool) or records < 0:
            return ("", 0)
        digest = hashlib.sha256()
        hashed = 0
        if records == 0:
            return (digest.hexdigest(), hashed)
        try:
            paths = segment_paths(self._kind, self._id)
            for path in paths:
                with open(path, "rb") as source:
                    for index, raw in enumerate(
                        bounded_raw_records(source, path, cap=MAX_ENTRY_BYTES, label="crew log")
                    ):
                        if index == 0:
                            continue
                        digest.update(raw)
                        hashed += 1
                        if hashed == records:
                            return (digest.hexdigest(), hashed)
        except Exception:
            log_exception_text(
                logger, logging.DEBUG, "crew log raw prefix for %s could not be read", self._id
            )
        return (digest.hexdigest(), hashed)

    def raw_records_through(self, seq: int) -> int | None:
        """How many raw entry records end at the entry numbered *seq*, or ``None``.

        This is the boundary :meth:`raw_prefix_digest` has to be given, and it
        exists because the two ways of measuring "how much of this file did a fold
        consume" are NOT the same number. A fold counts ENTRIES, so its span is
        ``last_seq - first_seq + 1``. The digest walks RAW records, and a blank or
        unparseable interior line is a record to that walk while the fold skips it.
        Handed the entry span, the walk therefore stops one record short per such
        line, and the trailing records a fold did consume fall outside the digest --
        where later damage to them passes verification, which is the one thing the
        digest exists to prevent.

        So the boundary is resolved here, once, by the same walk the digest uses,
        and the record's own ``seq`` decides where it lies. That costs a JSON decode
        per record, which is why only the WRITE path calls it: a savepoint is
        written rarely, and the reader is handed the resolved count so its own
        verification stays decode-free.

        ``None`` when *seq* is not a real boundary in this log: a bad argument, an
        unreadable or unframeable segment, or a file whose records do not reach it.
        A savepoint is then not written, which costs a cold fold rather than
        recording a boundary nothing can check.
        """
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            return None
        seen = 0
        try:
            for path in segment_paths(self._kind, self._id):
                with open(path, "rb") as source:
                    for index, raw in enumerate(
                        bounded_raw_records(source, path, cap=MAX_ENTRY_BYTES, label="crew log")
                    ):
                        if index == 0:
                            continue  # the header
                        seen += 1
                        stripped = raw.strip()
                        if not stripped:
                            continue
                        parsed = _parses_to_object(stripped)
                        if parsed is None:
                            continue
                        own = parsed.get("seq")
                        if not isinstance(own, int) or isinstance(own, bool):
                            continue
                        if own == seq:
                            return seen
                        if own > seq:
                            # Past the boundary without landing on it, so the entry
                            # is not in this log and no count describes it.
                            return None
        except Exception:
            log_exception_text(
                logger, logging.DEBUG, "crew log prefix boundary for %s could not be read", self._id
            )
            return None
        return None

    def iter_from(
        self,
        seq: int = 1,
        *,
        known: Collection[str] | None = None,
        strict_seq: bool = True,
    ) -> Iterator[Entry]:
        """Every entry from *seq* onward, OLDEST first -- the shape a fold wants.

        *known* is the reader DECLARING the types it can interpret, and passing
        it is what turns this read into a reconstruction rather than a listing.
        Then an entry whose type is not in *known* either is skipped, when the
        writer marked it ``ignorable``, or raises ``unknown_entry_type`` when it
        did not: a required entry the reader cannot interpret may change the
        meaning of every entry after it, so a fold that continued past one would
        produce a confident wrong answer instead of an admitted failure.

        The default, ``None``, means "I understand everything" and is exactly the
        behaviour every caller had before the marker existed -- no refusal, every
        entry yielded. The gate is opt-in because only a caller that FOLDS state
        is harmed by a line it skipped; :meth:`page` deliberately has no such
        parameter, since paging renders history for a human and showing an
        unfamiliar line is not a wrong answer.

        Reads across SEGMENTS, oldest first, and requires seq to stay contiguous
        over each boundary -- a gap between segments is damage or a partial copy,
        and yielding across it would hand a fold a hole it cannot see. A gap at the
        FRONT is not damage: that is retention, so a first segment starting above 1
        is read as it stands.

        Every WALKED entry -- including the ones below *seq* that are never
        yielded -- must carry a seq strictly greater than the entry before it.
        A duplicate or backward seq is refused with ``bad_data``, the same
        verdict :func:`~kiro_crew.crew_log.projection.advance` gives it, so an
        incremental read and a fold from the start agree on a damaged file
        instead of one refusing and the other silently skipping the record.
        The append-only writer cannot produce a non-advancing seq, so one in
        the file is external damage, not history. A forward GAP stays
        tolerated: inside one file it is a damaged line ``_iter_entries``
        skipped on purpose, and this check deliberately does not harden into
        a contiguity requirement.

        *strict_seq* is that refusal, and ``False`` is for the RENDERING
        callers only: a page shows history to a human, so refusing every
        intact line of a unit because one damaged line exists elsewhere in it
        would take the history away exactly when damage makes it most worth
        reading. A caller that FOLDS state must keep the default -- tolerating
        a non-advancing seq there is how two reads of the same bytes disagree,
        which is the defect this parameter's default closes.
        """
        walked = 0
        for entry in self._iter_segments():
            if entry.seq <= walked:
                if strict_seq:
                    raise CrewLogError(
                        f"entry {entry.seq} is at or below the previously read "
                        f"entry's seq {walked}; a duplicate or backward seq is "
                        "damage the append-only writer cannot produce",
                        code=CODE_BAD_DATA,
                        field="seq",
                    )
            else:
                walked = entry.seq
            if entry.seq < seq:
                continue
            if known is not None and entry.type not in known:
                if not entry.ignorable:
                    raise CrewLogError(
                        f"entry {entry.seq} has type {entry.type!r}, which this reader "
                        "does not know and which is not marked ignorable; "
                        "reconstruction stops here",
                        code=CODE_UNKNOWN_ENTRY_TYPE,
                        field="type",
                    )
                continue
            yield entry

    def _iter_segments(self) -> Iterator[Entry]:
        """Every entry of every segment, oldest first, refusing bad provenance.

        Every segment repeats the header because retention may remove the first one.
        Each header must therefore identify this same crew log and schema before any
        entry from that independent file becomes authoritative. The filename's first
        seq is a second provenance claim and must agree with the physical first entry
        when that record is readable.

        Seq continuity remains a boundary-only check. Inside one file a missing seq
        means a damaged line, which ``_iter_entries`` skips on purpose -- one
        unreadable record must not make the rest of the file unreadable. Across two
        files a gap is a different thing: a half-finished copy or deleted middle
        segment is invisible unless the boundary is checked.
        """
        expected = 0
        for index, path in enumerate(segment_paths(self._kind, self._id)):
            raw_header = _read_header_line(path)
            parsed_header = None if not raw_header else _parses_to_object(raw_header)
            try:
                segment_header = parse_header(
                    parsed_header,
                    kind=self._kind,
                    unit_id=self._id,
                )
            except CrewLogError as exc:
                raise CrewLogError(
                    f"segment {path.name} has an invalid header: {exc}",
                    code=CODE_BAD_SEGMENT,
                    field=exc.field,
                ) from exc
            if segment_header.version != self._header.version:
                raise CrewLogError(
                    f"segment {path.name} has schema version {segment_header.version}, "
                    f"but this crew log uses {self._header.version}",
                    code=CODE_BAD_SEGMENT,
                    field="version",
                )

            declared_first = _segment_first_seq(path)
            actual_first = _read_first_entry_seq(path)
            if actual_first is not None and actual_first != declared_first:
                raise CrewLogError(
                    f"segment {path.name} declares first seq {declared_first} in its filename, "
                    f"but its first entry is seq {actual_first}",
                    code=CODE_BAD_SEGMENT,
                    field="seq",
                )

            at_boundary = bool(index) and expected > 0
            for entry in _iter_entries(path):
                if at_boundary and entry.seq != expected:
                    raise CrewLogError(
                        f"segment {path.name} starts at seq {entry.seq}, but the "
                        f"previous segment ended at {expected - 1}: the log is not "
                        "contiguous across the boundary",
                        code=CODE_SEGMENT_GAP,
                        field="seq",
                    )
                at_boundary = False
                expected = entry.seq + 1
                yield entry

    def get(self, seq: int) -> Entry | None:
        """The entry at *seq*, or ``None``.

        Stops as soon as the stream passes *seq*: entries are written in seq
        order, so a miss costs the prefix, not the file.

        Reads across SEGMENTS, like :meth:`iter_from`. Reading only the newest file
        made a point read of a rotated entry answer ``None`` -- indistinguishable
        from "no such entry" -- so a caller resolving a seq it had just been handed
        would be told it does not exist. Retention is the one thing that legitimately
        removes an entry, and it deletes whole segments off the front; while a
        segment is still on disk its entries are still readable.
        """
        for entry in self._iter_segments():
            if entry.seq == seq:
                return entry
            if entry.seq > seq:
                return None
        return None

    def page(self, before: int | None = None, limit: int = DEFAULT_PAGE_LIMIT) -> Page:
        """Up to *limit* entries older than *before*, NEWEST first.

        ``before`` is exclusive, so feeding ``next_before`` straight back walks
        the history without repeating or skipping a line.
        """
        return self._page(before, limit, keep=lambda _entry: True)

    def thread_page(
        self, anchor: int, before: int | None = None, limit: int = DEFAULT_PAGE_LIMIT
    ) -> Page:
        """One thread's entries, NEWEST first, the anchor last.

        A thread is a GROUPING key, not a tree: members carry ``thread ==
        anchor`` and the anchor carries its own seq. The anchor is included, so
        the final page of a thread ends with the entry the group hangs off --
        which is what makes a thread readable bottom-up without a second call.
        """
        return self._page(
            before, limit, keep=lambda entry: entry.thread == anchor or entry.seq == anchor
        )

    def _page(self, before: int | None, limit: int, *, keep: Callable[[Entry], bool]) -> Page:
        bound = max(1, min(int(limit), MAX_PAGE_LIMIT))
        # One extra slot answers "is there an older entry" exactly, so the
        # cursor is None precisely when the caller has seen everything.
        window: deque[Entry] = deque(maxlen=bound + 1)
        # Across SEGMENTS, like `iter_from` and `get`. Reading only the newest file
        # made paging stop dead at a rotation boundary and report `next_before` as
        # None -- telling the caller it had seen the whole history when it had seen
        # one segment of it. Paging renders history for a human, and silently
        # truncating that is the one answer it must not give.
        for entry in self._iter_segments():
            if before is not None and entry.seq >= before:
                continue
            if keep(entry):
                window.append(entry)
        newest_first = list(reversed(window))
        has_more = len(newest_first) > bound
        entries = tuple(newest_first[:bound])
        return Page(entries=entries, next_before=entries[-1].seq if has_more and entries else None)

    def resolve(self, ref: Ref | dict[str, Any]) -> Resolution:
        """Follow *ref* and return the segment it cites.

        Four outcomes. ``ok`` carries the entries. ``gone`` means the cited crew log
        does not exist at all -- a normal answer, not an error, since the cited
        crew log may legitimately have been deleted and the citing entry stays honest
        about having pointed at it. ``pruned`` means the span reaches below the
        oldest surviving segment's first seq, so retention removed those lines.
        ``corrupt`` means the span lies inside a segment that still exists yet read
        short, which is damage.

        The last two are never reported as each other, and the classification is
        made from the segment names rather than from the short read: a reader told
        ``pruned`` stops looking, because retention removing old lines is a normal
        answer, while ``corrupt`` tells it the file it still has is not intact.
        Reporting damage as retention would turn a recoverable alarm into silence.
        Citing PAST the newest entry is ``corrupt`` as well, and deliberately not
        ``ok``: a ``Ref`` always names a definite span -- an absent ``to_seq``
        means the single line at ``from_seq``, so there is no open-ended spelling
        -- which makes a span reaching beyond the newest entry a claim about lines
        that are not in the file. ``ok`` means every cited seq was read back.

        This layer makes NO authorization claim, and takes no access callback. A
        half-built one would be worse than none: a check that defaults to allow
        makes the shortest call shape the insecure one, and a check with no
        permission model behind it only looks like a boundary. Every caller today
        is in-process gateway code that can already read the file. A ``forbidden``
        status arrives with the first caller that HAS a permission model -- the
        routes that mount this -- and it belongs there, where the caller identity
        it must be derived from actually exists.
        """
        pointer = _as_ref(ref)
        if pointer.unit == self._kind and pointer.id == self._id:
            target: CrewLog | None = self
        else:
            try:
                target = CrewLog.open(pointer.unit, pointer.id)
            except CrewLogError as exc:
                # Only "there is no such crew log" is `gone`. A crew log that EXISTS
                # but will not open -- a damaged header, an unreadable segment --
                # is damage, and answering `gone` for it tells the reader to stop
                # looking for a file that is right there and broken.
                if getattr(exc, "code", "") == CODE_NO_LEDGER:
                    return Resolution(status=STATUS_GONE, entries=())
                return Resolution(status=STATUS_CORRUPT, entries=())
        if target is None:
            return Resolution(status=STATUS_GONE, entries=())
        last = pointer.last_seq
        try:
            found = tuple(
                entry for entry in target.iter_from(pointer.from_seq) if entry.seq <= last
            )
        except CrewLogError:
            # A missing middle segment or a torn line RAISES out of the read. That
            # is the very condition this method promises to report, so it is
            # answered rather than propagated -- a caller resolving a citation
            # wants a verdict, and an exception here would make `corrupt`
            # unreachable for exactly the damage it names.
            return Resolution(status=STATUS_CORRUPT, entries=())
        # A SHORT answer has two possible causes and they are not the same fact.
        # Retention deletes whole segments off the front, so a span reaching below
        # the oldest survivor is `pruned` -- a normal answer. A span that lies
        # inside a segment which still exists, yet reads short, is DAMAGE: the
        # lines should be there. Reporting that as retention tells a reader to
        # stop looking for a file that is in fact corrupt.
        firsts = segment_first_seqs(target.kind, target.id)
        oldest = firsts[0] if firsts else 1
        if pointer.from_seq < oldest:
            # Only the surviving part of the span is expected, so coverage is
            # measured from the oldest survivor rather than from the citation's
            # own start. A hole INSIDE that surviving range is damage, not
            # retention, and gets `corrupt` on a span that also reaches below it.
            if not _covers_span(found, oldest, last):
                return Resolution(status=STATUS_CORRUPT, entries=found)
            return Resolution(status=STATUS_PRUNED, entries=found)
        # Seq is contiguous inside a file, so coverage is the test -- and the span
        # it is measured against comes from what the CITATION claims existed, never
        # from what is on disk now. `ok` means every cited seq was read back;
        # anything short of that is the citation failing, whatever shortened the
        # file.
        #
        # Measuring against the file's own tail instead is wrong in the one
        # direction that matters. A clean end-truncation -- whole lines removed, no
        # torn bytes, nothing for the read to raise on -- lowers the walked entries
        # and a reopened handle's `last_seq` together, so a tail-derived span
        # shrinks to exactly what survived and the verdict reads `ok` with the
        # cited lines missing. That is the worst available answer: a caller
        # resolving a citation is asking whether it can still be read, and `ok`
        # with fewer entries tells it yes while handing it a hole. A file that GREW
        # needs no tail-derived span either, because this walk stops at `last`, so
        # entries past the citation are never in `found` and cannot inflate the count.
        if not _covers_span(found, pointer.from_seq, last):
            # `corrupt` rather than a new status. The distinction a caller acts on is
            # "resolvable or not", and retention -- the one shortening that is normal
            # -- already has its own answer in the `pruned` branch above.
            return Resolution(status=STATUS_CORRUPT, entries=found)
        return Resolution(status=STATUS_OK, entries=found)


# --------------------------------------------------------------------------- #
# File primitives
# --------------------------------------------------------------------------- #


#: Directories this process has already failed to restrict. Every append goes
#: through :func:`_mkdir_private`, so a filesystem that refuses ``chmod`` would
#: otherwise log a full traceback twice per entry -- turning one true fact about the
#: host into a flood that buries the entries it is warning about. Warn once per
#: directory instead, like the boot-time restriction does.
_restrict_failed: set[str] = set()


def log_exception_text(log: logging.Logger, level: int, msg: str, *args: object) -> None:
    """Log the active exception with its traceback RENDERED to text, never as ``exc_info``.

    Every caller sits in a frame that holds a ``CrewLog`` handle (a method's ``self``, a
    local ``handle``), and a handle's write lease is released by a finalizer when the
    handle is dropped. An ``exc_info`` triple on the record keeps the traceback, the
    traceback keeps that frame, and a handler that keeps records (a ``MemoryHandler``, a
    test harness) then keeps the handle -- and its lease -- for as long as it keeps the
    record. A string keeps nothing; the render is skipped when the level is off.
    """
    if not log.isEnabledFor(level):
        return
    log.log(level, msg + "\n%s", *args, traceback.format_exc().rstrip())


def _mkdir_private(directory: Path) -> None:
    """Create *directory* and its parents owner-only.

    A bare ``mkdir`` takes the process umask, which is commonly world-readable, and
    these directories hold conversation bodies. The eager tightening at
    ``<home>/crew-log`` normally makes a leaf's own mode moot -- an unreadable parent
    is enough -- but it is best-effort, and this runs on the path taken when it did
    not succeed. So the guarantee is asserted per directory as well as once at the
    root, rather than resting on a parent that may not have been tightened.

    Already-correct is the common case and costs nothing: this runs on EVERY append,
    so the mode is checked before it is set and the ``chmod`` is skipped when the
    directory is already owner-only.

    Best-effort for the same reason the root's is: a filesystem that refuses the mode
    change must not make a crew log unwritable, and the sandbox mask and the file-tool
    fence still stand.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            if directory.stat().st_mode & 0o777 == 0o700:
                return
        except OSError:
            # Fall through and try to set it; a stat that fails is not a reason to
            # skip the restriction.
            pass
    try:
        restrict_dir_to_owner(directory)
    except OSError:
        key = str(directory)
        if key not in _restrict_failed:
            _restrict_failed.add(key)
            log_exception_text(
                logger,
                logging.WARNING,
                "Cannot restrict %s to owner-only; it may be readable by other users",
                directory,
            )


def _append_line(path: Path, line: str, *, needs_newline: bool) -> None:
    """Append *line* plus its terminator, then fsync.

    The handle is BINARY, which is what keeps the terminator exactly one ``\\n``:
    text mode on Windows translates it to ``\\r\\n``, and the byte offsets the
    torn-tail truncation computes then stop matching what is on disk.

    *needs_newline* re-supplies a separator the previous write lost -- the one
    case where a record survived but its terminator did not. It PREPENDS rather
    than rewriting that line, so the append-only rule holds.

    A failure ROLLS BACK. See :func:`_write_then_sync`.
    """
    _mkdir_private(path.parent)
    prefix = "\n" if needs_newline else ""
    _write_then_sync(path, f"{prefix}{line}\n".encode())


def _append_lines(path: Path, lines: "list[str]", *, needs_newline: bool) -> None:
    """Append every line in ONE write and ONE fsync.

    The atomicity this buys is not a filesystem guarantee -- a write can still tear
    -- but it collapses the window: a group written this way is on disk as a whole
    or ends in a torn tail the existing truncation removes, instead of leaving each
    line separately durable and the group half-present.

    Same binary-handle and *needs_newline* rules as :func:`_append_line`, and the
    same rollback on failure, for the same reasons.
    """
    _mkdir_private(path.parent)
    prefix = "\n" if needs_newline else ""
    body = "".join(f"{line}\n" for line in lines)
    _write_then_sync(path, f"{prefix}{body}".encode())


def _write_then_sync(path: Path, blob: bytes) -> None:
    """Append *blob* and fsync it, restoring the previous size if either fails.

    The rollback is what makes a failure DEFINITE. Bytes reach the file before the
    fsync runs, so a failing fsync leaves an outcome nobody knows: the entry may well
    be durable. The write-behind retains and retries a failure, and a retry against
    an unknown outcome either writes the same fact twice under two seqs -- a duplicate
    no reader can tell from a real repeat -- or has to reason about what is already
    there. Truncating back to the size the file had before removes the question:
    either the append is whole and synced, or it is gone and the retry writes it
    cleanly.

    A partial write is rolled back for the same reason, and one more: leaving half a
    line behind would make the next append's torn-tail truncation the thing that
    cleans up, so the file would carry a fragment until then.

    If the ROLLBACK itself fails -- likely, since whatever broke the write is often
    still broken -- the file may hold bytes nobody can account for, and that is what
    :class:`IndeterminateAppend` reports. Its bytes are unterminated or unparseable
    at the tail, which is exactly the shape the next ``open`` truncates, so the
    recovery already exists; the distinct type is so a caller can tell "nothing
    happened" from "something may have".

    Truncating is safe against a concurrent writer because every caller reaches here
    holding the session's append lock, so no other handle can have appended between
    this write and its rollback -- the bytes removed can only be this call's own.
    """
    with open(path, "ab") as handle:
        before = handle.tell()
        try:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            try:
                handle.close()
                _rollback_append(path, before)
            except OSError as undo:
                raise IndeterminateAppend(
                    f"append to {path.name} failed ({exc}) and could not be rolled "
                    f"back ({undo}): the file may hold bytes no entry claims",
                    written=blob,
                    offset=before,
                ) from exc
            raise


def _rollback_append(path: Path, size: int) -> None:
    """Cut *path* back to *size* and fsync it.

    Not an exception to the never-rewrite rule: the bytes removed are the ones the
    caller's own failed append just wrote, so this restores the file to a state a
    reader could already have seen rather than editing history. It is the same
    category as the torn-tail truncation, moved to the moment the tear happens
    instead of the next open.
    """
    with open(path, "r+b") as handle:
        handle.truncate(size)
        handle.flush()
        os.fsync(handle.fileno())


def _refuse_content_repair(unit_id: str, skipped: str) -> None:
    """Say why a content-aware repair is declining to touch this file.

    Named once because both check sites answer the same question -- the fold ran
    over records it could not read, so what it reports about the tail is not what
    the file says -- and a repair that acts on it appends an outcome no writer
    observed, permanently, to a log nothing rewrites.
    """
    logger.warning(
        "session log %r: not repairing an interrupted turn -- the fold skipped a "
        "record (%s), so its result cannot be trusted and appending a closer could "
        "fabricate an outcome",
        unit_id,
        skipped,
    )


def _classify_record(raw: bytes, index: int) -> "tuple[Entry | None, str | None]":
    """*raw* as an entry, or the reason it is not one.

    Both strict walks share this: the scan that produces a truncation offset, and
    the fold that decides whether a closer may be appended. Their results feed
    the same mutation -- one chooses where the file is cut, the other whether
    history is added to it -- so they have to agree about what counts as a
    record. A second spelling of this classification is a second answer waiting
    to disagree with the first.

    *index* is the record's position in the file, so the reason names the line a
    reader can go and look at. Record 1 is the header and is the caller's to skip.
    """
    stripped = raw.strip()
    if not stripped:
        return None, f"record {index + 1} is blank"
    parsed = _parses_to_object(stripped)
    if parsed is None:
        return None, f"record {index + 1} is malformed"
    entry = Entry.from_dict(parsed)
    if entry is None:
        return None, f"record {index + 1} is not a valid entry"
    return entry, None


def _orphan_chunk_offset(path: Path) -> "tuple[int, int, int] | None":
    """A trailing chunk group with no citing entry: its offset and seq range.

    A body too large for one line is written as chunks plus the entry that cites
    their seqs, in one batch. A hard kill during that write can still tear the tail,
    and the citing entry is the LAST line -- so what survives is chunks nothing
    points at. The body is on disk and unreachable: no entry names those seqs, and
    the message the group belongs to has no record at all.

    Dropping them is the honest repair. It costs the one message that was mid-write,
    which is the same residual as any single entry lost to a hard kill, and it leaves
    the file free of lines whose only purpose was to be cited by an entry that does
    not exist.

    Only a TRAILING run counts. A chunk group followed by any other entry was
    completed -- its citing entry landed -- and re-reading which entry cites what is
    not this function's job.

    Returns ``(byte offset of the first orphan, first seq, last seq)`` or ``None``.

    Reads with the ABORT posture, not the skipping one. The offsets computed here
    feed a truncation, and ``bounded_raw_records`` drops an over-cap record in full
    -- terminator included -- without yielding it, so a byte walk over what it does
    yield stops matching the file at the first oversized record and every offset
    after it is short by that record's length. Truncating at a short offset cuts
    valid history, which is the one thing this file's rules forbid. So a record that
    cannot be delivered intact aborts the scan and this returns ``None``: no offset,
    no truncation, the file left exactly as it is with a warning naming why.
    """
    offsets: "list[tuple[int, int, str]]" = []
    try:
        with open(path, "rb") as source:
            at = 0
            for index, raw in enumerate(strict_raw_records(source, path, cap=MAX_ENTRY_BYTES)):
                start = at
                at += len(raw)
                if index == 0:
                    continue
                entry, _ = _classify_record(raw, index)
                if entry is not None:
                    offsets.append((start, entry.seq, entry.type))
    except FileNotFoundError:
        return None
    except UnreadableRecord as exc:
        # Fail closed. Every byte has to be accounted for before an offset into this
        # file can be trusted, and this read could not account for one.
        logger.warning(
            "crew log %r: not scanning for an orphan chunk group -- a record could not "
            "be read intact (%s), so a byte offset into this file cannot be trusted "
            "and truncating on one could cut valid entries",
            path,
            exc,
        )
        return None
    trailing = 0
    while trailing < len(offsets) and offsets[len(offsets) - 1 - trailing][2] == "message/chunk":
        trailing += 1
    if not trailing:
        return None
    first = offsets[len(offsets) - trailing]
    return (first[0], first[1], offsets[-1][1])


def _truncate(path: Path, offset: int) -> None:
    """Drop everything at or after *offset* -- the one allowed mutation."""
    with open(path, "r+b") as handle:
        handle.truncate(offset)
        handle.flush()
        os.fsync(handle.fileno())


# --------------------------------------------------------------------------- #
# Interrupted-tail closers
# --------------------------------------------------------------------------- #

#: What a closer records as the reason a turn ended, and as a tool's outcome.
#: Two distinct words on purpose: the turn ENDED (the writer stopped), while the
#: tool's result is simply not knowable from the record.
STOP_REASON_INTERRUPTED = "interrupted"
TOOL_STATUS_UNKNOWN = "unknown"

#: The same "not knowable from the record" word, for the two other openers a
#: session's crew log can be left holding. Only the repair ever writes them: a live
#: writer always knows what a human decided and how a child ended, so a reader
#: seeing either value knows it is reading a reconstruction rather than an
#: observation. Spelled as separate constants because they name different fields
#: on different types, and a future change to one must not silently move the other.
APPROVAL_DECISION_UNKNOWN = "unknown"
SUBAGENT_OUTCOME_UNKNOWN = "unknown"


@dataclass(frozen=True)
class _OpenTail:
    """What a session log was left holding open, in first-seen order.

    ``turn`` is None when the newest turn DID complete and the only thing still
    open is a child that outlived it -- which is the ordinary shape for a subagent,
    since a child routinely finishes turns after the one that spawned it.

    ``calls`` and ``approvals`` are turn-scoped: both belong to the turn that
    opened them and cannot outlive it, so they are collected only from inside an
    open turn. ``children`` is FILE-scoped for the opposite reason -- a child's
    whole point is that it runs past its asking turn, so scoping its closer to the
    open turn would leave exactly the common case unclosed.
    """

    turn: Any
    calls: tuple[dict[str, Any], ...]
    last_time: int
    approvals: tuple[dict[str, Any], ...] = ()
    children: tuple[dict[str, Any], ...] = ()


def _open_tail(
    path: Path, *, child_gone: "Callable[[str], bool] | None" = None
) -> "tuple[_OpenTail | None, str | None]":
    """The unbalanced tail and why a repair fold skipped a record, if it did.

    Three kinds of opener can be left unbalanced, and they are NOT scoped alike.

    A ``turn/started`` with no later ``turn/completed`` means the writer stopped
    mid-turn. Unmatched ``tool/called`` and ``approval/requested`` entries are
    collected only from INSIDE that turn: both are turn-scoped by construction --
    a call completes within its turn, and an approval is decided in the same
    ``finally`` that the turn's own path runs through -- so an unmatched one in a
    turn that DID complete is a different anomaly, and inventing an outcome for it
    here would be this reader editing history it was not asked about.

    An unmatched ``subagent/spawned`` is collected across the WHOLE file instead,
    because a child is deliberately not turn-scoped: it starts, steers and reports
    long after its asking turn ended, so the ordinary dangling case is a child
    whose turn completed normally. Scoping it to the open turn would close only the
    rare child that died inside its own turn and leave every common one open
    forever.

    Children are kept only when *child_gone* says so, and that asymmetry is the
    point. "The writer is gone" is what a repair knows, and for a turn-scoped
    opener that settles it -- the turn died with its writer. It does NOT settle a
    child: a child outlives the turn that asked for it BY DESIGN, so a writer
    torn down inside a live process can leave a child still running, still able to
    file its own real terminal. Closing it from here would put two outcomes for one
    ``agent_id`` in a file nothing rewrites. Nothing in the file distinguishes the
    two, so the question goes to the caller's live subagent registry; with no
    predicate the answer is "still running" and the opener stands.

    This fold feeds a mutation, so it reports every record the ordinary reader
    would skip. A repair cannot distinguish a truly open turn from one whose real
    completion is damaged, and appending a closer after such a skip would make a
    false outcome permanent.
    """
    open_turn: Any = None
    last_time = 0
    last_walked = 0
    calls: dict[str, dict[str, Any]] = {}
    approvals: dict[str, dict[str, Any]] = {}
    children: dict[str, dict[str, Any]] = {}
    skipped: str | None = None
    try:
        with open(path, "rb") as source:
            for index, raw in enumerate(strict_raw_records(source, path, cap=MAX_ENTRY_BYTES)):
                if index == 0:
                    continue
                entry, reason = _classify_record(raw, index)
                if entry is None:
                    skipped = skipped or reason
                    continue
                if entry.seq <= last_walked:
                    # A non-advancing seq is damage the append-only writer cannot
                    # produce (the same verdict ``iter_from`` and ``advance`` give
                    # it). This fold feeds a MUTATION: ``_scan_tail`` takes the
                    # newest line's seq, so a backward tail lowers the closers'
                    # first seq onto records that already exist -- a repair here
                    # would amplify the damage before any reader refuses it.
                    skipped = skipped or (
                        f"entry {entry.seq} at record {index} is at or below the "
                        f"previously read entry's seq {last_walked}"
                    )
                    continue
                last_walked = entry.seq
                last_time = entry.time
                data = entry.data if isinstance(entry.data, dict) else {}
                if entry.type == "turn/started":
                    open_turn = data.get("turn")
                    calls = {}
                    approvals = {}
                elif entry.type == "turn/completed":
                    open_turn = None
                    calls = {}
                    approvals = {}
                elif entry.type == "tool/called" and open_turn is not None:
                    call_id = data.get("call_id")
                    if isinstance(call_id, str) and call_id and call_id not in calls:
                        calls[call_id] = {
                            "call_id": call_id,
                            "name": data.get("name", ""),
                            "server": data.get("server", ""),
                        }
                elif entry.type == "tool/completed" and open_turn is not None:
                    call_id = data.get("call_id")
                    if isinstance(call_id, str):
                        calls.pop(call_id, None)
                elif entry.type == "approval/requested" and open_turn is not None:
                    approval_id = data.get("approval_id")
                    if (
                        isinstance(approval_id, str)
                        and approval_id
                        and approval_id not in approvals
                    ):
                        approvals[approval_id] = {"approval_id": approval_id}
                elif entry.type == "approval/decided" and open_turn is not None:
                    approval_id = data.get("approval_id")
                    if isinstance(approval_id, str):
                        approvals.pop(approval_id, None)
                elif entry.type == "subagent/spawned":
                    agent_id = data.get("agent_id")
                    if isinstance(agent_id, str) and agent_id and agent_id not in children:
                        children[agent_id] = {"agent_id": agent_id}
                elif entry.type in ("subagent/completed", "subagent/failed"):
                    agent_id = data.get("agent_id")
                    if isinstance(agent_id, str):
                        children.pop(agent_id, None)
    except FileNotFoundError:
        return None, None
    except UnreadableRecord as exc:
        skipped = skipped or str(exc)
    # An unmatched opener says the file never recorded an outcome; only the caller
    # can say whether one is still COMING. Without a predicate nothing is closed,
    # and a predicate that raises is read as "still running" -- both leave the
    # opener standing, which is the direction that loses nothing a reader cannot
    # recover. Asking per surviving opener rather than per entry keeps the question
    # to the children that are actually unbalanced.
    if child_gone is None:
        children.clear()
    else:
        for agent_id in list(children):
            try:
                gone = child_gone(agent_id)
            except Exception:
                gone = False
            if not gone:
                children.pop(agent_id, None)
    if open_turn is None and not children:
        return None, skipped
    return (
        _OpenTail(
            turn=open_turn,
            calls=tuple(calls.values()),
            last_time=last_time,
            approvals=tuple(approvals.values()),
            children=tuple(children.values()),
        ),
        skipped,
    )


def _closer_entries(tail: _OpenTail, first_seq: int) -> list[Entry]:
    """The closers for *tail*, in the order they are appended.

    Approvals, then unmatched calls, then children, then the turn. The order is
    the one a LIVE writer could have produced: an approval is decided before the
    call it gates completes, and a turn cannot be closed while a call inside it is
    still open, so closing them the other way round would produce a record no live
    writer could ever have produced.

    The turn closer is written only when a turn was actually open. A child that
    outlived a completed turn leaves this function with children and no turn, and
    appending a ``turn/completed`` there would close a turn that already closed
    itself.

    Every closer reuses the LAST REAL entry's ``time``. A closer describes
    something that happened when the writer stopped, not when a later process
    happened to open the file, so stamping it with the current clock would put a
    gap of arbitrary length inside a turn and make a duration computed off these
    entries a measure of downtime. Reusing the time also makes the repair
    deterministic: the same bytes in produce the same bytes out, whenever it runs.
    """
    entries: list[Entry] = []
    seq = first_seq
    for approval in tail.approvals:
        entries.append(
            Entry(
                type="approval/decided",
                seq=seq,
                time=tail.last_time,
                src="gateway",
                data={
                    "turn": tail.turn,
                    "approval_id": approval["approval_id"],
                    "decision": APPROVAL_DECISION_UNKNOWN,
                },
            )
        )
        seq += 1
    for call in tail.calls:
        entries.append(
            Entry(
                type="tool/completed",
                seq=seq,
                time=tail.last_time,
                src="gateway",
                data={
                    "turn": tail.turn,
                    "call_id": call["call_id"],
                    "name": call["name"],
                    "server": call["server"],
                    "status": TOOL_STATUS_UNKNOWN,
                },
            )
        )
        seq += 1
    for child in tail.children:
        # No `turn`: `subagent/failed` carries none, because a child's outcome is
        # not an event of any one turn -- which is the same reason its opener is
        # matched across the whole file rather than inside the open turn.
        entries.append(
            Entry(
                type="subagent/failed",
                seq=seq,
                time=tail.last_time,
                src="gateway",
                data={
                    "agent_id": child["agent_id"],
                    "outcome": SUBAGENT_OUTCOME_UNKNOWN,
                },
            )
        )
        seq += 1
    if tail.turn is not None:
        entries.append(
            Entry(
                type="turn/completed",
                seq=seq,
                time=tail.last_time,
                src="gateway",
                data={"turn": tail.turn, "stop_reason": STOP_REASON_INTERRUPTED},
            )
        )
    return entries


def _close_interrupted_tail(
    kind: str,
    unit_id: str,
    path: Path,
    *,
    child_gone: "Callable[[str], bool] | None" = None,
) -> int:
    """Append closers for an interrupted tail. Returns how many were written.

    Session logs only, and RESUME ONLY. A crash, a SIGKILL or a pod eviction
    leaves openers unbalanced, and every later reader then has to carry the same
    special case: is this still running, or did its writer die? Closing the tail
    when the crew log is LOADED after an interruption answers that once, in the
    record, instead of in each reader.

    Two openers are closed on any repair, each with the "not knowable from the
    record" word its own type already uses: an unmatched ``tool/called`` and an
    unmatched ``approval/requested``, both turn-scoped, both settled by the one
    thing a repair knows -- that the writer is gone.

    A third, ``subagent/spawned``, is closed only for an ``agent_id`` the caller's
    *child_gone* predicate reports finished. A child is not turn-scoped, so a
    writer torn down inside a LIVE process can leave a child still running and
    still able to file its own real terminal; closing it from here would put two
    outcomes for one ``agent_id`` in a file nothing rewrites. Only the repairing
    process's own registry of running children can rule that out. This can
    therefore fire on a file whose newest turn completed normally and whose only
    open thing is a child that outlived it.

    "Loaded after an interruption" is the whole precondition, which is why this is
    never reached from a plain ``open``. An open turn is indistinguishable from a
    dead one by looking at the file, so the CALLER's situation is the only thing
    that can tell them apart: a resume knows the previous writer is gone, and a
    live writer reconnecting to its own log knows it is not. Closing a turn
    that is still running would append an outcome it never had and then let the
    turn keep writing past its own completion.

    It stays append-only -- nothing is rewritten, seq continues -- so a reader that
    already folded the file sees only new lines.

    Best-effort by design: a closer that cannot be written leaves the tail open,
    which is the state every reader must already tolerate.
    """
    if kind != KIND_SESSION:
        return 0
    with _open_lock(_lock_path(kind, unit_id)):
        tail = _scan_tail(path)
        if tail.empty:
            return 0
        if tail.torn_offset is not None:
            _truncate(path, tail.torn_offset)
            tail = _scan_tail(path)
        # Check the fold before any content-aware repair. A damaged completion can
        # look exactly like an open turn once the ordinary reader skips it, and a
        # closer appended from that fold would state an outcome no writer observed.
        opened, skipped = _open_tail(path, child_gone=child_gone)
        if skipped is not None:
            _refuse_content_repair(unit_id, skipped)
            return 0
        orphan = _orphan_chunk_offset(path)
        # A chunk group whose citing entry never landed. Dropped BEFORE the closers,
        # so the closers do not sit on top of lines nothing can reach, and before the
        # tail is re-read, so their seqs continue from the truncated file.
        if orphan is not None:
            offset, first_seq, last_seq = orphan
            _truncate(path, offset)
            tail = _scan_tail(path)
            logger.warning(
                "dropped an unreachable chunk group from session log %r: seq %d-%d "
                "were written for a message whose citing entry never landed, so that "
                "one message is missing from this log",
                unit_id,
                first_seq,
                last_seq,
            )
            opened, skipped = _open_tail(path, child_gone=child_gone)
            if skipped is not None:
                _refuse_content_repair(unit_id, skipped)
                return 0
        if opened is None:
            return 0
        needs_newline = tail.needs_newline
        written = 0
        for entry in _closer_entries(opened, tail.last_seq + 1):
            line = require_entry_line(serialize(entry.to_dict()))
            _append_line(path, line, needs_newline=needs_newline)
            needs_newline = False
            written += 1
    logger.info(
        "closed an interrupted tail in session log %r: %d closer(s) appended",
        unit_id,
        written,
    )
    return written

"""Finding units on disk and proving which unit a directory holds.

A unit is identified by its header, read from disk and accepted only when the id it stores
folds back to its directory (``_proved_header``, ``_unit_header_object``). The header reads
(``unit_header_slot``, ``unit_header_created_at``, ``unit_opened_previous``), the listings
(``unit_ids``, ``unit_dirs``, ``unit_dir_for``, ``unprovable_session_units``) and the slot
index rest on that proof. The slot index says which session units each slot ran under
(``session_units_by_slot``, ``session_units_for_slot``); it is cached in ``_slot_index``
against a fingerprint of the root, and in strict mode a child that holds entries but cannot
be proved is refused.
"""

from __future__ import annotations

import os
import time
from collections.abc import Collection
from pathlib import Path
from typing import Any

from kiro_crew.crew_log.errors import CODE_BAD_HEADER, CrewLogError
from kiro_crew.crew_log.schema import KIND_SESSION, require_kind
from kiro_crew.crew_log.store_paths import _checked_crew_log_root, _segment_first_seq, crew_log_dir
from kiro_crew.crew_log.store_segment_reads import (
    _has_content,
    _parses_to_object,
    _read_header_line,
    read_head,
)
from kiro_crew.session_ledger import _store_name, is_link


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

#: ``[0]`` is ``(stat, triple, first_listed_ns, verified)`` for the ``_slot_index``
#: triple, or None. *stat* is the root stat taken just before the listing that produced
#: *triple*; *first_listed_ns* is the monotonic time the first listing under that stat
#: COMPLETED; *verified* says a second listing, whose stat was taken at least
#: :data:`_RACY_WINDOW_NS` after that completion, saw the same stat and the same names. Only a verified record is served without a
#: listing. Tied to the triple by identity, so a triple written any other way -- a
#: rebuild, a test planting one -- is never served on a stat it was not built under. A
#: one-slot list mutated in place, never rebound, so the store facade can bind the same
#: object (``_slot_index`` is the one rebound global).
_SLOT_INDEX_TRUST: "list[tuple[tuple[Any, ...] | None, object, int, bool] | None]" = [None]

#: How long after the first listing under a stat the confirming listing must wait. A
#: filesystem stamps a change with a clock that ticks coarser than a listing takes (a
#: kernel tick on ext4 and xfs, 2 s on FAT), so a create landing in the same tick as the
#: listed state can leave mtime, ctime and link count all equal. Every such create
#: happens within one tick of the first listing, so a second listing taken a full window
#: later sees it by name. After that, a change can only stamp a later tick than the one
#: the stat holds, so an equal stat means an equal directory. Only the root's own stamps
#: are compared with each other; the local clock measures elapsed time and is never
#: compared with a filesystem stamp, so a data home whose server clock is off this
#: host's is handled the same way. This is git's "racily clean" rule without its clock
#: comparison.
_RACY_WINDOW_NS = 2_000_000_000


def _monotonic_ns() -> int:
    """The local elapsed-time clock the confirming listing waits on."""
    return time.monotonic_ns()


def _root_stat_key(root: Path) -> "tuple[Any, ...] | None":
    """The root's identity, change stamps and link count, from one ``stat``.

    None when the root will not stat.
    """
    try:
        stat = root.stat()
    except OSError:
        return None
    return (
        str(root),
        stat.st_dev,
        stat.st_ino,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_nlink,
        stat.st_size,
    )


def _next_trust(
    stat_key: "tuple[Any, ...] | None", triple: object, started_ns: int, done_ns: int
) -> "tuple[tuple[Any, ...] | None, object, int, bool] | None":
    """The trust record after a listing under *stat_key* produced *triple*.

    *started_ns* is read before this call's stat and *done_ns* after its listing. A
    listing under the same stat as a record for the same triple confirms it when its stat
    was taken a full window after that record's first listing COMPLETED, so a stall
    anywhere inside either call cannot shrink the gap between the two looks. Any other
    listing starts a new, unconfirmed record stamped with its own completion.
    """
    if stat_key is None:
        return None
    held = _SLOT_INDEX_TRUST[0]
    if held is not None and held[0] == stat_key and held[1] is triple:
        confirmed = held[3] or started_ns - held[2] >= _RACY_WINDOW_NS
        return (stat_key, triple, held[2], confirmed)
    return (stat_key, triple, done_ns, False)


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
    except (CrewLogError, OSError):
        if strict:
            raise
        return {}
    # The O(1) answer. One ``stat`` of the root, compared with the stat the cached map
    # was listed and confirmed under; equal means no unit has appeared or disappeared
    # since, in this process or any other (see ``_RACY_WINDOW_NS``), so the listing and
    # its sort are skipped. Children the map could not prove are still re-checked: a
    # header published inside an existing directory does not touch the root's stat.
    now_ns = _monotonic_ns()
    stat_key = _root_stat_key(root)
    cached = _slot_index
    trust = _SLOT_INDEX_TRUST[0]
    if (
        cached is not None
        and trust is not None
        and trust[1] is cached
        and trust[3]
        and stat_key is not None
        and trust[0] == stat_key
        and not _any_now_provable(root, cached[2])
    ):
        if strict:
            _refuse_unprovable_unit(root, cached[2])
        return cached[1]
    try:
        names = sorted(os.listdir(root))
        listed_ns = _monotonic_ns()
    except OSError:
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
        _SLOT_INDEX_TRUST[0] = _next_trust(stat_key, cached, now_ns, listed_ns)
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
    built = (fingerprint, by_slot, tuple(unproven))
    _slot_index = built
    _SLOT_INDEX_TRUST[0] = _next_trust(stat_key, built, now_ns, listed_ns)
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


def unit_opened_previous(kind: str, unit_id: str) -> "str | None":
    """The predecessor id *unit_id*'s own ``session/opened`` records, or None.

    The DURABLE half of the supersede edge. The emitter writes ``previous.sid`` on
    the opening entry and never rewrites it, so this answers "which crew log was
    this slot writing before" from the file rather than from a process's memory --
    which is what a caller recovering after a crash has and a caller reading its
    own live state does not need.

    Reads the OLDEST segment's SECOND line, which is where ``session/opened`` is:
    the header is line 1 and the opening entry is appended immediately after it, so
    this is O(1) whatever the crew log has grown to. A rotated log keeps its opening
    entry in the first segment, which is why the segments are ordered rather than
    the newest one read.

    None is "cannot prove", never "there is no predecessor", exactly as in
    :func:`_unit_header_object` and for the same reason: the refusals are a
    directory that is a link, a header that does not fold back to the directory
    holding it, no second line yet, a line that does not parse, a second line that
    is not a ``session/opened``, and a ``previous`` that is absent or not shaped as
    an object carrying a non-empty string ``sid``. A caller acting on this value
    must treat None as "no recovery available here" rather than as a statement that
    the crew log stands alone.

    Read-only: it takes no lease and writes nothing, so it cannot disturb a crew log
    another process is appending to.
    """
    require_kind(kind)
    try:
        named = _checked_crew_log_root(kind) / _store_name(unit_id)
        if is_link(named):
            return None
        directory = crew_log_dir(kind, unit_id)
        if is_link(directory):
            return None
        segments = [
            (first, child)
            for child in directory.iterdir()
            if (first := _segment_first_seq(child)) is not None
        ]
    except (CrewLogError, OSError):
        return None
    if not segments:
        return None
    segments.sort(key=lambda pair: pair[0])
    try:
        header, entry, _has_line = read_head(segments[0][1])
    except OSError:
        return None
    if header is None or entry is None:
        return None
    own_id = header.get("id")
    if not isinstance(own_id, str) or not own_id or _store_name(own_id) != directory.name:
        return None
    if entry.type != "session/opened":
        return None
    previous = entry.data.get("previous")
    if not isinstance(previous, dict):
        return None
    sid = previous.get("sid")
    return sid if isinstance(sid, str) and sid else None

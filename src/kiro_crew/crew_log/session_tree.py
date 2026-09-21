"""The session tree -- which session opened which, folded across every session log.

:mod:`kiro_crew.crew_log.projection` folds ONE log and reads nothing else (RFC
FR-4). This module is the one reader that looks across logs, and it is a
different kind of thing on purpose. A ``session_create`` child records its
creator on its own ``session/opened`` entry (``parent {slot, sid?}``: the child
knows its creator at its first turn, the creator never learns the child's id),
so the tree of sessions is not a fold over any one log but over the FIRST entry
of all of them, keyed by slot. dsh does the same with its ``parentSession``
header field and a read-side ``flattenLineage``: the record lives on the child,
the tree is a pure function over the collection, and an orphan or a cycle
degrades to root rather than to an error.

Two pieces, kept apart so each is testable on its own:

* :func:`fold_tree` -- pure. :class:`OpenedRecord` in, one :class:`TreeNode`
  per slot out.
* :class:`SessionTree` -- the scanner. It reads the header and the first entry
  of every session log's oldest surviving segment and keeps that head cached
  per unit, for at most :data:`TREE_UNIT_CAP` units per scan. The store never
  rewrites a written line, so the two lines a scan reads are immutable for as
  long as the segment exists; the cache therefore needs no mtime and is dropped
  only when the segment is gone (retention, removal) or the file was replaced
  (a new inode under the same name, or a shorter file under a recycled one).

Only the first entry is read, and that is enough. The emitter writes ``parent``
from a process-local mint witness that exists before the child's first turn or
never, so the entry that CREATED the log carries the parent whenever any entry
does, and a later re-attach in the same process can only repeat it. The fold
still applies the wider rule -- the parent is taken from ANY record of a slot
that carries one, and a record without one never retracts it -- so a slot whose
later logs were opened after a gateway restart (witness gone, no ``parent``)
folds to the parent its first log recorded.

The tree is keyed by SLOT. ``parent.sid`` on the entry is the creator's ACP
session id at the moment of creation -- an audit citation for a reader of the
logs themselves, not a tree key: a slot outlives its ACP session, and the live
row a child nests under is the slot's. This reader does not read it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Final

from kiro_crew.crew_log.schema import KIND_SESSION, Entry
from kiro_crew.crew_log.store import (
    oldest_segment,
    read_head,
    unit_dir_for,
    unit_dirs,
)
from kiro_crew.session_ledger import _store_name
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN, MAX_SHORT_STRING

logger = logging.getLogger(__name__)

#: The one entry type the tree reads a parent from.
TYPE_OPENED: Final[str] = "session/opened"

#: How many session-log units one scan ADMITS -- probes, lists, reads, caches
#: and folds. The ONE bound on everything the scanner does and holds, and every
#: loop in this module is cut by it: the live sessions' ids are probed through
#: ``islice(preferred, cap)`` whether or not a log exists for them, the store's
#: listing stops after ``cap`` candidates plus one look-ahead
#: (:func:`~kiro_crew.crew_log.store.unit_dirs`), the head cache holds one entry
#: per admitted unit and nothing for a unit past the cap, and each retained
#: string is bounded at admission too (:func:`opened_record`). A population
#: past the cap is reported as :attr:`SessionTree.over_cap`, a fact and not a
#: count: counting would mean walking the population, which is the cost the
#: bound exists to refuse. Far above any real population (retention keeps the
#: store far smaller); a store that reaches it usually has retention disabled.
TREE_UNIT_CAP: Final[int] = 4096


@dataclass(frozen=True)
class OpenedRecord:
    """What one session log contributes: its header identity, and the creator
    slot its first entry names (``None`` for a session nobody created)."""

    sid: str
    slot: str
    created_at: int
    parent_slot: str | None = None


@dataclass(frozen=True)
class TreeNode:
    """One slot in the tree.

    ``parent_slot`` is the creator the slot's records cite. It is retained even
    when the tree could not follow it (orphan, cycle): it is the child's record,
    not the fold's. ``cycle`` marks a slot on a cycle of citations, which the
    consumer must not nest. Depth is not carried: the one consumer that nests
    (the Sessions table) walks the parent chain itself.
    """

    slot: str
    parent_slot: str | None
    cycle: bool


@dataclass(frozen=True)
class TreeReading:
    """One scan's nodes and records, and whether that scan saw every unit the store holds.

    The three travel together on purpose. A reader that only DISPLAYS lineage can
    ignore ``incomplete`` -- a missing edge renders as "no creator known", which
    is what it looked like before this reader existed. A reader that DECIDES on
    an edge cannot: dropping a candidate because it is an ancestor, on a tree
    whose edge for that candidate was lost to a transient read fault, is a
    confident wrong answer rather than a missing one.

    ``incomplete`` is computed in the call that produced ``nodes`` rather than
    left on the tree, for the reason :class:`SessionTree` is shared: a flag read
    in a second, unlocked call can belong to another reader's scan.

    ``records`` is the same reason one step further. A consumer that places a UNIT
    rather than a slot needs the scan's ``sid -> slot`` map, which the fold drops:
    :func:`fold_tree` is keyed by slot and a unit id appears nowhere in its output.
    Asking :meth:`records` for it separately would be a SECOND scan, under a second
    take of the lock, so its map could describe a different population than the
    nodes and the flag do -- and the ancestor rule would then be applied across two
    moments. Carrying the records the scan already read costs nothing and makes that
    impossible rather than discouraged.
    """

    nodes: dict[str, TreeNode]
    incomplete: bool
    #: Every provable record this scan read, unordered. Empty when the scan failed,
    #: which is the same answer ``nodes`` gives and is why it needs no separate flag.
    records: tuple[OpenedRecord, ...] = ()


def fold_tree(records: Iterable[OpenedRecord]) -> dict[str, TreeNode]:
    """Every slot's node, from the records of every session log. Pure.

    Input order does not matter: records are folded oldest log first, by the
    header's ``createdAt`` and then id, so two scans of the same files agree.
    A slot's parent is the one its OLDEST record carrying a parent names; a
    record with no parent never retracts it. A record whose header has no slot
    has no place in a slot-keyed tree and is dropped.

    An edge is FOLLOWED only when it lands on a slot that has a log of its own.
    A cited creator with no log is a citation, not a place in the tree, so the
    child is a root that still carries its ``parent``. A cycle -- reachable only
    through forged or damaged records, since a child cannot have created its own
    creator -- marks every slot on it, so a consumer nests none of them; a slot
    hanging off a member keeps its edge to it.
    """
    ordered = sorted(records, key=lambda record: (record.created_at, record.sid))
    has_log: set[str] = set()
    cited: dict[str, str] = {}
    for record in ordered:
        if not record.slot:
            continue
        has_log.add(record.slot)
        if record.parent_slot and record.slot not in cited:
            cited[record.slot] = record.parent_slot

    edge: dict[str, str] = {}
    on_cycle: set[str] = set()
    for slot, parent_slot in cited.items():
        if parent_slot == slot:
            on_cycle.add(slot)
        elif parent_slot in has_log:
            edge[slot] = parent_slot
    on_cycle |= _cycle_members(edge)

    nodes: dict[str, TreeNode] = {}
    for slot in has_log:
        nodes[slot] = TreeNode(
            slot=slot,
            parent_slot=cited.get(slot),
            cycle=slot in on_cycle,
        )
    return nodes


def _cycle_members(edge: Mapping[str, str]) -> set[str]:
    """Every node that lies ON a cycle of *edge* (child -> parent), by colouring.

    A node that merely leads INTO a cycle is not a member: its chain ends at a
    member, which the fold then treats as that chain's root.
    """
    white, grey, black = 0, 1, 2
    colour: dict[str, int] = {}
    members: set[str] = set()
    for start in edge:
        if colour.get(start, white) != white:
            continue
        path: list[str] = []
        cursor: str | None = start
        while cursor is not None and colour.get(cursor, white) == white:
            colour[cursor] = grey
            path.append(cursor)
            cursor = edge.get(cursor)
        if cursor is not None and colour.get(cursor) == grey:
            members.update(path[path.index(cursor) :])
        for node in path:
            colour[node] = black
    return members


def opened_record(
    directory: Path, header: Mapping[str, Any] | None, entry: Entry | None
) -> OpenedRecord | None:
    """The record a unit directory contributes given its parsed head, or ``None``.

    ``None`` is a REFUSAL: an unreadable or non-session header, a header whose
    own id does not fold back to this directory (the refusal
    :func:`~kiro_crew.crew_log.store.unit_header_slot` makes: a directory carrying
    another unit's id would answer for that unit), no entry, or any retained
    string past its bound. Bounds refuse rather than truncate, because every
    string here is RETAINED in the scanner's cache for as long as the log
    exists: a session id past ``MAX_ACP_SESSION_ID_LEN`` (the one constant every
    store of a backend-authored id shares) and a slot key past
    ``MAX_SHORT_STRING`` are not values the gateway writes, and a truncated one
    would be a different key that matches nothing.

    The parent is taken from the first entry only when that entry is a
    ``session/opened`` naming a creator ``slot``; the entry's ``parent.sid`` is
    not read (see the module docstring). Any other first entry -- retention that
    took the creating segment, a process that died between create and announce
    -- contributes the slot with no parent, which the fold reads as "no parent
    known here", never as a retraction.
    """
    if header is None or entry is None or header.get("type") != KIND_SESSION:
        return None
    sid = header.get("id")
    if not isinstance(sid, str) or not _bounded(sid, MAX_ACP_SESSION_ID_LEN):
        return None
    if _store_name(sid) != directory.name:
        return None
    slot = header.get("slot")
    if slot is not None and not _bounded(slot, MAX_SHORT_STRING):
        return None
    created = header.get("createdAt")
    parent_slot: str | None = None
    if entry.type == TYPE_OPENED:
        parent = entry.data.get("parent")
        if isinstance(parent, dict):
            cited_slot = parent.get("slot")
            if cited_slot is not None and not _bounded(cited_slot, MAX_SHORT_STRING):
                return None
            if isinstance(cited_slot, str) and cited_slot:
                parent_slot = cited_slot
    return OpenedRecord(
        sid=sid,
        slot=slot if isinstance(slot, str) else "",
        created_at=created if isinstance(created, int) and not isinstance(created, bool) else 0,
        parent_slot=parent_slot,
    )


def header_unreadable(segment: Path) -> bool:
    """Whether "no header" means the header could not be READ.

    ``read_head`` answers an over-cap or unparseable line 1 with the same "no
    header" it gives a file whose header has not been written yet: the emitter
    creates the file and appends in two writes, and a read can land between them.
    Caching that absence as a verdict is what makes the difference matter -- a
    cached ``None`` is re-served on every later scan while the file's identity
    holds, so a damaged header would be reported complete for as long as the
    file exists.

    The two are told apart by asking whether the file holds any BYTES. An empty
    file is the transient; bytes that produced no header are damage. A header
    that parsed and was then REFUSED is neither -- that one was read, and the
    answer it gave is the refusal.
    """
    try:
        return segment.stat().st_size > 0
    except OSError:
        # Gone between the read and this question: retention, which is an
        # absence, and the caller's own next scan finds the unit evicted.
        return False


def _bounded(value: Any, limit: int) -> bool:
    """Whether *value* is a non-empty string of at most *limit* characters."""
    return isinstance(value, str) and 0 < len(value) <= limit


@dataclass(frozen=True)
class _Head:
    """One unit's cached head: the segment it was read from, that file's
    identity, its size when read, and what it contributed -- ``None`` for a
    refused unit, cached too, so a planted bad line costs one read rather than
    one per scan.

    ``size`` is part of the identity because ``(st_dev, st_ino)`` alone is
    not: a filesystem hands a freed inode number to the next file it creates,
    so a segment removed and recreated under the same name can answer the same
    ``stat``. A segment is append-only and never shrinks, so a file SHORTER
    than it was when read is not that file, whatever its inode says.
    """

    segment: Path
    dev: int
    ino: int
    size: int
    record: OpenedRecord | None
    #: Set when this unit's head was JUDGED DAMAGED rather than read. Cached like
    #: any other verdict so a planted bad line costs one read, and reported every
    #: time it is served: a damaged announce record says nothing about the session's
    #: creator, and serving that as "no creator" is what stops the ancestor rule
    #: dropping a supervising conductor -- a confident wrong holder on an answer
    #: that calls itself complete.
    faulted: bool = False


class SessionTree:
    """The scanner and its per-unit head cache. BLOCKING: it lists a directory
    and stats every unit, so call it off the event loop.

    One instance per reader (the System page's sampler owns one). The cache is
    keyed by unit directory name and validated per scan against the segment's
    path, ``(st_dev, st_ino)`` and the append-only size floor: a segment that is
    gone, replaced, or shorter than when it was read is re-read, an untouched
    one costs a single ``stat``. A unit whose header has landed but whose first
    entry has not -- or is still being written -- is NOT cached, so a log caught
    between its create and its first append, or inside that append, is read
    again on the next scan.

    A scan admits at most :data:`TREE_UNIT_CAP` units: the live sessions' logs
    first (the caller names them), then the store's own order until the cap is
    full. Every loop is cut by the cap -- the probes for named units, the
    listing (which stops one candidate past the cap), the heads cached -- so a
    store of any size costs a scan the cap's worth of work and no more. What lies
    past the cap is neither walked nor counted; that there IS something past it
    lands in :attr:`over_cap`, which every snapshot reports, so a tree that
    could not admit everything never reads as one that had nothing more. Since
    live logs go first, what it misses is logs of closed sessions. A live row
    nests on one of those in exactly one case: a slot restarted since it was
    opened, whose current log carries no ``parent`` (the witness is gone) and
    whose creator is named only by its older, closed log, which competes for the
    cap like any other; that row folds as a root while the store is over the
    cap. Every other live row keeps its edge, unless the live rows alone exceed
    the cap.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._heads: dict[str, _Head] = {}
        self._over_cap = False

    @property
    def over_cap(self) -> bool:
        """Whether the latest scan found more units than :data:`TREE_UNIT_CAP`
        admits -- a fact, not a count. ``False`` until a scan has run."""
        return self._over_cap

    def records(self, preferred: Iterable[str] = ()) -> list[OpenedRecord]:
        """One record per provable session log on disk, unordered.

        Drops whether the scan faulted, so it is the wrong projection for a
        reader that DECIDES on a lineage edge; :meth:`reading` is the caller that
        keeps the bit. This one is the raw-records view over the same locked
        scan, without the fold :meth:`reading` builds.
        """
        return self._records_with_fault(preferred)[0]

    def _records_with_fault(
        self, preferred: Iterable[str] = ()
    ) -> tuple[list[OpenedRecord], bool, bool]:
        """:meth:`records`, plus whether any unit's bytes could not be READ, plus
        whether the population ran past the cap.

        *preferred* names unit ids (the live sessions' ACP session ids) whose
        logs are admitted FIRST, whatever their place in the store's order: the
        rows on screen are what the tree is folded for, so past the cap it is
        the logs of closed sessions that go unread, not a live row's -- as long
        as the live rows themselves fit in the cap, and with the one exception
        the class docstring names (a slot restarted since it was opened nests on
        its older, closed log). The rest of the cap is
        filled from the store's listing (:func:`store.unit_dirs`, excluding
        what was already admitted), which stops one candidate past the cap and
        says only whether anything was left, in :attr:`over_cap`. *preferred*
        is cut at the cap BEFORE it is probed -- ``islice``, so the number of
        stats is bounded by the cap whether or not a log exists for an id -- and
        a gateway running more than the cap in live logged sessions does lose
        lineage on the rows past it.

        The fault bit is accumulated HERE, inside the lock that did the reading,
        and returned rather than stored. One tree serves every reader in the
        process, so a bit left on the instance could be read by a second,
        unlocked call and describe another reader's scan.
        """
        out: list[OpenedRecord] = []
        seen: set[str] = set()
        faulted = False
        with self._lock:
            # Live sessions' units first, by name: one stat each to find them,
            # at most TREE_UNIT_CAP stats however many ids the caller names.
            named: list[Path] = []
            for unit_id in islice(preferred, TREE_UNIT_CAP):
                directory = unit_dir_for(KIND_SESSION, unit_id)
                if directory is not None and directory.name not in seen:
                    seen.add(directory.name)
                    named.append(directory)
            # Then the store's own order for the rest of the cap. The listing
            # stops one candidate past what it was asked for, so a scan never
            # walks, holds or counts more than the cap.
            listed, over_cap, listing_faulted = unit_dirs(
                KIND_SESSION, limit=TREE_UNIT_CAP - len(named), exclude=seen
            )
            self._over_cap = over_cap
            if listing_faulted:
                # Reported by the read that FAILED. A probe of our own cannot
                # stand in for it: `iterdir` yields as it goes, so a failure after
                # the first entry escapes anything that draws one entry and stops,
                # and even a full re-read answers for a different moment.
                faulted = True
            for directory in listed:
                seen.add(directory.name)
            for directory in named + listed:
                record, read_faulted = self._read(directory)
                faulted = faulted or read_faulted
                if record is not None:
                    out.append(record)
            # Evict what this scan did not admit: a unit that is gone, and one
            # that fell past the cap because the population grew in front of it.
            for gone in [name for name in self._heads if name not in seen]:
                del self._heads[gone]
        return out, faulted, over_cap

    def _read(self, directory: Path) -> tuple[OpenedRecord | None, bool]:
        """One unit's record, from the cache when its segment is unchanged, and
        whether the read FAULTED. Caller holds the lock.

        The two values are different facts and a caller needs both. No record
        because this unit has none -- no segment, or a create whose announce has
        not landed -- is the store answering. No record because the bytes could
        not be read is the store failing to answer, and only that makes a scan
        see less than the store holds. Blurring them would let a lineage edge
        vanish on a transient fault while the answer built from it still called
        itself complete.
        """
        name = directory.name
        segment = oldest_segment(directory)
        if segment is None:
            # `oldest_segment` answers an unreadable DIRECTORY with None, the same
            # answer it gives for a unit that genuinely has no segment. Probe which
            # one this is, on the empty path only: a directory that cannot be listed
            # is a fault, one that is absent or truly empty is the store answering.
            try:
                next(iter(directory.iterdir()), None)
            except FileNotFoundError:
                return None, False
            except OSError:
                return None, True
            return None, False
        try:
            stat = segment.stat()
        except OSError:
            return None, True
        cached = self._heads.get(name)
        if (
            cached is not None
            and cached.segment == segment
            and cached.dev == stat.st_dev
            and cached.ino == stat.st_ino
            and cached.size <= stat.st_size
        ):
            return cached.record, cached.faulted
        try:
            header, entry, announced = read_head(segment)
        except OSError:
            # The bytes were not seen, so there is no verdict to cache: a
            # moment's I/O fault, or a unit retention removed between the stat
            # and the open. The next scan reads it again, or finds it gone and
            # evicts it.
            return None, True
        if header is not None and not announced:
            # The create landed, the announce has not: nothing to cache.
            self._heads.pop(name, None)
            return None, False
        if announced and entry is None:
            # The announce record is THERE and could not be read as an entry, so
            # what it said about this session's creator is unknown -- not absent. A
            # missing creator edge is the one shape that stops the ancestor rule
            # dropping a supervising conductor, so serving damage as "no creator" is
            # a confident wrong holder on an answer that calls itself complete.
            #
            # Cached as a JUDGED fault rather than dropped: the log is append-only,
            # so those bytes cannot become readable and re-reading them every scan
            # buys nothing -- but the verdict is a fault every time it is served,
            # which a cached absence would not be.
            #
            # Recorded HERE, and only here, because this is the one arm that knows
            # both the cause and WHICH unit carries it. Every later scan is served by
            # the cache above and never reaches this line, so the operator gets one
            # record per damaged unit instead of one per scan; and because it sits at
            # the producer, it covers every consumer of the fault bit -- the per-unit
            # refusal door and the unit listing alike -- rather than one call site.
            # The other fault arms are deliberately silent: each is transient or
            # re-judged on the next scan, so logging them would be per-scan noise.
            logger.warning(
                "crew log unit %s: its announce record is present and could not be read, "
                "so this session's creator edge is unknown. Lineage readings will report "
                "themselves incomplete for as long as these bytes stand.",
                name,
            )
            self._heads[name] = _Head(
                segment, stat.st_dev, stat.st_ino, stat.st_size, None, faulted=True
            )
            return None, True
        if header is None and header_unreadable(segment):
            # Bytes that produced no header. Nothing is cached for it: a cached
            # absence is re-served on every later scan while the file's identity
            # holds, which would turn one damaged header into a permanent silent
            # omission on a reading that calls itself complete.
            self._heads.pop(name, None)
            return None, True
        record = opened_record(directory, header, entry)
        self._heads[name] = _Head(segment, stat.st_dev, stat.st_ino, stat.st_size, record)
        return record, False

    def reading(self, preferred: Iterable[str] = ()) -> TreeReading:
        """The tree as of this scan, WITH whether that scan saw the whole store.

        Prefer this over :meth:`snapshot` wherever the answer is folded into
        something a consumer acts on. ``incomplete`` is true when a unit's bytes
        could not be read, when the population ran past :data:`TREE_UNIT_CAP`,
        or when the fold itself failed -- three ways of saying a lineage edge may
        be missing, which for a reader that DROPS a candidate on the strength of
        an edge is the difference between a right answer and a confident wrong
        one.

        The flag travels IN the reading rather than on the instance, and is taken
        from the same call that produced the nodes. One tree serves every reader
        in the process, so a flag read in a second, unlocked call could belong to
        another reader's scan, and the caller would render a partial lineage as a
        complete one.
        """
        try:
            records, faulted, over_cap = self._records_with_fault(preferred)
            return TreeReading(
                nodes=fold_tree(records),
                incomplete=faulted or over_cap,
                records=tuple(records),
            )
        except Exception:  # pragma: no cover -- defensive; the store calls are guarded
            logger.warning("session tree scan failed; reporting no lineage", exc_info=True)
            return TreeReading(nodes={}, incomplete=True)

    def snapshot(self, preferred: Iterable[str] = ()) -> dict[str, TreeNode]:
        """The tree as of this scan: :func:`fold_tree` over :meth:`records`,
        with *preferred* (the live sessions' unit ids) admitted first.

        Never raises: a lineage read is decoration on the pages that show it,
        and a store fault must not take the page down. The fault is logged and
        the tree is reported empty, which every consumer renders as "no
        creator known", the same as before this reader existed.

        Drops the completeness of the scan. That is right for a page that shows
        lineage as decoration and wrong for anything that DECIDES on an edge:
        use :meth:`reading` there.
        """
        return self.reading(preferred).nodes


def parent_payload(
    node: TreeNode | None, live_key_of: Mapping[str, str], own_key: str
) -> dict[str, Any] | None:
    """The ``parent`` a session row carries on the wire, or ``None``.

    *live_key_of* maps a slot key -- bare, and in its full session-key spelling
    -- to the session key of a LIVE row. ``key`` is the creator's live session
    key when the creator is running and the edge can be followed, so a table
    nests the child under it exactly as it nests a task; it is ``None`` when the
    creator is not running (the child stays a root), when the node sits on a
    cycle, and when the citation would point at the row itself. ``slot`` is the
    child's own citation and survives all of those.
    """
    if node is None or node.parent_slot is None:
        return None
    parent_key = live_key_of.get(node.parent_slot)
    if node.cycle or parent_key == own_key:
        parent_key = None
    return {"slot": node.parent_slot, "key": parent_key}

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

Three pieces, kept apart so each is testable on its own:

* :func:`fold_tree` -- pure. :class:`OpenedRecord` in, one :class:`TreeNode`
  per slot out.
* :func:`fold_slot_chain` -- pure. The same records in, one slot's logs out,
  newest first, with the reason the walk stopped.
* :class:`SessionTree` -- the scanner. It reads the header and the first entry
  of every session log's oldest surviving segment and keeps that head cached
  per unit, for at most :data:`TREE_UNIT_CAP` units per scan. The store never
  rewrites a written line, so the two lines a scan reads are immutable for as
  long as the segment exists; the cache therefore needs no mtime and is dropped
  only when the segment is gone (retention, removal) or the file was replaced
  (a new inode under the same name, or a shorter file under a recycled one).
  Asked for edges, it also reads the END of each unit's newest segment, cached
  on the same identity plus an exact size, because that answer moves with every
  append where the head's cannot.

A session's creating edge is in its head, and that is where the head read is
enough. The emitter writes ``parent`` from a process-local mint witness that
exists before the child's first turn or never, so the entry that CREATED the log
carries the parent whenever any entry does, and a later re-attach in the same
process can only repeat it. The fold still applies the wider rule -- the parent is
taken from ANY record of a slot that carries one, and a record without one never
retracts it -- so a slot whose later logs were opened after a gateway restart
(witness gone, no ``parent``) folds to the parent its first log recorded.

A session can also be MOVED after it was opened, and those records are not in the
head. ``session/adopted`` says another session took this one over and
``session/released`` says its parent let it go, both written on the session that
moved -- the same side the creating edge is written on, so one entry moves a whole
subtree because descendants cite this slot rather than a path through it. They are
:class:`EdgeRecord` rather than :class:`OpenedRecord` because they are a different
kind of statement: an opened record says who opened the session, which stays true
and is never rewritten, while a decision says who holds it NOW and replaces the
citation outright. :func:`fold_tree` applies the newest decision per slot over the
creating citations and then runs its cycle colouring over the result, which is
what guards a takeover recorded against a reading of the tree that has since
moved.

The tree is keyed by SLOT. ``parent.sid`` on the entry is the creator's ACP
session id at the moment of creation -- an audit citation for a reader of the
logs themselves, not a tree key: a slot outlives its ACP session, and the live
row a child nests under is the slot's. This reader does not read it.

The same first entry carries a SECOND edge, on a different axis, and this module
reads both. ``parent {slot, sid?}`` is PARENTHOOD, between two slots. ``previous
{sid}`` is SUCCESSION, between two logs of ONE slot: a slot outlives its ACP
session, so a supersede -- a restart whose ``session/load`` does not re-attach, a
reset, an agent, model or effort switch, a compaction, a provider swap -- gives
that slot a new log under a new id, and the new log names the one it replaced.
Parenthood is keyed by slot and folded over the whole collection; succession is
keyed by ACP session id and WALKED from one log backwards, because its whole
purpose is the order the slot's logs came in, which a slot-keyed fold cannot
express. :func:`fold_slot_chain` is that walk, and it is the third piece.

The walk enforces the edge's own specification rather than trusting it. ``previous``
means the log the SAME slot was writing, so a step is taken only onto a log whose
immutable header slot matches the one the walk started on. The emitter checks this
too when it writes the edge, and that is not a reason for the reader to skip it: the
id reaches the emitter from an agent-writable mapping, logs written before that
check existed are still on disk, and a walk that followed a foreign edge would
present another slot's turns, costs and approvals as this slot's own. A step it
refuses ends the walk and says so, which is strictly better than a wrong answer
that calls itself whole.
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
    find_last_tree_edge,
    newest_segment,
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

#: The two entry types that MOVE a slot after it was opened: one session took
#: another over, and a parent let one go. Both are written on the session that
#: moved -- the same side of the edge ``session/opened.parent`` is written on --
#: so one entry moves a whole subtree, because descendants cite this slot and not
#: a path through it.
TYPE_ADOPTED: Final[str] = "session/adopted"
TYPE_RELEASED: Final[str] = "session/released"

#: Both of them, for a reader deciding whether an entry carries an edge at all.
EDGE_TYPES: Final[frozenset[str]] = frozenset({TYPE_ADOPTED, TYPE_RELEASED})

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

#: How many logs ONE slot-succession walk visits, counting the log it starts on.
#: Separate from :data:`TREE_UNIT_CAP` because it bounds a different thing: the
#: cap above bounds how much of the store a SCAN admits, this one bounds how far
#: a walk steps through what that scan already produced. A walk is over records
#: held in memory, so the cost it refuses is not I/O but an unbounded loop over
#: records whose edges an agent-writable mapping can influence. A slot reaches a
#: new log only by being superseded -- a restart, a reset, a model switch -- so a
#: real slot's population is orders below this, and a walk that hits the bound
#: reports :data:`CHAIN_END_CAP` rather than pretending it reached the first log.
SLOT_CHAIN_CAP: Final[int] = 512

#: Why a succession walk stopped. Exactly one holds, which is why this is one
#: field rather than several flags: "ran off the end", "refused a step" and "hit
#: the bound" are alternatives, and a set of bools would make their impossible
#: combinations representable and force every reader to check all of them.
#:
#: Only :data:`CHAIN_END_FIRST` means the walk reached the slot's whole life. The
#: other four each mean there is more the walk could not reach, and they are kept
#: apart because the remedies differ: retention took a log, a step was refused as
#: another slot's, the edges form a loop, the bound was hit.
CHAIN_END_FIRST: Final[str] = "first"
#: The cited log answered no record -- retention removed it, or its header was
#: refused. An absence, and the ordinary end of an old chain.
CHAIN_END_MISSING: Final[str] = "missing"
#: The cited log exists and its immutable header names a DIFFERENT slot, so the
#: step is REFUSED. Not an absence: a record that should not have been written,
#: and the one end reason that reports damage rather than age.
CHAIN_END_FOREIGN: Final[str] = "foreign"
#: The cited log is already on this walk. Reachable only through forged or
#: damaged records, since a log cannot have superseded its own successor.
CHAIN_END_CYCLE: Final[str] = "cycle"
#: :data:`SLOT_CHAIN_CAP` visits were made and the chain had not ended.
CHAIN_END_CAP: Final[str] = "cap"
#: The log the walk was ASKED to start from answered no usable record, so there
#: is no slot to walk and no citation to report. Distinct from
#: :data:`CHAIN_END_MISSING`, which is a step that failed after a real start.
CHAIN_END_UNKNOWN: Final[str] = "unknown"


@dataclass(frozen=True)
class OpenedRecord:
    """What one session log contributes: its header identity, the creator
    slot its first entry names (``None`` for a session nobody created), and the
    log of the SAME slot this one superseded (``None`` when it is that slot's
    first, which the emitter writes as an omitted key so the two are distinct).

    The two citations are on different axes and neither implies the other: a slot
    that nobody created still supersedes its own earlier logs, and a slot's first
    log still names its creator.
    """

    sid: str
    slot: str
    created_at: int
    parent_slot: str | None = None
    previous_sid: str | None = None


@dataclass(frozen=True)
class EdgeRecord:
    """One later DECISION about where a slot hangs: an adoption, or a release.

    ``parent_slot`` is the slot the session now hangs under, and ``None`` is the
    release -- the one record that means "no parent", as opposed to an
    :class:`OpenedRecord` carrying no parent, which only means the entry did not
    repeat a creator.

    ``sid`` and ``seq`` order the decisions for one slot, and the pair is chosen so
    that ordering never rests on a clock. ``seq`` is the writer-assigned position in
    that log and it only ever increases, so two decisions in ONE log are ordered by the
    store itself; a clock that steps backward between them -- an NTP correction, a VM
    resume -- cannot invert them, which a timestamp comparison can. ``sid`` says WHICH
    log, because a slot outlives its ACP session and seq starts again in the log that
    replaces it: decisions from two different logs are ordered by which log is newer,
    never by comparing seqs that are not comparable.

    ``at`` is the entry's own millisecond and is NOT the ordering key. It is the audit
    reading -- when this happened -- and the last resort for placing two logs whose
    order nothing else establishes.

    ``sid`` is load-bearing a third way: it is the unit this decision was read
    from, so a projection dropping a removed unit knows which edges went with it.
    """

    slot: str
    parent_slot: str | None
    at: int
    sid: str
    #: Position in the citing log. Defaults to 0 so a hand-built record in a test or a
    #: checkpoint written without it still compares -- 0 simply loses to any real entry
    #: from the same log, which is the safe direction.
    seq: int = 0


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
    #: Every later DECISION this scan read -- at most one per unit, the newest in
    #: that unit's tail window. Empty when the caller did not ask for them, which is
    #: why ``nodes`` is the value to read rather than these: a consumer cannot tell a
    #: scan that found no adoptions from one that never looked, and does not need to.
    edges: tuple[EdgeRecord, ...] = ()


@dataclass(frozen=True)
class ChainReading:
    """One scan's succession chain, and whether that scan saw every unit.

    The pair exists for the same reason :class:`TreeReading` is a pair, and the
    consequence is sharper: a chain's ``ended`` reason is computed from the records
    a scan produced, so a predecessor the scan never admitted is indistinguishable
    -- to the walk -- from one retention removed. ``incomplete`` is the only thing
    that separates "the slot's chain ends here" from "my scan ends here", and a
    reader that totals a whole life must not present the second as the first.
    """

    chain: SlotChain
    incomplete: bool


def edge_supersedes(
    candidate: EdgeRecord,
    held: EdgeRecord,
    log_rank: Mapping[str, tuple[int, int, str]] | None = None,
) -> bool:
    """Whether *candidate* is the LATER decision of the two. Pure, and clock-free
    wherever the store can answer instead.

    Two cases, and they are different questions:

    * **Same log.** ``seq`` decides, and only ``seq``. It is assigned by the writer and
      only increases, so this answer cannot be inverted by a clock that steps backward
      between the two appends -- which is an ordinary operational event (an NTP
      correction, a VM resume), not an exotic one, and is exactly what a timestamp
      comparison gets wrong.
    * **Different logs.** Seqs are not comparable: a slot outlives its ACP session and
      the log that replaces it starts its own sequence. So the newer LOG wins, and
      *log_rank* is how a caller states which that is -- :func:`log_rank_of`, whose
      leading term is the log's depth in the ``previous_sid`` succession chain the store
      itself wrote, so this comparison is clock-free here too for any two logs the chain
      relates. :func:`fold_tree` sorts its records by that same map, so the decisions
      inherit the log order the tree is built from rather than inventing a second one. A
      log the caller cannot rank falls back to ``(at, sid)``: it is the weakest answer
      here and it is confined to the case where nothing better exists.

    Ties answer ``False``: an equal record does not supersede, which is what keeps a
    replay of a decision already held from being a change.
    """
    if candidate.sid == held.sid:
        return candidate.seq > held.seq
    if log_rank is not None:
        candidate_rank = log_rank.get(candidate.sid)
        held_rank = log_rank.get(held.sid)
        if candidate_rank is not None and held_rank is not None:
            return candidate_rank > held_rank
    return (candidate.at, candidate.sid) > (held.at, held.sid)


def latest_edges(
    edges: Iterable[EdgeRecord], log_rank: Mapping[str, tuple[int, int, str]] | None = None
) -> dict[str, EdgeRecord]:
    """The newest decision per slot. Pure, and order-independent.

    Newest by :func:`edge_supersedes`, which asks the store rather than the clock --
    see there for why. Order-independence is the property that matters: the records
    reach a fold from a checkpoint, from a tail replay and from the writer itself, in
    whatever order those arrive, and a release that lost a race with the adoption it
    undoes would put a session back under a parent that already let it go.
    """
    newest: dict[str, EdgeRecord] = {}
    for edge in edges:
        if not edge.slot:
            # An edge is keyed by slot; a decision with no slot addresses nothing.
            continue
        held = newest.get(edge.slot)
        if held is None or edge_supersedes(edge, held, log_rank):
            newest[edge.slot] = edge
    return newest


def _chain_step(by_sid: "Mapping[str, OpenedRecord]", sid: str) -> str | None:
    """The predecessor *sid*'s log succeeds, or ``None`` where the chain stops here.

    One step, used by every walk over ``previous_sid``, because a walk that takes the
    step differently is a walk that can disagree about which log is newer.

    Three links stop the walk rather than being followed, and only one of them is an
    ordinary shape: a log with no predecessor is the start of its chain. The other two
    are damaged or forged records -- a predecessor that is not among *by_sid* (retention
    has removed it, or it never existed), and a predecessor belonging to ANOTHER slot.
    The foreign step is refused because this chain answers "which of THIS slot's logs is
    newer": a link off the slot does not extend that history, and following it would
    both borrow another slot's depth and let a record a different slot's writer produced
    reorder this slot's decisions. :func:`slot_chain` refuses the same step for the same
    reason, and reports it as ``CHAIN_END_FOREIGN``.
    """
    previous = by_sid[sid].previous_sid
    if not previous:
        return None
    step = by_sid.get(previous)
    if step is None or step.slot != by_sid[sid].slot:
        return None
    return previous


def _succession_depth(by_sid: "Mapping[str, OpenedRecord]") -> dict[str, int]:
    """Each log's distance from the start of the chain its ``previous_sid`` links it into.

    ``session/opened`` records which session this one REPLACED, so a slot's logs form a
    chain the store itself wrote -- and walking it is how two of them are ordered without
    asking a clock. Depth 0 is a log whose predecessor is not among *by_sid*, which covers
    both a first session and one whose predecessor retention has already removed: either
    way it is the oldest log still readable, which is the only order this can be about.

    Three shapes forged or damaged records can take are handled first, because each would
    otherwise make the answer depend on which sid the walk happened to reach first, and
    :func:`fold_tree` promises the same tree for any input order. A ``previous_sid`` cycle
    has no start, so EVERY log on it is given depth 0 and the clock tiebreak places them --
    breaking the loop at whichever member came first would rank them by iteration order. A
    link naming a log that is not here is treated as absent. A link naming a log of ANOTHER
    slot is treated as absent too: this depth orders one slot's logs against each other, so
    a step off the slot does not measure that, and following it would let one slot's history
    set another slot's ranks -- see :func:`_chain_step`.

    Iterative, so a slot with a very long session history cannot exhaust the stack, and each
    chain is measured once.
    """
    on_cycle: set[str] = set()
    settled: set[str] = set()
    for start in by_sid:
        if start in settled:
            continue
        path: list[str] = []
        seen_here: dict[str, int] = {}
        cursor: str | None = start
        while cursor is not None and cursor in by_sid and cursor not in settled:
            if cursor in seen_here:
                # Closed back onto this same walk: everything from that point on is a loop.
                on_cycle.update(path[seen_here[cursor] :])
                break
            seen_here[cursor] = len(path)
            path.append(cursor)
            cursor = _chain_step(by_sid, cursor)
        settled.update(path)

    depth: dict[str, int] = {}
    for start in by_sid:
        if start in depth:
            continue
        chain: list[str] = []
        cursor = start
        while cursor is not None and cursor in by_sid and cursor not in depth:
            chain.append(cursor)
            if cursor in on_cycle:
                cursor = None
                break
            cursor = _chain_step(by_sid, cursor)
        base = depth[cursor] if cursor is not None and cursor in depth else -1
        for step, sid in enumerate(reversed(chain)):
            depth[sid] = 0 if sid in on_cycle else base + 1 + step
    return depth


def log_rank_of(records: Iterable[OpenedRecord]) -> dict[str, tuple[int, int, str]]:
    """Each log's place in the order :func:`fold_tree` folds them in. Clock-free within a
    succession chain.

    ``sid -> (succession_depth, created_at, sid)``. The DEPTH leads, because it is the one
    part the store wrote: ``previous_sid`` on ``session/opened`` says which session this
    log replaced, so two logs of one slot are ordered by that link and not by a header
    timestamp a backward clock step can invert. ``created_at`` and ``sid`` stay behind it
    as the tiebreak for two logs the chain does not relate -- two chain starts, a
    predecessor retention removed -- which is the weakest answer available and is now
    confined to the case where the chain has nothing to say.

    One function for this, used BOTH to sort the records and to rank the decisions, so the
    tree cannot disagree with itself about which of its own logs is newer.
    """
    by_sid = {record.sid: record for record in records if record.sid}
    depth = _succession_depth(by_sid)
    return {sid: (depth.get(sid, 0), record.created_at, sid) for sid, record in by_sid.items()}


def fold_tree(
    records: Iterable[OpenedRecord], edges: Iterable[EdgeRecord] = ()
) -> dict[str, TreeNode]:
    """Every slot's node, from the records of every session log. Pure.

    Input order does not matter: records are folded oldest log first, by the
    header's ``createdAt`` and then id, so two scans of the same files agree.
    A slot's parent is the one its OLDEST record carrying a parent names; a
    record with no parent never retracts it. A record whose header has no slot
    has no place in a slot-keyed tree and is dropped.

    *edges* are the later decisions -- an adoption, a release -- and each one
    REPLACES the creating citation for its slot outright rather than being merged
    with it. The two are not rival readings of one fact: the opened entry says who
    opened the session, which stays true and is never rewritten, and an edge says
    who holds it now. Only the newest decision per slot is applied
    (:func:`latest_edges`), and a release applies as "no parent" -- the one way a
    parent is taken away.

    An edge for a slot with no log of its own is dropped, for the same reason a
    cited creator with no log is not followed: this fold's nodes ARE the slots that
    have logs, so an edge onto anything else would name a node that does not exist.

    An edge is FOLLOWED only when it lands on a slot that has a log of its own.
    A cited creator with no log is a citation, not a place in the tree, so the
    child is a root that still carries its ``parent``. A cycle -- reachable
    through forged or damaged records, and now also through a takeover recorded
    against a stale reading of the tree -- marks every slot on it, so a consumer
    nests none of them; a slot hanging off a member keeps its edge to it. The
    guard runs HERE, over the applied edges, and not only where an edge is
    written: the writer checks the tree as it stands at that moment, while a fold
    sees a checkpoint and a replayed tail whose decisions can reach it in an order
    no writer ever saw.
    """
    # ONE order for the records and for the decisions below, from ``log_rank_of``: within a
    # slot's succession chain it is the ``previous_sid`` link the store wrote, so neither
    # half can be inverted by a clock that steps backward between two sessions. Sorting
    # here by a header timestamp while ranking the decisions by the chain would be the two
    # disagreeing orderings the decision comment below warns about.
    #
    # Materialised first because *records* is an ITERABLE and is now read twice -- ranking
    # needs the whole population before any record can be placed in it. A caller handing
    # over a generator (``reversed(...)``, a scanner's yield) would otherwise find it spent
    # by the sort and fold an empty tree.
    population = list(records)
    rank = log_rank_of(population)
    ordered = sorted(
        population, key=lambda record: rank.get(record.sid, (0, record.created_at, record.sid))
    )
    has_log: set[str] = set()
    cited: dict[str, str | None] = {}
    for record in ordered:
        if not record.slot:
            continue
        has_log.add(record.slot)
        if record.parent_slot and record.slot not in cited:
            cited[record.slot] = record.parent_slot

    # The decisions are placed by the SAME log order the records above were folded in,
    # handed to the comparison rather than re-derived inside it (see
    # :func:`edge_supersedes`): two orderings of one slot's logs could disagree, and the
    # tree would then contradict itself about which of its own records is newer.
    for slot, edge in latest_edges(edges, rank).items():
        if slot in has_log:
            cited[slot] = edge.parent_slot

    edge_of: dict[str, str] = {}
    on_cycle: set[str] = set()
    for slot, parent_slot in cited.items():
        if not parent_slot:
            continue
        if parent_slot == slot:
            on_cycle.add(slot)
        elif parent_slot in has_log:
            edge_of[slot] = parent_slot
    on_cycle |= _cycle_members(edge_of)

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


@dataclass(frozen=True)
class SlotChain:
    """One slot's logs in succession order, newest first, and why the walk stopped.

    ``sids`` always begins with the log the walk was asked to start from, so a slot
    with one log answers a single id and :data:`CHAIN_END_FIRST`. It is empty only
    for :data:`CHAIN_END_UNKNOWN`, where that starting log answered no record.

    ``ended`` is the load-bearing field and a reader that joins folds MUST read it.
    ``sids`` alone cannot say whether it is the slot's whole life: a chain cut short
    by retention, a refused step, a loop or the bound looks exactly like a complete
    one from the ids. Only :data:`CHAIN_END_FIRST` means whole.

    ``cited`` is the id the walk could not follow -- present for
    :data:`CHAIN_END_MISSING`, :data:`CHAIN_END_FOREIGN` and
    :data:`CHAIN_END_CYCLE`, and ``None`` otherwise. It is kept because it is
    evidence: a reader reporting an incomplete chain can name the log it stopped
    at, and for :data:`CHAIN_END_FOREIGN` that id is the only record of a citation
    that should never have been written.
    """

    slot: str
    sids: tuple[str, ...]
    ended: str
    cited: str | None = None


def fold_slot_chain(records: Iterable[OpenedRecord], head_sid: str) -> SlotChain:
    """The logs of *head_sid*'s slot, newest first, by walking ``previous``. Pure.

    Bounded by :data:`SLOT_CHAIN_CAP` visits, cycle-guarded by the set of ids
    already visited, and restricted to ONE slot: a step is taken only onto a
    record whose header slot equals the slot the walk started on.

    The same-slot rule is the reader's own enforcement of what ``previous`` means,
    not a re-check of something already guaranteed. The id reaches the emitter from
    an agent-writable mapping; the emitter verifies the candidate's header before
    writing the edge, but logs written before that check existed are still on disk
    and a damaged entry can carry anything. Following a foreign edge would join
    another slot's turns, costs and approvals into this slot's whole-life figure --
    a wrong answer presenting itself as complete -- so the step is refused and the
    walk ends at :data:`CHAIN_END_FOREIGN`.

    A cited id that no record answers is NOT the same refusal and gets its own
    reason: retention removing an old log is the ordinary way a long-lived slot's
    chain ends, and reporting that as damage would cry wolf on every healthy store.

    Order is the edges', never a timestamp. ``created_at`` is wall clock, so a
    backward clock step across a restart gives the newer log the earlier stamp and
    two creates inside one millisecond tie; the edge inverts in neither case, which
    is the whole reason it was recorded.
    """
    by_sid: dict[str, OpenedRecord] = {}
    for record in records:
        # First writer wins, so a duplicate id -- which the store's own layout makes
        # impossible and only a hand-built record list can produce -- cannot make
        # the same walk answer differently on two runs over the same input.
        if record.sid and record.sid not in by_sid:
            by_sid[record.sid] = record

    head = by_sid.get(head_sid)
    if head is None or not head.slot:
        # No record, or one whose header carried no slot. Either way there is no
        # slot to hold the walk to, and a walk with no same-slot rule is exactly
        # the foreign-edge hazard above, so it does not start.
        return SlotChain(slot="", sids=(), ended=CHAIN_END_UNKNOWN)

    slot = head.slot
    sids: list[str] = [head.sid]
    seen: set[str] = {head.sid}
    cursor = head
    while True:
        cited = cursor.previous_sid
        if cited is None:
            return SlotChain(slot=slot, sids=tuple(sids), ended=CHAIN_END_FIRST)
        if cited in seen:
            return SlotChain(slot, tuple(sids), CHAIN_END_CYCLE, cited)
        if len(sids) >= SLOT_CHAIN_CAP:
            # Checked BEFORE the lookup so the bound limits the work, not just the
            # answer's length.
            return SlotChain(slot, tuple(sids), CHAIN_END_CAP, None)
        step = by_sid.get(cited)
        if step is None:
            return SlotChain(slot, tuple(sids), CHAIN_END_MISSING, cited)
        if step.slot != slot:
            return SlotChain(slot, tuple(sids), CHAIN_END_FOREIGN, cited)
        sids.append(step.sid)
        seen.add(step.sid)
        cursor = step


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

    The superseded log is taken from the same entry's ``previous.sid``, and here
    the id IS the value: succession is an edge between logs, so the citation a
    walk follows is the id and there is no slot key to prefer. It is bounded by
    ``MAX_ACP_SESSION_ID_LEN`` rather than ``MAX_SHORT_STRING`` for that reason.
    An entry that names no ``previous`` contributes none, which
    :func:`fold_slot_chain` reads as "this is the slot's first log" -- the
    emitter omits the key rather than writing a null exactly so that a reader can
    tell that from a predecessor it failed to record.
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
    previous_sid: str | None = None
    if entry.type == TYPE_OPENED:
        parent = entry.data.get("parent")
        if isinstance(parent, dict):
            cited_slot = parent.get("slot")
            if cited_slot is not None and not _bounded(cited_slot, MAX_SHORT_STRING):
                return None
            if isinstance(cited_slot, str) and cited_slot:
                parent_slot = cited_slot
        # The superseded log of this same slot. Bounded by the SESSION ID limit,
        # not the short-string one: it is an ACP session id, the same kind of
        # value as ``sid`` above, and it is retained in the scanner's cache for as
        # long as the log exists. Refusing the whole record past the bound rather
        # than dropping just this key is deliberate and matches the parent above:
        # a value the gateway never writes means this entry is not the emitter's,
        # so nothing on it should be believed -- and a truncated id would be a
        # different key, which would resolve to no log or, worse, to another one.
        previous = entry.data.get("previous")
        if isinstance(previous, dict):
            cited_sid = previous.get("sid")
            if cited_sid is not None and not _bounded(cited_sid, MAX_ACP_SESSION_ID_LEN):
                return None
            if isinstance(cited_sid, str) and cited_sid:
                previous_sid = cited_sid
    return OpenedRecord(
        sid=sid,
        slot=slot if isinstance(slot, str) else "",
        created_at=created if isinstance(created, int) and not isinstance(created, bool) else 0,
        parent_slot=parent_slot,
        previous_sid=previous_sid,
    )


def edge_record(slot: str, sid: str, entry: Entry | None) -> EdgeRecord | None:
    """The decision *entry* contributes for the log identified by *slot* and *sid*,
    or ``None``.

    The identity is passed IN rather than re-derived, because both callers already
    hold it from a head they proved: the scanner has the unit's
    :class:`OpenedRecord`, and the replay has the header it just read and folded back
    to the directory. Re-reading the header here would double the reads on the one
    path where reads are the cost.

    Both strings are bounded anyway. This function is the door into a fold's state,
    and a door that trusts its caller is only as safe as its least careful one; the
    limits are the ones :func:`opened_record` applies, so no path admits a value
    another path would refuse. Bounds REFUSE rather than truncate: a truncated slot
    key is a different key, which matches nothing or matches another session.

    An adoption must name a parent ``slot``. One that does not is refused rather
    than read as a release: the entry that means "no parent" is
    :data:`TYPE_RELEASED`, and taking the strongest possible meaning from a
    malformed entry is how a fold detaches a subtree nobody asked it to.

    ``previous_parent`` is not read. It is audit -- who held the session before --
    and the current edge is stated once, by ``parent``, so a reader cannot find two
    answers to one question inside a single entry.
    """
    if entry is None or entry.type not in EDGE_TYPES:
        return None
    if not _bounded(slot, MAX_SHORT_STRING) or not _bounded(sid, MAX_ACP_SESSION_ID_LEN):
        return None
    parent_slot: str | None = None
    if entry.type == TYPE_ADOPTED:
        parent = entry.data.get("parent")
        if not isinstance(parent, dict):
            return None
        cited_slot = parent.get("slot")
        if not _bounded(cited_slot, MAX_SHORT_STRING):
            return None
        parent_slot = cited_slot
    at = entry.time
    seq = entry.seq
    return EdgeRecord(
        slot=slot,
        parent_slot=parent_slot,
        at=at if isinstance(at, int) and not isinstance(at, bool) else 0,
        sid=sid,
        # The store's own position for this line, which is what orders two decisions in
        # one log without consulting a clock.
        seq=seq if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0 else 0,
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


@dataclass(frozen=True)
class _Edge:
    """One unit's cached DECISION: the segment its tail was read from, that file's
    identity, its size when read, and what the tail said -- ``None`` for a unit
    holding no decision, cached too, so a log with none costs one read rather than
    one per scan.

    ``size`` is compared for EQUALITY here, where :class:`_Head` compares it as a
    floor. The two lines a head read returns are immutable for as long as the file
    exists, so growth cannot change that answer; a tail answer is about the END of
    the file, and every append moves it. A segment that grew must be read again, and
    one that SHRANK is a different file on a recycled inode.
    """

    segment: Path
    dev: int
    ino: int
    size: int
    record: EdgeRecord | None


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
        self._edges: dict[str, _Edge] = {}
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
        self, preferred: Iterable[str] = (), *, with_edges: bool = False
    ) -> tuple[list[OpenedRecord], list[EdgeRecord], bool, bool]:
        """:meth:`records`, plus the later decisions when asked for, plus whether any
        unit's bytes could not be READ, plus whether the population ran past the cap.

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

        ``with_edges`` is OFF by default, and that is a cost decision rather than a
        correctness one. A decision lives at the END of a unit's log, so reading it
        means a second ``stat`` per unit and a bounded read of every unit whose log
        has grown -- worth paying once, on the cold start that has no checkpoint to
        replay, and not worth paying on a succession walk, which reads a slot's own
        chain and has no use for where that slot hangs.

        The fault bit is accumulated HERE, inside the lock that did the reading,
        and returned rather than stored. One tree serves every reader in the
        process, so a bit left on the instance could be read by a second,
        unlocked call and describe another reader's scan.
        """
        out: list[OpenedRecord] = []
        edges: list[EdgeRecord] = []
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
                if with_edges and record is not None:
                    # Only for a unit whose head PROVED itself. An edge is keyed by
                    # slot and the head record is where this scan learned the slot,
                    # so a unit with no usable head has nothing to key a decision by
                    # -- and the fold would drop such an edge anyway, since its nodes
                    # are the slots that have logs.
                    edge, edge_faulted = self._read_edge(directory, record)
                    faulted = faulted or edge_faulted
                    if edge is not None:
                        edges.append(edge)
            # Evict what this scan did not admit: a unit that is gone, and one
            # that fell past the cap because the population grew in front of it.
            for gone in [name for name in self._heads if name not in seen]:
                del self._heads[gone]
            for gone in [name for name in self._edges if name not in seen]:
                del self._edges[gone]
        return out, edges, faulted, over_cap

    def _read_edge(self, directory: Path, head: OpenedRecord) -> tuple[EdgeRecord | None, bool]:
        """One unit's newest DECISION, from the cache when its newest segment is
        byte-identical to the one read, and whether the read FAULTED. Caller holds
        the lock.

        The two values are the two facts :meth:`_read` returns and they are kept
        apart for the same reason: "this unit records no adoption" is the store
        answering, and "its bytes could not be read" is the store failing to. Only
        the first is cached, and a caller that folds a tree needs to know which it
        got, because a decision it could not see renders as the session still
        hanging where it was.

        *head* is the record :meth:`_read` produced for this unit, which is what
        proved the header folds back to this directory and is where the slot and the
        id come from. So no header is read here.

        The read is the WHOLE log's, not one tail window. A decision the window cannot
        reach is not a missing edge but a wrong one -- the fold falls back to the
        creating edge and shows the session under a parent that released it -- and this
        scan is the cold path that has no checkpoint to recover it from. The cache is
        still keyed on the newest segment's exact size, so a unit that has not been
        appended to since the last scan costs one ``stat``.
        """
        name = directory.name
        try:
            segment = newest_segment(directory)
        except OSError:
            # The listing failed, so this scan has learned NOTHING about whether the unit
            # records a decision. Reported as incomplete and with no verdict: the cached
            # entry is left alone, because dropping it would replace a possibly-correct
            # edge with the creating one, and a reader that DECIDES on an edge is told the
            # reconciliation did not complete.
            return None, True
        if segment is None:
            # No surviving segment: absent or empty, which IS an answer -- nothing on disk
            # records a decision for this unit, so a stale cached edge must not outlive it.
            self._edges.pop(name, None)
            return None, False
        try:
            stat = segment.stat()
        except OSError:
            return None, True
        cached = self._edges.get(name)
        if (
            cached is not None
            and cached.segment == segment
            and cached.dev == stat.st_dev
            and cached.ino == stat.st_ino
            and cached.size == stat.st_size
        ):
            return cached.record, False
        try:
            entry = find_last_tree_edge(directory)
        except (OSError, ValueError):
            # The bytes were not seen, so there is no verdict to cache -- a moment's
            # I/O fault, or a unit removed between the stat and the open.
            # ``ValueError`` is caught here and not on the head read because a tail
            # scan parses a window it may have entered mid-line.
            return None, True
        record = edge_record(head.slot, head.sid, entry)
        self._edges[name] = _Edge(segment, stat.st_dev, stat.st_ino, stat.st_size, record)
        return record, False

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

    def reading(self, preferred: Iterable[str] = (), *, with_edges: bool = False) -> TreeReading:
        """The tree as of this scan, WITH whether that scan saw the whole store.

        ``with_edges`` also reads each admitted unit's newest tree DECISION -- an
        adoption, a release -- from its tail, and folds it. Off by default because it
        costs a second ``stat`` per unit and a bounded read of every unit that has
        grown since the last scan: the caller that needs it is the projection's cold
        start, which has no checkpoint to take those decisions from, and it runs once
        per process. A reading taken WITHOUT it describes where every slot was
        opened, which is a different answer from where it hangs now, so a consumer
        that shows the tree must ask for them.

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
            records, edges, faulted, over_cap = self._records_with_fault(
                preferred, with_edges=with_edges
            )
            return TreeReading(
                nodes=fold_tree(records, edges),
                incomplete=faulted or over_cap,
                records=tuple(records),
                edges=tuple(edges),
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

    def chain(self, head_sid: str, preferred: Iterable[str] = ()) -> ChainReading:
        """One slot's succession chain as of this scan, WITH whether that scan saw
        the whole store.

        *head_sid* is the log to walk back from -- for a dashboard caller, the unit
        a slot key resolves to. It is admitted FIRST, ahead of *preferred*, because
        a walk that cannot read its own starting log answers
        :data:`CHAIN_END_UNKNOWN` and nothing else is worth scanning for.

        ``incomplete`` matters more here than it does for the tree, and a caller
        must not collapse it into ``ended``. A predecessor this scan did not admit
        -- past :data:`TREE_UNIT_CAP`, or a unit whose bytes faulted -- is absent
        from the records, so the walk reports :data:`CHAIN_END_MISSING` for a log
        that is on disk and readable. The walk cannot tell those apart; only the
        scan can, and it says so here. So an incomplete reading downgrades every
        end reason except :data:`CHAIN_END_FIRST` to "this is where MY scan
        stopped", which is why the two travel in one object taken from one call.

        Never raises, for the reason :meth:`snapshot` does not: a whole-life figure
        is an enrichment, and a store fault must not take its page down.
        """
        try:
            records, _, faulted, over_cap = self._records_with_fault([head_sid, *preferred])
            return ChainReading(
                chain=fold_slot_chain(records, head_sid), incomplete=faulted or over_cap
            )
        except Exception:  # pragma: no cover -- defensive; the store calls are guarded
            logger.warning(
                "session chain scan failed for %s; reporting no succession",
                head_sid,
                exc_info=True,
            )
            return ChainReading(
                chain=SlotChain(slot="", sids=(), ended=CHAIN_END_UNKNOWN), incomplete=True
            )


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

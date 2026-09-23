"""The session tree as a PROJECTION: a fold that applies deltas and never rescans.

:mod:`kiro_crew.crew_log.session_tree` re-derives the whole tree from disk on every
read -- a directory listing plus roughly four syscalls per unit, for up to
:data:`~kiro_crew.crew_log.session_tree.TREE_UNIT_CAP` units, on a 5-second dashboard
poll inside a process with well over a hundred threads. The store is append-only, so
that work is almost entirely re-reading bytes that cannot have changed. This module is
the answer an append-only store deserves: state in memory, advanced by the writer at
commit time, read without touching the disk at all.

The shape is dsh's ``session-projection`` and ``session-projection-cache``
(``packages/session/session-projection*``), and the three rules taken from it are what
make this safe rather than merely fast:

* **A pure fold, driven eagerly at commit.** The projection does not subscribe to
  anything and does not poll. :func:`~kiro_crew.crew_log.emit` calls :meth:`apply`
  immediately after a ``session/opened`` append has SUCCEEDED -- durability first, then
  memory, dsh's write-chain order -- so the disk can never hold an edge the memory
  lacks. This gateway is the store's only writer (a pod or the internal gateway has its
  own data home), which is what makes an in-process projection complete rather than a
  guess about another writer.
* **The same reference while nothing changed.** :meth:`nodes` returns the SAME
  dictionary object until the state actually moves, so a reader that re-reads on a
  timer does zero work and a consumer can compare by identity. An :meth:`apply` that
  carries a record already held changes nothing and is not a change.
* **The checkpoint is a fold shortcut, never an authority.** It is "possibly stale but
  never wrong": every write is fail-soft (a lost one costs a longer tail replay), a
  ``ver`` mismatch DISCARDS the file instead of migrating it, and any doubt falls back
  to a cold rebuild. Nothing is ever served because a checkpoint said so and the fold
  could not confirm it.

What the tail replay is, and what it is not. On the first read in a process the
projection loads the checkpoint and then reconciles it against the store ONCE, at a
cost proportional to the DELTA rather than to the store: one ``iterdir`` for the root's
entry NAMES (no per-unit stat), heads read only for names the checkpoint does not
already hold, and records dropped for names that are gone. The new set is normally
EMPTY -- it is non-empty only when the process died between an append and the
checkpoint write, or when this build has never written one. That is the whole point:
the replay pays for the gap, not for the history.

:class:`~kiro_crew.crew_log.session_tree.SessionTree` is kept, unchanged, as the cold
rebuild path and for its own tests. Nothing calls it per read any more.

Two completeness flags travel here, not one, because two different questions are asked
of them. ``incomplete`` is the broad one -- a unit's bytes could not be read, or the
population ran past the cap -- and a reader that DECIDES on an edge needs it.
``over_cap`` is the narrow one, and the dashboard's ``lineage_over_cap`` means exactly
it: a page saying "this store is larger than the scan admits" must not also light up
for a transient read fault. Folding them into one value would make one of the two
answers wrong, so both are carried from whichever seed or replay last set them.

:data:`~kiro_crew.crew_log.session_tree.TREE_UNIT_CAP` bounds the records held, and it
bounds them on EVERY path that adds one -- the seed, the tail replay, and :meth:`apply`
alike. A bound that only the scan honoured would be no bound at all: the state is
long-lived where a scan's result was per-read, so an unbounded writer grows for as long
as the process runs. Past the cap :meth:`apply` admits the new record and evicts the
OLDEST held one by ``created_at`` (``sid`` breaking a tie, so the choice is
deterministic rather than dictionary-ordered), and sets both flags -- the state is then
genuinely missing edges, and ``over_cap`` is the specific reason. Age is the eviction
key because the record being applied was just committed, so evicting the oldest keeps
the sessions a sidebar is actually showing; liveness is deliberately NOT consulted, as
the emitter's commit path has no live-session roster to consult and acquiring one would
put a dashboard dependency on the store's writer.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import weakref
from dataclasses import replace
from pathlib import Path
from typing import Any, Final, Optional

from kiro_crew.crew_log.checkpoint import CHECKPOINT_DIR, MAX_CHECKPOINT_BYTES
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.session_tree import (
    TREE_UNIT_CAP,
    EdgeRecord,
    OpenedRecord,
    TreeNode,
    TreeReading,
    edge_supersedes,
    fold_tree,
    log_rank_of,
)
from kiro_crew.crew_log.store import log_exception_text
from kiro_crew.session_ledger import _store_name
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN, MAX_SHORT_STRING

logger = logging.getLogger(__name__)

#: Checkpoint file name, under the store root's ``projections`` directory. Store-level
#: rather than per-unit (``checkpoint.checkpoint_path``) because this fold is over the
#: COLLECTION of session logs, not over one of them -- the one projection in this
#: package that is, which is why it gets its own file rather than a name in a unit's
#: bundle.
CHECKPOINT_NAME: Final[str] = "session-tree.json"
#: Serialized-state version. Bump when the stored fields or the fold's semantics
#: change: a mismatch DISCARDS the file and falls back to a cold rebuild, which costs
#: one scan and is always correct. Migrating a checkpoint would mean trusting an older
#: build's reading of a rule this build may have changed.
#: Bumped when the payload's shape changes. A mismatch DISCARDS the file rather than
#: migrating it, so an older build's checkpoint -- which carries no ``root`` and so
#: cannot be proven to describe this store, or no ``edges`` and so cannot be proven to
#: hold the store's adoptions -- is rebuilt from the log instead. The ``edges`` half is
#: why this is a bump rather than a key read additively: a file written before
#: adoptions existed would load as "no session has ever been adopted", which looks
#: exactly like the truth and is wrong.
CHECKPOINT_VERSION: Final[int] = 4

#: One unit's log identity for the scan cache: the directory's mtime, its segment count,
#: and the newest segment's size and mtime. Named because it appears in the state, in the
#: checkpoint and in two signatures, and four bare ints in a tuple say nothing at a call
#: site about which four.
ScanIdentity = tuple[int, int, int, int]

#: What a checkpoint load yields, and what a tail replay yields on top of it. Aliased for
#: the same reason: a five-place tuple in a signature is not readable at the call site.
_LoadedCheckpoint = tuple[
    "dict[str, OpenedRecord]", "dict[tuple[str, str], EdgeRecord]", "dict[str, ScanIdentity]"
]
_ReplayResult = tuple[
    "dict[str, OpenedRecord]",
    "dict[tuple[str, str], EdgeRecord]",
    "dict[str, ScanIdentity]",
    bool,
    bool,
]

#: How long a dirty projection waits before its checkpoint is written, in seconds. A
#: debounce, not a delay: a burst of session creations coalesces into ONE write. Short
#: enough that an ordinary shutdown leaves almost nothing for the tail replay to pick
#: up, and a longer gap costs only that replay.
CHECKPOINT_DEBOUNCE_SECS: Final[float] = 1.0


def _checkpoint_path() -> Path:
    """Where the checkpoint lives. Does not create anything, and never raises here --
    the caller's own try/except owns every failure, because a checkpoint that cannot be
    located is exactly as recoverable as one that cannot be parsed: rebuild."""
    from kiro_crew.crew_log.store import crew_log_root

    return crew_log_root(KIND_SESSION) / CHECKPOINT_DIR / CHECKPOINT_NAME


def _current_root() -> str:
    """The store root this process would read right now, as a string, or ``""``.

    Path arithmetic over the data home, not I/O, so it is cheap enough to check on
    every read. It is the projection's store IDENTITY: the fold is the in-memory image
    of ONE store, and a home that moves under the process must re-seed rather than keep
    answering from the old one.
    """
    try:
        from kiro_crew.crew_log.store import crew_log_root

        return str(crew_log_root(KIND_SESSION))
    except Exception:
        return ""


def _record_to_json(record: OpenedRecord) -> dict[str, Any]:
    """One record as plain JSON. Keys are short because the file holds one per unit."""
    out: dict[str, Any] = {
        "sid": record.sid,
        "slot": record.slot,
        "at": record.created_at,
    }
    # Omitted rather than written as null, the same distinction the emitter keeps on the
    # entry itself: "no creator recorded" and "a creator recorded as nothing" are
    # different facts, and only the first is a thing that happens.
    if record.parent_slot:
        out["parent"] = record.parent_slot
    if record.previous_sid:
        out["prev"] = record.previous_sid
    return out


def _within_bounds(record: OpenedRecord) -> bool:
    """Whether every RETAINED string on *record* is within its bound.

    One rule for every door into the state, because the cost is the same at each: these
    strings live in memory for as long as the projection does, up to ``TREE_UNIT_CAP``
    records, and they are written into the checkpoint. The limits are the ones the cold
    scanner already applies to a unit's head -- ``MAX_ACP_SESSION_ID_LEN`` for an ACP
    session id, ``MAX_SHORT_STRING`` for a slot key -- so no path admits a value another
    path would refuse.

    Callers REFUSE the record rather than truncating it. A truncated id or slot key is a
    DIFFERENT key: it matches nothing, or it matches another session.
    """
    if len(record.sid) > MAX_ACP_SESSION_ID_LEN:
        return False
    if len(record.slot) > MAX_SHORT_STRING:
        return False
    if record.parent_slot is not None and len(record.parent_slot) > MAX_SHORT_STRING:
        return False
    if record.previous_sid is not None and len(record.previous_sid) > MAX_ACP_SESSION_ID_LEN:
        return False
    return True


def _edge_within_bounds(edge: EdgeRecord) -> bool:
    """Whether every RETAINED string on *edge* is within its bound.

    The same limits :func:`_within_bounds` applies, for the same reason and against
    the same risk: these strings are held for as long as the projection lives and are
    written into the checkpoint. The slot keys take ``MAX_SHORT_STRING`` and the
    citing log's id takes ``MAX_ACP_SESSION_ID_LEN``, which is what the scanner
    applies to the same two values on a unit's head.
    """
    if len(edge.slot) > MAX_SHORT_STRING:
        return False
    if edge.parent_slot is not None and len(edge.parent_slot) > MAX_SHORT_STRING:
        return False
    if len(edge.sid) > MAX_ACP_SESSION_ID_LEN:
        return False
    return True


def _record_from_json(raw: Any) -> OpenedRecord | None:
    """One record from the checkpoint, or ``None`` when it is not one.

    Every field is type-checked rather than coerced. The file is disposable, so a row
    this cannot read costs a re-read of that unit's head on the replay; a row coerced
    into the wrong shape would instead be folded as though it had been read.

    BOUNDED, on the same terms and with the same limits the scanner applies to a unit's
    head: these strings are RETAINED for as long as the projection lives, so the
    checkpoint cannot be the one path that admits a value the scanner would refuse.
    Bounds refuse the whole record rather than truncating it -- a truncated id or slot
    key is a DIFFERENT key, which matches nothing or, worse, matches another session.
    The checkpoint is written from folded records and so should never carry one, but it
    is a plain file under the data home: what makes it safe to load is this check, not
    its provenance.
    """
    if not isinstance(raw, dict):
        return None
    sid = raw.get("sid")
    slot = raw.get("slot")
    at = raw.get("at")
    parent = raw.get("parent")
    previous = raw.get("prev")
    if not isinstance(sid, str) or not sid:
        return None
    if not isinstance(slot, str):
        return None
    if isinstance(at, bool) or not isinstance(at, int):
        return None
    if parent is not None and not isinstance(parent, str):
        return None
    if previous is not None and not isinstance(previous, str):
        return None
    record = OpenedRecord(
        sid=sid,
        slot=slot,
        created_at=at,
        parent_slot=parent or None,
        previous_sid=previous or None,
    )
    return record if _within_bounds(record) else None


def _edge_to_json(edge: EdgeRecord) -> dict[str, Any]:
    """One decision as plain JSON. Short keys, one row per adopted slot.

    ``parent`` is OMITTED for a release rather than written as null, the same
    distinction the entry itself keeps: a decision with no parent is the release, and
    a key that is absent cannot be confused with a key whose value failed to load.
    """
    out: dict[str, Any] = {
        "slot": edge.slot,
        "at": edge.at,
        "sid": edge.sid,
        "seq": edge.seq,
    }
    if edge.parent_slot:
        out["parent"] = edge.parent_slot
    return out


def _edge_from_json(raw: Any) -> EdgeRecord | None:
    """One decision from the checkpoint, or ``None`` when it is not one.

    Type-checked rather than coerced, and BOUNDED on the same limits every other
    door into this state applies, for the reason :func:`_record_from_json` gives: the
    file is a plain file under the data home, and what makes it safe to load is this
    check rather than its provenance. A row this cannot read costs that slot's
    decision until the next cold scan, which is why it is dropped rather than
    guessed at.
    """
    if not isinstance(raw, dict):
        return None
    slot = raw.get("slot")
    at = raw.get("at")
    sid = raw.get("sid")
    parent = raw.get("parent")
    if not isinstance(slot, str) or not slot or len(slot) > MAX_SHORT_STRING:
        return None
    if isinstance(at, bool) or not isinstance(at, int):
        return None
    if not isinstance(sid, str) or not sid or len(sid) > MAX_ACP_SESSION_ID_LEN:
        return None
    if parent is not None and (not isinstance(parent, str) or len(parent) > MAX_SHORT_STRING):
        return None
    seq = raw.get("seq", 0)
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return None
    return EdgeRecord(slot=slot, parent_slot=parent or None, at=at, sid=sid, seq=seq)


#: Every projection alive in this process. WEAK, so membership never keeps one from
#: being collected -- this exists to reach a projection's pending checkpoint write, not
#: to own the projection. :func:`reset_for_tests` is the reader: the process-wide
#: instance is not the only one that arms a write, so cancelling just that one would
#: leave a directly-constructed projection's worker sleeping with a write still owed.
_LIVE_PROJECTIONS: "weakref.WeakSet[SessionTreeProjection]" = weakref.WeakSet()


class SessionTreeProjection:
    """The in-memory session tree, advanced by the writer and read without I/O.

    State is ``{sid: OpenedRecord}`` -- the very records
    :func:`~kiro_crew.crew_log.session_tree.fold_tree` already consumes, so this class
    introduces no second fold and no second notion of what the tree is. ``nodes`` is
    that fold, computed in memory and cached until the state moves.

    Thread-safe: the emitter's thread applies, the maintenance pool writes the
    checkpoint, and dashboard worker threads read, so every field is taken under one
    lock. Reads are a dictionary lookup under that lock, which is why holding it costs
    nothing worth measuring.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, OpenedRecord] = {}
        #: The newest DECISION per slot -- an adoption, or a release. Keyed by slot
        #: because that is the tree's key, while ``_records`` is keyed by unit id: one
        #: slot can write several logs, and every one of them speaks for the same
        #: node. A second layer rather than a field on the record, because an opened
        #: record says who OPENED the session and a decision says who holds it now;
        #: writing one into the other would make the fold's oldest-record rule pick
        #: the creator over a takeover that came after it.
        #:
        #: Keyed by ``(slot, sid)`` -- every log's decision for a slot, not one winner.
        #: :func:`latest_edges` picks between them at fold time, which is what lets a
        #: deleted unit leave the slot with the surviving log's decision instead of with
        #: none.
        self._edges: dict[tuple[str, str], EdgeRecord] = {}
        #: Per unit, the identity its log had when a COMPLETE tree-edge read last found
        #: no decision (:func:`~kiro_crew.crew_log.store.tree_edge_scan_identity`).
        #:
        #: A CACHE, and only ever of a negative verdict. The complete read is
        #: proportional to a unit's whole log and a unit that records no decision is the
        #: only case that reads all of it, so without this every process start re-reads
        #: the entire store to re-learn what it already knew. An entry here lets the
        #: decision pass skip that read while the identity still matches.
        #:
        #: Never produces an edge. A stale or missing entry costs one re-read, which is
        #: the pass's ordinary cost; the one thing it cannot do is assert a decision,
        #: because no read of the current bytes stands behind it.
        self._scans: dict[str, ScanIdentity] = {}
        #: The cached fold. ``None`` marks it owed, so :meth:`nodes` recomputes once and
        #: then hands out the same object until something actually changes.
        self._nodes: Optional[dict[str, TreeNode]] = None
        self._incomplete = False
        self._over_cap = False
        self._seeded = False
        #: The store root this state was folded from, as a string. The fold's IDENTITY:
        #: a root that does not match means these records describe another store, so
        #: they are dropped rather than served or reconciled.
        self._root: Optional[str] = None
        #: True while the checkpoint on disk is behind the state in memory.
        self._dirty = False
        #: True while a debounced write is already scheduled, so a burst of applies
        #: coalesces into one write instead of one per record.
        self._write_scheduled = False
        #: Bumped to abandon a debounced write that has not fired yet. A worker
        #: compares the epoch it was armed with against this one and writes nothing if
        #: they differ, which is how a discarded projection stops writing without
        #: anyone waiting for its thread.
        self._cancel_epoch = 0
        #: True while a seed's scan is running OFF the lock.
        self._seeding = False
        #: Serializes SEEDS, which ``_lock`` cannot: the scan deliberately runs off
        #: ``_lock`` so it does not stall readers, and ``_seeding`` plus
        #: ``_forgotten_while_seeding`` are single-seed state -- a second concurrent
        #: seed clears the removal record the first one is still filling, and the
        #: first one's install then puts a deleted unit back. Held across the whole
        #: of :meth:`ensure_seeded`, so a second caller WAITS for the seed in flight
        #: and then returns on the already-seeded fast path instead of starting its
        #: own. Always taken BEFORE ``_lock``, never the reverse.
        self._seed_gate = threading.Lock()
        #: Sids forgotten while a seed was in flight. The scan may have listed such a
        #: unit before it was removed, and ``forget`` has nothing to pop from a state
        #: that is still empty, so without this record the install would put the
        #: removed unit back and it would stay until the process restarts.
        self._forgotten_while_seeding: set[str] = set()
        _LIVE_PROJECTIONS.add(self)

    # ── reads ──────────────────────────────────────────────────────────────

    def nodes(self) -> dict[str, TreeNode]:
        """The tree, folded in memory. NO I/O, ever.

        Returns the SAME object while the state has not changed (dsh's same-reference
        rule), so a caller polling on a timer does no work and may compare by identity.
        The dictionary is shared, not copied, and must be treated as read-only: copying
        it per read would reintroduce a per-read cost proportional to the population,
        which is the whole thing this module exists to remove.
        """
        with self._lock:
            if self._nodes is None:
                self._nodes = fold_tree(self._records.values(), self._edges.values())
            return self._nodes

    def reading(self) -> TreeReading:
        """:meth:`nodes` plus the completeness of whatever last established the state.

        Shaped as a :class:`TreeReading` so a consumer can take it where it takes the
        scanner's, and carrying the flag from the seed or replay rather than from a
        scan of its own: after the cold start there is no scan, so "how complete is
        this" is a property of the last reconciliation and not of this call.
        """
        with self._lock:
            if self._nodes is None:
                self._nodes = fold_tree(self._records.values(), self._edges.values())
            return TreeReading(
                nodes=self._nodes,
                incomplete=self._incomplete,
                records=tuple(self._records.values()),
            )

    @property
    def over_cap(self) -> bool:
        """Whether the last reconciliation found more units than the cap admits.

        Narrower than ``incomplete`` on purpose: the dashboard's ``lineage_over_cap``
        means this and only this.
        """
        with self._lock:
            return self._over_cap

    @property
    def seeded(self) -> bool:
        """Whether a cold start has established the state. Tests read this; callers
        use :meth:`ensure_seeded`, which is idempotent."""
        with self._lock:
            return self._seeded

    @property
    def seeded_for_current_store(self) -> bool:
        """Whether :meth:`nodes` can be trusted RIGHT NOW, for the store configured
        now. NO I/O and never blocks, so a caller on the event loop may ask.

        Stricter than :attr:`seeded`, and the difference is the whole point: a data
        home that moved leaves the state seeded from the PREVIOUS store, which
        :meth:`ensure_seeded` discards on its next call. A caller that cannot afford
        to block needs to know that before reading, so it can ship "no creator known"
        for one frame instead of another store's lineage forever.

        The root is path arithmetic over the environment, not I/O, which is what makes
        this safe to call per frame.
        """
        root = _current_root()
        with self._lock:
            return self._seeded and self._root == root

    # ── the delta ──────────────────────────────────────────────────────────

    def apply(self, record: OpenedRecord) -> None:
        """Fold ONE newly-committed record in. Pure with respect to the disk.

        Called by the emitter right after the ``session/opened`` append succeeded, so
        the memory never runs ahead of durability. Idempotent by identity: a record
        equal to the one already held is not a change, so it neither invalidates the
        cached fold nor dirties the checkpoint -- which is what keeps a re-attach in
        the same process from costing a rewrite.

        A record with no ``sid`` is dropped: the state is keyed by it, and a blank key
        would collide every such record onto one entry. A record carrying a string past
        its bound is dropped too, on the same terms as every other door into this state
        (see :func:`_within_bounds`): the emitter passes through the id the BACKEND chose,
        which this process does not control, and retaining it would hold that string for
        the projection's life and then write it to the checkpoint.

        Bounded by :data:`TREE_UNIT_CAP` like every other path that adds a record. Past
        the cap the new record is admitted and the oldest held one is evicted, and both
        completeness flags are set because the state then genuinely lacks edges. See the
        module docstring for why age is the eviction key.

        A citation is never RETRACTED by a later record for the same session. A resume
        appends its own ``session/opened``, and that entry need not repeat the creator
        the first one named -- so replacing outright would drop the edge on resume and
        leave the session looking like a root until the process re-seeds from disk. The
        held ``parent_slot`` therefore survives a parentless update. It is not the
        reverse of a real change: nothing retracts a creator, because the crew log has no
        entry that means "this session was not opened by anyone after all".
        """
        if not record.sid:
            return
        if not _within_bounds(record):
            # The emitter passes through what the BACKEND named the session, which this
            # process does not choose and cannot assume is sane. Retaining it would hold
            # that string for the projection's life and write it to the checkpoint.
            logger.debug("session tree projection refused an over-long record; dropping it")
            return
        with self._lock:
            held = self._records.get(record.sid)
            if held is not None and record.parent_slot is None and held.parent_slot is not None:
                record = replace(record, parent_slot=held.parent_slot)
            if held == record:
                return
            self._records[record.sid] = record
            self._evict_to_cap_locked()
            self._nodes = None
            self._dirty = True
        self._schedule_checkpoint()

    def apply_edge(self, edge: EdgeRecord) -> None:
        """Fold ONE newly-committed decision in -- a takeover, or a release.

        Called by the emitter right after the ``session/adopted`` or
        ``session/released`` append succeeded, on the same terms as :meth:`apply`: the
        memory never runs ahead of durability.

        SAME-REFERENCE when nothing moves. A decision equal to the one held is not a
        change, so it neither invalidates the cached fold nor dirties the checkpoint,
        and :meth:`nodes` keeps handing out the same object. That is what stops a
        replay of a tail the checkpoint already covered from costing a rewrite and a
        re-render on every cold start.

        AN OLDER DECISION NEVER WINS. The held one stays unless the arriving record is
        newer by ``(at, sid)``. Ordering by arrival would be correct for the writer,
        which cannot deliver a decision before it makes it -- but this is not the only
        door: a checkpoint load, a tail replay and the writer all reach this state, and
        a replay walks units in directory order, so a release read from one unit can
        arrive after an adoption made later. Taking the last arrival would put a
        session back under a parent that already let it go, and it would stay there
        until the process restarted.

        A record whose slot is blank is dropped: the state is keyed by it, and a blank
        key would collide every such decision onto one entry. One past its bound is
        dropped for the reason :meth:`apply` drops one -- the strings live as long as
        the projection and are written to the checkpoint -- and refused rather than
        truncated, because a truncated slot key is a different key.

        Keyed by ``(slot, sid)``, so a slot served by more than one log keeps EVERY
        log's decision and the winner is derived at fold time by ``latest_edges``, which
        the fold already calls with the log ranking. One entry per slot would mean the
        loser is discarded here, and then deleting the winning unit would leave the
        slot with no decision at all rather than with the survivor's -- restoring the
        opening edge and moving a session that had been taken over. The comparison
        above therefore settles two readings of ONE log, where ``seq`` decides and only
        increases.

        Bounded by :data:`TREE_UNIT_CAP` like every path that adds to this state.
        """
        if not edge.slot:
            return
        if not _edge_within_bounds(edge):
            logger.debug("session tree projection refused an over-long decision; dropping it")
            return
        with self._lock:
            key = (edge.slot, edge.sid)
            held = self._edges.get(key)
            if held == edge:
                return
            if held is not None and not edge_supersedes(
                edge, held, log_rank_of(self._records.values())
            ):
                # Not the later decision: a replay reading one that was already
                # superseded. Nothing moves, and nothing is dirtied. The comparison asks
                # the store's own sequence rather than a timestamp, so a clock that
                # stepped backward between two appends cannot make the newer one lose.
                return
            self._edges[key] = edge
            # This unit HAS a decision, so a cached "no decision" verdict for it is void.
            # The identity would have changed anyway -- the append moved the bytes -- but
            # a cache that can be dropped at the moment it is known wrong should be.
            self._scans.pop(edge.sid, None)
            self._evict_edges_to_cap_locked()
            self._nodes = None
            self._dirty = True
        self._schedule_checkpoint()

    def _evict_edges_to_cap_locked(self) -> None:
        """Bring the decisions back within :data:`TREE_UNIT_CAP`. Caller holds the lock.

        Oldest-first by ``(at, slot, sid)``, the same shape
        :meth:`_evict_to_cap_locked` uses and for the same reason: the record just
        applied was committed a moment ago, so evicting the oldest keeps the sessions
        a sidebar is actually showing, and the trailing keys make the choice
        deterministic rather than dependent on dictionary order.

        Both completeness flags are set when it evicts, because an evicted decision
        means the tree shows a session hanging somewhere it does not hang -- which is
        exactly what a reader deciding on an edge must not be told confidently.
        """
        surplus = len(self._edges) - TREE_UNIT_CAP
        if surplus <= 0:
            return
        doomed = sorted(self._edges.values(), key=lambda e: (e.at, e.slot, e.sid))
        for edge in doomed[:surplus]:
            self._edges.pop((edge.slot, edge.sid), None)
        self._incomplete = True
        self._over_cap = True

    def _evict_to_cap_locked(self) -> None:
        """Bring the records back within :data:`TREE_UNIT_CAP`. Caller holds the lock.

        Evicts oldest-first by ``(created_at, sid)``. The ``sid`` tiebreak is what makes
        the eviction deterministic: records sharing a timestamp are common (a burst of
        sessions opened in the same second), and without it which one goes would depend
        on dictionary order.

        Sets ``incomplete`` AND ``over_cap`` when it evicts. Both, because both
        questions now have the same answer: a reader deciding on an edge must know the
        state is partial, and the specific reason is that the population outgrew the
        cap. A no-op eviction touches neither flag -- staying within the bound is not a
        completeness event.
        """
        surplus = len(self._records) - TREE_UNIT_CAP
        if surplus <= 0:
            return
        doomed = sorted(self._records.values(), key=lambda r: (r.created_at, r.sid))
        for record in doomed[:surplus]:
            self._records.pop(record.sid, None)
        self._incomplete = True
        self._over_cap = True

    def retract_parent(self, sid: str) -> None:
        """Keep the record, drop its citation -- the LOG does not carry one.

        The only case where retracting a creator is right, and it is the mirror of why
        :meth:`apply` never does. There, a parentless update means "this entry did not
        repeat the creator", so preserving the held edge is the honest reading. Here the
        creating SEGMENT has been removed while later ones survive, so a fresh scan of
        this unit would contribute the slot with NO parent -- that is the scanner's own
        answer, and a projection still serving the old edge disagrees with the disk it is
        supposed to be an image of.

        Dropping the record instead would orphan this unit's children: they cite its
        SLOT, and a slot with no record is a creator that never existed rather than one
        whose own parent is unknown. So sid and slot stay and only the two citations go.

        Half the answer, and the citation half. A DECISION lives in whichever segment
        recorded it rather than in the first one, so the same removal can strand one of
        those instead -- or as well -- and :meth:`reconcile_edge` is what re-reads it.

        Best effort and silent about a record it does not hold, for the same reason
        :meth:`forget` is.
        """
        if not sid:
            return
        with self._lock:
            held = self._records.get(sid)
            if held is None or (held.parent_slot is None and held.previous_sid is None):
                return
            self._records[sid] = replace(held, parent_slot=None, previous_sid=None)
            self._nodes = None
            self._dirty = True
        self._schedule_checkpoint()

    def reconcile_edge(self, sid: str, slot: str) -> None:
        """Re-derive ONE unit's decision from the segments that survive a partial removal.

        The other half of :meth:`retract_parent`. That one answers for the CREATING
        citation, which lives in the unit's first segment; this one answers for the
        decision, which can live in any segment and so is lost by a removal that took
        some of them and left the rest. Nothing else corrects it: a decision is only
        re-read by a cold rebuild, so without this the tree asserts a takeover whose
        segment is deleted, serves it as complete, and writes it into the checkpoint --
        until a restart, which is far too long for something a routine unlink refusal
        can cause.

        THE DISK DECIDES, and unconditionally, which is what makes this different from
        :meth:`apply_edge`. There an arriving record must prove it is newer, because the
        writer, a replay and a checkpoint load all arrive in an order none of them
        controls. Here the argument is not an arrival at all: the segments were just
        removed, so the complete read IS the unit's current answer and a held decision
        that outranks it is exactly the stale one being corrected.

        Three outcomes, and the middle one is the finding this exists for:

        * A decision is found -- it replaces whatever was held for this unit.
        * The read SUCCEEDS and finds none -- the decision has been retained out of the
          log, so the held one is dropped. A complete read finding nothing is the only
          evidence that can say so, which is why an unconditional pop is right here and
          would be wrong anywhere a window was read.
        * The read RAISES -- nothing is proven, so the held decision is LEFT alone and
          the reading is marked incomplete. Dropping on an unreadable directory would
          discard a valid decision over a transient error; keeping it silently would
          serve a possibly-stale tree as complete.

        Best effort and silent about a unit it does not hold, for the reason
        :meth:`forget` is: the removal is reported by the code that did it.
        """
        if not sid or not slot:
            return
        from kiro_crew.crew_log.session_tree import edge_record
        from kiro_crew.crew_log.store import _checked_crew_log_root, find_last_tree_edge

        key = (slot, sid)
        with self._lock:
            if key not in self._edges:
                # No decision held for this unit, so a removal cannot have stranded one.
                # Checked under the lock and before the read, so the common partial
                # removal -- a unit that never recorded a decision -- costs no file IO.
                return
        try:
            root = _checked_crew_log_root(KIND_SESSION)
            found = edge_record(slot, sid, find_last_tree_edge(root / _store_name(sid)))
        except (OSError, ValueError):
            with self._lock:
                self._incomplete = True
            return
        with self._lock:
            held = self._edges.get(key)
            if held is None:
                return
            if found is None:
                del self._edges[key]
                # NOT recorded as a cached negative verdict: this read is of a unit whose
                # segments were being removed as it ran, so the identity it would be
                # keyed by is not one a later boot should trust to skip its own read.
                self._scans.pop(sid, None)
            elif held == found:
                # Already the disk's answer: the removal took segments this decision was
                # not in. Same-reference, like every other no-op path here.
                return
            else:
                self._edges[key] = found
            self._nodes = None
            self._dirty = True
        self._schedule_checkpoint()

    def forget(self, sid: str) -> None:
        """Drop one unit's record -- retention removed it, or a delete took it.

        Best effort and silent about a record it does not hold: removal is reported by
        the code that did it, and a projection asked to forget something twice has
        nothing to complain about.

        A removal arriving while a seed's scan is running is REMEMBERED even though
        there is nothing to pop. The scan may have listed that unit moments before it
        was deleted, and installing its result would otherwise put the record back --
        so the sid is recorded and the install drops it.

        Any DECISION this unit contributed goes with it. An adoption is read from the
        log that recorded it, so a unit that is gone is not evidence for where its slot
        hangs, and keeping the edge would leave the tree asserting a takeover whose only
        record is deleted. The slot's other logs, if it has any, still speak for it:
        decisions are held per ``(slot, sid)``, so the survivors are untouched and the
        fold picks the newest of them -- where holding one winner per slot would drop the
        slot's last decision with the winning unit and silently restore the OPENING
        edge, moving a session that had been taken over.
        """
        if not sid:
            return
        with self._lock:
            if self._seeding:
                self._forgotten_while_seeding.add(sid)
            dropped_edges = [key for key, edge in self._edges.items() if edge.sid == sid]
            for key in dropped_edges:
                self._edges.pop(key, None)
            # The cached "this unit records no decision" verdict goes too: it is keyed by
            # a unit that is gone, so the only thing it could still answer about is a
            # directory some later session happens to be given the same name.
            self._scans.pop(sid, None)
            if self._records.pop(sid, None) is None and not dropped_edges:
                return
            self._nodes = None
            self._dirty = True
        self._schedule_checkpoint()

    # ── cold start ─────────────────────────────────────────────────────────

    def ensure_seeded(self, live_sids: "tuple[str, ...]" = ()) -> None:
        """Establish the state once per process AND per store. BLOCKING -- call it
        off the loop.

        Idempotent for a given store: after the first call this returns immediately,
        which is what lets every reader call it without coordinating.

        SERIALIZED. Only one seed runs at a time on a projection, and a caller that
        arrives while one is in flight waits for it rather than starting a second.
        Two overlapping seeds are not merely wasteful: the scan runs off ``_lock`` so
        it does not stall readers, and the removal record that protects it --
        ``_forgotten_while_seeding`` -- belongs to one seed, so a second seed clearing
        it loses the deletions the first is still collecting and that seed's install
        then resurrects a unit already removed. Overlap is reachable in the ordinary
        case, because the System page's sampling and the sidebar's lazy seed are
        different callers with no knowledge of each other.

        Bound to the store root it folded, and re-seeded when that root CHANGES. The
        state is the in-memory image of one store, so serving it for a different one
        would report another store's lineage as this one's -- reachable whenever the
        data home moves under the process (a pod, a relocated home, a test pointing
        ``KIROCREW_HOME`` somewhere new). The root is path arithmetic over the
        environment, not I/O, so checking it per call costs nothing.

        Two paths, and the fast one is the ordinary one. With a checkpoint: load it,
        then ONE tail replay proportional to the delta (see the module docstring).
        With no checkpoint -- first boot on this build -- one cold
        :meth:`SessionTree.reading` seeds the records and both flags, and that
        scanner is not called again.

        Never raises. A seed that cannot be established leaves the projection empty
        and ``incomplete``, which every consumer renders as "no creator known" --
        the same answer the pages gave before any of this existed.
        """
        root = _current_root()
        with self._seed_gate:
            with self._lock:
                if self._seeded and self._root == root:
                    return
                if self._root != root:
                    # A different store. Drop the previous one's fold rather than
                    # reconciling it: none of those records describe this store, and a
                    # replay would keep every one it could not disprove.
                    self._records = {}
                    self._edges = {}
                    self._nodes = None
                    self._incomplete = False
                    self._over_cap = False
                    self._seeded = False
                    self._dirty = False
                    # An armed debounced write pinned the PREVIOUS store's path but
                    # builds its payload from ``_records`` when it wakes -- which is
                    # about to hold this store's records. Left alone it would write
                    # them into the other store's checkpoint, where a later read would
                    # trust them and report one store's lineage as the other's. Moving
                    # the epoch makes that worker a no-op.
                    self._cancel_epoch += 1
            try:
                self._seed(live_sids)
            except Exception:  # pragma: no cover -- defensive; every step is guarded
                logger.warning(
                    "session tree projection could not be seeded; reporting no lineage",
                    exc_info=True,
                )
                with self._lock:
                    self._seeded = True
                    self._incomplete = True
                    self._nodes = None
            with self._lock:
                self._root = root

    def _install_seed_locked(self, scanned: "dict[str, OpenedRecord]") -> None:
        """Install what a seed established WITHOUT discarding commits made meanwhile.

        Caller holds the lock. The scan itself runs OFF it -- deliberately, because it
        is the one slow step here and holding the lock across it would stall every
        reader for its duration. The cost of that choice is precisely this: an
        :meth:`apply` can commit while the scan runs.

        Such a record is strictly better evidence than the scan's copy of the same sid.
        It came from an append that COMPLETED; the scan may have listed the store
        before that append landed, so its absence there is a stale reading rather than
        a fact. The scan's records therefore go in first and the held ones overwrite
        them, which is why this merges instead of assigning.

        Replacing wholesale drops such a record outright, and nothing downstream
        recovers it: the emitter fires once per opening, so the edge is gone until the
        process restarts and re-seeds.

        A held record is better evidence that the session EXISTS, but it is not evidence
        that the session has no creator. A resume appends its own ``session/opened`` that
        need not repeat the creator, so a resume landing while the scan runs holds a
        PARENTLESS record for a sid the scan found parented -- and merging it over the
        scan would persist that child as a root, with no self-recovery once the state is
        checkpointed. The scanned ``parent_slot`` therefore survives a parentless held
        record, which is the same rule :meth:`apply` applies when the record is already
        held; the difference here is only that during a seed there is nothing held yet
        for ``apply`` to have preserved.

        Symmetrically, a unit REMOVED while the scan ran is excluded. The scan may have
        listed it before the deletion, and the state it was popped from was still empty,
        so nothing else would keep the removal.
        """
        merged = dict(scanned)
        for sid in self._forgotten_while_seeding:
            merged.pop(sid, None)
        for sid, held in self._records.items():
            scanned_record = merged.get(sid)
            if (
                scanned_record is not None
                and held.parent_slot is None
                and scanned_record.parent_slot is not None
            ):
                merged[sid] = replace(held, parent_slot=scanned_record.parent_slot)
            else:
                merged[sid] = held
        self._records = merged
        self._evict_to_cap_locked()

    def _install_seed_edges_locked(self, scanned: "dict[tuple[str, str], EdgeRecord]") -> None:
        """Install the decisions a seed established, keeping the NEWER of the two.

        Caller holds the lock. The records above merge by provenance -- a held record
        beats the scan's copy because it came from a completed append -- and decisions
        deliberately do not: they carry their own order, so the rule is the same one
        :meth:`apply_edge` uses, and the newer of held and scanned wins.

        That is not a weaker rule but a stricter one. A takeover committed while the
        scan ran is newer than anything the scan could have read, so it survives; a
        decision the scan read from a log this process has not applied is newer than a
        stale held one, so it wins instead of being overwritten by it. Provenance
        cannot separate those two cases and the decisions' own order can.

        Merged per ``(slot, sid)``, so the comparison is again between two readings of
        ONE log and every log's decision survives the merge. Choosing between LOGS is
        the fold's job, once, with the record order it already has.

        A unit REMOVED while the scan ran takes its decisions with it, for the reason
        its record is excluded: the scan may have listed the log before the deletion.
        """
        merged = dict(scanned)
        rank = log_rank_of(self._records.values())
        for key, edge in list(merged.items()):
            if edge.sid in self._forgotten_while_seeding:
                merged.pop(key, None)
        for key, held in self._edges.items():
            if held.sid in self._forgotten_while_seeding:
                continue
            scanned_edge = merged.get(key)
            if scanned_edge is None or not edge_supersedes(scanned_edge, held, rank):
                merged[key] = held
        self._edges = merged
        self._evict_edges_to_cap_locked()

    def _seed(self, live_sids: "tuple[str, ...]") -> None:
        """The body of :meth:`ensure_seeded`, so its caller owns one try/except."""
        with self._lock:
            self._seeding = True
            self._forgotten_while_seeding.clear()
        try:
            self._seed_inner(live_sids)
        finally:
            # Cleared even when the seed raised: left set, every later ``forget`` would
            # keep growing a record nothing reads.
            with self._lock:
                self._seeding = False
                self._forgotten_while_seeding.clear()

    def _seed_inner(self, live_sids: "tuple[str, ...]") -> None:
        """Load or replay, then install. Runs with ``_seeding`` raised."""
        loaded = _load_checkpoint()
        if loaded is None:
            # No usable checkpoint: one cold scan, and it is the last one. Its
            # ``records`` are exactly what this state is made of, and its flags are
            # exactly the two carried here, so nothing is re-derived.
            from kiro_crew.crew_log.session_tree import SessionTree

            scanner = SessionTree()
            reading = scanner.reading(live_sids, with_edges=True)
            with self._lock:
                # Flags first, then the merge: eviction can only escalate them, so
                # assigning the scan's values afterwards would undo that escalation.
                self._incomplete = reading.incomplete
                self._over_cap = scanner.over_cap
                self._install_seed_locked({r.sid: r for r in reading.records if r.sid})
                # EVERY decision the scan read, not one per slot. Reducing here would
                # choose between two logs without the record order that ranks them --
                # ``latest_edges`` falls back to the timestamp with no ranking, so a
                # clock that stepped backward between the two logs would pick the older
                # decision and the cold rebuild would restore a lineage that had been
                # superseded. The fold reduces instead, with the ranking it derives from
                # these same records, which is also the one place that choice is made.
                self._install_seed_edges_locked(
                    {(e.slot, e.sid): e for e in reading.edges if e.slot}
                )
                self._nodes = None
                self._seeded = True
                self._dirty = True
            self._schedule_checkpoint()
            return
        records, edges, scans, incomplete, over_cap = self._replay_tail(*loaded)
        with self._lock:
            self._incomplete = incomplete
            self._over_cap = over_cap
            self._install_seed_locked(records)
            self._install_seed_edges_locked(edges)
            # Kept only for units the merge actually holds, so the cache cannot outlive
            # the population it describes and grow without bound across boots.
            self._scans = {sid: ident for sid, ident in scans.items() if sid in self._records}
            self._nodes = None
            self._seeded = True
            # Compared AFTER the merge, against the state actually installed: a record
            # that arrived during the scan is a difference from the checkpoint and owes
            # a write, which comparing the replay's own output would miss. The scan cache
            # counts too: a boot that learned a unit records no decision has something
            # worth writing even when the tree itself did not move, since that is exactly
            # what spares the NEXT boot the read.
            moved = (
                self._records != loaded[0] or self._edges != loaded[1] or self._scans != loaded[2]
            )
            if moved:
                self._dirty = True
        # Only worth a write when the seed actually moved something; an untouched
        # checkpoint is already what is on disk.
        if moved:
            self._schedule_checkpoint()

    def _replay_tail(
        self,
        loaded: dict[str, OpenedRecord],
        loaded_edges: dict[tuple[str, str], EdgeRecord],
        loaded_scans: "dict[str, ScanIdentity] | None" = None,
    ) -> "_ReplayResult":
        """Reconcile a loaded checkpoint against the store, at the cost of the DELTA.

        One ``iterdir`` for the root's entry NAMES -- no per-unit stat, which is the
        syscall this module exists to stop paying per read. A name the checkpoint does
        not hold gets its head read; a held record whose name is gone is dropped.
        Normally both of those sets are empty and the head half is a single directory
        listing.

        The DECISIONS are reconciled on different terms, and they have to be. A head is
        immutable once read, so a name the checkpoint already holds needs no second
        look; a decision lives at the END of a log and a session can be adopted at any
        moment, so a unit already in the checkpoint is exactly where a decision the
        checkpoint missed will be. This therefore reads every admitted unit's tail, not
        only the new ones -- a listing, a ``stat`` and a bounded read per unit, paid
        once per process on the cold start. Skipping the held ones would make the
        checkpoint authoritative for adoptions, and a stale one would leave a session
        hanging under a parent that released it with nothing to correct it.

        Returns ``(records, edges, incomplete, over_cap)``. A listing that fails at all
        makes the answer ``incomplete``: the records are still served, because a stale
        edge renders as a root and that is the pre-existing degradation, but a reader
        that DECIDES on an edge is told the reconciliation did not complete.
        """
        from kiro_crew.crew_log.session_tree import TREE_UNIT_CAP, edge_record, opened_record
        from kiro_crew.crew_log.store import (
            _checked_crew_log_root,
            find_last_tree_edge,
            oldest_segment,
            read_head,
            tree_edge_scan_identity,
        )

        records = dict(loaded)
        edges = dict(loaded_edges)
        scans = dict(loaded_scans or {})
        incomplete = False
        try:
            root = _checked_crew_log_root(KIND_SESSION)
            # NAMES only. ``is_dir()`` here would be one stat per unit, which is the
            # per-read cost being removed -- so the checkpoint directory is excluded by
            # NAME instead, and anything else that is not a unit simply answers no
            # record when its head is read.
            names = {p.name for p in root.iterdir() if p.name != CHECKPOINT_DIR}
        except FileNotFoundError:
            # No store root yet: nothing has been written on this home. An absence, not
            # a fault, and the checkpoint's own records are all there is to serve.
            return records, edges, scans, False, False
        except OSError:
            logger.warning(
                "session tree projection: the store root could not be listed, so its "
                "checkpoint could not be reconciled; lineage reads as incomplete",
                exc_info=True,
            )
            return records, edges, scans, True, False

        held = {_store_name(sid): sid for sid in records}
        gone = [sid for name, sid in held.items() if name not in names]
        for sid in gone:
            records.pop(sid, None)
            # A unit that is gone is not evidence for its slot's decisions, the same
            # rule ``forget`` applies for the same reason.
            for key in [k for k, edge in edges.items() if edge.sid == sid]:
                edges.pop(key, None)
        new = [name for name in names if name not in held]
        # The cap bounds this loop like every other loop over the population. Past it
        # the replay has not seen everything, which is what ``over_cap`` reports.
        over_cap = len(names) > TREE_UNIT_CAP
        for name in new[:TREE_UNIT_CAP]:
            directory = root / name
            segment = oldest_segment(directory)
            if segment is None:
                continue
            try:
                header, entry, announced = read_head(segment)
            except OSError:
                # This unit's bytes were not seen. Its edge is unknown rather than
                # absent, which is the one shape that matters to a reader deciding on
                # an ancestor, so the reconciliation says so.
                incomplete = True
                continue
            if not announced:
                # Created, not yet announced. Nothing to fold and nothing to record:
                # the emitter's own ``apply`` delivers this edge the moment the append
                # lands, so this is not a gap the replay has to close.
                continue
            record = opened_record(directory, header, entry)
            if record is not None and record.sid:
                records[record.sid] = record

        # The decision pass, over every unit the records now cover -- the checkpoint's
        # and the ones just read. Keyed off the records because a decision is keyed by
        # SLOT and the record is where this reconciliation knows the slot from, the same
        # way the scanner reads the head first and the tail second. Each unit keeps its
        # OWN decision, so the comparison below settles two readings of one log; which
        # LOG speaks for a slot is the fold's single decision, made with the ranking it
        # derives from these same records.
        for record in list(records.values())[:TREE_UNIT_CAP]:
            if not record.slot:
                continue
            directory = root / _store_name(record.sid)
            key = (record.slot, record.sid)
            try:
                identity = tree_edge_scan_identity(directory)
            except OSError:
                # Could not even stat the unit, so nothing is established about it.
                incomplete = True
                continue
            if identity is not None and key not in edges and scans.get(record.sid) == identity:
                # An earlier complete read of these same bytes found no decision, and no
                # decision is held for this unit to confirm or drop -- so the read would
                # re-derive a verdict already in hand. This is the case that dominates a
                # real store: most sessions are never adopted, and their logs stop
                # changing once they close, while the read they would otherwise cost is
                # proportional to the whole log rather than to a window of it.
                continue
            try:
                # The WHOLE log, not a tail window: this is the cold path, so a decision
                # it cannot reach is not recovered by anything later -- the fold would
                # serve the creating edge for a session that has been moved.
                entry = find_last_tree_edge(directory)
            except (OSError, ValueError):
                incomplete = True
                continue
            found = edge_record(record.slot, record.sid, entry)
            if found is None:
                # The read SUCCEEDED and found no decision, which is a verdict rather
                # than a gap: this log does not record one, so a decision the checkpoint
                # carries for it has been retained out of the log and must not outlive
                # it. Keeping it would pin the slot under a parent with nothing on disk
                # behind it, and no later scan would ever contradict it -- a complete
                # read that finds nothing is the only thing that can, and it is here.
                # A read that RAISED took the branch above and leaves the edge alone.
                edges.pop(key, None)
                if identity is not None:
                    # Remembered so the next boot can reach this same verdict without the
                    # read. Only the NEGATIVE verdict is worth caching: a decision that
                    # was found is carried by ``edges`` itself.
                    scans[record.sid] = identity
                continue
            scans.pop(record.sid, None)
            candidate = edges.get(key)
            if candidate is None or edge_supersedes(found, candidate, None):
                edges[key] = found
        return records, edges, scans, incomplete or over_cap, over_cap

    # ── the checkpoint ─────────────────────────────────────────────────────

    def _schedule_checkpoint(self) -> None:
        """Arm ONE debounced background write. Never raises, never blocks the caller.

        The caller is the emitter's thread finishing an append, or a removal path, so
        the write must not happen inline: it is a file write on a path neither of those
        is waiting for. The scheduling flag is what makes a burst of session creations
        cost one write rather than one per session.

        A pool that will not accept the job is not an error worth reporting: the
        checkpoint is a shortcut, so the cost of never writing it is a longer tail
        replay next time.
        """
        with self._lock:
            if self._write_scheduled or not self._dirty:
                return
            self._write_scheduled = True
            epoch = self._cancel_epoch
            # Resolved HERE rather than in the worker. The path comes from the
            # environment, and the worker resolves nothing until a debounce has
            # elapsed -- by which time the data home can be a different one (a pod, a
            # relocated home, a test that repointed it). Pinning it at arm time is what
            # makes the write land in the store this state was folded from, or nowhere.
            target = _checkpoint_path()
        try:
            from kiro_crew.executors import maintenance_executor

            maintenance_executor().submit(self._debounced_write, target, epoch)
        except Exception:
            # Including a pool shut down during teardown, which is ordinary at exit.
            with self._lock:
                self._write_scheduled = False
            # Rendered text, never ``exc_info``. This frame holds no handle, but its
            # CALLER chain can: ``apply`` <- ``record_opened`` <- the emitter's edge
            # recorder, whose ``log`` is a live ``CrewLog``. A retained traceback reaches
            # that frame through ``tb_frame.f_back`` and keeps the handle, and its write
            # lease, alive past the drop that should have released it. Pinned by
            # test_crew_log_session_tree_projection.py.
            log_exception_text(
                logger, logging.DEBUG, "session tree checkpoint could not be scheduled"
            )

    def cancel_pending_checkpoint(self) -> None:
        """Abandon a debounced write that has not fired yet. Never blocks, never raises.

        For a caller discarding this projection -- a test tearing down its data home, a
        shutdown -- that must not leave a worker sleeping on the maintenance pool with a
        write still owed. The worker is not interrupted (nothing here joins a pool
        thread); it is made a no-op, so it wakes, sees the epoch moved, and returns.

        ``dirty`` is deliberately left alone. Cancelling says "not this write", not
        "the state is saved": a projection that keeps being used re-arms on its next
        change, and clearing the flag here would silently forfeit that write instead.
        """
        with self._lock:
            self._cancel_epoch += 1

    def _debounced_write(self, target: "Path | None" = None, epoch: int = -1) -> None:
        """Wait out the debounce, then write once. Runs on the maintenance pool.

        It writes the checkpoint; it does not scan. That distinction is the whole
        reason a background task is acceptable here at all.

        ``target`` is the path resolved when this was armed and ``epoch`` the
        cancellation generation then current; a mismatch against the live epoch means
        the write was abandoned while this waited, so it returns having written nothing.
        """
        try:
            time.sleep(CHECKPOINT_DEBOUNCE_SECS)
        except Exception:  # pragma: no cover -- defensive
            pass
        with self._lock:
            self._write_scheduled = False
            if epoch != self._cancel_epoch:
                return
            if not self._dirty:
                return
            payload = {
                "ver": CHECKPOINT_VERSION,
                "written_at": int(time.time() * 1000),
                # The store these records describe, written INTO the file so a reader
                # can prove it rather than assume it. The target alone cannot: it is
                # pinned when the write is armed, and the payload is built here.
                "root": self._root or _current_root(),
                "records": [_record_to_json(r) for r in self._records.values()],
                "edges": [_edge_to_json(e) for e in self._edges.values()],
                # The unit identities at which a complete read found NO decision, so the
                # next boot can skip re-deriving that per unit. A cache, never evidence:
                # see ``_scans``.
                "scans": {sid: list(ident) for sid, ident in self._scans.items()},
            }
            # Cleared BEFORE the write, and deliberately: an apply landing during it
            # re-dirties the state and arms another write, where clearing after would
            # let that apply be swallowed by this one's completion.
            self._dirty = False
        if not _save_checkpoint(payload, target):
            # Fail-soft: the state in memory is still right, and the cost of a lost
            # write is a longer tail replay on the next cold start. Re-dirtied so a
            # later change tries again rather than leaving the file permanently behind.
            with self._lock:
                if epoch == self._cancel_epoch:
                    self._dirty = True

    def flush_checkpoint(self) -> bool:
        """Write the checkpoint NOW, skipping the debounce. Returns whether it wrote.

        For a caller that wants the shortcut on disk at a chosen moment -- a test, or a
        deliberate shutdown -- rather than whenever the debounce elapses.
        """
        with self._lock:
            if not self._records and not self._edges and not self._dirty:
                return False
            payload = {
                "ver": CHECKPOINT_VERSION,
                "written_at": int(time.time() * 1000),
                "root": self._root or _current_root(),
                "records": [_record_to_json(r) for r in self._records.values()],
                "edges": [_edge_to_json(e) for e in self._edges.values()],
                # The unit identities at which a complete read found NO decision, so the
                # next boot can skip re-deriving that per unit. A cache, never evidence:
                # see ``_scans``.
                "scans": {sid: list(ident) for sid, ident in self._scans.items()},
            }
            self._dirty = False
            # Pinned from the SAME locked block that built the payload. Resolving it
            # inside the write would let a home that moves in between send these
            # records to a store they do not describe.
            target = _checkpoint_path()
        wrote = _save_checkpoint(payload, target)
        if not wrote:
            with self._lock:
                self._dirty = True
        return wrote


def _load_checkpoint() -> "_LoadedCheckpoint | None":
    """The checkpoint's records, decisions and scan cache, or ``None`` when there is no
    usable one.

    ``None`` is every failure, undifferentiated on purpose: absent, unreadable,
    oversized, unparseable, wrong ``ver``, or a payload whose shape this does not
    recognise all mean the same thing to the caller -- rebuild, which is always
    correct. Never raises, and never migrates: a ``ver`` mismatch is DISCARDED, because
    forward-applying an older build's state is how a fold quietly becomes garbage.

    The two are returned together, from one read, for the reason
    :class:`~kiro_crew.crew_log.session_tree.TreeReading` carries its flag: they are
    halves of one state, and a caller that could load the records and ask for the
    decisions separately could fold a tree from two different moments.
    """
    try:
        path = _checkpoint_path()
        size = path.stat().st_size
        if size > MAX_CHECKPOINT_BYTES:
            logger.warning(
                "session tree checkpoint is %d bytes, past the %d-byte ceiling; "
                "discarding it and rebuilding",
                size,
                MAX_CHECKPOINT_BYTES,
            )
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.debug("session tree checkpoint unreadable; rebuilding", exc_info=True)
        return None
    if not isinstance(payload, dict) or payload.get("ver") != CHECKPOINT_VERSION:
        return None
    # The file says which store it describes; a mismatch is DISCARDED. Without this the
    # only thing tying a checkpoint to a store is its path, and a file that reached the
    # wrong path -- a write armed before the data home moved, a home restored from
    # elsewhere -- would be trusted and would report another store's lineage as this
    # one's, silently and with no recovery path.
    stored_root = payload.get("root")
    if not isinstance(stored_root, str) or stored_root != _current_root():
        logger.debug("session tree checkpoint describes another store; rebuilding")
        return None
    rows = payload.get("records")
    if not isinstance(rows, list):
        return None
    # A missing ``edges`` key cannot reach here: the version gate above admits only a
    # file this build wrote, and this build writes the key whether or not any session
    # has been adopted. So a payload that lacks it is not an older file to tolerate but
    # a damaged one, and the whole checkpoint is discarded rather than read as "no
    # adoptions" -- which is the one wrong answer that looks exactly like a right one.
    edge_rows = payload.get("edges")
    if not isinstance(edge_rows, list):
        return None
    # Same reasoning as ``edges`` for the key having to be PRESENT, and the opposite
    # reasoning for a malformed VALUE inside it: this one is a cache, so a row that does
    # not parse is dropped and its unit is simply re-read, where dropping a decision row
    # would change the tree.
    scan_rows = payload.get("scans")
    if not isinstance(scan_rows, dict):
        return None
    records: dict[str, OpenedRecord] = {}
    for raw in rows:
        record = _record_from_json(raw)
        if record is not None:
            records[record.sid] = record
    edges: dict[tuple[str, str], EdgeRecord] = {}
    for raw in edge_rows:
        edge = _edge_from_json(raw)
        if edge is not None:
            edges[(edge.slot, edge.sid)] = edge
    scans: dict[str, ScanIdentity] = {}
    for sid, raw in scan_rows.items():
        if not isinstance(sid, str) or not sid:
            continue
        if not isinstance(raw, list) or len(raw) != 4:
            continue
        if not all(isinstance(part, int) and not isinstance(part, bool) for part in raw):
            continue
        scans[sid] = (raw[0], raw[1], raw[2], raw[3])
    return records, edges, scans


def _save_checkpoint(payload: dict[str, Any], path: "Path | None" = None) -> bool:
    """Write the checkpoint atomically. Returns whether it landed. Never raises.

    ``path`` is the resolved target. A background caller pins it when it ARMS the write,
    so a debounce that elapses after the data home moved still writes where the state
    came from; ``None`` resolves it here, which is right for a synchronous caller whose
    environment cannot have changed underneath it.

    No ``fsync``, the same choice ``checkpoint.save`` makes for the same reason: what a
    crash leaves unpersisted is an older shortcut or none, and both are states the
    reconciliation already handles. Paying for durability on a value that is explicitly
    disposable would be paying for the wrong thing.
    """
    try:
        from kiro_crew.atomic_write import atomic_write

        blob = json.dumps(payload, separators=(",", ":"))
        if len(blob.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
            logger.warning(
                "session tree checkpoint would exceed the %d-byte ceiling; not written",
                MAX_CHECKPOINT_BYTES,
            )
            return False
        # ``atomic_write`` creates its target's parents itself, which is why there is no
        # ``mkdir`` here -- the same reason ``checkpoint.save`` has none.
        atomic_write(path or _checkpoint_path(), blob, fsync=False, newline="")
        return True
    except Exception:
        logger.debug("session tree checkpoint could not be written", exc_info=True)
        return False


_projection: Optional[SessionTreeProjection] = None
_projection_lock = threading.Lock()


def projection() -> SessionTreeProjection:
    """The process's one projection.

    One instance, because it is the in-memory image of one store that this process
    alone writes: a second instance would be a second fold of the same log, and the
    emitter can only advance one of them.
    """
    global _projection
    with _projection_lock:
        if _projection is None:
            _projection = SessionTreeProjection()
        return _projection


def reset_for_tests() -> None:
    """Drop the process's projection. For tests that change the data home.

    The state is keyed to one store, so a test pointing ``KIROCREW_HOME`` somewhere new
    must not inherit the previous home's fold.

    Cancels the pending checkpoint of EVERY projection alive in this process, not just
    the one being dropped. A debounced write outlives the object that armed it, and it
    resolves its target directory when it WAKES -- so a test whose home is already torn
    down is exactly when such a write lands somewhere it was never meant to. A test that
    constructs a projection directly arms writes on an instance this function never held
    a reference to, which is why the weak registry is consulted rather than the
    singleton alone.
    """
    global _projection
    with _projection_lock:
        _projection = None
    for proj in list(_LIVE_PROJECTIONS):
        proj.cancel_pending_checkpoint()


def record_opened(
    sid: str,
    slot: str,
    created_at: int,
    parent_slot: str | None,
    previous_sid: str | None,
) -> None:
    """Fold a just-committed ``session/opened`` into the projection.

    The emitter's one door in, taking the values it just wrote rather than an
    :class:`OpenedRecord` so that module does not have to import the record type.
    Never raises: a projection that cannot be advanced degrades to a longer tail
    replay, and an append that already succeeded must not be reported as failed
    because the memory image of it did not land.
    """
    try:
        projection().apply(
            OpenedRecord(
                sid=sid,
                slot=slot or "",
                created_at=created_at,
                parent_slot=parent_slot or None,
                previous_sid=previous_sid or None,
            )
        )
    except Exception:  # pragma: no cover -- defensive
        # Rendered text, never ``exc_info``: the caller is the emitter's edge recorder,
        # whose ``log`` is a live ``CrewLog``, and a retained traceback reaches that frame
        # through ``tb_frame.f_back`` -- so a handler that keeps records would keep the
        # handle and its write lease. Pinned by test_crew_log_session_tree_projection.py.
        log_exception_text(
            logger, logging.DEBUG, "session tree projection could not apply an opened record"
        )


def record_adopted(sid: str, slot: str, at: int, parent_slot: str, seq: int = 0) -> None:
    """Fold a just-committed ``session/adopted`` into the projection.

    The emitter's door for a takeover, taking the values it just wrote rather than an
    :class:`EdgeRecord` so that module does not have to import the record type -- the
    same shape :func:`record_opened` has, for the same reason.

    ``seq`` is the written line's position in that log, and it is what orders this
    decision against the others for the same log -- a clock-free comparison, which
    matters because a backward clock step between two appends would otherwise make the
    newer one look older and lose. ``at`` rides along as the audit reading. An adoption
    with no ``parent_slot`` is dropped rather than folded as a release: the two are
    different records and the emitter writes the one it means.

    Never raises: an append that already succeeded must not be reported as failed
    because the memory image of it did not land, and a decision the projection missed
    is recovered by the tail replay on the next cold start.
    """
    if not parent_slot:
        return
    try:
        projection().apply_edge(
            EdgeRecord(slot=slot or "", parent_slot=parent_slot, at=at, sid=sid, seq=seq)
        )
    except Exception:  # pragma: no cover -- defensive
        # Rendered text, never ``exc_info``, for the reason :func:`record_opened` gives:
        # the caller is the emitter's edge recorder and a retained traceback reaches its
        # live ``CrewLog`` through ``tb_frame.f_back``.
        log_exception_text(
            logger, logging.DEBUG, "session tree projection could not apply an adoption"
        )


def record_released(sid: str, slot: str, at: int, seq: int = 0) -> None:
    """Fold a just-committed ``session/released`` into the projection.

    The counterpart of :func:`record_adopted`, and the only door that takes an edge
    AWAY. Never raises, for the same reason.
    """
    try:
        projection().apply_edge(
            EdgeRecord(slot=slot or "", parent_slot=None, at=at, sid=sid, seq=seq)
        )
    except Exception:  # pragma: no cover -- defensive
        # Rendered text, never ``exc_info``, for the same reason.
        log_exception_text(
            logger, logging.DEBUG, "session tree projection could not apply a release"
        )


def retract_unit_parent(sid: str) -> None:
    """Clear a unit's held citation, keeping the record. Never raises, same reason."""
    try:
        projection().retract_parent(sid)
    except Exception:  # pragma: no cover -- defensive
        logger.debug("session tree projection could not retract a citation", exc_info=True)


def reconcile_unit_edge(sid: str, slot: str) -> None:
    """Re-derive a unit's decision after a partial removal. Never raises, same reason."""
    try:
        projection().reconcile_edge(sid, slot)
    except Exception:  # pragma: no cover -- defensive
        logger.debug("session tree projection could not reconcile a decision", exc_info=True)


def forget_unit(sid: str) -> None:
    """Drop a removed unit from the projection. Never raises, for the same reason."""
    try:
        projection().forget(sid)
    except Exception:  # pragma: no cover -- defensive
        logger.debug("session tree projection could not forget a unit", exc_info=True)

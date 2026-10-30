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

``SessionTreeProjection`` is composed from two owners: the checkpoint -- its name, version
and codec, the string bounds every door into the state applies, its load and atomic save,
and the debounced write (``session_tree_projection_checkpoint``) -- and the cold start that
seeds the state from the checkpoint and a tail replay, or from one cold scan, and
re-attempts a seed that raised (``session_tree_projection_seed``). An owner function whose
first parameter is ``self`` is bound as the class attribute of the same name. This module
keeps the state, the reads, every delta, the change announcement, the process-wide
projection and the emitter's doors, and stays the only import path and patch surface:
:mod:`kiro_crew._facade_mirror` carries a patch of one of its names to every owner that
reads it. Which owner takes new work is recorded in
``docs/system-specs/modules/crew-log-projection.md`` (section 6).
"""

from __future__ import annotations

import importlib as _importlib
import json
import logging
import sys as _sys
import threading
import time
import weakref
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Optional

from kiro_crew import _facade_mirror
from kiro_crew.crew_log import session_tree_projection_checkpoint as _stp_checkpoint
from kiro_crew.crew_log import session_tree_projection_seed as _stp_seed
from kiro_crew.crew_log.checkpoint import CHECKPOINT_DIR, MAX_CHECKPOINT_BYTES
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.session_tree import (
    TREE_UNIT_CAP,
    EdgeRecord,
    OpenedRecord,
    TreeNode,
    TreeReading,
    _ScanFaults,
    edge_supersedes,
    fold_tree,
    log_rank_of,
)
from kiro_crew.crew_log.session_tree_projection_checkpoint import (
    ScanIdentity,
    _current_root,
    _edge_within_bounds,
    _within_bounds,
)
from kiro_crew.crew_log.store import log_exception_text
from kiro_crew.session_ledger import _store_name
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN, MAX_SHORT_STRING

logger = logging.getLogger("kiro_crew.crew_log.session_tree_projection")


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
        #: Every ``session/spawned`` held, keyed ``(child_slot, creator_sid)``. The
        #: creators' own record of a ``session_create``, which is the only durable
        #: creating edge a child has before its first turn writes a log of its own.
        #: :func:`fold_tree` reads one only for a child whose own records name no
        #: parent, so it never competes with the child's ``session/opened.parent``,
        #: and the decisions above still replace it.
        self._spawned: dict[tuple[str, str], EdgeRecord] = {}
        #: Per creator unit, the identity its log had when its ``session/spawned``
        #: entries were last read COMPLETELY -- found or not. The entries held for that
        #: unit in ``_spawned`` are its whole answer while the identity still matches,
        #: so a boot skips re-reading a log that has not changed. A cache: a mismatch
        #: costs one re-read and never asserts an edge.
        self._spawn_scans: dict[str, ScanIdentity] = {}
        #: The cached fold. ``None`` marks it owed, so :meth:`nodes` recomputes once and
        #: then hands out the same object until something actually changes.
        self._nodes: Optional[dict[str, TreeNode]] = None
        self._incomplete = False
        #: ``(read, sid)`` for each unit whose decision (``"edge"``) or spawn (``"spawn"``)
        #: reconcile read FAULTED and has not read cleanly since. Part of
        #: ``incomplete`` while non-empty, and kept apart from ``_incomplete`` so a
        #: later clean read of the same unit takes the fault back out: one transient
        #: read error must not mark the tree incomplete until the process restarts.
        self._fault_sids: set[tuple[str, str]] = set()
        self._over_cap = False
        #: Units whose decision this state may have WRONG: one evicted past the cap, or
        #: one whose bytes faulted after its head was read. The narrow half of
        #: ``incomplete``, so a reader deciding about ONE slot can ask whether that
        #: slot's own units are among them instead of refusing for the whole store. A
        #: unit absent from ``_records`` needs no entry, and has none: a walk over the
        #: slot's ``previous_sid`` chain finds the gap by itself, and keeping only held
        #: units bounds this set by :data:`TREE_UNIT_CAP` like the records.
        self._suspect_sids: set[str] = set()
        #: A fault no unit can be named for -- the store root could not be listed, or
        #: the seed itself raised. Any slot could be the one it hid, so a reader
        #: deciding on an edge refuses for every slot while it stands.
        self._unattributed_gap = False
        self._seeded = False
        #: When a seed that RAISED may be re-attempted, on the monotonic clock. ``None``
        #: means the state was established rather than given up on, which is the only
        #: case where reading it is final: a failed seed serves an EMPTY tree, and
        #: without this the first transient store fault of a process would cost every
        #: reader its lineage until the gateway restarted, because nothing else clears
        #: :attr:`_seeded` for an unchanged root.
        self._seed_retry_at: Optional[float] = None
        #: The armed re-attempt of a failed seed, if any. See :meth:`_arm_seed_retry`.
        self._seed_retry_timer: Optional[threading.Timer] = None
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

    def _fold_locked(self) -> dict[str, TreeNode]:
        """The one fold over everything held. Caller holds the lock."""
        return fold_tree(self._records.values(), self._edges.values(), self._spawned.values())

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
                self._nodes = self._fold_locked()
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
                self._nodes = self._fold_locked()
            return TreeReading(
                nodes=self._nodes,
                incomplete=self._incomplete or bool(self._fault_sids),
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
        this safe to call per frame. It can still FAIL -- resolving the data home
        touches the filesystem -- and :func:`_current_root` answers with an empty
        string when it does. That is "cannot tell", not "another store", and the two
        must not be collapsed: read as an identity the empty string matches nothing, so
        one transient fault reported a correct fold unseeded and every slots frame in
        that window shipped no lineage at all. An unreadable root therefore leaves the
        answer at whatever the last readable one established.
        """
        root = _current_root()
        with self._lock:
            if not root:
                return self._seeded
            return self._seeded and self._root == root

    @property
    def seed_retry_due(self) -> bool:
        """Whether the state was given up on and a re-attempt is owed NOW. No I/O.

        Separate from :attr:`seeded_for_current_store` rather than folded into it,
        because the two answer different questions and one of them is depended on
        elsewhere. That property means "may :meth:`nodes` be read", and a failed seed
        leaves a readable -- empty -- state for the store configured now, so reporting
        it as unseeded would also re-point every other caller that asks, including the
        adoption guard in ``session_control``, which already refuses on
        :attr:`~TreeReading.incomplete` and needs no second signal.

        What this one says is that the empty answer is provisional: a caller that cannot
        block can ask for a seed and tell its reader to come back. False whenever the
        state was established, which is the ordinary path, so the clock is not read at
        all on a healthy projection.
        """
        with self._lock:
            return self._retry_due_locked()

    @property
    def seed_given_up(self) -> bool:
        """Whether the state is a failed seed's EMPTY stand-in, awaiting a re-attempt.

        No I/O. :attr:`seeded_for_current_store` is true for that state too, which is
        right for a page that renders lineage as decoration and wrong for a reader
        that DECIDES on an edge: an empty tree answers "no creator" for everything.
        Such a reader asks this and falls back to its own scan while it holds.
        """
        with self._lock:
            return self._seed_retry_at is not None

    def trusted_creator(self, slot: str, head_sid: str) -> str:
        """*slot*'s creator, if the slot's OWN logs prove it still hangs there. No I/O.

        The answer is WRITTEN into an append-only ``session/opened`` and re-cited by
        every later log, so it decides on an edge, and a stale one would be durable. It
        is still judged per slot rather than for the whole store: a store past
        :data:`TREE_UNIT_CAP` is always partial, and those are exactly the stores whose
        oldest logs eviction and retention remove first.

        The walk goes back over *slot*'s ``previous_sid`` chain from *head_sid* to the
        NEWEST log that cites a creator. Every log on that span must be held, belong to
        *slot*, and not be suspect (a decision evicted or unread). Logs older than that
        span cannot change the answer: a citation is only ever re-written while the tree
        hangs the slot under that creator, so whatever was decided before it is already
        reflected in it, and only a decision recorded at or after it can have moved the
        slot. A log missing from the span -- evicted, skipped past the cap, or a head
        that could not be read -- stops the walk, so that case needs no bookkeeping.

        ``""`` while a fault with no owning unit stands (the root could not be listed,
        or the seed raised), when the walk stops before finding a citation, when the
        slot's node sits on a cycle the consumer will not nest anyway, and when the
        tree hangs the slot somewhere other than that creator: a release or a takeover moved
        it, and re-citing the creator would bring the old edge back once retention
        removes the log that recorded the move. The creator, never the folded holder:
        a takeover moves the node to the adopter, and writing that into a
        ``session/opened`` would record the adopter as the one who opened it.
        """
        if not slot or not head_sid:
            return ""
        with self._lock:
            if self._unattributed_gap:
                return ""
            creator = ""
            sid = head_sid
            for _ in range(TREE_UNIT_CAP):
                record = self._records.get(sid)
                if record is None or record.slot != slot or sid in self._suspect_sids:
                    return ""
                if record.parent_slot:
                    creator = record.parent_slot
                    break
                # The id alone, as ``_chain_step`` follows it: a record built from a
                # checkpoint or from the emitter's own values carries ``previous_sid``
                # and leaves ``previous_edge`` at its default, so asking for the
                # scanner's EDGE_NAMED here would stop at every such log.
                if not record.previous_sid:
                    return ""
                sid = record.previous_sid
            if not creator or creator == slot:
                return ""
            if self._nodes is None:
                self._nodes = self._fold_locked()
            node = self._nodes.get(slot)
        if node is None or node.cycle or node.parent_slot != creator:
            return ""
        return creator

    def spawned_creator(self, slot: str) -> str:
        """*slot*'s creator from a trusted ``session/spawned`` record, or ``""``. No I/O.

        The companion of :meth:`trusted_creator` for the one case that method cannot
        answer: a child restored after a restart that happened BEFORE its first turn. It
        has no predecessor log to inherit from, so :meth:`trusted_creator` is asked with
        no ``head_sid`` and declines -- yet the creator recorded the edge on its OWN log
        at ``session_create`` (the ``session/spawned`` entry this PR writes), and that is
        exactly the record meant to carry the child across such a restart.

        Only for a child with NO log of its own, which is the sole case
        :func:`session_tree.fold_tree` lets a spawn speak for -- once the child opens a
        log, its own records and any release/adopt edge decide, and this must not
        re-cite the creator over a move. And only when the creator still has a log, the
        same floor the fold applies (a spawn whose creator unit is gone is a citation
        with nothing behind it). When more than one spawn names the child (only damage
        does), the earliest by ``(at, seq, sid)`` wins, matching the fold's own order so
        the two never disagree.

        The caller still gates this on the restored ``_created_by`` matching the answer,
        the way it gates :meth:`trusted_creator`: the fence that keeps a metadata edit
        from forging lineage is that both the restored attribution and this record,
        written independently, must name the same creator.
        """
        if not slot:
            return ""
        with self._lock:
            if self._unattributed_gap:
                return ""
            if any(record.slot == slot for record in self._records.values()):
                # The child has a log of its own; the fold reads that, not a spawn.
                return ""
            have_log = {record.slot for record in self._records.values() if record.slot}
            best: EdgeRecord | None = None
            for spawn in self._spawned.values():
                if spawn.slot != slot or not spawn.parent_slot:
                    continue
                if spawn.parent_slot not in have_log:
                    continue
                if best is None or (spawn.at, spawn.seq, spawn.sid) < (best.at, best.seq, best.sid):
                    best = spawn
        if best is None or not best.parent_slot or best.parent_slot == slot:
            return ""
        return best.parent_slot

    def created(self) -> "dict[str, tuple[str, int]]":
        """Each slot's creator and when, from CREATION records only. No I/O.

        A creation record is the creator's own ``session/spawned`` or the child's
        ``session/opened`` naming a parent. A release or a takeover is a later
        decision (an :class:`EdgeRecord` in the edges), so it leaves this answer
        standing: a crewmate's page still knows which workers it made after it let one
        go. Per slot the EARLIEST record wins, its ``at`` the creation time.
        """
        found: dict[str, tuple[str, int]] = {}

        def _keep(slot: str, creator: "str | None", at: int) -> None:
            if not slot or not creator or creator == slot:
                return
            held = found.get(slot)
            if held is None or at < held[1]:
                found[slot] = (creator, at)

        with self._lock:
            for spawn in self._spawned.values():
                _keep(spawn.slot, spawn.parent_slot, spawn.at)
            for record in self._records.values():
                _keep(record.slot, record.parent_slot, record.created_at)
        return found

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
        self._changed()

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
        self._changed()

    def apply_spawned(self, spawn: EdgeRecord) -> None:
        """Fold ONE newly-committed ``session/spawned`` in.

        Called by the emitter right after the append to the CREATOR's log succeeded,
        on the terms :meth:`apply` keeps: the memory never runs ahead of durability.
        *spawn* is keyed on the child -- ``slot`` the child, ``parent_slot`` the
        creator, ``sid`` the creator's unit.

        Same-reference when nothing moves, bounded like every other door, and past
        :data:`TREE_UNIT_CAP` the oldest held spawn is evicted and both flags are set.
        """
        if not spawn.slot or not spawn.parent_slot or not spawn.sid:
            return
        if not _edge_within_bounds(spawn):
            logger.debug("session tree projection refused an over-long spawn; dropping it")
            return
        with self._lock:
            key = (spawn.slot, spawn.sid)
            if self._spawned.get(key) == spawn:
                return
            self._spawned[key] = spawn
            # The creator's log just grew, so its cached read is void.
            self._spawn_scans.pop(spawn.sid, None)
            self._evict_spawned_to_cap_locked()
            self._nodes = None
            self._dirty = True
        self._changed()

    def _evict_spawned_to_cap_locked(self) -> None:
        """Bring the spawns back within :data:`TREE_UNIT_CAP`. Caller holds the lock.

        Oldest-first by ``(at, slot, sid)``, the shape the decisions use. An evicted
        spawn may be a child's only edge, so both flags are set and the creator's unit
        is marked suspect, and its cached read is dropped so the next boot re-reads it.
        """
        surplus = len(self._spawned) - TREE_UNIT_CAP
        if surplus <= 0:
            return
        doomed = sorted(self._spawned.values(), key=lambda e: (e.at, e.slot, e.sid))
        for spawn in doomed[:surplus]:
            self._spawned.pop((spawn.slot, spawn.sid), None)
            self._spawn_scans.pop(spawn.sid, None)
            if spawn.sid in self._records:
                self._suspect_sids.add(spawn.sid)
        self._incomplete = True
        self._over_cap = True
        self._prune_faults_locked()

    def _replace_unit_spawned_locked(self, sid: str, found: "list[EdgeRecord]") -> bool:
        """Make *found* the whole set of spawns held for creator unit *sid*. Caller
        holds the lock. Returns whether anything changed."""
        held = {key: spawn for key, spawn in self._spawned.items() if spawn.sid == sid}
        fresh = {(spawn.slot, spawn.sid): spawn for spawn in found}
        if held == fresh:
            return False
        for key in held:
            self._spawned.pop(key, None)
        self._spawned.update(fresh)
        self._evict_spawned_to_cap_locked()
        return True

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
            # Only for a unit still held: a unit with no record already stops the
            # walk, so remembering it would only grow the set with history.
            if edge.sid in self._records:
                self._suspect_sids.add(edge.sid)
        self._incomplete = True
        self._over_cap = True
        self._prune_faults_locked()

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
            self._suspect_sids.discard(record.sid)
        self._incomplete = True
        self._over_cap = True
        self._prune_faults_locked()

    def _prune_faults_locked(self) -> None:
        """Drop each fault marker whose unit does not hold the evidence it was about.

        A marker is added only for a unit with a held decision (``edge``) or spawn
        (``spawn``), so this keeps the set within what the bounded stores hold.
        Caller holds the lock.
        """
        edge_sids = {edge.sid for edge in self._edges.values()}
        spawn_sids = {spawn.sid for spawn in self._spawned.values()}
        self._fault_sids = {
            (kind, sid)
            for kind, sid in self._fault_sids
            if sid in (edge_sids if kind == "edge" else spawn_sids)
        }

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
        self._changed()

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

        # The unit's spawns live in whichever segments recorded them, so the same
        # removal can strand them. Re-read on the same terms as the decision below.
        self._reconcile_spawned(sid, slot)

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
                self._fault_sids.add(("edge", sid))
                # A new ``nodes`` object, so a reader caching by its identity sees
                # the completeness change.
                self._nodes = None
                if sid in self._records:
                    self._suspect_sids.add(sid)
            return
        with self._lock:
            if ("edge", sid) in self._fault_sids:
                self._fault_sids.discard(("edge", sid))
                self._nodes = None
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
        self._changed()

    def _reconcile_spawned(self, sid: str, slot: str) -> None:
        """Re-read creator unit *sid*'s spawns after a partial removal. The disk
        decides: a complete read replaces what is held, a read that raises leaves it
        and marks the reading incomplete. No I/O when nothing is held for the unit."""
        from kiro_crew.crew_log.session_tree import SPAWNED_PER_UNIT_CAP, spawned_record
        from kiro_crew.crew_log.store import _checked_crew_log_root, find_spawned_entries

        with self._lock:
            if not any(spawn.sid == sid for spawn in self._spawned.values()):
                return
            # Children whose own log already names a parent: the fold applies that over
            # any spawn, so dropping such a spawn past the cap costs nothing and must
            # not report the unit cut. Read under the lock that guards ``_records``.
            self_parented = {
                rec.slot for rec in self._records.values() if rec.slot and rec.parent_slot
            }
        try:
            root = _checked_crew_log_root(KIND_SESSION)
            entries, cut, _hit_cap = find_spawned_entries(
                root / _store_name(sid), SPAWNED_PER_UNIT_CAP, self_parented
            )
        except (OSError, ValueError):
            with self._lock:
                self._fault_sids.add(("spawn", sid))
                # A new ``nodes`` object, so a reader caching by its identity sees
                # the completeness change.
                self._nodes = None
                if sid in self._records:
                    self._suspect_sids.add(sid)
            return
        found = [s for e in entries if (s := spawned_record(slot, sid, e)) is not None]
        with self._lock:
            if ("spawn", sid) in self._fault_sids:
                self._fault_sids.discard(("spawn", sid))
                self._nodes = None
            # This path never caches a scan identity for the unit (it pops it), so a
            # hit_cap-but-not-cut scan needs no extra guard here -- the next read is a
            # fresh reconcile. Only a load-bearing cut makes the projection incomplete.
            self._spawn_scans.pop(sid, None)
            if cut:
                # The newest spawns are installed, and the unit is reported as cut.
                self._incomplete = True
                self._over_cap = True
                if sid in self._records:
                    self._suspect_sids.add(sid)
            if not self._replace_unit_spawned_locked(sid, found):
                return
            self._nodes = None
            self._dirty = True
        self._changed()

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
            # The children this unit recorded go with it: the record they came from is
            # gone. A child that opened its own log still has whatever that log says.
            dropped_spawns = [key for key, spawn in self._spawned.items() if spawn.sid == sid]
            for key in dropped_spawns:
                self._spawned.pop(key, None)
            dropped_edges = dropped_edges + dropped_spawns
            self._spawn_scans.pop(sid, None)
            # The cached "this unit records no decision" verdict goes too: it is keyed by
            # a unit that is gone, so the only thing it could still answer about is a
            # directory some later session happens to be given the same name.
            self._scans.pop(sid, None)
            self._suspect_sids.discard(sid)
            faulted = bool({("edge", sid), ("spawn", sid)} & self._fault_sids)
            self._fault_sids -= {("edge", sid), ("spawn", sid)}
            if self._records.pop(sid, None) is None and not dropped_edges and not faulted:
                return
            self._nodes = None
            self._dirty = True
        self._changed()

    # ── cold start ─────────────────────────────────────────────────────────

    # Bound from session_tree_projection_seed: the seed, its install, and its retry.
    ensure_seeded = _stp_seed.ensure_seeded
    _retry_due_locked = _stp_seed._retry_due_locked
    _install_seed_locked = _stp_seed._install_seed_locked
    _install_seed_edges_locked = _stp_seed._install_seed_edges_locked
    _install_seed_spawned_locked = _stp_seed._install_seed_spawned_locked
    _seed = _stp_seed._seed
    _seed_inner = _stp_seed._seed_inner
    _replay_tail = _stp_seed._replay_tail
    _replay_spawned = _stp_seed._replay_spawned
    _arm_seed_retry = _stp_seed._arm_seed_retry
    _start_seed_retry_timer = _stp_seed._start_seed_retry_timer

    # ── the checkpoint ─────────────────────────────────────────────────────

    def _changed(self) -> None:
        """The state moved: tell the bus, then arm the checkpoint. Never raises.

        Every incremental change ends here, OUTSIDE the lock, so the push to a browser
        is the same event as the write to disk and neither can be forgotten for a path
        the other covers. A seed is the one exception: :meth:`ensure_seeded` announces
        once when it finishes, whatever the seed installed. The announcement goes first
        because it is the one a person is waiting on; the checkpoint is a shortcut for
        the next boot.
        """
        self._announce()
        self._schedule_checkpoint()

    def _announce(self) -> None:
        """Publish one :class:`TreeAdvanced`. Never raises, never blocks on a subscriber's work."""
        try:
            from kiro_crew.crew_log import bus

            bus.publish(bus.TREE_ADVANCED, bus.TreeAdvanced())
        except Exception:  # pragma: no cover -- ``bus.publish`` guards its subscribers
            log_exception_text(logger, logging.DEBUG, "session tree change could not be announced")

    # Bound from session_tree_projection_checkpoint: the debounced write and its cancellation.
    _schedule_checkpoint = _stp_checkpoint._schedule_checkpoint
    cancel_pending_checkpoint = _stp_checkpoint.cancel_pending_checkpoint
    _debounced_write = _stp_checkpoint._debounced_write
    flush_checkpoint = _stp_checkpoint.flush_checkpoint


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


def inherited_parent(slot: str, head_sid: str) -> str:
    """The creator the tree already holds for *slot*, or ``""``. No I/O.

    For a ``session/opened`` whose caller has no witness of its own -- a slot restored
    after a restart, whose ``_created_by`` came back from transcript metadata and so is
    not evidence. Without this the new log carries no ``parent`` and the slot's edge
    lives ONLY in its first log: the one :meth:`SessionTreeProjection._evict_to_cap_locked`
    drops first, being the oldest, and the one retention deletes first. A long-lived
    worker then folds as a root with no citation at all, under a conductor that is still
    open. Re-citing what the tree holds puts the edge in every log the slot writes, so
    it lasts as long as the slot does.

    The source is the crew log's own fold, never the slot's restored metadata, which is
    what keeps the fence ``_crew_log_lineage`` guards: an agent that edits a transcript
    file can change ``_created_by``, but not this.

    *head_sid* is the log the new one supersedes -- the ``previous_sid`` it is about to
    write. See :meth:`SessionTreeProjection.trusted_creator` for which of the slot's own
    logs must be held, and held cleanly, for the answer to be written down.

    ``""`` when the projection is not seeded for this store (an answer from another
    store, or a provisional empty one, is not evidence), and whenever that method
    answers ``""``.
    """
    if not slot or not head_sid:
        return ""
    try:
        proj = projection()
        if not proj.seeded_for_current_store:
            return ""
        return proj.trusted_creator(slot, head_sid)
    except Exception:  # pragma: no cover -- defensive; a read must not fail an open
        log_exception_text(logger, logging.DEBUG, "session tree parent could not be read")
        return ""


def spawned_creator(slot: str) -> str:
    """The creator a trusted ``session/spawned`` names for a logless *slot*, or ``""``.

    The fallback for :func:`inherited_parent` when the slot has no predecessor log to
    cite -- a child restored after a restart that happened before its first turn. The
    edge then lives only in the creator's ``session/spawned`` record, which this reads
    from the fold. No I/O; the same seeded-for-this-store guard applies, so a provisional
    or another store's answer is never used. See
    :meth:`SessionTreeProjection.spawned_creator` for the logless-only and creator-has-a-log
    conditions.
    """
    if not slot:
        return ""
    try:
        proj = projection()
        if not proj.seeded_for_current_store:
            return ""
        return proj.spawned_creator(slot)
    except Exception:  # pragma: no cover -- defensive; a read must not fail an open
        log_exception_text(logger, logging.DEBUG, "session tree spawn creator could not be read")
        return ""


def creator_of(slot: str) -> str:
    """The slot that created *slot*, from creation records, or ``""``. No I/O.

    Unlike the folded parent, a release does not clear it
    (:meth:`SessionTreeProjection.created`). ``""`` while the tree is not seeded for
    this store, like :func:`spawned_creator`.
    """
    if not slot:
        return ""
    try:
        proj = projection()
        if not proj.seeded_for_current_store:
            return ""
        held = proj.created().get(slot)
    except Exception:  # pragma: no cover -- defensive; a read must not fail a wake
        log_exception_text(logger, logging.DEBUG, "session tree creator could not be read")
        return ""
    return held[0] if held else ""


def created_children(creator: str) -> "tuple[str, ...]":
    """The slots *creator* created, newest first, from creation records. No I/O.

    ``()`` while the tree is not seeded for this store, like :func:`spawned_creator`:
    an unseeded tree names no children, which is not the same as having none.
    """
    if not creator:
        return ()
    try:
        proj = projection()
        if not proj.seeded_for_current_store:
            return ()
        created = proj.created()
    except Exception:  # pragma: no cover -- defensive; a read must not fail a fold read
        log_exception_text(logger, logging.DEBUG, "session tree children could not be read")
        return ()
    mine = [(at, slot) for slot, (by, at) in created.items() if by == creator]
    return tuple(slot for _at, slot in sorted(mine, reverse=True))


def _depth_readable_nodes() -> "dict[str, TreeNode] | None":
    """The folded tree's nodes when it can answer a depth at all, else ``None``. No I/O.

    ONE PLACE, because the censoring rule has to be one rule. A depth answer is only
    meaningful against a tree that is seeded for the store being read and whose reading
    is not ``incomplete``; a reading is ``incomplete`` when the population passed
    :data:`~kiro_crew.crew_log.session_tree.TREE_UNIT_CAP` or a unit's bytes faulted,
    which leaves a well-formed fold that is simply missing edges. Both
    :func:`slot_depth` and :func:`root_depth` refuse on the same answer from here, so
    the depth distribution cannot be censored on one side only: a tree that cannot
    place a child cannot be trusted to say another session is a root either, and
    dropping only the children would turn every over-cap install into a report that
    almost nothing delegates.

    Never raises: an opener must not fail because a number for a metric could not be
    read.
    """
    try:
        proj = projection()
        if not proj.seeded_for_current_store:
            return None
        reading = proj.reading()
        if reading.incomplete:
            return None
        return reading.nodes
    except Exception:  # pragma: no cover -- defensive; a read must not fail an open
        log_exception_text(logger, logging.DEBUG, "session tree depth could not be read")
        return None


def root_depth() -> "int | None":
    """``0`` for a session nobody created, or ``None`` when the folded tree cannot
    place sessions at all. No I/O.

    The ROOT half of :func:`slot_depth`, and it exists so that half is censored by the
    same condition. A root's depth needs no walk -- it is zero by definition -- but
    writing that zero while a child of an unreadable tree is withheld biases the
    distribution toward 0 by exactly the population the reader cares about most: a busy
    install, the one with the most delegation, is the one that passes
    :data:`~kiro_crew.crew_log.session_tree.TREE_UNIT_CAP` and starts withholding its
    children. Asking the tree first costs one in-memory read and makes the two answers
    absent together, so the depth counter either describes a tree it could read or says
    nothing about it.
    """
    return None if _depth_readable_nodes() is None else 0


def slot_depth(slot: str) -> "int | None":
    """How many creators stand ABOVE *slot* in the folded tree, or ``None``. No I/O.

    ``0`` is a root -- a slot the tree places under nobody -- and each hop up the
    parent chain is one more. For the ``depth`` a child's ``session/opened`` records:
    the caller asks for its CREATOR's depth and writes one more than the answer, so
    the number in the entry is the one this would return for the child once its own
    log is folded.

    ``None`` is "not established", and every refusal below folds into it because the
    one reader of this answer counts trees: a wrong 0 is an invented tree, while an
    absent answer is one session left out of a distribution. The refusals are an
    unseeded projection, a projection seeded for ANOTHER store, a reading reported
    ``incomplete``, a slot with no node, a slot on a cycle, and an ancestor the tree
    cannot place.

    That last one is the subtle refusal and it is deliberate. :func:`fold_tree`
    keeps a cited creator that has no log of its own as a CITATION rather than as a
    place, so the child is a root of the folded tree while still carrying its
    ``parent``. Counting it as depth 0 would report a tree that is really a branch
    of one whose top the fold cannot see, so a chain that ends at a citation rather
    than at a genuine root answers nothing at all.

    ``incomplete`` is refused for the reason the adoption guard refuses it: a unit
    whose bytes could not be read, or a population past
    :data:`~kiro_crew.crew_log.session_tree.TREE_UNIT_CAP`, leaves a well-formed
    fold that is simply missing edges -- and a missing edge is exactly what makes a
    child look like a root.

    Never raises: an opener must not fail because a number for a metric could not be
    read.
    """
    if not slot:
        return None
    nodes = _depth_readable_nodes()
    if nodes is None:
        return None
    cursor = nodes.get(slot)
    if cursor is None or cursor.cycle:
        return None
    depth = 0
    # Bounded by the POPULATION rather than by a constant, which is both the tightest
    # true bound and the one that cannot drift: a simple chain of n nodes has depth at
    # most n - 1, so a walk that takes n hops without reaching a root is walking a
    # structure that is not a tree. ``cycle`` already marks the one way the records
    # produce that, and this bound is what makes the walk terminate even if a future
    # fold leaves a cycle unmarked -- a loop over a shared dict read per session open
    # has to be unable to hang whatever it is handed.
    for _ in range(len(nodes)):
        parent_slot = cursor.parent_slot
        if parent_slot is None:
            return depth
        parent = nodes.get(parent_slot)
        if parent is None or parent.cycle:
            # A citation the fold could not place, or an ancestor on a cycle. Either
            # way the top of this chain is not readable, so no depth is.
            return None
        depth += 1
        cursor = parent
    return None


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


def record_spawned(
    creator_sid: str, creator_slot: str, child_slot: str, at: int, seq: int = 0
) -> None:
    """Fold a just-committed ``session/spawned`` into the projection.

    The emitter's door for a creation, on the terms :func:`record_adopted` keeps. Never
    raises: the append already succeeded, and a spawn the memory missed is recovered by
    the replay on the next cold start.
    """
    try:
        projection().apply_spawned(
            EdgeRecord(
                slot=child_slot or "",
                parent_slot=creator_slot or None,
                at=at,
                sid=creator_sid,
                seq=seq,
            )
        )
    except Exception:  # pragma: no cover -- defensive
        log_exception_text(logger, logging.DEBUG, "session tree projection could not apply a spawn")


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


#: BASE imports this module binds for its importers and for the patches the mirror
#: routes through it, though its own code does not read them.
_BOUND_FOR_IMPORTERS = (
    Any,
    CHECKPOINT_DIR,
    Final,
    MAX_ACP_SESSION_ID_LEN,
    MAX_CHECKPOINT_BYTES,
    MAX_SHORT_STRING,
    Path,
    _ScanFaults,
    json,
    time,
)
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.crew_log.session_tree_projection_checkpoint",
    "kiro_crew.crew_log.session_tree_projection_seed",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
#: The unsplit module's names: everything the family binds except the methods the class
#: binds from its owners, which were never module names.
_SURFACE = frozenset().union(globals(), *(vars(_sys.modules[o]) for o in _OWNERS)) - {
    n for n, m in vars(SessionTreeProjection).items() if getattr(m, "__module__", "") in _OWNERS
}
_EXPORTS.update(
    _facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=(), surface=_SURFACE).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.crew_log.session_tree_projection_checkpoint import (  # noqa: F401
        CHECKPOINT_DEBOUNCE_SECS,
        CHECKPOINT_NAME,
        CHECKPOINT_VERSION,
        _checkpoint_path,
        _edge_from_json,
        _edge_to_json,
        _load_checkpoint,
        _LoadedCheckpoint,
        _record_from_json,
        _record_to_json,
        _ReplayResult,
        _save_checkpoint,
        _scans_from_json,
    )
    from kiro_crew.crew_log.session_tree_projection_seed import (  # noqa: F401
        SEED_RETRY_COOLDOWN_SECS,
    )

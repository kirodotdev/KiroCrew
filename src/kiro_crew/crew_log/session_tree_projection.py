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
    OpenedRecord,
    TreeNode,
    TreeReading,
    fold_tree,
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
#: cannot be proven to describe this store -- is rebuilt from the log instead.
CHECKPOINT_VERSION: Final[int] = 2

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
                self._nodes = fold_tree(self._records.values())
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
                self._nodes = fold_tree(self._records.values())
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

    def forget(self, sid: str) -> None:
        """Drop one unit's record -- retention removed it, or a delete took it.

        Best effort and silent about a record it does not hold: removal is reported by
        the code that did it, and a projection asked to forget something twice has
        nothing to complain about.

        A removal arriving while a seed's scan is running is REMEMBERED even though
        there is nothing to pop. The scan may have listed that unit moments before it
        was deleted, and installing its result would otherwise put the record back --
        so the sid is recorded and the install drops it.
        """
        if not sid:
            return
        with self._lock:
            if self._seeding:
                self._forgotten_while_seeding.add(sid)
            if self._records.pop(sid, None) is None:
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
            reading = scanner.reading(live_sids)
            with self._lock:
                # Flags first, then the merge: eviction can only escalate them, so
                # assigning the scan's values afterwards would undo that escalation.
                self._incomplete = reading.incomplete
                self._over_cap = scanner.over_cap
                self._install_seed_locked({r.sid: r for r in reading.records if r.sid})
                self._nodes = None
                self._seeded = True
                self._dirty = True
            self._schedule_checkpoint()
            return
        records, incomplete, over_cap = self._replay_tail(loaded)
        with self._lock:
            self._incomplete = incomplete
            self._over_cap = over_cap
            self._install_seed_locked(records)
            self._nodes = None
            self._seeded = True
            # Compared AFTER the merge, against the state actually installed: a record
            # that arrived during the scan is a difference from the checkpoint and owes
            # a write, which comparing the replay's own output would miss.
            moved = self._records != loaded
            if moved:
                self._dirty = True
        # Only worth a write when the seed actually moved something; an untouched
        # checkpoint is already what is on disk.
        if moved:
            self._schedule_checkpoint()

    def _replay_tail(
        self, loaded: dict[str, OpenedRecord]
    ) -> "tuple[dict[str, OpenedRecord], bool, bool]":
        """Reconcile a loaded checkpoint against the store, at the cost of the DELTA.

        One ``iterdir`` for the root's entry NAMES -- no per-unit stat, which is the
        syscall this module exists to stop paying per read. A name the checkpoint does
        not hold gets its head read; a held record whose name is gone is dropped.
        Normally both sets are empty and this is a single directory listing.

        Returns ``(records, incomplete, over_cap)``. A listing that fails at all makes
        the answer ``incomplete``: the records are still served, because a stale edge
        renders as a root and that is the pre-existing degradation, but a reader that
        DECIDES on an edge is told the reconciliation did not complete.
        """
        from kiro_crew.crew_log.session_tree import TREE_UNIT_CAP, opened_record
        from kiro_crew.crew_log.store import (
            _checked_crew_log_root,
            oldest_segment,
            read_head,
        )

        records = dict(loaded)
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
            return records, False, False
        except OSError:
            logger.warning(
                "session tree projection: the store root could not be listed, so its "
                "checkpoint could not be reconciled; lineage reads as incomplete",
                exc_info=True,
            )
            return records, True, False

        held = {_store_name(sid): sid for sid in records}
        gone = [sid for name, sid in held.items() if name not in names]
        for sid in gone:
            records.pop(sid, None)
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
        return records, incomplete or over_cap, over_cap

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
            if not self._records and not self._dirty:
                return False
            payload = {
                "ver": CHECKPOINT_VERSION,
                "written_at": int(time.time() * 1000),
                "root": self._root or _current_root(),
                "records": [_record_to_json(r) for r in self._records.values()],
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


def _load_checkpoint() -> "dict[str, OpenedRecord] | None":
    """The checkpoint's records, or ``None`` when there is no usable one.

    ``None`` is every failure, undifferentiated on purpose: absent, unreadable,
    oversized, unparseable, wrong ``ver``, or a payload whose shape this does not
    recognise all mean the same thing to the caller -- rebuild, which is always
    correct. Never raises, and never migrates: a ``ver`` mismatch is DISCARDED, because
    forward-applying an older build's state is how a fold quietly becomes garbage.
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
    records: dict[str, OpenedRecord] = {}
    for raw in rows:
        record = _record_from_json(raw)
        if record is not None:
            records[record.sid] = record
    return records


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


def retract_unit_parent(sid: str) -> None:
    """Clear a unit's held citation, keeping the record. Never raises, same reason."""
    try:
        projection().retract_parent(sid)
    except Exception:  # pragma: no cover -- defensive
        logger.debug("session tree projection could not retract a citation", exc_info=True)


def forget_unit(sid: str) -> None:
    """Drop a removed unit from the projection. Never raises, for the same reason."""
    try:
        projection().forget(sid)
    except Exception:  # pragma: no cover -- defensive
        logger.debug("session tree projection could not forget a unit", exc_info=True)

"""The session tree projection's cold start: the state, established once per process and store.

``ensure_seeded`` loads the checkpoint and reconciles it against the store with one tail
replay proportional to the delta (``_replay_tail`` for records and decisions,
``_replay_spawned`` for spawns) or, with no usable checkpoint, seeds from one cold
:class:`~kiro_crew.crew_log.session_tree.SessionTree` scan. It installs the result over
the commits made while the scan ran (``_install_seed_locked`` and its two siblings), and
re-attempts a seed that raised once ``SEED_RETRY_COOLDOWN_SECS`` has passed, on a timer
of its own. Each function here whose first parameter is ``self`` is bound as the
``SessionTreeProjection`` attribute of the same name in
:mod:`kiro_crew.crew_log.session_tree_projection`, which keeps the projection's state,
so ``self.<name>(...)`` calls and instance and class patches reach it as they reach a
method defined in the class body. They read ``_cancel_epoch``, ``_edges``,
``_forgotten_while_seeding``, ``_lock``, ``_records``, ``_root``, ``_scans``,
``_seed_gate``, ``_seed_retry_at``, ``_seed_retry_timer``, ``_seeded``, ``_spawn_scans``
and ``_spawned``, write ``_cancel_epoch``, ``_dirty``, ``_edges``, ``_incomplete``,
``_nodes``, ``_over_cap``, ``_records``, ``_root``, ``_scans``, ``_seed_retry_at``,
``_seed_retry_timer``, ``_seeded``, ``_seeding``, ``_spawn_scans``, ``_spawned``,
``_suspect_sids`` and ``_unattributed_gap``, and update ``_forgotten_while_seeding`` and
``_suspect_sids`` in place. New seeding work goes here.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

from kiro_crew.crew_log.checkpoint import CHECKPOINT_DIR
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.session_tree import (
    EdgeRecord,
    OpenedRecord,
    _ScanFaults,
    edge_supersedes,
    log_rank_of,
)
from kiro_crew.crew_log.session_tree_projection_checkpoint import _current_root, _load_checkpoint
from kiro_crew.crew_log.store import log_exception_text
from kiro_crew.session_ledger import _store_name

if TYPE_CHECKING:
    from kiro_crew.crew_log.session_tree_projection import SessionTreeProjection
    from kiro_crew.crew_log.session_tree_projection_checkpoint import ScanIdentity, _ReplayResult

logger = logging.getLogger("kiro_crew.crew_log.session_tree_projection")


#: How long the projection waits before re-attempting a seed that RAISED, in seconds.
#: A failed seed serves an empty tree, so latching it for the life of the process makes
#: one transient store fault cost every reader its lineage until the gateway restarts --
#: the seed gate is otherwise cleared only by the data home changing. A cooldown rather
#: than an immediate retry because the readers arrive on a timer, and a bare unlatch
#: would re-pay a full cold scan every few seconds for as long as the store stays broken.
#: Sized so a fault that clears is picked up within a minute without the retries
#: themselves becoming the load.
SEED_RETRY_COOLDOWN_SECS: Final[float] = 30.0


def ensure_seeded(self: SessionTreeProjection, live_sids: "tuple[str, ...]" = ()) -> None:
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
    then ONE tail replay proportional to the delta (see the facade's docstring).
    With no checkpoint -- first boot on this build -- one cold
    :meth:`SessionTree.reading` seeds the records and both flags, and that
    scanner is not called again.

    Never raises. A seed that cannot be established leaves the projection empty
    and ``incomplete``, which every consumer renders as "no creator known" --
    the same answer the pages gave before any of this existed. That state is
    RE-ATTEMPTED: the next call after :data:`SEED_RETRY_COOLDOWN_SECS` seeds again,
    so a store fault that clears costs a minute of missing lineage rather than the
    rest of the process's life. A seed that SUCCEEDS is final for its store.
    """
    root = _current_root()
    with self._seed_gate:
        with self._lock:
            # An unreadable root identifies no store, so it neither satisfies the
            # gate nor condemns the fold: keep the identity already held and let the
            # next readable answer decide. See ``seeded_for_current_store``.
            target = root or self._root
            if self._seeded and self._root == target and not self._retry_due_locked():
                return
            if target != self._root:
                # A different store. Drop the previous one's fold rather than
                # reconciling it: none of those records describe this store, and a
                # replay would keep every one it could not disprove.
                self._records = {}
                self._edges = {}
                self._spawned = {}
                self._spawn_scans = {}
                self._nodes = None
                self._incomplete = False
                self._over_cap = False
                self._suspect_sids = set()
                self._unattributed_gap = False
                self._seeded = False
                self._seed_retry_at = None
                self._dirty = False
                # An armed debounced write pinned the PREVIOUS store's path but
                # builds its payload from ``_records`` when it wakes -- which is
                # about to hold this store's records. Left alone it would write
                # them into the other store's checkpoint, where a later read would
                # trust them and report one store's lineage as the other's. Moving
                # the epoch makes that worker a no-op.
                self._cancel_epoch += 1
            # Stamped BEFORE the seed runs, not after it returns. The seed installs
            # its records and raises ``_seeded`` under the lock, so a stamp on the
            # way out leaves a window where the fold is complete and this object
            # still names the previous store -- and every reader in that window is
            # told the projection is unseeded and ships no lineage. The fold and the
            # identity it belongs to have to become true together, and the identity
            # is known first.
            self._root = target
        try:
            self._seed(live_sids)
        except Exception:  # pragma: no cover -- defensive; every step is guarded
            logger.warning(
                "session tree projection could not be seeded; reporting no lineage "
                "until the next attempt",
                exc_info=True,
            )
            with self._lock:
                self._seeded = True
                self._incomplete = True
                self._unattributed_gap = True
                self._nodes = None
                # A store fault is usually transient, and the state it leaves is an
                # EMPTY tree, so latching it is a much worse answer than re-paying
                # a scan: the gate above is the only thing that clears for an
                # unchanged root, so without an expiry the first fault of a process
                # costs every reader its lineage until the gateway restarts.
                #
                # A cooldown rather than an immediate retry because the readers are
                # on a timer -- the memory sampler asks per sample -- so a bare
                # unlatch would re-pay a full cold scan every few seconds for as
                # long as the store stays broken, which is the cost the projection
                # exists to remove.
                self._seed_retry_at = time.monotonic() + SEED_RETRY_COOLDOWN_SECS
                epoch = self._cancel_epoch
            # The re-attempt is ARMED, not left to the next reader. The sidebar does
            # not poll, and a quiet gateway may send no frame that would ask, so
            # without this a failed seed leaves the tree empty until something else
            # happens to read it. A seed that then lands announces, and the
            # dashboard pushes the rows it settles.
            self._arm_seed_retry(target, epoch)
        else:
            with self._lock:
                # Established, so nothing is owed. Cleared on the way out of a
                # SUCCESSFUL seed only, which is what makes a recovered store stop
                # re-scanning.
                self._seed_retry_at = None
        # Announced whether the seed succeeded or not, and even when it moved nothing
        # since the checkpoint: either way the answer a reader gets has just gone from
        # "pending" to final, and a sidebar that painted the pending frame learns it
        # from this event instead of asking again.
        self._announce()


def _retry_due_locked(self: SessionTreeProjection) -> bool:
    """Whether a seed that failed earlier may be re-attempted now. ``_lock`` held.

    False whenever the state was established, which is the ordinary path: the marker
    is set only by the handler above, so a healthy projection answers without reading
    the clock.
    """
    at = self._seed_retry_at
    return at is not None and time.monotonic() >= at


def _install_seed_locked(self: SessionTreeProjection, scanned: "dict[str, OpenedRecord]") -> None:
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
    # The suspects a seed reported, cut to the units it installed: like every other
    # field here, the set is bounded by the records it describes.
    self._suspect_sids.intersection_update(self._records)


def _install_seed_edges_locked(
    self: SessionTreeProjection, scanned: "dict[tuple[str, str], EdgeRecord]"
) -> None:
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


def _install_seed_spawned_locked(
    self: SessionTreeProjection, scanned: "dict[tuple[str, str], EdgeRecord]"
) -> None:
    """Install the spawns a seed established, keeping any committed meanwhile.

    Caller holds the lock. A spawn applied while the scan ran came from an append
    that completed, so it is kept over the scan's reading, on the terms
    :meth:`_install_seed_locked` gives for records. A unit removed while the scan
    ran takes its spawns with it.
    """
    merged = {
        key: spawn
        for key, spawn in scanned.items()
        if spawn.sid not in self._forgotten_while_seeding
    }
    for key, held in self._spawned.items():
        if held.sid not in self._forgotten_while_seeding:
            merged[key] = held
    self._spawned = merged
    self._evict_spawned_to_cap_locked()


def _seed(self: SessionTreeProjection, live_sids: "tuple[str, ...]") -> None:
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


def _seed_inner(self: SessionTreeProjection, live_sids: "tuple[str, ...]") -> None:
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
            self._suspect_sids = set(reading.suspect_sids)
            self._unattributed_gap = reading.unattributed_fault
            self._install_seed_locked({r.sid: r for r in reading.records if r.sid})
            # EVERY decision the scan read, not one per slot. Reducing here would
            # choose between two logs without the record order that ranks them --
            # ``latest_edges`` falls back to the timestamp with no ranking, so a
            # clock that stepped backward between the two logs would pick the older
            # decision and the cold rebuild would restore a lineage that had been
            # superseded. The fold reduces instead, with the ranking it derives from
            # these same records, which is also the one place that choice is made.
            self._install_seed_edges_locked({(e.slot, e.sid): e for e in reading.edges if e.slot})
            self._install_seed_spawned_locked(
                {(s.slot, s.sid): s for s in reading.spawned if s.slot}
            )
            self._nodes = None
            self._seeded = True
            self._dirty = True
        self._schedule_checkpoint()
        return
    faults = _ScanFaults()
    records, edges, scans, incomplete, over_cap = self._replay_tail(
        loaded[0], loaded[1], loaded[2], faults=faults
    )
    spawned, spawn_scans, spawn_incomplete = self._replay_spawned(
        records, loaded[3], loaded[4], faults=faults
    )
    incomplete = incomplete or spawn_incomplete
    with self._lock:
        self._incomplete = incomplete
        self._over_cap = over_cap
        self._suspect_sids = set(faults.sids)
        self._unattributed_gap = faults.unattributed
        self._install_seed_locked(records)
        self._install_seed_edges_locked(edges)
        # Kept only for units the merge actually holds, so the cache cannot outlive
        # the population it describes and grow without bound across boots.
        self._scans = {sid: ident for sid, ident in scans.items() if sid in self._records}
        # Installed BEFORE the spawns, because installing them can evict past the
        # cap, and eviction drops the evicted unit's identity. Assigning the cache
        # afterwards would restore it, and the next boot would skip a unit whose
        # spawns are not all held.
        self._spawn_scans = {
            sid: ident for sid, ident in spawn_scans.items() if sid in self._records
        }
        self._install_seed_spawned_locked(spawned)
        self._nodes = None
        self._seeded = True
        # Compared AFTER the merge, against the state actually installed: a record
        # that arrived during the scan is a difference from the checkpoint and owes
        # a write, which comparing the replay's own output would miss. The scan cache
        # counts too: a boot that learned a unit records no decision has something
        # worth writing even when the tree itself did not move, since that is exactly
        # what spares the NEXT boot the read.
        moved = (
            self._records != loaded[0]
            or self._edges != loaded[1]
            or self._scans != loaded[2]
            or self._spawned != loaded[3]
            or self._spawn_scans != loaded[4]
        )
        if moved:
            self._dirty = True
    # Only worth a write when the seed actually moved something; an untouched
    # checkpoint is already what is on disk.
    if moved:
        self._schedule_checkpoint()


def _replay_tail(
    self: SessionTreeProjection,
    loaded: dict[str, OpenedRecord],
    loaded_edges: dict[tuple[str, str], EdgeRecord],
    loaded_scans: "dict[str, ScanIdentity] | None" = None,
    *,
    faults: "_ScanFaults | None" = None,
) -> "_ReplayResult":
    """Reconcile a loaded checkpoint against the store, at the cost of the DELTA.

    One ``iterdir`` for the root's entry NAMES -- no per-unit stat, which is the
    syscall the projection exists to stop paying per read. A name the checkpoint does
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
        if faults is not None:
            faults.unattributed = True
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
    held_records = list(records.values())
    if faults is not None:
        # Past the cap the pass below reads no decision, yet the record itself is
        # kept until the install evicts down to the cap -- and the one kept need not
        # be the one evicted. A unit whose decision was never read is suspect for the
        # same reason one whose read faulted is.
        faults.sids.extend(r.sid for r in held_records[TREE_UNIT_CAP:] if r.sid)
    for record in held_records[:TREE_UNIT_CAP]:
        if not record.slot:
            continue
        directory = root / _store_name(record.sid)
        key = (record.slot, record.sid)
        try:
            identity = tree_edge_scan_identity(directory)
        except OSError:
            # Could not even stat the unit, so nothing is established about it.
            incomplete = True
            if faults is not None:
                faults.sids.append(record.sid)
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
            if faults is not None:
                faults.sids.append(record.sid)
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


def _replay_spawned(
    self: SessionTreeProjection,
    records: dict[str, OpenedRecord],
    loaded_spawned: dict[tuple[str, str], EdgeRecord],
    loaded_scans: "dict[str, ScanIdentity]",
    *,
    faults: "_ScanFaults | None" = None,
) -> "tuple[dict[tuple[str, str], EdgeRecord], dict[str, ScanIdentity], bool]":
    """Reconcile the checkpoint's spawns against the store, unit by unit.

    A unit whose log identity still matches the one its spawns were last read at
    keeps the checkpoint's answer with no read. Any other unit is read in full and
    its answer replaces the checkpoint's -- including with none, since a complete
    read that finds nothing is the only evidence that the entries are gone. A read
    that raises keeps the checkpoint's answer and reports the replay incomplete.
    Spawns cited by a unit missing from *records* are dropped.

    Returns ``(spawned, spawn_scans, incomplete)``.
    """
    from kiro_crew.crew_log.session_tree import (
        SPAWNED_PER_UNIT_CAP,
        TREE_UNIT_CAP,
        spawned_record,
    )
    from kiro_crew.crew_log.store import (
        _checked_crew_log_root,
        find_spawned_entries,
        tree_edge_scan_identity,
    )

    spawned = {key: s for key, s in loaded_spawned.items() if s.sid in records}
    scans = {sid: ident for sid, ident in loaded_scans.items() if sid in records}
    incomplete = False
    # Children whose own log already names a parent: the fold applies that over any
    # spawn, so a spawn for such a child is dead weight and dropping it past the cap
    # must not report the creator cut.
    self_parented = {rec.slot for rec in records.values() if rec.slot and rec.parent_slot}
    try:
        root = _checked_crew_log_root(KIND_SESSION)
    except OSError:
        # Already reported by the record replay, which lists the same root.
        return spawned, scans, True
    for record in list(records.values())[:TREE_UNIT_CAP]:
        if not record.slot:
            continue
        directory = root / _store_name(record.sid)
        try:
            identity = tree_edge_scan_identity(directory)
        except OSError:
            incomplete = True
            if faults is not None:
                faults.sids.append(record.sid)
            continue
        if identity is not None and scans.get(record.sid) == identity:
            continue
        try:
            entries, cut, hit_cap = (
                find_spawned_entries(directory, SPAWNED_PER_UNIT_CAP, self_parented)
                if identity is not None
                else ([], False, False)
            )
        except (OSError, ValueError):
            incomplete = True
            if faults is not None:
                faults.sids.append(record.sid)
            continue
        for key in [k for k, s in spawned.items() if s.sid == record.sid]:
            spawned.pop(key, None)
        for entry in entries:
            spawn = spawned_record(record.slot, record.sid, entry)
            if spawn is not None:
                spawned[(spawn.slot, spawn.sid)] = spawn
        if cut:
            # A load-bearing cut is not this unit's whole answer: report it.
            incomplete = True
            if faults is not None:
                faults.sids.append(record.sid)
        if hit_cap:
            # The cap evicted at least one spawn, self-parented or not. Even when
            # the eviction was all self-parented (cut suppressed), a later removal
            # of one of those children's own logs leaves this creator's identity
            # unchanged -- so caching this scan would re-serve an answer that is no
            # longer complete. Never cache a capped scan; re-read it next boot.
            scans.pop(record.sid, None)
        elif identity is not None:
            scans[record.sid] = identity
        else:
            scans.pop(record.sid, None)
    return spawned, scans, incomplete


def _arm_seed_retry(self: SessionTreeProjection, root: "str | None", epoch: int) -> None:
    """Re-attempt a failed seed once its cooldown has passed. Never raises.

    A :class:`threading.Timer`, not a sleep on the maintenance pool: a store that
    stays broken would otherwise hold one of that pool's workers for every
    cooldown, and the pool is shared with the checkpoint writes. The timer's own
    thread runs the seed, so nothing shared waits.

    Abandoned when the projection was discarded (the epoch moved), when the state
    was established some other way, or when the store changed under it: in each case
    the next reader seeds the store it has. A timer that fires a clock tick before
    the deadline it was armed for (Windows ticks are about 16 ms) re-arms for the
    rest instead of reading "not due" and giving up for good.
    """

    def _fire() -> None:
        with self._lock:
            self._seed_retry_timer = None
            if epoch != self._cancel_epoch or self._seed_retry_at is None:
                return
            remaining = self._seed_retry_at - time.monotonic()
        if remaining > 0:
            self._start_seed_retry_timer(remaining + 0.001, _fire)
            return
        if _current_root() != root:
            return
        self.ensure_seeded()

    with self._lock:
        at = self._seed_retry_at
    delay = SEED_RETRY_COOLDOWN_SECS if at is None else max(0.0, at - time.monotonic())
    self._start_seed_retry_timer(delay + 0.001, _fire)


def _start_seed_retry_timer(self: SessionTreeProjection, delay: float, fire: "Any") -> None:
    """Start ONE daemon timer for :meth:`_arm_seed_retry`. Never raises."""
    try:
        timer = threading.Timer(delay, fire)
        timer.daemon = True
        with self._lock:
            held = self._seed_retry_timer
            self._seed_retry_timer = timer
        if held is not None:
            held.cancel()
        timer.start()
    except Exception:
        log_exception_text(logger, logging.DEBUG, "session tree seed retry could not be scheduled")

"""The driver: fold events through registered definitions and report real changes.

One registry serves many STORES. A store is whatever unit its client keeps one log
per -- a member slug, a session id -- and it is a plain string here because the
kernel only ever uses it to keep one client's units apart from another's. Folded
state is cached per ``(key, store)`` in a cell that also records how far that cell
has observed, and that WATERMARK is what makes folding idempotent: an event at or
below it was already folded into this cell, so it is dropped rather than applied a
second time. A client may therefore re-drive a range it is unsure about and pay
nothing for the overlap.

The registry never reads inside an event. It needs exactly one number from each --
the ``seq`` that orders the log -- and it takes that from a reader the client
supplies, so a carrier type stays in the package that owns it (contract: the kernel
imports no domain's event type). The kernel ships two readers:
:func:`mapping_seq` for a carrier subscripted like a mapping, which is the member
log's ``Event`` TypedDict and the default, and :func:`attribute_seq` for one
holding it as a field, which is the crew log's ``Entry`` dataclass.

``mapping_seq`` is the default rather than a required argument because it is what
every present caller of a TypedDict envelope wants, and a required argument would
make each construction site restate it. A client whose carrier is shaped otherwise
passes its own reader, and the kernel needs no change: the crew log's slot folds
pass one that reads the ordinal they wrap each entry in.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from kiro_crew.projection.checkpoint import (
    EMPTY_WATERMARK,
    Admit,
    CheckpointStore,
    Savepoint,
)
from kiro_crew.projection.definition import ProjectionDefinition

#: How the registry reads the ordering number out of one event.
SeqOf = Callable[[Any], int]

#: on_change(store, key, view, seq)
OnChange = Callable[[str, str, dict, int], None]


def mapping_seq(event: Any) -> int:
    """``seq`` out of a carrier subscripted like a mapping (a TypedDict envelope)."""
    return event["seq"]


def attribute_seq(event: Any) -> int:
    """``seq`` out of a carrier holding it as a field (a dataclass entry)."""
    return event.seq


class _Cell:
    """Per-(key, store) folded state and how far it has observed."""

    __slots__ = ("state", "observed_seq")

    def __init__(self, state: Any, observed_seq: int) -> None:
        self.state = state
        self.observed_seq = observed_seq


class ProjectionRegistry:
    def __init__(self, seq_of: SeqOf = mapping_seq) -> None:
        self._defns: dict[str, ProjectionDefinition] = {}
        self._cells: dict[tuple[str, str], _Cell] = {}
        self._on_change: OnChange | None = None
        self._seq_of = seq_of
        self._lock = threading.Lock()

    # ---- registration -----------------------------------------------------
    def register(self, defn: ProjectionDefinition) -> Callable[[], None]:
        """Register a unit; return a disposer. Duplicate key raises."""
        with self._lock:
            if defn.key in self._defns:
                raise ValueError(f"projection key {defn.key!r} already registered")
            self._defns[defn.key] = defn

        def dispose() -> None:
            with self._lock:
                self._defns.pop(defn.key, None)
                for k in [ck for ck in self._cells if ck[0] == defn.key]:
                    del self._cells[k]

        return dispose

    def set_on_change(self, cb: OnChange | None) -> None:
        with self._lock:
            self._on_change = cb

    # ---- priming ----------------------------------------------------------
    def prime(self, store: str, events: Iterable[Any]) -> None:
        """Fold all of a store's events from ``init()``, no change callbacks.

        ONE pass over *events*, folding every definition as each event arrives,
        rather than a pass per definition. That is what lets the caller hand over a
        STREAM: a per-definition loop has to re-read, so it forces the whole history
        into a list first, and that list is as long as the store's life. The cost
        is unchanged either way -- definitions times events -- only the peak moves.

        A definition that offers ``apply_owned`` is folded onto ONE private state:
        ``init()`` is deep-copied once, and every event then edits that copy in place.
        Nothing outside this method can see the states between the first event and the
        last -- no callback fires and no cell holds them until the loop ends -- so the
        copy ``apply`` makes per event to keep an observed state intact buys nothing
        here, and paying it costs a copy that grows with the state on every event: a
        cold prime of N events would be O(N^2). A definition without ``apply_owned``
        keeps ``apply``. :meth:`drive` always uses ``apply``, because there the prior
        state IS observed: :meth:`cells` hands it out as held.
        """
        with self._lock:
            defns = list(self._defns.values())
            folders: list[Callable[[Any, Any], Any]] = []
            states: list[Any] = []
            for defn in defns:
                owned = getattr(defn, "apply_owned", None)
                if owned is None:
                    folders.append(defn.apply)
                    states.append(defn.init())
                else:
                    folders.append(owned)
                    # The one copy: ``init()`` may hand back an object it keeps, and the
                    # state about to be edited in place has to be this pass's alone.
                    states.append(copy.deepcopy(defn.init()))
            last_seq = EMPTY_WATERMARK
            for ev in events:
                last_seq = self._seq_of(ev)
                for i, folder in enumerate(folders):
                    states[i] = folder(states[i], ev)
            for defn, state in zip(defns, states):
                self._cells[(defn.key, store)] = _Cell(state, last_seq)

    # ---- checkpointed priming ---------------------------------------------
    def prime_checkpointed(
        self,
        store: str,
        checkpoints: CheckpointStore,
        identity: Mapping[str, Any],
        tail_from: Callable[[int], Iterable[Any]],
        *,
        admit: Admit | None = None,
    ) -> int:
        """Restore every unit from its savepoint, then fold only the tail past it.

        Returns the seq the restore started folding from minus one -- that is, the
        watermark the whole set resumed at, which is ``EMPTY_WATERMARK`` when any
        unit had no usable savepoint. A caller that wants to know whether the
        shortcut was taken at all compares it against ``EMPTY_WATERMARK``.

        The FLOOR, not each unit's own watermark, decides where the tail starts, and
        that is the whole correctness argument. Units are saved independently, so one
        can be newer than another; folding each from its own watermark would need a
        separate pass per unit over a different range, and the single pass this makes
        instead hands every unit the same events. A unit already past an event drops
        it on its own watermark as the fold applies it, so replaying from the floor
        costs the newer units nothing and cannot double-count.

        *tail_from* is the client's, because the kernel owns no log: it is called
        ONCE with the floor and returns the events after it. Taking a callable rather
        than an iterable is what lets a client read fewer records instead of reading
        the whole log and discarding the front -- which is the cost this method
        exists to remove.

        No change callbacks fire, matching :meth:`prime`: a restore is not news.
        """
        with self._lock:
            defns = list(self._defns.values())
            if not defns:
                return EMPTY_WATERMARK
            restored: dict[str, tuple[Any, int]] = {}
            for defn in defns:
                savepoint = checkpoints.load(
                    store,
                    defn.key,
                    state_version=defn.state_version,
                    identity=identity,
                    admit=admit,
                )
                if savepoint is None:
                    # One unusable savepoint costs only its own unit a cold fold, but
                    # the SHARED pass has to start low enough for that unit, so the
                    # floor drops to empty and every unit refolds. Cheaper than a
                    # per-unit range, and it cannot serve a stale value.
                    restored.clear()
                    break
                restored[defn.key] = (savepoint.state, savepoint.watermark)
            if len(restored) != len(defns):
                floor = EMPTY_WATERMARK
                for defn in defns:
                    self._cells[(defn.key, store)] = _Cell(defn.init(), EMPTY_WATERMARK)
            else:
                floor = min(watermark for _, watermark in restored.values())
                for defn in defns:
                    state, watermark = restored[defn.key]
                    self._cells[(defn.key, store)] = _Cell(state, watermark)

        # Fold the tail OUTSIDE the lock, through the same fold a live event takes, so
        # a resumed fold and a cold fold run the same code over the same events. A
        # second tail-folding loop here would be a path that can disagree with that
        # fold about the same bytes, and nothing in the file would say which one is
        # right. Emission is off for the same reason :meth:`prime` has none: these
        # events are history the client's store already holds.
        #
        # A COLD pass (EVERY cell at or below seq 0: no unit has folded an event yet,
        # since seqs start at 1 -- the crew log's session reader hands in ``initial()``
        # checkpoints at seq 0 rather than no checkpoints) whose definitions all offer ``apply_owned`` is folded onto private copies instead --
        # one deep copy per definition, then in place per event, with the same per-cell
        # watermark rule ``_fold_one`` applies -- and installed once at the end. A cold
        # session fold reaches here over the whole log, and stepping each event through
        # ``apply`` copied the growing state once per entry: O(N^2). A RESUMED pass keeps
        # ``apply``: its tail is only what arrived since the savepoint, and a restored
        # state is untrusted -- a fold's copier is what refuses one malformed below its
        # top level, which the savepoint readers rely on to retire the file. A registry
        # with any definition lacking ``apply_owned`` (the member event log's, driven
        # live by other threads) keeps this path too.
        # Every cell, not the floor: a bundle mixing one fold at seq 0 with another
        # resumed further along has a floor of 0, and that resumed state is exactly the
        # untrusted one the copier must still see.
        owned = [getattr(defn, "apply_owned", None) for defn in defns]
        with self._lock:
            cold = all(self._cells[(defn.key, store)].observed_seq <= 0 for defn in defns)
        if cold and all(fn is not None for fn in owned):
            self._fold_tail_owned(store, defns, owned, tail_from(floor))
            return floor
        for event in tail_from(floor):
            self._fold_one(store, event, emit=False)
        return floor

    def _fold_tail_owned(
        self,
        store: str,
        defns: list[ProjectionDefinition],
        owned: list[Any],
        events: Iterable[Any],
    ) -> None:
        """:meth:`prime_checkpointed`'s tail onto private states, installed at the end."""
        with self._lock:
            seeds = [self._cells[(defn.key, store)] for defn in defns]
        states = [copy.deepcopy(cell.state) for cell in seeds]
        marks = [cell.observed_seq for cell in seeds]
        started = list(marks)
        for event in events:
            seq = self._seq_of(event)
            for i, folder in enumerate(owned):
                if seq <= marks[i]:
                    continue
                states[i] = folder(states[i], event)
                marks[i] = seq
        # Install only over the cell this pass started from, still at the seq it started
        # at. A ``drive`` that landed on this store while the tail folded outside the
        # lock has advanced that cell (``_fold_one`` edits the cell object), and
        # overwriting it would move its watermark backwards and drop an event already
        # announced; the shared path's watermark check would have kept it instead.
        with self._lock:
            for defn, seed, begun, state, mark in zip(defns, seeds, started, states, marks):
                if self._cells.get((defn.key, store)) is seed and seed.observed_seq == begun:
                    self._cells[(defn.key, store)] = _Cell(state, mark)

    def savepoints(
        self,
        store: str,
        identity: Mapping[str, Any],
        *,
        witness: Mapping[str, Any] | None = None,
    ) -> list[Savepoint]:
        """A savepoint per registered unit for *store*, ready to hand to a store.

        Reads the cells and builds the payloads; it does NOT write. When to spend a
        write is the client's call and deliberately not the kernel's: the crew log
        writes only past a threshold of entries advanced, and a kernel that wrote on
        every drive would make that client write a file per entry -- the exact cost
        its savepoints exist to remove. A unit that has folded nothing is skipped,
        since a savepoint at the empty watermark saves no replay.

        *witness* is the client's evidence about the log the state was folded from,
        stamped VERBATIM onto every savepoint built here and interpreted no more than
        the identity is. It is a parameter rather than something the kernel derives
        because only the client can read its own log, and only the client can read it
        at the one moment the evidence is worth anything -- before its fold consumed
        the bytes. Omitted leaves the witness empty, which is a savepoint that carries
        no evidence, and what that is worth is the reading client's call.

        Each savepoint carries its own ``watermark``, so a client whose witness
        certifies ONE boundary has to compare the two itself and skip a unit standing
        somewhere else: the kernel cannot do it, since it cannot read what a boundary
        means inside a mapping it does not interpret.
        """
        stamped = dict(witness) if witness else {}
        with self._lock:
            out: list[Savepoint] = []
            for key, defn in self._defns.items():
                cell = self._cells.get((key, store))
                if cell is None or cell.observed_seq <= EMPTY_WATERMARK:
                    continue
                out.append(
                    Savepoint(
                        key=key,
                        state_version=defn.state_version,
                        watermark=cell.observed_seq,
                        state=cell.state,
                        identity=dict(identity),
                        witness=dict(stamped),
                    )
                )
            return out

    # ---- drive ------------------------------------------------------------
    def drive(self, store: str, event: Any) -> None:
        """Fold ONE new event through every unit, emitting on real change.

        A unit or store seen for the first time folds lazily from ``init()``
        over just this event; callers that need a full history must
        ``prime`` first (a client with a log on disk does at load).
        """
        self._fold_one(store, event, emit=True)

    def _fold_one(self, store: str, event: Any, *, emit: bool) -> None:
        """Fold one event through every unit, announcing the changes only when *emit*.

        The fold is identical either way, and that is the point: a live event and a
        replayed one reach a cell through this one function over the same bytes, so
        there is no second folding path that could disagree with it about them.

        *emit* decides only whether those changes are announced. An event being new to
        a cell does not make it news to a client: replaying a log the client's store
        already holds reaches the value it already has by a route it need not hear
        about, and a callback is a client's egress rather than a bookkeeping hook.
        """
        # Read the seq BEFORE taking the lock: it is a pure read of the payload,
        # and the reader belongs to the client, so there is no reason to run it
        # while holding a lock the client's callback may also contend for.
        seq = self._seq_of(event)
        with self._lock:
            defns = list(self._defns.items())
            on_change = self._on_change if emit else None
            fired: list[tuple[str, dict, int]] = []
            for key, defn in defns:
                cell = self._cells.get((key, store))
                if cell is None:
                    cell = _Cell(defn.init(), EMPTY_WATERMARK)
                    self._cells[(key, store)] = cell
                if seq <= cell.observed_seq:
                    # Replay / stale event: already folded.
                    continue
                new_state = defn.apply(cell.state, event)
                cell.observed_seq = seq
                if new_state is cell.state:
                    continue
                cell.state = new_state
                fired.append((key, defn.view(new_state), seq))

        # Emit outside the lock: view() is done, and the callback may re-enter.
        if on_change is not None:
            for key, view, fired_seq in fired:
                on_change(store, key, view, fired_seq)

    # ---- observed ---------------------------------------------------------
    def cells(self, store: str) -> dict[str, tuple[Any, int]]:
        """``{key: (state, watermark)}`` for every registered unit of *store*.

        The read a client needs to carry a fold's POSITION in a record of its own --
        a savepoint it writes itself, a cached bundle it hands to its next call.
        Neither of the other readers answers it: :meth:`snapshot` renders, so the
        bookkeeping a fold needs to continue is gone from what it returns, and
        :meth:`savepoints` is built for a write, so it skips a unit that has folded
        nothing and wraps the rest in an identity a read has no use for.

        A unit with no cell reports ``init()`` at :data:`EMPTY_WATERMARK`, which is
        what it would fold from, so a caller never has to tell absent from empty.

        State comes back AS HELD, not copied. It is the registry's object, and the
        same-reference rule is what makes that safe to hand out: ``apply`` returns a
        new object rather than modifying this one, so a later :meth:`drive` replaces
        the cell's state instead of mutating what a caller kept.
        """
        with self._lock:
            out: dict[str, tuple[Any, int]] = {}
            for key, defn in self._defns.items():
                cell = self._cells.get((key, store))
                if cell is None:
                    out[key] = (defn.init(), EMPTY_WATERMARK)
                else:
                    out[key] = (cell.state, cell.observed_seq)
            return out

    def observed_floor(self, store: str) -> int:
        """The lowest seq EVERY registered unit has already folded for *store*.

        :data:`EMPTY_WATERMARK` when no unit is registered, or when any registered
        unit has no cell yet: such a cell folds from ``init()`` over whatever it is
        first driven with, so a caller must :meth:`prime` it rather than drive a
        range at it, and a floor would invite exactly that. A client that primes at
        load therefore has a cell missing only before its first read.
        """
        with self._lock:
            floor: int | None = None
            for key in self._defns:
                cell = self._cells.get((key, store))
                if cell is None:
                    return EMPTY_WATERMARK
                if floor is None or cell.observed_seq < floor:
                    floor = cell.observed_seq
            return EMPTY_WATERMARK if floor is None else floor

    # ---- snapshot ---------------------------------------------------------
    def snapshot(self, store: str) -> dict:
        """{"asOfSeq": last_seq, "values": {key: view}} for one store."""
        with self._lock:
            defns = list(self._defns.items())
            values: dict[str, dict] = {}
            as_of = EMPTY_WATERMARK
            for key, defn in defns:
                cell = self._cells.get((key, store))
                if cell is None:
                    values[key] = defn.view(defn.init())
                    continue
                values[key] = defn.view(cell.state)
                if cell.observed_seq > as_of:
                    as_of = cell.observed_seq
            return {"asOfSeq": as_of, "values": values}

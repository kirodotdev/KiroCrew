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
imports no domain's event type). Two readers cover the shapes in this codebase:
:func:`mapping_seq` for a carrier subscripted like a mapping, which is the member
log's ``Event`` TypedDict and the default, and :func:`attribute_seq` for one
holding it as a field, which is the crew log's ``Entry`` dataclass.

``mapping_seq`` is the default rather than a required argument because it is what
every present caller of a TypedDict envelope wants, and a required argument would
make each construction site restate it. A client whose carrier is shaped otherwise
passes its own reader and the kernel needs no change.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Any

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
        """
        with self._lock:
            defns = list(self._defns.values())
            states = [defn.init() for defn in defns]
            last_seq = -1
            for ev in events:
                last_seq = self._seq_of(ev)
                for i, defn in enumerate(defns):
                    states[i] = defn.apply(states[i], ev)
            for defn, state in zip(defns, states):
                self._cells[(defn.key, store)] = _Cell(state, last_seq)

    # ---- drive ------------------------------------------------------------
    def drive(self, store: str, event: Any) -> None:
        """Fold ONE new event through every unit, emitting on real change.

        A unit or store seen for the first time folds lazily from ``init()``
        over just this event; callers that need a full history must
        ``prime`` first (a client with a log on disk does at load).
        """
        # Read the seq BEFORE taking the lock: it is a pure read of the payload,
        # and the reader belongs to the client, so there is no reason to run it
        # while holding a lock the client's callback may also contend for.
        seq = self._seq_of(event)
        with self._lock:
            defns = list(self._defns.items())
            on_change = self._on_change
            fired: list[tuple[str, dict, int]] = []
            for key, defn in defns:
                cell = self._cells.get((key, store))
                if cell is None:
                    cell = _Cell(defn.init(), -1)
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
    def observed_floor(self, store: str) -> int:
        """The lowest seq EVERY registered unit has already folded for *store*.

        ``-1`` when any registered unit has no cell yet: such a cell folds from
        ``init()`` over whatever it is first driven with, so a caller must
        :meth:`prime` it rather than drive a range at it, and a floor would
        invite exactly that. A client that primes at load therefore has a cell
        missing only before its first read.
        """
        with self._lock:
            floor: int | None = None
            for key in self._defns:
                cell = self._cells.get((key, store))
                if cell is None:
                    return -1
                if floor is None or cell.observed_seq < floor:
                    floor = cell.observed_seq
            return -1 if floor is None else floor

    # ---- snapshot ---------------------------------------------------------
    def snapshot(self, store: str) -> dict:
        """{"asOfSeq": last_seq, "values": {key: view}} for one store."""
        with self._lock:
            defns = list(self._defns.items())
            values: dict[str, dict] = {}
            as_of = -1
            for key, defn in defns:
                cell = self._cells.get((key, store))
                if cell is None:
                    values[key] = defn.view(defn.init())
                    continue
                values[key] = defn.view(cell.state)
                if cell.observed_seq > as_of:
                    as_of = cell.observed_seq
            return {"asOfSeq": as_of, "values": values}

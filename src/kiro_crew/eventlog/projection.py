"""Projection registry: fold a member's events into named derived views.

A projection is a small pure fold. Each unit owns a ``key``, a
``state_version``, an ``init()`` that returns fresh empty state, an
``apply(state, event) -> state`` and a ``view(state) -> dict``.

The contract that makes change-detection cheap: ``apply`` MUST return the SAME
object (by identity) when the event does not affect the unit. The registry
compares by ``is``; an unchanged identity emits no change callback, a new
object does.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from kiro_crew.eventlog.types import Event


@runtime_checkable
class ProjectionDefinition(Protocol):
    key: str
    state_version: int

    def init(self) -> Any: ...
    def apply(self, state: Any, event: Event) -> Any: ...
    def view(self, state: Any) -> dict: ...


# on_change(slug, key, view, seq)
OnChange = Callable[[str, str, dict, int], None]


class _Cell:
    """Per-(key, slug) folded state and how far it has observed."""

    __slots__ = ("state", "observed_seq")

    def __init__(self, state: Any, observed_seq: int) -> None:
        self.state = state
        self.observed_seq = observed_seq


class ProjectionRegistry:
    def __init__(self) -> None:
        self._defns: dict[str, ProjectionDefinition] = {}
        self._cells: dict[tuple[str, str], _Cell] = {}
        self._on_change: OnChange | None = None
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
    def prime(self, slug: str, events: list[Event]) -> None:
        """Fold all of a slug's events from ``init()``, no change callbacks."""
        with self._lock:
            defns = list(self._defns.values())
            last_seq = events[-1]["seq"] if events else -1
            for defn in defns:
                state = defn.init()
                for ev in events:
                    state = defn.apply(state, ev)
                self._cells[(defn.key, slug)] = _Cell(state, last_seq)

    # ---- drive ------------------------------------------------------------
    def drive(self, slug: str, event: Event) -> None:
        """Fold ONE new event through every unit, emitting on real change.

        A unit or slug seen for the first time folds lazily from ``init()``
        over just this event; callers that need a full history must
        ``prime`` first (the service does at load).
        """
        with self._lock:
            defns = list(self._defns.items())
            on_change = self._on_change
            fired: list[tuple[str, dict, int]] = []
            for key, defn in defns:
                cell = self._cells.get((key, slug))
                if cell is None:
                    cell = _Cell(defn.init(), -1)
                    self._cells[(key, slug)] = cell
                if event["seq"] <= cell.observed_seq:
                    # Replay / stale event: already folded.
                    continue
                new_state = defn.apply(cell.state, event)
                cell.observed_seq = event["seq"]
                if new_state is cell.state:
                    continue
                cell.state = new_state
                fired.append((key, defn.view(new_state), event["seq"]))

        # Emit outside the lock: view() is done, and the callback may re-enter.
        if on_change is not None:
            for key, view, seq in fired:
                on_change(slug, key, view, seq)

    # ---- snapshot ---------------------------------------------------------
    def snapshot(self, slug: str) -> dict:
        """{"asOfSeq": last_seq, "values": {key: view}} for one slug."""
        with self._lock:
            defns = list(self._defns.items())
            values: dict[str, dict] = {}
            as_of = -1
            for key, defn in defns:
                cell = self._cells.get((key, slug))
                if cell is None:
                    values[key] = defn.view(defn.init())
                    continue
                values[key] = defn.view(cell.state)
                if cell.observed_seq > as_of:
                    as_of = cell.observed_seq
            return {"asOfSeq": as_of, "values": values}

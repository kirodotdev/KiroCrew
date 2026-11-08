"""Which session crew logs the writer must not write side by side: the units of one slot.

The writer's lanes write DIFFERENT units at once (``WriterLimits.lanes``). A work entry's
append and its publish to the slot's ``work-unit-order`` file are two steps
(``session_ledger.note_work_unit_recorded``), and the ``work`` and ``workstreams`` folds
read that file as "which unit holds the newest record". Two units of one slot written
on two lanes at once could publish in the reverse of their append order. So the writer
is handed :func:`hold_key`, and units with one key are written by one lane at a time.

The key is the slot the unit's HEADER records, which is the spelling the order file is
published under (``session_ledger.canonical_slot``). It is learned without reading a
file under the writer's lock: the emitter notes it when it hands over a unit's opening
entry or a work record, and the board relay notes a target it resolved. The writer calls
:func:`hold_key` under its own lock, so it is a plain dict read.

:data:`PROCESS_HOLDS` is the other half: the units and keys a lane or an inline write
holds right now, shared by every writer in the process. A writer rebuilt by
``emit.reset_caches`` sees what a retired writer's stuck lane still holds, so the two
never write one file side by side.

shortcut: a unit with no key (never opened, relayed into or recorded to in this process;
evicted from the bounded map; or a slot or id over :data:`MAX_KEY_CHARS`) is written
alone. Its own order is still safe (``PROCESS_HOLDS`` fences the unit id), only its
publish order against a sibling of its slot is not. Upgrade by reading the header off
the lock if such a unit ever records work beside a sibling.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Final

#: Most units remembered at once, oldest forgotten first. The same population and bound
#: as the board relay's unit -> slot memo (``work_relay._UNIT_SLOTS``), which reads it
#: from here, so the two caches forget a unit at the same rate.
UNIT_SLOTS_LIMIT: Final[int] = 4096

#: Longest slot or unit id remembered. A longer one is not noted, so the unit is held
#: alone; it bounds the memory the map can hold to about UNIT_SLOTS_LIMIT * 2 * this.
MAX_KEY_CHARS: Final[int] = 512

#: unit id -> its header slot, newest last.
_UNIT_SLOTS: "OrderedDict[str, str]" = OrderedDict()
_guard = threading.Lock()


def note_unit_slot(unit: str, slot: str) -> None:
    """Remember that *unit*'s header records *slot*. Ignores empty or over-long values."""
    if not unit or not slot or len(unit) > MAX_KEY_CHARS or len(slot) > MAX_KEY_CHARS:
        return
    with _guard:
        _UNIT_SLOTS[unit] = slot
        _UNIT_SLOTS.move_to_end(unit)
        while len(_UNIT_SLOTS) > UNIT_SLOTS_LIMIT:
            _UNIT_SLOTS.popitem(last=False)


def known(unit: str) -> bool:
    """Whether *unit* already has a key."""
    return unit in _UNIT_SLOTS


def slot_of(unit: str) -> str:
    """*unit*'s noted header slot, or ``""`` when none was noted."""
    return _UNIT_SLOTS.get(unit, "")


def hold_key(unit: str) -> str:
    """The writer's hold key for *unit*: ``slot:<slot>``, or ``""`` (the unit alone).

    No lock: one ``dict.get`` is atomic, and this runs under the writer's lock. The
    prefix keeps a slot key from ever equalling a unit id.
    """
    slot = _UNIT_SLOTS.get(unit)
    return f"slot:{slot}" if slot else ""


def forget(unit: str) -> None:
    """Drop *unit*'s key: its session closed."""
    with _guard:
        _UNIT_SLOTS.pop(unit, None)


def clear() -> None:
    """Forget every key. For ``emit.reset_caches``."""
    with _guard:
        _UNIT_SLOTS.clear()


class Holds:
    """The units a lane or an inline write holds right now, and the keys each holds.

    THE INVARIANT: two units that share a slot are never held at the same time, whatever
    the order of note, forget, freeze and evict. A claim is refused when its unit or
    key is already held, AND when any held unit's key -- as the claimer reads it NOW, not
    as it was when that unit was claimed -- is the key being claimed. So a unit held
    under its bare id before its slot was known (or after it was forgotten) still fences
    its slot the moment the slot is known again. The one case it cannot see is a slot
    known to neither claim: both units are then their own keys. ``key_of`` is the
    claiming writer's key function (frozen-or-live), so a held unit's frozen slot counts.

    Thread-safe; the lock is a leaf, taken inside a writer's own lock. ``key_of`` is
    called under it and must be a pure in-memory lookup.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: held unit -> the names (its id and the keys) it was claimed with.
        self._units: "dict[str, frozenset[str]]" = {}

    def _taken_locked(self, unit: str, names: "frozenset[str]", key_of: "Any") -> bool:
        for held, held_names in self._units.items():
            if names & held_names:
                return True
            if held != unit and (key_of(held) or held) in names:
                return True
        return False

    def taken(self, unit: str, key: str, key_of: "Any") -> bool:
        """Whether claiming *unit* under *key* would break the invariant right now."""
        with self._lock:
            return self._taken_locked(unit, frozenset((unit, key)), key_of)

    def try_claim(self, unit: str, key: str, key_of: "Any") -> bool:
        """Hold *unit* under *key*, or nothing when :meth:`taken` says no."""
        names = frozenset((unit, key))
        with self._lock:
            if self._taken_locked(unit, names, key_of):
                return False
            self._units[unit] = names
            return True

    def release(self, unit: str) -> None:
        """Give back *unit*'s hold."""
        with self._lock:
            self._units.pop(unit, None)

    def held(self) -> "frozenset[str]":
        """The units held right now. For tests and diagnostics."""
        with self._lock:
            return frozenset(self._units)


#: The process's holds, shared by every writer (see the module docstring).
PROCESS_HOLDS: Final[Holds] = Holds()

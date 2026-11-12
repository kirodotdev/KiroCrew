"""The session-tree decisions a slot's older crew logs hold, carried into its newest one.

A slot that moves to a new crew log (a reset, a model switch) has its fold states carried
into the new unit so retention may collect the old ones (:func:`~kiro_crew.crew_log.emit_session._write_carries`).
The session tree is not a fold: it reads each unit's own ``session/adopted``,
``session/released`` and ``session/spawned`` entries, so a unit retention removes takes
those decisions with it. A worker that was adopted or released then reset would snap back to
the parent that created it, and a creator whose only ``session/spawned`` lived in the removed
unit would lose the child it recorded.

This module reads those entries from the older units and builds the same entries for the
newest one, each marked ``carried: true``. They are appended BEFORE the fold carries in one
group commit, so the slot folds, which resume after the carry, never see them twice, and the
tree orders them by the newer log, which is how a later log's decision outranks an older
one's. A fold of ONE unit applies no carry, so a per-unit count of these entries skips the
marked ones (``projection_otel_session_tree``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TreeCarry:
    """One entry to append, and what the in-memory tree folds once it is on disk."""

    entry_type: str
    data: "dict[str, Any]"
    #: The adopting parent slot for an adoption, ``None`` for a release; unused for a spawn.
    parent_slot: "str | None" = None
    #: The child slot a ``session/spawned`` names; empty for a decision.
    child_slot: str = ""


def tree_carries(slot: str, session_id: str, older: "tuple[str, ...]") -> "list[TreeCarry] | None":
    """The tree entries of *older* that *session_id* does not already hold.

    *older* is read in the slot's durable succession (``previous_sid``), not in the order
    given and not by header clock: a clock that stepped backward lists a successor before
    its predecessor, and the predecessor's decision would then be the one carried last.

    ``None`` when a unit could not be read: nothing is then proven, so the caller carries
    nothing and closes nothing. Only the NEWEST decision of the slot is carried, and only
    when *session_id* records none of its own, because a decision it wrote itself is newer
    than anything carried. A spawn is carried unless *session_id* already names that child.

    ``None`` too when the spawns would not all be kept: a reader of one unit keeps its
    newest ``SPAWNED_PER_UNIT_CAP`` (``store.find_spawned_entries``), so an older unit
    holding more than that, or the spawns *session_id* holds plus the carried ones past
    it, would leave some child's creating edge in the older units alone, and closed for
    retention it would go with them. The older units then stay, and their own reads
    still hold what they held. (A *session_id* holding more than the bound already fills
    it, so any spawn to carry is past it; with none to carry, closing loses no spawn.)
    """
    from kiro_crew.crew_log.projection_readers import slot_succession
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.session_tree import (
        SPAWNED_PER_UNIT_CAP,
        edge_record,
        spawned_record,
    )
    from kiro_crew.crew_log.store import find_last_tree_edge, find_spawned_entries, unit_dir_for

    own = unit_dir_for(KIND_SESSION, session_id)
    if own is None:
        return None
    try:
        held_edge = find_last_tree_edge(own) is not None
        held = find_spawned_entries(own, SPAWNED_PER_UNIT_CAP)[0]
        held_children = {
            spawn.slot
            for entry in held
            if (spawn := spawned_record(slot, session_id, entry)) is not None
        }
        edge: "TreeCarry | None" = None
        spawns: "dict[str, TreeCarry]" = {}
        # Units the succession does not list (it reads the slot's headers) keep their given
        # place, first, as the oldest of what is read.
        listed = slot_succession(slot)
        ordered = tuple(unit for unit in older if unit not in listed) + tuple(
            unit for unit in listed if unit in older
        )
        for unit in ordered:
            directory = unit_dir_for(KIND_SESSION, unit)
            if directory is None:
                return None
            entry = find_last_tree_edge(directory)
            found = edge_record(slot, unit, entry)
            if entry is not None and found is not None:
                edge = TreeCarry(entry.type, _carried(entry.data), found.parent_slot)
            # ``hit_cap``, which ``cut`` implies: the scan left this unit's oldest out.
            spawned, _cut, hit_cap = find_spawned_entries(directory, SPAWNED_PER_UNIT_CAP)
            if hit_cap:
                return None
            for spawn_entry in spawned:
                spawn = spawned_record(slot, unit, spawn_entry)
                if spawn is None or spawn.slot in held_children:
                    continue
                spawns.pop(spawn.slot, None)
                spawns[spawn.slot] = TreeCarry(
                    spawn_entry.type, _carried(spawn_entry.data), child_slot=spawn.slot
                )
    except (OSError, ValueError):
        return None
    if len(held) + len(spawns) > SPAWNED_PER_UNIT_CAP:
        return None
    carries = list(spawns.values())
    if edge is not None and not held_edge:
        carries.insert(0, edge)
    return carries


def _carried(data: "dict[str, Any]") -> "dict[str, Any]":
    """*data* marked as carried (``carried: true``), so a per-unit count passes it over.

    The session tree reads a carried entry as its own, which is the point of the carry. A
    fold that counts what a unit itself did does not: the older unit already holds the
    original, and a count of the newest unit's ``session/spawned`` would add every child
    again on each reset (``projection_otel_session_tree``).
    """
    return {**data, "carried": True}

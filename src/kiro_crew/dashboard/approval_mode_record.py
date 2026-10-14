"""The approval mode a chat holds, in the picker's words, and the record of a change.

``approval_mode_seen`` on the metadata line remembers the mode a person last saw a
chat in, so a restore can tell them when a restart dropped it (``restart_resume``).
This module holds the three readers of the live mode that the line writer, the
picker route and the dispatcher share: the mode itself, the before-picture of a
pick, and the dirtying that makes the record follow a pick on an idle chat. It
sits beside the codec's facade rather than among its owners, which import nothing
of each other, and pulls in neither the codec nor the dispatcher.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def live_approval_mode(slot: Any) -> str:
    """The approval mode the slot holds now, in the picker's words.

    ``yolo`` is process-global and never a property of one chat, so a slot under
    the global override still reports its own mode.
    """
    if getattr(slot, "_trust", False):
        return "trust"
    if getattr(slot, "_trust_reads", False):
        return "trust_reads"
    return "normal"


def approval_modes(slots: Iterable[Any]) -> dict[str, str]:
    """``{slot.key: live mode}`` for *slots*: the before-picture of a mode change."""
    return {slot.key: live_approval_mode(slot) for slot in slots}


def mark_changed_approval_modes(slots: Iterable[Any], before: dict[str, str]) -> int:
    """Dirty every slot whose live mode moved off *before*. Returns how many.

    The periodic flush skips a slot that is not dirty, and a mode pick on an idle
    chat appends no row: without this the line keeps the previous mode and the
    next restart's notice compares against what the person did NOT last see.
    Called from the picker route (``apply_approval_mode``). The crewmate seed
    does not call it: re-opening a bound thread is a no-op by contract, and the
    seeded mode reaches the line with the thread's next save.
    """
    changed = 0
    for slot in slots:
        if live_approval_mode(slot) != before.get(slot.key, "normal"):
            slot._dirty = True
            changed += 1
    return changed

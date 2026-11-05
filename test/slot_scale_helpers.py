"""Seed a dashboard state with many chat slots without paying a broadcast per slot.

``get_or_create_slot`` pushes the full slot list after each new slot, so creating
N slots one by one serializes 1 + 2 + ... + N rows: 45,150 ``serialize_slot``
calls for 300 slots. :func:`seed_slots` creates them inside
``suspend_slots_push``, which absorbs every per-slot push and emits one broadcast
when the block exits, so seeding costs one pass over the list.

The slots it makes are plain dashboard chats, titled so that no end-of-turn path
asks a model for a title, with keys ``chat-seed-<i>``. Shared by the slot scale
tests; import it rather than copying it, so one definition of a seeded slot exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def seed_key(i: int) -> str:
    """The key of the ``i``-th seeded slot."""
    return f"chat-seed-{i:05d}"


def seed_slots(state: "DashboardState", n: int) -> "list[_ChatSlot]":
    """Create ``n`` slots on ``state`` with one slot-list broadcast in total.

    Returns the slots in key order.
    """
    slots = []
    with state.suspend_slots_push():
        for i in range(n):
            slot = state.get_or_create_slot(seed_key(i))
            slot._titled = True
            slot.title = f"Seeded session {i}"
            slots.append(slot)
    return slots

"""Give back the saved head of idle chat windows.

A tab that stays open keeps every row it appended in memory, up to the window
cap (``slot_transcript._MAX_SLOT_MESSAGES``), while a restart loads only the
newest ``chat_persistence._WINDOW_ROWS`` of it. This pass makes a long-lived slot
match the restarted one: once a slot has been idle for
:data:`RELEASE_IDLE_SECS`, its saved rows older than the newest window move to
the frozen prefix (``_ChatSlot.release_saved_head``), where every reader of
older rows already finds them for a restored slot. Nothing is written.

Driven by the five-second flush loop (``dashboard_persistence``) at most once
per :data:`RELEASE_INTERVAL_SECS`, on the event loop, after that loop's flush
has finished. LOOP-AFFINE.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: How long a slot goes without a new row before its window head is released.
RELEASE_IDLE_SECS = 600.0
#: The least time between two release passes.
RELEASE_INTERVAL_SECS = 60.0


def release_idle_windows(state: DashboardState, *, now: float) -> int:
    """Release the saved head of every idle slot holding more than a window.

    Skips a slot under construction, closing, with a turn admitted or running,
    or whose window was never loaded. Returns how many rows left memory.
    """
    # circular import: chat_persistence -> state -> dashboard_persistence -> here
    from kiro_crew.dashboard.chat_persistence import _WINDOW_ROWS
    from kiro_crew.dashboard.slot_retention import slot_last_activity

    cutoff = now - RELEASE_IDLE_SECS
    under_construction = getattr(state, "_slots_under_construction", ())
    released = 0
    for key, slot in list(state._slots.items()):
        if (
            len(slot._messages) <= _WINDOW_ROWS
            or key in under_construction
            or slot.window_pending
            or slot.running
            or getattr(slot, "is_closing", False)
        ):
            continue
        last_activity = slot_last_activity(slot)
        if not last_activity or last_activity >= cutoff:
            continue
        released += slot.release_saved_head(_WINDOW_ROWS)
    if released:
        logger.debug("Released %d saved rows from idle chat windows", released)
    return released

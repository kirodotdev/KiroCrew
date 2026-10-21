"""Mark a slot while the gateway prepares an assistant message outside a turn.

Some producers post an assistant row after a background model call that no
turn owns (the warm task brief is one). ``slot.running`` stays false for them,
so without a mark the chat shows no working indicator and the thread looks
hung. :func:`preparing_message` raises the slot's ``preparing`` flag in the
slots frame for exactly the span it wraps.

The flag is display-only. It never sets ``running``, admits nothing and holds
no lock, so sends, queueing, patrol turns and any producer logic that reads
``slot.running`` behave as if it did not exist. It lives in memory only: a
gateway restart drops the work and the flag together, and a reconnecting client
reads the current value from the next slots frame.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def is_preparing(slot: "_ChatSlot") -> bool:
    """Whether any background message is being prepared for *slot* now."""
    return int(getattr(slot, "_preparing_count", 0) or 0) > 0


@contextmanager
def preparing_message(state: "DashboardState", slot: "_ChatSlot") -> Iterator[None]:
    """Show the slot's working indicator for the wrapped span.

    Post the finished row INSIDE the span so a client sees the row before the
    indicator drops. The flag clears on every exit, cancellation included.
    """
    slot._preparing_count = int(getattr(slot, "_preparing_count", 0) or 0) + 1
    state.push_slots_update()
    try:
        yield
    finally:
        slot._preparing_count = max(0, slot._preparing_count - 1)
        state.push_slots_update()

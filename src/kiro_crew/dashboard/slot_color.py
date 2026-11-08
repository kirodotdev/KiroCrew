"""The one write that stores a session's sidebar color.

Both color writers call :func:`write_slot_color`: the sidebar color menu's
``PATCH /api/chat/slots/{slot}/color`` route and the ``session_set_color`` agent
verb (``session_control_parts.session_titles.set_color_target``). Each decides
the final pair of values its own way; this function stores that pair, so a
change to how a color is kept lands in both writers at once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def write_slot_color(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    color_index: int | None,
    color_hex: str | None,
) -> None:
    """Store *slot*'s color as exactly ``color_index`` and ``color_hex``.

    Callers keep the two mutually exclusive: at most one is non-null. The slot is
    marked dirty so the periodic flush persists it, including for a session with
    no messages yet, and the sidebar is told at once.
    """
    slot.color_index = color_index
    slot.color_hex = color_hex
    slot._dirty = True
    state.push_slots_update()

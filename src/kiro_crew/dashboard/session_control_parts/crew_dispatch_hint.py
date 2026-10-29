"""Crew Mode dispatch hint: tell the person where a worker their conductor started went.

A Crew Mode root's workers are not rows of the session list: they are listed in the
Crew tab of the root's side panel. A person watching the conductor's chat sees a tool
line for the dispatch and nothing else, so a worker can start without the person
knowing it exists. After a successful ``session_create`` from a stamped Crew Mode root,
the gateway writes one ``notice`` row into that root's transcript that names the
Crew tab; the dashboard draws it with a button that opens it.

The row is a product notice, not model text: it appears whatever the conductor writes.
A ``notice`` row is never part of a prompt. It lands inside the conductor's open tool
group, so the turn's tool-group walks read past it (``crew_dispatch_notice``). One row
per person turn: a second dispatch
before the next ``user`` row adds nothing, so a conductor that starts five workers in
one turn shows one hint. Only the root's OWN dispatches write it; a worker's
sub-dispatch is already listed under the same root.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from kiro_crew.dashboard.crew_dispatch_notice import (
    CREW_DISPATCH_HINT_KIND,
    is_crew_dispatch_hint,
)

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")

#: English fallback text, for any surface that draws a notice as plain text.
CREW_DISPATCH_HINT_TEXT: Final[str] = (
    "Crew started work. Open the Crew tab in the right-side panel to follow it."
)


def _hinted_this_turn(messages: list[dict[str, Any]]) -> bool:
    """Whether a hint row already follows the newest ``user`` row."""
    for row in reversed(messages):
        if row.get("role") == "user":
            return False
        if is_crew_dispatch_hint(row):
            return True
    return False


async def note_crew_dispatch(state: Any, caller_slot: Any, *, in_crew_tree: bool) -> bool:
    """Write the dispatch hint into *caller_slot*'s transcript; True when one was written.

    Called after a create has committed, so it is best-effort: a failure here is
    logged and never undoes the create.
    """
    if not in_crew_tree or caller_slot is None:
        return False
    from kiro_crew import crew_mode
    from kiro_crew.dashboard.state import append_and_surface, load_window_off_loop

    try:
        if not crew_mode.slot_is_crew_mode_root(caller_slot):
            return False
        await load_window_off_loop(caller_slot)
        key = str(getattr(caller_slot, "key", "") or "")
        if state.get_slot(key) is not caller_slot:
            return False
        if _hinted_this_turn(caller_slot.messages):
            return False
        append_and_surface(
            state,
            caller_slot,
            "notice",
            CREW_DISPATCH_HINT_TEXT,
            "msg msg-info",
            meta={"kind": CREW_DISPATCH_HINT_KIND},
        )
        return True
    except Exception:  # the hint must never undo a committed create
        logger.warning("create_session: Crew Mode dispatch hint not written", exc_info=True)
        return False

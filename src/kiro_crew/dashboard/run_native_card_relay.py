"""The gateway's half of a Crew run's harness-native child cards.

A run on a backend that draws its own children (``ACP_BACKENDS_RUN_NATIVE_CHILD_CARDS``)
reconciles their cards itself (``subagent_manager.native_child_cards``) and hands each
frame to the manager's event callback as one ``run_native_card`` event. The gateway's
``_subagent_event`` passes that event here, and it is never broadcast itself: the frame
it carries is, in the tab the run tree started from (the run's own frames' tab), with no
``batch_id``, since a grandchild belongs to no wave.

Only the four card frames are drawn, and only for a card id that names the run
(``native:<run id>:<child>``). An open card is registered in ``state._native_cards``
with the run's id, which is how its Stop finds the run's own client
(``native_child_stop.run_card_owner``), and its owning tab, which is how the Stop's
ownership check reads it; its end unregisters it. Chunks go to subagent subscribers
only, as a run's own do, and every frame passes the scale coalescer first.
"""

from __future__ import annotations

import time
from typing import Any

#: The frames a run's child card is drawn with.
RELAYED_FRAMES = frozenset({"subagent_spawn", "subagent_tool", "subagent_chunk", "subagent_done"})


def relay_run_native_card(
    state: Any, coalescer: Any, slot: str, run_id: str, extra: dict[str, Any]
) -> None:
    """Draw the card frame *extra* carries for the run *run_id* in the tab *slot*."""
    etype = extra.get("etype")
    frame = extra.get("frame")
    if etype not in RELAYED_FRAMES or not isinstance(frame, dict):
        return
    card_id = frame.get("id")
    if not isinstance(card_id, str) or not card_id.startswith(f"native:{run_id}:"):
        return
    payload = {**frame, "slot": slot}
    if not isinstance(getattr(state, "_native_cards", None), dict):
        state._native_cards = {}
    if etype == "subagent_spawn":
        child = extra.get("child")
        state._native_cards[card_id] = {
            "slot": slot,
            "session_id": child if isinstance(child, str) else "",
            "started": time.time(),
            "run_id": run_id,
        }
    elif etype == "subagent_done":
        state._native_cards.pop(card_id, None)
    if coalescer.handle(etype, payload):
        return
    if etype == "subagent_chunk":
        state.broadcast_ws_subagent_subscribers(etype, payload)
    else:
        state.broadcast_ws(etype, payload)

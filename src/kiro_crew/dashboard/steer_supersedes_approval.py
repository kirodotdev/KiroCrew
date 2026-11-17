"""A steer the owner typed over an open card settles it: Reject, or Cancel for a question.

While a card is open, the turn's runner waits on the card's future, and a steer
is read only at the next inference boundary. This module settles every open card
right after the steer is written: a permission card is rejected and an MCP
elicitation card is cancelled. The tool result then returns, and the steer
reaches the model at the boundary that follows.

Only the owner's own steer settles a card. Steers from a peer, an app, an
automation or a Shared Crew teammate leave the card open.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.agent_sdk.elicitation import ELICITATION_CANCEL
from kiro_crew.dashboard.interaction_coordinator import native_resolution_target

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: The reason a backend that forwards rejection reasons hands the model. Worded
#: like the ``ask_question`` composer outcome (``mcp_tools/control.py``), so every
#: card a chat reply settles tells the agent the same thing.
SUPERSEDED_REJECTION_REASON = (
    "The user replied in chat instead of answering this approval. Their message "
    "follows as the next user message — read it before continuing."
)

#: The SEL outcome for a card a steer answered, next to Stop's ``rejected_on_stop``.
SUPERSEDED_SEL_OUTCOME = "rejected_by_steer"


def reject_approvals_superseded_by_steer(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Settle every card still open on *slot*.

    A permission card is rejected. An MCP elicitation card is CANCELLED, MCP's
    "dismissed without a choice": the person moved past the question rather than
    refusing the server. Its runner stamps the row, broadcasts
    ``elicitation_resolved`` and writes the SEL record itself, so only the future
    is settled here.

    Called after the steer has been written into the turn, never before: the
    settlement wakes the runner, and the steer must already be held by the
    backend for the boundary that settlement reaches. Nothing here awaits, so
    no answer can land between reading a future as pending and settling it.
    """
    # Imported here: chat_handlers imports this module through chat_delivery.
    from kiro_crew.dashboard.chat_handlers import (
        _pending_elicitation_ids,
        _settle_approval_rejected,
    )

    settled = False
    pending_questions = _pending_elicitation_ids(slot)
    for request_id, fut in list(slot._approval_futures.items()):
        if fut.done():
            continue
        # Read while still pending: names the row the frame below settles.
        resolved_mid = slot.approval_instance(request_id) or ""
        if request_id in pending_questions:
            fut.set_result(ELICITATION_CANCEL)
            settled = True
            continue
        # Kept before the future settles, so the runner the settlement wakes finds it.
        slot.set_rejection_reason(request_id, fut, SUPERSEDED_REJECTION_REASON)
        try:
            _settle_approval_rejected(slot, request_id, fut, SUPERSEDED_SEL_OUTCOME)
        except Exception:  # noqa: BLE001 - the audit line must not undo the decision
            logger.warning("Settling a steer-superseded approval failed", exc_info=True)
        settled = True
        # The card is otherwise drawn as pending until the turn ends, and its
        # buttons answer 404 because the future is gone.
        state.broadcast_ws(
            "approval_resolved",
            {
                "id": request_id,
                "approved": False,
                "slot": slot.key,
                **native_resolution_target(resolved_mid),
            },
        )
    if settled:
        # The runner skips its own Board push for a row already resolved here.
        state.push_slots_update()

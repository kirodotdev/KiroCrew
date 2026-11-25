"""A stop retires, on every page, the approval cards it rejected in an engine chat.

A stop rejects each approval the turn is waiting on (``chat_handlers._reject_pending_approvals``):
the future resolves ``rejected``, the permission row is marked resolved and the
audit line says ``rejected_on_stop``. That settles the request, and nothing can be
allowed through it afterwards. It does not tell a page: the runner's own
``approval_resolved`` frame is sent only when the runner is the one that marks the
row, and here the stop marked it first. The composer's Stop button clears the
cards of the tab that pressed it, so only that tab notices; a stop from anywhere
else leaves every page drawing the card as waiting, with buttons that answer
``already_resolved``.

In a chat whose sub-agents run in the KAS engine (the ``kas_subagents`` preview) that
"anywhere else" is the sub-agent chip itself: Stop all ends the turn there, and a
sub-agent's call may be waiting on a card. So for such a chat the stop sends the
frame itself, as the two sibling settlements do (``steer_supersedes_approval``,
``native_child_stop``): one ``approval_resolved`` with ``approved: false`` per
permission card it rejected. An MCP elicitation card is left to its runner, which
answers ``cancel``, stamps the row and retires it. Any other chat, and every chat
registered with the switch off, is left exactly as it was. Which chat this is, is
what its session registered: turning the switch off does not take it from a chat
opened with it on, whose sub-agents still run in the engine.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def waiting_permission_cards(state: Any, slot: Any) -> list[tuple[str, str]]:
    """Each permission card an engine chat's *slot* still waits on, as ``(request id, row mid)``.

    Empty for a chat whose sub-agents do not run in the engine
    (``engine_subagent_cards.session_runs_engine_children``), so nothing is sent for
    it. Read BEFORE the stop rejects the cards: the mid names the row the frame
    retires and is read off the pending row. Elicitation cards are left out. Never
    raises: a slot that cannot be read has no card to retire, and the stop goes on.
    """
    # Imported here: chat_handlers imports this module.
    from kiro_crew.dashboard.chat_handlers import _pending_elicitation_ids
    from kiro_crew.dashboard.engine_subagent_cards import session_runs_engine_children

    try:
        waiting = [
            (request_id, fut)
            for request_id, fut in list(slot._approval_futures.items())
            if not fut.done()
        ]
        if not waiting or not session_runs_engine_children(slot):
            return []
        questions = _pending_elicitation_ids(slot)
        return [
            (str(request_id), slot.approval_instance(request_id) or "")
            for request_id, _fut in waiting
            if request_id not in questions
        ]
    except Exception:  # noqa: BLE001 - reading the cards must never fail a stop
        logger.debug("reading the waiting approval cards failed", exc_info=True)
        return []


def retire_stopped_cards(state: Any, slot: Any, waiting: list[tuple[str, str]]) -> None:
    """Tell every page that the cards in *waiting*, now rejected by a stop, are settled.

    Sent only for a card whose future the stop did settle. Never raises: the request
    is settled whatever a page shows, and a failed frame must not undo the stop.
    """
    if not waiting:
        return
    from kiro_crew.dashboard.interaction_coordinator import native_resolution_target

    try:
        retired = False
        for request_id, mid in waiting:
            fut = slot._approval_futures.get(request_id)
            if fut is not None and not fut.done():
                continue
            state.broadcast_ws(
                "approval_resolved",
                {
                    "id": request_id,
                    "approved": False,
                    "slot": slot.key,
                    **native_resolution_target(mid),
                },
            )
            retired = True
        if retired:
            # The runner skips its own Board push for a row already resolved here.
            state.push_slots_update()
    except Exception:  # noqa: BLE001 - the request is settled whatever a page shows
        logger.warning("retiring a stopped turn's approval cards failed", exc_info=True)


__all__ = ["retire_stopped_cards", "waiting_permission_cards"]

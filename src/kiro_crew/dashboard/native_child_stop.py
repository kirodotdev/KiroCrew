"""A native child card's Stop, sent to the harness when it can stop that one child.

A native card (``native:<id>``, ``chat_turn/native_cards``) draws a child that runs
inside the parent's harness process, so Crew holds no run of its own to cancel. The
client of the slot's running turn decides whether the harness can stop this one child
(``cancel_native_child``: its backend, the child's ``capabilities.cancel`` grant and
its state) and sends the request. The card then stays open, and the end the harness
reports closes it, drawn as stopped (:data:`STOP_REQUESTED` on the child's tracker
record). Any other card the caller closes itself.

A child stopped while it waits on a person leaves that card open: the harness withdraws
the request (``$/cancel_request``), which the turn, waiting on the card, never reads.
So the Stop settles each card the child left waiting, as the chat's Stop settles its
own: rejected, audited ``rejected_on_stop``, marked resolved and retired on every
window. A permission is rejected for that call alone, so the turn's later requests,
another child's included, are still put to the person.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The tracker-record mark of a child a person asked the harness to stop. Its
#: ``cancelled`` end is drawn as stopped (``native_cards._native_subagent_sync``).
STOP_REQUESTED = "stop_requested"


async def harness_stops_native_child(state: Any, card_info: dict[str, Any]) -> bool:
    """Whether the harness was asked to stop the child the card *card_info* draws.

    False, with nothing sent, when the card's slot has no running turn, the turn's
    client cannot stop a native child, or it declines this one (a Workflow agent, a
    child granted no cancel, one already ended). A failure to ask is False too: the
    caller then closes the card itself, so a Stop never raises. When it was asked, the
    cards the child left waiting are settled (:func:`_settle_child_requests`).
    """
    slot: Any = None
    client: Any = None
    child_sid = str(card_info.get("session_id") or "")
    record: dict[str, Any] | None = None
    sent = False
    try:
        slot = state.get_slot(card_info.get("slot", ""))
        client = getattr(slot, "_acp_client", None)
        cancel = getattr(client, "cancel_native_child", None)
        if inspect.iscoroutinefunction(cancel):
            record = _open_record(slot, child_sid)
            if record is not None:
                # Set before the request goes out: the end it causes can be read
                # while the write is still being awaited.
                record[STOP_REQUESTED] = True
            sent = await cancel(child_sid) is True
    except Exception:  # noqa: BLE001 - a Stop that cannot be sent closes the card
        logger.debug("asking the harness to stop a native child failed", exc_info=True)
        sent = False
    if not sent:
        if record is not None:
            record.pop(STOP_REQUESTED, None)
        return False
    try:
        _settle_child_requests(state, slot, client, child_sid)
    except Exception:  # noqa: BLE001 - the child is stopping whatever its cards say
        logger.warning("settling a stopped native child's open cards failed", exc_info=True)
    return True


def _open_record(slot: Any, child_sid: str) -> dict[str, Any] | None:
    """The slot's tracker record of the child *child_sid* while its card is open."""
    tracker = getattr(slot, "_native_subagent_tracker", None)
    record = tracker.get(child_sid) if isinstance(tracker, dict) else None
    return record if isinstance(record, dict) and not record.get("done") else None


def _settle_child_requests(state: Any, slot: Any, client: Any, child_sid: str) -> None:
    """Settle each card on *slot* the child *child_sid* asked and still waits on.

    The ids come from the client (``native_child_requests``); a card counts only while
    its future is pending. Each id goes in ``_approval_stopped`` first, as the chat's
    Stop marks its own, so the runner the settlement wakes records the host's decision,
    answers an elicitation ``cancel``, and reports no person's answer. Nothing here
    awaits, so no answer can land between reading a card as waiting and settling it.
    """
    requests = getattr(client, "native_child_requests", None)
    if not callable(requests):
        return
    waiting = [
        (request_id, fut)
        for request_id in requests(child_sid)
        if (fut := slot._approval_futures.get(request_id)) is not None and not fut.done()
    ]
    if not waiting:
        return
    # Imported at use, as the steer settlement does: chat_handlers loads most of the
    # dashboard.
    from kiro_crew.dashboard.chat_handlers import (
        _pending_elicitation_ids,
        _settle_approval_rejected,
    )
    from kiro_crew.dashboard.interaction_coordinator import native_resolution_target

    questions = _pending_elicitation_ids(slot)
    for request_id, fut in waiting:
        # Read while still pending: names the row the frame below settles.
        resolved_mid = slot.approval_instance(request_id) or ""
        slot._approval_stopped.add(request_id)
        if request_id in questions:
            # Its runner answers ``cancel``, stamps the row and retires the card.
            _settle_approval_rejected(slot, request_id, fut, "rejected_on_stop")
            continue
        _settle_approval_rejected(slot, request_id, fut, "rejected_on_stop", result="rejected_once")
        # The turn goes on, so the card is retired now, not at the turn's end.
        state.broadcast_ws(
            "approval_resolved",
            {
                "id": request_id,
                "approved": False,
                "slot": slot.key,
                **native_resolution_target(resolved_mid),
            },
        )
    # The runner skips its own Board push for a row already resolved here.
    state.push_slots_update()

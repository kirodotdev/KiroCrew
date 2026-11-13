"""The discard of a session nothing was written in (``api_chat_slot_discard``): the
slot's close plus its History row's delete. The artifact companion chat and the
routed chat's app draft launch each create a session before the user types, and
discard it through this route when the user leaves it untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        DashboardState,
        SlotCloseError,
        _history_key_for,
        close_slot,
        deny_app_slot_access,
        slot_history_key,
        slot_not_found,
    )


async def api_chat_slot_discard(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/discard — close a session nothing was written in,
    and delete its history row.

    The emptiness check runs on the live slot, both before the close and again in
    its pre-pop re-check, so a message that lands while the close awaits is never
    closed. The history row is deleted only when no slot holds the key after the
    close: a send can recreate the key while the close saves history, and that
    replacement owns the transcript. The answer is then the history delete's own.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_for_dashboard_caller
    from kiro_crew.dashboard.handlers.sessions import (
        _claim_history_delete,
        _delete_claimed_session,
    )
    from kiro_crew.dashboard.slot_projection import summary_rows
    from kiro_crew.dashboard.slot_retention import live_loop_slot_keys

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    request_app = str(request.get("app") or "")
    denied = deny_app_slot_access(request_app, slot, name, "slot_discard")
    if denied is not None:
        return denied
    history_key = _history_key_for(name)
    # App-owned slots need close hooks; linked transcripts belong to their channel, cron or workflow.
    if slot._app or slot_history_key(slot) != history_key:
        return web.json_response(
            {"error": "only a dashboard-owned session can be discarded", "code": "not_discardable"},
            status=409,
        )

    def refuse_unless_empty() -> None:
        if slot is None or state.get_slot(name) is not slot:
            raise SlotCloseError(
                "the session was replaced during the close", code="slot_replaced", status=409
            )
        if slot.running:
            raise SlotCloseError(
                "a turn is running on this slot", code="turn_in_flight", status=409
            )
        if summary_rows(slot) or slot.queue_depth:
            raise SlotCloseError("the session is not empty", code="slot_not_empty", status=409)
        # The close would retire a goal loop or monitor, paused ones too, as the idle
        # sweep keeps them; the close fences new arms before it retires.
        looped = live_loop_slot_keys(include_paused=True)
        if looped is None:
            raise SlotCloseError(
                "the auto-nudge service is not running, so a bound loop cannot be ruled out",
                code="automation_unknown",
                status=409,
            )
        if name in looped:
            raise SlotCloseError(
                "a goal loop or monitor is bound to this session",
                code="automation_active",
                status=409,
            )

    try:
        refuse_unless_empty()
        await close_slot(state, slot, name, pre_pop_check=refuse_unless_empty)
    except SlotCloseError as exc:
        if exc.status == 409:
            return web.json_response({"error": exc.message, "code": exc.code}, status=409)
        return web.json_response({"error": exc.message, "code": exc.code}, status=500)

    # Deleting a History row is the owner's call, as on DELETE /api/sessions/{key}, so
    # a non-owner dashboard caller's discard closes the session and keeps its row.
    if await require_owner_for_dashboard_caller(request, "slot_discard") is not None:
        return web.json_response({"ok": True})
    # No await from this check to the claim below, so no slot can take the key between them.
    if state.get_slot(name) is None and state.conversation_log:
        with contextlib.ExitStack() as delete_window:
            claim = _claim_history_delete(delete_window, state, history_key)
            if claim.slot is None:
                # A slot recreated on an archived key opens with an empty window over
                # the saved rows, which the live check above cannot see.
                if await asyncio.to_thread(state.conversation_log.has_messages, history_key):
                    return web.json_response(
                        {"error": "the session is not empty", "code": "slot_not_empty"},
                        status=409,
                    )
                return await _delete_claimed_session(state, history_key, claim, request_app)
    return web.json_response({"ok": True})

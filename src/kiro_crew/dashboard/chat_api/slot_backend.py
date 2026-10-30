"""POST /api/chat/slots/{slot}/backend — the composer's per-chat backend picker.

Phase 2 of docs/request-for-change/rfc-chat-backend-selection.md: the owner's
dropdown in the chat composer calls THIS route. It is owner-only (a pick sends
the chat's transcript to another provider, which is the person's decision) and
delegates the switch itself to ``chat_handlers.switch_slot_backend``, the same
verb the session-control MCP tool runs — the creator fence of that surface does
not apply here, the dashboard owner is the person the RFC assigns the pick to.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers._shared_request_body import read_bounded_json
from kiro_crew.dashboard.relay_archive import relay_archive_refusal
from kiro_crew.dashboard.session_control import SessionControlError
from kiro_crew.dashboard.slot_ownership import deny_app_slot_access, slot_not_found


async def api_chat_slot_backend(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/backend — set the slot's backend pick.

    Body ``{"backend": "claude"}`` picks Claude for this chat's next turns;
    ``""`` clears the pick back to the gateway default. The pick is validated
    and stored by ``switch_slot_backend``; the swap itself happens between
    turns. Refusals (unknown/not-installed backend, turn in flight, member
    session, ...) come back as the verb's ``SessionControlError`` status and
    code, rendered verbatim like the session-control routes render them.
    """
    denied = await require_owner_dashboard_request(request, "chat.slot.backend.set")
    if denied is not None:
        return denied
    state = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_backend")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    pick = body.get("backend", "")
    if not isinstance(pick, str):
        return web.json_response(
            {"error": "backend must be a string", "code": "invalid_backend"}, status=400
        )
    refusal = relay_archive_refusal(slot)
    # Deferred: chat_handlers imports this module for the route re-export, so a
    # module-scope import of the verb would be a cycle (same pattern as the
    # session-control verbs).
    from kiro_crew.dashboard.chat_handlers import switch_slot_backend
    if refusal is not None:
        return refusal
    try:
        result: dict[str, Any] = await switch_slot_backend(
            state,
            slot,
            name,
            pick,
            still_ours=lambda: state._slots.get(name) is slot,
            requested_by="",
        )
    except SessionControlError as exc:
        return web.json_response({"error": exc.message, "code": exc.code}, status=exc.status)
    state.push_slots_update()
    return web.json_response(result)

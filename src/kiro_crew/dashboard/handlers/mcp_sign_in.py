"""POST /api/chat/slots/{slot}/mcp/sign-in: start an MCP sign-in from the chat's MCP menu.

On KAS an unsigned remote MCP server exposes no tools, so the agent can never
reach it, and a chat offers its Authorize card only when the user's message
names the server. The chat header's MCP menu lists such a server as "Awaiting
authorization"; this route is that row's Sign in action. It starts the sign-in
through the session's own ``begin_mcp_sign_in`` path, so the consent link
becomes the same Authorize card in this chat, one live card per server. A click
also re-offers a server whose card was dismissed.

Starts nothing new for a server that is signed in or unknown to the session, or
on a backend with no such start (kiro-cli sends its own OAuth requests).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.dashboard.chat_turn.mcp_session import _drain_session_init_oauth_requests
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.slot_ownership import deny_app_slot_access, slot_not_found

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: A server name longer than this is not one the session tracks (``NAME_CAP``).
_SERVER_NAME_MAX = 128


async def api_chat_slot_mcp_sign_in(request: web.Request) -> web.Response:
    """Start a sign-in for one MCP server of the slot's live session.

    Body: ``{"server": "<configured server name>"}``. Answers
    ``{"ok": true, "outcome": ...}`` where outcome is ``started`` (an
    Authorize card follows), ``live`` (this server's card is already live),
    ``queued`` (another sign-in holds the engine's one slot; this one starts
    next), ``not_needed`` (nothing to sign in to) or ``unsupported`` (this
    session's backend starts none). 409 ``no_session`` when the slot has no
    live session to sign in through.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "chat.slot_mcp_sign_in")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    server = body.get("server")
    if not isinstance(server, str) or not server.strip() or len(server) > _SERVER_NAME_MAX:
        return web.json_response(
            {"error": "server must be a non-empty server name", "code": "invalid_server"},
            status=400,
        )
    server = server.strip()
    provider = state.sessions.get_provider(effective_session_key(slot))
    if provider is None:
        return web.json_response(
            {"error": "this chat has no live session", "code": "no_session"}, status=409
        )
    request_sign_in = getattr(provider, "request_mcp_sign_in", None)
    if request_sign_in is None:
        return web.json_response({"ok": True, "outcome": "unsupported"})
    outcome = await request_sign_in(server)
    if outcome == "started":
        # Collected between turns: emit it as the chat's Authorize card now.
        # During a turn the list is empty and the dispatch loop yields it.
        await _drain_session_init_oauth_requests(state, slot, provider)
    logger.info("MCP sign-in from the chat menu for slot %s: %s", name, outcome)
    return web.json_response({"ok": True, "outcome": outcome})

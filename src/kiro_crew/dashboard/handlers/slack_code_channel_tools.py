"""``POST /api/slack/code-channel/tool`` -- the gateway leg of the code-channel tools.

Its only caller is :mod:`kiro_crew.mcp_tools.code_channels` in an MCP
subprocess. The route is STRICT (local transport only, no cookie fall-through),
and the session it acts for is the ``X-Session-Key`` header ONLY once the
transport attests it (:func:`kiro_crew.member_memory_auth.session_key_is_attested`:
the kernel-verified unix peer, or the signed per-session token). The code channel
is then derived from that session (:func:`kiro_crew.slack.code_channels.tools.run_tool`);
nothing in the body can name one.

Body: ``{"tool": "<name>", "args": {...}}``. Reply: ``{"ok": bool, "text": str,
"code": str}`` -- ``text`` is what the agent reads back.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

_OPERATION = "slack.code_channel.tool"


def _deny(session: str, error: str, status: int, code: str) -> web.Response:
    from kiro_crew.slack.code_channels.surface import audit

    audit(session or "unknown", _OPERATION, "denied", "transport", error)
    return web.json_response({"ok": False, "error": error, "code": code}, status=status)


async def api_code_channel_tool(request: web.Request) -> web.Response:
    """Run one code-channel tool for the transport-attested calling session."""
    if not (request.get("internal_auth") is True or request.get("peer_verified") is True):
        return _deny("", "local caller required", 403, "not_local_caller")
    session = request.headers.get("X-Session-Key", "").strip()
    if not session:
        return _deny("", "X-Session-Key required", 400, "session_key_required")
    from kiro_crew.member_memory_auth import session_key_is_attested

    if not await asyncio.to_thread(session_key_is_attested, request, session):
        return _deny(
            session, "the calling session could not be verified", 403, "session_not_attested"
        )
    try:
        body = await request.json()
    except Exception:
        return _deny(session, "malformed JSON body", 400, "invalid_body")
    if not isinstance(body, dict):
        return _deny(session, "malformed JSON body", 400, "invalid_body")
    tool = body.get("tool")
    args = body.get("args")
    if not isinstance(tool, str) or (args is not None and not isinstance(args, dict)):
        return _deny(session, "tool and args are required", 400, "invalid_body")
    from kiro_crew.slack.code_channels.tools import run_tool

    try:
        outcome = await run_tool(request.app.get("state"), session, tool, args or {})
    except Exception:
        logger.exception("code channel tool %s failed", tool[:64])
        return web.json_response(
            {
                "ok": False,
                "text": "Error: the code-channel tool failed unexpectedly.",
                "code": "internal",
            },
            status=500,
        )
    return web.json_response({"ok": outcome.ok, "text": outcome.text, "code": outcome.code})

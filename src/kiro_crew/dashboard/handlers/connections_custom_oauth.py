"""Crew-owned sign-in for an owner-added MCP server (the MCP Servers page's Sign in).

``POST /api/connections/mint {server}`` lands here first. With the
``dashboard.feature_previews.crew_oauth`` preview on a bridged chat backend the
gateway runs the sign-in itself and holds the grant under the server's name and
url (:func:`kiro_crew.connections.crew_oauth.custom_key`); otherwise the caller
keeps the harness mint. The mint row the page polls is the same either way.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

from aiohttp import web


def _mark_live_sessions_for_new_bridge(app: web.Application) -> None:
    """After Crew's first grant for an owner server, live sessions re-project MCP.

    The grant is what bridges that server (``crew_oauth._custom_slug``), so a
    session started before it still holds the harness's own entry. Every live
    provider that applies an MCP change at its next turn is marked; any other
    picks the bridge up at its next start. Runs on the loop; never resets a session.
    """
    from kiro_crew.mcp_hot_reload import mark_reinjecting_providers

    sessions = app["state"].sessions
    mark_reinjecting_providers(list(sessions.active_providers()) + list(sessions.warm_providers()))
    fence = getattr(sessions, "mark_mcp_config_changed", None)
    if callable(fence):
        fence()


async def crew_sign_in(
    request: web.Request, configured: dict[str, Any], project_dirs: Any
) -> web.Response | None:
    """Start Crew's sign-in for *configured* (an owner-recorded server), or ``None``.

    ``None`` means Crew does not sign in to this entry and the caller's harness
    mint applies (:func:`kiro_crew.connections.crew_oauth.signs_in_custom`).
    """
    from kiro_crew.connections import crew_oauth

    if not await asyncio.to_thread(crew_oauth.signs_in_custom, configured["entry"]):
        return None
    name = configured["slug"]
    token = await crew_oauth.start_mint_row(
        crew_oauth.custom_key(name, configured["mcp_url"]),
        project_dirs,
        configured,
        functools.partial(_mark_live_sessions_for_new_bridge, request.app),
    )
    return web.json_response({"ok": True, "slug": name, "state": "minting", "token": token})

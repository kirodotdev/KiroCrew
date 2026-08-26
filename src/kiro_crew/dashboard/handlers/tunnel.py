"""Tunnel status handler."""

from __future__ import annotations

import time

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.tunnel.manager import TunnelState


async def api_tunnel_status(request: web.Request) -> web.Response:
    """GET /api/tunnel/status — return current tunnel state."""
    state: DashboardState = request.app["state"]
    mgr = state.tunnel_manager
    if mgr is None:
        return web.json_response({"state": "disabled", "url": "", "error": "", "uptime": 0, "reconnect_attempt": 0})
    status = mgr.status
    uptime = (
        time.time() - status.connected_at
        if (status.connected_at and status.state == TunnelState.CONNECTED)
        else 0
    )
    return web.json_response({
        "state": status.state.value,
        "url": status.url,
        "error": status.error,
        "uptime": round(uptime),
        "reconnect_attempt": status.reconnect_attempt,
    })

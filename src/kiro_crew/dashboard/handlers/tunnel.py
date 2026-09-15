"""Tunnel status handler."""

from __future__ import annotations

import time

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.tunnel import publish_disabled
from kiro_crew.tunnel.manager import TunnelState


async def api_tunnel_status(request: web.Request) -> web.Response:
    """GET /api/tunnel/status — return current tunnel state.

    A ``disabled`` state carries a machine-readable ``reason`` so the caller can
    tell the two ways of being off apart. ``boot_flag`` means this process was
    started with ``--no-tunnel`` and will never publish, whatever
    ``tunnel.enabled`` says in config — without it, an operator looking at a pod
    (whose config can have been flipped back on after the HOME was seeded) sees a
    tunnel that reads enabled while nothing is running. Empty means the ordinary
    case: the tunnel is simply not configured or not started.
    """
    state: DashboardState = request.app["state"]
    mgr = state.tunnel_manager
    if mgr is None:
        return web.json_response({
            "state": "disabled",
            "url": "",
            "error": "",
            "uptime": 0,
            "reconnect_attempt": 0,
            "reason": "boot_flag" if publish_disabled() else "",
        })
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
        "reason": "",
    })

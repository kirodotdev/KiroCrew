"""Tunnel status handler."""

from __future__ import annotations

import asyncio
import time

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.tunnel import publish_disabled
from kiro_crew.tunnel.manager import TunnelState

#: The states a personal Crew that hosts only by default waits in while it cannot
#: host yet, each of which it leaves on its own or at its next start.
_QUIET_STATES = frozenset({TunnelState.STARTING, TunnelState.RECONNECTING, TunnelState.STOPPED})


async def api_tunnel_status(request: web.Request) -> web.Response:
    """GET /api/tunnel/status — return current tunnel state.

    A ``disabled`` state carries a machine-readable ``reason`` so the caller can
    tell the two ways of being off apart. ``boot_flag`` means this process was
    started with ``--no-tunnel`` and will never publish, whatever
    ``tunnel.enabled`` says in config — without it, an operator looking at a pod
    (whose config can have been flipped back on after the HOME was seeded) sees a
    tunnel that reads enabled while nothing is running. Empty means the ordinary
    case: the tunnel is simply not configured or not started.
    ``personal_default`` means a personal Crew that the Kiro relay hosts only by
    default cannot host yet (starting, reconnecting or stopped, with no error or
    an eligibility hold the status marks ``cannot_host_yet``); ``error`` says why,
    and the Overview tile shows nothing for it. A transport fault and an ``error``
    state are never marked, so they show as they do for any Crew.
    """
    state: DashboardState = request.app["state"]
    mgr = state.tunnel_manager
    if mgr is None:
        return web.json_response(
            {
                "state": "disabled",
                "url": "",
                "error": "",
                "uptime": 0,
                "reconnect_attempt": 0,
                "reason": "boot_flag" if publish_disabled() else "",
            }
        )
    status = mgr.status
    uptime = (
        time.time() - status.connected_at
        if (status.connected_at and status.state == TunnelState.CONNECTED)
        else 0
    )
    reason = ""
    waiting = status.cannot_host_yet or not status.error
    if status.state in _QUIET_STATES and waiting and await asyncio.to_thread(_personal_default):
        # A personal Crew that hosts only by default and cannot host yet (its
        # sign-in, its account or organization, or its bind): the dashboard shows
        # nothing for it, and the error keeps the reason. A fault on the way (a
        # timeout, a refused connection, a 5xx) and a final failure (ERROR) are not
        # marked, so they show as for any Crew.
        reason = "personal_default"
    return web.json_response(
        {
            "state": status.state.value,
            "url": status.url,
            "error": status.error,
            "uptime": round(uptime),
            "reconnect_attempt": status.reconnect_attempt,
            "reason": reason,
        }
    )


def _personal_default() -> bool:
    """Whether the Kiro relay hosts this Crew only by default, on a personal sign-in. BLOCKING."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform.context import current_context
    from kiro_crew.remote_control.hosting import personal_by_default

    # An edition's own tunnel provider is not the Kiro relay, so it keeps its errors.
    if not getattr(current_context().tunnel, "kiro_relay", False):
        return False
    return personal_by_default(KiroCrewConfig.load().tunnel)

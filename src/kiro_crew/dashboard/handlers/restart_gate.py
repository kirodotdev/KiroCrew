"""``POST /api/restart/when-idle``: the gateway's own idle check before a restart.

``kirocrew restart --when-idle`` asks it before restarting, so a restart started
from another machine (the Remote Crew "Update" and "Restart" actions run that
command over SSH or SSM) waits for the turns, subagents and workflows that the
gateway's own scheduled update would wait for. The census and the admission
pause are the orchestrator's (``GatewayOrchestrator._restart_gate_when_idle``);
this handler only gates the caller and reports the answer.

A STRICT internal path (``server_internal_paths``): its only caller is the CLI
on this host with the local secret, never a browser. The handler requires the
secret-verified ``internal_auth`` mark, because a strict path without the
header falls through to cookie auth, and re-asserts host locality the way
``/api/update/approve`` does, because a ``local_only=False`` deployment
reclassifies strict paths as mixed.
"""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew.config.loader import launched_as_managed_service
from kiro_crew.dashboard.handlers.updates_step_up import _loopback_peer
from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


async def api_restart_when_idle(request: web.Request) -> web.Response:
    """POST /api/restart/when-idle — ``{idle, turns, background, service_managed}``.

    ``idle: true`` means nothing a restart would interrupt is running, and new
    turns stay paused until the caller's restart lands (or a short hold
    expires). ``idle: false`` reopens admission and carries the counts. The
    optional body ``{"service_managed_only": true}`` says the caller will
    restart the kirocrew service: a gateway the service did not launch then
    answers ``idle: false`` with ``service_managed: false`` without asking the
    gate, so no admission is paused for a restart that will not reach it. Any
    other body is ``400 invalid_body``. ``503 restart_gate_unavailable`` when
    this process runs no orchestrator to ask.
    """
    # Both checks are load-bearing. A loopback request with no X-Internal-Secret
    # falls through to cookie auth on a strict path, so loopback alone would let
    # any dashboard user (a shared-crew member behind a local proxy, say) pause
    # admission on repeat. ``internal_auth`` is set only after the middleware's
    # constant-time match on the secret, which the CLI always sends.
    if request.get("internal_auth") is not True or not _loopback_peer(request):
        return web.json_response(
            {
                "error": "the restart idle check is accepted from the gateway host only",
                "code": "restart_gate_not_local",
            },
            status=403,
        )
    service_only = await _read_service_only(request)
    if service_only is None:
        return web.json_response(
            {"error": "invalid request body", "code": "invalid_body"}, status=400
        )
    service_managed = launched_as_managed_service()
    if service_only and not service_managed:
        # The caller's restart goes to the service, which did not launch this
        # gateway: its idleness is not the caller's question, and pausing
        # admission here would hold it for a restart that never comes.
        return web.json_response(
            {"idle": False, "turns": 0, "background": 0, "service_managed": False}
        )
    state: DashboardState = request.app["state"]
    gate = getattr(state, "_restart_idle_gate", None)
    if gate is None:
        return web.json_response(
            {
                "error": "this gateway cannot check for in-flight work",
                "code": "restart_gate_unavailable",
            },
            status=503,
        )
    idle, turns, background = await gate()
    logger.info("Restart idle check: idle=%s turns=%d background=%d", bool(idle), turns, background)
    return web.json_response(
        {
            "idle": bool(idle),
            "turns": max(0, int(turns)),
            "background": max(0, int(background)),
            # Whether a generated service definition launched this gateway, so
            # the CLI can tell that a service restart would reach THIS gateway.
            "service_managed": service_managed,
        }
    )


async def _read_service_only(request: web.Request) -> bool | None:
    """``service_managed_only`` from the request body, or ``None`` if malformed.

    No body and ``{}`` both mean ``False``.
    """
    if not request.can_read_body:
        return False
    try:
        body = await request.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or set(body) - {"service_managed_only"}:
        return None
    value = body.get("service_managed_only", False)
    return value if isinstance(value, bool) else None

"""Local machine: the local gateway's control routes for this computer's grants.

Granting is not here. A grant is written only after NATIVE consent on this
machine, and that consent runs on the private stdio pipe between the desktop
app's main process and the ``kirocrew connected-computer desktop-host`` child it
spawned (:mod:`.desktop_host`). The gateway serves no route that creates or
widens a grant: nothing that holds this gateway's internal secret or a dashboard
session (an app backend, an agent, a page) can share a folder without the native
confirmation and folder picker (B13).

Turning access OFF never needs the native path -- it only narrows:
``DELETE /api/connected-computers/local/grants/{grant_id}`` and
``POST /api/connected-computers/local/off`` take effect at once, switch on or off.
``GET /api/connected-computers/local/grants`` lists what is shared.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final, Optional

from aiohttp import web

from kiro_crew.connected_computers.flag import local_enabled
from kiro_crew.connected_computers.permission_audit import as_caller

logger = logging.getLogger(__name__)

GRANTS_PATH: Final[str] = "/api/connected-computers/local/grants"
OFF_PATH: Final[str] = "/api/connected-computers/local/off"


def _audit(operation: str, outcome: str, detail: str) -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="connected_computers",
            operation=operation,
            outcome=outcome,
            source="connected_computers",
            resources=detail,
        )
    except Exception:  # noqa: BLE001
        logger.debug("connected computers: audit failed", exc_info=True)


def _refuse(operation: str, status: int, code: str, error: str) -> web.Response:
    _audit(operation, "denied", code)
    return web.json_response({"error": error, "code": code}, status=status)


async def _owner(request: web.Request, operation: str) -> Optional[web.Response]:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, operation)


async def api_local_grants(request: web.Request) -> web.Response:
    from kiro_crew.connected_computers.controller import get_controller

    if (refusal := await _owner(request, "connected_computers.grants")) is not None:
        return refusal
    rows = await asyncio.to_thread(get_controller().status)
    enabled = await asyncio.to_thread(local_enabled)
    return web.json_response({"grants": rows, "enabled": enabled})


async def api_local_grant_revoke(request: web.Request) -> web.Response:
    from kiro_crew.connected_computers.controller import get_controller

    op = "connected_computers.revoke"
    if (refusal := await _owner(request, op)) is not None:
        return refusal
    with as_caller(request.get("user", "dashboard")):
        grant = await get_controller().revoke(request.match_info["grant_id"])
    if grant is None:
        return _refuse(op, 404, "unknown_grant", "no such grant")
    return web.json_response({"revoked": grant.grant_id})


async def api_local_off(request: web.Request) -> web.Response:
    from kiro_crew.connected_computers.controller import get_controller

    op = "connected_computers.off"
    if (refusal := await _owner(request, op)) is not None:
        return refusal
    with as_caller(request.get("user", "dashboard")):
        ended = await get_controller().off()
    return web.json_response({"revoked": [g.grant_id for g in ended]})


def register_routes(app: web.Application) -> None:
    app.router.add_get(GRANTS_PATH, api_local_grants)
    app.router.add_delete(GRANTS_PATH + "/{grant_id}", api_local_grant_revoke)
    app.router.add_post(OFF_PATH, api_local_off)

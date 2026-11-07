"""``/api/remote-control``: the personal Remote Control setting, its notice, one-click off.

``GET`` reports what the setting asks for, whether the Kiro sign-in allows it, the
notice to show when it does not, and whether the relay is running now.

``PUT {"enabled": false}`` is one-click off: it writes an explicit ``false`` (so it
stays off across a restart), answers, and then stops a running relay once the
relay's open requests (this one's answer included) are done, within
:data:`~kiro_crew.remote_control.guard.DRAIN_SECONDS`.
``PUT {"enabled": true}`` is refused with 409 and the notice while the sign-in is
refused, and writes nothing. When the relay was not chosen at boot there is no
relay to start, so turning it on answers ``restart_required: true``; turning it off
never needs a restart.

Both verbs are owner-only.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared_access import require_owner_dashboard_request
from kiro_crew.remote_control import guard as guard_mod
from kiro_crew.remote_control import setting
from kiro_crew.remote_control.defaults import REMOTE_CONTROL_ON_BY_DEFAULT
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_OPERATION = "remote_control"


async def _view() -> dict[str, Any]:
    view = await asyncio.to_thread(setting.read)
    guard = guard_mod.current_guard()
    running = False
    if guard is not None:
        try:
            running = await guard.running()
        except Exception:  # noqa: BLE001 - a status read must not fail the page
            logger.debug("Remote Control running probe failed", exc_info=True)
    return {
        "requested": view.requested,
        "allowed": view.decision.ok,
        "reason": view.decision.reason,
        "notice": view.decision.notice,
        "effective": view.effective,
        "running": running,
        # Whether this gateway can start and stop the relay without a restart.
        "live": guard is not None,
        "default_on": REMOTE_CONTROL_ON_BY_DEFAULT,
    }


async def api_remote_control_get(request: web.Request) -> web.Response:
    """GET /api/remote-control."""
    denied = await require_owner_dashboard_request(request, _OPERATION)
    if denied is not None:
        return denied
    return web.json_response(await _view())


def _audit(request: web.Request, outcome: str, resources: str) -> None:
    sel().log_api_access(
        caller=str(request.get("user") or "dashboard"),
        operation=_OPERATION,
        outcome=outcome,
        source="dashboard",
        resources=resources,
    )


async def api_remote_control_put(request: web.Request) -> web.Response:
    """PUT /api/remote-control ``{"enabled": bool}``."""
    denied = await require_owner_dashboard_request(request, _OPERATION)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool):
        return web.json_response(
            {"error": "enabled must be a boolean", "code": "invalid_field_type"}, status=400
        )

    guard = guard_mod.current_guard()
    stopping = False
    if enabled:
        try:
            await asyncio.to_thread(setting.turn_on)
        except setting.RemoteControlRefused as refused:
            _audit(request, "denied", f"enabled=true reason={refused.decision.reason}")
            return web.json_response(
                {
                    "error": refused.decision.notice,
                    "code": "remote_control_refused",
                    "reason": refused.decision.reason,
                },
                status=409,
            )
    else:
        # The base write can become visible before an overlay write finishes.
        # Reserve the stop before persistence yields, then drain only on success.
        with guard.reserve_stop() if guard is not None else nullcontext():
            await asyncio.to_thread(setting.turn_off)
            if guard is not None:
                guard.stop_after_drain()
                stopping = True
    _audit(request, "allowed", f"enabled={'true' if enabled else 'false'}")

    if guard is not None and enabled:
        await guard.apply()
    result = await _view()
    if stopping:
        # The stop is committed and bounded; the card re-reads the live state.
        result["running"] = False
    result["restart_required"] = bool(enabled and guard is None)
    return web.json_response(result)

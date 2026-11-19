"""``/api/remote-control``: the personal Remote Control setting, its notice, one-click off.

``GET`` reports what the setting asks for, whether this build locks it, whether
the relay is connected now, and (``problem``) why config keeps it from starting.

``PUT {"enabled": false}`` is one-click off: it writes an explicit ``false`` (so it
stays off across a restart), answers, and then stops a running relay once the
relay's open requests (this one's answer included) are done, within
:data:`~kiro_crew.remote_control.guard.DRAIN_SECONDS`. Turning off never needs a
restart. The dashboard cannot turn Remote Control on: ``{"enabled": true}`` is
refused with 400 and writes nothing; that is the terminal command's job
(``kirocrew remote-control on``).

Both verbs are owner-only. ``PUT`` is also local-only: a request the Kiro relay
delivered is refused with 403 (``relay_remote_control_refused``) and audited, even
from the owner, because a Crew turned off over the relay can only be turned on again
at its machine. ``GET`` still answers over the relay, with ``relayed: true``, so the
card shows the state read-only there.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from typing import Any

from aiohttp import web

from kiro_crew.config import live
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.kiro_relay.caller import published_relay_caller
from kiro_crew.remote_control import guard as guard_mod
from kiro_crew.remote_control import setting
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_OPERATION = "remote_control"


async def _view(request: web.Request) -> dict[str, Any]:
    requested = await asyncio.to_thread(setting.requested)
    locked = setting.locked()
    # Why a requested relay refuses to start, so the card never asks for a restart.
    problem = await asyncio.to_thread(setting.start_problem) if requested and not locked else None
    guard = guard_mod.current_guard()
    connected = False
    if guard is not None:
        try:
            connected = await guard.connected()
        except Exception:  # noqa: BLE001 - a status read must not fail the page
            logger.debug("Remote Control connection probe failed", exc_info=True)
    return {
        "requested": requested,
        # This build ignores the setting, so no restart connects (LTS tunnel lock).
        "locked": locked,
        # Connected to the relay, so other devices can reach this Crew. A client
        # that ended in error is not.
        "running": connected,
        **({"problem": problem} if problem else {}),
        # Delivered by the Kiro relay: the card shows the state but no Turn off.
        "relayed": published_relay_caller(request) is not None,
    }


async def api_remote_control_get(request: web.Request) -> web.Response:
    """GET /api/remote-control."""
    denied = await require_owner_dashboard_request(request, _OPERATION)
    if denied is not None:
        return denied
    return web.json_response(await _view(request))


def _audit(request: web.Request, outcome: str, resources: str) -> None:
    sel().log_api_access(
        caller=str(request.get("user") or "dashboard"),
        operation=_OPERATION,
        outcome=outcome,
        source="dashboard",
        resources=resources,
    )


async def api_remote_control_put(request: web.Request) -> web.Response:
    """PUT /api/remote-control ``{"enabled": false}``: one-click off, nothing else."""
    denied = await require_owner_dashboard_request(request, _OPERATION)
    if denied is not None:
        return denied
    if published_relay_caller(request) is not None:
        _audit(request, "denied", "enabled=false reason=relay")
        return web.json_response(
            {
                "error": "Remote Control can only be turned off on the machine running this Crew",
                "code": "relay_remote_control_refused",
            },
            status=403,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if enabled is not False:
        return web.json_response(
            {
                "error": "the dashboard can only turn Remote Control off: send enabled=false",
                "code": "invalid_field_value",
            },
            status=400,
        )

    guard = guard_mod.current_guard()
    stopping = False
    # The base write can become visible before an overlay write finishes.
    # Reserve the stop before persistence yields, then drain only on success.
    with guard.reserve_stop() if guard is not None else nullcontext():
        await asyncio.to_thread(setting.turn_off)
        if guard is not None:
            await live.watch().refresh_now()
            guard.stop_after_drain()
            stopping = True
    _audit(request, "allowed", "enabled=false")

    if not stopping:
        return web.json_response(await _view(request))
    # The drain stops the relay within DRAIN_SECONDS, and this answer may be riding
    # it, so it waits on nothing the relay carries: the stored off, and the card
    # re-reads the live state with GET.
    return web.json_response(
        {
            "requested": await asyncio.to_thread(setting.requested),
            "locked": setting.locked(),
            # The stop is committed and bounded.
            "running": False,
            "relayed": False,
        }
    )

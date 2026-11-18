"""Remote gateway: the dashboard's view of enrolled computers, and its two removals.

* ``GET /api/connected-computers/devices`` -- each enrolled computer, whether it
  is connected now, and the root it reported; refused while the switch is off.
* ``POST /api/connected-computers/devices/{device_id}/revoke`` -- refuse its
  credentials and end its socket now; its chats stay bound to it (B07).
* ``POST /api/connected-computers/devices/{device_id}/remove`` -- the same, and the
  computer is listed as removed. Its files and the computer itself are untouched
  (B12).

Owner only. Revoke and remove narrow access, so they work with the switch off.
Nothing here can enroll a computer or widen a grant: that happens only from the
computer itself.

Every gateway registers these routes, preview on or off, so this module stays
import-light: at module level it imports only :mod:`.flag`. The broker, the
registry, the vault and the connector socket (:mod:`.ws_route`) are imported
inside the handlers, on the first request that needs them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from aiohttp import web

from kiro_crew.connected_computers.flag import live_preview_on, remote_refusal

logger = logging.getLogger(__name__)

DEVICES_PATH: Final[str] = "/api/connected-computers/devices"


def _registry() -> Any:
    from kiro_crew.connected_computers.devices import DeviceRegistry
    from kiro_crew.connected_computers.vault_doc import open_vault

    return DeviceRegistry(open_vault())


def device_rows() -> list[dict[str, Any]]:
    """Every enrolled computer with its live state, for the picker and the list."""
    from kiro_crew.connected_computers.broker import get_broker

    broker = get_broker()
    rows = []
    records = _registry().list()
    labels: dict[str, int] = {}
    for rec in records:
        labels[rec.label] = labels.get(rec.label, 0) + 1
    for rec in records:
        conn = broker.live(rec.device_id)
        row = rec.public()
        row["connected"] = conn is not None
        row["root"] = conn.root if conn is not None else ""
        row["confinement"] = conn.confinement if conn is not None else ""
        # Labels may repeat (B09): the id then tells them apart.
        row["label_is_shared"] = labels[rec.label] > 1
        rows.append(row)
    return rows


async def _owner(request: web.Request, operation: str) -> Any:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, operation)


async def api_devices(request: web.Request) -> web.Response:
    if (refusal := await _owner(request, "connected_computers.devices")) is not None:
        return refusal
    # Off the loop: without a live config snapshot the switch is read from disk.
    off = await asyncio.to_thread(remote_refusal)
    if off is not None:
        return web.json_response({"error": off, "code": "connected_computers_disabled"}, status=403)
    return web.json_response({"devices": await asyncio.to_thread(device_rows)})


async def _end(request: web.Request, *, remove: bool) -> web.Response:
    from kiro_crew.connected_computers.broker import get_broker

    op = "connected_computers.remove" if remove else "connected_computers.revoke"
    if (refusal := await _owner(request, op)) is not None:
        return refusal
    record = await end_device(
        _registry(), get_broker(), request.match_info["device_id"], remove=remove
    )
    if record is None:
        return web.json_response(
            {"error": "no such computer", "code": "unknown_device"}, status=404
        )
    _audit_end(request, record.device_id, remove=remove)
    return web.json_response({"device": record.public()})


def _audit_end(request: web.Request, device_id: str, *, remove: bool) -> None:
    """Record a Disconnect or Remove in the security event log, after the write.

    Both withdraw a computer's credential, so the log must show it. A failed
    audit never undoes or refuses the withdrawal, which has already happened.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="connected_computer_remove" if remove else "connected_computer_revoke",
            outcome="ok",
            source="connected_computers",
            resources=device_id,
        )
    except Exception:  # noqa: BLE001 -- the credential is already withdrawn
        logger.warning("connected computers: could not audit ending %s", device_id, exc_info=True)


async def end_device(registry: Any, broker: Any, device_id: str, *, remove: bool) -> Any:
    """Disconnect or Remove ``device_id``: the record first, then its live connection.

    Both run inside the broker's admission fence for the device, so a connector
    whose credential was verified just before cannot be admitted after this
    write without this call seeing and ending its connection
    (:meth:`.broker.ConnectionBroker.admission`). None when there is no such record.
    """
    action = registry.remove if remove else registry.revoke
    async with broker.admission(device_id):
        record = await asyncio.to_thread(action, device_id)
        if record is None:
            return None
        # The authenticated refusal tells the computer this was an explicit end.
        await broker.disconnect(
            device_id,
            "removed" if remove else "access revoked",
            refusal=record.status,
        )
    return record


async def api_device_revoke(request: web.Request) -> web.Response:
    return await _end(request, remove=False)


async def api_device_remove(request: web.Request) -> web.Response:
    return await _end(request, remove=True)


def register_routes(app: web.Application) -> None:
    app.router.add_get(DEVICES_PATH, api_devices)
    app.router.add_post(DEVICES_PATH + "/{device_id}/revoke", api_device_revoke)
    app.router.add_post(DEVICES_PATH + "/{device_id}/remove", api_device_remove)


#: The methods the connector socket answers while the preview is on: what
#: ``add_get`` would route (GET, and HEAD with it).
CONNECT_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})


async def api_connector_connect(request: web.Request) -> web.StreamResponse:
    """Any method on ``/api/connected-computers/connect``.

    With the preview off this is the router's own answer to a path no route
    serves: the token gate has already run, because the path's token-auth
    exemption reads the same live switch (``token_auth_paths._exact_method_bypass``),
    and this raises the 404 an unmatched path raises. The route takes every
    method so that a POST is that 404 too, not a 405. The routes are frozen at
    boot, so they cannot come and go with the switch.

    With it on, GET and HEAD reach :func:`.ws_route.api_connector_connect`,
    imported on the first connect so a gateway that is never dialled never loads
    the socket, the broker or the credential code; any other method gets the
    router's 405.
    """
    if not live_preview_on():
        raise web.HTTPNotFound()
    if request.method not in CONNECT_METHODS:
        raise web.HTTPMethodNotAllowed(request.method, CONNECT_METHODS)
    from kiro_crew.connected_computers.ws_route import api_connector_connect as connect

    return await connect(request)


def register_all(app: web.Application) -> None:
    """Every connected-computers route: the connector socket and the device list."""
    from kiro_crew.dashboard.token_auth_paths import CONNECTOR_CONNECT_PATH

    app.router.add_route("*", CONNECTOR_CONNECT_PATH, api_connector_connect)
    register_routes(app)

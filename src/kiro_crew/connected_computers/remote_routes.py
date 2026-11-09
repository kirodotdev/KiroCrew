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
computer itself (:mod:`.local_routes`).
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from aiohttp import web

from kiro_crew.connected_computers.flag import remote_refusal

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
    return web.json_response({"device": record.public()})


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


def register_all(app: web.Application) -> None:
    """Every connected-computers route: remote socket, remote list, local consent."""
    from kiro_crew.connected_computers import local_routes, ws_route

    ws_route.register_routes(app)
    register_routes(app)
    local_routes.register_routes(app)

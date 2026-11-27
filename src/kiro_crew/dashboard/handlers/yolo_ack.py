"""``GET`` / ``PUT /api/chat/yolo-ack``: the owner's YOLO confirmation acknowledgement.

The approval-mode picker skips its YOLO confirmation once the owner ticked
*Don't show again*. The browser's copy is per origin and stays out of the
UI-preferences backup, so these routes keep the owner's copy on the gateway in
the ``yolo_ack.json`` keystone (:mod:`kiro_crew.yolo_ack`), which an agent cannot
write. The PUT is its only writer.

Both verbs answer the machine owner only (``is_host_owner_dashboard_request``): a
Shared Crew teammate, an app token, an internal caller and an allow-listed
non-owner get 403 ``dashboard_owner_required`` and keep the per-browser copy. The
body is ``{"acknowledged": bool}`` both ways; ``false`` re-arms the confirmation
in a browser that does not hold its own ``mc-yolo-ack`` copy. Each write (a failed
one included) and each refused call, GET or PUT, is audited to the SEL; a
successful GET is not, and a malformed body writes nothing.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew import yolo_ack
from kiro_crew.dashboard.handlers._shared import _owner_denial_response, read_bounded_json
from kiro_crew.dashboard.handlers.source_providers import is_host_owner_dashboard_request

logger = logging.getLogger(__name__)

_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_INVALID_BODY = "invalid_body"
_CODE_WRITE_FAILED = "yolo_ack_write_failed"
_MAX_JSON_BYTES = 1024
_OP_SET = "chat.yolo_ack.set"
_OP_READ = "chat.yolo_ack.read"


def _audit(request: web.Request, outcome: str, detail: str, operation: str) -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=str(request.get("user") or "anonymous"),
            operation=operation,
            outcome=outcome,
            source="yolo_ack",
            resources=detail,
            critical=outcome != "denied",
        )
    except Exception:  # noqa: BLE001 -- the audit record never changes the outcome
        logger.debug("yolo acknowledgement audit failed", exc_info=True)


async def _owner_refusal(request: web.Request, operation: str) -> web.Response:
    await asyncio.to_thread(_audit, request, "denied", f"app={request.get('app')!r}", operation)
    return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)


async def api_yolo_ack_get(request: web.Request) -> web.Response:
    if not is_host_owner_dashboard_request(request):
        return await _owner_refusal(request, _OP_READ)
    acknowledged = await asyncio.to_thread(yolo_ack.read_acknowledged)
    return web.json_response({"acknowledged": acknowledged})


async def api_yolo_ack_put(request: web.Request) -> web.Response:
    if not is_host_owner_dashboard_request(request):
        return await _owner_refusal(request, _OP_SET)
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_JSON_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    value = body.get("acknowledged") if set(body) == {"acknowledged"} else None
    if not isinstance(value, bool):
        return web.json_response(
            {"error": 'send {"acknowledged": boolean}', "code": _CODE_INVALID_BODY}, status=400
        )
    try:
        await asyncio.to_thread(yolo_ack.write_acknowledged, value)
    except OSError:
        logger.error("could not write the yolo acknowledgement", exc_info=True)
        await asyncio.to_thread(_audit, request, "write_failed", f"acknowledged={value}", _OP_SET)
        return web.json_response(
            {"error": "could not record the acknowledgement", "code": _CODE_WRITE_FAILED},
            status=500,
        )
    await asyncio.to_thread(
        _audit, request, "acknowledged" if value else "rearmed", f"acknowledged={value}", _OP_SET
    )
    return web.json_response({"acknowledged": value})

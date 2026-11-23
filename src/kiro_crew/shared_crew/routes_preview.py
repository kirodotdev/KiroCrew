"""``GET`` / ``PUT /api/shared-crew/preview``: the owner's Shared Crew switch.

Owner-only, with the gate the invite routes use. Registered whether or not the
preview is on, because the owner must be able to turn it on from a Crew that
has it off; a caller who is not the owner is refused 403, never 404.

The body is ``{"enabled": bool, "active": bool, "env_override": bool,
"restart_required": bool}``. ``enabled`` is the stored choice, ``active`` what
this gateway started with, ``env_override`` true while
``KIROCREW_SHARED_CREW_PREVIEW=1`` forces it on, and ``restart_required`` true
when the next start will differ from this one. A PUT body is ``{"enabled":
bool}``. It writes the keystone and changes nothing in the running process.

:func:`audit`, :func:`respond_setting` and :func:`put_setting` are the owner
gate's other side for every preview switch of this shape; the memory provider
switch's routes (:mod:`kiro_crew.memory_provider.routes_preview`) call them too.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import ModuleType

from aiohttp import web

from kiro_crew.shared_crew import preview

logger = logging.getLogger(__name__)

PREVIEW_PATH = "/api/shared-crew/preview"
_CODE_INVALID_BODY = "invalid_body"
_CODE_WRITE_FAILED = "preview_write_failed"
_MAX_JSON_BYTES = 1024


def _error(status: int, code: str) -> web.Response:
    return web.json_response({"error": code, "code": code}, status=status)


def audit(request: web.Request, enabled: bool, outcome: str, feature: str) -> None:
    """Record a switch change as ``<feature>.preview.set`` from ``<feature>_preview``."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=str(request.get("user") or "owner"),
            operation=f"{feature}.preview.set",
            outcome=outcome,
            source=f"{feature}_preview",
            resources=f"enabled={enabled}",
            critical=True,
        )
    except Exception:  # noqa: BLE001 -- the audit record never changes the outcome
        logger.debug("%s preview audit failed", feature.replace("_", " "), exc_info=True)


async def respond_setting(switch: ModuleType) -> web.Response:
    """Answer with *switch*'s ``current_setting()``."""
    setting = await asyncio.to_thread(switch.current_setting)
    return web.json_response(setting.to_dict())


async def put_setting(
    request: web.Request,
    switch: ModuleType,
    audited: Callable[[web.Request, bool, str], None],
    feature: str,
) -> web.Response:
    """Store the owner's ``{"enabled": bool}`` through *switch*, once the owner gate passed."""
    if request.content_length is not None and request.content_length > _MAX_JSON_BYTES:
        return _error(413, _CODE_INVALID_BODY)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- any unreadable body is a bad request
        return _error(400, _CODE_INVALID_BODY)
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool):
        return _error(400, _CODE_INVALID_BODY)
    try:
        await asyncio.to_thread(switch.write_stored, enabled)
    except OSError:
        logger.error(
            "could not write the %s preview switch", feature.replace("_", " "), exc_info=True
        )
        await asyncio.to_thread(audited, request, enabled, "write_failed")
        return _error(500, _CODE_WRITE_FAILED)
    await asyncio.to_thread(audited, request, enabled, "enabled" if enabled else "disabled")
    return await respond_setting(switch)


async def _refusal(request: web.Request) -> web.Response | None:
    # Imported when a request arrives, so registering the route pulls in none
    # of the invite machinery while the preview is off.
    from kiro_crew.shared_crew.routes_invites import _owner_refusal

    return await _owner_refusal(request)


def _audit(request: web.Request, enabled: bool, outcome: str) -> None:
    audit(request, enabled, outcome, "shared_crew")


async def api_get_preview(request: web.Request) -> web.Response:
    refused = await _refusal(request)
    if refused is not None:
        return refused
    return await respond_setting(preview)


async def api_put_preview(request: web.Request) -> web.Response:
    refused = await _refusal(request)
    if refused is not None:
        return refused
    return await put_setting(request, preview, lambda *a: _audit(*a), "shared_crew")


def register(app: web.Application) -> None:
    """Register the preview routes on *app*."""
    app.router.add_get(PREVIEW_PATH, api_get_preview)
    app.router.add_put(PREVIEW_PATH, api_put_preview)

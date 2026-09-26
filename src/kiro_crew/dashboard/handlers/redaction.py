"""Owner-only routes behind the redaction cards: allowed hosts.

``POST /api/redaction/allowed-hosts`` / ``DELETE`` / ``GET`` manage the hosts a
reader allowed long queries for in one workspace (``security.redaction_allow``).
Only these routes write that list, never an agent: an agent able to add a host
could allow the destination it wants to send conversation data to.

A false-positive report is not a route: the card opens a prefilled issue on the
project tracker that names the rule, and the reader reviews it before sending.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.security.redaction_allow import (
    allow_host,
    list_allowed,
    normalize_workspace,
    revoke_host,
    valid_host,
)

logger = logging.getLogger(__name__)

_MAX_BODY = 4096


def _audit(operation: str, outcome: str, request: web.Request, resources: str = "") -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=str(request.get("user") or "unknown"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )
    except Exception:  # pragma: no cover - an audit must not change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _bad(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=400)


async def api_redaction_allowed_hosts(request: web.Request) -> web.Response:
    """``GET``: every workspace's allowed hosts, for Settings -> Security -> Redaction."""
    denied = await require_owner_dashboard_request(request, "redaction.allowed_hosts.list")
    if denied is not None:
        return denied
    return web.json_response({"workspaces": await asyncio.to_thread(list_allowed)})


async def api_redaction_allow_host(request: web.Request) -> web.Response:
    """``POST {slot, host}``: allow long queries on one host in the slot's workspace.

    The workspace is read from the slot on the server, so the rule lands where
    the message the reader is looking at was written.
    """
    denied = await require_owner_dashboard_request(request, "redaction.allowed_hosts.add")
    if denied is not None:
        return denied
    body, error = await read_bounded_json(request, max_bytes=_MAX_BODY)
    if body is None:
        return error or _bad("invalid body", "body_not_object")
    host, slot_key = body.get("host"), body.get("slot")
    if not isinstance(host, str) or not valid_host(host.lower()):
        return _bad("invalid host", "invalid_host")
    if not isinstance(slot_key, str):
        return _bad("invalid slot", "invalid_slot")
    slot = request.app["state"].get_slot(slot_key)
    if slot is None:
        return web.json_response({"error": "slot not found", "code": "slot_not_found"}, status=404)
    workspace = normalize_workspace(slot.workspace)
    if workspace is None:
        return _bad(
            "this conversation's workspace name cannot hold allowed hosts", "unsupported_workspace"
        )
    ok = await asyncio.to_thread(allow_host, workspace, host)
    _audit("redaction.allowed_hosts.add", "allowed" if ok else "refused", request, host.lower())
    if not ok:
        return _bad("the allowed-host list is full", "allow_list_full")
    return web.json_response({"ok": True, "workspace": workspace})


async def api_redaction_revoke_host(request: web.Request) -> web.Response:
    """``DELETE ?workspace=&host=``: revoke one allowed host."""
    denied = await require_owner_dashboard_request(request, "redaction.allowed_hosts.revoke")
    if denied is not None:
        return denied
    host = request.query.get("host", "")
    workspace = request.query.get("workspace")
    if not valid_host(host.lower()):
        return _bad("invalid host", "invalid_host")
    removed = await asyncio.to_thread(revoke_host, workspace, host)
    _audit(
        "redaction.allowed_hosts.revoke", "removed" if removed else "absent", request, host.lower()
    )
    return web.json_response({"ok": True, "removed": removed})

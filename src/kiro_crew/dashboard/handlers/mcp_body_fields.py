"""Dashboard handlers for the MCP message-body fields (``security.mcp_body_fields``).

``GET /api/security/mcp-body-fields`` lists the reviewed defaults, the rows the
owner added, and the fields the tool_input gate recently refused for size.
``POST`` adds one of those refused fields ("Allow this field from now on");
``DELETE`` removes an owner-added row. All three are OWNER-gated, as the
credential-redaction switch is: a Slack-allow-listed non-owner and an app token
are refused. These routes are the only writer of ``mcp_body_fields.json``; no MCP
tool reaches it.

A row can be added only when the gate itself recorded that exact
``(server, tool, field)`` as a size refusal of a call with a trusted identity.
The owner never types a field name, and a request naming anything else is
refused, so a page or an agent cannot get the owner to allow a field the gate
never saw.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.security import mcp_body_fields as store

logger = logging.getLogger(__name__)

_SOURCE = "mcp-body-fields"


def _audit(*, outcome: str, caller: str, detail: str, critical: bool = False) -> bool:
    """One SEL record; returns whether it was written. Never raises."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation=f"mcp_body_fields.{outcome}",
            outcome=outcome,
            source=_SOURCE,
            resources=detail[:200],
            critical=critical,
        )
        return True
    except Exception:
        logger.debug("could not write the body-field audit event", exc_info=True)
        return False


async def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    if is_owner_dashboard_request(request):
        return None
    logger.warning(
        "refused %s: body fields are a dashboard owner action (app=%s)",
        operation,
        request.get("app"),
    )
    await asyncio.to_thread(
        _audit,
        outcome="denied",
        caller=str(request.get("user") or "anonymous"),
        detail=f"{operation}: non-owner caller refused (app={request.get('app')!r})",
    )
    return _owner_denial_response(request, "dashboard owner required", "dashboard_owner_required")


def _listing() -> dict:
    owner = store.owner_rows()
    allowed = {(r["server"], r["tool"], r["field"]) for r in owner}
    allowed.update((r.server, r.tool, r.field) for r in store.DEFAULT_BODY_FIELDS)
    return {
        "defaults": [r.to_dict() for r in store.DEFAULT_BODY_FIELDS],
        "owner": owner,
        "refused": [
            r for r in store.size_refusals() if (r["server"], r["tool"], r["field"]) not in allowed
        ],
    }


async def _row_from(request: web.Request) -> store.BodyField | web.Response:
    """The row a request names: the JSON body of a POST, the query of a DELETE."""
    if request.method == "DELETE":
        body: object = dict(request.query)
    else:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict) or not store.is_valid_row(
        body.get("server"), body.get("tool"), body.get("field")
    ):
        return web.json_response(
            {"error": "server, tool and field are required", "code": "invalid_body"}, status=400
        )
    return store.BodyField(body["server"], body["tool"], body["field"])


async def api_mcp_body_fields_get(request: web.Request) -> web.Response:
    """GET /api/security/mcp-body-fields."""
    denied = await _deny_non_owner(request, "mcp_body_fields.read")
    if denied:
        return denied
    return web.json_response(await asyncio.to_thread(_listing))


async def api_mcp_body_fields_add(request: web.Request) -> web.Response:
    """POST /api/security/mcp-body-fields -- allow one field the gate refused for size."""
    denied = await _deny_non_owner(request, "mcp_body_fields.add")
    if denied:
        return denied
    row = await _row_from(request)
    if isinstance(row, web.Response):
        return row
    subject = str(request.get("user") or "owner")

    def _add() -> str:
        # Loosening, so the record comes first and an add it cannot record does
        # not happen; both under the store lock so the SEL order is the write order.
        with store.transaction():
            if not store.is_size_refusal(row):
                return "not_refused"
            if not _audit(
                outcome="added", caller=subject, detail=str(row.to_dict()), critical=True
            ):
                return "audit_unavailable"
            store.add_owner_row(row)
            return "ok"

    try:
        result = await asyncio.to_thread(_add)
    except OSError as exc:
        logger.error("could not write the body-field list: %s", exc)
        return web.json_response(
            {"error": "could not record the field", "code": "write_failed"}, status=500
        )
    if result == "not_refused":
        return web.json_response(
            {"error": "only a field the gate refused can be allowed", "code": "not_refused"},
            status=409,
        )
    if result == "audit_unavailable":
        return web.json_response(
            {"error": "the security event log is unavailable", "code": "audit_unavailable"},
            status=503,
        )
    return web.json_response(await asyncio.to_thread(_listing))


async def api_mcp_body_fields_remove(request: web.Request) -> web.Response:
    """DELETE /api/security/mcp-body-fields -- drop one owner-added row."""
    denied = await _deny_non_owner(request, "mcp_body_fields.remove")
    if denied:
        return denied
    row = await _row_from(request)
    if isinstance(row, web.Response):
        return row
    subject = str(request.get("user") or "owner")

    def _remove() -> bool:
        # Tightening: proceeds even when the audit record cannot be written.
        with store.transaction():
            _audit(outcome="removed", caller=subject, detail=str(row.to_dict()), critical=True)
            return store.remove_owner_row(row)

    try:
        removed = await asyncio.to_thread(_remove)
    except OSError as exc:
        logger.error("could not write the body-field list: %s", exc)
        return web.json_response(
            {"error": "could not record the change", "code": "write_failed"}, status=500
        )
    if not removed:
        return web.json_response({"error": "no such field", "code": "not_found"}, status=404)
    return web.json_response(await asyncio.to_thread(_listing))

"""Per-tool grants: the ``/api/security/tool-grants`` routes behind the MCP tool list.

Customize -> MCP Servers shows an "Always allow" toggle beside each
tool. It reads, adds and removes the exact ``@server/tool`` entries of
``hooks.auto_approve_tool_identities`` -- the same grants an approval card's "Always allow
this tool" writes (``dashboard.always_allow_tool``). A change is applied by the
config watcher before the response, so the next call of that tool is answered
under it with no restart.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.dashboard import always_allow_tool


def _list_refusal(server: str) -> str:
    """Overlay ownership blocks removal even when another rule blocks additions."""
    from kiro_crew.config.loader import overlay_pins

    if overlay_pins("hooks", always_allow_tool.GRANT_KEY):
        return always_allow_tool.REFUSAL_SET_ELSEWHERE
    return always_allow_tool.server_refusal(server)


def _snapshot(server: str) -> dict:
    """The grants in force, and why *server*'s tools cannot be granted (if named)."""
    out: dict = {"grants": [{"ref": ref} for ref in always_allow_tool.list_grants()]}
    if server:
        out["server"] = server
        out["refusal"] = _list_refusal(server)
    return out


async def _grants_response(server: str = "") -> web.Response:
    return web.json_response(await asyncio.to_thread(_snapshot, server))


async def api_tool_grants_list(request: web.Request) -> web.Response:
    """GET /api/security/tool-grants[?server=<name>] -- the per-tool grants (read).

    With ``server``, also ``refusal``: the code that keeps that server's toggles
    off (``""`` when they may be turned on).
    """
    server = request.query.get("server", "")
    if server and not always_allow_tool.tool_ref(server, "x"):
        return web.json_response({"error": "invalid server", "code": "invalid_server"}, status=400)
    return await _grants_response(server)


async def _ref_from_body(request: web.Request, op: str) -> str | web.Response:
    try:
        body = await request.json()
    except Exception:
        body = None
    ref = body.get("ref") if isinstance(body, dict) else None
    if not isinstance(ref, str) or not always_allow_tool.is_tool_grant(ref):
        return always_allow_tool.refuse(
            request, op, "invalid_ref", "ref must be one @server/tool grant", 400
        )
    return ref


async def api_tool_grant_add(request: web.Request) -> web.Response:
    """POST /api/security/tool-grants -- body ``{"ref": "@server/tool"}``. Idempotent.

    Owner only, like every Settings write. Refused, with the same codes the toggle
    shows, for a Kiro Crew, app or Connection server, a tool a ``confirm_tools``
    pattern covers, and while ``config.local.json`` sets the list.
    """
    op = "security.tool_grants.add"
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
    ref = await _ref_from_body(request, op)
    if isinstance(ref, web.Response):
        return ref
    refusal = await asyncio.to_thread(always_allow_tool.refusal_for, ref)
    if refusal:
        return always_allow_tool.refuse(
            request, op, refusal, "this tool cannot be always allowed", 409, ref=ref
        )
    refused = await always_allow_tool.save_grant(request, ref, add=True, operation=op)
    if refused is not None:
        return refused
    return await _grants_response(ref[1:].partition("/")[0])


async def api_tool_grant_remove(request: web.Request) -> web.Response:
    """POST /api/security/tool-grants/remove -- body ``{"ref": "@server/tool"}``. Idempotent.

    Owner only. Only an exact ``@server/tool`` entry of the grant list is
    removed: ``hooks.auto_approve_tools`` and wider patterns an operator wrote are
    not this control's to touch. Refused with 409
    ``set_elsewhere`` while ``config.local.json`` owns the effective list.
    """
    op = "security.tool_grants.remove"
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
    ref = await _ref_from_body(request, op)
    if isinstance(ref, web.Response):
        return ref
    refusal = await asyncio.to_thread(_list_refusal, ref[1:].partition("/")[0])
    if refusal == always_allow_tool.REFUSAL_SET_ELSEWHERE:
        return always_allow_tool.refuse(
            request, op, refusal, "this tool grant is controlled elsewhere", 409, ref=ref
        )
    refused = await always_allow_tool.save_grant(request, ref, add=False, operation=op)
    if refused is not None:
        return refused
    return await _grants_response(ref[1:].partition("/")[0])

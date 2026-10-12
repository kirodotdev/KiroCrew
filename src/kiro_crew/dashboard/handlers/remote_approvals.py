"""Peer route: the remote hub's answer to a floored run's tool request.

``POST /api/spawn/{agent_id}/approvals/{approval_id}`` with ``{"approved": bool}``
settles one request a floored run parked in
:mod:`kiro_crew.subagent_manager.hub_approvals`. Only the dashboard owner may
answer, which is the credential a hub forwards through its instance tunnel: an
agent on this gateway (an internal MCP caller or an app) must never be able to
approve a tool request, its own or another run's.
"""

from __future__ import annotations

import re

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

_APPROVAL_ID_RE = re.compile(r"^[a-f0-9]{16}\Z")


async def api_spawn_approval_answer(request: web.Request) -> web.Response:
    """Settle one pending hub-relayed tool request of a floored run."""
    if request.get("internal_auth") or request.get("app", ""):
        return _owner_denial_response(request, "dashboard owner required", "owner_only")
    if not is_owner_dashboard_request(request):
        return _owner_denial_response(request, "dashboard owner required", "owner_only")
    agent_id = request.match_info["agent_id"]
    approval_id = request.match_info["approval_id"]
    if not _APPROVAL_ID_RE.fullmatch(approval_id):
        return web.json_response(
            {"error": "invalid approval id", "code": "invalid_approval_id"}, status=400
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    approved = body.get("approved") if isinstance(body, dict) else None
    if not isinstance(approved, bool):
        return web.json_response(
            {"error": "approved must be true or false", "code": "invalid_approval_answer"},
            status=400,
        )
    from kiro_crew.sel import sel
    from kiro_crew.subagent_manager.hub_approvals import resolve

    if not resolve(agent_id, approval_id, approved):
        return web.json_response(
            {"error": "no such pending approval", "code": "approval_not_pending"}, status=404
        )
    sel().log_api_access(
        caller="remote-hub",
        operation="subagent.hub_approval",
        outcome="approved" if approved else "rejected",
        source="subagent",
        resources=f"subagent_id={agent_id},approval_id={approval_id}",
    )
    return web.json_response({"id": agent_id, "approval_id": approval_id, "approved": approved})

"""This-crew AgentCore Gateway catalog — Settings → Security debug.

Owner dashboard cookie only. App tokens are refused: these routes
enumerate AWS account Gateways and can start a target Sync. GET is
live control-plane + optional tools/list (through the workload SigV4
proxy) + a vend-and-discard WAT probe; POST /verify is the same
snapshot with a distinct SEL verb;
POST /sync asks the Gateway to refresh one DEFAULT target.

A WAT never leaves this handler. Non-2xx bodies carry a machine
``code``.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.platform.agentcore_inspect import inspect_snapshot, synchronize_target

logger = logging.getLogger(__name__)

OP_GET = "agentcore.gateway.get"
OP_VERIFY = "agentcore.gateway.verify"
OP_SYNC = "agentcore.gateway.sync"


def _audit(
    request: web.Request,
    *,
    operation: str,
    outcome: str,
    resources: str = "",
    error: str = "",
) -> None:
    try:
        import kiro_crew.dashboard.handlers as pkg

        pkg.sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
            error=error,
        )
    except Exception:
        logger.warning("SEL logging failed for %s", operation, exc_info=True)


def _refuse_non_owner(request: web.Request, operation: str) -> web.Response | None:
    from kiro_crew.dashboard.handlers.source_providers import (
        is_owner_dashboard_request,
        stale_owner_session_response,
    )

    if request.get("app"):
        _audit(
            request,
            operation=operation,
            outcome="denied",
            error="app tokens may not inspect AgentCore Gateway",
        )
        return web.json_response(
            {"error": "dashboard user required", "code": "dashboard_user_required"},
            status=403,
        )
    if not is_owner_dashboard_request(request):
        _audit(request, operation=operation, outcome="denied", error="non_owner")
        stale = stale_owner_session_response(request)
        if stale is not None:
            return stale
        return web.json_response(
            {"error": "dashboard owner required", "code": "dashboard_owner_required"},
            status=403,
        )
    return None


async def api_agentcore_gateway_get(request: web.Request) -> web.Response:
    """GET /api/agentcore/gateway — live catalog + checks."""
    refused = _refuse_non_owner(request, OP_GET)
    if refused is not None:
        return refused
    payload = await asyncio.to_thread(inspect_snapshot, include_tools=True)
    _audit(
        request,
        operation=OP_GET,
        outcome="success",
        resources=str(payload.get("code") or ""),
    )
    return web.json_response(payload)


async def api_agentcore_gateway_verify(request: web.Request) -> web.Response:
    """POST /api/agentcore/gateway/verify — same snapshot, operator-asked."""
    refused = _refuse_non_owner(request, OP_VERIFY)
    if refused is not None:
        return refused
    payload = await asyncio.to_thread(inspect_snapshot, include_tools=True)
    _audit(
        request,
        operation=OP_VERIFY,
        outcome="success",
        resources=str(payload.get("code") or ""),
    )
    return web.json_response(payload)


async def api_agentcore_gateway_sync(request: web.Request) -> web.Response:
    """POST /api/agentcore/gateway/sync — SynchronizeGatewayTargets one target."""
    refused = _refuse_non_owner(request, OP_SYNC)
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except Exception:
        _audit(request, operation=OP_SYNC, outcome="denied", resources="invalid_json")
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        _audit(request, operation=OP_SYNC, outcome="denied", resources="body_not_object")
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_json"},
            status=400,
        )
    raw = body.get("target_id")
    if not isinstance(raw, str) or not raw.strip():
        _audit(request, operation=OP_SYNC, outcome="denied", resources="bad_target")
        return web.json_response(
            {"error": "target_id is required", "code": "invalid_target"},
            status=400,
        )
    result = await asyncio.to_thread(synchronize_target, raw.strip())
    code = str(result.get("code") or "aws_error")
    if code == "accepted":
        _audit(request, operation=OP_SYNC, outcome="success", resources=raw.strip())
        return web.json_response(result)
    status = 403 if code == "aws_denied" else 400
    if code in {"aws_error", "not_found"}:
        status = 502 if code == "aws_error" else 404
    _audit(request, operation=OP_SYNC, outcome="denied", resources=code)
    return web.json_response(
        {"error": "could not sync this Gateway target", "code": code, **result},
        status=status,
    )

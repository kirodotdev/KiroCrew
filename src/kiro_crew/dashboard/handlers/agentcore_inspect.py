"""This-crew AgentCore Gateway catalog — Settings → Security debug.

Owner dashboard cookie only. App tokens are refused: these routes
enumerate AWS account Gateways and can start a target Sync. GET is
live control-plane + optional tools/list (through the workload SigV4
proxy) + a vend-and-discard WAT probe; POST /verify is the same
snapshot with a distinct SEL verb;
POST /sync is operator-gated (``KIROCREW_AGENTCORE_GATEWAY_SYNC=1``):
the standard instance role does not permit SynchronizeGatewayTargets.

A WAT never leaves this handler. Non-2xx bodies carry a machine
``code``.
"""

from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web

# Standard instance role omits SynchronizeGatewayTargets. Opt-in only.
GATEWAY_SYNC_ENV = "KIROCREW_AGENTCORE_GATEWAY_SYNC"

logger = logging.getLogger(__name__)

OP_GET = "agentcore.gateway.get"
OP_VERIFY = "agentcore.gateway.verify"
OP_PREVIEW = "agentcore.gateway.preview"
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


def _refuse_disabled_capability(request: web.Request, operation: str) -> web.Response | None:
    """Fail closed when the effective ceiling does not permit AgentCore."""
    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY, governance_permits

    try:
        decision = governance_permits(
            "capabilities.agentcore",
            "",
            session_key=HOST_SESSION_KEY,
            fail_closed=True,
            log_warning=False,
        )
    except Exception:
        _audit(request, operation=operation, outcome="denied", error="governance_unavailable")
        return web.json_response(
            {"error": "AgentCore identity is disabled", "code": "agentcore_disabled"},
            status=403,
        )
    if not bool(getattr(decision, "permitted", False)):
        _audit(request, operation=operation, outcome="denied", error="capability_disabled")
        return web.json_response(
            {"error": "AgentCore identity is disabled", "code": "agentcore_disabled"},
            status=403,
        )
    return None


async def _audit_async(
    request: web.Request,
    *,
    operation: str,
    outcome: str,
    resources: str = "",
    error: str = "",
) -> None:
    """SEL first-use can mkdir; keep that off the event loop."""
    await asyncio.to_thread(
        _audit,
        request,
        operation=operation,
        outcome=outcome,
        resources=resources,
        error=error,
    )


async def _owner_gate(request: web.Request, operation: str) -> web.Response | None:
    refused = await asyncio.to_thread(_refuse_non_owner, request, operation)
    if refused is not None:
        return refused
    return await asyncio.to_thread(_refuse_disabled_capability, request, operation)


async def api_agentcore_gateway_get(request: web.Request) -> web.Response:
    """GET /api/agentcore/gateway — live catalog + checks."""
    refused = await _owner_gate(request, OP_GET)
    if refused is not None:
        return refused
    from kiro_crew.platform.agentcore_inspect import inspect_snapshot

    payload = await asyncio.to_thread(inspect_snapshot, include_tools=True)
    await _audit_async(
        request,
        operation=OP_GET,
        outcome="success",
        resources=str(payload.get("code") or ""),
    )
    return web.json_response(payload)


async def api_agentcore_gateway_verify(request: web.Request) -> web.Response:
    """POST /api/agentcore/gateway/verify — same snapshot, operator-asked."""
    refused = await _owner_gate(request, OP_VERIFY)
    if refused is not None:
        return refused
    from kiro_crew.platform.agentcore_inspect import inspect_snapshot

    payload = await asyncio.to_thread(inspect_snapshot, include_tools=True)
    await _audit_async(
        request,
        operation=OP_VERIFY,
        outcome="success",
        resources=str(payload.get("code") or ""),
    )
    return web.json_response(payload)


_PREVIEW_POSTURES = frozenset({"workload", "login"})


async def api_agentcore_gateway_preview(request: web.Request) -> web.Response:
    """POST /api/agentcore/gateway/preview — what Save WOULD grant, unsaved.

    Body ``{gateway_url, posture}``. The drafted URL is validated exactly as
    the identity PUT validates it, then read through the same control-plane
    calls the catalog card uses, so the operator sees the Gateway and its
    targets at the decision point instead of after committing. Nothing is
    persisted and no identity is created; tools are reported as deferred.

    Gated like the PUT it previews, not like the catalog: owner cookie plus a
    WRITABLE home policy. The capability gate the other inspect routes use
    would refuse exactly the crew this exists for -- one whose saved posture
    is still Off -- since ``capabilities.agentcore`` is not yet permitted
    there. A fleet-signed, distributed or env-overridden policy answers 409
    ``policy_not_writable`` (the same refusal Save would give).
    """
    refused = await asyncio.to_thread(_refuse_non_owner, request, OP_PREVIEW)
    if refused is not None:
        return refused
    from kiro_crew.dashboard.handlers.agentcore_identity import _write_reason

    blocked = await asyncio.to_thread(_write_reason)
    if blocked:
        await _audit_async(
            request, operation=OP_PREVIEW, outcome="denied", resources=f"not_writable:{blocked}"
        )
        return web.json_response(
            {
                "error": "this crew's AgentCore identity is not configurable here",
                "code": "policy_not_writable",
                "reason": blocked,
            },
            status=409,
        )
    try:
        body = await request.json()
    except Exception:
        await _audit_async(
            request, operation=OP_PREVIEW, outcome="denied", resources="invalid_json"
        )
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        await _audit_async(
            request, operation=OP_PREVIEW, outcome="denied", resources="body_not_object"
        )
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_json"},
            status=400,
        )
    raw_posture = body.get("posture")
    posture = raw_posture.strip().lower() if isinstance(raw_posture, str) else ""
    if posture not in _PREVIEW_POSTURES:
        await _audit_async(request, operation=OP_PREVIEW, outcome="denied", resources="bad_posture")
        return web.json_response(
            {"error": "posture must be workload or login", "code": "invalid_posture"},
            status=400,
        )
    from kiro_crew.platform.agentcore_schema import normalize_agentcore_gateway_url
    from kiro_crew.platform.agentcore_sigv4 import is_agentcore_gateway_url

    raw_url = body.get("gateway_url")
    gateway_url = ""
    if isinstance(raw_url, str) and raw_url.strip():
        try:
            gateway_url = normalize_agentcore_gateway_url(raw_url)
        except ValueError:
            gateway_url = ""
    if not gateway_url or not is_agentcore_gateway_url(gateway_url):
        await _audit_async(
            request, operation=OP_PREVIEW, outcome="denied", resources="bad_gateway_url"
        )
        return web.json_response(
            {
                "error": "gateway_url must be an AgentCore Gateway MCP URL",
                "code": "invalid_agentcore_gateway_url",
            },
            status=400,
        )
    from kiro_crew.platform.agentcore_inspect import preview_snapshot

    payload = await asyncio.to_thread(preview_snapshot, gateway_url, posture)
    await _audit_async(
        request,
        operation=OP_PREVIEW,
        outcome="success",
        resources=str(payload.get("code") or ""),
    )
    return web.json_response(payload)


async def api_agentcore_gateway_sync(request: web.Request) -> web.Response:
    """POST /api/agentcore/gateway/sync — SynchronizeGatewayTargets one target."""
    refused = await _owner_gate(request, OP_SYNC)
    if refused is not None:
        return refused
    if os.environ.get(GATEWAY_SYNC_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}:
        await _audit_async(
            request,
            operation=OP_SYNC,
            outcome="denied",
            resources="operator_gate",
        )
        return web.json_response(
            {
                "error": "Gateway target sync is not permitted on the standard instance role",
                "code": "sync_not_permitted",
            },
            status=403,
        )
    try:
        body = await request.json()
    except Exception:
        await _audit_async(request, operation=OP_SYNC, outcome="denied", resources="invalid_json")
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        await _audit_async(
            request, operation=OP_SYNC, outcome="denied", resources="body_not_object"
        )
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_json"},
            status=400,
        )
    raw = body.get("target_id")
    if not isinstance(raw, str) or not raw.strip():
        await _audit_async(request, operation=OP_SYNC, outcome="denied", resources="bad_target")
        return web.json_response(
            {"error": "target_id is required", "code": "invalid_target"},
            status=400,
        )
    from kiro_crew.platform.agentcore_inspect import synchronize_target

    result = await asyncio.to_thread(synchronize_target, raw.strip())
    code = str(result.get("code") or "aws_error")
    if code == "accepted":
        await _audit_async(request, operation=OP_SYNC, outcome="success", resources=raw.strip())
        return web.json_response(result)
    await _audit_async(request, operation=OP_SYNC, outcome="denied", resources=code)
    error = "could not sync this Gateway target"
    if code == "aws_denied":
        return web.json_response({"error": error, "code": "aws_denied"}, status=403)
    if code == "not_found":
        return web.json_response({"error": error, "code": "not_found"}, status=404)
    if code == "aws_error":
        return web.json_response({"error": error, "code": "aws_error"}, status=502)
    return web.json_response({"error": error, "code": code}, status=400)

"""GET /api/agentcore/consent — allowlisted pending 3LO URL.

Owner dashboard cookie only. App tokens and allow-listed messaging
users are refused: the URL is a live 3LO start and names the
operator's IdP. Identity GET/PUT is a later PR.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

OP_CONSENT = "agentcore.consent.get"


def _write_consent_audit(
    request: web.Request,
    *,
    operation: str,
    outcome: str,
    resources: str = "",
    error: str = "",
) -> None:
    """SEL row for a consent GET. Nested in ``to_thread``; tests call this
    directly because CI coverage does not trace the worker thread.
    """
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


async def _audit(
    request: web.Request,
    *,
    operation: str,
    outcome: str,
    resources: str = "",
    error: str = "",
) -> None:
    await asyncio.to_thread(
        _write_consent_audit,
        request,
        operation=operation,
        outcome=outcome,
        resources=resources,
        error=error,
    )


async def api_agentcore_consent_get(request: web.Request) -> web.Response:
    """GET /api/agentcore/consent — allowlisted pending 3LO URL, or absent."""
    from kiro_crew.dashboard.handlers.source_providers import (
        is_owner_dashboard_request,
        stale_owner_session_response,
    )
    from kiro_crew.platform.agentcore_gateway import consent_snapshot

    if not is_owner_dashboard_request(request):
        await _audit(
            request,
            operation=OP_CONSENT,
            outcome="denied",
            error="non_owner",
        )
        stale = stale_owner_session_response(request)
        if stale is not None:
            return stale
        return web.json_response(
            {"error": "dashboard owner required", "code": "dashboard_owner_required"},
            status=403,
        )

    snap = await asyncio.to_thread(consent_snapshot)
    if snap["refused"]:
        await _audit(
            request,
            operation=OP_CONSENT,
            outcome="denied",
            resources="consent_host_refused",
        )
        return web.json_response(
            {
                "error": "consent host is not on this crew's allowlist",
                "code": "consent_host_refused",
            },
            status=403,
        )
    if snap["pending"]:
        await _audit(request, operation=OP_CONSENT, outcome="success", resources="pending")
        return web.json_response({"pending": True, "url": snap["url"]})
    await _audit(request, operation=OP_CONSENT, outcome="success", resources="none")
    return web.json_response({"pending": False, "url": None})

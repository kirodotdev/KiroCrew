"""Owner-only Docker registry credential grant API.

The grant is a protected keystone rather than an ordinary config value.  That
keeps an agent from enabling its own access and gives Settings one honest,
dedicated surface for reading and changing the authorization.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any

from aiohttp import web

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    docker_registry_access_state,
    docker_registry_access_state_path,
)
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

logger = logging.getLogger(__name__)

OP_READ = "docker_registry_access.read"
OP_WRITE = "docker_registry_access.write"
DOCKER_REGISTRY_ACCESS_TTL_SECONDS = 6 * 60 * 60


def _snapshot() -> dict[str, Any]:
    """Return the effective grant plus platform availability."""
    return {
        **docker_registry_access_state(),
        "supported": sys.platform.startswith("linux"),
    }


def _audit_sync(
    *,
    caller: str,
    outcome: str,
    resources: str,
    error: str = "",
    critical: bool = False,
    operation: str = OP_WRITE,
) -> None:
    """Require a durable grant audit; keep denial and revocation best-effort."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
            error=error,
            critical=critical,
        )
    except Exception:
        if critical:
            raise
        logger.warning("SEL logging failed for %s", operation, exc_info=True)


async def _audit(
    request: web.Request,
    *,
    outcome: str,
    resources: str,
    error: str = "",
    critical: bool = False,
    operation: str = OP_WRITE,
) -> None:
    """Write the SEL record without blocking the event loop."""
    await asyncio.to_thread(
        _audit_sync,
        caller=str(request.get("user", "dashboard")),
        outcome=outcome,
        resources=resources,
        error=error,
        critical=critical,
        operation=operation,
    )


async def api_docker_registry_access_get(request: web.Request) -> web.Response:
    """GET the operator grant and its effective platform availability."""
    owner_denied = await require_owner_dashboard_request(request, OP_READ)
    if owner_denied is not None:
        return owner_denied
    await _audit(request, operation=OP_READ, outcome="approved", resources="grant_state")
    try:
        payload = await asyncio.to_thread(_snapshot)
    except Exception:
        logger.warning("Docker registry access state could not be resolved", exc_info=True)
        return web.json_response(
            {"error": "could not resolve Docker registry access", "code": "state_unavailable"},
            status=503,
        )
    return web.json_response(payload)


async def api_docker_registry_access_put(request: web.Request) -> web.Response:
    """Persist an explicit owner decision and refresh only future sessions."""
    owner_denied = await require_owner_dashboard_request(request, OP_WRITE)
    if owner_denied is not None:
        return owner_denied

    try:
        body = await request.json()
    except Exception:
        await _audit(request, outcome="denied", resources="invalid_json")
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict) or not set(body).issubset(
        {"enabled", "permanent", "acknowledged"}
    ):
        await _audit(request, outcome="denied", resources="invalid_body")
        return web.json_response(
            {
                "error": "body must contain enabled; grants also require permanent and acknowledged",
                "code": "invalid_body",
            },
            status=400,
        )
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        await _audit(request, outcome="denied", resources="enabled=invalid")
        return web.json_response(
            {"error": "enabled must be boolean", "code": "invalid_enabled"},
            status=400,
        )
    permanent = body.get("permanent", False)
    if not isinstance(permanent, bool) or (permanent and not enabled):
        await _audit(request, outcome="denied", resources="permanent=invalid")
        return web.json_response(
            {
                "error": "permanent must be boolean and requires enabled=true",
                "code": "invalid_permanent",
            },
            status=400,
        )

    if enabled and ("permanent" not in body or body.get("acknowledged") is not True):
        await _audit(request, outcome="denied", resources="consent=missing")
        return web.json_response(
            {"error": "explicit duration and acknowledgement required", "code": "consent_required"},
            status=400,
        )

    if enabled and not sys.platform.startswith("linux"):
        await _audit(request, outcome="denied", resources="platform_unsupported")
        return web.json_response(
            {
                "error": "Docker registry credential access requires a Linux namespace sandbox",
                "code": "platform_unsupported",
            },
            status=409,
        )

    state: dict[str, object] = {"enabled": enabled}
    if enabled:
        if permanent:
            state["permanent"] = True
        else:
            state["expires_at"] = time.time() + DOCKER_REGISTRY_ACCESS_TTL_SECONDS
    payload = json.dumps(state, indent=2) + "\n"
    if enabled:
        try:
            await _audit(
                request,
                outcome="approved",
                resources=f"enabled=true permanent={str(permanent).lower()}",
                critical=True,
            )
        except Exception:
            logger.warning("Docker registry grant audit failed", exc_info=True)
            return web.json_response(
                {"error": "could not audit Docker registry access", "code": "audit_failed"},
                status=503,
            )
    try:
        await asyncio.to_thread(
            atomic_write,
            docker_registry_access_state_path(),
            payload,
            restrict_to_owner=True,
        )
    except OSError:
        logger.warning("Docker registry access state could not be persisted", exc_info=True)
        await _audit(request, outcome="error", resources="write_failed")
        return web.json_response(
            {"error": "could not save Docker registry access", "code": "write_failed"},
            status=500,
        )

    # The authorization is committed once atomic_write returns. Everything
    # below is follow-up: never report the SAVE as failed after the keystone has
    # already changed. Best-effort drain the warm pool so idle processes do not
    # retain an old snapshot. Every subsequent agent spawn re-reads the keystone
    # independently; a failed refresh cannot re-authorize a revoked grant.
    # Existing live processes keep their namespace until the operator stops them.
    await _audit(request, outcome="ok", resources=f"enabled={str(enabled).lower()}")
    try:
        await request.app["state"].sessions.refresh_defaults()
    except Exception:
        logger.exception("Docker registry access saved, but session defaults refresh failed")

    try:
        response_payload = await asyncio.to_thread(_snapshot)
    except Exception:
        logger.warning("Docker registry access saved, but response refresh failed", exc_info=True)
        response_payload = {
            "enabled": enabled,
            "supported": sys.platform.startswith("linux"),
            "permanent": state.get("permanent", False),
            "expires_at": state.get("expires_at"),
        }
    return web.json_response(response_payload)


__all__ = ["api_docker_registry_access_get", "api_docker_registry_access_put"]

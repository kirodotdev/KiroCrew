"""Authenticated dashboard handlers for Kiro CLI first-run setup."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from aiohttp import web

from kiro_crew.kiro_prerequisite import (
    OFFICIAL_INSTALL_DOCS_URL,
    KiroPrerequisiteService,
    OperationStatus,
    PrerequisiteBusyError,
    PrerequisiteStatus,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)
_LOCAL_DASHBOARD_OWNER_SUBJECTS = frozenset({"local-app", "local-startup"})


def _not_ready_snapshot(initial_setup_complete: bool = False) -> dict[str, Any]:
    """A retryable not-ready snapshot for when a status probe cannot run.

    Shaped exactly like ``KiroPrerequisiteService.snapshot()`` (built from the
    same dataclasses so it cannot drift), it reports the CLI as installed but
    not signed in so the dashboard shows a retry path rather than a 500 flash.

    ``initial_setup_complete`` is carried through from the service so a failed
    probe does not demote a returning user to first-run — reporting ``False``
    here makes the SPA restore the full-screen first-run setup gate for someone
    who finished setup long ago.
    """

    result: dict[str, Any] = asdict(
        PrerequisiteStatus(
            platform="gateway",
            installed=True,
            initial_setup_complete=initial_setup_complete,
        )
    )
    result["operation"] = asdict(
        OperationStatus(
            status="failed",
            message="Could not check Kiro CLI. Retry the gateway check.",
            error="Kiro CLI status check could not run.",
        )
    )
    return result


def _service(request: web.Request) -> KiroPrerequisiteService:
    service = request.app.get("kiro_prerequisite_service")
    if not isinstance(service, KiroPrerequisiteService):
        raise web.HTTPServiceUnavailable(
            text="Kiro prerequisite service unavailable.",
            content_type="text/plain",
        )
    return service


def _caller(request: web.Request) -> str:
    user = request.get("user", "")
    return str(user) if user else "dashboard-user"


def _is_dashboard_owner(request: web.Request) -> bool:
    """Return whether a signed dashboard identity may operate host setup."""

    state = request.app["state"]
    owner_id = str(getattr(state, "owner_id", "") or "")
    caller = str(request.get("user") or "")
    return request.get("app") == "" and (
        (owner_id and caller == owner_id)
        or (not owner_id and caller in _LOCAL_DASHBOARD_OWNER_SUBJECTS)
    )


async def _dashboard_owner_only(request: web.Request) -> web.Response | None:
    """Require the configured owner or a signed standalone-local identity."""

    if _is_dashboard_owner(request):
        return None

    caller = str(request.get("user") or "")
    audit_caller = str(request.get("app") or caller or "unknown")

    def _audit() -> None:
        sel().log_api_access(
            caller=audit_caller,
            operation="kiro_prerequisite_access",
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error="dashboard owner required",
        )

    try:
        await asyncio.to_thread(_audit)
    except Exception:
        logger.debug("Could not audit denied Kiro prerequisite access", exc_info=True)
    return web.json_response({"error": "dashboard owner required"}, status=403)


async def api_kiro_prerequisite_status(request: web.Request) -> web.Response:
    """GET /api/kiro-prerequisite — current install/login readiness.

    Reads LATCHED state by default: readiness is probed at gateway start and
    then only on explicit request, so the SPA's background poll costs no
    ``kiro-cli`` subprocess. ``?refresh=1`` (the gate's Refresh / Check again
    button) is the explicit user action that forces a real probe.
    """

    if request.get("app") != "":
        denied = await _dashboard_owner_only(request)
        assert denied is not None
        return denied

    # Only an owner may force a host probe; a non-owner's refresh reads latched
    # state like any other poll (they receive the redacted payload regardless).
    force = request.query.get("refresh") in ("1", "true") and _is_dashboard_owner(request)

    # Resolve the service OUTSIDE the guard: a genuinely unwired service is a
    # real misconfiguration that must stay a 503, not be masked as a 200
    # not-ready. Only the probe itself is guarded.
    service = _service(request)
    try:
        snapshot = await service.snapshot(force=force)
    except asyncio.CancelledError:
        raise
    except web.HTTPException:
        raise
    except Exception:
        # A transient probe failure must not surface as a 500 that flashes the
        # full-screen "could not check Kiro CLI" gate. Report a retryable
        # not-ready snapshot so the dashboard keeps polling. (The probe layer
        # already degrades most failures; this is the last-resort backstop.)
        # The first-run bit is read from the data home, not the probe, so it
        # survives this path and keeps a returning user out of first-run setup.
        logger.warning("Kiro prerequisite status probe failed", exc_info=True)
        snapshot = _not_ready_snapshot(bool(service.initial_setup_complete))
    if _is_dashboard_owner(request):
        return web.json_response({**snapshot, "setup_allowed": True})

    # Authorized non-owner dashboard users need the readiness bit so the
    # application gate does not lock them out after the owner completes setup.
    # Do not expose the host platform, candidate state, operation output, URLs,
    # or mutations to those users.
    return web.json_response(
        {
            "platform": "gateway",
            "installed": False,
            "authenticated": False,
            "ready": bool(snapshot.get("ready")),
            "initial_setup_complete": bool(snapshot.get("initial_setup_complete")),
            "can_auto_install": False,
            "can_login": False,
            "repair_required": False,
            "docs_url": OFFICIAL_INSTALL_DOCS_URL,
            "setup_allowed": False,
            "operation": {
                "kind": "",
                "status": "idle",
                "message": "",
                "detail": "",
                "url": "",
                "error": "",
            },
        }
    )


async def api_kiro_prerequisite_install(request: web.Request) -> web.Response:
    """POST /api/kiro-prerequisite/install — start the fixed official installer."""

    denied = await _dashboard_owner_only(request)
    if denied is not None:
        return denied
    try:
        snapshot = _service(request).start_install(_caller(request))
    except PrerequisiteBusyError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    return web.json_response({**snapshot, "setup_allowed": True}, status=202)


async def api_kiro_prerequisite_login(request: web.Request) -> web.Response:
    """POST /api/kiro-prerequisite/login — start Kiro device-flow login."""

    denied = await _dashboard_owner_only(request)
    if denied is not None:
        return denied
    try:
        snapshot = _service(request).start_login(_caller(request))
    except PrerequisiteBusyError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    return web.json_response({**snapshot, "setup_allowed": True}, status=202)

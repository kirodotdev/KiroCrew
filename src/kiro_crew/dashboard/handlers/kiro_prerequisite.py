"""Authenticated dashboard handlers for Kiro CLI first-run setup."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.kiro_prerequisite import (
    OFFICIAL_INSTALL_DOCS_URL,
    KiroPrerequisiteService,
    PrerequisiteBusyError,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)
_LOCAL_DASHBOARD_OWNER_SUBJECTS = frozenset({"local-app", "local-startup"})


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
    """GET /api/kiro-prerequisite — current install/login readiness."""

    if request.get("app") != "":
        denied = await _dashboard_owner_only(request)
        assert denied is not None
        return denied

    snapshot = await _service(request).snapshot()
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

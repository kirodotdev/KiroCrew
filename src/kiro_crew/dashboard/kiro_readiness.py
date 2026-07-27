"""Shared pre-enqueue guard for Kiro-backed dashboard sessions."""

from __future__ import annotations

from aiohttp import web

from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

_KIRO_NOT_READY_RESPONSE = {
    "error": "Kiro CLI setup or sign-in is required before starting a session.",
    "code": "kiro_prerequisite_required",
}


async def kiro_session_ready(service: object) -> bool:
    """Return the service's latched readiness for session-starting paths."""

    if not isinstance(service, KiroPrerequisiteService):
        return False
    return await service.session_ready()


async def reject_if_kiro_not_ready(request: web.Request) -> web.Response | None:
    """Return 503 before a session is created or a turn is enqueued.

    Production and embedded applications must install an explicit prerequisite
    service. Tests that intentionally bypass host probing can use
    ``KiroPrerequisiteService(assume_ready=True)``.
    """

    service = request.app.get("kiro_prerequisite_service")
    if service is None:
        service = getattr(request.app.get("state"), "kiro_prerequisite_service", None)
    if await kiro_session_ready(service):
        return None
    return web.json_response(_KIRO_NOT_READY_RESPONSE, status=503)

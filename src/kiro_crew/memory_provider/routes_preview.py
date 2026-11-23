"""``GET`` / ``PUT /api/memory/provider-preview``: the owner's memory provider switch.

Owner-only, with the gate the memory provider routes use. Registered whether or
not the preview is on, because the owner must be able to turn it on from a Crew
that has it off; a caller who is not the owner is refused 403, never 404. The
path sits outside ``/api/memory/provider/``, every route of which is a 404 while
the preview is off.

The body is :class:`kiro_crew.shared_crew.preview.PreviewSetting`'s, as for the
Shared Crew switch. A PUT body is ``{"enabled": bool}``; it writes the keystone and
changes nothing in the running process. Past the owner gate, both handlers are the
Shared Crew switch's (:mod:`kiro_crew.shared_crew.routes_preview`).
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.memory_provider import preview
from kiro_crew.shared_crew.routes_preview import audit, put_setting, respond_setting

PREVIEW_PATH = "/api/memory/provider-preview"


async def _owner(request: web.Request, operation: str) -> None:
    """Raise the memory provider routes' 403 for anyone but the owner."""
    from kiro_crew.dashboard.handlers.memory_provider import _caller

    await _caller(request, operation, owner_only=True)


def _audit(request: web.Request, enabled: bool, outcome: str) -> None:
    audit(request, enabled, outcome, "memory_provider")


async def api_get_preview(request: web.Request) -> web.Response:
    await _owner(request, "preview.get")
    return await respond_setting(preview)


async def api_put_preview(request: web.Request) -> web.Response:
    await _owner(request, "preview.set")
    return await put_setting(request, preview, lambda *a: _audit(*a), "memory_provider")

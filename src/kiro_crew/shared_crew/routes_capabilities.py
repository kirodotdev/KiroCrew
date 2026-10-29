"""``GET /api/shared-crew/capabilities``: what this Crew can truthfully back.

Readable by any caller the dashboard auth already admitted, like every other
read route; it reports states and reasons only, never a credential, URL or
reference a provider holds. The gateway's route table mounts :func:`register`.
"""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew.shared_crew.capabilities import aggregate_capabilities

logger = logging.getLogger(__name__)

CAPABILITIES_PATH = "/api/shared-crew/capabilities"


async def api_shared_crew_capabilities(request: web.Request) -> web.Response:
    report = aggregate_capabilities()
    return web.json_response(report.to_dict(), headers={"Cache-Control": "no-store"})


def register(app: web.Application) -> None:
    app.router.add_get(CAPABILITIES_PATH, api_shared_crew_capabilities)

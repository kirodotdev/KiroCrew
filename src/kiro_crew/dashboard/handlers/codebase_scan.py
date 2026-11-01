"""``GET /api/codebase/scan``: the model a codebase session draws (PoC).

Owner-only, like a doc: the scan lists a work tree's files, so an app token, a peer
crew or an unattributed caller is refused before the root is looked at. The scan
runs off the event loop.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.dashboard import codebase_scan
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request


async def api_codebase_scan(request: web.Request) -> web.Response:
    if not is_owner_dashboard_request(request):
        return web.json_response({"error": "forbidden", "code": "codebase_owner_only"}, status=403)
    root = request.query.get("root", "")
    since = request.query.get("since") or None
    try:
        model = await asyncio.to_thread(codebase_scan.scan, root, since)
    except codebase_scan.ScanError as exc:
        return web.json_response({"error": str(exc), "code": exc.code}, status=400)
    return web.json_response(model)


def register_codebase_routes(app: web.Application) -> None:
    app.router.add_get("/api/codebase/scan", api_codebase_scan)

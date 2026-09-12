"""In-process HTTP surface for the Kiro Crew Guide app.

``register_routes(app)`` mounts handlers on the gateway's OWN aiohttp
application at startup, so requests are same-origin and already authenticated by
the gateway middleware — there is no second server and no port of its own.

Because routes are registered once at startup while the app is opt-in
(``defaultEnabled: false``), every handler is wrapped in an ``is_app_enabled``
gate — deny-by-default, matching ``meetings`` / ``issue_radar``. All read-only:
the guide is data, not state.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from functools import wraps
from typing import Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.manager import is_app_enabled

from . import search

logger = logging.getLogger("kirocrew.app.guide")

APP_NAME = search.APP_NAME
API_BASE = f"/api/apps/{APP_NAME}"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _require_enabled(handler: Handler) -> Handler:
    """Deny every request while the app is disabled (deny-by-default).

    ``is_app_enabled`` reads ``installed.json`` synchronously, so it runs off the
    event loop.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": f"{APP_NAME} is disabled", "code": "app_disabled"}, status=403
            )
        return await handler(request)

    return _wrapped


def _query_int(request: web.Request, key: str, *, default: int, low: int, high: int) -> int:
    try:
        value = int(request.query.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


@_require_enabled
async def handle_list_entries(request: web.Request) -> web.StreamResponse:
    """GET /entries?q=&platform=&topic=&limit= — ranked summaries.

    With no ``q`` the search returns the highest-weight entries, so this doubles
    as the default listing the UI shows before the user types.
    """
    q = request.query.get("q", "")
    platform = request.query.get("platform") or None
    topic = request.query.get("topic") or None
    limit = _query_int(request, "limit", default=25, low=1, high=50)
    results = await asyncio.to_thread(search.search, q, platform=platform, topic=topic, limit=limit)
    return web.json_response({"entries": results, "total": len(results)})


@_require_enabled
async def handle_get_entry(request: web.Request) -> web.StreamResponse:
    """GET /entries/{id} — one entry, full text."""
    entry_id = request.match_info["entry_id"]
    entry = await asyncio.to_thread(search.get_entry, entry_id)
    if entry is None:
        return web.json_response(
            {"error": f"no guide entry {entry_id}", "code": "not_found"}, status=404
        )
    return web.json_response(entry)


@_require_enabled
async def handle_get_media(request: web.Request) -> web.StreamResponse:
    """GET /media/{key} — a media file (overlay dir overrides base dir)."""
    key = request.match_info["key"]
    path = await asyncio.to_thread(search.resolve_media, key)
    if path is None:
        return web.json_response({"error": "media not found", "code": "not_found"}, status=404)
    ctype, _ = mimetypes.guess_type(str(path))
    return web.FileResponse(path, headers={"Content-Type": ctype or "application/octet-stream"})


@_require_enabled
async def handle_index(request: web.Request) -> web.StreamResponse:
    """GET /index — the id set (for in-text entry autolinking) plus the distinct
    platform and topic values (for the filter chips), computed from the merged data.
    """
    return web.json_response(await asyncio.to_thread(search.index))


def register_routes(app: web.Application) -> None:
    """Register the Guide app's routes on the gateway's aiohttp Application.

    Signature matches every other builtin app (one argument, no base path).
    """
    router = app.router
    router.add_get(f"{API_BASE}/entries", handle_list_entries)
    router.add_get(f"{API_BASE}/index", handle_index)
    router.add_get(API_BASE + "/entries/{entry_id}", handle_get_entry)
    # ``{key:.*}`` so a media key may contain slashes (e.g. a subdir).
    router.add_get(API_BASE + "/media/{key:.*}", handle_get_media)

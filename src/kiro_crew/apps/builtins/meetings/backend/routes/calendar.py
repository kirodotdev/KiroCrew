"""Calendar routes — sync from the configured provider and read the cache.

``GET  …/calendar``        the cached upcoming meetings
``POST …/calendar/sync``   fetch from the configured provider, refresh the cache
``GET  …/calendar/providers``  registered providers (for the settings picker)

The internal MCP path and its internal-website scraping fallback are gone; see
``backend/providers/calendar.py`` for the ``.ics`` replacement and the registry
an out-of-repo companion registers its own provider into.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.providers import calendar as cal
from kiro_crew.apps.builtins.meetings.backend.routes._common import (
    audit,
    data_root,
    query_int,
)

logger = logging.getLogger("kirocrew.app.meetings")


def _read_cached_calendar(root: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read the app config and the cached calendar events. BLOCKING.

    Runs on a worker thread, never the event loop: two JSON file reads, and the
    event cache holds up to ``MAX_CALENDAR_EVENTS`` records.

    Grouped into one hop because the response pairs the cached events with the
    provider config they were fetched under — reading them in separate hops could
    report a freshly-changed provider alongside the previous provider's events.
    """
    return store.read_config(root), store.read_calendar_cache(root)


async def handle_get_calendar(request: web.Request) -> web.Response:
    """The last successful sync's events, straight from the cache."""
    config, events = await asyncio.to_thread(_read_cached_calendar, data_root(request))
    calendar_cfg = config.get("calendar") or {}
    return web.json_response(
        {
            "events": events,
            "provider": calendar_cfg.get("provider", k.DEFAULT_CALENDAR_PROVIDER),
            "configured": bool(calendar_cfg.get("source")),
        }
    )


async def handle_calendar_providers(request: web.Request) -> web.Response:
    return web.json_response({"providers": cal.available_calendar_providers()})


async def handle_calendar_sync(request: web.Request) -> web.Response:
    """Fetch from the configured provider and replace the cache.

    The fetch is fully async (aiohttp for a URL, an executor for a local file),
    so a slow or wedged calendar endpoint cannot park the gateway's event loop.
    """
    root = data_root(request)
    days = query_int(request, "days", default=k.CALENDAR_SYNC_DAYS, low=1, high=365)
    # Two hops rather than one grouped helper: the fetch between them is an
    # `await`, so the read and the write cannot share a thread. They touch
    # different files, so there is no read-modify-write to keep atomic.
    config = await asyncio.to_thread(store.read_config, root)
    calendar_cfg = config.get("calendar") or {}
    provider = cal.get_calendar_provider(
        str(calendar_cfg.get("provider") or ""), str(calendar_cfg.get("source") or "")
    )

    try:
        events = await provider.fetch(days=days)
    except cal.CalendarError as exc:
        audit("meetings.calendar_sync", provider.provider_id, outcome="error", error=str(exc))
        return web.json_response({"ok": False, "error": str(exc), "code": "calendar_sync_failed"}, status=502)

    payload = [event.to_dict() for event in events]
    await asyncio.to_thread(store.write_calendar_cache, payload, root)
    audit("meetings.calendar_sync", provider.provider_id, outcome="ok")
    return web.json_response(
        {"ok": True, "count": len(payload), "events": payload, "provider": provider.provider_id}
    )

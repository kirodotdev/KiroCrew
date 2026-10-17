"""One calendar sync: fetch from the configured provider, replace the cache.

Shared by the ``POST …/calendar/sync`` route and the background poller
(:mod:`.calendar_poller`), so the two cannot drift on what "a sync" means — the
same provider resolution, the same fetch, the same cache write, the same audit
record. The route adds HTTP shape on top; the poller adds cadence and the
pre-creation pass.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.providers import calendar as cal
from kiro_crew.apps.builtins.meetings.backend.routes._common import audit


def calendar_settings(config: dict[str, Any]) -> dict[str, Any]:
    """The ``calendar`` block of *config* with every key present and typed.

    ``store.read_config`` fills missing keys from the defaults, but a caller
    handed a raw dict (a test, an older cache) should not have to repeat that,
    and the poller reads these on every tick.
    """
    raw = config.get("calendar")
    raw = raw if isinstance(raw, dict) else {}
    merged = {**store.DEFAULT_CONFIG["calendar"], **raw}
    interval = merged.get("poll_interval_secs")
    lead = merged.get("precreate_lead_minutes")
    return {
        "provider": str(merged.get("provider") or k.DEFAULT_CALENDAR_PROVIDER),
        "source": str(merged.get("source") or ""),
        "auto_sync": merged.get("auto_sync") is True,
        "poll_interval_secs": _clamp_int(
            interval,
            k.CALENDAR_POLL_INTERVAL_SECS,
            k.CALENDAR_POLL_MIN_SECS,
            k.CALENDAR_POLL_MAX_SECS,
        ),
        "precreate_lead_minutes": _clamp_int(
            lead, k.CALENDAR_PRECREATE_LEAD_MINUTES, 0, k.CALENDAR_PRECREATE_LEAD_MAX_MINUTES
        ),
    }


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(value, high))


async def sync_calendar(
    root: Any, *, days: int = k.CALENDAR_SYNC_DAYS
) -> tuple[str, list[dict[str, Any]]]:
    """Fetch from the configured provider and replace the cache.

    Returns ``(provider_id, events)`` with the events already in wire form.
    Raises :class:`cal.CalendarError` when the provider refuses or fails; the
    audit record is written on both outcomes, so a caller only adds its own
    surface (an HTTP status, a log line).

    Two ``to_thread`` hops rather than one grouped helper: the fetch between them
    is an ``await``, so the read and the write cannot share a thread. They touch
    different files, so there is no read-modify-write to keep atomic.
    """
    config = await asyncio.to_thread(store.read_config, root)
    settings = calendar_settings(config)
    provider = cal.get_calendar_provider(settings["provider"], settings["source"])
    try:
        events = await provider.fetch(days=days)
    except cal.CalendarError as exc:
        audit("meetings.calendar_sync", provider.provider_id, outcome="error", error=str(exc))
        raise
    payload = [event.to_dict() for event in events]
    await asyncio.to_thread(store.write_calendar_cache, payload, root)
    audit("meetings.calendar_sync", provider.provider_id, outcome="ok")
    return provider.provider_id, payload

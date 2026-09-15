"""Background calendar poll: keep the cache fresh and have the next meeting ready.

The calendar is otherwise pull-only: the cache changes when the user presses
Sync, and a meeting directory comes into being the first time the user opens
the row. A calendar integration is meant to work the other way round — the app
should already know what is about to start. So one loop, started with the app's
other ``on_startup`` hooks, does two things on each tick:

1. **Sync.** :func:`calendar_sync.sync_calendar`, exactly what the Sync button
   runs, so the list a user sees on opening the dashboard is current without a
   click.
2. **Pre-create.** For every timed event that starts within
   ``calendar.precreate_lead_minutes`` (or has started and not yet ended), create
   the meeting directory through the same idempotent ``init`` the dashboard uses.
   The meeting stays ``idle``: pre-creation never starts a session, never spawns
   an agent, and never opens a microphone — it only makes the folder, metadata,
   tasks file, and seeded outputs exist ahead of time.

Polling rather than provider push, deliberately: the gateway is normally
reachable only on loopback, and both Google's ``events.watch`` and Graph's
``subscriptions`` need an internet-reachable HTTPS endpoint.

What the loop will NOT do:

* run while the app is disabled (the enable gate is checked per tick, the same
  ``is_app_enabled`` the request gate reads);
* sync when the provider is ``none`` — that is the default install, and a
  ``CalendarError`` every five minutes would be a log flood about nothing;
* pre-create an all-day event. Its start is a date anchor, not an instant a
  meeting is about to begin at, so "starts within 15 minutes" has no meaning
  for it;
* let one bad tick end the loop. A provider failure is audited (by
  ``sync_calendar``) and logged, and the next tick runs on schedule.

Everything that touches the disk runs off the event loop — ``read_config``,
``read_meeting_meta``, the init transaction — because a periodic task is as
loop-reachable as a request handler (AUTOSDE ``no-blocking-call-on-event-loop``).
The pre-create hop takes ``START_LOCK`` for the same reason the init route does:
creation shares the lifecycle lock with deletion, so a meeting cannot be
recreated behind a concurrent delete.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import calendar_sync
from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.providers import calendar as cal
from kiro_crew.apps.builtins.meetings.backend.routes import _common, meeting_lifecycle

logger = logging.getLogger("kirocrew.app.meetings")

# Module-level strong ref so the task is not garbage-collected mid-flight (the
# same shape as issue-radar's watcher).
_poll_task: asyncio.Task[None] | None = None


async def start_poller(app: web.Application) -> None:
    """``app.on_startup`` hook — launch the single background loop. Idempotent."""
    global _poll_task
    if _poll_task is not None and not _poll_task.done():
        return
    _poll_task = asyncio.create_task(_poll_loop(app), name="meetings-calendar-poll")
    logger.info("meetings: calendar poller started")


async def stop_poller(app: web.Application) -> None:
    """``app.on_cleanup`` hook — cancel the loop on gateway shutdown."""
    global _poll_task
    task = _poll_task
    _poll_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover — defensive
        logger.debug("meetings: calendar poller shutdown raised", exc_info=True)


def is_running() -> bool:
    """Whether the loop is live. For the status surface and tests."""
    return _poll_task is not None and not _poll_task.done()


async def _poll_loop(app: web.Application) -> None:
    # The first tick is delayed so a calendar fetch is never part of gateway
    # startup; after that each tick decides how long to wait for the next, so a
    # cadence changed in Settings takes effect without a restart.
    delay = k.CALENDAR_POLL_STARTUP_DELAY_SECS
    while True:
        try:
            await asyncio.sleep(delay)
            delay = await poll_once(app)
        except asyncio.CancelledError:
            break
        except Exception:  # never let one bad cycle kill the loop
            logger.warning("meetings: calendar poll tick failed", exc_info=True)
            delay = k.CALENDAR_POLL_INTERVAL_SECS


async def poll_once(app: web.Application) -> int:
    """One tick: sync if configured, pre-create what is due. Returns the next delay.

    Public so a test (or a future "poll now" route) can drive a tick without
    the loop's sleeps.
    """
    root = app.get("_meetings_data_root")
    # Through `_common`, not imported by name: that is the seam the request gate
    # reads and the one the test fixtures stub, so the poller and the routes
    # always agree on whether the app is on.
    if not await asyncio.to_thread(_common.is_app_enabled, k.APP_NAME):
        return k.CALENDAR_POLL_INTERVAL_SECS

    config = await asyncio.to_thread(store.read_config, root)
    settings = calendar_sync.calendar_settings(config)
    interval = int(settings["poll_interval_secs"])
    if not settings["auto_sync"] or settings["provider"] == k.CALENDAR_PROVIDER_NONE:
        return interval

    try:
        _provider_id, events = await calendar_sync.sync_calendar(root)
    except cal.CalendarError as exc:
        # Already audited by `sync_calendar`. Info, not warning: a calendar host
        # being briefly unreachable is routine, and the Sync button reports the
        # same message to the user on demand.
        logger.info("meetings: background calendar sync failed: %s", exc)
        return interval

    lead = int(settings["precreate_lead_minutes"])
    if lead > 0:
        created = await precreate_due_meetings(events, lead_minutes=lead, root=root)
        if created:
            logger.info("meetings: pre-created %d meeting(s) from the calendar", len(created))
    return interval


def due_events(
    events: list[dict[str, Any]], *, now: datetime, lead_minutes: int
) -> list[dict[str, Any]]:
    """The cached events whose meeting should exist right now. Pure.

    Due means: a TIMED event (all-day rows are skipped, see the module docstring)
    that starts within *lead_minutes* from *now*, or that has already started and
    has not ended — a poll that ran late must still prepare a meeting that is
    under way. An event with an unreadable ``start`` is skipped rather than
    guessed at.
    """
    horizon = now + timedelta(minutes=lead_minutes)
    due: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or event.get("all_day"):
            continue
        start = _parse_stamp(event.get("start"))
        if start is None or start > horizon:
            continue
        end = _parse_stamp(event.get("end"))
        if end is not None and end <= now:
            continue
        if end is None and start < now - timedelta(hours=1):
            # No end and long past its start: the `.ics` default duration is one
            # hour, so treat it as over rather than resurrecting stale rows.
            continue
        due.append(event)
    return due


def _parse_stamp(value: Any) -> datetime | None:
    """A cache timestamp (``%Y-%m-%dT%H:%M:%SZ``) as an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _precreate_one(meeting_id: str, title: str, root: Any) -> bool:
    """Create the meeting if it does not exist yet. BLOCKING; returns True if created.

    The existence check and the init run in ONE worker-thread hop, under the
    lifecycle lock held by the caller, so the answer to "did this tick create
    it" is exact rather than a before/after comparison across an ``await``.
    """
    if store.read_meeting_meta(meeting_id, root) is not None:
        return False
    meeting_lifecycle.init_meeting_blocking(meeting_id, title, {}, root)
    return True


async def precreate_due_meetings(
    events: list[dict[str, Any]], *, lead_minutes: int, root: Any
) -> list[str]:
    """Create a meeting directory for every due event that has none. Returns the ids created."""
    created: list[str] = []
    now = datetime.now(timezone.utc)
    for event in due_events(events, now=now, lead_minutes=lead_minutes):
        raw_id = str(event.get("event_id") or "")
        try:
            meeting_id = store.safe_meeting_id(raw_id)
        except store.MeetingsPathError:
            # The provider funnels every id through `_event_id_for`, so this is
            # a corrupt cache row rather than a reachable path; skip it, and let
            # the next sync rewrite the cache.
            logger.debug("meetings: skipping calendar row with unusable id %r", raw_id[:80])
            continue
        title = str(event.get("title") or "Meeting")[: k.MAX_TITLE_LEN]
        async with meeting_lifecycle.START_LOCK:
            was_created = await asyncio.to_thread(_precreate_one, meeting_id, title, root)
        if was_created:
            _common.audit("meetings.calendar_precreate", meeting_id, outcome="ok")
            created.append(meeting_id)
    return created

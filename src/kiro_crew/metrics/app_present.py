"""The daily ``kirocrew.app.present`` pass: one row per installed app per UTC day.

The gateway starts :func:`start` at boot (``slack/gateway_runtime/process_entry.py``),
beside the daily heartbeat; no request path runs it. The first pass runs
:data:`FIRST_PASS_DELAY_SECS` after start. After that the task wakes at least every
:data:`MAX_SLEEP_SECS` and just after each UTC midnight, so a gateway that runs for
days records once more on every new day.

The UTC day a pass last recorded is kept next to the heartbeat's own daily stamp
(``beacon.APP_PRESENT_STAMP_FILE``), so any number of restarts on one day record one
row per app. While the recorder is off (telemetry disabled, an opt-out env var, a
policy pin) a pass records nothing and writes no stamp, so a later pass that day
records once recording is back on.

A pass never raises: a failed listing is retried on the next wake, and a raising
recorder is caught in :func:`feature_usage._emit`, so the stamp is still written.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from kiro_crew.metrics import feature_usage

logger = logging.getLogger(__name__)

#: The first pass waits this long after gateway start, off the boot path.
FIRST_PASS_DELAY_SECS = 600.0
#: The longest wait between two checks of the stamp.
MAX_SLEEP_SECS = 3600.0

#: Strong reference to the running task (the loop holds tasks weakly).
_TASK: "asyncio.Task[None] | None" = None


def seconds_until_next_check(now: datetime | None = None) -> float:
    """Seconds until the next check: one second past the next UTC midnight, at most an hour."""
    now = now or datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return min(MAX_SLEEP_SECS, (midnight - now).total_seconds() + 1.0)


def _status(app: dict[str, Any]) -> str:
    enabled = app.get("enabled")
    if enabled is True:
        return "enabled"
    if enabled is False:
        return "disabled"
    return feature_usage.OTHER


def _backend(app: dict[str, Any], running: set[Any]) -> str:
    manifest = app.get("manifest")
    backend = manifest.get("backend") if isinstance(manifest, dict) else None
    if not isinstance(backend, dict) or not backend.get("entryPoint"):
        return "none"
    return "running" if app.get("name") in running else "stopped"


def record_today() -> int:
    """Record one row per installed app unless today's rows are recorded; the count recorded.

    Reads the install records: call off the event loop. Never raises.
    """
    try:
        from kiro_crew import beacon

        day = beacon.app_present_due_day()
        if not day:
            return 0
        from kiro_crew.metrics.provider import get_recorder

        if not get_recorder().enabled:
            return 0
        from kiro_crew.apps.backend import list_app_processes
        from kiro_crew.apps.manager import list_apps

        ids = feature_usage.refresh_public_app_ids()
        apps = list_apps()
        running = {p.get("app_name") for p in list_app_processes() if p.get("running")}
    except Exception:
        logger.debug("app presence pass failed; retrying on the next wake", exc_info=True)
        return 0
    for app in apps:
        feature_usage.record_app_present(app.get("name"), _status(app), _backend(app, running), ids)
    beacon.mark_app_present_recorded(day)
    return len(apps)


async def run_daily() -> None:
    """Run :func:`record_today` after the first delay, then on every wake, until cancelled."""
    await asyncio.sleep(FIRST_PASS_DELAY_SECS)
    while True:
        try:
            await asyncio.to_thread(record_today)
        except Exception:
            logger.debug("app presence pass failed", exc_info=True)
        await asyncio.sleep(seconds_until_next_check())


def start() -> None:
    """Start the daily task on the running loop; never raises."""
    global _TASK
    try:
        _TASK = asyncio.get_running_loop().create_task(run_daily(), name="app-present-daily")
    except Exception:
        logger.debug("app presence task did not start", exc_info=True)

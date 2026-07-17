"""Periodic watchdog that detects stale/missing dashboard assets.

When an update prunes kirocrew, the running gateway's install directory is
pruned — the process keeps running but its static assets are gone. The gateway
then serves the fallback page (see ``DASHBOARD_HTML_NOT_FOUND_MARKER`` in
``handlers/core.py``) and rejects freshly minted tokens (signing key mismatch).
External clients can kill and restart the process, but detection can take
minutes and a forced restart may fail on slow cold starts.

This watchdog runs inside the gateway itself and catches the problem at the
source: if the dashboard static bundle is missing, log a CRITICAL warning
and initiate graceful shutdown so a supervisor (systemd, launchd) can
restart a fresh process immediately.

The check is cheap (one Path.is_file() + one Path.is_file() — no I/O beyond
stat()) and runs every 60 seconds by default. It only arms itself if assets
are present at startup — a dev/source install that never built its frontend
won't be killed (the watchdog detects "assets vanished", not "assets never
existed").

The presence check mirrors ``handlers/core.py:index()``'s serve criterion
exactly (``dist/index.html`` is a file — the legacy ``dashboard.html`` fallback
was removed, Talos V2285871874), so a partial-prune state where an empty
``dist/`` directory node remains cannot mask a genuine vanish.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from kiro_crew.dashboard.handlers.core import _DIST_INDEX

logger = logging.getLogger(__name__)

# Default check interval (seconds). Long enough to be negligible overhead,
# short enough that an update mid-session is caught within a minute.
_CHECK_INTERVAL_SECS = 60

# Delay before re-checking a failed sample (seconds). A frontend rebuild in a
# source install deletes and recreates static/dist/ in well under this window;
# a genuine update prune is permanent, so the confirmation only adds this
# much detection latency.
_CONFIRM_DELAY_SECS = 2.0


class _ShutdownSignal(Protocol):
    """Minimal contract we need from a shutdown-signalling event."""

    def is_set(self) -> bool:
        ...

    def set(self) -> None:
        ...

    async def wait(self) -> bool:
        ...


def assets_present() -> bool:
    """Return True if the dashboard can serve a real page (not the fallback).

    Mirrors the criterion used by ``handlers/core.py:index()``: the React
    bundle's ``dist/index.html`` must be present (the legacy ``dashboard.html``
    fallback was removed — Talos V2285871874). Checking ``_DIST_INDEX.is_file()``
    (not ``_DIST_DIR.is_dir()``) is critical: an empty ``dist/`` directory node
    is a valid partial-prune state where the handler serves the guidance page,
    and the watchdog must recognise that as "assets vanished."
    """
    return _DIST_INDEX.is_file()


async def run_stale_asset_watchdog(
    shutdown_event: _ShutdownSignal,
    *,
    interval: float = _CHECK_INTERVAL_SECS,
    confirm_delay: float = _CONFIRM_DELAY_SECS,
) -> None:
    """Background loop: check asset presence, trigger shutdown if stale.

    Only arms if assets are present at startup. A fresh source/dev install
    that never built its frontend will NOT be killed — the watchdog
    specifically detects "assets were here and then vanished" (the update
    scenario), not "assets never existed."

    A failed check is re-confirmed after ``confirm_delay`` seconds before
    shutdown is triggered, so a transient asset gap (e.g. a frontend rebuild
    that deletes and recreates ``static/dist/``) that coincides with a tick
    cannot kill an otherwise-healthy gateway. A genuine update prune is
    permanent and still shuts down within one confirmation delay.

    Parameters
    ----------
    shutdown_event:
        The gateway's global shutdown event. Setting it initiates graceful
        shutdown (same as SIGTERM).
    interval:
        Seconds between checks. Default 60s.
    confirm_delay:
        Seconds to wait before re-checking a failed sample. Default 2s.
    """
    if not assets_present():
        # Assets were never here — this is likely a dev/source install that
        # hasn't built its frontend yet. Don't arm the watchdog; let the
        # gateway serve the fallback page as it always has.
        logger.info(
            "Stale-asset watchdog: assets not present at startup — "
            "not arming (dev/source install without a built frontend)."
        )
        return

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass

        if not assets_present():
            # Re-confirm after a short delay: a frontend rebuild in a source
            # install deletes and recreates static/dist/, and an unlucky tick
            # inside that window must not kill a healthy gateway. An update
            # prune is permanent, so it still fails the second check. Wait
            # on shutdown_event so an external SIGTERM interrupts promptly.
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=confirm_delay
                )
                return
            except asyncio.TimeoutError:
                pass
            if assets_present():
                logger.warning(
                    "Stale-asset watchdog: assets briefly missing but "
                    "reappeared — likely a frontend rebuild; not shutting "
                    "down."
                )
                continue
            if shutdown_event.is_set():
                # Someone else shut us down during the confirm window; don't
                # log a misleading "watchdog fired" CRITICAL.
                return
            logger.critical(
                "Dashboard static assets vanished — an update likely "
                "pruned the running install. Initiating graceful shutdown "
                "so a supervisor can restart a fresh gateway."
            )
            shutdown_event.set()
            return

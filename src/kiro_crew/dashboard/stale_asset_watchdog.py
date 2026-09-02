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
exactly (``dist/index.html`` is a file — there is no ``dashboard.html``
fallback), so a partial-prune state where an empty
``dist/`` directory node remains cannot mask a genuine vanish.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
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

# Max time to let in-flight backend turns finish before forcing shutdown after
# a confirmed asset vanish. An update prune only breaks static-asset serving —
# live ACP turns keep working — so draining lets active turns complete (result
# captured, history saved) instead of being killed mid-prompt when the
# supervisor restarts. Bounded so a wedged turn can't defer the restart forever.
_DRAIN_TIMEOUT_SECS = 120.0
# Poll cadence while draining (seconds). Also the max latency to react to an
# external SIGTERM arriving mid-drain.
_DRAIN_POLL_SECS = 2.0


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
    bundle's ``dist/index.html`` must be present (there is no ``dashboard.html``
    fallback). Checking ``_DIST_INDEX.is_file()``
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
    count_in_flight: Callable[[], int] | None = None,
    drain_timeout: float = _DRAIN_TIMEOUT_SECS,
    drain_poll: float = _DRAIN_POLL_SECS,
) -> None:
    """Background loop: check asset presence, trigger shutdown if stale.

    Only arms if assets are present at startup. A fresh source/dev install
    that never built its frontend will NOT be killed — the watchdog
    specifically detects "assets were here and then vanished" (the update
    scenario), not "assets never existed."

    A failed check is re-confirmed after ``confirm_delay`` seconds before
    shutdown is triggered, so a transient asset gap (e.g. a frontend rebuild
    that deletes and recreates ``static/dist/``) that coincides with a tick
    cannot kill an otherwise-healthy gateway. Presence is re-checked once more
    once in-flight work has drained, covering a rebuild that outlives the
    confirmation but finishes while turns are still draining; on an idle
    gateway there is nothing to drain and that re-check adds no grace. A
    genuine update prune is permanent and fails every check, so it still shuts
    down.

    Once a vanish is confirmed, in-flight backend work is *drained* before the
    shutdown event is set (see ``_drain_in_flight``): the prune only breaks
    static-asset serving, so active ACP turns can finish rather than being
    killed mid-prompt when the supervisor restarts a fresh process.

    Parameters
    ----------
    shutdown_event:
        The gateway's global shutdown event. Setting it initiates graceful
        shutdown (same as SIGTERM).
    interval:
        Seconds between checks. Default 60s.
    confirm_delay:
        Seconds to wait before re-checking a failed sample. Default 2s.
    count_in_flight:
        Optional callable returning the number of in-flight backend tasks
        (active provider turns, Slack session turns). When provided, the
        watchdog waits for it to reach zero — bounded by ``drain_timeout`` —
        before triggering shutdown. ``None`` disables draining (shut down
        immediately on vanish).
    drain_timeout:
        Max seconds to wait for in-flight work to finish. Default 120s.
    drain_poll:
        Seconds between in-flight re-counts while draining. Default 2s.
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
            await _drain_in_flight(
                shutdown_event,
                count_in_flight,
                drain_timeout=drain_timeout,
                drain_poll=drain_poll,
            )
            if shutdown_event.is_set():
                # An external SIGTERM arrived during the drain window and has
                # already begun graceful shutdown — don't double-signal.
                return
            # Re-check once more now that in-flight work has drained. This
            # covers a rebuild that outlives the confirmation but finishes
            # while turns are still draining; it adds no grace on an idle
            # gateway, where _drain_in_flight returns immediately. Without it a
            # drain long enough for the assets to reappear still ends in a
            # shutdown — the healthy-gateway kill the confirmation exists to
            # prevent.
            if assets_present():
                logger.warning(
                    "Stale-asset watchdog: assets reappeared while draining "
                    "in-flight work — likely a slow frontend rebuild; not "
                    "shutting down."
                )
                continue
            logger.critical(
                "Dashboard static assets vanished — an update likely "
                "pruned the running install. Initiating graceful shutdown "
                "so a supervisor can restart a fresh gateway."
            )
            shutdown_event.set()
            return


async def _drain_in_flight(
    shutdown_event: _ShutdownSignal,
    count_in_flight: Callable[[], int] | None,
    *,
    drain_timeout: float,
    drain_poll: float,
) -> None:
    """Wait (bounded) for in-flight backend turns to finish before shutdown.

    An update prune breaks only static-asset serving; live ACP turns keep
    working. Draining lets active turns complete (result captured, history
    saved) instead of being killed mid-prompt when the supervisor restarts —
    directly preventing the "❌ lost to gateway restart / no result captured"
    orphaning seen on an abrupt prune.

    Returns as soon as any of the following is true:
      * there is no in-flight work,
      * the ``drain_timeout`` elapses (remaining tasks are snapshotted for
        resume by the normal shutdown path), or
      * an external shutdown is signalled mid-drain (SIGTERM wins).

    Any failure to count in-flight work is treated as "idle" — a broken
    predicate must never wedge shutdown.
    """
    if count_in_flight is None or drain_timeout <= 0:
        return
    try:
        pending = count_in_flight()
    except Exception:
        logger.debug(
            "Stale-asset watchdog: initial in-flight count failed — "
            "skipping drain.",
            exc_info=True,
        )
        return
    if pending <= 0:
        return

    logger.warning(
        "Stale-asset watchdog: draining %d in-flight task(s) before shutdown "
        "(up to %.0fs)…",
        pending,
        drain_timeout,
    )
    loop = asyncio.get_event_loop()
    deadline = loop.time() + drain_timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        # Sleep interruptibly: an external SIGTERM sets shutdown_event and
        # wakes us immediately so we don't keep draining past a real shutdown.
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=min(drain_poll, remaining)
            )
            logger.warning(
                "Stale-asset watchdog: external shutdown during drain — "
                "stopping drain."
            )
            return
        except asyncio.TimeoutError:
            pass
        try:
            pending = count_in_flight()
        except Exception:
            logger.debug(
                "Stale-asset watchdog: in-flight count failed mid-drain — "
                "proceeding with shutdown.",
                exc_info=True,
            )
            return
        if pending <= 0:
            logger.warning(
                "Stale-asset watchdog: all in-flight tasks drained — "
                "proceeding with shutdown."
            )
            return

    logger.warning(
        "Stale-asset watchdog: drain timeout (%.0fs) elapsed with %d task(s) "
        "still in flight — proceeding with shutdown; open sessions resume "
        "from snapshot on restart.",
        drain_timeout,
        pending,
    )

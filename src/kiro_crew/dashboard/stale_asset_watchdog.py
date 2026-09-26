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

# Max time the watchdog will stand down for a gateway-hosted update apply
# (seconds). The stand-down exists because our OWN installer deletes the assets
# it is replacing, so a vanish during an apply we launched is expected state.
# It is BOUNDED on purpose: an apply that never reports finishing (a wedged
# installer, a lost `finally`) must not silently disable the watchdog forever.
# Comfortably above the installer subprocess's own 300s timeout, so a healthy
# apply always completes inside the window and only a genuinely stuck one is
# overridden.
_UPDATE_SUPPRESS_MAX_SECS = 600.0

# Process exit status the gateway uses when THIS watchdog initiated the
# shutdown. Non-zero on purpose: the whole point of the shutdown is to be
# restarted by a supervisor, and ``Restart=on-failure`` (systemd) /
# ``KeepAlive.SuccessfulExit=false`` (launchd) style policies only relaunch a
# process that did NOT exit 0. A unit generated before ``Restart=always``
# landed, or one an operator hand-edited back to ``on-failure``, would
# otherwise treat the clean exit as "done" and leave the gateway down until
# a human notices. 75 is ``EX_TEMPFAIL`` from ``sysexits.h`` ("temporary
# failure; retry later"), the closest standard meaning to "restart me".
STALE_ASSET_EXIT_CODE = 75


def shutdown_exit_code(watchdog: "asyncio.Future[bool] | None") -> int:
    """Map the watchdog task's outcome onto the gateway's process exit status.

    ``STALE_ASSET_EXIT_CODE`` iff the watchdog has finished and reported
    ``True`` (it confirmed a vanish and set the shutdown event itself). Every
    other state is 0: no watchdog, still running (the event was set by
    SIGTERM/``systemctl stop`` while it slept), cancelled, or crashed — a
    crashed watchdog must not turn an operator's stop into a restart.
    """
    if watchdog is None or not watchdog.done() or watchdog.cancelled():
        return 0
    try:
        fired = watchdog.result()
    except Exception:
        logger.debug("Stale-asset watchdog task raised", exc_info=True)
        return 0
    return STALE_ASSET_EXIT_CODE if fired else 0


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
    update_in_progress: Callable[[], bool] | None = None,
    update_suppress_max: float = _UPDATE_SUPPRESS_MAX_SECS,
) -> bool:
    """Background loop: check asset presence, trigger shutdown if stale.

    Returns ``True`` iff this watchdog is the one that set ``shutdown_event``
    (a confirmed asset vanish). Every other exit — never armed, or
    ``shutdown_event`` set externally by SIGTERM/``systemctl stop`` — returns
    ``False``. The gateway maps ``True`` onto :data:`STALE_ASSET_EXIT_CODE` so
    the process exits non-zero and a restart-on-failure supervisor relaunches
    it, while an operator-initiated stop still exits 0 and stays stopped.

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

    A vanish caused by an update *this gateway itself launched* is not evidence
    of an external prune at all — the in-place installer deletes the assets it
    is replacing. ``update_in_progress`` lets the gateway say so, and while it
    reports True the watchdog stands down instead of shutting down. Without it
    the watchdog shuts the gateway down mid-install, and that shutdown cancels
    the installer task, which kills the installer before it writes its console
    scripts — the gateway destroying its own install. The stand-down is bounded
    by ``update_suppress_max`` so an apply that never reports finishing cannot
    disable the watchdog indefinitely.

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
    update_in_progress:
        Optional predicate reporting whether a gateway-hosted update apply is
        currently mutating this install's own tree. While it returns True a
        missing asset bundle is expected state, so the watchdog stands down
        rather than triggering shutdown. ``None`` disables the stand-down
        (historical behaviour). A predicate that raises is treated as "not
        updating" — a broken predicate must never wedge shutdown.
    update_suppress_max:
        Max seconds of continuous stand-down before the watchdog overrides
        ``update_in_progress`` and shuts down anyway. Default 600s.
    """
    if not assets_present():
        # Assets were never here — this is likely a dev/source install that
        # hasn't built its frontend yet. Don't arm the watchdog; let the
        # gateway serve the fallback page as it always has.
        logger.info(
            "Stale-asset watchdog: assets not present at startup — "
            "not arming (dev/source install without a built frontend)."
        )
        return False

    loop = asyncio.get_running_loop()
    suppressed_since: float | None = None

    def _update_owns_the_gap() -> bool:
        """Report whether OUR OWN update apply explains the missing assets.

        True means stand down: the installer we launched is replacing the tree
        we serve from, so the gap is expected and a shutdown here would cancel
        that installer mid-write. Bounded by ``update_suppress_max`` — a
        stand-down that outlives any healthy apply is overridden so a wedged
        update cannot switch the watchdog off for the rest of the process's
        life.
        """
        nonlocal suppressed_since
        if update_in_progress is None:
            return False
        try:
            applying = bool(update_in_progress())
        except Exception:
            # Same doctrine as count_in_flight: a broken predicate must never
            # wedge shutdown, so fail towards the watchdog's normal behaviour.
            logger.debug(
                "Stale-asset watchdog: update-in-progress predicate failed — "
                "treating as 'not updating'.",
                exc_info=True,
            )
            suppressed_since = None
            return False
        if not applying:
            suppressed_since = None
            return False
        now = loop.time()
        if suppressed_since is None:
            suppressed_since = now
            logger.warning(
                "Stale-asset watchdog: assets missing while an update apply "
                "this gateway launched is in flight — expected state, not an "
                "external prune; standing down for up to %.0fs.",
                update_suppress_max,
            )
            return True
        held = now - suppressed_since
        if held >= update_suppress_max:
            logger.warning(
                "Stale-asset watchdog: an update apply has claimed the asset "
                "gap for %.0fs (ceiling %.0fs) — overriding the stand-down so "
                "a stuck update cannot disable the watchdog.",
                held,
                update_suppress_max,
            )
            return False
        return True

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return False
        except asyncio.TimeoutError:
            pass

        if not assets_present():
            if _update_owns_the_gap():
                continue
            # Re-confirm after a short delay: a frontend rebuild in a source
            # install deletes and recreates static/dist/, and an unlucky tick
            # inside that window must not kill a healthy gateway. An update
            # prune is permanent, so it still fails the second check. Wait
            # on shutdown_event so an external SIGTERM interrupts promptly.
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=confirm_delay
                )
                return False
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
                return False
            await _drain_in_flight(
                shutdown_event,
                count_in_flight,
                drain_timeout=drain_timeout,
                drain_poll=drain_poll,
            )
            if shutdown_event.is_set():
                # An external SIGTERM arrived during the drain window and has
                # already begun graceful shutdown — don't double-signal.
                return False
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
            # Re-ask right before signalling: an apply can begin inside the
            # confirm or drain window, and shutting down then is the exact
            # self-inflicted kill this guard exists to prevent.
            if _update_owns_the_gap():
                continue
            logger.critical(
                "Dashboard static assets vanished — an update likely "
                "pruned the running install. Initiating graceful shutdown "
                "so a supervisor can restart a fresh gateway."
            )
            shutdown_event.set()
            return True
        else:
            # Healthy sample — drop any stand-down window we were tracking so a
            # later apply gets a full ceiling rather than inheriting this one.
            suppressed_since = None
    # Loop never entered: the event was already set when the watchdog armed.
    return False


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

"""Periodic watchdog that detects stale/missing dashboard assets.

When an update prunes kirocrew, the running gateway's install directory is
pruned: the process keeps running but its static assets are gone. The gateway
then serves the fallback page (see ``DASHBOARD_HTML_NOT_FOUND_MARKER`` in
``handlers/core.py``) and rejects freshly minted tokens (signing key mismatch).

This watchdog runs inside the gateway itself and catches the problem at the
source. It does NOT exit the gateway: a running gateway is never restarted by
an unattended path (the decision entry "updates never restart a running
gateway" in ``docs/decisions``). It
keeps serving, logs one CRITICAL line, and records "update ready, restart to use
it" (``update_restart_ready``), which the dashboard shows as a notice. The next
restart a person chooses brings in a fresh process. A gap an update step this
gateway is running owns (``update_ownership``) is left alone, since the step is
what will close it.

``KIROCREW_AUTO_RESTART=0`` and an ``auto-restart.disabled`` file in the state
directory the edition passes to :func:`set_auto_restart_state_dir` are the
operator's opt-out from automatic restarts. The watchdog restarts nothing, so
the opt-out changes nothing it does; it is read here so the edition's restarter
and this watchdog answer from one place (:func:`auto_restart_disabled`), and the
log line says which way it is set.

The check is cheap (one ``Path.is_file()``) and runs every 60 seconds by
default. It only arms itself if assets are present at startup: a dev/source
install that never built its frontend is not told anything (the watchdog
detects "assets vanished", not "assets never existed").

The presence check mirrors ``handlers/core.py:index()``'s serve criterion
exactly (``dist/index.html`` is a file, there is no ``dashboard.html``
fallback), so a partial-prune state where an empty ``dist/`` directory node
remains cannot mask a genuine vanish.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from kiro_crew import update_ownership, update_restart_ready
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

#: The operator's opt-out from automatic restarts: this variable set to ``0``, or
#: this file in the edition's state directory.
AUTO_RESTART_ENV = "KIROCREW_AUTO_RESTART"
AUTO_RESTART_OFF_FILE = "auto-restart.disabled"

# Process exit status the watchdog used when IT initiated a shutdown. It no
# longer does, so ``shutdown_exit_code`` reads 0 here; the name stays because the
# gateway's exit path combines it with the other self-initiated shutdowns.
STALE_ASSET_EXIT_CODE = 75

_auto_restart_state_dir: Path | None = None


def set_auto_restart_state_dir(state_dir: str | os.PathLike[str] | None) -> None:
    """Name the directory whose ``auto-restart.disabled`` file turns automatic restart off.

    The hook the edition sets once at boot with its own state directory; core
    has no state directory of its own to look in. ``None`` clears it.
    """
    global _auto_restart_state_dir
    _auto_restart_state_dir = Path(state_dir) if state_dir else None


def auto_restart_disabled() -> bool:
    """Whether the operator turned automatic restart off (the variable, or the file)."""
    if os.environ.get(AUTO_RESTART_ENV, "").strip() == "0":
        return True
    state_dir = _auto_restart_state_dir
    if state_dir is None:
        return False
    try:
        return (state_dir / AUTO_RESTART_OFF_FILE).exists()
    except OSError:
        # An unreadable state directory is not an opt-out nobody wrote.
        return False


def shutdown_exit_code(watchdog: "asyncio.Future[bool] | None") -> int:
    """Map the watchdog task's outcome onto the gateway's process exit status.

    :data:`STALE_ASSET_EXIT_CODE` iff the watchdog finished and reported
    ``True``. It reports ``True`` only when it set the shutdown event itself,
    which it never does, so this is 0 in every state: no watchdog, still
    running, cancelled, crashed, or finished. An operator's stop stays a stop.
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

    def is_set(self) -> bool: ...

    async def wait(self) -> bool: ...


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
    on_change: Callable[[], None] | None = None,
) -> bool:
    """Background loop: check asset presence and say so when the bundle is gone.

    Returns ``False`` on every exit (never armed, or ``shutdown_event`` set by
    SIGTERM/``systemctl stop``): the watchdog never signals a shutdown itself.

    Only arms if assets are present at startup. A fresh source/dev install
    that never built its frontend is NOT told anything; the watchdog
    specifically detects "assets were here and then vanished" (the update
    scenario), not "assets never existed."

    A failed check is re-confirmed after ``confirm_delay`` seconds before it is
    reported, so a transient asset gap (a frontend rebuild that deletes and
    recreates ``static/dist/``) that coincides with a tick is not read as an
    update. A genuine prune is permanent and fails every check.

    A confirmed vanish records "update ready, restart to use it" and calls
    ``on_change`` so the dashboard learns of it at once; the record is dropped
    when a later sample finds the bundle again. A bundle missing while an update
    step this gateway is running owns the gap (``update_ownership``) is waited
    out: the owner is read on every missing sample and once more as the last
    thing before reporting, with no await in between. A bundle missing at
    startup while a step owns it is waited out rather than read as a dev
    install; if it is still missing once that step ends, the update made the
    gap, so the watchdog arms and reports it like any other vanish.
    """
    announced: str | None = None

    def _owner() -> str | None:
        nonlocal announced
        owner = update_ownership.current_owner()
        if owner is not None and owner != announced:
            announced = owner
            logger.warning(
                "Stale-asset watchdog: assets missing while %s owns the gap; standing down.",
                owner,
            )
        return owner

    def _changed() -> None:
        if on_change is None:
            return
        try:
            on_change()
        except Exception:
            logger.debug("Stale-asset watchdog: change callback failed", exc_info=True)

    present = assets_present()
    owner_seen = False
    while not present and _owner() is not None:
        # An update started at boot can have the bundle missing before the
        # watchdog arms: wait for it rather than disarm as a dev install.
        owner_seen = True
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return False
        except asyncio.TimeoutError:
            pass
        present = assets_present()
    if not present and not owner_seen:
        # Assets were never here: likely a dev/source install that hasn't built
        # its frontend yet. Don't arm the watchdog; the gateway serves the
        # fallback page as it always has.
        logger.info(
            "Stale-asset watchdog: assets not present at startup — "
            "not arming (dev/source install without a built frontend)."
        )
        return False

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return False
        except asyncio.TimeoutError:
            pass

        if assets_present():
            # A healthy sample ends the gap: a later one is announced afresh.
            announced = None
            if update_restart_ready.clear(update_restart_ready.REASON_ASSETS_GONE):
                logger.info("Stale-asset watchdog: assets are back; the restart notice is cleared.")
                _changed()
            continue
        if _owner() is not None:
            continue
        # Re-confirm after a short delay: a frontend rebuild deletes and recreates
        # static/dist/, and an unlucky tick inside that window is not an update.
        # Wait on shutdown_event so an external SIGTERM interrupts promptly.
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=confirm_delay)
            return False
        except asyncio.TimeoutError:
            pass
        if assets_present():
            logger.warning(
                "Stale-asset watchdog: assets briefly missing but reappeared, "
                "likely a frontend rebuild; nothing to report."
            )
            continue
        # With no await in between: an update step can have started inside the
        # confirm window.
        if _owner() is not None:
            continue
        if update_restart_ready.mark(update_restart_ready.REASON_ASSETS_GONE):
            logger.critical(
                "Dashboard static assets vanished: an update likely pruned the "
                "running install. The gateway keeps serving; restart it to use the "
                "new version.%s",
                " Automatic restart is turned off." if auto_restart_disabled() else "",
            )
            _changed()
    # Loop never entered: the event was already set when the watchdog armed.
    return False

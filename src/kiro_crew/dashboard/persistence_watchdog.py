"""Periodic watchdog that detects a data home that has stopped persisting.

The boot preflight (``platform_compat.probe_file_persistence``) refuses to start
a gateway whose data home cannot create, lock, replace and remove files -- the
known cause being a seccomp syscall filter inherited from a sandboxed parent,
which turns basic file syscalls into ``ENOSYS`` while ``stat`` keeps working.
But that preflight runs ONCE, at boot. A filter whose sandbox is still alive at
startup passes the probe legitimately -- the syscalls genuinely work at that
moment -- and breaks minutes later when the sandbox is torn down underneath the
process.

From then on the gateway is in a near-silent, indefinite failure:

* every file ``open`` fails with ``ENOSYS``, so ``aiohttp``'s ``FileResponse``
  turns every static asset into a bare 404 (it swallows the ``OSError``) and the
  SPA never boots;
* ``GET /`` keeps serving a correct ``index.html`` already in memory and
  ``/api/health`` touches no files, so both stay green;
* file logging dies of the same ``ENOSYS`` and drops every record;
* the process never exits, so ``Restart=on-failure`` never fires.

A ``stat``-based check (``stale_asset_watchdog``) cannot see this: the build
tree is still present and ``resolve(strict=True)`` still succeeds. Only an
actual open/write/lock -- exactly what the boot probe does -- distinguishes "the
filesystem went away under us" from "this file is genuinely missing".

This watchdog re-runs that same probe periodically. On a failure re-confirmed a
short delay later it logs loudly to stderr AND the logger (file logging is
probably dead, so stderr/journal is the only surface that survives). When the
failure is the restart-curable ``ENOSYS`` case it then sets the gateway's
shutdown event so the process exits non-zero and a restart-on-failure
supervisor relaunches a fresh gateway with no inherited filter.

Why a shutdown rather than a repair: a seccomp filter cannot be removed from a
live process, so re-staging the frontend, ``systemctl reload`` and the UI's own
"Clear cache and retry" are all incapable of recovering it. Only a new process
clears the filter, and only a non-zero exit makes a supervisor start one.

Two honesty caveats this watchdog respects:

* A failure a restart cannot cure (a full disk ``ENOSPC``, a read-only remount
  ``EROFS``, an exceeded quota) is logged loudly but does NOT exit: a relaunch
  would meet the identical boot preflight and crash-loop, whereas staying up
  keeps serving reads from memory until the condition clears. Only the
  ``ENOSYS`` class self-terminates.
* The inherited filter only reaches a gateway started from inside a sandboxed
  session (e.g. by hand with ``nohup``), which may have no supervisor to
  relaunch it. For that gateway the non-zero exit still beats a silent zombie:
  it stops loudly instead of serving a dead dashboard indefinitely. A gateway
  started by systemd/launchd never inherits the filter in the first place but
  gets the clean relaunch when any other persistence fault turns restart-curable.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of one persistence probe.

    ``reason`` is a human-readable description of the first failing primitive.
    ``restartable`` is ``True`` only when a fresh process would plausibly cure
    the failure -- i.e. the ``ENOSYS`` case, where the running process inherited
    a seccomp filter from a torn-down sandbox and a relaunch clears it. It is
    ``False`` for failures a restart cannot fix (a full disk ``ENOSPC``, a
    read-only remount ``EROFS``, an exceeded quota ``EDQUOT``): exiting on those
    would only crash-loop against the same boot preflight while the gateway
    could otherwise keep serving reads from memory.
    """

    reason: str
    restartable: bool


# Default check interval (seconds). Long enough to be negligible overhead (one
# mkstemp/write/flock/replace/unlink cycle per tick, off the event loop), short
# enough that a mid-life teardown is caught within a minute rather than lingering
# undetected for days.
_CHECK_INTERVAL_SECS = 30.0

# Delay before re-confirming a failed probe (seconds). A single probe can lose a
# race against a transient filesystem hiccup (an NFS reconnect, a momentary
# ENOSPC that clears); a genuinely-broken data home fails the re-probe too. The
# boot preflight has no equivalent because at boot a single failure is already
# disqualifying, but here a healthy gateway must not be killed by one unlucky
# sample.
_CONFIRM_DELAY_SECS = 2.0

# Process exit status when THIS watchdog initiated the shutdown. Non-zero on
# purpose: the whole point of the shutdown is to be relaunched by a supervisor,
# and ``Restart=on-failure`` (systemd) / ``KeepAlive`` (launchd) only relaunch a
# process that did NOT exit 0. 72 is ``EX_OSFILE`` from ``sysexits.h`` ("critical
# OS file is inaccessible") -- the data home the process needs to persist state
# cannot be read or written -- and is deliberately NOT 78: the service
# unit sets ``RestartPreventExitStatus=78`` (``LIVE_HOLDER_EXIT_CODE`` in
# ``gateway_lock``) to make the "another gateway already serves this home" exit
# TERMINAL, so a persistence watchdog exiting 78 would never be relaunched --
# the exact opposite of what this fix needs. 72 is restarted like the listener
# guard's 69 and the stale-asset watchdog's 75, and a journal histogram tells
# all four exits apart (69 listener lost, 72 persistence lost, 75 stale assets,
# 78 live-holder terminal).
PERSISTENCE_LOST_EXIT_CODE = 72

# A loud banner prefix for the stderr line, mirroring the cron child's
# ``CHILD_PERSISTENCE_PREFIX`` so operators grepping journals see one shape for
# both. File logging is probably dead by the time we print this, so stderr is
# the surface that survives.
_STDERR_PREFIX = "\u274c Gateway data home stopped persisting: "


class _ShutdownSignal(Protocol):
    """Minimal contract the watchdog needs from a shutdown-signalling event."""

    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    async def wait(self) -> bool: ...


def shutdown_exit_code(watchdog: "asyncio.Future[bool] | None") -> int:
    """Map the watchdog task's outcome onto the gateway's process exit status.

    ``PERSISTENCE_LOST_EXIT_CODE`` iff the watchdog has finished and reported
    ``True`` (it confirmed the data home stopped persisting and set the shutdown
    event itself). Every other state is 0: no watchdog, still running (the event
    was set by SIGTERM/``systemctl stop`` while it slept), cancelled, or crashed
    -- a crashed watchdog must not turn an operator's stop into a restart.
    """
    if watchdog is None or not watchdog.done() or watchdog.cancelled():
        return 0
    try:
        fired = watchdog.result()
    except Exception:
        logger.debug("Persistence watchdog task raised", exc_info=True)
        return 0
    return PERSISTENCE_LOST_EXIT_CODE if fired else 0


async def run_persistence_watchdog(
    shutdown_event: _ShutdownSignal,
    probe: Callable[[], Awaitable[ProbeResult | None]],
    *,
    interval: float = _CHECK_INTERVAL_SECS,
    confirm_delay: float = _CONFIRM_DELAY_SECS,
) -> bool:
    """Background loop: re-run the persistence probe, shut down if it fails.

    Returns ``True`` iff this watchdog is the one that set ``shutdown_event``
    (a confirmed, re-confirmed persistence failure). Every other exit -- an
    external SIGTERM/``systemctl stop`` setting the event while it slept, or a
    cancellation -- returns ``False``. The gateway maps ``True`` onto
    :data:`PERSISTENCE_LOST_EXIT_CODE` so the process exits non-zero and a
    restart-on-failure supervisor relaunches it, while an operator stop still
    exits 0 and stays stopped.

    ``probe`` is an async callable returning ``None`` when the data home still
    persists and a :class:`ProbeResult` otherwise -- the gateway passes an
    off-loop wrapper of ``platform_compat.probe_file_persistence``, the exact
    check the boot preflight uses. A failed probe is re-confirmed after
    ``confirm_delay`` before acting, so a transient hiccup cannot take down a
    healthy gateway; a genuine seccomp teardown fails the re-probe too.

    The watchdog self-terminates ONLY when the re-confirmed failure is
    ``restartable`` (the ``ENOSYS`` seccomp-teardown case a fresh process
    cures). A non-restartable failure (a full disk, a read-only remount, an
    exceeded quota) is logged loudly but does NOT exit: a restart would fail the
    identical boot preflight and crash-loop, whereas staying up keeps the
    gateway serving reads from memory until the condition clears.

    Both waits are interruptible by ``shutdown_event`` so an operator stop
    (SIGTERM / ``systemctl stop``) is honoured promptly -- the same
    ``asyncio.wait_for(shutdown_event.wait(), timeout=...)`` idiom
    ``stale_asset_watchdog`` uses. The periodic cadence alone catches the broken
    state within one ``interval``; detection is not coupled to the request path,
    so an idle gateway recovers just as a busy one does.
    """
    # The reason last announced on the stay-up path. A fault that persists
    # across ticks is announced once; the next announcement needs a different
    # reason or a healthy probe in between.
    announced_reason: str | None = None
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return False  # operator stop during the periodic sleep
        except asyncio.TimeoutError:
            pass  # interval elapsed -- time to probe

        first = await probe()
        if first is None:
            announced_reason = None
            continue

        # One failure might be a transient hiccup. Re-confirm after a short
        # delay before acting; the wait is interruptible by an operator stop.
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=confirm_delay)
            return False  # operator stop during the confirm window
        except asyncio.TimeoutError:
            pass
        confirmed = await probe()
        if shutdown_event.is_set():
            return False  # operator stop while the confirming probe ran
        if confirmed is None:
            announced_reason = None
            logger.warning(
                "Persistence watchdog: data home briefly failed a probe but "
                "recovered on re-check (%s) -- not shutting down.",
                first.reason,
            )
            continue

        if not confirmed.restartable:
            # A full disk / read-only mount / quota: a restart cannot cure it
            # and would crash-loop the identical boot preflight. Say so once
            # per distinct reason, keep serving, and let the next tick re-check.
            if confirmed.reason != announced_reason:
                _announce_unrecoverable(confirmed.reason)
                announced_reason = confirmed.reason
            continue

        _announce_failure(confirmed.reason)
        shutdown_event.set()
        return True
    return False


def _announce_failure(reason: str) -> None:
    """Log a confirmed, restart-curable persistence failure as loudly as possible.

    File logging is almost certainly dead by now (it died of the same ``ENOSYS``
    the probe just re-detected), so the message goes to stderr with ``flush``
    FIRST -- that is the surface systemd/journald and a terminal still capture --
    and to the logger second for any handler that is still alive (a journald
    handler, an already-open fd).
    """
    message = (
        "data home stopped persisting after a healthy boot -- the gateway cannot "
        "save chat/cron/session state and every static asset 404s silently. "
        "Exiting non-zero so a supervisor restarts a fresh process (a seccomp "
        "filter inherited from a sandboxed parent cannot be removed from a live "
        f"process). {reason}"
    )
    try:
        print(f"{_STDERR_PREFIX}{reason}", file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - stderr itself is gone
        pass
    logger.critical("Persistence watchdog: %s", message)


def _announce_unrecoverable(reason: str) -> None:
    """Log a confirmed persistence failure a restart would NOT cure, without exiting.

    A full disk, a read-only remount or an exceeded quota fails the probe just
    as a seccomp teardown does, but a fresh process meets the identical boot
    preflight and dies again -- a crash loop that serves nothing, where staying
    up keeps serving reads from memory until the condition clears. So this is
    loud (stderr + logger, same as the exit path) but does not set the shutdown
    event. The watchdog calls it once per distinct reason, not on every tick, so
    a disk that stays full for hours produces one record rather than one every
    interval. The next periodic probe re-checks and will exit if the fault later
    turns restart-curable.
    """
    try:
        print(
            f"{_STDERR_PREFIX}{reason} (a restart cannot cure this; staying up)",
            file=sys.stderr,
            flush=True,
        )
    except Exception:  # pragma: no cover - stderr itself is gone
        pass
    logger.critical(
        "Persistence watchdog: data home stopped persisting (%s). A restart "
        "cannot cure this class of failure, so the gateway stays up and keeps "
        "serving what it can; it will re-check each interval.",
        reason,
    )


def probe_data_home(data_home: Path) -> Callable[[], Awaitable[ProbeResult | None]]:
    """Build the off-loop async probe the gateway hands to the watchdog.

    ``probe_file_persistence`` does real filesystem I/O (mkstemp + fsync +
    flock + replace + unlink), so it runs on a worker thread exactly as the boot
    preflight does: a stalled filesystem would otherwise wedge the event loop
    this watchdog exists to protect. A thread-pool exhaustion means the probe
    could not RUN, not that the data home is broken, so it is reported as a
    non-restartable failure: it is logged (the gateway is degraded) but does not
    self-exit, because nothing shows a restart cures a worker-thread shortage.

    The returned :class:`ProbeResult` marks a failure ``restartable`` only when
    it is the ``ENOSYS`` case (an inherited seccomp filter a relaunch clears).
    ``probe_file_persistence`` returns a message string, not an errno, so the
    ``ENOSYS`` signal is read from that message -- it embeds the ``OSError``'s
    ``repr`` (``[Errno 38]`` / ``Function not implemented``), which is the same
    text the boot preflight prints.
    """
    from kiro_crew import platform_compat

    enosys = errno.ENOSYS

    def _is_enosys(message: str) -> bool:
        return f"[Errno {enosys}]" in message or "Function not implemented" in message

    async def _probe() -> ProbeResult | None:
        try:
            reason = await asyncio.to_thread(platform_compat.probe_file_persistence, data_home)
        except RuntimeError as exc:
            # Could not get a worker thread: the probe could not RUN, which is
            # not evidence the data home is broken and not something a restart
            # is known to cure. Report it (the gateway is degraded) without the
            # self-exit path -- restartable=False.
            return ProbeResult(f"cannot run the persistence probe: {exc}", restartable=False)
        if reason is None:
            return None
        return ProbeResult(reason, restartable=_is_enosys(reason))

    return _probe

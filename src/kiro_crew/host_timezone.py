"""The IANA time zone of the machine this process runs on, kept current while it runs.

A cron job, or the config default, whose zone is ``local`` follows the host: a
laptop that travels switches its system zone, and the job's wall clock follows it.

Reading the zone is filesystem I/O on POSIX and a registry read on Windows, so it
never runs on the gateway's event loop. :func:`refresh_host_timezone` does the read
and publishes the result; :func:`published_host_timezone` returns what was
published. The gateway refreshes from the cron timer's worker-thread scan, which
runs at least every ``_TIMER_POLL_SECS``. A process with no event loop on the
calling thread (the CLI, and the ``mcp-cron`` stdio server, whose tool calls run
on a worker thread) refreshes on read once the published value is older than
:data:`HOST_ZONE_TTL_SECS`.

``TZ`` in the process environment wins over the system setting, as it does for
every other program: a gateway started with ``TZ=UTC`` stays on UTC.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from kiro_crew import platform_compat

logger = logging.getLogger("kiro_crew.host_timezone")

#: The zone value that follows the host. No IANA key spells it, so it can never
#: be mistaken for one (``localtime`` can: some Linux tzdata ships that file).
LOCAL_TIMEZONE = "local"

#: How old a published host zone may be before a loop-less reader re-reads it.
HOST_ZONE_TTL_SECS = 30.0

#: Zone changes remembered, oldest dropped first. A trip is a handful of changes.
_HISTORY_MAX = 32


@dataclass(frozen=True)
class _HostZone:
    zone: str
    checked_at: float
    # (wall-clock epoch the zone was first seen, zone), oldest first.
    history: tuple[tuple[float, str], ...]


_STATE: _HostZone | None = None
_STATE_LOCK = threading.Lock()
# Held across a whole read-and-publish, so two refreshes never publish out of order.
_REFRESH_LOCK = threading.Lock()

_LOCALTIME = Path("/etc/localtime")
# Debian and Ubuntu name the zone here too, which is what is left when /etc/localtime
# is a copied file rather than a link (container images, a bind mount).
_ETC_TIMEZONE = Path("/etc/timezone")
_UNDETECTED_WARNED = [False]
# A zone every refresh publishes instead of reading the host; None reads the host.
_FROZEN: list[str | None] = [None]
# Whether a refresh an event-loop reader started is still running on a worker thread.
_REFRESH_PENDING = [False]


def is_local_timezone(tz_name: str) -> bool:
    """Whether *tz_name* is the ``local`` zone, in any letter case."""
    return tz_name.strip().lower() == LOCAL_TIMEZONE


def detect_host_timezone() -> str:
    """Return the system IANA tz name, or empty string if it can't be detected.

    ``TZ`` and ``/etc/localtime`` are POSIX-only. On Windows the zone lives in
    the registry as a Windows zone id ("Pacific Standard Time") that needs a
    Windows->IANA mapping, so without a Windows branch this returned "" there and
    the whole product silently fell back to UTC — cron schedules and the agent's
    "today" were off by the local offset. ``tzlocal`` (a Windows-marked dep whose
    only requirement, ``tzdata``, we already ship) resolves it via the CLDR
    windowsZones table; it is imported lazily so a source checkout without it
    degrades to the old skip-and-ask flow rather than crashing. It caches the
    zone it read, so it is reloaded first: a running gateway must see the zone
    the laptop switched to.
    """
    tz_env = os.environ.get("TZ", "").lstrip(":")
    if tz_env and not tz_env.startswith("/"):
        return tz_env
    try:
        if _LOCALTIME.is_symlink():
            target = str(_LOCALTIME.resolve())
            if "zoneinfo/" in target:
                return target.split("zoneinfo/", 1)[1]
    except Exception:
        pass
    if not platform_compat.IS_WINDOWS:
        try:
            named = _ETC_TIMEZONE.read_text(encoding="utf-8").strip()
        except Exception:
            named = ""
        if named:
            return named
    if platform_compat.IS_WINDOWS:
        try:
            # tzlocal ships no type stubs; it is a Windows-only lazy import, so
            # ignore the missing-stub error rather than add a types-* dep.
            import tzlocal  # type: ignore[import-untyped]

            reload = getattr(tzlocal, "reload_localzone", None)
            if reload is not None:
                reload()
            name = tzlocal.get_localzone_name()
            if name:
                return str(name)
        except Exception:
            # No tzlocal / unmappable zone: fall through to the ask-or-skip path
            # rather than crash the setup wizard.
            pass
    return ""


def _loadable(name: str) -> bool:
    try:
        ZoneInfo(name)
    except Exception:
        return False
    return True


def publish_host_timezone(zone: str, *, at: float | None = None) -> None:
    """Publish *zone* as the host zone, seen at wall-clock epoch *at* (now by default).

    An empty *zone* keeps the last zone that was detected, so a failed read does
    not move every host-following job onto UTC. A zone different from the last
    one is added to the history :func:`host_timezone_at` reads.
    """
    global _STATE
    seen_at = time.time() if at is None else at
    with _STATE_LOCK:
        prev = _STATE
        history = prev.history if prev is not None else ()
        kept = zone or (prev.zone if prev is not None else "")
        if kept and (not history or history[-1][1] != kept):
            if history:
                logger.info("Host time zone changed: %s -> %s", history[-1][1], kept)
            history = (*history, (seen_at, kept))[-_HISTORY_MAX:]
        _STATE = _HostZone(zone=kept, checked_at=time.monotonic(), history=history)


def refresh_host_timezone() -> str:
    """Re-read the host zone and publish it. Blocking I/O: never call it on the event loop.

    A refresh already in progress on another thread makes this one return the
    published zone instead of reading: two reads finishing in the wrong order
    would publish the older zone last.
    """
    if not _REFRESH_LOCK.acquire(blocking=False):
        state = _STATE
        return state.zone if state is not None else ""
    try:
        return _read_and_publish()
    finally:
        _REFRESH_LOCK.release()


def _read_and_publish() -> str:
    frozen = _FROZEN[0]
    detected = frozen if frozen is not None else detect_host_timezone()
    # No zone at all is normal on a server or a container; a name that will not load is not.
    unloadable = bool(detected) and not _loadable(detected)
    if unloadable:
        detected = ""
    if not detected and frozen is None and not _UNDETECTED_WARNED[0]:
        _UNDETECTED_WARNED[0] = True
        logger.log(
            logging.WARNING if unloadable else logging.INFO,
            "Could not detect this computer's time zone; a schedule that follows it "
            "keeps the last zone detected, or UTC when there is none",
        )
    publish_host_timezone(detected)
    state = _STATE
    return state.zone if state is not None else ""


def _refresh_in_background(loop: asyncio.AbstractEventLoop) -> None:
    """Start a refresh on a worker thread and return without waiting for it."""
    if _REFRESH_PENDING[0]:
        return
    _REFRESH_PENDING[0] = True

    def _run() -> None:
        try:
            refresh_host_timezone()
        except Exception:
            logger.warning("Host time zone refresh failed", exc_info=True)
        finally:
            _REFRESH_PENDING[0] = False

    loop.run_in_executor(None, _run)


def published_host_timezone() -> str:
    """The host's IANA zone, or ``""`` when it has never been detected.

    Never reads the filesystem on an event loop. A published value older than
    :data:`HOST_ZONE_TTL_SECS` (or none at all) is refreshed: in place off the
    loop, and on a worker thread when the reader is on the loop, which then gets
    the value published so far. So every process keeps the zone current on its
    own, a gateway whose cron scheduler is not running included.
    """
    state = _STATE
    stale = state is None or time.monotonic() - state.checked_at >= HOST_ZONE_TTL_SECS
    if stale:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return refresh_host_timezone()
        _refresh_in_background(loop)
    return state.zone if state is not None else ""


def _read_now() -> str:
    """Read and publish the host zone now. Blocking.

    Waits for a refresh already in progress rather than skipping it, then reads
    again, so the answer is the zone as it is now, never one published earlier.
    """
    with _REFRESH_LOCK:
        return _read_and_publish()


async def ensure_host_timezone() -> str:
    """The host zone as it is now, read on a worker thread.

    For an on-loop caller about to turn a ``local`` wall clock into a stored instant:
    there :func:`published_host_timezone` answers what was published, ``""`` before
    the first read and possibly a zone the host has since left, and an instant
    resolved in either is wrong for good. A fresh read is cheap and such a caller
    is rare, so it never trusts the snapshot.
    """
    return await asyncio.to_thread(_read_now)


def ensure_host_timezone_off_loop() -> None:
    """:func:`ensure_host_timezone` for a caller that may be off the event loop.

    Off the loop (a tool call on a worker thread, the CLI) the zone is read and
    published now, after any refresh already in progress, which
    :func:`published_host_timezone` would answer around instead. On the loop it
    does nothing: a read there would block the loop, and such a caller awaits
    :func:`ensure_host_timezone` first.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _read_now()


def host_timezone_at(ts: float) -> str:
    """The host zone in effect at wall-clock epoch *ts*, or ``""`` when unknown.

    Unknown means this process had not yet seen a zone then: the history starts
    at the first detection, so it does not survive a restart.
    """
    state = _STATE
    found = ""
    for since, zone in state.history if state is not None else ():
        if since > ts:
            break
        found = zone
    return found


def host_timezones_between(start: float, end: float) -> frozenset[str]:
    """Every host zone in effect at some instant from *start* to *end*, or none when unknown."""
    state = _STATE
    found: set[str] = set()
    at_start = ""
    for since, zone in state.history if state is not None else ():
        if since <= start:
            at_start = zone
        elif since <= end:
            found.add(zone)
    if at_start:
        found.add(at_start)
    return frozenset(found)


def reset_host_timezone() -> None:
    """Forget the published zone and its history. For tests."""
    global _STATE
    with _STATE_LOCK:
        _STATE = None
    _UNDETECTED_WARNED[0] = False
    _REFRESH_PENDING[0] = False


def freeze_host_timezone(zone: str | None) -> None:
    """Make every refresh publish *zone* instead of reading the host; ``None`` reads it again.

    For tests: a test must not see the zone of the machine it happens to run on.
    ``""`` is "never detected", which resolves the way an unset zone did before
    ``local`` existed (UTC). A zone published directly still stands, since an
    empty refresh keeps the last zone.
    """
    _FROZEN[0] = zone

"""Clear what an earlier gateway life left on Kiro's Chromium profile before a launch.

A gateway that exits without its teardown (a crash, an ended process, a power
loss) leaves Kiro's Chromium running: on Windows it is started in a process group
of its own and no job ends it with the gateway. Chromium hands a second browser on
the same ``--user-data-dir`` to the first one and exits, so every later start on the
profile would fail while the old browser keeps it.

The earlier browser is found through what it left in this data home's own
owner-only profile, and only when all three hold: Chromium's ``lockfile`` is there
(it exists only while a process holds the profile), ``DevToolsActivePort`` names a
port that answers, and the endpoint at that file's ``/devtools/browser/<id>`` path
replies to ``Browser.getVersion`` as Chromium (a Chromium with another id refuses
the path). Anything less is not proven to be this profile's browser, and the start
goes on as it always did. Nothing here signals a process: the proven browser is
asked to close over CDP, and the start fails with a message naming it when it
does not.

On macOS and Linux the browser dies with the gateway (it runs on the debugging
pipe), but its profile lock can outlive it: ``SingletonLock`` is a symlink whose
target names ``<host>-<pid>``, and Chromium refuses a profile whose lock names
another host without asking whether that pid is alive. A machine whose host name
changes on restart (a cloud VM, a container) therefore keeps its browser down until
someone removes the lock, which the sandbox hides from the agent. So before a POSIX
launch the lock is read and removed when it is stale (:func:`clear_stale_singleton_lock`).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.browser_harness.relay import request_chromium_close

logger = logging.getLogger(__name__)

#: Chromium's Windows profile lock (``chrome::kLockfilename``), opened
#: delete-on-close, so it exists only while a process holds the profile.
_CHROMIUM_LOCKFILE = "lockfile"

#: Chromium's POSIX profile lock (``chrome::kSingletonLockFilename``): a symlink to
#: ``<host>-<pid>``, written by the process that holds the profile and left behind
#: when that process does not exit cleanly.
_SINGLETON_LOCK = "SingletonLock"

#: The lock's siblings, symlinks to the holder's socket and its cookie. Chromium
#: recreates both when it takes the profile; they go with a stale lock so nothing
#: of the old holder is left pointing into a temp dir that is gone.
_SINGLETON_SIBLINGS = ("SingletonSocket", "SingletonCookie")

#: Separates host from pid in the lock's target (``kProcessSingletonLockDelimiter``).
#: A host name can contain it, so the target is split at its last one.
_SINGLETON_LOCK_DELIMITER = "-"


def earlier_chromium_detail(profile: Path) -> str:
    """The start failure for an earlier browser that did not close on request."""
    return (
        f"a Kiro's Chromium left by an earlier run is still using {profile} and did "
        "not close: end it in Task Manager, then try again"
    )


async def held_profile_detail(profile: Path, detail: str) -> str:
    """*detail* of a Kiro's Chromium that exited during its start, with the likely cause.

    The browser just spawned has exited, so a ``lockfile`` still there is held by
    another process: Chromium hands a launch on a held profile to the holder.
    """
    if await asyncio.to_thread((profile / _CHROMIUM_LOCKFILE).exists):
        return (
            f"{detail}: a Kiro's Chromium left by an earlier run may still be using "
            f"{profile}; end it in Task Manager, then try again"
        )
    return detail


async def prepare_profile(profile: Path) -> None:
    """Make the profile, close an earlier run's browser on it (Windows), clear its tabs.

    The close comes before the clear: a browser that closes cleanly writes its
    session files, and the next launch must not reopen those tabs.
    """
    # Late: the supervisor imports this module.
    from kiro_crew.browser_harness import supervisor as sup

    await asyncio.to_thread(platform_compat.make_owner_only_dir, profile)
    if platform_compat.IS_WINDOWS:
        await reclaim_profile(profile)
    else:
        await asyncio.to_thread(clear_stale_singleton_lock, profile)
    await asyncio.to_thread(sup._clear_restored_session, profile)


def _stale_lock_reason(target: str) -> str | None:
    """Why the lock whose symlink target is *target* protects no holder.

    ``None`` when a live process on this host may hold it, or when the target is not
    the ``<host>-<pid>`` Chromium writes: Chromium removes a lock it cannot parse by
    itself, and a lock from this host is its own to judge once the pid is alive (it
    checks that the process is a browser, then talks to it). Only the two shapes
    Chromium refuses or cannot see through are judged here: another host's lock,
    which it declines without a liveness check, and a gone pid on this host.
    """
    host, delimiter, pid_text = target.rpartition(_SINGLETON_LOCK_DELIMITER)
    if not delimiter or not host:
        return None
    if host != socket.gethostname():
        return f"it was taken on host {host!r}, not this one"
    if not pid_text.isdigit():
        return None
    if not platform_compat.pid_exists(int(pid_text)):
        return f"the process that took it, pid {pid_text}, is gone"
    return None


def clear_stale_singleton_lock(profile: Path) -> None:
    """Remove *profile*'s ``SingletonLock``, socket and cookie links when the lock is stale.

    Only a symlink is read: a regular file at the lock's path is not Chromium's and
    stays. The links are unlinked, never followed, so nothing outside the profile is
    touched. A link that cannot go is logged and left; the launch goes on and
    Chromium reports the lock itself.
    """
    lock = profile / _SINGLETON_LOCK
    try:
        target = lock.readlink()
    except OSError:
        return
    reason = _stale_lock_reason(str(target))
    if reason is None:
        return
    logger.info("browser: removing the stale Chromium lock %s -> %s: %s", lock, target, reason)
    for name in (_SINGLETON_LOCK, *_SINGLETON_SIBLINGS):
        path = profile / name
        if not path.is_symlink():
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("browser: could not remove %s: %s", path, exc)


async def _asked_to_close(url: str, bound: float) -> bool:
    """Send ``Browser.close`` to *url* once it answered as Chromium, within *bound*.

    ``True`` once the request was sent, even when the bound runs out in the close
    handshake that follows: that browser was asked, so its exit is waited for.
    """
    sent = asyncio.Event()
    try:
        asked = await asyncio.wait_for(request_chromium_close(url, sent), max(bound, 0.0))
    except Exception:  # noqa: BLE001 - judged by whether the request went out
        asked = sent.is_set()
    if asked:
        logger.info("browser: asked a Kiro's Chromium an earlier run left to close")
    else:
        logger.info("browser: the endpoint in the profile's port file took no close request")
    return asked


def _lock_released(lock: Path) -> bool:
    """Remove *lock*; ``False`` when it cannot go, as while a process holds it.

    Chromium opens it delete-on-close without delete sharing, so on Windows a
    holder makes the unlink fail (a sharing violation). Only reached after a
    browser was proven to hold the profile and asked to close, so any lock that
    will not go is read as that browser still holding it.
    """
    try:
        lock.unlink(missing_ok=True)
    except OSError:
        return False
    return True


async def reclaim_profile(profile: Path) -> None:
    """Close an earlier Kiro's Chromium proven to hold *profile*, within one grace.

    The port launcher runs after this. By then ``ensure()`` has torn down every
    browser this supervisor started, so a browser proven to hold the profile
    belongs to an earlier gateway life. Raises
    :class:`~kiro_crew.browser_harness.supervisor.BrowserUnavailable` when that
    browser was asked to close and, after ``_STOP_GRACE_SECS``, still has its port
    open or still holds the profile lock; nothing is launched then, and the port
    file goes first, so a later start never asks the same port again.
    """
    # Late: the supervisor imports this module. Its probe and bounds are read
    # through it, so a patch of supervisor's own names reaches here too.
    from kiro_crew.browser_harness import supervisor as sup

    deadline = time.monotonic() + sup._STOP_GRACE_SECS
    lock = profile / _CHROMIUM_LOCKFILE
    port_file = profile / sup.DEVTOOLS_PORT_FILE
    if not await asyncio.to_thread(lock.exists):
        return
    url = await sup._devtools_ws_url(profile)
    if url is None or not await _asked_to_close(url, deadline - time.monotonic()):
        return
    while time.monotonic() < deadline:
        if await sup._devtools_ws_url(profile) is None and not await asyncio.to_thread(lock.exists):
            return
        await asyncio.sleep(sup._POLL_SECS)
    if await sup._devtools_ws_url(profile) is not None or not await asyncio.to_thread(
        _lock_released, lock
    ):
        await asyncio.to_thread(port_file.unlink, missing_ok=True)
        raise sup.BrowserUnavailable(earlier_chromium_detail(profile))

"""Leftover-process half of the RSS ceiling tick.

A shell command run under a timeout kills only its direct child. A job that child
started is reparented to init and drops out of the runtime's process tree, but it
keeps the runtime's session id, because the runtime is spawned as a session leader.
``session_pid.find_session_leftovers`` names those processes, and the ceiling's
measurement already counts them. This module is what the ceiling does with them:

* An idle session recycled over the ceiling ends them once its runtime is gone
  (:func:`end_after_recycle`). A leftover still in the runtime's process group is
  already ended by the runtime's own group signal; this reaches the ones that
  left the group too (a job started under ``set -m``).
* A session over the ceiling with a turn in flight is never recycled, so its
  leftovers are logged with pid, size and name, and the person watching it gets
  one notice per turn (:func:`report_busy_sessions`). Ending them mid-turn is
  opt-in behind ``session.watchdog_end_leftovers_mid_turn``, off by default: a
  leftover can be work the agent started on purpose, such as a dev server.

The functions take the :class:`~kiro_crew.session_cleanup.SessionCleanup` instance
as ``cleanup`` and read ``cleanup._owner``, ``cleanup._deps`` and ``cleanup.state``,
as :mod:`kiro_crew.session_cleanup_spare` does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kiro_crew import session_pid
from kiro_crew.session_cleanup_spare import rss_limit_for

if TYPE_CHECKING:
    from kiro_crew.session_cleanup import SessionCleanup
    from kiro_crew.session_pid_rss import SessionLeftovers

#: Processes a notice names; the log line names every one.
NOTICE_MAX_LISTED = 5


def end_mid_turn_enabled(cfg: object) -> bool:
    """``session.watchdog_end_leftovers_mid_turn``, read at each tick; only ``True`` is on."""
    return getattr(getattr(cfg, "session", None), "watchdog_end_leftovers_mid_turn", False) is True


def describe(leftovers: SessionLeftovers, limit: int | None = None) -> str:
    """``pid 4242 python3 (1100 MB), ...``, largest first, cut to *limit* entries."""
    items = sorted(leftovers.items, key=lambda item: item.rss_mb, reverse=True)
    shown = items if limit is None else items[:limit]
    text = ", ".join(f"pid {item.pid} {item.name} ({item.rss_mb} MB)" for item in shown)
    if len(items) > len(shown):
        text += f" and {len(items) - len(shown)} more"
    return text


def can_end_leftovers() -> bool:
    """Whether this host can end a leftover: the pinned terminator signals on Linux only."""
    return session_pid.platform_compat.IS_LINUX is True


def leftover_notice(
    leftovers: SessionLeftovers, ceiling_mb: int, ended: int | None, *, can_end: bool
) -> str:
    """The one chat notice a turn gets about its leftovers.

    *ended* is None when ending them mid-turn is off, else how many were ended.
    *can_end* is False on a host that cannot end them at all, where the notice
    promises nothing.
    """
    total = sum(item.rss_mb for item in leftovers.items)
    lead = (
        f"Processes that this session's commands left running hold {total} MB, and the "
        f"session is over its memory limit of {ceiling_mb} MB: "
        f"{describe(leftovers, NOTICE_MAX_LISTED)}. They are outside the agent's process "
        "tree, so they keep running after the command that started them ended."
    )
    if not can_end:
        return (
            f"{lead} The watchdog cannot end them safely on this host, so they keep "
            "running until they exit or are ended by hand."
        )
    if ended is None:
        return (
            f"{lead} The watchdog does not recycle a session during a turn; it ends them "
            "with the session when it recycles it after this turn. To end them during a "
            "turn, turn on session.watchdog_end_leftovers_mid_turn."
        )
    if ended:
        return f"{lead} {ended} of them were ended (session.watchdog_end_leftovers_mid_turn)."
    return f"{lead} They could not be ended on this host."


async def snapshot(
    cleanup: SessionCleanup, pid: int, loop: asyncio.AbstractEventLoop
) -> SessionLeftovers | None:
    """The leftovers of runtime *pid*, read off the loop, or None when unreadable."""
    if cleanup._deps.is_windows():
        return None
    try:
        return await loop.run_in_executor(
            cleanup._deps.get_maintenance_executor(), session_pid.find_session_leftovers, pid
        )
    except Exception:
        cleanup._deps.logger.debug("Leftover-process read failed for pid %s", pid, exc_info=True)
        return None


async def _end(
    cleanup: SessionCleanup,
    pid: int,
    leftovers: SessionLeftovers,
    loop: asyncio.AbstractEventLoop,
    *,
    only_if_root_gone: bool,
) -> int:
    try:
        return await loop.run_in_executor(
            cleanup._deps.get_maintenance_executor(),
            lambda: session_pid.end_session_leftovers(
                pid, leftovers, only_if_root_gone=only_if_root_gone
            ),
        )
    except Exception:
        cleanup._deps.logger.warning(
            "Could not end the leftover processes of runtime %s", pid, exc_info=True
        )
        return 0


async def end_after_recycle(
    cleanup: SessionCleanup,
    key: str,
    pid: int,
    leftovers: SessionLeftovers | None,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """End *leftovers*, read before *key* was recycled, once runtime *pid* is gone.

    Nothing is signalled while *pid* is still the process it was: a runtime a
    co-tenant keeps alive keeps its processes too.
    """
    if leftovers is None or not leftovers.items:
        return
    ended = await _end(cleanup, pid, leftovers, loop, only_if_root_gone=True)
    if ended:
        cleanup._deps.logger.warning(
            "RSS recycle: ended %d leftover process(es) of session %s outside its "
            "runtime's tree: %s",
            ended,
            key,
            describe(leftovers),
        )


async def report_busy_sessions(
    cleanup: SessionCleanup,
    busy: list[tuple[str, int, Any]],
    measure: Callable[[int], int] | None,
    rss_by_pid: dict[int, int],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Log and surface the leftovers of each session over the ceiling mid-turn.

    A turn is never recycled, so this is the one place a leftover holding the
    memory of a running turn is named. The log line fires again only when the set
    of leftover pids changes, and the notice once per turn: a key leaves the record
    when its turn releases the session (``SessionAllocationService.release``), or
    on the first tick that finds it idle. ``rss_by_pid`` is the tick's per-runtime
    cache, shared with the recycle pass.
    """
    noticed: dict[str, frozenset[int]] = cleanup.state.leftover_noticed
    busy_keys = {key for key, _, _ in busy}
    for key in [key for key in noticed if key not in busy_keys]:
        del noticed[key]
    ceiling = cleanup.state.rss_max_mb
    if not busy or measure is None or not ceiling:
        return
    end_mid_turn = end_mid_turn_enabled(cleanup._owner._cfg)
    can_end = can_end_leftovers()
    for key, pid, _session in busy:
        try:
            rss = rss_by_pid.get(pid)
            if rss is None:
                rss = await loop.run_in_executor(
                    cleanup._deps.get_maintenance_executor(), measure, pid
                )
                rss_by_pid[pid] = rss
            # Scaled by the chat leases on the pid, as the idle pass scales it: a
            # shared runtime's tree holds every co-tenant's memory.
            limit = rss_limit_for(cleanup, pid, rss)
            if rss <= limit:
                continue
            leftovers = await snapshot(cleanup, pid, loop)
            if leftovers is None or not leftovers.items:
                continue
            pids = frozenset(item.pid for item in leftovers.items)
            if noticed.get(key) != pids:
                cleanup._deps.logger.warning(
                    "RSS ceiling: session %s tree rss=%dMB exceeds %dMB during a turn; "
                    "processes outside its runtime's tree hold memory: %s",
                    key,
                    rss,
                    limit,
                    describe(leftovers),
                )
            # The opt-in is honoured on every tick, not only when the set changes:
            # turning it on mid-turn ends leftovers that were already reported.
            ended: int | None = None
            if can_end and end_mid_turn:
                ended = await _end(cleanup, pid, leftovers, loop, only_if_root_gone=False)
                if ended:
                    cleanup._deps.logger.warning(
                        "RSS ceiling: ended %d leftover process(es) of session %s mid-turn "
                        "(session.watchdog_end_leftovers_mid_turn)",
                        ended,
                        key,
                    )
            first = key not in noticed
            noticed[key] = pids
            callback = getattr(cleanup._owner, "on_leftover_memory", None)
            if first and callback is not None:
                callback(key, leftover_notice(leftovers, limit, ended, can_end=can_end))
        except Exception:
            cleanup._deps.logger.exception("Leftover-process report failed for session %s", key)

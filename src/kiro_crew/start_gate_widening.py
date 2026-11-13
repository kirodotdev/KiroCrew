"""Widen the ``auto`` start gates when the host has more free memory than at boot.

The three start bounds (``agent.session_start_concurrency`` and the two cold-start
widths that follow it) are sized from one host reading taken as the gateway
boots (``session_start_sizing``). A gateway that boots while memory is short
would keep that moment's widths for its whole life, so a person's chat start
queues behind background starts on a host that has long since had room.

This module re-reads the host on a slow interval and raises the widths. It
never narrows one, never passes what the host's cores give, and stops once the
cores, not the memory, set the width. Memory per start stays bounded by
``agent.spawn_min_memory_gb``, read at each start, which this does not touch.

A neutral module: the gateway's boot task calls it and it reads only the sizing
modules, never ``kiro_crew.acp``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from kiro_crew.session_start_sizing import (
    cached_session_start_concurrency,
    is_auto,
    memory_limits_auto_width,
    refresh_host_capacity,
    resolve_session_start_sizing,
)

logger = logging.getLogger(__name__)

#: Seconds between host re-reads while memory still sets the width. One re-read
#: is a few small file reads in a worker thread.
HOST_REREAD_INTERVAL_SECS = 60.0


async def reread_host_and_widen(agent: Any, sessions: Any) -> bool:
    """Re-read free memory once and widen the ``auto`` start gates; True when they grew.

    The host read runs in a worker thread. *sessions* (the session manager, or
    ``None``) grows its cold-start queues here, on the loop; the ``session/new``
    gate grows on its next lookup (``acp.runtime_start.session_start_gate``).
    """
    configured = agent.session_start_concurrency
    before = cached_session_start_concurrency(configured)
    try:
        await asyncio.to_thread(refresh_host_capacity)
    except Exception:
        logger.warning("Host re-read for the start gates failed", exc_info=True)
        return False
    if cached_session_start_concurrency(configured) <= before:
        return False
    logger.info(
        "Session start concurrency widened: %s",
        resolve_session_start_sizing(configured).describe(),
    )
    if sessions is not None:
        try:
            sessions.widen_cold_start_queues()
        except Exception:
            logger.warning("Cold-start queue widening failed", exc_info=True)
    return True


async def widen_start_gates_as_memory_frees(
    agent: Any,
    sessions: Any,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Re-read the host every :data:`HOST_REREAD_INTERVAL_SECS` until the cores set the width.

    Returns at once when ``agent.session_start_concurrency`` is an explicit
    integer: that setting is ``restart=True`` and the cold-start widths follow
    it. *sleep* is the wait between re-reads.
    """
    if not is_auto(agent.session_start_concurrency):
        return
    while memory_limits_auto_width():
        await sleep(HOST_REREAD_INTERVAL_SECS)
        await reread_host_and_widen(agent, sessions)

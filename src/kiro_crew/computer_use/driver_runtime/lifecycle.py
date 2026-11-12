"""The desktop driver child around the gateway: stopped on shutdown and before a restart.

A graceful shutdown stops the supervised child through ``on_cleanup``. An in-place
restart's ``exec`` skips every cleanup hook while keeping the pid, so the same stop
is registered with :mod:`kiro_crew.pre_exec_stops` too; the cleanup hook
unregisters it. Nothing starts the child here: it starts on the first desktop call,
and a child left behind by an earlier life of this gateway is reclaimed then (the
supervisor's pid file names it).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

PRE_EXEC_NAME = "Computer Use driver"


async def _stop() -> None:
    from kiro_crew.computer_use.driver_runtime import supervisor

    try:
        await asyncio.to_thread(supervisor.stop_shared)
    except Exception:  # noqa: BLE001 - a stop that fails must not block shutdown
        logger.debug("computer-use driver stop failed", exc_info=True)


def register(app: Any) -> None:
    """Hook the driver stop into *app*'s startup/cleanup and the pre-exec stops."""
    from kiro_crew.pre_exec_stops import register_pre_exec_stop

    holder: dict[str, Any] = {}

    async def _startup(_app: Any) -> None:
        holder["unregister"] = register_pre_exec_stop(PRE_EXEC_NAME, _stop)

    async def _cleanup(_app: Any) -> None:
        unregister = holder.pop("unregister", None)
        if unregister is not None:
            unregister()
        await _stop()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)


__all__ = ["PRE_EXEC_NAME", "register"]

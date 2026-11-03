"""Browser Harness's lifecycle around the gateway.

The startup reclaim and the shutdown / pre-exec stop of the supervisor that owns the
agent's browser (``kiro_crew.browser_harness.supervisor``) and its install job.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        DashboardState,
        handlers,
        logger,
        register_pre_exec_stop,
    )


def _register_browser_harness_lifecycle(app: web.Application, state: DashboardState) -> None:
    """Browser Harness around the gateway: reclaim at startup, stop at shutdown.

    The supervisor (``browser_harness.supervisor``) owns three children -- Kiro's
    Chromium, the Harness daemon and the helper worker -- plus the relay's sockets.
    A graceful shutdown closes the agent's tabs and stops all of them through the
    ``on_cleanup`` hook. An in-place restart's exec skips every ``on_cleanup`` hook
    while keeping the pid, so the same stop is registered with
    :mod:`kiro_crew.pre_exec_stops` too; the cleanup hook unregisters it.

    Startup runs the same stop once, in the background: a previous life of this
    gateway that died without reaching cleanup left a daemon answering on the IPC
    socket, and a fresh daemon seeing it would exit as "already running" while the
    stale one keeps pointing at a dead relay. The engine install
    job (``stop_browser_harness_install``) is stopped alongside.

    After that stop, the same background task runs the startup auto-install
    (``auto_setup_browser_harness``): one attempt per gateway, the Settings install
    job installing what is missing or stale, then a warm connect only where nothing
    would prompt the user. Nothing here is awaited by the startup hook itself, so
    neither the bind nor the loop waits on pip or a download.

    Registered BEFORE ``runner.setup()`` freezes the app's signal lists.
    """
    from kiro_crew.browser_harness.supervisor import get_supervisor
    from kiro_crew.config.paths import data_home

    async def _stop_supervisor() -> None:
        try:
            await get_supervisor(data_home()).stop()
        except Exception:  # noqa: BLE001 - a stop that fails must not block the restart
            logger.debug("browser harness stop failed", exc_info=True)

    async def _reclaim_then_auto_setup() -> None:
        await _stop_supervisor()
        try:
            from kiro_crew.dashboard.handlers.browser_harness_status import (
                auto_setup_browser_harness,
            )

            outcome = await auto_setup_browser_harness(state)
            logger.debug("browser harness startup auto-setup: %s", outcome)
        except Exception:  # noqa: BLE001 - an optional subsystem never fails the gateway
            logger.debug("browser harness startup auto-setup failed", exc_info=True)

    async def _harness_startup(app_: web.Application) -> None:
        state._browser_harness_unregister = register_pre_exec_stop(
            "Browser Harness", _stop_supervisor
        )
        task = asyncio.create_task(_reclaim_then_auto_setup())
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

    async def _harness_shutdown(app_: web.Application) -> None:
        unregister = getattr(state, "_browser_harness_unregister", None)
        if unregister is not None:
            unregister()
        try:
            await handlers.stop_browser_harness_install(app_["state"])
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("browser harness install stop failed during shutdown", exc_info=True)
        await _stop_supervisor()

    app.on_startup.append(_harness_startup)
    app.on_cleanup.append(_harness_shutdown)

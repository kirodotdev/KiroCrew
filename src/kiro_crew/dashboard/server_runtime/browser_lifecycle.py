"""The browser CLI's lifecycle around the gateway.

The loop that keeps the snapshot directory bounded, and the cleanup hooks that stop a
browser install and the CLI dashboard process when the gateway shuts down.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        DashboardState,
        browser_cli_launcher,
        browser_cli_snapshots,
        browser_cli_view,
        handlers,
        logger,
        register_pre_exec_stop,
    )


async def _prune_browser_snapshots_loop() -> None:
    """Keep the browser snapshot directory bounded for as long as we run.

    `playwright-cli` writes one snapshot YAML per command and prunes nothing, so
    retention belongs to a long-lived component. It lives here rather than in the
    agent because the agent has no reason to know the policy, and a per-command
    prune would race the CLI daemon writing the next file.

    The first pass is delayed so it never competes with boot work for disk, and the
    interval is coarse because the retention bound is a ceiling, not a deadline.
    """
    await asyncio.sleep(60.0)
    while True:
        try:
            await asyncio.to_thread(browser_cli_snapshots.prune)
        except Exception:
            logger.debug("browser snapshot prune failed", exc_info=True)
        await asyncio.sleep(30 * 60.0)


def _register_browser_install_cleanup(app: web.Application, state: DashboardState) -> None:
    """Stop any browser install owned by this gateway during shutdown."""

    async def _browser_install_shutdown(app_: web.Application) -> None:
        try:
            await handlers.stop_browser_install(app_["state"])
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("browser install stop failed during shutdown", exc_info=True)

    app.on_cleanup.append(_browser_install_shutdown)


def _register_browser_view_cleanup(app: web.Application, state: DashboardState) -> None:
    """Stop the CLI dashboard process when the gateway shuts down.

    `playwright-cli show` is spawned in its OWN session (``start_new_session``) so
    a browsing view outlives the request that started it. That same detachment
    means a restart would leave it running while the new gateway loses its pid,
    and the next view request starts a SECOND process tree. Stopping it on
    cleanup makes a graceful shutdown idempotent.

    An in-place restart's exec keeps the pid but skips every ``on_cleanup`` hook,
    so ``_browser_view_shutdown`` below would not run and the detached child
    would survive the exec parented to the new image. ``view.stop()`` is also
    registered with :mod:`kiro_crew.pre_exec_stops` so both exec-based restarts
    reap it before the exec; ``_browser_view_shutdown`` unregisters that stop.
    The launcher sessions do NOT need the seam: a previous life's ``panel-``
    daemons are reclaimed at the next startup by owner tag below.

    Registered BEFORE ``runner.setup()`` freezes the app's signal lists --
    appending later raises ``RuntimeError: Cannot modify frozen list``, which is
    exactly what a first attempt at this hook did.

    Best-effort: a failure to reap a supervised child must never block shutdown.

    The browser sessions the panel's address bar opened (``browser_cli.launcher``)
    are closed here too, and first: their daemons are detached processes the
    orphan sweep deliberately never touches (a ``panel-`` name is operator-class
    to it), so this hook is the one place their lifetime ends. Only the sessions
    THIS gateway opened are closed -- never a global ``close-all`` -- so an
    operator's own independently opened browser survives a restart.

    The mirror image runs at startup: a previous life of this gateway that died
    without reaching this hook left its ``panel-`` daemons running, and
    :func:`browser_cli_launcher.reclaim_stranded` closes exactly those -- the
    owner tag in the name keeps a sibling gateway's browsers out of reach. It
    spawns the CLI, so it runs as a background task rather than gating the port
    bind (the same reasoning as the instances revive below).
    """

    async def _browser_sessions_startup(app_: web.Application) -> None:
        # The exec of an in-place restart skips _browser_view_shutdown below but
        # keeps the pid, so the detached `playwright-cli show` child would
        # outlive this image still parented to the new one, which then starts a
        # second process tree on the next view request. The restart runs this
        # same reap first through the pre-exec seam. view.stop() runs under its
        # own lock, is a bounded tree-kill, and never raises; it is sync, so it
        # goes off the loop. The launcher sessions are deliberately NOT in the
        # seam -- reclaim_stranded below closes a prior life's panel- daemons by
        # owner tag at the next startup.
        async def _stop_view_before_exec() -> None:
            await asyncio.to_thread(browser_cli_view.stop)

        state._browser_view_unregister = register_pre_exec_stop(
            "Browser view", _stop_view_before_exec
        )

        async def _reclaim() -> None:
            try:
                await asyncio.to_thread(browser_cli_launcher.reclaim_stranded)
            except Exception:  # noqa: BLE001 - startup must not raise
                logger.debug(
                    "browser launcher session reclaim failed during startup", exc_info=True
                )

        task = asyncio.create_task(_reclaim())
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

    async def _browser_view_shutdown(app_: web.Application) -> None:
        unregister = getattr(state, "_browser_view_unregister", None)
        if unregister is not None:
            unregister()
        try:
            await handlers.close_relay_client(app_)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("browser view relay client close failed during shutdown", exc_info=True)
        try:
            await asyncio.to_thread(browser_cli_launcher.close_all)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("browser launcher session close failed during shutdown", exc_info=True)
        try:
            await asyncio.to_thread(browser_cli_view.stop)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("browser view stop failed during shutdown", exc_info=True)

    app.on_startup.append(_browser_sessions_startup)
    app.on_cleanup.append(_browser_view_shutdown)

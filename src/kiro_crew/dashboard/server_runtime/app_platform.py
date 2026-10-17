"""The app platform's boot work in ``start_dashboard``.

Warming the PreToolUse gate's builtin app names and the materialized-agent snapshot,
reconciling every enabled app's resources, starting the app backends with the App SDK
gateway hooks system, and the bound-port wave started once the listener serves.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        DashboardState,
        cautious_boot,
        init_hook_reconciler,
        init_hooks_system,
        logger,
        on_gateway_shutdown,
        on_gateway_startup,
        refresh_materialized_agents,
        start_deferred_app_backends,
        start_enabled_app_backends,
        stop_hook_reconciler,
        subprocess_executor,
    )


async def _warm_builtin_app_names() -> None:
    """Warm the PreToolUse gate's first-party (builtin) app-name set from the
    shipped manifests, ONCE, on the executor (the discovery walk touches the
    filesystem and must not run on the event loop). The gate's app-own-server
    auto-approve then does a pure in-memory membership test with zero I/O; an
    empty set (should this fail) simply fails closed (owns-server calls prompt).
    """
    try:
        from kiro_crew.apps.execution import (
            builtin_app_agents,
            builtin_app_mcp_servers,
            builtin_app_names,
        )
        from kiro_crew.hooks import (
            set_builtin_app_agents,
            set_builtin_app_mcp_servers,
            set_builtin_app_names,
        )

        names = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), builtin_app_names
        )
        set_builtin_app_names(names)
        servers = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), builtin_app_mcp_servers
        )
        set_builtin_app_mcp_servers(servers)
        # Agent → owning app, so a builtin whose UI is not an app iframe
        # (empty Slot._app) can still auto-approve calls to its OWN server.
        agents = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), builtin_app_agents
        )
        set_builtin_app_agents(agents)
    except Exception:  # noqa: BLE001 — a warm failure only costs an extra prompt
        logger.warning("Failed to warm builtin app-name set for the gate", exc_info=True)


async def _warm_materialized_agents() -> None:
    """Prime the materialized-agent snapshot on the executor. The resolver's read
    path does zero filesystem work, so this boot scan (plus the one
    `_register_agents` does after it writes) is what keeps the snapshot current
    without ever scanning on the event loop.
    """
    try:
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), refresh_materialized_agents
        )
    except Exception:  # noqa: BLE001 — a warm failure only costs one fallback
        logger.debug("Failed to warm materialized agent names", exc_info=True)


async def _reconcile_app_resources() -> None:
    """Reconcile resources (agents / skills / crons / MCP) for every ENABLED app.
    Registration otherwise happens only in the enable path, so an app that
    gains agents or skills in a later version never registers them for a user
    who already enabled it. Runs on the executor: it walks the apps tree and
    writes into ~/.kiro/agents.
    """
    from kiro_crew.apps.bridges import reconcile_enabled_app_resources

    try:
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), reconcile_enabled_app_resources
        )
    except Exception as exc:  # noqa: BLE001 — never block gateway startup
        logger.warning("App resource reconcile failed: %s", exc)


async def _start_app_backends(app: web.Application, state: DashboardState) -> None:
    """Start the enabled apps' backends and wire the App SDK gateway hooks system.

    Runs after the dashboard port is reserved, so every backend is spawned with the
    reserved socket's own name as its origin evidence, and before
    ``runner.setup()``, so an app's startup hooks find its backend running.
    """
    # Start backends for enabled apps on the subprocess_executor bulkhead:
    # the startup stale-reap shells out to `ps` per orphan and may SIGTERM→
    # sleep→SIGKILL for seconds, and start_app_backend blocks on a survival
    # poll — all wedge-prone blocking work that would freeze this event loop
    # if run inline. subprocess_executor (not the default to_thread pool)
    # isolates it so a hung `ps` cannot starve asyncio's default executor
    # (the RFC's bulkhead intent).
    await cautious_boot.pause_before("app backends")
    started_apps = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), start_enabled_app_backends
    )
    if started_apps:
        logger.info("Started %d app backend(s): %s", len(started_apps), ", ".join(started_apps))

    # Both adapters are shared with the enable path (apps/routes.py) so the two
    # entry points cannot drift into giving an app different capabilities.
    from kiro_crew.apps.event_bus import build_broadcast_fn
    from kiro_crew.apps.spawn_sdk import build_spawn_impl

    _app_event_broadcast = build_broadcast_fn(state.broadcast_ws)
    _app_spawn = build_spawn_impl(state.subagents)

    # Initialize App SDK Gateway Hooks system
    init_hooks_system(
        app,
        cron_service=state.crons,
        broadcast_fn=_app_event_broadcast,
        spawn_impl=_app_spawn,
    )

    async def _hooks_startup(app_: web.Application) -> None:
        await on_gateway_startup(
            cron_service=state.crons,
            broadcast_fn=_app_event_broadcast,
            spawn_impl=_app_spawn,
        )
        # App dev-mode live reload: watch dev-flagged apps' ui/ dirs and
        # broadcast app_reload WS events on change (see apps/dev_mode.py).
        from kiro_crew.apps.dev_mode import init_dev_mode_watcher

        await init_dev_mode_watcher(state.broadcast_ws)

        # App hook reconciler: the CLI (`kirocrew app enable/disable/install/
        # uninstall`) mutates apps on disk in a DIFFERENT process and never
        # notifies this gateway, so without reconciliation a CLI reinstall leaves
        # the old backend.hooks module live, its on_startup task running, and its
        # .app_secret stale. This poll reloads changed hooks in-process — the same
        # "CLI writes disk, gateway reconciles" contract already used for crons
        # and UI files. Started AFTER on_gateway_startup so the boot pass has
        # already recorded its loaded-hook signatures in the shared registry and
        # the reconciler's first tick sees no drift. Synchronous + IO-free, so it
        # adds nothing before the dashboard socket binds.
        init_hook_reconciler(
            cron_service=state.crons,
            broadcast_fn=_app_event_broadcast,
            spawn_impl=_app_spawn,
        )

    app.on_startup.append(_hooks_startup)

    async def _hooks_shutdown(app_: web.Application) -> None:
        # Stop the background pollers BEFORE the gateway hook shutdown sweep.
        # The reconciler and the dev-mode watcher can each LOAD/START app hooks
        # on a tick; if either is still live while on_gateway_shutdown() tears
        # hooks down, a poll landing mid-sweep could re-import a module or spawn
        # an on_startup task AFTER it was torn down, so that app's code would
        # survive an in-process gateway restart. Cancelling them first also stops
        # their module-global tasks from leaking stale gateway service handles /
        # a stale broadcast_ws across the restart. Await cancellation so neither
        # can fire one more tick during the sweep.
        from kiro_crew.apps.dev_mode import stop_dev_mode_watcher

        # on_gateway_shutdown() is the sweep that actually tears down app
        # backends; it MUST run even if stopping a poller hangs (its bounded
        # drain can burn its budget) or raises, otherwise a spawned app backend
        # survives gateway exit. Stop the pollers first (preserving the
        # no-tick-during-sweep ordering) but never let a stop failure abort the
        # sweep: catch and log it, then always run on_gateway_shutdown.
        try:
            await stop_dev_mode_watcher()
            await stop_hook_reconciler()
        except Exception:
            logger.exception(
                "Error stopping background pollers on shutdown; proceeding to the "
                "gateway hook shutdown sweep so app backends are torn down"
            )
        await on_gateway_shutdown()

    app.on_cleanup.append(_hooks_shutdown)


async def _start_bound_port_app_backends() -> None:
    """Start the app backends the main wave deferred until the listener serves.

    The backend the main wave deferred (``apps.backend.DEV_FLEET_APP_NAME``):
    ``apps/backend.py`` hands the Dev Fleet backend ``KIROCREW_BOUND_PORT`` at
    spawn. Under ``start_dashboard``'s port reservation that value exists before
    the main wave too, but the admission split lives in
    ``apps/backend_runtime/startup.py`` and is shared with the headless entrypoint,
    where the value only exists post-listen -- so the second wave stays. Same
    bulkhead as the main wave; admission already ran there.
    """
    deferred_apps = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), start_deferred_app_backends
    )
    if deferred_apps:
        logger.info(
            "Started %d bound-port app backend(s): %s", len(deferred_apps), ", ".join(deferred_apps)
        )

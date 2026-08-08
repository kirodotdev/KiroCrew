"""Hooks Integration — wires the hooks system into the gateway lifecycle.

This module provides the glue functions that connect:
- RouteRegistry into the enable/disable flow
- LifecycleDispatcher into gateway startup/shutdown
- CronSDK cleanup into disable/uninstall

These functions are called from routes.py and server.py at the appropriate
lifecycle points. An app that declares no hooks is a no-op.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.bridges import (
    disarm_app_crons_for_execution,
    register_app_crons_with_service,
)
from kiro_crew.apps.context import build_app_context
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.execution import (
    app_execution_denied,
    shipped_builtin_app_root,
)
from kiro_crew.apps.lifecycle import LifecycleDispatcher
from kiro_crew.apps.manager import app_dir, list_apps
from kiro_crew.apps.route_registry import RouteRegistry
from kiro_crew.cron import CronStoreBusy
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Module-level singletons (initialized at gateway startup)
_route_registry: RouteRegistry | None = None
_lifecycle_dispatcher: LifecycleDispatcher | None = None


def init_hooks_system(
    app: web.Application,
    *,
    cron_service: Any = None,
    broadcast_fn: Any = None,
    spawn_impl: Any = None,
) -> None:
    """Initialize the hooks system at gateway startup.

    Called from server.py after all core routes are registered.
    """
    global _route_registry, _lifecycle_dispatcher

    _route_registry = RouteRegistry(app)
    _route_registry.ensure_catch_all()

    _lifecycle_dispatcher = LifecycleDispatcher(
        cron_service=cron_service,
        broadcast_fn=broadcast_fn,
        spawn_impl=spawn_impl,
    )

    logger.info("Hooks system initialized")


def get_route_registry() -> RouteRegistry | None:
    """Get the global RouteRegistry instance."""
    return _route_registry


def get_lifecycle_dispatcher() -> LifecycleDispatcher | None:
    """Get the global LifecycleDispatcher instance."""
    return _lifecycle_dispatcher


def _app_hook_root(app_name: str) -> Path:
    """Return the immutable shipped root when one owns this app name."""
    return shipped_builtin_app_root(app_name) or app_dir(app_name)


def _build_app_context_from_info(
    app_info: dict[str, Any],
    cron_service: Any = None,
    broadcast_fn: Any = None,
    spawn_impl: Any = None,
) -> Any:
    """Build an AppContext from app info dict — shared helper for consistent context."""
    name = app_info.get("name", "")
    manifest = app_info.get("manifest", {})
    permissions = manifest.get("permissions", {})
    data_path = app_dir(name) / "data"
    data_path.mkdir(parents=True, exist_ok=True)
    return build_app_context(
        app_name=name,
        data_dir=data_path,
        permissions=permissions,
        cron_service=cron_service,
        broadcast_fn=broadcast_fn,
        spawn_impl=spawn_impl,
        app_config=manifest.get("extra", {}),
    )


async def on_app_enable(
    app_name: str,
    app_info: dict[str, Any],
    *,
    cron_service: Any = None,
    broadcast_fn: Any = None,
    spawn_impl: Any = None,
) -> dict[str, Any]:
    """Called after an app is enabled — register routes and invoke startup hook.

    Returns dict with hook results to include in the enable response.
    """
    result: dict[str, Any] = {}
    denied = app_execution_denied(
        app_name,
        action="hook_enable_register",
        app_root=_app_hook_root(app_name),
        caller="gateway",
    )
    if denied:
        logger.warning(
            "App %s: skipping enable-time hooks and crons: %s",
            app_name,
            denied,
        )
        if cron_service is not None:
            await disarm_app_crons_for_execution(app_name, cron_service)
        if _route_registry:
            _route_registry.deregister_app_routes(app_name)
        return result

    manifest = app_info.get("manifest", {})
    backend = manifest.get("backend", {})
    hooks = backend.get("hooks", {})

    # Promote app-declared crons into the running scheduler.
    try:
        # register_app_crons_with_service is async: it awaits the async CronSDK
        # mutation API, which offloads each bounded store-lock spin (whose
        # contention spin does time.sleep) to a worker thread. Awaiting it here
        # never parks the gateway loop, and timer arming is owned by CronService
        # (no caller-side re-arm needed).
        registered = await register_app_crons_with_service(app_name, cron_service)
        if registered:
            result["crons_registered"] = registered
        sel().log_api_access(
            caller="gateway",
            operation="app_crons_register",
            outcome="completed",
            resources=f"app={app_name} crons={registered}",
        )
    except Exception as exc:
        logger.warning("Cron registration failed for %s: %s", app_name, exc)
        sel().log_api_access(
            caller="gateway",
            operation="app_crons_register",
            outcome="failed",
            resources=app_name,
            error=str(exc),
        )

    if not hooks:
        return result

    sel().log_api_access(
        caller="gateway",
        operation="app_hooks_enable",
        outcome="started",
        resources=app_name,
    )

    # Build AppContext for this app (shared helper ensures consistency)
    ctx = _build_app_context_from_info(app_info, cron_service, broadcast_fn, spawn_impl)

    # Register routes if declared
    routes_hook = hooks.get("routes", "")
    if routes_hook and _route_registry:
        app_root = _app_hook_root(app_name)
        registered = await _route_registry.register_app_routes(
            app_name, app_root, routes_hook, ctx
        )
        if registered:
            result["hooks_routes"] = registered

    # Invoke on_startup hook if declared
    startup_hook = hooks.get("on_startup", "")
    if startup_hook and _lifecycle_dispatcher:
        success = await _lifecycle_dispatcher._invoke(app_name, startup_hook, ctx)
        result["hooks_startup"] = "ok" if success else "failed"

    # Report health status
    if ctx.health.status != "healthy":
        result["health_status"] = ctx.health.to_dict()

    sel().log_api_access(
        caller="gateway",
        operation="app_hooks_enable",
        outcome="completed",
        resources=app_name,
    )
    return result


async def on_app_disable(
    app_name: str, app_info: dict[str, Any], *, run_app_hooks: bool = True
) -> dict[str, Any]:
    """Called before an app is disabled — deregister routes and invoke shutdown hook.

    ``run_app_hooks=False`` skips the app's OWN ``on_shutdown`` hook while still
    doing everything the GATEWAY owns (route deregistration, cron cleanup). The
    caller passes it when there is no reason to believe the app is running: its
    shutdown hook is third-party code, and *starting* that code as part of
    withdrawing its permission to run would turn the security operation into an
    execution vector. Nothing that STOPS something is ever skipped by this flag —
    see the split in ``apps/teardown.py``.
    """
    result: dict[str, Any] = {}
    manifest = app_info.get("manifest", {})
    backend = manifest.get("backend", {})
    hooks = backend.get("hooks", {})

    if hooks:
        sel().log_api_access(
            caller="gateway",
            operation="app_hooks_disable",
            outcome="started",
            resources=app_name,
        )

    # Invoke on_shutdown hook if declared
    shutdown_hook = hooks.get("on_shutdown", "")
    if shutdown_hook and _lifecycle_dispatcher and run_app_hooks:
        success = await _lifecycle_dispatcher._invoke(
            app_name,
            shutdown_hook,
            _lifecycle_dispatcher._build_context(app_info),
        )
        result["hooks_shutdown"] = "ok" if success else "failed"

    # Deregister routes
    if _route_registry:
        _route_registry.deregister_app_routes(app_name)

    # Clean up cron jobs owned by this app
    permissions = manifest.get("permissions", {})
    if permissions.get("cron"):
        # We need the cron_service — get it from the lifecycle dispatcher
        if _lifecycle_dispatcher and _lifecycle_dispatcher._cron_service:
            cron_service = _lifecycle_dispatcher._cron_service
            sdk = CronSDK(app_name, cron_service)
            # remove_all_async removes every owned job in ONE atomic
            # CronService.remove_jobs transaction (store-lock spin offloaded to
            # a worker thread; timer arming owned by CronService) — all-or-
            # nothing, never a partial removal that orphans still-enabled jobs.
            # A contended store raises CronStoreBusy; REPORT it (rather than
            # crash the disable or claim a false success) so the caller sees the
            # cleanup did not complete and the app's jobs may still be enabled.
            try:
                removed = await sdk.remove_all_async()
                if removed:
                    result["cron_cleanup"] = f"removed {removed} job(s)"
            except CronStoreBusy as exc:
                logger.warning(
                    "App %s: cron cleanup could not complete on disable — " "store busy: %s",
                    app_name,
                    exc,
                )
                result["cron_cleanup"] = "failed: cron store busy — jobs may still be enabled"
                sel().log_api_access(
                    caller="gateway",
                    operation="app_crons_deregister",
                    outcome="failed",
                    resources=app_name,
                    error=str(exc),
                )

    return result


async def on_gateway_startup(
    *, cron_service: Any = None, broadcast_fn: Any = None, spawn_impl: Any = None
) -> None:
    """Called during gateway startup — register routes then invoke on_startup hooks.

    Order matches on_app_enable: routes first, then startup hooks.
    Should be called after init_hooks_system() and after all apps are registered.
    """
    if not _lifecycle_dispatcher:
        return

    enabled = [a for a in list_apps() if a.get("enabled")]
    if not enabled:
        return

    # Step 1 & 2: Share a single AppContext per app for both routes and startup hooks.
    # This ensures health status changes made by the startup hook are visible to
    # route handlers (and vice versa), matching the on_app_enable approach.
    for app_info in sorted(enabled, key=lambda a: a.get("name", "")):
        name = app_info.get("name", "")
        denied = app_execution_denied(
            name,
            action="hook_boot_register",
            app_root=_app_hook_root(name),
            caller="gateway",
        )
        if denied:
            logger.warning(
                "Startup: skipping hooks and crons for denied app %s: %s",
                name,
                denied,
            )
            if cron_service is not None:
                await disarm_app_crons_for_execution(name, cron_service)
            if _route_registry:
                _route_registry.deregister_app_routes(name)
            continue

        # Reconcile app-declared crons into the running scheduler.
        if cron_service is not None:
            try:
                # Async register: awaits the CronSDK mutation API (bounded
                # store-lock spin offloaded to a worker thread), so the gateway
                # loop is never parked; timer arming is owned by CronService.
                registered = await register_app_crons_with_service(name, cron_service)
                if registered:
                    logger.info(
                        "Startup: registered %d cron(s) for app %s: %s",
                        len(registered),
                        name,
                        ", ".join(registered),
                    )
                sel().log_api_access(
                    caller="gateway",
                    operation="app_crons_register",
                    outcome="completed",
                    resources=f"app={name} crons={registered}",
                )
            except Exception as exc:
                logger.exception("Startup: cron registration failed for %s", name)
                sel().log_api_access(
                    caller="gateway",
                    operation="app_crons_register",
                    outcome="failed",
                    resources=name,
                    error=str(exc),
                )

        manifest = app_info.get("manifest", {})
        hooks = manifest.get("backend", {}).get("hooks", {})
        if not hooks:
            continue

        ctx = _build_app_context_from_info(app_info, cron_service, broadcast_fn, spawn_impl)

        # Register routes (if declared)
        routes_hook = hooks.get("routes", "")
        if routes_hook and _route_registry:
            await _route_registry.register_app_routes(
                name, _app_hook_root(name), routes_hook, ctx
            )

        # Invoke on_startup hook (if declared)
        startup_hook = hooks.get("on_startup", "")
        if startup_hook and _lifecycle_dispatcher:
            success = await _lifecycle_dispatcher._invoke(name, startup_hook, ctx)
            if success:
                logger.info("Startup hook invoked for: %s", name)


async def on_gateway_shutdown() -> None:
    """Called during gateway shutdown — invoke on_shutdown hooks for all enabled apps."""
    if not _lifecycle_dispatcher:
        return

    enabled = [a for a in list_apps() if a.get("enabled")]
    apps_with_hooks = [
        a
        for a in enabled
        if a.get("manifest", {}).get("backend", {}).get("hooks", {}).get("on_shutdown")
    ]

    if apps_with_hooks:
        invoked = await _lifecycle_dispatcher.dispatch_shutdown(apps_with_hooks)
        if invoked:
            logger.info("Shutdown hooks invoked for: %s", ", ".join(invoked))

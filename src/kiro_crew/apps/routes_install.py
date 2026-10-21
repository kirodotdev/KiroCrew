"""Updating an app in place, and registering an external app.

``handle_update_app`` with the cron resync an update drives, and
``handle_register_external``. A local install (``handle_install_app``) stays in
``apps/routes.py``.

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from typing import Any

from aiohttp import web

from kiro_crew.apps.backend import start_app_backend, stop_app_backend
from kiro_crew.apps.bridges import register_app_crons_with_service
from kiro_crew.apps.manager import app_lifecycle_lock, get_app, register_external_app, update_app
from kiro_crew.apps.registry import (
    install_from_registry,
    is_registry_source,
    registry_name_from_source,
)
from kiro_crew.apps.routes_lifecycle import (
    _app_may_run_after_install,
    _deregister_app_off_loop,
    _refuse_while_startup_hook_runs,
    _register_app_off_loop,
    _restore_app_after_failed_update,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")


async def handle_update_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/update — update an installed app from its source path."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "app_update")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    # Apps with lifecycle != "gateway" handle their own updates
    lifecycle = info.get("lifecycle", "gateway")
    if lifecycle != "gateway":
        return web.json_response(
            {
                "error": f"app {name!r} has lifecycle={lifecycle!r} — cannot be updated via this endpoint"
            },
            status=400,
        )

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}

    source = body.get("source", info.get("source", ""))

    # Registry-installed apps: re-clone from registry.
    # Same order as the local-source branch below: stop the backend and scrub
    # its resources BEFORE the files are replaced. For an already-installed
    # app ``install_from_registry`` reaches ``update_app``, which renames the
    # live tree aside and copies the new one in -- on Windows that fails with a
    # sharing violation (WinError 32) while the backend still holds a file open
    # under the tree. A failed install must not leave the app broken either:
    # the failure path restores what was there (``_restore_app_after_failed_update``),
    # exactly as the local-source branch does.
    if is_registry_source(source):
        registry_name = registry_name_from_source(source)
        async with app_lifecycle_lock(name):
            # Preflight BEFORE the stop so a retryable refusal leaves app state
            # untouched (app-kit-platform: refusal "without mutating app state").
            # ``install_from_registry`` re-checks at its own replacement boundary.
            startup_refusal = await _refuse_while_startup_hook_runs(name, action="update")
            if startup_refusal is not None:
                return startup_refusal

            # Stop the backend, then deregister old resources -- same order as
            # uninstall and the disable rollback. Stopping pops the tracking record,
            # so the health watch cannot re-register the OLD manifest's MCP servers
            # after the scrub (see app-kit-platform §17).
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), stop_app_backend, name
            )
            await _deregister_app_off_loop(name)

            reg_install = await install_from_registry(registry_name)
            if not reg_install.get("ok"):
                await _restore_app_after_failed_update(name)
                sel().log_api_access(
                    caller="dashboard",
                    operation="app_update",
                    outcome="failed",
                    resources=name,
                    error=reg_install.get("error", ""),
                )
                return web.json_response(reg_install, status=400)
            # Live read, not the pre-update ``info`` snapshot: ``update_app`` drops
            # ``enabled`` when the new version adds ``permissions.sessionApproval``,
            # and a backend started here would run an app the UI shows as disabled.
            still_enabled = await _app_may_run_after_install(name)
            if still_enabled:
                reg_result = await _register_app_off_loop(name)
                await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), start_app_backend, name
                )
                reg_install["registration"] = reg_result.to_dict()
        sel().log_api_access(
            caller="dashboard", operation="app_update", outcome="completed", resources=name
        )
        return web.json_response(reg_install)

    if not source:
        return web.json_response(
            {"error": "source path required (not found in installed metadata)"},
            status=400,
        )

    # Per-app lifecycle lock: the stop → deregister → copy → re-register
    # sequence must not interleave with another update/install/uninstall of
    # the same app — update_app moves user data through a shared
    # ``.{name}-data-tmp`` path, so an interleaving can destroy it.
    # (The registry branch above holds the same lock around the same sequence.)
    async with app_lifecycle_lock(name):
        startup_refusal = await _refuse_while_startup_hook_runs(name, action="update")
        if startup_refusal is not None:
            return startup_refusal

        # Stop the backend, then deregister old resources — same order as uninstall and
        # the disable rollback. Stopping pops the tracking record, so the health watch
        # cannot re-register the OLD manifest's MCP servers after the scrub
        # (see app-kit-platform §17).
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), stop_app_backend, name
        )
        await _deregister_app_off_loop(name)

        # Off-loop: blocking filesystem copy (see handle_install_app).
        # expected_name makes update_app itself reject a source whose
        # manifest names a different app than the one this lock guards.
        up_result = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), lambda: update_app(source, expected_name=name)
        )
        if not up_result.ok:
            await _restore_app_after_failed_update(name)
            sel().log_api_access(
                caller="dashboard",
                operation="app_update",
                outcome="failed",
                resources=name,
                error=up_result.error,
            )
            return web.json_response(up_result.to_dict(), status=400)

        # Re-register with the new manifest only if the app is STILL enabled.
        # ``update_app`` drops ``enabled`` when the new version adds
        # ``permissions.sessionApproval``, so the pre-update ``info`` snapshot
        # would start a backend the user has not re-consented to.
        up_reg = None
        still_enabled = await _app_may_run_after_install(name)
        if still_enabled:
            up_reg = await _register_app_off_loop(name)
            # Promote the rewritten app-crons.json into the RUNNING scheduler
            # now. register_app_crons_with_service re-applies a changed entry to
            # the job it already registered, but its other callers are enable,
            # gateway start and the CLI -- so without this call an updated
            # cron field kept its old value until the next gateway start, with
            # nothing saying so. Async: it awaits the CronSDK mutators, which
            # offload each store-lock spin, so the loop is never parked. A
            # failure is logged and audited, never a failed update: the files
            # and registry are already current, and the next registration
            # pass applies the same definitions.
            await _resync_app_crons_after_update(request, name)
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), start_app_backend, name
            )

    sel().log_api_access(
        caller="dashboard", operation="app_update", outcome="completed", resources=name
    )
    resp: dict[str, Any] = up_result.to_dict()
    if up_reg:
        resp["registration"] = up_reg.to_dict()
    return web.json_response(resp)


async def _resync_app_crons_after_update(request: web.Request, name: str) -> None:
    """Register the updated app's crons with the running scheduler, if there is one.

    Split out of :func:`handle_update_app` so the no-service case (a test app
    with no ``state.crons``, a gateway booting without a scheduler) and the
    audit shape stay readable there. Mirrors ``hooks_integration.on_app_enable``:
    the same bridge call, the same ``app_crons_register`` SEL rows.
    """
    state = request.app.get("state")
    cron_service = getattr(state, "crons", None) if state else None
    if cron_service is None:
        return
    try:
        registered = await register_app_crons_with_service(name, cron_service)
        sel().log_api_access(
            caller="dashboard",
            operation="app_crons_register",
            outcome="completed",
            resources=f"app={name} crons={registered}",
        )
    except Exception as exc:
        logger.warning("Cron registration after updating %s failed: %s", name, exc)
        sel().log_api_access(
            caller="dashboard",
            operation="app_crons_register",
            outcome="failed",
            resources=name,
            error=str(exc),
        )


async def handle_register_external(request: web.Request) -> web.Response:
    """POST /api/apps/register — register a self-managed app.

    Self-managed apps handle their own agent/skill/MCP registration.
    Kiro Crew only tracks metadata so the dashboard can display them.
    Idempotent — calling again with a newer version updates the entry.

    Body: { name, version, displayName, source?, manifest? }
    """
    # local import: avoids a circular import with dashboard.handlers
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "app_register_external")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    name = body.get("name", "")
    version = body.get("version", "")
    display_name = body.get("displayName", "")
    if not name or not version or not display_name:
        return web.json_response(
            {"error": "name, version, and displayName are required"},
            status=400,
        )

    # Registry provenance is server-owned. A self-registering process may
    # refresh its display metadata, but it cannot mint the marker that makes a
    # later update resolve by registry name or claim a registry origin. Existing
    # registry installs are already identified by their durable sourceUrl, which
    # register_external_app preserves independently of these request fields.
    source = body.get("source", "")
    source = source if isinstance(source, str) else ""
    if source == "builtin" or is_registry_source(source):
        source = ""

    # Share the install/update/uninstall lock across the complete metadata
    # transaction. Without it, this read-modify-write could restore a stale
    # sourceUrl/provenance snapshot over a concurrent registry transition.
    # The manager call performs blocking filesystem and secret-store I/O, so it
    # belongs in the subprocess executor while the loop owns the lock.
    async with app_lifecycle_lock(name):
        result = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            partial(
                register_external_app,
                name=name,
                version=version,
                display_name=display_name,
                source=source,
                manifest_data=body.get("manifest"),
                origin="external",
                resources=body.get("resources", "app"),
                lifecycle=body.get("lifecycle", "app"),
            ),
        )
    if not result.ok:
        sel().log_api_access(
            caller="dashboard",
            operation="app_register_external",
            outcome="failed",
            resources=name,
            error=result.error,
        )
        return web.json_response(result.to_dict(), status=400)
    sel().log_api_access(
        caller="dashboard", operation="app_register_external", outcome="completed", resources=name
    )
    resp = result.to_dict()
    # Include the generated app secret so the caller can use it for auth
    if result.secret:
        resp["secret"] = result.secret
    return web.json_response(resp, status=201)

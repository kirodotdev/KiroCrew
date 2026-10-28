"""Installing an app from a local path, updating one in place, and registering an external app.

``handle_install_app``, ``handle_update_app`` and the :func:`apply_app_update` sequence it
shares with the automatic updater (with the cron resync an update drives), and
``handle_register_external``.

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.apps.backend import start_app_backend, stop_app_backend
from kiro_crew.apps.bridges import register_app_crons_with_service
from kiro_crew.apps.manager import (
    app_lifecycle_lock,
    get_app,
    install_app,
    refresh_app_config_keys,
    register_external_app,
    update_app,
)
from kiro_crew.apps.registry import (
    install_from_registry,
    is_registry_source,
    registry_name_from_source,
)
from kiro_crew.apps.routes_lifecycle import (
    _app_may_run_after_install,
    _check_min_version,
    _deregister_app_off_loop,
    _refuse_while_startup_hook_runs,
    _register_app_off_loop,
    _restore_app_after_failed_update,
    _start_backend_after_install,
    _suspend_app_for_session_approval_reconsent,
)
from kiro_crew.apps.routes_proxy import invalidate_app_secret_cache
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("kiro_crew.apps.routes")


async def handle_update_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/update — update an installed app from its source path."""
    from kiro_crew.dashboard.handlers._shared import (
        body_not_object_error,
        require_owner_dashboard_request,
    )

    denied = await require_owner_dashboard_request(request, "app_update")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    source = body.get("source")
    state = request.app.get("state")
    status, payload = await apply_app_update(
        request.match_info["name"],
        source=source if isinstance(source, str) else None,
        cron_service=getattr(state, "crons", None) if state else None,
    )
    if status < 400:
        return web.json_response(payload)
    # Every refusal carries a machine-readable ``code``; a manager error with
    # none of its own gets the generic one.
    return web.json_response(
        {**payload, "code": payload.get("code") or "app_update_failed"}, status=status
    )


async def _startup_refusal_body(name: str) -> tuple[int, dict[str, Any]] | None:
    """:func:`_refuse_while_startup_hook_runs` as a status and body, or ``None``."""
    refusal = await _refuse_while_startup_hook_runs(name, action="update")
    if refusal is None:
        return None
    return refusal.status, json.loads(refusal.text or "{}")


async def apply_app_update(
    name: str,
    *,
    source: str | None = None,
    cron_service: Any = None,
    caller: str = "dashboard",
    refuse_permission_widening: bool = False,
    still_idle: Callable[[], Awaitable[str]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Update the installed app *name* and answer ``(http status, body)``.

    The one update sequence, shared by the Update button
    (:func:`handle_update_app`) and the automatic updater
    (``kiro_crew.apps.auto_update``), so an update nobody pressed takes the same
    steps a pressed one does: under the app lifecycle lock, the admission
    re-check, backend stop, deregistration, the copy that keeps ``data/``,
    re-registration including the running scheduler's crons, backend start, and
    restoring the old version when the copy fails.

    *source* overrides the recorded source (the route's request body). With
    *refuse_permission_widening*, an update whose new version declares a
    permission the installed one does not is refused with
    ``app_update_needs_consent`` before any file moves.

    *still_idle*, when given, is awaited under the lifecycle lock just before the
    backend stops; a non-empty answer (why the app is busy) puts the update off
    with a retryable 409 and changes nothing. The automatic updater passes its
    idle check here so work that started after its first look is not cut off.

    The installed record is read again under the lock: an uninstall or a source
    change that won the lock first ends the update there, so a queued update
    can never reinstall an app the owner just removed.
    """
    info = await asyncio.to_thread(get_app, name)
    if not info:
        return 404, {"error": f"app {name!r} not installed", "code": "app_not_installed"}

    # Apps with lifecycle != "gateway" handle their own updates
    lifecycle = info.get("lifecycle", "gateway")
    if lifecycle != "gateway":
        return 400, {
            "error": f"app {name!r} has lifecycle={lifecycle!r} — cannot be updated via this endpoint",
            "code": "app_lifecycle_self_managed",
        }

    # Local: these three would otherwise become names the routes facade serves.
    from contextlib import nullcontext

    from kiro_crew.apps.update_consent import refusing_permission_widening

    recorded = info.get("source", "")
    recorded = recorded if isinstance(recorded, str) else ""
    source = source or recorded
    scope = refusing_permission_widening() if refuse_permission_widening else nullcontext()
    with scope:
        if is_registry_source(source):
            return await _update_from_registry(
                name, source, cron_service, caller, still_idle, recorded_source=recorded
            )
        if not source:
            return 400, {
                "error": "source path required (not found in installed metadata)",
                "code": "app_source_missing",
            }
        return await _update_from_path(name, source, cron_service, caller, still_idle)


async def _locked_preflight(
    name: str, still_idle: Callable[[], Awaitable[str]] | None
) -> tuple[int, dict[str, Any]] | None:
    """The checks an update makes once it holds the lifecycle lock, before it stops anything.

    The app must still be installed, no retained startup hook may be running,
    and *still_idle* (when given) must answer ``""``.
    """
    if await asyncio.to_thread(get_app, name) is None:
        return 404, {"error": f"app {name!r} not installed", "code": "app_not_installed"}
    startup_refusal = await _startup_refusal_body(name)
    if startup_refusal is not None:
        return startup_refusal
    if still_idle is not None:
        busy = await still_idle()
        if busy:
            return 409, {
                "error": f"cannot update {name!r} now: {busy}",
                "code": "app_busy",
                "retryable": True,
                "app": name,
            }
    return None


async def _update_from_registry(
    name: str,
    source: str,
    cron_service: Any,
    caller: str,
    still_idle: Callable[[], Awaitable[str]] | None = None,
    *,
    recorded_source: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Re-clone a registry-installed app and swap it in.

    *recorded_source* is the installed record's source as read before the lock,
    which the locked re-read is compared with. It differs from *source* when the
    request names a source override; ``None`` means *source* is the recorded one.

    Same order as :func:`_update_from_path`: stop the backend and scrub its
    resources BEFORE the files are replaced. For an already-installed app
    ``install_from_registry`` reaches ``update_app``, which renames the live tree
    aside and copies the new one in -- on Windows that fails with a sharing
    violation (WinError 32) while the backend still holds a file open under the
    tree. A failed install must not leave the app broken either: the failure
    path restores what was there (``_restore_app_after_failed_update``).
    """
    registry_name = registry_name_from_source(source)
    async with app_lifecycle_lock(name):
        # Preflight BEFORE the stop so a retryable refusal leaves app state
        # untouched (app-kit-platform: refusal "without mutating app state").
        # ``install_from_registry`` re-checks at its own replacement boundary.
        refusal = await _locked_preflight(name, still_idle)
        if refusal is not None:
            return refusal
        # Re-read under the lock: an earlier holder may have moved the app to a
        # different source, and this update must not act on the stale one.
        current = await asyncio.to_thread(get_app, name)
        expected = source if recorded_source is None else recorded_source
        now_source = current.get("source", "") if current else ""
        # Coerced as ``recorded_source`` was, so a non-string field compares equal.
        now_source = now_source if isinstance(now_source, str) else ""
        if not current or now_source != expected:
            return 409, {
                "error": f"app {name!r} changed while this update waited; nothing was updated",
                "code": "app_changed",
                "retryable": True,
            }

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
                caller=caller,
                operation="app_update",
                outcome="failed",
                resources=name,
                error=reg_install.get("error", ""),
            )
            return 400, reg_install
        # Live read, not a pre-update snapshot: ``update_app`` drops ``enabled``
        # when the new version adds ``permissions.sessionApproval``, and a
        # backend started here would run an app the UI shows as disabled.
        if await _app_may_run_after_install(name):
            reg_result = await _register_app_off_loop(name)
            await _resync_app_crons_after_update(cron_service, name, caller)
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), start_app_backend, name
            )
            reg_install["registration"] = reg_result.to_dict()
    sel().log_api_access(caller=caller, operation="app_update", outcome="completed", resources=name)
    return 200, reg_install


async def _update_from_path(
    name: str,
    source: str,
    cron_service: Any,
    caller: str,
    still_idle: Callable[[], Awaitable[str]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Copy a local source directory over the installed app."""
    # Per-app lifecycle lock: the stop → deregister → copy → re-register
    # sequence must not interleave with another update/install/uninstall of
    # the same app — update_app moves user data through a shared
    # ``.{name}-data-tmp`` path, so an interleaving can destroy it.
    async with app_lifecycle_lock(name):
        refusal = await _locked_preflight(name, still_idle)
        if refusal is not None:
            return refusal

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
        # ``copy_context`` carries the permission-widening scope into the
        # executor thread, which ``run_in_executor`` alone would not.
        import contextvars

        ctx = contextvars.copy_context()
        up_result = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), lambda: ctx.run(update_app, source, expected_name=name)
        )
        if not up_result.ok:
            await _restore_app_after_failed_update(name)
            sel().log_api_access(
                caller=caller,
                operation="app_update",
                outcome="failed",
                resources=name,
                error=up_result.error,
            )
            return 400, up_result.to_dict()

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
            await _resync_app_crons_after_update(cron_service, name, caller)
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), start_app_backend, name
            )

    sel().log_api_access(caller=caller, operation="app_update", outcome="completed", resources=name)
    resp: dict[str, Any] = up_result.to_dict()
    if up_reg:
        resp["registration"] = up_reg.to_dict()
    return 200, resp


async def _resync_app_crons_after_update(cron_service: Any, name: str, caller: str) -> None:
    """Bring the running scheduler's jobs for *name* in line with its updated manifest.

    Both update branches call it, so the no-service case (a test app with no
    ``state.crons``, a gateway booting without a scheduler) and the audit shape
    live in one place. Mirrors ``hooks_integration.on_app_enable``: the same
    bridge call, the same ``app_crons_register`` SEL rows.
    """
    if cron_service is None:
        return
    try:
        registered = await register_app_crons_with_service(name, cron_service)
        sel().log_api_access(
            caller=caller,
            operation="app_crons_register",
            outcome="completed",
            resources=f"app={name} crons={registered}",
        )
    except Exception as exc:
        logger.warning("Cron registration after updating %s failed: %s", name, exc)
        sel().log_api_access(
            caller=caller,
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
    from kiro_crew.dashboard.handlers._shared import (
        body_not_object_error,
        require_owner_dashboard_request,
    )

    denied = await require_owner_dashboard_request(request, "app_register_external")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object

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
        if result.ok:
            # The manifest just written can declare config_keys; republish the
            # snapshot config validation reads, as install and update do. A
            # directory scan, so it runs in the same executor.
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), refresh_app_config_keys
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


async def handle_install_app(request: web.Request) -> web.Response:
    """POST /api/apps/install — install an app from a local path."""
    from kiro_crew.dashboard.handlers._shared import (
        body_not_object_error,
        require_owner_dashboard_request,
    )

    denied = await require_owner_dashboard_request(request, "app_install")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object

    source = body.get("source", "")
    if not source:
        return web.json_response({"error": "source path required"}, status=400)

    # Check minKiroCrewVersion before installing
    source_path = await asyncio.to_thread(Path(source).expanduser().resolve)
    if not await asyncio.to_thread(source_path.is_dir):
        detail = f"source is not a directory: {source_path}"
        sel().log_api_access(
            caller="dashboard",
            operation="app_install",
            outcome="failed",
            resources=str(source_path),
            error=detail,
        )
        return web.json_response(
            {
                "ok": False,
                "name": "",
                "error": detail,
                "code": "source_not_directory",
            },
            status=400,
        )

    manifest_path = source_path / "app.json"
    lock_name: str | None = None
    if await asyncio.to_thread(manifest_path.is_file):
        try:
            manifest_data = json.loads(
                await asyncio.to_thread(manifest_path.read_text, encoding="utf-8")
            )
            if not isinstance(manifest_data, dict):
                # Valid JSON that is not an object is a settled invalid manifest,
                # not a manifest caught mid-write, so it gets install_app's own
                # invalid-manifest answer instead of the retryable 409 below.
                detail = (
                    "invalid app.json: app.json must be a JSON object, "
                    f"got {type(manifest_data).__name__}"
                )
                sel().log_api_access(
                    caller="dashboard",
                    operation="app_install",
                    outcome="failed",
                    resources=str(source_path),
                    error=detail,
                )
                return web.json_response(
                    {"ok": False, "name": "", "error": detail, "code": "manifest_not_object"},
                    status=400,
                )
            ver_err = _check_min_version(manifest_data)
            if ver_err:
                return web.json_response({"error": ver_err}, status=400)
            raw_name = manifest_data.get("name")
            if isinstance(raw_name, str) and raw_name:
                lock_name = raw_name
        except (json.JSONDecodeError, OSError):
            pass

    # A path is not an app identity: install_app may observe a manifest created
    # or replaced after this preflight and target a different lifecycle lock.
    if lock_name is None:
        detail = "app manifest identity is unavailable or unreadable"
        sel().log_api_access(
            caller="dashboard",
            operation="app_install",
            outcome="denied",
            resources=str(source_path),
            error=detail,
        )
        return web.json_response(
            {
                "error": (
                    "cannot install while app.json has no readable app identity; "
                    "retry after the source manifest is stable"
                ),
                "code": "app_identity_unavailable",
                "retryable": True,
            },
            status=409,
        )

    # Per-app lifecycle lock (shared with registry installs), held across
    # the whole install transaction — copy, registration, and backend start —
    # so a concurrent uninstall cannot deregister between our copy and our
    # register, leaving a running backend for a removed app.
    async with app_lifecycle_lock(lock_name):
        startup_refusal = await _refuse_while_startup_hook_runs(lock_name, action="install")
        if startup_refusal is not None:
            return startup_refusal

        # Off-loop: the copy in install_app is blocking filesystem I/O that can
        # take minutes on large source trees — running it on the loop would trip
        # the loop-stall watchdog and kill the gateway.
        result = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            partial(install_app, source, expected_name=lock_name),
        )
        if not result.ok:
            sel().log_api_access(
                caller="dashboard",
                operation="app_install",
                outcome="failed",
                resources=source,
                error=result.error,
            )
            return web.json_response(result.to_dict(), status=400)
        invalidate_app_secret_cache(result.name)

        # Same gate as the registry paths: a fresh install whose manifest declares
        # ``permissions.sessionApproval`` is consent-pending, so its resources
        # are not registered and its backend does not start until the user
        # enables it from the disclosure surface.
        if await _app_may_run_after_install(result.name, fresh_install=True):
            # Auto-register resources
            reg = await _register_app_off_loop(result.name)
            # Spawn the backend now so the app is reachable without a gateway
            # reboot (see _start_backend_after_install). No-op for backend-less apps.
            await _start_backend_after_install(result.name)
        else:
            reg = await _suspend_app_for_session_approval_reconsent(result.name)
    sel().log_api_access(
        caller="dashboard", operation="app_install", outcome="completed", resources=result.name
    )
    return web.json_response(
        {
            **result.to_dict(),
            "registration": reg.to_dict(),
        },
        status=201,
    )

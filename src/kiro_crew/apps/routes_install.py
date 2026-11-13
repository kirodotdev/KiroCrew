"""Installing an app from a local path, updating one in place, and registering an external app.

``handle_install_app``, ``handle_update_app`` with the cron resync an update drives, and
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
from typing import Any

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
    git_unavailable_refusal,
    is_registry_source,
    registry_name_from_source,
)
from kiro_crew.apps.routes_lifecycle import (
    _announce_app_ui_changed,
    _app_may_run_after_install,
    _backend_platform_refusal,
    _check_min_version,
    _deregister_app_off_loop,
    _prune_dropped_resources,
    _refuse_while_startup_hook_runs,
    _register_app_off_loop,
    _reinstall_from_registry,
    _rerun_on_enable_after_update,
    _restore_app_after_failed_update,
    _start_backend_after_install,
    _suspend_app_for_session_approval_reconsent,
)
from kiro_crew.apps.routes_proxy import invalidate_app_secret_cache
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel
from kiro_crew.user_json import loads_user_json

logger = logging.getLogger("kiro_crew.apps.routes")


async def handle_update_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/update — update an installed app from its source path.

    ``source`` (optional) overrides the recorded source; ``registry:<name>``
    updates the app from the registry. ``trustRepository`` (optional, only with
    that source) is the consent proof from an ``app_trust_local_only`` refusal:
    the app's local-only grant is switched to that repository for the update and
    put back if the update fails (:mod:`kiro_crew.apps.local_trust_switch`).
    """
    # Deferred like the gate below: the switch reaches the trust writer in the
    # dashboard handlers package, which imports this route family.
    from kiro_crew.apps import local_trust_switch
    from kiro_crew.dashboard.handlers._shared import (
        body_not_object_error,
        require_owner_dashboard_request,
        string_field_error,
    )

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
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object

    source = body.get("source", info.get("source", ""))
    if (wrong_type := string_field_error("source", source)) is not None:
        return wrong_type

    # Consent to the repository a registry update installs, sent when the app's
    # grant covers its local folder only (the ``app_trust_local_only`` refusal
    # carries this value). Valid only with a registry source for this same app:
    # the grant it records names this app and the row this update installs.
    consent = body.get("trustRepository")
    if consent is not None and (
        not isinstance(consent, str)
        or not consent
        or not isinstance(source, str)
        or not is_registry_source(source)
        or registry_name_from_source(source) != name
    ):
        return web.json_response(
            {
                "error": (
                    "trustRepository must be a repository string and is accepted only "
                    "with source registry:<this app>"
                ),
                "code": "invalid_repository_consent",
            },
            status=400,
        )

    # Registry-installed apps: re-clone from registry.
    if is_registry_source(source):
        registry_name = registry_name_from_source(source)
        async with app_lifecycle_lock(name):
            # Preflight so a retryable refusal is the local branch's 409, leaving app
            # state untouched (app-kit-platform: refusal "without mutating app state").
            # ``install_from_registry`` re-checks at its own replacement boundary.
            startup_refusal = await _refuse_while_startup_hook_runs(name, action="update")
            if startup_refusal is not None:
                return startup_refusal
            # A host without git keeps the app running and reads the missing tool.
            git_refusal = git_unavailable_refusal(name)
            if git_refusal is not None:
                return web.json_response(
                    {
                        "ok": False,
                        "name": name,
                        "code": git_refusal["code"],
                        "error": git_refusal["error"],
                    },
                    status=400,
                )

            # The local-to-repository trust switch is checked and written BEFORE
            # the install, so a refused consent leaves the app untouched, and the
            # grant is in place before ``install_from_registry`` fetches or runs
            # any repository code.
            switched_to: local_trust_switch.SwitchedGrant | None = None
            if consent is not None:
                switched = await local_trust_switch.switch_cancel_safe(
                    name, consent, caller=request.get("user", "dashboard")
                )
                if isinstance(switched, local_trust_switch.SwitchRefusal):
                    return web.json_response(
                        {"ok": False, "name": name, "error": switched.error, "code": switched.code},
                        status=switched.status,
                    )
                switched_to = switched
            revert_task: asyncio.Future[None] | None = None

            async def _revert_switch() -> None:
                # One revert per request, shielded so a cancellation cannot stop it
                # halfway. A later call awaits the same revert, so the lifecycle
                # lock is not released while it is still writing.
                nonlocal revert_task
                if switched_to is None:
                    return
                if revert_task is None:
                    revert_task = asyncio.ensure_future(
                        local_trust_switch.revert_switch(
                            name, switched_to, caller=request.get("user", "dashboard")
                        )
                    )
                await asyncio.shield(revert_task)

            failed_before_take_down = False

            async def _after_failure(taken_down: bool) -> None:
                nonlocal failed_before_take_down
                failed_before_take_down = not taken_down
                await _revert_switch()

            # The whole install is one rollback scope: a failed result, a step that
            # raises, or a cancelled request must not leave the repository grant
            # over the local code that is still there. A failed install reverts
            # through ``after_failure`` BEFORE the restore starts the old local code
            # again, so it starts under the grant that covered it.
            try:
                reg_install = await _reinstall_from_registry(
                    name, registry_name, after_failure=_after_failure
                )
            except BaseException:
                await _revert_switch()
                raise
            if not reg_install.get("ok"):
                await _revert_switch()
                if (
                    switched_to is not None
                    and failed_before_take_down
                    and await _app_may_run_after_install(name)
                ):
                    # The old app kept running through the clone and build under the
                    # repository grant, which does not cover its local code, so the
                    # health watch can have stopped it. Start it again under the
                    # restored grant. This does nothing while it still runs.
                    restart_refused = await _start_backend_after_install(name)
                    if restart_refused and not reg_install.get("backend_refused"):
                        reg_install["backend_refused"] = restart_refused
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
            registered_mcp_servers: list[str] = []
            if not still_enabled:
                # The take-down kept the agent specs for the re-registration; an app
                # that may not run gets none (as on the registry install routes).
                await _suspend_app_for_session_approval_reconsent(name)
            else:
                reg_result = await _register_app_off_loop(name)
                registered_mcp_servers = reg_result.mcp_servers
                # Same promotion as the local-source branch: the re-install
                # rewrote app-crons.json, and only this call carries a changed
                # cron field into the RUNNING scheduler before the next start.
                await _resync_app_crons_after_update(request, name)
                backend = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), start_app_backend, name
                )
                reg_install["registration"] = reg_result.to_dict()
                backend_refused = await _backend_platform_refusal(name, backend)
                if backend_refused:
                    reg_install["backend_refused"] = backend_refused
                reg_install.update(await _rerun_on_enable_after_update(request, name))
        # The take-down removed an enabled app's MCP servers from the agent spec and
        # the registration wrote the new version's: end as an MCP page write does, so
        # a chat already open sees the change (app-kit-platform §1).
        held = info.get("enabled") and (info.get("manifest") or {}).get("mcpServers")
        if registered_mcp_servers or held:
            from kiro_crew.dashboard.handlers.template_server_request import _reach_sessions

            try:
                await _reach_sessions(request)
            except Exception:  # noqa: BLE001 - the update has committed
                logger.warning("Open chats did not get %s's MCP change", name, exc_info=True)
        sel().log_api_access(
            caller="dashboard", operation="app_update", outcome="completed", resources=name
        )
        _announce_app_ui_changed(request, name)
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
        # The agent configs stay, as on the registry branch: registration rewrites each
        # in place, so one it cannot rewrite keeps the user's edits (app-kit-platform §3).
        await _deregister_app_off_loop(name, keep_agents=True)

        # Off-loop: blocking filesystem copy (see handle_install_app).
        # expected_name makes update_app itself reject a source whose
        # manifest names a different app than the one this lock guards.
        up_result = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), lambda: update_app(source, expected_name=name)
        )
        if not up_result.ok:
            restore_refused = await _restore_app_after_failed_update(name)
            sel().log_api_access(
                caller="dashboard",
                operation="app_update",
                outcome="failed",
                resources=name,
                error=up_result.error,
            )
            failed_body = up_result.to_dict()
            if restore_refused:
                failed_body["backend_refused"] = restore_refused
            return web.json_response(failed_body, status=400)

        # Re-register with the new manifest only if the app is STILL enabled.
        # ``update_app`` drops ``enabled`` when the new version adds
        # ``permissions.sessionApproval``, so the pre-update ``info`` snapshot
        # would start a backend the user has not re-consented to.
        up_reg = None
        backend_refused = None
        on_enable: dict[str, Any] = {}
        still_enabled = await _app_may_run_after_install(name)
        if not still_enabled:
            # An app that may not run keeps none of the agent configs kept above.
            await _deregister_app_off_loop(name)
        else:
            await _prune_dropped_resources(name)
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
            backend = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), start_app_backend, name
            )
            backend_refused = await _backend_platform_refusal(name, backend)
            on_enable = await _rerun_on_enable_after_update(request, name)

    # As on the registry branch: the old MCP servers are out of the agent spec and the
    # new ones in, so end as an MCP page write does (app-kit-platform §1).
    held = info.get("enabled") and (info.get("manifest") or {}).get("mcpServers")
    if (up_reg and up_reg.mcp_servers) or held:
        from kiro_crew.dashboard.handlers.template_server_request import _reach_sessions

        try:
            await _reach_sessions(request)
        except Exception:  # noqa: BLE001 - the update has committed
            logger.warning("Open chats did not get %s's MCP change", name, exc_info=True)
    sel().log_api_access(
        caller="dashboard", operation="app_update", outcome="completed", resources=name
    )
    resp: dict[str, Any] = up_result.to_dict()
    if up_reg:
        resp["registration"] = up_reg.to_dict()
    if backend_refused:
        resp["backend_refused"] = backend_refused
    resp.update(on_enable)
    _announce_app_ui_changed(request, name)
    return web.json_response(resp)


async def _resync_app_crons_after_update(request: web.Request, name: str) -> None:
    """Register the updated app's crons with the running scheduler, if there is one.

    Every path that replaces an installed, still-enabled app's files calls it
    after re-registering: both branches of :func:`handle_update_app`, and a
    registry install or install-stream over an app already installed
    (``routes_registry``). A fresh install does not: it leaves the app disabled,
    and enabling it registers the crons. Split out so the no-service case (a
    test app with no ``state.crons``, a gateway booting without a scheduler) and
    the audit shape stay readable at each caller. Mirrors
    ``hooks_integration.on_app_enable``: the same bridge call, the same
    ``app_crons_register`` SEL rows.
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
    from kiro_crew.dashboard.handlers._shared import (
        body_not_object_error,
        require_owner_dashboard_request,
        string_field_error,
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
    if (wrong_type := string_field_error("name", name)) is not None:
        return wrong_type
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
        string_field_error,
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
    if (wrong_type := string_field_error("source", source)) is not None:
        return wrong_type
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
            manifest_data = loads_user_json(
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
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
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

        # Same gate as the registry paths: the live record decides. ``install_app``
        # writes it disabled, so the app's resources are not registered and its
        # backend does not start until the user enables it (from the disclosure
        # surface when the manifest declares ``permissions.sessionApproval``).
        if await _app_may_run_after_install(result.name):
            # Auto-register resources
            reg = await _register_app_off_loop(result.name)
            # Spawn the backend now so the app is reachable without a gateway
            # reboot (see _start_backend_after_install). No-op for backend-less apps.
            backend_refused = await _start_backend_after_install(result.name)
        else:
            reg = await _suspend_app_for_session_approval_reconsent(result.name)
            backend_refused = None
    sel().log_api_access(
        caller="dashboard", operation="app_install", outcome="completed", resources=result.name
    )
    install_body: dict[str, Any] = {**result.to_dict(), "registration": reg.to_dict()}
    if backend_refused:
        install_body["backend_refused"] = backend_refused
    _announce_app_ui_changed(request, result.name)
    return web.json_response(install_body, status=201)

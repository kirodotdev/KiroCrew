"""App management REST API endpoints for the KiroCrew dashboard.

All endpoints are registered under ``/api/apps`` by the dashboard handler
setup. These are aiohttp-compatible handler functions.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac as _hmac
import importlib
import importlib as _importlib
import json
import logging
import mimetypes
import os
import re
import stat
import sys
import sys as _sys
import time
from email.utils import formatdate
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import yarl
from aiohttp import web

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import platform_compat
from kiro_crew.apps import official_catalog
from kiro_crew.apps.backend import (
    get_app_backend_port,
    list_app_processes,
    recorded_backend_port,
    start_app_backend,
    stop_app_backend,
    unstopped_backend_port,
)
from kiro_crew.apps.bridges import (
    RegistrationResult,
    app_conversation_keys,
    deregister_app,
    deregister_app_crons_reporting_failures,
    register_app,
    register_app_crons_with_service,
)
from kiro_crew.apps.builtins import BUILTIN_NAMES
from kiro_crew.apps.dependencies import clean_dependencies
from kiro_crew.apps.dependencies import resolve_dependencies as _resolve_deps
from kiro_crew.apps.dependency_ledger import (
    canonical_dep_key,
    classify_and_clean_for_uninstall,
    classify_for_uninstall,
    declared_capability_keys,
)
from kiro_crew.apps.dev_mode import dev_mode_granted_root, is_dev_mode_cached
from kiro_crew.apps.event_bus import build_broadcast_fn
from kiro_crew.apps.execution import app_execution_denied
from kiro_crew.apps.hooks_integration import (
    get_all_hook_health,
    on_app_enable,
    stop_retained_startup_hooks,
)

# Aliased to keep `routes._run_lifecycle_script` patchable, which several tests rely on.
from kiro_crew.apps.lifecycle_scripts import run_lifecycle_script as _run_lifecycle_script
from kiro_crew.apps.manager import (
    _credential_free_source_metadata,
    app_enabled_state,
    app_lifecycle_lock,
    apps_dir,
    disable_app,
    enable_app,
    get_app,
    get_app_manifest,
    install_app,
    is_app_enabled,
    list_apps,
    register_external_app,
    resolve_mcp_backend_url,
    trust_grant_removal_blocked,
    uninstall_app,
    update_app,
)
from kiro_crew.apps.manifest import Dependencies, PlatformConfig, app_endpoint_allowed
from kiro_crew.apps.official_category_order import forget_cache as forget_category_order_cache
from kiro_crew.apps.official_category_order import load_category_order
from kiro_crew.apps.official_editorial import forget_cache as forget_editorial_cache
from kiro_crew.apps.official_editorial import load_sections
from kiro_crew.apps.proxy_auth import (
    PRINCIPAL_AGENT_TOOL,
    PRINCIPAL_APP_TOKEN,
    PRINCIPAL_NONE,
    PRINCIPAL_OWNER_SESSION,
    PROXY_PRINCIPAL_HEADER,
    sign_proxy_principal_claim,
)
from kiro_crew.apps.registry import (
    _ART_IMAGE_EXTENSIONS,
    _ART_MANIFEST_FIELDS,
    _ART_MANIFEST_LIST_FIELDS,
    _ART_MAX_BYTES,
    _REGISTRY_TRUST_TIERS,
    _SAFE_PATH_RE,
    _TRUST_INDEX,
    _TRUST_OWNER,
    _blob_cache_dir,
    _blob_cache_key,
    _context_clone_sandbox_mode,
    _entry_git_url,
    _git_fetch_branch,
    _git_target_is_unsupported,
    _git_url_host,
    _loggable_git_transport_output,
    _owner_designated_repo_target,
    _pinned_registries,
    _registry_identity_key,
    _registry_trust_tier_of,
    _repo_key_owner_count,
    _same_git_target,
    _sel_credential_grant,
    _strip_git_target_userinfo,
    anonymous_git_env,
    get_registry_app_by_repo,
    get_server_platform,
    install_from_registry,
    is_registry_source,
    known_registry_repos,
    list_catalog_apps,
    list_registry,
    minimal_env,
    registry_name_from_source,
    resolve_installed_trust_repository,
)
from kiro_crew.apps.registry_pipeline.checkout import _HOOKS_NEUTRALIZER_ARGV
from kiro_crew.apps.routes_art import (
    _BLOB_FETCH_SEMAPHORE,
    _BLOB_FETCH_TIMEOUT,
    _SAFE_REF_RE,
    _is_safe_repo_identifier,
    _read_declared_art,
    _touch_served_blob,
)
from kiro_crew.apps.routes_enable import handle_disable_app, handle_enable_app, handle_open_app
from kiro_crew.apps.routes_install import handle_register_external, handle_update_app
from kiro_crew.apps.routes_lifecycle import (
    _app_may_run_after_install,
    _check_min_version,
    _deregister_app_off_loop,
    _refuse_while_startup_hook_runs,
    _register_app_off_loop,
    _start_backend_after_install,
    _stop_backend_and_observe,
    _suspend_app_for_session_approval_reconsent,
    _unregister_notification_channels,
)
from kiro_crew.apps.routes_listing import (
    handle_get_app,
    handle_get_manifest,
    handle_list_apps,
    handle_publish_providers,
)
from kiro_crew.apps.routes_proxy import handle_app_api_proxy, invalidate_app_secret_cache
from kiro_crew.apps.routes_registry import (
    handle_registries,
    handle_registries_refresh,
    handle_registry,
    handle_registry_install,
    handle_registry_install_stream,
    handle_registry_refresh,
)
from kiro_crew.apps.routes_static import _CONTENT_TYPES, handle_app_dev_mode, handle_app_ui_file
from kiro_crew.apps.routes_uninstall import _deregister_crons_with_retry, handle_uninstall_preview
from kiro_crew.apps.spawn_sdk import build_spawn_impl
from kiro_crew.apps.teardown import forget_app_hooks, teardown_app_runtime
from kiro_crew.apps.update_notify import (
    deliver_app_update_notes,
    prepare_app_update_notes,
)
from kiro_crew.apps.update_status import attach_update, attach_updates
from kiro_crew.apps.version import check_min_version as _check_min_version_str
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    update_config_locked,
)
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable
from kiro_crew.dashboard.conditional_get import conditional_response, is_not_modified
from kiro_crew.executors import subprocess_executor
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    fd_real_path,
    is_reparse_point,
    open_in_pinned_parent,
    supports_pinned_walk,
)
from kiro_crew.publish_governance import DEPLOY_WEB_PROVIDER_ID, publish_denied_reason
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    scrub_env,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


async def handle_install_app(request: web.Request) -> web.Response:
    """POST /api/apps/install — install an app from a local path."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "app_install")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    source = body.get("source", "")
    if not source:
        return web.json_response({"error": "source path required"}, status=400)

    # Check minKiroCrewVersion before installing
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_dir():
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
    if manifest_path.is_file():
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
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


async def handle_uninstall_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/uninstall — uninstall an app.

    1. Check lifecycle field (locked → 400)
    2. Cron cleanup precondition (every app; abort with retryable 409 if
       the cron store stays busy — runs FIRST, before anything destructive)
    3. Run onUninstall script (if declared)
    4. Stop the backend (every app) + deregister resources (gateway-managed only)
    5. Clean removable dependencies (unless keep_dependencies=true)
    6. Remove app files (preserve data/ unless purge_data=true)

    Steps 2–6 run inside the per-app lifecycle lock so the whole teardown is
    atomic and the cron precondition can abort before any irreversible action.
    """
    # local import: avoids a circular import with dashboard.handlers
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "app_uninstall")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    lifecycle = info.get("lifecycle", "gateway")
    if lifecycle == "locked":
        return web.json_response(
            {"error": f"app {name!r} cannot be uninstalled (lifecycle=locked)"},
            status=400,
        )

    # Parse body
    # Preserve app data unless the caller supplies the dedicated destructive
    # action. Legacy ``keep_data: false`` payloads are intentionally ignored:
    # absence or malformed values must never become an implicit purge.
    keep_data = True
    keep_dependencies = False
    keep_specific: list[str] = []
    try:
        body = await request.json()
        keep_data = body.get("purge_data") is not True
        keep_dependencies = body.get("keep_dependencies", False)
        # Sanitize here, at the parse boundary: this is unvalidated client JSON,
        # and the dependency step that consumes it runs AFTER the onUninstall
        # script and deregistration — so a `{"keep_specific": [null]}` body that
        # raised downstream would leave the app half-removed.
        raw_keep = body.get("keep_specific", [])
        if isinstance(raw_keep, list):
            keep_specific = [k for k in raw_keep if isinstance(k, str) and k]
    except Exception:
        pass

    return await _run_uninstall(
        request,
        name,
        info,
        keep_data=keep_data,
        keep_dependencies=keep_dependencies,
        keep_specific=keep_specific,
    )


async def _run_uninstall(
    request: web.Request,
    name: str,
    info: dict[str, Any],
    *,
    keep_data: bool,
    keep_dependencies: bool,
    keep_specific: list[str],
    retired_builtin: bool = False,
) -> web.Response:
    """Run the uninstall preconditions and teardown for an owner-authorized request."""
    operation = "app_migrate_cleanup" if retired_builtin else "app_uninstall"
    resources = info.get("resources", "gateway")
    manifest = info.get("manifest", {})
    uninstall_log: list[str] = []
    # Serialized as ``warnings``, which ``print_result`` renders per item, so a
    # delegated uninstall surfaces it too. A still-listening port is also
    # appended to ``uninstall_log`` above, because that is the field the
    # dashboard's uninstall reads.
    backend_warnings: list[str] = []

    # Per-app lifecycle lock, wrapping the ENTIRE uninstall sequence:
    # cron-cleanup precondition → onUninstall script → backend stop →
    # deregistration → dependency cleanup → file removal. The lock is taken
    # FIRST, deliberately, because:
    #   (a) the cron-cleanup precondition below must be able to abort BEFORE
    #       any destructive action (see its comment), which requires it — and
    #       therefore the lock — to precede the onUninstall script; and
    #   (b) the onUninstall script may itself be destructive (it can wipe app
    #       data), so holding the lock across it stops a racing enable/update
    #       of the same app from starting a backend mid-teardown.
    # Cost: a concurrent same-app lifecycle op waits up to the onUninstall
    # timeout — acceptable, since those ops genuinely conflict and the lock is
    # per-app (other apps are unaffected).
    #
    # Counted inside the lock (Step 6) but reported after it, so it is bound
    # before the block that fills it. The flag rides along because the count alone
    # cannot be reported: a drop that did not reach disk is a pointer a reinstall
    # will still resume, and `dropped` on its own reads as a clean sweep.
    dropped = 0
    pointer_flush_failed = False
    async with app_lifecycle_lock(name):
        if retired_builtin:
            from kiro_crew.apps.manager import migrated_builtin_cleanup_applies

            if not await asyncio.to_thread(migrated_builtin_cleanup_applies, name):
                sel().log_api_access(
                    caller="dashboard",
                    operation=operation,
                    outcome="denied",
                    resources=name,
                    error="not a migrated builtin",
                )
                return web.json_response(
                    {"ok": False, "error": "not a migrated builtin", "code": "not_orphaned"},
                    status=400,
                )
        # A retained startup hook still owns the old app's AppContext. Bound the
        # wait and refuse the uninstall if it remains live; deleting files or
        # withdrawing trust first would falsely report that old code is gone.
        startup_refusal = await _refuse_while_startup_hook_runs(
            name, action="migrate_cleanup" if retired_builtin else "uninstall"
        )
        if startup_refusal is not None:
            return startup_refusal

        # Step 0: the execution grant must be removable before anything is
        # destroyed. A grant is keyed on the app NAME alone, so one left behind
        # admits a DIFFERENT app later installed under this name — code execution
        # with no consent prompt. Checking it inside uninstall_app (Step 5)
        # instead would make it unreachable as an abort: by then the cron
        # manifest, the onUninstall script, the backend and the dependencies have
        # all already been torn down, so the refusal strands a half-removed app
        # and every retry re-runs the non-idempotent script. Asking here keeps the
        # refusal free and the retry safe, exactly like the cron precondition.
        # Offloaded: the precondition reads config.json and config.local.json from
        # disk, and this is an async handler — the same reason `uninstall_app` below
        # goes through the executor rather than being called inline.
        grant_blocked = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), trust_grant_removal_blocked, name
        )
        if grant_blocked:
            logger.warning(
                "Uninstall of %s ABORTED: trust grant not removable (%s)",
                name,
                grant_blocked,
            )
            sel().log_api_access(
                caller="dashboard",
                operation=operation,
                outcome="denied",
                resources=f"app={name}",
                error=f"trust grant not removable, uninstall aborted: {grant_blocked}",
            )
            return web.json_response(
                {
                    "error": (
                        f"not uninstalling {name!r}: its third-party execution "
                        f"grant could not be removed ({grant_blocked}). The grant "
                        f"is keyed on the name, so removing the app while it "
                        f"stands would let any future app installed under this "
                        f"name run code without asking. Nothing has been changed "
                        f"— clear the cause and retry."
                    ),
                    "code": "trust_grant_not_removed",
                    "retryable": True,
                    "app": name,
                },
                status=409,
            )

        # Step 1: Cron cleanup is the FIRST uninstall precondition, run BEFORE
        # the (possibly destructive, non-idempotent) onUninstall script and
        # BEFORE the backend is stopped. Uninstall is irreversible: below this
        # point deregister_app() drops the per-app cron manifest and Step 5
        # deletes the app directory. If owned jobs are still persisted and
        # ENABLED at that moment they become permanent orphans — nothing
        # remains that knows they belong to a removed app, and the scheduler
        # keeps firing their command / script / agent payload indefinitely.
        # So a contended store ABORTS the uninstall with a retryable 409 having
        # changed NOTHING: no script run, no backend stopped, no manifest
        # touched. Only then is the "app is still installed; retry" message
        # literally true AND the retry safe — the non-idempotent onUninstall
        # has not executed, so re-running the uninstall cannot double-apply a
        # destructive teardown. "Durably disable the jobs instead" is not a
        # fallback: disabling is itself a store mutation needing the very lock
        # that is contended.
        # Clean up app-declared cron jobs from the scheduler before the
        # per-app cron manifest is removed by deregister_app(). Mirrors the
        # cleanup that on_app_disable performs on the disable path, which keys
        # on the app's cron PERMISSION and not on `resources`.
        #
        # `resources` does not gate this, for the same reason it does not gate
        # the backend stop in Step 3: the field is app-written metadata, so
        # gating teardown on it hands a trusted app a switch for its own
        # cleanup. It also would not describe who owns these jobs even if it
        # were trustworthy — an `app:<name>` job is persisted in the GATEWAY's
        # cron store and fired by the gateway's own CronService, which applies
        # no app-admission check at fire time. So a job left behind here runs
        # its command / script / agent payload against a deleted app directory
        # until the next gateway boot reconciles the store, and uninstall would
        # otherwise clean up less than the strictly less destructive disable.
        state = request.app.get("state")
        cron_service = getattr(state, "crons", None) if state else None
        if cron_service is not None:
            try:
                # deregister_app_crons_reporting_failures is async: it awaits the
                # CronSDK mutation API (per-job store-lock spin offloaded to
                # a worker thread), so the loop is never parked and timer
                # arming is owned by CronService (no caller-side drain).
                # It removes all owned jobs in ONE atomic transaction, so on any
                # failure nothing was removed — the aborts below leave no
                # partially-cleaned state.
                removed = await _deregister_crons_with_retry(name, cron_service)
                sel().log_api_access(
                    caller="dashboard",
                    operation="app_crons_deregister",
                    outcome="completed",
                    resources=f"app={name} removed={removed}",
                )
            except CronStoreBusy as exc:
                logger.warning(
                    "Uninstall of %s ABORTED: cron cleanup could not "
                    "complete (store busy) and continuing would orphan "
                    "still-enabled app jobs: %s",
                    name,
                    exc,
                )
                sel().log_api_access(
                    caller="dashboard",
                    operation=operation,
                    outcome="denied",
                    resources=f"app={name}",
                    error=f"cron cleanup failed, uninstall aborted: {exc}",
                )
                return web.json_response(
                    {
                        "error": (
                            f"cron cleanup for {name!r} could not complete "
                            "(cron store busy) — uninstall aborted so the "
                            "app's scheduled jobs are not orphaned. The app is "
                            "still installed; retry the uninstall."
                        ),
                        "retryable": True,
                        "app": name,
                        "log": uninstall_log,
                    },
                    status=409,
                )
            except CronStoreUnreadable as exc:
                # Same abort as CronStoreBusy above, for the same reason: the
                # owned-job set came back empty because the store could not be
                # READ, not because the app owns nothing, so continuing would
                # delete the app and leave its still-ENABLED jobs to resume.
                # Reported NON-retryable, matching the contract in
                # dashboard/handlers/cron_shared.py: an unreadable file does not heal
                # on its own, so a client that retries on busy must not retry
                # here. The exception already names the one action that fixes
                # it, so its message is surfaced verbatim.
                logger.warning(
                    "Uninstall of %s ABORTED: the cron store could not be read, "
                    "so cleanup could not prove the app owns no enabled jobs: %s",
                    name,
                    exc,
                )
                sel().log_api_access(
                    caller="dashboard",
                    operation=operation,
                    outcome="denied",
                    resources=f"app={name}",
                    error=f"cron store unreadable, uninstall aborted: {exc}",
                )
                return web.json_response(
                    {
                        "error": str(exc),
                        "code": "cron_store_unreadable",
                        "retryable": False,
                        "app": name,
                        "log": uninstall_log,
                    },
                    status=409,
                )
            except Exception as exc:
                # Same abort as the two named store failures above, for the same
                # reason. A removal that raised anything else did not persist, and
                # the count cannot reveal that: the bridge's other entry point
                # answers 0 for both "owned nothing" and "the write failed", and
                # re-counting the rows cannot settle it either, because a failing
                # save leaves them filtered out of the in-memory job list until a
                # reload. So the exception is the only evidence there is, and
                # continuing past it would run the non-idempotent onUninstall and
                # delete the app while its still-ENABLED rows keep firing from
                # disk until the next gateway boot reconciles them.
                #
                # Retryable, unlike the unreadable store: a write that failed on a
                # full or briefly unavailable disk can succeed on a later attempt,
                # and nothing destructive has run yet, so the retry is safe.
                logger.warning(
                    "Uninstall of %s ABORTED: cron cleanup failed and continuing "
                    "would orphan still-enabled app jobs: %s",
                    name,
                    exc,
                )
                sel().log_api_access(
                    caller="dashboard",
                    operation=operation,
                    outcome="denied",
                    resources=f"app={name}",
                    error=f"cron cleanup failed, uninstall aborted: {exc}",
                )
                return web.json_response(
                    {
                        "error": (
                            f"cron cleanup for {name!r} could not complete ({exc}) "
                            "— uninstall aborted so the app's scheduled jobs are "
                            "not orphaned. The app is still installed; retry the "
                            "uninstall."
                        ),
                        "code": "cron_cleanup_failed",
                        "retryable": True,
                        "app": name,
                        "log": uninstall_log,
                    },
                    status=409,
                )

        # Step 2: Run onUninstall script. Reached only once cron cleanup has
        # succeeded (or there were no crons / no cron service), so a
        # non-idempotent teardown never runs on an uninstall that will be
        # retried.
        on_uninstall = (manifest.get("setup") or {}).get("onUninstall", "")
        if on_uninstall and not retired_builtin:
            # The successor inherits data/, so retired code must not run a destructive hook.
            script_output = await _run_lifecycle_script(
                name,
                on_uninstall,
                timeout=120,
                extra_env={
                    "KEEP_DATA": "1" if keep_data else "0",
                    "PURGE_DATA": "0" if keep_data else "1",
                },
                action="on_uninstall",
            )
            if script_output.get("output"):
                from kiro_crew.security import redact

                cleaned = redact(script_output["output"])
                uninstall_log.append(cleaned)
            if script_output.get("failed"):
                uninstall_log.append("onUninstall script failed (exit code non-zero)")

        # Step 3: Stop the backend, then deregister gateway-managed resources.
        #
        # The stop runs for EVERY app; `resources` does not gate it. That field
        # comes from the app's own installed metadata, so gating the stop on it
        # hands a trusted app a switch for its own teardown: declare
        # `resources: "app"` and the uninstall deletes the files while the backend
        # keeps executing, holding its port, its app secret and its proxied
        # routes. Deregistration still honors the field — an app that owns its
        # agents, skills and crons must not have the gateway delete them — but the
        # PROCESS is not the app's to keep. Same split `teardown_app_runtime`
        # makes on disable and on trust withdrawal.
        #
        # A still-listening port is REPORTED, not made to abort. By here the
        # non-idempotent onUninstall script has already run, so refusing would
        # strand a half-removed app that no retry can finish cleanly, and an app
        # that cannot be uninstalled is a worse outcome than one whose port is
        # named as still in use. What must not happen is claiming a clean stop.
        live_port, had_port_evidence = await _stop_backend_and_observe(name)
        if live_port is not None:
            logger.warning(
                "backend for app %r is still listening on port %s after uninstall stop",
                name,
                live_port,
            )
            message = (
                f"backend still listening on port {live_port} after the stop — the "
                f"gateway stopped every process it was tracking, so this one is not "
                f"ours to stop and it is still running"
            )
            # Recorded in BOTH fields because their consumers are disjoint, and a
            # caller that sees neither is told a clean removal happened:
            # ``print_result`` renders ``warnings`` and never ``uninstall_log``,
            # while the dashboard's uninstall reads ``uninstall_log`` and never
            # ``warnings``. Neither consumer shows it twice.
            backend_warnings.append(message)
            uninstall_log.append(message)
        elif not had_port_evidence and bool((manifest.get("backend") or {}).get("entryPoint")):
            # The probe answered "nothing observed", which is not "stopped". With no
            # recorded port it had only the declared one to go on, and `onUninstall`
            # has already run app-controlled code inside the app directory that
            # declares it, so an untracked fixed-port backend could have relabelled
            # the port it is holding. Saying nothing here is the false clean removal
            # this step exists to prevent, and nothing later catches it: the files
            # are gone and the stop dropped the pidfile record the next start would
            # have reaped from.
            #
            # Gated on a backend being DECLARED, read from the installed record
            # captured before the hook ran, so an app cannot suppress this by
            # rewriting its manifest — and an app that never had a backend does not
            # collect a warning about one.
            message = (
                f"could not verify {name}'s backend stopped — the gateway held no "
                f"recorded port for it, so a listener it never tracked cannot be "
                f"ruled out. Check for a process still bound to the port this app "
                f"declared."
            )
            logger.warning("stop of app %r could not be verified: no recorded port", name)
            backend_warnings.append(message)
            uninstall_log.append(message)
        if resources == "gateway":
            deregistered = await _deregister_app_off_loop(name)
            if retired_builtin:
                backend_warnings.extend(deregistered.errors)

        # Step 4: Clean dependencies (atomic classify + ledger update)
        cleaned_deps: list[str] = []
        if not keep_dependencies:
            deps_data = manifest.get("dependencies", {})
            declared_deps = declared_capability_keys(deps_data)

            # Normalize client-supplied keep ids: a dashboard session whose
            # uninstall preview came from a pre-rename build echoes legacy keys,
            # and classification emits canonical ones — comparing the two raw
            # would drop the keep and delete a dep the user chose to keep.
            keep_canonical = [canonical_dep_key(k) for k in keep_specific]
            # Off-loop: the classification takes a blocking ledger lock and
            # rewrites the ledger, the same shape the preview offloads above.
            classification = await asyncio.to_thread(
                classify_and_clean_for_uninstall,
                name,
                declared_deps,
                keep_specific=keep_canonical,
            )
            removable = [
                d for d in classification.get("removable", []) if d.get("id") not in keep_canonical
            ]
            if removable:
                cleaned_deps = await clean_dependencies(name, removable)
                if cleaned_deps:
                    uninstall_log.append(f"Cleaned {len(cleaned_deps)} dependency(ies)")

        # Step 5: Remove files. Off-loop: rmtree of a large installed tree is
        # blocking filesystem I/O. (uninstall_app shares the
        # ``.{name}-data-tmp`` move-aside path with install/update — covered
        # by the lifecycle lock held above.)
        #
        # Held under the SHARED config lock because `uninstall_app` also runs
        # `_drop_trust_grant`, which is a read-modify-write of `config.json`.
        # `app_lifecycle_lock` is keyed on the APP name and so serializes nothing
        # against a concurrent settings/agent write, which takes this lock and
        # rewrites the same file: the two interleave into a lost update, either
        # dropping the user's settings or restoring the grant we just removed —
        # and a restored grant is a consent bypass for whatever is next installed
        # Deferred, not top-level, and NOT because of a circular import — I checked,
        # and hoisting it to module scope imports cleanly. The reason is layering:
        # `apps` sits below `dashboard`, so a module-scope import here would make the
        # app subsystem depend on a dashboard handler at LOAD time, in the one
        # direction the package tree is meant to forbid. Deferring keeps that
        # dependency at call time, where it is honest about being a shared-lock
        # lookup rather than a structural one. This also matches how every other
        # caller of this lock outside `dashboard/handlers` reaches it (see
        # `mcp.py`, `messaging.py`, `core.py`, `computer_use.py`,
        # `mcp_discover.py`) — the lock has no neutral home yet, and giving it one
        # is a ~15-file refactor that does not belong in this change.
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        async with _get_config_lock():
            result = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                lambda: (
                    uninstall_app(name, keep_data=keep_data, retired_builtin=True)
                    if retired_builtin
                    else uninstall_app(name, keep_data=keep_data)
                ),
            )

        # Step 6: drop the resume pointer of every conversation the app owned.
        #
        # INSIDE the lifecycle lock, and that is the point: this is a step of the
        # uninstall, not an epilogue to it. Outside, a concurrent reinstall could
        # take the lock the moment we release it and be serving the SAME slot key
        # again while our scan is still running — and the pointer we then clear is
        # the new installation's, not the dead one's. The lock is keyed on the app
        # name, so it serializes exactly the reinstall that would collide.
        #
        # On success only: a failed uninstall leaves nothing changed, so a
        # still-installed app keeps the pointers its slots are still entitled to
        # resume. Not in `deregister_app` (Step 3) — that also runs on disable, and
        # App Store Sync is a disable/enable pair.
        #
        # Enumerated AND cleared through the LIVE session map, never a throwaway
        # `SessionMap`: this gateway holds a long-lived map whose `_data` loaded at
        # startup and whose every write rewrites the whole file from that snapshot.
        # Both halves follow from that one fact.
        #
        # Writing detached would be undone by the next unrelated mutation —
        # restoring the very pointer just dropped — and would take whatever the live
        # map had not flushed with it (`SessionMap`'s rule 3).
        #
        # READING detached is the same fact from the other side: the file lags this
        # map by exactly what it has not flushed, so a detached enumeration can omit
        # a key whose pointer already exists, and the clear then leaves that pointer
        # for a reinstall to resume. `mapped_session_keys()` is the in-memory answer;
        # `session_keys()` adds a key whose allocation is in flight and has not
        # reached the map yet. Ownership still comes from the metadata line on disk,
        # which is what survives a closed tab.
        #
        # `discard_conversation` tears the live session down, so a still-open tab of
        # the uninstalled app cannot re-record a sid from the session it was holding.
        #
        # ONE pass, and the window it leaves is NAMED rather than narrowed. An
        # allocation already reserved is inside `session_keys()`, so it is enumerated
        # here. One that reserves after this pass is not, and no number of passes
        # reaches it: closing that window means holding admission against this app's
        # keys for the duration of the uninstall, and the only admission gate on the
        # manager sets `_closing` PROCESS-WIDE — it would refuse turns for every app
        # and every conversation while one app uninstalls, which is the larger harm.
        # A per-key admission gate is a change to the allocation boundary, owned by
        # whoever owns that boundary, not by this cleanup step. So the residual is
        # stated here and in the description rather than half-closed by a retry loop
        # that reads as though it were closed.
        #
        # The residual costs one stale pointer on one key of an app the user has
        # already removed, and the next cold start under that key self-corrects as
        # soon as the suppression flag is consumed.
        if result.ok:
            sessions = getattr(request.app.get("state"), "sessions", None)
            if sessions is not None:
                candidates = sessions.mapped_session_keys() | sessions.session_keys()
                # Ownership reads each candidate's metadata line off disk; off the
                # loop so a large history does not park the gateway.
                owned = await asyncio.get_running_loop().run_in_executor(
                    None,
                    functools.partial(app_conversation_keys, name, mapped_keys=candidates),
                )
                for key in owned:
                    try:
                        # `replay=False`, not the default: dropping the sid stops the
                        # NATIVE resume only. The transcript stays on disk by design,
                        # so a cold start under this key would still have
                        # `build_session_replay` inject the removed app's history into
                        # the next installation's first turn — the same bug through a
                        # second channel. The default `replay=True` actively DISCARDS
                        # any standing suppression, so leaving it would be worse than
                        # silent.
                        try:
                            await sessions.discard_conversation(key, replay=False)
                        finally:
                            # The sid is already gone by the time anything in there can
                            # raise: `discard_conversation` clears it and sets the
                            # in-memory flag inside its registry lock, and only THEN
                            # awaits `provider.shutdown()`, which its own `finally`
                            # deliberately lets propagate. Skipping this on that path
                            # leaves the suppression memory-only, so a restart before
                            # the reinstall replays the removed app's transcript into
                            # the new installation's first turn — the bug this step
                            # exists to prevent, reached through the failure path.
                            #
                            # Persistently at all, because the flag `replay=False` sets
                            # lives in this process's memory: a gateway restart between
                            # the uninstall and the reinstall would lose it.
                            sessions.suppress_replay_persistently(key)
                        dropped += 1
                    except Exception:  # noqa: BLE001 — bookkeeping must not fail an uninstall
                        logger.warning(
                            "could not drop the resume pointer for %r", key, exc_info=True
                        )
                if owned:
                    # `owned`, not `dropped`: a key whose teardown raised still had its
                    # suppression flag written by the `finally` above, and that write is
                    # only worth anything once it reaches disk.
                    #
                    # Durable BEFORE the uninstall reports success, which is the same
                    # invariant the CLI path states as `flush()` before releasing the
                    # lock. `clear_sid` on the loop only SCHEDULES a debounced flush,
                    # so without this the handler answers 200 while the dropped
                    # pointer is still only in memory — and a restart inside that
                    # window brings the stale sid back with the app already gone.
                    #
                    # Wrapped for the same reason the per-key body above is: by the
                    # time this runs `uninstall_app` has already removed the app's
                    # files, so the uninstall is past being retried as a whole. An
                    # ENOSPC or a permission error here would raise straight out of
                    # the handler and skip `invalidate_app_secret_cache`,
                    # `_unregister_notification_channels` and `forget_app_hooks` --
                    # and a surviving slot-close hook makes the removed app's
                    # leftover tabs UNDISMISSABLE, which costs the user more than
                    # the pointer this write failed to persist. The CLI sibling
                    # states the same rule as `SessionPointerCleanup(failed=True)`.
                    try:
                        await sessions.aflush()
                    except Exception:  # noqa: BLE001 -- bookkeeping must not fail an uninstall
                        pointer_flush_failed = True
                        logger.warning(
                            "could not persist %r's dropped resume pointer(s)",
                            name,
                            exc_info=True,
                        )

            # Step 7: remove the clone. Off-loop like Step 5 (a git tree), and INSIDE
            # the lock held since Step 2: a second acquisition queues behind a parked
            # install and would delete the tree that install just re-cloned. PR body.
            if is_registry_source(info.get("source", "")):
                app_reg_name = registry_name_from_source(info.get("source", ""))
                if app_reg_name:
                    from kiro_crew.apps.registry import app_source_dir

                    try:
                        ws_dir = app_source_dir(app_reg_name)
                    except ValueError as exc:  # the name is read from app-writable installed.json
                        logger.warning("Uninstall of %s skipped the clone removal: %s", name, exc)
                    else:
                        if ws_dir.is_dir():
                            await asyncio.to_thread(platform_compat.rmtree_force, ws_dir)
                            uninstall_log.append(f"Removed workspace for {app_reg_name}")
    if not result.ok:
        sel().log_api_access(
            caller="dashboard",
            operation=operation,
            outcome="failed",
            resources=name,
            error=result.error,
        )
        return web.json_response(result.to_dict(), status=400)
    invalidate_app_secret_cache(name)
    _unregister_notification_channels(request, name)
    # Same reason as the line above, for the hook registries: uninstall is the
    # terminal path, so an entry left behind is a closure over a store this
    # handler is about to delete. A surviving slot-close hook makes the app's
    # leftover tabs UNDISMISSABLE -- `notify_slot_closed` returns False when the
    # hook raises and `api_chat_slot_delete` refuses the close on that.
    forget_app_hooks(name)

    if dropped:
        if pointer_flush_failed:
            uninstall_log.append(
                f"Dropped {dropped} conversation pointer(s) in memory, but the write "
                "did not persist -- a reinstall may still resume one"
            )
        else:
            uninstall_log.append(f"Dropped {dropped} conversation pointer(s)")

    sel().log_api_access(
        caller="dashboard", operation=operation, outcome="completed", resources=name
    )
    resp = result.to_dict()
    if retired_builtin:
        from kiro_crew.security import redact

        for index, warning in enumerate(backend_warnings):
            backend_warnings[index] = redact(warning)
        notices = list(backend_warnings)
        if pointer_flush_failed:
            notices.append(
                "Conversation pointers were dropped in memory, but the write "
                "did not persist -- a reinstall may still resume one"
            )
        if notices:
            resp["notice"] = redact("\n".join(notices))
    if uninstall_log:
        resp["uninstall_log"] = "\n".join(uninstall_log)
    if backend_warnings:
        resp["warnings"] = backend_warnings
    if cleaned_deps:
        resp["cleaned_dependencies"] = cleaned_deps
    return web.json_response(resp)


async def handle_app_art_file(request: web.Request) -> web.Response:
    """GET /apps/{name}/art/{path:.*} — an installed app's own store art.

    The bytes of an installed app's icon, hero and screenshots are already on
    local disk, inside the directory the install created. Reaching them through
    ``/api/apps/blob`` instead means a git clone gated by an SSRF allowlist,
    which is why a catalog-listed app's art could 403 on a cold load: the
    allowlist is warmed by a network fetch that a page can outrun. Reading the
    file the gateway itself wrote has no such ordering, needs no network,
    survives a CDN outage, and adds no SSRF surface — the request never names a
    host.

    Mirrors :func:`handle_app_ui_file`'s shape (that route already serves an
    app's UI-bundle assets, and ``AppDetailPage`` already resolves a page icon
    through it) with two deliberate narrowings: images only, and the path must
    be one the app's manifest declares.
    """
    name = request.match_info["name"]
    file_path = request.match_info.get("path", "")
    # Cheap rejections first, before any syscall: these cannot be reached by a
    # declared path anyway, so answering here keeps a hostile request off the
    # thread pool entirely.
    if not file_path or ".." in file_path or file_path.startswith("/"):
        return web.json_response({"error": "invalid path", "code": "art_path_invalid"}, status=400)
    ext = Path(file_path).suffix.lower()
    if ext not in _ART_IMAGE_EXTENSIONS:
        return web.json_response(
            {"error": f"file type {ext!r} not allowed", "code": "art_type_not_allowed"},
            status=403,
        )
    full_path = await asyncio.to_thread(_read_declared_art, name, file_path)
    if full_path is None:
        # One answer for "not declared", "outside the install dir", "not a plain
        # file", "over the size ceiling" and "missing", so a probe cannot use the
        # status to map which paths a manifest names. One `code` for the same
        # reason: a caller that could tell them apart from the code would have the
        # mapping the shared status withholds.
        return web.json_response({"error": "not found", "code": "art_not_found"}, status=404)
    data, validator = full_path
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
    # `no-cache`, not a long max-age: an app update rewrites these bytes in place
    # under the same URL, so the browser must revalidate. The validator comes from
    # the descriptor the bytes were read from, so an unchanged icon still costs one
    # 304 rather than a full body — which matters because the rail re-renders on
    # every dashboard load.
    #
    # The CSP is load-bearing, not decoration. `.svg` is in the allowlist because an
    # SVG in an `<img>` is script-inert, but a TOP-LEVEL NAVIGATION to this URL is a
    # different thing: the response becomes a DOCUMENT on the dashboard's own origin,
    # and the dashboard's base CSP is deliberately permissive there
    # (`script-src 'self' 'unsafe-inline'`, so widget and MCP-app iframes can run
    # inline script), so it would NOT stop a scripted SVG an app declared as its art.
    # Same-origin script then reaches the authenticated dashboard API with the
    # viewer's session.
    #
    # `sandbox` with no tokens gives the document an opaque origin and no script at
    # all, and `default-src 'none'` stops it fetching anything; a response CSP does
    # not apply when the bytes are consumed as an `<img>` subresource, so the store's
    # own rendering is unaffected. `nosniff` matters because the Content-Type here is
    # derived from the EXTENSION, not the bytes: without it, art named `.png` whose
    # content is markup could still be sniffed into a document.
    #
    # Set on the response rather than in the middleware because the middleware uses
    # `setdefault` precisely so a handler can tighten its own answer.
    return conditional_response(
        request,
        data,
        content_type,
        etag=validator,
        cache_control="no-cache",
        extra_headers={"Content-Security-Policy": "default-src 'none'; sandbox"},
    )


async def handle_app_config(request: web.Request) -> web.Response:
    """GET/PUT /api/apps/{name}/config — read or write app config.json.

    Reads/writes ``~/.kiro/crew/apps/{name}/data/config.json``.
    GET returns the current config (empty ``{}`` if none exists).
    PUT replaces the config with the request body. A dashboard subject must be
    the owner to PUT; an app token reaches this handler only inside the scope
    ``token_auth`` already granted it (its own app, or a manifest grant).
    """
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        # Compat: migrated deploy-web — redirect to canonical deploy config endpoint.
        if name == "deploy-web":
            raise web.HTTPTemporaryRedirect(location="/api/deploy/config")
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    from kiro_crew.apps.manager import app_data_dir
    from kiro_crew.atomic_write import atomic_write

    data_dir = app_data_dir(name)
    config_path = data_dir / "config.json"  # noqa: F811 -- the header import stays for importers

    if request.method == "GET":
        if not config_path.is_file():
            # Missing config (e.g. data dir wiped by an app update) — seed an
            # empty config so the app gets a valid response instead of hanging
            # on a perpetual "loading" state. The app repopulates it on first use.
            try:
                await asyncio.to_thread(atomic_write, config_path, "{}\n")
            except OSError:
                pass
            return web.json_response({})
        try:
            text = await asyncio.to_thread(config_path.read_text, encoding="utf-8")
            return web.json_response(json.loads(text))
        except (json.JSONDecodeError, OSError) as exc:
            return web.json_response({"error": f"failed to read config: {exc}"}, status=500)

    # PUT — write config.
    # An app token was already scoped by token_auth's _enforce_app_scope (its own
    # app via _app_owns_path, a foreign app only with a manifest grant), so only a
    # dashboard subject (an empty or missing app claim) needs the owner check.
    if not request.get("app"):
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        denied = await require_owner_dashboard_request(request, "app_config_write")
        if denied is not None:
            return denied

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "config must be a JSON object"}, status=400)

    try:
        content = json.dumps(body, indent=2) + "\n"
        await asyncio.to_thread(atomic_write, config_path, content)
    except OSError as exc:
        return web.json_response({"error": f"failed to write config: {exc}"}, status=500)

    sel().log_api_access(
        caller="dashboard",
        operation="app_config_write",
        outcome="completed",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name})


async def _fetch_git_blob(
    repo: str,
    ref: str,
    file_path: str,
    cache_path: Path,
    *,
    git_url: str,
    owner_designated: bool = False,
    credential_target: str | None = None,
) -> bool:
    """Fetch a single file from a registry app's git repo via a shallow clone.

    Public git hosts (GitHub, etc.) disable the ``git-upload-archive`` service
    used by ``git archive --remote``, so we instead perform a shallow
    ``git clone --depth 1 --branch <ref>`` into a throwaway temp directory
    (mirroring how :mod:`kiro_crew.apps.registry` already clones), read the
    requested file out of the checkout, and write it to the blob cache.

    ``git_url`` is the credential-free clone identity, a REQUIRED parameter
    supplied by the caller. It is never resolved here from ``repo``: this
    function performs no registry lookup. For the exact owner-designated case,
    ``credential_target`` may carry the matching config-only HTTP transport URL.
    The match is rechecked here before any spawn, and the split fetch helper gives
    that raw value only to the network subprocess; argv, checkout and cache
    identity retain ``git_url``. This paired handoff closes the TOCTOU window a
    second registry-row resolution would open without retaining credentials in
    the row itself.

    ``owner_designated`` extends the same-repo credential carve-out to this third
    clone chokepoint.  It is ``True`` only when the caller has
    confirmed — via the merged :func:`_owner_designated_repo_target` predicate,
    evaluated against the SAME entry ``git_url`` was resolved from — that the
    entry's clone URL is byte-identical to the owner-typed
    ``ExternalRegistryConfig.repo``.  In that case the confused-deputy defense
    does not apply (the owner designated exactly this URL by configuring the
    registry), so the clone uses owner credentials: ``minimal_env`` + the
    context clone sandbox mode, and the ssh-agent socket (``wrap_argv`` re-judges
    that against the tier), exactly like the manifest/clone chokepoints.
    A sibling repo on the same host is a *different* URL, so it never matches
    and stays anonymous + strict — the carve-out is URL-exact, not host-granular.
    The public identity and optional transport target must sanitize to the same
    URL, so the granted credentials and the repository they reach cannot
    disagree. The grant is SEL-audited against the public ``git_url``.
    """
    # ``git_url`` is the caller's once-resolved public clone identity. The caller
    # already rejects an unresolvable URL (the
    # ``blob_no_git_url`` early-return), so ``git_url`` is non-empty here; keep a
    # defensive guard rather than assume it.
    if not git_url:
        logger.debug(
            "No git URL resolvable for registry repo %r — skipping blob fetch",
            _strip_git_target_userinfo(repo),
        )
        return False
    if _git_target_is_unsupported(git_url):
        logger.warning("blob clone refused an unsupported clone target")
        return False
    # ``git_url`` is the public repository identity used by argv and the blob
    # cache. The optional raw target is recovered server-side from current
    # configuration only for an exact owner-designated match; it is never read
    # from the retained registry row. Accept a raw ``git_url`` as a compatibility
    # fallback for direct internal callers, then immediately split it here.
    credential_target = credential_target or git_url
    git_url = _strip_git_target_userinfo(git_url)
    if _strip_git_target_userinfo(credential_target) != git_url:
        logger.warning("blob clone credential target did not match repository identity")
        return False
    credentialed_transport = credential_target != git_url

    # SSRF gate: a configured external registry's (untrusted) index can list an
    # app ``repo`` pointing at an internal address (e.g. ``https://127.0.0.1/x``)
    # or an attacker-controlled host — and it passes both ``known_registry_repos``
    # and ``_is_safe_repo_identifier``. Browsing the App Store fetches icons
    # through this path automatically, so honoring such a value would drive
    # ``git clone`` against the loopback/internal network (authenticated backend
    # SSRF). Constrain the clone to an explicitly-trusted host (public forge or a
    # host the owner configured as a registry); this is rebinding-proof because
    # it gates on the hostname, not its resolvable IP.
    from kiro_crew.apps.registry import is_clone_host_trusted

    if not await asyncio.to_thread(is_clone_host_trusted, git_url):
        logger.warning(
            "Blob clone refused for repo=%r url=%r: host not in trusted forge/registry set (SSRF gate)",
            _strip_git_target_userinfo(repo),
            _strip_git_target_userinfo(git_url),
        )
        return False

    import tempfile

    tmp_root: str | None = None
    try:
        tmp_root = await asyncio.to_thread(tempfile.mkdtemp, prefix="kirocrew-blob-")
        # Credential posture for the browse-time icon/blob clone.  By default
        # this is an index-originated automatic clone, so it forces the strict
        # sandbox (~/.ssh hidden) and a credential-free env: a trusted-host repo
        # injected by an untrusted registry index can't be cloned with the
        # gateway's ambient git/ssh identity (confused-deputy defense — see
        # anonymous_git_env).  The same-repo carve-out flips BOTH knobs together
        # when the caller confirmed the blob's URL is byte-identical to the
        # owner-configured registry repo: minimal_env + the context clone
        # sandbox mode.  The strict sandbox hiding ~/.ssh is the load-bearing
        # defense (not the env), which is why env and sandbox mode move as a
        # pair — exactly as the merged manifest/clone chokepoints do.
        if owner_designated:
            # ``_context_clone_sandbox_mode`` reaches the same config subsystem the
            # two sibling calls in this function already offload (the SSRF-gate
            # ``is_clone_host_trusted`` above and the caller's
            # ``_owner_designated_repo_target``): it flows
            # ``_configured_registry_hosts`` -> ``_effective_registries`` ->
            # ``KiroCrewConfig.load`` (an unbounded ``read_text`` + ``json.loads`` +
            # ``jsonschema.validate`` on a cold/invalidated cache, e.g. right after
            # a registry refresh rewrites config).  ``_fetch_git_blob`` runs on the
            # gateway event loop during App Store browsing, so calling it inline
            # would freeze every concurrent chat turn and the liveness heartbeat —
            # offload it, exactly like the adjacent reads.
            clone_mode = await asyncio.to_thread(_context_clone_sandbox_mode, git_url)
            clone_env = minimal_env()
            # Escalating this clone from anonymous+strict to owner credentials is
            # a security-relevant permission decision — leave an SEL audit record,
            # mirroring the merged carve-out's grants at the manifest/install sites.
            await asyncio.to_thread(_sel_credential_grant, "app_blob_proxy", git_url)
        else:
            clone_mode = "strict"
            clone_env = anonymous_git_env()
        clone_root = Path(tmp_root)
        if credentialed_transport:
            # A combined credentialed clone performs fetch and checkout in one
            # process. Repository-controlled filters (and checkout-time hooks)
            # would therefore inherit the one-shot URL rewrite containing the
            # secret. Reuse the registry split: only `git fetch` receives that
            # environment; init, checkout and file reads use the credential-free
            # base environment.
            clone_root /= "branch"
            fetch_log: list[str] = []
            fetch_error = await _git_fetch_branch(
                git_url,
                ref,
                clone_root,
                fetch_log,
                credential_target=credential_target,
                clone_env=clone_env,
                sandbox_mode=clone_mode,
            )
            if fetch_error is not None:
                logger.debug(
                    "git fetch failed for %s/%s: %s",
                    _strip_git_target_userinfo(repo),
                    file_path,
                    _loggable_git_transport_output("\n".join(fetch_log), credentialed=True),
                )
                return False
        else:
            clone_cmd = [
                "git",
                *_HOOKS_NEUTRALIZER_ARGV,
                "clone",
                "--depth",
                "1",
                "--branch",
                ref,
                "--single-branch",
                git_url,
                tmp_root,
            ]
            sandboxed_cmd, _cleanup = await wrap_argv_async(
                clone_cmd,
                mode=clone_mode,
                forward_ssh_auth_sock=owner_designated,
                _prepare=wrap_argv,
            )
            sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clone_env,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_BLOB_FETCH_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                logger.warning(
                    "git clone timed out for %s/%s",
                    _strip_git_target_userinfo(repo),
                    file_path,
                )
                return False

            if proc.returncode != 0:
                logger.debug(
                    "git clone failed for %s/%s: %s",
                    _strip_git_target_userinfo(repo),
                    file_path,
                    (
                        _loggable_git_transport_output(
                            stderr.decode(errors="replace").strip(),
                            credentialed=False,
                        )
                        if stderr
                        else ""
                    ),
                )
                return False

        # Read the requested file from the checkout, guarding against escapes
        # out of the clone via symlinks or traversal.
        clone_root = clone_root.resolve()
        blob_path = (clone_root / file_path).resolve()
        try:
            blob_path.relative_to(clone_root)
        except ValueError:
            logger.debug(
                "blob path escapes clone root for %s/%s",
                _strip_git_target_userinfo(repo),
                file_path,
            )
            return False
        if not blob_path.is_file():
            return False
        data = await asyncio.to_thread(blob_path.read_bytes)
    except OSError as exc:
        logger.debug(
            "Failed to fetch blob from %s/%s: %s",
            _strip_git_target_userinfo(repo),
            file_path,
            _loggable_git_transport_output(str(exc), credentialed=credentialed_transport),
        )
        return False
    finally:
        if tmp_root:
            await asyncio.to_thread(platform_compat.rmtree_force, tmp_root)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(cache_path.write_bytes, data)
    return True


async def handle_blob_proxy(request: web.Request) -> web.Response:
    """GET /api/apps/blob — proxy image files from a registry app's git repo.

    Query params:
      repo  — registry repo identifier (matches a registry entry's ``repo``)
      path  — file path within the repo (e.g. "assets/icon/logo.png")
      ref   — git ref, defaults to "main"

    SECURITY: Only serves repos listed in the registry JSON (prevents SSRF).
    Caches fetched blobs to ~/.kiro/crew/cache/blobs/{repo}/{ref}/{path}.
    Only serves image file types.
    """
    repo = request.query.get("repo", "")
    file_path = request.query.get("path", "")
    # Resolve the registry entry once: it feeds both the branch fallback below
    # and the same-repo credential carve-out decision at fetch time.  The lookup
    # reads local sync caches only (never fetches), so it is cheap and safe here.
    entry = await asyncio.to_thread(get_registry_app_by_repo, repo) if repo else None
    # Look up the registry entry's branch; fall back to query param or main
    ref = request.query.get("ref", "")
    if not ref:
        ref = entry.get("branch", "main") if entry else "main"

    # Validate inputs
    if not repo or not file_path:
        return web.json_response({"error": "repo and path required"}, status=400)
    if not _is_safe_repo_identifier(repo):
        return web.json_response({"error": "invalid repo URL or name"}, status=400)
    if not _SAFE_PATH_RE.match(file_path):
        return web.json_response({"error": "invalid path characters"}, status=400)
    if not _SAFE_REF_RE.match(ref):
        return web.json_response({"error": "invalid ref"}, status=400)
    if ".." in file_path or file_path.startswith("/"):
        return web.json_response({"error": "invalid path"}, status=400)
    # ``ref`` becomes a path segment in the blob cache tree
    # (``.../{repo_key}/{ref}/{file_path}``).  ``_SAFE_REF_RE`` permits ``.`` and
    # ``/``, so a value like ``../<other-repo-key>/main`` matches the regex; the
    # cache-root containment check below catches an escape OUT of the cache root
    # but NOT a ``..`` that stays UNDER the root while crossing into a DIFFERENT
    # repo's cache directory — a crafted ``ref`` would then yield a cache hit that
    # returns another repo's cached (possibly private) bytes without
    # authorization.  Reject any ``..`` segment or leading ``/`` in ``ref``
    # BEFORE the cache path is built or read from it, mirroring the
    # ``file_path`` guard above, so a ``ref`` can only ever name a flat branch
    # subtree under its own ``repo_key``.
    if ".." in ref or ref.startswith("/"):
        return web.json_response({"error": "invalid ref", "code": "blob_invalid_ref"}, status=400)
    # Block access to git internals and other hidden directories
    if any(seg.startswith(".") for seg in Path(file_path).parts):
        return web.json_response({"error": "hidden path segments not allowed"}, status=400)

    ext = Path(file_path).suffix.lower()
    if ext not in _ART_IMAGE_EXTENSIONS:
        return web.json_response({"error": f"file type {ext!r} not allowed"}, status=403)

    # SECURITY: Only allow repos that appear in the registry (prevents SSRF)
    allowed = await asyncio.to_thread(known_registry_repos)
    if repo not in allowed:
        return web.json_response({"error": "repo not in registry"}, status=403)

    # Resolve the blob's PROVENANCE — the clone URL — BEFORE the cache lookup, so
    # the cache key can be bound to it.  A ``repo`` key alone is not stable
    # provenance: registry A (private) can cache a blob under key X, be removed,
    # and registry B later be configured reusing key X — then B's request would
    # hit A's cached private bytes.  Binding the cache key to the resolved clone
    # URL namespaces the cache by the URL the bytes were actually cloned from, so
    # a repo-key reuse across registries lands in a distinct directory (a miss +
    # a fresh clone of B's own URL) rather than serving A's stale bytes.
    #
    # Both resolutions here are PURE in-memory (no registry read, no event-loop
    # stall): ``_entry_git_url`` reads the already-loaded ``entry`` dict, and the
    # no-entry branch is a string-shape test on the already-validated ``repo``.
    # The SAME ``entry`` object also backs the ``owner_designated`` credential
    # decision below, so the URL that keys the cache, the URL authorized, and the
    # URL cloned are one value by identity.
    if entry is not None:
        clone_url = _entry_git_url(entry)
    else:
        # No bundled entry: an external (federated) registry whose ``repo`` is
        # itself a full git URL never resolves an ``entry`` (that lookup searches
        # bundled entries only), yet ``_is_safe_repo_identifier`` admits such
        # URLs.  Honor the validated URL directly, or external blobs become
        # unreachable.  A second ``get_registry_app_by_repo`` read would stall the
        # event loop and reopen the TOCTOU seam, so this is a string-shape test on
        # ``repo``, never a lookup.
        clone_url = (
            repo if ("://" in repo) or repo.startswith("git@") or repo.endswith(".git") else ""
        )
    if not clone_url:
        logger.debug(
            "No git URL resolvable for registry repo %r — skipping blob fetch",
            _strip_git_target_userinfo(repo),
        )
        return web.json_response(
            {"error": "failed to fetch blob", "code": "blob_no_git_url"}, status=502
        )

    # Check cache.  ``repo`` may now be a full git URL (containing ``/`` and
    # ``:``), so derive a flat, filesystem-safe, injective cache key rather than
    # using the raw value as a directory tree.  The key is bound to the resolved
    # ``clone_url`` (provenance) so a repo-key reuse across registries cannot
    # serve another registry's cached bytes.  The resolved-path check below still
    # guards against any escape out of the cache root.
    repo_key = _blob_cache_key(repo, clone_url)
    cache_path = _blob_cache_dir() / repo_key / ref / file_path

    # SECURITY: Verify resolved path stays within cache dir BEFORE any
    # filesystem side effects (mkdir).  We resolve the parent against the
    # cache root to catch symlink-based escapes.
    cache_root_resolved = _blob_cache_dir().resolve()
    try:
        resolved_parent = cache_path.parent.resolve()
    except OSError:
        resolved_parent = cache_path.parent
    try:
        resolved_parent.relative_to(cache_root_resolved)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)
    resolved = cache_path.resolve()
    try:
        resolved.relative_to(cache_root_resolved)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    # Safe to create directories now that path is validated
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if not cache_path.is_file():
        # Same-repo credential carve-out, extended to the blob chokepoint:
        # only when the entry's clone URL is byte-identical to the owner-typed
        # registry repo does the clone get owner credentials.  Reuse the merged
        # predicate verbatim — no host normalization, no index-supplied URL trust;
        # a bundled entry (no ``_registry``) or a sibling repo on the same host
        # returns False and stays anonymous + strict.
        #
        # ``clone_url`` was resolved above from the SAME ``entry`` object the
        # credential decision is made against. The public identity remains the
        # cache key and argv URL; an exact raw config target is handed off
        # separately and accepted only when it sanitizes back to that identity.
        # The callee never re-resolves from ``repo``. This closes
        # a TOCTOU window: if the callee re-read the URL from ``repo`` a concurrent
        # registry refresh could, between this decision and the clone, swap the
        # entry backing ``repo`` to a private sibling — and the owner-credential
        # grant decided for the old URL would then clone the new one.  One read
        # of one entry makes ``owner_designated`` and ``clone_url`` describe the
        # same value by identity, not "by construction" across two reads.
        owner_designated = False
        owner_credential_target = ""
        if entry is not None:
            # The owner-credential grant must be scoped to the entry's CONFIGURED
            # branch, not an attacker-chosen ``ref``.  ``ref`` falls back to the
            # entry's ``branch`` only when the query param is empty; a caller can
            # otherwise supply any ``_SAFE_REF_RE``-valid ``ref`` (e.g.
            # ``iconPath=logo.png&ref=private``).  Without this gate the grant is
            # decided on the entry alone, so a crafted ``ref`` would drive an
            # owner-credentialed clone of an UNCONFIGURED (e.g. private) branch of
            # the owner's repo and serve its image bytes.  The configured branch
            # is the only ref the owner designated for this registry; require the
            # effective ``ref`` to equal it before honoring ``owner_designated``.
            # A differing ``ref`` is not rejected (the anonymous path still serves
            # a public branch) — it simply never attaches credentials.
            configured_branch = entry.get("branch", "main")
            if ref == configured_branch:
                # ``get_registry_app_by_repo`` selected this entry by ``repo`` key
                # alone (bundled first, then each external registry).  Provenance
                # is only unambiguous — and the owner-credential grant only
                # honestly attributable to the entry actually being served — when
                # exactly ONE configured source publishes that key.  If more than
                # one registry claims the same ``repo``, a request reachable
                # through registry B could resolve to registry A's owner-designated
                # entry, so downgrade to anonymous+strict (never grant) rather than
                # clone A's private repo with A's credentials on a B-reachable
                # request.  Only a single owner may reach
                # ``_owner_designated_repo_target``; the grant then stays gated on the
                # entry-scoped byte-identical URL check as before (no widening).
                owner_count = await asyncio.to_thread(_repo_key_owner_count, repo)
                if owner_count == 1:
                    owner_credential_target = await asyncio.to_thread(
                        _owner_designated_repo_target, entry
                    )
                    owner_designated = bool(owner_credential_target)
        async with _BLOB_FETCH_SEMAPHORE:
            # Re-check after acquiring semaphore (another request may have cached it)
            if not cache_path.is_file():
                fetch_kwargs: dict[str, Any] = {
                    "git_url": clone_url,
                    "owner_designated": owner_designated,
                }
                if owner_credential_target and owner_credential_target != clone_url:
                    fetch_kwargs["credential_target"] = owner_credential_target
                ok = await _fetch_git_blob(
                    repo,
                    ref,
                    file_path,
                    cache_path,
                    **fetch_kwargs,
                )
                if not ok:
                    return web.json_response({"error": "failed to fetch blob"}, status=502)

    content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    # Keep a SERVED file young. ``_gc_blob_cache_dir`` reclaims a blob-cache file whose
    # mtime has aged past the grace, and only the owner-tier prewarm re-publishes (and
    # so re-dates) a live row's art; an index-tier blob the proxy wrote once is never
    # re-published, so without this a file that is still being served every day would
    # age out and be reclaimed, then re-cloned on the next browse. Touching the mtime on
    # each serve makes "being served" keep a file young exactly as a rewrite does, so the
    # sweep only reclaims art that is neither served nor rewritten. Off the event loop:
    # ``os.utime`` is a blocking syscall (it can round-trip a network-homed data home),
    # and this handler runs on the loop. Best-effort: a serve must not fail because the
    # touch did (a read-only mount, a racing GC unlink), so any error is ignored -- the
    # file is served regardless, and at worst ages out one grace window later than it
    # might have. ``follow_symlinks=False``: the name was validated by the containment
    # check above, and a link planted at it afterwards must not have its TARGET touched.
    # Windows has no ``follow_symlinks=False`` for ``os.utime`` (it raises
    # NotImplementedError, which would skip every touch there), so where the flag is
    # unsupported the name is lstat-checked as a plain regular file first and touched
    # by name.
    try:
        await asyncio.get_running_loop().run_in_executor(None, _touch_served_blob, cache_path)
    except (OSError, NotImplementedError):
        pass
    sel().log_api_access(
        caller="dashboard",
        operation="app_blob_proxy",
        outcome="served",
        resources=f"repo={_strip_git_target_userinfo(repo)} path={file_path}",
    )
    return web.FileResponse(  # type: ignore[return-value]
        cache_path,
        headers={
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=86400",  # 24h browser cache
        },
    )


async def handle_migrate_cleanup(request: web.Request) -> web.Response:
    """DELETE /api/apps/{name}/migrate-cleanup — uninstall a retired builtin, keeping data."""
    from kiro_crew.apps.manager import (
        AppResult,
        _check_path_safety,
        _read_installed,
        migrated_builtin_cleanup_applies,
    )
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    if request.get("app"):
        sel().log_api_access(
            caller=request.get("app", ""),
            operation="app_migrate_cleanup",
            outcome="denied",
            resources=request.path,
            error="app token cannot clean up migrated apps",
        )
        return web.json_response(
            {
                "ok": False,
                "error": "app tokens cannot clean up migrated apps",
                "code": "app_token_forbidden",
            },
            status=403,
        )
    denied = await require_owner_dashboard_request(request, "app_migrate_cleanup")
    if denied is not None:
        return denied

    name = request.match_info["name"]
    result = AppResult(
        ok=False, name=name, error="not a migrated builtin", error_code="not_orphaned"
    )
    if _check_path_safety(name):
        meta = await asyncio.to_thread(_read_installed, name)
        if meta is None:
            result = AppResult(ok=True, name=name, message="not installed — nothing to clean up")
        elif meta.origin != "builtin":
            result = AppResult(
                ok=True, name=name, message="already migrated — standalone version is in place"
            )
        elif await asyncio.to_thread(migrated_builtin_cleanup_applies, name):
            info = await asyncio.to_thread(get_app, name)
            if info is not None:
                response = await _run_uninstall(
                    request,
                    name,
                    info,
                    keep_data=True,
                    keep_dependencies=False,
                    keep_specific=[],
                    retired_builtin=True,
                )
                # Uninstall precondition refusals retain their status and retry contract.
                payload = json.loads(response.text or "{}")
                payload["ok"] = response.status < 400
                return web.json_response(payload, status=response.status)
    sel().log_api_access(
        caller="dashboard",
        operation="app_migrate_cleanup",
        outcome="completed" if result.ok else "denied",
        resources=name,
        error=result.error,
    )
    if result.ok:
        return web.json_response(result.to_dict())
    return web.json_response(
        {"ok": False, "name": name, "error": result.error, "code": "not_orphaned"}, status=400
    )


def register_app_routes(app: web.Application) -> None:
    """Register all app management routes on an aiohttp Application."""

    async def _start_proxy_session(app: web.Application) -> None:
        app["_proxy_session"] = aiohttp.ClientSession()

    async def _close_proxy_session(app: web.Application) -> None:
        session = app.get("_proxy_session")
        if session and not session.closed:
            await session.close()

    app.on_startup.append(_start_proxy_session)
    app.on_cleanup.append(_close_proxy_session)

    app.router.add_get("/api/apps", handle_list_apps)
    app.router.add_get("/api/publish-providers", handle_publish_providers)
    app.router.add_get("/api/apps/registry", handle_registry)
    app.router.add_get("/api/apps/registries", handle_registries)
    app.router.add_put("/api/apps/registries", handle_registries)
    app.router.add_post("/api/apps/registries/refresh", handle_registries_refresh)
    app.router.add_get("/api/apps/blob", handle_blob_proxy)
    # Outside /api/apps/ on purpose: _app_owns_path would grant an app named
    # `registry` implicit ownership of /api/apps/registry/* -- see the
    # handler's docstring.
    app.router.add_post("/api/app-store/refresh", handle_registry_refresh)
    app.router.add_post("/api/apps/registry/install", handle_registry_install)
    app.router.add_post("/api/apps/registry/install-stream", handle_registry_install_stream)
    app.router.add_post("/api/apps/install", handle_install_app)
    app.router.add_post("/api/apps/register", handle_register_external)
    app.router.add_get("/api/apps/{name}", handle_get_app)
    app.router.add_get("/api/apps/{name}/manifest", handle_get_manifest)
    app.router.add_get("/api/apps/{name}/config", handle_app_config)
    app.router.add_put("/api/apps/{name}/config", handle_app_config)
    app.router.add_get("/api/apps/{name}/uninstall/preview", handle_uninstall_preview)
    app.router.add_post("/api/apps/{name}/uninstall", handle_uninstall_app)
    app.router.add_post("/api/apps/{name}/update", handle_update_app)
    app.router.add_post("/api/apps/{name}/enable", handle_enable_app)
    app.router.add_post("/api/apps/{name}/disable", handle_disable_app)
    app.router.add_post("/api/apps/{name}/open", handle_open_app)
    app.router.add_post("/api/apps/{name}/dev", handle_app_dev_mode)
    app.router.add_delete("/api/apps/{name}/migrate-cleanup", handle_migrate_cleanup)
    app.router.add_get("/apps/{name}/ui/{path:.*}", handle_app_ui_file)
    app.router.add_get("/apps/{name}/art/{path:.*}", handle_app_art_file)
    # Reverse proxy: dashboard app UI → app backend (same-origin, avoids CORS)
    app.router.add_route("*", "/apps/{name}/api/{path:.*}", handle_app_api_proxy)


# This module keeps the handlers whose blocking file-system calls the sync-io fs
# baseline counts against this path, the units that call them, the installed-art route
# and the route table. It is also the facade of the app management route owners: every
# other name is reached through it as ``kiro_crew.apps.routes.<name>``, a read is served by
# the owner that binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch``
# -- reaches every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never
# import this module, and each imports only the owners listed before it in ``_OWNERS``.
# New work goes to the owner of its responsibility:
#
#   ``routes_lifecycle`` -- Steps install, update, uninstall, enable and disable share.
#   ``routes_proxy`` -- The reverse proxy from an app's UI to its backend, and its secret cache.
#   ``routes_static`` -- An installed app's UI bundle and the dev-mode toggle.
#   ``routes_art`` -- Reading declared art; the validators and fetch bounds the blob proxy shares.
#   ``routes_listing`` -- The app list, one app, its manifest, and the publish providers.
#   ``routes_install`` -- Updating an app, and registering an external app.
#   ``routes_uninstall`` -- The uninstall preview and the bounded cron deregistration retry.
#   ``routes_enable`` -- Enable, disable and open an installed app.
#   ``routes_registry`` -- Browsing, refreshing and installing from the registry; its configured sources.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.routes_lifecycle",
    "kiro_crew.apps.routes_proxy",
    "kiro_crew.apps.routes_static",
    "kiro_crew.apps.routes_art",
    "kiro_crew.apps.routes_listing",
    "kiro_crew.apps.routes_install",
    "kiro_crew.apps.routes_uninstall",
    "kiro_crew.apps.routes_enable",
    "kiro_crew.apps.routes_registry",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.apps.routes_art import (  # noqa: F401
        _SAFE_HTTPS_URL_RE,
        _SAFE_REPO_RE,
        _SAFE_SCP_URL_RE,
        _SAFE_SSH_URL_RE,
        _declared_art_paths,
    )
    from kiro_crew.apps.routes_enable import (  # noqa: F401
        _BUILTIN_SERVICE_APPS,
        _OPEN_COMMAND_DESKTOP_ENV_KEYS,
        _client_install_manifest,
        _notify_builtin_service,
        _sync_builtin_config,
    )
    from kiro_crew.apps.routes_install import (  # noqa: F401
        _resync_app_crons_after_update,
    )
    from kiro_crew.apps.routes_lifecycle import (  # noqa: F401
        _redact_warning,
        _restore_app_after_failed_update,
    )
    from kiro_crew.apps.routes_listing import (  # noqa: F401
        _installed_app_detail,
        _listed_apps_with_trust,
        _provider_is_configured,
        _stamp_installed_trust_repository,
        collect_publish_providers,
    )
    from kiro_crew.apps.routes_proxy import (  # noqa: F401
        _PROXY_HOP_HEADERS,
        _PROXY_IDLE_TIMEOUT,
        _PROXY_STRIP_HEADERS,
        _PROXY_TIMEOUT,
        _app_secret_cache,
        _end_relay_midbody,
        _get_app_secret,
        _is_event_stream,
        _proxy_principal,
        _resolve_app_backend_url,
    )
    from kiro_crew.apps.routes_registry import (  # noqa: F401
        _announce_app_updates,
        _derive_registry_name,
        _write_registries_config,
    )
    from kiro_crew.apps.routes_static import (  # noqa: F401
        _ALLOWED_EXTENSIONS,
        _UI_MAX_BYTES,
        _UI_STREAM_CHUNK,
        _UI_STREAM_SEMAPHORE,
        _UI_STREAM_TIMEOUT_DEFAULT,
        _open_ui_file,
        _ui_stream_timeout_secs,
    )
    from kiro_crew.apps.routes_uninstall import (  # noqa: F401
        _CRON_CLEANUP_ATTEMPTS,
        _CRON_CLEANUP_BACKOFF_SECS,
    )

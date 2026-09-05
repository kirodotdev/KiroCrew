"""App management REST API endpoints for the KiroCrew dashboard.

All endpoints are registered under ``/api/apps`` by the dashboard handler
setup. These are aiohttp-compatible handler functions.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac
import importlib
import json
import logging
import mimetypes
import os
import posixpath
import re
import shutil
import stat
import sys
import time
import urllib.parse
from email.utils import formatdate
from functools import partial
from pathlib import Path
from typing import Any

import aiohttp
import yarl
from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.apps import official_catalog
from kiro_crew.apps.backend import (
    get_app_backend_port,
    list_app_processes,
    start_app_backend,
    stop_app_backend,
)
from kiro_crew.apps.bridges import (
    RegistrationResult,
    deregister_app,
    deregister_app_crons_from_service,
    register_app,
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
    app_lifecycle_lock,
    apps_dir,
    cleanup_migrated_builtin,
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
from kiro_crew.apps.manifest import Dependencies, PlatformConfig
from kiro_crew.apps.official_category_order import forget_cache as forget_category_order_cache
from kiro_crew.apps.official_category_order import load_category_order
from kiro_crew.apps.official_editorial import forget_cache as forget_editorial_cache
from kiro_crew.apps.official_editorial import load_sections
from kiro_crew.apps.registry import (
    _REGISTRY_TRUST_TIERS,
    _TRUST_INDEX,
    _TRUST_OWNER,
    _context_clone_sandbox_mode,
    _entry_git_url,
    _git_fetch_branch,
    _git_target_is_unsupported,
    _git_url_host,
    _loggable_git_transport_output,
    _owner_designated_repo_target,
    _pinned_registries,
    _registry_identity_key,
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
from kiro_crew.apps.spawn_sdk import build_spawn_impl
from kiro_crew.apps.teardown import forget_app_hooks, teardown_app_runtime
from kiro_crew.apps.version import check_min_version as _check_min_version_str
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_dir,
    config_path,
    update_config_locked,
)
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable
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
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Version compatibility check
# ---------------------------------------------------------------------------


def _check_min_version(manifest_data: dict[str, Any]) -> str | None:
    """Check if the app requires a newer KiroCrew version.

    Returns an error message if the current version is too old, or None if OK.
    """
    return _check_min_version_str(manifest_data.get("minKiroCrewVersion", ""))


# ---------------------------------------------------------------------------
# Builtin app helpers — sync config.json and stop/start live services
# ---------------------------------------------------------------------------


def _redact_warning(msg: str) -> str:
    """Redact credentials and exfiltration URLs from warning strings."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    msg, _ = redact_credentials(msg)
    msg, _ = redact_exfiltration_urls(msg)
    return msg


# Maps builtin app names to their config.json key and dashboard state
# restart callback attribute.  Only apps with a live gateway service
# (not just metadata) need entries here.  Empty in the open-source build —
# no bundled builtin ships a live gateway service.
_BUILTIN_SERVICE_APPS: dict[str, tuple[str, str]] = {}


def _unregister_notification_channels(request: web.Request, name: str) -> None:
    """Drop *name*'s notification channels from the bus registry.

    Called on uninstall/disable so channels don't linger as ghosts. Best
    effort and side-effect free beyond the in-memory registry: the push
    path independently re-checks enablement, so this is hygiene, not a
    security control. Re-enabling re-registers lazily on first push.
    """
    state = request.app.get("state")
    bus = getattr(state, "notification_bus", None) if state is not None else None
    if bus is None:
        return
    removed = bus.unregister_app_channels(name)
    if removed:
        logger.info("Unregistered %d notification channel(s) for app %s", removed, name)


def _sync_builtin_config(name: str, *, enabled: bool) -> None:
    """Update config.json for a builtin app so gateway reads the right state on restart.

    Blocking: performs a file-locked read-modify-write of config.json and then,
    on Windows, applies the owner-only lockdown (``restrict_to_owner``,
    in-process — but a possible SMB round-trip on a network-homed data home).
    Callers on the event loop must offload this through ``asyncio.to_thread``.
    """
    cfg_key, _ = _BUILTIN_SERVICE_APPS.get(name, (None, None))
    if cfg_key is None:
        return

    def _mutate(data: dict) -> dict:
        data.setdefault(cfg_key, {})["enabled"] = enabled
        return data

    # update_config_locked, the required path for new config.json mutations:
    # the whole read-modify-write runs under an advisory sidecar file lock, so
    # this write is serialized against every other converted writer and other
    # processes — not merely against itself, which is all an in-module lock
    # could offer. The write itself is mode-preserving (an operator's
    # tightened 0600 survives) and symlink-safe. A corrupt config fails
    # closed; it is re-raised as OSError because that is the failure type the
    # call sites catch and surface as a warning.
    try:
        update_config_locked(config_path(), mutate=_mutate)
    except ConfigReadError as exc:
        raise OSError(f"Could not read config.json: {exc}") from exc
    if not platform_compat.IS_POSIX:  # pragma: no cover — exercised on Windows CI
        # POSIX mode bits are meaningless on Windows, so the preserved mode
        # protects nothing there. update_config_locked's write path
        # (write_config_atomically) applies the owner-only DACL itself on a
        # LOCAL volume, but deliberately skips it on a network-homed data home
        # (it can be reached from the event loop, and a DACL write to a UNC or
        # mapped-drive path costs an unbounded SMB round-trip). This caller
        # runs off-loop (to_thread), so it can afford that round-trip and
        # applies the lockdown unconditionally — on a network-homed data home
        # this is the only lockdown config.json gets, and config.json can hold
        # inline credentials. Warn rather than raise — the settings write
        # itself succeeded, and the callers must still notify the service of
        # the new enabled state.
        try:
            platform_compat.restrict_to_owner(config_path())
        except OSError:
            logger.warning("could not restrict config.json permissions", exc_info=True)
    logger.info("Synced config.json %s.enabled = %s", cfg_key, enabled)


async def _notify_builtin_service(request: web.Request, name: str) -> str | None:
    """Stop/start a builtin service via its dashboard restart callback.

    Returns None on success, or a warning string on failure.
    The restart callback re-reads config.json, so calling _sync_builtin_config
    first ensures the service picks up the new enabled state.
    """
    _, restart_attr = _BUILTIN_SERVICE_APPS.get(name, (None, None))
    if restart_attr is None:
        return None
    state = request.app.get("state")
    if state is None:
        return "no gateway state available — restart gateway to apply"
    restart_fn = getattr(state, restart_attr, None)
    if restart_fn is None:
        return "no restart callback available — restart gateway to apply"
    try:
        result = await restart_fn()
        if result == "ok" or result == "init returned without service":
            return None
        return f"restart returned: {result}"
    except Exception as exc:
        logger.warning("Builtin service restart failed for %s: %s", name, exc)
        return f"restart failed: {exc}"


def _stamp_installed_trust_repository(app: dict[str, Any]) -> dict[str, Any]:
    """Overwrite the consent target from server-resolved provenance."""
    app.pop("trustRepository", None)
    try:
        resolved, repository = resolve_installed_trust_repository(app)
    except Exception:
        # Catalog failure or corrupt registry state must not make the app list
        # fail. Omitting the proof leaves the grant handler fail-closed.
        logger.warning(
            "could not resolve trust repository for installed app %r",
            app.get("name", ""),
            exc_info=True,
        )
        resolved, repository = False, ""
    if resolved and repository:
        app["trustRepository"] = repository
    # Installed provenance and manifest metadata are also returned by the apps
    # API. Keep clone credentials server-side even when an old install record
    # persisted a credential-bearing source URL.
    source = app.get("source")
    if isinstance(source, str):
        app["source"] = _credential_free_source_metadata(source)
    for coordinate_key in ("sourceUrl", "repo", "gitUrl"):
        coordinate = app.get(coordinate_key)
        if isinstance(coordinate, str):
            app[coordinate_key] = _strip_git_target_userinfo(coordinate)
    source_registry = app.get("sourceRegistry")
    if isinstance(source_registry, str):
        app["sourceRegistry"] = _credential_free_source_metadata(source_registry)
    manifest = app.get("manifest")
    if isinstance(manifest, dict):
        manifest_repo = manifest.get("repo")
        if isinstance(manifest_repo, str):
            manifest["repo"] = _strip_git_target_userinfo(manifest_repo)
    return app


def _listed_apps_with_trust() -> list[dict[str, Any]]:
    """Read installed apps and resolve their consent coordinates synchronously.

    Both halves may touch disk (and the legacy provenance resolver may consult
    registry/catalog state), so async handlers must offload this whole helper
    rather than moving only :func:`list_apps` off the event loop.
    """
    installed = list_apps()
    for app in installed:
        _stamp_installed_trust_repository(app)
    return installed


async def handle_list_apps(request: web.Request) -> web.Response:
    """GET /api/apps — list all installed apps."""
    # list_apps() walks the apps dir and reads two files per installed app, and
    # this endpoint re-runs it on every dashboard refresh — so the walk goes off
    # the loop (its cost scales with installed app count).
    apps = await asyncio.to_thread(_listed_apps_with_trust)
    # Enrich with backend process status
    procs = {p["app_name"]: p for p in list_app_processes()}
    # ...and with in-process hook wiring health, which has no process to inspect:
    # an app whose route hook failed to import has no route-table entry, so the
    # dispatcher answers 404 exactly like an app that was never installed. This is
    # the only place that failure becomes visible to an operator.
    #
    # Reported under the same "hooks" envelope and the same "health_status" key the
    # enable response already uses, so one record has ONE public spelling rather
    # than a second flat name to maintain. The envelope also keeps the subsystem
    # explicit: this is hook-wiring health, not the subprocess backend_status.
    hook_health = get_all_hook_health()
    for app in apps:
        proc = procs.get(app["name"])
        if proc:
            app["backend_status"] = {
                # Both flags come from the tracking record rather than being asserted
                # from its mere presence: a tracked backend can have exited (running) or
                # stopped answering its health endpoint (healthy) since it was started.
                "running": proc["running"],
                "port": proc["port"],
                "healthy": proc["healthy"],
                "pid": proc["pid"],
            }
        health = hook_health.get(app["name"])
        if health:
            if health.get("issues"):
                health["issues"] = [_redact_warning(i) for i in health["issues"]]
            app["hooks"] = {"health_status": health}
    return web.json_response(apps)


def _provider_is_configured(app_name: str, pp: dict[str, Any]) -> bool:
    """Resolve a provider's configured-state by reading the app's persisted config.

    Core never imports app code: it reads ``<apps_dir>/<app>/data/<configFile>`` and
    checks that ``configuredField`` is non-empty. When no ``configuredField`` is
    declared, the provider is considered configured as soon as the app is enabled.
    """
    field_name = str(pp.get("configuredField", "")).strip()
    if not field_name:
        return True
    config_file = str(pp.get("configFile", "config.json")) or "config.json"
    if ".." in config_file or "/" in config_file or "\\" in config_file:
        return False  # defensive: no path traversal in the declared config filename
    cfg_path = apps_dir() / app_name / "data" / config_file
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(cfg, dict) and str(cfg.get(field_name, "")).strip())


def collect_publish_providers(
    apps: list[dict[str, Any]],
    configured_resolver: Any = None,
) -> list[dict[str, Any]]:
    """Aggregate **enabled** apps that declare a publishProvider (design §1.3, Route B).

    Pure and testable — pass ``configured_resolver(app_name, pp_dict) -> bool`` to avoid
    touching the filesystem in tests. Each returned provider carries a ``configured``
    flag so the artifact page can render the publish action when configured or a
    "set it up" link otherwise. Built-in providers (e.g. the internal registry) are registered
    on the frontend; this function contributes only the app-declared ones.

    Endpoint allowlist (§9.3 security): app-declared provider endpoints MUST match
    ``/api/apps/<that-app>/`` — an app cannot declare an endpoint that routes to
    another app's namespace or to a core API. Non-conforming endpoints are dropped
    with a warning log.
    """
    resolver = configured_resolver or _provider_is_configured
    providers: list[dict[str, Any]] = []
    for app in apps:
        if not app.get("enabled"):
            continue
        manifest = app.get("manifest") or {}
        pp = manifest.get("publishProvider") or {}
        if not isinstance(pp, dict) or not pp.get("id") or not pp.get("endpoint"):
            continue
        app_name = str(app.get("name", ""))
        endpoint = str(pp["endpoint"])
        # Endpoint allowlist: must route within the app's own namespace.
        # Normalize BEFORE checking to prevent dot-segment traversal
        # (e.g. "/api/apps/foo/../../shutdown" bypassing prefix check).
        decoded_endpoint = urllib.parse.unquote(endpoint)
        normalized_endpoint = posixpath.normpath(decoded_endpoint)
        allowed_prefix = f"/api/apps/{app_name}/"
        if (
            ".." in decoded_endpoint
            or normalized_endpoint != decoded_endpoint.rstrip("/")
            # Boundary-safe prefix check: appending "/" prevents a sibling-app
            # collision ("/api/apps/foobar/x" passing app "foo"'s allowlist).
            or not (normalized_endpoint + "/").startswith(allowed_prefix)
        ):
            logger.warning(
                "publish provider for app %r declares non-conforming endpoint %r "
                "(must start with %r, no traversal) — dropping",
                app_name,
                endpoint,
                allowed_prefix,
            )
            continue
        providers.append(
            {
                "id": str(pp["id"]),
                "label": str(pp.get("label", pp["id"])),
                "icon": str(pp.get("icon", "")),
                "endpoint": endpoint,
                "kinds": [str(k) for k in pp.get("kinds", []) if k],
                "setupRoute": str(pp.get("setupRoute", "")),
                "app": app_name,
                "origin": "app",
                "configured": bool(resolver(app_name, pp)),
            }
        )
    return providers


async def handle_publish_providers(request: web.Request) -> web.Response:
    """GET /api/publish-providers — publish destinations (app-declared + core deploy).

    Returns enabled apps' publish providers plus the core deploy provider (folded
    from the former deploy_web app), each with a ``configured`` flag. Built-in
    providers (the internal registry) are registered frontend-side and are not returned here.

    The core deploy row is omitted when EITHER control closes the public-web
    path, and the two are independent:

    * the platform's ``external_access`` policy withholds cloud deployment; or
    * the publish-governance chokepoint denies its destination id (governance
      ceiling ∩ ``publish.allowed_destinations``), so an operator who has closed
      the path never sees the button.

    Because this list is what the Publish panel renders, that single omission is
    also what makes the panel correct — a deployment that registers only an
    internal destination shows only that one, with no frontend change. Omission is
    presentation only: ``/api/deploy/deploy`` consults the same chokepoint itself,
    because a filtered list is not a control.

    On EITHER closed path, rows carrying ``DEPLOY_WEB_PROVIDER_ID`` are dropped
    from the app-declared list too — the platform withhold and the governance
    denial share one closed path for exactly this reason.
    ``collect_publish_providers`` validates
    a declared *endpoint* (it must sit under the app's own namespace) but not the
    declared *id*, so an enabled app may publish a row under the core
    destination's id — and ``PublishHub`` routes a click at
    ``selected.app.endpoint``, which this chokepoint does not cover. Serving that
    row after the operator closed the destination would hand back the path they
    closed.

    On the PERMITTED path such a row is left alone. An app shadowing this id is
    pre-existing, reachable behaviour that ``test_publish_providers`` documents
    (its fixture app declares this very id and the test asserts the APP's endpoint
    is the one resolved for it, first-match order). Whether the core provider
    should own the id outright is a behaviour change worth making on its own
    merits, not a side effect of closing a denial hole.
    """
    # Both the apps-dir walk (list_apps) and the per-provider configured-state
    # probe (_provider_is_configured reads each app's persisted config file)
    # touch disk, so the whole collection runs off the loop — same shape as the
    # deploy registry read below.
    providers = await asyncio.to_thread(lambda: collect_publish_providers(list_apps()))
    # Two independent controls can close this destination, and BOTH must land on
    # the same closed path — an early return for one of them is how a closed
    # destination stays reachable (an app-declared row carrying the core id
    # publishes at its OWN endpoint, which this chokepoint does not cover).
    # circular import: apps.routes is imported by the dashboard handler layer, so
    # reaching back into it must be a function-local downward import.
    from kiro_crew.dashboard.handlers._shared import admits_cloud_deployment

    # Gate 1 — platform: advertising a destination whose every mutating route
    # refuses is worse than not advertising it. In a worker thread with the
    # registry read below: the admission path can initialize the SEL audit log,
    # which is blocking file IO on a fresh gateway.
    admits_cloud = await asyncio.to_thread(admits_cloud_deployment, "aws")

    # Gate 2 — operator: governance ceiling ∩ publish.allowed_destinations. Only
    # consulted when gate 1 admits, because the row is dropped either way and this
    # one reads policy + config off disk. A PlatformCompositionError propagates —
    # fail-closed CPP, same as every other publish surface.
    deploy_denied = None
    if admits_cloud:
        deploy_denied = await asyncio.to_thread(
            lambda: publish_denied_reason(request, DEPLOY_WEB_PROVIDER_ID)
        )

    if not admits_cloud or deploy_denied:
        reason = deploy_denied or "the platform withholds cloud deployment"
        logger.info(
            "publish provider %r omitted from the registry: %s",
            DEPLOY_WEB_PROVIDER_ID,
            reason,
        )
        for squatter in [p for p in providers if p.get("id") == DEPLOY_WEB_PROVIDER_ID]:
            logger.warning(
                "app %r declares the closed publish destination id %r — dropping its "
                "row too, or the operator's shutdown would be undone by an install",
                squatter.get("app", "?"),
                DEPLOY_WEB_PROVIDER_ID,
            )
        return web.json_response(
            {
                "providers": [p for p in providers if p.get("id") != DEPLOY_WEB_PROVIDER_ID],
            }
        )
    try:
        from kiro_crew.deploy import profiles as _deploy_profiles

        # Align with deploy/handlers.py: registry reads go through to_thread.
        reg = await asyncio.to_thread(_deploy_profiles.load_registry)
        configured = bool(reg["profiles"])
    except Exception:
        configured = False
    providers.append(
        {
            "id": DEPLOY_WEB_PROVIDER_ID,
            "label": "Publish to public web (your AWS)",
            "icon": "Globe",
            "endpoint": "/api/deploy/deploy",
            "kinds": ["widget", "html", "markdown"],
            "setupRoute": "/artifacts/deploy",
            "app": "",
            "origin": "core",
            "configured": configured,
        }
    )
    return web.json_response({"providers": providers})


async def handle_get_app(request: web.Request) -> web.Response:
    """GET /api/apps/{name} — get single app details."""
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        # Compat: migrated deploy-web requests hit this generic handler before
        # the deploy module's /api/apps/deploy-web/{tail} redirect (aiohttp
        # matches in registration order). Redirect to the canonical endpoint.
        if name == "deploy-web":
            raise web.HTTPTemporaryRedirect(location="/api/deploy/list")
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)
    return web.json_response(await asyncio.to_thread(_stamp_installed_trust_repository, info))


async def handle_get_manifest(request: web.Request) -> web.Response:
    """GET /api/apps/{name}/manifest — get app manifest."""
    name = request.match_info["name"]
    manifest = get_app_manifest(name)
    if not manifest:
        # Compat: migrated deploy-web — redirect to canonical endpoint.
        if name == "deploy-web":
            raise web.HTTPTemporaryRedirect(location="/api/deploy/config")
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)
    return web.json_response(manifest.to_dict())


async def _start_backend_after_install(name: str) -> None:
    """Spawn an app's backend after a fresh install/register, if it has one.

    ``start_app_backend`` is a no-op for apps that declare no backend and is
    idempotent for already-running ones, so this is safe to call unconditionally.
    It blocks on a health-check poll, so run it off the event loop. Failures are
    logged but never abort the install — the backend also gets a retry on the
    next gateway boot via ``start_enabled_app_backends``.
    """
    try:
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), start_app_backend, name
        )
    except Exception:
        logger.warning("Backend auto-start after install failed for app %s", name, exc_info=True)


async def _register_app_off_loop(name: str) -> RegistrationResult:
    """Run ``register_app`` on the subprocess executor, off the event loop.

    ``register_app`` / ``deregister_app`` do real filesystem work under
    ``KIROCREW_HOME`` — manifest reads, skill-dir symlink walks, agent JSON
    writes, and ``mcp.json`` read + atomic write.  On a stalled filesystem
    (e.g. a dead network mount) those calls block in the kernel; run on the
    loop they freeze every task including the liveness heartbeat until the
    stall watchdog kills the gateway.  Same offload pattern as ``install_app``
    and ``start_app_backend`` on these handlers.
    """
    return await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), register_app, name
    )


async def _deregister_app_off_loop(name: str) -> RegistrationResult:
    """Run ``deregister_app`` off the event loop (see ``_register_app_off_loop``)."""
    return await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), deregister_app, name
    )


async def handle_install_app(request: web.Request) -> web.Response:
    """POST /api/apps/install — install an app from a local path."""
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

        # Auto-register resources
        reg = await _register_app_off_loop(result.name)
        # Spawn the backend now so the app is reachable without a gateway reboot
        # (see _start_backend_after_install). No-op for backend-less apps.
        await _start_backend_after_install(result.name)
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


async def _refuse_while_startup_hook_runs(name: str, *, action: str) -> web.Response | None:
    """Refuse destructive lifecycle work while retained app code is still live."""
    stopped = await stop_retained_startup_hooks(name, bounded=True)
    if stopped:
        return None

    detail = "detached startup hook is still running or cleanup could not be verified"
    sel().log_api_access(
        caller="dashboard",
        operation=f"app_{action}",
        outcome="denied",
        resources=name,
        error=detail,
    )
    return web.json_response(
        {
            "error": (
                f"cannot {action} {name!r} while its timed-out startup hook "
                "is still running; retry after it exits"
            ),
            "code": "startup_hook_still_running",
            "retryable": True,
            "app": name,
        },
        status=409,
    )


async def handle_update_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/update — update an installed app from its source path."""
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
    # Attempt install first, only deregister old resources on success
    # to avoid leaving the app in a broken state on failure.
    if is_registry_source(source):
        registry_name = registry_name_from_source(source)
        async with app_lifecycle_lock(name):
            reg_install = await install_from_registry(registry_name)
            if not reg_install.get("ok"):
                sel().log_api_access(
                    caller="dashboard",
                    operation="app_update",
                    outcome="failed",
                    resources=name,
                    error=reg_install.get("error", ""),
                )
                return web.json_response(reg_install, status=400)
            # Install succeeded — now safe to swap resources. Stop the backend BEFORE
            # deregistering, matching uninstall and the disable rollback: stopping pops
            # the tracking record, which is what stops the health watch from
            # re-registering the OLD manifest's MCP servers in the window between the
            # two (see app-kit-platform §17). Deregistering first leaves that window
            # open, and the entries the update removed would survive it.
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), stop_app_backend, name
            )
            await _deregister_app_off_loop(name)
            if info.get("enabled"):
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

    # Per-app lifecycle lock: the deregister → stop → copy → re-register
    # sequence must not interleave with another update/install/uninstall of
    # the same app — update_app moves user data through a shared
    # ``.{name}-data-tmp`` path, so an interleaving can destroy it.
    # (The registry branch above holds the same lock around install_from_registry.)
    async with app_lifecycle_lock(name):
        startup_refusal = await _refuse_while_startup_hook_runs(name, action="update")
        if startup_refusal is not None:
            return startup_refusal

        # Stop the backend, then deregister old resources — same order as uninstall and
        # the disable rollback. Stopping pops the tracking record, so the health watch
        # can no longer re-register the OLD manifest's MCP servers after the scrub
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
            # Re-register old resources on failure
            await _register_app_off_loop(name)
            if info.get("enabled"):
                await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), start_app_backend, name
                )
            sel().log_api_access(
                caller="dashboard",
                operation="app_update",
                outcome="failed",
                resources=name,
                error=up_result.error,
            )
            return web.json_response(up_result.to_dict(), status=400)

        # Re-register with new manifest if app was enabled
        up_reg = None
        if info.get("enabled"):
            up_reg = await _register_app_off_loop(name)
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


async def handle_register_external(request: web.Request) -> web.Response:
    """POST /api/apps/register — register a self-managed app.

    Self-managed apps handle their own agent/skill/MCP registration.
    KiroCrew only tracks metadata so the dashboard can display them.
    Idempotent — calling again with a newer version updates the entry.

    Body: { name, version, displayName, source?, manifest? }
    """
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


_CRON_CLEANUP_ATTEMPTS = 3
_CRON_CLEANUP_BACKOFF_SECS = 0.5


async def _deregister_crons_with_retry(name: str, cron_service: Any) -> int:
    """Remove an app's cron jobs, retrying a contended store before giving up.

    ``deregister_app_crons_from_service`` already spins on the store lock for a
    bounded window and raises :class:`CronStoreBusy` if it never wins. On the
    uninstall path that exception ABORTS the uninstall (a 409), so a single
    unlucky collision with a concurrent mutator would surface to the user as a
    failed uninstall. Retry the whole atomic removal a few times with a short
    backoff first: contention is transient, and each attempt is all-or-nothing,
    so a retry can never partially remove jobs. Re-raises ``CronStoreBusy`` if
    every attempt loses.
    """
    for attempt in range(1, _CRON_CLEANUP_ATTEMPTS + 1):
        try:
            return await deregister_app_crons_from_service(name, cron_service)
        except CronStoreBusy:
            if attempt == _CRON_CLEANUP_ATTEMPTS:
                raise
            logger.info(
                "Cron cleanup for %s: store busy (attempt %d/%d), retrying",
                name,
                attempt,
                _CRON_CLEANUP_ATTEMPTS,
            )
            await asyncio.sleep(_CRON_CLEANUP_BACKOFF_SECS)
    raise AssertionError("unreachable")  # pragma: no cover


async def handle_uninstall_preview(request: web.Request) -> web.Response:
    """GET /api/apps/{name}/uninstall/preview — preview uninstall impact.

    Returns resource list and dependency classification (removable/shared/userInstalled).
    """
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

    manifest = info.get("manifest", {})
    deps_data = manifest.get("dependencies", {})

    # Collect declared dependency keys
    declared_deps = declared_capability_keys(deps_data)

    # Classify dependencies
    dep_classification = classify_for_uninstall(name, declared_deps)

    return web.json_response(
        {
            "app": name,
            "lifecycle": lifecycle,
            "resources": {
                "agents": manifest.get("agents", []),
                "skills": manifest.get("skills", []),
                "crons": [c.get("name", "") for c in manifest.get("crons", [])],
            },
            "dependencies": dep_classification,
        }
    )


async def handle_uninstall_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/uninstall — uninstall an app.

    1. Check lifecycle field (locked → 400)
    2. Cron cleanup precondition (gateway-managed; abort with retryable 409 if
       the cron store stays busy — runs FIRST, before anything destructive)
    3. Run onUninstall script (if declared)
    4. Stop backend + deregister resources (gateway-managed only)
    5. Clean removable dependencies (unless keep_dependencies=true)
    6. Remove app files (preserve data/ unless purge_data=true)

    Steps 2–6 run inside the per-app lifecycle lock so the whole teardown is
    atomic and the cron precondition can abort before any irreversible action.
    """
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

    resources = info.get("resources", "gateway")
    manifest = info.get("manifest", {})
    uninstall_log: list[str] = []

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
    async with app_lifecycle_lock(name):
        # A retained startup hook still owns the old app's AppContext. Bound the
        # wait and refuse the uninstall if it remains live; deleting files or
        # withdrawing trust first would falsely report that old code is gone.
        startup_refusal = await _refuse_while_startup_hook_runs(name, action="uninstall")
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
                operation="app_uninstall",
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
        if resources == "gateway":
            # Clean up app-declared cron jobs from the scheduler before the
            # per-app cron manifest is removed by deregister_app(). Mirrors the
            # cleanup that on_app_disable performs on the disable path.
            state = request.app.get("state")
            cron_service = getattr(state, "crons", None) if state else None
            if cron_service is not None:
                try:
                    # deregister_app_crons_from_service is async: it awaits the
                    # CronSDK mutation API (per-job store-lock spin offloaded to
                    # a worker thread), so the loop is never parked and timer
                    # arming is owned by CronService (no caller-side drain).
                    # It removes all owned jobs in ONE atomic transaction, so on
                    # CronStoreBusy nothing was removed — the abort below leaves
                    # no partially-cleaned state.
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
                        operation="app_uninstall",
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
                    # dashboard/handlers/cron.py: an unreadable file does not heal
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
                        operation="app_uninstall",
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
                    logger.warning("Cron cleanup failed for %s on uninstall: %s", name, exc)
                    sel().log_api_access(
                        caller="dashboard",
                        operation="app_crons_deregister",
                        outcome="failed",
                        resources=name,
                        error=str(exc),
                    )

        # Step 2: Run onUninstall script. Reached only once cron cleanup has
        # succeeded (or there were no crons / no cron service), so a
        # non-idempotent teardown never runs on an uninstall that will be
        # retried.
        on_uninstall = (manifest.get("setup") or {}).get("onUninstall", "")
        if on_uninstall:
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
                from kiro_crew.security import redact_credentials, redact_exfiltration_urls

                cleaned, _ = redact_exfiltration_urls(script_output["output"])
                cleaned, _ = redact_credentials(cleaned)
                uninstall_log.append(cleaned)
            if script_output.get("failed"):
                uninstall_log.append("onUninstall script failed (exit code non-zero)")

        # Step 3: Stop backend + deregister resources (gateway-managed only)
        if resources == "gateway":
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), stop_app_backend, name
            )
            await _deregister_app_off_loop(name)

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
            classification = classify_and_clean_for_uninstall(
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
                subprocess_executor(), lambda: uninstall_app(name, keep_data=keep_data)
            )
    if not result.ok:
        sel().log_api_access(
            caller="dashboard",
            operation="app_uninstall",
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

    # Step 6: Clean up workspace (each registry app has its own workspace)
    if is_registry_source(info.get("source", "")):
        app_reg_name = registry_name_from_source(info.get("source", ""))
        if app_reg_name:
            from kiro_crew.apps.registry import app_source_dir

            ws_dir = app_source_dir(app_reg_name)
            if ws_dir.is_dir():
                shutil.rmtree(ws_dir, ignore_errors=True)
                uninstall_log.append(f"Removed workspace for {app_reg_name}")

    sel().log_api_access(
        caller="dashboard", operation="app_uninstall", outcome="completed", resources=name
    )
    resp = result.to_dict()
    if uninstall_log:
        resp["uninstall_log"] = "\n".join(uninstall_log)
    if cleaned_deps:
        resp["cleaned_dependencies"] = cleaned_deps
    return web.json_response(resp)


def _client_install_manifest(manifest: dict[str, Any]) -> PlatformConfig | None:
    """The app's :class:`PlatformConfig` when it is a CLIENT-install app, else ``None``.

    A ``client`` app's real payload is a desktop application the user installs on
    their OWN machine; what the gateway holds is metadata plus a dashboard page.
    So its lifecycle scripts address something that legitimately may not be on
    this host, which is what makes them advisory rather than a health check —
    see :func:`handle_enable_app`.

    Never raises. ``PlatformConfig.from_dict`` iterates ``os`` and ``arch``
    directly, so a hand-edited ``app.json`` carrying ``"os": null`` raises
    ``TypeError`` there; this is the first place the enable path parses
    ``platform`` at all, so an unguarded call would turn a malformed manifest into
    a 500 on enable. A manifest this app cannot read is treated as "not a client
    app", which keeps the strict rollback behavior rather than silently widening
    the advisory path.
    """
    platform_raw = manifest.get("platform")
    if not isinstance(platform_raw, dict):
        return None
    try:
        platform_cfg = PlatformConfig.from_dict(platform_raw)
    except (TypeError, ValueError) as exc:
        logger.warning("Ignoring an unreadable platform block on app enable: %s", exc)
        return None
    return platform_cfg if platform_cfg.installMode == "client" else None


async def handle_enable_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/enable — enable an app.

    Behavior depends on ``resources`` field:
    - ``gateway``: register_app() + start_backend() + run onEnable
    - ``app``: run onEnable only

    If onEnable fails, the enable is rolled back (app stays disabled) — EXCEPT
    for a ``platform.installMode: "client"`` app, whose script is advisory. For a
    server-install app the script is part of bringing the app up, so a failure
    means the app would be enabled but broken and rolling back is right. A client
    app's script instead launches a desktop application distributed SEPARATELY
    (``crew-companion``'s ``open "$HOME/Applications/Crew Companion.app"``), so on
    a host where the user has not installed that application yet the script can
    only fail — and rolling back made the dashboard half of the app impossible to
    enable at all, reporting "onEnable script failed — app remains disabled" with
    no way forward. Enabling is also the step that reveals the app's own page,
    which is where a user learns how to get the desktop half.

    The script is skipped outright when the gateway's OS is not in the app's
    ``platform.os``: nothing else on the enable path consults that field, so a
    macOS-only app enabled on Linux/Windows would otherwise run a command that
    cannot succeed there.
    """
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    resources = info.get("resources", "gateway")
    manifest = info.get("manifest", {})
    on_enable = (manifest.get("setup") or {}).get("onEnable", "")
    enable_timeout = int((manifest.get("setup") or {}).get("onEnableTimeout", 30))

    # Per-app lifecycle lock: enable mutates metadata, registers resources,
    # and starts the backend — must not interleave with a concurrent
    # install/update/uninstall of the same app (e.g. enabling while an
    # off-loop uninstall is deleting the app directory).
    async with app_lifecycle_lock(name):
        result = enable_app(name)
        if not result.ok:
            sel().log_api_access(
                caller="dashboard",
                operation="app_enable",
                outcome="failed",
                resources=name,
                error=result.error,
            )
            return web.json_response(result.to_dict(), status=400)

        resp: dict[str, Any] = result.to_dict()

        # Register resources if gateway-managed
        if resources == "gateway":
            reg = await _register_app_off_loop(name)
            backend = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), start_app_backend, name
            )
            # MCP re-registration is HEALTH-GATED and happens inside start_app_backend,
            # not here. register_app ran before the backend was up, so an HTTP MCP server
            # with backend.port:"auto" still carries the manifest's illustrative port; the
            # health-check loop rewrites it to the real allocated port once /health passes
            # (and scrubs it if the backend never becomes healthy — the dead-url shape
            # that broke kiro-cli). An ADOPTED instance runs no health loop, so the
            # adoption path registers it through the same serialized transition before
            # arming its watch. Re-registering here instead would race that watch: the
            # call is queued after this handler returns, so a demotion could scrub in
            # between and the queued write would restore the dead url.
            resp["registration"] = reg.to_dict()
            if backend:
                resp["backend"] = backend.to_dict()

        # Resolve declared dependencies (if any)
        deps_data = manifest.get("dependencies")
        if deps_data and isinstance(deps_data, dict):
            deps = Dependencies.from_dict(deps_data)
            dep_result = await _resolve_deps(name, deps)
            sel().log_api_access(
                caller="dashboard",
                operation="app_enable_resolve_deps",
                outcome="partial_failure" if dep_result.failed else "success",
                resources=name,
                error=str(dep_result.failed) if dep_result.failed else "",
            )
            dep_info: dict[str, Any] = {}
            if dep_result.installed:
                dep_info["installed"] = dep_result.installed
            if dep_result.failed:
                dep_info["failed"] = dep_result.failed
            if dep_result.missing:
                dep_info["missing"] = dep_result.missing
            if dep_info:
                resp["dependencies"] = dep_info

        # Run onEnable script. A client-install app's script is advisory: it
        # addresses a separately-distributed desktop application, so it neither
        # gates nor rolls back the enable (see this handler's docstring).
        client_platform = _client_install_manifest(manifest)
        if (
            on_enable
            and client_platform is not None
            and not client_platform.supports_platform(sys.platform)
        ):
            resp["onEnable"] = {
                "output": "",
                "failed": False,
                "skipped": "unsupported_platform",
            }
        elif on_enable:
            script_output = await _run_lifecycle_script(
                name, on_enable, timeout=enable_timeout, action="on_enable"
            )
            if script_output.get("failed") and client_platform is None:
                # Rollback: disable the app again
                if resources == "gateway":
                    await asyncio.get_running_loop().run_in_executor(
                        subprocess_executor(), stop_app_backend, name
                    )
                    await _deregister_app_off_loop(name)
                disable_app(name)
                sel().log_api_access(
                    caller="dashboard",
                    operation="app_enable",
                    outcome="failed",
                    resources=name,
                    error="onEnable script failed",
                )
                from kiro_crew.security import redact_credentials

                cleaned, _ = redact_credentials(script_output.get("output", ""))
                return web.json_response(
                    {
                        "ok": False,
                        "name": name,
                        "error": "onEnable script failed — app remains disabled",
                        "script_output": cleaned,
                        "code": "on_enable_failed",
                    },
                    status=400,
                )
            resp["onEnable"] = {
                "output": "",
                "failed": False,
            }
            if script_output.get("output"):
                from kiro_crew.security import redact_credentials

                cleaned, _ = redact_credentials(script_output.get("output", ""))
                resp["onEnable"]["output"] = cleaned
            resp["onEnable"]["failed"] = script_output.get("failed", False)

        # Invoke Python lifecycle hooks (routes + on_startup) — runs AFTER shell scripts
        try:
            state = request.app.get("state")
            hooks_result = await on_app_enable(
                name,
                info,
                cron_service=getattr(state, "crons", None),
                # state exposes broadcast_ws, not broadcast: the old
                # getattr(state, "broadcast", None) always resolved to None, so an
                # app enabled from the dashboard got NO event bus at all.
                broadcast_fn=(
                    build_broadcast_fn(state.broadcast_ws) if state is not None else None
                ),
                spawn_impl=(
                    build_spawn_impl(getattr(state, "subagents", None))
                    if state is not None
                    else None
                ),
            )
            if hooks_result:
                # Redact any sensitive content in health_status issues
                if "health_status" in hooks_result:
                    hs = hooks_result["health_status"]
                    if "issues" in hs:
                        hs["issues"] = [_redact_warning(i) for i in hs["issues"]]
                resp["hooks"] = hooks_result
        except Exception as exc:
            logger.warning("Hook execution failed for %s: %s", name, exc)
            resp.setdefault("warnings", []).append(_redact_warning(f"hooks failed: {exc}"))

        # Sync config.json and start live service for builtin apps
        origin = info.get("origin", "")
        if origin == "builtin" and name in _BUILTIN_SERVICE_APPS:
            try:
                # ``run_config_write``, not a bare ``to_thread``: the helper is a
                # read-modify-write of the SAME ``config.json`` the legacy dashboard
                # writers (agents endpoint, updates.py, security.py, messaging.py,
                # mcp.py, core.py STT) mutate while holding ONLY the loop-side
                # ``_get_config_lock``. ``update_config_locked`` inside the helper
                # takes only the sidecar advisory flock, which excludes nothing that
                # family respects -- so a settings PUT landing mid-write commits from
                # a snapshot taken before it and silently reverts this app's enabled
                # flag, or loses the user's settings. ``run_config_write`` is the one
                # entry point that holds BOTH generations, and it still hands the
                # blocking work (the flock wait, and on Windows the owner-only
                # lockdown's possible SMB round-trip) to a worker, so the loop never
                # stalls -- the property the previous ``to_thread`` was there for.
                #
                # Lock order is app_lifecycle_lock -> config lock, matching
                # ``handle_app_uninstall`` above, which already nests them that way
                # for the same reason. Verified across the tree: 14 functions take
                # ``app_lifecycle_lock`` and none of them is reachable from inside a
                # config-lock block, so the reverse order does not exist.
                #
                # Call-time import for the layering reason documented at the
                # ``_get_config_lock`` import above: ``apps`` sits below
                # ``dashboard`` and must not depend on it at load time.
                from kiro_crew.dashboard.chat_utils import run_config_write

                await run_config_write(_sync_builtin_config, name, enabled=True)
            except OSError as exc:
                logger.warning("Failed to sync config.json for %s: %s", name, exc)
                resp.setdefault("warnings", []).append(
                    _redact_warning(f"config sync failed: {exc}")
                )
            else:
                svc_warn = await _notify_builtin_service(request, name)
                if svc_warn:
                    resp.setdefault("warnings", []).append(_redact_warning(svc_warn))

        sel().log_api_access(
            caller="dashboard", operation="app_enable", outcome="completed", resources=name
        )
        return web.json_response(resp)


async def handle_disable_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/disable — disable an app.

    Behavior depends on ``resources`` field:
    - ``gateway``: run onDisable + stop_backend() + deregister_app()
    - ``app``: run onDisable only
    If onDisable fails, disable proceeds anyway (with warnings).
    """
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    warnings: list[str] = []

    # Per-app lifecycle lock: disable stops the backend and deregisters
    # resources — must not interleave with a concurrent install/update/
    # uninstall/enable of the same app.
    async with app_lifecycle_lock(name):
        startup_refusal = await _refuse_while_startup_hook_runs(name, action="disable")
        if startup_refusal is not None:
            return startup_refusal

        # `onDisable` is NOT run here: it runs inside `teardown_app_runtime`
        # below, so that revoking an app's execution grant runs it too. Keeping it
        # handler-only would make revoke weaker than disable — see the ordering
        # rationale in apps/teardown.py. Running it here as well would run the
        # app's script twice per disable.
        #
        # Invoke Python lifecycle hooks, stop the backend PROCESS, and deregister
        # resources through the ONE shared teardown that revoking an app's
        # third-party execution grant also calls — see apps/teardown.py. Keeping a
        # second copy here is how the revoke path came to miss steps.
        teardown = await teardown_app_runtime(name, info)
        # This handler's documented contract is that disable proceeds even when a
        # step fails, so both lists become user-visible warnings rather than an
        # abort. (Trust revocation treats `failures` as fatal instead — it must not
        # claim an app was stopped when its crons may still fire.)
        for note in (*teardown.warnings, *teardown.failures):
            warnings.append(_redact_warning(note))

        result = disable_app(name)
        if not result.ok:
            sel().log_api_access(
                caller="dashboard",
                operation="app_disable",
                outcome="failed",
                resources=name,
                error=result.error,
            )
            return web.json_response(result.to_dict(), status=400)
        _unregister_notification_channels(request, name)

        # Run builtin on_disable hook if available. `name` is the manifest name
        # (hyphenated, e.g. `code-review-sage`) while `BUILTIN_NAMES` and the
        # package dirs use underscores, so the membership test and the import both
        # need the normalized form — without it this hook is unreachable for every
        # multi-word builtin, which is all of them but `meetings`, `mochi` and
        # `papyrus`.
        module_name = name.replace("-", "_")
        if module_name in BUILTIN_NAMES:
            try:
                mod = importlib.import_module(f"kiro_crew.apps.builtins.{module_name}")
                if hasattr(mod, "on_disable"):
                    mod.on_disable(request.app)
            except Exception as exc:
                logger.warning("on_disable hook for %s failed: %s", name, exc)
                warnings.append(_redact_warning(f"on_disable hook failed: {exc}"))

        # Sync config.json and stop live service for builtin apps
        origin = info.get("origin", "")
        if origin == "builtin" and name in _BUILTIN_SERVICE_APPS:
            try:
                # ``run_config_write``, not a bare ``to_thread``: the helper is a
                # read-modify-write of the SAME ``config.json`` the legacy dashboard
                # writers (agents endpoint, updates.py, security.py, messaging.py,
                # mcp.py, core.py STT) mutate while holding ONLY the loop-side
                # ``_get_config_lock``. ``update_config_locked`` inside the helper
                # takes only the sidecar advisory flock, which excludes nothing that
                # family respects -- so a settings PUT landing mid-write commits from
                # a snapshot taken before it and silently reverts this app's enabled
                # flag, or loses the user's settings. ``run_config_write`` is the one
                # entry point that holds BOTH generations, and it still hands the
                # blocking work (the flock wait, and on Windows the owner-only
                # lockdown's possible SMB round-trip) to a worker, so the loop never
                # stalls -- the property the previous ``to_thread`` was there for.
                #
                # Lock order is app_lifecycle_lock -> config lock, matching
                # ``handle_app_uninstall`` above, which already nests them that way
                # for the same reason. Verified across the tree: 14 functions take
                # ``app_lifecycle_lock`` and none of them is reachable from inside a
                # config-lock block, so the reverse order does not exist.
                #
                # Call-time import for the layering reason documented at the
                # ``_get_config_lock`` import above: ``apps`` sits below
                # ``dashboard`` and must not depend on it at load time.
                from kiro_crew.dashboard.chat_utils import run_config_write

                await run_config_write(_sync_builtin_config, name, enabled=False)
            except OSError as exc:
                logger.warning("Failed to sync config.json for %s: %s", name, exc)
                warnings.append(_redact_warning(f"config sync failed: {exc}"))
            else:
                svc_warn = await _notify_builtin_service(request, name)
                if svc_warn:
                    warnings.append(_redact_warning(svc_warn))

        sel().log_api_access(
            caller="dashboard", operation="app_disable", outcome="completed", resources=name
        )
        resp = result.to_dict()
        if warnings:
            resp["warnings"] = warnings
        return web.json_response(resp)


async def handle_open_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/open — launch an app using its openCommand.

    For apps that run outside the dashboard (e.g. Electron apps),
    the manifest can declare an ``openCommand`` shell string that
    launches the app.  This endpoint executes it in the background.

    On cloud/remote environments (no display), returns the command
    for the user to run locally instead of executing it.
    """
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        return web.json_response({"error": f"app {name!r} not found"}, status=404)

    if not info.get("enabled", False):
        error = f"app {name!r} is disabled"
        sel().log_api_access(
            caller="dashboard",
            operation="app_open",
            outcome="denied",
            resources=name,
            error=error,
        )
        return web.json_response({"error": error, "code": "app_disabled"}, status=409)

    manifest = info.get("manifest", {})
    open_cmd = manifest.get("openCommand", "")
    if not open_cmd:
        return web.json_response({"error": "app has no openCommand"}, status=400)

    denied = app_execution_denied(
        name,
        action="open_command",
        app_root=apps_dir() / name,
        caller="dashboard",
    )
    if denied:
        return web.json_response({"error": denied, "code": "app_execution_denied"}, status=403)

    # Detect cloud/remote — no DISPLAY and not macOS desktop
    import platform

    is_local = (
        platform.system() == "Darwin"
        or os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
    )

    if not is_local:
        return web.json_response(
            {
                "ok": False,
                "name": name,
                "remote": True,
                "command": open_cmd,
                "message": f"Kiro Crew is running remotely. Run this on your local machine: {open_cmd}",
            }
        )

    try:
        base_cmd = ["/bin/sh", "-c", open_cmd]
        sandboxed_cmd, _cleanup = await wrap_argv_async(
            base_cmd, mode="standard", _prepare=wrap_argv
        )
        sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
        proc = await create_subprocess_limited(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Don't wait — launch is fire-and-forget
        sel().log_api_access(
            caller="dashboard",
            operation="app_open",
            outcome="launched",
            resources=f"{name} pid={proc.pid}",
        )
        return web.json_response({"ok": True, "name": name, "pid": proc.pid})
    except Exception as exc:
        sel().log_api_access(
            caller="dashboard",
            operation="app_open",
            outcome="failed",
            resources=name,
            error=str(exc),
        )
        return web.json_response({"error": f"failed to launch: {exc}"}, status=500)


# ---------------------------------------------------------------------------
# Registry (browse & install from curated list)
# ---------------------------------------------------------------------------


async def handle_registry(request: web.Request) -> web.Response:
    """GET /api/apps/registry — list all apps available for installation."""
    # The published catalog is the storefront's source of truth when it is
    # reachable: its rows replace the seed + per-app manifest fetch. Offline the
    # catalog is empty and the seed listing answers instead.
    apps = await list_catalog_apps()
    if not apps:
        apps = await list_registry()
    # Published rail order and layout ride along on the response the store already
    # makes, rather than endpoints the page would have to wait on separately. They
    # are two documents with two caches, loaded CONCURRENTLY: each cold-cache load
    # does network I/O, and paying two fetch timeouts in series would delay the
    # whole store response. Both are presentation, so a failure degrades to the
    # client's own defaults per document and must never 500 the store -- the same
    # contract the catalog annotation keeps.
    order_result, sections_result = await asyncio.gather(
        asyncio.to_thread(load_category_order),
        asyncio.to_thread(load_sections),
        return_exceptions=True,
    )
    if isinstance(order_result, BaseException):
        logger.warning("ignoring the published category order", exc_info=order_result)
        category_order: list = []
    else:
        category_order = order_result
    if isinstance(sections_result, BaseException):
        logger.warning("ignoring the editorial sections", exc_info=sections_result)
        sections: list = []
    else:
        sections = sections_result
    return web.json_response(
        {
            "apps": apps,
            "serverPlatform": get_server_platform(),
            "categoryOrder": category_order,
            "editorialSections": sections,
        }
    )


async def handle_registry_refresh(request: web.Request) -> web.Response:
    """POST /api/app-store/refresh — drop the published-document caches.

    Drops the on-disk caches of all three published documents (catalog,
    category order, editorial), so the NEXT ``GET /api/apps/registry`` is
    rebuilt from fresh fetches. This exists because the caches degrade
    SILENTLY: a failed fetch overwrites the catalog cache with a failure
    sentinel and the store quietly falls back to the seed listing, and without
    an explicit refresh the user's only remedy is waiting out ``CACHE_TTL``.

    Deliberately OUTSIDE ``/api/apps/``: token_auth's ``_app_owns_path``
    grants an app token implicit ownership of everything under
    ``/api/apps/<its-own-name>/``, so a path like
    ``/api/apps/registry/refresh`` would hand any app that names itself
    ``registry`` the power to purge the shared catalog caches without
    declaring the permission. Under ``/api/app-store/`` no app name can
    collide, so an app token reaches this endpoint only through an explicit
    ``permissions.api`` grant.

    Two more deliberate shapes:

    - A POST, not a ``?refresh=1`` on the GET: deleting caches and triggering
      outbound fetches is a state change, and a state-changing GET is reachable
      by cross-site top-level navigation with a valid SameSite=Lax cookie --
      exactly the request the CSRF middleware never sees.
    - It only DROPS caches -- it never fetches. The follow-up GET pays the
      fetch on the exact same code path as a cold start, so refresh cannot
      behave differently from the load it is trying to repair.
    """
    # Off the event loop like every other disk touch on these routes; three
    # unlinks gathered because none depends on another.
    await asyncio.gather(
        asyncio.to_thread(official_catalog.forget_cache),
        asyncio.to_thread(forget_category_order_cache),
        asyncio.to_thread(forget_editorial_cache),
    )
    return web.json_response({"ok": True})


async def handle_registry_install(request: web.Request) -> web.Response:
    """POST /api/apps/registry/install — install an app from the registry.

    Clones the repo, runs the install script, and registers the app.
    This can take a while so the response includes a log of what happened.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    name = body.get("name", "")
    if not name:
        return web.json_response({"error": "app name required"}, status=400)

    # One lock for the complete transaction: install_from_registry is
    # lock-free internally (asyncio.Lock is not reentrant), so this is the
    # single acquisition covering clone/build → copy → register → backend.
    async with app_lifecycle_lock(name):
        result = await install_from_registry(name)

        # Redact install log and error before returning to client — build output
        # may contain internal hostnames, package URLs, or credential fragments.
        if result.get("log"):
            from kiro_crew.security import redact_credentials, redact_exfiltration_urls

            cleaned_log, _ = redact_exfiltration_urls(result["log"])
            cleaned_log, _ = redact_credentials(cleaned_log)
            result["log"] = cleaned_log
        if result.get("error"):
            from kiro_crew.security import redact_credentials, redact_exfiltration_urls

            cleaned_err, _ = redact_exfiltration_urls(result["error"])
            cleaned_err, _ = redact_credentials(cleaned_err)
            result["error"] = cleaned_err

        if result.get("needsClientInstall"):
            return web.json_response(result, status=200)
        if not result.get("ok"):
            sel().log_api_access(
                caller="dashboard",
                operation="app_registry_install",
                outcome="failed",
                resources=name,
                error=result.get("error", ""),
            )
            return web.json_response(result, status=400)

        # Auto-register resources
        reg = await _register_app_off_loop(result["name"])
        # Spawn the backend now so apps with a server are reachable immediately —
        # without this the backend only starts on the next gateway reboot (via
        # start_enabled_app_backends), leaving the app's UI with "no reachable
        # backend" until then. No-op for apps that declare no backend. Run in a
        # thread because start_app_backend blocks on a health-check poll.
        await _start_backend_after_install(result["name"])
    result["registration"] = reg.to_dict()
    sel().log_api_access(
        caller="dashboard", operation="app_registry_install", outcome="completed", resources=name
    )
    return web.json_response(result, status=201)


async def handle_registry_install_stream(request: web.Request) -> web.StreamResponse:
    """POST /api/apps/registry/install-stream — SSE streaming install.

    Same logic as ``handle_registry_install`` but streams log lines as
    Server-Sent Events in real-time, giving the user full transparency
    into what's happening during the (often slow) install process.

    Event types:
      ``log``   — a single log line (data: string)
      ``done``  — install finished (data: JSON with ok, name, error, etc.)

    The original ``/api/apps/registry/install`` endpoint is unchanged —
    CLI and other callers are not affected.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    name = body.get("name", "")
    if not name:
        return web.json_response({"error": "app name required"}, status=400)

    # Set up SSE response
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)

    # Create a queue-backed log collector so install_from_registry streams
    # each log line as it's appended — zero changes to the install logic.
    from kiro_crew.apps.registry import StreamingLogLines

    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=500)
    streaming_log = StreamingLogLines(queue)

    async def _send_sse(event: str, data: str) -> None:
        """Write a single SSE frame.

        Multi-line data is split into multiple ``data:`` lines per the
        SSE spec (each line prefixed with ``data: ``).  This prevents
        newline injection from breaking the event stream framing.
        """
        try:
            # SSE spec: multi-line data uses one "data:" prefix per line
            lines = data.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            payload = f"event: {event}\n"
            for line in lines:
                payload += f"data: {line}\n"
            payload += "\n"
            await resp.write(payload.encode("utf-8"))
        except (ConnectionResetError, ConnectionAbortedError):
            pass

    async def _drain_queue() -> None:
        """Forward queued log lines to the SSE stream until sentinel."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        while True:
            line = await queue.get()
            if line is None:
                break  # sentinel — install finished
            cleaned, _ = redact_exfiltration_urls(line)
            cleaned, _ = redact_credentials(cleaned)
            await _send_sse("log", cleaned)

    # Run install + drain concurrently. The complete lifecycle transaction —
    # install, resource registration, backend start — runs under one per-app
    # lock (install_from_registry is lock-free internally).
    async def _locked_install() -> dict[str, Any]:
        async with app_lifecycle_lock(name):
            r = await install_from_registry(name, log_lines=streaming_log)
            if r.get("ok") and not r.get("needsClientInstall"):
                reg = await _register_app_off_loop(r["name"])
                # Spawn the backend immediately (see handle_registry_install) so
                # the app is reachable without a gateway reboot. No-op for
                # backend-less apps.
                await _start_backend_after_install(r["name"])
                r["registration"] = reg.to_dict()
            return r

    install_task = asyncio.create_task(_locked_install())
    drain_task = asyncio.create_task(_drain_queue())

    try:
        result = await install_task
    except Exception as exc:
        result = {"ok": False, "name": name, "error": str(exc)}
    finally:
        # Signal the drain loop to stop, then wait for it to flush.
        # Use blocking put — put_nowait raises QueueFull if the queue
        # is at capacity, which would prevent the sentinel from being
        # delivered and hang _drain_queue forever.
        await queue.put(None)
        await drain_task

    # Redact the final log and error fields before sending to client —
    # error may contain internal hostnames, git URLs, or credential
    # fragments from subprocess failures.
    if result.get("log"):
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        cleaned_log, _ = redact_exfiltration_urls(result["log"])
        cleaned_log, _ = redact_credentials(cleaned_log)
        result["log"] = cleaned_log
    if result.get("error"):
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        cleaned_err, _ = redact_exfiltration_urls(result["error"])
        cleaned_err, _ = redact_credentials(cleaned_err)
        result["error"] = cleaned_err

    if result.get("needsClientInstall"):
        await _send_sse("done", json.dumps(result))
        await resp.write_eof()
        return resp

    if not result.get("ok"):
        sel().log_api_access(
            caller="dashboard",
            operation="app_registry_install_stream",
            outcome="failed",
            resources=name,
            error=result.get("error", ""),
        )
        await _send_sse("done", json.dumps(result))
        await resp.write_eof()
        return resp

    # Resource registration + backend start already ran inside the locked
    # transaction above; result carries "registration".
    sel().log_api_access(
        caller="dashboard",
        operation="app_registry_install_stream",
        outcome="completed",
        resources=name,
    )
    await _send_sse("done", json.dumps(result))
    await resp.write_eof()
    return resp


# ---------------------------------------------------------------------------
# Static file serving for app UI bundles
# ---------------------------------------------------------------------------

_ALLOWED_EXTENSIONS = frozenset(
    {
        ".mjs",
        ".js",
        ".css",
        ".json",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".woff",
        ".woff2",
        ".ttf",
        ".map",
    }
)

_CONTENT_TYPES = {
    ".mjs": "application/javascript",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".map": "application/json",
}


#: Store-art fields an installed app's manifest may declare. A path under
#: ``/apps/{name}/art/`` is servable ONLY when it is one of these values
#: verbatim, which is what makes the route need no traversal reasoning of its
#: own: the manifest, not the request, chooses the file.
#:
#: Deliberately NOT a path filter rooted at the install directory. That
#: directory is the app's whole checkout and ``_ALLOWED_EXTENSIONS`` admits
#: ``.json``, so a filter would also serve ``installed.json``, ``app.json`` and
#: every other JSON in the tree — a widening nobody asked for to display an icon.
_ART_MANIFEST_FIELDS = (
    "iconPath",
    "iconPathDark",
    "heroImage",
    "heroImageDark",
    "heroImageDetail",
    "heroImageDetailDark",
)

#: The same, for the fields that hold a LIST of paths.
_ART_MANIFEST_LIST_FIELDS = ("screenshots", "screenshotsDark")

#: Images only — narrower than ``_ALLOWED_EXTENSIONS`` on purpose. Store art is
#: rendered into an ``<img>``, so nothing script-shaped (``.mjs``/``.js``) or
#: data-shaped (``.json``) belongs here. ``.svg`` stays because an SVG loaded as
#: an ``<img>`` source cannot execute script.
#:
#: ONE set for both art paths — this route for an installed app, the blob proxy
#: for a not-installed external-registry row. The parity is load-bearing rather
#: than incidental: the route REPLACES the proxy per surface, so a file the proxy
#: would serve and this refuses (or the reverse) means the same app's art renders
#: or 403s depending only on whether it happens to be installed. Two frozensets
#: spelled separately were identical member-for-member and nothing pinned them,
#: which is a divergence waiting for whoever edits one of them next.
_ART_IMAGE_EXTENSIONS = frozenset({".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"})


def _declared_art_paths(name: str) -> set[str]:
    """The art paths *name*'s own installed manifest declares.

    Blocking (reads the manifest off disk) — callers must offload it.
    """
    manifest = get_app_manifest(name)
    if manifest is None:
        return set()
    extra = getattr(manifest, "extra", None)
    fields: dict[str, Any] = extra if isinstance(extra, dict) else {}
    declared: set[str] = set()
    for key in _ART_MANIFEST_FIELDS:
        value = fields.get(key)
        if isinstance(value, str) and value:
            declared.add(value[2:] if value.startswith("./") else value)
    for key in _ART_MANIFEST_LIST_FIELDS:
        values = fields.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value:
                declared.add(value[2:] if value.startswith("./") else value)
    return declared


#: Ceiling on one art file this route will hold. The bytes are read under a pinned
#: descriptor rather than streamed from a path (see :func:`_read_declared_art`), so
#: without a cap an app could make the gateway buffer an arbitrarily large file by
#: declaring one. Generous against the publishing guide's own limits — a 512px
#: icon, a 16:9 hero — so a real asset never meets it.
_ART_MAX_BYTES = 8 * 1024 * 1024


def _read_declared_art(name: str, file_path: str) -> tuple[bytes, str] | None:
    """The BYTES of *file_path* and a validator for them, or None to refuse.

    Returning bytes rather than a path is the security-relevant part. Validating a
    path and then handing it to ``FileResponse`` opens it a SECOND time, so the app
    that owns this directory can swap a declared name for a symlink between the
    check and that open and have the gateway read the target instead — and the
    gateway is NOT sandboxed, so this would launder a read the app's own code can be
    refused. Checking a path and acting on a re-resolution of it is worse than no
    check, because it reports success.

    One open, validated as a DESCRIPTOR, is the only shape without that window.
    :func:`open_in_pinned_parent` does one ``openat`` per component carrying
    ``O_NOFOLLOW``, so a component swapped for a link after the parent was resolved
    fails instead of being followed, and the final name is refused if it is a link
    at all. Everything after that reads the descriptor, which cannot be re-pointed.

    One thread hop for the whole decision — manifest read, declaration check, open,
    stat and read — because every part is a blocking syscall and the gateway runs on
    one event loop (``no-blocking-call-on-event-loop``).
    """
    if file_path not in _declared_art_paths(name):
        return None
    root = apps_dir() / name
    target = root / file_path
    try:
        # Resolved ONCE, here, because `pin_parent` requires the CALLER to do it:
        # resolving inside would re-follow whatever an ancestor points at by then,
        # which is the mistake that makes a guard look defensible and do nothing.
        resolved_root = Path(os.path.realpath(root))
        resolved_parent = Path(os.path.realpath(target.parent))
        resolved_parent.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        # `RuntimeError` because `Path`/`realpath` resolution raises THAT -- not an
        # OSError -- on a symlink loop, and the app that plants one is the app whose
        # art this serves. All three mean the path is not servable, which is the same
        # answer as undeclared and missing.
        return None

    # `O_NONBLOCK` is not an optimisation, it is what makes the descriptor checks
    # REACHABLE. Opening a FIFO blocks until a writer appears, and this runs inside
    # `asyncio.to_thread` -- so an app declaring a FIFO as its icon path parks a
    # thread-pool worker forever, and enough such requests starve every other
    # blocking call in the gateway. Measured: the open hangs indefinitely without
    # this flag, returns immediately with it, and then `S_ISREG` below refuses the
    # FIFO. On an ordinary file it changes nothing -- the read returns the same bytes.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if supports_pinned_walk():
        try:
            fd = open_in_pinned_parent(
                str(resolved_parent),
                Path(file_path).name,
                flags=flags,
                mode=0o600,
                what="app art file",
            )
        except (PinnedPathRefusal, OSError, ValueError):
            # `ValueError` is NOT redundant with `OSError`: `os.open` raises it --
            # never an OSError -- for a name the OS layer cannot even encode. Two
            # reachable classes, both measured: an embedded NUL raises `ValueError`
            # ("embedded null character in path"), and a lone surrogate raises
            # `UnicodeEncodeError`, which is a ValueError SUBCLASS. Screening NUL at
            # the door would therefore be INCOMPLETE -- the surrogates carry no NUL.
            #
            # Reachable because such a name survives every earlier check: the
            # extension allowlist reads the suffix AFTER the bad byte
            # (`bad\x00.png` -> `.png`), and containment resolves the PARENT, which
            # is clean when the bad byte sits in the final component. So the first
            # thing that touches it is this open, and uncaught it is a 500 on a
            # route whose every other refusal is a clean status.
            return None
    else:
        # Windows: no `O_NOFOLLOW` and no descriptor-relative open, so the pinned
        # walk is unavailable. An unprivileged process there cannot create a FILE
        # symlink at all (that needs elevation, which is why `symlink_or_junction`
        # exists), so the reachable swap is a junction on an ancestor -- refused by
        # the reparse-point probe below. The window is narrowed rather than closed,
        # and it is narrowed against what the platform actually permits.
        try:
            if any(
                is_reparse_point(p)
                for p in (target, *target.parents)
                if p == resolved_root or resolved_root in p.parents or p == target
            ):
                return None
            fd = os.open(target, flags)
        except (OSError, ValueError):
            # Same unencodable-name classes as the pinned branch above: this open
            # takes the whole path rather than a name under a descriptor, so it is
            # reachable the same way and needs the same tuple.
            return None

    try:
        st = os.fstat(fd)
        # `st_nlink != 1` is the third gate, and it is the only one that can see a
        # HARDLINK. An alias shares the target's inode, so every path-based guard is
        # blind to it: `is_symlink()` is False, `realpath` yields the alias's own name
        # (so containment passes), and `O_NOFOLLOW` has no link to refuse. Measured:
        # a declared `assets/icon.webp` hardlinked to a file outside the install
        # directory opens cleanly, reports S_ISREG, sits under the size cap, and its
        # bytes are served with a 200 -- laundering, through an unsandboxed gateway, a
        # read the app's own sandboxed code can be refused.
        #
        # Checked on the DESCRIPTOR, which is what makes it race-free: this fd already
        # refers to the inode being judged. Every other descriptor-validated read in
        # the tree applies the same gate (`hooks.py`, `memory.py`, `spec_builder`,
        # `onboarding_import.py`, `pinned_fs.copy_file_pinned`), so this route was the
        # outlier rather than a new rule.
        #
        # Inline rather than `pinned_fs.refuse_hardlink_alias`, which is the same
        # check behind an exception: that helper CLOSES the fd before raising, and this
        # function closes in a `finally`, so routing through it would double-close --
        # and a reused fd number makes that a worse bug than the one being fixed. The
        # inline spelling is what the sibling sites above use for the same reason:
        # their refusal is a return value, not a raise.
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > _ART_MAX_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(_ART_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > _ART_MAX_BYTES:
        return None
    # Validator derived from the DESCRIPTOR we actually read, not from a second stat
    # of the path. `no-cache` means the browser revalidates every load, and the rail
    # renders on every load, so without a validator each one would be a full 200.
    validator = f'"{st.st_ino:x}-{st.st_size:x}-{st.st_mtime_ns:x}"'
    return data, validator


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
    headers = {
        "Cache-Control": "no-cache",
        "ETag": validator,
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
    }
    if request.headers.get("If-None-Match") == validator:
        return web.Response(status=304, headers=headers)
    return web.Response(body=data, headers={**headers, "Content-Type": content_type})


async def handle_app_config(request: web.Request) -> web.Response:
    """GET/PUT /api/apps/{name}/config — read or write app config.json.

    Reads/writes ``~/.kiro/crew/apps/{name}/data/config.json``.
    GET returns the current config (empty ``{}`` if none exists).
    PUT replaces the config with the request body.
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
    config_path = data_dir / "config.json"

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

    # PUT — write config
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


#: Ceiling on one UI-bundle file this route will serve. With streaming (see
#: :func:`handle_app_ui_file`) the ceiling no longer bounds gateway memory —
#: per-request memory is one :data:`_UI_STREAM_CHUNK` regardless of file size —
#: it bounds the WORK one unauthenticated request can command (bytes read and
#: sent per request; the route bypasses token auth). Measured reality: the
#: largest UI asset a bundled app in this tree ships is ~9 KB
#: (``website/public/apps/agent-worlds/ui/index.mjs`` — the scaffold's vite
#: config externalizes react/react-dom/the SDK, so bundles stay small). 8 MiB
#: is ~900x that, matches the posture already accepted for ``_ART_MAX_BYTES``
#: above, and still clears any plausible self-bundled entry chunk. The one
#: class it can refuse is the sourcemap of an app that bundles a very heavy
#: dependency — a ``.map`` 404 degrades only devtools debugging of that app,
#: never the app itself.
_UI_MAX_BYTES = 8 * 1024 * 1024

#: Read granularity when streaming a UI file from its validated descriptor.
#: This — not the file size — is what one in-flight request pins in memory, so
#: N slow clients cost N chunks, not N files. 256 KiB keeps the thread-hop
#: count low (an 8 MiB worst case is 32 hops) while staying far below any
#: amplification concern.
_UI_STREAM_CHUNK = 256 * 1024

#: Max concurrent requests HOLDING AN OPEN DESCRIPTOR on this route. Acquired
#: before `_open_ui_file` and released after the descriptor closes: bounding
#: only the streaming loop would let every QUEUED request already hold an fd
#: while waiting for a slot, so slow-paced unauthenticated GETs could walk the
#: gateway to `EMFILE`. Under this scope at most 8 descriptors exist at once
#: and everyone else waits fd-less. It also bounds the per-chunk `to_thread`
#: hops on the SHARED default executor (same reason `_BLOB_FETCH_SEMAPHORE`
#: bounds git fetches). Refusals and body-less 304s hold a slot only for
#: microseconds; 8 comfortably covers a dashboard loading assets in parallel.
_UI_STREAM_SEMAPHORE = asyncio.Semaphore(8)

#: Wall-clock ceiling on the body-writing phase of one UI-file response, and
#: therefore on how long one client can hold a `_UI_STREAM_SEMAPHORE` permit
#: while paced by its own read speed. Without it the 8 permits are a
#: head-of-line queue an UNAUTHENTICATED caller controls: 8 sockets that
#: connect, receive one chunk and then stop reading pin every permit (and
#: descriptor) indefinitely, and every app UI on the host stops loading. The
#: value matches `_BLOB_FETCH_TIMEOUT` / `_PROXY_TIMEOUT` in this file — 30s is
#: the ceiling this module already treats as "no longer a live client", and it
#: is ~100x the budget a real transfer needs (`_UI_MAX_BYTES` is 8 MiB, so even
#: the largest servable file only needs ~280 KB/s to finish, over a loopback
#: connection to the dashboard). Expiry cancels the write loop; the enclosing
#: `finally` still closes the descriptor and the permit is released.
_UI_STREAM_TIMEOUT = 30  # seconds


def _open_ui_file(name: str, file_path: str) -> tuple[int, os.stat_result] | str:
    """An OPEN validated descriptor for *file_path* under *name*'s ui/ root
    plus its ``fstat``, or a refusal code: ``"invalid"`` (containment failure
    -> 400, the status this route has always answered escapes with) or
    ``"not_found"`` (-> 404). On the tuple path the CALLER owns closing the fd.

    Handing back the descriptor rather than a path is the security-relevant
    part, and it is the same fix :func:`_read_declared_art` carries (#6794):
    validating a path and then handing it to ``FileResponse`` opens it a SECOND
    time, so the app that owns this directory can swap a validated name for a
    symlink between the check and that open and have the gateway read the
    target instead — and the gateway is NOT sandboxed, so this launders a read
    the app's own code can be refused. This route serves the SAME app-owned
    directory with a BROADER extension allowlist (``.json``, ``.mjs``), so it
    must not keep the weaker open. One open, validated as a DESCRIPTOR, is the
    only shape without that window; everything after it reads the fd, which
    cannot be re-pointed.

    One thread hop for the whole decision — resolve, containment, open and
    stat — because every part is a blocking syscall and the gateway runs on one
    event loop (``no-blocking-call-on-event-loop``).
    """
    ui_root = apps_dir() / name / "ui"
    target = ui_root / file_path
    try:
        # Resolved ONCE, here, because `pin_parent` requires the CALLER to do
        # it: resolving inside would re-follow whatever an ancestor points at by
        # then. The ui root itself is resolved THROUGH: the documented dev-mode
        # setup links ui/ at the developer's source tree, so the ROOT being a
        # link is legitimate — containment is proven against wherever it really
        # lands, not against the link's own name.
        resolved_root = Path(os.path.realpath(ui_root))
        # But a root that lands OUTSIDE the app's own install directory is only
        # legitimate under the OPERATOR's dev-mode grant. Without this check an
        # app could ship `ui` as a symlink to a credential directory
        # (`ui -> ~/.docker`) and have this UNAUTHENTICATED route (the
        # `/apps/{name}/ui/` token-auth bypass) serve `config.json` — `.json`
        # is in the allowlist — laundering a read the app's own sandboxed code
        # is refused. `dev_mode_granted_root`, not `is_dev_mode`: the latter
        # reads only the app's own writable `installed.json`, which the app
        # could edit to authorize itself — the grant is the operator record
        # at the apps ROOT (written by the dev-mode toggle, never by the
        # startup reconcile, and refused outright for sensitive targets), and
        # it BINDS the specific resolved root granted: the current root must
        # EQUAL it, so a grant left behind by a crash, an update, or a
        # reinstall authorizes only the exact tree the operator approved,
        # never wherever `ui` points now. The disk reads are paid only on
        # this exceptional path.
        #
        # The containment ANCHOR resolves only the gateway-owned apps root
        # and then appends the literal `name` — it must NOT re-resolve
        # through the app's own entry (`realpath(apps_dir()/name)`): the two
        # realpath calls in this function would then race, and an app
        # alternating its install entry between them could get an escaping
        # `resolved_root` accepted as "inside the install". The apps root
        # itself is not app-writable, so this anchor cannot be swapped; an
        # install entry that IS a link makes its resolved ui root land
        # outside this anchor and take the grant path like any other escape.
        resolved_install = Path(os.path.realpath(apps_dir())) / name
        try:
            resolved_root.relative_to(resolved_install)
        except ValueError:
            if str(resolved_root) != dev_mode_granted_root(name):
                return "invalid"
        resolved_parent = Path(os.path.realpath(target.parent))
        # The PARENT containment is load-bearing, not belt-and-braces:
        # `pin_parent`'s contract is that a component swapped BEFORE parent
        # resolution is followed by that resolution, so an already-symlinked
        # ancestor is caught only here.
        resolved_parent.relative_to(resolved_root)
        # The full path too: a link at the FINAL name whose target escapes the
        # root is answered 400 like every other escape (the contract this route
        # has always had). This is a pre-check, not the enforcement — the pinned
        # open below refuses ANY link at the final name, escaping or not.
        Path(os.path.realpath(target)).relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        # `RuntimeError` because path resolution raises THAT — not an OSError —
        # on a symlink loop, and the app that plants one is the app whose UI
        # this serves.
        return "invalid"

    # `O_NONBLOCK` is what makes the descriptor checks REACHABLE, not an
    # optimisation: opening a FIFO blocks until a writer appears, and this runs
    # inside `asyncio.to_thread`, so an app shipping a FIFO as a UI asset would
    # park a thread-pool worker forever. With the flag the open returns
    # immediately and `S_ISREG` below refuses it. On a plain file it changes
    # nothing. Same rationale as `_read_declared_art`.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if supports_pinned_walk():
        try:
            fd = open_in_pinned_parent(
                str(resolved_parent),
                Path(file_path).name,
                flags=flags,
                mode=0o600,
                what="app UI bundle file",
            )
        except (PinnedPathRefusal, OSError, ValueError):
            # `ValueError` is NOT redundant with `OSError`: `os.open` raises it
            # — never an OSError — for a name the OS layer cannot encode (an
            # embedded NUL, a lone surrogate). Such a name survives every
            # earlier check: the extension allowlist reads the suffix AFTER the
            # bad byte, and containment resolves the PARENT, which is clean when
            # the bad byte sits in the final component.
            return "not_found"
    else:
        # Windows: no `O_NOFOLLOW` and no descriptor-relative open, so the
        # pinned walk is unavailable. An unprivileged process there cannot
        # create a FILE symlink at all, so the reachable swap is a junction —
        # refused by the reparse-point probe. Same degradation as the art
        # route; the ui-root link (dev mode) sits ABOVE the resolved root and
        # is deliberately outside the screen. The probe is name-based, so a
        # junction planted BETWEEN the probe and the open would still be
        # followed — which is why the DESCRIPTOR's own final path is
        # validated below, after the open, closing the residual window on
        # the descriptor rather than the name.
        try:
            if any(
                is_reparse_point(p)
                for p in (target, *target.parents)
                if p == resolved_root or resolved_root in p.parents or p == target
            ):
                return "not_found"
            fd = os.open(target, flags)
        except (OSError, ValueError):
            return "not_found"
        # Race-free containment on the OPENED handle: resolve the descriptor's
        # final path (`GetFinalPathNameByHandleW` there; /proc/F_GETPATH on
        # the POSIX hosts the tests run on) and require it to still sit under
        # the resolved root. Fail closed when it cannot be read — on this
        # branch the descriptor is the only trustworthy witness.
        fd_real = fd_real_path(fd)
        if fd_real is None:
            os.close(fd)
            return "not_found"
        try:
            Path(fd_real).relative_to(resolved_root)
        except ValueError:
            os.close(fd)
            return "not_found"

    try:
        st = os.fstat(fd)
        # `st_nlink != 1` is the one gate that can see a HARDLINK: the alias
        # shares the target's inode, so `is_symlink()` is False, `realpath`
        # yields the alias's own name (containment passes), and `O_NOFOLLOW`
        # has no link to refuse. Checked on the DESCRIPTOR, which is what makes
        # it race-free. Inline rather than `pinned_fs.refuse_hardlink_alias`
        # for the same double-close reason `_read_declared_art` documents.
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > _UI_MAX_BYTES:
            os.close(fd)
            return "not_found"
    except OSError:
        os.close(fd)
        return "not_found"
    return fd, st


async def handle_app_ui_file(request: web.Request) -> web.StreamResponse:
    """GET /apps/{name}/ui/{path:.*} — serve app UI bundle files.

    Serves bytes STREAMED from a pinned descriptor (see :func:`_open_ui_file`)
    rather than handing a validated path to ``FileResponse``, which re-opens it
    and re-introduces the check-then-reopen window #6794 closed on the art
    route. Streaming rather than buffering is itself load-bearing: this route
    is UNAUTHENTICATED (the ``/apps/{name}/ui/`` token-auth bypass), so a
    buffered body would let N outstanding requests each pin a whole file in
    gateway memory — with streaming, per-request memory is one chunk
    (:data:`_UI_STREAM_CHUNK`) regardless of file size or client speed.
    Behaviour contract preserved: 400 on ``..``/absolute/escaping paths, 403 on
    a disallowed extension, 404 on a missing file, Content-Type from
    ``_CONTENT_TYPES``, and conditional requests (If-None-Match /
    If-Modified-Since) still answer a body-less 304.
    """
    name = request.match_info["name"]
    file_path = request.match_info.get("path", "")
    if ".." in file_path or file_path.startswith("/"):
        return web.json_response({"error": "invalid path"}, status=400)
    ext = Path(file_path).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        return web.json_response({"error": f"file type {ext!r} not allowed"}, status=403)
    # The semaphore is acquired BEFORE the descriptor exists and released only
    # after it is closed. Bounding just the streaming loop would cap streams at
    # 8 while every QUEUED request already held an open fd waiting for a slot —
    # an unauthenticated client could then drive the gateway to `EMFILE` with
    # slow-paced GETs. Under this scope, at most 8 requests hold a descriptor
    # at any instant and everyone else waits fd-less. The refusal paths inside
    # (400/403/404, body-less 304) hold their slot only microseconds.
    async with _UI_STREAM_SEMAPHORE:
        result = await asyncio.to_thread(_open_ui_file, name, file_path)
        if result == "invalid":
            return web.json_response({"error": "invalid path"}, status=400)
        if isinstance(result, str):
            return web.json_response({"error": "not found"}, status=404)
        fd, st = result
        try:
            content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
            # Dev-mode apps: never cache — the file-watch live-reload reloads on
            # every change and must always see the latest bytes. `is_dev_mode_cached`
            # is the watcher-maintained in-memory flag, so this hot path does NO
            # disk IO on the event loop for the mode lookup
            # (no-blocking-call-on-event-loop). Everything else: no-cache (NOT
            # no-store) — the browser may cache but MUST revalidate each load. The
            # validators are derived from the DESCRIPTOR being served, not a second
            # stat of the path, so unchanged files stay a body-less 304 while app
            # updates are picked up on a plain refresh. A long
            # ``public,max-age=...`` instead would serve an app's UI stale for that
            # whole window after an update.
            cache = "no-store" if is_dev_mode_cached(name) else "no-cache"
            etag_value = f"{st.st_ino:x}-{st.st_size:x}-{st.st_mtime_ns:x}"
            # `nosniff` because the Content-Type is derived from the EXTENSION, not
            # the bytes; the CSP neuters a scripted `.svg` opened as a TOP-LEVEL
            # document on the dashboard's own origin (a response CSP does not apply
            # when the bytes are consumed as a subresource, so module/style/img
            # loads are unaffected). Same pair, same reasons, as the art route.
            headers = {
                "Cache-Control": cache,
                "ETag": f'"{etag_value}"',
                "Last-Modified": formatdate(st.st_mtime, usegmt=True),
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-Content-Type-Options": "nosniff",
            }
            # aiohttp's parsed accessors, not raw header strings: If-None-Match may
            # carry a list, a weak `W/"..."` form, or `*`, and If-Modified-Since
            # needs HTTP-date parsing that forces UTC (a raw `parsedate_to_datetime`
            # hands back a NAIVE datetime for `-0000`/asctime forms, which
            # `.timestamp()` then reads as server-LOCAL time — a stale 304 for up to
            # a whole UTC offset after an app update). Mirrors what `FileResponse`
            # did.
            if_none_match = request.if_none_match
            if if_none_match:
                # RFC 7232 §3.2: If-None-Match uses the WEAK comparison, so a weak
                # form of the current tag matches too.
                if (len(if_none_match) == 1 and if_none_match[0].value == "*") or any(
                    t.value == etag_value for t in if_none_match
                ):
                    return web.Response(status=304, headers=headers)
            else:
                # RFC 7232 §3.3: If-Modified-Since is evaluated only when no
                # If-None-Match was sent. Both sides are second-granular (HTTP
                # dates carry no sub-second part, so `st_mtime` is truncated).
                since = request.if_modified_since
                if since is not None and int(st.st_mtime) <= since.timestamp():
                    return web.Response(status=304, headers=headers)
            resp = web.StreamResponse(status=200, headers={**headers, "Content-Type": content_type})
            # Length pinned to the fstat that was validated: a file the app GROWS
            # after the open must not stream past the length the client was told,
            # so the loop below caps at `remaining` as well as EOF.
            resp.content_length = st.st_size
            await resp.prepare(request)
            remaining = st.st_size
            # The enclosing `_UI_STREAM_SEMAPHORE` scope (acquired before the
            # open, released after the close) is what bounds this loop's
            # `to_thread` hops on the shared default executor — no second
            # acquisition here: a nested acquire under the same semaphore
            # would deadlock once 8 holders each waited for a 9th permit.
            # Bounded by wall clock as well as by `remaining`: the permit is
            # held across this loop, so a client that stops reading would
            # otherwise hold it (and its fd) forever — 8 such clients wedge the
            # route for everyone. On expiry the `TimeoutError` propagates, the
            # `finally` below closes the descriptor, the permit is released, and
            # aiohttp drops a connection whose announced `content_length` can no
            # longer be honoured.
            async with asyncio.timeout(_UI_STREAM_TIMEOUT):
                while remaining > 0:
                    chunk = await asyncio.to_thread(os.read, fd, min(_UI_STREAM_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    await resp.write(chunk)
                await resp.write_eof()
            return resp
        finally:
            # Off the loop: `os.close` is on the no-blocking-call-on-event-loop
            # deny list (it can block in the kernel), and this `finally` runs on
            # the loop for every request, error paths included. `shield` is
            # load-bearing, not decoration: a client disconnect CANCELS this
            # handler, and an unshielded `to_thread` awaited during cancellation
            # can have its work item cancelled while still queued — the close
            # never runs and the descriptor leaks, on an UNAUTHENTICATED route
            # where repeated connect-then-drop would walk the gateway into
            # RLIMIT_NOFILE. Shielded, the close task runs to completion even
            # when this await is interrupted.
            await asyncio.shield(asyncio.to_thread(os.close, fd))


async def handle_app_dev_mode(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/dev — toggle dev mode (body: {"enabled": bool}).

    Metadata-only change (installed.json); the dev-mode watcher picks it up
    within one poll interval, so no gateway restart is needed.

    This route deliberately carries NO way to confirm an out-of-install ui
    root: app UI bundles run as same-origin modules with the dashboard's own
    credentials, so any request-body confirmation flag would be data the app
    controls, not operator attestation — an app could self-grant serving an
    arbitrary non-sensitive directory over the unauthenticated UI route by
    POSTing to its own toggle. Enabling dev mode on such a root therefore
    always answers 400 here (``code:
    "dev_mode_out_of_install_confirmation_required"``, naming the CLI
    command); confirmation is supplied only from the gateway host via
    ``kirocrew app dev <name> --confirm-out-of-install-root``.
    """
    from kiro_crew.apps.dev_mode import set_dev_mode

    name = request.match_info["name"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return web.json_response({"error": "enabled must be a boolean"}, status=400)
    # set_dev_mode does blocking filesystem IO (reads/writes installed.json and
    # the dev sentinel) — offload it so the gateway event loop never stalls.
    result = await asyncio.to_thread(set_dev_mode, name, enabled)
    if "error" in result:
        return web.json_response(result, status=404 if "not installed" in result["error"] else 400)
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="app_dev_mode",
        outcome="ok",
        resources=f"{name} enabled={enabled}",
    )
    return web.json_response(result)


# ---------------------------------------------------------------------------
# Git blob proxy — serve images from a registry app's git repo
# ---------------------------------------------------------------------------


def _blob_cache_dir() -> Path:
    return config_dir() / "cache" / "blobs"


def _blob_cache_key(repo: str, clone_url: str = "") -> str:
    """Derive a flat, filesystem-safe AND injective cache key for a repo.

    ``repo`` may be a full git URL (``/``, ``:``), so it can't be used as a
    directory tree.  Slugification alone is not injective (``org/app`` and
    ``org_app`` would collide and serve each other's blobs), so a short stable
    sha256 is appended to guarantee distinct repos never share a cache directory.

    The cache key is bound to the blob's PROVENANCE — the resolved clone URL
    (``clone_url``), not the ``repo`` key alone.  A ``repo`` key is not stable
    provenance: two registries can publish the same ``repo`` key over time
    (registry A is removed and registry B is later configured reusing key X), so
    a key derived from ``repo`` alone would let B's request hit A's cached
    (possibly private) bytes — a stale-provenance cross-registry read.  Folding
    the resolved clone URL into the hash namespaces the cache by the URL the
    bytes were actually cloned from, so a repo-key reuse across registries lands
    in a DISTINCT cache directory (a miss, then a fresh clone of B's own URL)
    rather than serving A's stale bytes.  ``clone_url`` defaults to empty only so
    the pure key of a bare-name repo with no resolvable URL stays stable; when a
    URL is resolved it MUST be threaded in.
    """
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", repo)
    digest = hashlib.sha256(f"{repo}\x00{clone_url}".encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}"


_BLOB_FETCH_TIMEOUT = 30  # seconds — shallow clone of a single-branch repo
_BLOB_FETCH_SEMAPHORE = asyncio.Semaphore(3)  # max 3 concurrent git fetches
# Bare-name repo identifier (legacy registry entries) — no scheme, no path.
_SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# https git URL: https://host[:port]/org/app[.git]. Host/path charset is
# restricted and shell metacharacters / traversal are rejected separately.
# Plaintext ``http://`` is deliberately NOT accepted: registry clones fetch an
# index + app manifests whose setup code later runs with gateway privileges
# (signatures are optional by default), so an unauthenticated transport would
# let a network (MITM) attacker swap in an attacker-controlled app. Require TLS
# for HTTP-style remotes; use an explicit ssh:// / scp form for private ones.
_SAFE_HTTPS_URL_RE = re.compile(r"^https://[A-Za-z0-9.\-]+(?::[0-9]+)?/[A-Za-z0-9._/\-]+$")
# scp-style ssh remote: user@host:org/app[.git]
_SAFE_SCP_URL_RE = re.compile(r"^[A-Za-z0-9._\-]+@[A-Za-z0-9.\-]+:[A-Za-z0-9._/\-]+$")
# ssh:// URL form: ssh://[user@]host[:port]/org/app[.git]
# Userinfo is optional — userless ssh URLs (e.g. ssh://git.example.com/pkg/X) are
# a standard git form where ~/.ssh/config supplies the user.
_SAFE_SSH_URL_RE = re.compile(
    r"^ssh://(?:[A-Za-z0-9._\-]+@)?[A-Za-z0-9.\-]+(?::[0-9]+)?/[A-Za-z0-9._/\-]+$"
)
# `\Z`, not `$`: Python's `$` also matches immediately BEFORE a trailing newline, so with
# the `.match` calls in the blob handler a value like "main\n" passes -- and both of these
# feed git argv and a filesystem join. Same defect class as the catalog-side coordinate
# patterns; these are the blob handler's instances. (The class is wider than this file:
# other `$`-anchored request-path patterns exist elsewhere, e.g. papyrus's GIT_URL_RE.)
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+\Z")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+\Z")


def _is_safe_repo_identifier(repo: str) -> bool:
    """Validate the blob-proxy ``repo`` query parameter.

    Registry entries are now full git URLs (``https://github.com/org/app``,
    ``git@host:org/app.git``), but legacy entries may still use a bare name.
    Accept either a bare token OR a vetted git URL — never an arbitrary string.

    Git URLs are validated against a restricted scheme/host/path charset and
    rejected outright if they contain shell metacharacters or ``..`` path
    traversal, so the value is safe to pass to ``git clone`` argv.
    """
    if not repo:
        return False
    # Reject shell metacharacters and traversal regardless of form.
    if ".." in repo or any(c in repo for c in " \t\n\r;|&$`<>()*?!\\\"'"):
        return False
    if _SAFE_REPO_RE.match(repo):
        return True
    if _SAFE_HTTPS_URL_RE.match(repo):
        return True
    if _SAFE_SCP_URL_RE.match(repo):
        return True
    if _SAFE_SSH_URL_RE.match(repo):
        return True
    return False


def _derive_registry_name(repo: str) -> str:
    """Derive a safe display name from a git URL (host + path slugified).

    Used when a URL registry is added without an explicit ``name`` — defaulting
    ``name=repo`` (the legacy behavior) would make two URL registries with
    disallowed name characters collide.  Strips the scheme + userinfo, drops a
    trailing ``.git``, and slugifies host+path to ``[A-Za-z0-9_-]`` so
    ``https://github.com/acme/apps`` becomes ``github-com-acme-apps``.

    A short stable hash of the ORIGINAL ``repo`` is appended so two distinct
    URLs whose slugs collide (e.g. ``…/org/a-b`` and ``…/org/a_b`` both slugify
    to ``…-org-a-b``) never derive the same name — and therefore never share an
    ``_external_registry_cache_path`` cache file, which would otherwise let one
    registry's fetch clobber the other's index.
    """
    s = repo.strip()
    # Strip URL scheme (https://, ssh://, git://, git+ssh://, ...).
    s = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*://", "", s)
    # Strip leading userinfo (scp-style ``user@host:path`` or ssh userinfo).
    s = re.sub(r"^[^/@]+@", "", s)
    # Drop a trailing ``.git``.
    s = re.sub(r"\.git$", "", s)
    # Slugify everything that is not alphanumeric to a single dash.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-") or "registry"
    # Disambiguate on the original repo so distinct URLs never collide.
    digest = hashlib.sha256(repo.strip().encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _repo_key_owner_count(repo: str) -> int:
    """Count the configured registry SOURCES that publish an entry keyed on ``repo``.

    The blob credential carve-out grants owner credentials only when
    :func:`_owner_designated_repo_target` confirms the resolved entry's clone URL is
    byte-identical to *its own* registry's configured ``repo``.  That predicate is
    entry-scoped and sound for the entry it is handed — but the entry is SELECTED
    by :func:`get_registry_app_by_repo`, which returns the FIRST source (bundled,
    then each external/federated registry) whose entry ``repo`` key equals the
    served ``repo``.  The selection is keyed on ``repo`` alone and provenance-blind.

    So if two configured registries both publish the same ``repo`` key, a request
    reachable through registry B can resolve to registry A's owner-designated
    entry and clone A's private repo with A's credentials, serving A's private
    image bytes to a caller who only had access to B — a cross-registry
    confused-deputy read.  The grant is only honestly attributable to a single
    owner when exactly ONE configured source claims the key.

    This counts the DISTINCT sources (the bundled registry counts once; each
    external registry counts once) whose entries carry ``entry["repo"] == repo``,
    using the SAME union :func:`known_registry_repos` admits — reading local sync
    caches only (``ignore_ttl``), never fetching, so it is safe on the per-request
    blob worker thread.  A return of ``> 1`` means the provenance is ambiguous and
    the caller must downgrade to anonymous+strict.  On any read failure it returns
    ``2`` (treat-as-ambiguous): a provenance we cannot establish must never buy a
    credential grant.
    """
    from kiro_crew.apps.registry import (
        _effective_registries,
        _load_registry_file,
        _read_external_registry_cache,
    )

    try:
        sources = 0
        if any(
            isinstance(e.get("repo"), str) and _same_git_target(e["repo"], repo)
            for e in _load_registry_file()
        ):
            sources += 1
        for reg in _effective_registries():
            cached = _read_external_registry_cache(reg.name or reg.repo, ignore_ttl=True)
            if any(
                isinstance(e, dict)
                and isinstance(e.get("repo"), str)
                and _same_git_target(e["repo"], repo)
                for e in cached or []
            ):
                sources += 1
                if sources > 1:
                    return sources  # already ambiguous — no need to keep counting
        return sources
    except Exception:  # provenance unresolvable → treat as ambiguous, never grant
        logger.debug(
            "_repo_key_owner_count: read failed for %r",
            _strip_git_target_userinfo(repo),
            exc_info=True,
        )
        return 2


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

    ``owner_designated`` extends the same-repo credential carve-out (PR 918) to
    this third clone chokepoint.  It is ``True`` only when the caller has
    confirmed — via the merged :func:`_owner_designated_repo_target` predicate,
    evaluated against the SAME entry ``git_url`` was resolved from — that the
    entry's clone URL is byte-identical to the owner-typed
    ``ExternalRegistryConfig.repo``.  In that case the confused-deputy defense
    does not apply (the owner designated exactly this URL by configuring the
    registry), so the clone uses owner credentials: ``minimal_env`` + the
    context clone sandbox mode, exactly like the manifest/clone chokepoints.
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
                clone_cmd, mode=clone_mode, _prepare=wrap_argv
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
    # BEFORE it is used to build or read the cache path, mirroring the
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
        # Same-repo credential carve-out (PR 918, extended to the blob chokepoint):
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


# ---------------------------------------------------------------------------
# Reverse proxy — app dashboard UI → app backend (same-origin, avoids CORS)
# ---------------------------------------------------------------------------

_PROXY_TIMEOUT = 30  # seconds

# App secret cache — secrets don't change after install, no need to read
# from disk on every proxied request.  Invalidated on install/uninstall.
_app_secret_cache: dict[str, str] = {}


def _get_app_secret(name: str) -> str:
    """Read the app secret, using an in-memory cache.

    Empty values are NOT cached — the secret may be provisioned after
    the first proxy attempt (e.g. install-from-source race).
    """
    cached = _app_secret_cache.get(name)
    if cached:
        return cached
    path = apps_dir() / name / ".app_secret"
    secret = path.read_text().strip() if path.is_file() else ""
    if secret:
        _app_secret_cache[name] = secret
    return secret


def invalidate_app_secret_cache(name: str) -> None:
    """Remove a cached secret (call on install/uninstall)."""
    _app_secret_cache.pop(name, None)


_PROXY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Strip sensitive auth headers — app backends use X-KiroCrew-Proxy HMAC, not user cookies
_PROXY_STRIP_HEADERS = _PROXY_HOP_HEADERS | frozenset(
    {
        "cookie",
        "authorization",
    }
)


def _resolve_app_backend_url(name: str) -> str | None:
    """Resolve the backend URL for an app.

    For gateway-managed apps: use the tracked backend port.
    For self-managed apps: check manifest for backend.url or mcpServers URL.
    """
    # 1. Gateway-managed backend (spawned by backend.py)
    port = get_app_backend_port(name)
    if port:
        return f"http://127.0.0.1:{port}"

    # 2. Self-managed: check manifest for explicit backend URL
    manifest = get_app_manifest(name)
    if not manifest:
        return None

    # backend.routes field contains the base URL for some apps
    if manifest.backend.entryPoint and manifest.backend.port != "auto":
        try:
            return f"http://127.0.0.1:{int(manifest.backend.port)}"
        except ValueError:
            pass

    # 3. Fallback: derive from the MCP server URL (common for self-managed apps)
    # e.g. crew-companion declares mcpServers."crew-companion".url =
    # "http://127.0.0.1:7778/mcp" -> the backend is at http://127.0.0.1:7778
    #
    # Shared with register_builtin_apps(), which uses the SAME function to decide
    # whether to issue the .app_secret this proxy signs with. Keeping one
    # definition is load-bearing: if resolution and secret issuance disagree, an
    # app resolves a backend here and is then refused below with 502 "has no
    # secret", which is not detectable at registration time.
    return resolve_mcp_backend_url(manifest.mcpServers)


async def handle_app_api_proxy(request: web.Request) -> web.StreamResponse:
    """Reverse proxy: /apps/{name}/api/{path} → app backend.

    Allows dashboard app UIs to call their own backend through the gateway
    (same-origin), avoiding CORS issues. The gateway authenticates the
    request and forwards it to the app's backend.
    """
    name = request.match_info["name"]
    path = request.match_info.get("path", "")

    # Path traversal guard (input validation first)
    if ".." in path:
        return web.json_response({"error": "invalid path"}, status=400)

    # Cross-app guard (CWE-269): if the caller authenticated with an APP token
    # (``request["app"]`` set by token_auth_middleware), it may only proxy into
    # its OWN backend. Dashboard-user requests (empty app identity) are allowed
    # to any app's proxy — that's the in-dashboard app UI calling same-origin.
    # The middleware's app-scope gate already blocks this, but the proxy is a
    # trust boundary (it signs the request with the target app's secret), so we
    # re-check here rather than rely solely on upstream.
    token_app = request.get("app", "")
    if token_app and token_app != name:
        # SEL audit for the permission decision (cross-app escalation attempt),
        # matching the sibling deny paths that emit log_api_access.
        sel().log_api_access(
            caller=token_app,
            operation="app_proxy_cross_app",
            outcome="denied",
            source="app_routes",
            resources=f"/apps/{name}/{path}",
            error="app token cannot access another app's backend",
        )
        return web.json_response(
            {"error": "app token cannot access another app's backend"}, status=403
        )

    # Enablement gate. The checks above prove WHO is calling; this proves the app
    # is allowed to run at all. Without it, an app the user never turned on -- every
    # builtin ships `defaultEnabled: false` -- still had an authenticated, secret-signed
    # proxy to its backend, so a mutation could reach a local app that was never
    # activated. Governance denial is covered transitively: a denied app cannot be
    # activated, so it is never enabled.
    #
    # Deliberately NOT folded into _resolve_app_backend_url: that resolver is shared
    # with register_builtin_apps(), where an app is legitimately not yet enabled, and
    # returning None here would surface refusal as the same misleading 502 "no
    # reachable backend" that sharing the resolver was meant to eliminate. This is an
    # authorization decision, so it sits with the other authorization checks and says
    # so with 403.
    if not await asyncio.to_thread(is_app_enabled, name):
        # SEL audit for the permission decision, matching the sibling deny path
        # above. An authorization denial that leaves no trail is invisible to the
        # audit log, so a repeated probe against a disabled app's backend would be
        # unobservable — which is most of the value of having the gate.
        sel().log_api_access(
            caller=request.get("app", "") or "dashboard",
            operation="app_proxy_disabled_app",
            outcome="denied",
            source="app_routes",
            resources=f"/apps/{name}/{path}",
            error="app is not enabled",
        )
        # `code` is required by test_error_code_contract.py, and is the right shape
        # here regardless: the dashboard renders `error` prose verbatim into a
        # localized page, so the machine-readable identifier is what a client can
        # switch on (and translate) while the sentence stays advisory.
        return web.json_response(
            {"code": "app_not_enabled", "error": f"app {name!r} is not enabled"},
            status=403,
        )

    # Resolve backend URL
    backend_url = _resolve_app_backend_url(name)
    if not backend_url:
        return web.json_response(
            {"error": f"app {name!r} has no reachable backend"},
            status=502,
        )

    # Build target URL — preserve the exact wire encoding of path and query
    # params so the gateway's HMAC signature matches what the backend sees on
    # self.path. `request.query_string` is DECODED by aiohttp, so signing it causes
    # HMAC verification to fail closed (401) whenever query parameters contain
    # percent-encodable characters like spaces, non-ASCII, or '+'.
    raw_qs = request.rel_url.raw_query_string
    target_path = f"/api/{path}" + (f"?{raw_qs}" if raw_qs else "")
    target_url = yarl.URL(f"{backend_url}{target_path}", encoded=True)
    wire_target = target_url.raw_path_qs

    # Forward headers (strip hop-by-hop, inject proxy auth)
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() not in _PROXY_STRIP_HEADERS and key.lower() != "host":
            headers[key] = value

    # Read request body first so the HMAC can bind it (integrity: prevents
    # a MITM/compromised path from swapping the body under a valid signature).
    body = await request.read() if request.can_read_body else None

    # Sign the proxy request with the app's secret so the backend can
    # verify it came from the gateway. Works on loopback and remote.
    # Header format: X-KiroCrew-Proxy: <timestamp>:<hmac-sha256>
    # The HMAC is computed over "timestamp:method:path[?query]:sha256(body)"
    # using the app secret as key. Backend verifies by recomputing with its
    # copy of the secret and checking the timestamp is recent (±60s).
    try:
        secret = _get_app_secret(name)
        if not secret:
            return web.json_response(
                {"error": f"app {name!r} has no secret — cannot authenticate proxy request"},
                status=502,
            )
        ts = str(int(time.time()))
        body_hash = hashlib.sha256(body or b"").hexdigest()
        msg = f"{ts}:{request.method}:{wire_target}:{body_hash}"
        sig = _hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        headers["X-KiroCrew-Proxy"] = f"{ts}:{sig}"
    except OSError as exc:
        logger.warning("Failed to read app secret for %s: %s", name, exc)
        return web.json_response(
            {"error": "proxy auth failed: cannot read app secret"},
            status=502,
        )

    try:
        timeout = aiohttp.ClientTimeout(total=_PROXY_TIMEOUT)
        session = request.app.get("_proxy_session")
        owns_session = session is None or session.closed
        if owns_session:
            session = aiohttp.ClientSession()
        try:
            async with session.request(
                method=request.method,
                url=target_url,
                headers=headers,
                data=body,
                timeout=timeout,
                allow_redirects=False,
            ) as upstream:
                # Stream response back
                resp = web.StreamResponse(
                    status=upstream.status,
                    headers={
                        k: v
                        for k, v in upstream.headers.items()
                        if k.lower() not in _PROXY_HOP_HEADERS
                    },
                )
                await resp.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        finally:
            if owns_session:
                await session.close()
    except aiohttp.ClientError as exc:
        logger.warning("Proxy to app %s failed: %s", name, exc)
        return web.json_response(
            {"error": "backend unreachable"},
            status=502,
        )
    except asyncio.TimeoutError:
        return web.json_response({"error": "backend timeout"}, status=504)


async def handle_migrate_cleanup(request: web.Request) -> web.Response:
    """DELETE /api/apps/{name}/migrate-cleanup — remove orphaned builtin metadata.

    Validates:
    1. Target app is an orphaned builtin
    2. The standalone replacement is installed

    Preserves data/ directory.
    """
    name = request.match_info["name"]
    result = cleanup_migrated_builtin(name)
    if not result.ok:
        # Map structured error_code to HTTP status
        _cleanup_status = {
            "not_orphaned": 400,
            "replacement_missing": 409,
            "io_error": 500,
        }
        status = _cleanup_status.get(result.error_code, 400)
        sel().log_api_access(
            caller="dashboard",
            operation="app_migrate_cleanup",
            outcome="failed",
            resources=name,
            error=result.error,
        )
        return web.json_response(result.to_dict(), status=status)
    sel().log_api_access(
        caller="dashboard", operation="app_migrate_cleanup", outcome="completed", resources=name
    )
    return web.json_response(result.to_dict())


async def handle_registries(request: web.Request) -> web.Response:
    """GET/PUT /api/apps/registries — manage external federated registries."""
    if request.method == "GET":
        config = KiroCrewConfig.load()
        # Operator rows report `index` as their tier because that is what is in
        # FORCE for them: `registry._registry_trust_tier` resolves `owner` only
        # from build-pinned rows, since `config.json` is agent-writable. Echoing a
        # hand-edited `owner` back would report a grant the runtime does not honour.
        registries = [
            {
                "name": r.name,
                "repo": _strip_git_target_userinfo(r.repo),
                "branch": r.branch,
                "trust": _TRUST_INDEX,
            }
            for r in config.registries
        ]
        # Edition-pinned registries are reported SEPARATELY and read-only. They
        # are not part of ``registries`` because PUT replaces that list verbatim:
        # a GET→edit→PUT round-trip would persist an edition default into the
        # operator's config.json, where a later edition change could no longer
        # move it. The client renders these as non-editable rows.
        pinned = [
            {
                "name": r.name,
                "repo": _strip_git_target_userinfo(r.repo),
                "branch": r.branch,
                "trust": r.trust,
            }
            for r in _pinned_registries()
        ]
        sel().log_api_access(
            caller="dashboard",
            operation="registries.read",
            outcome="success",
            resources=f"count={len(registries)} pinned={len(pinned)}",
        )
        return web.json_response({"registries": registries, "pinned": pinned})

    def _deny(msg: str, resources: str = "") -> web.Response:
        sel().log_api_access(
            caller="dashboard",
            operation="registries.update",
            outcome="denied",
            resources=resources or msg,
        )
        return web.json_response({"error": msg}, status=400)

    # PUT — replace the entire registries list
    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON", "invalid JSON body")

    entries = body.get("registries")
    if not isinstance(entries, list):
        return _deny("registries must be an array")

    # Validate each entry
    validated: list[dict[str, str]] = []
    _blocked_repos = {"KiroCrew"}
    # Keyed the same way `_effective_registries` decides a contest — by the cache
    # file the registry would use, not the raw string. Comparing raw names here
    # would let `Official` past the guard against a pinned `official`, persist it,
    # and then have the merge drop BOTH as contested: exactly the inert-registry
    # outcome this guard exists to prevent, now with the operator's own row lost too.
    _pinned_names = {_registry_identity_key(r.name or r.repo) for r in _pinned_registries()}
    _pinned_repos = {r.repo for r in _pinned_registries()}
    for entry in entries:
        if not isinstance(entry, dict):
            return _deny("each registry must be an object")
        repo = str(entry.get("repo", "")).strip()
        if not repo:
            return _deny("repo is required")
        public_repo = _strip_git_target_userinfo(repo)
        # Accept a bare name (legacy — kept for companion resolution) OR a
        # vetted full git URL. Reuse the blob-proxy validator, which rejects
        # shell metacharacters / traversal and owner/repo shorthand.
        if not _is_safe_repo_identifier(repo):
            return _deny(f"invalid repo URL or name: {public_repo!r}", f"repo={public_repo}")
        if repo in _blocked_repos:
            return _deny(
                f"{public_repo!r} is the core registry — no need to add it",
                f"blocked_repo={public_repo}",
            )
        if any(_same_git_target(repo, pinned_repo) for pinned_repo in _pinned_repos):
            return _deny(
                f"{public_repo!r} is already provided by this build",
                f"pinned_repo={public_repo}",
            )
        # Bare names default the display name to the repo (legacy). Full URLs
        # derive a safe slug from host+path so two URL registries never collide
        # on a default name.
        default_name = repo if _SAFE_REPO_RE.match(repo) else _derive_registry_name(repo)
        name = str(entry.get("name", "")).strip() or default_name
        if not re.match(r"^[A-Za-z0-9_\-. ]+$", name):
            return _deny(f"invalid registry name: {name!r}", f"name={name}")
        branch = str(entry.get("branch", "main")).strip() or "main"
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_\-./]*$", branch) or ".." in branch:
            return _deny(f"invalid branch name: {branch!r}", f"branch={branch}")
        # `trust` is accepted only as `index` for an operator row, and that is the
        # value stored. `registry._registry_trust_tier` resolves `owner` solely
        # from `default_registries()` — the build — because `config.json` is
        # agent-writable, so a tier persisted here could never be honoured.
        # Accepting it would hand back a setting the runtime ignores, and there is
        # correspondingly no tier to PRESERVE across a replace-all PUT: an omitted
        # value simply means `index`, which is what an operator row always is.
        raw_trust = entry.get("trust")
        trust = _TRUST_INDEX if raw_trust is None else (str(raw_trust).strip() or _TRUST_INDEX)
        if trust not in _REGISTRY_TRUST_TIERS:
            return _deny(f"invalid registry trust: {trust!r}", f"trust={trust}")
        if trust == _TRUST_OWNER:
            return _deny(
                "the trusted tier is supplied by this build, not by configuration",
                f"owner_trust_refused={name}",
            )
        # A name an edition-pinned registry already owns is refused rather than
        # persisted: `_effective_registries` drops a same-named operator row, so
        # storing it would leave a registry in config.json that never loads and
        # whose per-row refresh 404s, with nothing telling the operator why.
        if _registry_identity_key(name) in _pinned_names:
            return _deny(
                f"{name!r} is the name of a registry this build provides — choose another",
                f"pinned_name_collision={name}",
            )
        validated.append({"name": name, "repo": repo, "branch": branch, "trust": trust})

    # Update config file (atomic write to prevent corruption on crash)
    cfg = Path(config_path())
    try:
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.is_file() else {}
    except json.JSONDecodeError:
        sel().log_api_access(
            caller="dashboard",
            operation="registries.update",
            outcome="failed",
            resources="config.json malformed",
        )
        return web.json_response(
            {"error": "config.json is malformed — fix it before updating registries"},
            status=500,
        )
    except OSError as exc:
        sel().log_api_access(
            caller="dashboard",
            operation="registries.update",
            outcome="failed",
            resources=f"config read error: {exc}",
        )
        return web.json_response({"error": f"cannot read config: {exc}"}, status=500)
    # Detect hosts this PUT newly introduces to the registry trust set. A
    # configured registry host is fed into the loosened-sandbox / SSH-clone
    # trust set (see registry._configured_registry_hosts) AND its apps become
    # installable with gateway privileges, so admitting a host is a genuine
    # trust grant — not just a config edit. The generic ``registries.update``
    # event does not record WHICH host gained trust, leaving an unreconstructable
    # audit gap; emit a distinct, per-host ``registries.host_trust_granted``
    # event so incident response can always establish when/how a host entered
    # the trust set. Compare against the PRIOR on-disk config, not the freshly
    # validated list, so re-saving an unchanged list emits nothing.
    # ``data.get("registries") or []`` (not ``data.get("registries", [])``):
    # a config carrying an explicit ``"registries": null`` loads fine elsewhere
    # via the same ``or []`` idiom, so iterating the bare ``.get`` default would
    # attempt to loop over ``None`` and turn this repair-PUT into an HTTP 500,
    # blocking the only dashboard path that could fix the malformed value.
    prior = data.get("registries") or []
    prior_hosts = {
        h for r in prior if isinstance(r, dict) and (h := _git_url_host(str(r.get("repo", ""))))
    }
    newly_trusted_hosts: list[str] = []
    for r in validated:
        host = _git_url_host(r["repo"])
        if host and host not in prior_hosts and host not in newly_trusted_hosts:
            newly_trusted_hosts.append(host)
            sel().log_api_access(
                caller="dashboard",
                operation="registries.host_trust_granted",
                outcome="success",
                resources=f"host={host} repo={_strip_git_target_userinfo(r['repo'])}",
            )

    data["registries"] = validated
    cfg.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(cfg, json.dumps(data, indent=2) + "\n")

    sel().log_api_access(
        caller="dashboard",
        operation="registries.update",
        outcome="success",
        resources=(
            f"count={len(validated)} repos="
            f"{','.join(_strip_git_target_userinfo(r['repo']) for r in validated)}"
        ),
    )
    public_registries = [
        {**row, "repo": _strip_git_target_userinfo(row["repo"])} for row in validated
    ]
    return web.json_response(
        {
            "ok": True,
            "registries": public_registries,
            "newlyTrustedHosts": newly_trusted_hosts,
        }
    )


async def handle_registries_refresh(request: web.Request) -> web.Response:
    """POST /api/apps/registries/refresh — bust registry caches and re-warm.

    Optional JSON body ``{"repo": "<git-url-or-name>"}`` refreshes only the
    registry whose ``.repo`` matches; omit/empty to refresh all. The blocking
    cache-bust + re-fetch is offloaded inside ``refresh_registries`` (async).
    """
    from kiro_crew.apps.registry import refresh_registries

    caller = request.get("user", "dashboard")
    repo: str | None = None
    body_bytes = await request.read()
    if body_bytes:
        try:
            body = json.loads(body_bytes)
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        # A non-empty body MUST decode to an object. A valid-but-non-object
        # payload (e.g. ``[]`` or ``"foo"``) would otherwise leave ``repo=None``
        # and refresh EVERY configured registry — an unintended fan-out of git
        # clones / cache writes from a malformed request. Reject it as a 400.
        if not isinstance(body, dict):
            return web.json_response({"error": "request body must be a JSON object"}, status=400)
        raw = body.get("repo")
        if raw is not None:
            repo = str(raw).strip() or None

    if repo is not None and not _is_safe_repo_identifier(repo):
        public_repo = _strip_git_target_userinfo(repo)
        sel().log_api_access(
            caller=caller,
            operation="registries.refresh",
            outcome="denied",
            resources=f"repo={public_repo}",
        )
        return web.json_response({"error": f"invalid repo: {public_repo!r}"}, status=400)

    result = await refresh_registries(repo)
    if result.get("not_found"):
        public_repo = _strip_git_target_userinfo(repo or "")
        sel().log_api_access(
            caller=caller,
            operation="registries.refresh",
            outcome="not_found",
            resources=f"repo={public_repo}",
        )
        return web.json_response(
            {"error": f"no configured registry matches repo: {public_repo!r}"},
            status=404,
        )
    sel().log_api_access(
        caller=caller,
        operation="registries.refresh",
        outcome="success" if result.get("ok") else "partial",
        resources=(
            f"refreshed={len(result.get('refreshed', []))} "
            f"failed={len(result.get('failed', []))} apps={result.get('apps')}"
        ),
    )
    return web.json_response(result)


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

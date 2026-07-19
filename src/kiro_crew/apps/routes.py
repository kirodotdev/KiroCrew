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
import time
import urllib.parse
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.apps.backend import (
    get_app_backend_port,
    list_app_processes,
    start_app_backend,
    stop_app_backend,
)
from kiro_crew.apps.bridges import (
    deregister_app,
    deregister_app_crons_from_service,
    register_app,
    reregister_app_mcp_servers,
)
from kiro_crew.apps.builtins import BUILTIN_NAMES
from kiro_crew.apps.dependencies import resolve_dependencies as _resolve_deps
from kiro_crew.apps.hooks_integration import on_app_disable, on_app_enable
from kiro_crew.apps.manager import (
    apps_dir,
    cleanup_migrated_builtin,
    disable_app,
    enable_app,
    get_app,
    get_app_manifest,
    install_app,
    list_apps,
    register_external_app,
    uninstall_app,
    update_app,
)
from kiro_crew.apps.manifest import Dependencies
from kiro_crew.apps.registry import (
    get_registry_app_by_repo,
    get_server_platform,
    install_from_registry,
    is_registry_source,
    known_registry_repos,
    list_registry,
    minimal_env,
    registry_name_from_source,
)
from kiro_crew.apps.version import check_min_version as _check_min_version_str
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_dir, config_path
from kiro_crew.executors import subprocess_executor
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
# Lifecycle script helper
# ---------------------------------------------------------------------------

async def _run_lifecycle_script(
    app_name: str,
    script: str,
    *,
    timeout: int = 30,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a lifecycle script (onEnable/onDisable/onUpdate/onUninstall) in the app directory.

    Returns dict with ``output`` (str) and ``failed`` (bool).
    """
    app_root = apps_dir() / app_name
    if not app_root.is_dir():
        return {"output": f"app directory not found: {app_root}", "failed": True}

    safe_script = f"set -euo pipefail\n{script}"
    from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, wrap_argv
    base_cmd = ["/bin/bash", "-c", safe_script]
    sandboxed_cmd, cleanup = wrap_argv(base_cmd, mode="standard")
    sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling (Talos bdf0d7e5)
    env = minimal_env(NONINTERACTIVE="1")
    if extra_env:
        env.update(extra_env)
    try:
        # Process-group isolation for timeout tree-kill. Pass both flags explicitly
        # (NOT **dict unpack — breaks mypy's Popen overload resolution on the build
        # fleet): start_new_session=True is a no-op on Windows, creationflags is 0
        # (no-op) on POSIX. (App lifecycle scripts are bash; on Windows without bash
        # they fail gracefully rather than crash here.)
        proc = await asyncio.create_subprocess_exec(
            *sandboxed_cmd,
            cwd=str(app_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            preexec_fn=resource_limit_preexec(),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = (stdout or b"").decode(errors="replace").strip()
            lines = output.split("\n")
            output = "\n".join(lines[-20:])  # last 20 lines
        except asyncio.TimeoutError:
            try:
                # killpg on POSIX, taskkill /T on Windows — via platform_compat.
                platform_compat.kill_process_tree(proc.pid, platform_compat.SIGTERM)
            except OSError:
                proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            return {"output": f"script timed out after {timeout}s", "failed": True}
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass

    failed = bool(proc.returncode and proc.returncode != 0)
    return {"output": output, "failed": failed}


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


def _sync_builtin_config(name: str, *, enabled: bool) -> None:
    """Update config.json for a builtin app so gateway reads the right state on restart."""
    cfg_key, _ = _BUILTIN_SERVICE_APPS.get(name, (None, None))
    if cfg_key is None:
        return
    path = config_path()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (json.JSONDecodeError, OSError) as exc:
        raise OSError(f"Could not read config.json: {exc}") from exc
    section = data.setdefault(cfg_key, {})
    section["enabled"] = enabled
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.chmod(0o600)
        tmp.rename(path)
    except OSError:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
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


async def handle_list_apps(request: web.Request) -> web.Response:
    """GET /api/apps — list all installed apps."""
    apps = list_apps()
    # Enrich with backend process status
    procs = {p["app_name"]: p for p in list_app_processes()}
    for app in apps:
        proc = procs.get(app["name"])
        if proc:
            app["backend_status"] = {
                "running": True,
                "port": proc["port"],
                "healthy": proc["healthy"],
                "pid": proc["pid"],
            }
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
    "set it up" link otherwise. Built-in providers (e.g. Artifactory) are registered
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
                app_name, endpoint, allowed_prefix,
            )
            continue
        providers.append({
            "id": str(pp["id"]),
            "label": str(pp.get("label", pp["id"])),
            "icon": str(pp.get("icon", "")),
            "endpoint": endpoint,
            "kinds": [str(k) for k in pp.get("kinds", []) if k],
            "setupRoute": str(pp.get("setupRoute", "")),
            "app": app_name,
            "origin": "app",
            "configured": bool(resolver(app_name, pp)),
        })
    return providers


async def handle_publish_providers(request: web.Request) -> web.Response:
    """GET /api/publish-providers — publish destinations (app-declared + core deploy).

    Returns enabled apps' publish providers plus the core deploy provider (folded
    from the former deploy_web app), each with a ``configured`` flag. Built-in
    providers (Artifactory) are registered frontend-side and are not returned here.
    """
    providers = collect_publish_providers(list_apps())
    # Core deploy provider (always present, regardless of any app install state)
    try:
        from kiro_crew.deploy import profiles as _deploy_profiles

        # Align with deploy/handlers.py: registry reads go through to_thread.
        reg = await asyncio.to_thread(_deploy_profiles.load_registry)
        configured = bool(reg["profiles"])
    except Exception:
        configured = False
    providers.append({
        "id": "deploy-web-aws",
        "label": "Publish to public web (your AWS)",
        "icon": "Globe",
        "endpoint": "/api/deploy/deploy",
        "kinds": ["widget", "html", "markdown"],
        "setupRoute": "/artifacts/deploy",
        "app": "",
        "origin": "core",
        "configured": configured,
    })
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
    return web.json_response(info)


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
        await asyncio.get_running_loop().run_in_executor(subprocess_executor(), start_app_backend, name)
    except Exception:
        logger.warning("Backend auto-start after install failed for app %s", name, exc_info=True)


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
    manifest_path = source_path / "app.json"
    if manifest_path.is_file():
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            ver_err = _check_min_version(manifest_data)
            if ver_err:
                return web.json_response({"error": ver_err}, status=400)
        except (json.JSONDecodeError, OSError):
            pass

    result = install_app(source)
    if not result.ok:
        sel().log_api_access(caller="dashboard", operation="app_install", outcome="failed", resources=source, error=result.error)
        return web.json_response(result.to_dict(), status=400)
    invalidate_app_secret_cache(result.name)

    # Auto-register resources
    reg = register_app(result.name)
    # Spawn the backend now so the app is reachable without a gateway reboot
    # (see _start_backend_after_install). No-op for backend-less apps.
    await _start_backend_after_install(result.name)
    sel().log_api_access(caller="dashboard", operation="app_install", outcome="completed", resources=result.name)
    return web.json_response({
        **result.to_dict(),
        "registration": reg.to_dict(),
    }, status=201)


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
            {"error": f"app {name!r} has lifecycle={lifecycle!r} — cannot be updated via this endpoint"},
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
        reg_install = await install_from_registry(registry_name)
        if not reg_install.get("ok"):
            sel().log_api_access(caller="dashboard", operation="app_update", outcome="failed", resources=name, error=reg_install.get("error", ""))
            return web.json_response(reg_install, status=400)
        # Install succeeded — now safe to swap resources
        deregister_app(name)
        await asyncio.get_running_loop().run_in_executor(subprocess_executor(), stop_app_backend, name)
        if info.get("enabled"):
            reg_result = register_app(name)
            await asyncio.get_running_loop().run_in_executor(subprocess_executor(), start_app_backend, name)
            reg_install["registration"] = reg_result.to_dict()
        sel().log_api_access(caller="dashboard", operation="app_update", outcome="completed", resources=name)
        return web.json_response(reg_install)

    if not source:
        return web.json_response(
            {"error": "source path required (not found in installed metadata)"}, status=400,
        )

    # Deregister old resources before update
    deregister_app(name)
    await asyncio.get_running_loop().run_in_executor(subprocess_executor(), stop_app_backend, name)

    up_result = update_app(source)
    if not up_result.ok:
        # Re-register old resources on failure
        register_app(name)
        if info.get("enabled"):
            await asyncio.get_running_loop().run_in_executor(subprocess_executor(), start_app_backend, name)
        sel().log_api_access(caller="dashboard", operation="app_update", outcome="failed", resources=name, error=up_result.error)
        return web.json_response(up_result.to_dict(), status=400)

    # Re-register with new manifest if app was enabled
    up_reg = None
    if info.get("enabled"):
        up_reg = register_app(name)
        await asyncio.get_running_loop().run_in_executor(subprocess_executor(), start_app_backend, name)

    sel().log_api_access(caller="dashboard", operation="app_update", outcome="completed", resources=name)
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
            {"error": "name, version, and displayName are required"}, status=400,
        )

    result = register_external_app(
        name=name,
        version=version,
        display_name=display_name,
        source=body.get("source", ""),
        manifest_data=body.get("manifest"),
        origin=body.get("origin", "external"),
        resources=body.get("resources", "app"),
        lifecycle=body.get("lifecycle", "app"),
    )
    if not result.ok:
        sel().log_api_access(caller="dashboard", operation="app_register_external", outcome="failed", resources=name, error=result.error)
        return web.json_response(result.to_dict(), status=400)
    sel().log_api_access(caller="dashboard", operation="app_register_external", outcome="completed", resources=name)
    resp = result.to_dict()
    # Include the generated app secret so the caller can use it for auth
    if result.secret:
        resp["secret"] = result.secret
    return web.json_response(resp, status=201)


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
    declared_deps: list[str] = []
    aim_deps = deps_data.get("aim", {})
    for dep_type in ("mcp", "skills", "agents"):
        for entry in aim_deps.get(dep_type, []):
            dep_id = entry.get("id") if isinstance(entry, dict) else entry
            if not dep_id:
                continue
            declared_deps.append(f"aim/{dep_type}/{dep_id}")

    # Classify dependencies
    from kiro_crew.apps.dependency_ledger import classify_for_uninstall
    dep_classification = classify_for_uninstall(name, declared_deps)

    return web.json_response({
        "app": name,
        "lifecycle": lifecycle,
        "resources": {
            "agents": manifest.get("agents", []),
            "skills": manifest.get("skills", []),
            "crons": [c.get("name", "") for c in manifest.get("crons", [])],
        },
        "dependencies": dep_classification,
    })


async def handle_uninstall_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/uninstall — uninstall an app.

    1. Check lifecycle field (locked → 400)
    2. Run onUninstall script (if declared)
    3. Stop backend + deregister resources (gateway-managed only)
    4. Clean removable dependencies (unless keep_dependencies=true)
    5. Remove app files (preserve data/ if keep_data=true)
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
    keep_data = False
    keep_dependencies = False
    keep_specific: list[str] = []
    try:
        body = await request.json()
        keep_data = body.get("keep_data", False)
        keep_dependencies = body.get("keep_dependencies", False)
        keep_specific = body.get("keep_specific", [])
    except Exception:
        pass

    # Step 1: Run onUninstall script
    on_uninstall = (manifest.get("setup") or {}).get("onUninstall", "")
    if on_uninstall:
        script_output = await _run_lifecycle_script(
            name, on_uninstall, timeout=120,
            extra_env={"KEEP_DATA": "1" if keep_data else "0"},
        )
        if script_output.get("output"):
            from kiro_crew.security import redact_credentials, redact_exfiltration_urls
            cleaned, _ = redact_exfiltration_urls(script_output["output"])
            cleaned, _ = redact_credentials(cleaned)
            uninstall_log.append(cleaned)
        if script_output.get("failed"):
            uninstall_log.append("onUninstall script failed (exit code non-zero)")

    # Step 2: Deregister resources (gateway-managed only)
    if resources == "gateway":
        await asyncio.get_running_loop().run_in_executor(subprocess_executor(), stop_app_backend, name)
        # Clean up app-declared cron jobs from the scheduler before the
        # per-app cron manifest is removed by deregister_app(). Mirrors the
        # cleanup that on_app_disable performs on the disable path.
        state = request.app.get("state")
        cron_service = getattr(state, "crons", None) if state else None
        if cron_service is not None:
            try:
                removed = deregister_app_crons_from_service(name, cron_service)
                sel().log_api_access(
                    caller="dashboard",
                    operation="app_crons_deregister",
                    outcome="completed",
                    resources=f"app={name} removed={removed}",
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
        deregister_app(name)

    # Step 3: Clean dependencies (atomic classify + ledger update)
    cleaned_deps: list[str] = []
    if not keep_dependencies:
        deps_data = manifest.get("dependencies", {})
        aim_deps = deps_data.get("aim", {})
        declared_deps: list[str] = []
        for dep_type in ("mcp", "skills", "agents"):
            for entry in aim_deps.get(dep_type, []):
                dep_id = entry.get("id") if isinstance(entry, dict) else entry
                if not dep_id:
                    continue
                declared_deps.append(f"aim/{dep_type}/{dep_id}")

        from kiro_crew.apps.dependency_ledger import classify_and_clean_for_uninstall
        classification = classify_and_clean_for_uninstall(
            name, declared_deps, keep_specific=list(keep_specific),
        )
        removable = [
            d for d in classification.get("removable", [])
            if d.get("id") not in keep_specific
        ]
        if removable:
            from kiro_crew.apps.dependencies import clean_dependencies
            cleaned_deps = await clean_dependencies(name, removable)
            if cleaned_deps:
                uninstall_log.append(f"Cleaned {len(cleaned_deps)} dependency(ies)")

    # Step 4: Remove files
    result = uninstall_app(name, keep_data=keep_data)
    if not result.ok:
        sel().log_api_access(caller="dashboard", operation="app_uninstall", outcome="failed", resources=name, error=result.error)
        return web.json_response(result.to_dict(), status=400)
    invalidate_app_secret_cache(name)

    # Step 5: Clean up workspace (each registry app has its own workspace)
    if is_registry_source(info.get("source", "")):
        app_reg_name = registry_name_from_source(info.get("source", ""))
        if app_reg_name:
            from kiro_crew.apps.registry import app_source_dir
            ws_dir = app_source_dir(app_reg_name)
            if ws_dir.is_dir():
                shutil.rmtree(ws_dir, ignore_errors=True)
                uninstall_log.append(f"Removed workspace for {app_reg_name}")

    sel().log_api_access(caller="dashboard", operation="app_uninstall", outcome="completed", resources=name)
    resp = result.to_dict()
    if uninstall_log:
        resp["uninstall_log"] = "\n".join(uninstall_log)
    if cleaned_deps:
        resp["cleaned_dependencies"] = cleaned_deps
    return web.json_response(resp)


async def handle_enable_app(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/enable — enable an app.

    Behavior depends on ``resources`` field:
    - ``gateway``: register_app() + start_backend() + run onEnable
    - ``app``: run onEnable only
    If onEnable fails, the enable is rolled back (app stays disabled).
    """
    name = request.match_info["name"]
    info = get_app(name)
    if not info:
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    resources = info.get("resources", "gateway")
    manifest = info.get("manifest", {})
    on_enable = (manifest.get("setup") or {}).get("onEnable", "")
    enable_timeout = int((manifest.get("setup") or {}).get("onEnableTimeout", 30))

    result = enable_app(name)
    if not result.ok:
        sel().log_api_access(caller="dashboard", operation="app_enable", outcome="failed", resources=name, error=result.error)
        return web.json_response(result.to_dict(), status=400)

    resp: dict[str, Any] = result.to_dict()

    # Register resources if gateway-managed
    if resources == "gateway":
        reg = register_app(name)
        backend = await asyncio.get_running_loop().run_in_executor(subprocess_executor(), start_app_backend, name)
        # MCP re-registration is HEALTH-GATED (review CR-284432051). register_app ran before
        # the backend was up, so an HTTP MCP server with backend.port:"auto" carries the
        # manifest's illustrative port. The backend's health-check loop calls
        # _gate_mcp_registration once /health passes, rewriting the url to the real allocated
        # port (and scrubbing it if the backend never becomes healthy — the dead-url shape
        # that broke kiro-cli). EXCEPTION: an adopted already-healthy instance runs no health
        # loop, so register it synchronously here.
        if backend is not None and getattr(backend, "healthy", False):
            try:
                reregister_app_mcp_servers(name, live_port=getattr(backend, "port", None))
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP re-registration after backend start failed for %s: %s", name, exc)
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

    # Run onEnable script
    if on_enable:
        script_output = await _run_lifecycle_script(name, on_enable, timeout=enable_timeout)
        if script_output.get("failed"):
            # Rollback: disable the app again
            if resources == "gateway":
                await asyncio.get_running_loop().run_in_executor(subprocess_executor(), stop_app_backend, name)
                deregister_app(name)
            disable_app(name)
            sel().log_api_access(caller="dashboard", operation="app_enable", outcome="failed", resources=name, error="onEnable script failed")
            from kiro_crew.security import redact_credentials
            cleaned, _ = redact_credentials(script_output.get("output", ""))
            return web.json_response({
                "ok": False, "name": name,
                "error": "onEnable script failed — app remains disabled",
                "script_output": cleaned,
            }, status=400)
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
            name, info,
            cron_service=getattr(state, "crons", None),
            broadcast_fn=getattr(state, "broadcast", None),
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
            _sync_builtin_config(name, enabled=True)
        except OSError as exc:
            logger.warning("Failed to sync config.json for %s: %s", name, exc)
            resp.setdefault("warnings", []).append(_redact_warning(f"config sync failed: {exc}"))
        else:
            svc_warn = await _notify_builtin_service(request, name)
            if svc_warn:
                resp.setdefault("warnings", []).append(_redact_warning(svc_warn))

    sel().log_api_access(caller="dashboard", operation="app_enable", outcome="completed", resources=name)
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

    resources = info.get("resources", "gateway")
    manifest = info.get("manifest", {})
    on_disable = (manifest.get("setup") or {}).get("onDisable", "")
    disable_timeout = int((manifest.get("setup") or {}).get("onDisableTimeout", 30))
    warnings: list[str] = []

    # Run onDisable script first
    if on_disable:
        script_output = await _run_lifecycle_script(name, on_disable, timeout=disable_timeout)
        if script_output.get("failed"):
            from kiro_crew.security import redact_credentials
            raw_output = script_output.get("output", "")[:200]
            cleaned, _ = redact_credentials(raw_output)
            warnings.append(f"onDisable script failed: {cleaned}")
            logger.warning("onDisable failed for %s, proceeding with disable", name)

    # Invoke Python lifecycle hooks (on_shutdown + route deregistration + cron cleanup)
    try:
        hooks_result = await on_app_disable(name, info)
        if hooks_result:
            for k, v in hooks_result.items():
                if k == "cron_cleanup" and isinstance(v, str):
                    warnings.append(v)
    except Exception as exc:
        logger.warning("Hook disable failed for %s: %s", name, exc)
        warnings.append(_redact_warning(f"hooks disable failed: {exc}"))

    # Deregister resources if gateway-managed
    if resources == "gateway":
        await asyncio.get_running_loop().run_in_executor(subprocess_executor(), stop_app_backend, name)
        deregister_app(name)

    result = disable_app(name)
    if not result.ok:
        sel().log_api_access(caller="dashboard", operation="app_disable", outcome="failed", resources=name, error=result.error)
        return web.json_response(result.to_dict(), status=400)

    # Run builtin on_disable hook if available
    if name in BUILTIN_NAMES:
        try:
            mod = importlib.import_module(f"kiro_crew.apps.builtins.{name}")
            if hasattr(mod, "on_disable"):
                mod.on_disable(request.app)
        except Exception as exc:
            logger.warning("on_disable hook for %s failed: %s", name, exc)
            warnings.append(_redact_warning(f"on_disable hook failed: {exc}"))

    # Sync config.json and stop live service for builtin apps
    origin = info.get("origin", "")
    if origin == "builtin" and name in _BUILTIN_SERVICE_APPS:
        try:
            _sync_builtin_config(name, enabled=False)
        except OSError as exc:
            logger.warning("Failed to sync config.json for %s: %s", name, exc)
            warnings.append(_redact_warning(f"config sync failed: {exc}"))
        else:
            svc_warn = await _notify_builtin_service(request, name)
            if svc_warn:
                warnings.append(_redact_warning(svc_warn))

    sel().log_api_access(caller="dashboard", operation="app_disable", outcome="completed", resources=name)
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

    manifest = info.get("manifest", {})
    open_cmd = manifest.get("openCommand", "")
    if not open_cmd:
        return web.json_response({"error": "app has no openCommand"}, status=400)

    # Detect cloud/remote — no DISPLAY and not macOS desktop
    import os
    import platform
    is_local = (
        platform.system() == "Darwin"
        or os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
    )

    if not is_local:
        return web.json_response({
            "ok": False,
            "name": name,
            "remote": True,
            "command": open_cmd,
            "message": f"KiroCrew is running remotely. Run this on your local machine: {open_cmd}",
        })

    try:
        from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, wrap_argv
        base_cmd = ["/bin/sh", "-c", open_cmd]
        sandboxed_cmd, _cleanup = wrap_argv(base_cmd, mode="standard")
        sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling (Talos bdf0d7e5)
        proc = await asyncio.create_subprocess_exec(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=resource_limit_preexec(),
        )
        # Don't wait — launch is fire-and-forget
        sel().log_api_access(
            caller="dashboard", operation="app_open",
            outcome="launched", resources=f"{name} pid={proc.pid}",
        )
        return web.json_response({"ok": True, "name": name, "pid": proc.pid})
    except Exception as exc:
        sel().log_api_access(
            caller="dashboard", operation="app_open",
            outcome="failed", resources=name, error=str(exc),
        )
        return web.json_response({"error": f"failed to launch: {exc}"}, status=500)


# ---------------------------------------------------------------------------
# Registry (browse & install from curated list)
# ---------------------------------------------------------------------------

async def handle_registry(request: web.Request) -> web.Response:
    """GET /api/apps/registry — list all apps available for installation."""
    apps = await list_registry()
    return web.json_response({
        "apps": apps,
        "serverPlatform": get_server_platform(),
    })


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
        sel().log_api_access(caller="dashboard", operation="app_registry_install", outcome="failed", resources=name, error=result.get("error", ""))
        return web.json_response(result, status=400)

    # Auto-register resources
    reg = register_app(result["name"])
    # Spawn the backend now so apps with a server are reachable immediately —
    # without this the backend only starts on the next gateway reboot (via
    # start_enabled_app_backends), leaving the app's UI with "no reachable
    # backend" until then. No-op for apps that declare no backend. Run in a
    # thread because start_app_backend blocks on a health-check poll.
    await _start_backend_after_install(result["name"])
    result["registration"] = reg.to_dict()
    sel().log_api_access(caller="dashboard", operation="app_registry_install", outcome="completed", resources=name)
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

    # Run install + drain concurrently
    install_task = asyncio.create_task(
        install_from_registry(name, log_lines=streaming_log)
    )
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
        sel().log_api_access(caller="dashboard", operation="app_registry_install_stream", outcome="failed", resources=name, error=result.get("error", ""))
        await _send_sse("done", json.dumps(result))
        await resp.write_eof()
        return resp

    # Auto-register resources (same as non-streaming endpoint)
    reg = register_app(result["name"])
    # Spawn the backend immediately (see handle_registry_install) so the app is
    # reachable without a gateway reboot. No-op for backend-less apps.
    await _start_backend_after_install(result["name"])
    result["registration"] = reg.to_dict()
    sel().log_api_access(caller="dashboard", operation="app_registry_install_stream", outcome="completed", resources=name)
    await _send_sse("done", json.dumps(result))
    await resp.write_eof()
    return resp


# ---------------------------------------------------------------------------
# Static file serving for app UI bundles
# ---------------------------------------------------------------------------

_ALLOWED_EXTENSIONS = frozenset({
    ".mjs", ".js", ".css", ".json", ".svg", ".png", ".jpg",
    ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".ttf", ".map",
})

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


async def handle_app_config(request: web.Request) -> web.Response:
    """GET/PUT /api/apps/{name}/config — read or write app config.json.

    Reads/writes ``~/.kirocrew/apps/{name}/data/config.json``.
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
            return web.json_response(
                {"error": f"failed to read config: {exc}"}, status=500
            )

    # PUT — write config
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    if not isinstance(body, dict):
        return web.json_response(
            {"error": "config must be a JSON object"}, status=400
        )

    try:
        content = json.dumps(body, indent=2) + "\n"
        await asyncio.to_thread(atomic_write, config_path, content)
    except OSError as exc:
        return web.json_response(
            {"error": f"failed to write config: {exc}"}, status=500
        )

    sel().log_api_access(
        caller="dashboard",
        operation="app_config_write",
        outcome="completed",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name})


async def handle_app_ui_file(request: web.Request) -> web.Response:
    """GET /apps/{name}/ui/{path:.*} — serve app UI bundle files."""
    name = request.match_info["name"]
    file_path = request.match_info.get("path", "")
    if ".." in file_path or file_path.startswith("/"):
        return web.json_response({"error": "invalid path"}, status=400)
    from pathlib import Path
    ext = Path(file_path).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        return web.json_response({"error": f"file type {ext!r} not allowed"}, status=403)
    full_path = apps_dir() / name / "ui" / file_path
    if not full_path.is_file():
        return web.json_response({"error": "not found"}, status=404)
    ui_root = (apps_dir() / name / "ui").resolve()
    try:
        full_path.resolve().relative_to(ui_root)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
    return web.FileResponse(full_path, headers={"Content-Type": content_type, "Cache-Control": "public, max-age=3600"})  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Git blob proxy — serve images from a registry app's git repo
# ---------------------------------------------------------------------------

def _blob_cache_dir() -> Path:
    return config_dir() / "cache" / "blobs"


_BLOB_FETCH_TIMEOUT = 30  # seconds — shallow clone of a single-branch repo
_BLOB_FETCH_SEMAPHORE = asyncio.Semaphore(3)  # max 3 concurrent git fetches
# Bare-name repo identifier (legacy registry entries) — no scheme, no path.
_SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# https/http git URL: https://host[:port]/org/app[.git]. Host/path charset is
# restricted and shell metacharacters / traversal are rejected separately.
_SAFE_HTTPS_URL_RE = re.compile(
    r"^https?://[A-Za-z0-9.\-]+(?::[0-9]+)?/[A-Za-z0-9._/\-]+$"
)
# scp-style ssh remote: user@host:org/app[.git]
_SAFE_SCP_URL_RE = re.compile(
    r"^[A-Za-z0-9._\-]+@[A-Za-z0-9.\-]+:[A-Za-z0-9._/\-]+$"
)
# ssh:// URL form: ssh://user@host[:port]/org/app[.git]
_SAFE_SSH_URL_RE = re.compile(
    r"^ssh://[A-Za-z0-9._\-]+@[A-Za-z0-9.\-]+(?::[0-9]+)?/[A-Za-z0-9._/\-]+$"
)
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_BLOB_ALLOWED_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"})


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


def _registry_git_url(repo: str) -> str | None:
    """Resolve the git clone URL for a registry repo, or ``None``.

    The registry entry's ``repo`` field carries the clone URL for the
    open-source build (e.g. ``https://github.com/org/app`` or
    ``git@github.com:org/app.git``).  An entry may also set an explicit
    ``gitUrl``/``cloneUrl`` field.  Returns ``None`` when the entry has no
    resolvable URL so the caller can fail gracefully instead of assuming
    any particular host.
    """
    entry = get_registry_app_by_repo(repo)
    if not entry:
        return None
    for key in ("gitUrl", "cloneUrl"):
        url = entry.get(key)
        if isinstance(url, str) and url:
            return url
    # The repo field itself is treated as a clone URL when it looks like one.
    if ("://" in repo) or repo.startswith("git@") or repo.endswith(".git"):
        return repo
    return None


async def _fetch_git_blob(repo: str, ref: str, file_path: str, cache_path: Path) -> bool:
    """Fetch a single file from a registry app's git repo via a shallow clone.

    Public git hosts (GitHub, etc.) disable the ``git-upload-archive`` service
    used by ``git archive --remote``, so we instead perform a shallow
    ``git clone --depth 1 --branch <ref>`` into a throwaway temp directory
    (mirroring how :mod:`kiro_crew.apps.registry` already clones), read the
    requested file out of the checkout, and write it to the blob cache.  The
    clone URL is resolved from the registry entry; returns ``False`` (graceful
    fallback) when no URL is resolvable or anything goes wrong.
    """
    from kiro_crew.apps.registry import (
        _context_clone_sandbox_mode,
        minimal_env,
    )
    from kiro_crew.sandbox import cgroup_scope_argv, resource_limit_preexec, wrap_argv

    git_url = _registry_git_url(repo)
    if not git_url:
        logger.debug("No git URL resolvable for registry repo %r — skipping blob fetch", repo)
        return False

    import tempfile

    tmp_root: str | None = None
    try:
        tmp_root = await asyncio.to_thread(tempfile.mkdtemp, prefix="kirocrew-blob-")
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
        # Route through the context-aware clone-sandbox decision (same as the
        # registry.py clone sites) so a companion's extended trusted-host set
        # applies here too; standalone resolves to the same bare decision.
        sandboxed_cmd, _cleanup = wrap_argv(
            clone_cmd, mode=_context_clone_sandbox_mode(git_url)
        )
        sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling (Talos bdf0d7e5)
        proc = await asyncio.create_subprocess_exec(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=minimal_env(),
            preexec_fn=resource_limit_preexec(),
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_BLOB_FETCH_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.warning("git clone timed out for %s/%s", repo, file_path)
            return False

        if proc.returncode != 0:
            logger.debug(
                "git clone failed for %s/%s: %s", repo, file_path,
                stderr.decode(errors="replace").strip() if stderr else "",
            )
            return False

        # Read the requested file from the checkout, guarding against escapes
        # out of the clone via symlinks or traversal.
        clone_root = Path(tmp_root).resolve()
        blob_path = (clone_root / file_path).resolve()
        try:
            blob_path.relative_to(clone_root)
        except ValueError:
            logger.debug("blob path escapes clone root for %s/%s", repo, file_path)
            return False
        if not blob_path.is_file():
            return False
        data = await asyncio.to_thread(blob_path.read_bytes)
    except OSError as exc:
        logger.debug("Failed to fetch blob from %s/%s: %s", repo, file_path, exc)
        return False
    finally:
        if tmp_root:
            await asyncio.to_thread(shutil.rmtree, tmp_root, ignore_errors=True)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(cache_path.write_bytes, data)
    return True


async def handle_blob_proxy(request: web.Request) -> web.Response:
    """GET /api/apps/blob — proxy image files from a registry app's git repo.

    Query params:
      repo  — registry repo identifier (matches a registry entry's ``repo``)
      path  — file path within the repo (e.g. "assets/icon/logo.png")
      ref   — git ref, defaults to "mainline"

    SECURITY: Only serves repos listed in the registry JSON (prevents SSRF).
    Caches fetched blobs to ~/.kirocrew/cache/blobs/{repo}/{ref}/{path}.
    Only serves image file types.
    """
    repo = request.query.get("repo", "")
    file_path = request.query.get("path", "")
    # Look up the registry entry's branch; fall back to query param or mainline
    ref = request.query.get("ref", "")
    if not ref:
        entry = get_registry_app_by_repo(repo) if repo else None
        ref = entry.get("branch", "mainline") if entry else "mainline"

    # Validate inputs
    if not repo or not file_path:
        return web.json_response({"error": "repo and path required"}, status=400)
    if not _is_safe_repo_identifier(repo):
        return web.json_response({"error": "invalid repo name"}, status=400)
    if not _SAFE_PATH_RE.match(file_path):
        return web.json_response({"error": "invalid path characters"}, status=400)
    if not _SAFE_REF_RE.match(ref):
        return web.json_response({"error": "invalid ref"}, status=400)
    if ".." in file_path or file_path.startswith("/"):
        return web.json_response({"error": "invalid path"}, status=400)
    # Block access to git internals and other hidden directories
    if any(seg.startswith(".") for seg in Path(file_path).parts):
        return web.json_response({"error": "hidden path segments not allowed"}, status=400)

    ext = Path(file_path).suffix.lower()
    if ext not in _BLOB_ALLOWED_EXT:
        return web.json_response({"error": f"file type {ext!r} not allowed"}, status=403)

    # SECURITY: Only allow repos that appear in the registry (prevents SSRF)
    allowed = await asyncio.to_thread(known_registry_repos)
    if repo not in allowed:
        return web.json_response({"error": "repo not in registry"}, status=403)

    # Check cache.  ``repo`` may now be a full git URL (containing ``/`` and
    # ``:``), so derive a flat, filesystem-safe cache key rather than using the
    # raw value as a directory tree.  The resolved-path check below still
    # guards against any escape out of the cache root.
    repo_key = re.sub(r"[^A-Za-z0-9_.-]", "_", repo)
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
        async with _BLOB_FETCH_SEMAPHORE:
            # Re-check after acquiring semaphore (another request may have cached it)
            if not cache_path.is_file():
                ok = await _fetch_git_blob(repo, ref, file_path, cache_path)
                if not ok:
                    return web.json_response({"error": "failed to fetch blob"}, status=502)

    content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    sel().log_api_access(caller="dashboard", operation="app_blob_proxy", outcome="served", resources=f"repo={repo} path={file_path}")
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


_PROXY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})

# Strip sensitive auth headers — app backends use X-KiroCrew-Proxy HMAC, not user cookies
_PROXY_STRIP_HEADERS = _PROXY_HOP_HEADERS | frozenset({
    "cookie", "authorization",
})


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

    # 3. Fallback: derive from MCP server URL (common for self-managed apps)
    # e.g. mochi-pet declares mcpServers.mochi-pet.url = "http://localhost:7778/mcp"
    # → backend is at http://127.0.0.1:7778
    for _server_name, server_cfg in manifest.mcpServers.items():
        if isinstance(server_cfg, dict):
            url = server_cfg.get("url", "")
            if url.startswith("http"):
                # Strip the path (e.g. /mcp) to get the base URL
                from urllib.parse import urlparse
                parsed = urlparse(url)
                # Normalize localhost → 127.0.0.1 to avoid IPv6 resolution
                # issues with aiohttp on macOS (::1 may not be bound).
                host = parsed.hostname or "127.0.0.1"
                if host == "localhost":
                    host = "127.0.0.1"
                # Validate loopback to prevent SSRF via manifest-declared URLs
                import ipaddress
                try:
                    if not ipaddress.ip_address(host).is_loopback:
                        logger.warning("Refusing non-loopback backend URL %s for app %s", url, name)
                        continue
                except ValueError:
                    logger.warning("Refusing non-IP backend host %r for app %s", host, name)
                    continue
                port_num = parsed.port or (443 if parsed.scheme == "https" else 80)
                # Reject self-referential URLs (gateway's own port) to prevent SSRF
                gateway_port = int(os.environ.get("KIROCREW_PORT", "5476"))
                if port_num == gateway_port:
                    logger.warning("Refusing self-referential backend URL for app %s", name)
                    continue
                return f"{parsed.scheme}://{host}:{port_num}"

    return None


async def handle_app_api_proxy(request: web.Request) -> web.StreamResponse:
    """Reverse proxy: /apps/{name}/api/{path} → app backend.

    Allows dashboard app UIs to call their own backend through the gateway
    (same-origin), avoiding CORS issues. The gateway authenticates the
    request and forwards it to the app's backend.
    """
    # (moved to top-level)

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

    # Resolve backend URL
    backend_url = _resolve_app_backend_url(name)
    if not backend_url:
        return web.json_response(
            {"error": f"app {name!r} has no reachable backend"}, status=502,
        )

    # Build target URL — preserve the `/api/` prefix from the route so the
    # backend sees its own `/api/...` routes without needing any path-
    # rewriting middleware.  The route captures `path` after `/api/`, so we
    # explicitly re-add `/api/` here.
    target = f"{backend_url}/api/{path}"
    if request.query_string:
        target += f"?{request.query_string}"

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
        msg = f"{ts}:{request.method}:/api/{path}"
        if request.query_string:
            msg += f"?{request.query_string}"
        msg += f":{body_hash}"
        sig = _hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        headers["X-KiroCrew-Proxy"] = f"{ts}:{sig}"
    except OSError as exc:
        logger.warning("Failed to read app secret for %s: %s", name, exc)
        return web.json_response(
            {"error": "proxy auth failed: cannot read app secret"}, status=502,
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
                url=target,
                headers=headers,
                data=body,
                timeout=timeout,
                allow_redirects=False,
            ) as upstream:
                # Stream response back
                resp = web.StreamResponse(
                    status=upstream.status,
                    headers={
                        k: v for k, v in upstream.headers.items()
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
            {"error": "backend unreachable"}, status=502,
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
        sel().log_api_access(caller="dashboard", operation="app_migrate_cleanup", outcome="failed", resources=name, error=result.error)
        return web.json_response(result.to_dict(), status=status)
    sel().log_api_access(caller="dashboard", operation="app_migrate_cleanup", outcome="completed", resources=name)
    return web.json_response(result.to_dict())


async def handle_registries(request: web.Request) -> web.Response:
    """GET/PUT /api/apps/registries — manage external federated registries."""
    if request.method == "GET":
        config = KiroCrewConfig.load()
        registries = [
            {"name": r.name, "repo": r.repo, "branch": r.branch}
            for r in config.registries
        ]
        sel().log_api_access(
            caller="dashboard",
            operation="registries.read",
            outcome="success",
            resources=f"count={len(registries)}",
        )
        return web.json_response({"registries": registries})

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
    for entry in entries:
        if not isinstance(entry, dict):
            return _deny("each registry must be an object")
        repo = str(entry.get("repo", "")).strip()
        if not repo:
            return _deny("repo is required")
        if not re.match(r"^[A-Za-z0-9_\-]+$", repo):
            return _deny(f"invalid repo name: {repo!r}", f"repo={repo}")
        if repo in _blocked_repos:
            return _deny(
                f"{repo!r} is the core registry — no need to add it", f"blocked_repo={repo}"
            )
        name = str(entry.get("name", "")).strip() or repo
        if not re.match(r"^[A-Za-z0-9_\-. ]+$", name):
            return _deny(f"invalid registry name: {name!r}", f"name={name}")
        branch = str(entry.get("branch", "mainline")).strip() or "mainline"
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_\-./]*$", branch) or ".." in branch:
            return _deny(f"invalid branch name: {branch!r}", f"branch={branch}")
        validated.append({"name": name, "repo": repo, "branch": branch})

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
        return web.json_response(
            {"error": f"cannot read config: {exc}"}, status=500
        )
    data["registries"] = validated
    cfg.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(cfg, json.dumps(data, indent=2) + "\n")

    sel().log_api_access(
        caller="dashboard",
        operation="registries.update",
        outcome="success",
        resources=f"count={len(validated)} repos={','.join(r['repo'] for r in validated)}",
    )
    return web.json_response({"ok": True, "registries": validated})


def register_app_routes(app: web.Application) -> None:
    """Register all app management routes on an aiohttp Application."""
    # (moved to top-level)

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
    app.router.add_get("/api/apps/blob", handle_blob_proxy)
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
    app.router.add_delete("/api/apps/{name}/migrate-cleanup", handle_migrate_cleanup)
    app.router.add_get("/apps/{name}/ui/{path:.*}", handle_app_ui_file)
    # Reverse proxy: dashboard app UI → app backend (same-origin, avoids CORS)
    app.router.add_route("*", "/apps/{name}/api/{path:.*}", handle_app_api_proxy)

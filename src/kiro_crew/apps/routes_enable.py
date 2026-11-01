"""Switching an installed app on and off, and opening it.

``handle_enable_app`` and ``handle_disable_app`` with the built-in service sync they
drive and the client-install platform check (``_client_install_manifest``), and
``handle_open_app`` with the desktop environment its ``openCommand`` launcher keeps.

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.apps.backend import start_app_backend, stop_app_backend
from kiro_crew.apps.builtins import BUILTIN_NAMES
from kiro_crew.apps.dependencies import resolve_dependencies as _resolve_deps
from kiro_crew.apps.event_bus import build_broadcast_fn
from kiro_crew.apps.execution import app_execution_denied
from kiro_crew.apps.hooks_integration import on_app_enable

# Aliased to keep `routes._run_lifecycle_script` patchable, which several tests rely on.
from kiro_crew.apps.lifecycle_scripts import run_lifecycle_script as _run_lifecycle_script
from kiro_crew.apps.manager import (
    app_enabled_state,
    app_lifecycle_lock,
    apps_dir,
    disable_app,
    enable_app,
    get_app,
)
from kiro_crew.apps.manifest import Dependencies, PlatformConfig
from kiro_crew.apps.point_grants import point_grant_families
from kiro_crew.apps.registry import minimal_env
from kiro_crew.apps.routes_lifecycle import (
    _deregister_app_off_loop,
    _redact_warning,
    _refuse_while_startup_hook_runs,
    _register_app_off_loop,
    _unregister_notification_channels,
)
from kiro_crew.apps.spawn_sdk import build_spawn_impl
from kiro_crew.apps.teardown import teardown_app_runtime
from kiro_crew.config.loader import ConfigReadError, config_path, update_config_locked
from kiro_crew.executors import subprocess_executor
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    scrub_env,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")


#: Variables an ``openCommand`` launcher needs in order to reach the running
#: desktop session, copied through on top of the :func:`minimal_env` allowlist
#: (which has none of them). Every name here is a LOCATION HINT -- a display
#: number, an X authority file path, a bus address, a session flavour -- so
#: copying them widens what the child can FIND, never what it can
#: authenticate as. Enumerated rather than pattern-matched: a prefix rule over
#: the parent environment is how a credential reaches an app-authored shell by
#: accident. ``XDG_RUNTIME_DIR`` is absent because the allowlist already
#: carries it.
#:
#: ``DBUS_SESSION_BUS_ADDRESS`` belongs here, and the child KEEPS it. Two
#: things to know about that.
#:
#: This list decides it, not the sandbox. :func:`handle_open_app` wraps with
#: ``wrap_argv`` + ``cgroup_scope_argv`` and spawns with an explicit ``env``
#: built from ``scrub_env(minimal_env())`` plus these keys. The standard-mode
#: credential scrub names neither locator, and ``cgroup_scope_argv`` only
#: prepends the ``systemd-run`` wrapper. The restore-then-drop that
#: ``sandbox._CGROUP_SCOPE_BUS_ENV_KEYS`` describes -- both locators put back
#: after the scrub for ``systemd-run``'s own use, then removed again INSIDE
#: the scope with an ``env -u`` shim -- happens only in
#: ``sandbox.sandboxed_spawn_argv``, and this path does not call it. So the
#: ``openCommand`` child holds ``XDG_RUNTIME_DIR`` (from the allowlist) and
#: ``DBUS_SESSION_BUS_ADDRESS`` (from here) in every sandbox mode. The
#: positive control in ``test/test_s29_spawn_env_regression.py`` pins the env
#: and ``test/test_open_command_bus_locators.py`` pins the argv chain; change
#: this comment and those tests together.
#:
#: Dropping the address alone would close no door. libdbus falls back to
#: ``$XDG_RUNTIME_DIR/bus``, and ``XDG_RUNTIME_DIR`` is in ``minimal_env``'s
#: allowlist because it is also where the Wayland socket lives -- so removing
#: it to close the fallback would break every Wayland launch, which is the
#: legitimate use this endpoint exists for. Whether a standard-mode
#: ``openCommand`` child should hold a live user-bus address at all is a
#: sandbox question, and not one this list settles.
_OPEN_COMMAND_DESKTOP_ENV_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
)


# ---------------------------------------------------------------------------
# Builtin app helpers — sync config.json and stop/start live services
# ---------------------------------------------------------------------------


# Maps builtin app names to their config.json key and dashboard state
# restart callback attribute.  Only apps with a live gateway service
# (not just metadata) need entries here.  Empty in the open-source build —
# no bundled builtin ships a live gateway service.
_BUILTIN_SERVICE_APPS: dict[str, tuple[str, str]] = {}


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
        # nosemgrep: python.flask.security.audit.directly-returned-format-string.directly-returned-format-string -- the warning text goes into a JSON response and is never rendered as HTML
        return f"restart returned: {result}"
    except Exception as exc:
        logger.warning("Builtin service restart failed for %s: %s", name, exc)
        return f"restart failed: {exc}"


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
    # Dashboard-only. ``_app_owns_path`` grants an app token its own
    # ``/api/apps/{name}/**`` namespace, and ``disable_app`` only flips
    # ``enabled`` -- the token stays valid. Enabling is the user's consent
    # moment: ``app_can_manage_session_approvals`` reads ``enabled`` as the
    # live grant, and an update that widens the grant leaves the app disabled
    # precisely so the user re-enables it deliberately. An app that could POST
    # its own enable route would turn both into a formality, so refuse app
    # identities outright (mirrors ``handle_uninstall_preview``).
    if request.get("app"):
        sel().log_api_access(
            caller=request.get("app", ""),
            operation="app_enable_forbidden",
            outcome="denied",
            source="app_routes",
            resources=request.path,
            error="app token cannot enable an app",
        )
        return web.json_response(
            {
                "error": "app tokens cannot enable apps",
                "code": "app_token_forbidden",
            },
            status=403,
        )

    # local import: avoids a circular import with dashboard.handlers
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "app_enable")
    if denied is not None:
        return denied

    name = request.match_info["name"]
    info = await asyncio.to_thread(get_app, name)
    if not info:
        return web.json_response({"error": f"app {name!r} not installed"}, status=404)

    body: dict[str, Any] = {}
    if request.can_read_body:
        try:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
        except (json.JSONDecodeError, web.HTTPBadRequest, UnicodeDecodeError, LookupError):
            # ``request.json()`` decodes the body with the declared charset
            # before parsing: a bad charset or invalid bytes must land on the
            # same "no body" path as malformed JSON, not a 500.
            pass
    session_approval_consent = body.get("sessionApprovalConsent") is True
    # The entries the owner was shown, as {"api": [...], "events": [...], ...}.
    # Anything else approves nothing: a bare flag cannot say what was seen.
    raw_grants = body.get("grantsConsent")
    grants_consent: dict[str, list[str]] | None = None
    if isinstance(raw_grants, dict):
        grants_consent = {
            family: [e for e in raw_grants.get(family) or [] if isinstance(e, str)]
            for family in ("api", "events", *point_grant_families())
            if isinstance(raw_grants.get(family) or [], list)
        }
    if session_approval_consent or grants_consent is not None:
        # Consent hands an app control of the OWNER's sessions, so only the
        # owner can give it. Same gate the other machine-global mutations use.
        # Approving held-back api/events entries widens the app the same way.
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        denied = await require_owner_dashboard_request(request, "app_enable_session_consent")
        if denied is not None:
            return denied

    resources = info.get("resources", "gateway")
    manifest = info.get("manifest", {})
    on_enable = (manifest.get("setup") or {}).get("onEnable", "")
    # The record carries the timeout as `int()` read it, the form the signature
    # covers; the 1..300 bound is applied (and a clamp logged) here, where the script
    # runs under the app lifecycle lock. A local import keeps this helper out of the
    # names the routes facade serves from this module.
    from kiro_crew.apps.manifest import lifecycle_timeout_seconds

    enable_timeout = lifecycle_timeout_seconds(
        (manifest.get("setup") or {}).get("onEnableTimeout"),
        app_name=name,
        field_name="onEnableTimeout",
    )

    # Per-app lifecycle lock: enable mutates metadata, registers resources,
    # and starts the backend — must not interleave with a concurrent
    # install/update/uninstall of the same app (e.g. enabling while an
    # off-loop uninstall is deleting the app directory).
    async with app_lifecycle_lock(name):
        # A re-enable repeats every step but the Python hooks: the flag does not prove
        # onEnable ran (a file-only CLI enable skips it), while hook_reconcile loads hooks.
        was_enabled = await asyncio.to_thread(app_enabled_state, name) is True
        result = await asyncio.to_thread(
            enable_app,
            name,
            session_approval_consent=session_approval_consent,
            grants_consent=grants_consent,
        )
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
                await asyncio.to_thread(disable_app, name)
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
            hooks_result = (
                None
                if was_enabled
                else await on_app_enable(
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
                # ``handle_app_uninstall`` (routes.py), which already nests them that way
                # for the same reason. Verified across the tree: 14 functions take
                # ``app_lifecycle_lock`` and none of them is reachable from inside a
                # config-lock block, so the reverse order does not exist.
                #
                # Call-time import for the layering reason documented at the
                # ``_get_config_lock`` import in routes.py: ``apps`` sits below
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
    # local import: avoids a circular import with dashboard.handlers
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "app_disable")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    info = await asyncio.to_thread(get_app, name)
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
        # Teardown decides whether app code may run from `enabled`; read it under the lock.
        if (enabled := await asyncio.to_thread(app_enabled_state, name)) is not None:
            info = {**info, "enabled": enabled}

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

        result = await asyncio.to_thread(disable_app, name)
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
                # ``handle_app_uninstall`` (routes.py), which already nests them that way
                # for the same reason. Verified across the tree: 14 functions take
                # ``app_lifecycle_lock`` and none of them is reachable from inside a
                # config-lock block, so the reverse order does not exist.
                #
                # Call-time import for the layering reason documented at the
                # ``_get_config_lock`` import in routes.py: ``apps`` sits below
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
    # local import: avoids a circular import with dashboard.handlers
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    owner_denied = await require_owner_dashboard_request(request, "app_open")
    if owner_denied is not None:
        return owner_denied
    name = request.match_info["name"]
    info = await asyncio.to_thread(get_app, name)
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
        # The manifest's own shell string runs here, and an installed app is
        # untrusted content, so this child gets the same ALLOWLIST the install
        # and build commands from that same manifest get. An allowlist rather
        # than a denied-key scrub because the set to withhold is open-ended: the
        # gateway's own model credential is not a channel token and is not named
        # by any scrub list, so a subtract-the-known-bad environment handed it
        # straight to the app.
        #
        # The allowlist alone cannot launch a desktop app -- it carries no
        # display, authority-file or bus address -- and this endpoint only
        # reaches the spawn on a host that HAS a display, so the location hints
        # in ``_OPEN_COMMAND_DESKTOP_ENV_KEYS`` are copied on top, each only
        # when the parent actually defines it.
        # The allowlist is shared with the install and build commands, which run
        # git against the owner's own repositories, so it carries the SSH agent
        # socket. A launcher does not need it, and an app-authored shell holding
        # it authenticates as the operator wherever their keys reach. ``scrub_env``
        # takes it back out, together with the AWS secret/session pair, the GPG
        # home and the askpass hook.
        launch_env = scrub_env(minimal_env())
        for desktop_key in _OPEN_COMMAND_DESKTOP_ENV_KEYS:
            desktop_value = os.environ.get(desktop_key)
            if desktop_value is not None:
                launch_env[desktop_key] = desktop_value
        proc = await create_subprocess_limited(
            *sandboxed_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=launch_env,
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

"""Hooks Integration — wires the hooks system into the gateway lifecycle.

This module provides the glue functions that connect:
- RouteRegistry into the enable/disable flow
- LifecycleDispatcher into gateway startup/shutdown
- CronSDK cleanup into disable/uninstall

These functions are called from routes.py and server.py at the appropriate
lifecycle points. An app that declares no hooks is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.backend import spawned_backend_names, stop_app_backend
from kiro_crew.apps.bridges import (
    disarm_app_crons_for_execution,
    register_app_crons_with_service,
)
from kiro_crew.apps.context import AppContext, build_app_context
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.execution import (
    app_execution_denied,
    shipped_builtin_app_root,
)
from kiro_crew.apps.job_routes import register_job_routes
from kiro_crew.apps.job_sdk import forget_sdk, get_sdk, reconcile_all, register_sdk
from kiro_crew.apps.lifecycle import LifecycleDispatcher
from kiro_crew.apps.manager import app_dir, list_apps
from kiro_crew.apps.module_loader import clear_all_shutdown_callables
from kiro_crew.apps.route_registry import RouteRegistry
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Module-level singletons (initialized at gateway startup)
_route_registry: RouteRegistry | None = None
_lifecycle_dispatcher: LifecycleDispatcher | None = None

# Last hook-wiring health per app, for apps whose hooks did NOT come up clean.
# ``AppHealthStatus`` lives on the AppContext, which both wiring paths drop as
# soon as they finish, so the reason a hook failed had no reader on the startup
# path -- an app that failed to load was indistinguishable from one that was
# never installed. Published here so ``GET /api/apps`` can report it under the
# same ``hooks.health_status`` spelling the enable response already uses.
_hook_health: dict[str, dict[str, Any]] = {}

# The ONE source of truth for "which app's backend hooks does this gateway
# currently have loaded, and against what on-disk identity". Written by whichever
# path drives the lifecycle -- the dashboard enable/disable handlers, the boot
# startup pass, AND the out-of-process hook reconciler (apps/hook_reconcile.py)
# -- so none of them can desync from the others. The reconciler reads it to
# decide whether disk has drifted from what is loaded; because the dashboard
# handlers update it too, a dashboard-driven enable/disable leaves nothing for
# the reconciler to "re-fix", which is what stops it from double-running an
# app's on_startup/on_shutdown right after an in-process handler already did
# (see the design note on shared bookkeeping). An entry present = hooks loaded
# under that signature; absent = not loaded.
_loaded_hook_signatures: dict[str, tuple[Any, ...]] = {}

# Retained manifest of each loaded app, keyed by name, recorded alongside the
# signature. Its purpose is the uninstall case: once an app's files are gone from
# disk its on_shutdown cannot be resolved, so the reconciler falls back to this
# retained manifest to stop the code it actually loaded (see loaded_hook_manifest).
_loaded_hook_manifests: dict[str, dict[str, Any]] = {}


def hook_signature(app_info: dict[str, Any]) -> tuple[Any, ...]:
    """The hook-identity signature all lifecycle drivers agree on.

    Folds ONLY hook-relevant inputs so a metadata write that does not touch hook
    code never forces a reload: the app version, a digest of the declared hook
    source files' (path, mtime, size), and the ``.app_secret`` mtime (a reinstall
    rotates it, and the live module must pick up the new secret). Deliberately
    EXCLUDES two things:

    * ``installed.json`` mtime -- a dashboard permissions/config edit rewrites
      that file without changing any hook, and reloading a healthy running app on
      such an edit would discard its in-flight hook state for nothing.
    * the ``enabled`` flag -- enablement is an ORTHOGONAL axis the reconciler
      already checks separately (it disables a loaded app that turned off and
      loads an enabled one), so folding it in here caused a spurious mismatch: a
      denied/disabled path records a signature computed from an ``enabled=False``
      copy, then the next poll (which re-reads ``enabled=True`` from disk) sees a
      different signature and runs a needless shutdown/startup cycle against the
      no-op it was promised. The signature answers "did the hook CODE change",
      not "is the app on".

    Blocking: it ``stat``s the declared hook files and ``.app_secret``. On the
    gateway event loop (the dashboard enable/disable handlers, the boot pass) it
    must be reached via :func:`compute_hook_signature`, which offloads it; the
    reconcile loop runs it inside its own ``to_thread`` step. Kept here, beside
    the registry it feeds, so every driver computes the identical value.
    Import-light and defensive: unreadable files contribute their path alone (so
    a vanished/relocated hook still perturbs the digest), never an exception.
    """
    name = app_info.get("name", "")
    manifest = app_info.get("manifest", {}) or {}
    hooks = manifest.get("backend", {}).get("hooks", {}) or {}
    rels = sorted(
        {
            spec.split(":", 1)[0].replace(".", "/") + ".py"
            for key in ("on_startup", "on_shutdown", "routes")
            if (spec := hooks.get(key, ""))
            if spec.split(":", 1)[0]
        }
    )
    root = app_dir(name)
    mask = (1 << 63) - 1
    digest = 0
    for rel in rels:
        try:
            st = (root / rel).stat()
            digest ^= hash((rel, st.st_mtime_ns, st.st_size)) & mask
        except OSError:
            digest ^= hash((rel, 0, 0)) & mask
    try:
        secret_mtime = (root / ".app_secret").stat().st_mtime_ns
    except OSError:
        secret_mtime = 0
    return (
        str(app_info.get("version", "")),
        digest,
        secret_mtime,
    )


async def compute_hook_signature(app_info: dict[str, Any]) -> tuple[Any, ...]:
    """``hook_signature`` off the event loop.

    ``hook_signature`` stats files, so every caller that runs on the gateway
    event loop (the dashboard enable/disable handlers, the boot pass) must reach
    it through here -- a synchronous stat storm on a slow/network data home would
    otherwise stall the dashboard and the liveness heartbeat
    (no-blocking-call-on-event-loop).
    """
    return await asyncio.to_thread(hook_signature, app_info)


async def record_loaded_hook_signature(app_name: str, app_info: dict[str, Any]) -> None:
    """Mark an app's hooks loaded under its current on-disk signature.

    Called by every path that successfully wires an app's hooks -- the dashboard
    enable handler, the boot startup pass, and the reconciler -- so the shared
    registry reflects reality regardless of who did the loading. Async because it
    computes the signature off the event loop (see ``compute_hook_signature``).

    Also retains the loaded app's ``manifest`` (the hooks half is what matters):
    if the app is later UNINSTALLED between reconciler ticks, its files are gone
    and its ``on_shutdown`` can no longer be resolved from disk -- yet a
    background task its ``on_startup`` spawned is still live in the gateway after
    uninstall removed execution trust. The reconciler uses this retained manifest
    to run ``on_shutdown`` against the code that was ACTUALLY loaded, so that
    residual third-party work is stopped rather than orphaned.
    """
    _loaded_hook_signatures[app_name] = await compute_hook_signature(app_info)
    _loaded_hook_manifests[app_name] = app_info.get("manifest", {}) or {}


async def record_hook_antichurn_signature(app_name: str, app_info: dict[str, Any]) -> None:
    """Record ONLY the on-disk signature, with NO retained manifest.

    For a path that did NOT healthily load the app's hooks into the gateway but
    must still stop the out-of-process reconciler from re-running the whole
    enable path every tick -- specifically the execution-DENIED enable, where the
    gateway refuses to run the hooks at all. Recording the signature makes the
    reconciler see no drift (so it retries only when the on-disk hooks actually
    change, e.g. the admission grant is later added), while deliberately NOT
    retaining a manifest: nothing of the app's was ever started, so a later
    teardown must run NO ``on_shutdown`` for it. ``_disable_loaded`` treats the
    absent manifest as "no app code to stop" and skips app hooks -- without this
    split a denied app would look loaded and its shutdown-only code could execute
    on the next disable/uninstall (a shutdown-only execution vector).
    """
    _loaded_hook_signatures[app_name] = await compute_hook_signature(app_info)
    _loaded_hook_manifests.pop(app_name, None)


def clear_loaded_hook_signature(app_name: str) -> None:
    """Forget an app's loaded-hooks record (disable / teardown / uninstall)."""
    _loaded_hook_signatures.pop(app_name, None)
    _loaded_hook_manifests.pop(app_name, None)


def loaded_hook_signature(app_name: str) -> tuple[Any, ...] | None:
    """The signature an app's hooks are currently loaded under, or None."""
    return _loaded_hook_signatures.get(app_name)


def loaded_hook_manifest(app_name: str) -> dict[str, Any] | None:
    """The manifest an app's currently-loaded hooks were wired from, or None.

    Retained at load time so an UNINSTALLED app -- whose files are gone from disk
    -- can still have its ``on_shutdown`` resolved and run against the code that
    was actually loaded, stopping residual third-party background work after
    uninstall removes execution trust.
    """
    return _loaded_hook_manifests.get(app_name)


def loaded_hook_apps() -> list[str]:
    """Every app name the gateway currently has hooks loaded for."""
    return list(_loaded_hook_signatures)


def manifest_declares_hooks(app_info: dict[str, Any]) -> bool:
    """Whether an app's manifest declares any backend hook."""
    return bool((app_info.get("manifest", {}) or {}).get("backend", {}).get("hooks", {}))


def hook_enable_denied(app_name: str) -> str | None:
    """Non-empty reason string when the gateway must NOT run this app's hooks.

    Wraps ``app_execution_denied`` with the same action/root the enable and boot
    paths use, so the reconciler asks the identical admission question they do.
    The reconciler re-checks this UNDER the per-app lifecycle lock immediately
    before an enable: a trust withdrawal clears the loaded-signature during its
    teardown, and without this re-check a lock-free reconciler could observe the
    still-``enabled`` metadata and re-run the app's startup hooks/routes after
    trust was revoked. Returning the reason here keeps that resurrection from
    happening.
    """
    return app_execution_denied(
        app_name,
        action="hook_reconcile_register",
        app_root=_app_hook_root(app_name),
        caller="gateway",
    )


def _publish_hook_health(app_name: str, ctx: AppContext) -> dict[str, Any] | None:
    """Record (or clear) one app's hook-wiring health and return the snapshot.

    Both wiring paths funnel through here so they cannot drift: whatever
    ``register_app_routes`` and the lifecycle dispatcher marked on the context is
    what an operator reads back. A healthy wire-up clears any earlier entry, so a
    fixed app stops reporting a stale failure after a re-enable.
    """
    if ctx.health.status == "healthy":
        _hook_health.pop(app_name, None)
        return None
    snapshot = ctx.health.to_dict()
    _hook_health[app_name] = snapshot
    return snapshot


def clear_hook_health(app_name: str) -> None:
    """Forget an app's recorded hook health (disable / teardown)."""
    _hook_health.pop(app_name, None)


def get_all_hook_health() -> dict[str, dict[str, Any]]:
    """Return every recorded non-healthy hook-wiring snapshot, by app name."""
    return {name: dict(snapshot) for name, snapshot in _hook_health.items()}


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

    # BEFORE the catch-all, not after: aiohttp matches in registration order, so
    # /api/apps/{app_name}/{path:.*} would otherwise swallow every _jobs request
    # and answer it from the app's own dispatch table.
    register_job_routes(app)

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


async def stop_retained_startup_hooks(app_name: str, *, bounded: bool) -> bool:
    """Wait for retained startup execution, failing closed on ownership errors."""
    if _lifecycle_dispatcher is None:
        return True
    try:
        return await _lifecycle_dispatcher.stop_detached_startup_hooks(app_name, bounded=bounded)
    except Exception:  # noqa: BLE001 - destructive lifecycle work must fail closed
        logger.exception("Could not verify detached startup-hook cleanup for %s", app_name)
        return False


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
    ctx = build_app_context(
        app_name=name,
        data_dir=data_path,
        permissions=permissions,
        cron_service=cron_service,
        broadcast_fn=broadcast_fn,
        spawn_impl=spawn_impl,
        app_config=manifest.get("extra", {}),
    )
    # The shared _jobs routes are mounted once for every app and resolve the app
    # from the URL, so they need a name -> SDK lookup. Publishing happens here,
    # in the gateway wiring, rather than inside build_app_context, so building a
    # context in a test does not put an SDK behind the live routes.
    if ctx.job is not None:
        register_sdk(ctx.job)
    return ctx


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
        # Record the on-disk signature even though nothing was loaded: an
        # execution-denied app is enabled with hooks the gateway will not run, so
        # WITHOUT this the reconciler would see no record every tick and re-run
        # on_app_enable forever (a cron-store mutation + SEL audit write + route
        # dereg each time) for an app that can never load. Record the SIGNATURE
        # ONLY (no manifest): the reconciler then retries only when the on-disk
        # hooks change (e.g. the admission grant is later added and a
        # reinstall/enable bumps the signature), and because no manifest is
        # retained a later disable/uninstall runs NO ``on_shutdown`` for this app
        # -- nothing of its was ever started, so recording a full loaded record
        # here would be a shutdown-only code-execution vector.
        await record_hook_antichurn_signature(app_name, app_info)
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
        registered = await _route_registry.register_app_routes(app_name, app_root, routes_hook, ctx)
        if registered:
            result["hooks_routes"] = registered

    # Invoke on_startup hook if declared
    startup_hook = hooks.get("on_startup", "")
    if startup_hook and _lifecycle_dispatcher:
        success = await _lifecycle_dispatcher._invoke(app_name, startup_hook, ctx, phase="startup")
        result["hooks_startup"] = "ok" if success else "failed"

    # Reconcile this app's job records now that its startup hook has registered
    # the runners. The boot-time pass runs once, after the enable LOOP, so an app
    # enabled later in the gateway's life never got one -- and reconciliation is
    # only decidable once the runners are known, since "no runner for this kind"
    # is one of the two outcomes it reports. Without this a record left
    # non-terminal by a previous process stayed that way until the next restart,
    # and `list_active` kept reporting work that had already stopped, which is the
    # exact symptom this SDK exists to remove.
    #
    # Scoped to the hooks path on purpose: an app with no backend hooks returned
    # above, and it can register no runners, so there is nothing here to decide
    # against -- the boot-time pass already owns that case.
    if getattr(ctx, "job", None) is not None:
        try:
            flipped = await asyncio.to_thread(ctx.job.reconcile)
            if flipped:
                result["job_reconcile"] = f"resolved {flipped} interrupted run(s)"
        except Exception as exc:  # noqa: BLE001 - enable must not fail on this
            logger.warning(
                "App %s: job reconciliation after enable did not complete: %s", app_name, exc
            )
            result["job_reconcile"] = "failed: stale run records may remain"

    # Report health status
    health_snapshot = _publish_hook_health(app_name, ctx)
    if health_snapshot:
        result["health_status"] = health_snapshot

    # Shared source of truth: record that this app's hooks are now loaded under
    # its current on-disk signature, so the out-of-process reconciler sees no
    # drift for an enable THIS handler just performed and does not re-run the
    # startup hook on its next tick. ``_publish_hook_health`` returns a snapshot
    # exactly when the context is NOT healthy and ``None`` when it is, so gate on
    # that return rather than re-reading ``ctx.health`` -- the context object is
    # the single source for both, and reusing the already-computed result avoids
    # assuming a ``health`` attribute shape on every caller's context.
    #
    # Record ONLY when the wiring came up healthy. A route or startup hook that
    # failed to import (or a detached/timed-out ``on_startup``) must stay
    # UN-recorded so the reconciler re-attempts it on its next tick and picks the
    # app up once a transient failure clears -- the poll-retry-after-recovery the
    # reconciler exists to provide. Freezing a degraded import as the current
    # loaded state would strand it until its code changed. A prior record is
    # cleared on failure for the same reason.
    if health_snapshot is None:
        # Cache the declared on_shutdown callable now (files still present) so a
        # later CLI uninstall can stop background work this app spawned -- even a
        # ROUTES-ONLY app with no on_startup, and even when on_shutdown lives in a
        # separate module. Done on healthy wiring independently of on_startup
        # presence/success (dispatcher no-ops when no on_shutdown is declared).
        if _lifecycle_dispatcher:
            await _lifecycle_dispatcher.cache_shutdown_for(app_info)
        await record_loaded_hook_signature(app_name, app_info)
    else:
        # Degraded wiring (e.g. a route import failed) that nonetheless ran a
        # successful ``on_startup`` -- or a route hook -- may have spawned a
        # background child that OUTLIVES the startup call (the wrapper returns, the
        # child keeps running). Clearing the signature alone arms a reconciler
        # RE-RUN, so ``on_startup`` fires again and a fresh child stacks on the
        # live one every poll until exhaustion. So run the app's SHUTDOWN lifecycle
        # first -- ``on_shutdown`` is what actually stops such a spawned child --
        # and also settle any detached startup hooks. Clear the signature (arming a
        # retry) ONLY when BOTH confirm clean. If either is unconfirmed the child
        # may be live, so RETAIN the loaded signature instead: a retained record
        # keeps the reconciler from re-running and duplicating. Best-effort: a
        # failure here must not turn a degraded enable into a crash.
        shutdown_ok = False
        stopped = False
        try:
            if _lifecycle_dispatcher:
                shutdown_ok = await _lifecycle_dispatcher.dispatch_disable(app_info)
            stopped = await stop_app_startup_hooks(app_name, bounded=True)
        except Exception:  # noqa: BLE001 - teardown must not fail the enable
            logger.exception(
                "App %s: running the shutdown lifecycle before degraded-retry clear "
                "failed; retaining the loaded signature so the reconciler does not "
                "re-run and stack a duplicate worker",
                app_name,
            )
        if shutdown_ok and stopped:
            clear_loaded_hook_signature(app_name)
        else:
            # Teardown did not confirm the spawned work stopped: keep the loaded
            # record so the poll does not spawn another. record is the retain (it
            # also carries the manifest the retained-startup teardown path needs).
            await record_loaded_hook_signature(app_name, app_info)

    sel().log_api_access(
        caller="gateway",
        operation="app_hooks_enable",
        outcome="completed",
        resources=app_name,
    )
    return result


async def stop_app_startup_hooks(app_name: str, *, bounded: bool = False) -> bool:
    """Prove retained startup ownership clear before teardown mutates state."""
    if not _lifecycle_dispatcher:
        return True
    return await _lifecycle_dispatcher.stop_detached_startup_hooks(app_name, bounded=bounded)


async def _cleanup_app_jobs(app_name: str, result: dict[str, Any]) -> None:
    """Stop and drop an app's durable job runs, mirroring the cron contract:
    idempotent, and a failure is REPORTED rather than crashing the disable.

    Signalling is all the SDK can do about the WORK -- a runner that never polls
    its handle within the deadline is reported -- and the RECORDS stay deleted:
    the SDK marks every live handle discarded under the same lock its guarded
    writer takes, so a worker returning mid-cleanup cannot write its record back,
    and a partial delete is reported rather than read as clean.

    Keyed off the REGISTRY, not the manifest grant. Gating this on
    ``permissions.get("jobs")`` meant revoking the grant and then disabling took
    the one path that skips it entirely: the SDK stays registered from the enable
    that DID have the grant, its workers keep executing, and the lookup entry is
    dropped afterwards -- so nothing can ever reach them again. The grant governs
    whether an app may START jobs; whether it HAS any running is a fact about the
    registry, and that is what teardown has to ask.

    A separate function so that grant-independence is testable on its own; it was
    unreachable while the logic sat inline behind the condition it must ignore.
    """
    job_sdk = get_sdk(app_name)
    if job_sdk is None:
        return
    try:
        cleanup = await job_sdk.remove_all_async()
        if not cleanup.is_clean:
            # Reported, not swallowed: a cleanup that left records behind OR left
            # app code executing must not read as clean.
            parts = []
            if cleanup.failed:
                parts.append(f"{cleanup.failed} run record(s) remain")
            if cleanup.still_running:
                parts.append(f"{cleanup.still_running} worker(s) still running")
            result["job_cleanup"] = f"partial: removed {cleanup.removed}, " + "; ".join(parts)
            sel().log_api_access(
                caller="gateway",
                operation="jobs.deregister",
                outcome="partial",
                resources=(
                    f"app={app_name} removed={cleanup.removed} "
                    f"failed={cleanup.failed} running={cleanup.still_running}"
                ),
            )
        elif cleanup.removed:
            result["job_cleanup"] = f"removed {cleanup.removed} run record(s)"
    except OSError as exc:
        logger.warning("App %s: job cleanup could not complete on disable: %s", app_name, exc)
        result["job_cleanup"] = "failed: run records may remain"
        sel().log_api_access(
            caller="gateway",
            operation="jobs.deregister",
            outcome="failed",
            resources=app_name,
            error=str(exc),
        )


async def on_app_disable(
    app_name: str,
    app_info: dict[str, Any],
    *,
    run_app_hooks: bool = True,
    bounded_startup_cleanup: bool = False,
    startup_stopped: bool | None = None,
) -> dict[str, Any]:
    """Called before an app is disabled — deregister routes and invoke shutdown hook.

    ``run_app_hooks=False`` skips the app's OWN ``on_shutdown`` hook while still
    doing everything the GATEWAY owns (route deregistration, cron cleanup). The
    caller passes it when there is no reason to believe the app is running: its
    shutdown hook is third-party code, and *starting* that code as part of
    withdrawing its permission to run would turn the security operation into an
    execution vector. Nothing that STOPS something is ever skipped by this flag.

    ``bounded_startup_cleanup`` distinguishes trust withdrawal from ordinary
    disable. Ordinary disable waits until an owned detached startup task exits;
    trust withdrawal stays bounded and reports residual execution as a hard
    failure so the grant remains in place for a retry. Shared teardown passes a
    pre-established ``startup_stopped`` result so this function cannot repeat the
    ownership wait after other teardown work has begun.
    """
    result: dict[str, Any] = {}
    manifest = app_info.get("manifest", {})
    backend = manifest.get("backend", {})
    hooks = backend.get("hooks", {})

    # A startup hook may have been detached after its readiness deadline. It is
    # still third-party code with a live AppContext, so disable/revocation must
    # stop it even if the current manifest no longer declares hooks. A resistant
    # task becomes a hard teardown failure; callers keep trust in place rather
    # than falsely claiming all app code stopped.
    if startup_stopped is None:
        startup_stopped = await stop_app_startup_hooks(app_name, bounded=bounded_startup_cleanup)
    if not startup_stopped:
        result["startup_cleanup"] = (
            "failed: detached startup hook is still running; teardown not started"
        )
        return result

    if hooks:
        sel().log_api_access(
            caller="gateway",
            operation="app_hooks_disable",
            outcome="started",
            resources=app_name,
        )

    # Invoke on_shutdown only after retained startup ownership is proven clear.
    # Trust withdrawal must stay bounded and must never overlap partially
    # initialized startup state with an unbounded third-party shutdown hook.
    shutdown_hook = hooks.get("on_shutdown", "")
    if shutdown_hook and _lifecycle_dispatcher and run_app_hooks and startup_stopped:
        success = await _lifecycle_dispatcher._invoke(
            app_name,
            shutdown_hook,
            _lifecycle_dispatcher._build_context(app_info),
            phase="shutdown",
        )
        result["hooks_shutdown"] = "ok" if success else "failed"

    # Deregister routes
    if _route_registry:
        _route_registry.deregister_app_routes(app_name)

    # A disabled app has no live hooks, so a recorded failure would linger as a
    # stale claim about an app that is no longer wired up at all.
    clear_hook_health(app_name)

    # Shared source of truth: the app's hooks are now torn down, so drop its
    # loaded-signature record. Reached only past the startup-ownership guard
    # above, so a teardown that did NOT settle (retained startup task) leaves the
    # record in place and the reconciler keeps retrying rather than treating the
    # app as unloaded.
    clear_loaded_hook_signature(app_name)

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
            except CronStoreUnreadable as exc:
                # Sibling class of CronStoreBusy, so it escaped the arm below
                # entirely and would CRASH the disable — the outcome the comment
                # above forbids. Reported rather than retried: an unreadable store
                # does not heal on its own.
                logger.warning(
                    "App %s: cron cleanup could not complete on disable — " "store unreadable: %s",
                    app_name,
                    exc,
                )
                result["cron_cleanup"] = "failed: cron store unreadable — jobs may still be enabled"
                sel().log_api_access(
                    caller="gateway",
                    operation="app_crons_deregister",
                    outcome="failed",
                    resources=app_name,
                    error=str(exc),
                )
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

    # Stop and drop this app's durable job runs. Keyed off the registry, not
    # the manifest grant -- see _cleanup_app_jobs for why that distinction is
    # load-bearing on a revoked grant.
    await _cleanup_app_jobs(app_name, result)

    # Drop the lookup entry unconditionally, for the same reason the cleanup
    # above ignores the grant: a revoked capability must not survive in the
    # registry, and a conditional forget would leave the SDK published for the
    # rest of the gateway's life. The route guard re-reads the manifest so it
    # would refuse anyway, but the registry must not disagree with it.
    forget_sdk(app_name)

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

    # list_apps() walks the apps dir (two file reads per app) — off the loop.
    installed = await asyncio.to_thread(list_apps)
    enabled = [a for a in installed if a.get("enabled")]
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
            # Same anti-churn record as on_app_enable's denied path: a hook-
            # declaring app the gateway refuses to run must be recorded so the
            # reconciler does not re-attempt it every tick. Record the SIGNATURE
            # ONLY (no manifest) so a later teardown runs no on_shutdown for an
            # app that never started (shutdown-only execution vector). Hookless
            # denied apps need no record (the reconciler ignores them).
            if manifest_declares_hooks(app_info):
                await record_hook_antichurn_signature(name, app_info)
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
            await _route_registry.register_app_routes(name, _app_hook_root(name), routes_hook, ctx)

        # Invoke on_startup hook (if declared)
        startup_hook = hooks.get("on_startup", "")
        if startup_hook and _lifecycle_dispatcher:
            success = await _lifecycle_dispatcher._invoke(name, startup_hook, ctx, phase="startup")
            if success:
                logger.info("Startup hook invoked for: %s", name)

        # Same publication as on_app_enable: the reason a hook failed must outlive
        # the context, or boot is the one path where it is collected and dropped.
        # No log line here on purpose: every site that marks the context degraded
        # already logs at ERROR itself (route_registry.py:142,151,159 and
        # lifecycle.py:352,396, plus the cancelled site via
        # _mark_cancelled_startup_residual at lifecycle.py:66), so an aggregate
        # would only re-log them, and only ever for this one caller.
        boot_health = _publish_hook_health(name, ctx)

        # Shared source of truth: record what boot loaded so the out-of-process
        # reconciler starts already agreeing with the gateway and does not
        # reimport every app on its first tick. Recorded for any hook-declaring
        # app the loop reached (it has already `continue`d past hookless apps),
        # covering routes-only apps as well as those with on_startup -- but ONLY
        # when the wiring came up healthy (``_publish_hook_health`` returns a
        # snapshot only when it did NOT). A boot-time import failure stays
        # un-recorded so the reconciler re-attempts it and picks the app up once a
        # transient failure clears, rather than freezing the degraded state as
        # current.
        if boot_health is None:
            # Cache the declared on_shutdown regardless of on_startup (routes-only
            # apps included; dispatcher no-ops when none is declared), so a later
            # CLI uninstall can stop background work this app spawned.
            if _lifecycle_dispatcher:
                await _lifecycle_dispatcher.cache_shutdown_for(app_info)
            await record_loaded_hook_signature(name, app_info)

    # AFTER the loop, deliberately: only here has every enabled app registered
    # its runners, so a run whose kind has no runner can be told apart from an
    # app that has simply not loaded yet. Reconciliation resolves runs that a
    # previous gateway process left mid-flight -- a run must never be left
    # `running` forever, and must never silently vanish.
    try:
        interrupted = await asyncio.to_thread(reconcile_all)
        if interrupted:
            logger.info("Startup: reconciled %d interrupted job run(s)", interrupted)
            sel().log_api_access(
                caller="gateway",
                operation="jobs.reconcile",
                outcome="completed",
                resources=f"interrupted={interrupted}",
            )
    except Exception as exc:  # noqa: BLE001 - boot must not fail on a bad run store
        logger.exception("Startup: job reconciliation failed")
        sel().log_api_access(
            caller="gateway",
            operation="jobs.reconcile",
            outcome="failed",
            resources="startup",
            error=str(exc),
        )


#: One shared deadline for the whole backend-stop sweep at gateway shutdown.
#: The sweep runs inside the gateway's cooperative shutdown, which
#: ``GRACEFUL_SHUTDOWN_SECS`` caps at 10s before the supervisor force-exits;
#: each individual stop can spend up to the 5s SIGTERM grace before its SIGKILL
#: escalation, so the stops run concurrently and this bound keeps the sweep as
#: a whole from consuming the budget the rest of cleanup still needs.
_BACKEND_STOP_BUDGET_SECS = 6.0


async def on_gateway_shutdown() -> None:
    """Called during gateway shutdown — invoke on_shutdown hooks for enabled
    apps, then stop the backend processes this gateway spawned.

    The backend stop is the load-bearing half. Spawned backends are child
    processes of the gateway: without an explicit stop they reparent to PID 1
    when the gateway exits and keep listening on their ports, so anything
    probing those ports reads a stopped gateway as "connected". The next boot
    reaps them only lazily (``_reap_stale_app_backends``), which does not help
    a gateway that stays down. Hooks run FIRST so an app's ``on_shutdown``
    still has its own backend alive.

    Stop targets come from the runtime tracking table
    (:func:`spawned_backend_names`), never from persisted ``enabled`` metadata:
    the metadata filter is wrong in both directions here (it would signal an
    ADOPTED externally-managed backend whose contract is to survive gateway
    exit, and it would miss a still-running child whose app was disabled
    cross-process, metadata-only). It also keeps :func:`stop_app_backend` — and
    its pidfile-record erasure — away from apps with nothing running, so a
    retained prior-generation orphan record stays recoverable by the next
    boot's stale-reap.
    """
    # list_apps() walks the apps dir (two file reads per app) — off the loop.
    installed = await asyncio.to_thread(list_apps)
    enabled = [a for a in installed if a.get("enabled")]

    try:
        if _lifecycle_dispatcher and enabled:
            invoked = await _lifecycle_dispatcher.dispatch_shutdown(enabled)
            if invoked:
                logger.info("Shutdown hooks invoked for: %s", ", ".join(invoked))
    finally:
        # The sweep MUST run even when hook dispatch hangs or raises: hooks are
        # third-party code, and dispatch awaits an invoked on_shutdown hook to
        # completion — so a wedged hook would hold this coroutine until the
        # gateway's graceful-shutdown deadline CANCELS it right here. Running
        # the sweep from the finally block means that cancellation still stops
        # the spawned backends (a task may keep awaiting during cleanup after
        # a single cancel request), instead of force-exiting with every one of
        # them orphaned — the exact defect this function exists to fix.
        await _stop_spawned_backends()

    # Shutdown hooks have run (they needed the cached on_shutdown callables while
    # the cache was still valid) and backends are stopped. Drop the whole shutdown
    # cache now: this sweep does not go through per-app unload_app_modules, so
    # without this a callable captured this generation would survive into an
    # in-process gateway restart and be selected to stop a NEWLY loaded worker.
    clear_all_shutdown_callables()


async def _stop_spawned_backends() -> None:
    """Stop every backend this gateway spawned, concurrently, under one budget.

    Deliberately NOT gated on ``_lifecycle_dispatcher`` or on the enabled list:
    backends are started by the boot path independently of the hooks system, so
    neither absence may leave them orphaned. The tracking-table read takes the
    module lock — off the loop like every other blocking step.
    """
    names = await asyncio.to_thread(spawned_backend_names)
    if not names:
        return

    # Each stop signals a process group and waits on it — blocking syscalls,
    # so offloaded to the subprocess executor. All stops start CONCURRENTLY
    # under ONE shared deadline: awaiting them serially would multiply the
    # per-app SIGTERM grace by the number of apps and blow the gateway's
    # cooperative shutdown budget, so the supervisor's force-exit would orphan
    # every backend the sweep had not reached yet.
    loop = asyncio.get_running_loop()
    stops = [loop.run_in_executor(subprocess_executor(), stop_app_backend, name) for name in names]
    gathered = asyncio.gather(*stops, return_exceptions=True)
    try:
        # The gather is SHIELDED so a deadline overrun (or a cancellation of
        # this coroutine) releases the caller WITHOUT cancelling the stops.
        # The executor is shared with the rest of shutdown, so a stop can
        # still be QUEUED when the deadline fires — cancelling it then would
        # drop it before stop_app_backend ever ran, and that backend would
        # never be signalled at all. A running stop's thread cannot be
        # interrupted anyway; the shield extends the same guarantee to the
        # queued ones, which keep running in the background.
        results = await asyncio.wait_for(
            asyncio.shield(gathered),
            timeout=_BACKEND_STOP_BUDGET_SECS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "app backend stop sweep did not finish within %.0fs at gateway "
            "shutdown; unfinished stops continue in the background: %s",
            _BACKEND_STOP_BUDGET_SECS,
            ", ".join(names),
        )
        return
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            logger.warning(
                "stopping backend for app %r on gateway shutdown failed: %s",
                name,
                result,
            )
        elif result:
            logger.info("Stopped app backend on gateway shutdown: %s", name)

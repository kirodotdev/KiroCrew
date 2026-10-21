"""Uninstalling an app: the uninstall, its preview, and the migrated-app cleanup.

``handle_uninstall_app`` and ``handle_migrate_cleanup`` over ``_run_uninstall``,
``handle_uninstall_preview``, and the bounded cron deregistration retry an uninstall uses
(``_deregister_crons_with_retry``).

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.apps.bridges import app_conversation_keys, deregister_app_crons_reporting_failures
from kiro_crew.apps.dependencies import clean_dependencies
from kiro_crew.apps.dependency_ledger import (
    canonical_dep_key,
    classify_and_clean_for_uninstall,
    classify_for_uninstall,
    declared_capability_keys,
)
from kiro_crew.apps.lifecycle_scripts import run_lifecycle_script as _run_lifecycle_script
from kiro_crew.apps.manager import (
    app_lifecycle_lock,
    get_app,
    trust_grant_removal_blocked,
    uninstall_app,
)
from kiro_crew.apps.registry import is_registry_source, registry_name_from_source
from kiro_crew.apps.routes_lifecycle import (
    _deregister_app_off_loop,
    _refuse_while_startup_hook_runs,
    _stop_backend_and_observe,
    _unregister_notification_channels,
)
from kiro_crew.apps.routes_proxy import invalidate_app_secret_cache
from kiro_crew.apps.teardown import forget_app_hooks
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")


_CRON_CLEANUP_ATTEMPTS = 3
_CRON_CLEANUP_BACKOFF_SECS = 0.5


async def _deregister_crons_with_retry(name: str, cron_service: Any) -> int:
    """Remove an app's cron jobs, retrying a contended store before giving up.

    ``deregister_app_crons_reporting_failures`` already spins on the store lock
    for a bounded window and raises :class:`CronStoreBusy` if it never wins. On
    the uninstall path that exception ABORTS the uninstall (a 409), so a single
    unlucky collision with a concurrent mutator would surface to the user as a
    failed uninstall. Retry the whole atomic removal a few times with a short
    backoff first: contention is transient, and each attempt is all-or-nothing,
    so a retry can never partially remove jobs. Re-raises ``CronStoreBusy`` if
    every attempt loses.

    The reporting variant is the one called, not the wrapper that answers ``0``:
    uninstall is irreversible from here, so it needs a store that failed to be
    told apart from an app that owned nothing. Only contention is retried; a
    write that could not land is not transient and is raised on the first try.
    """
    for attempt in range(1, _CRON_CLEANUP_ATTEMPTS + 1):
        try:
            return await deregister_app_crons_reporting_failures(name, cron_service)
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
    # Dashboard-only: ``_app_owns_path`` grants an app token its own
    # ``/api/apps/{name}/**`` namespace, but the classification below discloses
    # SIBLING app names (``shared[].usedBy`` / ``reason``), which an app must
    # not see. The confirm dialog is an operator surface, so refuse app tokens
    # outright.
    if request.get("app"):
        # SEL audit for the permission decision, matching the sibling app-token
        # deny paths of the app routes: an authorization denial that leaves no trail
        # is invisible to the audit log, so a repeated probe would be
        # unobservable.
        sel().log_api_access(
            caller=request.get("app", ""),
            operation="app_uninstall_preview_forbidden",
            outcome="denied",
            source="app_routes",
            resources=request.path,
            error="app token cannot preview uninstall",
        )
        return web.json_response(
            {
                "error": "app tokens cannot preview uninstall",
                "code": "app_token_forbidden",
            },
            status=403,
        )

    name = request.match_info["name"]
    # Registry read + ledger classification both touch disk (the ledger takes
    # a blocking file lock), so they run off-loop like the sibling handlers.
    # ``get_app`` can also WRITE: for a self-managed app with version drift it
    # rewrites ``installed.json``, and unsynchronized against a concurrent
    # uninstall that write can land after the deletion and recreate the file
    # as a ghost installation. Take the per-app lifecycle lock around it, the
    # same lock the uninstall sequence holds.
    async with app_lifecycle_lock(name):
        info = await asyncio.to_thread(get_app, name)
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
    dep_classification = await asyncio.to_thread(classify_for_uninstall, name, declared_deps)

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
                        if await asyncio.to_thread(ws_dir.is_dir):
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

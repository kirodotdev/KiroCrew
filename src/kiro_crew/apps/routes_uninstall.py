"""The uninstall preview, and the bounded cron deregistration retry an uninstall uses.

``handle_uninstall_preview`` and ``_deregister_crons_with_retry``. The uninstall itself
(``handle_uninstall_app``, ``_run_uninstall``) and the migrated-app cleanup stay in
``apps/routes.py``.

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.apps.bridges import deregister_app_crons_reporting_failures
from kiro_crew.apps.dependency_ledger import classify_for_uninstall, declared_capability_keys
from kiro_crew.apps.manager import app_lifecycle_lock, get_app
from kiro_crew.cron import CronStoreBusy
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

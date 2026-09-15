"""Dev Fleet — standalone aiohttp backend for KiroCrew feature worktrees.

Manages KiroCrew feature worktrees (git worktrees of the main repo) and their
isolated pod test instances. Runs as a subprocess spawned by the KiroCrew app
backend system (apps/backend.py). The gateway proxies /apps/dev-fleet/api/* to
this process with X-KiroCrew-Proxy HMAC signing; HMAC middleware validates
every request (except /health) fail-closed.

Routes (as seen by the backend after prefix stripping by gateway):
  GET  /api/fleet             -> lightweight worktree + pod list (polled)
  GET  /api/worktree?name=    -> lazy per-branch detail (pr/commits/disk)
  GET  /api/pod/logs?name=&n=
  GET  /api/run?id=           -> async run status + streamed output
  GET  /api/prune-candidates
  GET  /api/prune-status
  GET  /api/disk
  POST /api/sync              -> pull main + rebuild
  POST /api/worktree/remove {name, force?, discard_untracked_paths?}
  POST /api/prune-run {names, force_names?, discard_untracked_paths?}
  POST /api/pod/up   {name}
  POST /api/pod/down {name}
  POST /api/pod/restart {name}
  POST /api/pod/token {name}
  POST /api/pod/provision {name}  -> start async build, returns {run_id}
  POST /api/rebase  {name}
  GET  /api/health            -> {"status": "ok", "start_id": ...}  (restart handshake; proxied)
  GET  /health                -> same body, HMAC-exempt (gateway-internal liveness poll only)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from types import ModuleType

from aiohttp import web

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    http_api,
    live,
    pointer_broker,
    repository,
    runtime,
    worktree_ops,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import boot_platform

_COMPONENTS = (
    runtime,
    repository,
    live,
    fleet_state,
    worktree_ops,
    http_api,
)
_EXPORT_OWNERS = {name: component for component in _COMPONENTS for name in component.__all__}
if sum(len(component.__all__) for component in _COMPONENTS) != len(_EXPORT_OWNERS):
    raise RuntimeError("duplicate Dev Fleet compatibility export owner")


def __getattr__(name: str):
    """Resolve legacy server attributes from their canonical component owner."""
    try:
        owner = _EXPORT_OWNERS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    return getattr(owner, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORT_OWNERS})


class _CompatibilityModule(ModuleType):
    """Forward legacy attribute mutation to the canonical component owner."""

    def __setattr__(self, name: str, value: object) -> None:
        owner = _EXPORT_OWNERS.get(name)
        if owner is None:
            super().__setattr__(name, value)
        else:
            setattr(owner, name, value)

    def __delattr__(self, name: str) -> None:
        owner = _EXPORT_OWNERS.get(name)
        if owner is None:
            super().__delattr__(name)
        else:
            delattr(owner, name)


sys.modules[__name__].__class__ = _CompatibilityModule


# --- startup hook ---
async def dev_fleet_startup(app: web.Application) -> None:
    """Start the background fleet refresher on app startup."""
    # Full discovery here rather than at import: it reads config.json and stats
    # candidate directories, both of which would block the event loop on the
    # route-registration path. Shared with the gateway's in-gateway cutover route,
    # which runs the same chain lazily in ITS process (``repository`` owns it, so
    # the MAIN_REPO write and the AST ratchet's single-assignment guarantee stay in
    # one place).
    await repository.ensure_main_repo_discovered()
    # Resolve the node build toolchain here, on the executor, so no request
    # handler ever pays for the filesystem scan (NFS homes make it slow).
    await runtime._warm_build_path()
    if worktree_ops._background_tasks_disabled():
        return
    if worktree_ops._refresher_task is None or worktree_ops._refresher_task.done():
        worktree_ops._refresher_task = asyncio.create_task(worktree_ops._status_refresher())
    if worktree_ops._reaper_task is None or worktree_ops._reaper_task.done():
        worktree_ops._reaper_task = asyncio.create_task(worktree_ops._auto_prune_reaper())
    try:
        repository._repo()
    except repository.RepoUnavailable:
        # Same reason as the reaper: a configured-but-unusable path is truthy, and
        # warming would only raise into a task nobody awaits ("Task exception was
        # never retrieved"). The setup / discovery-error state is served from the
        # route instead.
        pass
    else:
        worktree_ops._warm_task = asyncio.create_task(fleet_state._fleet_refresh())


async def dev_fleet_cleanup(app: web.Application) -> None:
    """Cancel and await background tasks so a stopped runner leaves nothing behind."""
    # Close the admission window first: set the flag and snapshot _ACTIVE_RUNS
    # atomically under the admission lock.  The lock is held only for these two
    # fast dict operations — no I/O, no awaits — so it cannot stall any in-
    # flight handler or create done-callback deadlocks.  Once we drop the lock,
    # _SHUTDOWN_IN_PROGRESS is True and _start_run will refuse new registrations,
    # so the snapshot is complete: every run that could ever be in _ACTIVE_RUNS
    # is either already in `active_snapshot` or will be refused by _start_run.
    async with runtime._SHUTDOWN_ADMISSION_LOCK:
        runtime._SHUTDOWN_IN_PROGRESS = True
        active_snapshot = list(runtime._ACTIVE_RUNS.items())
    # Kill active sync/provision subprocess trees first, then cancel workers —
    # otherwise a gateway restart leaves pip/npm mutating shared checkouts.
    for rid, (task, proc) in active_snapshot:
        if proc is not None and proc.returncode is None:
            await runtime._kill_tree(proc.pid)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        runtime._ACTIVE_RUNS.pop(rid, None)
    # Cancel and await the idle background poll loops and the in-flight prune
    # worker. Cancelling the prune worker is safe because its two destructive
    # git mutations (`git worktree remove`, `update-ref -d`) run through
    # _run_uninterruptible: the cancel is delivered only at the safe boundary
    # once the timeout-bounded mutation has completed and its lock is released,
    # so the shared checkout is never left half-removed.
    for bg_task in (
        worktree_ops._refresher_task,
        worktree_ops._warm_task,
        worktree_ops._reaper_task,
        worktree_ops._prune_task,
    ):
        if bg_task is not None and not bg_task.done():
            bg_task.cancel()
            try:
                await bg_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    worktree_ops._refresher_task = None
    worktree_ops._warm_task = None
    worktree_ops._reaper_task = None
    worktree_ops._prune_task = None


# =============================================================================
# Application factory and main
# =============================================================================


def create_app() -> web.Application:
    """Build the aiohttp Application with all routes and lifecycle hooks."""
    app = web.Application(middlewares=[http_api.hmac_proxy_middleware])
    app.router.add_get("/health", http_api.api_health)
    # The dashboard reaches this backend ONLY through the gateway proxy, which
    # matches /apps/dev-fleet/api/{path} and forwards to /api/{path}
    # (handle_app_api_proxy). The bare /health above is reachable only by the
    # gateway's own in-process liveness poll (127.0.0.1:<port>/health, and it is
    # the one path the HMAC middleware exempts). So the restart-identity
    # handshake MUST poll a PROXIED path -- expose the same handler
    # under /api/health, which the browser reaches at /apps/dev-fleet/api/health.
    app.router.add_get("/api/health", http_api.api_health)
    app.router.add_get("/api/fleet", http_api.api_dev_fleet_fleet)
    app.router.add_get("/api/worktree", http_api.api_dev_fleet_worktree)
    app.router.add_get("/api/pod/logs", http_api.api_dev_fleet_pod_logs)
    app.router.add_get("/api/run", http_api.api_dev_fleet_run)
    app.router.add_get("/api/prune-candidates", http_api.api_dev_fleet_prune_candidates)
    app.router.add_get("/api/prune-status", http_api.api_dev_fleet_prune_status)
    app.router.add_get("/api/disk", http_api.api_dev_fleet_disk)
    app.router.add_post("/api/sync", http_api.api_dev_fleet_sync)
    app.router.add_post("/api/worktree/remove", http_api.api_dev_fleet_worktree_remove)
    app.router.add_post("/api/prune-run", http_api.api_dev_fleet_prune_run)
    app.router.add_post("/api/pod/up", http_api.api_dev_fleet_pod_up)
    app.router.add_post("/api/pod/down", http_api.api_dev_fleet_pod_down)
    app.router.add_post("/api/pod/restart", http_api.api_dev_fleet_pod_restart)
    app.router.add_post("/api/pod/token", http_api.api_dev_fleet_pod_token)
    app.router.add_post("/api/pod/provision", http_api.api_dev_fleet_pod_provision)
    app.router.add_post("/api/pod/provision/dismiss", http_api.api_dev_fleet_pod_provision_dismiss)
    app.router.add_post("/api/rebase", http_api.api_dev_fleet_rebase)
    # NOT here: /api/make-live and /api/restart-gateway. Both touch the live-target
    # pointer (or its cutover latch), which this sandboxed backend must never reach —
    # they are served by the GATEWAY process under /api/apps/dev-fleet/ (see
    # gateway_routes.py), authorised by the dashboard owner's own request.
    app.on_startup.append(dev_fleet_startup)
    app.on_cleanup.append(dev_fleet_cleanup)
    return app


def main() -> int:
    """Entry point when run as a module by the app backend system.

    Install the platform context FIRST. This runs as its own subprocess
    (``python -m ...`` spawned by the app backend launcher), so unlike an
    in-gateway import it inherits no installed context. Without this, the first
    code path that reads the context -- e.g. the sandbox floor resolved while
    wrapping this app's own ``git worktree`` scan -- calls ``current_context()``
    cold. On a non-standalone edition that raises ``PlatformCompositionError``
    ("no installed context but profile resolved to ..."), whose message then
    surfaces verbatim in the UI as an opaque sandbox error. ``boot_platform`` is
    idempotent and, like the CLI entry point, fails CLOSED: a non-standalone
    profile that cannot compose its companion aborts here rather than serving a
    backend with no security overlay or credential redaction.
    """
    boot_platform(KiroCrewConfig.load())
    # This process is the SANDBOXED backend: the live-target pointer is masked from
    # it, so every pointer-derived read goes to the gateway's broker route. The
    # bound port arrives from apps/backend.py; the app secret is the same material
    # the HMAC middleware verifies proxied requests with. Installed before the app
    # is built so the first fleet refresh already reads through the broker.
    # Without a port (a foreground gateway that had not bound when it spawned us,
    # or a hand-launched backend) the provider still installs and reports the
    # outage on each read: silently answering "nothing is live" is the one thing
    # the removal guards must never be told.
    bound = os.environ.get("KIROCREW_BOUND_PORT", "")
    broker: pointer_broker.GatewayPointerBroker | None = None
    if bound.isdigit():
        broker = pointer_broker.GatewayPointerBroker(
            port=int(bound), app_secret=http_api._load_app_secret()
        )
        live.install_pointer_provider(broker)
        live.install_removal_lease_client(
            (
                broker.acquire_removal_lease,
                broker.renew_removal_lease,
                broker.release_removal_lease,
            )
        )
    else:
        reason = "the Dev Fleet backend was started without KIROCREW_BOUND_PORT"
        live.install_pointer_provider(pointer_broker.unconfigured_provider(reason))
        live.install_removal_lease_client(pointer_broker.unconfigured_lease_client(reason))
    app = create_app()
    if broker is not None:
        # Closed inside the server's own loop at shutdown (the session was created
        # there), so no unclosed-session warning and no second event loop.
        async def _close_broker(_app: web.Application) -> None:
            assert broker is not None
            await broker.aclose()

        app.on_cleanup.append(_close_broker)
    runtime.logger.info("Dev Fleet backend starting on 127.0.0.1:%d", http_api.PORT)
    try:
        web.run_app(app, host="127.0.0.1", port=http_api.PORT, print=None)
    finally:
        # ``run_app`` blocks for the process's life, so this runs at shutdown — and
        # in a test that stubs it, immediately: the provider is process identity,
        # and must not outlive the server it was installed for.
        live.install_pointer_provider(None)
        live.install_removal_lease_client(None)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    raise SystemExit(main())

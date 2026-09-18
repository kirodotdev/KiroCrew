"""Dev Fleet's in-gateway routes: the live-target cutover and its pointer-state broker.

Why these run in the GATEWAY process and not in the Dev Fleet backend
=====================================================================

``live_target.json`` names the checkout the gateway ``execve``s into at its next
start, so it is bind-masked from every sandboxed process. The Dev Fleet backend is
one of those processes — and so is everything it spawns: a nested sandbox is denied
by design, so its ``npm ci`` / build children run inside the SAME namespace. A leaf
the backend could write, a worktree's lifecycle script could write too. Carving the
pointer out for that backend (the first version of this fix) therefore handed a
routine build the power to choose the gateway's next image.

The gateway already owns the pointer (it is the process that reads it at boot), and
it already authenticates the dashboard owner's browser. So the cutover runs here,
authorised by the OWNER'S OWN REQUEST — never by a credential the backend holds,
because the backend's build children can read ``/proc/<pid>/environ`` and every
file in its namespace, so any such credential would be theirs as well.

Routes, all under ``/api/apps/dev-fleet/`` (the in-gateway namespace; the
sandboxed backend is reverse-proxied at ``/apps/dev-fleet/api/``):

``POST make-live``
    The cutover / staged-cancel, unchanged in behaviour from when it lived in the
    backend (``live._make_live``). OWNER-ONLY, and REFUSED for any app principal —
    including Dev Fleet's own app token, which is exactly the credential a build
    child could steal.
``POST restart-gateway``
    Same gate. It shares the ``_MAKE_LIVE_LOCK`` / ``_MAKE_LIVE_COMMITTED`` latch
    with the cutover, so the two must live in one process.
``GET live-target``
    The pointer-state read broker: ``{live, staged, staged_cancel_available}`` as
    :class:`live.PointerState`. Admits ONLY Dev Fleet's own app token — this is how
    the sandboxed backend learns which row is live or staged (``server.main``
    installs :class:`pointer_broker.GatewayPointerBroker`); the dashboard never
    fetches it (the fleet view reads the redacted fleet payload), and the answer
    carries unredacted checkout paths, so a plain dashboard principal is refused. A
    stolen dev-fleet token buys a build child two paths and no write: the running
    checkout, which ``sys.executable`` already tells it, and the STAGED checkout,
    which it could not otherwise learn from inside the sandbox — a path to a
    checkout the same fleet already lists, not a capability to select it. Any
    OTHER app's token is refused.
``POST`` / ``PUT`` / ``DELETE live-target/removal-lease``
    The removal lease the backend holds across ``git worktree remove`` (see
    ``live.removal_lease``): gateway-held state that excludes a cutover or a
    gateway restart from landing mid-deletion. Admits ONLY Dev Fleet's own app
    token — the backend is the one process that runs the removal, and a lease
    latches ``busy`` on the owner-only cutover and restart, so a dashboard human
    (owner or not) is refused here, as the read route refuses them too. The lease
    itself is a CAPABILITY: ``POST {path}`` returns an unguessable token,
    and ``PUT {token}`` (heartbeat) / ``DELETE {token}`` require it — the shared app
    credential alone cannot cancel another operation's lease. A forged or
    forgotten lease can only DELAY a cutover, and it expires unless renewed.

Registered at gateway startup by the ``BUILTIN_NAMES`` loop in
``dashboard/routes/system.py`` (``_mod.register_routes(app)``), so every handler
checks the app's enabled state itself, like every other builtin.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.builtins.dev_fleet import live, repository, runtime
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.sel import sel

logger = logging.getLogger("kirocrew.app.dev-fleet.gateway")

APP_NAME = "dev-fleet"
API_PREFIX = f"/api/apps/{APP_NAME}"

_Handler = Callable[[web.Request], Awaitable[web.Response]]


def _caller(request: web.Request) -> str:
    return str(request.get("app") or request.get("user") or request.remote or "unknown")


def _deny(
    request: web.Request, operation: str, error: str, *, status: int, code: str = "forbidden"
) -> web.Response:
    sel().log_api_access(
        caller=_caller(request),
        operation=operation,
        outcome="denied",
        source="dev_fleet_gateway",
        resources=request.path,
        error=error,
    )
    return web.json_response({"ok": False, "code": code, "error": error}, status=status)


def _require_enabled(handler: _Handler) -> _Handler:
    """Same shape as every other builtin: a disabled app has no live routes.

    ``is_app_enabled`` is a synchronous ``installed.json`` read, so it runs off the
    loop (``asyncio.to_thread``), as the sibling builtins do — a data home on a slow
    filesystem must not stall every other request behind an enablement check.
    """

    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            # The same audited refusal every other denial on these routes takes: a
            # request that reaches a registered route while the app is disabled is a
            # refused access, and the SEL record is what says so afterwards.
            return _deny(
                request,
                "dev_fleet_gateway_route_disabled",
                "app not enabled",
                status=404,
                code="app_not_enabled",
            )
        return await handler(request)

    _wrapped.__name__ = handler.__name__
    return _wrapped


def _principal_is_backend(request: web.Request) -> bool:
    """Dev Fleet's OWN app token and nothing else — the gate on every non-owner route.

    The read broker's only caller is the backend's ``GatewayPointerBroker`` (the
    dashboard never fetches it — the fleet view reads the redacted fleet payload
    instead), and its answer carries unredacted absolute checkout paths; a lease
    latches ``busy`` on the owner-only cutover and restart, so it is not a read
    either. A dashboard human without an app principal — including a
    Slack-allowlisted non-owner holding a plain dashboard token — is refused on both.
    """
    return (request.get("app") or "") == APP_NAME


def _require_owner_human(operation: str) -> Callable[[_Handler], _Handler]:
    """The write gate: the dashboard OWNER, on a request that is not an app's.

    ``request["app"]`` is set by the token middleware on every authenticated path
    (``""`` for a dashboard user, the app name for an app token). It is checked
    FIRST and separately from the owner check, so the refusal names the actual
    reason: an app token — Dev Fleet's own included — must never move the pointer,
    because that token is readable by every build child in the backend's namespace.
    """

    def _decorate(handler: _Handler) -> _Handler:
        async def _wrapped(request: web.Request) -> web.Response:
            if request.get("app"):
                return _deny(
                    request,
                    operation,
                    "the live-target cutover is a dashboard-owner action; an app "
                    "token cannot perform it",
                    status=403,
                )
            refused = await require_owner_dashboard_request(request, operation)
            if refused is not None:
                return refused
            return await handler(request)

        _wrapped.__name__ = handler.__name__
        return _wrapped

    return _decorate


async def _ensure_repo() -> web.Response | None:
    """Run Dev Fleet's main-checkout discovery once in this process.

    ``_make_live`` validates its target against the discovered worktree set, which
    the backend resolves at ITS startup; the gateway resolves it lazily here, on
    first use, so a host that never opens Dev Fleet pays nothing.
    """
    try:
        await repository.ensure_main_repo_discovered()
    except Exception as exc:  # noqa: BLE001 — discovery must surface, not 500
        logger.warning("dev-fleet: main checkout discovery failed: %s", exc)
        return web.json_response(
            {
                "ok": False,
                "code": "repo_not_configured",
                "error": f"Dev Fleet main checkout unavailable: {runtime._redact(str(exc))}",
            },
            status=409,
        )
    return None


@_require_enabled
async def handle_live_target(request: web.Request) -> web.Response:
    """GET /api/apps/dev-fleet/live-target — the pointer-state read broker."""
    if not _principal_is_backend(request):
        return _deny(
            request,
            "dev_fleet_live_target_read",
            "only the Dev Fleet backend may read the live-target state",
            status=403,
        )
    fresh = request.query.get("fresh", "") in ("1", "true", "yes")
    # ``pointer_state`` is gateway-local here (the provider is only installed in the
    # backend): it reads and validates the pointer file and resolves paths, all
    # synchronous filesystem work, so it runs on the executor rather than the loop.
    state = await live.pointer_state(fresh=fresh)
    _audit_granted(request, "dev_fleet_live_target_read", resources="", outcome="read")
    return web.json_response(
        {
            "ok": True,
            "live": state.live,
            "staged": state.staged,
            "staged_cancel_available": state.staged_cancel_available,
            "previous": state.previous,
        }
    )


async def _lease_body(request: web.Request, key: str) -> tuple[str | None, web.Response | None]:
    body, err = await read_bounded_json(request)
    if err is not None:
        return None, err
    assert body is not None
    value = body.get(key)
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 4096:
        return None, web.json_response(
            {
                "ok": False,
                "code": f"invalid_{key}",
                "error": f"'{key}' must be a non-empty string without NUL bytes",
            },
            status=400,
        )
    return value, None


@_require_enabled
async def handle_removal_lease_acquire(request: web.Request) -> web.Response:
    """POST /api/apps/dev-fleet/live-target/removal-lease — ``{path}``.

    The backend takes this before ``git worktree remove`` so a cutover or a gateway
    restart cannot land mid-deletion; the gateway refuses it while a cutover is in
    flight. ``{"ok": true, "granted": bool, "token": str|null}`` — the token is the
    CAPABILITY the holder must present to renew or release, so a process that merely
    shares the app credential (a build child in the backend's namespace) cannot cancel
    a removal's lease from under it. A refusal is a normal answer, not an error.
    """
    if not _principal_is_backend(request):
        return _deny(
            request,
            "dev_fleet_removal_lease",
            "only the Dev Fleet backend may lease a worktree",
            status=403,
        )
    path, err = await _lease_body(request, "path")
    if err is not None:
        return err
    assert path is not None
    token = live.acquire_removal_lease(path)
    _audit_granted(
        request,
        "dev_fleet_removal_lease",
        resources=path,
        outcome="granted" if token is not None else "refused",
    )
    return web.json_response({"ok": True, "granted": token is not None, "token": token})


@_require_enabled
async def handle_removal_lease_renew(request: web.Request) -> web.Response:
    """PUT /api/apps/dev-fleet/live-target/removal-lease — ``{token}``.

    ``{"ok": true, "renewed": bool}``; ``false`` means the lease is gone (expired, or the
    gateway restarted) and the holder must not start a mutation it has not started.
    """
    if not _principal_is_backend(request):
        return _deny(
            request,
            "dev_fleet_removal_lease",
            "only the Dev Fleet backend may renew a worktree lease",
            status=403,
        )
    token, err = await _lease_body(request, "token")
    if err is not None:
        return err
    assert token is not None
    renewed = live.renew_removal_lease(token)
    # The token is the capability — never written to the log; the route is the resource.
    _audit_granted(
        request, "dev_fleet_removal_lease", resources="", outcome="renewed" if renewed else "lapsed"
    )
    return web.json_response({"ok": True, "renewed": renewed})


@_require_enabled
async def handle_removal_lease_release(request: web.Request) -> web.Response:
    """DELETE /api/apps/dev-fleet/live-target/removal-lease — ``{token}``. Idempotent;
    a token that names no lease is a no-op, never a way to find one."""
    if not _principal_is_backend(request):
        return _deny(
            request,
            "dev_fleet_removal_lease",
            "only the Dev Fleet backend may release a worktree lease",
            status=403,
        )
    token, err = await _lease_body(request, "token")
    if err is not None:
        return err
    assert token is not None
    live.release_removal_lease(token)
    _audit_granted(request, "dev_fleet_removal_lease", resources="", outcome="released")
    return web.json_response({"ok": True})


@_require_enabled
@_require_owner_human("dev_fleet_restart_gateway")
async def handle_restart_gateway(request: web.Request) -> web.Response:
    """POST /api/apps/dev-fleet/restart-gateway."""
    result = await live._restart_gateway()
    _audit(request, "dev_fleet_restart_gateway", result, target="")
    return web.json_response(result)


@_require_enabled
@_require_owner_human("dev_fleet_make_live")
async def handle_make_live(request: web.Request) -> web.Response:
    """POST /api/apps/dev-fleet/make-live — body ``{path, dry_run?, expected_staged?, undo?}``.

    Body validation is byte-for-byte the contract the dashboard client already
    speaks, so the client's request shape is the same on this route as on the
    reverse-proxied backend routes.
    """
    body, err = await read_bounded_json(request)
    if err is not None:
        return err
    assert body is not None
    path = body.get("path")
    if not isinstance(path, str) or not path:
        return web.json_response(
            {"code": "invalid_path", "error": "'path' must be a non-empty string"}, status=400
        )
    dry_run = body.get("dry_run")
    if dry_run is not None and not isinstance(dry_run, bool):
        return web.json_response(
            {"code": "invalid_dry_run", "error": "dry_run must be a boolean"}, status=400
        )
    undo = body.get("undo")
    if undo is not None and not isinstance(undo, bool):
        return web.json_response(
            {"code": "invalid_undo", "error": "undo must be a boolean"}, status=400
        )
    expected_staged = body.get("expected_staged")
    if expected_staged is not None and (
        not isinstance(expected_staged, str) or not expected_staged or "\x00" in expected_staged
    ):
        return web.json_response(
            {
                "code": "invalid_expected_staged",
                "error": "expected_staged must be a non-empty string without NUL bytes",
            },
            status=400,
        )
    if undo is True and expected_staged is not None:
        return web.json_response(
            {
                "code": "undo_conflicts_with_expected_staged",
                "error": "undo and expected_staged are mutually exclusive",
            },
            status=400,
        )
    refused = await _ensure_repo()
    if refused is not None:
        return refused
    try:
        result = await live._make_live(
            path, dry_run is True, expected_staged=expected_staged, undo=undo is True
        )
    except repository.RepoUnavailable as exc:
        # Discovery can complete with NO checkout resolved (a packaged install with Dev
        # Fleet enabled but nothing configured); the cutover's own worktree lookup then
        # raises here, before any pointer mutation. Same 409 as a failed discovery.
        return web.json_response(
            {
                "ok": False,
                "code": "repo_not_configured",
                "error": f"Dev Fleet main checkout unavailable: {runtime._redact(str(exc))}",
            },
            status=409,
        )
    _audit(request, "dev_fleet_make_live", result, target=path)
    return web.json_response(result)


def _audit(request: web.Request, operation: str, result: dict[str, Any], *, target: str) -> None:
    """One SEL record per mutation, mirroring the backend's ``_audited`` decorator."""
    ok = bool(result.get("ok"))
    try:
        sel().log_api_access(
            caller=_caller(request),
            operation=operation,
            outcome="success" if ok else "denied",
            source="dev_fleet_gateway",
            resources=runtime._redact(target) if target else "",
            error="" if ok else runtime._redact(str(result.get("error") or "")),
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _audit_granted(request: web.Request, operation: str, *, resources: str, outcome: str) -> None:
    """One SEL record per ACCEPTED backend-principal call on the read and lease routes.

    ``_deny`` records the refusals; without this the accepted side of the same
    authorization decision — a pointer read, a lease granted, renewed or released —
    would leave no trace. ``outcome`` names the fact (``read``, ``granted``,
    ``refused``, ``renewed``, ``lapsed``, ``released``); the resource is the path
    when the call names one, else the route.
    """
    try:
        sel().log_api_access(
            caller=_caller(request),
            operation=operation,
            outcome=outcome,
            source="dev_fleet_gateway",
            resources=runtime._redact(resources) if resources else request.path,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def register_routes(app: web.Application) -> None:
    """Register this app's in-gateway routes (``_mod.register_routes(app)`` contract)."""
    app.router.add_get(f"{API_PREFIX}/live-target", handle_live_target)
    app.router.add_post(f"{API_PREFIX}/live-target/removal-lease", handle_removal_lease_acquire)
    app.router.add_put(f"{API_PREFIX}/live-target/removal-lease", handle_removal_lease_renew)
    app.router.add_delete(f"{API_PREFIX}/live-target/removal-lease", handle_removal_lease_release)
    app.router.add_post(f"{API_PREFIX}/restart-gateway", handle_restart_gateway)
    app.router.add_post(f"{API_PREFIX}/make-live", handle_make_live)


__all__ = (
    "API_PREFIX",
    "APP_NAME",
    "handle_live_target",
    "handle_make_live",
    "handle_removal_lease_acquire",
    "handle_removal_lease_release",
    "handle_removal_lease_renew",
    "handle_restart_gateway",
    "register_routes",
)

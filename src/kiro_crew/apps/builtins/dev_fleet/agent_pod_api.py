"""Pod lifecycle for an agent session, served by the GATEWAY process.

WHY THIS EXISTS AT ALL
----------------------
An agent session runs behind a sandbox with its own user namespace. Its shells
therefore cannot ``connect(2)`` the systemd user-bus socket, and every pod verb
needs that bus: ``kirocrew pod up`` runs ``systemctl --user daemon-reload`` plus
``start``, which fails with a bare ``Permission denied``. Pod-based testing is
the QA loop for Kiro Crew development, so losing it from agent sessions means a
human has to boot each pod by hand and paste a ``{base_url, token}`` handle into
the session.

The gateway process has no such wall: it is the process the sandbox launcher is a
CHILD of, so it holds the host namespace and the host bus. These routes are the
gateway doing the systemd part on the agent's behalf. The agent reaches them the
way it reaches every other tool -- an MCP call over the stdio pipe, then loopback
HTTP to the gateway -- and no D-Bus passthrough into the sandbox is needed.

WHY THESE ROUTES AND NOT THE APP BACKEND'S EXISTING ONES
--------------------------------------------------------
Dev Fleet already serves ``/api/pod/up`` and friends from its backend subprocess,
reached through the gateway's ``/apps/dev-fleet/api/*`` reverse proxy. That proxy
is browser-shaped: it requires a dashboard session cookie or token, and the agent
holds neither (cookies are httpOnly, ``KIROCREW_INTERNAL_SECRET`` is stripped from
agent env, and ``.local_secret`` is on the sensitive-path denylist). Admitting an
internal-secret caller to that proxy would have opened the app's WHOLE backend
surface to anything holding the secret, so instead these four routes are named
one by one in ``server._STRICT_INTERNAL_API_PATHS`` -- the same shape
``/api/apps/issue-radar/investigation`` and the Ops Mission Control agent surface
already use, and for the same reason.

The pod work itself is NOT reimplemented here. Every handler delegates to the
``worktree_ops`` helpers the dashboard's own buttons call, which shell out to
``kirocrew pod ... --json``. One definition of what "up" means, one definition of
what a pod's status is, and an agent and a human clicking the same operation get
the same result.

WHAT GATES IT
-------------
``dev-fleet`` ships ``defaultEnabled: false``, and every handler is wrapped in
:func:`_require_enabled`. Routes are registered once at gateway startup, before any
app is known to be on, so without that wrapper an app the operator never turned on
would still be callable. Each handler additionally re-asserts that the caller is a
local process holding the internal secret (:func:`_machine_only`).

There is deliberately NO per-operator opt-in on top of that, and no per-call
approval. A pod runs the code in a git worktree an agent can write, started by the
user systemd manager, so it executes outside the agent's sandbox -- but that
reachability is Kiro Crew's DOCUMENTED posture, not something this module invents:
``docs/system-specs/modules/security.md``, under "Scoped user-bus locator forward",
records that sandboxed agent shells legitimately run ``systemctl --user`` and the
``kirocrew pod`` CLI, and names the residual in the same paragraph. The builtin
``pod-e2e`` skill has always instructed agents to boot pods. An extra gate here
would not close that residual; it would only stop the agent-driven QA loop this
module exists to restore, while leaving every other path to the same reachability
open. Agent pod control is an intended capability, so it is not gated.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from aiohttp import web

from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.sel import sel

APP_NAME = "dev-fleet"

# Bound on a pod name accepted from a caller. `rt.validate_name` is the real
# authority (and runs inside the CLI), but a name also reaches `git worktree
# list` matching and a filesystem path before that, so an unbounded string is
# rejected here rather than carried further.
_MAX_NAME_LEN = 200


def _machine_only(
    handler: Callable[[web.Request], Awaitable[web.Response]],
) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Re-assert that the caller is a local process holding the internal secret.

    Being listed in ``server._STRICT_INTERNAL_API_PATHS`` does NOT prove the secret
    was checked. With the header absent the middleware deliberately falls through to
    cookie auth, and a ``local_only=False`` deployment reclassifies every strict path
    as "mixed" -- either way a caller holding only a dashboard cookie or an
    app-scoped token would arrive here. These routes start and stop local services
    and delete a data directory, so both halves are re-checked at the handler, the
    way ``/api/computer-use/frame`` does.

    LOCAL ORIGIN IS A UNION, NOT A LOOPBACK ADDRESS. ``mcp_core`` prefers the
    gateway's AF_UNIX socket whenever the file exists, which is the normal case on
    POSIX -- the only platform where ``systemd --user`` pods exist at all -- and over
    AF_UNIX ``request.remote`` is EMPTY, so ``is_loopback("")`` is False. Testing the
    loopback half alone would 403 every pod tool on the platform the feature is for.
    This mirrors the middleware's own condition (``_unix_sock is not None or
    is_loopback(...)``, token_auth.py) rather than half of it.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        from kiro_crew.dashboard.origin import is_loopback, request_is_unix_socket

        if not (request_is_unix_socket(request) or is_loopback(request.remote or "")):
            _audit("machine_guard", request.path, "denied", error="non-local caller")
            return web.json_response(
                {"ok": False, "code": "loopback_only", "error": "local callers only"}, status=403
            )
        if request.get("internal_auth") is not True:
            _audit("machine_guard", request.path, "denied", error="internal secret required")
            return web.json_response(
                {
                    "ok": False,
                    "code": "internal_secret_required",
                    "error": (
                        "pod lifecycle is reachable only by a local Kiro Crew process "
                        "holding the internal secret"
                    ),
                },
                status=403,
            )
        return await handler(request)

    return _wrapped


def _require_enabled(
    handler: Callable[[web.Request], Awaitable[web.Response]],
) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Deny when Dev Fleet is disabled (deny-by-default).

    ``is_app_enabled`` reads ``installed.json`` synchronously, so it runs off the
    event loop -- same as the Issue Radar gate this mirrors.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {
                    "ok": False,
                    "code": "app_not_enabled",
                    "error": (
                        "dev-fleet is not enabled. Turn the Dev Fleet app on in the "
                        "dashboard App Store before using the pod tools."
                    ),
                },
                status=403,
            )
        return await handler(request)

    return _wrapped


def _audit(op: str, target: str, outcome: str, *, error: str = "") -> None:
    """Record the lifecycle decision in the Security Event Log.

    Pod boot and teardown start and stop a long-lived local service and delete an
    isolated HOME, and the caller here is an agent rather than a person clicking a
    button -- so the trail is what makes an unattended run reviewable afterwards.
    Fire-and-forget: the HTTP response is decided by the caller either way.
    """
    sel().log_api_access(
        caller="agent:dev-fleet",
        operation=f"dev_fleet.agent.{op}",
        outcome=outcome,
        source="builtin-app",
        resources=target,
        error=error[:200] if error else "",
    )


def _clean_name(raw: Any) -> tuple[str, web.Response | None]:
    """The pod/worktree name from a caller, or a 400 saying what was wrong."""
    if not isinstance(raw, str) or not raw.strip():
        return "", web.json_response(
            {
                "ok": False,
                "code": "invalid_worktree",
                "error": "'worktree' must be a non-empty string naming a git worktree",
            },
            status=400,
        )
    name = raw.strip()
    if len(name) > _MAX_NAME_LEN:
        return "", web.json_response(
            {
                "ok": False,
                "code": "invalid_worktree",
                "error": f"'worktree' is longer than {_MAX_NAME_LEN} characters",
            },
            status=400,
        )
    return name, None


async def _body(request: web.Request) -> tuple[dict[str, Any], web.Response | None]:
    """The request's JSON object, or a 400. A missing body reads as ``{}``."""
    if not request.can_read_body:
        return {}, None
    try:
        parsed = await request.json()
    except Exception:  # noqa: BLE001 - any decode failure is one 400
        return {}, web.json_response(
            {"ok": False, "code": "invalid_body", "error": "body must be a JSON object"},
            status=400,
        )
    if not isinstance(parsed, dict):
        return {}, web.json_response(
            {"ok": False, "code": "invalid_body", "error": "body must be a JSON object"},
            status=400,
        )
    return parsed, None


def _ops() -> Any:
    """The Dev Fleet pod operations module, imported on first use.

    Deliberately not a module-level import. ``dashboard/routes/system.py`` imports
    every builtin app package during gateway startup to find its ``register_routes``,
    and ``worktree_ops`` pulls in the frontend build, dependency sync and sandbox
    layers -- boot cost every gateway would pay for an app that is off by default
    and whose routes may never be called.
    """
    from kiro_crew.apps.builtins.dev_fleet import worktree_ops

    return worktree_ops


def _error_text(payload: dict[str, Any]) -> str:
    """The refusal sentence from a ``worktree_ops`` result, always non-empty.

    Those helpers report failure in-band as ``{"ok": False, "error": ...}`` because
    the dashboard renders the sentence. An agent needs the same distinction on the
    STATUS LINE, plus a machine-readable ``code``, so each handler below turns a
    refusal into a 409 with a literal code of its own: the request was well-formed
    and authorized, the host just is not in a state where it can be carried out (no
    such worktree, pod already claimed, systemd refused). 500 would claim a gateway
    bug that has not happened.

    Each handler spells its own body out rather than spreading the result dict: an
    ``error-code-baseline`` bucket counts a spread body as unreadable to the static
    check, and this surface is small enough to state plainly.
    """
    return str(payload.get("error") or "the pod operation did not complete")


async def _run_op(op: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Await a ``worktree_ops`` call, turning "no usable checkout" into a refusal.

    ``repository._repo()`` RAISES (``RepoNotConfigured`` / ``RepoUnreadable``) rather
    than returning a falsy path, and it sits on EVERY branch these handlers reach:
    the subprocess cwd, ``_pod_env``, and -- less obviously -- inside
    ``_pod_checkout_guard``, whose chain runs ``repository._find_worktree`` ->
    ``_discover_worktrees`` -> ``_repo()`` (repository.py:581) before any helper-level
    guard could see it. Catching per helper therefore covers one branch and leaves
    the others answering a setup problem with a 500. One place, before the result is
    ever inspected, is the only shape that covers all of them -- and it stays correct
    when a new verb is added, because a new verb cannot forget it.
    """
    from kiro_crew.apps.builtins.dev_fleet import repository

    try:
        return await op()
    except repository.RepoUnavailable as exc:
        return {"ok": False, "repo_unavailable": True, "error": str(exc)}


def _repo_refusal(payload: dict[str, Any]) -> web.Response | None:
    """A 409 naming the setup problem, or None when the checkout was usable."""
    if not payload.get("repo_unavailable"):
        return None
    return web.json_response(
        {"ok": False, "code": "repo_unavailable", "error": _error_text(payload)},
        status=409,
    )


@_machine_only
@_require_enabled
async def handle_pod_up(request: web.Request) -> web.Response:
    """POST /api/apps/dev-fleet/pod/up -- boot a pod, answer with its handle.

    The response carries the ``base_url`` and ``token`` the CLI's ``--json`` prints,
    which is the whole point: minting happens here, in the gateway, from the pod's
    own ``.local_secret``. The agent receives a scoped 2h credential and never
    touches the secret that produced it.

    Provisioning is deliberately NOT reachable from here. A cold venv plus an SPA
    build is minutes of work, and holding one request -- and one MCP tool call --
    open for that long is a shape that dies to any harness timeout or gateway
    restart with no way to learn the outcome. The dashboard's Provision button
    streams the same work under a run id because a browser needs to watch it; an
    agent has no such channel. Without a built dist the CLI refuses and names the
    remedy, which is a better answer than a 16-minute call that might not survive.
    """
    body, err = await _body(request)
    if err is not None:
        return err
    name, err = _clean_name(body.get("worktree"))
    if err is not None:
        return err
    result = await _run_op(functools.partial(_ops()._pod_up, name))
    refusal = _repo_refusal(result)
    if refusal is not None:
        return refusal
    _audit(
        "pod_up",
        name,
        "granted" if result.get("ok") else "failed",
        error=str(result.get("error") or ""),
    )
    if not result.get("ok"):
        return web.json_response(
            {"ok": False, "code": "pod_up_failed", "error": _error_text(result)}, status=409
        )
    return web.json_response(result)


@_machine_only
@_require_enabled
async def handle_pod_down(request: web.Request) -> web.Response:
    """POST /api/apps/dev-fleet/pod/down -- stop a pod and reclaim its HOME."""
    body, err = await _body(request)
    if err is not None:
        return err
    name, err = _clean_name(body.get("worktree"))
    if err is not None:
        return err
    result = await _run_op(functools.partial(_ops()._pod_down, name))
    refusal = _repo_refusal(result)
    if refusal is not None:
        return refusal
    _audit(
        "pod_down",
        name,
        "granted" if result.get("ok") else "failed",
        error=str(result.get("error") or ""),
    )
    if not result.get("ok"):
        return web.json_response(
            {"ok": False, "code": "pod_down_failed", "error": _error_text(result)}, status=409
        )
    return web.json_response(result)


@_machine_only
@_require_enabled
async def handle_pod_status(request: web.Request) -> web.Response:
    """GET /api/apps/dev-fleet/pod/status?worktree=<name> -- one pod's state."""
    name, err = _clean_name(request.query.get("worktree"))
    if err is not None:
        return err
    result = await _run_op(functools.partial(_ops()._pod_status, name))
    refusal = _repo_refusal(result)
    if refusal is not None:
        return refusal
    if not result.get("ok"):
        return web.json_response(
            {"ok": False, "code": "pod_status_failed", "error": _error_text(result)}, status=409
        )
    return web.json_response(result)


@_machine_only
@_require_enabled
async def handle_pod_list(request: web.Request) -> web.Response:
    """GET /api/apps/dev-fleet/pod/list -- every pod active on this host."""
    result = await _run_op(_ops()._pod_ls)
    refusal = _repo_refusal(result)
    if refusal is not None:
        return refusal
    if not result.get("ok"):
        return web.json_response(
            {"ok": False, "code": "pod_list_failed", "error": _error_text(result)}, status=409
        )
    return web.json_response(result)


def register_routes(app: web.Application) -> None:
    """Register the agent-facing pod routes on the gateway's aiohttp app.

    Single-argument signature with hardcoded paths, matching every other builtin
    (the call site in ``dashboard/routes/system.py`` passes only ``app``).

    Each path here is also named in ``server._STRICT_INTERNAL_API_PATHS``. That
    table is exact-or-prefix, so ``/api/apps/dev-fleet/pod`` is deliberately NOT
    listed as a prefix: a future route under it must be admitted on purpose rather
    than inherit the internal-secret grant by sitting in the same directory.
    """
    app.router.add_post("/api/apps/dev-fleet/pod/up", handle_pod_up)
    app.router.add_post("/api/apps/dev-fleet/pod/down", handle_pod_down)
    app.router.add_get("/api/apps/dev-fleet/pod/status", handle_pod_status)
    app.router.add_get("/api/apps/dev-fleet/pod/list", handle_pod_list)

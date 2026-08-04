"""Instances API handlers — owner-only control plane for multi-instance mgmt.

Backs the ``/api/instances/*`` routes. Every route:

* is **gated behind ``instances.enabled``** (default off) — returns 403 when
  the feature is disabled, so toggling requires an explicit opt-in;
* is **owner-only and never reachable via the Slack path** — a request whose
  ``X-Session-Key`` indicates a Slack origin is rejected, so chat-sharing a hub
  can never pivot into SSH control;
* emits a **SEL audit event** on both reads and writes (security observability
  for an SSH-pivoting control plane).

Connect responds with the minted dashboard **token** so the browser can set the
embedded iframe's first-party cookie; that response is the *only* place a token
crosses this boundary, it is never logged, and it never appears in list/status.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.instances.registry import (
    DuplicateInstanceError,
    InstanceNotFoundError,
    InstancesError,
    InstancesRegistry,
    InvalidInstanceError,
)
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


def _audit(operation: str, outcome: str, *, request_id: str = "", error: str = "") -> None:
    """Emit a SEL audit event for an instances control-plane action."""
    try:
        sel().log_tool_invocation(
            session_key="dashboard:instances",
            tool_name=f"instances_{operation}",
            outcome=outcome,
            request_id=request_id,
            source="dashboard",
            error=error,
        )
    except Exception:  # audit must never break the request path
        logger.debug("SEL audit failed for instances_%s", operation, exc_info=True)


def _is_slack_origin(request: web.Request) -> bool:
    """True if the request arrived via the Slack path (X-Session-Key 'slack:*')."""
    sk = request.headers.get("X-Session-Key", "")
    return sk.startswith("slack:")


def _guard(request: web.Request, operation: str) -> web.Response | None:
    """Shared gate: enabled-check + owner-only (non-Slack). Returns a denial
    Response to short-circuit, or None to proceed. Audits every denial."""
    if _is_slack_origin(request):
        _audit(operation, "denied", error="slack-origin rejected")
        return web.json_response(
            {"error": "instances control plane is owner-only (not reachable via Slack)"},
            status=403,
        )
    # Deny-by-default: positively confirm an authenticated owner. The dashboard's
    # require_auth middleware sets request["user"] ONLY after validating the
    # owner's dashboard token; its absence means the caller is unauthenticated, so
    # we reject rather than relying on the middleware implicitly (defense in depth
    # for this SSH-pivoting control plane).
    if not request.get("user"):
        _audit(operation, "denied", error="unauthenticated (no owner identity)")
        return web.json_response(
            {"error": "authentication required (owner-only control plane)"},
            status=401,
        )
    cfg = KiroCrewConfig.load()
    if not cfg.instances.enabled:
        _audit(operation, "denied", error="feature disabled")
        return web.json_response(
            {"error": "instances feature is disabled (set instances.enabled=true)"},
            status=403,
        )
    return None


def _registry(state: "DashboardState"):
    """Return the live InstancesRegistry, creating a standalone one if needed."""
    reg = getattr(state, "instances_registry", None)
    if reg is None:
        reg = InstancesRegistry()
        state.instances_registry = reg
    return reg


def _status_for(state: "DashboardState", instance_id: str) -> dict:
    """Live tunnel status dict for an instance, or a disconnected default.

    When there is no live tunnel but the manager retained a failure reason from
    a failed connect/reconnect (e.g. a startup auto-revive that couldn't reach
    the host), report an ``error`` state carrying that reason so a sticky tab
    can show *why* it is down rather than a bare "disconnected".
    """
    mgr = getattr(state, "instances_manager", None)
    if mgr is not None:
        st = mgr.status(instance_id)
        if st is not None:
            d = st.to_dict()
            # Surface token TTL remaining (lives on the manager, not the status).
            ttl = mgr.token_ttl_remaining(instance_id)
            if ttl is not None:
                d["token_ttl_remaining"] = ttl
            return d
        last_err = mgr.last_error(instance_id)
        if last_err:
            return {"instance_id": instance_id, "state": "error", "error": last_err}
    return {"instance_id": instance_id, "state": "disconnected"}


def _instance_view(state: "DashboardState", inst) -> dict:
    """Registry record + live status, merged for the Manage panel. No token."""
    view = inst.to_dict()
    view["status"] = _status_for(state, inst.id)
    return view


# ── read endpoints ───────────────────────────────────────────────────────


async def api_instances_list(request: web.Request) -> web.Response:
    """GET /api/instances — list configured instances with live status."""
    denied = _guard(request, "list")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    reg = _registry(state)
    items = [_instance_view(state, i) for i in reg.list()]
    _audit("list", "success")
    return web.json_response(
        {
            # ``active`` distinguishes "enabled in config" from "usable now": the
            # _guard only checks the (live) config flag, so this endpoint answers
            # 200 as soon as instances.enabled=true — but the SSH manager only
            # exists if the flag was on at gateway startup. When enabled && not
            # active, the UI shows a "restart the gateway to activate" hint.
            "active": getattr(state, "instances_manager", None) is not None,
            "instances": items,
            "warm_set_cap": KiroCrewConfig.load().instances.warm_set_cap,
        }
    )


async def api_instances_status(request: web.Request) -> web.Response:
    """GET /api/instances/{id}/status — live tunnel status for one instance."""
    denied = _guard(request, "status")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    reg = _registry(state)
    if reg.get(instance_id) is None:
        _audit("status", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found"}, status=404)
    # Optional on-demand failure diagnosis: ?diagnose=1 runs the ordered probe
    # ladder and attaches the result to the returned status.
    # NOTE: for an instance that has never connected there is no live tunnel,
    # so diagnose() can't store the result on a tunnel status — it returns it
    # instead. Merge that return value into the response (otherwise Diagnose on
    # a disconnected instance silently shows nothing, which defeats its purpose).
    diag = None
    if request.query.get("diagnose"):
        mgr = getattr(state, "instances_manager", None)
        if mgr is not None:
            diag = await mgr.diagnose(instance_id)
    _audit("status", "success", request_id=instance_id)
    status = _status_for(state, instance_id)
    if diag is not None:
        status.setdefault("diagnosis", diag)
    return web.json_response(status)


# ── write endpoints ──────────────────────────────────────────────────────


async def api_instances_add(request: web.Request) -> web.Response:
    """POST /api/instances — add a configured instance."""
    denied = _guard(request, "add")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    reg = _registry(state)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)
    try:
        inst = reg.add(
            name=str(body.get("name", "")),
            ssh_host=str(body.get("ssh_host", "")),
            remote_port=int(body.get("remote_port", 7777)),
            ttl=str(body.get("ttl", "20h")),
            remote_bin=str(body.get("remote_bin", "")),
            connection_method=str(body.get("connection_method", "ssh")),
            ssm_target=str(body.get("ssm_target", "")),
            ssm_run_as=str(body.get("ssm_run_as", "")),
            aws_profile=str(body.get("aws_profile", "")),
            aws_region=str(body.get("aws_region", "")),
            instance_id=body.get("id"),
        )
    except (DuplicateInstanceError, InvalidInstanceError) as e:
        _audit("add", "denied", error=str(e))
        return web.json_response({"error": str(e)}, status=400)
    except (TypeError, ValueError) as e:
        _audit("add", "denied", error=str(e))
        return web.json_response({"error": f"invalid field: {e}"}, status=400)
    _audit("add", "success", request_id=inst.id)
    return web.json_response(_instance_view(state, inst), status=201)


async def api_instances_update(request: web.Request) -> web.Response:
    """PATCH /api/instances/{id} — edit a configured instance."""
    denied = _guard(request, "update")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    reg = _registry(state)
    instance_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)
    # Only allow editing user-facing config fields (not internal hints).
    allowed = {
        "name",
        "ssh_host",
        "remote_port",
        "ttl",
        "remote_bin",
        "connection_method",
        "ssm_target",
        "ssm_run_as",
        "aws_profile",
        "aws_region",
    }
    changes = {k: v for k, v in body.items() if k in allowed}
    try:
        inst = reg.update(instance_id, **changes)
    except InstanceNotFoundError as e:
        _audit("update", "denied", request_id=instance_id, error=str(e))
        return web.json_response({"error": str(e)}, status=404)
    except (InvalidInstanceError, InstancesError) as e:
        _audit("update", "denied", request_id=instance_id, error=str(e))
        return web.json_response({"error": str(e)}, status=400)
    _audit("update", "success", request_id=instance_id)
    return web.json_response(_instance_view(state, inst))


async def api_instances_remove(request: web.Request) -> web.Response:
    """DELETE /api/instances/{id} — remove an instance (disconnects first)."""
    denied = _guard(request, "remove")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    reg = _registry(state)
    instance_id = request.match_info["id"]
    mgr = getattr(state, "instances_manager", None)
    if mgr is not None:
        await mgr.disconnect(instance_id)  # tear down any live tunnel first
    existed = reg.remove(instance_id)
    if not existed:
        _audit("remove", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found"}, status=404)
    _audit("remove", "success", request_id=instance_id)
    return web.json_response({"removed": instance_id})


async def api_instances_connect(request: web.Request) -> web.Response:
    """POST /api/instances/{id}/connect — open tunnel + mint token.

    On success returns the live status plus the minted ``token`` (the only
    place a token crosses this boundary; never logged, never in list/status).
    """
    denied = _guard(request, "connect")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("connect", "denied", request_id=instance_id, error="manager unavailable")
        return web.json_response({"error": "instances manager not running"}, status=503)
    try:
        status = await mgr.connect(instance_id)
    except KeyError:
        _audit("connect", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found"}, status=404)
    body = status.to_dict()
    if status.state.value == "connected":
        token = mgr.get_token(instance_id)
        # Validate the stored token before handing it to the browser. connect()
        # is idempotent and may return a CONNECTED tunnel whose token went stale
        # (a failed self-heal re-mint, or a remote `kirocrew restart` that
        # invalidates tokens). A stale token yields a server-rendered 403 page on
        # the iframe's first load, so the SPA never boots to fire `mc-auth-expired`
        # and the reactive recovery can't help. Probe over the live tunnel (no
        # SSH); deny-by-default, so anything short of a positive confirmation
        # forces a fresh mint.
        if not token or not await mgr.token_validates(status.local_port, token):
            token = await mgr.refresh_token(instance_id) or ""
            if not token:
                # The probe couldn't confirm the token AND the re-mint failed —
                # the link is genuinely unreachable. Serving the unconfirmed
                # token would just reproduce the stuck-iframe 403, so surface a
                # clean error instead of a token we know we can't stand behind.
                _audit(
                    "connect",
                    "failure",
                    request_id=instance_id,
                    error="token unconfirmed and re-mint failed",
                )
                body["error"] = "token expired and re-mint failed"
                return web.json_response(body, status=502)
        body["token"] = token  # delivered to owner only
        _audit("connect", "success", request_id=instance_id)
        return web.json_response(body)
    _audit("connect", "failure", request_id=instance_id, error=status.error)
    return web.json_response(body, status=502)


async def api_instances_refresh_token(request: web.Request) -> web.Response:
    """POST /api/instances/{id}/refresh-token — force a fresh token mint.

    Returns the newly minted ``token`` so the owner's browser can reload the
    embedded iframe with a valid credential — proactively before the gateway's
    TTL cap, or reactively when the embedded dashboard reports an expired
    session. Like ``connect``, this is the only other place a token crosses this
    boundary; it is never logged and never appears in list/status.
    """
    denied = _guard(request, "refresh_token")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("refresh_token", "denied", request_id=instance_id, error="manager unavailable")
        return web.json_response({"error": "instances manager not running"}, status=503)
    token = await mgr.refresh_token(instance_id)
    if not token:
        _audit(
            "refresh_token",
            "failure",
            request_id=instance_id,
            error="mint failed or instance not connected",
        )
        return web.json_response(
            {"error": "could not refresh token (instance not connected?)"}, status=502
        )
    _audit("refresh_token", "success", request_id=instance_id)
    st = mgr.status(instance_id)
    body = st.to_dict() if st is not None else {"instance_id": instance_id, "state": "connected"}
    body["token"] = token  # delivered to owner only
    return web.json_response(body)


async def api_instances_disconnect(request: web.Request) -> web.Response:
    """POST /api/instances/{id}/disconnect — tear down one tunnel."""
    denied = _guard(request, "disconnect")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    mgr = getattr(state, "instances_manager", None)
    existed = False
    if mgr is not None:
        existed = await mgr.disconnect(instance_id)
    _audit("disconnect", "success", request_id=instance_id)
    return web.json_response({"disconnected": instance_id, "was_connected": existed})


async def api_instances_restart(request: web.Request) -> web.Response:
    """POST /api/instances/{id}/restart — restart the remote gateway over SSH."""
    denied = _guard(request, "restart")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    if _registry(state).get(instance_id) is None:
        _audit("restart", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found"}, status=404)
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("restart", "denied", request_id=instance_id, error="manager unavailable")
        return web.json_response({"error": "instances manager not running"}, status=503)
    result = await mgr.restart_remote(instance_id)
    if result.get("ok"):
        _audit("restart", "success", request_id=instance_id)
        return web.json_response(result)
    _audit("restart", "failure", request_id=instance_id, error=result.get("message", ""))
    return web.json_response(result, status=502)

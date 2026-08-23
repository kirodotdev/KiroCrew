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

import asyncio
import dataclasses
import functools
import logging
import math
from typing import TYPE_CHECKING

from aiohttp import web

import kiro_crew.dashboard.handlers as _h
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.session_transfer import (
    SnapshotUnstable,
    build_transfer_bundle_async,
    local_instance_label,
)
from kiro_crew.history import SEARCH_MIN_CHARS
from kiro_crew.instances.registry import (
    DEFAULT_REMOTE_PORT,
    DuplicateInstanceError,
    InstanceNotFoundError,
    InstancesError,
    InstancesRegistry,
    InvalidInstanceError,
    validate_ttl,
)
from kiro_crew.instances.ssh_tunnel_manager import TunnelState
from kiro_crew.sel import sel
from kiro_crew.validation import sanitize_string

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

# Per-string ceiling for fields in a PEER's federated-search reply. A peer is
# untrusted input (see api_instances_search_sessions), and without a clamp a
# hostile or broken remote could ship megabyte titles/snippets straight to the
# browser — and feed the redaction regexes unbounded input on the way. Sized
# well above any honest value (local snippets are a short match window and
# titles are one line) so it only ever bites on garbage.
_PEER_FIELD_MAX_CHARS = 2048


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


def _apply_update(reg, instance_id: str, changes: dict) -> object:
    """Write *changes* to *instance_id*. Blocking — callers offload it.

    Module level on purpose: the registry write must never run on the event loop,
    and a closure defined inside the async handler reads (to a human and to the
    AST ratchet in ``test_apps_instances_loop_offload``) as a call on the loop
    even when every caller hands it to a thread.
    """
    return reg.update(instance_id, **changes)


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
    # Registry calls read (and mutations atomically rewrite + fsync)
    # instances.json under a threading lock a to_thread worker may hold across
    # its fsync — so every registry touch in these handlers goes off the loop.
    items = [_instance_view(state, i) for i in await asyncio.to_thread(reg.list)]
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
    if await asyncio.to_thread(reg.get, instance_id) is None:
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
        inst = await asyncio.to_thread(
            reg.add,
            name=str(body.get("name", "")),
            ssh_host=str(body.get("ssh_host", "")),
            remote_port=int(body.get("remote_port", DEFAULT_REMOTE_PORT)),
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


# Declared type of every field the PATCH body may set. `remote_port` is the only
# non-string; bool is excluded explicitly because `isinstance(True, int)` is True
# and `True` would otherwise validate as port 1.
_PATCH_FIELD_TYPES: dict[str, type] = {
    "name": str,
    "ssh_host": str,
    "ttl": str,
    "remote_bin": str,
    "connection_method": str,
    "ssm_target": str,
    "ssm_run_as": str,
    "aws_profile": str,
    "aws_region": str,
    "remote_port": int,
}


def _wrong_typed_field(changes: dict) -> str | None:
    """Return an error message for the first field whose JSON type is wrong."""
    for key, value in changes.items():
        expected = _PATCH_FIELD_TYPES.get(key)
        if expected is None:
            continue
        if expected is int and (isinstance(value, bool) or not isinstance(value, int)):
            return f"invalid {key}: expected a number"
        if expected is str and not isinstance(value, str):
            return f"invalid {key}: expected a string"
    return None


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
    # Only allow editing user-facing config fields (not internal hints). Derived
    # from the type map rather than listed twice, so a field can never be editable
    # without a declared type to check it against.
    allowed = set(_PATCH_FIELD_TYPES)
    changes = {k: v for k, v in body.items() if k in allowed}
    # The POST path coerces every field (`str(...)` / `int(...)`); PATCH passed the
    # decoded JSON value straight into the record, so a wrong-typed value reached
    # validators that assume the declared type: `{"name": 123}` crashed
    # `name.strip()` with an AttributeError (HTTP 500), and `{"remote_port": true}`
    # slipped through as port 1 because bool IS an int. Type-check at the boundary
    # instead of coercing, so a malformed body is REFUSED rather than silently
    # reinterpreted -- a PATCH states an intent, and guessing at it is how "7777"
    # becomes a port nobody chose.
    bad = _wrong_typed_field(changes)
    if bad is not None:
        _audit("update", "denied", request_id=instance_id, error=bad)
        return web.json_response({"error": bad, "code": "instance_invalid"}, status=400)
    # A tunnel is opened from these fields, so editing one of them makes a live
    # tunnel wrong rather than merely out of date: it keeps forwarding the old
    # port to the old host under the new label. Tear it down as part of the save
    # so the next connect builds from what the user just entered.
    transport_keys = {
        "ssh_host",
        "remote_port",
        "connection_method",
        "ssm_target",
        "ssm_run_as",
        "aws_profile",
        "aws_region",
        "remote_bin",
    }
    current = await asyncio.to_thread(reg.get, instance_id)
    if current is None:
        _audit("update", "denied", request_id=instance_id, error="not found")
        return web.json_response(
            {"error": "not found", "code": "instance_not_found"}, status=404
        )
    transport_changed = any(
        k in transport_keys and v != getattr(current, k) for k, v in changes.items()
    )
    # Validate the PROPOSED record before touching the tunnel. The registry
    # validates too, but that happens after the teardown — so a rejected edit
    # would answer 400 having already disconnected a healthy crew, punishing the
    # user for a typo the save never accepted.
    try:
        dataclasses.replace(current, **changes).validate()  # type: ignore[arg-type]
        # Not part of the record invariant: a ttl is checked where it is WRITTEN,
        # so a legacy value cannot fail an unrelated hint write (see
        # registry.validate_ttl). The edit path is such a write.
        if "ttl" in changes:
            validate_ttl(str(changes["ttl"]))
    except (InvalidInstanceError, AttributeError, TypeError, ValueError) as e:
        # AttributeError is the backstop for a field added to `allowed` but not to
        # _PATCH_FIELD_TYPES: a validator calling `.strip()` on a non-string must
        # still answer 400, never 500.
        _audit("update", "denied", request_id=instance_id, error=str(e))
        return web.json_response({"error": str(e), "code": "instance_invalid"}, status=400)
    mgr = getattr(state, "instances_manager", None)

    try:
        if transport_changed and mgr is not None:
            # The teardown and the coordinate rewrite must not be observable
            # apart. With the lock released between them a `connect` can read the
            # OLD record, and whether its tunnel is already CONNECTED or still
            # CONNECTING when the write lands decides whether any after-the-fact
            # sweep would notice — so the window is closed rather than narrowed:
            # reconfigure() holds the manager lock across both, and a racing
            # connect either finishes before (and is torn down inside) or starts
            # after (and reads the new coordinates).
            inst = await mgr.reconfigure(
                instance_id, functools.partial(_apply_update, reg, instance_id, changes)
            )
        else:
            if transport_changed:
                # No manager: nothing to tear down, but say so — a silent skip
                # would look identical to a teardown that ran.
                logger.warning(
                    "instance %s transport edited with no manager running; "
                    "no tunnel teardown performed",
                    instance_id,
                )
            inst = await asyncio.to_thread(
                functools.partial(_apply_update, reg, instance_id, changes)
            )
    except InstanceNotFoundError as e:
        _audit("update", "denied", request_id=instance_id, error=str(e))
        return web.json_response(
            {"error": str(e), "code": "instance_not_found"}, status=404
        )
    except (InvalidInstanceError, InstancesError) as e:
        _audit("update", "denied", request_id=instance_id, error=str(e))
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        # The teardown could not stop the old forward. Nothing was persisted, so
        # the record still matches the tunnel that is actually running; saying so
        # is better than advancing the record over a live connection to the
        # previous machine.
        _audit("update", "denied", request_id=instance_id, error=str(e))
        logger.warning(
            "refusing to reconfigure %s: its tunnel could not be torn down",
            instance_id,
            exc_info=True,
        )
        return web.json_response(
            {
                "error": (
                    "could not close the current tunnel, so the new settings were "
                    "not saved; disconnect this crew and try again"
                ),
                "code": "tunnel_teardown_failed",
            },
            status=503,
        )
    _audit("update", "success", request_id=instance_id)
    return web.json_response(_instance_view(state, inst))


async def api_instances_remove(request: web.Request) -> web.Response:
    """DELETE /api/instances/{id} — remove an instance (disconnects first).

    The pre-removal disconnect and the offloaded ``reg.remove`` are separate
    awaits, so a reconnect landing between them can re-establish a tunnel for
    the record while it is being deleted. The post-removal disconnect closes
    that window: once the record is gone, ``connect`` refuses the unknown id,
    so a final teardown after the successful remove cannot itself be raced —
    any tunnel it finds is the leftover of a reconnect that slipped in.
    """
    denied = _guard(request, "remove")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    reg = _registry(state)
    instance_id = request.match_info["id"]
    mgr = getattr(state, "instances_manager", None)
    if mgr is not None:
        await mgr.disconnect(instance_id)  # tear down any live tunnel first
    existed = await asyncio.to_thread(reg.remove, instance_id)
    if not existed:
        _audit("remove", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found"}, status=404)
    if mgr is not None:
        await mgr.disconnect(instance_id)  # sweep any reconnect that raced the removal
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
    if await asyncio.to_thread(_registry(state).get, instance_id) is None:
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


async def api_instances_search_sessions(request: web.Request) -> web.Response:
    """GET /api/instances/search-sessions — federated session search.

    Query params mirror ``/api/sessions/search`` (``q`` min 2 chars, ``limit``
    default 50 / max 200). Fans the query out to every CONNECTED instance's own
    ``/api/sessions/search`` (concurrently, over the already-open tunnels — the
    minted tokens never leave ``SshTunnelManager``) and runs the local search in
    the same gather, then rank-interleaves the sources: position k of the reply
    cycles through each source's k-th best hit, local first. Interleaving needs
    no cross-instance score wire format (each gateway may run a different
    ranking version), keeps every source represented in the top rows, and
    preserves each source's own order.

    Rows from a peer carry ``instance_id`` + ``instance_name``; local rows carry
    neither, so the reply shape for a hub with no peers degrades to exactly the
    local search's. Unreachable/refusing peers never fail the request: they are
    reported in ``unreachable`` as ``{id, name, code}`` so the UI can say "N
    instances unreachable" instead of silently narrowing the search.

    Same gate as every instances route (owner-only, non-Slack,
    ``instances.enabled``); peers' replies are untrusted input and are re-shaped
    and re-redacted locally before they reach the browser.
    """
    denied = _guard(request, "search_sessions")
    if denied is not None:
        return denied
    # STRICTER than _guard for this route: _guard's identity check is
    # deliberately permissive enough for send-session's app path (an app token
    # sets request["user"] and the transfer confines it with a per-slot
    # ownership check downstream). A bulk read has no per-slot confinement to
    # lean on — it discloses EVERY local and remote session's titles/snippets —
    # so it requires the positively-identified OWNER: not an app token, and not
    # a Slack user who minted a dashboard token via `!dashboard` (app == "" but
    # a non-owner subject).
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        _audit("search_sessions", "denied", error="non-owner identity rejected")
        return web.json_response(
            {"error": "federated session search is owner-only", "code": "owner_only"},
            status=403,
        )
    state: DashboardState = request.app["state"]
    q = sanitize_string(request.query.get("q", "")).strip()[:256]
    if len(q) < SEARCH_MIN_CHARS:
        # §6: every guarded call emits an SEL event, success and denial alike.
        # A sub-threshold query still passed the permission gate, so the
        # decision is recorded even though no search runs.
        _audit("search_sessions", "success", request_id="short-query")
        return web.json_response({"sessions": [], "unreachable": []})
    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 200))
    except (TypeError, ValueError):
        limit = 50

    mgr = getattr(state, "instances_manager", None)
    connected: list[str] = []
    peer_calls = []
    if mgr is not None:
        connected = [
            iid
            for iid, st in mgr.status_all().items()
            if getattr(st, "state", None) is TunnelState.CONNECTED
        ]
        peer_calls = [mgr.search_sessions_remote(iid, q, limit) for iid in connected]

    async def _local() -> list[dict]:
        if not state.conversation_log:
            return []
        return await asyncio.get_running_loop().run_in_executor(
            None, state.conversation_log.search_sessions, q, limit
        )

    results = await asyncio.gather(_local(), *peer_calls, return_exceptions=True)

    # Snapshot instance names ONCE, off the loop, before the merge. registry.get
    # acquires a threading lock and re-reads instances.json per call — a lock a
    # to_thread mutation worker may hold across its fsync — so resolving names
    # inline per peer row (up to limit x peers per keystroke) could freeze the
    # event loop for the fsync's duration. Same rule as every other registry
    # touch in these handlers (see api_instances_list).
    names: dict[str, str] = {}
    if connected:
        try:
            reg = _registry(state)
            names = {i.id: i.name for i in await asyncio.to_thread(reg.list)}
        except Exception:
            names = {}  # badge degrades to the instance id

    def _name(iid: str) -> str:
        return names.get(iid) or iid

    def _clean(row: object, iid: str) -> dict | None:
        """Re-shape one untrusted peer row: allowlist keys, coerce, re-redact."""
        if not isinstance(row, dict):
            return None
        key = row.get("key")
        if not isinstance(key, str) or not key or len(key) > _PEER_FIELD_MAX_CHARS:
            return None
        out: dict = {"key": key, "instance_id": iid, "instance_name": _name(iid)}
        for field in ("title", "snippet", "agent", "created", "folder_id", "memory_mode"):
            value = row.get(field)
            if isinstance(value, str) and value:
                # Length clamp BEFORE redaction: a hostile/broken peer must not
                # ship megabyte strings to the browser (or feed the redaction
                # regexes unbounded input).
                value = value[:_PEER_FIELD_MAX_CHARS]
                if field in ("title", "snippet"):
                    value, _ = _h.redact_exfiltration_urls(value)
                    value, _ = _h.redact_credentials(value)
                out[field] = value
        for field in ("modified", "messages"):
            value = row.get(field)
            # bool is an int subclass and 1e309 is a float — but True/Infinity
            # in these fields is peer garbage, and a non-finite value makes
            # json.dumps emit bare `Infinity`, which the browser's JSON.parse
            # rejects, losing the WHOLE federated response to one bad row.
            # isfinite itself raises OverflowError on an int too large for
            # float (peer JSON carries arbitrary-precision ints), so that
            # garbage is dropped the same way rather than crashing the merge.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    if math.isfinite(value):
                        out[field] = value
                except OverflowError:
                    pass
        return out

    sources: list[list[dict]] = []
    unreachable: list[dict] = []
    local_rows = results[0]
    if isinstance(local_rows, list):
        # Local redaction: /api/sessions/search redacts title/snippet in its
        # HANDLER, and this endpoint calls conversation_log.search_sessions
        # directly, bypassing that handler — so the same redaction must run
        # here before the rows reach the browser.
        redacted_local: list[dict] = []
        for row in local_rows:
            for field in ("title", "snippet"):
                value = row.get(field)
                if isinstance(value, str) and value:
                    value, _ = _h.redact_exfiltration_urls(value)
                    value, _ = _h.redact_credentials(value)
                    row[field] = value
            redacted_local.append(row)
        sources.append(redacted_local)
    for iid, result in zip(connected, results[1:]):
        if isinstance(result, BaseException):
            _audit("search_sessions", "failure", request_id=iid, error=type(result).__name__)
            unreachable.append({"id": iid, "name": _name(iid), "code": "search_unreachable"})
            continue
        ok, payload = result
        if not ok:
            code = str(payload.get("code", "search_unreachable"))
            # The code lands in the SEL audit trail too, so an operator can
            # tell a stale credential from a dead tunnel per peer after the
            # fact without reproducing the search.
            _audit("search_sessions", "failure", request_id=iid, error=code)
            unreachable.append({"id": iid, "name": _name(iid), "code": code})
            continue
        rows = payload.get("sessions")
        cleaned = []
        if isinstance(rows, list):
            for row in rows[:limit]:
                c = _clean(row, iid)
                if c is not None:
                    cleaned.append(c)
        sources.append(cleaned)

    merged: list[dict] = []
    for rank in range(limit):
        for source in sources:
            if rank < len(source):
                merged.append(source[rank])
            if len(merged) >= limit:
                break
        if len(merged) >= limit:
            break
    _audit("search_sessions", "success", request_id=f"{len(connected)} peers")
    return web.json_response({"sessions": merged, "unreachable": unreachable})


async def api_instances_send_session(request: web.Request) -> web.Response:
    """POST /api/instances/{id}/send-session — copy a local session to a peer.

    Body: ``{ "slot": "<local slot key>" }``.

    Copy semantics: the local session is left completely untouched, and the peer
    allocates a fresh key. Nothing here can delete or mutate a conversation on
    either side, which is why the route needs no confirmation step.

    The minted token is NOT part of this response — the transfer is performed
    inside ``SshTunnelManager.send_session_bundle`` precisely so that ``connect``
    and ``refresh-token`` stay the only two routes where a token crosses this
    boundary (see the module docstring and instances.md §6).
    """
    denied = _guard(request, "send_session")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    inst = await asyncio.to_thread(_registry(state).get, instance_id)
    if inst is None:
        _audit("send_session", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found", "code": "instance_not_found"}, status=404)
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("send_session", "denied", request_id=instance_id, error="manager unavailable")
        return web.json_response(
            {"error": "instances manager not running", "code": "instances_manager_down"},
            status=503,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "invalid JSON body", "code": "transfer_invalid_json"}, status=400
        )
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "transfer_body_not_object"},
            status=400,
        )
    slot_key = body.get("slot")
    if not isinstance(slot_key, str) or not slot_key:
        return web.json_response(
            {"error": "slot must be a non-empty string", "code": "transfer_missing_slot"},
            status=400,
        )
    slot = state._slots.get(slot_key)
    if slot is None:
        _audit("send_session", "denied", request_id=instance_id, error="slot not found")
        return web.json_response(
            {"error": "session not found", "code": "transfer_slot_not_found"}, status=404
        )
    # App-scope ownership, mirroring chat_fork. An app token sets request["user"]
    # so it clears _guard(), and an app whose manifest declares /api/instances
    # reaches here — without this it could name ANOTHER slot's key and have that
    # transcript copied to a peer, which is an exfiltration path out of the app
    # sandbox. 404 rather than 403, like fork: a slot owned by another app must be
    # indistinguishable from one that does not exist, or the error code itself
    # enumerates slots across the isolation boundary (CWE-204). The real reason is
    # recorded server-side in the audit event.
    request_app = request.get("app", "")
    if request_app and (not getattr(slot, "_app", "") or slot._app != request_app):
        _audit(
            "send_session",
            "denied",
            request_id=instance_id,
            error=f"app {request_app!r} does not own this slot",
        )
        return web.json_response(
            {"error": "session not found", "code": "transfer_slot_not_found"}, status=404
        )
    if slot.memory_mode != "persistent":
        # An incognito/temporary session has no durable transcript by design;
        # transferring one would defeat the mode the user deliberately chose.
        _audit("send_session", "denied", request_id=instance_id, error="non-persistent slot")
        return web.json_response(
            {
                "error": "cannot transfer a non-persistent session",
                "code": "transfer_slot_not_persistent",
            },
            status=400,
        )

    # The source is NOT flushed before bundling. A copy leaves the source
    # untouched, and build_transfer_bundle already merges the on-disk transcript
    # with the unflushed in-memory tail — so a flush here would add nothing
    # except a duplication bug: save_slot_off_loop writes the tail to disk
    # without clearing ``_dirty``, after which the bundle re-appends that same
    # tail from memory and every unsaved turn lands twice in the copy.
    try:
        bundle = await build_transfer_bundle_async(state, slot, origin=local_instance_label())
    except SnapshotUnstable:
        # No consistent view of the source: either a flush landed inside every
        # retry, or a rewind/regenerate rewrite is still owed so disk is stale.
        # Retryable, and the source is untouched.
        _audit("send_session", "failure", request_id=instance_id, error="snapshot unstable")
        return web.json_response(
            {
                "error": "the session could not be copied consistently right now; please retry",
                "code": "transfer_snapshot_unstable",
            },
            status=503,
        )
    ok, payload = await mgr.send_session_bundle(instance_id, bundle)
    if not ok:
        _audit(
            "send_session",
            "failure",
            request_id=instance_id,
            error=str(payload.get("code", "unknown")),
        )
        # Re-emit the peer's reason explicitly rather than forwarding *payload*
        # verbatim: the code must be statically visible in the response body
        # (test_error_code_contract.py), and spelling both fields out also
        # guarantees a code is present even if a future peer omits one.
        return web.json_response(
            {
                "error": payload.get("error", "the transfer failed"),
                "code": payload.get("code", "transfer_peer_refused"),
            },
            status=502,
        )
    _audit("send_session", "success", request_id=instance_id)
    return web.json_response(
        {
            "ok": True,
            "instance": instance_id,
            "remote_key": payload.get("key", ""),
            "messages": len(bundle.get("messages", [])),
            # Forwarded from the peer so the row can distinguish a full-fidelity
            # copy from one that degraded to the transcript-only prefix. Without
            # it a lossy transfer shows the same "Sent" as a resumable one -- the
            # silent degradation this feature exists to remove. Older peers omit
            # it; "" means unknown, which the UI treats as plain "Sent".
            "resume_mode": payload.get("resume_mode", ""),
        }
    )

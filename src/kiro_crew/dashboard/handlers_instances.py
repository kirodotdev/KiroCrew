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
import re
from typing import TYPE_CHECKING
from urllib.parse import unquote

from aiohttp import web

import kiro_crew
import kiro_crew.dashboard.handlers as _h
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.session_transfer import (
    SnapshotUnstable,
    build_transfer_bundle_async,
    local_instance_label,
)
from kiro_crew.history import SEARCH_MIN_CHARS
from kiro_crew.instances.constants import (
    PROXY_PATH_MAX_DECODE_PASSES,
    PROXY_REQUEST_BODY_MAX_BYTES,
)
from kiro_crew.instances.registry import (
    DEFAULT_REMOTE_PORT,
    DuplicateInstanceError,
    InstanceNotFoundError,
    InstancesError,
    InstancesRegistry,
    InvalidInstanceError,
    validate_ttl,
)
from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError, TunnelState
from kiro_crew.instances.warm_set import resolve_warm_set_cap
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


# The addressing fields Stop/Start/Delete resolve the real EC2 stack through
# (see coordsOf() in RemoteCrewPanel.tsx). Locked from PATCH for a correlated
# cloud instance — see _is_correlated_cloud_instance().
_ADDRESSING_FIELDS = {"connection_method", "ssm_target", "aws_profile", "aws_region"}


def _is_correlated_cloud_instance(ssm_target: str) -> bool:
    """True if *ssm_target* was provisioned by a Kiro Crew cloud launch.

    Deferred import: this is the one place the instances feature reaches into
    the cloud module, kept lazy so instances stays usable with the cloud
    module unavailable/import-broken (mirrors register_instance()'s own
    best-effort posture in cloud/connect.py). Only the import itself is
    best-effort (``ImportError`` -> not correlated, the "cloud feature
    absent" case) — a launch-job STORE read failure inside
    ``is_launched_instance()`` is a different failure mode and is NOT caught
    here, so it propagates to ``api_instances_update``, which fails the PATCH
    CLOSED rather than silently treating a possibly-launched instance as
    uncorrelated.
    """
    try:
        from kiro_crew.cloud.connect import is_launched_instance
    except ImportError:  # pragma: no cover - cloud feature absent
        return False
    return is_launched_instance(ssm_target)


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
    # Resolved here rather than served raw: the automatic mode (0) means "as many
    # as are connected", and this is the only place that holds both the stored
    # value and the live per-instance status. The browser therefore always
    # receives a concrete integer and needs no notion of automatic.
    connected = sum(1 for i in items if (i.get("status") or {}).get("state") == "connected")
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
            "warm_set_cap": resolve_warm_set_cap(
                KiroCrewConfig.load().instances.warm_set_cap, connected
            ),
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
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object", "code": "invalid_body"}, status=400
        )
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
    except DuplicateInstanceError as e:
        # Split from InvalidInstanceError because the two are different user
        # actions: a name collision is resolved by renaming, a rejected field by
        # correcting it. A client that cannot tell them apart has to parse prose.
        _audit("add", "denied", error=str(e))
        return web.json_response({"error": str(e), "code": "instance_duplicate"}, status=400)
    except InvalidInstanceError as e:
        _audit("add", "denied", error=str(e))
        return web.json_response({"error": str(e), "code": "instance_invalid"}, status=400)
    except (TypeError, ValueError) as e:
        _audit("add", "denied", error=str(e))
        return web.json_response(
            {"error": f"invalid field: {e}", "code": "invalid_field"}, status=400
        )
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
        return web.json_response({"error": "not found", "code": "instance_not_found"}, status=404)
    # Addressing fields resolve the real EC2 stack for Stop/Start/Delete, so
    # editing them on an instance Kiro Crew launched would strand a running,
    # billing instance with no dashboard path to reach it. Checked against the
    # `current` record already fetched above rather than re-reading the
    # registry, so this costs no extra (blocking) lookup.
    # Split rather than `and`-chained: mypy unifies the operand types of an
    # `and` expression, so folding the set-intersection test into the same
    # condition makes it infer to_thread's callable as returning set[str].
    correlated = False
    if _ADDRESSING_FIELDS & set(changes):
        try:
            correlated = await asyncio.to_thread(_is_correlated_cloud_instance, current.ssm_target)
        except Exception as exc:
            # The correlation check is what stands between a caller and
            # rewriting a launched instance's addressing fields out from
            # under Stop/Start/Delete, so a lookup failure here fails CLOSED
            # (refuse the edit, persist nothing) instead of falling back to
            # `correlated = False` and risking the exact stranding this lock
            # exists to prevent.
            logger.info(
                "correlation check failed for instance %r, refusing addressing edit: %s",
                instance_id,
                exc,
            )
            _audit(
                "update",
                "denied",
                request_id=instance_id,
                error=f"correlation check failed: {exc}",
            )
            return web.json_response(
                {
                    "error": (
                        "could not determine whether this instance's addressing "
                        "fields are locked (cloud launch store unreadable) — "
                        "refusing to edit connection_method/ssm_target/"
                        "aws_profile/aws_region; retry once the store is "
                        "reachable"
                    ),
                    "code": "cloud_instance_correlation_check_failed",
                },
                status=503,
            )
    if correlated:
        _audit(
            "update",
            "denied",
            request_id=instance_id,
            error="addressing fields locked: correlated cloud instance",
        )
        return web.json_response(
            {
                "error": (
                    "connection_method/ssm_target/aws_profile/aws_region cannot be "
                    "edited on an instance Kiro Crew launched — Stop/Start/Delete "
                    "resolve the real EC2 stack through these fields, so changing "
                    "them here would strand a running, billing instance with no "
                    "dashboard path to reach it"
                ),
                "code": "cloud_instance_addressing_locked",
            },
            status=400,
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
        return web.json_response({"error": str(e), "code": "instance_not_found"}, status=404)
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


def _connect_failure_code(body: dict, fallback: str) -> str:
    """Machine-readable ``code`` for a failed connect response.

    Promotes the failure-diagnosis ladder's own verdict (``ssh_unreachable``,
    ``remote_down``, ``tunnel_down``, …) to the top level, where a client reads
    it without walking into ``diagnosis``. Only a verdict that is present AND
    negative is promoted: the stored diagnosis is the last ladder RUN, so a stale
    ``ok`` from before the failure would otherwise be published as this call's
    reason. Without a usable verdict the caller's *fallback* names the stage that
    failed instead.
    """
    diagnosis = body.get("diagnosis")
    if isinstance(diagnosis, dict) and not diagnosis.get("ok"):
        code = diagnosis.get("code")
        if isinstance(code, str) and code:
            return code
    return fallback


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
        return web.json_response(
            {"error": "instances manager not running", "code": "instances_manager_unavailable"},
            status=503,
        )
    try:
        status = await mgr.connect(instance_id)
    except KeyError:
        _audit("connect", "denied", request_id=instance_id, error="not found")
        return web.json_response({"error": "not found", "code": "instance_not_found"}, status=404)
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
                body["code"] = _connect_failure_code(body, "instance_token_unconfirmed")
                return web.json_response(body, status=502)
        body["token"] = token  # delivered to owner only
        _audit("connect", "success", request_id=instance_id)
        return web.json_response(body)
    _audit("connect", "failure", request_id=instance_id, error=status.error)
    body["code"] = _connect_failure_code(body, "instance_connect_failed")
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
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        # Domain audit stays here rather than delegating to
        # ``require_owner_dashboard_request``: ``_audit`` emits
        # ``log_tool_invocation`` under ``instances_search_sessions`` /
        # ``dashboard:instances``, which is the record this module's SEL consumers
        # watch, and the shared helper's generic ``log_api_access`` /
        # ``non_owner_block`` would silently drop this route out of that stream.
        # Only the denial TAIL is shared -- see ``_owner_denial_response``.
        _audit("search_sessions", "denied", error="non-owner identity rejected")
        # Deny decision made above; only the response label changes for a signed
        # pre-owner bootstrap subject (see stale_owner_session_response).
        return _owner_denial_response(request, "federated session search is owner-only")
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

    # No flush HERE: the builder owns it. ``build_transfer_bundle_async`` flushes
    # a dirty slot itself (best_effort=False) and only then takes its boundary
    # slice, so by that point the tail is empty and the bundle comes wholly from
    # disk. A flush at this call site would add nothing — and the version of this
    # code that flushed here and then sliced on ``_resumed_count``, which the save
    # does NOT advance, is what re-appended the same tail from memory and landed
    # every unsaved turn twice in the copy.
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


# ── Generic chat proxy ────────────────────────────────────────────────────────

# Response headers forwarded from the peer to the browser — an explicit
# ALLOWLIST, not a hop-by-hop skip-list. Everything else (Set-Cookie, CSP,
# CORS, hop-by-hop per RFC 9110 §7.6.1) is dropped: the peer's cookie belongs
# to the peer's origin, and a compromised peer must not be able to plant
# headers (or a credential) on the hub origin.
_PROXY_RESP_ALLOW_HEADERS = frozenset(
    {
        "content-type",
        "cache-control",
        "x-accel-buffering",  # SSE: the peer disables proxy buffering; keep it
    }
)

# Response content types forwarded from the peer. The chat surface speaks JSON
# and SSE only; anything else — above all text/html — is refused so a
# compromised peer can never serve active content that executes on the
# authenticated hub origin.
_PROXY_RESP_ALLOW_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "text/event-stream",
    }
)

_PROXY_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


# The separator in a URL path, which is `/` on every platform (RFC 3986) —
# NOT the filesystem separator. Named rather than inlined because the two are
# genuinely different things: `pathlib`/`os.path.join` would be the WRONG tool
# here (on Windows they would emit `\`, which is not a URL separator and would
# corrupt every proxied request), and the repo's portability scan rightly asks
# any bare `"/"` split to say which of the two it means.
_URL_PATH_SEP = "/"

# One path segment the proxy will forward: unreserved characters plus the
# sub-delims a real endpoint or id uses. Deliberately an ALLOWLIST — it is what
# makes the rebuilt path safe to send verbatim, because no character in it can
# introduce a separator, a query, or another encoding layer. `.` is admitted
# (file-ish ids, version suffixes); a segment that is ONLY dots is rejected
# separately, since that is traversal rather than a name.
_PROXY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~:@!$&'()*+,;=-]+$")

# The peer surface the proxy will forward, as canonical segment prefixes — a
# positive ALLOWLIST, one named prefix per row. The proxy carries the
# remote-crew chat view and nothing else, so only the peer's `api/chat`
# subtree and its `api/stream` event feed are reachable (each a prefix grant:
# every route under it, including mutating ones — that breadth is the chat
# feature's own wire surface).
# Everything outside the named prefixes is refused — including the peer's own
# `api/instances` control plane (no chaining a hub through a peer into a
# third machine) and the peer's token-minting routes, whose JSON replies
# would otherwise carry a minted peer credential back through the hub
# in-band.
#
# `api/stream` is the peer's own SSE broadcast endpoint (its `api_stream`
# handler), the out-of-turn half of the chat view: the per-turn reply streams
# back from `api/chat`, while session-list and slot-state changes arrive here.
# It is deliberately SSE and not the sibling `api/ws`: a WebSocket row would
# need a `101 Switching Protocols` to cross this proxy, and the reply
# content-type gate below exists precisely to stop a peer serving anything but
# JSON/SSE onto the authenticated hub origin — an upgrade would tunnel straight
# through it. A GET returning `text/event-stream` needs no such exception.
#
# Note what this row admits: that feed is per-CLIENT but not per-slot, so a hub
# holding it receives the peer's whole notification/slot broadcast, not only the
# session on screen. That is peer content crossing to a hub user who is already
# the peer's owner (this route is owner-only), so it widens VOLUME, not
# privilege — but it is the reason this is a named row rather than a blanket
# `api/` grant.
#
# A new prefix is added HERE explicitly, never by widening the policy back to
# deny-only. The constant's exact value and row shape are pinned by tests.
_PROXY_ALLOWED_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("api", "chat"),
    ("api", "stream"),
)

# Derived from the allowlist so the refusal (and its SEL audit line) stays
# honest as rows are added.
_PROXY_PATH_DENIED_REASON = "path is outside the proxied peer surface (%s)" % ", ".join(
    _URL_PATH_SEP.join(prefix) for prefix in _PROXY_ALLOWED_PREFIXES
)


def _proxy_canonical_path(raw: str) -> tuple[str, str]:
    """Canonicalize *raw* into a forwardable path, or return a denial reason.

    Returns ``(path, "")`` on success and ``("", reason)`` on refusal.

    Built as a CONSTRUCTION, not a series of pattern checks: the earlier
    denylist shape (reject ``..``, reject an ``api/instances`` prefix) inspected
    a half-decoded string while the peer resolved the fully-decoded one, so any
    extra encoding layer — ``api/%252e%252e/api/instances/...``, which the
    router hands over as ``api/%2e%2e/...`` — passed every check and then
    normalized back into the control plane the checks existed to protect.

    So: decode to a fixed point FIRST, admit only plainly-named segments, and
    rebuild the outbound path from exactly the segments that were vetted. The
    proxy exists for the remote-crew chat surface, not as a general tunnel-HTTP
    escape hatch, so the vetted shape is narrow on purpose — only the prefixes
    in `_PROXY_ALLOWED_PREFIXES` are forwarded. Allowing the needed surface
    (rather than denying known-bad prefixes) is what keeps every peer route the
    feature never asked for — the `api/instances` control plane, the peer's
    token-minting routes, anything added to the peer later — unreachable by
    default instead of proxied silently.
    """
    path = raw
    for _ in range(PROXY_PATH_MAX_DECODE_PASSES):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    else:
        return "", "path encoding is too deeply nested"
    # After the fixed point a `%` can only be a malformed escape (a well-formed
    # one would have decoded); refusing it keeps "decoded" honest.
    if "%" in path:
        return "", "malformed percent-encoding in path"
    segments = path.strip(_URL_PATH_SEP).split(_URL_PATH_SEP)
    for seg in segments:
        if not seg:
            return "", "empty path segment"
        if not seg.strip("."):
            return "", "path traversal"
        if not _PROXY_SEGMENT_RE.match(seg):
            return "", "illegal character in path segment"
    for prefix in _PROXY_ALLOWED_PREFIXES:
        # A malformed row must fail CLOSED: an empty row would prefix-match
        # everything and a one-segment ("api",) row would restore the whole
        # peer /api/ surface. Rows shallower than two segments are ignored
        # here (and refused by the constant's shape test).
        if len(prefix) >= 2 and tuple(segments[: len(prefix)]) == prefix:
            return _URL_PATH_SEP.join(segments), ""
    return "", _PROXY_PATH_DENIED_REASON


#: Caps applied to a peer's capability reply before it reaches the browser. The
#: peer is the user's own machine but its reply is still untrusted input crossing
#: a trust boundary, and these lists feed pickers — an unbounded roster would
#: render an unusable menu and an unbounded string would break the layout.
_CAP_MAX_ROWS = 500
_CAP_MAX_STR = 512
_CAP_MAX_VERSION_STR = 64


def _cap_str(value: object, limit: int = _CAP_MAX_STR) -> str:
    """A peer-supplied string, redacted and clamped, or "" for anything else.

    Every one of these lands in a dashboard picker, so the peer's text is an
    output boundary: it runs the relay's credential + exfiltration-URL chain
    BEFORE the clamp, so a credential cannot survive by sitting past the limit.
    """
    if not isinstance(value, str):
        return ""
    # Deferred: this module is imported on the gateway's boot path and the relay
    # pulls in the chat-slot machinery, which a capability read does not need.
    from kiro_crew.dashboard.remote_relay import redact_peer_text

    return redact_peer_text(sanitize_string(value))[:limit]


def _cap_list(payload: object, key: str) -> object:
    """The row list out of a peer reply that is either bare or wrapped in *key*.

    The five capability endpoints do not agree on a shape: ``/api/models`` and
    ``/api/effort-levels`` answer a bare list, while ``/api/agents`` and
    ``/api/workspaces`` answer ``{"<key>": [...]}`` next to a sibling default.
    Normalizing here is what keeps a wrapped reply from reaching ``_cap_rows``,
    which accepts only a list — a dict would come back as an empty picker rather
    than an error, so the failure would look like "this crew has no agents".
    """
    return payload.get(key) if isinstance(payload, dict) else payload


def _cap_rows(payload: object, fields: dict[str, int]) -> list[dict[str, object]]:
    """Re-shape a peer's list reply to *fields*, dropping everything unlisted.

    Allowlist, not passthrough: the browser gets the keys this gateway knows how
    to render and nothing else, so a peer on a build with extra fields cannot
    inject content into a picker. ``context_window`` is the one numeric field and
    is coerced rather than clamped — a bogus value reads as "unknown", which the
    frontend already handles by falling back to the reference window.
    """
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, object]] = []
    for entry in payload[:_CAP_MAX_ROWS]:
        if not isinstance(entry, dict):
            continue
        row: dict[str, object] = {}
        for field, limit in fields.items():
            raw = entry.get(field)
            if field == "context_window":
                row[field] = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else 0
            else:
                row[field] = _cap_str(raw, limit)
        rows.append(row)
    return rows


async def api_instances_capabilities(request: web.Request) -> web.Response:
    """GET /api/instances/{id}/capabilities — what a connected peer can do.

    A local session bound to a peer for execution must offer the PEER's agents,
    models, effort levels and workspaces in its header — showing this machine's
    would let the user pick a crew or model that does not exist over there. Those
    rosters are gateway-wide reads, and every existing frontend control fetches
    them same-origin from the local gateway, so this route is the per-instance
    counterpart the frontend switches to when the active slot is peer-bound.

    Deliberately NOT served through ``/api/instances/{id}/proxy/*``: that route
    forwards a caller-supplied path and is fenced to the ``api/chat`` /
    ``api/stream`` prefixes. Widening it to reach ``api/agents`` would have
    granted the peer's mutating ``PUT /api/agents/{name}`` in the same stroke,
    because the fence matches prefixes and cannot distinguish verbs. So the reads
    go through ``SshTunnelManager.peer_capability``, whose target is chosen from a
    closed set in the backend.

    One peer read failing does not fail the request: each result is independent
    and a missing one is reported in ``unavailable`` so the frontend can disable
    exactly that control instead of showing an empty menu that looks like the peer
    has no models.
    """
    denied = _guard(request, "capabilities")
    if denied is not None:
        return denied
    # Owner-only, same bar as the proxy and the federated search: the reads run
    # on a peer with the OWNER's manager-held credential, so a Slack-minted
    # `!dashboard` subject (an authenticated non-owner) must not reach them.
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        _audit("capabilities", "denied", error="non-owner identity rejected")
        return _owner_denial_response(request, "remote-crew capabilities are owner-only")
    state: DashboardState = request.app["state"]
    instance_id = request.match_info.get("id", "")
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("capabilities", "denied", error="instances manager unavailable")
        return web.json_response(
            {"error": "remote crews are not available", "code": "instances_unavailable"},
            status=503,
        )

    paths = ("/api/version", "/api/agents", "/api/models", "/api/effort-levels", "/api/workspaces")
    results = await asyncio.gather(
        *(mgr.peer_capability(instance_id, path) for path in paths),
        return_exceptions=True,
    )
    raw: dict[str, object] = {}
    unavailable: dict[str, str] = {}
    for path, outcome in zip(paths, results):
        field = path.rsplit("/", 1)[-1].replace("-", "_")
        if isinstance(outcome, BaseException):
            logger.info(
                "Peer capability %s on %s raised (%s)",
                path,
                instance_id,
                type(outcome).__name__,
            )
            unavailable[field] = "capability_unreachable"
            continue
        ok, payload = outcome
        if ok:
            raw[field] = payload
        else:
            unavailable[field] = (
                _cap_str(payload.get("code"), 64)
                if isinstance(payload, dict)
                else "capability_error"
            )

    peer_version = ""
    version_payload = raw.get("version")
    if isinstance(version_payload, dict):
        peer_version = _cap_str(version_payload.get("version"), _CAP_MAX_VERSION_STR)
    agents_payload = raw.get("agents")
    workspaces_payload = raw.get("workspaces")
    workspaces = _cap_rows(
        _cap_list(workspaces_payload, "workspaces"), {"name": 128, "path": _CAP_MAX_STR}
    )
    effort_payload = _cap_list(raw.get("effort_levels"), "effort_levels")
    effort_levels = (
        [_cap_str(level, 32) for level in effort_payload[:_CAP_MAX_ROWS] if isinstance(level, str)]
        if isinstance(effort_payload, list)
        else []
    )

    _audit("capabilities", "success", request_id=instance_id)
    return web.json_response(
        {
            "instance_id": instance_id,
            "version": peer_version,
            "local_version": kiro_crew.__version__,
            # The gate the relay enforces on every dispatch, surfaced so the UI
            # can explain a refusal BEFORE the user types a message rather than
            # after their first send fails.
            "version_match": bool(peer_version) and peer_version == kiro_crew.__version__,
            "agents": _cap_rows(
                _cap_list(agents_payload, "agents"),
                {"name": 128, "description": _CAP_MAX_STR, "scope": 32, "model": 128},
            ),
            # What answers when the bound session has picked nothing. A local
            # session falls back to THIS machine's default agent, which for a
            # peer-bound session would name a crew the peer may not have — so the
            # shelf needs the peer's own default to render honestly before the
            # first pick.
            "default_agent": (
                _cap_str(agents_payload.get("default_agent"), 128)
                if isinstance(agents_payload, dict)
                else ""
            ),
            "models": _cap_rows(
                _cap_list(raw.get("models"), "models"),
                {
                    "model_name": 128,
                    "display_name": 128,
                    "description": _CAP_MAX_STR,
                    "context_window": 0,
                },
            ),
            "effort_levels": effort_levels,
            "workspaces": workspaces,
            "default_workspace": (
                _cap_str(workspaces_payload.get("default"), 128)
                if isinstance(workspaces_payload, dict)
                else ""
            ),
            "unavailable": unavailable,
        }
    )


async def api_instances_proxy(request: web.Request) -> web.StreamResponse:
    """ANY /api/instances/{id}/proxy/{path} — forward to a connected peer.

    The carrier for the remote-crew chat view (design: remote-crew-chat): the
    browser talks same-origin to the hub, the hub forwards over the already-open
    tunnel using the manager-held credential, and the reply — including a
    minutes-long SSE chat stream — is pumped back chunk-by-chunk. The peer's
    token never reaches the browser, no browser Origin or cookies are forwarded
    to the peer (the hub presents as a same-origin loopback client), and the
    peer's Set-Cookie never reaches the hub origin.
    """
    denied = _guard(request, "proxy")
    if denied is not None:
        return denied
    # Owner-only, strictly: `_guard` verifies an authenticated dashboard subject,
    # but a Slack-invited user who minted a `!dashboard` link is such a subject
    # too (app == "" with a non-owner identity). The proxy executes on a peer
    # with the OWNER's manager-held credential, so it requires the positively
    # identified owner — the same bar api_instances_search_sessions sets, for
    # the same reason.
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        # Domain audit stays here for the same reason as
        # api_instances_search_sessions above: ``_audit`` is this module's
        # ``instances_*`` SEL stream. Only the denial tail is shared.
        _audit("proxy", "denied", error="non-owner identity rejected")
        # Deny decision made above; only the response label changes for a signed
        # pre-owner bootstrap subject (see stale_owner_session_response).
        return _owner_denial_response(request, "remote-crew proxy is owner-only")
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    path = request.match_info.get("path", "")
    if request.method.upper() not in _PROXY_METHODS:
        _audit("proxy", "denied", request_id=instance_id, error="method not allowed")
        return web.json_response(
            {"error": "method not allowed", "code": "proxy_method_not_allowed"}, status=405
        )
    # The forwarded path is the CANONICAL one this returns, never the raw
    # match_info: vetting one string and sending another is the gap that let a
    # double-encoded traversal through.
    path, reason = _proxy_canonical_path(path)
    if reason:
        _audit("proxy", "denied", request_id=instance_id, error=reason)
        return web.json_response({"error": reason, "code": "proxy_path_denied"}, status=400)
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("proxy", "failure", request_id=instance_id, error="manager unavailable")
        return web.json_response(
            {"error": "instances manager unavailable", "code": "instances_manager_unavailable"},
            status=503,
        )

    # Forward the query WITHOUT the hub's own credential: the browser may
    # authenticate this request with ?token=<hub token>, and forwarding it
    # verbatim would hand the peer a replayable credential for THIS gateway.
    # The peer-side credential is the manager-held cookie; nothing from the
    # browser's auth material may cross the tunnel.
    params = {k: v for k, v in request.query.items() if k != "token"}

    # Bound the inbound body BEFORE buffering (mirrors the federated-search
    # reply cap): the hub must not hold unbounded bytes for either side.
    body = b""
    if request.body_exists:
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.content.iter_chunked(65536):
            received += len(chunk)
            if received > PROXY_REQUEST_BODY_MAX_BYTES:
                _audit("proxy", "denied", request_id=instance_id, error="body too large")
                return web.json_response(
                    {"error": "request body too large", "code": "proxy_body_too_large"},
                    status=413,
                )
            chunks.append(chunk)
        body = b"".join(chunks)

    try:
        async with mgr.proxy_request(
            instance_id,
            request.method.upper(),
            path,
            params=params,
            data=body or None,
            content_type=request.headers.get("Content-Type", ""),
        ) as upstream:
            # Content-type gate: JSON and SSE only. A compromised peer must
            # not serve HTML (or anything active) that would execute on the
            # authenticated hub origin.
            upstream_ct = (upstream.headers.get("Content-Type") or "").split(";")[0].strip()
            if upstream_ct.lower() not in _PROXY_RESP_ALLOW_CONTENT_TYPES:
                _audit(
                    "proxy",
                    "denied",
                    request_id=instance_id,
                    error=f"peer content type refused: {upstream_ct or '(none)'}",
                )
                return web.json_response(
                    {
                        "error": "peer returned a content type the proxy does not forward",
                        "code": "proxy_content_type_refused",
                    },
                    status=502,
                )
            resp = web.StreamResponse(status=upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() in _PROXY_RESP_ALLOW_HEADERS:
                    resp.headers[key] = value
            resp.headers["X-Content-Type-Options"] = "nosniff"
            await resp.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
            except ConnectionResetError:
                # Browser went away mid-stream; the peer finishes its turn on
                # its own (its transcript is authoritative — see design doc).
                # Deliberately NOT catching asyncio.CancelledError alongside it:
                # absorbing a cancel would defeat cooperative shutdown.
                #
                # RETURN rather than fall through: write_eof() on the transport
                # that just refused a write raises again, and that second
                # exception escapes the handler. There is also nothing to audit
                # as a success — the response never completed.
                _audit("proxy", "partial", request_id=instance_id, error="client disconnected")
                return resp
            await resp.write_eof()
            _audit("proxy", "success", request_id=instance_id)
            return resp
    except ProxyRequestError as e:
        _audit("proxy", "failure", request_id=instance_id, error=e.code)
        # Literal statuses on purpose: the error-code contract ratchets
        # ``status=<expression>`` sites, and the carrier only ever suggests
        # 503 (not connected / no credential) or 502 (peer-side failure).
        if e.http_status == 503:
            return web.json_response({"error": e.message, "code": e.code}, status=503)
        return web.json_response({"error": e.message, "code": e.code}, status=502)

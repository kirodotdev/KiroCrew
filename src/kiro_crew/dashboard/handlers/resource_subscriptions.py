"""HTTP routes behind the resource-subscription tools and CLI (RFC Phase 4).

``rfc-mcp-resource-subscriptions.md`` §4.8 and §4.9.3. The holder service lives
in the gateway process; the three ``kirocrew-core`` tools reach it through the
session routes here, and ``kirocrew resource-subs remove`` through the operator
route.

* **Internal secret.** Every route answers only a request that presented
  ``X-Internal-Secret`` (``request["internal_auth"]``), checked before the
  session key is read; any other caller gets a 403 ``internal_secret_required``
  (:func:`_require_internal`).
* **Session routes.** The subscription a request touches is derived from the
  CALLING SESSION's identity (``X-Session-Key``, vetted by
  ``_recognize_session``), never from the body, so a session can only subscribe,
  list or remove its own. An id that belongs to another session is answered
  ``not_found`` and never listed.
* **Operator route.** Lists or removes any session's subscription for the CLI.
  It refuses a caller that names a session, so a session cannot reach it.

Past the secret check, a refusal is a 200 carrying
``{"ok": false, "refused": <code>, "reason": ...}``: the code is the contract
the tools relay, and the reason is for the model.

Every handler imports the service module at call time. The module is the
feature, and with ``mcp_gateway.resource_subscriptions.enabled`` off nothing on
the boot path may import it (``no-new-work-on-gateway-boot-path``).
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any, Optional

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.cron import _recognize_session
from kiro_crew.history import is_incognito_transcript
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.mcp_gateway.resource_subscription_store import HolderRecord
    from kiro_crew.mcp_gateway.resource_subscriptions import ResourceSubscriptionService

logger = logging.getLogger(__name__)

_SERVICE_MODULE = "kiro_crew.mcp_gateway.resource_subscriptions"

#: The refusal codes of RFC §4.9.3 decided here rather than by the service.
REFUSED_DISABLED = "disabled"
REFUSED_IDENTITY = "identity"
REFUSED_NO_DASHBOARD_SLOT = "no_dashboard_slot"
REFUSED_SERVER_REFUSED = "server_refused"
REFUSED_GATEWAY_UNAVAILABLE = "gateway_unavailable"
NOT_FOUND = "not_found"
OPERATOR_STARTING = "starting"

#: Longest instruction a subscription keeps. It rides every wake, so it is bounded
#: where it is retained.
INSTRUCTION_MAX_CHARS = 2000
#: Longest server name accepted; real names are tens of characters.
SERVER_MAX_CHARS = 128


def _refused(code: str, reason: str, *, running: Optional[bool] = None) -> web.Response:
    payload: dict[str, Any] = {"ok": False, "refused": code, "reason": reason}
    if running is not None:
        payload["running"] = running
    return web.json_response(payload)


def _switch_on() -> bool:
    """``mcp_gateway.resource_subscriptions.enabled`` in the live config."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = live.snapshot() or KiroCrewConfig.load()
    return bool(cfg.mcp_gateway.resource_subscriptions.enabled)


def _service() -> Optional["ResourceSubscriptionService"]:
    """The running service, without importing the module when nothing started it."""
    module = sys.modules.get(_SERVICE_MODULE)
    return module.active_service() if module is not None else None


def _unavailable(caller: str, operation: str) -> web.Response:
    if not _switch_on():
        code = REFUSED_DISABLED
        reason = "resource subscriptions are off (mcp_gateway.resource_subscriptions.enabled)"
    else:
        code = REFUSED_GATEWAY_UNAVAILABLE
        reason = (
            "resource subscriptions are on but their service is not running yet; "
            "the switch takes effect after a restart"
        )
    _audit(caller, operation, "denied", f"refused={code}")
    return _refused(code, reason)


def _row(record: "HolderRecord", uri: str) -> dict[str, Any]:
    """One subscription as the tools and the CLI show it."""
    meta = record.subscriptions.get(uri)
    if record.paused_reason:
        state, reason = "paused", record.paused_reason
    elif uri in record.paused_uris:
        state, reason = "paused", record.paused_uris[uri]
    else:
        state, reason = "active", ""
    from kiro_crew.dashboard.resource_wakes import WAKES_PER_DAY, utc_day

    wakes = meta.wakes if meta is not None and meta.wake_day == utc_day() else 0
    if state == "active" and wakes >= WAKES_PER_DAY:
        state, reason = "paused", f"{WAKES_PER_DAY} wakes today; resumes at the next UTC day"
    return {
        "id": meta.id if meta is not None else "",
        "server": record.server,
        "uri": uri,
        "instruction": meta.instruction if meta is not None else "",
        "state": state,
        "reason": reason,
        "wakes_today": wakes,
    }


def rows_for(records: "list[HolderRecord]") -> list[dict[str, Any]]:
    """Every subscription in *records*, each as :func:`_row`, in a stable order."""
    rows = []
    for record in records:
        for uri in [*record.uris, *(u for u in record.paused_uris if u not in record.uris)]:
            row = _row(record, uri)
            row["session_key"] = record.session_key
            rows.append(row)
    return sorted(rows, key=lambda r: (r["session_key"], r["server"], r["uri"]))


def _require_internal(request: web.Request, operation: str) -> Optional[web.Response]:
    """The audited 403 for a caller that did not present ``X-Internal-Secret``.

    Every route here is on ``_STRICT_INTERNAL_API_PATHS``, and strict is not
    self-enforcing at the handler: on loopback a request without the secret
    header falls through to cookie auth and is granted, and ``local_only=False``
    makes every strict path a mixed one. ``X-Session-Key`` is the identity these
    routes act on and nothing ties it to a cookie's or an app token's principal,
    so without this check a page holding only the dashboard cookie could
    subscribe a live session with an instruction of its choosing, list its
    subscriptions, or remove any session's through the operator route.
    ``internal_auth`` is set only after a constant-time secret match, so this one
    check closes the cookie path, the app-token path and the non-loopback
    reclassification. The three tools and ``kirocrew resource-subs remove`` send
    the secret.

    An internal caller whose identity resolves to an app is admitted, unlike on
    the agent-panel publish route, which resolves the session to a crew the app
    may not own: here the app is derived from the same ``X-Session-Key`` the
    route acts on, so an app-owned session reaches only its own subscriptions.
    """
    if request.get("internal_auth") is True:
        return None
    _audit(
        request.headers.get("X-Session-Key", ""), operation, "denied", "internal_secret_required"
    )
    return web.json_response({"error": "forbidden", "code": "internal_secret_required"}, status=403)


async def _caller(request: web.Request, operation: str) -> tuple[str, Optional[web.Response]]:
    """The vetted calling session, or the refusal to return.

    ``_recognize_session`` audits the recognition as allowed, so each refusal it
    lets through here is audited as denied with its reason, as
    ``agent_panel_publish`` does for its restricted-session refusal.
    """
    refusal = _require_internal(request, operation)
    if refusal is not None:
        return "", refusal
    state = request.app["state"]
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state, sk, operation, blocks_persisted_mode=is_incognito_transcript
    )
    if refusal is not None:
        return "", refusal
    if sk == "dashboard:ui":
        _audit(sk, operation, "denied", "dashboard_ui")
        return "", _refused(REFUSED_IDENTITY, "a subscription belongs to a session, not the UI")
    if _is_restricted_session(state, request):
        _audit(sk, operation, "denied", "restricted_session")
        return "", _refused(
            REFUSED_IDENTITY, "resource subscriptions are not available in this session mode"
        )
    return sk, None


def _audit(caller: str, operation: str, outcome: str, resources: str) -> None:
    try:
        sel().log_api_access(
            caller=caller or "unknown",
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )
    except Exception:  # noqa: BLE001 - audit must not undo the action
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _invalid(caller: str, operation: str, code: str, error: str) -> web.Response:
    _audit(caller, operation, "denied", f"refused={code}")
    return web.json_response({"error": error, "code": code}, status=400)


def _removal_refusal(
    service: "ResourceSubscriptionService",
    session_key: str,
    caller: str,
    operation: str,
    subscription_id: str,
    *,
    running: Optional[bool] = None,
) -> web.Response:
    if service.find_subscription(session_key, subscription_id) is None:
        code = NOT_FOUND
        reason = f"no subscription {subscription_id}"
    else:
        code = REFUSED_GATEWAY_UNAVAILABLE
        reason = "the removal did not commit; try again"
    _audit(caller, operation, "denied", f"id={subscription_id} refused={code}")
    return _refused(code, reason, running=running)


async def _body(request: web.Request) -> Optional[dict[str, Any]]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - any parse failure is the same bad request
        return None
    return body if isinstance(body, dict) else None


async def api_resource_subscribe(request: web.Request) -> web.Response:
    """POST /api/resource-subscriptions — subscribe the calling session (§4.9.3)."""
    operation = "resource_subscribe"
    sk, refusal = await _caller(request, operation)
    if refusal is not None:
        return refusal
    body = await _body(request)
    if body is None:
        return _invalid(sk, operation, "invalid_body", "request body must be a JSON object")
    server, uri, instruction = body.get("server"), body.get("uri"), body.get("instruction", "")
    if not isinstance(server, str) or not server or len(server) > SERVER_MAX_CHARS:
        return _invalid(
            sk,
            operation,
            "invalid_server_name",
            f"server must be a name of at most {SERVER_MAX_CHARS} characters",
        )
    if not isinstance(uri, str) or not uri:
        return _invalid(sk, operation, "invalid_uri", "uri must be a non-empty string")
    if not isinstance(instruction, str) or len(instruction) > INSTRUCTION_MAX_CHARS:
        return _invalid(
            sk,
            operation,
            "invalid_instruction",
            f"instruction must be at most {INSTRUCTION_MAX_CHARS} characters",
        )
    service = _service()
    if service is None or not _switch_on():
        return _unavailable(sk, operation)
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key

    if not dashboard_slot_key(sk):
        _audit(sk, operation, "denied", f"server={server} refused={REFUSED_NO_DASHBOARD_SLOT}")
        return _refused(
            REFUSED_NO_DASHBOARD_SLOT,
            "this session has no dashboard tab to wake; resource subscriptions are "
            "for dashboard sessions",
        )
    from kiro_crew.mcp_gateway.resource_subscriptions import SubscribeRefused

    try:
        reply = await service.subscribe(sk, server, uri, instruction=instruction)
    except SubscribeRefused as exc:
        _audit(sk, operation, "denied", f"server={server} refused={exc.code}")
        return _refused(exc.code, str(exc))
    if "error" in reply:
        _audit(sk, operation, "denied", f"server={server} refused={REFUSED_SERVER_REFUSED}")
        return web.json_response(
            {
                "ok": False,
                "refused": REFUSED_SERVER_REFUSED,
                "reason": f"{server!r} refused the subscribe",
                "server_error": reply["error"],
            }
        )
    meta = service.subscription(sk, server, uri)
    _audit(sk, operation, "allowed", f"server={server} id={meta.id if meta else ''}")
    return web.json_response(
        {
            "ok": True,
            "id": meta.id if meta is not None else "",
            "server_result": reply.get("result"),
        }
    )


async def api_resource_subscriptions_list(request: web.Request) -> web.Response:
    """GET /api/resource-subscriptions — the calling session's own subscriptions."""
    operation = "resource_subscriptions"
    sk, refusal = await _caller(request, operation)
    if refusal is not None:
        return refusal
    service = _service()
    if service is None:
        return _unavailable(sk, operation)
    rows = rows_for(service.records(sk))
    for row in rows:
        row.pop("session_key")
    return web.json_response({"ok": True, "subscriptions": rows})


async def api_resource_unsubscribe(request: web.Request) -> web.Response:
    """POST /api/resource-subscriptions/remove — remove one of the caller's own."""
    operation = "resource_unsubscribe"
    sk, refusal = await _caller(request, operation)
    if refusal is not None:
        return refusal
    body = await _body(request)
    subscription_id = body.get("subscription_id") if body is not None else None
    if not isinstance(subscription_id, str) or not subscription_id:
        return _invalid(
            sk,
            operation,
            "invalid_subscription_id",
            "subscription_id must be a string",
        )
    service = _service()
    if service is None:
        return _unavailable(sk, operation)
    found = service.find_subscription(sk, subscription_id)
    if found is None:
        _audit(sk, operation, "denied", f"id={subscription_id} refused={NOT_FOUND}")
        return _refused(NOT_FOUND, f"this session has no subscription {subscription_id}")
    record, uri = found
    from kiro_crew.mcp_gateway.resource_subscriptions import SubscribeRefused

    try:
        removed = await service.unsubscribe(
            sk, record.server, uri, expected_subscription_id=subscription_id
        )
    except SubscribeRefused as exc:
        _audit(sk, operation, "denied", f"id={subscription_id} refused={exc.code}")
        return _refused(exc.code, str(exc))
    if not removed:
        return _removal_refusal(service, sk, sk, operation, subscription_id)
    _audit(sk, operation, "allowed", f"id={subscription_id}")
    return web.json_response({"ok": True, "removed": subscription_id})


async def api_resource_subscriptions_operator(request: web.Request) -> web.Response:
    """POST ``{"subscription_id"}``: remove any session's subscription.

    ``kirocrew resource-subs remove``. A caller naming a session is refused: this
    route acts across sessions, which no session may do. ``running`` false tells
    the CLI to edit the store itself. Every answer an internal caller gets from an
    existing service says ``running`` true, including startup and not-found
    refusals.
    """
    refusal = _require_internal(request, "resource_subs_operator")
    if refusal is not None:
        return refusal
    service = _service()
    if request.headers.get("X-Session-Key"):
        _audit(request.headers["X-Session-Key"], "resource_subs_operator", "denied", "session")
        return web.json_response(
            {
                "error": "operator route; a session cannot call it",
                "code": "operator_only",
                "running": service is not None,
            },
            status=403,
        )
    body = await _body(request)
    subscription_id = body.get("subscription_id") if body is not None else None
    if not isinstance(subscription_id, str) or not subscription_id:
        return web.json_response(
            {
                "error": "subscription_id must be a string",
                "code": "invalid_subscription_id",
                "running": service is not None,
            },
            status=400,
        )
    if service is None:
        return web.json_response({"ok": False, "running": False})
    if not service.started:
        _audit(
            "cli",
            "resource_subs_remove",
            "denied",
            f"id={subscription_id} refused={OPERATOR_STARTING}",
        )
        return _refused(
            OPERATOR_STARTING,
            "the gateway subscription service is starting; try again",
            running=True,
        )
    from kiro_crew.mcp_gateway.resource_subscriptions import SubscribeRefused

    for record in service.records():
        found = service.find_subscription(record.session_key, subscription_id)
        if found is not None:
            try:
                removed = await service.unsubscribe(
                    record.session_key,
                    found[0].server,
                    found[1],
                    expected_subscription_id=subscription_id,
                )
            except SubscribeRefused as exc:
                _audit(
                    "cli",
                    "resource_subs_remove",
                    "denied",
                    f"id={subscription_id} refused={exc.code}",
                )
                return _refused(exc.code, str(exc), running=True)
            if not removed:
                return _removal_refusal(
                    service,
                    record.session_key,
                    "cli",
                    "resource_subs_remove",
                    subscription_id,
                    running=True,
                )
            _audit("cli", "resource_subs_remove", "allowed", f"id={subscription_id}")
            return web.json_response({"ok": True, "running": True, "removed": subscription_id})
    return _refused(NOT_FOUND, f"no subscription {subscription_id}", running=True)

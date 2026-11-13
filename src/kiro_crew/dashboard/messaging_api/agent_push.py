"""``POST /api/notifications/agent``: an agent session's ``send_notification`` push,
published on the ``system.agent`` channel of the notification bus.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        MAX_SHORT_STRING,
        DashboardState,
        NotificationPayload,
        NotificationValidationError,
        _publishing_session_meta,
        _redact_note_value,
        _sel,
        attributable_session_key,
        caller_names_a_missing_slot,
        logger,
        member_identity_for_session,
        read_bounded_json,
    )


def _refuse_unattributable_caller(
    request: web.Request, state: DashboardState, operation: str
) -> web.Response | None:
    """The refusal for a caller that may not act on system.agent notes, or None.

    Publishing and dismissing share it: both act as ``source="system"`` on the
    system.agent channel, so the same three callers are refused for both.
    """
    # App tokens must never reach this endpoint: an app's
    # declared ``permissions.api`` uses prefix-boundary matching, so an app
    # allowed ``/api/notifications`` is also admitted to this child route by
    # the auth middleware. This publish path is MCP/internal-secret only —
    # it publishes ``source="system"`` (channel system.agent), so an app
    # reaching it could impersonate system notifications and bypass its app
    # rate limits / declared-channel checks. Apps publish through
    # POST /api/notifications where their
    # token-verified ``app:<name>`` source is enforced. The middleware publishes
    # ``request["app"]`` on app-token auth and also on the internal-secret path
    # whenever the calling session resolves to an app, so this check bites for
    # an app agent arriving over MCP too.
    if request.get("app"):
        # Permission denial on a security boundary — audited before the
        # response (backend-security-controls: every denial emits SEL).
        _sel().log_api_access(
            caller=f"app:{request.get('app')}",
            operation=operation,
            outcome="denied",
            source="notifications_api",
            error="app tokens forbidden on the agent publish path",
        )
        return web.json_response({"error": "forbidden for app tokens"}, status=403)
    # MCP/internal-secret ONLY: the strict-internal
    # middleware also admits loopback dashboard-COOKIE callers to this
    # route, and a browser-credentialed caller publishing source="system"
    # would bypass MCP governance. The middleware sets
    # request["internal_auth"] only on the
    # validated X-Internal-Secret path — exactly the transport the
    # send_notification tool uses.
    if not request.get("internal_auth"):
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation=operation,
            outcome="denied",
            source="notifications_api",
            error="internal-secret authentication required (cookie callers forbidden)",
        )
        return web.json_response({"error": "internal-secret authentication required"}, status=403)
    # A caller whose own slot is GONE cannot be attributed. The
    # app-token check above refuses an app by name, but a tab closed while this
    # call was in flight takes the ``_app`` that check reads with it, so an
    # app-owned session going through that race would publish source="system"
    # here as though it were the person. Absence of an app claim is only
    # trustworthy for a caller that never had a slot (a Slack thread, a cron the
    # person owns); a ``dashboard:`` key names one, so a missing slot is a
    # failure to attribute rather than proof of the dashboard user. Refused HERE
    # rather than in the middleware: a popped slot does not say whose tab it
    # was, so refusing centrally would also refuse the person's own in-flight
    # calls on every internal route. This route refuses because of what it
    # publishes -- ``source="system"`` on the system.agent channel.
    if caller_names_a_missing_slot(
        getattr(state, "_slots", None), request.headers.get("X-Session-Key", "")
    ):
        _sel().log_api_access(
            caller=str(request.headers.get("X-Session-Key") or ""),
            operation=operation,
            outcome="denied",
            source="notifications_api",
            error="calling session's slot is gone; cannot attribute a system publish",
        )
        return web.json_response(
            {"error": "calling session not found", "code": "caller_session_missing"},
            status=403,
        )
    return None


def _refuse_redactable_key(key: object) -> web.Response | None:
    """The refusal for a key delivery redaction would rewrite, or None.

    A note's fields are redacted before it is stored, and the stored key is what a
    later publish or dismiss is matched against. A key that looks like a credential
    or a URL would be stored as its redaction, so two different keys could collapse
    into one and replace each other's notes, and the original could never be
    dismissed. Refusing it keeps a key's identity exactly what the caller sent.
    """
    if isinstance(key, str) and key and _redact_note_value(key) != key:
        return web.json_response(
            {
                "error": "key must not contain a credential or a URL",
                "code": "key_would_be_redacted",
            },
            status=400,
        )
    return None


async def api_notification_agent_push(request: web.Request) -> web.Response:
    """POST /api/notifications/agent — send_notification MCP tool (RFC Phase 5).

    Agent sessions publish schema-v2 notifications through the system.agent
    channel. Body: ``{"title": str, "body"?: str, "priority"?: str,
    "url"?: str, "group_key"?: str, "key"?: str, "actions"?: [{id,label,url?}]}``.
    A ``key`` replaces the agent note already published under it.
    ``source``/``channel``/``slot`` are server-fixed (never body-supplied), and the
    full payload validation applies — internal-path urls, action caps,
    length caps. Durability mirrors the app push: a 200 awaits the persist.
    """
    state: DashboardState = request.app["state"]
    refusal = _refuse_unattributable_caller(request, state, "notification_agent_push")
    if refusal is not None:
        return refusal
    # Bound the body BEFORE decoding, mirroring the app push endpoint: without
    # this the strict-internal route inherits the server-wide client_max_size,
    # and a large JSON object would be buffered and decoded on the event-loop
    # thread. Shared helper so the cap and the 413/400
    # contract cannot drift from the app push endpoint.
    body, _cap_err = await read_bounded_json(request)
    if _cap_err is not None:
        return _cap_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # Type-check optional fields BEFORE payload construction: the bus
    # validator assumes str/list shapes, so a non-string url or non-list
    # actions would raise AttributeError/TypeError past the
    # NotificationValidationError catch -- a 500 where the contract says 400.
    for field_name in ("title", "body", "priority", "url", "group_key", "key"):
        value = body.get(field_name)
        if value is not None and not isinstance(value, str):
            return web.json_response({"error": f"{field_name} must be a string"}, status=400)
    actions = body.get("actions")
    if actions is not None and not isinstance(actions, list):
        return web.json_response({"error": "actions must be a list"}, status=400)
    key_refusal = _refuse_redactable_key(body.get("key"))
    if key_refusal is not None:
        return key_refusal
    # Attribution comes from an attested, non-app session header, never the
    # body, so no caller can put another crewmate's face on its note or point
    # its note at another session.
    attribution_key = await asyncio.to_thread(attributable_session_key, request)
    member = await asyncio.to_thread(member_identity_for_session, attribution_key)
    session_meta = _publishing_session_meta(state, attribution_key)
    payload = NotificationPayload(
        source="system",
        channel="system.agent",
        kind="agent",
        title=body.get("title") or "",
        body=body.get("body") or "",
        priority=body.get("priority"),
        url=body.get("url"),
        group_key=body.get("group_key"),
        key=body.get("key"),
        actions=actions,
        member=member,
        meta=session_meta or {},
        publisher_session=attribution_key or None,
    )
    try:
        note = state.notification_bus.push(payload)
    except NotificationValidationError as exc:
        _sel().log_api_access(
            caller="agent",
            operation="notification_agent_push",
            outcome="denied",
            source="notifications_api",
            error=str(exc),
        )
        return web.json_response({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("agent notification delivery failed")
        _sel().log_api_access(
            caller="agent",
            operation="notification_agent_push",
            outcome="error",
            source="notifications_api",
            error="delivery failed",
        )
        return web.json_response({"error": "notification delivery failed"}, status=500)
    # Same durability guarantee as the app push endpoint: only acknowledge
    # once the persist job has succeeded.
    persist = state.last_notification_persist
    if persist is not None and not await persist:
        _sel().log_api_access(
            caller="agent",
            operation="notification_agent_push",
            outcome="error",
            source="notifications_api",
            error="persistence failed",
        )
        return web.json_response({"error": "failed to persist notification"}, status=500)
    _sel().log_api_access(
        caller="agent",
        operation="notification_agent_push",
        outcome="success",
        source="notifications_api",
    )
    return web.json_response({"ok": True, "note": note})


async def api_notification_agent_dismiss(request: web.Request) -> web.Response:
    """POST /api/notifications/agent/dismiss — dismiss_notification MCP tool.

    Body: ``{"key": str}``. Removes the system.agent note published under that key
    from the bell, every open view and history. Keys are shared by every agent
    session, as on publish, so any of the person's sessions can dismiss a note
    another one posted. ``dismissed`` says whether a note held the key.
    """
    state: DashboardState = request.app["state"]
    refusal = _refuse_unattributable_caller(request, state, "notification_agent_dismiss")
    if refusal is not None:
        return refusal
    body, _cap_err = await read_bounded_json(request)
    if _cap_err is not None:
        return _cap_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    key = body.get("key")
    # The same cap NotificationPayload.key has on publish, so a key no publish could
    # have stored is refused here rather than reported as matching nothing.
    if not isinstance(key, str) or not key.strip() or len(key) > MAX_SHORT_STRING:
        return web.json_response(
            {
                "error": f"key must be a non-empty string of at most {MAX_SHORT_STRING} chars",
                "code": "invalid_key",
            },
            status=400,
        )
    key_refusal = _refuse_redactable_key(key)
    if key_refusal is not None:
        return key_refusal
    dismissed, persisted = await state.dismiss_notification(source="system", key=key)
    if not persisted:
        _sel().log_api_access(
            caller="agent",
            operation="notification_agent_dismiss",
            outcome="error",
            source="notifications_api",
            error="persistence failed",
        )
        return web.json_response(
            {"error": "failed to persist the dismissal", "code": "persist_failed"}, status=500
        )
    _sel().log_api_access(
        caller="agent",
        operation="notification_agent_dismiss",
        outcome="success",
        source="notifications_api",
    )
    return web.json_response({"ok": True, "dismissed": dismissed})

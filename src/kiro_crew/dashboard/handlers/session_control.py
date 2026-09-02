"""Routes behind the session-control MCP tools.

Strict-internal (loopback + ``X-Internal-Secret``): no browser calls these, and
they are the entry point to acting on another live conversation, so a cookie
fall-through would be a genuinely new path rather than a convenience.

The handlers do parsing and status mapping only — resolution, authorization and
the operations themselves live in ``dashboard/session_control.py`` so the verbs
that take a target share one guard.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers._shared import _read_session_key
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


async def _require_internal(request: web.Request) -> web.Response | None:
    """Refuse anything that did not present ``X-Internal-Secret``.

    These paths are on ``_STRICT_INTERNAL_API_PATHS``, but strict is not
    self-enforcing at the handler: with the header ABSENT the middleware falls
    through to cookie auth, and a ``local_only=False`` deployment reclassifies
    every strict path as "mixed". Either way a same-origin page holding only a
    dashboard cookie could reach here and — by choosing ``X-Session-Key`` —
    message, stop, or read any of the user's sessions AS one of them. The
    session key is an identity claim these routes authorize on, so it has to be
    backed by the secret rather than by whatever a browser sends.

    ``internal_auth`` is set only after a constant-time secret match, so
    requiring it closes the cookie path, the app-token path, and the
    non-loopback reclassification in one check. Returns the refusal, or ``None``
    when the caller is authentic.
    """
    if request.get("internal_auth") is True:
        return None
    # Off the loop AND best-effort, the two properties `_audit_denied` exists to
    # carry for exactly this shape of site: a refusal logged BEFORE the audit
    # middleware has run. `log_api_access` only enqueues, but the FIRST `sel()` of
    # a process constructs the log -- trust-dir creation and key validation,
    # blocking file IO -- so a fresh gateway whose first session-control request
    # is unauthenticated would run that synchronously on
    # the event loop. And construction can raise (a trust root too short to sign
    # the chain), which unguarded would turn this 403 into a 500: losing the
    # denial in order to report it.
    try:
        await asyncio.to_thread(
            lambda: sel().log_api_access(
                caller="unknown",
                operation=f"session_control.{request.path.rsplit('/', 1)[-1]}",
                outcome="denied",
                source="dashboard",
                resources=request.path,
                error="internal secret required",
            )
        )
    except Exception:
        logger.warning("Failed to log a session-control denial to SEL", exc_info=True)
    return web.json_response({"error": "forbidden", "code": "internal_secret_required"}, status=403)


def _refusal(exc: sc.SessionControlError) -> web.Response:
    """Render a :class:`SessionControlError` as its HTTP response.

    Written as an explicit branch per status rather than
    ``status=exc.status`` so the route can only ever answer with a status from
    this closed set: an unmapped value degrades to 400 instead of forwarding
    whatever integer reached it. ``code`` is the field callers match on;
    ``message`` is advisory prose.
    """
    if exc.status == 403:
        return web.json_response({"error": exc.message, "code": exc.code}, status=403)
    if exc.status == 404:
        return web.json_response({"error": exc.message, "code": exc.code}, status=404)
    if exc.status == 409:
        return web.json_response({"error": exc.message, "code": exc.code}, status=409)
    if exc.status == 429:
        return web.json_response({"error": exc.message, "code": exc.code}, status=429)
    if exc.status == 500:
        # A genuine server-side failure — `close_target` raises this for the three
        # close-path failures (nudge retire / app hook / history save), each of
        # which left the tab open with every partial step rolled back. It is not a
        # client error, so it must not degrade to 400.
        return web.json_response({"error": exc.message, "code": exc.code}, status=500)
    return web.json_response({"error": exc.message, "code": exc.code}, status=400)


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise sc.SessionControlError("invalid JSON", code="invalid_json")
    if not isinstance(body, dict):
        raise sc.SessionControlError("body must be a JSON object", code="invalid_body")
    return body


def _target(body: dict) -> str:
    target = body.get("target")
    if not isinstance(target, str) or not target.strip():
        raise sc.SessionControlError("target is required", code="target_required")
    return target.strip()


async def api_session_control_create(request: web.Request) -> web.Response:
    """POST /api/session-control/create — open a session this caller will own."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        # Warmed AFTER the body read, which suspends: a config edit landing in that
        # window would change the fingerprint and leave `create_session`'s own
        # synchronous gate re-reading the file on the loop. Nothing suspends between
        # here and that gate, which is the first thing `create_session` does.
        await sc.prewarm_enabled_check()
        result = await sc.create_session(
            state,
            caller_session_key=_read_session_key(request),
            title=str(body.get("title") or ""),
            agent=str(body.get("agent") or ""),
            folder_id=str(body.get("folder_id") or ""),
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_stop(request: web.Request) -> web.Response:
    """POST /api/session-control/stop — stop another session's in-flight turn."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    # No prewarm here: `stop_target` warms the config after its own SEL prewarm,
    # which is the last suspension before the gate. Warming here as well would be
    # dead work -- the body read and that SEL await both sit in between.
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        result = await sc.stop_target(
            state,
            caller_session_key=_read_session_key(request),
            target=_target(body),
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_close(request: web.Request) -> web.Response:
    """POST /api/session-control/close — archive another session (tab ✕)."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    # No prewarm here: `close_target` warms the SEL logger and the config after
    # its own SEL prewarm, the same ordering `stop_target`/`send_to_target` use
    # and for the same reason — the body read above is a suspension point.
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        result = await sc.close_target(
            state,
            caller_session_key=_read_session_key(request),
            target=_target(body),
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_send(request: web.Request) -> web.Response:
    """POST /api/session-control/send — deliver a message to another session."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    # No prewarm here: `send_to_target` warms the config after its own SEL
    # prewarm, the same ordering `stop_target` uses and for the same reason.
    state: DashboardState = request.app["state"]
    try:
        body = await _body(request)
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            raise sc.SessionControlError("message is required", code="message_required")
        result = await sc.send_to_target(
            state,
            caller_session_key=_read_session_key(request),
            target=_target(body),
            message=message,
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)


async def api_session_control_read(request: web.Request) -> web.Response:
    """GET /api/session-control/read — read another session's transcript tail."""
    refused = await _require_internal(request)
    if refused is not None:
        return refused
    # Stays at the top for read alone: everything between here and the gate is
    # synchronous query parsing (`request.query` is already available and
    # `read_messages` does not await), so there is no suspension to invalidate it.
    await sc.prewarm_enabled_check()
    state: DashboardState = request.app["state"]
    try:
        target = (request.query.get("target") or "").strip()
        if not target:
            raise sc.SessionControlError("target is required", code="target_required")
        limit_raw = request.query.get("limit")
        since_raw = request.query.get("since")
        try:
            limit = int(limit_raw) if limit_raw else sc.DEFAULT_READ_MESSAGES
            since = int(since_raw) if since_raw else None
        except ValueError:
            raise sc.SessionControlError(
                "limit and since must be integers", code="invalid_pagination"
            )
        result = sc.read_messages(
            state,
            caller_session_key=_read_session_key(request),
            target=target,
            limit=limit,
            since=since,
        )
    except sc.SessionControlError as exc:
        return _refusal(exc)
    return web.json_response(result)

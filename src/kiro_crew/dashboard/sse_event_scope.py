"""App-token scope for the dashboard SSE streams (``GET /api/stream``, ``GET /api/logs``).

``DashboardState._broadcast`` puts the same raw note on every SSE queue, so the
stream has to make the per-app decision the WebSocket chokepoint makes in
``WebSocketHub._ws_client_allowed`` / ``_serialize_for_client``. It makes it with
the same helpers from :mod:`kiro_crew.dashboard.ws_event_scope`; this module
evaluates no scope of its own. The log stream takes the ``log`` / ``log:all``
decision the WebSocket's ``subscribe_logs`` takes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.dashboard.token_auth_paths import _deny
from kiro_crew.dashboard.ws_event_scope import (
    _TIER0_ALWAYS,
    DASHBOARD_USER_AUDITEE,
    _audit_allow,
    _audit_deny,
    declared_events_unreadable,
    effective_allowed_events,
    filter_slots_for_app,
    load_declared_events_checked,
    strip_owner_only_status,
    ws_event_allowed,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.handlers.updates")

_REFUSED = "app token not permitted for this endpoint"


def _audit_deny_quietly(app: str, event_type: str, reason: str) -> None:
    try:
        _audit_deny(app or "<unknown>", event_type, reason)
    except Exception:
        logger.debug("sse: audit for %s deny failed", event_type, exc_info=True)


def _audit_allow_quietly(app: str, event_type: str) -> None:
    try:
        _audit_allow(app or "<unknown>", event_type)
    except Exception:
        logger.debug("sse: audit for %s grant failed", event_type, exc_info=True)


def stream_app(request: web.Request) -> str:
    """The app claim a stream reader is scoped to, or ``""`` for a dashboard user."""
    if request.get("is_dashboard_user", False):
        return ""
    return str(request.get("app", "") or "")


def unidentified_reader_refusal(request: web.Request, event_type: str) -> web.Response | None:
    """403 for a reader the auth middleware marked neither a dashboard user nor an app.

    Both marks are POSITIVE: a request that reached the handler without either
    (no middleware ran, or a branch that sets neither) is refused before the
    stream starts, rather than streamed as a dashboard user or as an unnamed app.
    """
    if request.get("is_dashboard_user", False) or stream_app(request):
        return None
    _audit_deny_quietly("", event_type, "stream_reader_unidentified")
    return _deny(request, "stream reader not identified")


async def _connect_declarations(app: str, event_type: str) -> tuple[bool, frozenset[str] | None]:
    """*app*'s enablement and declared scopes at connect; ``None`` when unreadable.

    Offloaded and cache-priming, as ``api_ws`` reads them. A read that fails, in
    the loader or around it, is audited and reported as ``None``.
    """
    try:
        enabled, events = await asyncio.to_thread(load_declared_events_checked, app)
    except Exception:
        logger.debug("sse: could not resolve scope for app %r", app, exc_info=True)
        _audit_deny_quietly(app, event_type, "scope_check_exception")
        return True, None
    if events is None:
        _audit_deny_quietly(app, event_type, "declarations_unreadable")
    return enabled, events


async def stream_reader_admission(
    request: web.Request, app: str
) -> tuple[web.Response | None, frozenset[str] | None]:
    """403 for a disabled app on ``GET /api/stream``, else its connect-time scopes.

    ``None`` scopes (unreadable declarations) admit the reader to Tier 0 only.
    """
    enabled, events = await _connect_declarations(app, "stream")
    if not enabled:
        _audit_deny_quietly(app, "stream", "app_disabled")
        return _deny(request, _REFUSED), None
    return None, events


def _unreadable_now(app: str) -> bool:
    """Whether *app*'s live declarations are unreadable; a check that fails says so."""
    try:
        return declared_events_unreadable(app)
    except Exception:
        logger.debug("sse: live declarations unknown for app %r", app, exc_info=True)
        return True


def log_scope_granted(app: str, connect_events: frozenset[str]) -> bool:
    """Whether *app* may read the log stream now, as ``subscribe_logs`` decides it.

    Declarations a later read found unreadable refuse, as at connect. Every
    answer is audited through the deduplicated SEL helpers.
    """
    if _unreadable_now(app):
        _audit_deny_quietly(app, "log", "declarations_unreadable")
        return False
    try:
        live = effective_allowed_events(app, connect_events)
    except Exception:
        logger.debug("sse: live log scope unreadable for app %r", app, exc_info=True)
        _audit_deny_quietly(app, "log", "scope_check_exception")
        return False
    if "log" in live or "log:all" in live:
        _audit_allow_quietly(app, "log")
        return True
    _audit_deny_quietly(app, "log", "log_scope_not_granted")
    return False


async def log_reader_refusal(
    request: web.Request, app: str
) -> tuple[web.Response | None, frozenset[str]]:
    """403 unless *app* declared the ``log`` scope, and the connect-time scopes.

    Fails closed: declarations that cannot be read, or a disabled app, refuse.
    """
    enabled, connect_events = await _connect_declarations(app, "log")
    if connect_events is None:
        return _deny(request, _REFUSED), frozenset()
    if not enabled:
        _audit_deny_quietly(app, "log", "app_disabled")
        return _deny(request, _REFUSED), frozenset()
    if not log_scope_granted(app, connect_events):
        return _deny(request, _REFUSED), frozenset()
    return None, connect_events


def status_frame_for_reader(data: dict[str, Any], app: str) -> dict[str, Any]:
    """The ``dashboard`` status frame for a stream reader, its grant audited.

    An app reader (*app* set) gets it without the owner-only fields, through the
    helper the WebSocket's ``_push_status`` uses.
    """
    if app:
        strip_owner_only_status(data)
    _audit_allow_quietly(app or DASHBOARD_USER_AUDITEE, "dashboard")
    return data


def scope_note_for_app(
    note: dict[str, Any],
    *,
    app: str,
    connect_events: frozenset[str] | None,
    state: DashboardState,
) -> dict[str, Any] | None:
    """The note an app-token SSE reader may receive, or None to send nothing.

    *connect_events* is the app's declared set read at connect, intersected per
    note with the live declarations as the WebSocket does. ``None`` means the
    declarations could not be read at connect, and a later read that finds them
    unreadable (:func:`declared_events_unreadable`) does the same: only Tier 0
    frames go out. A ``slots`` note comes back with its ``slots`` JSON narrowed
    to the rows the app may see, and as None when no row is left.
    """
    msg_type = str(note.get("_type", "") or "notification")
    if connect_events is None or _unreadable_now(app):
        if msg_type in _TIER0_ALWAYS:
            _audit_allow_quietly(app, msg_type)
            return note
        _audit_deny_quietly(app, msg_type, "declarations_unreadable")
        return None
    try:
        allowed = effective_allowed_events(app, connect_events)
        if not ws_event_allowed(msg_type, note, app=app, allowed_events=allowed, state=state):
            return None
        if msg_type != "slots":
            return note
        rows = note.get("_slots_list")
        if rows is None:
            rows = json.loads(note["slots"])
        visible = filter_slots_for_app(rows, app, allowed, state)
    except Exception:
        logger.debug("sse: scope check failed for app %r on %s", app, msg_type, exc_info=True)
        _audit_deny_quietly(app, msg_type, "scope_check_exception")
        return None
    if not visible:
        return None
    return {**note, "slots": json.dumps(visible)}

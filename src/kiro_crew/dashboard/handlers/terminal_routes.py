"""The terminal's session-management REST routes: create a session, list the sessions,
and delete one.

The WebSocket, completion and redaction routes and the title poller stay in the facade. An
owner of :mod:`kiro_crew.dashboard.handlers.terminal`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.terminal_lifecycle import _kill_session, _sess_alive, _sess_pid
from kiro_crew.dashboard.handlers.terminal_session import (
    _MAX_SESSION_ID_LEN,
    _MAX_SESSIONS,
    _get_config,
    _get_registry,
    _is_enabled,
    _sel,
)
from kiro_crew.dashboard.handlers.terminal_shell import _resolve_shell
from kiro_crew.executors import discovery_executor

# The facade's logger, so a log filter or capture keyed on the terminal handler's
# name sees this module's records too.
logger = logging.getLogger("kiro_crew.dashboard.handlers.terminal")


async def api_terminal_create(request: web.Request) -> web.Response:
    """POST /api/terminal/sessions — create a new terminal session (returns session_id)."""
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown",
            operation="terminal.session.create",
            outcome="denied",
            source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(request, "terminal.session.create")
    if owner_denied is not None:
        return owner_denied
    if not _is_enabled(request):
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.create",
            outcome="denied",
            source="dashboard",
            resources="feature_disabled",
        )
        return web.Response(status=403, text="Terminal panel disabled")

    registry = _get_registry(request)
    cfg = _get_config(request)
    max_sessions = cfg.get("max_sessions", _MAX_SESSIONS)

    if len(registry) >= max_sessions:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.create",
            outcome="denied",
            source="dashboard",
            resources=f"max_sessions={max_sessions}",
        )
        return web.json_response(
            {"error": f"Max {max_sessions} sessions", "code": "terminal_max_sessions"},
            status=429,
        )

    session_id = uuid.uuid4().hex[:12]
    # Off-loop for the same reason as the spawn sites: the PATH scan must not
    # stall the event loop at request rate.
    shell, rejected_shell = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(),
        _resolve_shell,
        cfg,
    )
    _sel().log_api_access(
        caller=caller,
        operation="terminal.session.create",
        outcome="ok",
        source="dashboard",
        resources=f"session={session_id}",
    )
    body: dict = {
        "session_id": session_id,
        "shell": shell,
    }
    # Surface a rejected configured shell at the API level rather than
    # silently substituting: an API caller can tell a typo'd setting from a
    # deliberate default. The dashboard panel spawns via the WS handler and
    # does not read these fields — its typo surface is the save-time Settings
    # validation (config PATCH), with the spawn-site log as the backstop.
    if rejected_shell:
        body["shell_fallback"] = True
        body["configured_shell"] = rejected_shell
    return web.json_response(body)


async def api_terminal_delete(request: web.Request) -> web.Response:
    """DELETE /api/terminal/sessions/{session_id} — kill a terminal session."""
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown",
            operation="terminal.session.delete",
            outcome="denied",
            source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(request, "terminal.session.delete")
    if owner_denied is not None:
        return owner_denied
    if not _is_enabled(request):
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.delete",
            outcome="denied",
            source="dashboard",
            resources="feature_disabled",
        )
        return web.Response(status=403, text="Terminal panel disabled")

    session_id = request.match_info.get("session_id", "")
    # Same bound ``api_terminal_ws`` applies when a session is created, so an id
    # outside it provably keys nothing in this registry. Rejecting it here means
    # a malformed request is told so, rather than being answered "no such
    # session" after the lookup. Plaintext 400 to match this handler's other
    # responses (401/403/404 above and below are all plaintext).
    if not session_id or len(session_id) > _MAX_SESSION_ID_LEN:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.delete",
            outcome="denied",
            source="dashboard",
            resources=f"invalid_session_id={session_id!r}",
        )
        return web.Response(status=400, text="Invalid session_id")

    registry = _get_registry(request)
    if session_id not in registry:
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.delete",
            outcome="denied",
            source="dashboard",
            resources="unknown_session",
        )
        return web.Response(status=404, text="Session not found")
    sess = registry[session_id]
    if sess is None:
        # An opening handler can still publish a shell; 404 would claim it stopped.
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.delete",
            outcome="denied",
            source="dashboard",
            resources=f"session={session_id},reservation_in_flight=1",
        )
        return web.Response(status=409, text="Terminal session is still opening")
    del registry[session_id]

    if sess.ws and not sess.ws.closed:
        await sess.ws.close()
    await _kill_session(sess)

    _sel().log_api_access(
        caller=caller,
        operation="terminal.session.delete",
        outcome="ok",
        source="dashboard",
        resources=f"session={session_id}",
    )
    return web.json_response({"deleted": session_id})


async def api_terminal_list(request: web.Request) -> web.Response:
    """GET /api/terminal/sessions — list active terminal sessions."""
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown",
            operation="terminal.session.list",
            outcome="denied",
            source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(request, "terminal.session.list")
    if owner_denied is not None:
        return owner_denied
    if not _is_enabled(request):
        _sel().log_api_access(
            caller=caller,
            operation="terminal.session.list",
            outcome="denied",
            source="dashboard",
            resources="feature_disabled",
        )
        return web.json_response({"enabled": False, "sessions": []})

    registry = _get_registry(request)
    sessions = []
    for sid, sess in registry.items():
        if sess is None:
            continue  # placeholder during ws.prepare()
        sessions.append(
            {
                "session_id": sid,
                "pid": _sess_pid(sess),
                "alive": _sess_alive(sess),
                "cols": sess.cols,
                "rows": sess.rows,
                "connected": sess.ws is not None and not sess.ws.closed,
            }
        )
    _sel().log_api_access(
        caller=caller,
        operation="terminal.session.list",
        outcome="ok",
        source="dashboard",
        resources=f"count={len(sessions)}",
    )
    return web.json_response({"enabled": True, "sessions": sessions})

"""``POST /api/chat/slots/{slot}/terminal-run`` -- what became of a Run in terminal.

The dashboard host that answered a Run in terminal click (``useRunInTerminalBridge``)
reports the answer it gave: the command ran in a new terminal tab, was copied
instead, or did not run, and why. When *slot* is a crewmate's own pinned thread
the crewmate is told in one ``[User action]`` line
(:func:`kiro_crew.dashboard.mate_user_actions.note_terminal_run`); any other slot
is answered ``{"noted": false}`` and nobody is told.

Owner-only, app tokens refused, the same gate as the slot's other side-panel
routes. It writes nothing: the line is held in memory until the thread is idle.
The command is the crewmate's own text the owner confirmed; it is scrubbed, cut
and fenced as data in the note.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from kiro_crew.dashboard import mate_user_actions
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.members import _deny_app_caller

#: The longest command body accepted; the note keeps less (``MAX_COMMAND_CHARS``).
MAX_BODY_COMMAND_CHARS = 20_000


def _bad(code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=400)


def _choice(body: dict[str, Any], key: str, allowed: frozenset[str], default: str) -> str | None:
    value = body.get(key, default)
    if value in ("", None):
        value = default
    return value if isinstance(value, str) and value in allowed else None


async def api_slot_terminal_run(request: web.Request) -> web.Response:
    """POST ``{command, outcome, reason?, source?}`` -- tell the owning crewmate thread."""
    denied = await _deny_app_caller(request, "chat.terminal_run")
    if denied is not None:
        return denied
    denied = await require_owner_dashboard_request(request, "chat.terminal_run")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return _bad("invalid_json", "the body must be a JSON object")
    if not isinstance(body, dict):
        return _bad("invalid_json", "the body must be a JSON object")
    command = body.get("command")
    if not isinstance(command, str) or not command.strip():
        return _bad("command_empty", "command is empty")
    if len(command) > MAX_BODY_COMMAND_CHARS:
        return _bad("command_too_long", "command is too long")
    outcome = _choice(body, "outcome", mate_user_actions.TERMINAL_OUTCOMES, "")
    if outcome is None:
        return _bad("invalid_outcome", "outcome must be ran, copied or failed")
    reason = _choice(body, "reason", mate_user_actions.TERMINAL_REASONS | {""}, "")
    source = _choice(body, "source", mate_user_actions.TERMINAL_SOURCES, "code_block")
    if reason is None or source is None:
        return _bad("invalid_field", "reason or source is not one this route knows")
    noted = mate_user_actions.note_terminal_run(
        request.app["state"],
        request.match_info.get("slot", ""),
        outcome=outcome,
        source=source,
        reason=reason,
        command=command,
    )
    return web.json_response({"ok": True, "noted": noted})

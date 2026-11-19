"""The ``waiting_on_you`` and ``waiting_on_you_clear`` tools: pin and clear an ask.

An agent blocked on the person pins what it needs above the composer once, and the
card stays across turns, loop cycles and restarts until the person answers or writes,
the agent clears it, or seven days pass. The gateway owns the asks
(``dashboard/waiting_on_you.py``); these handlers only validate and forward to the
strict ``/api/agent-waiting/*`` routes under the caller's own attested session key.

Served through :mod:`kiro_crew.mcp_tools.control`, which callers import and tests
patch; this module never imports it.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.session_surface import has_dashboard_surface
from kiro_crew.validation import validate_tool_args
from kiro_crew.waiting_on_you_contract import (
    CODE_TOO_MANY_ASKS,
    WAITING_ON_YOU_CLEAR_SCHEMA,
    WAITING_ON_YOU_SCHEMA,
    AskError,
    validate_declaration,
)

logger = logging.getLogger("kiro_crew.mcp_tools.control")

#: Where a session with no dashboard tab is pointed instead.
WAITING_NOT_DASHBOARD = (
    "waiting_on_you only works from a dashboard chat session with its tab open. "
    "Say what you need in plain text instead, or end your turn with an "
    "[OPTIONS: a | b | c] tag."
)
_STAYS = "It stays until the person answers or writes, you clear it, or 7 days pass."


def _caller(tool: str) -> str | None:
    """The caller's strictly resolved dashboard session key, else ``None``."""
    sk, _ = mcp_core.require_strict_session_key(tool)
    if not sk or not sk.startswith("dashboard:") or not has_dashboard_surface(sk):
        return None
    return sk


def _pinned_line(keys: Any) -> str:
    names = [str(k) for k in keys or [] if isinstance(k, str)]
    return f"Pinned asks: {', '.join(names)}." if names else "No asks are pinned."


def _refused(reply: Any) -> str:
    """The tool text for a gateway refusal of a set or clear."""
    if not isinstance(reply, dict):
        return "Error: the dashboard did not answer; try again."
    code = str(reply.get("code") or "")
    error = str(reply.get("error") or "")
    if code == "crew_worker_asks_conductor":
        return error or "Ask your conductor with work_report."
    if code == CODE_TOO_MANY_ASKS:
        return f"Error: {error}"
    if code == "slot_not_found":
        return WAITING_NOT_DASHBOARD
    if reply.get("transport_error"):
        return "Error: the dashboard could not be reached; nothing was pinned. Try again."
    return f"Error: {error or code or 'the dashboard refused the ask'}"


def waiting_on_you(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WAITING_ON_YOU_SCHEMA)
    try:
        ask = validate_declaration(args)
    except AskError as exc:
        return f"Error: {exc.message}"
    sk = _caller("waiting_on_you")
    if sk is None:
        return WAITING_NOT_DASHBOARD
    if mcp_core.directive_capture_active():
        # A gateway-side replay runs this on the gateway's own loop, where a
        # loopback POST back to it can never be served. Nothing is derived here.
        return ""
    reply = mcp_core._post("/api/agent-waiting/set", ask, session_key=sk)
    if not (isinstance(reply, dict) and reply.get("ok") is True):
        return _refused(reply)
    keys = reply.get("keys")
    if not reply.get("changed"):
        return (
            f'"{ask["key"]}" is already pinned exactly as written; nothing changed. '
            f"{_pinned_line(keys)}"
        )
    return f'Pinned "{ask["key"]}". {_pinned_line(keys)} {_STAYS}'


def waiting_on_you_clear(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WAITING_ON_YOU_CLEAR_SCHEMA)
    every = args.get("all") is True
    key = args.get("key")
    if every == bool(key):
        return "Error: pass the key of a pinned ask, or all: true."
    sk = _caller("waiting_on_you_clear")
    if sk is None:
        return WAITING_NOT_DASHBOARD
    if mcp_core.directive_capture_active():
        return ""
    body: dict[str, Any] = {"all": True} if every else {"key": key}
    reply = mcp_core._post("/api/agent-waiting/clear", body, session_key=sk)
    if not (isinstance(reply, dict) and reply.get("ok") is True):
        return _refused(reply)
    cleared = [str(k) for k in reply.get("cleared") or []]
    pinned = _pinned_line(reply.get("keys"))
    if not cleared:
        what = "No ask was pinned" if every else f'No ask named "{key}" is pinned'
        return f"{what}; nothing changed. {pinned}"
    names = ", ".join(f'"{k}"' for k in cleared)
    return f"Cleared {names}. {pinned}"

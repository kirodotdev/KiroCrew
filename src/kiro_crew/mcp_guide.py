"""MCP server ``kirocrew-guide`` — an agent guides the human through the dashboard.

The agent names one or more REGISTERED actions (show a setting, create a
crewmate, open the existing add-MCP-server page) and the dashboard walks the
person through them with a non-modal pointer. The agent never sends a route,
selector, markup or code, and it never performs a mutation: the human's own click
on the existing owner-only save does, and only the gateway's record of that save
completes a mutation step.

Why this is its own server
--------------------------
Assignment is per server, so the server IS the unit of authorization, and
``kirocrew-core`` is exempt from Tool Search deferral -- a tool there costs its
schema in every request of every session. Guiding is a capability granted on
purpose, so it is ``opt_in`` in ``agent._MANAGED_MCP_SERVERS``: a default agent's
spec carries neither the entry nor an ``@kirocrew-guide`` reference.

Why no tool takes a session, slot or tab
----------------------------------------
The guide a call starts, reads or cancels is the CALLING session's own, resolved
strictly (``require_strict_session_key``) and sent as the verified key, so the
value that was checked is the value that is used. The gateway derives the slot
from that key against its live slot table. A subagent has no tab of its own and
is refused rather than walked up to its parent's.

Stateless: every call is one round trip to the gateway, which owns all guide
state. Nothing here holds per-caller data between calls.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

from kiro_crew.guide_catalog import GUIDE_ID_MAX_LEN
from kiro_crew.guide_catalog import MAX_ACTIONS as GUIDE_MAX_ACTIONS
from kiro_crew.mcp_core import (
    _get,
    _post,
    _resolve_session_key,
    require_strict_session_key,
)
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import MCP_GUIDE_SCHEMAS, validate_tool_args

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-guide"
SERVER_VERSION = "1.0.0"

_ACTION_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "enum": ["settings.show", "crewmate.create", "mcp.open_add"],
            "description": "A registered action id from guide_list_actions.",
        },
        "params": {
            "type": "object",
            "description": "The action's parameters, per its params_schema.",
        },
    },
    "required": ["id"],
    "additionalProperties": False,
}


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "guide_list_actions",
            "description": (
                "List the registered dashboard actions you can guide the user "
                "through, each with its parameter schema and whether it changes "
                "anything. Call it before guide_start when unsure."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "guide_start",
            "description": (
                f"Offer the user a step-by-step pointer through 1-{GUIDE_MAX_ACTIONS} registered "
                "actions in THEIR dashboard tab for this conversation. Nothing "
                "changes until the user clicks the real Save/Create button, and "
                "you are told only the saved identity, never setting values or "
                "credentials. Returns the guide; delivered_clients=0 means it is "
                "queued until the tab loads, not shown. One guide at a time: "
                "cancel the current one before starting another. END YOUR TURN "
                "after starting; check progress later with guide_status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "actions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": GUIDE_MAX_ACTIONS,
                        "items": _ACTION_ITEM_SCHEMA,
                    }
                },
                "required": ["actions"],
            },
        },
        {
            "name": "guide_status",
            "description": (
                "Read this conversation's guide: status (offered, active, "
                "target_missing, completed, cancelled, expired), the current action "
                "and step, and each finished action's result. Omit guide_id for "
                "the latest one."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"guide_id": {"type": "string", "maxLength": GUIDE_ID_MAX_LEN}},
            },
        },
        {
            "name": "guide_cancel",
            "description": (
                "Stop this conversation's guide. A change the user already saved " "is not undone."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"guide_id": {"type": "string", "maxLength": GUIDE_ID_MAX_LEN}},
                "required": ["guide_id"],
            },
        },
    ]


def _list_tools() -> list[dict[str, Any]]:
    """Unconditional: reaching this process means a spec granted the set."""
    return _tool_definitions()


def _strict_session_key() -> tuple[str, str]:
    return require_strict_session_key(
        "Error: this session's identity could not be verified strictly, so there "
        "is no dashboard tab to guide from here. Subagents inherit no session "
        "identity of their own — start the guide from the parent session instead.",
        SERVER_NAME,
    )


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    schema = MCP_GUIDE_SCHEMAS.get(name)
    if schema is None:
        return args
    return validate_tool_args(args, schema)


def _render(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _error(d: dict[str, Any]) -> str | None:
    err = d.get("error")
    if not err:
        return None
    return redact(f"Error: {err}")


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    if name not in {"guide_list_actions", "guide_start", "guide_status", "guide_cancel"}:
        return f"Error: unknown tool '{name}'"
    sk, err = _strict_session_key()
    if err:
        return err

    if name == "guide_list_actions":
        d = _get("/api/guide/agent/actions", session_key=sk)
        return _error(d) or _render({"actions": d.get("actions") or []})

    if name == "guide_start":
        d = _post("/api/guide/agent/start", {"actions": args.get("actions")}, session_key=sk)
        return _error(d) or _render(d)

    if name == "guide_status":
        path = "/api/guide/agent/status"
        guide_id = args.get("guide_id")
        if guide_id:
            path += "?" + urllib.parse.urlencode({"guide_id": guide_id})
        d = _get(path, session_key=sk)
        return _error(d) or _render(d)

    d = _post("/api/guide/agent/cancel", {"guide_id": args.get("guide_id")}, session_key=sk)
    return _error(d) or _render(d)


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    """Guarded entry point — schema validation and SEL audit live in the wrapper."""
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        session_key=_resolve_session_key() or SERVER_NAME,
        downstream_service=SERVER_NAME,
    )


#: Consumes the per-call caller block the gateway injects rather than reading
#: identity from its own process, and refuses a caller the gateway cannot name.
#: Kept in step with ``mcp_discovery._MANAGED_SERVERS_CALLER_AWARE``.
ADVERTISE_CALLER_IDENTITY = True


def run_mcp_server() -> None:
    """Run the MCP stdio server — reads JSON-RPC from stdin, writes to stdout."""
    run_mcp_stdio_loop(
        SERVER_NAME,
        SERVER_VERSION,
        _list_tools,
        _call_tool,
        advertise_caller_identity=ADVERTISE_CALLER_IDENTITY,
    )


if __name__ == "__main__":  # pragma: no cover - process entry
    logging.basicConfig(level=logging.INFO)
    run_mcp_server()

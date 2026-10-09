"""MCP server ``kirocrew-guide`` — a crewmate's tools for the Kiro Crew dashboard.

Today it carries one tool, ``rename_self``: a crewmate takes the name its user
just gave it, and the dashboard shows the new name at once
(``POST /api/guide/agent/rename``). The tool never names a member: the gateway
renames the crewmate whose own chat the verified caller is.

Why this is its own server
--------------------------
The server is the unit of assignment, authorization and governance: an
operator or a policy can withhold the whole set from an agent at once, and the
set's grants (``agent._GUIDE_AUTO_GRANTS``) are reviewed together. It is
mounted on every crewmate and dashboard session (not ``opt_in`` in
``agent._MANAGED_MCP_SERVERS``). Its tools only work for a conversation open in
a dashboard tab, on a turn the user sent, so off the dashboard (Slack, Discord,
the CLI, a schedule, a subagent) each of them refuses with a one-line reason
(:func:`_off_dashboard`).

The behaviour rules a tool depends on live in its description and in each
result's ``next`` hint, not in any one agent's prompt, so they reach every agent
and survive context compaction.

Why no tool takes a session, slot or tab
----------------------------------------
The caller is the CALLING session, resolved strictly
(``require_strict_session_key``) and sent as the verified key, so the value that
was checked is the value that is used. The gateway derives the slot from that
key against its live slot table. A subagent has no tab of its own and is
refused rather than walked up to its parent's.

Stateless: every call is one round trip to the gateway, which owns all state.
Nothing here holds per-caller data between calls.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_crew.mcp_core import _post, _resolve_session_key, require_strict_session_key
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import MAX_SHORT_STRING, MCP_GUIDE_SCHEMAS, validate_tool_args

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-guide"
SERVER_VERSION = "1.0.0"


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "rename_self",
            "description": (
                "Change the name the user sees for you, in this chat, the roster "
                "and the sidebar. Call it only when the user has just told you what "
                "they want to call you; never on your own initiative and never to "
                "rename anyone else. Pass the name exactly as they gave it. Only a "
                "crewmate in its own chat can call it, from a message the user sent "
                "in the dashboard."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": MAX_SHORT_STRING}
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    ]


_TOOL_NAMES = frozenset(t["name"] for t in _tool_definitions())


def _list_tools() -> list[dict[str, Any]]:
    """Unconditional: reaching this process means a spec granted the set."""
    return _tool_definitions()


def _strict_session_key() -> tuple[str, str]:
    return require_strict_session_key(
        "this session's identity could not be verified strictly, so it has no "
        "dashboard tab (a subagent has none of its own; ask from the parent session)",
        SERVER_NAME,
    )


#: Gateway refusal codes that mean "this turn was not sent from the dashboard"
#: (``dashboard.handlers.guide._resolve_agent_caller``): not open in a tab, a
#: message from a messaging channel, a subagent, a scheduled run, an app, or no
#: session key at all.
_OFF_DASHBOARD_CODES = frozenset(
    {
        "no_live_slot",
        "no_dashboard_turn",
        "channel_caller",
        "subagent_caller",
        "unattended_caller",
        "app_caller",
        "app_scoped_caller",
        "missing_session_key",
        "not_user_turn",
        "unattested_caller",
    }
)


def _off_dashboard(name: str, reason: str) -> str:
    """The one-line refusal of a tool called without a dashboard turn."""
    reason = reason.removeprefix("Error: ").strip().rstrip(".")
    return redact(f"Error: {name} needs the dashboard: {reason}. Nothing changed. Answer in words.")


def _error_for(name: str, d: dict[str, Any]) -> str | None:
    """The gateway's refusal, with an off-dashboard refusal said as one."""
    err = d.get("error")
    if not err:
        return None
    if d.get("code") in _OFF_DASHBOARD_CODES:
        return _off_dashboard(name, str(err))
    return redact(f"Error: {err}")


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    schema = MCP_GUIDE_SCHEMAS.get(name)
    if schema is None:
        return args
    return validate_tool_args(args, schema)


def _render(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    if name not in _TOOL_NAMES:
        return f"Error: unknown tool '{name}'"
    sk, err = _strict_session_key()
    if err:
        return _off_dashboard(name, err)

    # rename_self
    d = _post("/api/guide/agent/rename", {"name": args.get("name")}, session_key=sk)
    refused = _error_for(name, d)
    if refused:
        return f"{refused}\nYour name did not change; do not say it did."
    return _render(
        {
            **d,
            "next": "Your name now shows as "
            + str(d.get("display_name") or "")
            + " everywhere in the dashboard. Answer to it from now on.",
        }
    )


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

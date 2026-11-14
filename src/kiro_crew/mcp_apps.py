"""MCP server ``kirocrew-apps`` -- the tools third-party apps declare, each call approved.

A first-party app's manifest ``agentTools`` are listed under ``kirocrew-core``
(``mcp_tools/app_tools.py``), which the default agent auto-approves as a whole
server. A third-party app's are listed here instead (``app_agent_tools.
third_party_tools``), and no agent's ``allowedTools`` names this server, so kiro-cli
asks before every call and the PreToolUse gate sees each one. That is the "listing
path that asks before each call" section 30.1 of ``app-kit-platform.md`` names.

Nothing here runs app code. A call is checked against the declared schema through
the field engine every core tool uses, posted to the gateway's strict internal
``POST /api/app-tools/prompted-call`` (which resolves third-party declarations only
and checks the call again), and answered with the app's reply redacted and cut at
the declared cap, exactly as ``kirocrew-core`` answers a first-party tool
(:func:`kiro_crew.mcp_tools.app_tools.shape_answer`).

Stateless: every call is one round trip to the gateway. Nothing here holds
per-caller data between calls.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.apps import app_agent_tools
from kiro_crew.apps.manager import app_enabled_state
from kiro_crew.mcp_core import _post, _resolve_session_key
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.validation_parts.field_engine import ValidationError, validate_tool_args

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-apps"
SERVER_VERSION = "1.0.0"

#: The gateway route every call reaches: strict internal, and it resolves only the
#: tools a third-party app declares (``dashboard/handlers/app_agent_tools.py``).
CALL_PATH = "/api/app-tools/prompted-call"


def _descriptor(tool: app_agent_tools.DeclaredTool) -> dict[str, Any]:
    """The declared descriptor, its description naming the app that serves the tool.

    The approval prompt shows the tool's name; the description is what tells the
    model, and anyone reading the call, whose code answers it.
    """
    descriptor = tool.decl.descriptor()
    served_by = f"Served by the installed app {tool.app!r}, not by Kiro Crew."
    return {**descriptor, "description": f"{served_by}\n\n{descriptor['description']}"}


def _list_tools() -> list[dict[str, Any]]:
    """The installed third-party apps' tools, minus those of an app readably switched off.

    Hidden only on a readable ``False``, as ``kirocrew-core`` hides a first-party
    app's: the gateway refuses a call to a disabled app either way, while hiding on a
    transient fault would last the session's whole life.
    """
    tools = app_agent_tools.third_party_tools()
    hidden: set[str] = set()
    for app in sorted({t.app for t in tools}):
        try:
            state = app_enabled_state(app)
        except Exception:  # noqa: BLE001 - an unreadable state lists the tools
            continue
        if state is False:
            hidden.add(app)
    return [_descriptor(t) for t in tools if t.app not in hidden]


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    tool = app_agent_tools.third_party_tool(name)
    if tool is None:
        raise ValidationError("name", f"no installed third-party app declares {name!r}")
    return validate_tool_args(args, app_agent_tools.build_tool_schema(tool))


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    """Post the validated call; the gateway resolves the app and checks it again.

    The post carries the lenient session key every core post carries: the gateway
    derives the calling app from it (``request["app"]``) and refuses a session that
    one app owns calling another app's tool. That key may name a subagent's parent,
    so it cannot vouch for the calling session, which is why a third-party block
    that declares ``strictSession`` is never listed
    (``app_agent_tools.third_party_tools``).
    """
    # Imported here: it loads the ``kirocrew-core`` domain registry, which a
    # ``tools/list`` of this server never needs.
    from kiro_crew.mcp_tools.app_tools import shape_answer

    tool = app_agent_tools.third_party_tool(name)
    if tool is None:
        return f"Error: no installed third-party app declares the tool {name!r}."
    resp = _post(CALL_PATH, {"tool": name, "arguments": args}, timeout=tool.decl.timeout_seconds)
    # The credential passthrough of a text answer is a first-party app's only.
    return shape_answer(name, tool, resp, credential_passthrough=False)


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    """Guarded entry point: schema validation and the SEL audit live in the wrapper."""
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        session_key=_resolve_session_key() or SERVER_NAME,
        downstream_service=SERVER_NAME,
    )


#: Consumes the per-call caller block the gateway injects, through the same
#: ``mcp_core`` resolvers ``kirocrew-mate`` uses. Kept in step with
#: ``mcp_discovery._MANAGED_SERVERS_CALLER_AWARE``.
ADVERTISE_CALLER_IDENTITY = True


def run_mcp_server() -> None:
    """Run the MCP stdio server. Entry point for ``kirocrew mcp-apps``."""
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

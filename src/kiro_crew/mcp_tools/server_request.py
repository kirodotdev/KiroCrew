"""``request_server``: any template-based session asks the user for an MCP server it lacks.

The tool names a server and where it comes from; it never carries a launch line,
a session or a template. The gateway resolves the calling session from its
attested key (``POST /api/cards/agent/request-server``), works out the agent
template that session runs on, builds the server entry from a server this
gateway already runs or the official registry's install plan, and raises a
``template.mcp_server`` change card in the caller's own chat. Nothing changes
until the user presses the card. Stateless: the outcome reaches the agent on its
next turn, as every card outcome does.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.mcp_tools.catalog.server_request import (  # noqa: F401
    _DESCRIPTION,
    schemas,
)

REQUEST_SERVER_PATH = "/api/cards/agent/request-server"


def request_server(name: str, args: dict[str, Any]) -> str:
    session, refusal = mcp_core.require_strict_session_key(
        "Error: request_server needs an identified session"
    )
    if not session:
        return refusal
    body = {k: args[k] for k in ("server", "source", "registry_id", "reason") if k in args}
    result = mcp_core._post(REQUEST_SERVER_PATH, body, session_key=session)
    if not isinstance(result, dict):
        return "Error: unexpected gateway answer"
    if result.get("error"):
        code = result.get("code")
        return f"Error: {result['error']}" + (f" ({code})" if code else "")
    card = {
        k: result[k]
        for k in ("id", "status", "title", "risk", "scope", "delivered_clients")
        if k in result
    }
    return json.dumps({"card": card}, ensure_ascii=False)


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "request_server": request_server,
}

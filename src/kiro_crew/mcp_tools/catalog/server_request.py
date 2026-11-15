"""Tool declarations of the ``server_request`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.server_request`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

from typing import Any

#: Which sessions get a refusal rather than a card, said up front so the agent
#: does not try from a turn nobody sees.
_DESCRIPTION = (
    "Ask the user to add one MCP server to the agent you run on, when the work needs "
    "a tool you do not have. Name the server: source 'gateway' for a server this "
    "Kiro Crew already runs (its name on the MCP page), or source 'registry' with "
    "the official MCP registry id. You never give a command or a URL; Kiro Crew "
    "builds the server entry. The user sees a card listing every agent and open "
    "session the server reaches and decides. The card adds no tool approval. "
    "Use this instead of starting a server's program from the shell. Only "
    "a message the user sent in the dashboard can raise the card: a schedule, a "
    "subagent, a messaging channel or a patrol wake is refused with the reason. "
    "Nothing changes when you call this; the outcome reaches you on your next turn."
)


def schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "request_server",
            "description": _DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "maxLength": 128,
                        "description": "The server's name on this gateway (source 'gateway').",
                    },
                    "source": {"type": "string", "enum": ["gateway", "registry"]},
                    "registry_id": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "The official MCP registry id (source 'registry').",
                    },
                    "reason": {"type": "string", "maxLength": 500},
                },
                "required": ["source"],
                "additionalProperties": False,
            },
        }
    ]

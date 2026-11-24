"""Tool declarations of the ``server_request`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.server_request`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

from typing import Any

#: Which sessions get a refusal rather than a card, said up front so the agent
#: does not try from a turn nobody sees.
_DESCRIPTION = (
    "Ask the user to add, change or remove an MCP server. This is the only way to "
    "change MCP servers: the MCP config files (mcp.json) are not writable by you. "
    "To give the agent you run on a server it lacks, use action 'add' with source "
    "'gateway' for a server this Kiro Crew already runs (its name on the MCP page) "
    "or source 'registry' with the official MCP registry id; Kiro Crew builds the "
    "entry, and the card lists every agent and open session it reaches. To add a "
    "new server to the MCP page, use action 'add' with source 'custom' and the "
    "server's entry ({command, args?, env?} or {url, headers?}). To change one, use "
    "action 'change' with the server's name and its whole new entry; to remove one, "
    "action 'remove' with its name. With an entry, always set uses_chat_folder: true "
    "if the server reads or writes files in the chat's folder (a filesystem, git or "
    "code tool, or one given '.', a relative path or the project path), false if it "
    "does not; Kiro Crew also checks the entry itself, and a server that uses the "
    "folder keeps one copy per chat. The card shows the change and the user applies "
    "it. To also add it to kiro-cli's own list (~/.kiro/settings/mcp.json, read by "
    "kiro-cli and other Kiro tools), set target 'kiro-cli'. The card adds no tool approval. "
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
                        "description": (
                            "The server's name: on this gateway (source 'gateway'), the "
                            "new name (source 'custom'), or the one to change or remove."
                        ),
                    },
                    "action": {
                        "type": "string",
                        "enum": ["add", "change", "remove"],
                        "description": "Default 'add'.",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["gateway", "registry", "custom"],
                        "description": "For action 'add' only.",
                    },
                    "entry": {
                        "type": "object",
                        "description": (
                            "The server's whole entry, for source 'custom' and for "
                            "action 'change': {command, args?, env?} or {url, headers?}."
                        ),
                    },
                    "registry_id": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "The official MCP registry id (source 'registry').",
                    },
                    "uses_chat_folder": {
                        "type": "boolean",
                        "description": (
                            "Required with an entry (source 'custom' or action "
                            "'change'): does the server read or write files in the "
                            "chat's folder?"
                        ),
                    },
                    "target": {
                        "type": "string",
                        "enum": ["crew", "kiro-cli"],
                        "description": (
                            "For action 'add' with source 'custom': 'kiro-cli' also adds "
                            "it to ~/.kiro/settings/mcp.json. Default 'crew'."
                        ),
                    },
                    "reason": {"type": "string", "maxLength": 500},
                },
                "required": [],
                "additionalProperties": False,
            },
        }
    ]

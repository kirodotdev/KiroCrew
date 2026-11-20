"""Tool declarations of the ``resources`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.resources`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

from typing import Any


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the resource-subscription tools."""
    return [
        {
            "name": "resource_subscribe",
            "description": (
                "Subscribe THIS session to an MCP resource on one of its own MCP "
                "servers, and be woken when the server reports it changed -- even "
                "after this session's kiro-cli has stopped. Each wake is a "
                "[Webhook wake] turn that names the URI as untrusted data and "
                "repeats your `instruction`, which is the only part of the wake "
                "that instructs. Updates are coalesced (one wake per 60 s window) "
                "and budgeted (48 wakes a day per subscription, then paused until "
                "the next UTC day). Subscribing the same server and URI again keeps "
                "the subscription and replaces its instruction; it also resumes a "
                "subscription paused because its server could not be reached. "
                "Refused with a reason when resource subscriptions are off, the "
                "session has no dashboard tab, the server is not attached to this "
                "session or does not support subscriptions, or a limit is reached. "
                "Only for dashboard sessions; prefer it to a polling loop when the "
                "server supports it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "maxLength": 128,
                        "description": "The MCP server's name as this session knows it.",
                    },
                    "uri": {
                        "type": "string",
                        "maxLength": 8192,
                        "description": "The resource URI to watch.",
                    },
                    "instruction": {
                        "type": "string",
                        "maxLength": 2000,
                        "description": "What to do when woken: carried by every wake.",
                    },
                },
                "required": ["server", "uri", "instruction"],
            },
        },
        {
            "name": "resource_subscriptions",
            "description": (
                "List THIS session's MCP resource subscriptions: id, server, full "
                "URI, instruction, state (active, or paused with its reason) and "
                "wakes today."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "resource_unsubscribe",
            "description": (
                "Remove one of THIS session's MCP resource subscriptions by the id "
                "resource_subscribe or resource_subscriptions returned. Answers "
                "not_found for an id this session does not hold."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subscription_id": {"type": "string", "maxLength": 64},
                },
                "required": ["subscription_id"],
            },
        },
    ]

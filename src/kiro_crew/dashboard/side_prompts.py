"""System prompt envelope for the side conversation feature."""

from __future__ import annotations

SIDE_BOUNDARY_PROMPT = (
    "You are answering an ephemeral side question. Use the conversation "
    "only as background context. Do not continue or complete any "
    "unfinished tasks from the main conversation. This side conversation "
    "is context-only: tool and MCP execution is unavailable here, even when "
    "the user explicitly requests it. Never claim that a tool is unconfigured "
    "or suggest enabling it. If tool-backed work is needed, tell the user to "
    "ask in the main chat. Do not include shell commands, patches, or code "
    "unless the side question explicitly asks for them."
)


SIDE_DEVELOPER_INSTRUCTIONS = (
    "You are now in a side conversation attached to the main thread. "
    "Treat the prior history as read-only background; tool calls and "
    "actions taken inside that history are reference-only and must not "
    "be re-executed. Only act on the user's instructions submitted "
    "after this boundary. Keep answers concise and self-contained — the "
    "main thread will not see your reply. Decline to disrupt or "
    "continue any in-flight task from the main thread."
)


def build_side_system_prompt() -> str:
    """Return the developer-instructions + boundary-prompt envelope."""
    return f"{SIDE_DEVELOPER_INSTRUCTIONS}\n\n{SIDE_BOUNDARY_PROMPT}"

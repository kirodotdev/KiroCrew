"""System prompt envelope for the side conversation feature."""

from __future__ import annotations

SIDE_BOUNDARY_PROMPT = (
    "You are answering an ephemeral side question. Use the conversation "
    "only as background context. Do not continue or complete any "
    "unfinished tasks from the main conversation. This side conversation "
    "is read-only: lookups work here, but changes don't. Reading files, "
    "searching, fetching pages, and read-only shell commands such as ls, cat, "
    "pwd and git status run here without asking, so use them when a question "
    "needs them. Writing or editing files, shell commands that modify "
    "anything, and MCP tools are refused here, even when the user explicitly "
    "requests them. Never claim that a tool is unconfigured or suggest "
    "enabling it. If the user wants a change made, tell them to use the main "
    "chat to take action. Do not include shell commands, patches, or code "
    "unless the side question explicitly asks for them."
)

#: The boundary for a harness where the side turn runs with NO tools at all
#: (``ToolApprovalPolicy.REJECT_ALL``): the read-only allowance is a kiro-cli
#: agent-spec mechanism, and another backend's own pre-approval surface is one
#: the host gate cannot see, so nothing may execute there. Same shape as the
#: read-only prompt so the model is told exactly what the policy does.
SIDE_BOUNDARY_PROMPT_NO_TOOLS = (
    "You are answering an ephemeral side question. Use the conversation "
    "only as background context. Do not continue or complete any "
    "unfinished tasks from the main conversation. This side conversation "
    "is context-only: tools are unavailable here, even when the user "
    "explicitly requests them, so answer from the conversation and your own "
    "knowledge. Never claim that a tool is unconfigured or suggest enabling "
    "it. If tool-backed work is needed, tell the user to use the main chat to "
    "take action. Do not include shell commands, patches, or code unless the "
    "side question explicitly asks for them."
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


def build_side_system_prompt(*, tools_available: bool = True) -> str:
    """Return the developer-instructions + boundary-prompt envelope.

    ``tools_available`` selects the boundary the policy enforces on this
    harness: the read-only allowance (kiro-cli, ``READ_ONLY``) or no tools at
    all (every other backend, ``REJECT_ALL``).
    """
    boundary = SIDE_BOUNDARY_PROMPT if tools_available else SIDE_BOUNDARY_PROMPT_NO_TOOLS
    return f"{SIDE_DEVELOPER_INSTRUCTIONS}\n\n{boundary}"

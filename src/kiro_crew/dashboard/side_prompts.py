"""System prompt envelope for the side conversation feature."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

SIDE_BOUNDARY_PROMPT = (
    "You are answering an ephemeral side question. Use the conversation "
    "only as background context. Do not continue or complete any "
    "unfinished tasks from the main conversation. This side conversation "
    "is read-only: lookups work here, but changes don't. Reading files, "
    "searching, fetching pages, and read-only shell commands such as ls, cat "
    "and pwd run here without asking, so use them when a question needs them. "
    "git is refused here; for git output, tell the user to ask in the main "
    "chat. If a file-read, search or fetch tool is refused, use a "
    "read-only shell command (cat, grep, ls) instead rather than retrying it. "
    "Writing or editing files, shell commands that modify "
    "anything, and MCP tools are refused here, even when the user explicitly "
    "requests them. Never claim that a tool is unconfigured or suggest "
    "enabling it. If the user wants a change made, tell them to use the main "
    "chat to take action. Do not include shell commands, patches, or code "
    "unless the side question explicitly asks for them."
)

#: The read-only boundary for a side session with no shell tool (claude, which
#: starts it with only its read built-ins). Naming shell commands there would
#: send the model after a tool the session does not have.
SIDE_BOUNDARY_PROMPT_NO_SHELL = (
    "You are answering an ephemeral side question. Use the conversation "
    "only as background context. Do not continue or complete any "
    "unfinished tasks from the main conversation. This side conversation "
    "is read-only: lookups work here, but changes don't. Reading files, "
    "searching the codebase, and searching or fetching web pages run here "
    "without asking, so use them when a question needs them. There is no "
    "shell here. Writing or editing files, running commands, and MCP tools "
    "are refused here, even when the user explicitly requests them. Never "
    "claim that a tool is unconfigured or suggest enabling it. If the user "
    "wants a change made, tell them to use the main chat to take action. Do "
    "not include shell commands, patches, or code unless the side question "
    "explicitly asks for them."
)

#: The read-only boundary for a side session that can list folders and view
#: images but has no shell and no tool that reads a file's text (goose, whose
#: only text reader is its shell).
SIDE_BOUNDARY_PROMPT_LISTING = (
    "You are answering an ephemeral side question. Use the conversation "
    "only as background context. Do not continue or complete any "
    "unfinished tasks from the main conversation. This side conversation "
    "is read-only: lookups work here, but changes don't. Listing folders and "
    "viewing images run here without asking, so use them when a question "
    "needs them. There is no shell here and no tool that reads a file's text, "
    "so answer questions about file contents from the conversation. Writing "
    "or editing files, running commands, and MCP tools are refused here, even "
    "when the user explicitly requests them. Never claim that a tool is "
    "unconfigured or suggest enabling it. If the user wants a change made, "
    "tell them to use the main chat to take action. Do not include shell "
    "commands, patches, or code unless the side question explicitly asks for "
    "them."
)

#: The boundary for a harness where the side turn runs with NO tools at all
#: (``ToolApprovalPolicy.REJECT_ALL``): its own pre-approval surface is one the
#: host gate cannot see, so nothing may execute there. Same shape as the
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


def side_boundary_prompt(
    *,
    tools_available: bool = True,
    shell_available: bool = True,
    file_reads_available: bool = True,
) -> str:
    """The boundary the policy enforces on this harness.

    ``tools_available`` picks the read-only allowance (``READ_ONLY``) or no
    tools at all (``REJECT_ALL``); ``shell_available`` says whether the
    read-only session has a shell tool for the read-only commands, and
    ``file_reads_available`` whether a shell-less one can read a file's text.
    """
    if not tools_available:
        return SIDE_BOUNDARY_PROMPT_NO_TOOLS
    if shell_available:
        return SIDE_BOUNDARY_PROMPT
    return SIDE_BOUNDARY_PROMPT_NO_SHELL if file_reads_available else SIDE_BOUNDARY_PROMPT_LISTING


def build_side_system_prompt(
    *,
    tools_available: bool = True,
    shell_available: bool = True,
    file_reads_available: bool = True,
) -> str:
    """Return the developer-instructions + boundary-prompt envelope."""
    boundary = side_boundary_prompt(
        tools_available=tools_available,
        shell_available=shell_available,
        file_reads_available=file_reads_available,
    )
    return f"{SIDE_DEVELOPER_INSTRUCTIONS}\n\n{boundary}"


def build_side_format_prompt(cfg: KiroCrewConfig | None = None) -> str:
    """The dashboard's rendering contract and the reply-style setting, for a side turn.

    The main chat gets both on every turn from ``context.build_message``: the
    ``[CRITICAL RULES]`` block says how the dashboard draws a reply (an absolute
    path in backticks becomes a file chip, a URL must be a markdown link, an
    ``[OPTIONS:]`` last line becomes buttons), and ``[RESPONSE PREFERENCES]``
    carries ``dashboard.verbosity``. A side turn builds its own envelope
    (``side_context.build_side_message``) and never calls ``build_message``, and
    the derived read-only agent spec keeps the base spec's prompt, which on the
    built-in agent only points at an ``[AGENT SYSTEM PROMPT]`` block no side turn
    sends. So the side transcript is drawn by the same components as the main
    chat while its model was told nothing about how they draw.

    The rules are the main chat's own constants rather than a copy, so the two
    surfaces cannot drift apart. The diff-block rule is the one part left out:
    a side turn cannot change a file, so there is nothing for it to describe.

    ``cfg`` is the config the turn already loaded (``_run_side_turn`` reads it to
    resolve the agent); ``None`` when that load failed, in which case the
    reply-style block is omitted rather than loaded again here. Imported inside
    the function: ``kiro_crew.context`` is a heavy module that the dashboard
    handlers load lazily for the same reason.
    """
    from kiro_crew.context import _CRITICAL_RULES_HEAD, _CRITICAL_RULES_TAIL
    from kiro_crew.context_assembly.sections import _build_response_preferences_section

    block = _CRITICAL_RULES_HEAD + _CRITICAL_RULES_TAIL
    if cfg is not None:
        block += _build_response_preferences_section(cfg)
    return block.rstrip("\n")

"""What a finished tool call's PostToolUse hooks are matched on.

kiro-cli matches a ``postToolUse`` hook against its own tool name. On a backend
where Crew fires the spec's hooks, a call's frames name it late or not at all, so
each turn loop keeps a :class:`PostToolIdentity` per call from its ``tool_call``
frame and its permission request (:func:`post_tool_identity`) and matches the
finished call on that.

These live here, not in :mod:`kiro_crew.agent_sdk.spec_hooks`, to keep that module
under its line cap; they are re-exported from it for the modules that import them
there.
"""

from __future__ import annotations

from typing import NamedTuple

from kiro_crew.agent_sdk.capabilities import capabilities_of


def post_tool_hook_tool_id(provider: object, *, harness_tool_id: str, is_shell: bool) -> str:
    """The tool id a finished call's PostToolUse spec hooks match on, or "".

    kiro-cli matches a postToolUse hook against its own tool name. KAS's tool-call
    frame names no built-in tool (its ``_meta.kiro.toolId`` is set for a few
    non-built-in ones only) and titles the call in prose, so the title alone
    meets no tool matcher a spec writes. The call is named by what the turn
    learned of it instead: the id its permission request stated
    (*harness_tool_id*), else, for a call the frame classified as a shell
    command, KAS's shell tool, which :func:`spec_hook_tool_names` reads as
    kiro-cli's ``execute_bash`` -- the one tool kiro-cli runs a command with.

    claude names its shell call the same way, as its frames carry no tool id. A codex
    ``tool_call`` event carries its own id (``codex#exec_command``, ``codex#apply_patch``,
    :func:`kiro_crew.acp._dispatch_parts.tool_call_identity.codex_tool_name`), so the
    fallback names only a codex call that event left unnamed; a codex MCP call is
    kind ``execute`` too, but its MCP marker classifies it not shell.

    "" on a backend whose harness runs the spec's hooks itself, and for a call
    nothing names, which keeps the title matching it had.
    """
    if not capabilities_of(provider).crew_fires_spec_observer_hooks:
        return ""
    if harness_tool_id:
        return harness_tool_id
    if not is_shell:
        return ""
    # circular import: see spec_hooks.spec_hook_tool_names.
    from kiro_crew.acp.harness_tool_names import ENVELOPE_NAMED_TOOL_BACKENDS
    from kiro_crew.acp.kas_permissions import KAS_TOOL_IDS_BY_KIRO_TOOL

    # On Strands the shell verdict a card carries is the CLI's guess from the tool
    # name, so only the id the gate envelope stated names the call.
    if capabilities_of(provider).backend in ENVELOPE_NAMED_TOOL_BACKENDS:
        return ""

    shell_ids = KAS_TOOL_IDS_BY_KIRO_TOOL.get("execute_bash") or ()
    return shell_ids[0] if shell_ids else ""


class PostToolIdentity(NamedTuple):
    """What a finished call's PostToolUse hooks are matched on, kept per call.

    The harness id (:func:`post_tool_hook_tool_id`), and the call's canonical tool
    name and trusted MCP server, so a spec matcher written ``@server/tool`` meets an
    MCP call by those forms as it does on PreToolUse
    (``hooks.pre_tool_match_names``). ``requested`` says a permission request has
    stated the call's tool id, after which no later request renames it.
    """

    tool_id: str = ""
    tool_identity: str = ""
    mcp_server: str = ""
    requested: bool = False


def post_tool_identity(
    provider: object,
    event: object,
    *,
    is_shell: bool,
    earlier: PostToolIdentity | None = None,
) -> PostToolIdentity | None:
    """*event*'s :class:`PostToolIdentity`, or ``None`` where Crew fires no
    PostToolUse spec hooks.

    *event* is the call's ``tool_call`` frame or its permission request; a field
    the event leaves empty keeps what *earlier* recorded for the same call.

    The first request that states a tool id names the call, and a later request
    under the same call id changes nothing. One call can raise several: KAS asks
    once per consent round (``_meta.kiro.consentRound``), and a later round states
    the tool that round is about, not the call's. A shell call that also reads its
    directory asks as ``run_command``, then as ``list_directory``; named by the
    last, the finished command would meet no ``execute_bash`` hook.
    """
    if not capabilities_of(provider).crew_fires_spec_observer_hooks:
        return None
    if earlier is not None and earlier.requested:
        return earlier
    # circular import: kiro_crew.acp imports the agent_sdk package at load time.
    from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST

    base = earlier or PostToolIdentity()
    stated = getattr(event, "harness_tool_id", "") or ""
    tool_id = post_tool_hook_tool_id(provider, harness_tool_id=stated, is_shell=is_shell)
    identity = getattr(event, "tool_name", "") or ""
    server = getattr(event, "mcp_server_name", "") or ""
    return PostToolIdentity(
        tool_id=tool_id or base.tool_id,
        tool_identity=identity if isinstance(identity, str) and identity else base.tool_identity,
        mcp_server=server if isinstance(server, str) and server else base.mcp_server,
        requested=bool(stated) and getattr(event, "kind", "") == EVENT_PERMISSION_REQUEST,
    )

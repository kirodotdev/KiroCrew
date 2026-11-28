"""Whether a dashboard tool call is one the person must confirm under
``hooks.confirm_git_writes``, and the audit row and card notice for one that is.

``tool_approval._decide_permission_request`` reads :func:`_git_write_needing_confirm`
once per request, before any grant rung; a non-empty reason sends the call to a
one-shot Allow once / Reject card."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.source_providers.adf import _md_inline_code
from kiro_crew.name_grant import shell_command_for_event
from kiro_crew.security.git_write import UNREADABLE as UNREADABLE_GIT_COMMAND
from kiro_crew.security.git_write import git_history_write, names_git

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import sel
    from kiro_crew.dashboard.chat_turn.deny_chokepoints import _redact_display_text


def _git_write_needing_confirm(state: Any, slot: Any, event: Any) -> str:
    """Why this shell call needs the person under ``hooks.confirm_git_writes``, or ``""``.

    Non-empty when the call runs git in any way ``security.git_write`` cannot
    show is a known read, or holds a shell expansion it does not read, or cannot
    be read at all. It means every auto-approve rung below the hook gate's deny
    is skipped for this call -- the hook layer's auto-approve, native crew,
    session trusted patterns, trust-reads, session trust (``_trust`` and the
    scoped ``_trust_scope``) and YOLO -- and it goes to the interactive card,
    every time: nothing a session holds lifts it. A deny still wins: this runs
    after the gate's ``TOOL_DENY``.

    Empty when ``hooks.confirm_git_writes`` is off, for a non-shell tool, and for
    a shell command that writes no history (``security.git_write``). The command
    is the one the hook gate judged (``event.shell_command``). A low-fidelity
    child request (arguments absent) counts as a write unless its MCP identity is
    verified (``child_mcp_identity_trusted``), and so does a shell call
    whose command cannot be read when no hook gate ran (the gate refuses it
    otherwise).
    """
    builder = getattr(state, "context_builder", None)
    hooks = getattr(builder, "hooks", None) if builder else None
    if hooks is not None and not bool(getattr(hooks, "confirm_git_writes", True)):
        return ""
    if getattr(event, "child_low_fidelity", False) and not getattr(
        event, "child_mcp_identity_trusted", False
    ):
        # A backend-subagent request whose arguments never reached us: nothing
        # shows it is not a git history write, so it is put to the person. A
        # verified MCP identity is not a shell command, and with its arguments
        # present it would not ask either (``_raw_shell_commands``), so it passes.
        return UNREADABLE_GIT_COMMAND
    if not getattr(event, "is_shell", False):
        # A harness may stream its shell tool under another kind (the DeepSeek
        # harness sends ``bash`` as ``other``): the tool's own ``command``/``cmd``
        # argument is what runs. This can only add a confirm, never a grant.
        for raw_command in _raw_shell_commands(event):
            reason = git_history_write(raw_command)
            if reason:
                return reason
        return ""
    cmd = shell_command_for_event(event)
    if not cmd:
        # With a hook gate, its deny-by-default tier refuses a shell call
        # whose command cannot be recovered before any grant here. With no
        # gate, nothing has read the command, so it asks.
        return "" if builder is not None else UNREADABLE_GIT_COMMAND
    return git_history_write(cmd)


def _note_git_write_confirm(
    *, session_key: str, slot: Any, event: Any, reason: str, notices: dict[str, str]
) -> None:
    """Audit that a confirmed command is being put to the person, and hold the card notice."""
    sel().log_tool_invocation(
        session_key=session_key,
        agent=slot.agent or "kirocrew",
        source="dashboard",
        tool_name=_redact_display_text(event.title),
        tool_kind=event.tool_kind,
        outcome="auto_approve_declined",
        request_id=event.request_id,
        error="git history writes are confirmed in this session",
        metadata={"reason": "git_write_confirm", "git_write": reason},
    )
    if getattr(event, "is_shell", False):
        cmd = shell_command_for_event(event) or ""
    else:
        cmd = next(
            (c for c in _raw_shell_commands(event) if git_history_write(c)),
            "",
        )
    if not cmd:
        # Nothing of the call's arguments was read (a sub-agent request whose
        # arguments never arrived, or a shell call with no recoverable command):
        # the card names the tool, and claims nothing about what it would run.
        note = (
            f"The arguments of {_md_inline_code(_unread_call_name(event))} could not "
            "be checked, so it asks even with trust or YOLO on. Approve it once to run it."
        )
    elif not names_git(cmd):
        # A git-free command that holds a shell expansion: it asks because the
        # expansion is not read, not because it writes git.
        note = (
            "This command holds a shell expansion, so it asks even with trust or "
            "YOLO on. Approve it once to run it."
        )
    else:
        note = (
            "This git command is not a known read-only one, so it asks even with "
            "trust or YOLO on. Approve it once to run it."
        )
    notices.setdefault("git_write_confirm", note)


def _raw_shell_commands(event: Any) -> list[str]:
    """A non-shell call's own ``command``/``cmd`` argument, as the gate reads it.

    Mirrors ``GateFacts.raw_shell_commands`` (``hook_runtime/gate_tiers.py``): a
    harness may stream its shell tool under another kind, so this is the command
    that runs. Empty for a call that names its MCP server, whose arguments are not
    shell text, and for a call with no parameters dict.
    """
    raw_params = getattr(event, "raw_tool_params", None)
    if getattr(event, "mcp_server_name", "") or not isinstance(raw_params, dict):
        return []
    commands: list[str] = []
    for key in ("command", "cmd"):
        raw_command = raw_params.get(key)
        if isinstance(raw_command, str) and raw_command and raw_command not in commands:
            commands.append(raw_command)
    return commands


def _unread_call_name(event: Any) -> str:
    """The tool a call whose arguments were not read is shown as on its card.

    Its verified ``@server/tool`` identity when there is one, else its title,
    redacted and cut to one short line (the title is agent-authored).
    """
    if (
        event.mcp_server_name
        and event.tool_name
        and (event.mcp_identity_trusted or getattr(event, "child_mcp_identity_trusted", False))
    ):
        return f"@{event.mcp_server_name}/{event.tool_name}"
    title = " ".join(_redact_display_text(event.title or "").split()) or "this tool"
    return title if len(title) <= 80 else f"{title[:79]}…"

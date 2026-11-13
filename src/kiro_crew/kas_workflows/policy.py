"""Screen a workflow step's tool call against the operator's command deny rules.

A KAS workflow step runs in a session KAS opened itself, under a ``wf-*`` agent
that carries none of Crew's hooks, so the deny rules a chat turn enforces never
see its commands. Before a step's permission request reaches a person, its
command is run through the same three gates the cron ``command`` field uses
(``mcp_cron``): the effective deny set (Settings > Security opt-outs honoured,
governance pins re-added), the sensitive-path check and the exfiltration audit.
A match is answered with a reject and never shown as an approvable card: an
operator's deny rule is not a question for whoever is watching the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: Longest command text screened. A longer one is refused unscreened: a deny
#: regex can only vouch for text it actually read.
MAX_SCREENED_COMMAND_CHARS = 64 * 1024

#: ``rawInput`` keys a shell-shaped tool names its command under.
_COMMAND_KEYS = ("command", "cmd", "script")


@dataclass(frozen=True)
class StepCommand:
    """The command a step's permission request asks to run, as every reader sees it.

    ``text`` is the command to screen, show and grant on, or None when the call
    names none. ``unreadable`` is True when a command is named in a shape this
    resolver cannot read (not a non-empty string or list of strings, or two places
    naming different commands): such a call is never screened clean, never matched
    by a grant and never answered by an approval mode, so a person is asked.
    """

    text: str | None
    unreadable: bool = False


_NONE = StepCommand(None)
_UNREADABLE = StepCommand(None, unreadable=True)


def _command_text(value: Any) -> str | None:
    """*value* as command text, or None when it is not a readable command."""
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        joined = " ".join(value)
        return joined if joined.strip() else None
    return None


def resolve_step_command(params: Mapping[str, Any]) -> StepCommand:
    """The command a step's permission request asks to run, from every place KAS names it.

    KAS's own ``_meta.kiro.command`` (set only for a shell call) and the call's
    ``toolCall.rawInput`` (``command`` / ``cmd`` / ``script``) are both read. Any
    of them present but unreadable, or two of them disagreeing, is
    :data:`StepCommand.unreadable`.
    """
    found: list[Any] = []
    meta = params.get("_meta")
    if meta is not None:
        if not isinstance(meta, Mapping):
            return _UNREADABLE
        kiro = meta.get("kiro")
        if kiro is not None and not isinstance(kiro, Mapping):
            return _UNREADABLE
        if isinstance(kiro, Mapping) and "command" in kiro:
            found.append(kiro.get("command"))
    tool_call = params.get("toolCall")
    if tool_call is not None and not isinstance(tool_call, Mapping):
        return _UNREADABLE
    raw = tool_call.get("rawInput") if isinstance(tool_call, Mapping) else None
    if raw is not None:
        if not isinstance(raw, Mapping):
            return _UNREADABLE
        found.extend(raw.get(key) for key in _COMMAND_KEYS if key in raw)
    if not found:
        return _NONE
    texts = [_command_text(value) for value in found]
    if any(text is None for text in texts) or len(set(texts)) != 1:
        return _UNREADABLE
    return StepCommand(texts[0])


def deny_reason(command: str) -> str | None:
    """Why *command* is refused by the operator's rules, or None when it is not."""
    if len(command) > MAX_SCREENED_COMMAND_CHARS:
        return "command is too long to screen"
    from kiro_crew.hooks import effective_denied_regexes_from_config
    from kiro_crew.platform import current_context
    from kiro_crew.security import (
        audit_bash_exfiltration,
        enabled_rule_ids,
        is_sensitive_bash_command,
    )

    effective = effective_denied_regexes_from_config()
    enabled_ids = enabled_rule_ids(effective)
    return (
        current_context().security.is_denied(command, denied_regexes=effective)
        or is_sensitive_bash_command(command, enabled_ids=enabled_ids)
        or audit_bash_exfiltration(command, enabled_ids=enabled_ids)
        or None
    )

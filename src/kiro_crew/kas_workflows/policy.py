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

from typing import Any, Mapping

#: Longest command text screened. A longer one is refused unscreened: a deny
#: regex can only vouch for text it actually read.
MAX_SCREENED_COMMAND_CHARS = 64 * 1024

#: ``rawInput`` keys a shell-shaped tool names its command under.
_COMMAND_KEYS = ("command", "cmd", "script")


def step_command(params: Mapping[str, Any]) -> str | None:
    """The shell command a step's permission request asks to run, if any."""
    tool_call = params.get("toolCall")
    if not isinstance(tool_call, Mapping):
        return None
    raw = tool_call.get("rawInput")
    if not isinstance(raw, Mapping):
        return None
    for key in _COMMAND_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            return " ".join(value)
    return None


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

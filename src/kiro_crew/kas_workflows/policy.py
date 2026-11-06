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


#: Largest screening snapshot a held step keeps (:func:`screen_snapshot`). A
#: larger request keeps none, and the sweep and a person's answer then judge the
#: identity event alone.
MAX_SNAPSHOT_CHARS = 64 * 1024


def screen_snapshot(params: Mapping[str, Any]) -> dict[str, Any] | None:
    """The bounded part of a step's request every re-check reads, or None.

    Only what the screens read: ``toolCall`` title, ``rawInput`` and
    ``locations``, and ``_meta.kiro`` ``toolId`` / ``command`` / ``consent``. None
    when it does not fit :data:`MAX_SNAPSHOT_CHARS` or cannot be rendered.
    """
    import json

    tool_call = params.get("toolCall")
    tool_call = tool_call if isinstance(tool_call, Mapping) else {}
    meta = params.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, Mapping) else None
    kiro = kiro if isinstance(kiro, Mapping) else {}
    snap = {
        "sessionId": params.get("sessionId"),
        "toolCall": {k: tool_call.get(k) for k in ("toolCallId", "title", "rawInput", "locations")},
        "_meta": {"kiro": {k: kiro.get(k) for k in ("toolId", "command", "consent")}},
    }
    try:
        if len(json.dumps(snap)) > MAX_SNAPSHOT_CHARS:
            return None
    except (TypeError, ValueError):
        return None
    return snap


def _screen_event(event: Any, params: Mapping[str, Any] | None) -> Any | None:
    """A copy of *event* whose ``tool_input`` also carries the request's own inputs.

    The chat path's screen reads ``event.tool_input`` (the call's arguments, from
    the ``tool_call`` frame). A step's request may name more -- ``toolCall.rawInput``,
    every ``toolCall.locations[].path``, KAS's ``_meta.kiro.consent.resource`` --
    and each is screened too, which can only refuse more. None when they cannot
    be rendered.
    """
    import copy
    import json

    params = params or {}
    tool_call = params.get("toolCall")
    tool_call = tool_call if isinstance(tool_call, Mapping) else {}
    meta = params.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, Mapping) else None
    consent = kiro.get("consent") if isinstance(kiro, Mapping) else None
    locations = tool_call.get("locations")
    inputs = {
        "input": event.tool_input or "",
        "rawInput": tool_call.get("rawInput"),
        "locations": [
            loc.get("path")
            for loc in (locations if isinstance(locations, list) else [])
            if isinstance(loc, Mapping)
        ],
        "resource": consent.get("resource") if isinstance(consent, Mapping) else None,
    }
    try:
        rendered = json.dumps(inputs)
    except (TypeError, ValueError):
        return None
    screened = copy.copy(event)
    screened.tool_input = rendered
    return screened


def step_input_denial(event: Any, params: Mapping[str, Any] | None = None) -> str | None:
    """Why a step's tool call is refused on its inputs, or None when nothing refuses it.

    The chat turn's own screen (:func:`~kiro_crew.llm_helpers_parts.tool_approval.
    tool_input_screen`: the title tier, then the tool-input tier with its path,
    shell and deny-rule checks and its size ceiling) over *event*, the step's
    identity event (:mod:`kiro_crew.kas_workflows.step_identity`), with the
    request's own inputs added (:func:`_screen_event`).

    Fails closed: inputs that cannot be rendered, or a screen that raises, refuse.
    Blocking: call it off the loop.
    """
    from kiro_crew.hooks import effective_denied_regexes_from_config
    from kiro_crew.llm_helpers_parts.tool_approval import tool_input_screen

    regexes = effective_denied_regexes_from_config()
    title = event.title if isinstance(event.title, str) else ""
    if not title:
        return "the tool call carried no title"  # as the chat turn refuses it
    # The event as the chat turn screens it, then the request's own inputs too.
    hit = tool_input_screen(event, None, title, title, regexes)
    if hit is not None:
        return hit[1]
    screened = _screen_event(event, params)
    if screened is None:
        return "the step's tool inputs could not be read"
    hit = tool_input_screen(screened, None, title, title, regexes)
    return hit[1] if hit is not None else None


@dataclass(frozen=True)
class StepGate:
    """The chat turn's tool gate on a step's request: refuse it, hold it, or neither."""

    #: Why the request is refused (a gate deny or a PreToolUse block), or None.
    refusal: str | None = None
    #: A moved Bot's held rule covers the call: only a person may allow it.
    held: bool = False


def _gate_refusal(event: Any, session_key: str) -> str | None:
    from kiro_crew.hooks import unattended_turn
    from kiro_crew.permission_floor import refusal_for

    # A step under Trust runs with nobody watching: a governance check that
    # cannot be evaluated refuses, as on a monitor wake.
    with unattended_turn(True):
        return refusal_for(event, session_key=session_key, security_only=False)


async def step_gate(event: Any, session_key: str, *, fire_hooks: bool = True) -> StepGate:
    """The tool gate a chat turn runs before any approval path, over a step's event.

    The same checks, in the chat turn's order, on the identity event under the
    owning chat's *session_key*: ``HookManager.on_tool_call`` (governance ceiling,
    the operator's ``auto_deny_tools`` and deny rules, every deny tier) through
    :func:`kiro_crew.permission_floor.refusal_for`, any ``TOOL_DENY`` refusing;
    then, with *fire_hooks*, the Hooks page's PreToolUse hooks on the call's own
    arguments (``permission_pre_tool_block``, the channel and sub-agent gate), a
    block refusing; then a moved Bot's held rules (``mate_held_rules.held_for``),
    which hold the call for a person. A re-judgement passes ``fire_hooks=False``:
    the hooks fired once for the request, as the chat path fires them once and
    re-judges an overridden call on the gate alone. Fails closed: a gate that
    cannot be built and a hook fire that raises both refuse.
    """
    import asyncio

    from kiro_crew import hooks as hooks_mod
    from kiro_crew import mate_held_rules

    reason = await asyncio.to_thread(_gate_refusal, event, session_key)
    if reason:
        return StepGate(refusal=reason)
    if fire_hooks:
        blocked = await hooks_mod.permission_pre_tool_block(
            None,
            (),
            None,
            str(event.title or ""),
            event.tool_input,
            tool_identity=str(getattr(event, "tool_name", "") or ""),
            mcp_server=str(getattr(event, "mcp_server_name", "") or ""),
            harness_tool_id=str(getattr(event, "harness_tool_id", "") or ""),
            session_key=session_key or None,
        )
        if blocked:
            return StepGate(refusal=f"PreToolUse hook: {blocked}")
    held = await asyncio.to_thread(mate_held_rules.held_for, event, session_key=session_key)
    # An organization's ``ask`` on a KAS web call: only a person answers it, as on
    # a chat turn (``web_ask_rule.needs_person``).
    from kiro_crew.web_ask_rule import held_phrases

    return StepGate(held=bool(held) or bool(held_phrases(event)))


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

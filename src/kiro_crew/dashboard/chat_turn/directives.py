"""What a person and the agent are told when a session directive did not apply."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        _ChatSlot,
        append_and_surface,
        directive_queue,
        logger,
    )


#: Outcome line for a directive tool whose effect was not applied, keyed by
#: the tool. The reader is the human watching the session, so the line names
#: what did NOT happen to their session, not the plumbing that failed.
_DIRECTIVE_NOT_APPLIED_OUTCOMES: dict[str, str] = {
    "monitor_start": "Monitor was not set up.",
    "monitor_watch": "Monitor was not set up.",
    "monitor_update": "Monitor was not changed.",
    "monitor_stop": "Monitor was not stopped.",
    "autonudge_stop": "Monitor was not stopped.",
    "set_project": "The project change was not applied.",
    "reset_conversation": "The conversation was not reset.",
    "chat_tag": "The session tags were not changed.",
    "ask_question": "The question was not shown.",
    "nothing_to_do": "The quiet end was not recorded.",
    "suggest_followup": "The follow-up suggestions were not shown.",
}


#: The human-worded outcome for a directive tool this table does not name. The
#: tool identifier belongs in the agent instruction, never first in a line a
#: person reads.
_DIRECTIVE_NOT_APPLIED_FALLBACK = "The request was not applied."


#: The directive tools whose effect ``monitor_inspect`` can confirm.
_MONITOR_DIRECTIVE_TOOLS: frozenset[str] = frozenset(
    {"monitor_start", "monitor_watch", "monitor_update", "monitor_stop", "autonudge_stop"}
)


#: The unattributed case: the gateway holds a parked request that no tool call
#: in this turn claimed, so it cannot even name the tool. Only a person reads
#: this row (no tool result exists to carry an agent instruction), so it states
#: the outcome in plain words and nothing else.
UNCLAIMED_DIRECTIVE_NOTICE = (
    "A request from this turn was not applied. Check that your last request took effect."
)


def _directive_recovery_instruction(tool: str) -> str:
    """The agent's one recovery step for a dropped *tool* effect.

    A monitor tool has an inspector (``monitor_inspect``) that answers whether a
    monitor exists; every other directive tool is told to confirm the session's
    state in its own terms, without being sent to a monitor it never touched.
    """
    if tool in _MONITOR_DIRECTIVE_TOOLS:
        return "call monitor_inspect before requesting it again."
    return "confirm the session's state before requesting it again."


def unverified_directive_outcome(tool: str) -> str:
    """The one sentence a person reads when a directive *tool* effect was dropped.

    This is the whole transcript row: it says what did not happen, in words,
    and carries no tool identifier and no agent instruction.
    """
    return _DIRECTIVE_NOT_APPLIED_OUTCOMES.get(tool, _DIRECTIVE_NOT_APPLIED_FALLBACK)


def unverified_directive_notice(tool: str) -> str:
    """Text appended to the result of a directive tool whose effect was dropped.

    Leads with the same outcome sentence the transcript row shows, then gives
    the agent one tool-appropriate instruction for confirming the session's
    real state before asking again. Only the tool result carries this text;
    the row a person reads is :func:`unverified_directive_outcome` alone.
    """
    return (
        f"{unverified_directive_outcome(tool)} Agent: the {tool} result could not be "
        f"verified, so nothing changed; {_directive_recovery_instruction(tool)}"
    )


async def _report_unclaimed_directives(
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
    *,
    _turn_started: float,
    _seen_tool_identity: dict[str, tuple[str, str]],
) -> None:
    """Tell the person when a directive parked during this turn reached no tool frame."""
    _unclaimed_markers = directive_queue.unclaimed_digest_markers(
        session_key,
        not_before=_turn_started,
    )
    if _unclaimed_markers:
        append_and_surface(state, slot, "notice", UNCLAIMED_DIRECTIVE_NOTICE, "msg msg-info")
        _identity_markers = tuple(
            f"{call_id}:{server or '-'}:{tool or '-'}"
            for call_id, (server, tool) in sorted(_seen_tool_identity.items())
        )
        logger.warning(
            "session-directive UNCLAIMED_AT_TURN_END "
            "session_key=%r count=%d record_digests=%r tool_identities=%r",
            session_key,
            len(_unclaimed_markers),
            _unclaimed_markers,
            _identity_markers,
        )

"""What the model is told about a tool call whose own silence timer ran out.

When a call's silence timer runs out (``acp.tool_silence``) the session handle cancels
the turn, since ACP has no per-call cancel, and ends it as a tool stall whose
completion names the call and carries ``silence_timer=`` evidence. A harness that can
cancel one call returns an error result for it and the model goes on; here the same
session is sent :func:`build_timed_out_continuation` instead, which tells the model
the call timed out, to check whether it took effect, and to retry it if it still
needs it.

Each surface that hands the call back sends this one wording. The dashboard and a
sub-agent run queue it as their tool-stall recovery
(``build_tool_stall_recovery_prompt(..., timed_out=True)``); a messaging channel turn,
the native Slack turn, an agent channel member's turn, the CLI chat, the task runner
and ``stream_and_collect`` send it inside the turn (``acp.timed_out_continuation``). Standard library only, so the dashboard, the
channel-neutral messaging layer and the ACP layer can all import it.
"""

from __future__ import annotations

#: The envelope a tool-stall continuation opens with, which the dashboard renders as
#: a recovery card rather than a user bubble (``dashboard.state`` re-exports it).
TOOL_STALL_RECOVERY_PREFIX = "[Tool stall \u2014 automatic recovery]"

#: How a silence expiry's evidence starts, so a reader (the metric bucket, the
#: recovery prompt) can tell it from an oracle reading.
EVIDENCE_SILENCE_TIMER = "silence_timer"


def is_silence_timeout(evidence: str) -> bool:
    """Whether a tool-stall end's evidence says a per-call silence timer ran out."""
    return (evidence or "").startswith(f"{EVIDENCE_SILENCE_TIMER}=") or (
        f"; {EVIDENCE_SILENCE_TIMER}=" in (evidence or "")
    )


def evidence_int(evidence: str, key: str) -> int:
    """The whole number a tool-stall end's *evidence* carries as ``key=<n>``, or 0."""
    for part in (evidence or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == key and value.isdigit():
            return int(value)
    return 0


def timed_out_call_body(tool_title: str, idle_secs: int) -> str:
    """What the model is told about a call its silence timer cancelled, without the envelope."""
    idle_mins = max(1, round(idle_secs / 60))
    tool_label = tool_title or "a tool call"
    return "\n".join(
        [
            f"Your previous turn stalled: {tool_label} produced no response for "
            f"~{idle_mins} minute(s) and the turn was ended by a Kiro Crew watchdog. "
            "This was NOT a user action: do not treat it as a cancellation or "
            "interruption by the user.",
            "",
            "Before doing anything else, check whether the tool actually completed "
            "or left partial results; do NOT blindly re-run the whole task or "
            "repeat steps that already succeeded.",
            "",
            f"Kiro Crew cancelled {tool_label} with a timeout error: it returned no "
            f"result and no progress for ~{idle_mins} minute(s). Treat it as failed. "
            "If you still need it, check whether it took effect, then retry it.",
            "",
            "Then continue the task from where you left off.",
        ]
    )


def build_timed_out_continuation(tool_title: str, idle_secs: int) -> str:
    """The whole prompt that hands a timed-out call back to the model."""
    return f"{TOOL_STALL_RECOVERY_PREFIX}\n{timed_out_call_body(tool_title, idle_secs)}"

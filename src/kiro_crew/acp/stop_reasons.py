"""The stop-reason vocabulary: what an ``EVENT_COMPLETE.stop_reason`` can say, and the
one mapping (:func:`classify_stop_reason`) every completion consumer runs it through.

A new reason or class goes in this module; :mod:`kiro_crew.acp.types` re-exports every
public name here, so callers keep importing them from there.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiro_crew.recovery.ladder import SESSION_RECOVERY_MAX_ATTEMPTS

# ── Stop Reasons ──

STOP_REASON_CANCELLED = "cancelled"
STOP_REASON_END_TURN = "end_turn"
# Model-side content refusal ("response declined by the model"). Non-retryable:
# retrying the same prompt hits the same refusal, so chat_runner surfaces an
# actionable message instead of churning the retry ladder.
STOP_REASON_REFUSAL = "refusal"
# The Kiro service's own spelling of a content-filter refusal, as it appears in
# the ``stopReason`` field of a ``_kiro.dev/metadata`` notification. It is
# NORMALISED to ``STOP_REASON_REFUSAL`` on the ``EVENT_COMPLETE`` that follows
# (see ``RefusalInfo``), so no consumer outside ``acp/`` ever compares against
# it; named here so the parser and its tests share one literal.
STOP_REASON_CONTENT_FILTERED_WIRE = "CONTENT_FILTERED"
# Signalled by the ACP layer when a genuinely-wedged (stale) turn was probed via
# session/cancel and got no ack within the grace window — a confirmed wedge, not
# a done-but-missing-frame turn (which acks and completes normally). The
# dashboard routes this to reset+resume+continue-nudge auto-recovery.
STOP_REASON_STALE_RECOVER = "stale_recover"
# Signalled by the session handle when the backend never finished the turn within
# the turn ceiling: no complete frame arrived, so the turn's text is cut off.
STOP_REASON_TIMEOUT = "timeout"
# Signalled by the per-session watchdog when an in-flight tool was judged dead
# / stuck / UNKNOWN-past-budget and the session was cancelled. Kept in the
# "error:" family so callers without a dedicated branch fall back to the
# generic error handling; chat_runner routes it to a dedicated recovery
# (continue-nudge, NOT a verbatim re-run of the original message).
STOP_REASON_TOOL_STALL = "error: tool stall"
# Signalled by the ACP layer when automatic compaction reported `failed`
# and the backend then abandoned the turn (no prompt response, no
# end_turn) past the post-failure budget. Kept in the "error:" family so
# callers without a dedicated branch fall back to generic error handling;
# it deliberately triggers NO retry — the user-visible compaction notice
# already explains what happened, and this only releases the slot.
STOP_REASON_COMPACTION_FAILED = "error: compaction failed"
# Signalled by the session handle when a session/cancel got no ack within the
# cancel grace window and the runtime is still up: the caller is unblocked with
# this completion and the runtime is kept for its other sessions. Kept in the
# "error:" family, so it classifies as ``failed``; to the caller that sent the
# cancel it is that cancel, left unanswered.
STOP_REASON_CANCEL_UNACKED = "error: cancel unacked"

# ── Stop-reason classes ──
#
# ``EVENT_COMPLETE`` only says a stream/turn ENDED; it is not proof the work
# succeeded. Every entry that consumes a completion (main chat, sub-agent run,
# nested child, task runner) maps the raw ``stop_reason`` onto ONE of these
# classes through :func:`classify_stop_reason`, so the terminal/wait state a
# task lands in cannot drift between entries. The classes are the task-level
# vocabulary of ``docs/system-specs/modules/subagent.md`` ("Stop reason →
# state"): ``succeeded`` is a normal end of turn; ``stalled`` and
# ``recovering`` are RECOVERABLE (bounded continue-nudge budget, then
# ``failed``); ``cancelled`` and ``failed`` are terminal.
STOP_CLASS_SUCCEEDED = "succeeded"
STOP_CLASS_STALLED = "stalled"
STOP_CLASS_RECOVERING = "recovering"
STOP_CLASS_CANCELLED = "cancelled"
STOP_CLASS_FAILED = "failed"

#: Bounded continue-nudge budget shared by every entry that recovers a
#: ``stalled`` / ``recovering`` completion. The main chat's
#: ``slot._tool_stall_retries`` and the sub-agent run's ``_stop_recovery_used``
#: both count against this same number, so a wedged turn is retried the same
#: number of times whichever surface it runs on. The number itself is the
#: recovery ladder's L3 in-place budget (``recovery.ladder``), the one place
#: every session-level retry count is defined; this name is its re-export for
#: the stop-reason consumers.
STOP_RECOVERY_MAX_RETRIES = SESSION_RECOVERY_MAX_ATTEMPTS

#: Prefix of the "error:" family (transport / process failures the ACP layer
#: synthesises a completion for). Kept as one literal so no consumer spells
#: ``startswith("error:")`` on its own.
_STOP_REASON_ERROR_PREFIX = "error:"


@dataclass(frozen=True)
class StopClass:
    """Classification of one ``EVENT_COMPLETE.stop_reason``.

    ``name`` is one of the ``STOP_CLASS_*`` constants. ``recoverable`` is True
    exactly for ``stalled`` and ``recovering``: the turn may be continued IN
    PLACE (a continue-nudge on the same session, never a verbatim re-run of
    the original task) within :data:`STOP_RECOVERY_MAX_RETRIES`, after which
    the run is ``failed``. ``retryable`` is True for the generic ``error:``
    family — an infrastructure failure the caller may re-queue the ORIGINAL
    message for (pipe death, process exit) — and False for a refusal, a
    non-transient compaction failure and an unknown reason, which repeat
    identically. ``known`` is False for a reason this table does not
    recognise; such a reason is still classified ``failed`` so it can never be
    mistaken for success, and callers may log it as unexpected.
    """

    name: str
    stop_reason: str
    recoverable: bool = False
    retryable: bool = False
    known: bool = True

    @property
    def is_success(self) -> bool:
        return self.name == STOP_CLASS_SUCCEEDED

    @property
    def is_terminal_failure(self) -> bool:
        return self.name == STOP_CLASS_FAILED


def classify_stop_reason(
    stop_reason: str | None,
    *,
    compaction_transient: bool = False,
) -> StopClass:
    """Map a raw ACP ``stop_reason`` onto its :class:`StopClass`.

    The single mapping every completion consumer uses (see the table in
    ``docs/system-specs/modules/subagent.md``):

    - ``end_turn`` / absent → ``succeeded``
    - ``error: tool stall`` → ``stalled`` (recoverable)
    - ``stale_recover`` → ``recovering`` (recoverable)
    - ``cancelled`` → ``cancelled``
    - ``error: compaction failed`` → ``failed``, or ``recovering`` when the
      ACP layer recorded the compaction failure as transient
      (*compaction_transient*)
    - ``refusal`` → ``failed`` (never retried: the same prompt refuses again)
    - any other ``error:*`` → ``failed`` (retryable: transport / process death)
    - anything else → ``failed``, ``known=False``

    An absent reason (``""``/``None``) is a normal end of turn: providers that
    never populate the field would otherwise fail every run.
    """
    reason = stop_reason or ""
    if reason in ("", STOP_REASON_END_TURN):
        return StopClass(STOP_CLASS_SUCCEEDED, reason)
    if reason == STOP_REASON_TOOL_STALL:
        return StopClass(STOP_CLASS_STALLED, reason, recoverable=True)
    if reason == STOP_REASON_STALE_RECOVER:
        return StopClass(STOP_CLASS_RECOVERING, reason, recoverable=True)
    if reason == STOP_REASON_CANCELLED:
        return StopClass(STOP_CLASS_CANCELLED, reason)
    if reason == STOP_REASON_COMPACTION_FAILED:
        if compaction_transient:
            return StopClass(STOP_CLASS_RECOVERING, reason, recoverable=True)
        return StopClass(STOP_CLASS_FAILED, reason)
    if reason == STOP_REASON_REFUSAL:
        return StopClass(STOP_CLASS_FAILED, reason)
    if reason.startswith(_STOP_REASON_ERROR_PREFIX):
        return StopClass(STOP_CLASS_FAILED, reason, retryable=True)
    return StopClass(STOP_CLASS_FAILED, reason, known=False)

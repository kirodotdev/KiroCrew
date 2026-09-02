"""Localized parsing for the second ACP backend's ``session/update`` frames.

Some of that backend's display/telemetry signals arrive in a different shape
than ``kiro-cli``'s. This module is the ONE place Crew reads that shape, so an
adjustment is a single-file edit; the handlers that act on a parsed frame live
in :mod:`kiro_crew.acp.session_handle`. The literals below are only what Crew
must match to route a frame — the backend's own internals are not documented
here.
"""

from __future__ import annotations

from typing import Any

# ── Envelope keys ──
# Where a frame's discriminant blob sits; both hops are guarded on read.
META_KEY = "_meta"
KIRO_KEY = "kiro"

# ── Frame discriminants Crew matches on ──
KIND_CONTEXT_USAGE = "context_usage"
KIND_TURN_COMPLETION = "turn_completion"
KIND_SUMMARIZATION_STARTED = "summarization_started"
KIND_SUMMARIZATION_COMPLETED = "summarization_completed"
KIND_SUMMARIZATION_FAILED = "summarization_failed"
KIND_STEERING_QUEUED = "steering_queued"
KIND_STEERING_INJECTED = "steering_injected"
KIND_STEERING_CLEARED = "steering_cleared"
KIND_AGENT_SUBTASK = "agent-subtask"  # hyphenated, not underscored

# Summarization maps to Crew's compaction status; steering to mid-turn steer.
SUMMARIZATION_KINDS = frozenset(
    {KIND_SUMMARIZATION_STARTED, KIND_SUMMARIZATION_COMPLETED, KIND_SUMMARIZATION_FAILED}
)
STEERING_KINDS = frozenset(
    {KIND_STEERING_QUEUED, KIND_STEERING_INJECTED, KIND_STEERING_CLEARED}
)

# ── Payload fields Crew reads ──
FIELD_KIND = "kind"
FIELD_USAGE_PERCENTAGE = "usagePercentage"
FIELD_CONVERSATION_SUMMARY = "conversationSummary"
FIELD_CONTENT = "content"
FIELD_PROMPT_TURN_SUMMARIES = "promptTurnSummaries"
FIELD_AGENT_SUBTASK_ID = "agentSubtaskId"
FIELD_PIPELINE = "pipeline"
FIELD_STAGES = "stages"
FIELD_UNIT = "unit"
FIELD_USAGE = "usage"
UNIT_CREDIT = "credit"


def kiro_meta(update: dict) -> dict[str, Any] | None:
    """Return the frame's discriminant blob, or ``None`` when absent.

    Both hops are ``isinstance``-guarded so a frame without it (a different
    backend, or a malformed one) falls through to the shared parser rather than
    raising. The single extraction step every handler shares — do not inline the
    two-step ``.get`` elsewhere.
    """
    meta = update.get(META_KEY)
    kiro = meta.get(KIRO_KEY) if isinstance(meta, dict) else None
    return kiro if isinstance(kiro, dict) else None


def turn_credits(kiro: dict) -> float | None:
    """Sum the per-turn credit cost from a ``turn_completion`` frame.

    Only ``credit``-unit entries contribute (the acp provider bills in credits).
    Returns the total to ASSIGN — the frame carries the whole turn's summary, so
    a replayed/duplicate frame reports the same total and must not accumulate —
    or ``None`` when there is no summaries list, so the caller leaves the prior
    value untouched rather than zeroing it.
    """
    summaries = kiro.get(FIELD_PROMPT_TURN_SUMMARIES)
    if not isinstance(summaries, list):
        return None
    # Deferred import: _token_count lives in the dispatch module, which imports
    # types; importing it at module scope would risk an import cycle once
    # session_handle imports this module.
    from kiro_crew.acp._dispatch import _token_count

    total = 0.0
    for entry in summaries:
        if not isinstance(entry, dict) or entry.get(FIELD_UNIT) != UNIT_CREDIT:
            continue
        value = _token_count(entry.get(FIELD_USAGE))
        if value is not None:
            total += float(value)
    return total

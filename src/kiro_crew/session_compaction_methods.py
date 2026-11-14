"""Pure helpers behind the configurable compaction method.

``session_compaction.CompactionCoordinator`` runs ``session.compaction_method``
when the auto-compact threshold fires. The rotation method (``soft``) is judged
before it runs by arithmetic over a few numbers, never I/O: how much of the
window the successor would start at (:func:`project_pct_after`) and whether that
clears the threshold (:func:`method_sufficient`). They live in one
dependency-light module so the coordinator stays a lifecycle module and the
surfaces that post the rotation's notices read the same names.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from kiro_crew.config.sections import (
    COMPACTION_METHOD_NATIVE,
    COMPACTION_METHOD_SOFT,
    COMPACTION_METHODS,
)

__all__ = [
    "COMPACTION_METHODS",
    "COMPACTION_METHOD_NATIVE",
    "COMPACTION_METHOD_SOFT",
    "OUTCOME_INSUFFICIENT",
    "OUTCOME_UNAVAILABLE",
    "ROTATION_HISTORY_FATE",
    "ROTATION_NOTICE_META_KEY",
    "estimate_tokens",
    "is_rotation_notice",
    "method_sufficient",
    "project_pct_after",
]

# Outcomes that hand the turn to ``native``. ``unavailable``: the rotation
# cannot run for this session (no window figure, no transcript on this surface).
# ``insufficient``: it could run but the projected usage stays inside the
# threshold band. A rotation that ran returns the coordinator's own result
# string instead, and ``native`` does not run.
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_INSUFFICIENT = "insufficient"

# What a rotation did with the history older than the tail the successor
# carries, by method, for the notice the surface posts after a compaction: a
# rotation and a native summary reach the user through the same callback, and
# a notice that reads the same for both hides that the pre-tail history is
# gone. Each value completes the dashboard notice's "earlier ones are ..."
# clause, which speaks of the agent's MEMORY: the transcript on screen is
# untouched by a rotation, and a bare "dropped" beside an intact transcript
# reads as a deletion. ``native`` has no entry: it keeps that history as an
# in-place summary and is not a rotation.
ROTATION_HISTORY_FATE: dict[str, str] = {
    COMPACTION_METHOD_SOFT: "gone from its memory",
}

#: ``meta`` key the dashboard sets, to the method's name, on every compaction
#: notice a rotation posts: the rotation itself, its wait for sub-agents, and a
#: Stop or failure inside it. Such a row is status for the person, not
#: conversation: counted by the successor's replay, it would spend the tail
#: budget the rotation's projection charged. The session replay leaves such rows
#: out while a rotation method is configured (:func:`is_rotation_notice`). A
#: notice for ``native`` carries no such key, so a transcript with no rotation
#: replays as before.
ROTATION_NOTICE_META_KEY = "rotation_method"


def is_rotation_notice(row: Mapping[str, Any]) -> bool:
    """Whether *row* is a compaction notice a rotation posted (see ``ROTATION_NOTICE_META_KEY``).

    The whole shape the dashboard writes is required (an ``assistant`` row with
    ``meta.kind`` ``"compaction"`` and the method key), not the key alone: a
    client can put arbitrary keys in the ``meta`` of the row it sends, and a key
    alone would let it hide its own message from the agent's replay.
    """
    meta = row.get("meta")
    return (
        row.get("role") == "assistant"
        and isinstance(meta, Mapping)
        and meta.get("kind") == "compaction"
        and bool(meta.get(ROTATION_NOTICE_META_KEY))
    )


#: Characters per token the estimates assume: the 4:1 ratio the replay budget
#: is sized with (80K chars for roughly 20K tokens at the reference window).
CHARS_PER_TOKEN = 4.0


def estimate_tokens(chars: int) -> int:
    """Token estimate for *chars* characters at ``CHARS_PER_TOKEN``, rounded up so a partial token counts."""
    if chars <= 0:
        return 0
    return math.ceil(chars / CHARS_PER_TOKEN)


def project_pct_after(
    *, window_tokens: int, carried_tokens: int, overhead_tokens: int = 0
) -> float | None:
    """Usage the successor starts at, as a percentage of *window_tokens*.

    *carried_tokens* is what the rotation seeds (the tail), *overhead_tokens*
    the fixed context every fresh session receives before any history. ``None``
    when the window is unknown (a provider that reports 0), so no rotation is
    judged on a figure that does not exist.
    """
    if window_tokens <= 0:
        return None
    used = max(0, carried_tokens) + max(0, overhead_tokens)
    return min(100.0, 100.0 * used / window_tokens)


def method_sufficient(
    pct_after: float | None, *, threshold_pct: float, min_effect_pct_points: float
) -> bool:
    """Whether a projected usage clears the threshold by the required margin.

    Mirrors the in-place verdict: a compaction landing within
    ``min_effect_pct_points`` under the threshold is judged ineffective there,
    so the same figure separates a rotation that runs from one that hands to
    ``native``. An unknown projection is never sufficient.
    """
    if pct_after is None:
        return False
    return pct_after <= threshold_pct - min_effect_pct_points

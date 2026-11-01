"""``kirocrew.turn.completed`` and its duration: the exportable turn pair.

``kirocrew.turn.duration`` (``metrics/turns.py``) answers turn latency and fault
rate locally, but it carries ``model`` and ``provider``, whose values are open, so
it cannot be exported. This pair counts the same turns with closed attributes
only:

* ``kirocrew.turn.completed`` -- counter, ``{turn}``: ``outcome``,
  ``trigger_source``, ``origin``, ``harness``.
* ``kirocrew.turn.completed.duration`` -- native histogram, ``ms``, on the turn
  buckets: ``outcome``, ``trigger_source``, ``harness``. ``origin`` is left off to
  keep its series bound small; the origin mix is read from the counter.

It is emitted from inside ``emit_turn_duration``, so the local and the exported
reading share one boundary and one outcome per turn.

Its own module, with its own recorder lookup, so the local turn family's tests
(which patch ``metrics.turns.get_recorder`` and count every call they see) keep
describing only that family.
"""

from __future__ import annotations

import logging

from kiro_crew.metrics.harness import ATTR_HARNESS, HARNESS_OTHER, HARNESS_UNKNOWN, HARNESS_VALUES
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.metrics.usage_attrs import (
    ATTR_ORIGIN,
    ATTR_TRIGGER_SOURCE,
    TRIGGER_OTHER,
    origin_of,
    trigger_source,
)

logger = logging.getLogger(__name__)

TURN_COMPLETED_METRIC = "kirocrew.turn.completed"
TURN_COMPLETED_DURATION_METRIC = "kirocrew.turn.completed.duration"

#: Every label ``metrics.turns.turn_outcome`` returns, plus the surface fallback
#: ``unclassified``. Spelled here because ``metrics.turns`` imports this module;
#: ``test_turn_trigger_source.py`` pins it against that function.
TURN_OUTCOME_VALUES: tuple[str, ...] = (
    "ok",
    "error",
    "timeout",
    "cancelled",
    "tool_stall",
    "stale_recover",
    "stall_exhausted",
    "unclassified",
)

_OUTCOME_SET = frozenset(TURN_OUTCOME_VALUES)


def emit_turn_completed(
    duration_ms: int | float | None,
    *,
    session_key: str,
    outcome: str,
    trigger: str = TRIGGER_OTHER,
    harness: str = HARNESS_UNKNOWN,
    origin: str | None = None,
) -> None:
    """Count one finished turn and record its duration (best-effort, never raises).

    The counter is written for every turn; the histogram only for a positive
    duration, because a recorded 0 reads as a measured instant turn.

    Every attribute is closed here rather than trusted from the caller:
    ``outcome`` onto :data:`TURN_OUTCOME_VALUES` (else ``unclassified``),
    ``trigger_source`` onto ``usage_attrs.TRIGGER_SOURCE_VALUES``, ``origin``
    onto ``usage_attrs.ORIGIN_VALUES`` and ``harness`` onto ``HARNESS_VALUES``
    (else ``other``). A missing trigger is ``other``, never ``user``.
    """
    try:
        attrs: dict[str, str | int | bool | float] = {
            "outcome": outcome if outcome in _OUTCOME_SET else "unclassified",
            ATTR_TRIGGER_SOURCE: trigger_source(trigger),
            ATTR_HARNESS: harness if harness in HARNESS_VALUES else HARNESS_OTHER,
        }
        recorder = get_recorder()
        recorder.counter(
            TURN_COMPLETED_METRIC,
            1,
            unit="{turn}",
            attrs={**attrs, ATTR_ORIGIN: origin_of(session_key, explicit=origin)},
        )
        if duration_ms and duration_ms > 0:
            recorder.histogram(TURN_COMPLETED_DURATION_METRIC, duration_ms, unit="ms", attrs=attrs)
    except Exception:
        logger.debug("turn completed metric emit failed", exc_info=True)

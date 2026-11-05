"""Safe bounds for the numbers the memory write and promote routes read from a body.

``POST /api/memory/promote`` hands ``min_count`` and ``min_sim`` straight to
``promote_episodic_patterns``, which TOMBSTONES every row of a cluster it
promotes. A ``min_sim`` at or below 0 puts nearly every row in one cluster,
and a ``min_count`` below 2 promotes clusters of one, so either value
retires rows nobody asked to fold. ``POST /api/memory/semantic`` stored
``confidence`` as given, while its readers expect ``0..1``.

Each helper returns a value the store can act on safely and logs a warning
whenever it replaced what the caller sent. A value inside the bounds is
returned unchanged. The warnings never echo the value: a body field can be a
400-digit integer.
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger("kiro_crew.dashboard.handlers.memory")

# The default ``promote_episodic_patterns`` and a bodyless promote POST use.
PROMOTE_MIN_SIM_DEFAULT = 0.75
# A cluster of one is every row: two is the smallest "repeated" pattern.
PROMOTE_MIN_COUNT_FLOOR = 2
# The confidence a semantic write without a usable one is stored at.
SEMANTIC_CONFIDENCE_DEFAULT = 1.0


def bounded_promote_min_count(value: int) -> int:
    """Raise a ``min_count`` below :data:`PROMOTE_MIN_COUNT_FLOOR` to the floor."""
    if value < PROMOTE_MIN_COUNT_FLOOR:
        logger.warning(
            "memory promote: min_count below %d; using %d",
            PROMOTE_MIN_COUNT_FLOOR,
            PROMOTE_MIN_COUNT_FLOOR,
        )
        return PROMOTE_MIN_COUNT_FLOOR
    return value


def bounded_promote_min_sim(value: float) -> float:
    """Replace a ``min_sim`` at or below 0 with :data:`PROMOTE_MIN_SIM_DEFAULT`.

    ``cluster_episodes`` merges a row when its similarity is ``> min_sim``, so
    a threshold at or below 0 (``-inf`` included) merges every row with a
    non-negative similarity; the default merges fewer. Every other value is
    returned unchanged: one above 1 (``inf`` included) or NaN merges no row,
    and the :data:`PROMOTE_MIN_COUNT_FLOOR` of 2 then promotes nothing.
    """
    if value <= 0:
        logger.warning("memory promote: min_sim not above 0; using %s", PROMOTE_MIN_SIM_DEFAULT)
        return PROMOTE_MIN_SIM_DEFAULT
    return value


def bounded_semantic_confidence(value: object) -> float:
    """The ``confidence`` a semantic write stores for the body's *value*.

    A number is clamped to ``0..1``. An integer is compared before it is
    converted, so one too large for a float clamps instead of raising
    ``OverflowError``. ``True``/``False`` keep the 1.0/0.0 they always stored.
    NaN and every non-number take :data:`SEMANTIC_CONFIDENCE_DEFAULT`, as a
    missing field does.
    """
    if value is None:
        return SEMANTIC_CONFIDENCE_DEFAULT
    if isinstance(value, bool):
        return float(value)
    if not isinstance(value, (int, float)) or (isinstance(value, float) and math.isnan(value)):
        logger.warning(
            "memory semantic write: confidence is not a number; using %s",
            SEMANTIC_CONFIDENCE_DEFAULT,
        )
        return SEMANTIC_CONFIDENCE_DEFAULT
    clamped = max(0, min(1, value))
    if clamped != value:
        logger.warning("memory semantic write: confidence outside 0..1; using %s", float(clamped))
    return float(clamped)

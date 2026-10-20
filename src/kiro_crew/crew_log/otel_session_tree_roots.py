"""The ``session.tree.roots`` observable gauge: how many roots of the session tree
have children, read off the folded tree each time the OTel SDK collects.

WHY AN OBSERVABLE GAUGE, NOT A COUNTER. "A depth-0 session that HAS child sessions"
is a property of the WHOLE session tree, not of any one session's own log: a root
gains a child when a DIFFERENT slot's ``session/opened`` cites it, and loses one when
a child is forgotten, so the quantity rises AND falls over time and is read off the
folded tree rather than tallied from an append stream. That is a gauge's shape -- a
current level -- not a monotonic counter's.

WHY A PLAIN READ EACH COLLECT, NO DIRTY FLAG OR CACHE. The SDK calls the gauge's
callback on its OWN periodic collection thread, never the crew-log writer thread, so
the O(n) count over the tree is already off the hot path. The tree is bounded by
:data:`~kiro_crew.crew_log.session_tree.TREE_UNIT_CAP` (4096 nodes), so folding it per
collect is a bounded, cheap read with no append-path cost -- a dirty-flag cache in
front of it would be optimizing a cost that was never measured and never on a hot
thread, so there is none. The callback just reads the current tree and counts.

WHAT THIS MODULE REGISTERS. One observable gauge, through
:func:`register_session_tree_gauge`, the same ``meter.create_observable_gauge`` path
:mod:`kiro_crew.metrics.inventory_gauges` and :mod:`kiro_crew.metrics.process_gauges`
use -- called once per live ``MeterProvider`` build from
:mod:`kiro_crew.metrics.provider`, under the same ``telemetry.enabled`` consent gate.
The code lives HERE and not in ``inventory_gauges`` because its source is crew_log's
session-tree projection, not the install inventory.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from opentelemetry.metrics import CallbackOptions, Meter, Observation

#: The metric name, spelled here the way each fold spells its own registry name, so a
#: reader imports one constant rather than a literal it could drift from. Still
#: ``...roots`` -- the issue's and the dev plan's name -- served by a gauge.
SESSION_TREE_ROOTS_METRIC = "kirocrew.crew.session.tree.roots"


def read_roots_with_children() -> "int | None":
    """How many roots of the current session tree have children, or ``None`` when the
    tree cannot be trusted. Run on the OTel SDK's collection thread; never raises.

    ``None`` -- "cannot tell this cycle" -- for an unseeded projection or an incomplete
    reading, which the gauge callback turns into NO observation (a gap in the series)
    rather than a fabricated ``0``. A seeded, complete tree with no branched roots is a
    real ``0``.

    Folds the tree once per call (O(nodes), bounded by
    :data:`~kiro_crew.crew_log.session_tree.TREE_UNIT_CAP`). No lock of its own, no
    cache: the projection's own read lock is brief and this runs off the writer thread.

    Imports function-locally, the boot-path posture the crew-log package keeps: this
    module stays importable without the session-tree surface until a collect asks.
    """
    try:
        from kiro_crew.crew_log.otel_session_tree_fold import count_roots_with_children
        from kiro_crew.crew_log.session_tree_projection import projection

        proj = projection()
        if not proj.seeded_for_current_store:
            # Not seeded for this store: the tree cannot be trusted, so report NOTHING
            # rather than a count folded from another store's or an empty state.
            return None
        reading = proj.reading()
        if reading.incomplete:
            # A reading missing edges makes a child look like a root; counting it would
            # over-report. Report nothing rather than a wrong number.
            return None
        return count_roots_with_children(reading.nodes)
    except Exception:  # pragma: no cover - telemetry must never raise into collect
        logger.debug("otel session-tree roots read failed; reporting no observation", exc_info=True)
        return None


def _roots_callback(_options: "CallbackOptions") -> "list[Observation]":
    """The gauge's observable callback: one untagged observation when the tree is
    readable, none when it is not. No attribute -- the metric is a level, not a
    breakdown. Called by the SDK on its collection thread.
    """
    from opentelemetry.metrics import Observation

    count = read_roots_with_children()
    if count is None:
        return []
    return [Observation(count, attributes={})]


def register_session_tree_gauge(meter: "Meter") -> None:
    """Register the roots observable gauge on *meter*.

    Called once per live ``MeterProvider`` build from
    :mod:`kiro_crew.metrics.provider`, the same path
    :func:`~kiro_crew.metrics.inventory_gauges.register_inventory_gauges` takes
    (instruments die with their provider, so a consent-driven rebuild re-registers on
    the new meter -- per-provider construction, not duplication). Best-effort: a
    failure loses this gauge, never telemetry as a whole, so the caller wraps it.
    """
    from kiro_crew.metrics.schema import validate_name

    validate_name(SESSION_TREE_ROOTS_METRIC)
    meter.create_observable_gauge(
        SESSION_TREE_ROOTS_METRIC,
        callbacks=[_roots_callback],
        unit="1",
        description="Session-tree roots (depth-0 sessions) that have at least one child",
    )

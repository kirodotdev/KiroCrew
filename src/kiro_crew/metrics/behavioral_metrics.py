"""``BEHAVIORAL_METRICS`` -- the product metrics that record a user action.

Most product metrics describe an install (inventory gauges) or the machinery
(turns, queues). A few record one thing a person did: a tip marked helpful, a
milestone reached. Those are exported under their own instrumentation scope,
:data:`BEHAVIORAL_SCOPE`, so a downstream analytics collector can admit user
behavior by scope alone, without a rule per metric and without taking the
inventory gauges that make up almost all of the ``kiro_crew`` scope.

Every point on that scope carries two closed attributes the collectors in this
family key on:

* ``Feature`` -- the Crew area, a constant per metric (``tips``, ``milestone``).
* ``Activity`` -- what happened: the value of one closed attribute the emit site
  already sets (a tip's ``action``), or, when the metric has none, the last
  segment of its name (``reached`` for ``kirocrew.milestone.reached``).

The closed target id, when the metric has one (a shipped doc name, a milestone
catalog key), moves to ``Entity``. Every other attribute is kept as it is.

The reshape only renames keys and adds constants, so it cannot introduce a value
the emit site did not already close. It is applied in ``MetricsRecorder`` on the
way in, so the emit sites and their tests are unchanged, and the local sink and
every OTLP destination see the same shape.

A name joins this set only in a reviewed change: it must already be in
``PRODUCT_METRICS`` and be a counter (``test_product_metrics.py``), and the
exported-metrics table in ``docs/guides/telemetry-otlp-export.md`` must list its
new shape.

Stdlib-only for the same reason as ``product_metrics``: the recorder imports it.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple, Union

#: The instrumentation scope behavioral metrics are exported under.
BEHAVIORAL_SCOPE = "kiro.crew"

#: name -> (``Feature``, attribute that becomes ``Activity``, attribute that
#: becomes ``Entity``). ``None`` for Activity means the last name segment; ``None``
#: for Entity means the metric has no target id.
_SHAPES: "Dict[str, Tuple[str, Optional[str], Optional[str]]]" = {
    "kirocrew.tips.feedback": ("tips", "action", "feature"),
    "kirocrew.feature_video.status": ("feature_video", "status", "feature"),
    "kirocrew.milestone.panel_opened": ("milestone", None, None),
    "kirocrew.milestone.starter_clicked": ("milestone", None, "milestone"),
    "kirocrew.milestone.reached": ("milestone", None, "milestone"),
    "kirocrew.milestone.skipped": ("milestone", None, "milestone"),
    "kirocrew.milestone.mark_all_done": ("milestone", None, None),
    "kirocrew.milestone.notice_shown": ("milestone", None, None),
    "kirocrew.milestone.features_completed": ("milestone", None, None),
}

#: The reviewed behavioral metrics: a subset of ``PRODUCT_METRICS``.
BEHAVIORAL_METRICS: "frozenset[str]" = frozenset(_SHAPES)

AttrValue = Union[str, int, bool, float]


def behavioral_attributes(name: str, attrs: "Dict[str, AttrValue]") -> "Dict[str, AttrValue]":
    """*attrs* of behavioral metric *name* with ``Feature`` / ``Activity`` / ``Entity`` set.

    Returns a new dict; *attrs* is not changed. A missing Activity attribute reads
    ``other``, the same fold every closed list uses.
    """
    feature, activity_key, entity_key = _SHAPES[name]
    out = dict(attrs)
    if activity_key is None:
        activity: AttrValue = name.rsplit(".", 1)[1]
    else:
        activity = out.pop(activity_key, "other")
    out["Feature"] = feature
    out["Activity"] = activity
    if entity_key is not None and entity_key in out:
        out["Entity"] = out.pop(entity_key)
    return out

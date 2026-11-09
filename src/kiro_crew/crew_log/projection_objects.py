"""``objects``: the newest ``object/observed`` record per subject and producer, slot-keyed.

``object/observed`` is the crew log's code-only record of something outside the session
(a pull request, say) as one named producer read it: the producer vocabulary is closed
and the emitter writes with ``src=gateway``, so no value here is text an agent typed.
This fold keeps, for each subject (``target``, the URL) and each producer, the last
facts read and when (``observed_at``), so the Ultra Mode Board draws a pull request's
state with its read time and no network read on page load.

Bounded: at most :data:`OBJECTS_TARGET_LIMIT` subjects, the least recently observed
evicted first; every cell is re-clamped because the bytes come off a file the reader
does not control, and facts wider than :data:`OBJECTS_FACTS_BYTES` are dropped whole.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, Final

from kiro_crew.crew_log.entry_types import OBJECT_PRODUCERS
from kiro_crew.crew_log.projection_values import _as_int, _rows_in
from kiro_crew.crew_log.schema import Entry

OBJECT_ENTRY_TYPE: Final[str] = "object/observed"
#: Subjects one slot keeps; a conductor names at most a few dozen pull requests.
OBJECTS_TARGET_LIMIT: Final[int] = 64
#: Bytes one producer's facts may hold, serialized; a pull request's are under 200.
OBJECTS_FACTS_BYTES: Final[int] = 2_048
OBJECTS_TEXT_LIMIT: Final[int] = 512
#: Characters of ``kind`` kept; the only kind written is ``github_pull_request``.
OBJECTS_KIND_LIMIT: Final[int] = 64


def _objects_start() -> dict[str, Any]:
    return {"latest": {}, "omitted": 0, "seen": 0}


def _facts_fit(value: Any) -> bool:
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        return False
    return size <= OBJECTS_FACTS_BYTES


def _objects_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != OBJECT_ENTRY_TYPE:
        return
    data = entry.data
    producer = data.get("producer")
    target = data.get("target")
    facts = data.get("facts")
    observed = data.get("observed_at")
    if (
        producer not in OBJECT_PRODUCERS
        or not isinstance(target, str)
        or not target
        or len(target) > OBJECTS_TEXT_LIMIT
        or not isinstance(facts, Mapping)
        or not _facts_fit(facts)
        or isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or not math.isfinite(observed)
    ):
        return
    latest: dict[str, Any] = state["latest"]
    row = latest.get(target)
    if row is None and len(latest) >= OBJECTS_TARGET_LIMIT:
        oldest = min(latest, key=lambda key: _as_int(latest[key].get("order")))
        del latest[oldest]
        state["omitted"] = _as_int(state.get("omitted")) + 1
    state["seen"] = _as_int(state.get("seen")) + 1
    by = dict(row.get("by") or {}) if isinstance(row, Mapping) else {}
    previous = by.get(producer)
    # A unit folded out of order never overwrites a newer read of the same producer.
    if isinstance(previous, Mapping) and float(previous.get("observed_at") or 0) > observed:
        return
    kind = data.get("kind")
    by[producer] = {
        "kind": kind[:OBJECTS_KIND_LIMIT] if isinstance(kind, str) else "",
        "facts": dict(facts),
        "observed_at": float(observed),
    }
    latest[target] = {"by": by, "order": state["seen"]}


def _objects_render(state: dict[str, Any]) -> dict[str, Any]:
    """``latest``: ``{target: {producer: {kind, facts, observed_at}}}``; ``omitted``
    counts subjects evicted at the bound."""
    latest: dict[str, Any] = state["latest"]
    return {
        "latest": {
            target: {p: dict(cell) for p, cell in (row.get("by") or {}).items()}
            for target, row in latest.items()
        },
        "omitted": _as_int(state.get("omitted")),
    }


def _objects_copy(state: dict[str, Any]) -> dict[str, Any]:
    return {
        **state,
        "latest": {
            target: {"by": dict(row.get("by") or {}), "order": row.get("order")}
            for target, row in state["latest"].items()
        },
    }


def _objects_rows(state: Mapping[str, Any]) -> int:
    """``objects``: one row per subject."""
    return _rows_in(state.get("latest"))

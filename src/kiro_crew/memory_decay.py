"""How strong each V1 experience is in recall today, and when it falls behind.

V1 recall scores an experience as ``cosine * (0.7 + 0.3 * importance) *
exp(-rate * days_old)`` (:func:`episodic_search.recall_score`). Its *strength* is
that score at a perfect match (:func:`episodic_search.recall_weight`): the part
the query does not decide. The memory page reads it from there, so the page and
recall agree.

The *fade line* is the score of a memory written today, at the lowest importance,
that just clears the relevance gate: ``_EPISODIC_RELEVANCE_THRESHOLD * 0.7``.
Below it, even an exact match ranks under any such new memory. Nothing else about
the row changes there: it is still stored and still admitted on a close match.

Only importance, the tag rate (``memory.decay_rates``) and the row's age move the
strength. A recall stamps ``last_accessed_at`` and an edit changes the text, and
neither enters the score. V2 stores do not decay (``memory_v2.rank_score``), so
they get no strength at all.
"""

from __future__ import annotations

import functools
import math
from datetime import datetime, timezone
from types import ModuleType
from typing import Any


def _search() -> ModuleType:
    """``episodic_search``, imported on first use. This module is on the dashboard's
    boot path (through ``memory_edit``), and importing recall's module there would
    load the stemmer and the embedder on every boot (``test_perf_boot_path``)."""
    from kiro_crew.vector_memory_runtime import episodic_search

    return episodic_search


@functools.cache
def fade_line() -> float:
    """A memory under this strength ranks below every new memory that clears the
    relevance gate, even on an exact match: recall's own score for a memory
    written today, at importance 0, matched at exactly the gate."""
    search = _search()
    return round(search.recall_score(search._EPISODIC_RELEVANCE_THRESHOLD, 0.0, 0.0, 0), 4)


@functools.cache
def chart_importance() -> float:
    """The importance the rail's per-rate sentences are about: recall's own default
    for a row with none recorded."""
    return _search().row_importance({"importance": None})


#: "Fading soon" means the row crosses the fade line within this many days. A
#: week: at the default rate an importance-0.5 row crosses on day 27, so a month
#: would call nearly every new row fading.
SOON_DAYS = 7
STRONG, FADING, FADED = "strong", "fading", "faded"
STATES = (STRONG, FADING, FADED)


def applies(store: Any) -> bool:
    """Whether the store's recall fades experiences with age (V1 only)."""
    return getattr(store, "algorithm_version", "v1") != "v2"


def days_to_line(start: float, rate: float, days_old: int) -> int | None:
    """Days from now until a row of strength *start* on its first day, fading at
    *rate*, is first below :func:`fade_line`; 0 when it already is, ``None`` when
    it never will be.

    Ages are whole days, as recall counts them, so the answer is the first whole
    age whose strength is under the line.
    """
    if start < fade_line():
        return 0
    if rate <= 0:
        return None
    reach = math.log(start / fade_line()) / rate
    # A subnormal rate passes the config clamp and overflows the division.
    if not math.isfinite(reach):
        return None
    crossing = math.floor(reach) + 1
    return max(0, crossing - days_old)


def state(until: int | None) -> str:
    """The strength band for a row that crosses the fade line in *until* days."""
    if until == 0:
        return FADED
    if until is not None and until <= SOON_DAYS:
        return FADING
    return STRONG


def record_decay(store: Any, row: Any, now: datetime) -> dict[str, Any]:
    """One experience's strength today and what sets it, for the detail panel."""
    rate, tag = _search().decay_rule_for(store, row["tags"])
    importance = _search().row_importance(row)
    days_old = _search().row_age_days(row, now)
    start = _search().recall_score(1.0, importance, rate, 0)
    until = days_to_line(start, rate, days_old)
    return {
        "strength": round(_search().recall_weight(store, row, now), 4),
        "start": round(start, 4),
        "rate": rate,
        "rate_tag": tag,
        "days_old": days_old,
        "days_to_line": until,
        "state": state(until),
    }


#: Where a record list summary keeps what its strength is computed from.
TERMS = "_decay"


def summary_terms(store: Any, row: Any) -> tuple[float, float, float | None, int | None]:
    """What a summary's strength is computed from, none of which moves with time:
    the importance, the rate, the written time (epoch seconds, ``None`` when
    unreadable, which counts as today) and the age at which it is under the line.

    The record list caches summaries until the store changes, but a row's age,
    and so its strength and band, moves on its own; :func:`refresh` recomputes
    those two from these terms on every listing.
    """
    rate, _tag = _search().decay_rule_for(store, row["tags"])
    importance = _search().row_importance(row)
    start = _search().recall_score(1.0, importance, rate, 0)
    try:
        written: datetime | None = datetime.fromisoformat(row["created_at"])
    except (TypeError, ValueError):
        written = None
    if written is not None and written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    return importance, rate, written.timestamp() if written else None, days_to_line(start, rate, 0)


def refresh(summaries: list[dict[str, Any]], now: datetime) -> int:
    """Set each summary's strength and band for *now*, from its :data:`TERMS`.

    Returns a digest of every band, so a cache of counts over these summaries
    can tell when a band moved although the store did not.
    """
    now_ts = now.timestamp()
    bands = bytearray()
    recall_score = _search().recall_score
    for summary in summaries:
        terms = summary.get(TERMS)
        if terms is None:
            continue
        importance, rate, written, crossing = terms
        # Whole days, as timedelta.days counts them in row_age_days.
        days_old = max(0, math.floor((now_ts - written) / 86400)) if written is not None else 0
        band = state(None if crossing is None else max(0, crossing - days_old))
        summary["strength"] = round(recall_score(1.0, importance, rate, days_old), 4)
        summary["facets"]["strength"] = band
        bands.append(STATES.index(band))
    return hash(bytes(bands))


def store_decay(store: Any) -> dict[str, Any]:
    """The store's fade rates for the page's chart: the default, then each tag's.

    Each rate carries the age at which a row of :func:`chart_importance` is
    first under the line (``None`` for never), from :func:`days_to_line`, so the
    page never works the crossing out itself.
    """
    start = round(_search().recall_score(1.0, chart_importance(), 0.0, 0), 4)
    pairs = [(None, store._decay_default), *sorted(store._decay_by_tag.items())]
    return {
        "fade_line": fade_line(),
        "importance": chart_importance(),
        "start": start,
        "rates": [
            {"tag": tag, "rate": rate, "days_to_line": days_to_line(start, rate, 0)}
            for tag, rate in pairs
        ],
    }

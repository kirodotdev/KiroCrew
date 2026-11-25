"""Facets, filters and sort orders for the memory page's record list.

``memory_edit._rows`` yields one light summary per matching record when asked;
this module reads the facets a summary carries, filters on them, counts them and
orders the matches, so a page of records can be sorted across the whole store
while only that page is assembled.

A facet is something the page can filter on: the lesson fields (category, applies,
repo scope), the write origin (source) and an experience's tags. Confidence is a
range, not a facet: facts and lessons carry it, experiences do not.
"""

from __future__ import annotations

import json
from typing import Any

from kiro_crew.lesson_validation import LESSON_APPLIES_UNSTATED, LESSON_APPLIES_VALUES
from kiro_crew.validation_parts.input_bounds import normalize_lesson_category

# The value a lesson's scope facet takes when its stored repo_scope is present but
# unusable ("meant to be scoped, but we cannot tell where"). An admissible repo scope
# never holds ":" (project_scope.scope_is_admissible), so no real scope can equal it.
SCOPE_UNUSABLE = ":unusable"
SOURCE_UNKNOWN = "unknown"
FACET_GROUPS = ("category", "applies", "scope", "source", "tag")
SORTS = ("updated", "created", "confidence", "importance", "key", "match")
_MAX_VALUES = 50
_MAX_VALUE_LENGTH = 512


class FacetError(ValueError):
    """A filter or sort the record list cannot use."""


def lesson_facets(value_json: Any) -> dict[str, str]:
    """The category, applies tier and scope a lesson row carries.

    Reads the same fields, with the same fallbacks, as the lesson list and the
    injection path: a legacy string row has the default category, no tier and no
    scope.
    """
    # Deferred: the dashboard's memory handler imports this module, and the lesson
    # tier pulls in vector_memory, which the dashboard import path must not load
    # (test_perf_boot_path::test_memory_handler_does_not_import_vector_memory).
    from kiro_crew.vector_memory_runtime.lessons import (
        _lesson_applies,
        _lesson_scope,
        _lesson_scope_unusable,
    )

    try:
        decoded = json.loads(value_json) if isinstance(value_json, str) else None
    except (ValueError, TypeError, RecursionError):
        decoded = None
    category = normalize_lesson_category(
        decoded.get("category") if isinstance(decoded, dict) else None, strict=False
    )
    if _lesson_scope_unusable(decoded):
        scope = SCOPE_UNUSABLE
    else:
        scope = _lesson_scope(decoded) or ""
    return {
        "category": category.strip().lower(),
        "applies": _lesson_applies(decoded),
        "scope": scope,
    }


def episode_tags(tags_json: Any) -> list[str]:
    """An experience's tags as a list of non-blank strings; anything else is no tags."""
    try:
        tags = json.loads(tags_json) if isinstance(tags_json, str) and tags_json else []
    except (ValueError, TypeError, RecursionError):
        return []
    if not isinstance(tags, list):
        return []
    # A writer can repeat a tag; one record still counts once per tag.
    return list(dict.fromkeys(tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()))


def summary_facets(kind: str, value_json: Any, tags_json: Any, source: Any) -> dict[str, Any]:
    facets: dict[str, Any] = {
        "category": None,
        "applies": None,
        "scope": None,
        "source": source.strip() if isinstance(source, str) and source.strip() else SOURCE_UNKNOWN,
        "tag": episode_tags(tags_json) if kind == "episode" else [],
    }
    if kind == "directive":
        facets.update(lesson_facets(value_json))
    return facets


def _values(raw: Any, group: str) -> list[str]:
    values = raw if isinstance(raw, list) else [raw]
    if len(values) > _MAX_VALUES or not all(
        isinstance(value, str) and len(value) <= _MAX_VALUE_LENGTH for value in values
    ):
        raise FacetError(f"Use at most {_MAX_VALUES} short values for {group}.")
    if group == "applies" and set(values) - set(LESSON_APPLIES_VALUES) - {LESSON_APPLIES_UNSTATED}:
        raise FacetError("Use always, on_topic or unstated for applies.")
    return list(dict.fromkeys(values))


def _bound(raw: Any, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise FacetError(f"{name} must be a number from 0 to 1.") from None
    if not 0 <= value <= 1:
        raise FacetError(f"{name} must be a number from 0 to 1.")
    return value


def parse_filters(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate the facet groups and the confidence range a caller sent.

    *raw* maps each name to a string or a list of strings (a repeated query
    parameter). An empty group is no filter. Values are compared exactly; the
    empty string is a real scope value (a lesson for every repo).
    """
    filters: dict[str, Any] = {}
    for group in FACET_GROUPS:
        if group in raw:
            values = _values(raw[group], group)
            if values:
                filters[group] = values
    low = _bound(raw["conf_min"], "conf_min") if "conf_min" in raw else None
    high = _bound(raw["conf_max"], "conf_max") if "conf_max" in raw else None
    if low is not None and high is not None and low > high:
        raise FacetError("conf_min must not be above conf_max.")
    if low is not None:
        filters["conf_min"] = low
    if high is not None:
        filters["conf_max"] = high
    return filters


def matches(summary: dict[str, Any], filters: dict[str, Any], *, skip: str | None = None) -> bool:
    """Whether a summary passes every filter group except *skip*."""
    facets = summary["facets"]
    for group in FACET_GROUPS:
        if group == skip or group not in filters:
            continue
        wanted = filters[group]
        if group == "tag":
            if not set(facets["tag"]) & set(wanted):
                return False
        elif facets[group] not in wanted:
            return False
    if "conf_min" in filters or "conf_max" in filters:
        confidence = summary["confidence"]
        if confidence is None:
            return False
        if confidence < filters.get("conf_min", 0) or confidence > filters.get("conf_max", 1):
            return False
    return True


def count_facets(
    summaries: list[dict[str, Any]], filters: dict[str, Any], kind: str
) -> dict[str, dict[str, int]]:
    """Counts per facet value, each group counted with every other filter applied.

    A group ignores its own filter, so ticking one category still shows how many
    rows the others would add. The kind counts ignore the kind, so each kind tab
    shows what it would list.
    """
    counts: dict[str, dict[str, int]] = {group: {} for group in ("kind", *FACET_GROUPS)}
    for summary in summaries:
        if matches(summary, filters):
            counts["kind"][summary["kind"]] = counts["kind"].get(summary["kind"], 0) + 1
        if kind != "all" and summary["kind"] != kind:
            continue
        for group in FACET_GROUPS:
            if not matches(summary, filters, skip=group):
                continue
            values = summary["facets"][group]
            for value in values if group == "tag" else [values]:
                if value is not None:
                    counts[group][value] = counts[group].get(value, 0) + 1
    return counts


def order(summaries: list[dict[str, Any]], sort: str, descending: bool) -> list[dict[str, Any]]:
    """Sort *summaries* in place by *sort*, ties broken by kind and id.

    Confidence and importance each belong to some kinds only. Rows with no value
    always go after the rows that have one, newest first, whichever way the sort
    runs, so reversing the order does not put every experience on top.
    """
    if sort not in SORTS:
        raise FacetError("Unknown sort.")
    field = {"updated": "updated_at", "created": "created_at"}.get(sort, sort)
    # A legacy row can have a NULL id; it sorts with the empty one.
    summaries.sort(key=lambda s: (s["kind"], s["id"] or ""))
    if sort in ("confidence", "importance"):
        scored = [s for s in summaries if s[field] is not None]
        unscored = [s for s in summaries if s[field] is None]
        unscored.sort(key=lambda s: s["updated_at"] or "", reverse=True)
        scored.sort(key=lambda s: s[field], reverse=descending)
        summaries[:] = scored + unscored
    else:
        summaries.sort(
            key=lambda s: s[field] or "" if field != "match" else s[field], reverse=descending
        )
    return summaries


def match_score(tokens: list[str], key: str, text: str) -> float:
    """How well a row matches the search words, from 0 to 1.

    A word found in the key counts twice as much as one found only in the text,
    and the whole phrase found in the text adds a bonus.
    shortcut: word counts only, no semantic score; upgrade by blending in the
    episodic vector search score once the page needs ranking by meaning.
    """
    if not tokens:
        return 0.0
    score = 0.0
    for token in tokens:
        if token in key:
            score += 2
        elif token in text:
            score += 1
    phrase = " ".join(tokens)
    if len(tokens) > 1 and phrase in text:
        score += 1
    return round(min(1.0, score / (2 * len(tokens) + (1 if len(tokens) > 1 else 0))), 2)

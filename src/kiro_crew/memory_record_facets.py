"""Facets, filters and sort orders for the memory page's record list.

``memory_edit._rows`` yields one light summary per matching record when asked;
this module reads the facets a summary carries, filters on them, counts them and
orders the matches, so a page of records can be sorted across the whole store
while only that page is assembled.

A facet is something the page can filter on: the lesson fields (category, applies,
repo scope), the write origin (source), the channel (surface) and crew a row came
from, and an experience's tags. Surface and crew are stamped on the crew lineage
only; a Global V1 row has neither, so it counts toward no value of them. Strength
(``memory_decay``) is a V1 experience's band: strong, fading or faded. Confidence is a
range, not a facet: facts and lessons carry it, experiences do not.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Callable

from kiro_crew.lesson_validation import LESSON_APPLIES_UNSTATED, LESSON_APPLIES_VALUES
from kiro_crew.validation_parts.input_bounds import normalize_lesson_category

# The value a lesson's scope facet takes when its stored repo_scope is present but
# unusable ("meant to be scoped, but we cannot tell where"). An admissible repo scope
# never holds ":" (project_scope.scope_is_admissible), so no real scope can equal it.
SCOPE_UNUSABLE = ":unusable"
SOURCE_UNKNOWN = "unknown"
FACET_GROUPS = ("category", "applies", "scope", "source", "surface", "crew", "tag", "strength")
SORTS = ("updated", "created", "confidence", "importance", "strength", "key", "match")
# A date range filters on one of these times: the list's sort field, updated unless sorted by created.
DATE_FIELDS = ("updated", "created")
_MAX_VALUES = 50
# Records named one by one: at most this many refs. A graph box selection of any
# size travels as a ``box`` filter instead.
_MAX_REFS = 500
#: The kinds a ``box_kinds`` filter may name: the memory graph's node kinds.
BOX_KINDS = ("episode", "fact", "lesson")
#: Where :func:`resolve_box` puts the ids inside the box, for :func:`matches`.
BOX_IDS = "_box_ids"
_REF = re.compile(r"[0-9a-f]{10}")
_MAX_VALUE_LENGTH = 512


def record_ref(kind: str, record_id: Any) -> str:
    """A short, URL-safe name for one record: ten hex digits of ``sha256("kind:id")``.

    A graph selection names its records by these, so hundreds of them fit in one
    query string (``GET /api/memory/records?ref=…``).
    """
    return hashlib.sha256(f"{kind}:{record_id or ''}".encode()).hexdigest()[:10]


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


def source_group(source: Any) -> str:
    """The source facet value for a row's write origin.

    An origin written as ``<prefix>:<id>`` (``consolidation:dashboard:<session>``,
    one per chat) counts under its prefix, so the filter lists one "Learned from
    chat" value instead of one per session. The record itself keeps the full source.
    """
    if not isinstance(source, str) or not source.strip():
        return SOURCE_UNKNOWN
    prefix, colon, _ = source.strip().partition(":")
    return prefix if colon and prefix else source.strip()


def summary_facets(
    kind: str,
    value_json: Any,
    tags_json: Any,
    source: Any,
    *,
    surface: str | None = None,
    crew: str | None = None,
    strength: str | None = None,
) -> dict[str, Any]:
    """The facet values one row carries; ``None`` is no value for that group."""
    facets: dict[str, Any] = {
        "category": None,
        "applies": None,
        "scope": None,
        "source": source_group(source),
        "surface": surface,
        "crew": crew,
        "tag": episode_tags(tags_json) if kind == "episode" else [],
        # A V1 experience's band (memory_decay.STATES); nothing on other rows.
        "strength": strength,
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
    if "ref" in raw:
        refs = raw["ref"] if isinstance(raw["ref"], list) else [raw["ref"]]
        if len(refs) > _MAX_REFS or not all(isinstance(r, str) and _REF.fullmatch(r) for r in refs):
            raise FacetError(f"Use at most {_MAX_REFS} record refs of ten hex digits.")
        if refs:
            filters["ref"] = list(dict.fromkeys(refs))
    if "box" in raw:
        filters["box"] = parse_box(raw["box"])
        layout = raw.get("box_layout")
        if not isinstance(layout, str) or not layout.strip() or len(layout) > _MAX_VALUE_LENGTH:
            raise FacetError("A box needs the layout it was drawn on (box_layout).")
        filters["box_layout"] = layout
    if "box_kinds" in raw:
        kinds = raw["box_kinds"] if isinstance(raw["box_kinds"], list) else [raw["box_kinds"]]
        if not kinds or not all(k in BOX_KINDS for k in kinds):
            raise FacetError("box_kinds names episode, fact or lesson.")
        if "box" not in filters:
            raise FacetError("box_kinds needs a box.")
        filters["box_kinds"] = sorted(set(kinds))
    low = _bound(raw["conf_min"], "conf_min") if "conf_min" in raw else None
    high = _bound(raw["conf_max"], "conf_max") if "conf_max" in raw else None
    if low is not None and high is not None and low > high:
        raise FacetError("conf_min must not be above conf_max.")
    if low is not None:
        filters["conf_min"] = low
    if high is not None:
        filters["conf_max"] = high
    since = _instant(raw["since"], "since") if raw.get("since") else None
    until = _instant(raw["until"], "until") if raw.get("until") else None
    if since and until and since >= until:
        raise FacetError("since must be before until.")
    if since:
        filters["since"] = since.isoformat()
    if until:
        filters["until"] = until.isoformat()
    date_field = raw.get("date_field", "updated")
    if date_field not in DATE_FIELDS:
        raise FacetError("date_field must be updated or created.")
    if (since or until) and date_field != "updated":
        filters["date_field"] = date_field
    return filters


def _instant(raw: Any, name: str) -> datetime:
    if not isinstance(raw, str):
        raise FacetError(f"{name} must be an ISO date or time.")
    parsed = utc_time(raw)
    if parsed is None:
        raise FacetError(f"{name} must be an ISO date or time.")
    return parsed


def utc_time(raw: Any) -> datetime | None:
    """A stored or sent ISO time as an aware UTC datetime; a naive one is UTC."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _in_range(summary: dict[str, Any], filters: dict[str, Any]) -> bool:
    field = "created_at" if filters.get("date_field") == "created" else "updated_at"
    when = utc_time(summary.get(field))
    if when is None:
        return False
    if "since" in filters and when < datetime.fromisoformat(filters["since"]):
        return False
    return not ("until" in filters and when >= datetime.fromisoformat(filters["until"]))


def parse_box(raw: Any) -> list[float]:
    """A memory graph box, ``"x0,y0,x1,y1"`` in layout units, as four floats."""
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    try:
        parts = [float(p) for p in str(raw).split(",")] if isinstance(raw, str) else None
    except ValueError:
        parts = None
    if not parts or len(parts) != 4 or not all(math.isfinite(p) for p in parts):
        raise FacetError("A box is four numbers: x0,y0,x1,y1.")
    return parts


def resolve_box(
    filters: dict[str, Any], ids_in_box: Callable[[list[float], list[str]], Any]
) -> dict:
    """*filters* with a ``box`` resolved, once, into the ids the graph places
    inside it (:data:`BOX_IDS`); unchanged without a box.

    *ids_in_box* answers ``(box, kinds)`` from the store's graph layout. A summary's
    ``id`` is the graph's id for it: an experience's id, a fact's or lesson's key.
    """
    if "box" not in filters:
        return filters
    return {**filters, BOX_IDS: frozenset(ids_in_box(filters["box"], filters.get("box_kinds", [])))}


def matches(summary: dict[str, Any], filters: dict[str, Any], *, skip: str | None = None) -> bool:
    """Whether a summary passes every filter group except *skip*.

    A ``box`` filter must have gone through :func:`resolve_box` first; an
    unresolved box matches nothing, so a box can never widen a selection.
    """
    if "box" in filters and summary.get("id") not in filters.get(BOX_IDS, ()):
        return False
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
    if "ref" in filters and summary.get("ref") not in filters["ref"]:
        return False
    if "conf_min" in filters or "conf_max" in filters:
        confidence = summary["confidence"]
        if confidence is None:
            return False
        if confidence < filters.get("conf_min", 0) or confidence > filters.get("conf_max", 1):
            return False
    if ("since" in filters or "until" in filters) and not _in_range(summary, filters):
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

    Confidence, importance and strength each belong to some rows only. Rows with no value
    always go after the rows that have one, newest first, whichever way the sort
    runs, so reversing the order does not put every experience on top.
    """
    if sort not in SORTS:
        raise FacetError("Unknown sort.")
    field = {"updated": "updated_at", "created": "created_at"}.get(sort, sort)
    # A legacy row can have a NULL id; it sorts with the empty one.
    summaries.sort(key=lambda s: (s["kind"], s["id"] or ""))
    if sort in ("confidence", "importance", "strength"):
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

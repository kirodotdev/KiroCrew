"""The memory page's Audit tab: the ``memory_events`` table, filtered and counted.

Every row is one write the store recorded or refused (``_log_event``). The page
filters by event, kind and source, searches the key and both values, limits the
time range, sorts by time and pages; the counts per value and the total come
from SQL over the whole table with every other filter applied, so they are exact
whatever has been loaded. Kind is derived as the record list derives it: an
``episodic`` row is an experience, a ``lesson.*`` key a lesson, any other a fact.
Source counts ``<prefix>:<id>`` origins under their prefix
(``memory_record_facets.source_group``). Rows are never deleted here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kiro_crew.memory_record_facets import SOURCE_UNKNOWN

GROUPS = ("event", "kind", "source")
KINDS = ("fact", "directive", "episode")
_MAX_VALUES = 50
_MAX_QUERY = 200

_KIND_SQL = (
    "CASE WHEN memory_type = 'episodic' THEN 'episode'"
    " WHEN memory_key LIKE 'lesson.%' THEN 'directive' ELSE 'fact' END"
)
_SOURCE_SQL = (
    "CASE WHEN trim(coalesce(source, '')) = '' THEN '" + SOURCE_UNKNOWN + "'"
    " WHEN instr(trim(source), ':') > 1 THEN substr(trim(source), 1, instr(trim(source), ':') - 1)"
    " ELSE trim(source) END"
)
_COLUMN = {"event": "event_type", "kind": _KIND_SQL, "source": _SOURCE_SQL}


class AuditError(ValueError):
    """A filter the Audit tab cannot use."""


def _values(raw: Any, group: str) -> list[str]:
    values = raw if isinstance(raw, list) else [raw]
    if len(values) > _MAX_VALUES or not all(
        isinstance(v, str) and 0 < len(v) <= 128 for v in values
    ):
        raise AuditError(f"Use at most {_MAX_VALUES} short values for {group}.")
    if group == "kind" and set(values) - set(KINDS):
        raise AuditError("Use fact, directive or episode for kind.")
    return list(dict.fromkeys(values))


def _instant(raw: Any, name: str) -> str:
    if not isinstance(raw, str):
        raise AuditError(f"{name} must be an ISO date or time.")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise AuditError(f"{name} must be an ISO date or time.") from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    # Stored times are UTC ISO strings ("…+00:00"); compare on the naive prefix.
    return parsed.isoformat()


def parse_query(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate the filters a caller sent; each group maps to a list of values."""
    query: dict[str, Any] = {"filters": {}}
    for group in GROUPS:
        if group in raw:
            values = _values(raw[group], group)
            if values:
                query["filters"][group] = values
    q = raw.get("q", "")
    if not isinstance(q, str) or len(q) > _MAX_QUERY:
        raise AuditError(f"Search text must be at most {_MAX_QUERY} characters.")
    query["q"] = q.strip()
    query["since"] = _instant(raw["since"], "since") if raw.get("since") else None
    query["until"] = _instant(raw["until"], "until") if raw.get("until") else None
    if query["since"] and query["until"] and query["since"] >= query["until"]:
        raise AuditError("since must be before until.")
    order = raw.get("order", "desc")
    if order not in ("asc", "desc"):
        raise AuditError("Order must be asc or desc.")
    query["order"] = order
    return query


def _where(query: dict[str, Any], skip: str | None = None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for group, values in query["filters"].items():
        if group == skip:
            continue
        clauses.append(f"({_COLUMN[group]}) IN ({','.join('?' for _ in values)})")
        params.extend(values)
    if query["q"]:
        like = "%" + query["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append(
            "(memory_key LIKE ? ESCAPE '\\' OR coalesce(old_value, '') LIKE ? ESCAPE '\\'"
            " OR coalesce(new_value, '') LIKE ? ESCAPE '\\')"
        )
        params.extend([like, like, like])
    # created_at is "YYYY-MM-DDTHH:MM:SS…+00:00": its first 26 characters compare as UTC.
    if query["since"]:
        clauses.append("substr(created_at, 1, 26) >= ?")
        params.append(query["since"])
    if query["until"]:
        clauses.append("substr(created_at, 1, 26) < ?")
        params.append(query["until"])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def list_events(
    store: Any, raw: dict[str, Any], *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """One page of events, newest first unless ``order=asc``, with exact counts.

    ``total`` counts every event the filters match; ``facets`` gives, per group,
    how many events each value would list with the other groups' filters applied.
    """
    if isinstance(limit, bool) or isinstance(offset, bool) or not 1 <= limit <= 200 or offset < 0:
        raise AuditError("Use a page size of 1–200 and a non-negative offset.")
    query = parse_query(raw)
    where, params = _where(query)
    direction = "ASC" if query["order"] == "asc" else "DESC"
    columns = f"id, event_type, memory_type, memory_key, old_value, new_value, source, created_at, {_KIND_SQL} AS kind"
    with store._db_lock:
        rows = store.db.execute(
            f"SELECT {columns} FROM memory_events{where} ORDER BY created_at {direction}, id {direction} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        total = store.db.execute(f"SELECT COUNT(*) FROM memory_events{where}", params).fetchone()[0]
        facets: dict[str, dict[str, int]] = {}
        for group in GROUPS:
            group_where, group_params = _where(query, skip=group)
            facets[group] = {
                str(value): count
                for value, count in store.db.execute(
                    f"SELECT {_COLUMN[group]} AS v, COUNT(*) FROM memory_events{group_where} GROUP BY v",
                    group_params,
                ).fetchall()
            }
    return {
        "events": [dict(row) for row in rows],
        "total": total,
        "has_more": total > offset + limit,
        "facets": facets,
    }

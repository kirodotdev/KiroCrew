"""Optional kind and time filters for on-demand ``memory_recall``.

A caller asking "what did we decide in October" or "what was true on 3 March"
needs the record kind and the time a record was written or valid, data the store
already keeps (``created_at``/``updated_at`` on every row, ``status``/``valid_from``/
``valid_until`` in ``memory_record_meta``). This module only parses and applies
that data; it adds no storage.

Meanings, all in UTC and half-open:

* ``kind``: ``fact``, ``episode`` or ``lesson``. Only that part of the answer is returned.
* ``since`` / ``until``: the content returned was written in ``[since, until)``: an
  episode when it was recorded, a fact when its current value was set (``updated_at``;
  an upsert keeps a fact's ``created_at``).
* ``as_of``: the record was valid at that instant, as
  :func:`memory_record_metadata.eligible` judges it, the same check recall applies at
  the current time. A record without ``valid_from`` counts from when its content was
  written, so a fact whose value was set after ``as_of`` is not returned for it. A
  fact whose window has since closed is found by a date inside it. A fact that was
  replaced or forgotten is removed from the store and cannot be found; its history
  stays in the revision log.

Lessons are standing rules, not dated events, so time filters beside
``kind=lesson`` are dropped, and an unfiltered kind leaves them out of a timed query.

A malformed or conflicting filter never turns a recall into a refusal. It is
dropped, the recall runs as it would without it, and ``parse_filters`` returns a
one-line warning for the caller to show. Valid filters beside it still apply.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from kiro_crew import memory_record_metadata as record_meta

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

KINDS = ("fact", "episode", "lesson")
_NAMES = ("kind", "since", "until", "as_of")
_AS_OF: contextvars.ContextVar[datetime | None] = contextvars.ContextVar(
    "recall_as_of", default=None
)


def parse_instant(value: Any, name: str) -> datetime:
    """An ISO date or timestamp as an aware UTC datetime; a bare date is midnight UTC.

    Read by the store's own timestamp rule (``memory_record_metadata._timestamp``), the
    one validity windows are written with.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an ISO date or timestamp")
    return datetime.fromisoformat(record_meta._timestamp(value.strip(), name))


def _stored_instant(value: Any) -> datetime | None:
    """A stored timestamp as an aware UTC datetime, or None when absent or unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_instant(value, "stored")
    except ValueError:
        return None


@dataclass(frozen=True)
class RecallFilters:
    kind: str = ""
    since: datetime | None = None
    until: datetime | None = None
    as_of: datetime | None = None

    @property
    def timed(self) -> bool:
        return any(point is not None for point in (self.since, self.until, self.as_of))

    def wants(self, kind: str) -> bool:
        """Whether the answer includes *kind* (lessons never ride along a timed query)."""
        if kind == "lesson" and self.timed:
            return False
        return self.kind in ("", kind)

    @property
    def wants_notebook(self) -> bool:
        """Notebook pages are undated facts: they answer an untimed fact or unkinded query."""
        return not self.timed and self.wants("fact")

    def describe(self) -> dict[str, str]:
        shown = {"kind": self.kind}
        for name in ("since", "until", "as_of"):
            point = getattr(self, name)
            if point is not None:
                shown[name] = point.isoformat()
        return {key: value for key, value in shown.items() if value}


def parse_filters(raw: Mapping[str, Any]) -> tuple[RecallFilters | None, str]:
    """Filters from tool arguments or query parameters, and a warning for what was dropped.

    The filters are None when none were given or none survived. A filter that is
    malformed, or conflicts with another, is left out and named in the warning
    (empty when nothing was dropped); the rest still apply.
    """
    given = {name: raw.get(name) for name in _NAMES if raw.get(name) not in (None, "")}
    if not given:
        return None, ""
    dropped: list[str] = []
    kind = given.get("kind", "")
    if not isinstance(kind, str) or (kind and kind not in KINDS):
        dropped.append("kind ignored: it must be one of " + ", ".join(KINDS))
        kind = ""
    points: dict[str, datetime] = {}
    for name in _NAMES[1:]:
        if name not in given:
            continue
        try:
            points[name] = parse_instant(given[name], name)
        except ValueError:
            dropped.append(f"{name} ignored: it must be an ISO date or timestamp")
    since, until = points.get("since"), points.get("until")
    if since and until and since >= until:
        del points["since"], points["until"]
        dropped.append("since and until ignored: since must be earlier than until")
    if kind == "lesson" and points:
        dropped.append(
            ", ".join(points) + " ignored: time filters apply to facts and episodes, not lessons"
        )
        points = {}
    filters = RecallFilters(kind=kind, **points)
    warning = "Warning: " + "; ".join(dropped) if dropped else ""
    return (filters if kind or points else None), warning


def as_of_now() -> datetime | None:
    """The instant the running recall is evaluating validity at, if any."""
    return _AS_OF.get()


@contextlib.contextmanager
def as_of_scope(filters: RecallFilters | None) -> Iterator[None]:
    """Make this thread's eligibility checks answer for ``filters.as_of`` instead of now."""
    token = _AS_OF.set(filters.as_of if filters is not None else None)
    try:
        yield
    finally:
        _AS_OF.reset(token)


def _written_at(store: VectorMemoryStore, kind: str, row: dict) -> datetime | None:
    """When the content of a ranked row was written: a fact's ``updated_at``, an episode's
    ``created_at``. An upsert moves a fact's ``updated_at`` and keeps its ``created_at``,
    and nothing rewrites an episode, so this dates the content being returned."""
    column = "updated_at" if kind == "fact" else "created_at"
    written = _stored_instant(row.get(column))
    if written is not None:
        return written
    relation, key, identity = (
        (store._epi_rel, "id", row.get("id"))
        if kind == "episode"
        else (store._sem_rel, "key", row.get("key"))
    )
    with store._db_lock:
        found = store.db.execute(
            f"SELECT {column} FROM {relation} WHERE {key} = ?", (identity,)
        ).fetchone()
    return _stored_instant(found[0]) if found else None


def narrow(
    store: VectorMemoryStore, rows: list[dict], kind: str, filters: RecallFilters
) -> list[dict]:
    """The *rows* of a ranked candidate list that satisfy the time filters, in rank order."""
    if not filters.timed:
        return rows
    kept = []
    for row in rows:
        written = _written_at(store, kind, row)
        if written is None:
            continue
        if filters.since is not None and written < filters.since:
            continue
        if filters.until is not None and written >= filters.until:
            continue
        if filters.as_of is not None:
            identity = row["id"] if kind == "episode" else row["key"]
            with store._db_lock:
                metadata = record_meta.get_record_metadata(
                    store.db, record_meta.record_id_for(kind, identity)
                )
            if not record_meta.eligible(metadata, now=filters.as_of.isoformat()):
                continue
            if not metadata.get("valid_from") and written > filters.as_of:
                continue
        kept.append(row)
    return kept

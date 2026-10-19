"""One readable timeline per task, merged from the streams that already record it.

The history of one task is spread over three stores that share only an id: the
``task_events`` rows of the durable queue, the ``subagent/*`` entries of the
spawning session's crew log, and the run events of the workflow a
workflow-agent row belongs to. :func:`render_timeline` reads each through its
owner's existing read surface, merges the three into one chronological list and
renders it as markdown.

It is a read-only view. It stores nothing, writes nothing back into any of the
three streams, and adds no reader of its own to the crew log or the workflow
store: the crew-log side goes through ``unit_holding_child``, the ``subagents``
fold and ``read_page``; the workflow side goes through the run registry.

Scrubbing belongs to the caller and is not optional. ``scrub`` is a required
argument with no default, so there is no path that renders unscrubbed text. It
is applied to every key and leaf of an event's data, as the text it holds,
before anything encodes it (:func:`_leaf_copy`), to the text of every value and
label before it is cut (a credential is matched whole, never as the prefix a
cut leaves), and to every rendered line, which also covers the labels a hostile
or damaged store could put in a ``kind`` or a ``type``. The dashboard route
passes the same helper it scrubs its other task payloads with.

A stream that cannot be read is reported, not dropped: each stream carries a
:class:`StreamReport` whose status is ``ok``, ``absent``, ``truncated`` or
``unavailable`` with a closed reason, and the markdown ends with the same
table. An empty timeline therefore never reads as "nothing happened" when a
stream was simply unreadable.

Public surface: :func:`render_timeline`, :func:`events_from_registry`,
:func:`read_child_entries`, :class:`Timeline`, :class:`StreamReport`,
:class:`Sources`, :class:`CrewRead`, the stream, status and reason names, and
the bounds below.

Specification: ``docs/system-specs/modules/taskq.md`` (Task timeline).
"""

from __future__ import annotations

import json
import logging
import math
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final

from kiro_crew.taskq import model as taskq_model
from kiro_crew.taskq.adapters.runner import OWNER_WORKFLOW, owner_of

if TYPE_CHECKING:
    from kiro_crew.taskq.store import TaskStore

logger = logging.getLogger(__name__)

__all__ = [
    "CREW_ENTRIES_MAX",
    "CREW_SCAN_MAX",
    "REASON_KEPT_LIMIT",
    "REASON_NOT_APPLICABLE",
    "REASON_NO_RECORD",
    "REASON_NO_SOURCE",
    "REASON_READ_FAILED",
    "REASON_SCAN_LIMIT",
    "REASON_TAIL_LIMIT",
    "STATUS_ABSENT",
    "STATUS_OK",
    "STATUS_TRUNCATED",
    "STATUS_UNAVAILABLE",
    "STREAMS",
    "STREAM_CREW_LOG",
    "STREAM_TASK_EVENTS",
    "STREAM_WORKFLOW",
    "VALUE_MAX_CHARS",
    "WORKFLOW_EVENTS_MAX",
    "CrewRead",
    "Sources",
    "StreamReport",
    "Timeline",
    "events_from_registry",
    "read_child_entries",
    "render_timeline",
]

STREAM_TASK_EVENTS: Final = "task_events"
STREAM_CREW_LOG: Final = "crew_log"
STREAM_WORKFLOW: Final = "workflow"
#: The streams in tie-break order: two entries stamped in the same instant list
#: the queue's own record first, then the session log's, then the workflow's.
STREAMS: Final = (STREAM_TASK_EVENTS, STREAM_CREW_LOG, STREAM_WORKFLOW)
_RANK: Final = {name: index for index, name in enumerate(STREAMS)}

STATUS_OK: Final = "ok"
#: Nothing is recorded in this stream for the task, or the stream does not
#: apply to this kind of row.
STATUS_ABSENT: Final = "absent"
#: Entries exist that this view does not show; the reason names the bound.
STATUS_TRUNCATED: Final = "truncated"
#: The stream could not be read; what it holds is unknown, not empty.
STATUS_UNAVAILABLE: Final = "unavailable"

REASON_NOT_APPLICABLE: Final = "not_applicable"
REASON_NO_RECORD: Final = "no_record"
REASON_READ_FAILED: Final = "read_failed"
REASON_NO_SOURCE: Final = "no_source"
REASON_TAIL_LIMIT: Final = "tail_limit"
REASON_SCAN_LIMIT: Final = "scan_limit"
REASON_KEPT_LIMIT: Final = "kept_limit"

#: Entries of the spawning session's log read from the child's dispatch onward.
#: The read is one ``read_page`` call, so memory is bounded by this and the walk
#: to the end of the unit is paid once.
CREW_SCAN_MAX: Final = 5000
#: Crew-log entries kept for one child (the newest when more exist).
CREW_ENTRIES_MAX: Final = 200
#: Workflow run events kept for one run (the newest when more exist).
WORKFLOW_EVENTS_MAX: Final = 200
#: Characters kept of one rendered value, so one event cannot dominate a page.
VALUE_MAX_CHARS: Final = 300

_ELLIPSIS: Final = "..."


@dataclass(frozen=True)
class CrewRead:
    """What a crew-log reader found for one child.

    ``found`` is False when no unit of the session records the child's
    dispatch (an absence, not a failure). ``complete`` is False when the scan
    stopped at :data:`CREW_SCAN_MAX` before the end of the unit.
    """

    rows: tuple[Mapping[str, Any], ...] = ()
    found: bool = False
    complete: bool = True


@dataclass(frozen=True)
class Sources:
    """The reads this view needs from outside the task store.

    ``slot_of`` maps a row's session key to the slot key the crew log files its
    session under. ``workflow_events`` answers one run id with that run's
    events oldest first, each the run's own event object or its JSON form, or
    ``None`` for a run it does not know; ``None`` here means the gateway has no
    workflow service.
    """

    slot_of: Callable[[str], str]
    workflow_events: Callable[[str], Iterable[Any] | None] | None = None


@dataclass(frozen=True)
class StreamReport:
    """How one stream fared: its status, entry count and, when not plain ok, why."""

    stream: str
    status: str
    count: int = 0
    reason: str = ""
    #: Entries read but not placed because their timestamp was unreadable.
    dropped: int = 0


@dataclass(frozen=True)
class Timeline:
    """The rendered view of one task."""

    task_id: str
    markdown: str
    entries: int
    streams: tuple[StreamReport, ...] = field(default_factory=tuple)

    def public(self) -> dict[str, Any]:
        """The route's body; the stream reports reach it as the markdown's ``## Streams`` list."""
        return {"task_id": self.task_id, "markdown": self.markdown, "entries": self.entries}


@dataclass(frozen=True)
class _Item:
    ts: float
    stream: str
    order: int
    label: str
    detail: str


# ── value rendering ──────────────────────────────────────────────────────────


def _one_line(text: str, scrub: Callable[[Any], Any]) -> str:
    """*text* scrubbed whole, then put on one line and cut at :data:`VALUE_MAX_CHARS`.

    The scrubber runs before the cut, because cutting first could leave the
    prefix of a credential that no pattern recognises. Every value and label
    is cut here, so none is cut unscrubbed.
    """
    flat = " ".join(str(scrub(text)).split())
    if len(flat) > VALUE_MAX_CHARS:
        return flat[: VALUE_MAX_CHARS - len(_ELLIPSIS)] + _ELLIPSIS
    return flat


def _leaf_copy(value: Any, scrub: Callable[[Any], Any]) -> Any:
    """A copy of *value* in which every key and leaf is already scrubbed text.

    Every encoding step escapes a newline -- ``str`` and ``repr`` of bytes, JSON
    of any string -- and a credential pattern that spans lines does not
    match the escaped form. So each leaf is scrubbed here as the text it
    holds, before anything encodes it: a string as it is, bytes decoded as
    UTF-8, any other object through ``str``, and a mapping key the same way as
    a leaf. Numbers, booleans and ``None`` are kept, since no pattern matches
    them. The copy has only string keys and JSON scalars, so the encoders that
    run on it later cannot fail or fall back to raw text.
    """
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, str):
        cleaned = scrub(value)
        return cleaned if isinstance(cleaned, str) else str(cleaned)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(_leaf_copy(key, scrub)): _leaf_copy(item, scrub) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_leaf_copy(item, scrub) for item in value]
    return _leaf_copy(str(value), scrub)


def _value(value: Any, scrub: Callable[[Any], Any]) -> str:
    """One value on one scrubbed line, encoded only after :func:`_leaf_copy`."""
    cleaned = _leaf_copy(value, scrub)
    if isinstance(cleaned, str):
        return _one_line(cleaned, scrub)
    return _one_line(json.dumps(cleaned, sort_keys=True, separators=(",", ":")), scrub)


def _describe(
    data: Mapping[str, Any],
    scrub: Callable[[Any], Any],
    *,
    skip: frozenset[str] = frozenset(),
) -> str:
    """``from -> to`` when the data carries a transition, then ``key=value`` pairs."""
    head = ""
    omit = set(skip)
    if "from" in data and "to" in data:
        head = f"{_value(data['from'], scrub)} -> {_value(data['to'], scrub)}"
        omit |= {"from", "to"}
    pairs = [
        f"{_value(key, scrub)}={_value(val, scrub)}"
        for key, val in sorted(data.items(), key=lambda kv: str(kv[0]))
        if key not in omit
    ]
    return " ".join(part for part in (head, " ".join(pairs)) if part)


def _stamp(ts: float) -> str:
    """UTC instant to the millisecond, the same spelling for every stream."""
    moment = datetime.fromtimestamp(ts, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _seconds(value: Any, *, per_second: float = 1.0) -> float | None:
    """A stored time as epoch seconds :func:`_stamp` can spell, else ``None``.

    A damaged or hostile store can hold any number that parses: ``inf``,
    ``nan``, or an instant outside the years the clock can spell. Each would
    raise when its line is stamped and fail the whole view, so the caller
    counts it as an unreadable time instead.
    """
    try:
        seconds = float(value) / per_second
        if not math.isfinite(seconds):
            return None
        _stamp(seconds)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return seconds


def _order(value: Any) -> int:
    """A stream's own order for one entry; 0 when it does not read as an integer."""
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _mapping(scrub: Callable[[Any], Any], data: Any) -> dict[str, Any]:
    """*data* scrubbed key by key and leaf by leaf ({} when it is not a mapping)."""
    if not isinstance(data, Mapping):
        return {}
    cleaned = scrub(_leaf_copy(data, scrub))
    return dict(cleaned) if isinstance(cleaned, Mapping) else {}


def _text(scrub: Callable[[Any], Any], value: Any) -> str:
    return _value("" if value is None else value, scrub)


def _newest(
    entries: Iterable[Any], time_of: Callable[[Any], float | None], bound: int
) -> tuple[list[tuple[float, Any]], int, int]:
    """The newest *bound* entries whose time reads, with how many read and how many did not.

    One pass over a stream's entries, parsing only the time, into a buffer of
    *bound* slots. An entry that falls off the front of the buffer and an entry
    whose time does not read are each counted and never kept, so only what
    comes back is converted or rendered: a run or a log with thousands of
    entries costs this view its bound, not its length.
    """
    kept: deque[tuple[float, Any]] = deque(maxlen=bound)
    readable = dropped = 0
    for entry in entries:
        ts = time_of(entry)
        if ts is None:
            dropped += 1
            continue
        readable += 1
        kept.append((ts, entry))
    return list(kept), readable, dropped


# ── the three streams ────────────────────────────────────────────────────────


def _task_event_items(
    events: Sequence[Any], scrub: Callable[[Any], Any]
) -> tuple[list[_Item], int]:
    items: list[_Item] = []
    dropped = 0
    for event in events:
        ts = _seconds(getattr(event, "ts", 0.0) or 0.0)
        if ts is None:
            dropped += 1
            continue
        data = _mapping(scrub, getattr(event, "data", None))
        items.append(
            _Item(
                ts=ts,
                stream=STREAM_TASK_EVENTS,
                order=_order(getattr(event, "seq", 0)),
                label=_text(scrub, getattr(event, "kind", "")),
                detail=_describe(data, scrub),
            )
        )
    return items, dropped


def read_child_entries(slot_key: str, agent_id: str) -> CrewRead:
    """The ``subagent/*`` entries naming *agent_id* in the session that dispatched it.

    Blocking; the caller runs it off the loop. The unit is found by the crew
    log's own search over the slot's units, the child's dispatch position comes
    from the ``subagents`` fold, and the entries come from ``read_page`` over a
    window that starts there, because nothing about a child precedes its
    dispatch. A store that cannot answer raises (``UnitSearchFailed`` or the
    fold's own error); that is the caller's ``unavailable``, not an absence.
    """
    from kiro_crew.crew_log.projection import fold_session
    from kiro_crew.crew_log.read import read_page
    from kiro_crew.crew_log.resolve import unit_holding_child

    unit = unit_holding_child(slot_key, agent_id)
    if not unit:
        return CrewRead()
    folded = fold_session(unit, names=("subagents",)).projection("subagents").value
    row = (folded.get("by_id") or {}).get(agent_id)
    seq = row.get("seq_spawned") if isinstance(row, Mapping) else None
    start = seq if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0 else 1
    page = read_page(unit, start, start + CREW_SCAN_MAX - 1)
    rows = tuple(
        entry
        for entry in page.get("entries") or ()
        if isinstance(entry, Mapping)
        and str(entry.get("type") or "").startswith("subagent/")
        and isinstance(entry.get("data"), Mapping)
        and entry["data"].get("agent_id") == agent_id
    )
    return CrewRead(rows=rows, found=True, complete=page.get("next_from") is None)


def _crew_stream(
    record: Any, sources: Sources, scrub: Callable[[Any], Any]
) -> tuple[list[_Item], StreamReport]:
    if str(getattr(record, "kind", "")) != taskq_model.KIND_SUBAGENT:
        return [], StreamReport(STREAM_CREW_LOG, STATUS_ABSENT, reason=REASON_NOT_APPLICABLE)
    task_id = str(record.id)
    try:
        found = read_child_entries(
            sources.slot_of(str(getattr(record, "session_key", "") or "")), task_id
        )
    except Exception:
        logger.warning("task timeline: crew log read failed for %s", task_id, exc_info=True)
        return [], StreamReport(STREAM_CREW_LOG, STATUS_UNAVAILABLE, reason=REASON_READ_FAILED)
    if not found.found:
        return [], StreamReport(STREAM_CREW_LOG, STATUS_ABSENT, reason=REASON_NO_RECORD)
    kept, readable, dropped = _newest(found.rows, _crew_time, CREW_ENTRIES_MAX)
    items = [
        _Item(
            ts=ts,
            stream=STREAM_CREW_LOG,
            order=_order(row.get("seq")),
            label=_text(scrub, row.get("type")),
            detail=_describe(_mapping(scrub, row.get("data")), scrub, skip=frozenset({"agent_id"})),
        )
        for ts, row in kept
    ]
    status, reason = STATUS_OK if items else STATUS_ABSENT, "" if items else REASON_NO_RECORD
    if readable > CREW_ENTRIES_MAX:
        status, reason = STATUS_TRUNCATED, REASON_KEPT_LIMIT
    elif not found.complete:
        status, reason = STATUS_TRUNCATED, REASON_SCAN_LIMIT
    return items, StreamReport(STREAM_CREW_LOG, status, len(items), reason, dropped)


def _crew_time(row: Mapping[str, Any]) -> float | None:
    """A crew-log entry's ``time`` (epoch milliseconds) as seconds, else ``None``."""
    millis = row.get("time")
    if not isinstance(millis, (int, float)) or isinstance(millis, bool):
        return None
    return _seconds(millis, per_second=1000.0)


def _iso_seconds(text: Any) -> float | None:
    """An ISO 8601 stamp as epoch seconds; a naive one is read as UTC."""
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _workflow_stream(
    task_id: str, sources: Sources, scrub: Callable[[Any], Any]
) -> tuple[list[_Item], StreamReport]:
    owner = owner_of(task_id)
    if owner is None or owner[0] != OWNER_WORKFLOW:
        return [], StreamReport(STREAM_WORKFLOW, STATUS_ABSENT, reason=REASON_NOT_APPLICABLE)
    if sources.workflow_events is None:
        return [], StreamReport(STREAM_WORKFLOW, STATUS_UNAVAILABLE, reason=REASON_NO_SOURCE)
    try:
        events = sources.workflow_events(owner[1])
    except Exception:
        logger.warning("task timeline: workflow read failed for %s", task_id, exc_info=True)
        return [], StreamReport(STREAM_WORKFLOW, STATUS_UNAVAILABLE, reason=REASON_READ_FAILED)
    if events is None:
        return [], StreamReport(STREAM_WORKFLOW, STATUS_ABSENT, reason=REASON_NO_RECORD)
    kept, readable, dropped = _newest(events, _workflow_time, WORKFLOW_EVENTS_MAX)
    items = [
        _Item(
            ts=ts,
            stream=STREAM_WORKFLOW,
            order=_order(_field(event, "seq")),
            label=_text(scrub, _field(event, "type")),
            detail=_describe(_mapping(scrub, _field(event, "data")), scrub),
        )
        for ts, event in kept
    ]
    status, reason = STATUS_OK if items else STATUS_ABSENT, "" if items else REASON_NO_RECORD
    if readable > WORKFLOW_EVENTS_MAX:
        status, reason = STATUS_TRUNCATED, REASON_KEPT_LIMIT
    return items, StreamReport(STREAM_WORKFLOW, status, len(items), reason, dropped)


def _field(event: Any, name: str) -> Any:
    """One field of a workflow event, from the run's own event object or its JSON form."""
    if isinstance(event, Mapping):
        return event.get(name)
    return getattr(event, name, None)


def _workflow_time(event: Any) -> float | None:
    parsed = _iso_seconds(_field(event, "ts"))
    return None if parsed is None else _seconds(parsed)


def events_from_registry(registry: Any) -> Callable[[str], Iterable[Any] | None]:
    """A ``Sources.workflow_events`` reader over a workflow run registry.

    The registry's ``get`` answers a run handle (live or restored from the
    persisted run JSON) whose ``events`` are the same objects the run detail
    serves. They are handed over as they are, oldest first: nothing is copied
    or converted here, because the stream keeps only its newest
    :data:`WORKFLOW_EVENTS_MAX` of them before it reads any event's data.
    """

    def _events(run_id: str) -> Iterable[Any] | None:
        handle = registry.get(run_id)
        if handle is None:
            return None
        return getattr(handle, "events", None) or ()

    return _events


# ── the view ─────────────────────────────────────────────────────────────────


def _merge(*streams: list[_Item]) -> list[_Item]:
    """Every item in time order; ties fall to the stream order, then the stream's own order."""
    merged = [item for stream in streams for item in stream]
    merged.sort(key=lambda item: (item.ts, _RANK[item.stream], item.order))
    return merged


def render_timeline(
    task_id: str,
    *,
    store: TaskStore,
    scrub: Callable[[Any], Any],
    events_limit: int,
    sources: Sources,
) -> Timeline | None:
    """The merged timeline of *task_id*, or ``None`` when the store has no such row.

    Blocking (SQLite and file reads); the caller runs it off the loop.
    *events_limit* is the tail of ``task_events`` read, the same bound the task
    detail route serves. A stream that fails is reported on the result and does
    not fail the others; the store read itself is the one error that
    propagates, because without the row there is no timeline to qualify.
    """
    record = store.get(task_id)
    if record is None:
        return None
    # One row past the bound tells a tail that stopped at the bound from a task
    # whose history is exactly the bound long: only the former is truncated.
    probe = list(store.events(task_id, limit=events_limit + 1))
    truncated = len(probe) > events_limit
    events = probe[-events_limit:] if truncated else probe
    queue_items, queue_dropped = _task_event_items(events, scrub)
    queue_status, queue_reason = (
        (STATUS_OK, "") if queue_items else (STATUS_ABSENT, REASON_NO_RECORD)
    )
    if truncated:
        queue_status, queue_reason = STATUS_TRUNCATED, REASON_TAIL_LIMIT
    reports = [
        StreamReport(
            STREAM_TASK_EVENTS, queue_status, len(queue_items), queue_reason, queue_dropped
        )
    ]
    crew_items, crew_report = _crew_stream(record, sources, scrub)
    workflow_items, workflow_report = _workflow_stream(task_id, sources, scrub)
    reports += [crew_report, workflow_report]
    merged = _merge(queue_items, crew_items, workflow_items)

    lines = [
        f"# Timeline: {task_id}",
        "",
        f"- kind: {getattr(record, 'kind', '')}, state: {getattr(record, 'state', '')}, "
        f"attempts: {getattr(record, 'attempts', 0)}, generation: {getattr(record, 'generation', 0)}",
        "",
    ]
    if merged:
        for item in merged:
            tail = f": {item.detail}" if item.detail else ""
            lines.append(f"- {_stamp(item.ts)} [{item.stream}] {item.label}{tail}")
    else:
        lines.append("No entries are recorded for this task.")
    lines += ["", "## Streams", ""]
    for report in reports:
        note = f" ({report.reason})" if report.reason else ""
        lost = f", {report.dropped} without a readable time" if report.dropped else ""
        lines.append(f"- {report.stream}: {report.status}{note}, {report.count} entries{lost}")
    markdown = "\n".join(str(scrub(line)) for line in lines) + "\n"
    return Timeline(task_id=task_id, markdown=markdown, entries=len(merged), streams=tuple(reports))

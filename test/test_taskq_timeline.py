"""Task timeline: one chronological, scrubbed view of a task from three existing streams.

``taskq.timeline.render_timeline`` merges a task's ``task_events``, the
``subagent/*`` entries of the session that dispatched it and, for a workflow
agent row, the workflow run's events. ``GET /api/tasks/{id}/timeline`` serves it
behind the owner gate. The view is read-only and adds no storage, no reader of
the crew log's file and no scrubber of its own.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import source_corpus
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from overload_fakes import Clock, open_task_store
from overload_fakes import task_record as _rec

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import store as crew_store
from kiro_crew.dashboard import handlers as handlers_pkg
from kiro_crew.dashboard import session_health
from kiro_crew.dashboard.handlers import tasks as tasks_mod
from kiro_crew.taskq import model
from kiro_crew.taskq import timeline as tl
from kiro_crew.taskq.store import TaskStore
from kiro_crew.workflows import WorkflowEvent

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_taskq_timeline")

GATEWAY = "gateway"
SECRET = "AKIAIOSFODNN7EXAMPLE"
_KEY_WORDS = "PRIVATE " + "KEY"  # assembled so no key-block literal sits in the source
PEM = (
    f"-----BEGIN RSA {_KEY_WORDS}-----\n"
    "MIIEowIBAAKCAQEAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
    f"-----END RSA {_KEY_WORDS}-----"
)
TRUNCATED_PEM = PEM.split("\n-----END", 1)[0]


@pytest.fixture
def clock() -> Clock:
    return Clock(5000.0)


@pytest.fixture
def store(tmp_path: Path, clock: Clock):
    yield from open_task_store(tmp_path, clock)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test writes into its own data home, never the live one."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


_PATCH: pytest.MonkeyPatch | None = None


@pytest.fixture(autouse=True)
def _crew_reader_patch(_floor_monkeypatch: pytest.MonkeyPatch):
    """Lets :func:`_sources` stand a fake in for ``read_child_entries`` for one test."""
    global _PATCH
    _PATCH = _floor_monkeypatch
    yield
    _PATCH = None


@pytest.fixture(autouse=True)
def _fresh_monitor(_floor_monkeypatch: pytest.MonkeyPatch):
    """A private health monitor, as the tasks API tests use."""
    mon = session_health.SessionHealthMonitor(include_log_scan=False)
    _floor_monkeypatch.setattr(session_health, "_default_monitor", mon)
    yield mon


# ── helpers ──────────────────────────────────────────────────────────────────


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _run_row(
    store: TaskStore, clock: Clock, task_id: str, *, kind: str = model.KIND_SUBAGENT, **kw: Any
) -> None:
    """Accept, claim and start *task_id*; events land at t, t, t+1, t+2."""
    store.accept([_rec(task_id, kind=kind, session_key=kw.pop("session_key", "chat-1"), **kw)])
    gen = store.claim(task_id, owner="o1").generation
    clock.advance(1.0)
    assert store.transition(task_id, model.STARTING, generation=gen)
    clock.advance(1.0)
    assert store.transition(task_id, model.RUNNING, generation=gen)


def _sources(crew_reader: Any = None, **kw: Any) -> tl.Sources:
    """Sources for one render; *crew_reader*, when given, replaces ``read_child_entries``."""
    if crew_reader is not None:
        assert _PATCH is not None
        _PATCH.setattr(tl, "read_child_entries", crew_reader)
    return tl.Sources(slot_of=lambda key: key, **kw)


def _render(store: Any, task_id: str, **kw: Any) -> tl.Timeline | None:
    return tl.render_timeline(
        task_id,
        store=store,
        scrub=kw.pop("scrub", tasks_mod._scrub_value),
        events_limit=kw.pop("events_limit", 200),
        sources=kw.pop("sources", None) or _sources(crew_reader=lambda _slot, _id: tl.CrewRead()),
    )


def _entries(markdown: str) -> list[tuple[str, str]]:
    """``(stream, label)`` for every timeline line, in the order printed."""
    out: list[tuple[str, str]] = []
    for line in markdown.splitlines():
        if line.startswith("- 19") or line.startswith("- 20"):
            _stamp, stream, label = line[2:].split(" ", 2)
            out.append((stream.strip("[]"), label.split(":", 1)[0].split(" ", 1)[0]))
    return out


def _crew_row(
    seq: int, seconds: float, kind: str = "subagent/steered", **data: Any
) -> dict[str, Any]:
    return {
        "type": kind,
        "seq": seq,
        "time": int(seconds * 1000),
        "src": GATEWAY,
        "data": {"agent_id": "s1", **data},
    }


def _wf_event(seq: int, seconds: float, kind: str = "log", **data: Any) -> dict[str, Any]:
    return {"run_id": "wf_1", "seq": seq, "ts": _iso(seconds), "type": kind, "data": data}


class _FakeStore:
    """The two reads the renderer makes, over rows a real store would not hold."""

    def __init__(self, record: Any, events: list[model.TaskEvent]) -> None:
        self._record = record
        self._events = events

    def get(self, task_id: str) -> Any:
        return self._record if task_id == self._record.id else None

    def events(self, task_id: str, *, limit: int = 200) -> list[model.TaskEvent]:
        return list(self._events)[-limit:]


def _record(task_id: str = "s1", kind: str = model.KIND_SUBAGENT) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id, kind=kind, state="running", attempts=1, generation=1, session_key="chat-1"
    )


def _event(seq: int, seconds: float, kind: str = "transition", **data: Any) -> model.TaskEvent:
    return model.TaskEvent(task_id="s1", seq=seq, ts=seconds, kind=kind, data=data)


# ── the merged view ──────────────────────────────────────────────────────────


def test_an_unknown_id_renders_nothing(store: TaskStore) -> None:
    assert _render(store, "missing") is None


def test_a_queue_only_timeline_has_a_fixed_shape(store: TaskStore, clock: Clock) -> None:
    _run_row(store, clock, "s1")
    out = _render(store, "s1")
    assert out is not None
    assert out.markdown == (
        "# Timeline: s1\n"
        "\n"
        "- kind: subagent, state: running, attempts: 1, generation: 1\n"
        "\n"
        "- 1970-01-01T01:23:20.000Z [task_events] accepted: kind=subagent\n"
        "- 1970-01-01T01:23:20.000Z [task_events] claimed: generation=1 owner=o1\n"
        "- 1970-01-01T01:23:21.000Z [task_events] transition: admitted -> starting\n"
        "- 1970-01-01T01:23:22.000Z [task_events] transition: starting -> running\n"
        "\n"
        "## Streams\n"
        "\n"
        "- task_events: ok, 4 entries\n"
        "- crew_log: absent (no_record), 0 entries\n"
        "- workflow: absent (not_applicable), 0 entries\n"
    )
    assert out.entries == 4
    assert out.public() == {"task_id": "s1", "markdown": out.markdown, "entries": 4}


def test_a_subagent_timeline_interleaves_the_queue_and_the_session_log(
    store: TaskStore, clock: Clock
) -> None:
    _run_row(store, clock, "s1")
    crew = tl.CrewRead(
        rows=(
            _crew_row(10, 5000.5, "subagent/spawned", task="do it"),
            _crew_row(11, 5002.5, "subagent/completed", ms=42),
        ),
        found=True,
    )
    out = _render(store, "s1", sources=_sources(crew_reader=lambda _slot, _id: crew))
    assert out is not None
    assert _entries(out.markdown) == [
        ("task_events", "accepted"),
        ("task_events", "claimed"),
        ("crew_log", "subagent/spawned"),
        ("task_events", "transition"),
        ("task_events", "transition"),
        ("crew_log", "subagent/completed"),
    ]
    assert "[crew_log] subagent/spawned: task=do it" in out.markdown
    assert "[crew_log] subagent/completed: ms=42" in out.markdown


def test_a_workflow_agent_timeline_interleaves_the_run_events(
    store: TaskStore, clock: Clock
) -> None:
    _run_row(store, clock, "workflow:wf_1:agent1", kind=model.KIND_WORKFLOW_AGENT)
    events = [
        _wf_event(1, 4999.0, "run_started", name="demo"),
        _wf_event(2, 5001.5, "agent_started", agent_id="a0", call_index=0),
        _wf_event(3, 5003.0, "agent_finished", agent_id="a0", ok=True),
    ]
    asked: list[str] = []

    def _events(run_id: str) -> list[dict[str, Any]]:
        asked.append(run_id)
        return events

    out = _render(
        store,
        "workflow:wf_1:agent1",
        sources=_sources(workflow_events=_events),
    )
    assert out is not None
    assert asked == ["wf_1"]
    assert _entries(out.markdown) == [
        ("workflow", "run_started"),
        ("task_events", "accepted"),
        ("task_events", "claimed"),
        ("task_events", "transition"),
        ("workflow", "agent_started"),
        ("task_events", "transition"),
        ("workflow", "agent_finished"),
    ]
    assert "- crew_log: absent (not_applicable), 0 entries" in out.markdown


def test_the_merge_orders_by_time_then_stream_then_the_streams_own_order() -> None:
    items = [
        tl._Item(5.0, tl.STREAM_WORKFLOW, 1, "w", ""),
        tl._Item(5.0, tl.STREAM_CREW_LOG, 2, "c2", ""),
        tl._Item(5.0, tl.STREAM_CREW_LOG, 1, "c1", ""),
        tl._Item(5.0, tl.STREAM_TASK_EVENTS, 9, "q", ""),
        tl._Item(1.0, tl.STREAM_WORKFLOW, 7, "early", ""),
    ]
    merged = tl._merge(items[:2], items[2:4], items[4:])
    assert [item.label for item in merged] == ["early", "q", "c1", "c2", "w"]


def test_a_concatenation_of_the_streams_is_not_a_timeline() -> None:
    queue = [
        tl._Item(1.0, tl.STREAM_TASK_EVENTS, 1, "q1", ""),
        tl._Item(9.0, tl.STREAM_TASK_EVENTS, 2, "q2", ""),
    ]
    crew = [tl._Item(5.0, tl.STREAM_CREW_LOG, 1, "c1", "")]
    assert [item.label for item in tl._merge(queue, crew)] == ["q1", "c1", "q2"]


def test_a_row_that_is_neither_kind_has_both_other_streams_absent(
    store: TaskStore, clock: Clock
) -> None:
    _run_row(store, clock, "step1", kind=model.KIND_TASKRUNNER_STEP)
    out = _render(store, "step1")
    assert out is not None
    reports = {r.stream: (r.status, r.reason) for r in out.streams}
    assert reports[tl.STREAM_CREW_LOG] == (tl.STATUS_ABSENT, tl.REASON_NOT_APPLICABLE)
    assert reports[tl.STREAM_WORKFLOW] == (tl.STATUS_ABSENT, tl.REASON_NOT_APPLICABLE)


# ── a stream that cannot be read is reported, not dropped ───────────────────


def test_a_failing_crew_log_is_reported_and_the_queue_still_renders(
    store: TaskStore, clock: Clock
) -> None:
    _run_row(store, clock, "s1")

    def _boom(_slot: str, _id: str) -> tl.CrewRead:
        raise RuntimeError("log store would not say")

    out = _render(store, "s1", sources=_sources(crew_reader=_boom))
    assert out is not None
    assert len(_entries(out.markdown)) == 4
    assert "- crew_log: unavailable (read_failed), 0 entries" in out.markdown
    assert [r.status for r in out.streams] == ["ok", "unavailable", "absent"]


@pytest.mark.parametrize(
    ("sources", "status", "reason"),
    [
        (_sources(), tl.STATUS_UNAVAILABLE, tl.REASON_NO_SOURCE),
        (_sources(workflow_events=lambda _run: None), tl.STATUS_ABSENT, tl.REASON_NO_RECORD),
        (_sources(workflow_events=lambda _run: []), tl.STATUS_ABSENT, tl.REASON_NO_RECORD),
    ],
)
def test_a_workflow_row_reports_why_its_run_has_no_events(
    store: TaskStore, clock: Clock, sources: tl.Sources, status: str, reason: str
) -> None:
    _run_row(store, clock, "workflow:wf_1:agent1", kind=model.KIND_WORKFLOW_AGENT)
    out = _render(store, "workflow:wf_1:agent1", sources=sources)
    assert out is not None
    assert (out.streams[2].status, out.streams[2].reason) == (status, reason)


def test_a_failing_workflow_reader_is_reported(store: TaskStore, clock: Clock) -> None:
    _run_row(store, clock, "workflow:wf_1:agent1", kind=model.KIND_WORKFLOW_AGENT)

    def _boom(_run: str) -> list[dict[str, Any]]:
        raise OSError("run json unreadable")

    out = _render(store, "workflow:wf_1:agent1", sources=_sources(workflow_events=_boom))
    assert out is not None
    assert (out.streams[2].status, out.streams[2].reason) == (
        tl.STATUS_UNAVAILABLE,
        tl.REASON_READ_FAILED,
    )
    assert len(_entries(out.markdown)) == 4


def test_the_queue_tail_is_the_callers_bound_and_says_when_it_is_reached(
    store: TaskStore, clock: Clock
) -> None:
    _run_row(store, clock, "s1")
    out = _render(store, "s1", events_limit=3)
    assert out is not None
    assert [label for _s, label in _entries(out.markdown)] == [
        "claimed",
        "transition",
        "transition",
    ]
    assert (out.streams[0].status, out.streams[0].reason) == (
        tl.STATUS_TRUNCATED,
        tl.REASON_TAIL_LIMIT,
    )
    assert "- task_events: truncated (tail_limit), 3 entries" in out.markdown


def test_a_history_exactly_as_long_as_the_bound_is_not_called_truncated(
    store: TaskStore, clock: Clock
) -> None:
    _run_row(store, clock, "s1")
    n = len(store.events("s1", limit=200))
    assert n == 4
    out = _render(store, "s1", events_limit=n)
    assert out is not None
    assert len(_entries(out.markdown)) == n
    assert (out.streams[0].status, out.streams[0].reason) == (tl.STATUS_OK, "")
    assert "truncated" not in out.markdown
    # one row fewer than the history, and the tail is truncated again
    out = _render(store, "s1", events_limit=n - 1)
    assert out is not None
    assert len(_entries(out.markdown)) == n - 1
    assert out.streams[0].status == tl.STATUS_TRUNCATED


def test_the_session_log_keeps_the_newest_entries_past_its_bound(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tl, "CREW_ENTRIES_MAX", 2)
    _run_row(store, clock, "s1")
    rows = tuple(_crew_row(n, 5000.0 + n, "subagent/steered", mode=f"m{n}") for n in range(1, 5))
    out = _render(
        store, "s1", sources=_sources(crew_reader=lambda _s, _i: tl.CrewRead(rows=rows, found=True))
    )
    assert out is not None
    assert "mode=m1" not in out.markdown and "mode=m2" not in out.markdown
    assert "mode=m3" in out.markdown and "mode=m4" in out.markdown
    assert (out.streams[1].status, out.streams[1].reason) == (
        tl.STATUS_TRUNCATED,
        tl.REASON_KEPT_LIMIT,
    )


def test_an_incomplete_session_scan_is_reported_as_truncated(
    store: TaskStore, clock: Clock
) -> None:
    _run_row(store, clock, "s1")
    rows = (_crew_row(1, 5000.5, "subagent/spawned"),)
    out = _render(
        store,
        "s1",
        sources=_sources(
            crew_reader=lambda _s, _i: tl.CrewRead(rows=rows, found=True, complete=False)
        ),
    )
    assert out is not None
    assert (out.streams[1].status, out.streams[1].reason) == (
        tl.STATUS_TRUNCATED,
        tl.REASON_SCAN_LIMIT,
    )


def test_the_workflow_run_keeps_the_newest_events_past_its_bound(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tl, "WORKFLOW_EVENTS_MAX", 2)
    _run_row(store, clock, "workflow:wf_1:agent1", kind=model.KIND_WORKFLOW_AGENT)
    events = [_wf_event(n, 5000.0 + n, "log", message=f"m{n}") for n in range(1, 5)]
    out = _render(
        store, "workflow:wf_1:agent1", sources=_sources(workflow_events=lambda _r: events)
    )
    assert out is not None
    assert "message=m1" not in out.markdown and "message=m4" in out.markdown
    assert (out.streams[2].status, out.streams[2].reason) == (
        tl.STATUS_TRUNCATED,
        tl.REASON_KEPT_LIMIT,
    )


def test_entries_without_a_readable_time_are_counted_not_placed(
    store: TaskStore, clock: Clock
) -> None:
    _run_row(store, clock, "s1")
    rows = (
        _crew_row(1, 5000.5, "subagent/spawned"),
        {"type": "subagent/steered", "seq": 2, "time": "yesterday", "data": {"agent_id": "s1"}},
        {"type": "subagent/steered", "seq": 3, "time": True, "data": {"agent_id": "s1"}},
    )
    out = _render(
        store, "s1", sources=_sources(crew_reader=lambda _s, _i: tl.CrewRead(rows=rows, found=True))
    )
    assert out is not None
    assert out.streams[1].count == 1 and out.streams[1].dropped == 2
    assert "- crew_log: ok, 1 entries, 2 without a readable time" in out.markdown


def test_workflow_events_without_a_readable_time_are_counted_not_placed(
    store: TaskStore, clock: Clock
) -> None:
    _run_row(store, clock, "workflow:wf_1:agent1", kind=model.KIND_WORKFLOW_AGENT)
    events = [_wf_event(1, 5000.5), {**_wf_event(2, 5001.0), "ts": "not a time"}]
    out = _render(
        store, "workflow:wf_1:agent1", sources=_sources(workflow_events=lambda _r: events)
    )
    assert out is not None
    assert out.streams[2].count == 1 and out.streams[2].dropped == 1


@pytest.mark.parametrize(
    ("stream", "bad"),
    [
        (tl.STREAM_TASK_EVENTS, float("inf")),
        (tl.STREAM_TASK_EVENTS, float("nan")),
        (tl.STREAM_TASK_EVENTS, 1e20),
        (tl.STREAM_CREW_LOG, 10**20),
        (tl.STREAM_CREW_LOG, 10**400),
        (tl.STREAM_CREW_LOG, float("inf")),
        (tl.STREAM_WORKFLOW, "0001-01-01T00:00:00+14:00"),
        (tl.STREAM_WORKFLOW, "9999-12-31T23:59:59-14:00"),
    ],
)
def test_a_time_no_clock_can_spell_is_counted_not_placed(stream: str, bad: Any) -> None:
    """A time that parses but is not finite or lies outside the clock's years.

    Placing it would raise when its line is stamped, and the whole view would fail
    with it; it is counted like any other unreadable time instead.
    """
    row_id = "workflow:wf_1:agent1" if stream == tl.STREAM_WORKFLOW else "s1"
    kind = model.KIND_WORKFLOW_AGENT if stream == tl.STREAM_WORKFLOW else model.KIND_SUBAGENT
    queue = [_event(1, 5000.0, note="ok")]
    crew = tl.CrewRead()
    workflow: list[dict[str, Any]] = []
    if stream == tl.STREAM_TASK_EVENTS:
        queue.append(_event(2, bad, note="bad"))
    elif stream == tl.STREAM_CREW_LOG:
        rows = (_crew_row(1, 5000.5), {**_crew_row(2, 0.0), "time": bad})
        crew = tl.CrewRead(rows=rows, found=True)
    else:
        workflow = [_wf_event(1, 5001.0), {**_wf_event(2, 0.0), "ts": bad}]
    sources = _sources(crew_reader=lambda _s, _i: crew, workflow_events=lambda _r: workflow)
    out = _render(_FakeStore(_record(row_id, kind), queue), row_id, sources=sources)
    assert out is not None
    report = out.streams[tl.STREAMS.index(stream)]
    assert (report.count, report.dropped) == (1, 1)
    assert f"- {stream}: ok, 1 entries, 1 without a readable time" in out.markdown


def test_an_unreadable_order_leaves_the_entry_placed_by_its_time() -> None:
    rows = ({**_crew_row(1, 5000.5), "seq": "first"}, {**_crew_row(2, 5000.6), "seq": 1e400})
    out = _render(
        _FakeStore(_record(), []),
        "s1",
        sources=_sources(crew_reader=lambda _s, _i: tl.CrewRead(rows=rows, found=True)),
    )
    assert out is not None
    assert (out.streams[1].count, out.streams[1].dropped) == (2, 0)


def test_a_naive_workflow_stamp_is_read_as_utc(store: TaskStore, clock: Clock) -> None:
    _run_row(store, clock, "workflow:wf_1:agent1", kind=model.KIND_WORKFLOW_AGENT)
    naive = datetime.fromtimestamp(5000.5, tz=timezone.utc).replace(tzinfo=None).isoformat()
    events = [{**_wf_event(1, 0.0), "ts": naive}]
    out = _render(
        store, "workflow:wf_1:agent1", sources=_sources(workflow_events=lambda _r: events)
    )
    assert out is not None
    assert f"{tl._stamp(5000.5)} [workflow]" in out.markdown


def test_one_long_value_is_cut_on_one_line() -> None:
    record = _record()
    fake = _FakeStore(record, [_event(1, 5000.0, note="x" * 2000 + "\nsecond line")])
    out = _render(fake, "s1")
    assert out is not None
    line = next(ln for ln in out.markdown.splitlines() if "[task_events]" in ln)
    assert line.endswith("...") and "\n" not in line
    assert len(line.split("note=", 1)[1]) == tl.VALUE_MAX_CHARS


# ── scrubbing ────────────────────────────────────────────────────────────────

_STREAMS = [tl.STREAM_TASK_EVENTS, tl.STREAM_CREW_LOG, tl.STREAM_WORKFLOW]


def _planted(stream: str, plant: Any) -> tuple[Any, str, tl.Sources]:
    """A fake store and sources where only *stream* carries *plant* (a callable of the data)."""
    row_id = "workflow:wf_1:agent1" if stream == tl.STREAM_WORKFLOW else "s1"
    kind = model.KIND_WORKFLOW_AGENT if stream == tl.STREAM_WORKFLOW else model.KIND_SUBAGENT
    record = _record(row_id, kind)
    queue = _event(1, 5000.0, note="benign")
    crew = tl.CrewRead(rows=(_crew_row(1, 5000.5, task="benign"),), found=True)
    workflow = [_wf_event(1, 5001.0, message="benign")]
    # The planted mapping is merged into the data rather than passed as keywords,
    # so it may carry keys that are not strings.
    if stream == tl.STREAM_TASK_EVENTS:
        queue = _event(1, 5000.0)
        queue.data.update(plant("note"))
    elif stream == tl.STREAM_CREW_LOG:
        row = _crew_row(1, 5000.5)
        row["data"].update(plant("task"))
        crew = tl.CrewRead(rows=(row,), found=True)
    else:
        workflow = [_wf_event(1, 5001.0)]
        workflow[0]["data"].update(plant("message"))
    sources = _sources(crew_reader=lambda _s, _i: crew, workflow_events=lambda _r: workflow)
    return _FakeStore(record, [queue]), row_id, sources


@pytest.mark.parametrize("stream", _STREAMS)
@pytest.mark.parametrize(
    "plant",
    [
        lambda key: {key: f"boom {SECRET} end"},
        lambda key: {key: {"deep": [{"deeper": SECRET}]}},
        lambda key: {key: PEM},
        lambda key: {key: {"k": PEM}},
    ],
    ids=["leaf", "nested", "pem", "nested-pem"],
)
def test_a_credential_in_any_stream_never_reaches_the_output(stream: str, plant: Any) -> None:
    fake, row_id, sources = _planted(stream, plant)
    out = _render(fake, row_id, sources=sources)
    assert out is not None
    assert SECRET not in out.markdown and "MIIEow" not in out.markdown
    assert "[REDACTED" in out.markdown


@pytest.mark.parametrize("stream", _STREAMS)
def test_an_unterminated_pem_mapping_key_is_redacted_before_json_encoding(
    stream: str,
) -> None:
    fake, row_id, sources = _planted(stream, lambda key: {key: {TRUNCATED_PEM: 1}})
    out = _render(fake, row_id, sources=sources)
    assert out is not None
    assert "MIIEow" not in out.markdown
    assert "[REDACTED" in out.markdown


@pytest.mark.parametrize("stream", _STREAMS)
def test_an_unterminated_pem_top_level_key_is_redacted_before_its_value_is_joined(
    stream: str,
) -> None:
    """A top-level key is put on the line as ``key=value``. Scrubbing only the joined
    line lets the ``={}`` after an unterminated PEM block stop the pattern short of
    the block's last base64 line, so the key is scrubbed on its own first."""
    fake, row_id, sources = _planted(stream, lambda _key: {TRUNCATED_PEM: {}})
    out = _render(fake, row_id, sources=sources)
    assert out is not None
    assert "MIIEow" not in out.markdown
    assert "[REDACTED" in out.markdown


_BYTES_PEM = TRUNCATED_PEM.encode()


@pytest.mark.parametrize("stream", _STREAMS)
@pytest.mark.parametrize(
    "plant",
    [
        lambda key: {key: _BYTES_PEM},
        lambda key: {key: PEM.encode()},
        lambda key: {key: {"k": _BYTES_PEM}},
        lambda key: {key: [_BYTES_PEM]},
        lambda key: {key: {_BYTES_PEM: 1}},
        lambda _key: {_BYTES_PEM: 1},
    ],
    ids=["value", "terminated", "in-mapping", "in-list", "nested-key", "top-level-key"],
)
def test_a_bytes_pem_is_redacted_before_any_encoding_escapes_it(stream: str, plant: Any) -> None:
    """A workflow may log bytes. Encoding them first (``str``, ``repr``, JSON) turns
    each newline into an escape, which the key pattern does not match; every leaf
    and key is decoded and scrubbed before anything is encoded."""
    fake, row_id, sources = _planted(stream, plant)
    out = _render(fake, row_id, sources=sources)
    assert out is not None
    assert "MIIEow" not in out.markdown
    assert "[REDACTED" in out.markdown


def _scrub_keeping_key_types(value: Any) -> Any:
    """A caller's scrubber that redacts string leaves and leaves every key as it is."""
    if isinstance(value, str):
        return tasks_mod._scrub_prose(value)
    if isinstance(value, Mapping):
        return {key: _scrub_keeping_key_types(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_keeping_key_types(item) for item in value]
    return value


@pytest.mark.parametrize("stream", _STREAMS)
def test_a_mapping_json_cannot_sort_is_rendered_from_the_scrubbed_copy(stream: str) -> None:
    """Mixed key types make the sorted encode raise; the fallback must not repr the raw keys."""
    fake, row_id, sources = _planted(stream, lambda key: {key: {TRUNCATED_PEM: 1, 2: 3}})
    out = _render(fake, row_id, sources=sources, scrub=_scrub_keeping_key_types)
    assert out is not None
    assert "MIIEow" not in out.markdown
    assert "[REDACTED" in out.markdown


@pytest.mark.parametrize("stream", _STREAMS)
def test_a_credential_cut_by_the_line_bound_is_redacted_before_the_cut(stream: str) -> None:
    """A secret straddling the cut would leave a prefix no pattern recognises."""
    lead = "x" * (tl.VALUE_MAX_CHARS - 13)
    fake, row_id, sources = _planted(stream, lambda key: {key: lead + SECRET})
    out = _render(fake, row_id, sources=sources)
    assert out is not None
    assert "AKIAIO" not in out.markdown and "AKIA" not in out.markdown
    assert "[REDACTED" in out.markdown


class _Shown:
    """A leaf JSON cannot encode, so the serializer renders it through ``__str__``."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text


#: Long enough that the serialized value runs past the cut with the planted id
#: across it, short enough that the id starts before the cut.
_PAD = "x" * 285


def _strings_only(value: Any) -> Any:
    """The route's scrubber applied to strings alone: a mapping keeps keys JSON cannot sort."""
    return tasks_mod._scrub_value(value) if isinstance(value, str) else value


@pytest.mark.parametrize("stream", _STREAMS)
@pytest.mark.parametrize(
    ("plant", "scrub"),
    [
        (lambda key: {key: {f"{_PAD} {SECRET}": 1}}, tasks_mod._scrub_value),
        (lambda key: {key: _Shown(f"{_PAD} {SECRET}")}, tasks_mod._scrub_value),
        (lambda key: {key: {f"{_PAD} {SECRET}": 1, _Shown("z"): 3}}, _strings_only),
    ],
    ids=["nested-key", "str-rendered-leaf", "mixed-key-types"],
)
def test_text_the_serializer_adds_is_scrubbed_before_the_cut(
    stream: str, plant: Any, scrub: Any
) -> None:
    """A mapping key or a leaf rendered through ``str`` is text the leaf scrub never saw.

    It reaches the line through serialization, and the cut that follows would keep
    a prefix of a credential that no pattern recognises.
    """
    fake, row_id, sources = _planted(stream, plant)
    out = _render(fake, row_id, sources=sources, scrub=scrub)
    assert out is not None
    leaked = [SECRET[:n] for n in range(4, len(SECRET) + 1) if SECRET[:n] in out.markdown]
    assert leaked == []
    assert "[REDACTED" in out.markdown


@pytest.mark.parametrize("stream", _STREAMS)
def test_a_benign_nested_mapping_still_renders_whole(stream: str) -> None:
    nested = {"step": {"name": "build", "count": 2, "ok": True}}
    fake, row_id, sources = _planted(stream, lambda key: {key: nested})
    out = _render(fake, row_id, sources=sources)
    assert out is not None
    assert '={"step":{"count":2,"name":"build","ok":true}}' in out.markdown
    assert "[REDACTED" not in out.markdown


@pytest.mark.parametrize("stream", _STREAMS)
def test_a_credential_in_a_label_is_redacted(stream: str) -> None:
    record = _record("workflow:wf_1:agent1" if stream == tl.STREAM_WORKFLOW else "s1")
    if stream == tl.STREAM_TASK_EVENTS:
        queue = [_event(1, 5000.0, f"transition {SECRET}", a="b")]
        sources = _sources(crew_reader=lambda _s, _i: tl.CrewRead())
    elif stream == tl.STREAM_CREW_LOG:
        queue = []
        row = {**_crew_row(1, 5000.5), "type": f"subagent/steered {SECRET}"}
        sources = _sources(crew_reader=lambda _s, _i: tl.CrewRead(rows=(row,), found=True))
    else:
        record = _record("workflow:wf_1:agent1", model.KIND_WORKFLOW_AGENT)
        queue = []
        event = {**_wf_event(1, 5001.0), "type": f"log {SECRET}"}
        sources = _sources(workflow_events=lambda _r: [event])
    out = _render(_FakeStore(record, queue), record.id, sources=sources)
    assert out is not None
    assert SECRET not in out.markdown
    assert "[REDACTED" in out.markdown


def test_a_credential_in_the_rows_own_fields_is_redacted() -> None:
    record = _record()
    record.kind = f"subagent {SECRET}"
    record.state = f"running {SECRET}"
    out = _render(_FakeStore(record, []), "s1")
    assert out is not None
    assert SECRET not in out.markdown
    assert "[REDACTED" in out.markdown


def test_the_renderer_has_no_default_scrubber() -> None:
    params = inspect.signature(tl.render_timeline).parameters
    assert params["scrub"].default is inspect.Parameter.empty
    assert params["events_limit"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        tl.render_timeline("s1", store=_FakeStore(_record(), []), events_limit=5, sources=_sources())  # type: ignore[call-arg]


def test_the_renderer_uses_the_scrubber_it_is_given() -> None:
    seen: list[Any] = []

    def _mark(value: Any) -> Any:
        seen.append(value)
        return value if not isinstance(value, str) else value.replace("benign", "MARKED")

    out = _render(_FakeStore(_record(), [_event(1, 5000.0, note="benign")]), "s1", scrub=_mark)
    assert out is not None
    assert "note=MARKED" in out.markdown and "benign" not in out.markdown
    assert any(isinstance(v, dict) for v in seen) and any(isinstance(v, str) for v in seen)


# ── the real crew log ────────────────────────────────────────────────────────


def _log(unit_id: str, *, slot: str = "chat-1") -> CrewLog:
    handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": slot,
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
            "class": {"memory": "persistent"},
        },
        src=GATEWAY,
    )
    return handle


def _note(handle: CrewLog, kind: str, agent_id: str, **data: Any) -> None:
    handle.append(f"subagent/{kind}", {"agent_id": agent_id, **data}, src=GATEWAY)


@pytest.fixture
def ticks(monkeypatch: pytest.MonkeyPatch):
    """Crew log entry times that this test controls: 5000.5 s, then one second apart."""
    state = {"ms": 5_000_500}

    def _now_ms() -> int:
        value = state["ms"]
        state["ms"] += 1000
        return value

    monkeypatch.setattr(crew_store, "now_ms", _now_ms)
    return state


def test_the_reader_returns_only_the_childs_subagent_entries_in_order(
    ticks: dict[str, int],
) -> None:
    handle = _log("acp-1")
    _note(handle, "spawned", "s1", task="first")
    _note(handle, "spawned", "s2", task="other child")
    _note(handle, "steered", "s1", mode="interrupt")
    _note(handle, "completed", "s2", ms=5)
    _note(handle, "completed", "s1", ms=9)
    found = tl.read_child_entries("chat-1", "s1")
    assert found.found and found.complete
    assert [row["type"] for row in found.rows] == [
        "subagent/spawned",
        "subagent/steered",
        "subagent/completed",
    ]
    assert {row["data"]["agent_id"] for row in found.rows} == {"s1"}
    assert [row["seq"] for row in found.rows] == sorted(row["seq"] for row in found.rows)


def test_a_child_no_unit_records_is_absent_not_an_error(ticks: dict[str, int]) -> None:
    _note(_log("acp-1"), "spawned", "s1")
    assert tl.read_child_entries("chat-1", "nobody") == tl.CrewRead()
    assert tl.read_child_entries("no-such-slot", "s1") == tl.CrewRead()


def test_a_child_dispatched_before_a_reset_is_found_in_the_older_unit(
    ticks: dict[str, int],
) -> None:
    older = _log("acp-before")
    _note(older, "spawned", "old-1", task="before the reset")
    _note(older, "completed", "old-1", ms=3)
    newer = _log("acp-after")
    _note(newer, "spawned", "new-1")
    found = tl.read_child_entries("chat-1", "old-1")
    assert found.found
    assert [row["type"] for row in found.rows] == ["subagent/spawned", "subagent/completed"]


def test_the_scan_stops_at_its_bound_and_says_so(
    ticks: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = _log("acp-1")
    _note(handle, "spawned", "s1")
    for n in range(6):
        _note(handle, "steered", "other", mode=f"m{n}")
    _note(handle, "completed", "s1", ms=1)
    monkeypatch.setattr(tl, "CREW_SCAN_MAX", 3)
    found = tl.read_child_entries("chat-1", "s1")
    assert found.found and not found.complete
    assert [row["type"] for row in found.rows] == ["subagent/spawned"]


def test_the_scan_window_starts_at_the_childs_dispatch_not_at_the_top_of_the_log(
    ticks: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """More unrelated entries precede the dispatch than the window holds."""
    handle = _log("acp-1")
    for n in range(6):
        _note(handle, "steered", "other", mode=f"m{n}")
    _note(handle, "spawned", "s1", task="late child")
    _note(handle, "completed", "s1", ms=2)
    monkeypatch.setattr(tl, "CREW_SCAN_MAX", 4)
    found = tl.read_child_entries("chat-1", "s1")
    assert found.found and found.complete
    assert [row["type"] for row in found.rows] == ["subagent/spawned", "subagent/completed"]


def test_a_real_log_and_a_real_row_merge_end_to_end(
    store: TaskStore, clock: Clock, ticks: dict[str, int]
) -> None:
    _run_row(store, clock, "s1")
    handle = _log("acp-1")
    _note(handle, "spawned", "s1", task="do it")
    _note(handle, "completed", "s1", ms=7)
    out = tl.render_timeline(
        "s1",
        store=store,
        scrub=tasks_mod._scrub_value,
        events_limit=200,
        sources=tl.Sources(slot_of=lambda key: key),
    )
    assert out is not None
    # The header and the opened entry take two ticks; the spawn lands at the third.
    assert [(s, label) for s, label in _entries(out.markdown) if s == "crew_log"] == [
        ("crew_log", "subagent/spawned"),
        ("crew_log", "subagent/completed"),
    ]
    assert out.streams[1].status == tl.STATUS_OK and out.streams[1].count == 2
    assert "[crew_log] subagent/spawned: task=do it" in out.markdown


def test_a_session_log_that_cannot_be_searched_is_unavailable(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.crew_log import resolve

    def _fail(_slot: str, _agent: str) -> str:
        raise resolve.UnitSearchFailed("units could not be listed")

    monkeypatch.setattr(resolve, "unit_holding_child", _fail)
    _run_row(store, clock, "s1")
    out = tl.render_timeline(
        "s1",
        store=store,
        scrub=tasks_mod._scrub_value,
        events_limit=200,
        sources=tl.Sources(slot_of=lambda key: key),
    )
    assert out is not None
    assert (out.streams[1].status, out.streams[1].reason) == (
        tl.STATUS_UNAVAILABLE,
        tl.REASON_READ_FAILED,
    )
    assert len(_entries(out.markdown)) == 4


# ── the workflow registry adapter ────────────────────────────────────────────


class _Handle:
    def __init__(self, events: list[Any]) -> None:
        self.events = events


class _Registry:
    def __init__(self, runs: dict[str, _Handle]) -> None:
        self._runs = runs

    def get(self, run_id: str) -> _Handle | None:
        return self._runs.get(run_id)


def test_the_registry_adapter_hands_the_runs_events_over_in_place() -> None:
    """Nothing is copied or converted here; the stream keeps its newest bound first."""
    events = [
        WorkflowEvent(run_id="wf_1", seq=n, ts=_iso(5000.0 + n), type="log", data={"message": "m"})
        for n in range(1, 4)
    ]
    reader = tl.events_from_registry(_Registry({"wf_1": _Handle(events)}))
    assert reader("wf_1") is events
    assert reader("wf_unknown") is None


class _CountingEvent:
    """A run's own event object, counting its conversions and its data reads."""

    def __init__(self, seq: int, ts: str) -> None:
        self.run_id = "wf_1"
        self.seq = seq
        self.ts = ts
        self.type = "log"
        self._data = {"message": f"m{seq}"}
        self.converted = 0
        self.read = 0

    @property
    def data(self) -> dict[str, Any]:
        self.read += 1
        return self._data

    def to_json(self) -> dict[str, Any]:
        self.converted += 1
        return {"run_id": "wf_1", "seq": self.seq, "ts": self.ts, "type": "log", "data": self.data}


def test_a_long_run_is_bounded_before_any_event_is_converted_or_rendered(
    store: TaskStore, clock: Clock
) -> None:
    """A run with 1,000 events costs the view its bound, not the run's length.

    The two events without a readable time sit outside and inside the kept
    window; both are counted as dropped and neither takes a kept slot."""
    _run_row(store, clock, "workflow:wf_1:agent1", kind=model.KIND_WORKFLOW_AGENT)
    events = [_CountingEvent(n, _iso(5000.0 + n)) for n in range(1, 1001)]
    events[4].ts = "not a time"
    events[994].ts = "not a time"
    reader = tl.events_from_registry(_Registry({"wf_1": _Handle(events)}))
    out = _render(store, "workflow:wf_1:agent1", sources=_sources(workflow_events=reader))
    assert out is not None
    assert sum(event.converted for event in events) == 0
    assert sum(event.read for event in events) <= tl.WORKFLOW_EVENTS_MAX
    report = out.streams[2]
    assert (report.status, report.reason) == (tl.STATUS_TRUNCATED, tl.REASON_KEPT_LIMIT)
    assert (report.count, report.dropped) == (tl.WORKFLOW_EVENTS_MAX, 2)
    shown = [line for line in out.markdown.splitlines() if "[workflow]" in line]
    assert len(shown) == tl.WORKFLOW_EVENTS_MAX
    assert "message=m800" in shown[0] and "message=m1000" in shown[-1]
    assert "message=m995" not in out.markdown and "message=m5 " not in out.markdown


def _counting_scrub(rendered: list[Any]) -> Any:
    """The route's scrubber, recording each event data mapping it is handed."""

    def _scrub(value: Any) -> Any:
        if isinstance(value, Mapping):
            rendered.append(value)
        return tasks_mod._scrub_value(value)

    return _scrub


@pytest.mark.parametrize("stream", [tl.STREAM_CREW_LOG, tl.STREAM_WORKFLOW])
def test_a_stream_renders_only_the_entries_it_keeps(
    store: TaskStore, clock: Clock, stream: str
) -> None:
    """The bound is applied before rendering, so the renderer sees at most the bound."""
    if stream == tl.STREAM_CREW_LOG:
        _run_row(store, clock, "s1")
        rows = [_crew_row(n, 5000.0 + n, mode=f"m{n}") for n in range(1, 1001)]
        rows[4]["time"] = "yesterday"
        rows[994]["time"] = None
        sources = _sources(crew_reader=lambda _s, _i: tl.CrewRead(rows=tuple(rows), found=True))
        row_id, bound, index = "s1", tl.CREW_ENTRIES_MAX, 1
    else:
        _run_row(store, clock, "workflow:wf_1:agent1", kind=model.KIND_WORKFLOW_AGENT)
        events = [_wf_event(n, 5000.0 + n, "log", message=f"m{n}") for n in range(1, 1001)]
        events[4]["ts"] = "not a time"
        events[994]["ts"] = None
        sources = _sources(workflow_events=lambda _r: events)
        row_id, bound, index = "workflow:wf_1:agent1", tl.WORKFLOW_EVENTS_MAX, 2
    rendered: list[Any] = []
    out = _render(store, row_id, sources=sources, scrub=_counting_scrub(rendered))
    assert out is not None
    # The queue's own four events render too; the planted key tells the stream's apart.
    assert len([data for data in rendered if "mode" in data or "message" in data]) == bound
    report = out.streams[index]
    assert (report.status, report.reason) == (tl.STATUS_TRUNCATED, tl.REASON_KEPT_LIMIT)
    assert (report.count, report.dropped) == (bound, 2)
    assert "=m800" in out.markdown and "=m1000" in out.markdown
    assert "=m799 " not in out.markdown and "=m995" not in out.markdown


# ── the route ────────────────────────────────────────────────────────────────


class _Manager:
    def __init__(self, store: Any) -> None:
        self._taskq = store
        self.max_concurrent = 4
        self.running_count = 0
        self._queue: list[dict[str, Any]] = []
        self.running: list[Any] = []


def _state(store: Any, *, owner_id: str = "", **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(subagents=_Manager(store), _slots={}, owner_id=owner_id, **extra)


def _get(
    state: Any,
    task_id: str,
    *,
    user: str | None = "local-app",
    app_token: str | None = "",
) -> web.Request:
    """A GET /api/tasks/{id}/timeline as the token middleware leaves it: claims set."""
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(
        "GET", f"/api/tasks/{task_id}/timeline", match_info={"task_id": task_id}, app=app
    )
    if user is not None:
        req["user"] = user
    if app_token is not None:
        req["app"] = app_token
    return req


def _body(resp: web.Response) -> dict[str, Any]:
    assert resp.text is not None
    return json.loads(resp.text)


@pytest.mark.asyncio
async def test_the_owner_reads_the_timeline(store: TaskStore, clock: Clock) -> None:
    _run_row(store, clock, "s1")
    resp = await tasks_mod.api_task_timeline(_get(_state(store), "s1"))
    assert resp.status == 200
    body = _body(resp)
    assert body["task_id"] == "s1" and body["entries"] >= 4
    assert "[task_events] accepted" in body["markdown"]
    assert set(body) == {"task_id", "markdown", "entries"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_id", "user", "app_token"),
    [
        ("", "local-app", "some-app"),  # an app token is never the owner
        ("owner-1", "guest-2", ""),  # a dashboard user who is not the configured owner
        ("", None, None),  # no claims at all
    ],
)
async def test_a_non_owner_is_refused_before_the_row_is_read(
    store: TaskStore,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    owner_id: str,
    user: str | None,
    app_token: str | None,
) -> None:
    _run_row(store, clock, "s1")
    reads: list[str] = []
    real_get = store.get

    def _counting_get(task_id: str) -> Any:
        reads.append(task_id)
        return real_get(task_id)

    monkeypatch.setattr(store, "get", _counting_get)
    resp = await tasks_mod.api_task_timeline(
        _get(_state(store, owner_id=owner_id), "s1", user=user, app_token=app_token)
    )
    assert resp.status in (401, 403)
    body = _body(resp)
    assert body.get("code")
    assert "markdown" not in body and "s1" not in json.dumps(body)
    assert reads == []


@pytest.mark.asyncio
async def test_the_configured_owner_passes_the_gate(store: TaskStore, clock: Clock) -> None:
    _run_row(store, clock, "s1")
    resp = await tasks_mod.api_task_timeline(
        _get(_state(store, owner_id="owner-1"), "s1", user="owner-1")
    )
    assert resp.status == 200


@pytest.mark.asyncio
async def test_an_unknown_id_is_404_with_a_code(store: TaskStore) -> None:
    resp = await tasks_mod.api_task_timeline(_get(_state(store), "nope"))
    assert resp.status == 404
    assert _body(resp)["code"] == "not_found"


@pytest.mark.asyncio
async def test_no_store_is_404_with_a_code() -> None:
    resp = await tasks_mod.api_task_timeline(_get(_state(None), "s1"))
    assert resp.status == 404
    assert _body(resp)["code"] == "unavailable"


@pytest.mark.asyncio
async def test_a_store_that_fails_is_503_with_a_code(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_row(store, clock, "s1")

    def _boom(_task_id: str) -> Any:
        raise OSError("disk gone")

    monkeypatch.setattr(store, "get", _boom)
    resp = await tasks_mod.api_task_timeline(_get(_state(store), "s1"))
    assert resp.status == 503
    assert _body(resp)["code"] == "unavailable"


@pytest.mark.asyncio
async def test_the_route_scrubs_what_the_store_holds(store: TaskStore, clock: Clock) -> None:
    store.accept([_rec("s1", session_key="chat-1")])
    gen = store.claim("s1", owner="o1").generation
    clock.advance(1.0)
    assert store.transition("s1", model.STARTING, generation=gen)
    clock.advance(1.0)
    assert store.transition("s1", model.RUNNING, generation=gen)
    clock.advance(1.0)
    assert store.transition(
        "s1", model.FAILED, generation=gen, detail={"error": f"boom {SECRET}", "log": [PEM]}
    )
    body = _body(await tasks_mod.api_task_timeline(_get(_state(store), "s1")))
    assert SECRET not in body["markdown"] and "MIIEow" not in body["markdown"]
    assert "boom [REDACTED: credential]" in body["markdown"]


@pytest.mark.asyncio
async def test_the_route_reads_workflow_events_from_the_service_registry(
    store: TaskStore, clock: Clock
) -> None:
    _run_row(store, clock, "workflow:wf_1:agent1", kind=model.KIND_WORKFLOW_AGENT)
    event = WorkflowEvent(
        run_id="wf_1", seq=1, ts=_iso(5000.5), type="log", data={"message": "hello"}
    )
    service = SimpleNamespace(registry=_Registry({"wf_1": _Handle([event])}))
    resp = await tasks_mod.api_task_timeline(
        _get(_state(store, workflow_service=service), "workflow:wf_1:agent1")
    )
    body = _body(resp)
    assert "[workflow] log: message=hello" in body["markdown"]
    assert "- workflow: ok, 1 entries" in body["markdown"]


@pytest.mark.asyncio
async def test_without_a_workflow_service_the_stream_is_reported_unavailable(
    store: TaskStore, clock: Clock
) -> None:
    _run_row(store, clock, "workflow:wf_1:agent1", kind=model.KIND_WORKFLOW_AGENT)
    body = _body(await tasks_mod.api_task_timeline(_get(_state(store), "workflow:wf_1:agent1")))
    assert "- workflow: unavailable (no_source), 0 entries" in body["markdown"]


@pytest.mark.asyncio
async def test_reading_a_timeline_changes_nothing(store: TaskStore, clock: Clock) -> None:
    _run_row(store, clock, "s1")
    store.accept([_rec("s2", session_key="chat-1")])

    def _snapshot() -> tuple[Any, Any]:
        rows = [r.public() for r in store.list_rows(limit=100)]
        events = {t: [(e.seq, e.ts, e.kind, e.data) for e in store.events(t)] for t in ("s1", "s2")}
        return rows, events

    before = _snapshot()
    for _ in range(2):
        resp = await tasks_mod.api_task_timeline(_get(_state(store), "s1"))
        assert resp.status == 200
    assert _snapshot() == before


# ── registration and the existing routes ─────────────────────────────────────


@pytest.mark.asyncio
async def test_the_lazy_wrapper_calls_the_real_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    request = make_mocked_request("GET", "/api/tasks/s1/timeline")
    response = web.json_response({"ok": True})
    handler = AsyncMock(return_value=response)
    monkeypatch.setattr(tasks_mod, "api_task_timeline", handler)
    assert await handlers_pkg.api_task_timeline(request) is response
    handler.assert_awaited_once_with(request)


def _tasks_routes() -> set[tuple[str, str]]:
    from kiro_crew.dashboard.routes import system

    app = web.Application()
    system.register(app)
    return {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.resource is not None
        and route.resource.canonical.startswith("/api/tasks")
        and route.method != "HEAD"  # aiohttp adds a HEAD twin to every add_get
    }


def test_the_task_routes_are_the_old_set_plus_one_get() -> None:
    assert _tasks_routes() == {
        ("GET", "/api/tasks"),
        ("GET", "/api/tasks/summary"),
        ("GET", "/api/tasks/{task_id}"),
        ("POST", "/api/tasks/{task_id}"),
        ("POST", "/api/tasks/{task_id}/cancel"),
        ("GET", "/api/tasks/{task_id}/timeline"),
    }


@pytest.mark.asyncio
async def test_the_timeline_path_resolves_to_its_own_handler_not_the_detail_route() -> None:
    from kiro_crew.dashboard.routes import system

    app = web.Application()
    system.register(app)
    match = await app.router.resolve(make_mocked_request("GET", "/api/tasks/s1/timeline"))
    assert match.route.handler is handlers_pkg.api_task_timeline
    assert match.get("task_id") == "s1" or match.match_info.get("task_id") == "s1"
    post = await app.router.resolve(make_mocked_request("POST", "/api/tasks/s1/timeline"))
    assert post.http_exception is not None and post.http_exception.status == 405


# The responses the five pre-existing routes gave on the base tree for one fixed store,
# captured with the clock, the claim owner and the wall clock pinned.
_BASE_GOLDEN = json.loads(r"""{
 "action_bad": [
  400,
  "{\"error\": \"unknown action\", \"code\": \"bad_action\", \"actions\": [\"answer_input\", \"cancel_wait\"]}"
 ],
 "cancel_terminal": [
  409,
  "{\"ok\": false, \"cancelled\": false, \"code\": \"terminal\", \"task\": {\"id\": \"g1\", \"kind\": \"subagent\", \"state\": \"failed\", \"lane\": \"dashboard:chat-1\", \"session_key\": \"dashboard:chat-1\", \"parent_id\": null, \"root_id\": \"g1\", \"attempts\": 1, \"generation\": 1, \"next_run_at\": null, \"deadline_at\": null, \"lease_owner\": null, \"lease_expires_at\": null, \"wait\": null, \"wait_reason\": null, \"wait_since\": null, \"wait_deadline_at\": null, \"age_secs\": 1997.0, \"created_at\": 5000.0, \"updated_at\": 5003.0, \"terminal\": true}}"
 ],
 "detail": [
  200,
  "{\"task\": {\"id\": \"g1\", \"kind\": \"subagent\", \"state\": \"failed\", \"lane\": \"dashboard:chat-1\", \"session_key\": \"dashboard:chat-1\", \"parent_id\": null, \"root_id\": \"g1\", \"attempts\": 1, \"generation\": 1, \"next_run_at\": null, \"deadline_at\": null, \"lease_owner\": null, \"lease_expires_at\": null, \"wait\": null, \"wait_reason\": null, \"wait_since\": null, \"wait_deadline_at\": null, \"age_secs\": 0.0, \"created_at\": 5000.0, \"updated_at\": 5003.0, \"terminal\": true}, \"events\": [{\"seq\": 1, \"ts\": 5000.0, \"kind\": \"accepted\", \"data\": {\"kind\": \"subagent\"}}, {\"seq\": 2, \"ts\": 5000.0, \"kind\": \"claimed\", \"data\": {\"generation\": 1, \"owner\": \"owner-fixed\"}}, {\"seq\": 3, \"ts\": 5001.0, \"kind\": \"transition\", \"data\": {\"from\": \"admitted\", \"to\": \"starting\"}}, {\"seq\": 4, \"ts\": 5002.0, \"kind\": \"transition\", \"data\": {\"from\": \"starting\", \"to\": \"running\"}}, {\"seq\": 5, \"ts\": 5003.0, \"kind\": \"transition\", \"data\": {\"error\": \"boom [REDACTED: credential]\", \"from\": \"running\", \"to\": \"failed\"}}]}"
 ],
 "detail_missing": [
  404,
  "{\"error\": \"not found\", \"code\": \"not_found\"}"
 ],
 "list": [
  200,
  "{\"available\": true, \"tasks\": [{\"id\": \"g1\", \"kind\": \"subagent\", \"state\": \"failed\", \"lane\": \"dashboard:chat-1\", \"session_key\": \"dashboard:chat-1\", \"parent_id\": null, \"root_id\": \"g1\", \"attempts\": 1, \"generation\": 1, \"next_run_at\": null, \"deadline_at\": null, \"lease_owner\": null, \"lease_expires_at\": null, \"wait\": null, \"wait_reason\": null, \"wait_since\": null, \"wait_deadline_at\": null, \"age_secs\": 0.0, \"created_at\": 5000.0, \"updated_at\": 5003.0, \"terminal\": true}, {\"id\": \"g2\", \"kind\": \"subagent\", \"state\": \"queued\", \"lane\": \"dashboard:chat-1\", \"session_key\": \"dashboard:chat-1\", \"parent_id\": null, \"root_id\": \"g2\", \"attempts\": 0, \"generation\": 0, \"next_run_at\": null, \"deadline_at\": null, \"lease_owner\": null, \"lease_expires_at\": null, \"wait\": null, \"wait_reason\": null, \"wait_since\": null, \"wait_deadline_at\": null, \"age_secs\": 0.0, \"created_at\": 5003.0, \"updated_at\": 5003.0, \"terminal\": false}], \"count\": 2, \"limit\": 100, \"filters\": {\"state\": null, \"lane\": null}}"
 ],
 "list_bad_state": [
  400,
  "{\"error\": \"unknown task state\", \"code\": \"bad_state\", \"states\": [\"admitted\", \"cancelled\", \"done\", \"failed\", \"queued\", \"recovering\", \"retry_wait\", \"running\", \"starting\", \"unknown_side_effect\", \"waiting_children\", \"waiting_dependency\", \"waiting_infra\", \"waiting_input\", \"waiting_permission\"]}"
 ],
 "list_done": [
  200,
  "{\"available\": true, \"tasks\": [], \"count\": 0, \"limit\": 1, \"filters\": {\"state\": \"done\", \"lane\": null}}"
 ],
 "summary_keys_probe": [
  200,
  [
   "adaptive",
   "available",
   "counts",
   "degrade_reason",
   "depth",
   "generated_at",
   "lanes",
   "oldest_wait_secs",
   "recovering",
   "slots",
   "stall_after_secs",
   "stalled",
   "waiting"
  ]
 ]
}""")


@pytest.mark.asyncio
async def test_every_existing_task_route_answers_byte_for_byte_as_before(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.accept([_rec("g1", session_key="dashboard:chat-1")])
    gen = store.claim("g1", owner="owner-fixed").generation
    clock.advance(1.0)
    assert store.transition("g1", model.STARTING, generation=gen)
    clock.advance(1.0)
    assert store.transition("g1", model.RUNNING, generation=gen)
    clock.advance(1.0)
    assert store.transition("g1", model.FAILED, generation=gen, detail={"error": f"boom {SECRET}"})
    store.accept([_rec("g2", session_key="dashboard:chat-1")])
    monkeypatch.setattr(tasks_mod, "time", SimpleNamespace(time=lambda: 7000.0))
    app = web.Application()
    app["state"] = _state(store)
    seen: dict[str, list[Any]] = {}

    async def call(
        key: str, handler: Any, method: str, path: str, *, match: Any = None, body: Any = None
    ) -> None:
        req = make_mocked_request(method, path, match_info=match or {}, app=app)
        if body is not None:
            fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
            fut.set_result(body)
            req.json = lambda: fut  # type: ignore[method-assign]
        resp = await handler(req)
        seen[key] = [resp.status, resp.text]

    await call("list", tasks_mod.api_tasks_list, "GET", "/api/tasks")
    await call("list_done", tasks_mod.api_tasks_list, "GET", "/api/tasks?state=done&limit=1")
    await call("list_bad_state", tasks_mod.api_tasks_list, "GET", "/api/tasks?state=bogus")
    await call("detail", tasks_mod.api_task_detail, "GET", "/api/tasks/g1", match={"task_id": "g1"})
    await call(
        "detail_missing",
        tasks_mod.api_task_detail,
        "GET",
        "/api/tasks/nope",
        match={"task_id": "nope"},
    )
    await call(
        "action_bad",
        tasks_mod.api_task_action,
        "POST",
        "/api/tasks/g1",
        match={"task_id": "g1"},
        body={"action": "bogus"},
    )
    await call(
        "cancel_terminal",
        tasks_mod.api_task_cancel,
        "POST",
        "/api/tasks/g1/cancel",
        match={"task_id": "g1"},
    )
    await call("summary_keys_probe", tasks_mod.api_tasks_summary, "GET", "/api/tasks/summary")
    seen["summary_keys_probe"] = [
        seen["summary_keys_probe"][0],
        sorted(json.loads(seen["summary_keys_probe"][1])),
    ]
    assert seen == _BASE_GOLDEN


# ── tree scans ───────────────────────────────────────────────────────────────

_TIMELINE = "taskq/timeline.py"
_RENDER_CALLERS = {"dashboard/handlers/tasks.py"}
#: Modules timeline.py may import at module level.
_ALLOWED_IMPORTS = {
    "__future__",
    "json",
    "logging",
    "math",
    "collections",
    "collections.abc",
    "dataclasses",
    "datetime",
    "typing",
    "kiro_crew.taskq",
    "kiro_crew.taskq.adapters.runner",
}
#: Calls that would write, mutate or emit through the task store, a file or the crew
#: log. ``append`` and ``replace`` are not listed: here they are a list's and a
#: datetime's methods, and the crew log's own ``append`` is kept out by the import
#: allowlist in ``test_the_crew_log_is_reached_only_through_its_read_surface``.
_MUTATING_CALLS = {
    "write",
    "write_text",
    "write_bytes",
    "unlink",
    "rename",
    "execute",
    "executemany",
    "commit",
    "accept",
    "claim",
    "transition",
    "cancel",
    "reconcile_unknown",
    "defer",
    "wake_wait",
    "record_progress",
    "open",
    "emit",
    "dump",
    "remove",
    "rmtree",
}


def _timeline_tree() -> ast.Module:
    return ast.parse((source_corpus.src_root() / _TIMELINE).read_text(encoding="utf-8"))


def test_the_timeline_is_a_read_only_leaf_with_no_scrubber_of_its_own() -> None:
    tree = _timeline_tree()
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module != "":
            imported.add(node.module)
        elif isinstance(node, ast.If):  # the TYPE_CHECKING block
            for inner in node.body:
                if isinstance(inner, ast.ImportFrom) and inner.module:
                    imported.add(inner.module)
    extra = imported - _ALLOWED_IMPORTS - {"kiro_crew.taskq.store"}
    assert not extra, f"timeline.py imports {sorted(extra)}; it is a leaf under taskq"
    names = {
        n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not {n for n in names if any(w in n.lower() for w in ("scrub", "redact", "mask"))}, names
    source = (source_corpus.src_root() / _TIMELINE).read_text(encoding="utf-8")
    assert "kiro_crew.security" not in source and "redact_and_truncate" not in source


def test_the_timeline_makes_no_mutating_call_and_never_opens_a_file() -> None:
    calls = {
        getattr(n.func, "attr", getattr(n.func, "id", ""))
        for n in ast.walk(_timeline_tree())
        if isinstance(n, ast.Call)
    }
    assert not (calls & _MUTATING_CALLS), sorted(calls & _MUTATING_CALLS)


def test_the_crew_log_is_reached_only_through_its_read_surface() -> None:
    tree = _timeline_tree()
    pulled: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "kiro_crew.crew_log"
        ):
            pulled |= {f"{node.module}.{alias.name}" for alias in node.names}
    assert pulled == {
        "kiro_crew.crew_log.projection.fold_session",
        "kiro_crew.crew_log.read.read_page",
        "kiro_crew.crew_log.resolve.unit_holding_child",
    }, sorted(pulled)


def test_timeline_py_is_the_only_reader_added_and_the_route_the_only_caller() -> None:
    root = source_corpus.src_root()
    renderers: set[str] = set()
    readers: set[str] = set()
    sites = 0
    for path, _text, tree in source_corpus.parsed_candidates(require_all=("timeline",)):
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name == "render_timeline":
                renderers.add(rel)
                sites += 1
            if name == "read_child_entries":
                readers.add(rel)
    assert renderers == _RENDER_CALLERS, f"render_timeline is called from {sorted(renderers)}"
    assert sites == 1, f"render_timeline has {sites} call expressions in src/; the contract is 1"
    assert readers <= {
        _TIMELINE
    }, f"a second reader of a child's crew-log entries: {sorted(readers)}"
    assert (root / _TIMELINE).is_file()


def test_the_task_events_tail_is_the_detail_routes_bound() -> None:
    source = (source_corpus.src_root() / "dashboard/handlers/tasks.py").read_text(encoding="utf-8")
    assert "events_limit=_EVENTS_TAIL" in source
    assert tasks_mod._EVENTS_TAIL == 200

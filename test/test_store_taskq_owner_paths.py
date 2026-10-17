"""The failure arms of the store's moved writes and reads, and of the deferral beside them.

The writes ``TaskStore`` binds from its ``store_*`` owners, like the deferral writes it
keeps beside ``claim``, run in one ``BEGIN IMMEDIATE`` transaction and convert a database
failure into ``TaskStoreUnavailable``. These tests make the ``COMMIT`` itself fail AFTER the
transaction's statements ran, so the only thing that can leave the row as it was is the
rollback: a rollback that committed instead would leave the write in place and turn each
test red. The reads' fallbacks are driven with rows whose stored JSON is not JSON, the
shape a half-written or hand-edited file leaves behind.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from overload_fakes import Clock, open_task_store

from kiro_crew.taskq import model
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.taskq.waits import WaitRecord


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> Iterator[TaskStore]:
    yield from open_task_store(tmp_path, clock, name="tasks/tasks.db", window=4)


class _CommitFails:
    """The store's connection, with every ``COMMIT`` refused after the statements before
    it ran; everything else, ``ROLLBACK`` included, reaches the real connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *args: Any) -> Any:
        if sql == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args)


@contextmanager
def _failing_commit(store: TaskStore) -> Iterator[None]:
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(store, "_conn", _CommitFails(store._conn))
        yield


def _accept(store: TaskStore, task_id: str) -> None:
    store.accept_one(
        model.TaskRecord(id=task_id, kind=model.KIND_SUBAGENT, params={"task": task_id})
    )


def _waiting(store: TaskStore, clock: Clock, task_id: str) -> int:
    """Walk a row to ``waiting_input`` through the real transitions; its generation."""
    _accept(store, task_id)
    assert store.claim(task_id) is not None
    assert store.transition(task_id, model.STARTING)
    assert store.transition(task_id, model.RUNNING)
    assert store.enter_wait(task_id, WaitRecord.input(f"call-{task_id}", since=clock.t).to_dict())
    row = store.get(task_id)
    assert row is not None and row.state == model.WAITING_INPUT
    return row.generation


def _kinds(store: TaskStore, task_id: str) -> list[str]:
    return [event.kind for event in store.events(task_id)]


def _rewrite_event_data(store: TaskStore, task_id: str, kind: str, data_json: str) -> None:
    """Overwrite the stored JSON of *task_id*'s *kind* events through a second
    connection, the way another writer on the same file would."""
    conn = sqlite3.connect(str(store.path), timeout=5.0)
    try:
        conn.execute(
            "UPDATE task_events SET data_json=? WHERE task_id=? AND kind=?",
            (data_json, task_id, kind),
        )
        conn.commit()
    finally:
        conn.close()


# ── store_events ──────────────────────────────────────────────────────────────


def test_an_event_append_whose_commit_fails_leaves_no_event(store: TaskStore) -> None:
    _accept(store, "e1")
    before = _kinds(store, "e1")
    with _failing_commit(store):
        with pytest.raises(TaskStoreUnavailable, match="task event write failed"):
            store.append_event("e1", "progress", {"step": 1})
    assert _kinds(store, "e1") == before
    store.append_event("e1", "progress", {"step": 2})
    assert _kinds(store, "e1") == [*before, "progress"]


def test_an_event_whose_stored_json_is_unreadable_reads_back_empty(store: TaskStore) -> None:
    _accept(store, "e2")
    store.append_event("e2", "progress", {"step": 1})
    _rewrite_event_data(store, "e2", "progress", "{not json")
    (event,) = [e for e in store.events("e2") if e.kind == "progress"]
    assert event.data == {}
    latest = store.latest_events(["e2"], ["progress"])
    assert latest[("e2", "progress")].data == {}


def test_an_event_whose_stored_json_is_not_an_object_reads_back_empty(store: TaskStore) -> None:
    _accept(store, "e3")
    store.append_event("e3", "progress", {"step": 1})
    _rewrite_event_data(store, "e3", "progress", json.dumps(["a", "list"]))
    (event,) = [e for e in store.events("e3") if e.kind == "progress"]
    assert event.data == {}
    assert store.latest_events(["e3"], ["progress"])[("e3", "progress")].data == {}


def test_the_newest_events_of_no_rows_or_no_kinds_are_none(store: TaskStore) -> None:
    _accept(store, "e4")
    assert store.latest_events([], ["accepted"]) == {}
    assert store.latest_events(["e4"], []) == {}
    assert ("e4", "accepted") in store.latest_events(["e4", ""], ["accepted"])


# ── the deferral writes (store.py) ────────────────────────────────────────────


def test_a_deferral_whose_commit_fails_keeps_the_row_eligible(store: TaskStore) -> None:
    _accept(store, "d1")
    with _failing_commit(store):
        with pytest.raises(TaskStoreUnavailable, match="task defer failed"):
            store.defer("d1", wait=60.0, reason="memory")
    row = store.get("d1")
    assert row is not None and row.next_run_at is None
    assert "deferred" not in _kinds(store, "d1")
    assert [r.id for r in store.fetch_dispatchable(model.KIND_SUBAGENT, limit=4)] == ["d1"]


def test_an_expedite_whose_commit_fails_keeps_the_deferral(store: TaskStore, clock: Clock) -> None:
    _accept(store, "d2")
    assert store.defer("d2", wait=60.0, reason="memory")
    until = clock.t + 60.0
    with _failing_commit(store):
        with pytest.raises(TaskStoreUnavailable, match="task expedite failed"):
            store.expedite(["d2"])
    row = store.get("d2")
    assert row is not None and row.next_run_at == until
    assert store.fetch_dispatchable(model.KIND_SUBAGENT, limit=4) == []


def test_a_deferral_whose_until_is_unreadable_counts_no_parked_time(
    store: TaskStore, clock: Clock
) -> None:
    _accept(store, "d3")
    _accept(store, "d4")
    assert store.defer("d3", wait=600.0, reason="memory")
    assert store.defer("d4", wait=600.0, reason="memory")
    _rewrite_event_data(store, "d3", "deferred", "{not json")
    clock.advance(120.0)
    parked = store.deferred_longer_than(model.KIND_SUBAGENT, 60.0)
    assert [r.id for r in parked] == ["d4"]


# ── store_waits ───────────────────────────────────────────────────────────────


def test_a_wait_update_whose_commit_fails_keeps_the_record(store: TaskStore, clock: Clock) -> None:
    generation = _waiting(store, clock, "w1")
    before = store.get("w1")
    assert before is not None
    rewritten = WaitRecord.input("call-other", since=clock.t).to_dict()
    with _failing_commit(store):
        with pytest.raises(TaskStoreUnavailable, match="wait update failed"):
            store.update_wait("w1", rewritten, generation=generation)
    after = store.get("w1")
    assert after is not None and after.wait == before.wait
    assert "wait_updated" not in _kinds(store, "w1")


def test_a_wake_whose_commit_fails_keeps_the_row_waiting(store: TaskStore, clock: Clock) -> None:
    generation = _waiting(store, clock, "w2")
    with _failing_commit(store):
        with pytest.raises(TaskStoreUnavailable, match="task wake failed"):
            store.wake_wait("w2", reason="answered", generation=generation)
    row = store.get("w2")
    assert row is not None
    assert (row.state, row.generation) == (model.WAITING_INPUT, generation)
    assert "wake" not in _kinds(store, "w2")
    assert store.is_unstarted("w2") is False
    assert store.wake_wait("w2", reason="answered", generation=generation) == generation + 1

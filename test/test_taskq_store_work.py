"""Accepted-work rows in tasks.db: idempotent insert, compare-and-set, filters, purge, ledger."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable, WorkRow


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TaskStore]:
    s = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    try:
        yield s
    finally:
        s.close()


def _row(work_id: str = "w1", **kw: object) -> WorkRow:
    base: dict[str, object] = dict(
        work_id=work_id,
        scope=f"scope-{work_id}",
        budget_key="p1",
        session_key="chat-1",
        state="queued",
        revision=0,
        accepted_at=100.0,
        updated_at=100.0,
        record_json='{"v":0}',
        payload_json='{"prompt":"hi"}',
    )
    base.update(kw)
    return WorkRow(**base)  # type: ignore[arg-type]


def test_tables_exist_on_open(tmp_path: Path) -> None:
    s = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    s.close()
    conn = sqlite3.connect(tmp_path / "tasks.db")
    counts = [
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("accepted_work", "principal_storage")
    ]
    conn.close()
    assert counts == [0, 0]


def test_insert_is_idempotent_on_scope(store: TaskStore) -> None:
    first, inserted = store.work_insert_or_get(_row("w1"))
    assert inserted and first == _row("w1")
    again, inserted = store.work_insert_or_get(_row("w2", scope="scope-w1"))
    assert not inserted
    assert again.work_id == "w1"
    assert store.work_by_scope("scope-w1") == "w1"
    assert store.work_by_scope("missing") is None
    assert store.work_get("w2") is None


def test_duplicate_work_id_is_a_store_error(store: TaskStore) -> None:
    store.work_insert_or_get(_row("w1"))
    with pytest.raises(TaskStoreUnavailable):
        store.work_insert_or_get(_row("w1", scope="other"))


def test_compare_and_set_refuses_a_stale_revision(store: TaskStore) -> None:
    store.work_insert_or_get(_row("w1"))
    assert store.work_compare_and_set(
        "w1", expected_revision=0, state="running", revision=1, updated_at=110.0, record_json="{}"
    )
    assert not store.work_compare_and_set(
        "w1", expected_revision=0, state="failed", revision=1, updated_at=120.0, record_json="{}"
    )
    got = store.work_get("w1")
    assert got is not None and got.state == "running" and got.revision == 1
    assert got.payload_json == '{"prompt":"hi"}'
    assert store.work_compare_and_set(
        "w1",
        expected_revision=1,
        state="completed",
        revision=2,
        updated_at=130.0,
        record_json="{}",
        clear_payload=True,
    )
    got = store.work_get("w1")
    assert got is not None and got.payload_json is None and got.updated_at == 130.0


def test_rows_filter_and_order(store: TaskStore) -> None:
    store.work_insert_or_get(_row("b", accepted_at=200.0, budget_key="p2"))
    store.work_insert_or_get(_row("a", accepted_at=100.0, session_key="chat-2"))
    store.work_insert_or_get(_row("c", accepted_at=300.0, state="running"))
    assert [r.work_id for r in store.work_rows()] == ["a", "b", "c"]
    assert [r.work_id for r in store.work_rows(states=["running"])] == ["c"]
    assert store.work_rows(states=[]) == []
    assert [r.work_id for r in store.work_rows(session_key="chat-2")] == ["a"]
    assert [r.work_id for r in store.work_rows(budget_key="p2")] == ["b"]
    assert [r.work_id for r in store.work_rows(limit=1)] == ["a"]


def test_purge_removes_only_old_rows_in_named_states(store: TaskStore) -> None:
    store.work_insert_or_get(_row("old", state="completed", updated_at=10.0))
    store.work_insert_or_get(_row("new", state="completed", updated_at=500.0))
    store.work_insert_or_get(_row("live", state="running", updated_at=10.0))
    assert store.work_purge(states=[], updated_before=1000.0) == 0
    assert store.work_purge(states=["completed"], updated_before=100.0) == 1
    assert {r.work_id for r in store.work_rows()} == {"new", "live"}


def test_storage_ledger_never_goes_below_zero(store: TaskStore) -> None:
    assert store.storage_bytes("p1") == 0
    assert store.storage_charge("p1", 100, at=1.0) == 100
    assert store.storage_charge("p1", 50, at=2.0) == 150
    assert store.storage_charge("p1", -1000, at=3.0) == 0
    assert store.storage_charge("p2", -5, at=4.0) == 0
    assert store.storage_bytes("p1") == 0

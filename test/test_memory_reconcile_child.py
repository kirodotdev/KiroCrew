"""The FULL memory metadata pass runs in a child process, with an in-process fallback."""

import logging
import shutil
import threading

import pytest

from kiro_crew import memory_reconcile_child as child
from kiro_crew import memory_record_metadata as meta
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.vector_memory import VectorMemoryStore


@pytest.fixture
def path(tmp_path):
    return tmp_path / "memory.db"


def _open(path):
    store = VectorMemoryStore(db_path=path, embedding_dim=2)
    store.init()
    return store


def _upgraded_store(path, rows=30):
    """A populated store with no marker, as after an upgrade."""
    store = _open(path)
    for i in range(rows):
        store.set_semantic(f"user.k{i}", f"v{i}", 1, "user_explicit")
    store.close()
    db = sqlite3.connect(path)
    db.execute("DELETE FROM memory_reconcile_state")
    db.execute("DELETE FROM memory_record_meta")
    db.commit()
    db.close()


def _snapshot(path):
    db = sqlite3.connect(path)
    try:
        rows = db.execute(
            "SELECT record_id, content_hash FROM memory_record_meta ORDER BY record_id"
        ).fetchall()
        marker = db.execute(
            "SELECT relation, version, layout FROM memory_reconcile_state ORDER BY relation"
        ).fetchall()
        revisions = db.execute(
            "SELECT record_id, operation, source, status FROM memory_revisions ORDER BY record_id, id"
        ).fetchall()
        return rows, marker, revisions
    finally:
        db.close()


def test_full_pass_in_a_real_child_matches_the_in_process_pass(path, tmp_path):
    other = tmp_path / "other.db"
    _upgraded_store(path)
    shutil.copyfile(path, other)  # same rows, so the hashes can match
    assert child.run_full_pass_in_child(str(path)) is True
    db = sqlite3.connect(other)
    with db:
        meta.reconcile(db, full=True)
    db.close()
    rows, marker, revisions = _snapshot(path)
    assert rows and marker and revisions
    assert (rows, marker, revisions) == _snapshot(other)


def test_the_full_pass_finishes_while_the_parent_runs_a_busy_thread(path):
    _upgraded_store(path)
    stop = threading.Event()
    counter = [0]

    def spin():
        while not stop.is_set():
            counter[0] += 1

    thread = threading.Thread(target=spin)
    thread.start()
    try:
        assert child.run_full_pass_in_child(str(path)) is True
        seen = counter[0]
        while counter[0] == seen:  # the GIL-bound loop is still making progress
            pass
    finally:
        stop.set()
        thread.join()
    assert _snapshot(path)[1]


def test_a_store_open_boots_through_the_child_and_marks_it(path, monkeypatch):
    _upgraded_store(path)
    calls = []
    real = child.run_full_pass_in_child
    monkeypatch.setattr(
        child, "run_full_pass_in_child", lambda p, **kw: calls.append(p) or real(p, **kw)
    )
    _open(path).close()
    assert calls == [str(path)]
    assert _snapshot(path)[1]
    _open(path).close()
    assert len(calls) == 1  # marked: the second boot stays in process


def test_a_crashing_child_falls_back_in_process_with_one_warning(path, monkeypatch, caplog):
    _upgraded_store(path)
    monkeypatch.setattr(child, "run_child", _die)
    with caplog.at_level(logging.WARNING, logger=child.logger.name):
        _open(path).close()
    warnings = [r for r in caplog.records if r.name == child.logger.name]
    assert len(warnings) == 1 and "exited with code" in warnings[0].getMessage()
    assert _snapshot(path)[1]  # the in-process pass installed the marker


def _die(db_path, conn=None):
    raise SystemExit(3)


def test_a_child_that_cannot_start_falls_back_in_process(path, monkeypatch, caplog):
    _upgraded_store(path)

    def refuse(*_a, **_k):
        raise OSError("no processes")

    monkeypatch.setattr(child.multiprocessing, "get_context", refuse)
    with caplog.at_level(logging.WARNING, logger=child.logger.name):
        _open(path).close()
    assert [r.getMessage() for r in caplog.records if r.name == child.logger.name] == [
        "Memory reconcile child could not start (OSError: no processes); running the pass in process"
    ]
    assert _snapshot(path)[1]


def test_a_timed_out_child_is_terminated_before_the_fallback(path, monkeypatch, caplog):
    _upgraded_store(path)
    monkeypatch.setattr(child, "run_child", _hang)
    with caplog.at_level(logging.WARNING, logger=child.logger.name):
        assert child.run_full_pass_in_child(str(path), timeout=1.0) is False
    assert "timed out" in caplog.records[-1].getMessage()
    assert not [p for p in __import__("multiprocessing").active_children() if "reconcile" in p.name]


def _hang(db_path, conn=None):
    threading.Event().wait()


def test_a_busy_write_lock_takes_the_busy_path_not_the_fallback(path, caplog):
    _upgraded_store(path)
    # Rows already carry metadata, so the child only reads; the marker waits.
    _open_rows_marked(path)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with caplog.at_level(logging.WARNING, logger=child.logger.name):
            assert child.run_full_pass_in_child(str(path)) is True
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert not [r for r in caplog.records if r.name == child.logger.name]
    assert _snapshot(path)[1] == []  # no marker: the next start repeats the pass


def _open_rows_marked(path):
    """Give the rows their metadata without installing the marker."""
    db = sqlite3.connect(path)
    with db:
        meta.reconcile(db, full=True)
        db.execute("DELETE FROM memory_reconcile_state")
    db.close()


def test_the_passes_own_warnings_reach_the_parent_log(path, caplog):
    _upgraded_store(path)
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO episodic_memories (id, text, created_at) VALUES ('', 'b', '2026-01-01')"
    )
    db.commit()
    db.close()
    with caplog.at_level("WARNING", logger=meta.__name__):
        assert child.run_full_pass_in_child(str(path)) is True
    assert "skipped 1 episodic_memories row(s) with no id" in caplog.text

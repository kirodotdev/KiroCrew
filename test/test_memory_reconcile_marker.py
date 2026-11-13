"""The startup metadata reconcile checks only changed rows once a full pass has run."""

import pytest

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


def _count_hashes(monkeypatch):
    calls = []
    real = meta.content_hash

    def counted(row):
        calls.append(1)
        return real(row)

    monkeypatch.setattr(meta, "content_hash", counted)
    return calls


def _seed(path, rows=20):
    store = _open(path)
    for i in range(rows):
        store.set_semantic(f"user.k{i}", f"v{i}", 1, "user_explicit")
    store.close()


def _raw(path, sql, params=()):
    db = sqlite3.connect(path)
    try:
        db.execute(sql, params)
        db.commit()
    finally:
        db.close()


def _boot_hashes(path, monkeypatch):
    calls = _count_hashes(monkeypatch)
    _open(path).close()
    return len(calls)


def _meta(path, key):
    db = sqlite3.connect(path)
    try:
        return meta.get_record_metadata(db, f"key:{key}")
    finally:
        db.close()


def test_unchanged_rows_are_not_hashed_on_the_second_boot(path, monkeypatch):
    _seed(path)
    _open(path).close()  # completes a full pass and installs the marker
    assert _boot_hashes(path, monkeypatch) == 0


def test_first_boot_with_no_marker_runs_the_full_pass(path, monkeypatch):
    _seed(path)
    assert _boot_hashes(path, monkeypatch) == 20


def test_empty_store_installs_no_marker(path):
    _open(path).close()
    db = sqlite3.connect(path)
    found = db.execute(
        "SELECT count(*) FROM sqlite_schema WHERE name LIKE 'memory_reconcile%'"
    ).fetchone()[0]
    db.close()
    assert found == 0


def test_a_writer_holding_the_lock_does_not_fail_the_open(path, monkeypatch):
    _seed(path)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert _boot_hashes(path, monkeypatch) == 20  # scanned; the marker waits
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _boot_hashes(path, monkeypatch) == 20  # no marker yet, so another full pass
    assert _boot_hashes(path, monkeypatch) == 0


def test_same_length_same_updated_at_edit_is_caught(path, monkeypatch):
    _seed(path)
    _open(path).close()
    _raw(path, "UPDATE semantic_memory SET value_json='\"w3\"' WHERE key='user.k3'")
    assert _boot_hashes(path, monkeypatch) == 2  # the check, then the journaling write
    assert _meta(path, "user.k3")["revision"] == 2


def test_insert_after_the_marker_is_caught(path, monkeypatch):
    _seed(path)
    _open(path).close()
    _raw(
        path,
        "INSERT INTO semantic_memory (key, value_json, source, created_at, updated_at) "
        "VALUES ('user.new', '\"x\"', 'old', 't', 't')",
    )
    assert _boot_hashes(path, monkeypatch) == 2
    assert _meta(path, "user.new")["revision"] == 1


def test_hard_delete_is_a_no_op_like_the_full_pass(path, monkeypatch):
    _seed(path)
    _open(path).close()
    _raw(path, "DELETE FROM semantic_memory WHERE key='user.k3'")
    assert _boot_hashes(path, monkeypatch) == 0
    assert _boot_hashes(path, monkeypatch) == 0


def test_journaled_write_is_rechecked_once_then_cleared(path, monkeypatch):
    _seed(path)
    store = _open(path)
    store.set_semantic("user.k1", "again", 1, "user_explicit")
    store.close()
    assert _boot_hashes(path, monkeypatch) == 1
    assert _boot_hashes(path, monkeypatch) == 0


@pytest.mark.parametrize(
    "damage",
    [
        "DELETE FROM memory_reconcile_state",
        "UPDATE memory_reconcile_state SET version='0.0.0'",
        "UPDATE memory_reconcile_state SET layout='x'",
        "DROP TRIGGER memory_reconcile_semantic_memory_upd",
        "DROP TABLE memory_reconcile_dirty",
    ],
    ids=["missing", "version", "corrupt", "no-trigger", "no-dirty"],
)
def test_untrusted_marker_forces_the_full_pass(path, monkeypatch, damage):
    _seed(path)
    _open(path).close()
    _raw(path, damage)
    assert _boot_hashes(path, monkeypatch) == 20
    assert _boot_hashes(path, monkeypatch) == 0  # and the marker is rebuilt


def test_invalidate_marker_forces_the_full_pass(path, monkeypatch):
    _seed(path)
    _open(path).close()
    db = sqlite3.connect(path)
    meta.invalidate_marker(db)
    db.commit()
    db.close()
    assert _boot_hashes(path, monkeypatch) == 20


def test_doctor_flag_runs_the_full_pass(path, monkeypatch, capsys):
    from kiro_crew import cli_doctor, memory_stores

    _seed(path)
    _open(path).close()
    monkeypatch.setattr(memory_stores, "resolve_store_path", lambda _store: path)
    calls = _count_hashes(monkeypatch)
    cli_doctor._reconcile_memory()
    assert len(calls) == 20
    assert "every row checked" in capsys.readouterr().out


def test_full_flag_runs_the_full_pass(path, monkeypatch):
    _seed(path)
    _open(path).close()
    calls = _count_hashes(monkeypatch)
    db = sqlite3.connect(path)
    with db:
        meta.reconcile(db, full=True)
    db.close()
    assert len(calls) == 20


def _crew_db():
    from kiro_crew import memory_schema

    db = sqlite3.connect(":memory:")
    db.executescript(memory_schema.CREW_SCHEMA_SQL)
    meta.ensure_schema(db)
    for i in range(5):
        db.execute(
            "INSERT INTO memory_items (id, kind, key, text, value_json, source, created_at, "
            "updated_at) VALUES (?, 'fact', ?, 't', '\"a\"', 's', 't', 't')",
            (f"key:k{i}", f"k{i}"),
        )
        db.execute(
            "INSERT INTO memory_items (id, kind, text, source, created_at, updated_at) "
            "VALUES (?, 'episode', 'e', 's', 't', 't')",
            (f"ep{i}",),
        )
    db.commit()
    return db


def test_crew_lineage_views_are_covered_through_memory_items(monkeypatch):
    db = _crew_db()
    with db:
        assert meta.reconcile(db) == 10
    calls = _count_hashes(monkeypatch)
    with db:
        assert meta.reconcile(db) == 0
    assert calls == []
    db.execute("UPDATE memory_items SET value_json='\"b\"' WHERE key='k2'")
    db.execute("UPDATE memory_items SET text='f' WHERE id='ep3'")
    db.execute("UPDATE memory_items SET last_accessed_at='now' WHERE id='ep1'")
    with db:
        assert meta.reconcile(db) == 2
    assert len(calls) == 4  # two checks plus two journaling writes; the access touch is skipped

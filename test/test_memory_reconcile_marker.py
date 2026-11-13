"""The startup metadata reconcile checks only changed rows once a full pass has run."""

import pytest

from kiro_crew import memory_record_metadata as meta
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.vector_memory import VectorMemoryStore


@pytest.fixture(autouse=True)
def _clear_notes_every_start(_floor_monkeypatch):
    _floor_monkeypatch.setattr(meta, "_DIRTY_KEEP", 0)


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
    _raw(path, "DELETE FROM memory_reconcile_state")
    assert _boot_hashes(path, monkeypatch) == 20


def test_a_fresh_store_is_born_marked(path):
    _open(path).close()
    db = sqlite3.connect(path)
    marked = db.execute("SELECT count(*) FROM memory_reconcile_state").fetchone()[0]
    triggers = db.execute(
        "SELECT count(*) FROM sqlite_schema WHERE type='trigger' AND name LIKE 'memory_reconcile%'"
    ).fetchone()[0]
    db.close()
    assert (marked, triggers) == (2, 4)


def test_a_writer_holding_the_lock_does_not_fail_the_open(path, monkeypatch):
    _seed(path)
    _raw(path, "DELETE FROM memory_reconcile_state")  # an upgraded store: rows, no marker
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


def _dirty(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT count(*) FROM memory_reconcile_dirty").fetchone()[0]
    finally:
        db.close()


def test_a_busy_writer_is_not_waited_for_and_the_notes_wait(path, monkeypatch):
    _seed(path)
    _open(path).close()  # marker installed, nothing noted
    store = _open(path)
    store.set_semantic("user.k1", "changed", 1, "user_explicit")
    store.close()
    assert _dirty(path) == 1

    seen = []
    real = meta._take_write_lock

    def spy(db):
        statements = []
        db.set_trace_callback(statements.append)
        try:
            got = real(db)
        finally:
            db.set_trace_callback(None)
        seen.append((got, statements))
        return got

    with monkeypatch.context() as mp:
        mp.setattr(meta, "_take_write_lock", spy)
        holder = sqlite3.connect(path, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        blocked = VectorMemoryStore(db_path=path, embedding_dim=2)
        try:
            blocked.init()
        finally:
            holder.execute("ROLLBACK")
            holder.close()
            blocked.close()
    ((got, statements),) = seen
    assert got is False
    # The wait was switched off before the lock was asked for.
    assert statements.index("PRAGMA busy_timeout=0") < statements.index("BEGIN IMMEDIATE")
    assert _dirty(path) == 1  # left for the next start
    _open(path).close()
    assert _dirty(path) == 0


def test_a_few_noted_rows_are_rechecked_without_a_write(path, monkeypatch):
    _seed(path)
    _open(path).close()
    monkeypatch.setattr(meta, "_DIRTY_KEEP", 500)
    store = _open(path)
    store.set_semantic("user.k1", "changed", 1, "user_explicit")
    store.close()
    assert _boot_hashes(path, monkeypatch) == 1
    assert _dirty(path) == 1


def test_doctor_repair_leaves_the_wal_files_for_other_writers(path, monkeypatch):
    from pathlib import Path

    from kiro_crew import cli_doctor, memory_stores

    _seed(path)
    _open(path).close()
    monkeypatch.setattr(memory_stores, "resolve_store_path", lambda _store: path)
    cli_doctor._reconcile_memory()
    assert Path(f"{path}-wal").exists()
    assert Path(f"{path}-shm").exists()


def test_a_rebuilt_metadata_table_forces_the_full_pass(path, monkeypatch):
    _seed(path)
    _open(path).close()  # marker installed, nothing noted
    assert _dirty(path) == 0
    _raw(path, "DROP TABLE memory_record_meta")

    assert _boot_hashes(path, monkeypatch) == 40  # every row: the check, then its journaling write
    assert _meta(path, "user.k3")["revision"] == 1
    assert _boot_hashes(path, monkeypatch) == 0  # incremental again


def test_the_egress_redactor_strips_the_reconcile_state_and_a_restore_rescans(path, monkeypatch):
    from kiro_crew import snapshot_redact

    rewritten_name = "AKIAIOSFODNN7EXAMPLE"
    store = _open(path)
    store.set_semantic(f"user.{rewritten_name}", "x", 1, "user_explicit")
    store.set_semantic("user.k1", "plain", 1, "user_explicit")
    store.close()
    assert _dirty(path) >= 1  # a note is held for the row the redactor will rewrite
    stage = path.parent / "stage"
    stage.mkdir()
    staged = stage / "memory.db"
    src = sqlite3.connect(path)
    dst = sqlite3.connect(staged)
    src.backup(dst)
    src.close()
    dst.close()

    snapshot_redact.redact_bundle_for_egress(stage)

    db = sqlite3.connect(staged)
    left = db.execute(
        "SELECT name FROM sqlite_schema WHERE name LIKE 'memory_reconcile%'"
    ).fetchall()
    db.close()
    assert left == []
    rows = sqlite3.connect(staged).execute("SELECT count(*) FROM semantic_memory").fetchone()[0]
    # No marker: the restored copy is fully rescanned, every row.
    assert _boot_hashes(staged, monkeypatch) == rows
    assert _boot_hashes(staged, monkeypatch) == 0


def test_an_empty_marked_store_that_lost_its_notes_table_is_repaired(path, monkeypatch):
    _open(path).close()  # empty and born marked
    _raw(path, "DROP TABLE memory_reconcile_dirty")

    _open(path).close()

    assert _dirty(path) == 0  # the table is back
    store = _open(path)
    assert store.set_semantic("user.k1", "v", 1, "user_explicit") is None
    store.close()


def test_doctor_repairs_an_empty_store_that_lost_its_notes_table(path, monkeypatch, capsys):
    from kiro_crew import cli_doctor, memory_stores

    _open(path).close()
    _raw(path, "DROP TABLE memory_reconcile_dirty")
    monkeypatch.setattr(memory_stores, "resolve_store_path", lambda _store: path)

    cli_doctor._reconcile_memory()

    assert _dirty(path) == 0
    assert "every row checked" in capsys.readouterr().out


def test_the_off_host_snapshot_upload_proceeds_for_a_store_holding_a_redacted_key(
    path, tmp_path, monkeypatch
):
    import json
    import tarfile

    from kiro_crew import snapshot, snapshot_redact

    monkeypatch.setattr(snapshot_redact, "outbound_redaction_enabled", lambda: True)
    rewritten_key = "user.ghp_" + "A" * 36
    store = _open(path)
    store.set_semantic(rewritten_key, "a value", 1, "user_explicit")
    store.set_semantic("user.k1", "plain", 1, "user_explicit")
    store.close()
    assert _dirty(path) >= 1  # a note is held for the key the redactor rewrites

    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir()
    (payload / "MANIFEST.json").write_text(
        json.dumps({"version": 3, "components": {"memory": "unresolved"}}), encoding="utf-8"
    )
    (payload / "memory.db").write_bytes(path.read_bytes())
    bundle = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    work = tmp_path / "work"
    work.mkdir()

    redacted = snapshot.prepare_redacted_copy(bundle, work, ["memory"])

    assert redacted is not None  # the upload is not refused as unprovable
    with tarfile.open(redacted) as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith("memory.db"))
        data = tf.extractfile(member).read()
    assert b"ghp_" + b"A" * 36 not in data
    staged = tmp_path / "out.db"
    staged.write_bytes(data)
    db = sqlite3.connect(staged)
    left = db.execute("SELECT count(*) FROM sqlite_schema WHERE name LIKE 'memory_reconcile%'")
    assert left.fetchone()[0] == 0
    db.close()


def test_an_empty_store_without_a_marker_opens_past_a_busy_writer_and_is_marked_later(
    path, monkeypatch
):
    store = _open(path)
    store.close()
    db = sqlite3.connect(path)
    meta.strip_reconcile_state(db)  # an empty store from before the marker existed
    db.commit()
    db.close()

    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    seen = []
    blocked = VectorMemoryStore(db_path=path, embedding_dim=2)
    real = meta._take_write_lock

    def spy(conn):
        got = real(conn)
        seen.append(got)
        return got

    try:
        with monkeypatch.context() as mp:
            mp.setattr(meta, "_take_write_lock", spy)
            blocked.init()  # a 5 s wait that raised would fail here
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        blocked.close()
    assert seen == [False]

    _open(path).close()
    db = sqlite3.connect(path)
    marked = db.execute("SELECT count(*) FROM memory_reconcile_state").fetchone()[0]
    db.close()
    assert marked == 2


def test_doctor_repair_builds_the_metadata_tables_of_a_legacy_database(path, monkeypatch, capsys):
    from kiro_crew import cli_doctor, memory_stores

    _seed(path)
    db = sqlite3.connect(path)
    meta.strip_reconcile_state(db)
    for name, _statement in meta._SCHEMA_OBJECTS:
        kind = "TABLE" if name.startswith("memory_") and "idx" not in name else "INDEX"
        db.execute(f"DROP {kind} IF EXISTS {name}")
    db.commit()
    assert not meta.schema_is_current(db)  # a V1 file from before the metadata tables
    db.close()
    monkeypatch.setattr(memory_stores, "resolve_store_path", lambda _store: path)

    cli_doctor._reconcile_memory()

    assert "every row checked, 20 journaled" in capsys.readouterr().out
    assert _meta(path, "user.k3")["revision"] == 1

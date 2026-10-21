"""A store's first open builds its schema in ONE transaction.

Outside a transaction every DDL statement is its own FULL-sync commit, so building a
fresh file statement by statement pays one fsync per statement. Under heavy parallel
I/O that is enough for a per-test store's setup to outrun the test timeout. One
transaction also means a failure part-way through leaves no half-built schema behind.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

import pytest

import kiro_crew.vector_memory as vector_memory
import kiro_crew.vector_memory_lifecycle as lifecycle
from kiro_crew import memory_schema

# The SQLite library the store opens its file with (the bundled pysqlite3 where it
# is installed). Every connection here must use it: a second library in this process
# keeps its own lock table, so the store would not see that connection's locks, and
# its exceptions are unrelated classes.
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.memory_stores import memory_stores_root
from kiro_crew.vector_memory import VectorMemoryStore

#: (where the file lives, lineage it is born with). A named store's file under the
#: data home's ``memory_stores/`` takes the crew lineage; any other file takes v1.
_FRESH_FILES = (
    pytest.param(lambda tmp_path: tmp_path / "memory.db", memory_schema.LINEAGE_V1, id="v1"),
    pytest.param(
        lambda tmp_path: memory_stores_root() / "probe" / "memory.db",
        memory_schema.LINEAGE_CREW,
        id="crew",
    ),
)


def _wal_commits(db_path: Path) -> int:
    """Count the commit frames in *db_path*'s WAL: one per durable commit.

    Assumes a WAL written since the file was created: frames left from an earlier
    generation after a WAL reset would also be counted (their salts are not checked).
    """
    wal = Path(f"{db_path}-wal")
    if not wal.exists():
        return 0
    data = wal.read_bytes()
    if len(data) < 32:
        return 0
    page_size = struct.unpack(">I", data[8:12])[0]
    commits, offset = 0, 32
    while offset + 24 <= len(data):
        # Frame header bytes 4..8: the database size after a commit, 0 otherwise.
        if struct.unpack(">I", data[offset + 4 : offset + 8])[0]:
            commits += 1
        offset += 24 + page_size
    return commits


def _expected_versions(lineage: str) -> set[int]:
    migrations = (
        memory_schema.MIGRATIONS_CREW
        if lineage == memory_schema.LINEAGE_CREW
        else vector_memory._MIGRATIONS
    )
    return {ver for ver, _sql, _fn in migrations}


def _tables(db_path: Path) -> set[str]:
    probe = sqlite3.connect(db_path)
    try:
        return {
            row[0] for row in probe.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
    finally:
        probe.close()


@pytest.mark.parametrize(("where", "lineage"), _FRESH_FILES)
def test_a_fresh_store_builds_its_schema_in_one_commit(tmp_path, where, lineage):
    db_path = where(tmp_path)
    store = VectorMemoryStore(db_path=db_path)
    store.init()
    try:
        assert store._lineage == lineage
        # The WAL is checkpointed away only when the last connection closes, so
        # while the store is open it still holds every commit init made.
        assert _wal_commits(db_path) == 1
        versions = {row[0] for row in store.db.execute("SELECT version FROM schema_version")}
        assert versions == _expected_versions(lineage)
        assert {"memory_record_meta", "memory_revisions"} <= _tables(db_path)
    finally:
        store.close()


@pytest.mark.parametrize(("where", "lineage"), _FRESH_FILES)
def test_reopening_a_built_store_commits_nothing(tmp_path, where, lineage):
    db_path = where(tmp_path)
    first = VectorMemoryStore(db_path=db_path)
    first.init()
    first.close()
    again = VectorMemoryStore(db_path=db_path)
    again.init()
    try:
        assert again._lineage == lineage
        assert _wal_commits(db_path) == 0
    finally:
        again.close()


@pytest.mark.parametrize(("where", "lineage"), _FRESH_FILES)
def test_a_failure_after_the_first_create_leaves_no_partial_schema(
    tmp_path, monkeypatch, where, lineage
):
    real = memory_schema.execute_script

    def first_statement_then_fail(db, sql):
        pending = ""
        for piece in sql.split(";"):
            pending += piece + ";"
            if sqlite3.complete_statement(pending) and pending.strip(" \t\r\n;"):
                break
        assert pending.lstrip().upper().startswith("CREATE")
        real(db, pending)
        raise RuntimeError("injected failure after the first CREATE")

    monkeypatch.setattr(memory_schema, "execute_script", first_statement_then_fail)
    db_path = where(tmp_path)
    broken = VectorMemoryStore(db_path=db_path)
    with pytest.raises(RuntimeError, match="injected failure"):
        broken.init()
    broken.close()
    assert _tables(db_path) == set()

    monkeypatch.setattr(memory_schema, "execute_script", real)
    store = VectorMemoryStore(db_path=db_path)
    store.init()
    try:
        assert store._lineage == lineage
        versions = {row[0] for row in store.db.execute("SELECT version FROM schema_version")}
        assert versions == _expected_versions(lineage)
    finally:
        store.close()


def test_execute_script_runs_inside_the_open_transaction(tmp_path):
    db = sqlite3.connect(tmp_path / "script.db", isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        memory_schema.execute_script(
            db,
            """
            -- a comment; with a semicolon
            CREATE TABLE t (v TEXT);
            INSERT INTO t VALUES ('a;b');
            CREATE TABLE log (v TEXT);
            CREATE TRIGGER t_log AFTER INSERT ON t BEGIN
                INSERT INTO log VALUES (new.v);
            END;
            INSERT INTO t VALUES ('c')
            """,
        )
        assert db.in_transaction
        assert [row[0] for row in db.execute("SELECT v FROM t ORDER BY rowid")] == ["a;b", "c"]
        assert [row[0] for row in db.execute("SELECT v FROM log")] == ["c"]
        db.execute("ROLLBACK")
        assert db.execute("SELECT name FROM sqlite_schema").fetchall() == []
    finally:
        db.close()


class _TracingSqlite:
    """``vector_memory``'s ``sqlite3``, with every connection it opens traced."""

    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    def __getattr__(self, name):
        return getattr(sqlite3, name)

    def connect(self, *args, **kwargs):
        connection = sqlite3.connect(*args, **kwargs)
        connection.set_trace_callback(self._statements.append)
        return connection


@pytest.mark.parametrize(("where", "lineage"), _FRESH_FILES)
def test_the_schema_step_of_an_up_to_date_store_takes_no_write_lock(
    tmp_path, monkeypatch, where, lineage
):
    """The store is empty, so nothing after the schema step writes either.

    A store holding data still writes on open: see the populated test below.
    """
    db_path = where(tmp_path)
    first = VectorMemoryStore(db_path=db_path)
    first.init()
    first.close()

    statements: list[str] = []
    monkeypatch.setattr(vector_memory, "sqlite3", _TracingSqlite(statements))
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        store = VectorMemoryStore(db_path=db_path)
        # An open that asked for the write lock would wait out the store's own
        # busy_timeout and then raise "database is locked", so this is bounded.
        store.init()
        try:
            assert store._lineage == lineage
        finally:
            store.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert not [sql for sql in statements if sql.lstrip().upper().startswith("BEGIN")]


@pytest.mark.xfail(
    strict=True,
    raises=sqlite3.OperationalError,
    reason=(
        "#21056: limit_v1_accepted_history's open-time DELETE begins a write on every "
        "open even when it deletes nothing, so the open waits on another writer"
    ),
)
@pytest.mark.parametrize(("where", "lineage"), _FRESH_FILES)
def test_a_populated_up_to_date_store_opens_while_another_connection_holds_the_write_lock(
    tmp_path, monkeypatch, where, lineage
):
    monkeypatch.setattr(lifecycle, "_BUSY_TIMEOUT_MS", 200)
    db_path = where(tmp_path)
    first = VectorMemoryStore(db_path=db_path)
    first.init()
    try:
        first.set_semantic("pref.a", "x", 0.9, "user_explicit")
    finally:
        first.close()

    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        store = VectorMemoryStore(db_path=db_path)
        # Bounded by the store's busy_timeout, shortened above, after which it raises.
        store.init()
        store.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


@pytest.mark.parametrize(("where", "lineage"), _FRESH_FILES)
def test_two_openers_racing_on_a_fresh_file_build_its_schema_once(
    tmp_path, monkeypatch, caplog, where, lineage
):
    db_path = where(tmp_path)
    winner = VectorMemoryStore(db_path=db_path)
    racer = VectorMemoryStore(db_path=db_path)
    plans: list[int] = []
    real_steps = racer._schema_steps

    def plan_then_lose_the_race(migrated):
        steps = real_steps(migrated)
        plans.append(len(steps))
        if len(plans) == 1:
            # Between the racer's unlocked read and its BEGIN IMMEDIATE, the
            # other opener builds the whole schema.
            winner.init()
        return steps

    monkeypatch.setattr(racer, "_schema_steps", plan_then_lose_the_race)
    caplog.set_level(logging.INFO, logger=vector_memory.logger.name)
    racer.init()
    try:
        assert plans[0] > 0
        assert plans[1] == 0
        assert racer._lineage == winner._lineage == lineage
        versions = {row[0] for row in racer.db.execute("SELECT version FROM schema_version")}
        assert versions == _expected_versions(lineage)
        applied = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("Applied memory schema migration")
        ]
        assert sorted(applied) == sorted(
            f"Applied memory schema migration v{ver}" for ver in _expected_versions(lineage)
        )
    finally:
        racer.close()
        winner.close()


_SEMANTIC_ROW = (
    "INSERT INTO semantic_memory (key, value_json, confidence, source, created_at, updated_at) "
    "VALUES ('pref.a', '\"x\"', 0.9, 'user', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
)
_EPISODIC_ROW = (
    "INSERT INTO episodic_memories (id, conversation_id, text, created_at) "
    "VALUES ('e1', 'c1', 'an episode worth keeping', '2026-01-01T00:00:00')"
)


def _build_v1_store(db_path: Path, versions: set[int]) -> None:
    """A v1 file left at *versions* by an older binary, holding one row of each kind."""
    db = sqlite3.connect(db_path, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        for ver, sql, fn in vector_memory._MIGRATIONS:
            if ver in versions:
                if sql:
                    db.executescript(sql)
                if fn:
                    fn(db)
                db.execute("INSERT INTO schema_version VALUES (?, '2026-01-01T00:00:00')", (ver,))
        db.execute(_SEMANTIC_ROW)
        db.execute(_EPISODIC_ROW)
    finally:
        db.close()


def _snapshot(db_path: Path) -> tuple[list, dict]:
    """The file's whole schema and every row of every table."""
    db = sqlite3.connect(db_path)
    try:
        schema = db.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        rows = {
            name: db.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()
            for kind, name, _sql in schema
            if kind == "table"
        }
        return schema, rows
    finally:
        db.close()


@pytest.mark.parametrize("versions", [{1}, {1, 2}], ids=["v1", "v1+v2"])
def test_a_failed_upgrade_leaves_the_store_at_its_prior_version(tmp_path, monkeypatch, versions):
    db_path = tmp_path / "memory.db"
    _build_v1_store(db_path, versions)
    before = _snapshot(db_path)

    def fail_after_the_migrations(db):
        raise RuntimeError("injected failure after the migrations")

    monkeypatch.setattr(vector_memory.record_meta, "ensure_schema", fail_after_the_migrations)
    store = VectorMemoryStore(db_path=db_path)
    with pytest.raises(RuntimeError, match="injected failure"):
        store.init()
    assert store._db is None
    assert _snapshot(db_path) == before


def test_a_migration_step_that_commits_fails_the_open(tmp_path, monkeypatch):
    """A step that commits ends the setup transaction, so the closing COMMIT raises."""

    def commits(db):
        db.commit()

    monkeypatch.setattr(lifecycle, "_MIGRATIONS", [*lifecycle._MIGRATIONS, (99, "", commits)])
    store = VectorMemoryStore(db_path=tmp_path / "memory.db")
    with pytest.raises(sqlite3.OperationalError, match="no transaction is active"):
        store.init()
    assert store._db is None


def test_reopening_a_crew_store_that_lost_its_lineage_stamp_writes_it_back(tmp_path):
    db_path = memory_stores_root() / "probe" / "memory.db"
    first = VectorMemoryStore(db_path=db_path)
    first.init()
    first.close()
    edit = sqlite3.connect(db_path)
    try:
        with edit:
            edit.execute("DELETE FROM memory_meta WHERE key = ?", (memory_schema.LINEAGE_META_KEY,))
    finally:
        edit.close()

    again = VectorMemoryStore(db_path=db_path)
    again.init()
    try:
        assert again._read_meta(memory_schema.LINEAGE_META_KEY) == memory_schema.LINEAGE_CREW
    finally:
        again.close()

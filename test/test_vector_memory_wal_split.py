"""A second process's close must not delete the -wal the gateway still writes through.

POSIX drops every record lock a process holds on a file when it closes ANY
descriptor of that file. A gateway that lost its locks that way keeps writing
through its open ``memory.db-wal``. When another process (a CLI command) then
closed its own last connection, SQLite saw no lock, checkpointed, and deleted
``-wal`` and ``-shm``. The gateway's later commits went to an unlinked file no
other process could read: the database split, and pages were later written from
two different logs. Every test uses a real SQLite file under ``tmp_path``.
"""

from __future__ import annotations

import os
import sqlite3 as stdlib_sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import _sqlite_compat, diagnose_probes, sqlite_wal
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.xdist_group(name="vector_memory_wal_split")

_SECOND_PROCESS = """
import sys
from pathlib import Path
from kiro_crew import sqlite_wal
from kiro_crew.vector_memory import VectorMemoryStore

if sys.argv[2] == "without-setconfig":
    # What a driver with no Connection.setconfig (pysqlite3) reports.
    sqlite_wal.keep_wal_on_close = lambda conn: False
store = VectorMemoryStore(db_path=Path(sys.argv[1]))
store.init()
store.get_all_semantic()
store.close()
"""


def _drop_this_process_locks(db: Path) -> None:
    """What a plain open + read + close of the live files does to their locks.

    Losing the lock on ``memory.db`` alone is not enough: the ``-shm`` lock still
    stops the other process's close from deleting the ``-wal``.
    """
    for name in (str(db), f"{db}-shm"):
        with open(name, "rb") as handle:
            handle.read(16)


@pytest.mark.skipif(os.name == "nt", reason="POSIX record-lock semantics")
@pytest.mark.parametrize("second_driver", ["store-driver", "without-setconfig"])
def test_another_process_close_keeps_the_wal_a_lockless_writer_uses(
    tmp_path: Path, opened, second_driver: str
):
    db = tmp_path / "memory.db"
    gateway = opened(VectorMemoryStore(db_path=db))
    gateway.init()
    gateway.set_semantic("pref.before", "1", 0.9, "user_explicit")
    wal = Path(f"{db}-wal")
    wal_inode = wal.stat().st_ino
    _drop_this_process_locks(db)

    subprocess.run(
        [sys.executable, "-c", _SECOND_PROCESS, str(db), second_driver],
        cwd=tmp_path,
        check=True,
        timeout=120,
        env=os.environ.copy(),
    )

    assert wal.exists(), "the second process deleted the -wal the gateway writes through"
    assert wal.stat().st_ino == wal_inode
    gateway.set_semantic("pref.after", "2", 0.9, "user_explicit")
    reader = stdlib_sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        keys = {row[0] for row in reader.execute("SELECT key FROM semantic_memory")}
        assert reader.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reader.close()
    assert {"pref.before", "pref.after"} <= keys


@pytest.mark.parametrize("driver", ["store-driver", "without-setconfig"])
def test_a_closed_store_leaves_no_wal_content_behind(
    tmp_path: Path, opened, monkeypatch, driver: str
):
    if driver == "without-setconfig":
        monkeypatch.setattr(sqlite_wal, "keep_wal_on_close", lambda conn: False)
    db = tmp_path / "memory.db"
    store = opened(VectorMemoryStore(db_path=db))
    store.init()
    store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
    store.close()

    wal = Path(f"{db}-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    reader = stdlib_sqlite3.connect(db.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        keys = [row[0] for row in reader.execute("SELECT key FROM semantic_memory")]
    finally:
        reader.close()
    assert keys == ["pref.editor"]


def test_close_keeping_wal_still_closes_when_the_holder_cannot_open(tmp_path: Path, monkeypatch):
    conn = stdlib_sqlite3.connect(tmp_path / "plain.db")
    conn.execute("CREATE TABLE t(x)")

    def _refuse(*_args, **_kwargs):
        raise stdlib_sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(stdlib_sqlite3, "connect", _refuse)
    sqlite_wal.close_keeping_wal(conn)

    with pytest.raises(stdlib_sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_close_keeping_wal_closes_an_in_memory_connection():
    conn = stdlib_sqlite3.connect(":memory:")
    sqlite_wal.close_keeping_wal(conn)

    with pytest.raises(stdlib_sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_keep_wal_on_close_reports_a_driver_without_setconfig():
    class _NoSetconfig:
        pass

    assert sqlite_wal.keep_wal_on_close(_NoSetconfig()) is False


def test_diagnose_probes_open_memory_with_the_store_driver():
    # A second SQLite copy in the gateway would drop the store's locks on close.
    assert diagnose_probes.sqlite3 is _sqlite_compat.sqlite3

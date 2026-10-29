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

_DRIVER_KEEPS_WAL = hasattr(_sqlite_compat.sqlite3.connect(":memory:"), "setconfig")

_SECOND_PROCESS = """
import sys
from pathlib import Path
from kiro_crew.vector_memory import VectorMemoryStore

store = VectorMemoryStore(db_path=Path(sys.argv[1]))
store.init()
store.get_all_semantic()
store.close()
"""


def _drop_this_process_locks(db: Path) -> None:
    """What a plain open + read + close of the live database does to its locks."""
    with open(db, "rb") as handle:
        handle.read(16)


@pytest.mark.skipif(os.name == "nt", reason="POSIX record-lock semantics")
@pytest.mark.skipif(not _DRIVER_KEEPS_WAL, reason="driver has no Connection.setconfig")
def test_another_process_close_keeps_the_wal_a_lockless_writer_uses(tmp_path: Path, opened):
    db = tmp_path / "memory.db"
    gateway = opened(VectorMemoryStore(db_path=db))
    gateway.init()
    gateway.set_semantic("pref.before", "1", 0.9, "user_explicit")
    wal = Path(f"{db}-wal")
    wal_inode = wal.stat().st_ino
    _drop_this_process_locks(db)

    subprocess.run(
        [sys.executable, "-c", _SECOND_PROCESS, str(db)],
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


def test_a_closed_store_leaves_no_wal_content_behind(tmp_path: Path, opened):
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


def test_keep_wal_on_close_reports_a_driver_without_setconfig():
    class _NoSetconfig:
        pass

    assert sqlite_wal.keep_wal_on_close(_NoSetconfig()) is False


def test_diagnose_probes_open_memory_with_the_store_driver():
    # A second SQLite copy in the gateway would drop the store's locks on close.
    assert diagnose_probes.sqlite3 is _sqlite_compat.sqlite3

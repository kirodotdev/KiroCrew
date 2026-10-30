"""Switching a store into WAL survives another connection's lock.

The switch out of a rollback journal takes an exclusive lock by upgrading a read
lock, and SQLite answers a conflict on that upgrade at once, without calling the
busy handler. So ``busy_timeout`` alone leaves a second opener of a new file failing
with "database is locked". ``sqlite_wal.enable_wal`` retries the switch instead.
"""

from __future__ import annotations

import functools
import sqlite3 as stdlib_sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kiro_crew import sqlite_wal

# The library the stores open their files with: a second SQLite library in this
# process keeps its own lock table, so its locks would be invisible to the store.
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.vector_memory import VectorMemoryStore


class _FakeTime:
    """A clock that moves only when ``sleep`` is called, and can act on a sleep."""

    def __init__(self, on_sleep=None) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self._on_sleep = on_sleep

    def clock(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.now += secs
        if self._on_sleep is not None:
            self._on_sleep(len(self.sleeps))


def _journal_mode(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


def _rollback_file_held_for_write(path: Path, lib=sqlite3):
    """Create a rollback-journal database and return a connection holding its write lock."""
    holder = lib.connect(path, isolation_level=None)
    holder.execute("CREATE TABLE IF NOT EXISTS held (x)")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO held VALUES (1)")
    return holder


class TestEnableWal:
    # Some app stores open their file with the stdlib module rather than the compat one.
    @pytest.mark.parametrize("lib", [sqlite3, stdlib_sqlite3], ids=["compat", "stdlib"])
    def test_a_held_write_lock_is_waited_out(self, tmp_path: Path, lib) -> None:
        db = tmp_path / "held.db"
        holder = _rollback_file_held_for_write(db, lib)
        conn = lib.connect(db, isolation_level=None)
        try:
            # The premise: busy_timeout does not cover the switch.
            conn.execute("PRAGMA busy_timeout=5000")
            with pytest.raises(lib.OperationalError, match="locked"):
                conn.execute("PRAGMA journal_mode=WAL")

            def release(n: int) -> None:
                if n == 2:
                    holder.execute("COMMIT")

            fake = _FakeTime(on_sleep=release)
            mode = sqlite_wal.enable_wal(conn, timeout_secs=5.0, clock=fake.clock, sleep=fake.sleep)
        finally:
            conn.close()
            holder.close()
        assert mode == "wal"
        assert len(fake.sleeps) == 2
        assert _journal_mode(db) == "wal"

    def test_a_lock_that_outlasts_the_deadline_is_raised(self, tmp_path: Path) -> None:
        db = tmp_path / "stuck.db"
        holder = _rollback_file_held_for_write(db)
        conn = sqlite3.connect(db, isolation_level=None)
        fake = _FakeTime()
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                sqlite_wal.enable_wal(conn, timeout_secs=1.0, clock=fake.clock, sleep=fake.sleep)
        finally:
            conn.close()
            holder.close()
        assert fake.sleeps, "the switch was not retried"
        assert sum(fake.sleeps) == pytest.approx(1.0)
        assert max(fake.sleeps) <= sqlite_wal._MAX_WAIT_SECS

    def test_a_file_already_in_wal_is_not_switched_again(self, tmp_path: Path) -> None:
        db = tmp_path / "wal.db"
        setup = sqlite3.connect(db, isolation_level=None)
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE t (x)")
        setup.close()
        conn = sqlite3.connect(db, isolation_level=None)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        fake = _FakeTime()
        try:
            mode = sqlite_wal.enable_wal(conn, timeout_secs=5.0, clock=fake.clock, sleep=fake.sleep)
        finally:
            conn.close()
        assert mode == "wal"
        assert statements == ["PRAGMA journal_mode"]
        assert fake.sleeps == []

    def test_an_error_that_is_not_a_lock_is_raised_at_once(self) -> None:
        class _Conn:
            calls = 0

            def execute(self, sql: str):
                self.calls += 1
                if sql == "PRAGMA journal_mode":
                    return sqlite3.connect(":memory:").execute("SELECT 'delete'")
                raise sqlite3.OperationalError("disk I/O error")

        conn = _Conn()
        fake = _FakeTime()
        with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
            sqlite_wal.enable_wal(conn, timeout_secs=5.0, clock=fake.clock, sleep=fake.sleep)
        assert conn.calls == 2
        assert fake.sleeps == []


class TestMemoryStoreOpen:
    def test_concurrent_first_opens_of_a_fresh_file_all_succeed(self, tmp_path: Path) -> None:
        openers, rounds = 4, 20
        for n in range(rounds):
            db = tmp_path / f"round-{n}" / "memory.db"
            db.parent.mkdir()
            stores = [VectorMemoryStore(db_path=db) for _ in range(openers)]
            start = threading.Barrier(openers)

            def open_store(store: VectorMemoryStore) -> None:
                start.wait(timeout=30)
                store.init()

            try:
                with ThreadPoolExecutor(max_workers=openers) as pool:
                    futures = [pool.submit(open_store, s) for s in stores]
                    failures = []
                    for f in futures:
                        try:
                            f.result(timeout=60)
                        except Exception as exc:
                            failures.append(exc)
            finally:
                for s in stores:
                    s.close()
            assert failures == [], f"round {n}: {failures!r}"
            assert _journal_mode(db) == "wal"

    def test_an_open_waits_out_a_held_write_lock_then_switches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "memory.db"
        holder = _rollback_file_held_for_write(db)
        fake = _FakeTime(on_sleep=lambda n: holder.execute("COMMIT") if n == 1 else None)
        monkeypatch.setattr(
            sqlite_wal,
            "enable_wal",
            functools.partial(sqlite_wal.enable_wal, clock=fake.clock, sleep=fake.sleep),
        )
        store = VectorMemoryStore(db_path=db)
        try:
            store.init()
            mode = store.db.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            store.close()
            holder.close()
        assert fake.sleeps, "the open did not wait for the lock"
        assert str(mode).lower() == "wal"


def _wal_db(tmp_path: Path) -> tuple[Path, stdlib_sqlite3.Connection]:
    db = tmp_path / "w.db"
    conn = stdlib_sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    return db, conn


@pytest.mark.skipif(
    not hasattr(stdlib_sqlite3.Connection, "setconfig"), reason="Python before 3.12"
)
def test_a_kept_connection_closes_without_deleting_its_wal(tmp_path: Path) -> None:
    db, conn = _wal_db(tmp_path)
    assert sqlite_wal.keep_wal_on_close(conn) is True
    conn.close()
    assert Path(f"{db}-wal").exists()


def test_keep_wal_on_close_reports_a_connection_that_refuses_the_option(
    tmp_path: Path,
) -> None:
    _db, conn = _wal_db(tmp_path)
    conn.close()  # setconfig on a closed connection raises ProgrammingError
    assert sqlite_wal.keep_wal_on_close(conn) is False


def test_truncate_wal_before_close_empties_the_wal_in_place(tmp_path: Path) -> None:
    db, conn = _wal_db(tmp_path)
    wal = Path(f"{db}-wal")
    assert wal.stat().st_size > 0
    sqlite_wal.truncate_wal_before_close(conn)
    assert wal.exists() and wal.stat().st_size == 0
    conn.close()


def test_truncate_wal_before_close_never_raises(tmp_path: Path) -> None:
    _db, conn = _wal_db(tmp_path)
    conn.close()
    sqlite_wal.truncate_wal_before_close(conn)  # a closed connection is logged, not raised

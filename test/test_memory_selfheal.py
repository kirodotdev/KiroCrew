"""Regression tests for memory.py concurrency/self-heal fixes.

Track C bugs:
  1. FTS self-heal deleted the index on ANY sqlite error — including the
     transient 'database is locked' — turning lock contention into permanent
     data loss. It must only delete/rebuild on genuine corruption, and the
     connection must carry a busy_timeout so locks are waited out.
  2. append_history did an unlocked read-modify-write, so concurrent appends
     interleaved and lost entries. It must serialize the whole append behind an
     exclusive file lock.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

from kiro_crew import memory as memory_module
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.memory import MemoryStore, _is_corruption_error


def _steady_clock(day: date, next_day: date) -> type[datetime]:
    """A ``datetime`` stand-in whose local date a test moves, not the wall clock.

    ``now()`` answers midday on ``day`` until :meth:`cross` is called and
    midday on ``next_day`` after it. A test that names a dated file and then
    reads it back holds the date still for both halves, so it does not depend
    on where the host clock happens to sit; a test that wants the rollover
    asks for it.
    """

    class _Clock(datetime):
        crossed = False
        _lock = threading.Lock()

        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            with cls._lock:
                chosen = next_day if cls.crossed else day
            moment = datetime(chosen.year, chosen.month, chosen.day, 12, 0, 0)
            return moment if tz is None else moment.astimezone(tz)

        @classmethod
        def cross(cls) -> None:
            cls.crossed = True

    return _Clock


class TestCorruptionDetection:
    """_is_corruption_error must distinguish contention from corruption."""

    def test_locked_is_not_corruption(self):
        assert _is_corruption_error(sqlite3.OperationalError("database is locked")) is False

    def test_busy_is_not_corruption(self):
        assert _is_corruption_error(sqlite3.OperationalError("database is busy")) is False

    def test_malformed_is_corruption(self):
        assert (
            _is_corruption_error(sqlite3.DatabaseError("database disk image is malformed"))
            is True
        )

    def test_not_a_database_is_corruption(self):
        assert _is_corruption_error(sqlite3.DatabaseError("file is not a database")) is True

    def test_non_sqlite_error_is_not_corruption(self):
        assert _is_corruption_error(ValueError("boom")) is False


class TestFtsSelfHealGating:
    def test_lock_error_does_not_delete_db(self, tmp_path, monkeypatch):
        """BUG 1 regression: a transient lock error must NOT delete the index."""
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.write_preferences("# Prefs\n\n- likes Python\n")
        store.rebuild_index()

        db_path = tmp_path / "memory_index.db"
        assert db_path.exists()
        before = db_path.read_bytes()

        # Force _try_create_db to fail as if the DB is locked (contention).
        def _locked(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store, "_try_create_db", _locked)

        # The lock error must propagate, NOT trigger a delete-and-rebuild.
        raised = False
        try:
            store._get_db()
        except sqlite3.OperationalError:
            raised = True
        assert raised, "lock error should propagate, not be swallowed by self-heal"

        # The healthy index must survive untouched.
        assert db_path.exists(), "self-heal wrongly deleted the DB on a lock error"
        assert db_path.read_bytes() == before

    def test_genuine_corruption_still_self_heals(self, tmp_path):
        """Genuine on-disk corruption should still auto-rebuild."""
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.write_preferences("# Prefs\n\n- likes Python\n")
        store.rebuild_index()

        db_path = tmp_path / "memory_index.db"
        assert db_path.exists()
        db_path.write_bytes(b"this is not a valid sqlite database")

        # rebuild_index -> _get_db detects "file is not a database" and rebuilds.
        count = store.rebuild_index()
        assert count >= 1
        results = store.search("Python")
        assert len(results) >= 1

    def test_connection_has_busy_timeout(self, tmp_path):
        """The connection must carry a non-zero busy_timeout to wait out locks."""
        store = MemoryStore(workspace=tmp_path)
        store.init()
        conn = store._get_db()
        try:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout >= 5000
        finally:
            conn.close()


class TestConcurrentAppend:
    def test_concurrent_appends_do_not_lose_entries(self, tmp_path, monkeypatch):
        """BUG 2 regression: parallel appends must all survive (no clobbering)."""
        store = MemoryStore(workspace=tmp_path)
        store.init()

        # The store names its history file from the date it reads at the moment
        # of the call, so the writes below and the read after them would each
        # pick their own. Hold the date still for the whole test: what this
        # asserts is the append lock, and one file is what lets it assert that.
        day = date(2026, 3, 14)
        monkeypatch.setattr(memory_module, "datetime", _steady_clock(day, day + timedelta(days=1)))
        history_file = store._today_history_file()

        n = 40
        barrier = threading.Barrier(n)

        def _worker(idx: int) -> None:
            barrier.wait()  # maximize contention
            store.append_history(f"entry-{idx}")

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        content = history_file.read_text(encoding="utf-8")
        # Every distinct entry must be present exactly once.
        for i in range(n):
            assert f"entry-{i}" in content, f"lost entry-{i} under concurrent append"
        assert content.count("####") == n, "entry count mismatch — appends clobbered"

    def test_a_date_rollover_leaves_appended_entries_in_their_own_day(self, tmp_path, monkeypatch):
        """Why the test above names its file once.

        ``_today_history_file`` answers from the date at the moment of the
        call, so a rollover between an append and a read points the reader at
        a file the append never wrote and which does not exist yet.
        """
        store = MemoryStore(workspace=tmp_path)
        store.init()

        day = date(2026, 3, 14)
        clock = _steady_clock(day, day + timedelta(days=1))
        monkeypatch.setattr(memory_module, "datetime", clock)

        written = store._today_history_file()
        store.append_history("entry-before-the-rollover")
        assert written.exists()

        clock.cross()  # local midnight passes with the entry already on disk

        after = store._today_history_file()
        assert after != written, "the rollover must move the dated file name"
        assert not after.exists(), "the new day's file is what a later read misses"
        assert "entry-before-the-rollover" in written.read_text(encoding="utf-8")

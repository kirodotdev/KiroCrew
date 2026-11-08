"""The boot page-cache prefetch reads the right files, in a child, off the caller's thread."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from kiro_crew import page_cache


def _wait_for(threads_before: set[int], name: str) -> None:
    for thread in threading.enumerate():
        if thread.name == name and thread.ident not in threads_before:
            thread.join(5)


def _record_child_reads(monkeypatch) -> list[tuple[list[str], str]]:
    """Replace the child launch with a recorder of (file names, launching thread)."""
    calls: list[tuple[list[str], str]] = []

    def fake_run(argv, **kwargs):
        files = argv[argv.index(page_cache._CHILD_SOURCE) + 2 :]
        calls.append(([Path(f).name for f in files], threading.current_thread().name))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(page_cache.subprocess, "run", fake_run)
    return calls


def test_reads_each_file_and_its_wal_on_a_named_background_thread(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    db.write_bytes(b"x" * 10)
    (tmp_path / "a.db-wal").write_bytes(b"w" * 10)
    calls = _record_child_reads(monkeypatch)
    before = {t.ident for t in threading.enumerate()}
    page_cache.prefetch([db, None, tmp_path / "missing.db"], name="kc-test-prefetch")
    _wait_for(before, "kc-test-prefetch")
    assert calls == [(["a.db", "a.db-wal"], "kc-test-prefetch")]


def test_a_file_over_the_cap_is_left_alone(tmp_path, monkeypatch):
    big = tmp_path / "big.db"
    big.write_bytes(b"x" * 100)
    monkeypatch.setattr(page_cache, "MAX_BYTES", 10)
    calls = _record_child_reads(monkeypatch)
    page_cache.read_now([big])
    assert calls == []


def test_nothing_to_read_starts_no_thread():
    before = {t.ident for t in threading.enumerate()}
    page_cache.prefetch([None])
    assert {t.ident for t in threading.enumerate()} - before == set()


def test_an_unreadable_file_never_raises(tmp_path):
    folder = tmp_path / "is-a-dir.db"
    folder.mkdir()
    before = {t.ident for t in threading.enumerate()}
    page_cache.prefetch([folder], name="kc-test-dir")
    _wait_for(before, "kc-test-dir")


def test_read_now_reads_on_the_calling_thread_and_skips_none(tmp_path, monkeypatch):
    db = tmp_path / "now.db"
    db.write_bytes(b"x" * 10)
    calls = _record_child_reads(monkeypatch)
    page_cache.read_now([None, str(db)])
    assert calls == [(["now.db"], threading.current_thread().name)]


def test_a_child_that_cannot_start_skips_that_file_and_continues(tmp_path, monkeypatch):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"x")
    good = tmp_path / "good.db"
    good.write_bytes(b"y")
    launched: list[str] = []

    def failing_run(argv, **kwargs):
        name = Path(argv[-1]).name
        launched.append(name)
        if name == "bad.db":
            raise OSError(24, "too many open files")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(page_cache.subprocess, "run", failing_run)
    page_cache.read_now([bad, good])  # must not raise
    assert launched == ["bad.db", "good.db"]


def test_a_child_that_times_out_never_raises(tmp_path, monkeypatch):
    db = tmp_path / "slow.db"
    db.write_bytes(b"x")

    def slow_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(page_cache.subprocess, "run", slow_run)
    page_cache.read_now([db])


def test_prefetch_during_interpreter_shutdown_never_raises(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    db.write_bytes(b"x")

    def _refuse(self):
        raise RuntimeError("can't create new thread at interpreter shutdown")

    monkeypatch.setattr(threading.Thread, "start", _refuse)
    page_cache.prefetch([db])  # returns quietly; the read is only a hint


_TRY_WRITE = (
    "import sqlite3, sys\n"
    "db = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)\n"
    "try:\n"
    "    db.execute('BEGIN IMMEDIATE')\n"
    "except sqlite3.OperationalError as exc:\n"
    "    print('refused:', exc)\n"
    "else:\n"
    "    print('took the write lock')\n"
)


def test_reading_a_live_database_keeps_this_process_sqlite_locks(tmp_path):
    """On POSIX, closing any descriptor on a file drops every fcntl lock the process
    holds on it. A rollback-journal writer read in-process this way loses its
    RESERVED lock, and another process can then write into the same pages."""
    path = tmp_path / "live.db"
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("CREATE TABLE t (v TEXT)")
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO t VALUES ('pending')")

        page_cache.read_now([path])

        other = subprocess.run(
            [sys.executable, "-I", "-c", _TRY_WRITE, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        assert other.stdout.startswith("refused:"), other.stdout + other.stderr
        db.execute("COMMIT")
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        db.close()


def test_knowledge_maintenance_reads_the_library_only_while_a_rebuild_is_pending(
    tmp_path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.dashboard import server as srv
    from kiro_crew.knowledge.store import KnowledgeStore

    path = str(tmp_path / "kb.db")
    seed = KnowledgeStore(path)
    seed.add_item("How the gateway starts", "project notes on boot", "doc")
    seed.db.execute("PRAGMA user_version = 0")  # an FTS index from before format v1
    seed.db.commit()
    seed._close_all_for_tests()
    store = KnowledgeStore(path)
    reads: list[list] = []
    monkeypatch.setattr(page_cache, "read_now", lambda paths: reads.append(list(paths)))
    try:
        assert store.has_pending_one_time_work()

        async def kick():
            state = SimpleNamespace(_knowledge_store=store, _background_tasks=set())
            srv._kick_knowledge_orphan_reclaim(state)
            await asyncio.gather(*state._background_tasks)

        asyncio.run(kick())
        assert reads == [[path]]
        assert not store.has_pending_one_time_work()
        asyncio.run(kick())
        assert reads == [[path]], "nothing pending: no read on later starts"
    finally:
        store._close_all_for_tests()

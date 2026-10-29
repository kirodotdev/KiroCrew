"""The boot page-cache prefetch reads the right files, in order, off the caller's thread."""

from __future__ import annotations

import threading
from pathlib import Path

from kiro_crew import page_cache


def _wait_for(threads_before: set[int], name: str) -> None:
    for thread in threading.enumerate():
        if thread.name == name and thread.ident not in threads_before:
            thread.join(5)


def test_reads_each_file_and_its_wal_on_a_named_background_thread(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    db.write_bytes(b"x" * (3 * page_cache._CHUNK + 5))
    (tmp_path / "a.db-wal").write_bytes(b"w" * 10)
    read: list[tuple[str, str]] = []
    real_open = open

    def recording_open(path, mode="r", *args, **kwargs):
        if str(path).startswith(str(tmp_path)):
            read.append((Path(path).name, threading.current_thread().name))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", recording_open)
    before = {t.ident for t in threading.enumerate()}
    page_cache.prefetch([db, None, tmp_path / "missing.db"], name="kc-test-prefetch")
    _wait_for(before, "kc-test-prefetch")
    assert read == [("a.db", "kc-test-prefetch"), ("a.db-wal", "kc-test-prefetch")]


def test_a_file_over_the_cap_is_left_alone(tmp_path, monkeypatch):
    big = tmp_path / "big.db"
    big.write_bytes(b"x" * 100)
    monkeypatch.setattr(page_cache, "MAX_BYTES", 10)
    opened: list[str] = []
    real_open = open

    def recording_open(path, *args, **kwargs):
        if str(path).startswith(str(tmp_path)):
            opened.append(Path(path).name)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", recording_open)
    before = {t.ident for t in threading.enumerate()}
    page_cache.prefetch([big], name="kc-test-cap")
    _wait_for(before, "kc-test-cap")
    assert opened == []


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
    read: list[tuple[str, str]] = []
    real_open = open

    def recording_open(path, *args, **kwargs):
        if str(path).startswith(str(tmp_path)):
            read.append((Path(path).name, threading.current_thread().name))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", recording_open)
    page_cache.read_now([None, str(db)])
    assert read == [("now.db", threading.current_thread().name)]


def test_a_read_error_skips_that_file_and_continues(tmp_path, monkeypatch):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"x")
    good = tmp_path / "good.db"
    good.write_bytes(b"y")
    opened: list[str] = []
    real_open = open

    def denying_open(path, *args, **kwargs):
        if str(path).startswith(str(tmp_path)):
            opened.append(Path(path).name)
            if Path(path).name == "bad.db":
                raise PermissionError(13, "denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denying_open)
    page_cache.read_now([bad, good])  # must not raise
    assert opened == ["bad.db", "good.db"]


def test_prefetch_during_interpreter_shutdown_never_raises(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    db.write_bytes(b"x")

    def _refuse(self):
        raise RuntimeError("can't create new thread at interpreter shutdown")

    monkeypatch.setattr(threading.Thread, "start", _refuse)
    page_cache.prefetch([db])  # returns quietly; the read is only a hint


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

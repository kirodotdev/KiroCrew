"""Reads on pooled read-only connections, the writer lock, and the background pool.

A store in WAL mode runs every plain SELECT of its fetch helpers on a pooled
read-only connection, outside ``_db_lock``: a long read on one thread does not
hold up a short read on another, and a thread holding the writer lock does not
hold up a read. Writes still exclude each other on the one shared connection, a
read inside a locked section still sees that section's uncommitted rows, and
``close()`` leaves no read connection open. Taking the writer lock on a thread
that runs an event loop is reported (and raises under
``LOOP_THREAD_LOCK_RAISES``). Consolidation and the embedding backfill run on
their own ``mc-membg`` pool, so turn-context work on ``mc-embed`` does not queue
behind them. Every test opens a real SQLite file under ``tmp_path``.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kiro_crew import executors
from kiro_crew import vector_memory_lifecycle as lifecycle
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.xdist_group(name="vector_memory_read_pool")

_WAIT = 30.0


def _store(tmp_path: Path, opened) -> VectorMemoryStore:
    store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
    store.init()
    store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
    store.set_semantic("pref.shell", "zsh", 0.9, "user_explicit")
    return store


def _keys(store: VectorMemoryStore) -> list[str]:
    return [row["key"] for row in store.get_all_semantic()]


@pytest.fixture
def blocking_reads(monkeypatch):
    """Give every pooled read connection a ``block_read()`` SQL function.

    The function parks the reading thread until ``release`` is set, which holds
    one read in flight for as long as a test needs, on the real pooled path.
    """
    entered = threading.Event()
    release = threading.Event()
    real_open = lifecycle._open_reader

    def block_read() -> int:
        entered.set()
        assert release.wait(_WAIT)
        return 1

    def open_with_block(uri: str):
        conn = real_open(uri)
        conn.create_function("block_read", 0, block_read)
        return conn

    monkeypatch.setattr(lifecycle, "_open_reader", open_with_block)
    yield entered, release
    release.set()


class TestReadsRunBesideEachOther:
    def test_init_hands_reads_to_the_pool(self, tmp_path: Path, opened) -> None:
        store = _store(tmp_path, opened)
        assert store._readers.enabled
        assert _keys(store) == ["pref.editor", "pref.shell"]
        assert store._readers.open_count() >= 1

    def test_a_long_read_does_not_block_a_short_read(
        self, tmp_path: Path, opened, blocking_reads
    ) -> None:
        entered, release = blocking_reads
        store = _store(tmp_path, opened)
        with ThreadPoolExecutor(max_workers=2) as pool:
            long_read = pool.submit(store._fetch_one_locked, "SELECT block_read() AS x")
            assert entered.wait(_WAIT)
            short_read = pool.submit(_keys, store)
            assert short_read.result(timeout=_WAIT) == ["pref.editor", "pref.shell"]
            assert store.count_lessons() == 0
            assert not long_read.done()
            release.set()
            assert long_read.result(timeout=_WAIT)["x"] == 1

    def test_a_held_writer_lock_does_not_block_reads(self, tmp_path: Path, opened) -> None:
        store = _store(tmp_path, opened)
        with ThreadPoolExecutor(max_workers=1) as pool, store._db_lock:
            assert pool.submit(_keys, store).result(timeout=_WAIT) == [
                "pref.editor",
                "pref.shell",
            ]
            assert pool.submit(store.count_lessons).result(timeout=_WAIT) == 0
            assert pool.submit(store.get_lessons).result(timeout=_WAIT) == []
            assert pool.submit(store._ineligible_ids).result(timeout=_WAIT) == set()

    def test_a_statement_only_the_shared_connection_can_run_falls_back(
        self, tmp_path: Path, opened
    ) -> None:
        """A function registered on the shared connection alone fails on a pooled
        connection; the read answers from the shared one and the pooled connection
        is kept, so repeated calls do not open a connection each time."""
        store = _store(tmp_path, opened)
        with store._db_lock:
            store.db.create_function("shared_only", 0, lambda: 7)
        with ThreadPoolExecutor(max_workers=1) as pool:
            for _ in range(5):
                row = pool.submit(store._fetch_one_locked, "SELECT shared_only() AS v")
                assert row.result(timeout=_WAIT)["v"] == 7
        assert store._readers.open_count() == 1

    def test_read_counters_still_count_pooled_reads(self, tmp_path: Path, opened) -> None:
        store = _store(tmp_path, opened)
        before = store.read_counters()
        _keys(store)
        after = store.read_counters()
        assert after["statements_executed"] == before["statements_executed"] + 1
        assert after["rows_read"] == before["rows_read"] + 2


class TestWritesStaySerialized:
    def test_a_write_waits_for_the_writer_lock(self, tmp_path: Path, opened) -> None:
        store = _store(tmp_path, opened)
        written = threading.Event()

        def write() -> None:
            store.set_semantic("pref.theme", "dark", 0.9, "user_explicit")
            written.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with store._db_lock:
                future = pool.submit(write)
                # flake-ok: a negative wait; the write must NOT finish while the lock is held
                assert not written.wait(0.3)
            future.result(timeout=_WAIT)
        assert "pref.theme" in _keys(store)

    def test_a_read_inside_a_locked_section_sees_its_uncommitted_row(
        self, tmp_path: Path, opened
    ) -> None:
        store = _store(tmp_path, opened)
        insert = (
            "INSERT INTO semantic_memory (key, value_json, source, created_at, updated_at) "
            "VALUES ('pref.pending', '\"x\"', 'tool', '2026-01-01', '2026-01-01')"
        )
        with store._db_lock:
            store.db.execute(insert)
            assert store.db.in_transaction
            assert "pref.pending" in _keys(store)
            store.db.rollback()
        assert "pref.pending" not in _keys(store)

    def test_another_threads_read_sees_the_last_commit_during_a_write(
        self, tmp_path: Path, opened
    ) -> None:
        store = _store(tmp_path, opened)
        insert = (
            "INSERT INTO semantic_memory (key, value_json, source, created_at, updated_at) "
            "VALUES ('pref.pending', '\"x\"', 'tool', '2026-01-01', '2026-01-01')"
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            with store._db_lock:
                store.db.execute(insert)
                assert pool.submit(_keys, store).result(timeout=_WAIT) == [
                    "pref.editor",
                    "pref.shell",
                ]
                store.db.commit()
            assert "pref.pending" in pool.submit(_keys, store).result(timeout=_WAIT)

    def test_an_abandoned_transaction_is_read_on_the_shared_connection(
        self, tmp_path: Path, opened
    ) -> None:
        """A write that released the lock without committing leaves rows only the
        shared connection can see; a read then goes there, as it always did."""
        store = _store(tmp_path, opened)
        insert = (
            "INSERT INTO semantic_memory (key, value_json, source, created_at, updated_at) "
            "VALUES ('pref.orphan', '\"x\"', 'tool', '2026-01-01', '2026-01-01')"
        )
        with store._db_lock:
            store.db.execute(insert)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert "pref.orphan" in pool.submit(_keys, store).result(timeout=_WAIT)
        finally:
            with store._db_lock:
                store.db.rollback()


class TestLifecycle:
    def test_close_leaves_no_read_connection_open(self, tmp_path: Path) -> None:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
        with ThreadPoolExecutor(max_workers=3) as pool:
            for future in [pool.submit(_keys, store) for _ in range(6)]:
                future.result(timeout=_WAIT)
        assert store._readers.open_count() >= 1
        store.close()
        assert store._readers.open_count() == 0
        assert not store._readers.enabled
        with pytest.raises(RuntimeError, match="not initialized"):
            _keys(store)

    def test_close_interrupts_a_read_in_flight(
        self, tmp_path: Path, blocking_reads, monkeypatch
    ) -> None:
        entered, release = blocking_reads
        monkeypatch.setattr(lifecycle, "_READER_DRAIN_SECS", _WAIT)
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        with ThreadPoolExecutor(max_workers=1) as pool:
            read = pool.submit(store._fetch_one_locked, "SELECT block_read() AS x")
            assert entered.wait(_WAIT)
            closer = threading.Thread(target=store.close)
            closer.start()
            release.set()
            closer.join(_WAIT)
            assert not closer.is_alive()
            # The interrupted read falls back to the shared connection, which close()
            # has dropped, so it reports the closed store; or it finished first.
            try:
                read.result(timeout=_WAIT)
            except RuntimeError as exc:
                assert "not initialized" in str(exc)
        assert store._readers.open_count() == 0

    def test_init_again_reopens_the_pool(self, tmp_path: Path, opened) -> None:
        store = _store(tmp_path, opened)
        _keys(store)
        store.init()
        assert store._readers.enabled
        assert _keys(store) == ["pref.editor", "pref.shell"]

    def test_a_file_not_in_wal_mode_reads_on_the_shared_connection(
        self, tmp_path: Path, opened, monkeypatch
    ) -> None:
        monkeypatch.setattr(lifecycle.sqlite_wal, "enable_wal", lambda *a, **k: None)
        store = _store(tmp_path, opened)
        assert not store._readers.enabled
        assert _keys(store) == ["pref.editor", "pref.shell"]
        assert store._readers.open_count() == 0


class TestNoLockOnTheLoopThread:
    def test_taking_the_writer_lock_on_a_loop_thread_raises_under_test(
        self, tmp_path: Path, opened, monkeypatch
    ) -> None:
        store = _store(tmp_path, opened)
        monkeypatch.setattr(lifecycle, "LOOP_THREAD_LOCK_RAISES", True)

        async def inline_write() -> None:
            store.set_semantic("pref.theme", "dark", 0.9, "user_explicit")

        with pytest.raises(
            lifecycle.LoopThreadLockError, match="event loop thread at .*semantic.py"
        ):
            asyncio.run(inline_write())

    def test_a_loop_thread_lock_is_logged_once_per_site(
        self, tmp_path: Path, opened, monkeypatch, caplog
    ) -> None:
        store = _store(tmp_path, opened)
        monkeypatch.setattr(lifecycle, "_loop_lock_sites", set())

        async def inline_writes() -> None:
            for _ in range(3):
                with store._db_lock:
                    pass

        with caplog.at_level(logging.WARNING, logger="kiro_crew.vector_memory"):
            asyncio.run(inline_writes())
        hits = [r for r in caplog.records if "event loop thread" in r.getMessage()]
        assert len(hits) == 1

    def test_turn_path_reads_and_offloaded_writes_take_no_lock_on_the_loop(
        self, tmp_path: Path, opened, monkeypatch
    ) -> None:
        """The turn-context readers through the pools and an inline pooled read on
        the loop thread never take the writer lock there."""
        store = _store(tmp_path, opened)
        store.write_lesson(rule="always run the related tests first", category="tool")
        store.write_episodic("episodic memory about running the related tests first")
        monkeypatch.setattr(lifecycle, "LOOP_THREAD_LOCK_RAISES", True)

        async def turn() -> None:
            assert "pref.editor" in _keys(store)
            assert store.count_lessons() == 1
            await executors.run_in_embed_pool(store.get_lessons_context, query_text="tests")
            await executors.run_in_embed_pool(store.search_episodic, query_text="tests")
            await executors.run_in_embed_pool(store.get_semantic_context, query_text="tests")
            await executors.run_in_memory_background_pool(
                store.set_semantic, "pref.theme", "dark", 0.9, "user_explicit"
            )

        asyncio.run(turn())
        assert "pref.theme" in _keys(store)


class TestBackgroundPool:
    def test_background_work_runs_on_its_own_threads(self) -> None:
        async def names() -> tuple[str, str]:
            bg = await executors.run_in_memory_background_pool(
                lambda: threading.current_thread().name
            )
            fg = await executors.run_in_embed_pool(lambda: threading.current_thread().name)
            return bg, fg

        background, interactive = asyncio.run(names())
        assert background.startswith("mc-membg")
        assert interactive.startswith("mc-embed")

    def test_a_saturated_background_pool_does_not_delay_turn_recall(
        self, tmp_path: Path, opened
    ) -> None:
        """Fill every background worker and queue more behind them; a turn-context
        recall on mc-embed still answers inside a fixed bound."""
        store = _store(tmp_path, opened)
        store.write_lesson(rule="always run the related tests first", category="tool")
        release = threading.Event()
        started = threading.Semaphore(0)

        def background_pass() -> None:
            started.release()
            assert release.wait(_WAIT)

        async def run() -> str:
            backlog = [
                asyncio.ensure_future(executors.run_in_memory_background_pool(background_pass))
                for _ in range(executors._MAX_MEMORY_BACKGROUND_WORKERS * 3)
            ]
            for _ in range(executors._MAX_MEMORY_BACKGROUND_WORKERS):
                assert await asyncio.to_thread(started.acquire, True, _WAIT)
            try:
                return await asyncio.wait_for(
                    executors.run_in_embed_pool(store.get_lessons_context, query_text="tests"),
                    timeout=_WAIT,
                )
            finally:
                release.set()
                await asyncio.gather(*backlog)

        assert "related tests" in asyncio.run(run())

    def test_backfill_and_repair_loops_leave_consolidation_its_workers(self) -> None:
        """The backfill and repair loops hold background threads for a long time
        without admission; consolidation still runs its admitted passes at once."""
        release = threading.Event()
        loops_started = threading.Semaphore(0)
        passes_started = threading.Semaphore(0)

        def long_loop() -> None:
            loops_started.release()
            assert release.wait(_WAIT)

        def consolidation() -> None:
            passes_started.release()
            assert release.wait(_WAIT)

        async def run() -> None:
            loop = asyncio.get_running_loop()
            held = [
                loop.run_in_executor(executors.memory_background_executor(), long_loop)
                for _ in range(executors._MEMORY_BACKGROUND_LOOP_THREADS)
            ]
            for _ in held:
                assert await asyncio.to_thread(loops_started.acquire, True, _WAIT)
            passes = [
                asyncio.ensure_future(executors.run_in_memory_background_pool(consolidation))
                for _ in range(executors._MAX_MEMORY_BACKGROUND_WORKERS)
            ]
            try:
                for _ in passes:
                    assert await asyncio.to_thread(passes_started.acquire, True, _WAIT)
            finally:
                release.set()
                await asyncio.gather(*held, *passes)

        asyncio.run(run())

    def test_consolidation_and_backfill_never_use_the_turn_pool(self) -> None:
        """Every store pass of a consolidation, and the gateway's backfill and
        repair loops, are offloaded to the background pool."""
        from kiro_crew import history_consolidation_pass
        from kiro_crew.slack.gateway_runtime import memory_lifecycle

        def called(module) -> set[str]:
            tree = ast.parse(inspect.getsource(module))
            out = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    out.add(node.func.id)
            return out

        consolidation = called(history_consolidation_pass)
        assert "_run_in_memory_background_pool" in consolidation
        assert "run_in_embed_pool" not in consolidation
        gateway = called(memory_lifecycle)
        assert "memory_background_executor" in gateway
        assert "embed_executor" not in gateway

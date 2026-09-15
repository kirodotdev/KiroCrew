"""The knowledge store's orphan sweep runs after the bind, not inside ``__init__``.

Boot cost is the reason: the orphan sweep is writer-locked and data-scaled,
and the constructor runs on the event-loop thread before the socket binds
(``setup_knowledge_routes`` reads the lazy store at route registration). A
sweep inside ``_migrate()`` would, on a large store, stall boot long enough for
the runtime's timeouts to kill the gateway. These tests pin four
properties: construction does not sweep, ``reclaim_orphans`` does the
sweeping, ``start_dashboard`` kicks it off-loop strictly after the listener is
up, and the sweep is ordered against concurrent ingestion by the store's
ingestion gate rather than by a clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
import time
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import server as srv
from kiro_crew.knowledge import ingestion as ingestion_mod
from kiro_crew.knowledge import store as store_mod
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.store import KnowledgeStore

# The store binds ``pysqlite3`` when it is installed, so both the raw reads and
# the connect patch below go through the SAME module the store uses.
sqlite3 = store_mod.sqlite3


def _insert_orphan_source(store: KnowledgeStore, sid: str, sync_status: str = "error") -> None:
    """A non-folder source holding nothing: the sweep's canonical target.

    The status is terminal by default: a source still owed an ingest
    ('pending', 'pending_confirmation', 'syncing') is not an orphan.
    """
    store.db.execute(
        "INSERT INTO sources (id, name, source_type, uri, sync_status, created_at, updated_at) "
        "VALUES (?, ?, 'file', ?, ?, '2026-01-01', '2026-01-01')",
        (sid, sid, f"file:///{sid}", sync_status),
    )
    store.db.commit()


def _source_exists(path: str, sid: str) -> bool:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT 1 FROM sources WHERE id = ?", (sid,)).fetchone() is not None
    finally:
        conn.close()


class TestConstructionDoesNotSweep:
    def test_reopening_leaves_an_orphan_source_in_place(self, tmp_path):
        """The behavioural pin: an orphan survives construction. Before the fix
        the reopen deleted it (see ``test_reclaim_orphans_removes_it`` for the
        presence control proving the row IS an orphan by the sweep's rules)."""
        path = str(tmp_path / "k.db")
        first = KnowledgeStore(path)
        _insert_orphan_source(first, "orphan")
        first.close()

        second = KnowledgeStore(path)
        try:
            assert _source_exists(path, "orphan"), "construction still runs the orphan sweep"
        finally:
            second.close()

    def test_migrate_does_not_call_the_sweep(self, tmp_path, monkeypatch):
        calls: list[int] = []
        monkeypatch.setattr(KnowledgeStore, "reclaim_orphans", lambda self: calls.append(1))
        s = KnowledgeStore(str(tmp_path / "k.db"))
        try:
            s._migrate()
        finally:
            s.close()
        assert calls == [], "_migrate() reached the orphan sweep"

    def test_migrate_takes_no_writer_transaction(self, tmp_path):
        """The cost being removed was the writer lock, so pin that directly:
        no ``BEGIN IMMEDIATE`` is issued anywhere in construction."""
        path = str(tmp_path / "k.db")
        KnowledgeStore(path).close()  # schema exists, so the reopen is pure _migrate
        statements: list[str] = []
        real_connect = sqlite3.connect

        def recording_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sqlite3, "connect", recording_connect)
            KnowledgeStore(path).close()
        assert not any(s.strip().upper().startswith("BEGIN IMMEDIATE") for s in statements)

    def test_source_locations_dedup_is_skipped_once_the_unique_index_exists(self, tmp_path):
        """The other data-scaled statement in ``_migrate``: a GROUP BY over
        ``source_locations`` that only has work to do BEFORE the unique index
        exists. Once it does, duplicates are impossible and the scan is skipped."""
        path = str(tmp_path / "k.db")
        KnowledgeStore(path).close()
        statements: list[str] = []
        real_connect = sqlite3.connect

        def recording_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sqlite3, "connect", recording_connect)
            KnowledgeStore(path).close()
        assert not any("DELETE FROM source_locations" in s and "GROUP BY" in s for s in statements)

    def test_source_locations_dedup_still_runs_on_a_legacy_database(self, tmp_path):
        """The gate must not skip the one open that needs it: a database with
        duplicate locations and no unique index is de-duplicated and indexed.

        A fresh schema declares ``UNIQUE (item_id, source_id)`` inline, so the
        legacy shape (no constraint, no index) is pre-created before the first
        open; ``CREATE TABLE IF NOT EXISTS`` then leaves it in place."""
        path = str(tmp_path / "k.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE source_locations (id TEXT PRIMARY KEY, item_id TEXT NOT NULL, "
            "source_id TEXT NOT NULL, chunk_range TEXT, section_title TEXT, anchor TEXT, "
            "created_at TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        s = KnowledgeStore(path)
        item = s.add_item("t", "c", "note")
        src = s.add_source("s", "file", "file:///s")
        s.add_source_location(item, src)
        s.close()
        conn = sqlite3.connect(path)
        conn.execute("DROP INDEX idx_source_locations_item_source")
        conn.execute(
            "INSERT INTO source_locations (id, item_id, source_id, created_at) "
            "VALUES ('dup', ?, ?, '2026-01-01')",
            (item, src),
        )
        conn.commit()
        conn.close()

        reopened = KnowledgeStore(path)
        try:
            n = reopened.db.execute(
                "SELECT COUNT(*) FROM source_locations WHERE item_id = ? AND source_id = ?",
                (item, src),
            ).fetchone()[0]
            assert n == 1
            assert (
                reopened.db.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type = 'index' "
                    "AND name = 'idx_source_locations_item_source'"
                ).fetchone()
                is not None
            )
        finally:
            reopened.close()


class TestReclaimOrphansStillSweeps:
    def test_reclaim_orphans_removes_it(self, tmp_path):
        path = str(tmp_path / "k.db")
        s = KnowledgeStore(path)
        try:
            _insert_orphan_source(s, "orphan")
            folder = s.add_source("vault", "local_folder", "/tmp/vault")
            s.reclaim_orphans()
            assert not _source_exists(path, "orphan")
            # The exclusions the sweep always had are preserved with it.
            assert _source_exists(path, folder)
        finally:
            s.close()

    def test_reclaim_refreshes_an_already_loaded_graph(self, tmp_path):
        """New obligation of running AFTER readers exist: an entity nothing
        references is pruned from the tables, so a graph a reader already
        materialised must not keep serving it."""
        path = str(tmp_path / "k.db")
        s = KnowledgeStore(path)
        try:
            eid = s.add_entity(name="lonely", entity_type="concept")
            s.ensure_graph_loaded()
            assert s.graph.has_node(eid)
            s.reclaim_orphans()
            assert not s.graph.has_node(eid)
        finally:
            s.close()

    def test_reclaim_reads_the_loaded_flag_and_refreshes_under_the_graph_lock(self, tmp_path):
        """A first graph load racing the sweep must not publish pruned entities:
        the flag check and the refresh run under ``_graph_lock``, so a load
        either finishes first (and is then refreshed) or reads after the prune."""
        s = KnowledgeStore(str(tmp_path / "k.db"))
        try:
            s.ensure_graph_loaded()
            held: list[bool] = []
            original = KnowledgeStore._load_graph

            def observing_load(self):
                held.append(self._graph_lock._is_owned())
                return original(self)

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(KnowledgeStore, "_load_graph", observing_load)
                s.reclaim_orphans()
            assert held == [True]
        finally:
            s.close()

    def test_reclaim_does_not_materialise_an_unloaded_graph(self, tmp_path):
        s = KnowledgeStore(str(tmp_path / "k.db"))
        try:
            s.reclaim_orphans()
            assert s._graph_loaded is False
        finally:
            s.close()

    def test_store_is_readable_while_the_sweep_runs(self, tmp_path):
        """The sweep holds SQLite's writer lock; a reader on another thread
        (its own connection, WAL mode) must not wait on it."""
        path = str(tmp_path / "k.db")
        s = KnowledgeStore(path)
        item = s.add_item("t", "c", "note")
        _insert_orphan_source(s, "orphan")
        inside_sweep = threading.Event()
        release = threading.Event()
        original_prune = KnowledgeStore._prune_orphan_entities

        def blocking_prune(self):
            # Runs inside the sweep's BEGIN IMMEDIATE: the writer lock is held.
            inside_sweep.set()
            assert release.wait(5), "reader never released the sweep"
            return original_prune(self)

        read_result: list = []

        def read_in_thread():
            reader = KnowledgeStore(path)
            try:
                read_result.append(reader.get_item(item))
            finally:
                reader.close()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(KnowledgeStore, "_prune_orphan_entities", blocking_prune)
            sweeper = threading.Thread(target=s.reclaim_orphans)
            sweeper.start()
            try:
                assert inside_sweep.wait(5)
                reader = threading.Thread(target=read_in_thread)
                reader.start()
                reader.join(5)
                assert not reader.is_alive(), "reader blocked behind the sweep's writer lock"
            finally:
                release.set()
                sweeper.join(5)
        assert read_result and read_result[0] is not None
        assert not _source_exists(path, "orphan")
        s.close()


class _QuiescentStore:
    """A store stub whose maintenance window is always free."""

    @contextlib.contextmanager
    def maintenance_window(self, timeout=None):
        yield True


class TestGatewayKick:
    def test_kick_runs_the_sweep_off_loop_as_a_tracked_task(self, monkeypatch):
        loop_thread = threading.get_ident()
        seen: list[int] = []

        class _Store(_QuiescentStore):
            def reclaim_orphans(self):
                seen.append(threading.get_ident())

        state = SimpleNamespace(_knowledge_store=_Store(), _background_tasks=set())

        async def run():
            srv._kick_knowledge_orphan_reclaim(state)
            assert len(state._background_tasks) == 1
            await asyncio.gather(*state._background_tasks)

        asyncio.run(run())
        assert len(seen) == 1
        assert seen[0] != loop_thread, "the sweep ran on the event-loop thread"
        assert state._background_tasks == set()

    def test_kick_does_not_build_a_store_nobody_constructed(self):
        built: list[int] = []

        class _State:
            _background_tasks: set = set()
            _knowledge_store = None

            @property
            def knowledge_store(self):
                built.append(1)
                return None

        state = _State()

        async def run():
            srv._kick_knowledge_orphan_reclaim(state)
            await asyncio.gather(*state._background_tasks)

        asyncio.run(run())
        assert built == []

    def test_a_failing_sweep_is_logged_not_raised(self, caplog):
        class _Store(_QuiescentStore):
            def reclaim_orphans(self):
                raise sqlite3.OperationalError("database is locked")

        state = SimpleNamespace(_knowledge_store=_Store(), _background_tasks=set())

        async def run():
            srv._kick_knowledge_orphan_reclaim(state)
            await asyncio.gather(*state._background_tasks)

        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.server"):
            asyncio.run(run())
        assert any("orphan reclaim failed" in r.message for r in caplog.records)

    def test_start_dashboard_kicks_after_the_listener_binds(self):
        source = inspect.getsource(srv.start_dashboard)
        kick = "_kick_knowledge_orphan_reclaim(state)"
        assert kick in source
        assert source.index("_start_site(site, port)") < source.index(kick)
        assert "_kick_knowledge_orphan_reclaim" not in inspect.getsource(KnowledgeStore.__init__)


def _entity_exists(store: KnowledgeStore, eid: str) -> bool:
    return store.db.execute("SELECT 1 FROM entities WHERE id = ?", (eid,)).fetchone() is not None


def _mention_exists(store: KnowledgeStore, item_id: str, eid: str) -> bool:
    row = store.db.execute(
        "SELECT 1 FROM mentions WHERE item_id = ? AND entity_id = ?", (item_id, eid)
    ).fetchone()
    return row is not None


def _kick_worker(store: KnowledgeStore, *, timeout: float | None = None):
    """The deferred worker exactly as ``_kick_knowledge_orphan_reclaim`` builds it.

    Captured instead of run so the tests can start it on a thread of their own
    choosing; ``timeout`` narrows the maintenance wait for the bounded case."""
    captured: list = []

    async def fake_to_thread(fn, *args, **kwargs):
        captured.append(fn)

    state = SimpleNamespace(_knowledge_store=store, _background_tasks=set())

    async def run():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(srv.asyncio, "to_thread", fake_to_thread)
            srv._kick_knowledge_orphan_reclaim(state)
            await asyncio.gather(*state._background_tasks)

    asyncio.run(run())
    assert len(captured) == 1, "the kick did not hand exactly one worker to to_thread"
    worker = captured[0]
    if timeout is None:
        return worker
    original = store.maintenance_window

    def narrowed(_timeout=store_mod.MAINTENANCE_WAIT_SECS):
        return original(timeout)

    store.maintenance_window = narrowed  # type: ignore[method-assign]
    return worker


def _run_in_thread(fn) -> threading.Thread:
    # Non-daemon on purpose: a worker that outlives a failed assertion must be
    # joined by the test's ``finally`` rather than escape into the next test.
    th = threading.Thread(target=fn)
    th.start()
    return th


class TestSweepWaitsForIngestion:
    """The sweep is ordered against ingestion by the store's gate, not a clock.

    ``ingestion_in_flight`` brackets one whole ingest; ``maintenance_window``
    waits until no holder remains and holds new entrants off while the sweep
    runs. A source, an entity and a mention written while the gate is held
    therefore survive a sweep scheduled at any point during the ingest."""

    def test_sweep_scheduled_during_an_ingest_waits_and_rows_added_survive(self, tmp_path):
        path = str(tmp_path / "k.db")
        store = KnowledgeStore(path)
        try:
            _insert_orphan_source(store, "orphan")
            worker = _kick_worker(store)
            swept = threading.Event()
            sweeper: threading.Thread | None = None

            def sweep():
                worker()
                swept.set()

            with store.ingestion_in_flight():
                sweeper = _run_in_thread(sweep)
                # The ingest's half-states, written one autocommit at a time.
                sid = store.add_source("fresh", "file", "file:///fresh")
                item_id = store.add_item("doc", "body", "text", source_id=sid)
                eid = store.add_entity("Fresh", "concept")
                assert not swept.wait(0.3), "the sweep ran while an ingest held the gate"
                assert _source_exists(path, sid) and _entity_exists(store, eid)
                store.add_mention(item_id, eid, context="ctx")
            sweeper.join(5)
            assert swept.is_set(), "the sweep did not run once the ingest finished"
            assert _source_exists(path, sid), "the sweep deleted a source ingested under the gate"
            assert _entity_exists(store, eid) and _mention_exists(store, item_id, eid)
            assert not _source_exists(path, "orphan"), "the pre-boot orphan was not reclaimed"
        finally:
            if sweeper is not None:
                sweeper.join(5)
            store.close()

    def test_an_ingest_starting_while_the_sweep_holds_the_window_waits(self, tmp_path):
        store = KnowledgeStore(str(tmp_path / "k.db"))
        ingester: threading.Thread | None = None
        try:
            entered = threading.Event()

            def ingest():
                with store.ingestion_in_flight():
                    entered.set()

            with store.maintenance_window() as quiescent:
                assert quiescent is True
                ingester = _run_in_thread(ingest)
                assert not entered.wait(0.3), "an ingest started under the maintenance window"
            ingester.join(5)
            assert entered.is_set(), "the ingest never resumed after the window closed"
        finally:
            if ingester is not None:
                ingester.join(5)
            store.close()

    def test_bounded_wait_skips_and_logs_when_ingestion_never_drains(self, tmp_path, caplog):
        path = str(tmp_path / "k.db")
        store = KnowledgeStore(path)
        try:
            _insert_orphan_source(store, "orphan")
            worker = _kick_worker(store, timeout=0.2)
            with store.ingestion_in_flight():
                with caplog.at_level("WARNING", logger="kiro_crew.knowledge.store"):
                    started = time.monotonic()
                    worker()
                assert time.monotonic() - started < 5
            assert _source_exists(path, "orphan"), "a skipped sweep still deleted rows"
            assert any("maintenance skipped" in r.message for r in caplog.records)
        finally:
            store.close()

    def test_pre_boot_orphan_is_reclaimed_when_nothing_is_ingesting(self, tmp_path):
        path = str(tmp_path / "k.db")
        store = KnowledgeStore(path)
        try:
            _insert_orphan_source(store, "orphan")
            _kick_worker(store)()
            assert not _source_exists(path, "orphan")
        finally:
            store.close()

    @pytest.mark.parametrize(
        "status", ["pending", "pending_confirmation", "syncing", "active", "paused"]
    )
    def test_a_source_owed_an_ingest_survives_the_sweep(self, tmp_path, status):
        """Only a source whose ingest ran to an end state ('synced', 'error',
        'missing') is reclaimable. The upload and add-source handlers insert the
        row and only later hand it to a background ingest, whose gate and job row
        appear after the insert; 'active' is a producible initial status for a
        source a feature fills later; 'paused' is user-set. Each is written by
        the INSERT itself, so the sweep never sees such a row as an orphan."""
        path = str(tmp_path / "k.db")
        store = KnowledgeStore(path)
        try:
            _insert_orphan_source(store, "owed", sync_status=status)
            fresh = store.add_source("fresh", "local_file", "upload://fresh.md")
            _insert_orphan_source(store, "done")
            _kick_worker(store)()
            assert _source_exists(path, "owed")
            assert _source_exists(path, fresh)
            assert not _source_exists(path, "done")
        finally:
            store.close()

    @pytest.mark.parametrize("status", ["synced", "error", "missing"])
    def test_a_finished_itemless_source_is_reclaimed(self, tmp_path, status):
        path = str(tmp_path / "k.db")
        store = KnowledgeStore(path)
        try:
            _insert_orphan_source(store, "done", sync_status=status)
            _kick_worker(store)()
            assert not _source_exists(path, "done")
        finally:
            store.close()

    def test_sweep_deletes_in_short_writer_transactions(self, tmp_path):
        """A knowledge write issued on the event loop while the sweep runs waits
        for at most one chunk: the candidate list is read outside a transaction
        and the deletes run as one short ``BEGIN IMMEDIATE`` per chunk, plus one
        for the entity prune."""
        path = str(tmp_path / "k.db")
        store = KnowledgeStore(path)
        try:
            total = store_mod._RECLAIM_CHUNK * 2 + 7
            for i in range(total):
                _insert_orphan_source(store, f"o{i}")
            statements: list[str] = []
            store.db.set_trace_callback(statements.append)
            store.reclaim_orphans()
            store.db.set_trace_callback(None)
            begins = [st for st in statements if st.strip().upper().startswith("BEGIN IMMEDIATE")]
            assert len(begins) == 3 + 1
            assert store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        finally:
            store.close()

    def test_an_empty_aggregate_container_source_survives_the_sweep(self, tmp_path):
        """The agent-document and artifact sources are created empty and
        'active' the first time their feature needs them; the first document
        lands in a later write. Like folder sources they are containers, so an
        empty one is not an orphan."""
        path = str(tmp_path / "k.db")
        store = KnowledgeStore(path)
        try:
            agent = store.add_source(
                "Agent documents", "agent", "agent://", properties={"sync_status": "active"}
            )
            artifact = store.add_source(
                "Artifacts", "artifact", "artifact://", properties={"sync_status": "active"}
            )
            _insert_orphan_source(store, "done")
            _kick_worker(store)()
            assert _source_exists(path, agent)
            assert _source_exists(path, artifact)
            assert not _source_exists(path, "done")
        finally:
            store.close()

    def test_ingest_file_holds_the_gate_for_its_whole_run(self, tmp_path):
        """The pipeline's public entry brackets the whole ingest: while it is
        inside, a maintenance window cannot be acquired, and it can once the
        ingest returns."""
        store = KnowledgeStore(str(tmp_path / "k.db"))
        pipeline = IngestionPipeline.__new__(IngestionPipeline)
        pipeline.store = store
        pipeline._import_budget = ingestion_mod.ImportChunkBudget()
        seen: list[bool] = []

        async def fake_body(*args, **kwargs):
            with store.maintenance_window(timeout=0.05) as quiescent:
                seen.append(quiescent)
            return "job"

        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(pipeline, "_ingest_file_impl", fake_body)
                assert asyncio.run(pipeline.ingest_file("/nowhere")) == "job"
            assert seen == [False], "the maintenance window opened during ingest_file"
            with store.maintenance_window(timeout=0.05) as quiescent:
                assert quiescent is True, "the gate stayed held after ingest_file returned"
        finally:
            store.close()

    def test_a_nested_hold_by_the_same_task_does_not_wait_behind_a_waiting_window(self, tmp_path):
        """The store gate holds new entrants off once a maintenance window is
        waiting for the current holders to drain; a caller that already holds
        the gate and enters again (the ingest call inside a handler's span)
        must pass at once, not wait for the window's timeout."""
        store = KnowledgeStore(str(tmp_path / "k.db"))
        pipeline = IngestionPipeline.__new__(IngestionPipeline)
        pipeline.store = store
        window_waiting = threading.Event()
        window_result: list[bool] = []

        def open_window():
            window_waiting.set()
            with store.maintenance_window(timeout=2.0) as quiescent:
                window_result.append(quiescent)

        async def scenario() -> float:
            async with pipeline.ingestion_in_flight():
                th = threading.Thread(target=open_window)
                th.start()
                assert window_waiting.wait(2)
                await asyncio.sleep(0.1)  # the window is now waiting on the holder
                started = time.monotonic()
                async with pipeline.ingestion_in_flight():
                    pass
                elapsed = time.monotonic() - started
                return elapsed

        try:
            elapsed = asyncio.run(scenario())
            assert elapsed < 0.5, f"nested hold waited {elapsed:.2f}s behind the window"
        finally:
            store.close()

    def test_public_ingestion_in_flight_holds_the_gate_across_a_caller_span(self, tmp_path):
        """A caller that looks up a source and ingests into it later holds
        ``pipeline.ingestion_in_flight()`` across the whole span; the nested
        hold the ingest takes for itself is fine, and the window is free once
        the outer hold ends."""
        store = KnowledgeStore(str(tmp_path / "k.db"))
        pipeline = IngestionPipeline.__new__(IngestionPipeline)
        pipeline.store = store
        pipeline._import_budget = ingestion_mod.ImportChunkBudget()
        seen: list[bool] = []

        async def fake_body(*args, **kwargs):
            return "job"

        async def caller():
            async with pipeline.ingestion_in_flight():
                with store.maintenance_window(timeout=0.05) as quiescent:
                    seen.append(quiescent)
                return await pipeline.ingest_file("/nowhere")

        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(pipeline, "_ingest_file_impl", fake_body)
                assert asyncio.run(caller()) == "job"
            assert seen == [False], "the maintenance window opened inside the caller's hold"
            with store.maintenance_window(timeout=0.05) as quiescent:
                assert quiescent is True, "the gate stayed held after the caller returned"
        finally:
            store.close()


class TestWatcherReReadsUnderTheGate:
    @pytest.mark.asyncio
    async def test_a_local_file_source_deleted_before_the_gate_is_not_reingested(self, tmp_path):
        """The sweep's snapshot of ``sources`` is read outside the ingestion
        gate, so a row the orphan sweep takes in between is still in the list.
        The watcher re-reads the row once it holds the gate and skips a row that
        is gone instead of ingesting items for a deleted source."""
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.knowledge.watcher import KnowledgeWatcher

        store = KnowledgeStore(str(tmp_path / "k.db"))
        try:
            path = tmp_path / "notes.md"
            path.write_text("# notes\n")
            sid = store.add_source(
                name="notes", source_type="local_file", uri=str(path), properties={"mtime": 0}
            )
            store.update_source(sid, sync_status="error")
            pipeline = MagicMock()
            pipeline.ingest_file = AsyncMock(return_value="job")

            @contextlib.asynccontextmanager
            async def _gate_that_loses_the_row():
                # The orphan sweep ran while this hold was being taken.
                store.db.execute("DELETE FROM sources WHERE id = ?", (sid,))
                store.db.commit()
                yield

            pipeline.ingestion_in_flight = _gate_that_loses_the_row
            w = KnowledgeWatcher(store, pipeline)
            w._folder_watcher = MagicMock()
            w._folder_watcher.scan_source = AsyncMock(return_value={})
            w._maybe_dedup_sweep = AsyncMock()
            w._maybe_reembed_stale = AsyncMock()
            await w._scan()
            pipeline.ingest_file.assert_not_awaited()
            assert store.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        finally:
            store.close()


class TestHandoffJoinsTheHoldersAdmission:
    def test_a_task_started_by_a_holder_is_not_held_behind_a_waiting_window(self, tmp_path):
        """A handler holds the gate and starts a background task that must take
        its own hold before the handler lets go; a maintenance window begins
        waiting in between. Without the hand-off admission the three wait on
        each other until the window's timeout. With it the task's first hold
        joins the holder's admission at once, the handler releases, and the
        window then gets its turn."""
        store = KnowledgeStore(str(tmp_path / "k.db"))
        pipeline = IngestionPipeline.__new__(IngestionPipeline)
        pipeline.store = store
        window_waiting = threading.Event()
        window_result: list[bool] = []

        def open_window():
            window_waiting.set()
            with store.maintenance_window(timeout=5.0) as quiescent:
                window_result.append(quiescent)

        async def scenario() -> float:
            taken = asyncio.Event()

            async def child() -> None:
                async with pipeline.ingestion_in_flight():
                    taken.set()
                    await asyncio.sleep(0.05)

            started = time.monotonic()
            async with pipeline.ingestion_in_flight():
                th = threading.Thread(target=open_window)
                th.start()
                assert window_waiting.wait(2)
                await asyncio.sleep(0.1)  # the window is now waiting on this hold
                task = asyncio.create_task(child(), context=ingestion_mod.handoff_gate_context())
                await asyncio.wait_for(taken.wait(), timeout=2.0)
            await task
            th.join(timeout=10)
            return time.monotonic() - started

        try:
            elapsed = asyncio.run(scenario())
            assert elapsed < 2.0, f"hand-off waited {elapsed:.2f}s behind the window"
            assert window_result == [True], "the window did not get its turn after the holds"
        finally:
            store.close()

    def test_the_admission_covers_only_the_first_hold(self, tmp_path):
        """The task's later, separate holds wait like any other entrant."""
        store = KnowledgeStore(str(tmp_path / "k.db"))
        pipeline = IngestionPipeline.__new__(IngestionPipeline)
        pipeline.store = store

        async def scenario() -> bool:
            async def child() -> bool:
                async with pipeline.ingestion_in_flight():
                    pass
                return ingestion_mod._INGESTION_GATE_ADMITTED.get()

            return await asyncio.create_task(child(), context=ingestion_mod.handoff_gate_context())

        try:
            assert asyncio.run(scenario()) is False
        finally:
            store.close()

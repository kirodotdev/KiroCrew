"""Every Knowledge Library endpoint must reach the store off the event loop.

The gateway runs one asyncio loop. ``KnowledgeStore`` opens its connection with
``busy_timeout=10000``, so a single query taken ON that loop while an ingest
holds the write lock parks every task for up to ten seconds -- the watchdog
heartbeat with them. Reports from the field show that stall arriving daily and
escalating: the watchdog fires, the gateway self-exits, and on a host with no
cgroup ceilings the restart storm takes journald and sshd down with it.

The oracle is the store's own guard. ``KnowledgeStore.db`` calls
``OnLoopDBGuard.check()``, which RAISES ``OnLoopStoreError`` when
``KIROCREW_STRICT_ON_LOOP_STORE`` is set and a connection is taken with a
running loop. Arming that switch and driving each endpoint through a real
``TestClient`` therefore turns "this handler blocks the loop" into a test
failure -- and it catches the interprocedural case too, where the handler calls
a plain synchronous store method that takes the connection one frame down and no
lexical AST scan can see it.

``TestOracleHasTeeth`` is the mutation guard: it proves the armed guard really
does fail an on-loop take, so a green sweep above means the offloads are real
rather than the switch being inert.

Moving a write into a worker adds an ``await`` where there was none, so the
classes below pin what that ``await`` must not break: a check whose caller
re-reads it guards nothing (``TestSyncClaimIsAtomic``,
``TestSourceInsertIsTheArbiter``), a JSON blob read on one side of it and
written on the other loses a concurrent edit (``TestPropertiesBlobSurvives``),
a terminal state can be overwritten by a late second write
(``TestJobFinalizeIsCompareAndSet``), and an audit line written after the
worker returned can be dropped by a cancellation
(``TestAuditRidesWithTheWrite``).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import pathlib
import threading
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.knowledge.store import _ON_LOOP_DB_GUARD, KnowledgeStore
from kiro_crew.on_loop_db import OnLoopStoreError


@pytest.fixture()
def store(tmp_path):
    """A store built OFF the loop -- construction is the one sanctioned take."""
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _reset_throttle():
    """The guard is module-level, so its throttle clock leaks between tests."""
    _ON_LOOP_DB_GUARD.reset_throttle()
    yield
    _ON_LOOP_DB_GUARD.reset_throttle()


class _FakeEmbedder:
    """Stand-in for the real embedder: no model is ever loaded."""

    model = "fake-embed:1"
    content_budget = 2000

    async def is_available_async(self) -> bool:
        return True

    def embed_for_item(self, title, summary, content):
        return [0.1, 0.2, 0.3, 0.4]

    def embed(self, text):
        return [0.1, 0.2, 0.3, 0.4]


def _make_app(store, *, embedder=None, pipeline=None):
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    if embedder is not None:
        app["knowledge_embedder"] = embedder
    if pipeline is not None:
        app["knowledge_pipeline"] = pipeline
    app["knowledge_llm_pool"] = MagicMock(shutdown=AsyncMock())
    # No connector for any type, so add_source takes its generic https branch.
    app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=None))

    r = app.router
    r.add_get("/api/knowledge/namespaces", kh.list_namespaces)
    r.add_get("/api/knowledge/stats", kh.get_stats)
    r.add_get("/api/knowledge/items", kh.list_items)
    r.add_get("/api/knowledge/items/{id}", kh.get_item)
    r.add_patch("/api/knowledge/items/{id}", kh.update_item)
    r.add_delete("/api/knowledge/items/{id}", kh.delete_item)
    r.add_get("/api/knowledge/items/{id}/content", kh.get_item_content)
    r.add_get("/api/knowledge/items/{id}/related", kh.get_related_items)
    r.add_get("/api/knowledge/items/{id}/export", kh.export_item)
    r.add_get("/api/knowledge/entities", kh.list_entities)
    r.add_get("/api/knowledge/entities/by-name/{name}/items", kh.get_entity_items)
    r.add_get("/api/knowledge/export", kh.export_all)
    r.add_get("/api/knowledge/jobs/{id}", kh.get_job)
    r.add_get("/api/knowledge/sources", kh.list_sources)
    r.add_post("/api/knowledge/sources", kh.add_source)
    r.add_patch("/api/knowledge/sources/{id}", kh.rename_source)
    r.add_get("/api/knowledge/sources/{id}/files", kh.list_source_files)
    r.add_post("/api/knowledge/sources/{id}/files/retry", kh.retry_file)
    r.add_post("/api/knowledge/sources/{id}/files/skip", kh.skip_file)
    r.add_post("/api/knowledge/sources/{id}/confirm", kh.confirm_source)
    r.add_post("/api/knowledge/sources/{id}/pause", kh.pause_source)
    r.add_post("/api/knowledge/sources/{id}/resume", kh.resume_source)
    r.add_get("/api/knowledge/embedding/status", kh.get_embedding_status)
    r.add_post("/api/knowledge/embedding/generate", kh.batch_embed_items)
    r.add_post("/api/knowledge/import", kh.import_bundle)
    if pipeline is not None:
        r.add_post("/api/knowledge/ingest", kh.ingest_file)
    return app


def _seed(store) -> dict:
    """One source, one item, one entity, one job -- built off the loop."""
    source_id = store.add_source(
        name="folder",
        source_type="local_folder",
        uri="/tmp/kb-fixture",
        properties={"sync_status": "active"},
    )
    item_id = store.add_item("title", "body text", "note", source_id=source_id)
    entity_id = store.add_entity("Widget", "product")
    store.add_mention(item_id, entity_id, context="body text")
    store.db.execute(
        "INSERT OR REPLACE INTO folder_file_state "
        "(source_id, file_path, content_hash, text_hash, mtime, item_ids, last_seen, "
        "status, error_message, attempts) "
        "VALUES (?, ?, 'h', 'h', 0, '[]', '2026-01-01T00:00:00', 'failed', 'boom', 1)",
        (source_id, "/tmp/kb-fixture/a.md"),
    )
    store.db.execute(
        "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
        "VALUES ('job-1', NULL, 'completed', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    store.db.commit()
    return {"source_id": source_id, "item_id": item_id, "entity_id": entity_id}


# Every endpoint this module owns, as (method, path template, json body).
# ``{item}`` / ``{source}`` are filled from the seeded fixture.
_ENDPOINTS = [
    ("GET", "/api/knowledge/namespaces", None),
    ("GET", "/api/knowledge/stats", None),
    ("GET", "/api/knowledge/items", None),
    ("GET", "/api/knowledge/items?source_id={source}", None),
    ("GET", "/api/knowledge/items/{item}", None),
    ("GET", "/api/knowledge/items/{item}/content", None),
    ("GET", "/api/knowledge/items/{item}/related", None),
    ("GET", "/api/knowledge/items/{item}/export", None),
    ("GET", "/api/knowledge/entities", None),
    ("GET", "/api/knowledge/entities?q=Wid", None),
    ("GET", "/api/knowledge/entities/by-name/Widget/items", None),
    ("GET", "/api/knowledge/export", None),
    ("GET", "/api/knowledge/jobs/job-1", None),
    ("GET", "/api/knowledge/sources", None),
    ("GET", "/api/knowledge/sources/{source}/files", None),
    ("GET", "/api/knowledge/embedding/status", None),
    ("PATCH", "/api/knowledge/items/{item}", {"title": "renamed"}),
    ("PATCH", "/api/knowledge/sources/{source}", {"name": "renamed"}),
    (
        "POST",
        "/api/knowledge/sources",
        {"uri": "https://example.com/doc", "source_type": "web", "name": "doc"},
    ),
    ("POST", "/api/knowledge/sources/{source}/files/retry", {"file_path": "/tmp/kb-fixture/a.md"}),
    ("POST", "/api/knowledge/sources/{source}/files/skip", {"file_path": "/tmp/kb-fixture/a.md"}),
    ("POST", "/api/knowledge/sources/{source}/pause", {}),
    ("POST", "/api/knowledge/sources/{source}/resume", {}),
    ("POST", "/api/knowledge/sources/{source}/confirm", {}),
    ("DELETE", "/api/knowledge/items/{item}", None),
]


class TestEndpointsNeverTakeTheConnectionOnTheLoop:
    """Strict mode turns an on-loop connection take into a 500."""

    @pytest.mark.parametrize(
        "method,template,body", _ENDPOINTS, ids=[f"{m} {t}" for m, t, _b in _ENDPOINTS]
    )
    @pytest.mark.asyncio
    async def test_endpoint_is_off_loop(self, store, monkeypatch, method, template, body):
        ids = await asyncio.to_thread(_seed, store)
        path = template.format(item=ids["item_id"], source=ids["source_id"])
        # Armed only now: seeding above deliberately runs off-loop, and the
        # fixture's own construction is the sanctioned on-loop take.
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.request(method, path, json=body)
        # 500 is what an OnLoopStoreError surfaces as; any other status means the
        # handler answered without taking the connection on the loop.
        assert resp.status != 500, f"{method} {path} took the connection on the loop"

    @pytest.mark.asyncio
    async def test_stats_with_embedder_is_off_loop(self, store, monkeypatch):
        """The reported stack: ``get_stats`` -> four unfiltered ``COUNT(*)``.

        With an embedder configured the handler also counts embedded items, so
        this is the variant that ran two on-loop takes rather than one.
        """
        await asyncio.to_thread(_seed, store)
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        app = _make_app(store, embedder=_FakeEmbedder())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/knowledge/stats")
            assert resp.status == 200
            body = await resp.json()
        assert body["items"] == 1
        assert body["embeddings"] == {
            "enabled": True,
            "provider": "llama_cpp",
            "model": "fake-embed:1",
            "available": True,
            "embedded_items": 0,
        }

    @pytest.mark.asyncio
    async def test_batch_embed_is_off_loop(self, store, monkeypatch):
        """The fill-NULL embedding pass reads, writes and re-counts off-loop."""
        await asyncio.to_thread(_seed, store)
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        app = _make_app(store, embedder=_FakeEmbedder())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/knowledge/embedding/generate", json={})
            assert resp.status == 200
            body = await resp.json()
        assert body["embedded"] == 1
        assert body["remaining"] == 0

    @pytest.mark.asyncio
    async def test_import_bundle_is_off_loop(self, store, monkeypatch):
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        bundle = {
            "items": [],
            "entities": [],
            "relations": [],
            "sources": [],
            "source_locations": [],
            "mentions": [],
        }
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post(
                "/api/knowledge/import",
                data=json.dumps(bundle),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status != 500


class TestOracleHasTeeth:
    """The mutation guard: the armed switch must fail a real on-loop take.

    Without this, a green sweep above would also be the result of the guard
    being inert -- the exact way an offload test passes while the loop still
    blocks.
    """

    @pytest.mark.asyncio
    async def test_direct_query_on_the_loop_raises(self, store, monkeypatch):
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        with pytest.raises(OnLoopStoreError):
            store.db.execute("SELECT COUNT(*) FROM items").fetchone()

    @pytest.mark.asyncio
    async def test_store_method_on_the_loop_raises(self, store, monkeypatch):
        """The interprocedural shape the reports carried: a plain store method
        called from a coroutine, taking the connection one frame down."""
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        with pytest.raises(OnLoopStoreError):
            store.get_stats()

    @pytest.mark.asyncio
    async def test_handler_still_500s_when_a_helper_is_run_inline(self, store, monkeypatch):
        """Proof the sweep's 500 assertion can fail: put one helper back on the
        loop and the endpoint reds, which is what the pre-fix code did."""
        await asyncio.to_thread(_seed, store)
        monkeypatch.setattr(
            kh.asyncio, "to_thread", AsyncMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
        )
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.get("/api/knowledge/namespaces")
        assert resp.status == 500


class TestSyncClaimIsAtomic:
    """A sync claim is one statement, so two starts cannot both win."""

    @pytest.mark.asyncio
    async def test_only_one_of_two_concurrent_claims_wins(self, store):
        source_id = await asyncio.to_thread(_seed_source, store)
        first, second = await asyncio.gather(
            asyncio.to_thread(kh._claim_sync, store, source_id),
            asyncio.to_thread(kh._claim_sync, store, source_id),
        )
        assert sorted([first, second]) == [False, True]
        assert await asyncio.to_thread(_sync_status, store, source_id) == "syncing"

    @pytest.mark.asyncio
    async def test_a_lost_claim_leaves_the_row_untouched(self, store):
        """The loser must not restamp: the winner owns the row's next state."""
        source_id = await asyncio.to_thread(_seed_source, store)
        assert await asyncio.to_thread(kh._claim_sync, store, source_id) is True
        await asyncio.to_thread(_set_status, store, source_id, "synced")
        # A second claim now wins again, because 'synced' is claimable -- what is
        # never claimable is a row another sync is already holding.
        assert await asyncio.to_thread(kh._claim_sync, store, source_id) is True
        assert await asyncio.to_thread(kh._claim_sync, store, source_id) is False

    @pytest.mark.asyncio
    async def test_ingest_task_returns_without_writing_when_claim_is_lost(self, store, tmp_path):
        """The task, not the handler, owns the row -- so a task that loses the
        claim must do nothing at all, including no 'error' stamp."""
        source_id = await asyncio.to_thread(_seed_source, store)
        assert await asyncio.to_thread(kh._claim_sync, store, source_id) is True
        pipeline = MagicMock(ingest_file=AsyncMock())
        await kh._ingest_local_file_task(pipeline, store, str(tmp_path / "absent.md"), source_id)
        pipeline.ingest_file.assert_not_awaited()
        assert await asyncio.to_thread(_sync_status, store, source_id) == "syncing"

    @pytest.mark.asyncio
    async def test_sync_endpoint_still_409s_while_a_sync_holds_the_row(self, store):
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.to_thread(_set_status, store, source_id, "syncing")
        app = _make_app(store)
        app.router.add_post("/api/knowledge/sources/{id}/sync", kh.sync_source)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/knowledge/sources/{source_id}/sync")
        assert resp.status == 409


class TestSourceInsertIsTheArbiter:
    """``sources.uri`` is UNIQUE, so the insert decides, not a prior read."""

    @pytest.mark.asyncio
    async def test_second_concurrent_insert_reports_the_winner(self, store):
        kwargs = dict(name="doc", source_type="web", uri="https://example.com/doc", properties={})
        first, second = await asyncio.gather(
            asyncio.to_thread(kh._add_source_unique, store, **kwargs),
            asyncio.to_thread(kh._add_source_unique, store, **kwargs),
        )
        assert sorted([first[1], second[1]]) == [False, True]
        assert first[0] == second[0], "both callers must be told the same row id"

    @pytest.mark.asyncio
    async def test_two_concurrent_posts_give_one_201_and_one_409(self, store):
        """The race itself: both requests read before either inserts, so the
        pre-check clears twice and only the constraint can separate them."""
        body = {"uri": "https://example.com/doc", "source_type": "web", "name": "doc"}
        async with TestClient(TestServer(_make_app(store))) as client:
            first, second = await asyncio.gather(
                client.post("/api/knowledge/sources", json=body),
                client.post("/api/knowledge/sources", json=body),
            )
        assert sorted([first.status, second.status]) == [201, 409]
        rows = await asyncio.to_thread(
            lambda: store.db.execute(
                "SELECT COUNT(*) FROM sources WHERE uri = ?", (body["uri"],)
            ).fetchone()[0]
        )
        assert rows == 1

    @pytest.mark.asyncio
    async def test_a_non_uri_integrity_error_still_raises(self, store, monkeypatch):
        """Only the uri collision is recovered. Swallowing every
        IntegrityError would turn a real schema violation into a silent 201."""

        def _boom(**kwargs):
            raise sqlite3.IntegrityError("NOT NULL constraint failed: sources.name")

        monkeypatch.setattr(store, "add_source", _boom)
        monkeypatch.setattr(store, "get_source_by_uri", lambda uri: None)
        with pytest.raises(sqlite3.IntegrityError):
            await asyncio.to_thread(
                kh._add_source_unique,
                store,
                name=None,
                source_type="web",
                uri="https://example.com/x",
                properties={},
            )


class TestAuditRidesWithTheWrite:
    """A mutation's SEL record is written by the worker that committed it."""

    @pytest.mark.asyncio
    async def test_audit_is_emitted_from_the_worker_thread(self, store, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(
            kh, "_sel_log", lambda tool, **kw: seen.append(threading.current_thread().name)
        )
        item_id = await asyncio.to_thread(store.add_item, "t", "body", "note")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.patch(f"/api/knowledge/items/{item_id}", json={"title": "renamed"})
            assert resp.status == 200
        assert seen, "no audit record was emitted"
        assert (
            threading.current_thread().name not in seen
        ), "the audit ran on the loop thread, so a cancellation can drop it"

    @pytest.mark.asyncio
    async def test_cancelling_the_caller_cannot_drop_a_committed_audit(self, store, monkeypatch):
        """The window the fix closes: cancel while the worker is mid-write."""
        started = threading.Event()
        audited: list[str] = []
        monkeypatch.setattr(kh, "_sel_log", lambda tool, **kw: audited.append(tool))

        def _slow_write():
            started.set()
            threading.Event().wait(0.2)

        task = asyncio.ensure_future(
            kh._audited_write(_slow_write, event="item.update", fields={"item_id": "x"})
        )
        await asyncio.to_thread(started.wait, 2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The worker cannot be interrupted, so it finished and audited.
        await asyncio.to_thread(threading.Event().wait, 0.4)
        assert audited == ["item.update"]


def _seed_source(store) -> str:
    return store.add_source(
        name="folder",
        source_type="local_folder",
        uri="/tmp/kb-claim",
        properties={"sync_status": "active"},
    )


def _sync_status(store, source_id: str) -> str:
    return store.db.execute(
        "SELECT sync_status FROM sources WHERE id = ?", (source_id,)
    ).fetchone()[0]


def _set_status(store, source_id: str, status: str) -> None:
    store.db.execute("UPDATE sources SET sync_status = ? WHERE id = ?", (status, source_id))
    store.db.commit()


class TestBackgroundTasksOwnTheSyncStamp:
    """A background task stamps the row it works on, and never on the loop."""

    @pytest.mark.asyncio
    async def test_upload_ingest_stamps_rather_than_claims(self, store, monkeypatch):
        """An upload has no single-flight rule. Claiming here would refuse the
        second upload of a filename and silently drop the file the user sent."""
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.to_thread(_set_status, store, source_id, "syncing")
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        # The stamp is unconditional: a row already 'syncing' still gets ingested.
        await asyncio.to_thread(kh._set_sync_status, store, source_id, "syncing")
        assert await asyncio.to_thread(_sync_status, store, source_id) == "syncing"
        assert (
            "_claim_sync" not in _ingest_file_source()
        ), "ingest_file's background task must stamp, not claim"

    @pytest.mark.asyncio
    async def test_rebuild_job_finalize_is_off_loop_on_every_path(self, store, monkeypatch):
        """Including cancellation. `start_rebuild_job` sweeps a stale
        'processing' row, so an interrupted finalize cannot block the guard, and
        nothing here needs to reach the store on the loop."""
        job_id = await asyncio.to_thread(_seed_job, store)
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        embedder = _FakeEmbedder()

        async def _boom(*a, **k):
            raise asyncio.CancelledError()

        monkeypatch.setattr(kh, "rebuild_embeddings", _boom)
        with pytest.raises(asyncio.CancelledError):
            await kh._rebuild_embeddings_job(_make_app(store), store, embedder, job_id)
        # An OnLoopStoreError would have replaced the CancelledError above, and
        # the row must still be finalized rather than left 'processing'.
        assert await asyncio.to_thread(_job_status, store, job_id) == "cancelled"

    @pytest.mark.asyncio
    async def test_rebuild_job_finalize_is_off_loop_on_failure(self, store, monkeypatch):
        job_id = await asyncio.to_thread(_seed_job, store)
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")

        async def _boom(*a, **k):
            raise RuntimeError("embedder exploded")

        monkeypatch.setattr(kh, "rebuild_embeddings", _boom)
        await kh._rebuild_embeddings_job(_make_app(store), store, _FakeEmbedder(), job_id)
        assert await asyncio.to_thread(_job_status, store, job_id) == "failed"


class TestAFailedClaimIsNotASyncFailure:
    """Failing to TAKE the work is not the work failing.

    ``sync_all`` skips a row whose ``sync_status`` is 'error' on every sweep
    (``sync.py``), so 'error' is terminal. A writer-lock timeout on the claim
    itself must therefore leave the row alone: stamping 'error' for it would
    quiesce a healthy source until a human re-syncs by hand.
    """

    @staticmethod
    def _raising_claim(calls):
        def _claim(_store, source_id):
            calls.append(source_id)
            raise sqlite3.OperationalError("database is locked")

        return _claim

    @pytest.mark.asyncio
    async def test_local_file_task_leaves_the_row_alone(self, store, monkeypatch):
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.to_thread(_set_status, store, source_id, "pending")
        claims: list[str] = []
        stamped: list[str] = []
        monkeypatch.setattr(kh, "_claim_sync", self._raising_claim(claims))
        monkeypatch.setattr(kh, "_set_sync_status", lambda _s, _sid, status: stamped.append(status))
        ingested: list[str] = []
        pipeline = MagicMock(ingest_file=AsyncMock(side_effect=lambda *a, **k: ingested.append(1)))

        await kh._ingest_local_file_task(pipeline, store, "/tmp/nope.md", source_id)

        assert claims == [source_id], "the claim was not even attempted"
        assert stamped == [], f"a failed claim wrote a status: {stamped}"
        assert ingested == [], "the ingest ran without holding the claim"
        assert await asyncio.to_thread(_sync_status, store, source_id) == "pending"

    @pytest.mark.asyncio
    async def test_agent_sync_leaves_the_row_alone(self, store, monkeypatch):
        """The twin. Both call sites must change together, or the reviewer hands
        the second one back next round."""
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.to_thread(_set_status, store, source_id, "synced")
        claims: list[str] = []
        stamped: list[str] = []
        monkeypatch.setattr(kh, "_claim_sync", self._raising_claim(claims))
        monkeypatch.setattr(kh, "_set_sync_status", lambda _s, _sid, status: stamped.append(status))
        fetched: list[str] = []
        monkeypatch.setattr(
            kh, "fetch_url_content", AsyncMock(side_effect=lambda *a, **k: fetched.append(1))
        )

        await kh._background_agent_sync(
            source_id, "https://example.com/x", "x", store, MagicMock(), MagicMock()
        )

        assert claims == [source_id]
        assert stamped == [], f"a failed claim wrote a status: {stamped}"
        assert fetched == [], "the fetch ran without holding the claim"
        assert await asyncio.to_thread(_sync_status, store, source_id) == "synced"

    @pytest.mark.asyncio
    async def test_a_lost_claim_still_returns_without_writing(self, store, monkeypatch):
        """The opposite arm: a claim that is merely LOST (another sync holds it)
        must keep its existing silent return, not become an error path."""
        source_id = await asyncio.to_thread(_seed_source, store)
        stamped: list[str] = []
        monkeypatch.setattr(kh, "_claim_sync", lambda _s, _sid: False)
        monkeypatch.setattr(kh, "_set_sync_status", lambda _s, _sid, status: stamped.append(status))
        pipeline = MagicMock(ingest_file=AsyncMock())

        await kh._ingest_local_file_task(pipeline, store, "/tmp/nope.md", source_id)

        assert stamped == []
        pipeline.ingest_file.assert_not_awaited()


class TestTheProgressStampCannotDiscardAnUpload:
    """An upload's staged temp file is its only server-side copy.

    ``upload://`` has no re-fetchable URI, and ``_bg_ingest``'s ``finally``
    unlinks the staged file, so a failure between the response and
    ``ingest_file`` destroys a file the client was already told was accepted.
    The pre-ingest 'syncing' write is a UI hint reached through a contended
    SQLite writer lock, so it is exactly the write that can fail there.
    """

    @staticmethod
    def _pipeline(seen_present):
        async def _ingest(tmp_path, **kwargs):
            seen_present.append(pathlib.Path(tmp_path).exists())

        return MagicMock(
            ingest_file=AsyncMock(side_effect=_ingest),
            reserve_import_budget=AsyncMock(return_value=None),
            release_import_budget=MagicMock(),
        )

    @staticmethod
    async def _upload(client):
        form = aiohttp.FormData()
        form.add_field("file", b"# hello\n", filename="notes.md", content_type="text/markdown")
        return await client.post("/api/knowledge/ingest", data=form)

    @pytest.mark.asyncio
    async def test_a_failed_stamp_still_ingests_the_upload(self, store, monkeypatch):
        """The data-loss window: the stamp raises, and the file must survive."""
        monkeypatch.setattr(kh, "_sel_log", lambda tool, **kw: None)
        stamped: list[str] = []

        def _stamp(_store, source_id, status):
            stamped.append(status)
            if status == "syncing":
                raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(kh, "_set_sync_status", _stamp)
        present: list[bool] = []
        pipeline = self._pipeline(present)
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await self._upload(client)
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["status"] == "processing"
            for _ in range(200):
                if pipeline.ingest_file.await_args is not None:
                    break
                await asyncio.sleep(0.01)

        assert present == [True], "the ingest never ran, or ran after the unlink"
        assert "error" not in stamped, "a failed UI hint was turned into a failed ingest"

    @pytest.mark.asyncio
    async def test_the_stamp_still_lands_before_the_ingest(self, store, monkeypatch):
        """Making the stamp non-fatal must not make it late: a stamp written
        after the ingest would leave the row un-marked for the whole run, which
        is the state ``sync_all`` re-sweeps."""
        monkeypatch.setattr(kh, "_sel_log", lambda tool, **kw: None)
        order: list[str] = []

        def _stamp(_store, source_id, status):
            order.append(f"stamp:{status}")

        monkeypatch.setattr(kh, "_set_sync_status", _stamp)

        async def _ingest(tmp_path, **kwargs):
            order.append("ingest")

        pipeline = MagicMock(
            ingest_file=AsyncMock(side_effect=_ingest),
            reserve_import_budget=AsyncMock(return_value=None),
            release_import_budget=MagicMock(),
        )
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            assert (await self._upload(client)).status == 200
            for _ in range(200):
                if pipeline.ingest_file.await_args is not None:
                    break
                await asyncio.sleep(0.01)

        assert order == ["stamp:syncing", "ingest"]


def _ingest_file_source() -> str:
    """The source text of ``ingest_file``, for the stamp-not-claim assertion."""
    return inspect.getsource(kh.ingest_file)


def _seed_job(store, job_id: str = "job-rebuild") -> str:
    store.db.execute(
        "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
        "VALUES (?, NULL, 'processing', '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
        (job_id,),
    )
    store.db.commit()
    return job_id


def _job_status(store, job_id: str) -> str:
    return store.db.execute("SELECT status FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()[
        0
    ]


class TestPropertiesBlobSurvives:
    """``properties`` is a whole-column rewrite, so read and write share a take."""

    @pytest.mark.asyncio
    async def test_each_edit_is_built_on_the_previous_row(self, store):
        """Sequential: pause must see adopt's key, because it re-reads the blob
        inside its own take instead of carrying a snapshot across an await."""
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.to_thread(kh._adopt_source, store, source_id, event="source.confirm")
        await asyncio.to_thread(kh._pause_source_row, store, source_id)
        props = await asyncio.to_thread(_props, store, source_id)
        assert props[kh.AUTO_REGISTRATION_RETIRED_PROP] is True
        assert props["scan_paused"] is True

    @pytest.mark.asyncio
    async def test_concurrent_edits_do_not_lose_one_another(self, store):
        """Both edits run at once. Whichever writes second must have read the
        first's row, so both keys are present whatever the order."""
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.gather(
            asyncio.to_thread(kh._adopt_source, store, source_id, event="source.confirm"),
            asyncio.to_thread(kh._pause_source_row, store, source_id),
        )
        props = await asyncio.to_thread(_props, store, source_id)
        assert (
            kh.AUTO_REGISTRATION_RETIRED_PROP in props
        ), "adopt's edit was lost -- pause wrote a blob it read too early"

    @pytest.mark.asyncio
    async def test_adopt_reads_and_writes_in_one_take(self, store, monkeypatch):
        """The read must not happen on the loop: under strict mode an on-loop
        read raises, so a green call proves both ends are in the worker."""
        source_id = await asyncio.to_thread(_seed_source, store)
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        outcome, row, props = await asyncio.to_thread(
            kh._adopt_source, store, source_id, event="source.confirm"
        )
        assert outcome == "ok"
        assert props[kh.AUTO_REGISTRATION_RETIRED_PROP] is True
        assert await asyncio.to_thread(_sync_status, store, source_id) == "active"

    @pytest.mark.asyncio
    async def test_adopt_reports_a_missing_row_without_writing(self, store):
        outcome, row, props = await asyncio.to_thread(
            kh._adopt_source, store, "no-such-source", event="source.confirm"
        )
        assert outcome == "missing"
        assert row is None

    @pytest.mark.asyncio
    async def test_adopt_refuses_a_restricted_path(self, store, monkeypatch):
        """The sensitive-path check runs inside the take, so a denied source is
        never activated -- and the check's own `resolve()` stays off the loop."""
        source_id = await asyncio.to_thread(_seed_source, store)
        monkeypatch.setattr(kh, "is_sensitive_path", lambda p: True)
        outcome, _row, _props = await asyncio.to_thread(
            kh._adopt_source, store, source_id, event="source.confirm"
        )
        assert outcome == "denied"
        assert await asyncio.to_thread(_sync_status, store, source_id) == "active"


class TestPropertiesMergeIsSerializedByTheDatabase:
    """The blob's read and write happen under ONE write lock, not two statements.

    A read and a write on an autocommit connection are two statements, so a
    writer that read the older blob can still land last and erase another's
    keys -- the pause a user just asked for, silently undone by a resume that
    started earlier. ``merge_source_properties`` takes the write lock BEFORE it
    reads, which is what makes the erase impossible rather than merely narrow.
    """

    @pytest.mark.asyncio
    async def test_the_lock_is_taken_before_the_read(self, store):
        """Revert the merge to two bare statements and this fails: without
        BEGIN IMMEDIATE the read runs under no lock, which is the whole defect."""
        source_id = await asyncio.to_thread(_seed_source, store)

        def _trace() -> list:
            conn = store.db
            stmts: list = []
            conn.set_trace_callback(stmts.append)
            try:
                store.merge_source_properties(source_id, set_keys={"k": 1})
            finally:
                conn.set_trace_callback(None)
            return stmts

        stmts = await asyncio.to_thread(_trace)
        assert stmts, "no SQL was traced"
        assert (
            stmts[0].strip().upper().startswith("BEGIN IMMEDIATE")
        ), f"the lock is not taken first; first statement was {stmts[0]!r}"
        selects = [s for s in stmts if s.strip().upper().startswith("SELECT")]
        updates = [s for s in stmts if s.strip().upper().startswith("UPDATE")]
        assert selects and updates, stmts
        assert stmts.index(selects[0]) > 0, "the read happened before the lock"
        # The trace callback yields parameter-expanded SQL, so the guard reads
        # `AND properties = '<the blob>'` rather than carrying a placeholder.
        assert (
            "AND properties = " in updates[0]
        ), f"the write is not guarded by the blob it read: {updates[0]!r}"

    @pytest.mark.asyncio
    async def test_keys_it_was_not_asked_to_change_survive(self, store):
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.to_thread(
            store.merge_source_properties,
            source_id,
            set_keys={kh.AUTO_REGISTRATION_RETIRED_PROP: True},
        )
        await asyncio.to_thread(
            store.merge_source_properties, source_id, set_keys={"scan_paused": True}
        )
        props = await asyncio.to_thread(_props, store, source_id)
        assert props[kh.AUTO_REGISTRATION_RETIRED_PROP] is True
        assert props["scan_paused"] is True

    @pytest.mark.asyncio
    async def test_removed_keys_go_and_the_rest_stays(self, store):
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.to_thread(
            store.merge_source_properties, source_id, set_keys={"scan_paused": True, "keep": "me"}
        )
        props = await asyncio.to_thread(
            store.merge_source_properties, source_id, remove_keys=("scan_paused",)
        )
        assert props is not None
        assert "scan_paused" not in props
        assert props["keep"] == "me"

    @pytest.mark.asyncio
    async def test_a_missing_row_is_reported_not_created(self, store):
        assert (
            await asyncio.to_thread(
                store.merge_source_properties, "no-such-source", set_keys={"scan_paused": True}
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_the_status_lands_in_the_column_not_the_blob(self, store):
        """``sources.sync_status`` is the single source of truth, so a status must
        not be persisted into the properties JSON as a second answer."""
        source_id = await asyncio.to_thread(_seed_source, store)
        props = await asyncio.to_thread(
            store.merge_source_properties,
            source_id,
            set_keys={"scan_paused": True},
            sync_status="paused",
        )
        assert props is not None
        assert await asyncio.to_thread(_sync_status, store, source_id) == "paused"
        assert "sync_status" not in await asyncio.to_thread(_props, store, source_id)

    @pytest.mark.asyncio
    async def test_the_handlers_reach_the_blob_only_through_the_merge(self, store):
        """Both handler helpers must be on the serialized path; a direct
        ``update_source(properties=...)`` here would reopen the window."""
        source_id = await asyncio.to_thread(_seed_source, store)
        seen: list[str] = []
        real = store.merge_source_properties

        def _spy(sid, **kw):
            seen.append(sid)
            return real(sid, **kw)

        store.merge_source_properties = _spy  # type: ignore[method-assign]
        try:
            await asyncio.to_thread(kh._adopt_source, store, source_id, event="source.confirm")
            await asyncio.to_thread(kh._pause_source_row, store, source_id)
        finally:
            del store.merge_source_properties
        assert seen == [
            source_id,
            source_id,
        ], "a handler wrote the blob without going through the merge"


class TestItemEditIsSerializedByTheDatabase:
    """A title PATCH reads the row it re-indexes from under the write lock.

    ``update_item`` rebuilds the FTS entry from the row it read: it deletes the
    OLD terms and inserts the new ones. Two concurrent PATCHes of one item that
    both read before either commits would have the loser delete terms the winner
    had already replaced, leaving the item searchable under a superseded title.
    Nothing repairs that -- ``ensure_fts_index_current`` re-indexes on a
    term-representation version bump, never on content staleness. Taking the
    lock before the read is what makes the interleave impossible; on the event
    loop it was impossible for free, because nothing ran between the two
    statements.
    """

    @pytest.mark.asyncio
    async def test_the_lock_is_taken_before_the_fts_read(self, store):
        """Move BEGIN IMMEDIATE back below the old-row read and this fails."""
        item_id = await asyncio.to_thread(store.add_item, "old title", "body", "note")

        def _trace() -> list:
            conn = store.db
            stmts: list = []
            conn.set_trace_callback(stmts.append)
            try:
                store.update_item(item_id, title="new title")
            finally:
                conn.set_trace_callback(None)
            return stmts

        stmts = await asyncio.to_thread(_trace)
        begins = [i for i, s in enumerate(stmts) if s.strip().upper().startswith("BEGIN IMMEDIATE")]
        reads = [
            i
            for i, s in enumerate(stmts)
            if s.strip().upper().startswith("SELECT") and "FROM ITEMS" in s.upper()
        ]
        assert begins, f"no BEGIN IMMEDIATE was issued: {stmts}"
        assert reads, f"the old-row read is missing: {stmts}"
        assert begins[0] < reads[0], (
            "the row the FTS delete is built from was read before the write lock "
            f"was held: {stmts}"
        )

    @pytest.mark.asyncio
    async def test_a_renamed_item_is_searchable_only_under_the_new_title(self, store):
        """The behaviour the lock protects, checked end to end."""
        item_id = await asyncio.to_thread(store.add_item, "kangaroo", "body", "note")
        await asyncio.to_thread(store.update_item, item_id, title="wombat")
        hits = await asyncio.to_thread(store.search_items_fts, "wombat")
        assert any(h["id"] == item_id for h in hits), "the new title is not searchable"
        stale = await asyncio.to_thread(store.search_items_fts, "kangaroo")
        assert not any(
            h["id"] == item_id for h in stale
        ), "the item is still searchable under its superseded title"


class TestJobFinalizeIsCompareAndSet:
    """A terminal job state is written once, by whoever gets there first."""

    @pytest.mark.asyncio
    async def test_a_late_cancel_cannot_overwrite_completed(self, store):
        """The Opus finding: a shutdown cancel arriving while the success
        finalize is still in its worker lands in `except BaseException`, and a
        blind second write would stamp 'cancelled' over a committed
        'completed'."""
        job_id = await asyncio.to_thread(_seed_job, store)
        await asyncio.to_thread(kh._finalize_job, store, job_id, "completed", processed=7)
        await asyncio.to_thread(
            kh._finalize_job, store, job_id, "cancelled", error="CancelledError()"
        )
        assert await asyncio.to_thread(_job_status, store, job_id) == "completed"

    @pytest.mark.asyncio
    async def test_first_finalize_still_lands(self, store):
        job_id = await asyncio.to_thread(_seed_job, store)
        await asyncio.to_thread(kh._finalize_job, store, job_id, "failed", error="boom")
        assert await asyncio.to_thread(_job_status, store, job_id) == "failed"

    @pytest.mark.asyncio
    async def test_finalize_reports_whether_it_moved_the_row(self, store):
        """The rowcount is what the audit gate reads, so it has to be truthful."""
        job_id = await asyncio.to_thread(_seed_job, store)
        first = await asyncio.to_thread(kh._finalize_job, store, job_id, "completed", processed=7)
        second = await asyncio.to_thread(
            kh._finalize_job, store, job_id, "cancelled", error="CancelledError()"
        )
        assert first is True, "the finalize that moved the row reported False"
        assert second is False, "a no-op compare-and-set reported that it moved a row"


class TestOnlyTheFinalizeThatLandedIsAudited:
    """An outcome line names the state the row reached, not the one attempted.

    The GPT finding: a shutdown cancel delivered after the success finalize has
    committed 'completed' runs the except arm, whose compare-and-set moves
    nothing -- but an unconditional audit still recorded ``outcome='cancelled'``
    for a job that completed and embedded every vector. The audit is append-only,
    so that line is not recoverable.
    """

    @pytest.mark.asyncio
    async def test_a_no_op_finalize_writes_no_audit_line(self, store, monkeypatch):
        seen: list[dict] = []
        monkeypatch.setattr(kh, "_sel_log", lambda tool, **kw: seen.append(kw))
        job_id = await asyncio.to_thread(_seed_job, store)
        await asyncio.to_thread(
            kh._finalize_job_audited,
            store,
            job_id,
            "completed",
            processed=7,
            fields={"count": 7, "rebuild": True, "force": False, "outcome": "completed"},
        )
        assert [f["outcome"] for f in seen] == ["completed"]

        moved = await asyncio.to_thread(
            kh._finalize_job_audited,
            store,
            job_id,
            "cancelled",
            error="CancelledError()",
            fields={"rebuild": True, "force": False, "outcome": "cancelled"},
        )

        assert moved is False
        assert await asyncio.to_thread(_job_status, store, job_id) == "completed"
        assert [f["outcome"] for f in seen] == [
            "completed"
        ], "a cancellation that changed no rows still recorded an audit outcome"

    @pytest.mark.asyncio
    async def test_the_finalize_that_lands_is_audited(self, store, monkeypatch):
        seen: list[dict] = []
        monkeypatch.setattr(kh, "_sel_log", lambda tool, **kw: seen.append(kw))
        job_id = await asyncio.to_thread(_seed_job, store)
        moved = await asyncio.to_thread(
            kh._finalize_job_audited,
            store,
            job_id,
            "cancelled",
            error="CancelledError()",
            fields={"rebuild": True, "force": False, "outcome": "cancelled"},
        )
        assert moved is True
        assert [f["outcome"] for f in seen] == ["cancelled"]

    @pytest.mark.asyncio
    async def test_audit_rides_in_the_worker_thread(self, store, monkeypatch):
        """Gating must not move the line back onto the loop."""
        seen: list[str] = []
        monkeypatch.setattr(
            kh, "_sel_log", lambda tool, **kw: seen.append(threading.current_thread().name)
        )
        job_id = await asyncio.to_thread(_seed_job, store)
        await asyncio.to_thread(
            kh._finalize_job_audited,
            store,
            job_id,
            "completed",
            processed=1,
            fields={"count": 1, "rebuild": True, "force": False, "outcome": "completed"},
        )
        assert seen and threading.current_thread().name not in seen


def _props(store, source_id: str) -> dict:
    row = store.db.execute("SELECT properties FROM sources WHERE id = ?", (source_id,)).fetchone()
    return json.loads(row["properties"]) if row["properties"] else {}


class TestCancellationFinalizesSyncStatus:
    """A cancel must never strand a source at 'syncing'.

    ``CancelledError`` is a ``BaseException`` (3.8+), so a finalizer guarded by
    ``except Exception`` lets a shutdown cancel skip the terminal
    ``sync_status`` write -- the row stays 'syncing' and every later sync
    answers 409 forever. Each path must land the row in 'error' (the existing
    terminal vocabulary; a cancelled sync presents like a failed one) and
    RE-RAISE, because aiohttp shutdown drains ``_bg_tasks`` by cancelling them
    and swallowing the cancel would break task semantics. The shape mirrors
    ``_rebuild_embeddings_job``, the file's own precedent.
    """

    @staticmethod
    async def _wait_until(predicate, timeout: float = 5.0) -> None:
        for _ in range(int(timeout / 0.01)):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition never became true")

    @pytest.mark.asyncio
    async def test_cancelled_local_file_ingest_lands_in_error(self, store, tmp_path):
        source_id = await asyncio.to_thread(_seed_source, store)
        doc = tmp_path / "doc.md"
        doc.write_text("# hi\n")
        started = asyncio.Event()

        async def _hang(*_a, **_k):
            started.set()
            await asyncio.Event().wait()

        pipeline = MagicMock(ingest_file=AsyncMock(side_effect=_hang))
        task = asyncio.ensure_future(
            kh._ingest_local_file_task(pipeline, store, str(doc), source_id)
        )
        await asyncio.wait_for(started.wait(), 5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(_sync_status, store, source_id) == "error"

    @pytest.mark.asyncio
    async def test_cancelled_agent_sync_lands_in_error(self, store, monkeypatch):
        source_id = await asyncio.to_thread(_seed_source, store)
        started = asyncio.Event()

        async def _hang_fetch(_url, _pool):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(kh, "fetch_url_content", _hang_fetch)
        task = asyncio.ensure_future(
            kh._background_agent_sync(
                source_id, "https://example.com/x", "x", store, MagicMock(), MagicMock()
            )
        )
        await asyncio.wait_for(started.wait(), 5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(_sync_status, store, source_id) == "error"

    @pytest.mark.asyncio
    async def test_cancelled_upload_ingest_lands_in_error(self, store, monkeypatch):
        monkeypatch.setattr(kh, "_sel_log", lambda tool, **kw: None)
        started = asyncio.Event()

        async def _hang_ingest(_tmp_path, **_kw):
            started.set()
            await asyncio.Event().wait()

        pipeline = MagicMock(
            ingest_file=AsyncMock(side_effect=_hang_ingest),
            reserve_import_budget=AsyncMock(return_value=None),
            release_import_budget=MagicMock(),
        )
        app = _make_app(store, pipeline=pipeline)
        async with TestClient(TestServer(app)) as client:
            form = aiohttp.FormData()
            form.add_field("file", b"# hello\n", filename="notes.md", content_type="text/markdown")
            resp = await client.post("/api/knowledge/ingest", data=form)
            assert resp.status == 200, await resp.text()
            source_id = (await resp.json())["source_id"]
            await asyncio.wait_for(started.wait(), 5.0)
            task = next(t for t in app["_bg_tasks"] if not t.done())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await asyncio.to_thread(_sync_status, store, source_id) == "error"

    @pytest.mark.asyncio
    async def test_cancelled_ingest_text_reraises_and_leaves_the_claim_alone(
        self, store, monkeypatch
    ):
        """The handler variant: the cancel propagates (aiohttp owns it) but the
        row is NOT finalized here -- a 'syncing' row belongs to the sync task
        that requested this text, and that task's own finalizer clears it."""
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.to_thread(_set_status, store, source_id, "syncing")
        pipeline = MagicMock(ingest_file=AsyncMock(side_effect=asyncio.CancelledError()))
        app = _make_app(store, pipeline=pipeline)

        async def _body(_request, max_bytes=None, **_kw):
            return {"text": "hello"}, None

        monkeypatch.setattr(kh, "read_bounded_json", _body)
        req = make_mocked_request(
            "POST",
            f"/api/knowledge/sources/{source_id}/ingest-text",
            match_info={"id": source_id},
            app=app,
        )
        with pytest.raises(asyncio.CancelledError):
            await kh.ingest_text(req)
        assert await asyncio.to_thread(_sync_status, store, source_id) == "syncing"

    @pytest.mark.asyncio
    async def test_plain_ingest_text_failure_500s_without_touching_the_claim(self, store):
        """Regression: widening to BaseException must not change the non-cancel
        contract -- a plain failure still answers the existing 500 body, and
        the sibling sync's 'syncing' claim is left for its owner to finalize."""
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.to_thread(_set_status, store, source_id, "syncing")
        pipeline = MagicMock(ingest_file=AsyncMock(side_effect=RuntimeError("boom")))
        app = _make_app(store, pipeline=pipeline)
        app.router.add_post("/api/knowledge/sources/{id}/ingest-text", kh.ingest_text)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{source_id}/ingest-text", json={"text": "x"}
            )
            assert resp.status == 500
            assert (await resp.json())["error"] == "internal server error"
        assert await asyncio.to_thread(_sync_status, store, source_id) == "syncing"

    @pytest.mark.asyncio
    async def test_plain_local_file_failure_still_lands_in_error(self, store, tmp_path):
        """Regression: the background task's plain-Exception arm is unchanged."""
        source_id = await asyncio.to_thread(_seed_source, store)
        doc = tmp_path / "doc.md"
        doc.write_text("# hi\n")
        pipeline = MagicMock(ingest_file=AsyncMock(side_effect=RuntimeError("boom")))
        await kh._ingest_local_file_task(pipeline, store, str(doc), source_id)
        assert await asyncio.to_thread(_sync_status, store, source_id) == "error"

    @pytest.mark.asyncio
    async def test_successful_ingest_text_still_writes_synced_and_audits(self, store, monkeypatch):
        """The success invariant: 'synced' and the source.ingest_text audit ride
        one worker take, so a normal run emits both."""
        seen: list[str] = []
        monkeypatch.setattr(kh, "_sel_log", lambda tool, **kw: seen.append(tool))
        source_id = await asyncio.to_thread(_seed_source, store)
        pipeline = MagicMock(ingest_file=AsyncMock(return_value="job-9"))
        app = _make_app(store, pipeline=pipeline)
        app.router.add_post("/api/knowledge/sources/{id}/ingest-text", kh.ingest_text)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{source_id}/ingest-text", json={"text": "hello"}
            )
            assert resp.status == 200
        assert await asyncio.to_thread(_sync_status, store, source_id) == "synced"
        assert "source.ingest_text" in seen

    @pytest.mark.asyncio
    async def test_a_failed_finalize_write_does_not_mask_the_cancel(self, store, monkeypatch):
        """A finalizer must never replace the original exception: a DB failure
        inside the terminal write is logged and dropped, and the cancel still
        propagates."""
        source_id = await asyncio.to_thread(_seed_source, store)
        started = asyncio.Event()

        async def _hang_fetch(_url, _pool):
            started.set()
            await asyncio.Event().wait()

        attempts: list[str] = []

        def _boom_write(_store, _sid, status, _from_statuses=("syncing",)):
            attempts.append(status)
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(kh, "fetch_url_content", _hang_fetch)
        task = asyncio.ensure_future(
            kh._background_agent_sync(
                source_id, "https://example.com/x", "x", store, MagicMock(), MagicMock()
            )
        )
        await asyncio.wait_for(started.wait(), 5.0)
        monkeypatch.setattr(kh, "_finalize_sync_status_write", _boom_write)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert attempts == ["error"], "the terminal write was never attempted"

    @pytest.mark.asyncio
    async def test_cancel_during_the_claim_take_still_finalizes(self, store, monkeypatch):
        """A cancel delivered while the claim's worker is in flight does not
        stop the thread: it commits 'syncing' after the task is gone. The
        claim await must therefore finalize a claim it WON on the way out."""
        source_id = await asyncio.to_thread(_seed_source, store)
        entered = threading.Event()
        release = threading.Event()
        real_claim = kh._claim_sync

        def _slow_claim(s, sid):
            entered.set()
            release.wait(5.0)
            return real_claim(s, sid)

        monkeypatch.setattr(kh, "_claim_sync", _slow_claim)
        pipeline = MagicMock(ingest_file=AsyncMock())
        task = asyncio.ensure_future(
            kh._ingest_local_file_task(pipeline, store, "/tmp/x.md", source_id)
        )
        await asyncio.to_thread(entered.wait, 5.0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        pipeline.ingest_file.assert_not_awaited()
        assert await asyncio.to_thread(_sync_status, store, source_id) == "error"

    @pytest.mark.asyncio
    async def test_cancel_during_a_lost_claim_leaves_the_siblings_row_alone(
        self, store, monkeypatch
    ):
        """A lost claim belongs to the sibling sync that holds it: the cancel
        path must not stamp 'error' over a row another task is working on."""
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.to_thread(_set_status, store, source_id, "syncing")
        entered = threading.Event()
        release = threading.Event()
        real_claim = kh._claim_sync

        def _slow_claim(s, sid):
            entered.set()
            release.wait(5.0)
            return real_claim(s, sid)  # loses: the row is already 'syncing'

        monkeypatch.setattr(kh, "_claim_sync", _slow_claim)
        pipeline = MagicMock(ingest_file=AsyncMock())
        task = asyncio.ensure_future(
            kh._ingest_local_file_task(pipeline, store, "/tmp/x.md", source_id)
        )
        await asyncio.to_thread(entered.wait, 5.0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(_sync_status, store, source_id) == "syncing"

    @pytest.mark.asyncio
    async def test_finalize_is_compare_and_set_over_syncing(self, store):
        """A late cancel finalize must not overwrite a committed 'synced': only
        a row still mid-sync is moved (same rule as ``_finalize_job``)."""
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.to_thread(_set_status, store, source_id, "synced")
        await kh._finalize_sync_status(store, source_id, "error")
        assert await asyncio.to_thread(_sync_status, store, source_id) == "synced"
        await asyncio.to_thread(_set_status, store, source_id, "syncing")
        await kh._finalize_sync_status(store, source_id, "error")
        assert await asyncio.to_thread(_sync_status, store, source_id) == "error"

    @pytest.mark.asyncio
    async def test_cancel_during_the_upload_stamp_still_lands_in_error(self, store, monkeypatch):
        """The stamp's worker outlives a cancel and can commit 'syncing' after
        the cancel arm's CAS ran. The stamp is shielded and drained so the
        finalize always sees the stamp's landed state."""
        monkeypatch.setattr(kh, "_sel_log", lambda tool, **kw: None)
        entered = threading.Event()
        release = threading.Event()
        real_stamp = kh._set_sync_status

        def _slow_stamp(s, sid, status):
            entered.set()
            release.wait(5.0)
            real_stamp(s, sid, status)

        monkeypatch.setattr(kh, "_set_sync_status", _slow_stamp)
        pipeline = MagicMock(
            ingest_file=AsyncMock(),
            reserve_import_budget=AsyncMock(return_value=None),
            release_import_budget=MagicMock(),
        )
        app = _make_app(store, pipeline=pipeline)
        async with TestClient(TestServer(app)) as client:
            form = aiohttp.FormData()
            form.add_field("file", b"# hello\n", filename="notes.md", content_type="text/markdown")
            resp = await client.post("/api/knowledge/ingest", data=form)
            assert resp.status == 200, await resp.text()
            source_id = (await resp.json())["source_id"]
            task = next(t for t in app["_bg_tasks"] if not t.done())
            await asyncio.to_thread(entered.wait, 5.0)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await asyncio.to_thread(_sync_status, store, source_id) == "error"
            pipeline.ingest_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_stamp_plus_failed_ingest_still_lands_in_error(self, store, monkeypatch):
        """The upload path's plain-failure write is unconditional: when the
        best-effort 'syncing' stamp failed AND the ingest then fails, the CAS
        would move nothing, so the arm must write 'error' outright."""
        monkeypatch.setattr(kh, "_sel_log", lambda tool, **kw: None)
        real_stamp = kh._set_sync_status

        def _stamp(s, sid, status):
            if status == "syncing":
                raise sqlite3.OperationalError("database is locked")
            real_stamp(s, sid, status)

        monkeypatch.setattr(kh, "_set_sync_status", _stamp)
        pipeline = MagicMock(
            ingest_file=AsyncMock(side_effect=RuntimeError("parser exploded")),
            reserve_import_budget=AsyncMock(return_value=None),
            release_import_budget=MagicMock(),
        )
        app = _make_app(store, pipeline=pipeline)
        async with TestClient(TestServer(app)) as client:
            form = aiohttp.FormData()
            form.add_field("file", b"# hello\n", filename="notes.md", content_type="text/markdown")
            resp = await client.post("/api/knowledge/ingest", data=form)
            assert resp.status == 200, await resp.text()
            source_id = (await resp.json())["source_id"]
            task = next(t for t in app["_bg_tasks"] if not t.done())
            await task
            assert await asyncio.to_thread(_sync_status, store, source_id) == "error"

    @pytest.mark.asyncio
    async def test_cancel_during_the_pending_write_does_not_strand_syncing(
        self, store, monkeypatch
    ):
        """A cancel inside the budget-deferral arm is not caught by the sibling
        ``except BaseException`` arm, so the 'pending' write itself must be
        cancel-safe: drained, finalized, re-raised -- never left 'syncing'."""
        from kiro_crew.knowledge.ingestion import ImportChunkBudgetError

        source_id = await asyncio.to_thread(_seed_source, store)
        entered = threading.Event()
        release = threading.Event()
        real_stamp = kh._set_sync_status

        def _slow_stamp(s, sid, status):
            if status == "pending":
                entered.set()
                release.wait(5.0)
            real_stamp(s, sid, status)

        monkeypatch.setattr(kh, "_set_sync_status", _slow_stamp)
        pipeline = MagicMock(
            ingest_file=AsyncMock(side_effect=ImportChunkBudgetError(100, 3600.0, 100))
        )
        task = asyncio.ensure_future(
            kh._ingest_local_file_task(pipeline, store, "/tmp/x.md", source_id)
        )
        await asyncio.to_thread(entered.wait, 5.0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        status = await asyncio.to_thread(_sync_status, store, source_id)
        assert status != "syncing", "the deferral arm re-stranded the row"
        assert status == "pending", "the drained deferral write must land"

    @pytest.mark.asyncio
    async def test_the_upload_finalize_also_moves_a_pending_row(self, store):
        """A cancel can land before the upload's 'syncing' stamp, while the
        fresh row still reads the INSERT default 'pending' -- the widened CAS
        moves it, and a committed 'synced' still survives."""
        source_id = await asyncio.to_thread(_seed_source, store)
        await asyncio.to_thread(_set_status, store, source_id, "pending")
        await kh._finalize_sync_status(
            store, source_id, "error", from_statuses=("syncing", "pending")
        )
        assert await asyncio.to_thread(_sync_status, store, source_id) == "error"
        await asyncio.to_thread(_set_status, store, source_id, "synced")
        await kh._finalize_sync_status(
            store, source_id, "error", from_statuses=("syncing", "pending")
        )
        assert await asyncio.to_thread(_sync_status, store, source_id) == "synced"

"""Coverage tests for the Knowledge Library handlers that write into or delete a source.

Covers the agent-document route, ingest-text (including the ingestion gate it holds
across the body read), source delete, the URL-fetch branch of source sync, and the
background agent sync task that branch starts. Shared harness pieces come from
``knowledge_handlers_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from knowledge_handlers_coverage_helpers import (  # noqa: F401 -- the fixture is requested by name
    MODULE,
    _cfg,
    _client,
    _make_app,
    store_fixture,
)

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.testing.wait import await_until

# ------------------------------------------------------------ agent document


class TestAddAgentDocumentRoute:
    @pytest.mark.asyncio
    async def test_disabled_toggle_is_403(self, store, monkeypatch):
        monkeypatch.setattr(
            f"{MODULE}.KiroCrewConfig.load", staticmethod(lambda: _cfg(auto_add=False))
        )
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post("/api/knowledge/agent-document", json={})
            assert resp.status == 403
            assert (await resp.json())["code"] == "auto_add_documents_disabled"

    @pytest.mark.asyncio
    async def test_missing_pipeline_is_503(self, store, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/agent-document", json={})
            assert resp.status == 503
            assert (await resp.json())["code"] == "pipeline_unavailable"

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, store, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(
                "/api/knowledge/agent-document",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_rejected_document_is_400(self, store, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        monkeypatch.setattr(
            f"{MODULE}.add_agent_document",
            AsyncMock(return_value={"status": "error", "error": "too short"}),
        )
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(
                "/api/knowledge/agent-document", json={"title": "t", "content": "c"}
            )
            assert resp.status == 400
            body = await resp.json()
        assert (body["error"], body["code"]) == ("too short", "document_rejected")

    @pytest.mark.asyncio
    async def test_accepted_document_returns_result_and_coerces_fields(self, store, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        fake_add = AsyncMock(return_value={"status": "added", "title": "T", "item_ids": ["i1"]})
        monkeypatch.setattr(f"{MODULE}.add_agent_document", fake_add)
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(
                "/api/knowledge/agent-document",
                json={"title": "T", "content": "body", "reason": None},
            )
            assert resp.status == 200
            assert (await resp.json())["status"] == "added"
        # None-valued optional fields arrive as empty strings, never as None.
        assert fake_add.await_args.kwargs["reason"] == ""
        assert fake_add.await_args.kwargs["source_uri"] == ""


# ----------------------------------------------------------------- ingest_text


class TestIngestText:
    @pytest.mark.asyncio
    async def test_unknown_source_is_404(self, store):
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post("/api/knowledge/sources/ghost/ingest-text", json={"text": "x"})
            assert resp.status == 404
            assert (await resp.json())["error"] == "source not found"

    @pytest.mark.asyncio
    async def test_missing_pipeline_is_503(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_make_app(store)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/ingest-text", json={"text": "x"}
            )
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/ingest-text",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_empty_text_is_400(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/ingest-text", json={"text": ""})
            assert resp.status == 400
            assert (await resp.json())["error"] == "no text provided"

    @pytest.mark.asyncio
    async def test_ingests_and_marks_source_synced(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        pipeline = MagicMock(ingest_file=AsyncMock(return_value="job-9"))
        async with _client(_make_app(store, pipeline=pipeline)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/ingest-text",
                json={"text": "hello", "name": "doc", "namespace": "work"},
            )
            assert resp.status == 200
            assert (await resp.json())["job_id"] == "job-9"
        row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "synced"
        kwargs = pipeline.ingest_file.await_args.kwargs
        assert kwargs["original_name"] == "doc"
        assert kwargs["namespace"] == "work"
        # The temp file the handler wrote is removed on the way out.
        assert not Path(pipeline.ingest_file.await_args.args[0]).exists()

    @pytest.mark.asyncio
    async def test_ingest_failure_is_500_and_cleans_up(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        pipeline = MagicMock(ingest_file=AsyncMock(side_effect=RuntimeError("boom")))
        async with _client(_make_app(store, pipeline=pipeline)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/ingest-text", json={"text": "hello"}
            )
            assert resp.status == 500
            assert (await resp.json())["error"] == "internal server error"
        assert not Path(pipeline.ingest_file.await_args.args[0]).exists()

    @pytest.mark.asyncio
    async def test_the_gate_is_held_from_the_lookup_through_the_ingest(self, store, monkeypatch):
        """The body read is an await between the source lookup and the ingest,
        and the handler holds the store's ingestion gate across it: a
        maintenance window cannot open while the body is in flight, and can
        once the request has answered."""
        sid = store.add_source("s", "web", "https://example.com")
        store.update_source(sid, sync_status="error")
        pipeline = IngestionPipeline.__new__(IngestionPipeline)
        pipeline.store = store
        pipeline.ingest_file = AsyncMock(return_value="job-9")
        seen: list[bool] = []

        async def _body_read(request, max_bytes=None):
            # flake-ok: 0.05 s bounds the probe; a held gate keeps the window shut at any speed
            with store.maintenance_window(timeout=0.05) as quiescent:
                seen.append(quiescent)
            return {"text": "hello"}, None

        monkeypatch.setattr(kh, "read_bounded_json", _body_read)
        async with _client(_make_app(store, pipeline=pipeline)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/ingest-text", json={"text": "hello"}
            )
            assert resp.status == 200
        assert seen == [False], "the maintenance window opened during the body read"
        # flake-ok: 0.05 s bounds the probe; the handler drops the gate before it answers
        with store.maintenance_window(timeout=0.05) as quiescent:
            assert quiescent is True, "the gate stayed held after the request answered"


# --------------------------------------------------- source delete / agent sync


class TestDeleteSourceBranches:
    @pytest.mark.asyncio
    async def test_unknown_source_is_404(self, store):
        async with _client(_make_app(store)) as client:
            assert (await client.delete("/api/knowledge/sources/ghost")).status == 404

    @pytest.mark.asyncio
    async def test_delete_never_tombstones_any_source(self, store, monkeypatch):
        """No source is recorded as dismissed, auto-added or not.

        The tombstone existed so a recurring discovery sweep could not re-create the
        auto source a user had just deleted. Both discovery loops are gone, so a
        deletion is final on its own and recording it would be a write nothing reads.
        """
        auto = store.add_source(
            "auto", "local_folder", "/tmp/auto", properties={"auto_added": True}
        )
        manual = store.add_source("manual", "local_folder", "/tmp/manual")
        seen: list[tuple] = []
        monkeypatch.setattr(
            store, "delete_source_cascade", lambda source_id: seen.append((source_id,))
        )
        async with _client(_make_app(store)) as client:
            for sid in (auto, manual):
                resp = await client.delete(f"/api/knowledge/sources/{sid}")
                assert resp.status == 200
                assert (await resp.json())["status"] == "deleted"
        # One positional argument each: the cascade has no dismissal parameter left
        # for a caller to pass, so neither row can be tombstoned by mistake.
        assert seen == [(auto,), (manual,)]
        assert not store.db.execute("SELECT 1 FROM dismissed_auto_sources").fetchall()

    @pytest.mark.asyncio
    async def test_unreadable_properties_do_not_block_the_delete(self, store, monkeypatch):
        sid = store.add_source("odd", "local_folder", "/tmp/odd")
        store.db.execute("UPDATE sources SET properties = ? WHERE id = ?", ("{not json", sid))
        store.db.commit()
        monkeypatch.setattr(store, "delete_source_cascade", lambda source_id: None)
        async with _client(_make_app(store)) as client:
            assert (await client.delete(f"/api/knowledge/sources/{sid}")).status == 200

    @pytest.mark.asyncio
    async def test_cascade_failure_is_500(self, store, monkeypatch):
        sid = store.add_source("s", "local_folder", "/tmp/s")

        def _boom(source_id):
            raise RuntimeError("locked")

        monkeypatch.setattr(store, "delete_source_cascade", _boom)
        async with _client(_make_app(store)) as client:
            resp = await client.delete(f"/api/knowledge/sources/{sid}")
            assert resp.status == 500
            assert (await resp.json())["error"] == "internal server error"


class TestSyncSourceAgentBranch:
    """The URL-fetch fallback taken when no connector handles the source type."""

    @pytest.mark.asyncio
    async def test_source_without_url_is_400(self, store):
        sid = store.add_source("s", "web", "")
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 400
            assert (await resp.json())["error"] == "no URL to fetch"

    @pytest.mark.asyncio
    async def test_url_from_properties_when_uri_is_blank(self, store, monkeypatch):
        sid = store.add_source("s", "web", "", properties={"url": "https://e.test/a"})
        seen = {}

        async def _fake_sync(source_id, url, name, st, pipeline, pool, *, claim_settled=None):
            # The handler holds the ingestion gate until the task reports its claim.
            if claim_settled is not None:
                claim_settled.set()
            seen["url"] = url

        monkeypatch.setattr(f"{MODULE}._background_agent_sync", _fake_sync)
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 200
            assert (await resp.json())["status"] == "syncing"
            # flake-ok: a loop-turn yield, not a wait: the handler answers after the fake has run
            await asyncio.sleep(0)
        assert seen["url"] == "https://e.test/a"

    @pytest.mark.asyncio
    async def test_sync_already_in_progress_is_409(self, store):
        sid = store.add_source("s", "web", "https://e.test/a")
        store.db.execute("UPDATE sources SET sync_status = 'syncing' WHERE id = ?", (sid,))
        store.db.commit()
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 409
            assert (await resp.json())["error"] == "sync already in progress"

    @pytest.mark.asyncio
    async def test_missing_pipeline_is_503(self, store):
        sid = store.add_source("s", "web", "https://e.test/a")
        async with _client(_make_app(store)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 503
            assert (await resp.json())["error"] == "pipeline not configured"

    @pytest.mark.asyncio
    async def test_spawns_background_sync_and_tracks_the_task(self, store, monkeypatch):
        sid = store.add_source("s", "web", "https://e.test/a")
        ran = asyncio.Event()

        async def _fake_sync(source_id, url, name, st, pipeline, pool, *, claim_settled=None):
            # The handler holds the ingestion gate until the task reports its claim.
            if claim_settled is not None:
                claim_settled.set()
            ran.set()

        monkeypatch.setattr(f"{MODULE}._background_agent_sync", _fake_sync)
        app = _make_app(store, pipeline=MagicMock())
        async with _client(app) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            data = await resp.json()
            await await_until(ran, what="the background sync running")
        assert data == {"synced": False, "status": "syncing", "source_id": sid}
        # The task is parked in _bg_tasks so it cannot be garbage-collected
        # mid-flight, and is discarded once it finishes.
        assert app["_bg_tasks"] == set()


class TestBackgroundAgentSync:
    @pytest.mark.asyncio
    async def test_success_marks_source_synced_and_removes_temp_file(self, store, monkeypatch):
        sid = store.add_source("s", "web", "https://example.com")
        monkeypatch.setattr(f"{MODULE}.fetch_url_content", AsyncMock(return_value="fetched body"))
        pipeline = MagicMock(ingest_file=AsyncMock(return_value="j1"))
        await kh._background_agent_sync(
            sid, "https://example.com", "S", store, pipeline, MagicMock()
        )
        row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "synced"
        assert not Path(pipeline.ingest_file.await_args.args[0]).exists()

    @pytest.mark.asyncio
    async def test_fetch_failure_marks_source_error(self, store, monkeypatch):
        sid = store.add_source("s", "web", "https://example.com")
        monkeypatch.setattr(
            f"{MODULE}.fetch_url_content", AsyncMock(side_effect=RuntimeError("offline"))
        )
        await kh._background_agent_sync(
            sid, "https://example.com", "S", store, MagicMock(), MagicMock()
        )
        row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "error"

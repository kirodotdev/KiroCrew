"""Tests for sync_source local_file handling.

Regression coverage for the bug where clicking Sync on a local_file source routed
the filesystem path through the agent URL-fetch path (ReadInternalWebsites), which
rejects non-https paths with "Invalid URL format" and left the source in 'error'.
local_file sync must instead re-ingest via the FileReader pipeline (ingest_file),
matching how add_source ingests the same file.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.dashboard.handlers.knowledge import sync_source
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def _make_app(store, pipeline=None):
    """Minimal app exposing the sync route with no registered connectors."""
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    if pipeline:
        app["knowledge_pipeline"] = pipeline
    # No connector for local_file (matches production registration)
    app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=None))
    app["knowledge_llm_pool"] = MagicMock()
    app.router.add_post("/api/knowledge/sources/{id}/sync", sync_source)
    return app


async def _await_sync_status(store, source_id: str, want: str) -> str:
    """Wait until the sync task has stamped *want* on the row; return what it last saw.

    The task claims 'syncing' off the loop and stamps its outcome off the loop, so
    reaching a terminal state costs two worker-thread hops. A fixed sleep races that
    on a loaded runner. Waiting for a NAMED status rather than "anything terminal"
    is deliberate: 'pending' is both the row's initial value AND the terminal
    outcome of a budget deferral, so a generic wait cannot tell "not started yet"
    from "deferred". Returning the last-seen status keeps the caller's assertion
    message truthful when the wait times out.
    """
    status = None
    for _ in range(200):
        row = store.db.execute(
            "SELECT sync_status FROM sources WHERE id = ?", (source_id,)).fetchone()
        status = row["sync_status"] if row else None
        if status == want:
            return status
        await asyncio.sleep(0.01)
    return status


class TestSyncLocalFile:
    @pytest.mark.asyncio
    async def test_sync_reingests_via_ingest_file_not_agent_fetch(self, store, tmp_path, monkeypatch):
        test_file = tmp_path / "doc.md"
        test_file.write_text("# Doc")
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        # Guard: the agent URL-fetch path must NOT be used for local_file
        fetch_spy = AsyncMock(side_effect=AssertionError("local_file must not use fetch_url_content"))
        monkeypatch.setattr(kh, "fetch_url_content", fetch_spy)

        sid = store.add_source(name="doc.md", source_type="local_file", uri=str(test_file))
        # Simulate the prior buggy state (source left in error after a failed agent sync)
        store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (sid,))
        store.db.commit()

        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "syncing"
            status = await _await_sync_status(store, sid, "synced")

        pipeline.ingest_file.assert_called_once()
        _, kwargs = pipeline.ingest_file.call_args
        assert kwargs.get("source_id") == sid
        fetch_spy.assert_not_called()
        assert status == "synced"

    @pytest.mark.asyncio
    async def test_sync_already_syncing_returns_409(self, store, tmp_path):
        test_file = tmp_path / "busy.md"
        test_file.write_text("busy")
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        sid = store.add_source(name="busy.md", source_type="local_file", uri=str(test_file))
        store.db.execute("UPDATE sources SET sync_status = 'syncing' WHERE id = ?", (sid,))
        store.db.commit()

        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 409
        pipeline.ingest_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_missing_pipeline_returns_503(self, store, tmp_path):
        test_file = tmp_path / "np.md"
        test_file.write_text("x")
        sid = store.add_source(name="np.md", source_type="local_file", uri=str(test_file))
        async with TestClient(TestServer(_make_app(store, pipeline=None))) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_sync_failed_ingest_marks_error(self, store, tmp_path):
        test_file = tmp_path / "boom.md"
        test_file.write_text("boom")
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock(side_effect=RuntimeError("read failed"))
        sid = store.add_source(name="boom.md", source_type="local_file", uri=str(test_file))
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 200
            status = await _await_sync_status(store, sid, "error")
        assert status == "error"

"""Tests for sync_source on the aggregate "Artifacts" Knowledge source.

The aggregate source (uri ``artifact://``) is maintained by
``ArtifactKnowledgeSync``, not by a connector or a fetchable URL. A manual Sync
on it must run the sync's reconcile pass under the sync's own lock and end in
'synced'; it must never hand ``artifact://`` to the agent URL-fetch path, whose
failure leaves the row in a sticky 'error'. With auto-ingest switched off there
is no sync object, so the request is refused and the row is left as it was.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.dashboard.handlers.knowledge import sync_source
from kiro_crew.knowledge import artifact_ingest
from kiro_crew.knowledge.artifact_ingest import ArtifactKnowledgeSync, ensure_artifact_source
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.testing.wait import await_until, until_parked


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture()
def fetch_spy(monkeypatch):
    """The agent URL-fetch path, rigged to fail the test if anything reaches it."""
    spy = AsyncMock(side_effect=AssertionError("artifact:// must not reach fetch_url_content"))
    monkeypatch.setattr(kh, "fetch_url_content", spy)
    return spy


def _make_app(store, *, artifact_sync=None):
    """Minimal app exposing the sync route, with no registered connectors."""
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    app["knowledge_pipeline"] = MagicMock()
    app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=None))
    app["knowledge_fetch_pool"] = MagicMock()
    app["_bg_tasks"] = set()
    if artifact_sync is not None:
        app["artifact_knowledge_sync"] = artifact_sync
    app.router.add_post("/api/knowledge/sources/{id}/sync", sync_source)
    return app


def _artifact_source(store, status: str) -> str:
    sid, _ = ensure_artifact_source(store)
    store.db.execute("UPDATE sources SET sync_status = ? WHERE id = ?", (status, sid))
    store.db.commit()
    return sid


def _status(store, sid: str) -> str:
    row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
    return row["sync_status"]


async def _drain(app) -> None:
    """Await every background task the handler started."""
    await await_until(asyncio.gather(*list(app["_bg_tasks"])), what="artifact sync task")


class TestSyncArtifactSource:
    @pytest.mark.asyncio
    async def test_sync_runs_the_reconcile_pass_and_ends_synced(self, store, fetch_spy):
        sid = _artifact_source(store, "error")
        artifact_sync = MagicMock()
        artifact_sync.reconcile_now = AsyncMock(return_value=None)
        app = _make_app(store, artifact_sync=artifact_sync)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "syncing"
            await _drain(app)

        artifact_sync.reconcile_now.assert_awaited_once_with(sid)
        fetch_spy.assert_not_called()
        assert await asyncio.to_thread(_status, store, sid) == "synced"

    @pytest.mark.asyncio
    async def test_sync_with_auto_ingest_off_is_refused_and_leaves_status(self, store, fetch_spy):
        sid = _artifact_source(store, "error")
        app = _make_app(store, artifact_sync=None)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 409
            data = await resp.json()
            assert data["code"] == "artifact_auto_ingest_disabled"

        fetch_spy.assert_not_called()
        assert await asyncio.to_thread(_status, store, sid) == "error"

    @pytest.mark.asyncio
    async def test_sync_already_syncing_returns_409(self, store, fetch_spy):
        sid = _artifact_source(store, "syncing")
        artifact_sync = MagicMock()
        artifact_sync.reconcile_now = AsyncMock(return_value=None)
        app = _make_app(store, artifact_sync=artifact_sync)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 409
            data = await resp.json()
            assert data["code"] == "sync_in_progress"

        artifact_sync.reconcile_now.assert_not_called()
        fetch_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_reconcile_pass_marks_error(self, store, fetch_spy):
        sid = _artifact_source(store, "synced")
        artifact_sync = MagicMock()
        artifact_sync.reconcile_now = AsyncMock(side_effect=RuntimeError("store unreadable"))
        app = _make_app(store, artifact_sync=artifact_sync)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 200
            await _drain(app)

        fetch_spy.assert_not_called()
        assert await asyncio.to_thread(_status, store, sid) == "error"

    @pytest.mark.asyncio
    async def test_ingest_error_stamped_during_the_pass_is_kept(self, store, fetch_spy):
        """An artifact ingest that fails inside the pass stamps the row 'error'
        itself; the closing 'synced' only moves a row still 'syncing', so that
        failure stays visible."""
        sid = _artifact_source(store, "synced")

        def _ingest_failed() -> None:
            store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (sid,))
            store.db.commit()

        async def _pass_with_failed_ingest(source_id: str) -> None:
            await asyncio.to_thread(_ingest_failed)

        artifact_sync = MagicMock()
        artifact_sync.reconcile_now = AsyncMock(side_effect=_pass_with_failed_ingest)
        app = _make_app(store, artifact_sync=artifact_sync)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 200
            await _drain(app)

        artifact_sync.reconcile_now.assert_awaited_once_with(sid)
        fetch_spy.assert_not_called()
        assert await asyncio.to_thread(_status, store, sid) == "error"

    @pytest.mark.asyncio
    async def test_reconcile_waits_for_the_sync_lock(self, store, fetch_spy, monkeypatch):
        """The pass shares the live listener's lock, so it never interleaves with
        an artifact ingest or remove already in flight."""
        sid = _artifact_source(store, "error")
        reconcile = AsyncMock(return_value=(0, 0, 0))
        monkeypatch.setattr(artifact_ingest, "reconcile_artifacts", reconcile)
        artifact_sync = ArtifactKnowledgeSync(
            art_store=MagicMock(),
            pipeline=MagicMock(),
            kinds=set(),
            loop=asyncio.get_running_loop(),
        )
        app = _make_app(store, artifact_sync=artifact_sync)

        async with TestClient(TestServer(app)) as client:
            async with artifact_sync._lock:
                resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
                assert resp.status == 200
                await until_parked(artifact_sync._lock)
                reconcile.assert_not_called()
                assert await asyncio.to_thread(_status, store, sid) == "syncing"
            await _drain(app)

        reconcile.assert_awaited_once()
        assert reconcile.await_args.args[2] == sid
        fetch_spy.assert_not_called()
        assert await asyncio.to_thread(_status, store, sid) == "synced"

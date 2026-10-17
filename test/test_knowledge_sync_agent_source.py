"""Tests for sync_source on the aggregate "Auto-added" Knowledge source.

The aggregate source (uri ``agent://``) collects documents the agent hands over
through ``knowledge_add_document``; Kiro Crew fetches nothing for it. A manual
Sync must never hand ``agent://`` to the agent URL-fetch path, whose failure
leaves the row in a sticky 'error'. It is refused with 409
``source_not_syncable`` and the row's status is left as it was.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.dashboard.handlers.knowledge import sync_source
from kiro_crew.knowledge.agent_source import ensure_agent_source
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture()
def fetch_spy(monkeypatch):
    """The agent URL-fetch path, rigged to fail the test if anything reaches it."""
    spy = AsyncMock(side_effect=AssertionError("agent:// must not reach fetch_url_content"))
    monkeypatch.setattr(kh, "fetch_url_content", spy)
    return spy


def _make_app(store):
    """Minimal app exposing the sync route, with no registered connectors."""
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    app["knowledge_pipeline"] = MagicMock()
    app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=None))
    app["knowledge_fetch_pool"] = MagicMock()
    app["_bg_tasks"] = set()
    app.router.add_post("/api/knowledge/sources/{id}/sync", sync_source)
    return app


def _agent_source(store, status: str) -> str:
    sid, _ = ensure_agent_source(store)
    store.db.execute("UPDATE sources SET sync_status = ? WHERE id = ?", (status, sid))
    store.db.commit()
    return sid


def _status(store, sid: str) -> str:
    row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
    return row["sync_status"]


class TestSyncAgentSource:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["active", "error"])
    async def test_sync_is_refused_and_status_is_left_as_is(self, store, fetch_spy, status):
        sid = _agent_source(store, status)
        app = _make_app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "source_not_syncable"
        assert body["source_id"] == sid
        assert _status(store, sid) == status
        assert not app["_bg_tasks"]
        fetch_spy.assert_not_called()


class TestListSourcesSyncable:
    @pytest.mark.asyncio
    async def test_only_the_agent_source_is_marked_not_syncable(self, store):
        agent_sid = _agent_source(store, "active")
        file_sid = store.add_source(name="doc.md", source_type="local_file", uri="/tmp/doc.md")
        app = web.Application()
        state = MagicMock()
        state.knowledge_store = store
        app["state"] = state
        app.router.add_get("/api/knowledge/sources", kh.list_sources)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/knowledge/sources")
            rows = {r["id"]: r for r in await resp.json()}
        assert resp.status == 200
        assert rows[agent_sid]["syncable"] is False
        assert rows[file_sid]["syncable"] is True

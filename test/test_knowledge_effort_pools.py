from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.knowledge.store import KnowledgeStore


class _FakePool:
    def __init__(
        self,
        *,
        pool_size: int,
        effort: str | None = None,
        use_config_pool_size: bool = True,
    ) -> None:
        self.pool_size = pool_size
        self.effort = effort
        self.use_config_pool_size = use_config_pool_size
        self.shutdown = AsyncMock()


@pytest.fixture()
def store(tmp_path):
    value = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield value
    value.close()


def _config(background_effort: str | None = None):
    cfg = KiroCrewConfig()
    if background_effort is not None:
        cfg.agent.role_efforts = {"background": background_effort}
    return cfg


class TestKnowledgePoolSetup:
    @pytest.mark.parametrize("background_effort", [None, "low"])
    def test_setup_creates_isolated_extraction_and_fetch_pools(
        self, monkeypatch, background_effort
    ):
        app = web.Application()
        app["state"] = SimpleNamespace(knowledge_store=object())
        pools: list[_FakePool] = []
        extractor_calls: list[dict[str, object]] = []
        extractor = object()
        pipeline = object()

        def _pool_factory(**kwargs):
            pool = _FakePool(**kwargs)
            pools.append(pool)
            return pool

        def _extractor_factory(**kwargs):
            extractor_calls.append(kwargs)
            return extractor

        monkeypatch.setattr(
            kh.KiroCrewConfig, "load", lambda: _config(background_effort)
        )
        monkeypatch.setattr(kh, "LLMPool", _pool_factory)
        monkeypatch.setattr(kh, "EntityExtractor", _extractor_factory)
        monkeypatch.setattr(kh, "IngestionPipeline", lambda **kwargs: pipeline)
        monkeypatch.setattr(kh, "HeadingAwareChunker", lambda: object())
        monkeypatch.setattr(kh, "FileReader", lambda: object())
        monkeypatch.setattr(kh, "SyncScheduler", lambda **kwargs: object())
        monkeypatch.setattr(kh, "_create_embedder", lambda _app: None)

        kh.setup_knowledge_routes(app)

        assert len(pools) == 2
        extraction, fetch = pools
        assert extraction.pool_size == 3
        assert extraction.effort == "high"
        assert extraction.use_config_pool_size is False
        assert fetch.pool_size == 1
        assert fetch.effort is None
        assert fetch.use_config_pool_size is False
        assert extractor_calls[0]["pool"] is extraction
        assert app["knowledge_extraction_pool"] is extraction
        assert app["knowledge_fetch_pool"] is fetch
        assert app["knowledge_llm_pool"] is extraction


class TestKnowledgeFetchPoolWiring:
    @pytest.mark.asyncio
    async def test_agent_sync_prefers_fetch_pool(self, store, monkeypatch):
        source_id = store.add_source(
            name="source",
            source_type="web",
            uri="https://example.com/source",
        )
        extraction_pool = object()
        fetch_pool = object()
        app = web.Application()
        app["state"] = SimpleNamespace(knowledge_store=store)
        app["knowledge_pipeline"] = object()
        app["knowledge_sync"] = SimpleNamespace(get_connector=lambda _type: None)
        app["knowledge_extraction_pool"] = extraction_pool
        app["knowledge_fetch_pool"] = fetch_pool
        app["knowledge_llm_pool"] = extraction_pool
        app.router.add_post("/api/knowledge/sources/{id}/sync", kh.sync_source)
        observed: dict[str, object] = {}
        done = asyncio.Event()

        async def _fake_sync(source_id, url, name, store, pipeline, pool):
            observed["pool"] = pool
            done.set()

        monkeypatch.setattr(kh, "_background_agent_sync", _fake_sync)
        monkeypatch.setattr(kh, "_sel_log", lambda *args, **kwargs: None)

        async with TestClient(TestServer(app)) as client:
            response = await client.post(f"/api/knowledge/sources/{source_id}/sync")
            assert response.status == 200
            await asyncio.wait_for(done.wait(), timeout=5)

        assert observed["pool"] is fetch_pool


class TestKnowledgePoolCleanup:
    @pytest.mark.asyncio
    async def test_shutdowns_each_pool_once(self):
        extraction = _FakePool(pool_size=3, effort="high")
        fetch = _FakePool(pool_size=1)
        app = web.Application()
        app["knowledge_extraction_pool"] = extraction
        app["knowledge_fetch_pool"] = fetch
        app["knowledge_llm_pool"] = extraction

        await kh._shutdown_knowledge_pools(app)

        extraction.shutdown.assert_awaited_once()
        fetch.shutdown.assert_awaited_once()

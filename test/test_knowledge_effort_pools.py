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
        effort_key: str | None = None,
        fallback_effort: str = "",
        config_pool_size_key: str | None = None,
    ) -> None:
        self.pool_size = pool_size
        self.effort = effort
        self.effort_key = effort_key
        self.fallback_effort = fallback_effort
        self.config_pool_size_key = config_pool_size_key
        self.shutdown = AsyncMock()


@pytest.fixture()
def store(tmp_path):
    value = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield value
    value.close()


def _config(
    extraction_effort: str = "",
    fetch_effort: str = "",
    background_effort: str | None = None,
):
    cfg = KiroCrewConfig()
    cfg.knowledge.extraction_effort = extraction_effort
    cfg.knowledge.fetch_effort = fetch_effort
    if background_effort is not None:
        cfg.agent.role_efforts = {"background": background_effort}
    return cfg


class TestKnowledgePoolSetup:
    """The resolution chain as wired in ``setup_knowledge_routes``.

    Parametrized over (extraction_effort, fetch_effort, role background):
    the pool's ``effort`` attr carries the RESOLVED level, not the raw key.
    """

    @pytest.mark.parametrize(
        "extraction_effort,fetch_effort,background_effort,expected_extraction,expected_fetch",
        [
            # Default ("" everywhere, no role pin): extraction falls to the
            # high last resort; fetch stays on the provider default.
            ("", "", None, "high", None),
            # An explicit extraction pin beats both the role policy and the
            # fallback; fetch has no pin and inherits the same role effort.
            ("low", "", "high", "low", "high"),
            # No explicit pin: the background-role effort applies.
            ("", "", "medium", "medium", "medium"),
            # Explicit fetch pin on an otherwise default install.
            ("", "low", None, "high", "low"),
        ],
    )
    def test_setup_resolves_pool_efforts(
        self,
        monkeypatch,
        extraction_effort,
        fetch_effort,
        background_effort,
        expected_extraction,
        expected_fetch,
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
            kh.KiroCrewConfig,
            "load",
            lambda: _config(extraction_effort, fetch_effort, background_effort),
        )
        # The real LLMPool resolves the effort in start(); the handler passes
        # the workload binding, so resolution is asserted against the binding
        # plus (for the resolved level) through _get_workload_effort below.
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
        assert extraction.effort_key == "extraction_effort"
        assert extraction.fallback_effort == "high"
        assert extraction.config_pool_size_key == "extraction_pool_size"
        assert fetch.pool_size == 1
        assert fetch.effort_key == "fetch_effort"
        assert fetch.fallback_effort == ""
        assert fetch.config_pool_size_key is None
        assert extractor_calls[0]["pool"] is extraction
        assert app["knowledge_extraction_pool"] is extraction
        assert app["knowledge_fetch_pool"] is fetch
        assert "knowledge_llm_pool" not in app

        # Resolve the chain exactly as LLMPool.start() would, and prove the
        # operator pin wins over the old hard default. The role lookup reads
        # config.json's raw JSON (a dict), so a None role pin means "absent".
        from kiro_crew.knowledge.llm_pool import (
            DEFAULT_EXTRACTION_EFFORT,
            _get_workload_effort,
        )

        agent_section: dict = {}
        if background_effort is not None:
            agent_section["role_efforts"] = {"background": background_effort}
        raw = {
            "knowledge": {
                "extraction_effort": extraction_effort,
                "fetch_effort": fetch_effort,
            },
            "agent": agent_section,
        }
        assert (
            _get_workload_effort(raw, "extraction_effort", DEFAULT_EXTRACTION_EFFORT)
            == expected_extraction
        )
        assert _get_workload_effort(raw, "fetch_effort", "") == expected_fetch


class TestKnowledgeFetchPoolWiring:
    @pytest.mark.asyncio
    async def test_agent_sync_uses_fetch_pool(self, store, monkeypatch):
        source_id = store.add_source(
            name="source",
            source_type="web",
            uri="https://example.com/source",
        )
        fetch_pool = object()
        app = web.Application()
        app["state"] = SimpleNamespace(knowledge_store=store)
        app["knowledge_pipeline"] = object()
        app["knowledge_sync"] = SimpleNamespace(get_connector=lambda _type: None)
        app["knowledge_fetch_pool"] = fetch_pool
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

    @pytest.mark.asyncio
    async def test_agent_sync_fails_loudly_without_fetch_pool(self, store, monkeypatch, caplog):
        # The legacy knowledge_llm_pool fallback is gone: an app that never
        # registered the fetch pool fails loudly (the handler raises, aiohttp
        # answers 500) instead of silently running URL sync through the
        # extraction pool.
        source_id = store.add_source(
            name="source",
            source_type="web",
            uri="https://example.com/source",
        )
        app = web.Application()
        app["state"] = SimpleNamespace(knowledge_store=store)
        app["knowledge_pipeline"] = object()
        app["knowledge_sync"] = SimpleNamespace(get_connector=lambda _type: None)
        app.router.add_post("/api/knowledge/sources/{id}/sync", kh.sync_source)
        monkeypatch.setattr(kh, "_sel_log", lambda *args, **kwargs: None)
        dispatched: list[object] = []

        async def _fake_sync(source_id, url, name, store, pipeline, pool):
            dispatched.append(pool)

        monkeypatch.setattr(kh, "_background_agent_sync", _fake_sync)

        async with TestClient(TestServer(app)) as client:
            response = await client.post(f"/api/knowledge/sources/{source_id}/sync")

        assert response.status >= 500
        assert dispatched == []


class TestKnowledgePoolCleanup:
    @pytest.mark.asyncio
    async def test_shutdowns_each_pool_once(self):
        extraction = _FakePool(pool_size=3)
        fetch = _FakePool(pool_size=1)
        app = web.Application()
        app["knowledge_extraction_pool"] = extraction
        app["knowledge_fetch_pool"] = fetch

        await kh._shutdown_knowledge_pools(app)

        extraction.shutdown.assert_awaited_once()
        fetch.shutdown.assert_awaited_once()

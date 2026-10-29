from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.testing.wait import await_until


@pytest.fixture(autouse=True)
def _listed_test_hosts(_floor_monkeypatch):
    """List the hosts these sources name in ``knowledge.doc_ingest_hosts``.

    A source with no connector is fetched by Kiro Crew only from a listed host,
    and the list is empty by default; these tests exercise what happens after
    that gate lets the URL through.
    """
    _floor_monkeypatch.setattr(kh, "_doc_ingest_hosts", lambda: ["example.com"])


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
    background_effort: str | None = None,
):
    cfg = KiroCrewConfig()
    cfg.knowledge.extraction_effort = extraction_effort
    if background_effort is not None:
        cfg.agent.role_efforts = {"background": background_effort}
    return cfg


class TestKnowledgePoolSetup:
    """The resolution chain as wired in ``setup_knowledge_routes``.

    Parametrized over (extraction_effort, role background): the pool's
    ``effort`` attr carries the workload BINDING, and the resolved level comes
    out of ``_get_workload_effort`` exactly as ``LLMPool.start()`` computes it.
    Extraction effort is independent of the background role: a role pin never
    moves it.
    """

    @pytest.mark.parametrize(
        "extraction_effort,background_effort,expected_extraction",
        [
            # Default ("" everywhere, no role pin): extraction falls to the
            # high last resort.
            ("", None, "high"),
            # An explicit pin beats the fallback.
            ("low", "high", "low"),
            # No explicit pin: the background-role effort does NOT apply —
            # extraction keeps the historical high regardless of the role.
            ("", "medium", "high"),
        ],
    )
    def test_setup_resolves_pool_efforts(
        self,
        monkeypatch,
        extraction_effort,
        background_effort,
        expected_extraction,
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
            lambda: _config(extraction_effort, background_effort),
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

        # Extraction is the only LLM pool: a web-page source is fetched by the
        # gateway (url_fetch), not by a worker.
        assert len(pools) == 1
        (extraction,) = pools
        assert extraction.pool_size == 3
        assert extraction.effort_key == "extraction_effort"
        assert extraction.fallback_effort == "high"
        assert extraction.config_pool_size_key == "extraction_pool_size"
        assert extractor_calls[0]["pool"] is extraction
        assert app["knowledge_extraction_pool"] is extraction
        assert "knowledge_fetch_pool" not in app
        assert "knowledge_llm_pool" not in app

        # Resolve the chain exactly as LLMPool.start() would, and prove the
        # operator pin wins over the hard default while a background-role pin
        # never moves extraction. The role lookup reads config.json's raw JSON
        # (a dict), so a None role pin means "absent".
        from kiro_crew.knowledge.llm_pool import (
            DEFAULT_EXTRACTION_EFFORT,
            _get_workload_effort,
        )

        agent_section: dict = {}
        if background_effort is not None:
            agent_section["role_efforts"] = {"background": background_effort}
        raw = {
            "knowledge": {"extraction_effort": extraction_effort},
            "agent": agent_section,
        }
        assert (
            _get_workload_effort(raw, "extraction_effort", DEFAULT_EXTRACTION_EFFORT)
            == expected_extraction
        )


class TestUrlSyncNeedsNoPool:
    @pytest.mark.asyncio
    async def test_url_sync_is_dispatched_without_an_llm_pool(self, store, monkeypatch):
        source_id = store.add_source(
            name="source",
            source_type="web",
            uri="https://example.com/source",
        )
        pipeline = SimpleNamespace(ingestion_in_flight=contextlib.nullcontext)
        app = web.Application()
        app["state"] = SimpleNamespace(knowledge_store=store)
        app["knowledge_pipeline"] = pipeline
        app["knowledge_sync"] = SimpleNamespace(get_connector=lambda _type: None)
        app.router.add_post("/api/knowledge/sources/{id}/sync", kh.sync_source)
        observed: dict[str, object] = {}
        done = asyncio.Event()

        async def _fake_sync(source_id, url, name, store, pipeline, *, claim_settled=None):
            observed["url"] = url
            if claim_settled is not None:
                claim_settled.set()
            done.set()

        monkeypatch.setattr(kh, "_background_url_sync", _fake_sync)
        monkeypatch.setattr(kh, "_sel_log", lambda *args, **kwargs: None)

        async with TestClient(TestServer(app)) as client:
            response = await client.post(f"/api/knowledge/sources/{source_id}/sync")
            assert response.status == 200
            await await_until(done, what="sync completes")

        assert observed["url"] == "https://example.com/source"


class TestKnowledgePoolCleanup:
    @pytest.mark.asyncio
    async def test_shuts_down_the_extraction_pool(self):
        extraction = _FakePool(pool_size=3)
        app = web.Application()
        app["knowledge_extraction_pool"] = extraction

        await kh._shutdown_knowledge_pools(app)

        extraction.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failing_shutdown_is_logged_not_raised(self):
        extraction = _FakePool(pool_size=3)
        extraction.shutdown.side_effect = RuntimeError("worker hung")
        app = web.Application()
        app["knowledge_extraction_pool"] = extraction

        await kh._shutdown_knowledge_pools(app)

        assert extraction.shutdown.await_count == 1

    @pytest.mark.asyncio
    async def test_no_pool_is_a_no_op(self):
        await kh._shutdown_knowledge_pools(web.Application())

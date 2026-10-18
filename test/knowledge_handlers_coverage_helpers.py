"""Shared harness for the ``test_knowledge_handlers_coverage*.py`` modules.

The ``store`` fixture (a real :class:`KnowledgeStore` on a ``tmp_path`` sqlite file),
``_make_app`` (a minimal ``web.Application`` with the routes and app keys the handlers
read) and ``_client`` for it, the ``_FakeEmbedder`` stand-in, the ``_cfg`` config stub
with the knowledge toggles, and ``MODULE``, the handler module's dotted path for
``monkeypatch.setattr`` targets.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.embeddings import PRIORITY_NORMAL
from kiro_crew.knowledge.store import KnowledgeStore

MODULE = "kiro_crew.dashboard.handlers.knowledge"


@pytest.fixture(name="store")
def store_fixture(tmp_path):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s._close_all_for_tests()


class _FakeEmbedder:
    """Minimal stand-in for InProcessEmbedder (real model never loaded)."""

    def __init__(self, *, available=True, vec=(0.1, 0.2, 0.3, 0.4), model="fake-embed:1", dim=4):
        self.model = model
        # Width is part of the vector-space identity embed_signature hashes, so a
        # stand-in has to declare one just as InProcessEmbedder does.
        self.dim = dim
        self.content_budget = 2000
        self._available = available
        self._vec = list(vec)
        self.embed_calls: list[str] = []
        self.priorities: list[int] = []

    async def is_available_async(self) -> bool:
        return self._available

    def embed_for_item(self, title, summary, content, *, priority=PRIORITY_NORMAL):
        self.embed_calls.append(title or "")
        self.priorities.append(priority)
        return list(self._vec) if self._vec else None

    def embed(self, text, *, priority=PRIORITY_NORMAL):
        return list(self._vec) if self._vec else None


def _make_app(store, *, pipeline=None, embedder=None, watcher=None, pool=None):
    """Minimal app + every route these tests exercise."""
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    if pipeline is not None:
        app["knowledge_pipeline"] = pipeline
    if embedder is not None:
        app["knowledge_embedder"] = embedder
    if watcher is not None:
        app["knowledge_watcher"] = watcher
    # Our PR drops the legacy llm_pool fallback for URL sync: the fetch pool
    # is required explicitly, so the shared _make_app must register it.
    app["knowledge_fetch_pool"] = pool if pool is not None else MagicMock(shutdown=AsyncMock())
    app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=None))

    r = app.router
    r.add_get("/api/knowledge/namespaces", kh.list_namespaces)
    r.add_get("/api/knowledge/stats", kh.get_stats)
    r.add_get("/api/knowledge/entities", kh.list_entities)
    r.add_get("/api/knowledge/graph", kh.get_full_graph)
    r.add_get("/api/knowledge/export", kh.export_all)
    r.add_post("/api/knowledge/import", kh.import_bundle)
    r.add_post("/api/knowledge/agent-document", kh.add_agent_document_route)
    r.add_get("/api/knowledge/embedding/status", kh.get_embedding_status)
    r.add_post("/api/knowledge/embedding/generate", kh.batch_embed_items)
    r.add_get("/api/knowledge/search-for-context", kh.search_for_context)
    r.add_get("/api/knowledge/items/{id}", kh.get_item)
    r.add_patch("/api/knowledge/items/{id}", kh.update_item)
    r.add_delete("/api/knowledge/items/{id}", kh.delete_item)
    r.add_get("/api/knowledge/items/{id}/content", kh.get_item_content)
    r.add_get("/api/knowledge/items/{id}/related", kh.get_related_items)
    r.add_get("/api/knowledge/items/{id}/export", kh.export_item)
    r.add_get("/api/knowledge/entities/by-name/{name}/items", kh.get_entity_items)
    r.add_get("/api/knowledge/entities/{id}/graph", kh.get_entity_graph)
    r.add_get("/api/knowledge/jobs/{id}", kh.get_job)
    r.add_post("/api/knowledge/sources/{id}/ingest-text", kh.ingest_text)
    r.add_post("/api/knowledge/sources/{id}/sync", kh.sync_source)
    r.add_delete("/api/knowledge/sources/{id}", kh.delete_source)
    return app


def _client(app):
    return TestClient(TestServer(app))


def _cfg(auto_add=True, auto_ingest=False, kinds=()):
    cfg = MagicMock()
    cfg.knowledge.auto_add_documents = auto_add
    cfg.knowledge.auto_ingest_artifacts = auto_ingest
    cfg.knowledge.auto_ingest_artifact_kinds = list(kinds)
    return cfg

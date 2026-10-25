"""Coverage tests for the Knowledge Library chat-context search endpoint.

Covers ``search_for_context``: the 400 on a blank query, the citation cards it builds
and their fallbacks, the token budget read from ``config.json`` and the defaults used
for an unreadable or wrong-typed config or a non-numeric limit, and whether the
embedder reaches the hybrid retriever when it is available and not when it is not.
Shared harness pieces come from ``knowledge_handlers_coverage_helpers.py``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from knowledge_handlers_coverage_helpers import (  # noqa: F401 -- the fixture is requested by name
    MODULE,
    _client,
    _FakeEmbedder,
    _make_app,
    store_fixture,
)

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.knowledge.embedder import embedder_signature

# ------------------------------------------------------- chat-context search


def _patch_retriever(monkeypatch, results):
    """Route the handler's hybrid search at a fixed result list, off-pool."""

    async def _direct(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(f"{MODULE}.run_in_embed_pool", _direct)
    monkeypatch.setattr(
        f"{MODULE}.HybridRetriever",
        lambda *a, **kw: MagicMock(search=MagicMock(return_value=results)),
    )


class TestSearchForContext:
    @pytest.mark.asyncio
    async def test_missing_query_is_400(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/search-for-context", params={"q": "   "})
            assert resp.status == 400
            assert (await resp.json())["error"] == "q parameter required"

    @pytest.mark.asyncio
    async def test_builds_citation_cards(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(
            monkeypatch,
            [
                {
                    "id": "i1",
                    "title": "Design",
                    "content": "abcd" * 10,
                    "source": "src",
                    "source_type": "local_folder",
                    "source_name": "Notes",
                    "source_uri": "/notes",
                    "file_path": "/notes/a.md",
                    "section_title": "Intro",
                    "chunk_range": "1-2",
                    "match_type": "vector",
                    "summary": "sum",
                }
            ],
        )
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get("/api/knowledge/search-for-context", params={"q": "design"})
            ).json()
        card = data["results"][0]
        assert card["title"] == "Design"
        assert card["match_type"] == "vector"
        assert card["source_name"] == "Notes"
        assert card["section_title"] == "Intro"
        assert card["tokens"] == 10
        assert data["total_tokens"] == 10

    @pytest.mark.asyncio
    async def test_untitled_and_default_match_type_fallbacks(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(monkeypatch, [{"id": "i1", "title": "", "content": "x"}])
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get("/api/knowledge/search-for-context", params={"q": "x"})
            ).json()
        card = data["results"][0]
        assert card["title"] == "(untitled)"
        assert card["match_type"] == "keyword"
        assert card["source_type"] is None
        # No summary in the result: the card falls back to the content head.
        assert card["summary"] == "x"

    @pytest.mark.asyncio
    async def test_config_budget_truncates_and_then_stops(self, store, monkeypatch, tmp_path):
        (tmp_path / "config.json").write_text(
            json.dumps({"knowledge": {"fetch_top_n": 5, "fetch_max_tokens": 4}})
        )
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(
            monkeypatch,
            [
                {"id": "i1", "title": "big", "content": "z" * 400},
                {"id": "i2", "title": "dropped", "content": "y" * 400},
            ],
        )
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get("/api/knowledge/search-for-context", params={"q": "z"})
            ).json()
        assert data["max_tokens"] == 4
        assert data["total_tokens"] == 4
        # First card is clipped to the budget; the second never gets a slot.
        assert [c["id"] for c in data["results"]] == ["i1"]
        assert len(data["results"][0]["content"]) == 16

    @pytest.mark.asyncio
    async def test_unreadable_config_falls_back_to_defaults(self, store, monkeypatch, tmp_path):
        (tmp_path / "config.json").write_text("{ not json")
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(monkeypatch, [])
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get("/api/knowledge/search-for-context", params={"q": "z"})
            ).json()
        assert data["max_tokens"] == kh.KNOWLEDGE_FETCH_MAX_TOKENS
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_wrong_typed_fetch_config_falls_back_to_defaults(
        self, store, monkeypatch, tmp_path
    ):
        """A hand-edited non-numeric ``fetch_top_n`` / ``fetch_max_tokens``
        degrades to the defaults, so the read-only GET returns 200 rather than
        letting a non-int reach ``int()``."""
        (tmp_path / "config.json").write_text(
            json.dumps({"knowledge": {"fetch_top_n": [], "fetch_max_tokens": {}}})
        )
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(monkeypatch, [])
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/search-for-context", params={"q": "z"})
            assert resp.status == 200
            data = await resp.json()
        assert data["max_tokens"] == kh.KNOWLEDGE_FETCH_MAX_TOKENS
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_non_numeric_limit_falls_back_to_top_n(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        captured = {}

        async def _direct(fn, *args, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(f"{MODULE}.run_in_embed_pool", _direct)
        monkeypatch.setattr(
            f"{MODULE}.HybridRetriever",
            lambda *a, **kw: MagicMock(search=MagicMock(return_value=[])),
        )
        async with _client(_make_app(store)) as client:
            resp = await client.get(
                "/api/knowledge/search-for-context", params={"q": "z", "limit": "lots"}
            )
            assert resp.status == 200
        assert captured["limit"] == kh.KNOWLEDGE_FETCH_TOP_N

    @pytest.mark.asyncio
    async def test_available_embedder_is_wired_into_the_retriever(
        self, store, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        seen = {}

        async def _direct(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        def _retriever(_store, embedder=None, *, embed_sig=None):
            seen["embedder"] = embedder
            seen["embed_sig"] = embed_sig
            return MagicMock(search=MagicMock(return_value=[]))

        monkeypatch.setattr(f"{MODULE}.run_in_embed_pool", _direct)
        monkeypatch.setattr(f"{MODULE}.HybridRetriever", _retriever)
        emb = _FakeEmbedder()
        async with _client(_make_app(store, embedder=emb)) as client:
            assert (
                await client.get("/api/knowledge/search-for-context", params={"q": "z"})
            ).status == 200
        assert seen["embedder"] == emb.embed
        # Wiring the embedder without its signature would leave the vector leg
        # scoring items from any space, which is the defect the pair closes.
        assert seen["embed_sig"] == embedder_signature(emb)

    @pytest.mark.asyncio
    async def test_unavailable_embedder_is_not_wired(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        seen = {}

        async def _direct(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        def _retriever(_store, embedder=None, *, embed_sig=None):
            seen["embedder"] = embedder
            seen["embed_sig"] = embed_sig
            return MagicMock(search=MagicMock(return_value=[]))

        monkeypatch.setattr(f"{MODULE}.run_in_embed_pool", _direct)
        monkeypatch.setattr(f"{MODULE}.HybridRetriever", _retriever)
        app = _make_app(store, embedder=_FakeEmbedder(available=False))
        async with _client(app) as client:
            assert (
                await client.get("/api/knowledge/search-for-context", params={"q": "z"})
            ).status == 200
        assert seen["embedder"] is None
        assert seen["embed_sig"] is None


class TestSearchForContextLimitCap:
    """The caller's ``limit`` is clamped server-side to [1, 100] before it
    reaches the retriever, so a query cannot ask for an unbounded result set."""

    @staticmethod
    async def _searched_limit(store, monkeypatch, tmp_path, limit: str) -> int:
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        captured = {}

        async def _direct(fn, *args, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(f"{MODULE}.run_in_embed_pool", _direct)
        monkeypatch.setattr(
            f"{MODULE}.HybridRetriever",
            lambda *a, **kw: MagicMock(search=MagicMock(return_value=[])),
        )
        async with _client(_make_app(store)) as client:
            resp = await client.get(
                "/api/knowledge/search-for-context", params={"q": "z", "limit": limit}
            )
            assert resp.status == 200
        return captured["limit"]

    @pytest.mark.asyncio
    async def test_limit_has_server_side_upper_bound(self, store, monkeypatch, tmp_path):
        assert await self._searched_limit(store, monkeypatch, tmp_path, "100000") == 100

    @pytest.mark.asyncio
    async def test_limit_has_positive_lower_bound(self, store, monkeypatch, tmp_path):
        assert await self._searched_limit(store, monkeypatch, tmp_path, "-5") == 1

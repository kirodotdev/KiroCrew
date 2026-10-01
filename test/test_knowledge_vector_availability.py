"""The knowledge vector path comes back as soon as the model is ready.

``InProcessEmbedder`` caches an "unavailable" verdict for ``NEGATIVE_CACHE_TTL``
seconds. A model that finishes loading inside that window must be used at
once, not after the window runs out. And while the vector path is down, the
search tool says its results are keyword-only.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from kiro_crew.knowledge import embedder as embedder_mod
from kiro_crew.knowledge.embedder import InProcessEmbedder


class _LoadingBackend:
    """A backend whose model is still loading until ``ready`` is set."""

    model_id = "fake-model"
    dim = 4

    def __init__(self) -> None:
        self.ready = False
        self.embed_calls = 0

    def is_ready(self) -> bool:
        return self.ready

    def embed(self, text: str, *, priority: int = 0) -> list[float] | None:
        self.embed_calls += 1
        return [0.1, 0.2, 0.3, 0.4] if self.ready else None


def _embedder_with(monkeypatch, backend: _LoadingBackend) -> InProcessEmbedder:
    emb = InProcessEmbedder()
    monkeypatch.setattr(emb, "_get_embedder", lambda: backend)
    return emb


def test_cached_unavailable_flips_once_the_model_is_ready(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(embedder_mod.time, "time", lambda: now[0])
    backend = _LoadingBackend()
    emb = _embedder_with(monkeypatch, backend)

    assert emb.is_available() is False
    backend.ready = True
    now[0] += 1  # far inside the TTL
    assert emb.is_available() is True
    assert emb._available is True


def test_async_fast_path_also_rechecks_a_cached_unavailable(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(embedder_mod.time, "time", lambda: now[0])
    backend = _LoadingBackend()
    emb = _embedder_with(monkeypatch, backend)

    assert emb.is_available() is False
    backend.ready = True
    now[0] += 1
    assert asyncio.run(emb.is_available_async()) is True


def test_cached_unavailable_holds_while_the_model_is_still_loading(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(embedder_mod.time, "time", lambda: now[0])
    backend = _LoadingBackend()
    emb = _embedder_with(monkeypatch, backend)

    assert emb.is_available() is False
    now[0] += 1
    assert emb.is_available() is False
    # Inside the TTL a not-ready backend is not probed again with an embed.
    assert backend.embed_calls == 1


def _run_search(tmp_path, embedder, results):
    db_dir = tmp_path / "workspace" / "knowledge"
    db_dir.mkdir(parents=True)
    (db_dir / "knowledge.db").touch()
    with (
        patch("kiro_crew.mcp_core.config_dir", return_value=tmp_path),
        patch("kiro_crew.mcp_core.KnowledgeStore"),
        patch("kiro_crew.mcp_core.create_embedder_from_config", return_value=embedder),
        patch("kiro_crew.mcp_core.HybridRetriever") as retriever_cls,
    ):
        retriever_cls.return_value.search.return_value = results
        from kiro_crew.mcp_core import _call_tool_inner

        return _call_tool_inner("local_knowledge_search", {"query": "auth"})


_HIT = [{"title": "Auth Design", "content": "JWT tokens.", "score": 0.035, "source": "s1"}]
_NOTE = "vector path unavailable; keyword results only"


def test_search_says_keyword_only_when_the_vector_path_is_down(tmp_path) -> None:
    unavailable = MagicMock()
    unavailable.is_available.return_value = False

    assert _NOTE in _run_search(tmp_path / "a", unavailable, _HIT)
    assert _NOTE in _run_search(tmp_path / "b", unavailable, [])


def test_search_does_not_say_keyword_only_when_the_vector_path_is_up(tmp_path) -> None:
    available = MagicMock()
    available.is_available.return_value = True

    with patch("kiro_crew.mcp_core.vector_leg", return_value=(None, None)):
        assert _NOTE not in _run_search(tmp_path, available, _HIT)

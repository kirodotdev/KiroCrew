"""Model prefetch: the vector leg starts the model load before the other legs run.

Pins that the load overlaps the keyword and graph legs, that results do not
change, that a failing load behaves as before (keyword-only answer, cooldown
kept), that a not-serving backend is left alone, and that a loaded model is not
loaded again.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from test_embeddings import _make_failing_llama_class, _make_fake_llama_class, _write_model_file

from kiro_crew import embeddings as embeddings_mod
from kiro_crew.embeddings import LlamaCppEmbedder
from kiro_crew.knowledge import retrieval
from kiro_crew.knowledge.embedder import InProcessEmbedder, floats_to_bytes
from kiro_crew.knowledge.retrieval import HybridRetriever, vector_leg
from kiro_crew.knowledge.store import KnowledgeStore

pytest.importorskip("numpy")

# A bound on every wait, so a broken handshake fails instead of hanging.
_WAIT_SECS = 10.0


def _gated_llama_class():
    """A fake model whose load signals ``started`` and then waits for ``release``."""
    base = _make_fake_llama_class()
    started = threading.Event()
    release = threading.Event()

    class _Gated(base):
        def __init__(self, **kwargs):
            started.set()
            release.wait(_WAIT_SECS)
            super().__init__(**kwargs)

    _Gated.close = lambda self: None
    return _Gated, started, release


@pytest.fixture()
def store(tmp_path: Path):
    retrieval.clear_vector_matrix_cache()
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    retrieval.clear_vector_matrix_cache()


def _backend(tmp_path, monkeypatch, cls):
    monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: cls)
    emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
    monkeypatch.setattr(embeddings_mod, "get_shared_embedder", lambda: emb)
    return emb


def _populate(store, embedder) -> None:
    from kiro_crew.knowledge.embedder import embedder_signature

    sig = embedder_signature(embedder)
    dim = embedder.dim
    for i, title in enumerate(("How the gateway starts", "Memory recall notes", "Cron schedule")):
        vec = [0.0] * dim
        vec[i] = 1.0
        item = store.add_item(title, f"{title} content {i}", "doc", embedding=floats_to_bytes(vec))
        store.db.execute("UPDATE items SET embedding_sig = ? WHERE id = ?", (sig, item))
    store.db.commit()


def _search(store, embedder, q="gateway"):
    fn, sig = vector_leg(embedder)
    return HybridRetriever(store, embedder=fn, embed_sig=sig).search(q, limit=5)


class TestOverlap:
    def test_prefetch_returns_while_the_load_is_still_running(self, tmp_path, monkeypatch):
        cls, started, release = _gated_llama_class()
        emb = _backend(tmp_path, monkeypatch, cls)
        try:
            InProcessEmbedder().prefetch()  # returns with the load held at the gate
            assert started.wait(_WAIT_SECS)
            assert not emb.is_ready()
            assert emb._load_thread is not None and emb._load_thread.is_alive()
            release.set()
            assert emb.wait_ready(timeout=_WAIT_SECS)
        finally:
            release.set()
            emb.close()

    def test_the_load_starts_before_the_keyword_leg_finishes(self, store, tmp_path, monkeypatch):
        cls, started, release = _gated_llama_class()
        emb = _backend(tmp_path, monkeypatch, cls)
        try:
            embedder = InProcessEmbedder()
            _populate(store, embedder)
            real_kw = HybridRetriever._keyword_search
            seen: list[bool] = []

            def kw(self, *a, **k):
                # Serial loading would only start after this leg returns, so
                # the load is observed here only when it overlaps the leg.
                seen.append(started.wait(_WAIT_SECS))
                release.set()
                emb.wait_ready(timeout=_WAIT_SECS)
                return real_kw(self, *a, **k)

            monkeypatch.setattr(HybridRetriever, "_keyword_search", kw)
            rows = _search(store, embedder)
            assert seen == [True]
            assert rows and "vector" in rows[0]["match_type"]
        finally:
            release.set()
            emb.close()


class TestSameResults:
    def test_results_match_a_search_without_prefetch(self, store, tmp_path, monkeypatch):
        emb = _backend(tmp_path, monkeypatch, _make_fake_llama_class())
        try:
            embedder = InProcessEmbedder()
            _populate(store, embedder)
            embedder.prefetch()  # the load this side uses is the prefetch-started one
            assert emb.wait_ready(timeout=_WAIT_SECS)
            with_prefetch = [_search(store, embedder, q) for q in ("gateway", "memory", "cron x")]
        finally:
            emb.close()
        # Same model file, prefetch off, model loaded before the first search: once
        # the model is in memory, prefetch must not change any result.
        monkeypatch.setattr(InProcessEmbedder, "prefetch", lambda self: None)
        emb = LlamaCppEmbedder(model_path=tmp_path / "model.gguf")
        monkeypatch.setattr(embeddings_mod, "get_shared_embedder", lambda: emb)
        try:
            embedder = InProcessEmbedder()
            emb.embed("warm up")
            assert emb.wait_ready(timeout=_WAIT_SECS)
            without = [_search(store, embedder, q) for q in ("gateway", "memory", "cron x")]
            assert with_prefetch == without
        finally:
            emb.close()


class TestFailureIsUnchanged:
    def test_failing_load_gives_the_keyword_answer_and_keeps_the_cooldown(
        self, store, tmp_path, monkeypatch
    ):
        failing = _make_failing_llama_class()
        emb = _backend(tmp_path, monkeypatch, failing)
        try:
            embedder = InProcessEmbedder()
            store.add_item("How the gateway starts", "notes", "doc")
            rows = _search(store, embedder)
            assert rows and "vector" not in rows[0]["match_type"]
            if emb._load_thread is not None:
                emb._load_thread.join(timeout=_WAIT_SECS)
            attempts = failing.init_attempts
            assert attempts >= 1
            InProcessEmbedder().prefetch()
            InProcessEmbedder().prefetch()
            if emb._load_thread is not None:
                emb._load_thread.join(timeout=_WAIT_SECS)
            # The failed-load cooldown still holds: no new load per prefetch.
            assert failing.init_attempts == attempts
        finally:
            emb.close()

    def test_not_serving_backend_is_not_loaded(self, tmp_path, monkeypatch):
        cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: cls)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"), serving=False)
        try:
            emb.prefetch()
            assert emb._load_thread is None
            assert cls.init_attempts == 0
        finally:
            emb.close()

    def test_a_raising_prefetch_does_not_break_the_search(self, store, tmp_path, monkeypatch):
        emb = _backend(tmp_path, monkeypatch, _make_fake_llama_class())
        try:
            embedder = InProcessEmbedder()
            _populate(store, embedder)

            def boom():
                raise RuntimeError("no")

            monkeypatch.setattr(emb, "prefetch", boom)
            assert _search(store, embedder)
        finally:
            emb.close()


class TestLoadedModel:
    def test_prefetch_on_a_loaded_model_starts_nothing(self, store, tmp_path, monkeypatch):
        cls = _make_fake_llama_class()
        emb = _backend(tmp_path, monkeypatch, cls)
        try:
            embedder = InProcessEmbedder()
            _populate(store, embedder)
            embedder.prefetch()
            assert emb.wait_ready(timeout=_WAIT_SECS)
            assert "vector" in _search(store, embedder)[0]["match_type"]
            assert emb.is_ready()
            attempts = cls.init_attempts
            InProcessEmbedder().prefetch()
            assert cls.init_attempts == attempts
            assert "vector" in _search(store, embedder)[0]["match_type"]
        finally:
            emb.close()

"""The idle evictor never unloads the embedding model under work.

An embed in flight (a build or a recall waiting on its vector, or on a load it
kicked) and a background sweep pausing between two texts both hold the model;
once neither is there, an idle model is unloaded as before.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import embeddings_helpers
from embeddings_helpers import _make_fake_llama_class, _write_model_file

from kiro_crew.embeddings import LlamaCppEmbedder
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_reset_embedding_singletons = embeddings_helpers._reset_embedding_singletons


def _loaded(tmp_path: Path, monkeypatch) -> LlamaCppEmbedder:
    fake_cls = _make_fake_llama_class()
    fake_cls.close = lambda self: None
    monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
    backend = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"))
    assert backend.embed("warm") is not None
    # Join the loader: evict_if_idle refuses while it is alive (its epilogue
    # starts the evictor after the model is published).
    assert backend.wait_ready(timeout=FALLBACK_TIMEOUT_SECS)
    assert backend._load_thread is not None and not backend._load_thread.is_alive()
    backend._last_used = time.monotonic() - 10_000
    return backend


def test_an_embed_in_flight_holds_the_model(tmp_path, monkeypatch):
    backend = _loaded(tmp_path, monkeypatch)
    try:
        entered, release = threading.Event(), threading.Event()
        real = backend._embed_batch

        def held(texts, *, priority):
            entered.set()
            assert release.wait(FALLBACK_TIMEOUT_SECS)
            return real(texts, priority=priority)

        backend._embed_batch = held  # type: ignore[method-assign]
        worker = threading.Thread(target=backend.embed, args=("a recall",), daemon=True)
        worker.start()
        assert entered.wait(FALLBACK_TIMEOUT_SECS)
        backend._last_used = time.monotonic() - 10_000
        assert backend.evict_if_idle(1.0) is False
        assert backend.is_ready()
        release.set()
        worker.join(FALLBACK_TIMEOUT_SECS)
        assert not worker.is_alive()
        backend._last_used = time.monotonic() - 10_000
        assert backend.evict_if_idle(1.0) is True
    finally:
        backend.close()


def test_a_background_sweep_between_texts_holds_the_model(tmp_path, monkeypatch):
    backend = _loaded(tmp_path, monkeypatch)
    try:
        backend._bulk_ready_at = time.monotonic() + 3600
        assert backend.evict_if_idle(1.0) is False
        backend._bulk_ready_at = 0.0
        assert backend.evict_if_idle(1.0) is True
    finally:
        backend.close()

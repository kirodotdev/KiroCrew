"""Fakes and helpers the ``test_embeddings*.py`` suites share.

Fake and failing stand-ins for the vendored ``Llama`` class, a model file wide
enough to read as present, and the autouse ``_reset_embedding_singletons``, which
each suite binds by name so pytest registers it for that module.
``test_knowledge_model_prefetch.py`` imports ``_make_failing_llama_class``,
``_make_fake_llama_class`` and ``_write_model_file`` from ``test_embeddings``, which
imports them from here.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

import pytest

import kiro_crew.embeddings as embeddings_mod
from conftest import forget_env_at_teardown
from kiro_crew.embeddings import reset_download_manager, reset_shared_embedder

# Captured at import so the lru_cache can always be cleared even while a test
# has monkeypatched the module attribute ``_load_llama_class``.
_REAL_LOAD_LLAMA = embeddings_mod._load_llama_class

_DIM = 1024
# Wider than the production floor so model_file_present() does not read the file
# as a truncated placeholder.
_MODEL_SIZE = embeddings_mod._GGUF_MIN_BYTES + 100_000


@lru_cache(maxsize=1)
def _model_bytes() -> bytes:
    """Stand-in GGUF payload, built on first use rather than at import.

    The embeddings suites assert on the bytes (sha256 pins, byte-identity after salvage),
    so the payload cannot shrink. Building it at module scope instead would
    allocate it while the module is IMPORTED, so every xdist worker would pay it
    during collection and hold it for the session -- including the workers that
    never run these suites.
    """
    return b"g" * _MODEL_SIZE


@pytest.fixture(autouse=True)
def _reset_embedding_singletons(_floor_monkeypatch: pytest.MonkeyPatch):
    """Isolate the shared embedder, download manager, and loader environment.

    The environment teardown rides the floor's own ``MonkeyPatch`` so a test's
    ``monkeypatch.undo()`` cannot lift it (D11).
    """
    forget_env_at_teardown(_floor_monkeypatch, embeddings_mod._LIB_PATH_ENV)
    reset_shared_embedder()
    reset_download_manager()
    _REAL_LOAD_LLAMA.cache_clear()
    yield
    reset_shared_embedder()
    reset_download_manager()
    _REAL_LOAD_LLAMA.cache_clear()


def _write_model_file(path: Path, payload: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_model_bytes() if payload is None else payload)
    return path


def _live_embed_threads() -> set[threading.Thread]:
    """The embedder's own threads (``kc-embed-*``) still alive in this process.

    ``wait_ready()`` joins the load thread and ``close()`` joins the inference
    worker, so a test that closes its embedder adds nothing to this set.
    """
    return {t for t in threading.enumerate() if t.name.startswith("kc-embed-") and t.is_alive()}


def _make_fake_llama_class(dim: int = _DIM):
    """Return a fresh fake Llama class recording constructor + embed calls."""

    class _FakeLlama:
        instances: list = []
        init_attempts: int = 0
        embed_error: Exception | None = None
        response_override: dict | None = None

        def __init__(self, **kwargs):
            type(self).init_attempts += 1
            self.kwargs = kwargs
            self.embed_calls: list[list[str]] = []
            type(self).instances.append(self)

        def create_embedding(self, texts):
            error = type(self).embed_error
            if error is not None:
                raise error
            self.embed_calls.append(list(texts))
            if type(self).response_override is not None:
                return type(self).response_override
            return {"data": [{"embedding": [0.1] * dim} for _ in texts]}

    return _FakeLlama


def _make_failing_llama_class():
    """Return a fake Llama class whose constructor always raises."""

    class _FailLlama:
        init_attempts: int = 0

        def __init__(self, **kwargs):
            type(self).init_attempts += 1
            raise RuntimeError("corrupt model file")

    return _FailLlama

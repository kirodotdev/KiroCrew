"""The embedder's KV clear resets cell metadata and never zeroes the buffer.

``Llama.embed`` clears the KV cache before and after every decode through
``LlamaContext.kv_cache_clear``, which in the vendored binding calls
``llama_memory_clear(memory, True)`` and so memsets the whole KV buffer twice
per embed. The embedder replaces that method on its own context with a
``data=False`` clear. These tests pin that the loaded model gets the override,
that it passes ``False``, that it is inert on a cache-less or closed context,
and that it does not keep the context alive.

The fake-runtime tests load no model. ``TestRealModelVectors`` runs only when
``KIROCREW_TEST_EMBED_MODEL`` names a GGUF file and the vendored runtime loads;
it compares vectors byte for byte against the binding's zeroing clear.
"""

from __future__ import annotations

import gc
import os
import pathlib
import struct
import sys
import types
import weakref

import pytest

from kiro_crew import embeddings as embeddings_mod
from kiro_crew.embeddings import LlamaCppEmbedder


class _FakeContext:
    """Stand-in for ``llama_cpp._internals.LlamaContext``."""

    def __init__(self, memory: object | None = "mem-handle") -> None:
        self.ctx: object | None = "ctx-handle"
        self.memory = memory
        self.zeroing_calls = 0

    def kv_cache_clear(self) -> None:
        self.zeroing_calls += 1


class _FakeLlama:
    def __init__(self, memory: object | None = "mem-handle") -> None:
        self._ctx = _FakeContext(memory)


@pytest.fixture
def native_calls(monkeypatch) -> list[tuple[object, bool]]:
    """Install a fake ``llama_cpp.llama_cpp`` that records memory_clear calls."""
    calls: list[tuple[object, bool]] = []
    native = types.ModuleType("llama_cpp.llama_cpp")
    native.llama_memory_clear = lambda memory, data: calls.append((memory, data))  # type: ignore[attr-defined]
    package = types.ModuleType("llama_cpp")
    package.llama_cpp = native  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", package)
    monkeypatch.setitem(sys.modules, "llama_cpp.llama_cpp", native)
    return calls


class TestInstallMetadataKvClear:
    def test_clear_keeps_the_buffer(self, native_calls) -> None:
        llm = _FakeLlama()
        assert embeddings_mod._install_metadata_kv_clear(llm) is True
        llm._ctx.kv_cache_clear()
        llm._ctx.kv_cache_clear()
        assert native_calls == [("mem-handle", False), ("mem-handle", False)]
        assert llm._ctx.zeroing_calls == 0

    def test_cache_less_context_is_a_no_op(self, native_calls) -> None:
        llm = _FakeLlama(memory=None)
        assert embeddings_mod._install_metadata_kv_clear(llm) is True
        llm._ctx.kv_cache_clear()
        assert native_calls == []

    def test_closed_context_is_a_no_op(self, native_calls) -> None:
        llm = _FakeLlama()
        embeddings_mod._install_metadata_kv_clear(llm)
        llm._ctx.ctx = None  # what LlamaContext.close() leaves behind
        llm._ctx.kv_cache_clear()
        assert native_calls == []

    def test_override_does_not_keep_the_context_alive(self, native_calls) -> None:
        llm = _FakeLlama()
        embeddings_mod._install_metadata_kv_clear(llm)
        ref = weakref.ref(llm._ctx)
        gc.disable()
        try:
            del llm
            assert ref() is None, "the override formed a reference cycle"
        finally:
            gc.enable()

    def test_missing_symbol_keeps_the_binding_clear(self, monkeypatch) -> None:
        native = types.ModuleType("llama_cpp.llama_cpp")
        package = types.ModuleType("llama_cpp")
        package.llama_cpp = native  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "llama_cpp", package)
        monkeypatch.setitem(sys.modules, "llama_cpp.llama_cpp", native)
        llm = _FakeLlama()
        assert embeddings_mod._install_metadata_kv_clear(llm) is False
        llm._ctx.kv_cache_clear()
        assert llm._ctx.zeroing_calls == 1

    def test_object_without_context_is_left_alone(self, native_calls) -> None:
        assert embeddings_mod._install_metadata_kv_clear(object()) is False


def test_loaded_model_gets_the_override(tmp_path: pathlib.Path, monkeypatch, native_calls) -> None:
    class _LoadedLlama:
        instances: list = []

        def __init__(self, **kwargs) -> None:
            self._ctx = _FakeContext()
            type(self).instances.append(self)

        def create_embedding(self, texts):
            return {"data": [{"embedding": [0.1] * 1024} for _ in texts]}

    monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: _LoadedLlama)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"g" * (embeddings_mod._GGUF_MIN_BYTES + 100_000))
    embedder = LlamaCppEmbedder(model_path=model)
    try:
        assert embedder.wait_ready(timeout=30)
        ctx = _LoadedLlama.instances[0]._ctx
        ctx.kv_cache_clear()
        assert native_calls == [("mem-handle", False)]
        assert ctx.zeroing_calls == 0
    finally:
        embedder.close()


_REAL_MODEL = os.environ.get("KIROCREW_TEST_EMBED_MODEL", "")


@pytest.mark.skipif(
    not (_REAL_MODEL and pathlib.Path(_REAL_MODEL).is_file()),
    reason="set KIROCREW_TEST_EMBED_MODEL to a GGUF embedding model to run",
)
class TestRealModelVectors:
    def test_vectors_match_the_zeroing_clear_byte_for_byte(self) -> None:
        llama_cls = embeddings_mod._load_llama_class()
        if llama_cls is None:
            pytest.skip("vendored llama.cpp runtime unavailable on this host")
        path = pathlib.Path(_REAL_MODEL)
        policy = embeddings_mod._model_context_policy(path)
        llm = llama_cls(
            model_path=str(path),
            embedding=True,
            pooling_type=embeddings_mod._POOLING_TYPE_LAST,
            n_ctx=policy.n_ctx,
            n_batch=policy.n_batch,
            n_ubatch=policy.n_ubatch,
            n_threads=4,
            n_threads_batch=4,
            verbose=False,
        )
        zeroing = llm._ctx.kv_cache_clear
        assert embeddings_mod._install_metadata_kv_clear(llm)
        metadata = llm._ctx.kv_cache_clear

        def vec(text: str, clear) -> bytes:
            llm._ctx.kv_cache_clear = clear
            values = llm.create_embedding([text])["data"][0]["embedding"]
            return struct.pack(f"{len(values)}f", *values)

        # Long and short inputs alternate so a stale cell from a longer
        # previous decode would surface in the next, shorter one.
        texts = [("memory gateway session " * n).strip() for n in (40, 1, 25, 2, 12, 3, 60, 1)]
        first = [vec(t, metadata) for t in texts]  # metadata clear on a fresh context
        reference = [vec(t, zeroing) for t in texts]
        alternating = [vec(t, metadata if i % 2 else zeroing) for i, t in enumerate(texts)]
        assert first == reference
        assert alternating == reference
        llm.close()

"""The embedder sizes its llama.cpp context for the texts one call carries.

llama.cpp reserves its output buffer for ``n_seq_max`` outputs of ``n_vocab + n_embd``
floats when the context is created, before it knows the graph, and never shrinks it.
The vendored binding sets ``n_seq_max = min(n_batch, 256)`` for every embedding
context, 149.11 MiB for the shipped model. The embedder never sends more than
``_MAX_EMBED_BATCH_TEXTS`` texts in one call, so it passes that cap through the
vendored ``n_seq_max`` keyword. These tests pin the call site and the binding;
``test_the_cap_shrinks_the_reservation_and_keeps_the_vectors`` loads a real model
when ``KIROCREW_TEST_EMBED_MODEL`` names a GGUF file.
"""

from __future__ import annotations

import ast
import os
import pathlib
import struct

import pytest

import kiro_crew.embeddings as embeddings_mod
from kiro_crew.embeddings import LlamaCppEmbedder
from kiro_crew.testing.wait import default_timeout

_VENDORED_LLAMA = (
    pathlib.Path(embeddings_mod.__file__).resolve().parent / "_vendor" / "llama_cpp" / "llama.py"
)
_REAL_MODEL = os.environ.get("KIROCREW_TEST_EMBED_MODEL", "")


def _llama_method(name: str) -> ast.FunctionDef:
    tree = ast.parse(_VENDORED_LLAMA.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Llama")
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)


def test_the_embedder_caps_sequences_at_the_texts_one_call_carries(tmp_path, monkeypatch) -> None:
    class _RecordingLlama:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            type(self).instances.append(self)

        def create_embedding(self, texts):
            return {"data": [{"embedding": [0.1] * embeddings_mod._DEFAULT_DIM} for _ in texts]}

    monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: _RecordingLlama)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"g" * (embeddings_mod._GGUF_MIN_BYTES + 100_000))
    emb = LlamaCppEmbedder(model_path=model)
    try:
        assert emb.wait_ready(timeout=default_timeout()), "fake model failed to load"
        assert _RecordingLlama.instances, "Llama constructor was never called"
    finally:
        emb.close()
    assert _RecordingLlama.instances[0].kwargs["n_seq_max"] == embeddings_mod._MAX_EMBED_BATCH_TEXTS


def test_the_vendored_constructor_takes_the_cap_with_a_none_default() -> None:
    init = _llama_method("__init__")
    default = next(
        v for a, v in zip(init.args.kwonlyargs, init.args.kw_defaults) if a.arg == "n_seq_max"
    )
    assert isinstance(default, ast.Constant) and default.value is None


def test_the_cap_is_written_in_the_embedding_branch_beside_the_unified_cache() -> None:
    """The constructor swallows unknown keywords, so a lost divergence would be silent."""
    branch = next(
        n
        for n in ast.walk(_llama_method("__init__"))
        if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "embedding"
    )
    body = [ast.unparse(s) for s in branch.body]
    assert "self.context_params.kv_unified = True" in body
    cap = next(s for s in body if s.startswith("self.context_params.n_seq_max = "))
    assert "if n_seq_max is None else n_seq_max" in cap


def test_a_pickled_llama_keeps_the_cap() -> None:
    """``__setstate__`` rebuilds through ``__init__(**state)``, so the state must carry it."""
    assert "n_seq_max=self.context_params.n_seq_max" in ast.unparse(_llama_method("__getstate__"))


@pytest.mark.skipif(
    not (_REAL_MODEL and pathlib.Path(_REAL_MODEL).is_file()),
    reason="set KIROCREW_TEST_EMBED_MODEL to a GGUF embedding model to run",
)
def test_the_cap_shrinks_the_reservation_and_keeps_the_vectors(capfd) -> None:
    llama_cls = embeddings_mod._load_llama_class()
    if llama_cls is None:
        pytest.skip("vendored llama.cpp runtime unavailable on this host")
    path = pathlib.Path(_REAL_MODEL)
    policy = embeddings_mod._model_context_policy(path)
    words = "memory gateway session vector index transcript slot crew".split()
    texts = [
        " ".join(words[(i * 3 + k) % len(words)] for k in range(n))
        for i, n in enumerate((3, 40, 120, 9, 260, 17, 80, 1))
    ]
    assert len(texts) == embeddings_mod._MAX_EMBED_BATCH_TEXTS

    def run(cap: int | None) -> tuple[float, list[bytes]]:
        capfd.readouterr()
        llm = llama_cls(
            model_path=str(path),
            embedding=True,
            pooling_type=embeddings_mod._POOLING_TYPE_LAST,
            n_ctx=policy.n_ctx,
            n_batch=policy.n_batch,
            n_ubatch=policy.n_ubatch,
            n_threads=4,
            n_threads_batch=4,
            n_seq_max=cap,
            verbose=True,
        )
        line = next(ln for ln in capfd.readouterr().err.splitlines() if "output buffer size" in ln)
        data = llm.create_embedding(texts)["data"]
        llm.close()
        vectors = [struct.pack(f"{len(d['embedding'])}f", *d["embedding"]) for d in data]
        return float(line.split("=")[1].split()[0]), vectors

    capped_mib, capped = run(embeddings_mod._MAX_EMBED_BATCH_TEXTS)
    upstream_mib, upstream = run(None)
    assert capped == upstream
    # 8 and 256 outputs of (151,669 + 1,024) floats for the shipped model.
    assert capped_mib < 10 < 100 < upstream_mib, (capped_mib, upstream_mib)

"""``memory.embedding_weight_repack`` lets an operator drop llama.cpp's repacked weight copy.

With repacking on (llama.cpp's default) the CPU backend keeps a second, anonymous copy
of the embedding weights beside the mmap'd file: 446 MiB for the shipped model. Turning
it off saves that memory but slows bulk embedding and moves vectors very slightly, so it
is an opt-out with the default unchanged. These tests pin the reader, that the default
constructor call stays the one earlier releases made, that only an explicit ``false``
reaches llama.cpp, and that the vendored binding still carries the parameter.

No test loads a real model or hits the network.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import kiro_crew.embeddings as embeddings_mod
from kiro_crew.embeddings import LlamaCppEmbedder

_VENDORED_LLAMA = (
    pathlib.Path(embeddings_mod.__file__).resolve().parent / "_vendor" / "llama_cpp" / "llama.py"
)

# ═══════════════════════════════════════════════════════════════════════════
# The reader
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ({}, True),
        ({"embedding_weight_repack": True}, True),
        ({"embedding_weight_repack": False}, False),
        # Only an explicit false opts out; a malformed value keeps the default.
        ({"embedding_weight_repack": 0}, True),
        ({"embedding_weight_repack": "false"}, True),
        ({"embedding_weight_repack": None}, True),
    ],
)
def test_only_an_explicit_false_turns_repacking_off(monkeypatch, section, expected) -> None:
    monkeypatch.setattr(embeddings_mod, "_read_memory_config", lambda: section)
    assert embeddings_mod.embedding_weight_repack() is expected


def test_the_config_dataclass_defaults_to_repacking() -> None:
    from kiro_crew.config.section_builders import _build_memory_config

    assert _build_memory_config({}).embedding_weight_repack is True
    assert _build_memory_config({"embedding_weight_repack": False}).embedding_weight_repack is False


# ═══════════════════════════════════════════════════════════════════════════
# The call site
# ═══════════════════════════════════════════════════════════════════════════


def _load_kwargs(tmp_path: pathlib.Path, monkeypatch, *, repack: bool) -> dict:
    class _RecordingLlama:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            type(self).instances.append(self)

        def create_embedding(self, texts):
            return {"data": [{"embedding": [0.1] * embeddings_mod._DEFAULT_DIM} for _ in texts]}

    monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: _RecordingLlama)
    monkeypatch.setattr("kiro_crew.embeddings.embedding_weight_repack", lambda: repack)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"g" * (embeddings_mod._GGUF_MIN_BYTES + 100_000))
    emb = LlamaCppEmbedder(model_path=model)
    try:
        assert emb.wait_ready(timeout=5), "fake model failed to load"
        assert _RecordingLlama.instances, "Llama constructor was never called"
    finally:
        emb.close()
    return _RecordingLlama.instances[0].kwargs


def test_the_default_load_passes_no_repack_argument(tmp_path, monkeypatch) -> None:
    """Unset, the constructor call is byte-for-byte the one earlier releases made."""
    assert "use_extra_bufts" not in _load_kwargs(tmp_path, monkeypatch, repack=True)


def test_an_opt_out_reaches_the_constructor(tmp_path, monkeypatch) -> None:
    assert _load_kwargs(tmp_path, monkeypatch, repack=False)["use_extra_bufts"] is False


# ═══════════════════════════════════════════════════════════════════════════
# The vendored binding
# ═══════════════════════════════════════════════════════════════════════════


def _llama_init() -> ast.FunctionDef:
    tree = ast.parse(_VENDORED_LLAMA.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Llama")
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")


def test_the_vendored_constructor_accepts_the_parameter_with_a_none_default() -> None:
    init = _llama_init()
    names = [a.arg for a in init.args.args + init.args.kwonlyargs]
    assert "use_extra_bufts" in names
    pairs = list(zip(reversed(init.args.args), reversed(init.args.defaults)))
    pairs += list(zip(init.args.kwonlyargs, init.args.kw_defaults))
    default = next(v for a, v in pairs if a.arg == "use_extra_bufts")
    assert isinstance(default, ast.Constant) and default.value is None


def test_the_vendored_constructor_sets_the_model_param_only_when_given() -> None:
    """A None argument must leave llama.cpp's default untouched (upstream behaviour)."""
    source = ast.unparse(_llama_init())
    assert "if use_extra_bufts is not None:" in source
    assert "self.model_params.use_extra_bufts = use_extra_bufts" in source


def test_a_pickled_llama_keeps_the_repack_choice() -> None:
    """``__setstate__`` rebuilds through ``__init__(**state)``, so the state must carry it."""
    tree = ast.parse(_VENDORED_LLAMA.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Llama")
    getstate = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__getstate__"
    )
    assert "use_extra_bufts=self.model_params.use_extra_bufts" in ast.unparse(getstate)

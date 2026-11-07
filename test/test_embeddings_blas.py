"""The last-token decode micro-batch stays below ggml's BLAS threshold where BLAS exists.

ggml-blas offloads a mat-mul only when every dimension is at least 32, and for a
quantized weight it first dequantizes the whole matrix to float32 into a work
buffer it keeps until the context is freed. The output projection is
vocabulary-sized, so on a runtime with that backend the first decode of 32 or more
tokens holds n_vocab * n_embd * 4 bytes (592.5 MiB for the shipped model). These
tests pin the pure rule, the registry probe, and that the embedder applies the
rule only to the vendored runtime, so a fake Llama class sees the policy unchanged
on any host.

No test loads a real model or hits the network.
"""

from __future__ import annotations

import pathlib
import struct

import kiro_crew.embeddings as embeddings_mod
import kiro_crew.embeddings_llama as embeddings_llama
from kiro_crew.embeddings import LlamaCppEmbedder
from kiro_crew.embeddings_blas import _BLAS_MIN_BATCH, blas_safe_ubatch, runtime_has_blas_backend

# ═══════════════════════════════════════════════════════════════════════════
# The rule
# ═══════════════════════════════════════════════════════════════════════════


def test_without_blas_the_policy_micro_batch_is_kept() -> None:
    assert blas_safe_ubatch(128, 2048, has_blas=False) == 128


def test_with_blas_a_bounded_micro_batch_drops_below_the_threshold() -> None:
    assert blas_safe_ubatch(128, 2048, has_blas=True) == _BLAS_MIN_BATCH - 1


def test_with_blas_a_whole_input_micro_batch_is_never_split() -> None:
    """Non-causal files and mean/CLS pooling need every token in one micro-batch."""
    assert blas_safe_ubatch(2048, 2048, has_blas=True) == 2048
    assert blas_safe_ubatch(512, 512, has_blas=True) == 512


def test_with_blas_an_already_small_micro_batch_is_kept() -> None:
    assert blas_safe_ubatch(16, 2048, has_blas=True) == 16


# ═══════════════════════════════════════════════════════════════════════════
# The registry probe
# ═══════════════════════════════════════════════════════════════════════════


def _fake_native_lib(names: list[str]):
    """An object shaped like the binding's CDLL: three ggml registry functions."""

    class _Lib:
        pass

    lib = _Lib()

    def dev_count():
        return len(names)

    def dev_get(index):
        return index + 1

    def dev_name(handle):
        return names[handle - 1].encode()

    lib.ggml_backend_dev_count = dev_count
    lib.ggml_backend_dev_get = dev_get
    lib.ggml_backend_dev_name = dev_name
    return lib


def test_probe_finds_a_registered_blas_device() -> None:
    assert runtime_has_blas_backend(_fake_native_lib(["MTL0", "BLAS", "CPU"])) is True


def test_probe_answers_false_without_one() -> None:
    assert runtime_has_blas_backend(_fake_native_lib(["CPU"])) is False


def test_probe_answers_false_when_the_registry_is_unreadable() -> None:
    assert runtime_has_blas_backend(None) is False
    assert runtime_has_blas_backend(object()) is False


# ═══════════════════════════════════════════════════════════════════════════
# The call site
# ═══════════════════════════════════════════════════════════════════════════


def _recording_llama_class(*, vendored: bool):
    class Llama:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            type(self).instances.append(self)

        def create_embedding(self, texts):
            return {"data": [{"embedding": [0.1] * embeddings_mod._DEFAULT_DIM} for _ in texts]}

    if vendored:
        Llama.__module__ = "llama_cpp.llama"
    return Llama


def _decoder_header() -> bytes:
    """A minimal readable GGUF v3 header naming a causal decoder architecture."""
    key, value = b"general.architecture", b"qwen3"
    header = struct.pack("<4sIQQ", b"GGUF", 3, 0, 1)
    header += struct.pack("<Q", len(key)) + key + struct.pack("<I", 8)
    return header + struct.pack("<Q", len(value)) + value


def _load_kwargs(
    tmp_path: pathlib.Path, monkeypatch, *, vendored: bool, has_blas: bool, readable: bool = True
) -> dict:
    fake_cls = _recording_llama_class(vendored=vendored)
    monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
    monkeypatch.setattr(embeddings_llama, "_runtime_has_blas", lambda: has_blas)
    model = tmp_path / "model.gguf"
    size = embeddings_mod._GGUF_MIN_BYTES + 100_000
    model.write_bytes(
        (_decoder_header() if readable else b"g").ljust(size, b"\0" if readable else b"g")
    )
    emb = LlamaCppEmbedder(model_path=model)
    try:
        assert emb.wait_ready(timeout=5), "fake model failed to load"
        assert fake_cls.instances, "Llama constructor was never called"
    finally:
        emb.close()
    return fake_cls.instances[0].kwargs


def test_vendored_runtime_with_blas_gets_the_lowered_micro_batch(tmp_path, monkeypatch) -> None:
    kwargs = _load_kwargs(tmp_path, monkeypatch, vendored=True, has_blas=True)
    assert kwargs["n_ubatch"] == _BLAS_MIN_BATCH - 1
    assert (kwargs["n_ctx"], kwargs["n_batch"]) == (2048, 2048)


def test_vendored_runtime_without_blas_keeps_the_policy(tmp_path, monkeypatch) -> None:
    kwargs = _load_kwargs(tmp_path, monkeypatch, vendored=True, has_blas=False)
    assert kwargs["n_ubatch"] == embeddings_mod._N_UBATCH


def test_a_non_vendored_class_never_reads_the_probe(tmp_path, monkeypatch) -> None:
    """A fake or future backend keeps the policy whatever the host's runtime has."""
    kwargs = _load_kwargs(tmp_path, monkeypatch, vendored=False, has_blas=True)
    assert kwargs["n_ubatch"] == embeddings_mod._N_UBATCH


def test_an_unreadable_header_keeps_the_policy_micro_batch(tmp_path, monkeypatch) -> None:
    """The fallback sizes any file as a decoder, so a non-causal model may sit behind it.

    Lowering its micro-batch would make it abort on inputs past 31 tokens instead of 128.
    """
    kwargs = _load_kwargs(tmp_path, monkeypatch, vendored=True, has_blas=True, readable=False)
    assert kwargs["n_ubatch"] == embeddings_mod._N_UBATCH

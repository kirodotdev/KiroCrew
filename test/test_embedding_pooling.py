"""A custom embedding model runs with the pooling its GGUF declares."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
from pathlib import Path

import pytest

from kiro_crew import embeddings as emb


def _gguf(path: Path, pooling_type: int | None, *, architecture: str = "bert") -> Path:
    """A minimal GGUF v3 KV header naming *architecture* and its pooling."""

    def string(value: str) -> bytes:
        return struct.pack("<Q", len(value.encode())) + value.encode()

    metadata = [("general.architecture", 8, string(architecture))]
    if pooling_type is not None:
        metadata.append((f"{architecture}.pooling_type", 4, struct.pack("<I", pooling_type)))
    payload = bytearray(struct.pack("<4sIQQ", b"GGUF", 3, 0, len(metadata)))
    for key, value_type, value in metadata:
        payload += string(key) + struct.pack("<I", value_type) + value
    path.write_bytes(bytes(payload))
    return path


@pytest.mark.parametrize(
    ("pooling_type", "pooling"),
    [(1, "mean"), (2, "cls"), (3, "last"), (0, "last"), (4, "last"), (None, "last")],
    ids=["mean", "cls", "last", "none", "rank", "undeclared"],
)
def test_the_declared_pooling_is_read_from_the_gguf(
    tmp_path: Path, pooling_type: int | None, pooling: str
) -> None:
    assert emb.gguf_declared_pooling(_gguf(tmp_path / "m.gguf", pooling_type)) == pooling


def test_an_unreadable_header_keeps_last_token_pooling(tmp_path: Path) -> None:
    model = tmp_path / "m.gguf"
    model.write_bytes(b"not a gguf")
    assert emb.gguf_declared_pooling(model) == "last"


def test_a_newly_recorded_model_takes_its_files_pooling_into_its_id(tmp_path: Path) -> None:
    model = _gguf(tmp_path / "m.gguf", 2)
    model_id = emb._custom_model_id(model, "")
    assert model_id == f"custom:sha256:{hashlib.sha256(model.read_bytes()).hexdigest()}@cls"
    assert emb.model_pooling_type(model_id) == 2


def test_the_header_is_read_once_per_file_stamp(tmp_path: Path, monkeypatch) -> None:
    # The dashboard's status poll recomputes an unrecorded id on the event loop;
    # it must reuse the pooling as it reuses the digest, not walk the header again.
    model = _gguf(tmp_path / "m.gguf", 1)
    reads: list[Path] = []
    real = emb.gguf_declared_pooling
    monkeypatch.setattr(emb, "gguf_declared_pooling", lambda path: reads.append(path) or real(path))
    first = emb._custom_model_id(model, "")
    assert emb._custom_model_id(model, "") == first
    assert reads == [model]


@pytest.mark.parametrize(
    "configured",
    ["operator-label", "custom:sha256:{digest}"],
    ids=["operator-label", "recorded-custom"],
)
def test_an_id_already_recorded_or_labelled_keeps_its_label(
    tmp_path: Path, configured: str
) -> None:
    # An existing custom model's vectors were made under its recorded id, so
    # reading the file's pooling must not move them to another space.
    model = _gguf(tmp_path / "m.gguf", 1)
    digest = hashlib.sha256(model.read_bytes()).hexdigest()
    label = configured.split(":sha256:", 1)[0]
    model_id = emb._custom_model_id(model, configured.format(digest=digest))
    assert model_id == f"{label}:sha256:{digest}"


def test_an_unchanged_file_with_a_new_stamp_keeps_its_pooled_id(
    tmp_path: Path, monkeypatch
) -> None:
    # A touch or a copy moves the stamp, not the bytes: the recorded id names this
    # digest, so it is kept whole, suffix included, and the header is not read.
    model = _gguf(tmp_path / "m.gguf", 1)
    recorded_id = emb._custom_model_id(model, "")
    assert recorded_id == f"custom:sha256:{hashlib.sha256(model.read_bytes()).hexdigest()}@mean"
    recorded_stamp = list(emb._model_file_stamp(model))
    before = model.stat()
    os.utime(model, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    assert list(emb._model_file_stamp(model)) != recorded_stamp
    reads: list[Path] = []
    monkeypatch.setattr(emb, "gguf_declared_pooling", lambda path: reads.append(path) or "cls")
    config = tmp_path / "config.json"
    memory = {
        "embed_model_path": str(model),
        "embed_model_id": recorded_id,
        "embed_model_stamp": recorded_stamp,
    }
    config.write_text(json.dumps({"memory": memory}), encoding="utf-8")
    assert emb._verify_custom_model(model, recorded_id, recorded_stamp, config) == recorded_id
    persisted = json.loads(config.read_text(encoding="utf-8"))["memory"]
    assert persisted["embed_model_id"] == recorded_id
    assert persisted["embed_model_stamp"] == list(emb._model_file_stamp(model))
    assert reads == []


@pytest.mark.parametrize(
    ("recorded", "replaced"),
    [
        ("custom:sha256:{digest}@cls", "custom:sha256:{digest}@mean"),
        ("custom:sha256:{digest}", "custom:sha256:{digest}@mean"),
        ("e5:sha256:{digest}", "e5:sha256:{digest}"),
        ("custom@cls:sha256:{digest}", "custom@cls:sha256:{digest}"),
    ],
    ids=["cls-custom", "plain-custom", "operator-label", "operator-label-with-at"],
)
def test_a_file_replaced_in_place_takes_its_own_pooling_under_a_custom_id(
    tmp_path: Path, recorded: str, replaced: str
) -> None:
    # A `custom` id's suffix names the old file's pooling; the digest changed, so
    # the store re-embeds anyway and the new file's id takes its own header's.
    model = _gguf(tmp_path / "m.gguf", 2)
    old = hashlib.sha256(model.read_bytes()).hexdigest()
    assert emb._custom_model_id(model, "") == f"custom:sha256:{old}@cls"
    recorded_stamp = list(emb._model_file_stamp(model))
    before = model.stat()
    _gguf(model, 1)
    # Same inode and size: move the mtime so the stamp both caches key on changes.
    os.utime(model, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    new = hashlib.sha256(model.read_bytes()).hexdigest()
    configured = recorded.format(digest=old)
    config = tmp_path / "config.json"
    memory = {
        "embed_model_path": str(model),
        "embed_model_id": configured,
        "embed_model_stamp": recorded_stamp,
    }
    config.write_text(json.dumps({"memory": memory}), encoding="utf-8")
    model_id = emb._verify_custom_model(model, configured, recorded_stamp, config)
    assert model_id == replaced.format(digest=new)
    assert json.loads(config.read_text(encoding="utf-8"))["memory"]["embed_model_id"] == model_id


@pytest.mark.parametrize(
    ("recorded", "suffix"),
    [("nothing", ""), ("another-file", ""), ("this-file", "@mean")],
    ids=["env-only", "env-over-another-recorded-file", "also-configured"],
)
def test_a_model_set_only_by_the_env_var_keeps_last_token_pooling(
    tmp_path: Path, monkeypatch, recorded: str, suffix: str
) -> None:
    # Only memory.embed_model_path is ever recorded, so a file named only by
    # the env var would otherwise take a pooling no apply chose on every start,
    # from its own header or from a `custom` id recorded for another file.
    model = _gguf(tmp_path / "m.gguf", 1)
    with model.open("r+b") as stream:
        stream.truncate(emb._GGUF_MIN_BYTES + 1)
    memory: dict[str, object] = {}
    if recorded == "this-file":
        memory = {"embed_model_path": str(model)}
    elif recorded == "another-file":
        other = _gguf(tmp_path / "other.gguf", 2)
        memory = {
            "embed_model_path": str(other),
            "embed_model_id": f"custom:sha256:{hashlib.sha256(other.read_bytes()).hexdigest()}@cls",
            "embed_model_stamp": list(emb._model_file_stamp(other)),
        }
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"memory": memory}), encoding="utf-8")
    monkeypatch.setattr(emb, "config_path", lambda: config)
    monkeypatch.setattr(emb, "_last_custom_model_error", "")
    monkeypatch.setenv(emb._MODEL_PATH_ENV, str(model))
    spec = emb.resolve_custom_model()
    assert spec is not None and spec.error == ""
    assert (
        spec.model_id == f"custom:sha256:{hashlib.sha256(model.read_bytes()).hexdigest()}{suffix}"
    )
    persisted = json.loads(config.read_text(encoding="utf-8"))["memory"]
    expected = spec.model_id if recorded == "this-file" else memory.get("embed_model_id", "")
    assert persisted.get("embed_model_id", "") == expected


def test_a_loop_caller_that_misses_the_pooling_cache_never_reads_the_header(
    tmp_path: Path, monkeypatch
) -> None:
    # An operator-labelled id caches the digest without the pooling, so a loop
    # caller recording the file afresh hits the digest cache and misses the pooling.
    model = _gguf(tmp_path / "m.gguf", 1)
    emb._custom_model_id(model, "e5")
    reads: list[Path] = []
    monkeypatch.setattr(emb, "gguf_declared_pooling", lambda path: reads.append(path) or "mean")

    async def on_the_loop() -> str:
        return emb._custom_model_id(model, "")

    with pytest.raises(emb._ModelIdentityUnverified):
        asyncio.run(on_the_loop())
    assert reads == []


_HEX = "a" * 64


@pytest.mark.parametrize(
    ("model_id", "pooling_type"),
    [
        (f"custom:sha256:{_HEX}@mean", 1),
        (f"custom:sha256:{_HEX}@cls", 2),
        (f"custom:sha256:{_HEX}", 3),
        # A label is the operator's own text, so `custom@mean` there names no
        # pooling: no id an earlier release recorded ends in a pooling suffix.
        (f"custom@mean:sha256:{_HEX}", 3),
        (f"e5:sha256:{_HEX}@mean", 3),
        (f"custom:sha256:{_HEX[:63]}@mean", 3),
        (f"custom:sha256:{_HEX}@last", 3),
        (f"custom:sha256:{_HEX}@max", 3),
        ("qwen3-embedding:0.6b", 3),
    ],
    ids=[
        "mean",
        "cls",
        "plain-custom",
        "old-shape-label",
        "operator-label-suffix",
        "short-digest",
        "last-suffix",
        "unknown-suffix",
        "bundled",
    ],
)
def test_only_a_custom_id_suffixed_after_its_digest_names_a_pooling(
    model_id: str, pooling_type: int
) -> None:
    assert emb.model_pooling_type(model_id) == pooling_type


class _FakeLlama:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def n_embd(self) -> int:
        return 4


def test_the_loader_passes_the_pooling_its_id_names(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(emb, "_load_llama_class", lambda: _FakeLlama)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"\0" * (emb._GGUF_MIN_BYTES + 1))
    embedder = emb.LlamaCppEmbedder(model_path=model, dim=4, model_id=f"custom:sha256:{_HEX}@cls")
    try:
        assert embedder.wait_ready(timeout=5)
        assert embedder._llm.kwargs["pooling_type"] == 2  # type: ignore[attr-defined]
    finally:
        embedder.close()


def test_an_id_recorded_under_a_hand_typed_pooling_label_keeps_last_token_pooling(
    tmp_path: Path, monkeypatch
) -> None:
    # An operator could type `custom@mean` as a label before the file's pooling
    # was read; the vectors under that id were made with last-token pooling, so
    # the file declaring mean must not change the pooling while the id is kept.
    model = _gguf(tmp_path / "m.gguf", 1)
    with model.open("r+b") as stream:
        stream.truncate(emb._GGUF_MIN_BYTES + 1)
    configured = f"custom@mean:sha256:{hashlib.sha256(model.read_bytes()).hexdigest()}"
    stamp = list(emb._model_file_stamp(model))
    memory = {
        "embed_model_path": str(model),
        "embed_model_id": configured,
        "embed_model_stamp": stamp,
        "embedding_dim": 4,
    }
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"memory": memory}), encoding="utf-8")
    monkeypatch.setattr(emb, "config_path", lambda: config)
    monkeypatch.setattr(emb, "_last_custom_model_error", "")
    monkeypatch.delenv(emb._MODEL_PATH_ENV, raising=False)
    assert emb._custom_model_id(model, configured, recorded_stamp=stamp) == configured
    spec = emb.resolve_custom_model()
    assert spec is not None and spec.error == "" and spec.model_id == configured
    assert json.loads(config.read_text(encoding="utf-8"))["memory"] == memory
    assert emb.model_pooling_type(spec.model_id) == emb._POOLING_TYPE_LAST
    monkeypatch.setattr(emb, "_load_llama_class", lambda: _FakeLlama)
    embedder = emb.LlamaCppEmbedder(model_path=spec.path, dim=4, model_id=spec.model_id)
    try:
        assert embedder.wait_ready(timeout=30)
        kwargs = embedder._llm.kwargs  # type: ignore[attr-defined]
        assert kwargs["pooling_type"] == emb._POOLING_TYPE_LAST
    finally:
        embedder.close()


@pytest.mark.parametrize(
    ("suffix", "n_ubatch"),
    [("@mean", emb._N_CTX), ("@cls", emb._N_CTX), ("", emb._N_UBATCH)],
    ids=["mean", "cls", "last"],
)
def test_a_decoder_runs_mean_and_cls_pooling_in_one_micro_batch(
    tmp_path: Path, monkeypatch, suffix: str, n_ubatch: int
) -> None:
    # llama.cpp pools each micro-batch on its own and keeps only the last one's
    # result, so a mean or CLS vector covers the input only when one micro-batch
    # holds all of it; the token last-token pooling reads is always in the last.
    model = _gguf(tmp_path / "m.gguf", None, architecture="qwen3")
    with model.open("r+b") as stream:
        stream.truncate(emb._GGUF_MIN_BYTES + 1)
    # The header is read, and read as causal: its own policy keeps the 512.
    assert emb._read_gguf_embedding_metadata(model).architecture == "qwen3"
    assert emb._model_context_policy(model) == (emb._N_CTX, emb._N_CTX, emb._N_UBATCH)
    monkeypatch.setattr(emb, "_load_llama_class", lambda: _FakeLlama)
    embedder = emb.LlamaCppEmbedder(
        model_path=model, dim=4, model_id=f"custom:sha256:{_HEX}{suffix}"
    )
    try:
        assert embedder.wait_ready(timeout=30)
        kwargs = embedder._llm.kwargs  # type: ignore[attr-defined]
        assert (kwargs["n_batch"], kwargs["n_ubatch"]) == (emb._N_CTX, n_ubatch)
    finally:
        embedder.close()

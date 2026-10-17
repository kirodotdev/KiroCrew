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
    assert model_id.startswith("custom@cls:sha256:")
    assert emb.model_pooling_type(model_id) == 2


def test_the_header_is_read_once_per_file_stamp(tmp_path: Path, monkeypatch) -> None:
    # The dashboard's status poll recomputes an unrecorded id on the event loop;
    # it must reuse the label as it reuses the digest, not walk the header again.
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


@pytest.mark.parametrize(
    ("label", "new_label"),
    [("custom@cls", "custom@mean"), ("custom", "custom@mean"), ("e5", "e5")],
    ids=["cls-label", "plain-custom", "operator-label"],
)
def test_a_file_replaced_in_place_takes_its_own_pooling_under_a_loader_label(
    tmp_path: Path, label: str, new_label: str
) -> None:
    # A loader label names the old file's pooling; the digest changed, so the
    # store re-embeds anyway and the new file is labelled from its own header.
    model = _gguf(tmp_path / "m.gguf", 2)
    old = hashlib.sha256(model.read_bytes()).hexdigest()
    assert emb._custom_model_id(model, "") == f"custom@cls:sha256:{old}"
    recorded_stamp = list(emb._model_file_stamp(model))
    before = model.stat()
    _gguf(model, 1)
    # Same inode and size: move the mtime so the stamp both caches key on changes.
    os.utime(model, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    new = hashlib.sha256(model.read_bytes()).hexdigest()
    configured = f"{label}:sha256:{old}"
    config = tmp_path / "config.json"
    memory = {
        "embed_model_path": str(model),
        "embed_model_id": configured,
        "embed_model_stamp": recorded_stamp,
    }
    config.write_text(json.dumps({"memory": memory}), encoding="utf-8")
    model_id = emb._verify_custom_model(model, configured, recorded_stamp, config)
    assert model_id == f"{new_label}:sha256:{new}"
    assert json.loads(config.read_text(encoding="utf-8"))["memory"]["embed_model_id"] == model_id


@pytest.mark.parametrize(
    ("recorded", "label"),
    [("nothing", "custom"), ("another-file", "custom"), ("this-file", "custom@mean")],
    ids=["env-only", "env-over-another-recorded-file", "also-configured"],
)
def test_a_model_set_only_by_the_env_var_keeps_last_token_pooling(
    tmp_path: Path, monkeypatch, recorded: str, label: str
) -> None:
    # Only memory.embed_model_path is ever recorded, so a file named only by
    # the env var would otherwise take a pooling no apply chose on every start,
    # from its own header or from a loader label recorded for another file.
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
            "embed_model_id": f"custom:sha256:{hashlib.sha256(other.read_bytes()).hexdigest()}",
            "embed_model_stamp": list(emb._model_file_stamp(other)),
        }
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"memory": memory}), encoding="utf-8")
    monkeypatch.setattr(emb, "config_path", lambda: config)
    monkeypatch.setattr(emb, "_last_custom_model_error", "")
    monkeypatch.setenv(emb._MODEL_PATH_ENV, str(model))
    spec = emb.resolve_custom_model()
    assert spec is not None and spec.error == ""
    assert spec.model_id.startswith(f"{label}:sha256:")
    persisted = json.loads(config.read_text(encoding="utf-8"))["memory"]
    expected = spec.model_id if recorded == "this-file" else memory.get("embed_model_id", "")
    assert persisted.get("embed_model_id", "") == expected


def test_a_loop_caller_that_misses_the_label_cache_never_reads_the_header(
    tmp_path: Path, monkeypatch
) -> None:
    # An operator-labelled id caches the digest without the label, so a loop
    # caller recording the file afresh hits the digest cache and misses the label.
    model = _gguf(tmp_path / "m.gguf", 1)
    emb._custom_model_id(model, "e5")
    reads: list[Path] = []
    monkeypatch.setattr(emb, "gguf_declared_pooling", lambda path: reads.append(path) or "mean")

    async def on_the_loop() -> str:
        return emb._custom_model_id(model, "")

    with pytest.raises(emb._ModelIdentityUnverified):
        asyncio.run(on_the_loop())
    assert reads == []


@pytest.mark.parametrize(
    ("pooling", "label", "pooling_type"),
    [("mean", "custom@mean", 1), ("cls", "custom@cls", 2), ("last", "custom", 3)],
)
def test_a_custom_label_carries_its_pooling(pooling: str, label: str, pooling_type: int) -> None:
    assert emb.custom_model_label(pooling) == label
    assert emb.model_pooling_type(f"{label}:sha256:{'a' * 64}") == pooling_type


@pytest.mark.parametrize(
    "model_id",
    [
        "qwen3-embedding:0.6b",
        f"custom:sha256:{'a' * 64}",
        f"custom@max:sha256:{'a' * 64}",
        # An operator's label meant nothing before pooling was read, so one that
        # happens to end in @mean must not change the pooling under its vectors.
        f"e5@mean:sha256:{'a' * 64}",
    ],
    ids=["bundled", "plain-custom", "unknown-pooling", "operator-label"],
)
def test_every_other_identity_keeps_last_token_pooling(model_id: str) -> None:
    assert emb.model_pooling_type(model_id) == emb._POOLING_TYPE_LAST


def test_the_loader_passes_the_pooling_its_id_names(tmp_path: Path, monkeypatch) -> None:
    class _FakeLlama:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def n_embd(self) -> int:
            return 4

    monkeypatch.setattr(emb, "_load_llama_class", lambda: _FakeLlama)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"\0" * (emb._GGUF_MIN_BYTES + 1))
    embedder = emb.LlamaCppEmbedder(
        model_path=model, dim=4, model_id=f"custom@cls:sha256:{'a' * 64}"
    )
    try:
        assert embedder.wait_ready(timeout=5)
        assert embedder._llm.kwargs["pooling_type"] == 2  # type: ignore[attr-defined]
    finally:
        embedder.close()

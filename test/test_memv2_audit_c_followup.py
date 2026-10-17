"""Custom-model status must stay cheap; bulk queue wait is not a recall budget."""

import asyncio
import json
import math
import threading
from types import SimpleNamespace

import pytest

from kiro_crew import embeddings as emb
from kiro_crew.dashboard.handlers import memory as handlers


def model_file(tmp_path):
    model = tmp_path / "model.gguf"
    with model.open("wb") as stream:
        stream.truncate(emb._GGUF_MIN_BYTES + 1)
    return model


@pytest.mark.asyncio
async def test_unchanged_status_reuses_persisted_digest(tmp_path, monkeypatch):
    model = model_file(tmp_path)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(handlers, "config_path", lambda: config)
    await handlers._write_embed_model_config(str(model), 2)
    settings = json.loads(config.read_text(encoding="utf-8"))["memory"]
    assert settings["embed_model_stamp"] == list(emb._model_file_stamp(model))
    # Simulate a fresh process: none of its in-memory hashes exist yet.
    emb._model_content_digest.cache_clear()
    monkeypatch.setattr(emb, "_read_memory_config", lambda: settings)
    monkeypatch.delenv(emb._MODEL_PATH_ENV, raising=False)
    monkeypatch.setattr(emb, "_sha256_file", lambda path: pytest.fail("status hashed weights"))
    monkeypatch.setattr(emb, "_shared_embedder", None)
    monkeypatch.setattr(emb, "_backend_factory", None)
    monkeypatch.setattr(
        handlers,
        "model_download_manager",
        lambda: SimpleNamespace(status={"step": "idle", "error": "", "attempt": 0}),
    )
    response = await handlers.api_memory_embedding_status(SimpleNamespace())
    body = json.loads(response.body)
    assert body["model_id"] == settings["embed_model_id"]
    assert body["setup_step"] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded_stamp", [None, [0] * 5])
async def test_missing_or_stale_stamp_heals_without_operator_action(
    tmp_path, monkeypatch, recorded_stamp
):
    model = model_file(tmp_path)
    config = tmp_path / "config.json"
    settings = {
        "embed_model_path": str(model),
        "embed_model_id": "custom:model.gguf:1100000",
        "embedding_dim": 2,
    }
    if recorded_stamp is not None:
        settings["embed_model_stamp"] = recorded_stamp
    config.write_text(json.dumps({"memory": settings}), encoding="utf-8")
    monkeypatch.setattr(emb, "config_path", lambda: config)
    monkeypatch.delenv(emb._MODEL_PATH_ENV, raising=False)
    monkeypatch.setattr(emb, "_shared_embedder", None)
    monkeypatch.setattr(emb, "_backend_factory", None)
    monkeypatch.setattr(emb, "_model_verification_thread", None)
    monkeypatch.setattr(
        handlers,
        "model_download_manager",
        lambda: SimpleNamespace(status={"step": "idle", "error": "", "attempt": 0}),
    )
    emb._model_content_digest.cache_clear()
    hashing, release = threading.Event(), threading.Event()
    digest = emb._sha256_file
    loop_thread = threading.get_ident()
    calls = []

    def hash_off_loop(path):
        assert threading.get_ident() != loop_thread
        calls.append(path)
        hashing.set()
        assert release.wait(5)
        return digest(path)

    monkeypatch.setattr(emb, "_sha256_file", hash_off_loop)
    constructing = threading.Event()
    verify = emb._verify_custom_model

    def verify_and_signal(*args):
        if threading.current_thread() is not emb._model_verification_thread:
            constructing.set()
        return verify(*args)

    monkeypatch.setattr(emb, "_verify_custom_model", verify_and_signal)
    constructor = None
    try:
        first = json.loads((await handlers.api_memory_embedding_status(SimpleNamespace())).body)
        assert "unverified" in first["setup_error"]
        assert first["server_healthy"] is False
        assert emb._shared_embedder is None
        assert await asyncio.to_thread(hashing.wait, 2)
        worker = emb._model_verification_thread
        constructor = asyncio.create_task(asyncio.to_thread(emb.get_shared_embedder))
        assert await asyncio.to_thread(constructing.wait, 2)
        await handlers.api_memory_embedding_status(SimpleNamespace())
        assert emb._model_verification_thread is worker
        release.set()
        await asyncio.to_thread(worker.join, 5)
        assert not worker.is_alive()
        backend = await asyncio.wait_for(constructor, 5)
        assert backend.model_path == model
        healed = json.loads((await handlers.api_memory_embedding_status(SimpleNamespace())).body)
        assert ":sha256:" in healed["model_id"]
        assert healed["setup_step"] == "done"
        assert healed["server_healthy"] is True
        assert calls == [model]
        persisted = json.loads(config.read_text(encoding="utf-8"))["memory"]
        assert persisted["embed_model_id"] == healed["model_id"]
        assert persisted["embed_model_stamp"] == list(emb._model_file_stamp(model))
        emb._model_content_digest.cache_clear()
        monkeypatch.setattr(emb, "_shared_embedder", None)
        restarted = json.loads((await handlers.api_memory_embedding_status(SimpleNamespace())).body)
        assert restarted["setup_step"] == "done"
        assert restarted["model_id"] == healed["model_id"]
        assert calls == [model]
    finally:
        release.set()
        if constructor is not None:
            await asyncio.wait_for(asyncio.gather(constructor, return_exceptions=True), 5)
        worker = emb._model_verification_thread
        if worker is not None:
            await asyncio.to_thread(worker.join, 5)
            assert not worker.is_alive()


@pytest.mark.asyncio
async def test_changed_file_gets_new_identity_on_apply(tmp_path, monkeypatch):
    model = model_file(tmp_path)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(handlers, "config_path", lambda: config)
    calls = []
    digest = emb._sha256_file
    loop_thread = threading.get_ident()

    def off_loop(path):
        assert threading.get_ident() != loop_thread
        calls.append(path)
        return digest(path)

    monkeypatch.setattr(emb, "_sha256_file", off_loop)
    await handlers._write_embed_model_config(str(model), 2)
    old = json.loads(config.read_text(encoding="utf-8"))["memory"]
    replacement = tmp_path / "replacement.gguf"
    with replacement.open("wb") as stream:
        stream.write(b"changed")
        stream.truncate(model.stat().st_size)
    replacement.replace(model)
    await handlers._write_embed_model_config(str(model), 2)
    new = json.loads(config.read_text(encoding="utf-8"))["memory"]
    assert new["embed_model_id"] != old["embed_model_id"]
    assert new["embed_model_stamp"] != old["embed_model_stamp"]
    assert len(calls) == 2


def test_bulk_job_has_no_implicit_recall_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(emb, "_EMBED_WAIT_SECS", -1.0)
    backend = emb.LlamaCppEmbedder(model_path=tmp_path / "model.gguf", dim=2)
    calls = []
    native = SimpleNamespace(create_embedding=lambda texts: calls.append(texts))
    backend._llm = native
    try:
        job = backend._submit_infer(native, ["bulk"], emb.PRIORITY_BULK)
        assert job.work.deadline == math.inf
        assert calls == [["bulk"]]
    finally:
        backend.close()


def test_bulk_shared_cache_retries_after_explicit_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(emb, "_EMBED_WAIT_SECS", -1.0)
    backend = emb.LlamaCppEmbedder(model_path=tmp_path / "model.gguf", dim=2)
    calls = []

    def infer(texts):
        calls.extend(texts)
        return {"data": [{"embedding": [1.0, 0.0]} for _ in texts]}

    backend._llm = SimpleNamespace(create_embedding=infer)
    monkeypatch.setattr(emb, "get_shared_embedder", lambda: backend)
    previous = emb.embedding_work.get()
    emb.embedding_work.set(emb.EmbeddingWork(deadline=0))
    try:
        assert emb._shared_sync_embed("retry", priority=emb.PRIORITY_BULK) is None
        assert calls == []
    finally:
        emb.embedding_work.set(previous)
    try:
        assert emb._shared_sync_embed("retry", priority=emb.PRIORITY_BULK) == [1.0, 0.0]
        assert calls == ["retry"]
    finally:
        backend.close()


@pytest.mark.parametrize("pace", [False, True])
def test_expired_backfill_remains_pending_for_next_repair(tmp_path, monkeypatch, pace):
    from kiro_crew.vector_memory import VectorMemoryStore

    backend = emb.LlamaCppEmbedder(model_path=tmp_path / "model.gguf", dim=2)
    backend._llm = SimpleNamespace(
        create_embedding=lambda texts: {"data": [{"embedding": [1.0, 0.0]} for _ in texts]}
    )
    monkeypatch.setattr(emb, "get_shared_embedder", lambda: backend)
    monkeypatch.setattr(emb, "bulk_pace_delay", lambda elapsed: 0)
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    store.init()
    try:
        store.write_episodic("deployment outage needs a durable repair record")
        store.embed_fn = emb.make_sync_embed_fn()
        emb.align_store_embedding_space(store)
        previous = emb.embedding_work.get()
        emb.embedding_work.set(emb.EmbeddingWork(deadline=0))
        try:
            assert store.backfill_missing_embeddings(pace=pace) == 0
            assert store.has_pending_embeddings()
        finally:
            emb.embedding_work.set(previous)
        assert store.backfill_missing_embeddings(pace=pace) == 1
        assert not store.has_pending_embeddings()
    finally:
        store.close()
        backend.close()


@pytest.mark.parametrize("change_config", [False, True])
def test_worker_resolution_persists_only_its_config_generation(
    tmp_path, monkeypatch, change_config
):
    model = model_file(tmp_path)
    config = tmp_path / "config.json"
    settings = {"embed_model_path": str(model), "embed_model_id": "legacy", "embedding_dim": 2}
    config.write_text(json.dumps({"memory": settings}), encoding="utf-8")
    monkeypatch.setattr(emb, "config_path", lambda: config)
    monkeypatch.delenv(emb._MODEL_PATH_ENV, raising=False)
    digest = emb._sha256_file

    def hash_weights(path):
        result = digest(path)
        if change_config:
            config.write_text(
                json.dumps({"memory": {"embed_model_path": "another-model"}}), encoding="utf-8"
            )
        return result

    monkeypatch.setattr(emb, "_sha256_file", hash_weights)
    resolved = emb.resolve_custom_model()
    assert not resolved.error
    assert ":sha256:" in resolved.model_id
    persisted = json.loads(config.read_text(encoding="utf-8"))["memory"]
    if change_config:
        assert persisted == {"embed_model_path": "another-model"}
    else:
        assert persisted["embed_model_id"] == resolved.model_id
        assert persisted["embed_model_stamp"] == list(emb._model_file_stamp(model))


def test_verification_preserves_unreadable_config(tmp_path):
    model = model_file(tmp_path)
    config = tmp_path / "config.json"
    config.write_text("{invalid", encoding="utf-8")
    with pytest.raises(OSError, match="unreadable config"):
        emb._verify_custom_model(model, "legacy", None, config)
    assert config.read_text(encoding="utf-8") == "{invalid"

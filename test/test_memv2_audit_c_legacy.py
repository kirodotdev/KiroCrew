"""One-time custom identity migration preserves only attributable vectors."""

import json
import struct

import pytest
from member_memory_helpers import declare_v2_store

from kiro_crew import embeddings as emb
from kiro_crew import memory_stores
from kiro_crew.vector_memory import VectorMemoryStore


@pytest.fixture
def migration(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "memory_stores")
    monkeypatch.delenv(emb._MODEL_PATH_ENV, raising=False)
    config = tmp_path / "config.json"
    monkeypatch.setattr(emb, "config_path", lambda: config)
    model = tmp_path / "model.gguf"
    with model.open("wb") as stream:
        stream.truncate(emb._GGUF_MIN_BYTES + 1)
    private = declare_v2_store(tmp_path, "member-test")
    stores = [
        VectorMemoryStore(db_path=path, embedding_dim=2)
        for path in (tmp_path / "memory.db", private / "memory.db")
    ]
    try:
        for store in stores:
            store.init()
            store.write_episodic("a durable deployment incident", defer_embedding=True)
            store.set_semantic("project.status", "active", 1.0, "test")
            store.db.execute(
                f"UPDATE {store._epi_rel} SET embedding = ?", (struct.pack("2f", 1.0, 0.0),)
            )
            store.db.execute(
                f"UPDATE {store._sem_rel} SET embedding = ? WHERE 1 = 1{store._sem_guard}",
                (struct.pack("2f", 0.0, 1.0),),
            )
            store.db.commit()
        assert [store.algorithm_version for store in stores] == ["v1", "v2"]
        yield config, model, stores
    finally:
        for store in stores:
            store.close()


def vectors(store):
    return [
        row[0]
        for relation in ("episodic_memories", "semantic_memory")
        for row in store.db.execute(
            f"SELECT embedding FROM {relation} WHERE embedding IS NOT NULL"
        ).fetchall()
    ]


def activate(monkeypatch):
    backend = emb.default_embedding_backend()
    backend._llm = object()
    monkeypatch.setattr(emb, "get_shared_embedder", lambda: backend)
    monkeypatch.setattr(backend, "embed", lambda *args, **kwargs: pytest.fail("migration embedded"))
    return backend


@pytest.mark.parametrize("label", ["", "operator-label"])
def test_first_verified_stamp_preserves_v1_and_v2_vectors(migration, monkeypatch, label):
    config, model, stores = migration
    legacy = label or f"custom:{model.name}:{model.stat().st_size}"
    config.write_text(
        json.dumps(
            {
                "memory": {
                    "embed_model_path": str(model),
                    "embed_model_id": label,
                    "embedding_dim": 2,
                }
            }
        ),
        encoding="utf-8",
    )
    old = emb.embedding_space_signature(legacy, 2)
    for store in stores:
        store.reconcile_embedding_space(old)
    backend = activate(monkeypatch)
    new = emb.embedding_space_signature(backend.model_id, 2)
    assert new != old
    for store in stores:
        before, generation = vectors(store), store.space_generation
        assert len(before) == 2
        assert emb.align_store_embedding_space(store) == 0
        assert store.recorded_embedding_space() == new
        assert vectors(store) == before
        assert store.space_generation == generation
        assert not store.has_pending_embeddings()


def test_unrelated_space_still_clears(migration, monkeypatch):
    config, model, stores = migration
    config.write_text(
        json.dumps({"memory": {"embed_model_path": str(model), "embedding_dim": 2}}),
        encoding="utf-8",
    )
    for store in stores:
        store.reconcile_embedding_space(emb.embedding_space_signature("unrelated", 2))
    activate(monkeypatch)
    for store in stores:
        assert emb.align_store_embedding_space(store) == 2
        assert vectors(store) == []


def test_same_name_size_content_change_invalidates_migration_alias(migration, monkeypatch):
    config, model, stores = migration
    config.write_text(
        json.dumps({"memory": {"embed_model_path": str(model), "embedding_dim": 2}}),
        encoding="utf-8",
    )
    old = emb.embedding_space_signature(f"custom:{model.name}:{model.stat().st_size}", 2)
    for store in stores:
        store.reconcile_embedding_space(old)
    first = emb.default_embedding_backend()
    replacement = model.with_suffix(".replacement")
    with replacement.open("wb") as stream:
        stream.write(b"changed")
        stream.truncate(model.stat().st_size)
    replacement.replace(model)
    second = activate(monkeypatch)
    assert first.model_id != second.model_id
    for store in stores:
        assert emb.align_store_embedding_space(store) == 2
        assert vectors(store) == []


def test_existing_stamp_cannot_authorize_legacy_equivalence(migration, monkeypatch):
    config, model, stores = migration
    identity = emb._custom_model_id(model, "")
    config.write_text(
        json.dumps(
            {
                "memory": {
                    "embed_model_path": str(model),
                    "embedding_dim": 2,
                    "embed_model_id": identity,
                    "embed_model_stamp": list(emb._model_file_stamp(model)),
                }
            }
        ),
        encoding="utf-8",
    )
    for store in stores:
        store.reconcile_embedding_space(
            emb.embedding_space_signature(f"custom:{model.name}:{model.stat().st_size}", 2)
        )
    activate(monkeypatch)
    for store in stores:
        assert emb.align_store_embedding_space(store) == 2
        assert vectors(store) == []


@pytest.mark.asyncio
async def test_loop_waits_for_migration_proof_after_config_write(migration, monkeypatch):
    import asyncio
    import threading

    from kiro_crew.config import loader

    config, model, stores = migration
    config.write_text(
        json.dumps({"memory": {"embed_model_path": str(model), "embedding_dim": 2}}),
        encoding="utf-8",
    )
    legacy = emb.embedding_space_signature(f"custom:{model.name}:{model.stat().st_size}", 2)
    for store in stores:
        await asyncio.to_thread(store.reconcile_embedding_space, legacy)
    written, release = threading.Event(), threading.Event()
    update = loader.update_config_locked

    def write_then_pause(*args, **kwargs):
        result = update(*args, **kwargs)
        written.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(loader, "update_config_locked", write_then_pause)
    monkeypatch.setattr(emb, "_model_verification_thread", None)
    monkeypatch.setattr(emb, "_shared_embedder", None)
    monkeypatch.setattr(emb, "_backend_factory", None)
    try:
        emb.get_shared_embedder()
        assert await asyncio.to_thread(written.wait, 2)
        assert emb.get_shared_embedder().model_id == "custom:unverified"
        assert emb._shared_embedder is None
        release.set()
        await asyncio.to_thread(emb._model_verification_thread.join, 5)
        backend = emb.get_shared_embedder()
        backend._llm = object()
        for store in stores:
            assert await asyncio.to_thread(emb.align_store_embedding_space, store) == 0
            assert len(await asyncio.to_thread(vectors, store)) == 2
    finally:
        release.set()
        if emb._model_verification_thread is not None:
            await asyncio.to_thread(emb._model_verification_thread.join, 5)
            assert not emb._model_verification_thread.is_alive()


@pytest.mark.parametrize("label", ["", "operator-label"])
def test_lazily_opened_store_keeps_vectors_after_restart(migration, monkeypatch, label):
    config, model, stores = migration
    legacy = label or f"custom:{model.name}:{model.stat().st_size}"
    config.write_text(
        json.dumps(
            {
                "memory": {
                    "embed_model_path": str(model),
                    "embed_model_id": label,
                    "embedding_dim": 2,
                }
            }
        ),
        encoding="utf-8",
    )
    before = []
    for store in stores:
        store.reconcile_embedding_space(emb.embedding_space_signature(legacy, 2))
        before.append(vectors(store))
        store.close()
    first = emb.default_embedding_backend()
    persisted = json.loads(config.read_text(encoding="utf-8"))["memory"]
    assert legacy in persisted["embed_model_legacy_ids"]
    emb._model_content_digest.cache_clear()
    monkeypatch.setattr(emb, "_shared_embedder", None)
    monkeypatch.setattr(emb, "_backend_factory", None)
    monkeypatch.setattr(emb, "_sha256_file", lambda path: pytest.fail("restart hashed weights"))
    second = activate(monkeypatch)
    assert second is not first
    assert second.model_id == first.model_id
    for original, expected in zip(stores, before):
        reopened = VectorMemoryStore(db_path=original._db_path, embedding_dim=2)
        reopened.init()
        try:
            generation = reopened.space_generation
            assert emb.align_store_embedding_space(reopened) == 0
            assert vectors(reopened) == expected
            assert reopened.space_generation == generation
            assert reopened.recorded_embedding_space() == emb.embedding_space_signature(
                second.model_id, 2
            )
        finally:
            reopened.close()


def test_identity_metadata_is_typed_and_round_trips(migration, monkeypatch):
    from kiro_crew.config import loader
    from kiro_crew.config.schema import JSON_SCHEMA

    config, model, _stores = migration
    stamp = list(emb._model_file_stamp(model))
    labels = [f"custom:{model.name}:{stamp[2]}", "operator-label"]
    config.write_text(
        json.dumps(
            {
                "memory": {
                    "embed_model_path": str(model),
                    "embed_model_id": emb._custom_model_id(model, ""),
                    "embed_model_stamp": stamp,
                    "embed_model_legacy_ids": labels,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "config_path", lambda: config)
    loaded = loader.KiroCrewConfig.load()
    assert loaded.memory.embed_model_stamp == stamp
    assert loaded.memory.embed_model_legacy_ids == labels
    loaded.save()
    saved = json.loads(config.read_text(encoding="utf-8"))["memory"]
    assert saved["embed_model_stamp"] == stamp
    assert saved["embed_model_legacy_ids"] == labels
    fields = JSON_SCHEMA["properties"]["memory"]["properties"]
    assert fields["embed_model_stamp"]["type"] == "array"
    assert fields["embed_model_stamp"]["items"]["type"] == "integer"
    assert fields["embed_model_legacy_ids"]["items"]["type"] == "string"


@pytest.mark.asyncio
async def test_apply_clears_same_identity_inheritance(migration, monkeypatch):
    import asyncio

    from kiro_crew.dashboard.handlers import memory

    config, model, _stores = migration
    identity = await asyncio.to_thread(emb._custom_model_id, model, "")
    config.write_text(
        json.dumps(
            {
                "memory": {
                    "embed_model_path": str(model),
                    "embed_model_id": identity,
                    "embed_model_stamp": list(emb._model_file_stamp(model)),
                    "embed_model_legacy_ids": ["legacy"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(memory, "config_path", lambda: config)
    await memory._write_embed_model_config(str(model), 2)
    assert not json.loads(config.read_text(encoding="utf-8"))["memory"].get(
        "embed_model_legacy_ids"
    )
    with model.open("r+b") as stream:
        stream.write(b"changed")
    await memory._write_embed_model_config(str(model), 2)
    saved = json.loads(config.read_text(encoding="utf-8"))["memory"]
    assert saved["embed_model_id"] != identity
    assert not saved.get("embed_model_legacy_ids")


@pytest.mark.parametrize("failure", ["unlinked", "unreadable"])
def test_unavailable_loaded_model_stamp_reconciles_normally(migration, monkeypatch, failure):
    config, model, stores = migration
    config.write_text(
        json.dumps({"memory": {"embed_model_path": str(model), "embedding_dim": 2}}),
        encoding="utf-8",
    )
    legacy = emb.embedding_space_signature(f"custom:{model.name}:{model.stat().st_size}", 2)
    for store in stores:
        store.reconcile_embedding_space(legacy)
    backend = activate(monkeypatch)
    active = emb.embedding_space_signature(backend.model_id, 2)
    if failure == "unlinked":
        model.unlink()
    else:

        def unreadable(path):
            raise PermissionError("model stamp unavailable")

        monkeypatch.setattr(emb, "_model_file_stamp", unreadable)
    for store in stores:
        assert emb.align_store_embedding_space(store) == 2
        assert store.recorded_embedding_space() == active
        assert vectors(store) == []
        assert store.get_episodic_list()[0]["text"] == "a durable deployment incident"


@pytest.mark.asyncio
async def test_aligned_unlinked_model_keeps_vectors_and_warm_store(migration, monkeypatch):
    import asyncio

    from kiro_crew import context

    config, model, stores = migration
    config.write_text(
        json.dumps({"memory": {"embed_model_path": str(model), "embedding_dim": 2}}),
        encoding="utf-8",
    )
    legacy = emb.embedding_space_signature(f"custom:{model.name}:{model.stat().st_size}", 2)
    for store in stores:
        store.reconcile_embedding_space(legacy)
    backend = await asyncio.to_thread(activate, monkeypatch)
    for store in stores:
        await asyncio.to_thread(emb.align_store_embedding_space, store)
    before = [vectors(store) for store in stores]
    model.unlink()
    for store, expected in zip(stores, before):
        assert await asyncio.to_thread(emb.align_store_embedding_space, store) == 0
        assert vectors(store) == expected
        assert store.recorded_embedding_space() == emb.embedding_space_signature(
            backend.model_id, 2
        )
    monkeypatch.setattr(context, "_resolved_store_name", lambda name: name)
    monkeypatch.setattr(context, "_vector_stores", {"member-test": stores[1]})
    assert await context.ContextBuilder.ensure_store("member-test") is stores[1]
    assert vectors(stores[1]) == before[1]


def test_legacy_inheritance_warns_and_status_names_rebuild(migration, monkeypatch, caplog):
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import memory

    config, model, stores = migration
    config.write_text(
        json.dumps({"memory": {"embed_model_path": str(model), "embedding_dim": 2}}),
        encoding="utf-8",
    )
    old_id = emb._custom_model_id(model, "")
    legacy = emb.embedding_space_signature(f"custom:{model.name}:{model.stat().st_size}", 2)
    for store in stores:
        store.reconcile_embedding_space(legacy)
    before = [vectors(store) for store in stores]
    with model.open("r+b") as stream:
        stream.write(b"different weights")
    backend = activate(monkeypatch)
    assert backend.model_id != old_id
    for store, expected in zip(stores, before):
        assert emb.align_store_embedding_space(store) == 0
        assert vectors(store) == expected
        assert store.recorded_embedding_space() == emb.embedding_space_signature(
            backend.model_id, 2
        )
    assert "model file" in caplog.text.lower()
    assert "reapply" in caplog.text.lower()
    emb.resolve_custom_model()
    assert (
        sum(record.getMessage() == emb.LEGACY_EMBEDDING_WARNING for record in caplog.records) == 1
    )
    monkeypatch.setattr(
        memory,
        "model_download_manager",
        lambda: SimpleNamespace(status={"step": "idle", "error": "", "attempt": 0}),
    )
    payload = json.loads(asyncio.run(memory.api_memory_embedding_status(SimpleNamespace())).body)
    assert "model file" in payload["setup_warning"].lower()
    assert "reapply" in payload["setup_warning"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["absolute", "tilde", "write_failure", "reconcile_failure", "rollback_conflict"]
)
async def test_apply_rebuilds_inherited_vectors_only_after_config_succeeds(
    migration, monkeypatch, mode
):
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import memory

    config, model, stores = migration
    monkeypatch.setattr(memory, "config_path", lambda: config)
    monkeypatch.setattr(memory, "validated_cached_vector_stores", lambda: tuple(stores[1:]))
    monkeypatch.setattr(emb, "_shared_embedder", None)
    monkeypatch.setattr(emb, "_backend_factory", None)
    monkeypatch.setattr(emb, "_reembed_progress", emb.ReembedProgress())
    monkeypatch.setenv("HOME", str(model.parent))
    monkeypatch.setenv("USERPROFILE", str(model.parent))
    config.write_text(
        json.dumps({"memory": {"embed_model_path": str(model), "embedding_dim": 2}}),
        encoding="utf-8",
    )
    legacy = emb.embedding_space_signature(f"custom:{model.name}:{model.stat().st_size}", 2)
    for store in stores:
        await asyncio.to_thread(store.reconcile_embedding_space, legacy)
    initial = await asyncio.to_thread(emb.default_embedding_backend)
    initial._llm = SimpleNamespace(close=lambda: None)
    emb.install_shared_embedder(initial)
    for store in stores:
        await asyncio.to_thread(emb.align_store_embedding_space, store)
    before = [vectors(store) for store in stores]
    original_config = config.read_text(encoding="utf-8")
    calls = []
    candidates = []

    def candidate(path):
        result = emb.LlamaCppEmbedder(
            model_path=path, dim=2, model_id=emb._custom_model_id(path, ""), serving=False
        )

        def infer(texts):
            calls.extend(texts)
            return {"data": [{"embedding": [1.0, 1.0]} for text in texts]}

        result._llm = SimpleNamespace(create_embedding=infer, close=lambda: None)
        candidates.append(result)
        return result

    monkeypatch.setattr(memory, "build_gated_candidate", candidate)
    if mode == "write_failure":

        async def fail_write(*args, **kwargs):
            raise OSError("configuration write refused")

        monkeypatch.setattr(memory, "run_config_write", fail_write)
    if mode in {"reconcile_failure", "rollback_conflict"}:

        def fail_reconcile(target):
            if mode == "rollback_conflict":
                data = json.loads(config.read_text(encoding="utf-8"))
                data["memory"]["embed_model_id"] = "concurrent-owner-edit"
                config.write_text(json.dumps(data), encoding="utf-8")
            raise OSError("vector index unavailable")

        monkeypatch.setattr(memory, "reconcile_store_embedding_space", fail_reconcile)
    raw = "~/model.gguf" if mode == "tilde" else str(model)
    if mode == "write_failure":
        replacement = model.with_name("new-model.gguf")
        replacement.write_bytes(b"x" * model.stat().st_size)
        raw = str(replacement)
    try:
        await asyncio.to_thread(
            memory._apply_embedding_model, stores[0], raw, asyncio.get_running_loop()
        )
        status = emb.reembed_progress().snapshot()
        if mode == "rollback_conflict":
            assert status["step"] == "failed"
            assert not candidates[0]._serving
            assert (
                json.loads(config.read_text(encoding="utf-8"))["memory"]["embed_model_id"]
                == "concurrent-owner-edit"
            )
        elif mode in {"write_failure", "reconcile_failure"}:
            assert status["step"] == "failed"
            assert [vectors(store) for store in stores] == before
            assert (
                json.loads(config.read_text(encoding="utf-8"))["memory"]
                == json.loads(original_config)["memory"]
            )
            assert emb._shared_embedder is None
            assert calls == []
        else:
            assert status["step"] == "done", status
            assert len(calls) == 2
            assert [len(vectors(store)) for store in stores] == [2, 2]
            assert [vectors(store) for store in stores] != before
            saved = json.loads(config.read_text(encoding="utf-8"))["memory"]
            assert saved["embed_model_path"] == str(model)
            assert not saved.get("embed_model_legacy_ids")
    finally:
        await asyncio.to_thread(emb.reset_shared_embedder)
        for backend in candidates:
            await asyncio.to_thread(backend.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("interleave", ["during_load", "before_locked_write"])
async def test_apply_captures_inheritance_at_config_commit(migration, monkeypatch, interleave):
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import memory

    config, model, stores = migration
    config.write_text(
        json.dumps({"memory": {"embed_model_path": str(model), "embedding_dim": 2}}),
        encoding="utf-8",
    )
    legacy = emb.embedding_space_signature(f"custom:{model.name}:{model.stat().st_size}", 2)
    for store in stores:
        store.reconcile_embedding_space(legacy)
    before = [vectors(store) for store in stores]
    monkeypatch.setattr(memory, "config_path", lambda: config)
    monkeypatch.setattr(memory, "validated_cached_vector_stores", lambda: tuple(stores[1:]))
    monkeypatch.setattr(emb, "_shared_embedder", None)
    monkeypatch.setattr(emb, "_reembed_progress", emb.ReembedProgress())
    calls = []
    migrated = []

    def verify_and_align():
        spec = emb.resolve_custom_model()
        assert spec is not None and not spec.error
        assert json.loads(config.read_text(encoding="utf-8"))["memory"]["embed_model_legacy_ids"]
        for store in stores:
            assert emb.align_store_embedding_space(store) == 0
            assert store.recorded_embedding_space() == emb.embedding_space_signature(
                spec.model_id, 2
            )
        assert [vectors(store) for store in stores] == before
        migrated.append(True)

    def candidate(path):
        backend = emb.LlamaCppEmbedder(
            model_path=path, dim=2, model_id=emb._custom_model_id(path, ""), serving=False
        )

        def infer(texts):
            calls.extend(texts)
            return {"data": [{"embedding": [1.0, 1.0]} for text in texts]}

        def ready(timeout):
            backend._llm = SimpleNamespace(create_embedding=infer, close=lambda: None)
            if interleave == "during_load":
                verify_and_align()
            return True

        monkeypatch.setattr(backend, "wait_ready", ready)
        return backend

    monkeypatch.setattr(memory, "build_gated_candidate", candidate)
    write = memory.run_config_write

    async def interleaved_write(*args, **kwargs):
        if interleave == "before_locked_write":
            await asyncio.to_thread(verify_and_align)
        return await write(*args, **kwargs)

    monkeypatch.setattr(memory, "run_config_write", interleaved_write)
    try:
        await asyncio.to_thread(
            memory._apply_embedding_model, stores[0], str(model), asyncio.get_running_loop()
        )
        assert emb.reembed_progress().snapshot()["step"] == "done"
        assert migrated == [True]
        assert len(calls) == 2
        assert [len(vectors(store)) for store in stores] == [2, 2]
        assert all(vectors(store) != old for store, old in zip(stores, before))
        assert not json.loads(config.read_text(encoding="utf-8"))["memory"].get(
            "embed_model_legacy_ids"
        )
    finally:
        await asyncio.to_thread(emb.reset_shared_embedder)


@pytest.mark.parametrize(
    "labels,expected",
    [
        (None, False),
        ([], False),
        ("false", False),
        (17, False),
        (["legacy", 17], False),
        (["legacy"], True),
    ],
)
@pytest.mark.parametrize("surface", ["status", "alignment"])
def test_legacy_identity_status_and_alignment_validate_labels(
    migration, monkeypatch, labels, expected, surface
):
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import memory

    config, model, stores = migration
    identity = emb._custom_model_id(model, "")
    config.write_text(
        json.dumps(
            {
                "memory": {
                    "embed_model_path": str(model),
                    "embedding_dim": 2,
                    "embed_model_id": identity,
                    "embed_model_stamp": list(emb._model_file_stamp(model)),
                    "embed_model_legacy_ids": labels,
                }
            }
        ),
        encoding="utf-8",
    )
    for store in stores:
        store.reconcile_embedding_space(emb.embedding_space_signature("legacy", 2))
    activate(monkeypatch)
    monkeypatch.setattr(
        memory,
        "model_download_manager",
        lambda: SimpleNamespace(status={"step": "idle", "error": "", "attempt": 0}),
    )
    if surface == "status":
        payload = json.loads(
            asyncio.run(memory.api_memory_embedding_status(SimpleNamespace())).body
        )
        assert bool(payload["setup_warning"]) is expected
    else:
        for store in stores:
            assert emb.align_store_embedding_space(store) == (0 if expected else 2)
            assert len(vectors(store)) == (2 if expected else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labels,expected",
    [
        (None, False),
        ([], False),
        ("false", False),
        (17, False),
        (["legacy", 17], False),
        (["legacy"], True),
    ],
)
async def test_model_config_write_returns_validated_prior_inheritance(
    migration, monkeypatch, labels, expected
):
    from kiro_crew.dashboard.handlers import memory

    config, model, _stores = migration
    config.write_text(json.dumps({"memory": {"embed_model_legacy_ids": labels}}), encoding="utf-8")
    monkeypatch.setattr(memory, "config_path", lambda: config)
    rollback, inherited = await memory._write_embed_model_config(str(model), 2)
    assert inherited is expected
    assert "embed_model_legacy_ids" not in json.loads(config.read_text(encoding="utf-8"))["memory"]
    await rollback()
    assert (
        json.loads(config.read_text(encoding="utf-8"))["memory"]["embed_model_legacy_ids"] == labels
    )

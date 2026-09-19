"""Explicit rebuild intent survives closed stores and interrupted applications."""

import asyncio
import json
import struct
from types import SimpleNamespace

import pytest
from member_memory_helpers import write_member_home

from kiro_crew import context
from kiro_crew import embeddings as emb
from kiro_crew.config import loader
from kiro_crew.dashboard.handlers import memory
from kiro_crew.vector_memory import VectorMemoryStore, open_member_database


@pytest.fixture
def rebuild_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv(emb._MODEL_PATH_ENV, raising=False)
    config = write_member_home(tmp_path, "late")
    config["memory_stores"]["legacy"] = {}
    model = tmp_path / "model.gguf"
    with model.open("wb") as stream:
        stream.write(b"weights A")
        stream.truncate(emb._GGUF_MIN_BYTES + 1)
    config["memory"] = {"embed_model_path": str(model), "embedding_dim": 2}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    monkeypatch.setattr(emb, "_shared_embedder", None)
    monkeypatch.setattr(emb, "_backend_factory", None)
    monkeypatch.setattr(emb, "_reembed_progress", emb.ReembedProgress())
    monkeypatch.setattr(context, "_vector_stores", {})
    handles = []

    def open_store(path):
        if path.parent.name == "member-late":
            store = open_member_database(
                path, member_id="late", store_id="member-late", embedding_dim=2
            )
        else:
            store = VectorMemoryStore(db_path=path, embedding_dim=2)
            store.init()
        store.embed_fn = emb.make_sync_embed_fn()
        handles.append(store)
        return store

    global_store = open_store(tmp_path / "memory.db")
    legacy_path = tmp_path / "memory_stores" / "legacy" / "memory.db"
    legacy_path.parent.mkdir()
    # Copy an empty V1 database so the named legacy store keeps the V1 lineage.
    from kiro_crew._sqlite_compat import sqlite3

    with sqlite3.connect(str(legacy_path)) as destination:
        global_store.db.backup(destination)
    legacy = open_store(legacy_path)
    private = open_store(tmp_path / "memory_stores" / "member-late" / "memory.db")
    old_sig = emb.embedding_space_signature(f"custom:{model.name}:{model.stat().st_size}", 2)
    for store in (global_store, legacy, private):
        store.embed_fn = None
        store.write_episodic("deployment incident retained for this store", defer_embedding=True)
        store.set_semantic("project.status", "active", 1, "user_explicit")
        store.db.execute(
            f"UPDATE {store._epi_rel} SET embedding = ? WHERE 1 = 1{store._epi_guard}",
            (struct.pack("2f", 1, 0),),
        )
        store.db.execute(
            f"UPDATE {store._sem_rel} SET embedding = ? WHERE 1 = 1{store._sem_guard}",
            (struct.pack("2f", 1, 0),),
        )
        store.db.commit()
        store.reconcile_embedding_space(old_sig)
    with model.open("r+b") as stream:
        stream.write(b"weights B")

    def candidate(path):
        backend = emb.LlamaCppEmbedder(
            model_path=path, dim=2, model_id=emb._custom_model_id(path, ""), serving=False
        )
        backend._llm = SimpleNamespace(
            create_embedding=lambda texts: {"data": [{"embedding": [0.0, 1.0]} for _ in texts]},
            close=lambda: None,
        )
        return backend

    migrated = emb.default_embedding_backend()
    migrated._llm = SimpleNamespace(close=lambda: None)
    emb.install_shared_embedder(migrated)
    for store in (global_store, legacy, private):
        emb.align_store_embedding_space(store)
        store.embed_fn = emb.make_sync_embed_fn()
    monkeypatch.setattr(memory, "build_gated_candidate", candidate)
    try:
        yield SimpleNamespace(
            root=tmp_path,
            config=config_path,
            model=model,
            global_store=global_store,
            late=(legacy, private),
            open_store=open_store,
        )
    finally:
        emb.reset_shared_embedder()
        for store in handles:
            store.close()
        loader._invalidate_config_cache()


def vector_blobs(store):
    with store._db_lock:
        return [
            row[0]
            for relation in ("episodic_memories", "semantic_memory")
            for row in store.db.execute(f"SELECT embedding FROM {relation}")
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("labels_present", [True, False])
async def test_reapply_repairs_closed_already_restamped_databases(rebuild_home, labels_present):
    home = rebuild_home
    if not labels_present:

        def remove_labels(data):
            data["memory"].pop("embed_model_legacy_ids", None)
            return data

        loader.update_config_locked(home.config, mutate=remove_labels)
    before = await asyncio.to_thread(vector_blobs, home.late[0])
    for store in home.late:
        await asyncio.to_thread(store.close)
    await asyncio.wait_for(
        asyncio.to_thread(
            memory._apply_embedding_model,
            home.global_store,
            str(home.model),
            asyncio.get_running_loop(),
        ),
        10,
    )
    assert emb.reembed_progress().snapshot()["step"] == "done"
    assert await asyncio.to_thread(vector_blobs, home.global_store) == [struct.pack("2f", 0, 1)] * 2
    for original in home.late:
        reopened = await asyncio.to_thread(home.open_store, original._db_path)
        await asyncio.to_thread(emb.align_store_embedding_space, reopened)
        assert await asyncio.to_thread(vector_blobs, reopened) == [None, None]
        await asyncio.to_thread(reopened.backfill_missing_embeddings, pace=False)
        rebuilt = await asyncio.to_thread(vector_blobs, reopened)
        assert rebuilt != before
        assert all(blob == struct.pack("2f", 0, 1) for blob in rebuilt)
        await asyncio.to_thread(reopened.close)
        again = await asyncio.to_thread(home.open_store, original._db_path)
        assert await asyncio.to_thread(emb.align_store_embedding_space, again) == 0
        assert await asyncio.to_thread(vector_blobs, again) == rebuilt


@pytest.mark.parametrize("private", [False, True])
def test_episode_backfill_does_not_attach_old_body_vector(rebuild_home, monkeypatch, private):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from kiro_crew import memory_edit

    store = rebuild_home.late[1] if private else rebuild_home.global_store
    store.reconcile_embedding_space(store.recorded_embedding_space(), force=True)
    entered, release = Event(), Event()
    old_text = "deployment incident retained for this store"
    new_text = "database backups retained for this store"

    def embed(text, **kwargs):
        if text == old_text:
            entered.set()
            assert release.wait(5)
        return [1.0, 0.0] if text == old_text else [0.0, 1.0]

    monkeypatch.setattr(store, "_embed_bulk_row", embed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.backfill_missing_embeddings, pace=False)
        try:
            assert entered.wait(5)
            preview = memory_edit.preview_edit(
                store,
                "chosen",
                b"test-key",
                {
                    "selection": {"query": {"q": old_text}},
                    "operation": {
                        "type": "replace_text",
                        "find": old_text,
                        "replacement": new_text,
                    },
                },
            )
            assert (
                memory_edit.apply_edit(store, "chosen", b"test-key", preview["preview_id"])[
                    "changed_count"
                ]
                == 1
            )
        finally:
            release.set()
        future.result(timeout=5)
    assert store.get_episodic_list()[0]["text"] == new_text
    assert store.db.execute("SELECT embedding FROM episodic_memories").fetchone()[0] is None
    store.backfill_missing_embeddings(pace=False)
    assert store.db.execute("SELECT embedding FROM episodic_memories").fetchone()[0] == struct.pack(
        "2f", 0, 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [0, 1, 2])
async def test_committed_request_recovers_after_partial_restart(rebuild_home, completed):
    home = rebuild_home
    _, _ = await memory._write_embed_model_config(str(home.model), 2)
    generation = emb.embedding_rebuild_generation()
    stores = [home.global_store, *home.late]
    for store in stores[:completed]:
        await asyncio.to_thread(emb.align_store_embedding_space, store)
    for store in stores:
        await asyncio.to_thread(store.close)
    await asyncio.to_thread(emb.reset_shared_embedder)
    backend = await asyncio.to_thread(memory.build_gated_candidate, home.model)
    backend.activate()
    emb.install_shared_embedder(backend)
    for original in stores:
        reopened = await asyncio.to_thread(home.open_store, original._db_path)
        await asyncio.to_thread(emb.align_store_embedding_space, reopened)
        assert reopened.recorded_rebuild_generation() == generation
        assert await asyncio.to_thread(vector_blobs, reopened) == [None, None]
        await asyncio.to_thread(reopened.backfill_missing_embeddings, pace=False)
        before = await asyncio.to_thread(vector_blobs, reopened)
        assert all(before)
        assert await asyncio.to_thread(emb.align_store_embedding_space, reopened) == 0
        assert await asyncio.to_thread(vector_blobs, reopened) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["index", "metadata", "commit"])
async def test_acknowledgment_cannot_outlive_failed_invalidation(
    rebuild_home, monkeypatch, failure
):
    from pathlib import Path

    home = rebuild_home
    store = home.late[1]
    await memory._write_embed_model_config(str(home.model), 2)
    generation = emb.embedding_rebuild_generation()
    before = await asyncio.to_thread(vector_blobs, store)
    with monkeypatch.context() as patch:
        if failure == "index":
            unlink = Path.unlink

            def blocked(path, *args, **kwargs):
                if path == store._faiss_path:
                    raise PermissionError("index locked")
                return unlink(path, *args, **kwargs)

            patch.setattr(Path, "unlink", blocked)
            await asyncio.to_thread(emb.align_store_embedding_space, store)
        elif failure == "metadata":
            write = store._write_meta_in_transaction

            def fail_write(key, value):
                if key == "embedding_rebuild_generation":
                    raise OSError("metadata unavailable")
                write(key, value)

            patch.setattr(store, "_write_meta_in_transaction", fail_write)
            with pytest.raises(OSError):
                await asyncio.to_thread(emb.align_store_embedding_space, store)
            assert await asyncio.to_thread(vector_blobs, store) == before
        else:
            connection = store._db

            class FailCommit:
                def __getattr__(self, name):
                    return getattr(connection, name)

                def commit(self):
                    raise OSError("commit unavailable")

            patch.setattr(store, "_db", FailCommit())
            with pytest.raises(OSError):
                await asyncio.to_thread(emb.align_store_embedding_space, store)
            assert await asyncio.to_thread(vector_blobs, store) == before
        assert store.recorded_rebuild_generation() != generation
        assert emb.store_embedding_space_is_stale(store)
        assert await asyncio.to_thread(store._try_embed, "deployment incident") is None
    await asyncio.to_thread(emb.align_store_embedding_space, store)
    assert store.recorded_rebuild_generation() == generation
    assert await asyncio.to_thread(vector_blobs, store) == [None, None]


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", ["unrelated", "model", "request"])
async def test_rollback_preserves_repair_and_concurrent_settings(rebuild_home, concurrent):
    home = rebuild_home
    previous = json.loads(home.config.read_text(encoding="utf-8"))["memory"]
    rollback, _ = await memory._write_embed_model_config(str(home.model), 2)
    generation = emb.embedding_rebuild_generation()

    def edit(data):
        data["unrelated"] = {"kept": True}
        if concurrent == "model":
            data["memory"]["embed_model_id"] = "another-model"
        elif concurrent == "request":
            data["memory"]["embed_rebuild_generation"] = "another-request"
        return data

    loader.update_config_locked(home.config, mutate=edit)
    if concurrent == "unrelated":
        await rollback()
    else:
        with pytest.raises(ValueError, match="rollback refused"):
            await rollback()
    saved = json.loads(home.config.read_text(encoding="utf-8"))
    assert saved["unrelated"] == {"kept": True}
    if concurrent == "unrelated":
        assert saved["memory"].pop("embed_rebuild_generation") == generation
        assert saved["memory"] == previous
    elif concurrent == "model":
        assert saved["memory"]["embed_model_id"] == "another-model"
    else:
        assert saved["memory"]["embed_rebuild_generation"] == "another-request"


@pytest.mark.asyncio
async def test_status_reports_closed_scope_without_claiming_repair_done(rebuild_home, monkeypatch):
    home = rebuild_home
    await memory._write_embed_model_config(str(home.model), 2)
    monkeypatch.setattr(
        memory,
        "model_download_manager",
        lambda: SimpleNamespace(status={"step": "idle", "error": "", "attempt": 0}),
    )
    state = SimpleNamespace(
        context_builder=SimpleNamespace(memory=SimpleNamespace(vector_store=home.global_store))
    )
    response = await memory.api_memory_embedding_status(SimpleNamespace(app={"state": state}))
    result = json.loads(response.body)
    assert result["repair"]["scope"] == "open_stores"
    assert result["repair"]["pending_invalidation"] == 1
    assert result["repair"]["deferred_stores"] == 2
    assert result["reembed"]["step"] == "deferred"
    assert result["setup_warning_code"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,code",
    [
        ("missing", "model_path_not_found"),
        ("relative", "model_path_not_absolute"),
        ("directory", "model_path_not_a_file"),
        ("small", "model_path_too_small"),
        ("legacy", ""),
    ],
)
async def test_status_has_stable_error_and_warning_codes(rebuild_home, monkeypatch, case, code):
    home = rebuild_home

    def change(data):
        if case == "missing":
            data["memory"]["embed_model_path"] = str(home.root / "missing.gguf")
        elif case == "relative":
            data["memory"]["embed_model_path"] = "relative.gguf"
        elif case == "directory":
            data["memory"]["embed_model_path"] = str(home.root)
        elif case == "small":
            small = home.root / "small.gguf"
            small.write_bytes(b"small")
            data["memory"]["embed_model_path"] = str(small)
        return data

    loader.update_config_locked(home.config, mutate=change)
    monkeypatch.setattr(
        memory,
        "model_download_manager",
        lambda: SimpleNamespace(status={"step": "idle", "error": "", "attempt": 0}),
    )
    result = json.loads((await memory.api_memory_embedding_status(SimpleNamespace())).body)
    assert result["setup_error_code"] == code
    assert result["setup_warning_code"] == "legacy_embedding_vectors"
    assert result["setup_warning"]
    if code:
        assert result["setup_error"]
        assert result["setup_error_params"]["path"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["legacy", "member-late"])
async def test_store_opened_after_final_snapshot_requires_current_vectors(
    rebuild_home, monkeypatch, name
):
    import threading

    home = rebuild_home
    entered, release = threading.Event(), threading.Event()
    align = memory.reconcile_store_embedding_space
    original = home.late[1] if name == "member-late" else home.late[0]
    old_vectors = await asyncio.to_thread(vector_blobs, original)
    old_generation = original.recorded_rebuild_generation()
    for store in home.late:
        await asyncio.to_thread(store.close)

    def pause(target):
        entered.set()
        assert release.wait(5)
        return align(target)

    monkeypatch.setattr(memory, "reconcile_store_embedding_space", pause)
    task = asyncio.create_task(
        asyncio.to_thread(
            memory._apply_embedding_model,
            home.global_store,
            str(home.model),
            asyncio.get_running_loop(),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        generation = emb.embedding_rebuild_generation()
        assert generation
        late = await asyncio.wait_for(context.ContextBuilder.ensure_store(name), 5)
        if name == "member-late":
            # Read-time V2 preparation must not acknowledge a rebuild or mutate
            # old vectors; they remain unusable until explicit maintenance.
            assert late.recorded_rebuild_generation() == old_generation
            assert await asyncio.to_thread(vector_blobs, late) == old_vectors
            assert late.db.total_changes == 0
        else:
            assert late.recorded_rebuild_generation() == generation
            assert await asyncio.to_thread(vector_blobs, late) == [None, None]
    finally:
        release.set()
        await asyncio.wait_for(task, 10)
    if name == "member-late":
        assert await asyncio.to_thread(late._try_embed, "pending rebuild") is None
        await asyncio.to_thread(emb.align_store_embedding_space, late)
        assert late.recorded_rebuild_generation() == generation
        assert await asyncio.to_thread(vector_blobs, late) == [None, None]
    await asyncio.to_thread(late.backfill_missing_embeddings, pace=False)
    before = await asyncio.to_thread(vector_blobs, late)
    assert all(before)
    assert await context.ContextBuilder.ensure_store(name) is late
    assert await asyncio.to_thread(vector_blobs, late) == before
    await asyncio.to_thread(late.close)


@pytest.mark.asyncio
async def test_request_field_round_trips_and_reapply_mints_new_request(rebuild_home):
    home = rebuild_home
    assert not loader.KiroCrewConfig.load().memory.embed_rebuild_generation
    await memory._write_embed_model_config(str(home.model), 2)
    first = emb.embedding_rebuild_generation()
    config = loader.KiroCrewConfig.load()
    assert config.memory.embed_rebuild_generation == first
    await asyncio.to_thread(config.save)
    assert loader.KiroCrewConfig.load().memory.embed_rebuild_generation == first
    await memory._write_embed_model_config(str(home.model), 2)
    assert emb.embedding_rebuild_generation() != first


@pytest.mark.asyncio
async def test_lazy_embed_binding_cannot_bypass_pending_request(rebuild_home):
    home = rebuild_home
    backend = await asyncio.to_thread(memory.build_gated_candidate, home.model)
    backend.activate()
    emb.install_shared_embedder(backend)
    store = home.global_store
    store.embed_fn = None
    store.embed_fn_factory = emb.make_sync_embed_fn
    await memory._write_embed_model_config(str(home.model), 2)
    before = await asyncio.to_thread(vector_blobs, store)
    assert await asyncio.to_thread(store._try_embed, "deployment incident") is None
    assert await asyncio.to_thread(vector_blobs, store) == before
    assert emb.store_embedding_space_is_stale(store)


def test_store_progress_adapter_offsets_and_folds_phase_resets():
    """One store's (done, total) stream maps onto the multi-store bar monotonically."""
    prog = emb.ReembedProgress()
    prog.begin_run(10)
    report = memory._store_progress_adapter(prog, 4, 10)
    # Lesson phase: its own denominator, counted on top of the 4 earlier rows.
    report(0, 2)
    assert prog.snapshot()["done"] == 4
    report(2, 2)
    assert prog.snapshot()["done"] == 6
    # Episodic phase restarts at 0 — the bar must not jump back to 4.
    report(0, 3)
    assert prog.snapshot()["done"] == 6
    report(3, 3)
    assert prog.snapshot() == {"step": "running", "done": 9, "total": 10, "error": ""}
    # The multi-store total never falls below what has been counted.
    report(5, 5)
    assert prog.snapshot()["total"] == 11


@pytest.mark.asyncio
async def test_apply_reports_per_batch_progress_within_a_single_store(rebuild_home, monkeypatch):
    """Regression: the progress bar froze at 0/total until a whole store finished."""
    home = rebuild_home
    for store in home.late:
        await asyncio.to_thread(store.close)
    seen: list[tuple[int, int, str]] = []
    original = VectorMemoryStore.backfill_missing_embeddings

    def observed(self, progress=None, **kwargs):
        assert progress is not None, "apply must pass a per-store progress callback"

        def spy(done, total):
            progress(done, total)
            snap = emb.reembed_progress().snapshot()
            seen.append((int(snap["done"]), int(snap["total"]), str(snap["step"])))

        return original(self, progress=spy, **kwargs)

    monkeypatch.setattr(VectorMemoryStore, "backfill_missing_embeddings", observed)
    await asyncio.wait_for(
        asyncio.to_thread(
            memory._apply_embedding_model,
            home.global_store,
            str(home.model),
            asyncio.get_running_loop(),
        ),
        10,
    )
    final = emb.reembed_progress().snapshot()
    assert final["step"] == "done"
    # The single open store had two cleared vectors (one episode, one semantic row).
    assert final["done"] == 2 and final["total"] == 2
    # Progress advanced while the store was still inside its sweep, not only after.
    assert seen, "no per-batch progress was reported"
    assert any(done > 0 and step == "running" for done, _total, step in seen)
    assert all(step == "running" for _done, _total, step in seen)
    # Monotonic: the bar never moved backward within the sweep.
    dones = [done for done, _total, _step in seen]
    assert dones == sorted(dones)
    assert all(total >= 2 for _done, total, _step in seen)

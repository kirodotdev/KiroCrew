"""Regression tests for memory runtime identity, alignment and admission."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from kiro_crew import embeddings as emb
from kiro_crew import executors as pools
from kiro_crew.vector_memory import VectorMemoryStore


def test_same_name_size_weights_have_distinct_identity(tmp_path):
    first = tmp_path / "a" / "model.gguf"
    second = tmp_path / "b" / "model.gguf"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"abc")
    second.write_bytes(b"xyz")
    assert emb._custom_model_id(first, "") != emb._custom_model_id(second, "")
    assert emb._custom_model_id(first, "named") != emb._custom_model_id(second, "named")


def test_changed_weights_recompute_identity(tmp_path):
    path = tmp_path / "model.gguf"
    path.write_bytes(b"abc")
    before = emb._custom_model_id(path, "")
    replacement = tmp_path / "replacement.gguf"
    replacement.write_bytes(b"xyz")
    replacement.replace(path)
    assert before != emb._custom_model_id(path, "")


def test_late_store_aligns_width_even_when_signature_matches(tmp_path, monkeypatch):
    backend = SimpleNamespace(model_id="candidate", dim=3, is_ready=lambda: True)
    monkeypatch.setattr(emb, "get_shared_embedder", lambda: backend)
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    store.init()
    try:
        store.reconcile_embedding_space(emb.embedding_space_signature("candidate", 3))
        emb.reconcile_store_embedding_space(store)
        assert store._embedding_dim == 3
    finally:
        store.close()


def test_invalid_ready_width_does_not_change_store(tmp_path, monkeypatch):
    backend = SimpleNamespace(model_id="candidate", dim=0, is_ready=lambda: True)
    monkeypatch.setattr(emb, "get_shared_embedder", lambda: backend)
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    store.init()
    try:
        old = store.recorded_embedding_space()
        emb.reconcile_store_embedding_space(store)
        assert store.recorded_embedding_space() == old
        assert store._embedding_dim == 2
    finally:
        store.close()


def test_cache_collision_does_not_block_other_text(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def embed(text, **kwargs):
        if text == "bulk":
            entered.set()
            assert release.wait(5)
        return [1.0, 0.0]

    backend = SimpleNamespace(model_id="test", embed=embed)
    monkeypatch.setattr(emb, "get_shared_embedder", lambda: backend)
    monkeypatch.setattr(emb, "_sync_embed_stripes", (threading.Lock(),))
    with ThreadPoolExecutor(max_workers=2) as pool:
        bulk = pool.submit(emb._shared_sync_embed, "bulk", priority=emb.PRIORITY_BULK)
        try:
            assert entered.wait(5)
            interactive = pool.submit(emb._shared_sync_embed, "other", priority=0)
            assert interactive.result(timeout=1) == [1.0, 0.0]
        finally:
            release.set()
            bulk.result(timeout=5)


@pytest.mark.asyncio
async def test_recall_has_own_admission_and_cancels_queued_native_work(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Native:
        def create_embedding(self, texts):
            calls.extend(texts)
            if texts == ["first"]:
                entered.set()
                assert release.wait(5)
            return {"data": [{"embedding": [1.0, 0.0]} for _ in texts]}

    backend = emb.LlamaCppEmbedder(model_path=tmp_path / "model.gguf", dim=2)
    backend._llm = Native()
    monkeypatch.setattr(emb, "get_shared_embedder", lambda: backend)
    monkeypatch.setattr(emb, "_MAX_PENDING_EMBEDS", 16)

    def runner(func, *args, **kwargs):
        return pools.run_with_recall_deadline(pools.run_in_embed_pool(func, *args, **kwargs))

    tasks = [asyncio.create_task(runner(emb._shared_sync_embed, "first", priority=0))]
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        tasks += [
            asyncio.create_task(runner(emb._shared_sync_embed, str(i), priority=0))
            for i in range(7)
        ]

        async def queued():
            while backend._jobs.qsize() != 7:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(queued(), 2)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert await asyncio.wait_for(pools.run_in_embed_pool(lambda: "prompt"), 1) == "prompt"
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(backend.close)
    assert calls == ["first"]


def test_expired_native_job_never_runs(tmp_path):
    budget_type = getattr(emb, "EmbeddingWork", None)
    assert budget_type is not None, "native requests need a monotonic work budget"
    work = budget_type(deadline=time.monotonic() - 1)
    backend = emb.LlamaCppEmbedder(model_path=tmp_path / "model.gguf", dim=2)
    backend._llm = SimpleNamespace(create_embedding=lambda texts: pytest.fail("expired job ran"))
    previous = emb.embedding_work.get()
    emb.embedding_work.set(work)
    try:
        assert backend.embed("expired") is None
    finally:
        emb.embedding_work.set(previous)
        backend.close()


@pytest.mark.asyncio
async def test_same_key_bulk_is_promoted_without_duplicate_inference(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Native:
        def create_embedding(self, texts):
            calls.extend(texts)
            if texts == ["blocker"]:
                entered.set()
                assert release.wait(5)
            return {"data": [{"embedding": [1.0, 0.0]} for _ in texts]}

    backend = emb.LlamaCppEmbedder(model_path=tmp_path / "model.gguf", dim=2)
    backend._llm = Native()
    monkeypatch.setattr(emb, "get_shared_embedder", lambda: backend)
    tasks = [asyncio.create_task(asyncio.to_thread(emb._shared_sync_embed, "blocker"))]

    async def wait_until(predicate):
        while not predicate():
            await asyncio.sleep(0.001)

    try:
        assert await asyncio.to_thread(entered.wait, 2)
        tasks.append(
            asyncio.create_task(
                asyncio.to_thread(emb._shared_sync_embed, "same", priority=emb.PRIORITY_BULK)
            )
        )
        await asyncio.wait_for(wait_until(lambda: backend._jobs.qsize() == 1), 2)
        backend._bulk_ready_at = time.monotonic() + 60
        tasks.append(
            asyncio.create_task(
                asyncio.to_thread(emb._shared_sync_embed, "same", priority=emb.PRIORITY_INTERACTIVE)
            )
        )

        def promoted():
            with backend._jobs.mutex:
                return bool(backend._jobs.queue and backend._jobs.queue[0][0] == 0)

        await asyncio.wait_for(wait_until(promoted), 2)
        release.set()
        assert await asyncio.wait_for(asyncio.gather(*tasks), 2) == [[1.0, 0.0]] * 3
        assert calls == ["blocker", "same"]
    finally:
        release.set()
        await asyncio.to_thread(backend.close)
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_server_deadline_retains_running_worker_admission(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(pools, "RECALL_TIMEOUT_SECS", 0.1)

    def blocked():
        entered.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()

    task = asyncio.create_task(pools.run_with_recall_deadline(pools.run_in_embed_pool(blocked)))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, 2)
        admission = asyncio.get_running_loop()._kirocrew_recall_admission
        assert admission._value == pools._MAX_EMBED_WORKERS - 1
        assert await pools.run_in_embed_pool(lambda: "prompt") == "prompt"
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        await asyncio.gather(task, return_exceptions=True)


def test_model_digest_cached_until_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "model.gguf"
    path.write_bytes(b"abc")
    digest = emb._sha256_file
    calls = []

    def counted(model):
        calls.append(model)
        return digest(model)

    monkeypatch.setattr(emb, "_sha256_file", counted)
    first = emb._custom_model_id(path, "")
    assert emb._custom_model_id(path, first) == first
    assert calls == [path]


@pytest.mark.asyncio
@pytest.mark.parametrize("cold_open", [False, True])
async def test_http_deadline_wraps_real_recall_handler(monkeypatch, cold_open):
    from kiro_crew.dashboard.handlers import memory, memory_member

    release, entered, finished = threading.Event(), threading.Event(), threading.Event()
    seen = []
    monkeypatch.setattr(pools, "RECALL_TIMEOUT_SECS", 0.1)

    async def resolve(*args):
        return "default", None

    def recall(*args, **kwargs):
        seen.append(emb.embedding_work.get())
        entered.set()
        try:
            assert release.wait(5)
            return {}
        finally:
            finished.set()

    async def tier(*args):
        if cold_open:
            seen.append(emb.embedding_work.get())
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        return SimpleNamespace(recall=recall)

    monkeypatch.setattr(memory_member, "resolve_requested_memory_store", resolve)
    monkeypatch.setattr(memory_member, "vector_memory_for_store", tier)
    monkeypatch.setattr(memory_member, "requesting_slot_project", lambda *args: None)
    request = SimpleNamespace(
        app={"state": object()}, query={"q": "query", "store": "default"}, headers={}
    )
    try:
        response = await asyncio.wait_for(
            memory.memory_recall_deadline(memory_member.api_memory_recall)(request), 2
        )
        assert response.status == 504
        assert b"memory_recall_timeout" in response.body
        assert entered.is_set()
        assert seen[0].cancelled.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)


def test_embedding_signature_preserves_existing_encoding():
    import hashlib

    assert emb.embedding_space_signature("model", 2) == hashlib.sha256(b"model|2").hexdigest()[:16]
    assert (
        emb.default_embedding_space_signature()
        == hashlib.sha256(f"{emb._MODEL_ID}|{emb._DEFAULT_DIM}".encode()).hexdigest()[:16]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_ensure_store_aligns_off_loop_outside_cache_lock(tmp_path, monkeypatch, cached):
    from kiro_crew import context

    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    await asyncio.to_thread(store.init)
    monkeypatch.setattr(context, "_resolved_store_name", lambda name: name)
    monkeypatch.setattr(context, "_vector_stores", {"named": store} if cached else {})
    calls = []
    loop_thread = threading.get_ident()

    async def build(name):
        context._vector_stores[name] = store
        return store

    def align(target):
        assert threading.get_ident() != loop_thread
        assert context._stores_lock.acquire(blocking=False)
        context._stores_lock.release()
        calls.append(target)

    monkeypatch.setattr(context, "_build_store_vectors", build)
    monkeypatch.setattr(emb, "align_store_embedding_space", align)
    try:
        assert await context.ContextBuilder.ensure_store("named") is store
        assert calls == [store]
    finally:
        await asyncio.to_thread(store.close)


@pytest.mark.asyncio
async def test_store_opened_after_apply_snapshot_before_config_gets_new_width(
    tmp_path, monkeypatch
):
    import json

    from member_memory_helpers import write_member_home

    from kiro_crew import context
    from kiro_crew.config import loader
    from kiro_crew.dashboard.handlers import memory

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = write_member_home(tmp_path, "late")
    config["memory"] = {"embedding_dim": 2}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    monkeypatch.setattr(context, "_vector_stores", {})
    monkeypatch.setattr(emb, "_shared_embedder", None)
    monkeypatch.setattr(emb, "_reembed_progress", emb.ReembedProgress())
    candidate = emb.LlamaCppEmbedder(model_path=tmp_path / "candidate.gguf", dim=3)
    candidate._llm = SimpleNamespace(close=lambda: None)
    candidate._serving = False
    monkeypatch.setattr(memory, "build_gated_bundled", lambda: candidate)
    global_store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    late_path = tmp_path / "memory_stores" / "member-late" / "memory.db"

    def initialize():
        global_store.init()
        late = VectorMemoryStore(db_path=late_path, embedding_dim=2)
        late.init()
        late.close()

    await asyncio.to_thread(initialize)
    write_config = memory._write_embed_model_config
    opened = []

    async def open_before_write(path, dim):
        assert loader.KiroCrewConfig.load().memory.embedding_dim == 2
        assert not candidate._serving
        late = await context.ContextBuilder.ensure_store("member-late")
        opened.append(late)
        assert late._embedding_dim == 2
        return await write_config(path, dim)

    monkeypatch.setattr(memory, "_write_embed_model_config", open_before_write)
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                memory._apply_embedding_model, global_store, "", asyncio.get_running_loop()
            ),
            10,
        )
        assert opened, memory.reembed_progress().snapshot()
        assert candidate._serving
        assert loader.KiroCrewConfig.load().memory.embedding_dim == 3
        assert opened[0]._embedding_dim == 3
        assert await asyncio.to_thread(
            opened[0].recorded_embedding_space
        ) == emb.embedding_space_signature(candidate.model_id, 3)
    finally:
        for store in [global_store, *context._vector_stores.values()]:
            await asyncio.to_thread(store.close)
        await asyncio.to_thread(candidate.close)
        loader._invalidate_config_cache()


@pytest.mark.asyncio
async def test_registered_recall_route_has_server_deadline(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import memory_member
    from kiro_crew.dashboard.routes import memory as routes

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def resolve(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(pools, "RECALL_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(memory_member, "resolve_requested_memory_store", resolve)
    app = web.Application()
    app["state"] = object()
    routes.register(app)
    route = next(
        r
        for r in app.router.routes()
        if r.method == "GET" and r.resource.canonical == "/api/memory/recall"
    )
    assert route.handler is memory_member.api_memory_recall
    assert hasattr(route.handler, "__wrapped__")
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        response = await asyncio.wait_for(client.get("/api/memory/recall?store=default&q=query"), 2)
        assert response.status == 504
        assert (await response.json())["code"] == "memory_recall_timeout"
    assert entered.is_set() and cancelled.is_set()


def test_same_width_alignment_rejects_inflight_old_vector(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    old = SimpleNamespace(
        model_id="old-space", dim=2, is_ready=lambda: True, embed=lambda text, **kwargs: [1.0, 0.0]
    )
    new = SimpleNamespace(model_id="new-space", dim=2, is_ready=lambda: True)
    active = [old]
    monkeypatch.setattr(emb, "get_shared_embedder", lambda: active[0])
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    store.init()
    store.embed_fn = emb.make_sync_embed_fn()
    emb.align_store_embedding_space(store)
    embed = store._try_embed

    def pause_before_commit(*args, **kwargs):
        vector = embed(*args, **kwargs)
        assert vector == [1.0, 0.0]
        entered.set()
        assert release.wait(5)
        return vector

    monkeypatch.setattr(store, "_try_embed", pause_before_commit)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(store.write_episodic, "a deployment incident worth retaining")
            try:
                assert entered.wait(5)
                active[0] = new
                emb.align_store_embedding_space(store)
            finally:
                release.set()
            assert writer.result(timeout=5)
        assert store.recorded_embedding_space() == emb.embedding_space_signature(new.model_id, 2)
        row = store.db.execute("SELECT embedding FROM episodic_memories").fetchone()
        assert row[0] is None
        assert store.has_pending_embeddings()
    finally:
        release.set()
        store.close()

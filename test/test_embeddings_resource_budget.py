"""The shared inference boundary bounds work across concurrent memory stores."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from kiro_crew import embeddings as emb


@pytest.mark.parametrize(
    "settings,configured,effective",
    [
        ({}, (4, 1), (4, 1)),
        ({"embedding_threads": 4, "embedding_bulk_threads": 6}, (4, 6), (4, 6)),
        ({"embedding_threads": 999, "embedding_bulk_threads": 999}, (256, 256), (16, 16)),
    ],
)
def test_loaded_saved_config_preserves_explicit_threads_and_core_limit(
    tmp_path, monkeypatch, settings, configured, effective
):
    from kiro_crew.config import loader

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"memory": settings}), encoding="utf-8")
    monkeypatch.setattr(loader, "config_path", lambda: path)
    monkeypatch.setattr(emb, "config_path", lambda: path)
    monkeypatch.setattr(emb.os, "cpu_count", lambda: 16)
    loader._invalidate_config_cache()
    before = (emb._embed_threads(), emb.bulk_embed_threads())
    cfg = loader.KiroCrewConfig.load()
    assert (cfg.memory.embedding_threads, cfg.memory.embedding_bulk_threads) == configured
    cfg.save()
    assert (emb._embed_threads(), emb.bulk_embed_threads()) == before
    assert before == effective


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setattr(emb, "_read_memory_config", lambda: {})
    instance = emb.LlamaCppEmbedder(model_path=tmp_path / "unused.gguf", dim=1)
    try:
        yield instance
    finally:
        instance.close()


def test_one_cache_coalesces_parallel_stores_and_backend_replacement(monkeypatch):
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def embed(text, **kwargs):
        calls.append(text)
        entered.set()
        assert release.wait(5)
        return [1.0]

    original = SimpleNamespace(model_id="same-space", embed=embed)
    monkeypatch.setattr(emb, "get_shared_embedder", lambda: original)
    functions = [emb.make_sync_embed_fn() for _ in range(8)]
    assert all(fn is functions[0] for fn in functions)
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(fn, "shared question") for fn in functions]
        try:
            assert entered.wait(5)
        finally:
            release.set()
        assert [job.result(timeout=5) for job in pending] == [[1.0]] * 8
    assert calls == ["shared question"]
    replacement = SimpleNamespace(model_id="same-space", embed=lambda text, **kw: [2.0])
    monkeypatch.setattr(emb, "get_shared_embedder", lambda: replacement)
    assert functions[0]("shared question") == [2.0]


def test_shared_cache_bounds_entries_and_retained_input_text(monkeypatch):
    calls = []

    def embed(text, **kwargs):
        calls.append(text)
        return [1.0]

    backend = SimpleNamespace(model_id="bounded-cache", embed=embed)
    monkeypatch.setattr(emb, "get_shared_embedder", lambda: backend)
    fn = emb.make_sync_embed_fn()
    oversized = "x" * emb._MAX_EMBED_CHARS
    fn(oversized + "first tail")
    fn(oversized + "second tail")
    assert calls == [oversized]
    for index in range(emb._EMBED_CACHE_MAX):
        fn(f"bounded row {index}")
    assert len(emb._sync_embed_cache) == emb._EMBED_CACHE_MAX
    fn(oversized)
    assert calls.count(oversized) == 2, "the oldest cache entry was never evicted"


def test_queue_is_bounded_and_reserves_space_for_interactive_queries(backend, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    queued = threading.Event()
    count = 0

    def infer(texts):
        if texts == ["gate"]:
            entered.set()
            assert release.wait(5)
        return {"data": [{"embedding": [1.0]} for _ in texts]}

    backend._llm = SimpleNamespace(create_embedding=infer)
    with ThreadPoolExecutor(max_workers=10) as pool:
        running = pool.submit(backend.embed, "gate")
        try:
            assert entered.wait(5)
            put = backend._jobs.put

            def record(item):
                nonlocal count
                put(item)
                count += 1
                if count == emb._MAX_PENDING_EMBEDS - emb._INTERACTIVE_QUEUE_RESERVE:
                    queued.set()

            monkeypatch.setattr(backend._jobs, "put", record)
            pending = [
                pool.submit(backend.embed, f"normal-{i}")
                for i in range(emb._MAX_PENDING_EMBEDS - emb._INTERACTIVE_QUEUE_RESERVE)
            ]
            assert queued.wait(5)
            assert backend.embed("overflow") is None
            assert backend._jobs.qsize() == emb._MAX_PENDING_EMBEDS - emb._INTERACTIVE_QUEUE_RESERVE
            interactive = pool.submit(
                backend.embed, "explicit recall", priority=emb.PRIORITY_INTERACTIVE
            )
        finally:
            release.set()
        assert running.result(timeout=5) == [1.0]
        assert interactive.result(timeout=5) == [1.0]
        assert all(job.result(timeout=5) == [1.0] for job in pending)


def test_batch_work_is_split_before_native_inference(backend):
    batches = []

    def infer(texts):
        batches.append(list(texts))
        return {"data": [{"embedding": [float(text)]} for text in texts]}

    backend._llm = SimpleNamespace(create_embedding=infer)
    count = emb._MAX_EMBED_BATCH_TEXTS * 2 + 1
    assert backend.embed_batch([str(i) for i in range(count)]) == [[float(i)] for i in range(count)]
    assert [len(batch) for batch in batches] == [emb._MAX_EMBED_BATCH_TEXTS] * 2 + [1]


def test_bulk_cooldown_is_shared_but_does_not_delay_interactive_calls(backend, monkeypatch):
    import queue
    import time

    jobs = queue.PriorityQueue()
    bulk = emb._InferJob(object(), ["background"])
    interactive = emb._InferJob(object(), ["explicit query"])
    jobs.put((emb.PRIORITY_BULK, 1, bulk))
    backend._bulk_ready_at = time.monotonic() + 30

    def wake_with_query(delay):
        assert 0 < delay <= 30
        jobs.put((emb.PRIORITY_INTERACTIVE, 2, interactive))

    monkeypatch.setattr(backend._jobs_changed, "wait", wake_with_query)
    assert backend._next_infer_job(jobs)[2] is interactive
    assert jobs.qsize() == 1
    backend._bulk_ready_at = 0
    assert backend._next_infer_job(jobs)[2] is bulk


@pytest.mark.asyncio
async def test_cancelled_memory_work_keeps_its_slot_until_the_thread_finishes(monkeypatch):
    from kiro_crew import executors

    monkeypatch.setattr(executors, "_MAX_EMBED_WORKERS", 1)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    submitted = []

    def held():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        original_submit = pool.submit

        def submit(*args, **kwargs):
            submitted.append(True)
            return original_submit(*args, **kwargs)

        monkeypatch.setattr(pool, "submit", submit)
        monkeypatch.setattr(executors, "embed_executor", lambda: pool)
        first = asyncio.create_task(executors.run_in_embed_pool(held))
        second = None
        try:
            await asyncio.wait_for(started.wait(), 5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(executors.run_in_embed_pool(lambda: "static prompt"))
            await asyncio.sleep(0)
            assert not second.done()
            assert len(submitted) == 1, "cancelling a caller freed an occupied native slot"
        finally:
            release.set()
            if second is not None:
                assert await asyncio.wait_for(second, 5) == "static prompt"
            await asyncio.gather(first, return_exceptions=True)
        assert len(submitted) == 2

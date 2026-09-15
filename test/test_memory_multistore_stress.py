"""Bounded mixed-load exercise of private SQLite stores and the shared worker.

Native computation is deterministic test data, not GGUF performance evidence.
Run with pytest -s to capture the JSON metrics for each concurrency level.
"""

import asyncio
import hashlib
import json
import math
import struct
import threading
import time
from collections import Counter
from types import SimpleNamespace

import pytest
from member_memory_helpers import write_member_home

from kiro_crew import context
from kiro_crew import embeddings as emb
from kiro_crew.config import loader
from kiro_crew.executors import run_in_embed_pool, run_with_recall_deadline
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.vector_memory import VectorMemoryStore


def deterministic_vector(text):
    digest = hashlib.sha256(text.encode()).digest()
    values = [int.from_bytes(digest[i : i + 4], "little") / 2**31 - 1 for i in range(0, 32, 4)]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [8, 32, 64])
async def test_private_store_mixed_load(tmp_path, monkeypatch, concurrency):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = write_member_home(tmp_path, *(f"store{i}" for i in range(16)))
    config["memory"] = {"embedding_dim": 8, "embedding_bulk_duty": 1}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    monkeypatch.setattr(emb, "_reembed_progress", emb.ReembedProgress())
    monkeypatch.setattr(context, "_vector_stores", {})
    backend = emb.LlamaCppEmbedder(model_path=tmp_path / "fake.gguf", dim=8, model_id="test-space")
    entered, release = threading.Event(), threading.Event()
    calls = Counter()

    def infer(texts):
        for text in texts:
            calls[text] += 1
        if texts == ["hold native worker"]:
            entered.set()
            assert release.wait(10)
        return {"data": [{"embedding": deterministic_vector(text)} for text in texts]}

    backend._llm = SimpleNamespace(create_embedding=infer, close=lambda: None)
    emb.install_shared_embedder(backend)
    stores = {}
    started = time.monotonic()
    stats = {"completed": 0, "errors": 0, "recall_hits": 0, "recalls": 0, "queue_peak": 0}
    latencies, lags = [], []
    stop = asyncio.Event()
    limiter = asyncio.Semaphore(concurrency)

    def make_store(name):
        path = (
            tmp_path / "memory.db"
            if name == "default"
            else tmp_path / "memory_stores" / name / "memory.db"
        )
        store = VectorMemoryStore(db_path=path, embedding_dim=8)
        store.init()
        store.embed_fn = emb.make_sync_embed_fn()
        emb.align_store_embedding_space(store)
        return store

    async def sample():
        while not stop.is_set():
            start = time.monotonic()
            await asyncio.sleep(0.002)
            lags.append(max(0.0, time.monotonic() - start - 0.002))
            stats["queue_peak"] = max(stats["queue_peak"], backend._jobs.qsize())

    async def operation(name, kind, index):
        async with limiter:
            store = stores[name]
            marker = "marker" + name.replace("-", "") + "end"
            try:
                if kind == "episode":
                    await run_in_embed_pool(
                        store.write_episodic, f"{marker} deployment event number {index}"
                    )
                elif kind == "fact":
                    await run_in_embed_pool(
                        store.set_semantic,
                        "project.hot",
                        f"{marker} fact revision {index}",
                        1.0,
                        "user_explicit",
                    )
                elif kind == "recall":
                    start = time.monotonic()
                    result = await run_with_recall_deadline(run_in_embed_pool(store.recall, marker))
                    latencies.append(time.monotonic() - start)
                    stats["recalls"] += 1
                    stats["recall_hits"] += int(
                        marker in result["episodic_context"] or marker in result["semantic_context"]
                    )
                    body = result["episodic_context"] + result["semantic_context"]
                    assert all(
                        "marker" + other.replace("-", "") + "end" not in body
                        for other in stores
                        if other != name
                    )
                else:
                    await run_in_embed_pool(
                        store.backfill_missing_embeddings, pace=False, max_rows_per_kind=2
                    )
                stats["completed"] += 1
            except Exception:
                stats["errors"] += 1
                raise

    monitor = blocker = None
    tasks = []
    try:
        for name in ("default", *(f"member-store{i}" for i in range(16))):
            stores[name] = await asyncio.to_thread(make_store, name)
        context._vector_stores.update(
            {name: store for name, store in stores.items() if name != "default"}
        )
        # Seed one known record so recall correctness never depends on writer order.
        for name, store in stores.items():
            marker = "marker" + name.replace("-", "") + "end"
            await asyncio.to_thread(
                store.write_episodic, f"{marker} initial deployment record", defer_embedding=True
            )
        monitor = asyncio.create_task(sample())
        blocker = asyncio.create_task(asyncio.to_thread(backend.embed, "hold native worker"))
        assert await asyncio.to_thread(entered.wait, 5)
        for name in stores:
            for index in range(4):
                tasks.extend(
                    asyncio.create_task(operation(name, "episode", index)) for _ in range(2)
                )
                tasks.append(asyncio.create_task(operation(name, "fact", index)))
            tasks.extend(
                asyncio.create_task(operation(name, "recall", index)) for index in range(2)
            )
            tasks.append(asyncio.create_task(operation(name, "backfill", 0)))

        async def wait_for_queue():
            while backend._jobs.qsize() == 0:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_queue(), 5)
        stats["queue_peak"] = max(stats["queue_peak"], backend._jobs.qsize())
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 45)
        await blocker
        gateway = object.__new__(GatewayOrchestrator)
        gateway._memory_repair_stop = threading.Event()
        gateway._memory_repair_cursor = ""
        gateway._auto_migrate_task = None
        gateway.vector_memory = stores["default"]
        for _ in range(3 * len(stores)):
            await asyncio.wait_for(asyncio.to_thread(gateway._repair_member_memory_once), 10)
        duplicate = mismatch = nulls = cross_store = 0
        for name, store in stores.items():

            def inspect():
                rows = list(
                    store.db.execute(
                        "SELECT text, embedding FROM episodic_memories WHERE is_deleted = 0"
                    )
                )
                facts = list(
                    store.db.execute(
                        "SELECT key, value_json, embedding FROM semantic_memory WHERE is_deleted = 0"
                    )
                )
                dup = len(rows) - len({row["text"] for row in rows})
                bad = missing = foreign = 0
                marker = "marker" + name.replace("-", "") + "end"
                for text, blob in [(r["text"], r["embedding"]) for r in rows] + [
                    (f"{r['key']} {r['value_json']}", r["embedding"]) for r in facts
                ]:
                    foreign += int(marker not in text)
                    missing += int(blob is None)
                    if blob is not None:
                        actual = struct.unpack("8f", blob)
                        expected = deterministic_vector(text)
                        bad += int(max(abs(a - b) for a, b in zip(actual, expected)) > 1e-6)
                return dup, bad, missing, foreign, len(rows)

            values = await asyncio.to_thread(inspect)
            assert values[4] == 5
            duplicate += values[0]
            mismatch += values[1]
            nulls += values[2]
            cross_store += values[3]
            await asyncio.to_thread(store.close)
            reopened = await asyncio.to_thread(make_store, name)
            stores[name] = reopened
            store = reopened
            assert await asyncio.to_thread(inspect) == values
            assert not await asyncio.to_thread(reopened.has_pending_embeddings)
            assert len(await asyncio.to_thread(reopened.get_episodic_list)) == 5
        stats.update(
            end_to_end_ops_per_second=round(stats["completed"] / (time.monotonic() - started), 3),
            recall_p50_ms=round(sorted(latencies)[len(latencies) // 2] * 1000, 3),
            recall_p95_ms=round(sorted(latencies)[int(len(latencies) * 0.95)] * 1000, 3),
            concurrency=concurrency,
            private_stores=16,
            v1_controls=1,
            duplicates=duplicate,
            body_vector_mismatches=mismatch,
            null_vectors=nulls,
            cross_store_markers=cross_store,
            reopened_stores=len(stores),
            recall_max_ms=round(max(latencies) * 1000, 3),
            loop_delay_max_ms=round(max(lags, default=0) * 1000, 3),
            native_calls=sum(calls.values()),
            compute="deterministic_fake_native",
        )
        print("MEMORY_STRESS " + json.dumps(stats, sort_keys=True))
        assert stats["errors"] == duplicate == mismatch == nulls == cross_store == 0
        assert stats["completed"] == 255
        assert stats["recall_hits"] == stats["recalls"] == 34
        assert stats["queue_peak"] <= emb._MAX_PENDING_EMBEDS
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if blocker is not None:
            await asyncio.gather(blocker, return_exceptions=True)
        stop.set()
        if monitor is not None:
            await monitor
        await asyncio.to_thread(emb.reset_shared_embedder)
        for store in stores.values():
            await asyncio.to_thread(store.close)
        loader._invalidate_config_cache()

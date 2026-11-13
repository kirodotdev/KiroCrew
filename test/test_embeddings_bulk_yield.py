"""A bulk batch yields the model to waiting interactive work between its texts.

One bulk job holds up to ``_MAX_EMBED_BATCH_TEXTS`` texts on one thread, and a
started native call cannot be stopped. The batch runs one text at a time and
serves any queued interactive or normal job in between, so a chat-turn recall
embed waits for at most one text, not the whole batch. The batch still
finishes: every text embedded once, vectors in input order.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from kiro_crew import embeddings as emb
from kiro_crew.testing.wait import wait_until


@pytest.fixture
def backend(tmp_path, monkeypatch):
    # duty 1.0: no bulk pacing sleep, so the test measures order, not the clock.
    monkeypatch.setattr(emb, "_read_memory_config", lambda: {"embedding_bulk_duty": 1.0})
    instance = emb.LlamaCppEmbedder(model_path=tmp_path / "unused.gguf", dim=1)
    try:
        yield instance
    finally:
        instance.close()


def _vector(text: str) -> list[float]:
    return [float(len(text))]


def test_interactive_embed_runs_between_bulk_texts(backend):
    order: list[str] = []
    first_bulk_running = threading.Event()
    query_queued = threading.Event()

    def infer(texts):
        order.extend(texts)
        if texts and texts[0] == "b1":
            first_bulk_running.set()
            assert query_queued.wait(5)
        return {"data": [{"index": i, "embedding": _vector(t)} for i, t in enumerate(texts)]}

    backend._llm = SimpleNamespace(create_embedding=infer)

    with ThreadPoolExecutor(max_workers=2) as pool:
        bulk = pool.submit(backend.embed_batch, ["b1", "bb2", "bbb3"], priority=emb.PRIORITY_BULK)
        assert first_bulk_running.wait(5)
        jobs = backend._jobs
        original_put = jobs.put

        def record(item):
            original_put(item)
            query_queued.set()

        jobs.put = record
        try:
            query = pool.submit(backend.embed, "chat query", priority=emb.PRIORITY_INTERACTIVE)
            wait_until(query.done)
            wait_until(bulk.done)
        finally:
            jobs.put = original_put

    assert query.result() == _vector("chat query")
    assert bulk.result() == [_vector("b1"), _vector("bb2"), _vector("bbb3")]
    # The query ran after the text in flight, before the rest of the batch.
    assert order == ["b1", "chat query", "bb2", "bbb3"]


def test_bulk_batch_without_competition_embeds_each_text_once(backend):
    calls: list[list[str]] = []

    def infer(texts):
        calls.append(list(texts))
        return {"data": [{"index": i, "embedding": _vector(t)} for i, t in enumerate(texts)]}

    backend._llm = SimpleNamespace(create_embedding=infer)
    texts = [f"row-{i}" * (i + 1) for i in range(emb._MAX_EMBED_BATCH_TEXTS)]
    assert backend.embed_batch(texts, priority=emb.PRIORITY_BULK) == [_vector(t) for t in texts]
    assert sorted(t for call in calls for t in call) == sorted(texts)


def test_a_model_retired_mid_batch_is_never_used_for_the_rest(backend):
    """close() or an idle unload between two texts must not reach a freed model."""
    order: list[str] = []

    def infer(texts):
        order.extend(texts)
        if texts and texts[0] == "b1":
            # What close() does under _lock: it unpublishes the model.
            backend._llm = None
        return {"data": [{"index": i, "embedding": _vector(t)} for i, t in enumerate(texts)]}

    backend._llm = SimpleNamespace(create_embedding=infer)
    # None: the rows stay pending and are retried later, nothing half-stored.
    assert backend.embed_batch(["b1", "b2", "b3"], priority=emb.PRIORITY_BULK) is None
    assert order == ["b1"]


def test_a_batch_promoted_while_running_stops_yielding(backend):
    """promote_pending() means a person waits on this batch; it then runs to the end."""
    import math

    order: list[str] = []
    first_running = threading.Event()
    query_queued = threading.Event()
    work = emb.EmbeddingWork(math.inf, priority=emb.PRIORITY_BULK)

    def infer(texts):
        order.extend(texts)
        if texts and texts[0] == "b1":
            first_running.set()
            assert query_queued.wait(5)
        return {"data": [{"index": i, "embedding": _vector(t)} for i, t in enumerate(texts)]}

    def run_bulk():
        previous = emb.embedding_work.get()
        emb.embedding_work.set(work)
        try:
            return backend.embed_batch(["b1", "bb2", "bbb3"], priority=emb.PRIORITY_BULK)
        finally:
            emb.embedding_work.set(previous)

    backend._llm = SimpleNamespace(create_embedding=infer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        bulk = pool.submit(run_bulk)
        assert first_running.wait(5)
        backend.promote_pending(work, emb.PRIORITY_INTERACTIVE)
        jobs = backend._jobs
        original_put = jobs.put

        def record(item):
            original_put(item)
            query_queued.set()

        jobs.put = record
        try:
            query = pool.submit(backend.embed, "chat query", priority=emb.PRIORITY_INTERACTIVE)
            wait_until(query.done)
            wait_until(bulk.done)
        finally:
            jobs.put = original_put

    assert bulk.result() == [_vector("b1"), _vector("bb2"), _vector("bbb3")]
    assert query.result() == _vector("chat query")
    assert order == ["b1", "bb2", "bbb3", "chat query"]

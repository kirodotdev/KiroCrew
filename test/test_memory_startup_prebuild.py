"""The post-restart memory prebuild warms the session-start reads without changing them.

``memory_startup_prebuild`` builds the scoring set, its term index and the
lesson tokens on a worker thread after boot. It must only read (no embed, no write),
build the same structures the first session would, skip a group the config leaves
out, and leave every later read's result unchanged.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import memory_startup_prebuild as startup_prebuild
from kiro_crew.vector_memory import VectorMemoryStore
from kiro_crew.vector_memory_runtime import term_index

_DIM = 16


def _embed(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [(b - 128) / 64.0 for b in digest[:_DIM]]


def _cfg(*, memory: bool = True, lessons: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        memory=SimpleNamespace(
            persistence_enabled=True, inject_memory=memory, inject_lessons=lessons
        )
    )


@pytest.fixture
def store(tmp_path: Path) -> VectorMemoryStore:
    vs = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=_DIM)
    vs.init()
    vs.embed_fn = _embed
    words = ["tokyo", "paris", "deploy", "pipeline", "memory", "recall", "rust", "python"]
    for i in range(30):
        value = f"{words[i % len(words)]} {words[(i * 3) % len(words)]} note {i}"
        assert vs.set_semantic(f"project.item_{i}.fact", value, 1.0, "user_explicit") is None
    for i in range(4):
        vs.write_lesson(f"Always run the {words[i]} build before release step {i}", "tool")
    vs._semantic_scoring = None
    return vs


def _forbid_embed(store: VectorMemoryStore, monkeypatch) -> None:
    def refuse(*_a, **_k):
        raise AssertionError("the prebuild embedded")

    monkeypatch.setattr(store, "embed_fn", refuse)
    monkeypatch.setattr(store, "_try_embed", refuse)


def test_prebuild_builds_the_scoring_set_and_its_index_without_embedding(
    store: VectorMemoryStore, monkeypatch
) -> None:
    _forbid_embed(store, monkeypatch)
    before = store.db.total_changes
    spent = startup_prebuild.prebuild_startup_reads(store, _cfg())
    assert set(spent) == {"scoring_set", "term_index", "lessons"}
    derived = store._semantic_scoring.derived
    assert isinstance(derived["term_index"][0], term_index.RowTermIndex)
    assert store.db.total_changes == before, "the prebuild wrote to the store"


def test_reads_after_the_prebuild_match_a_cold_store(tmp_path: Path, store, monkeypatch) -> None:
    query = "tokyo deploy release"
    cold = store._semantic_candidates_v1(query)
    cold_lessons = store.get_lessons_context(query, background=True)
    store._semantic_scoring = None
    startup_prebuild.prebuild_startup_reads(store, _cfg())
    index = store._semantic_scoring.derived["term_index"]
    assert store._semantic_candidates_v1(query) == cold
    assert store.get_lessons_context(query, background=True) == cold_lessons
    assert store._semantic_scoring.derived["term_index"] is index, "the session rebuilt it"


@pytest.mark.parametrize(
    ("memory", "lessons", "expected"),
    [
        (False, True, {"lessons"}),
        (True, False, {"scoring_set", "term_index"}),
        (False, False, set()),
    ],
)
def test_a_group_the_config_withholds_is_not_built(
    store: VectorMemoryStore, memory: bool, lessons: bool, expected: set[str]
) -> None:
    spent = startup_prebuild.prebuild_startup_reads(store, _cfg(memory=memory, lessons=lessons))
    assert set(spent) == expected
    assert (store._semantic_scoring is not None) == memory


def test_a_stopping_gateway_skips_the_remaining_steps(store: VectorMemoryStore) -> None:
    asked: list[int] = []

    def stop_after_first_step() -> bool:
        asked.append(1)
        return len(asked) > 1

    spent = startup_prebuild.prebuild_startup_reads(
        store, _cfg(), should_stop=stop_after_first_step
    )
    assert set(spent) == {"scoring_set"}


def test_a_failing_step_is_logged_and_the_rest_still_run(
    store: VectorMemoryStore, monkeypatch, caplog
) -> None:
    def boom():
        raise RuntimeError("scoring unavailable")

    monkeypatch.setattr(store, "_semantic_scoring_set", boom)
    with caplog.at_level("WARNING", logger=startup_prebuild.__name__):
        spent = startup_prebuild.prebuild_startup_reads(store, _cfg())
    assert "scoring_set failed" in caplog.text
    assert set(spent) == {"scoring_set", "lessons"}


def test_the_gateway_hook_skips_a_gateway_without_a_vector_store(monkeypatch) -> None:
    from kiro_crew.dashboard import server

    called: list[object] = []
    monkeypatch.setattr(startup_prebuild, "prebuild_startup_reads", called.append)
    no_builder = SimpleNamespace(context_builder=None)
    no_vectors = SimpleNamespace(
        context_builder=SimpleNamespace(memory=SimpleNamespace(vector_store=None))
    )
    server._prebuild_memory_in_thread(no_builder)
    server._prebuild_memory_in_thread(no_vectors)
    assert called == []


def test_the_gateway_hook_prebuilds_the_default_memory_store(monkeypatch) -> None:
    from kiro_crew.dashboard import server

    vectors = object()
    seen: list[object] = []

    def record(store, cfg=None, should_stop=lambda: False):
        seen.append(store)
        assert should_stop() is False
        return {}

    monkeypatch.setattr(startup_prebuild, "prebuild_startup_reads", record)
    state = SimpleNamespace(
        context_builder=SimpleNamespace(memory=SimpleNamespace(vector_store=vectors))
    )
    server._prebuild_memory_in_thread(state)
    assert seen == [vectors]


class _KnowledgeStore:
    """The few store calls the knowledge maintenance pass makes, recording its turn."""

    def __init__(self, order: list[str], on_pass=lambda: None) -> None:
        self._order = order
        self._on_pass = on_pass

    def has_pending_one_time_work(self) -> bool:
        return False

    def maintenance_window(self):
        import contextlib

        self._order.append("knowledge")
        self._on_pass()
        return contextlib.nullcontext(False)

    def ensure_search_indexes(self) -> None:
        pass

    def ensure_fts_index_current(self) -> None:
        pass


def _kick(monkeypatch, state, *, preparing, prepared=lambda: True) -> None:
    """Run the knowledge kick to completion, its thread hops run inline."""
    import asyncio

    from kiro_crew import memory_startup
    from kiro_crew.dashboard import server

    async def inline_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(memory_startup, "memory_startup_preparing", preparing)
    monkeypatch.setattr(memory_startup, "memory_prepared", prepared)

    async def run() -> None:
        monkeypatch.setattr(server.asyncio, "to_thread", inline_to_thread)
        server._kick_knowledge_orphan_reclaim(state)
        while state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))

    asyncio.run(run())


def _state(order: list[str], **extra) -> SimpleNamespace:
    return SimpleNamespace(
        _knowledge_store=_KnowledgeStore(order),
        _background_tasks=set(),
        context_builder=SimpleNamespace(memory=SimpleNamespace(vector_store=object())),
        **extra,
    )


def _record_prebuild(monkeypatch, order: list[str]) -> None:
    monkeypatch.setattr(
        startup_prebuild,
        "prebuild_startup_reads",
        lambda store, cfg=None, should_stop=lambda: False: order.append("memory") or {},
    )


def test_the_prebuild_runs_after_the_knowledge_pass(monkeypatch) -> None:
    order: list[str] = []
    _record_prebuild(monkeypatch, order)
    _kick(monkeypatch, _state(order), preparing=lambda: False)
    assert order == ["knowledge", "memory"]


def test_the_prebuild_waits_for_memory_preparation(monkeypatch) -> None:
    """Before preparation is ready every store read refuses, so nothing would warm."""
    import asyncio

    order: list[str] = []
    _record_prebuild(monkeypatch, order)
    done = {"ready": False}

    async def run_with_preparation() -> None:
        # Preparation finishes only after the knowledge pass, so the prebuild
        # reaches its wait while preparation is still running.
        released = asyncio.Event()

        async def preparation() -> None:
            await released.wait()
            order.append("preparation")
            done["ready"] = True

        state = _state(order, memory_startup_task=asyncio.ensure_future(preparation()))
        state._knowledge_store = _KnowledgeStore(order, on_pass=released.set)
        from kiro_crew import memory_startup
        from kiro_crew.dashboard import server

        async def inline_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        monkeypatch.setattr(memory_startup, "memory_startup_preparing", lambda: not done["ready"])
        monkeypatch.setattr(memory_startup, "memory_prepared", lambda: done["ready"])
        monkeypatch.setattr(server.asyncio, "to_thread", inline_to_thread)
        server._kick_knowledge_orphan_reclaim(state)
        while state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))

    asyncio.run(run_with_preparation())
    assert order == ["knowledge", "preparation", "memory"]


def test_a_failed_preparation_skips_the_prebuild(monkeypatch) -> None:
    order: list[str] = []
    _record_prebuild(monkeypatch, order)
    _kick(monkeypatch, _state(order), preparing=lambda: False, prepared=lambda: False)
    assert order == ["knowledge"]


def test_a_gateway_without_a_context_builder_takes_no_thread_hop(monkeypatch) -> None:
    order: list[str] = []
    _record_prebuild(monkeypatch, order)
    state = SimpleNamespace(_knowledge_store=_KnowledgeStore(order), _background_tasks=set())
    _kick(monkeypatch, state, preparing=lambda: False)
    assert order == ["knowledge"]

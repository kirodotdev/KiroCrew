"""Memory preparation starts the embedding model load before it completes.

The first recall after readiness embeds its query. Once the model unloads when
idle, nothing loads it during boot, so that recall waited for the whole load
unless preparation starts it.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import memory_backup
from kiro_crew.memory_startup import MemoryStartup, require_memory_prepared
from kiro_crew.slack.gateway import GatewayOrchestrator


def _gateway(monkeypatch, *, embed_fn):
    gateway = object.__new__(GatewayOrchestrator)
    gateway._memory_startup = MemoryStartup.begin()
    gateway._memory_startup_task = None
    gateway._auto_migrate_task = None
    gateway._memory_repair_task = None
    gateway._memory_repair_stop = threading.Event()
    gateway._memory_repair_cursor = ""
    gateway._background_tasks = set()
    gateway.dashboard_state = SimpleNamespace(memory_startup_task=None, resume_channel_agents=None)
    memory = MagicMock()
    memory.rebuild_index.return_value = 0
    gateway.ctx_builder = SimpleNamespace(memory=memory)
    gateway.vector_memory = MagicMock()
    gateway.vector_memory.embed_fn = embed_fn
    gateway._auto_migrate_memory = AsyncMock()
    gateway._repair_member_memory = AsyncMock()
    monkeypatch.setattr("kiro_crew.context.reset_memory_caches", lambda memory: None)
    monkeypatch.setattr("kiro_crew.memory_stores.repair_legacy_member_stores", lambda: [])
    monkeypatch.setattr(memory_backup, "apply_pending_member_restores", lambda **kwargs: {})
    return gateway


class _Backend:
    def __init__(self, *, fail: bool = False):
        self.prefetches: list[bool] = []
        self.fail = fail

    def prefetch(self) -> None:
        # Recorded while the worker holds the barrier, i.e. before preparation completes.
        self.prefetches.append(True)
        if self.fail:
            raise RuntimeError("load hint failed")


@pytest.mark.asyncio
async def test_preparation_starts_the_model_load_before_it_completes(monkeypatch):
    backend = _Backend()
    gateway = _gateway(monkeypatch, embed_fn=lambda text: [0.0])
    seen_before_open: list[int] = []

    def open_vectors():
        require_memory_prepared()  # the worker alone is admitted
        seen_before_open.append(len(backend.prefetches))

    gateway.vector_memory.init.side_effect = open_vectors
    monkeypatch.setattr("kiro_crew.slack.gateway.get_shared_embedder", lambda: backend)
    try:
        await gateway._wait_for_memory_preparation()
        assert backend.prefetches == [True]
        assert seen_before_open == [1], "the load must start before the store opens"
    finally:
        await asyncio.to_thread(gateway._stop_memory_startup)


@pytest.mark.asyncio
async def test_no_model_file_means_no_prefetch(monkeypatch):
    backend = _Backend()
    gateway = _gateway(monkeypatch, embed_fn=None)
    monkeypatch.setattr("kiro_crew.slack.gateway.get_shared_embedder", lambda: backend)
    try:
        await gateway._wait_for_memory_preparation()
        assert backend.prefetches == []
    finally:
        await asyncio.to_thread(gateway._stop_memory_startup)


@pytest.mark.asyncio
async def test_preparation_reads_the_memory_databases_in_order_first(monkeypatch, tmp_path):
    from kiro_crew import page_cache

    gateway = _gateway(monkeypatch, embed_fn=None)
    gateway.vector_memory._db_path = tmp_path / "memory.db"
    gateway.ctx_builder.memory._index_db = tmp_path / "memory_index.db"
    gateway.dashboard_state._knowledge_store = SimpleNamespace(_db_path=str(tmp_path / "kb.db"))
    asked: list[list] = []
    opened_before: list[int] = []
    monkeypatch.setattr(page_cache, "prefetch", lambda paths, name="": asked.append(list(paths)))
    gateway.vector_memory.init.side_effect = lambda: opened_before.append(len(asked))
    try:
        await gateway._wait_for_memory_preparation()
        # Memory first (preparation reads it next), then the knowledge library
        # (the first search reads it); all after readiness, off the boot path.
        assert asked == [
            [tmp_path / "memory.db", tmp_path / "memory_index.db", str(tmp_path / "kb.db")]
        ]
        assert opened_before == [1], "the read must start before the store opens"
    finally:
        await asyncio.to_thread(gateway._stop_memory_startup)


@pytest.mark.asyncio
async def test_the_read_starts_only_after_pending_restores(monkeypatch, tmp_path):
    # A pending restore renames memory.db; on Windows an open read handle
    # would make that rename fail, so the read must not start before it.
    from kiro_crew import page_cache

    gateway = _gateway(monkeypatch, embed_fn=None)
    gateway.vector_memory._db_path = tmp_path / "memory.db"
    order: list[str] = []
    monkeypatch.setattr(
        memory_backup,
        "apply_pending_member_restores",
        lambda **kwargs: order.append("restore") or {},
    )
    monkeypatch.setattr(page_cache, "prefetch", lambda paths, name="": order.append("read"))
    try:
        await gateway._wait_for_memory_preparation()
        assert order == ["restore", "read"]
    finally:
        await asyncio.to_thread(gateway._stop_memory_startup)


@pytest.mark.asyncio
async def test_a_failing_prefetch_does_not_fail_preparation(monkeypatch):
    backend = _Backend(fail=True)
    gateway = _gateway(monkeypatch, embed_fn=lambda text: [0.0])
    monkeypatch.setattr("kiro_crew.slack.gateway.get_shared_embedder", lambda: backend)
    try:
        await gateway._wait_for_memory_preparation()
        assert backend.prefetches == [True]
        assert gateway._memory_startup.ready
        assert not gateway._memory_startup.error
    finally:
        await asyncio.to_thread(gateway._stop_memory_startup)

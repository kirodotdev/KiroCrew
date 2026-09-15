"""Deferred gateway recovery fences real memory entry points until ready."""

import ast
import asyncio
import inspect
import json
import textwrap
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew import embeddings, memory_backup
from kiro_crew.context import ContextBuilder, prepare_store_vectors
from kiro_crew.dashboard.handlers import memory_admin, memory_member
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_startup import (
    MemoryStartup,
    MemoryStartupUnavailable,
    require_memory_ready,
    wait_for_memory_preparation,
)
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.vector_memory import VectorMemoryStore

env = _member_env


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "member-alice"])
async def test_startup_blocks_cached_new_context_http_and_backup_access(env, tmp_path, name):
    tier = env.tiers[name]
    assert tier.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit") is None
    before = dict(tier.get_semantic("project.database"))
    markdown = MemoryStore()
    startup = MemoryStartup.begin()
    try:
        with pytest.raises(MemoryStartupUnavailable):
            tier.get_semantic("project.database")
        unopened = tmp_path / "must-stay-absent.db"
        with pytest.raises(MemoryStartupUnavailable):
            VectorMemoryStore(db_path=unopened).init()
        assert not unopened.exists()
        with pytest.raises(MemoryStartupUnavailable):
            markdown.init()
        with pytest.raises(MemoryStartupUnavailable):
            markdown.read_preferences()
        with pytest.raises(MemoryStartupUnavailable):
            ContextBuilder.get_memory_for(memory_store=name)
        with pytest.raises(MemoryStartupUnavailable):
            await prepare_store_vectors(SimpleNamespace(), name)
        with pytest.raises(MemoryStartupUnavailable):
            memory_backup.backup_store(tier._db_path)
        assert not memory_backup.pending_restore_status(tier._db_path)["pending"]
        assert not memory_backup.cancel_pending_restore(tier._db_path)
        response = await memory_member.api_memory_recall(
            request(env, query={"store": name or "default", "q": "database"}, owner=True)
        )
        assert response.status == 503
        assert json.loads(response.text)["code"] == "store_unavailable"
        assert startup.complete()
        assert dict(tier.get_semantic("project.database")) == before
    finally:
        startup.stop()
        startup.release()


def _gateway(monkeypatch):
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
    memory.init.side_effect = require_memory_ready
    memory.rebuild_index.return_value = 2
    gateway.ctx_builder = SimpleNamespace(memory=memory)
    gateway.vector_memory = MagicMock()
    gateway.vector_memory.init.side_effect = require_memory_ready
    gateway._auto_migrate_memory = AsyncMock()
    gateway._repair_member_memory = AsyncMock()
    monkeypatch.setattr("kiro_crew.context.reset_memory_caches", lambda memory: None)
    return gateway


@pytest.mark.asyncio
async def test_preparation_task_without_owner_cannot_admit_consumers(monkeypatch):
    gateway = _gateway(monkeypatch)
    owner = gateway._memory_startup
    completed = asyncio.get_running_loop().create_future()
    completed.set_result(None)
    gateway._schedule_memory_preparation = MagicMock(return_value=completed)
    gateway._memory_startup = None
    try:
        with pytest.raises(MemoryStartupUnavailable, match="no lifecycle owner"):
            await gateway._wait_for_memory_preparation()
        assert not completed.cancelled()
        gateway._auto_migrate_memory.assert_not_awaited()
    finally:
        gateway._memory_startup = owner
        await asyncio.to_thread(gateway._stop_memory_startup)


@pytest.mark.asyncio
async def test_pre_ready_schedule_publishes_task_before_worker_can_run(monkeypatch):
    gateway = _gateway(monkeypatch)
    started = threading.Event()
    release = threading.Event()

    def prepare():
        started.set()
        assert release.wait(5)
        return True

    gateway._initialize_memory_worker = prepare
    task = gateway._schedule_memory_preparation()
    try:
        assert task is not None
        assert gateway.dashboard_state.memory_startup_task is task
        # create_task does not run the coroutine until this turn yields, so the
        # caller can print READY immediately after publication without starting
        # restore, store-open or rebuild work on the boot path.
        assert not started.is_set()
        assert await asyncio.to_thread(started.wait, 5)
        assert not task.done()
    finally:
        release.set()
        if task is not None:
            await task
        await asyncio.to_thread(gateway._stop_memory_startup)


def test_run_publishes_ready_before_wait_and_starts_dispatchers_afterward():
    source = inspect.getsource(GatewayOrchestrator.run)
    tree = ast.parse(textwrap.dedent(source))
    awaited_calls = {
        node.value.func.attr: node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
    }
    ready_line = next(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
        and any(
            isinstance(part, ast.Constant)
            and isinstance(part.value, str)
            and "KIROCREW_READY:" in part.value
            for part in ast.walk(node)
        )
    )
    ready = source.index('print(f"KIROCREW_READY:')
    signals = source.index("self._install_shutdown_signal_handlers()")
    waited = source.index("await self._wait_for_memory_preparation()")
    dashboard_workers = source.index("self._start_dashboard_workers_after_memory_ready()")
    cron = source.index("await self._start_cron_after_memory_ready()")
    heartbeat = source.index("await self._init_heartbeat()")

    assert ready < signals < waited < dashboard_workers < cron < heartbeat
    admission = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.operand, ast.Await)
        and isinstance(node.test.operand.value, ast.Call)
        and isinstance(node.test.operand.value.func, ast.Attribute)
        and node.test.operand.value.func.attr == "_wait_for_memory_preparation"
    )
    assert isinstance(admission.body[-1], ast.Return)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_shutdown_and_exit"
        for node in ast.walk(admission)
    )
    assert awaited_calls["_init_api_server"] < ready_line
    assert not any(
        awaited_calls["_init_api_server"] < node.lineno < ready_line
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
    )


def test_post_memory_dashboard_workers_resume_legacy_channels_once(monkeypatch):
    gateway = _gateway(monkeypatch)
    try:
        assert gateway._memory_startup.complete()
        resume = MagicMock()
        gateway.dashboard_state.resume_channel_agents = resume

        gateway._start_dashboard_workers_after_memory_ready()

        resume.assert_called_once_with()
        assert gateway.dashboard_state.resume_channel_agents is None
    finally:
        gateway._stop_memory_startup()
    require_memory_ready()


@pytest.mark.asyncio
async def test_one_worker_finishes_restore_before_open_and_starts_migration(monkeypatch):
    gateway = _gateway(monkeypatch)
    order = []

    def restore(**kwargs):
        require_memory_ready()  # this worker alone is authorized
        order.append("restore")
        return {"member-alice": "saved-copy"}

    def open_vectors():
        require_memory_ready()
        assert order == ["restore"]
        order.append("open")

    monkeypatch.setattr(memory_backup, "apply_pending_member_restores", restore)
    gateway.vector_memory.init.side_effect = open_vectors
    try:
        await gateway._wait_for_memory_preparation()
        first = gateway._memory_startup_task
        await gateway._wait_for_memory_preparation()
        assert gateway._memory_startup_task is first
        assert gateway.dashboard_state.memory_startup_task is first
        assert gateway._auto_migrate_task is None
        assert gateway._memory_repair_task is None
        gateway._start_memory_after_ready()
        auto_migrate = gateway._auto_migrate_task
        repair = gateway._memory_repair_task
        gateway._start_memory_after_ready()
        assert gateway._auto_migrate_task is auto_migrate
        assert gateway._memory_repair_task is repair
        await gateway._auto_migrate_task
        await gateway._memory_repair_task
        assert order == ["restore", "open"]
        gateway.ctx_builder.memory.init.assert_called_once()
        gateway.ctx_builder.memory.rebuild_index.assert_called_once()
        gateway._auto_migrate_memory.assert_awaited_once()
        gateway._repair_member_memory.assert_awaited_once()
        require_memory_ready()
    finally:
        await asyncio.to_thread(gateway._stop_memory_startup)


def test_invalid_declared_store_is_scoped_without_failing_global_startup(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig

    startup = MemoryStartup.begin()
    visited = []
    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(
                memory_stores={
                    "default": SimpleNamespace(memory_version=1),
                    "../invalid": SimpleNamespace(memory_version=1),
                }
            )
        ),
    )
    monkeypatch.setattr(
        memory_backup,
        "_apply_pending_v1_restore",
        lambda path: visited.append(path) or None,
    )
    monkeypatch.setattr(memory_backup, "resolve_store_path", lambda name: name)
    try:
        with startup.worker():
            assert memory_backup.apply_pending_member_restores(on_error=startup.fail_store) == {}
        assert startup.complete()
        require_memory_ready()
        assert visited == ["default"]
        assert "../invalid" in startup.store_errors
        assert "memory_stores entry in config.json" in startup.store_errors["../invalid"]
        assert "cancel the pending restore" not in startup.store_errors["../invalid"]
        with pytest.raises(MemoryStartupUnavailable, match="invalid"):
            require_memory_ready("../invalid")
    finally:
        startup.stop()
        startup.release()


@pytest.mark.asyncio
async def test_post_ready_wait_uses_the_published_tracked_worker(monkeypatch):
    gateway = _gateway(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()

    async def prepare():
        started.set()
        await release.wait()
        gateway._memory_startup.ready = True

    gateway._memory_startup_task = asyncio.create_task(prepare())

    task = asyncio.create_task(gateway._wait_for_memory_preparation())
    try:
        await started.wait()
        assert not task.done()
        with pytest.raises(MemoryStartupUnavailable):
            require_memory_ready()
        release.set()
        await task
        require_memory_ready()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(gateway._stop_memory_startup)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ready", "failed", "stopped"])
async def test_turn_admission_requires_preparation_success(outcome):
    startup = MemoryStartup.begin()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def prepare():
        entered.set()
        await release.wait()
        if outcome == "ready":
            startup.complete()
        elif outcome == "failed":
            startup.fail(ValueError("restore configuration is unreadable"))
        else:
            startup.stop()

    task = asyncio.create_task(prepare())
    admitted = asyncio.create_task(wait_for_memory_preparation(task))
    try:
        await entered.wait()
        assert not admitted.done()
        release.set()
        if outcome == "ready":
            await admitted
            require_memory_ready()
            # A completed task follows the same authority check on the next turn.
            await wait_for_memory_preparation(task)
        else:
            reason = "unreadable" if outcome == "failed" else "stopping"
            with pytest.raises(MemoryStartupUnavailable, match=reason):
                await admitted
            with pytest.raises(MemoryStartupUnavailable, match=reason):
                await wait_for_memory_preparation(task)
    finally:
        release.set()
        await asyncio.gather(task, admitted, return_exceptions=True)
        startup.stop()
        startup.release()


@pytest.mark.asyncio
async def test_owner_shutdown_wins_while_restore_worker_remains_fenced(monkeypatch):
    gateway = _gateway(monkeypatch)
    stopping = asyncio.Event()
    monkeypatch.setattr("kiro_crew.slack.gateway.shutdown_event", stopping)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def restore(**kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return {}

    monkeypatch.setattr(memory_backup, "apply_pending_member_restores", restore)
    supervisor = asyncio.create_task(gateway._wait_for_memory_preparation())
    try:
        await asyncio.wait_for(started.wait(), 5)
        stopping.set()
        assert await asyncio.wait_for(supervisor, 1) is False
        assert gateway._memory_startup.stopped
        assert not gateway._memory_startup_task.done()
        assert not gateway._memory_startup_task.cancelled()
        gateway.vector_memory.init.assert_not_called()
        gateway._auto_migrate_memory.assert_not_awaited()
        with pytest.raises(MemoryStartupUnavailable):
            require_memory_ready()
        with pytest.raises(MemoryStartupUnavailable, match="Another gateway"):
            MemoryStartup.begin()
        release.set()
        await gateway._memory_startup_task
        gateway.vector_memory.init.assert_not_called()
        gateway.vector_memory.close.assert_called_once()
    finally:
        release.set()
        await asyncio.gather(supervisor, gateway._memory_startup_task, return_exceptions=True)
        await asyncio.to_thread(gateway._stop_memory_startup)


@pytest.mark.asyncio
async def test_owner_stop_during_waiter_cleanup_prevents_consumer_admission(monkeypatch):
    gateway = _gateway(monkeypatch)
    stopping = asyncio.Event()
    waiter_started = asyncio.Event()
    original_wait = stopping.wait

    async def wait_for_stop():
        waiter_started.set()
        try:
            return await original_wait()
        finally:
            stopping.set()

    async def prepare():
        await waiter_started.wait()
        gateway._memory_startup.complete()

    monkeypatch.setattr(stopping, "wait", wait_for_stop)
    monkeypatch.setattr("kiro_crew.slack.gateway.shutdown_event", stopping)
    gateway._memory_startup_task = asyncio.create_task(prepare())
    try:
        assert await gateway._wait_for_memory_preparation() is False
        assert gateway._memory_startup.stopped
        gateway._auto_migrate_memory.assert_not_awaited()
    finally:
        await gateway._memory_startup_task
        await asyncio.to_thread(gateway._stop_memory_startup)


@pytest.mark.asyncio
async def test_schedulers_refuse_to_arm_before_memory_preparation(monkeypatch):
    gateway = _gateway(monkeypatch)
    gateway._no_crons = False
    gateway.sessions = object()
    gateway.cron_svc = SimpleNamespace(start=AsyncMock(), start_reaper=MagicMock())
    gateway._cron_reconciled = True
    gateway._cron_armed = False
    try:
        with pytest.raises(RuntimeError, match="before memory preparation"):
            await gateway._start_cron_after_memory_ready()
        gateway.cron_svc.start.assert_not_awaited()
        gateway.cron_svc.start_reaper.assert_not_called()
        with pytest.raises(RuntimeError, match="before memory preparation"):
            await gateway._init_heartbeat()

        assert gateway._memory_startup.complete()
        await gateway._start_cron_after_memory_ready()
        gateway.cron_svc.start.assert_awaited_once()
        gateway.cron_svc.start_reaper.assert_called_once_with(gateway.sessions)

        await gateway._start_cron_after_memory_ready()
        gateway.cron_svc.start.assert_awaited_once()
    finally:
        await asyncio.to_thread(gateway._stop_memory_startup)


@pytest.mark.asyncio
async def test_stopped_worker_cannot_open_or_release_a_successors_barrier(monkeypatch):
    gateway = _gateway(monkeypatch)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def restore(**kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return {}

    monkeypatch.setattr(memory_backup, "apply_pending_member_restores", restore)
    task = asyncio.create_task(gateway._wait_for_memory_preparation())
    try:
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.to_thread(gateway._stop_memory_startup)
        with pytest.raises(MemoryStartupUnavailable):
            require_memory_ready()
        with pytest.raises(MemoryStartupUnavailable):
            MemoryStartup.begin()
        release.set()
        await task
        gateway.vector_memory.init.assert_not_called()
        gateway.vector_memory.close.assert_called_once()
        gateway._auto_migrate_memory.assert_not_awaited()
        successor = MemoryStartup.begin()
        try:
            gateway._memory_startup.release()
            with pytest.raises(MemoryStartupUnavailable):
                require_memory_ready()
        finally:
            successor.stop()
            successor.release()
    finally:
        release.set()
        await task
        await asyncio.to_thread(gateway._stop_memory_startup)


@pytest.mark.asyncio
async def test_owner_can_inspect_and_cancel_bad_journal_while_memory_stays_failed(env):
    tier = env.tiers[""]
    tier.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    backup = memory_backup.backup_store(tier._db_path)
    memory_backup.restore_from_backup(backup)
    pending = memory_backup.backup_dir_for(tier._db_path) / memory_backup._V1_PENDING
    pending.write_bytes(b"broken journal retained as evidence")
    startup = MemoryStartup.begin()
    startup.fail(ValueError("Global restore journal is unreadable"))
    try:
        response = await memory_admin.api_memory_backups(
            request(env, owner=True, query={"store": "default"})
        )
        assert response.status == 200
        body = json.loads(response.text)
        assert body["pending"] and body["restore_error"]
        assert body["recovery"]["journal"] == memory_backup._V1_PENDING
        response = await memory_admin.api_memory_restore_cancel(
            request(env, owner=True, body={"store": "default"})
        )
        assert response.status == 200
        assert json.loads(response.text)["cancelled"]
        with pytest.raises(MemoryStartupUnavailable, match="unreadable"):
            tier.get_semantic("project.database")
    finally:
        startup.stop()
        startup.release()
    assert json.loads(tier.get_semantic("project.database")["value_json"]) == "PostgreSQL"
    assert backup.exists()


def test_repair_probe_does_not_construct_or_warm_an_absent_model(monkeypatch):
    factory = MagicMock(side_effect=AssertionError("repair must not construct a backend"))
    monkeypatch.setattr(embeddings, "_shared_embedder", None)
    monkeypatch.setattr(embeddings, "_backend_factory", factory)
    assert embeddings.peek_ready_shared_embedder() is None
    factory.assert_not_called()
    backend = MagicMock()
    backend.is_ready.return_value = False
    monkeypatch.setattr(embeddings, "_shared_embedder", backend)
    assert embeddings.peek_ready_shared_embedder() is None
    backend.wait_ready.assert_not_called()
    backend.embed.assert_not_called()


def test_member_repair_revalidates_and_rotates_past_an_unavailable_member(env, monkeypatch):
    gateway = _gateway(monkeypatch)
    gateway.vector_memory = None
    gateway._memory_startup.complete()
    alice, bob = env.tiers["member-alice"], env.tiers["member-bob"]
    monkeypatch.setattr(
        "kiro_crew.context.cached_vector_store_entries",
        lambda: (("member-alice", alice), ("member-bob", bob)),
    )
    monkeypatch.setattr("kiro_crew.slack.gateway.peek_ready_shared_embedder", lambda: object())
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.reembed_progress", lambda: SimpleNamespace(is_active=lambda: False)
    )
    monkeypatch.setattr("kiro_crew.slack.gateway.reconcile_store_embedding_space", lambda store: 0)
    visits = []

    def validate(name):
        visits.append(name)
        if name == "member-alice":
            raise ValueError("Alice ownership is unavailable")
        return name

    monkeypatch.setattr("kiro_crew.memory_stores.require_memory_store", validate)
    monkeypatch.setattr(bob, "has_pending_embeddings", lambda: True)
    repair = MagicMock()
    monkeypatch.setattr(bob, "backfill_missing_embeddings", repair)
    try:
        with pytest.raises(ValueError, match="Alice ownership"):
            gateway._repair_member_memory_once()
        gateway._repair_member_memory_once()
        assert visits == ["member-alice", "member-bob"]
        assert repair.call_args.kwargs["max_rows_per_kind"] == 16
        assert repair.call_args.kwargs["should_stop"]() is False
        gateway._memory_repair_stop.set()
        gateway._repair_member_memory_once()
        assert repair.call_count == 1
    finally:
        gateway._stop_memory_startup()


def test_repair_rotates_across_active_global_and_cached_v1_v2_without_opening(monkeypatch):
    gateway = _gateway(monkeypatch)
    gateway._memory_startup.complete()
    global_v1 = gateway.vector_memory
    named_v1 = MagicMock()
    named_v2 = MagicMock()
    for store, version in ((global_v1, 1), (named_v1, 1), (named_v2, 2)):
        store._memory_version = version
        store.embed_fn = None
        store.has_pending_embeddings.return_value = True
    monkeypatch.setattr(
        "kiro_crew.context.cached_vector_store_entries",
        lambda: (("member-v2", named_v2), ("member-v1", named_v1)),
    )
    monkeypatch.setattr("kiro_crew.slack.gateway.peek_ready_shared_embedder", lambda: object())
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.reembed_progress", lambda: SimpleNamespace(is_active=lambda: False)
    )
    reconciled = []
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.reconcile_store_embedding_space",
        lambda store: reconciled.append(store),
    )
    validated = []
    monkeypatch.setattr(
        "kiro_crew.memory_stores.require_memory_store", lambda name: validated.append(name) or name
    )
    unopened = MagicMock(side_effect=AssertionError("repair must not open a memory store"))
    monkeypatch.setattr(ContextBuilder, "ensure_store", unopened)
    try:
        gateway._repair_member_memory_once()
        gateway._repair_member_memory_once()
        gateway._repair_member_memory_once()
        assert validated == ["default", "member-v1", "member-v2"]
        assert reconciled == [global_v1, named_v1, named_v2]
        unopened.assert_not_called()
        assert global_v1._memory_version == 1
        assert named_v1._memory_version == 1
        assert named_v2._memory_version == 2
        for store in (global_v1, named_v1, named_v2):
            store.backfill_missing_embeddings.assert_called_once()
            kwargs = store.backfill_missing_embeddings.call_args.kwargs
            assert kwargs["max_rows_per_kind"] == 16
            assert kwargs["should_stop"]() is False
    finally:
        gateway._stop_memory_startup()


def test_global_repair_waits_for_boot_migration(monkeypatch):
    gateway = _gateway(monkeypatch)
    gateway._memory_startup.complete()
    gateway.vector_memory.embed_fn = None
    gateway.vector_memory.has_pending_embeddings.return_value = True
    migration = MagicMock()
    migration.done.return_value = False
    gateway._auto_migrate_task = migration
    monkeypatch.setattr("kiro_crew.context.cached_vector_store_entries", lambda: ())
    monkeypatch.setattr("kiro_crew.slack.gateway.peek_ready_shared_embedder", lambda: object())
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.reembed_progress", lambda: SimpleNamespace(is_active=lambda: False)
    )
    reconcile = MagicMock()
    monkeypatch.setattr("kiro_crew.slack.gateway.reconcile_store_embedding_space", reconcile)
    try:
        gateway._repair_member_memory_once()
        gateway.vector_memory.backfill_missing_embeddings.assert_not_called()
        reconcile.assert_not_called()
        migration.done.return_value = True
        gateway._repair_member_memory_once()
        gateway.vector_memory.backfill_missing_embeddings.assert_called_once()
        reconcile.assert_called_once_with(gateway.vector_memory)
    finally:
        gateway._auto_migrate_task = None
        gateway._stop_memory_startup()


def test_global_repair_readiness_failure_does_not_starve_a_named_store(env, monkeypatch):
    gateway = _gateway(monkeypatch)
    gateway._memory_startup.fail_store("default", ValueError("Global restore failed"))
    gateway._memory_startup.complete()
    named = env.tiers["member-bob"]
    monkeypatch.setattr(
        "kiro_crew.context.cached_vector_store_entries", lambda: (("member-bob", named),)
    )
    monkeypatch.setattr("kiro_crew.slack.gateway.peek_ready_shared_embedder", lambda: object())
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.reembed_progress", lambda: SimpleNamespace(is_active=lambda: False)
    )
    monkeypatch.setattr("kiro_crew.slack.gateway.reconcile_store_embedding_space", lambda store: 0)
    monkeypatch.setattr(named, "has_pending_embeddings", lambda: True)
    repair = MagicMock()
    monkeypatch.setattr(named, "backfill_missing_embeddings", repair)
    try:
        with pytest.raises(MemoryStartupUnavailable, match="Global restore failed"):
            gateway._repair_member_memory_once()
        gateway._repair_member_memory_once()
        repair.assert_called_once()
    finally:
        gateway._stop_memory_startup()


def test_bounded_repair_retries_failed_rows_and_later_writes(env):
    tier = env.tiers["member-alice"]
    for key in ("project.a", "project.b"):
        tier.set_semantic(key, "stored", 1.0, "user_explicit")
    vector = [1.0] + [0.0] * (tier._embedding_dim - 1)
    tier.embed_fn = lambda text: None if "project.a" in text else vector
    assert tier._backfill_semantic_kv_embeddings(pace=False, max_rows=1) == 0
    assert tier._backfill_semantic_kv_embeddings(pace=False, max_rows=1) == 1
    assert tier.get_semantic("project.a")["embedding"] is None
    assert tier.get_semantic("project.b")["embedding"] is not None
    tier.embed_fn = lambda text: vector
    assert tier._backfill_semantic_kv_embeddings(pace=False, max_rows=1) == 1
    tier.set_semantic_if_absent("project.c", "later seed", 1.0, "user_explicit")
    assert tier.get_semantic("project.c")["embedding"] is None
    assert tier._backfill_semantic_kv_embeddings(pace=False, max_rows=1) == 1
    assert tier.get_semantic("project.c")["embedding"] is not None


def test_stopped_inference_never_commits_a_late_vector(env):
    tier = env.tiers["member-alice"]
    tier.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
    stopped = threading.Event()

    def embed(text):
        stopped.set()
        return [1.0] + [0.0] * (tier._embedding_dim - 1)

    tier.embed_fn = embed
    assert (
        tier._backfill_semantic_kv_embeddings(pace=False, max_rows=16, should_stop=stopped.is_set)
        == 0
    )
    assert tier.get_semantic("project.database")["embedding"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["preferences", "projects", "history", "context_preview"])
async def test_unreadable_private_tier_returns_named_503_before_access(env, surface):
    from kiro_crew.dashboard.handlers import memory as handlers

    private = env.tiers["member-alice"]
    private.close()
    private._db_path.write_bytes(b"invalid database header")
    before = {
        name: tier.db.total_changes for name, tier in env.tiers.items() if name != "member-alice"
    }
    handler = getattr(handlers, f"api_memory_{surface}")
    response = await handler(request(env, owner=True, query={"store": "member-alice"}))
    assert response.status == 503
    body = json.loads(response.text)
    assert body["code"] == "store_unavailable"
    assert "member-alice" in body["error"] and "database" in body["error"]
    assert str(private._db_path) not in response.text
    assert {name: env.tiers[name].db.total_changes for name in before} == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "surface", "reader"),
    [
        ("default", "history", "read_recent_history"),
        ("member-alice", "preferences", "read_preferences"),
    ],
)
async def test_recovery_starting_after_tier_lookup_is_still_a_structured_503(
    env, monkeypatch, store, surface, reader
):
    from kiro_crew.dashboard.handlers import memory as handlers
    from kiro_crew.dashboard.handlers._shared import markdown_memory_for_store

    memory = await markdown_memory_for_store(env.state, store if store != "default" else "")
    if store == "default":
        # This API fixture wires only its vector tier; supply its real Global
        # markdown tier so the runtime guard, rather than a mock, raises.
        memory = MemoryStore()
        memory.init()
        env.state.context_builder.memory = memory
    original = getattr(memory, reader)

    def begin_recovery_then_read():
        startup = MemoryStartup.begin()
        try:
            return original()
        finally:
            startup.stop()
            startup.release()

    monkeypatch.setattr(memory, reader, begin_recovery_then_read)
    response = await getattr(handlers, f"api_memory_{surface}")(
        request(env, owner=True, query={"store": store})
    )
    assert response.status == 503
    body = json.loads(response.text)
    assert body["code"] == "store_unavailable"
    assert "restored and prepared" in body["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("document", ["preferences", "projects"])
async def test_profile_identity_failure_is_503_instead_of_invalid_content_400(
    env, monkeypatch, document
):
    from kiro_crew.dashboard.handlers import memory as handlers
    from kiro_crew.dashboard.handlers._shared import markdown_memory_for_store
    from kiro_crew.memory_stores import UnknownMemoryStore

    memory = await markdown_memory_for_store(env.state, "member-alice")
    before = memory.read_preferences(), memory.read_projects()

    def ownership_lost(*args):
        raise UnknownMemoryStore(f"Member identity changed at {memory._workspace}")

    monkeypatch.setattr(memory, "write_private_profile_validated", ownership_lost)
    req = request(
        env,
        owner=True,
        session="dashboard:ui",
        query={"store": "member-alice"},
        body={"content": "Valid ordinary guidance"},
    ).clone(method="PUT")
    response = await getattr(handlers, f"api_memory_{document}")(req)
    assert response.status == 503
    assert json.loads(response.text)["code"] == "store_unavailable"
    assert str(memory._workspace) not in response.text
    assert (memory.read_preferences(), memory.read_projects()) == before

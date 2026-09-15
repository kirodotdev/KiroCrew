"""Bounded snapshots and lifecycle-owned memory maintenance."""

import asyncio
import json
import threading
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from member_memory_helpers import env as _member_env

from kiro_crew import context, memory_backup, memory_stores
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.config.loader import KiroCrewConfig, MemoryStoreConfig
from kiro_crew.dashboard.handlers._shared import markdown_memory_for_store
from kiro_crew.heartbeat import HeartbeatService
from kiro_crew.memory import MemoryStore
from kiro_crew.vector_memory import VectorMemoryStore

env = _member_env


@pytest.mark.asyncio
async def test_private_snapshot_validates_once_but_rechecks_each_opened_file(env, monkeypatch):
    import kiro_crew.memory as module

    memory = await markdown_memory_for_store(env.state, "member-alice")
    for day in ("2026-09-01", "2026-09-02"):
        (memory._history_dir / f"{day}.md").write_text(f"Decision on {day}", encoding="utf-8")
    validate = MagicMock(wraps=memory_stores.require_memory_store)
    opened = MagicMock(wraps=module.fd_real_path)
    monkeypatch.setattr(memory_stores, "require_memory_store", validate)
    monkeypatch.setattr(module, "fd_real_path", opened)
    snapshot = memory.markdown_snapshot()
    assert len(snapshot["history"]) == 2
    validate.assert_called_once_with("member-alice")
    assert opened.call_count >= 2
    config = KiroCrewConfig.load()
    del config.agents["alice"]
    config.save()
    with pytest.raises(memory_stores.UnknownMemoryStore, match="exclusively"):
        memory.read_history_entries()
    assert validate.call_count == 2  # no admission survives into another request


def test_eviction_releases_private_resident_data_without_touching_peer_or_disk(env, monkeypatch):
    alice, bob = env.tiers["member-alice"], env.tiers["member-bob"]
    memory = MemoryStore(workspace=alice._db_path.parent, memory_version=2)
    memory.vector_store = alice
    memory._history_cache[14] = (1.0, "2026-09-08", "remembered history")
    lesson = SimpleNamespace(_lock=threading.Lock(), _cache=(1.0, ["remembered rule"]))
    monkeypatch.setattr(context, "_vector_stores", {"member-alice": alice, "member-bob": bob})
    monkeypatch.setattr(context, "_memory_stores", {"store:member-alice": memory})
    monkeypatch.setattr(context, "_lesson_stores", {"store:member-alice": lesson})
    alice._faiss_index = object()
    alice._faiss_id_map = ["retained-vector-id"]
    alice._episodic_scoring = object()
    context.release_cached_memory_store("member-alice")
    assert alice._db is None and alice._faiss_index is None and alice._episodic_scoring is None
    assert alice._faiss_id_map == []
    assert memory.vector_store is None and memory._history_cache == {} and lesson._cache is None
    assert context.cached_vector_store_entries() == (("member-bob", bob),)
    assert bob._db is not None
    assert alice._db_path.exists() and bob._db_path.exists()


@pytest.mark.asyncio
async def test_late_named_store_construction_cannot_republish_after_eviction(env, monkeypatch):
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    store = MagicMock()

    def initialize():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)

    store.init.side_effect = initialize
    monkeypatch.setattr("kiro_crew.vector_memory.VectorMemoryStore", lambda **kwargs: store)
    monkeypatch.setattr("kiro_crew.embeddings.model_file_present", lambda: False)
    monkeypatch.setattr("kiro_crew.embeddings.reconcile_store_embedding_space", lambda store: 0)
    monkeypatch.setattr(context, "_vector_stores", {})
    monkeypatch.setattr(context, "_memory_stores", {})
    monkeypatch.setattr(context, "_lesson_stores", {})
    task = asyncio.create_task(context._build_store_vectors("member-alice"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        context.release_cached_memory_store("member-alice")
        release.set()
        with pytest.raises(memory_stores.UnknownMemoryStore, match="cache changed"):
            await task
        assert context.cached_vector_store_entries() == ()
        store.close.assert_called_once()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_archived_private_store_stays_visible_but_is_not_a_routine_backup_target(env):
    config = KiroCrewConfig.load()
    del config.agents["alice"]
    config.save()
    assert "member-alice" in memory_stores.declared_store_names()
    assert memory_stores.owned_store_path("member-alice") == env.tiers["member-alice"]._db_path
    assert memory_stores.active_store_names() == ["default", "member-bob"]
    assert memory_backup._stores_to_back_up() == [
        env.tiers[""]._db_path,
        env.tiers["member-bob"]._db_path,
    ]


def test_stopping_after_an_atomic_backup_never_prunes_or_starts_another_store(env, monkeypatch):
    stopped = threading.Event()
    original = memory_backup.backup_store
    copied = []

    def copy(path, **kwargs):
        result = original(path, **kwargs)
        copied.append(result)
        stopped.set()
        return result

    monkeypatch.setattr(memory_backup, "backup_store", copy)
    prune = MagicMock()
    monkeypatch.setattr(memory_backup, "prune_backups", prune)
    result = memory_backup.back_up_all_stores(should_stop=stopped.is_set)
    assert result["backed_up"] == 1 and len(copied) == 1 and copied[0].exists()
    prune.assert_not_called()


def test_automatic_backups_preserve_v1_and_archived_backups_while_manual_v1_still_works(env):
    config = KiroCrewConfig.load()
    config.memory_stores["legacy-team"] = MemoryStoreConfig(memory_version=1)
    config.save()
    legacy = VectorMemoryStore(db_path=env.home / "memory_stores" / "legacy-team" / "memory.db")
    env.tiers["legacy-team"] = legacy
    legacy.init()
    legacy.set_semantic("project.backup_scope", "Legacy team", 1.0, "user_explicit")
    env.tiers[""].set_semantic("project.backup_scope", "Global", 1.0, "user_explicit")
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    v1_paths = [env.tiers[""]._db_path, legacy._db_path]
    for path in v1_paths:
        for days in (3, 2):
            assert memory_backup.backup_store(path, now=now - timedelta(days=days)) is not None

    archived_path = env.tiers["member-bob"]._db_path
    archived_backup = memory_backup.backup_store(archived_path, now=now - timedelta(days=2))
    assert archived_backup is not None
    config = KiroCrewConfig.load()
    del config.agents["bob"]
    config.save()
    preserved_dirs = [memory_backup.backup_dir_for(path) for path in v1_paths]
    preserved_dirs.append(archived_backup.parent)

    def preserved_inventory():
        return {
            directory: (
                directory.stat().st_mtime_ns,
                {path.name: path.read_bytes() for path in directory.iterdir()},
            )
            for directory in preserved_dirs
        }

    before = preserved_inventory()
    active_path = env.tiers["member-alice"]._db_path
    preferences = active_path.parent / "memory" / "preferences.md"
    preferences.parent.mkdir(exist_ok=True)
    preferences.write_text("Alice keeps private deployment notes", encoding="utf-8")
    result = memory_backup.back_up_all_stores(keep=1, now=now, private_only=True)
    assert result == {"backed_up": 1, "skipped": 2, "pruned": 0, "failed": 0}
    assert preserved_inventory() == before
    [active_backup] = memory_backup.list_backups(active_path)
    with zipfile.ZipFile(active_backup) as archive:
        manifest = json.loads(archive.read("snapshot-manifest.json"))
        assert manifest["store"] == "member-alice" and manifest["owner_member"] == "alice"
        assert archive.read("memory/preferences.md") == preferences.read_bytes()
        assert "memory.db" in archive.namelist()

    # The explicit all-store helper retains V1 copying and its requested retention.
    result = memory_backup.back_up_all_stores(keep=1, now=now)
    assert result == {"backed_up": 2, "skipped": 1, "pruned": 4, "failed": 0}
    for path, expected in zip(v1_paths, ("Global", "Legacy team"), strict=True):
        [backup] = memory_backup.list_backups(path)
        assert backup.name.startswith("memory.20260908T000000000000Z-")
        assert backup.name.endswith(".db") and memory_backup.snapshot_time(backup) == now
        with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as db:
            [value] = db.execute(
                "SELECT value_json FROM semantic_memory WHERE key='project.backup_scope'"
            ).fetchone()
            assert json.loads(value) == expected
    assert preserved_inventory()[archived_backup.parent] == before[archived_backup.parent]


@pytest.mark.asyncio
async def test_first_eligible_heartbeat_schedules_one_backup_without_blocking_ticks(monkeypatch):
    started, release, finished = asyncio.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    observed_stop = []
    observed_private_only = []

    def copy(keep, *, should_stop, private_only):
        observed_private_only.append(private_only)
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5)
            observed_stop.append(should_stop())
            return {"backed_up": 1, "skipped": 0, "pruned": 0, "failed": 0}
        finally:
            finished.set()

    monkeypatch.setattr(memory_backup, "back_up_all_stores", copy)
    monkeypatch.setattr(
        "kiro_crew.heartbeat.KiroCrewConfig.load",
        lambda: SimpleNamespace(memory=SimpleNamespace(backup_enabled=True, backup_keep=7)),
    )
    consolidator = MagicMock()
    service = HeartbeatService(MagicMock(), consolidator=consolidator)
    service._process_heartbeat_file = AsyncMock()
    service._tick = 1
    try:
        await service._beat()
        await asyncio.wait_for(started.wait(), 5)
        assert observed_private_only == [True]
        task = service._memory_backup_task
        service._tick = 30
        await service._beat()
        assert service._memory_backup_task is task
        assert consolidator.check_idle_sessions.call_count == 2
        service.stop()
        await asyncio.gather(task, return_exceptions=True)
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        assert observed_stop == [True]
        service._schedule_memory_backup()
        assert service._memory_backup_task is task
    finally:
        service.stop()
        release.set()
        if service._memory_backup_task is not None:
            await asyncio.gather(service._memory_backup_task, return_exceptions=True)

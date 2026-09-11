"""A failed restore fences its own store while peers finish the same startup pass."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import request
from test_memory_startup import _gateway

from kiro_crew import member_memory_backup, memory_backup, memory_stores
from kiro_crew.config import loader
from kiro_crew.dashboard.handlers import memory_admin, memory_member
from kiro_crew.dashboard.handlers._shared import markdown_memory_for_store
from kiro_crew.heartbeat import HeartbeatService
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_startup import MemoryStartup, MemoryStartupUnavailable, require_memory_ready
from kiro_crew.vector_memory import VectorMemoryStore

env = _member_env


def _real_gateway(env, monkeypatch):
    gateway = _gateway(monkeypatch)
    memory = MemoryStore()
    memory.vector_store = env.tiers[""]
    gateway.ctx_builder = SimpleNamespace(memory=memory)
    gateway.vector_memory = env.tiers[""]
    env.state.context_builder = gateway.ctx_builder
    return gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["changed_stage", "install_failure"])
@pytest.mark.parametrize("restage", [False, True])
async def test_failed_member_does_not_block_later_restore_global_or_owner_recovery(
    env, monkeypatch, failure, restage
):
    backups = {}
    for name, tier in env.tiers.items():
        tier.set_semantic("project.restore", f"snapshot-{name}", 1.0, "user_explicit")
        if name:
            backups[name] = memory_backup.backup_store(tier._db_path)
            memory_backup.restore_from_backup(backups[name], name)
            tier.set_semantic("project.restore", f"live-{name}", 1.0, "user_explicit")
        tier.close()  # Startup activates only after old WAL writers have closed.
    alice = env.tiers["member-alice"]
    bob = env.tiers["member-bob"]
    out = backups["member-alice"].parent
    journal = json.loads((out / member_memory_backup.PENDING).read_text())
    stage = out / journal["stage"]
    if failure == "changed_stage":
        (stage / "memory.db").write_bytes(b"changed after staging")
    gateway = _real_gateway(env, monkeypatch)
    try:
        with monkeypatch.context() as patcher:
            if failure == "install_failure":
                replace = member_memory_backup.replace_with_retry

                def fail_alice_install(source, target):
                    if source == stage and target == alice._db_path.parent:
                        raise OSError("Alice staged directory could not be installed")
                    return replace(source, target)

                patcher.setattr(member_memory_backup, "replace_with_retry", fail_alice_install)
            await gateway._wait_for_memory_preparation()
            gateway._start_memory_after_ready()
        await gateway._auto_migrate_task
        await gateway._memory_repair_task
        assert gateway._memory_startup.ready
        assert set(gateway._memory_startup.store_errors) == {"member-alice"}
        bob.init()
        assert (
            json.loads(bob.get_semantic("project.restore")["value_json"]) == "snapshot-member-bob"
        )
        assert (
            json.loads(env.tiers[""].get_semantic("project.restore")["value_json"]) == "snapshot-"
        )
        assert not memory_backup.pending_restore_status(bob._db_path)["pending"]
        assert not memory_backup.pending_restore_status(env.tiers[""]._db_path)["pending"]
        for read in (alice.init, lambda: alice.get_semantic("project.restore")):
            with pytest.raises(MemoryStartupUnavailable, match="member-alice"):
                read()
        for name, expected in (("default", 200), ("member-alice", 503), ("member-bob", 200)):
            response = await memory_member.api_memory_recall(
                request(env, owner=True, query={"store": name, "q": "project restore"})
            )
            assert response.status == expected
        status = await memory_admin.api_memory_backups(
            request(env, owner=True, query={"store": "member-alice"})
        )
        body = json.loads(status.text)
        assert status.status == 200 and body["pending_restore"]
        assert body["activation_failed"] and body["restore_error"] and body["restart_required"]
        assert str(env.home) not in status.text

        # Cancel is owner recovery, not permission to read the retained old DB.
        cancelled = await memory_admin.api_memory_restore_cancel(
            request(env, owner=True, body={"store": "member-alice"})
        )
        assert cancelled.status == 200
        cancellation = json.loads(cancelled.text)
        assert cancellation["cancelled"] and not cancellation["pending"]
        assert cancellation["activation_failed"] and cancellation["restart_required"]
        refreshed = await memory_admin.api_memory_backups(
            request(env, owner=True, query={"store": "member-alice"})
        )
        assert json.loads(refreshed.text)["restore_error"] == cancellation["restore_error"]
        if restage:
            # Staging must also recover an unreadable current DB: it validates
            # the owner and saved copy without reopening the failed live file.
            alice._db_path.write_bytes(b"corrupt prior memory")
            response = await memory_admin.api_memory_restore(
                request(
                    env,
                    owner=True,
                    body={"store": "member-alice", "name": backups["member-alice"].name},
                )
            )
            assert response.status == 200 and json.loads(response.text)["pending"]
        with pytest.raises(MemoryStartupUnavailable, match="member-alice"):
            alice.init()
        assert (
            json.loads(bob.get_semantic("project.restore")["value_json"]) == "snapshot-member-bob"
        )
    finally:
        await asyncio.to_thread(gateway._stop_memory_startup)
        for tier in env.tiers.values():
            tier.close()

    successor = _real_gateway(env, monkeypatch)
    try:
        assert await asyncio.to_thread(successor._initialize_memory_worker)
        assert successor._memory_startup.store_errors == {}
        alice.init()
        expected = "snapshot-member-alice" if restage else "live-member-alice"
        assert json.loads(alice.get_semantic("project.restore")["value_json"]) == expected
        assert not memory_backup.pending_restore_status(alice._db_path)["restart_required"]
    finally:
        await asyncio.to_thread(successor._stop_memory_startup)


@pytest.mark.asyncio
async def test_uncorrected_journal_rebuilds_the_same_store_fence_after_restart(env, monkeypatch):
    alice = env.tiers["member-alice"]
    backup = memory_backup.backup_store(alice._db_path)
    memory_backup.restore_from_backup(backup, "member-alice")
    pending = backup.parent / member_memory_backup.PENDING
    journal = json.loads(pending.read_text())
    (backup.parent / journal["stage"] / "memory.db").write_bytes(b"invalid staged database")
    for tier in env.tiers.values():
        tier.close()
    for _ in range(2):
        gateway = _real_gateway(env, monkeypatch)
        try:
            assert await asyncio.to_thread(gateway._initialize_memory_worker)
            assert set(gateway._memory_startup.store_errors) == {"member-alice"}
            require_memory_ready()
            env.tiers["member-bob"].init()
            assert env.tiers["member-bob"].get_all_semantic() == []
            with pytest.raises(MemoryStartupUnavailable, match="member-alice"):
                alice.init()
            assert pending.exists()
        finally:
            await asyncio.to_thread(gateway._stop_memory_startup)
            for tier in env.tiers.values():
                tier.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preparing", "fatal", "stopped"])
async def test_owner_restore_stage_still_refuses_an_unfinished_or_stopped_pass(env, phase):
    alice = env.tiers["member-alice"]
    backup = memory_backup.backup_store(alice._db_path)
    startup = MemoryStartup.begin()
    try:
        startup.fail_store("member-alice", ValueError("member activation failed"))
        if phase == "fatal":
            startup.fail(ValueError("configuration could not be read"))
        elif phase == "stopped":
            startup.stop()
        response = await memory_admin.api_memory_restore(
            request(env, owner=True, body={"store": "member-alice", "name": backup.name})
        )
        assert response.status == 503
        assert not memory_backup.pending_restore_status(alice._db_path)["pending"]
    finally:
        startup.stop()
        startup.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["restore", "initialize"])
async def test_failed_global_skips_its_open_or_migration_but_members_remain_usable(
    env, monkeypatch, failure
):
    gateway = _gateway(monkeypatch)
    if failure == "restore":

        def restore(**kwargs):
            kwargs["on_error"]("default", ValueError("Global restore failed"))
            return {}

        monkeypatch.setattr(memory_backup, "apply_pending_member_restores", restore)
    else:
        monkeypatch.setattr(memory_backup, "apply_pending_member_restores", lambda **kwargs: {})
        gateway.vector_memory.init.side_effect = OSError("Global database could not be opened")
    try:
        await gateway._wait_for_memory_preparation()
        gateway._start_memory_after_ready()
        await gateway._memory_repair_task
        assert gateway._memory_startup.ready
        assert set(gateway._memory_startup.store_errors) == {"default"}
        assert gateway._auto_migrate_task is None
        gateway._auto_migrate_memory.assert_not_awaited()
        gateway._repair_member_memory.assert_awaited_once()
        if failure == "restore":
            gateway.ctx_builder.memory.init.assert_not_called()
            gateway.vector_memory.init.assert_not_called()
        with pytest.raises(MemoryStartupUnavailable, match="default"):
            env.tiers[""].get_semantic("project.database")
        for name in ("member-alice", "member-bob"):
            env.tiers[name].set_semantic("project.healthy", name, 1.0, "user_explicit")
            assert env.tiers[name].get_semantic("project.healthy")
            memory = await markdown_memory_for_store(env.state, name)
            memory.write_preferences("Keep this member's guidance")
            assert memory.read_preferences() == "Keep this member's guidance"
        result = memory_backup.back_up_all_stores()
        assert result["failed"] == 1 and result["backed_up"] == 2

        service = HeartbeatService(MemoryStore())
        service._tick = 1
        service._back_up_memory = AsyncMock()
        service._process_heartbeat_file = AsyncMock()
        try:
            await service._beat()
            await service._memory_backup_task
            service._back_up_memory.assert_awaited_once()
            service._process_heartbeat_file.assert_not_awaited()
        finally:
            service.stop()
    finally:
        await asyncio.to_thread(gateway._stop_memory_startup)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ["default", "legacy-team"])
async def test_named_v1_markdown_vectors_and_factories_keep_their_own_startup_identity(env, failed):
    config = loader.KiroCrewConfig.load()
    config.memory_stores["legacy-team"] = loader.MemoryStoreConfig(memory_version=1)
    config.save()
    memory_stores._DECLARED_MEMO = None
    path = env.home / "memory_stores" / "legacy-team" / "memory.db"
    vector = VectorMemoryStore(db_path=path)
    vector.init()
    markdown = MemoryStore(workspace=path.parent)
    markdown.init()
    markdown.write_preferences("Legacy team guidance")
    startup = MemoryStartup.begin()
    try:
        startup.fail_store(failed, ValueError("Recovery needs owner action"))
        assert startup.complete()
        if failed == "default":
            assert markdown.read_preferences() == "Legacy team guidance"
            assert vector.get_all_semantic() == []
            assert await markdown_memory_for_store(env.state, "legacy-team")
            memory_stores.ensure_memory_store_dir("legacy-team")
            with pytest.raises(MemoryStartupUnavailable):
                require_memory_ready()
        else:
            require_memory_ready()
            for action in (
                markdown.init,
                markdown.read_preferences,
                markdown.rebuild_index,
                markdown.index_row_count,
                lambda: markdown.search("guidance"),
                lambda: markdown.write_preferences("Must not replace guidance"),
                lambda: vector.get_all_semantic(),
                lambda: memory_stores.ensure_memory_store_dir("legacy-team"),
            ):
                with pytest.raises(MemoryStartupUnavailable, match="legacy-team"):
                    action()
            with pytest.raises(MemoryStartupUnavailable):
                await markdown_memory_for_store(env.state, "legacy-team")
        assert markdown._preferences_file.read_text() == "Legacy team guidance"
    finally:
        startup.stop()
        startup.release()
        vector.close()

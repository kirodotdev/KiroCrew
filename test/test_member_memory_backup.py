"""Complete V2 snapshots and recoverable startup-only activation."""

from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from member_memory_helpers import MEMBERS, forget_declared_stores, write_member_home

from kiro_crew import member_memory_backup as member_backup
from kiro_crew import memory_backup, memory_stores
from kiro_crew.config import loader
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.xdist_group("member_memory_backup")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    write_member_home(tmp_path, *MEMBERS)
    forget_declared_stores(monkeypatch)
    paths = {
        owner: tmp_path / "memory_stores" / f"member-{owner}" / "memory.db" for owner in MEMBERS
    }
    tiers = {}
    for owner, path in paths.items():
        tier = VectorMemoryStore(db_path=path)
        tier.init()
        tier.set_semantic("project.database", "PostgreSQL", 1.0, "user_explicit")
        tiers[owner] = tier
    home = paths["alice"].parent
    (home / "memory" / "history").mkdir(parents=True)
    (home / "memory" / "preferences.md").write_text("Use concise answers", encoding="utf-8")
    (home / "memory" / "projects.md").write_text("Project lantern", encoding="utf-8")
    (home / "memory" / "history" / "2026-09-07.md").write_text(
        "Initial deployment", encoding="utf-8"
    )
    (home / "lessons.jsonl").write_text('{"rule":"Check backups"}\n', encoding="utf-8")
    try:
        yield SimpleNamespace(paths=paths, tiers=tiers, home=home, root=tmp_path)
    finally:
        for tier in tiers.values():
            tier.close()
        loader._invalidate_config_cache()


def read_value(path):
    tier = VectorMemoryStore(db_path=path)
    tier.init()
    try:
        return json.loads(tier.get_semantic("project.database")["value_json"])
    finally:
        tier.close()


def test_complete_private_snapshot_includes_wal_and_all_memory_layers(env):
    backup = memory_backup.backup_store(env.paths["alice"])
    assert backup.suffix == ".zip"
    assert backup.parent == env.root / "memory_stores" / ".member-backups" / "member-alice"
    with zipfile.ZipFile(backup) as archive:
        assert set(archive.namelist()) == {
            "snapshot-manifest.json",
            "memory.db",
            "memory/preferences.md",
            "memory/projects.md",
            "memory/history/2026-09-07.md",
            "lessons.jsonl",
        }
        manifest = json.loads(archive.read("snapshot-manifest.json"))
        assert manifest["owner_member"] == "alice"
        assert manifest["store"] == "member-alice"
        assert all(
            hashlib.sha256(archive.read(name)).hexdigest() == digest
            for name, digest in manifest["files"].items()
        )
        assert archive.read("memory/preferences.md") == b"Use concise answers"


def test_journal_permission_failure_precedes_content_and_allows_retry(env, monkeypatch):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    out = backup.parent
    real_restrict = member_backup.platform_compat.restrict_to_owner
    sizes_at_restriction = []

    def refuse_journal_permission(candidate):
        candidate = Path(candidate)
        if candidate.parent == out and candidate.suffix in (".tmp", ".partial"):
            sizes_at_restriction.append(candidate.stat().st_size)
            raise OSError("injected journal permission failure")
        return real_restrict(candidate)

    with monkeypatch.context() as patcher:
        patcher.setattr(
            member_backup.platform_compat, "restrict_to_owner", refuse_journal_permission
        )
        with pytest.raises(OSError, match="journal permission failure"):
            member_backup.stage_restore(backup, path)
    assert sizes_at_restriction == [0]
    assert not member_backup.pending_restore_status(path)["pending"]
    assert not list(out.glob("restore-*"))
    assert not list(out.glob("*.tmp")) and not list(out.glob("*.partial"))
    env.tiers["alice"].set_semantic("project.database", "SQLite", 1.0, "user_explicit")
    assert read_value(path) == "SQLite" and read_value(env.paths["bob"]) == "PostgreSQL"
    member_backup.stage_restore(backup, path)
    assert member_backup.pending_restore_status(path)["pending"]
    assert read_value(path) == "SQLite" and backup.is_file()


def test_cli_stages_lost_member_directory_recovery_without_touching_another_member(env, capsys):
    from kiro_crew.cli_commands import _memory_backup_cmd

    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    env.tiers["alice"].close()
    env.home.rename(env.root / "lost-alice")
    _memory_backup_cmd("restore", SimpleNamespace(store="member-alice", from_backup=str(backup)))
    output = capsys.readouterr().out
    assert "Staged restore" in output
    assert "Restart the gateway" in output
    assert not env.home.exists()
    journal = json.loads((backup.parent / member_backup.PENDING).read_text())
    assert journal["prior_existed"] is False
    assert memory_backup.apply_pending_member_restores() == {"member-alice": ""}
    assert read_value(path) == "PostgreSQL"
    assert (env.home / "memory" / "preferences.md").read_text() == "Use concise answers"
    assert read_value(env.paths["bob"]) == "PostgreSQL"
    assert not (backup.parent / journal["aside"]).exists()


def test_lost_directory_restore_recovers_crash_after_install_without_prior_tree(env):
    backup = memory_backup.backup_store(env.paths["alice"])
    env.tiers["alice"].close()
    env.home.rename(env.root / "lost-alice")
    memory_backup.restore_from_backup(backup, "member-alice")
    journal = json.loads((backup.parent / member_backup.PENDING).read_text())
    (backup.parent / journal["stage"]).rename(env.home)
    assert memory_backup.apply_pending_member_restores() == {"member-alice": ""}
    assert read_value(env.paths["alice"]) == "PostgreSQL"


def test_existing_directory_with_missing_owner_marker_is_not_adopted_by_restore(env):
    backup = memory_backup.backup_store(env.paths["alice"])
    (env.home / memory_stores.MEMBER_MEMORY_MANIFEST).unlink()
    with pytest.raises(ValueError, match="metadata"):
        memory_backup.restore_from_backup(backup, "member-alice")
    assert not (backup.parent / member_backup.PENDING).exists()


def test_corrupt_private_database_can_be_restored_without_opening_it(env):
    backup = memory_backup.backup_store(env.paths["alice"])
    env.tiers["alice"].close()
    env.paths["alice"].write_bytes(b"corrupt database")
    memory_backup.restore_from_backup(backup, "member-alice")
    assert env.paths["alice"].read_bytes() == b"corrupt database"
    applied = memory_backup.apply_pending_member_restores()
    assert read_value(env.paths["alice"]) == "PostgreSQL"
    assert (
        backup.parent / applied["member-alice"] / "memory.db"
    ).read_bytes() == b"corrupt database"


def test_restore_is_pending_until_explicit_startup_barrier_and_preserves_old_tree(env):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    env.tiers["alice"].set_semantic("project.database", "SQLite", 1.0, "user_explicit")
    (env.home / "memory" / "preferences.md").write_text("Long answers", encoding="utf-8")
    (env.home / "memory" / "history" / "2026-09-08.md").write_text(
        "A later memory", encoding="utf-8"
    )
    memory_backup.restore_from_backup(backup, "member-alice")
    assert json.loads(env.tiers["alice"].get_semantic("project.database")["value_json"]) == "SQLite"
    assert (env.home / "memory" / "preferences.md").read_text() == "Long answers"
    env.tiers["alice"].close()
    # A CLI/read-only inspection constructing a vector store is not startup.
    assert read_value(path) == "SQLite"
    restored = memory_backup.apply_pending_member_restores()
    assert set(restored) == {"member-alice"}
    assert read_value(path) == "PostgreSQL"
    assert (env.home / "memory" / "preferences.md").read_text() == "Use concise answers"
    assert not (env.home / "memory" / "history" / "2026-09-08.md").exists()
    prior = backup.parent / restored["member-alice"]
    assert (prior / "memory" / "preferences.md").read_text() == "Long answers"
    assert (prior / "memory" / "history" / "2026-09-08.md").read_text() == "A later memory"
    assert memory_backup.list_backups(path) == [backup]
    assert memory_backup.apply_pending_member_restores() == {}


def test_foreign_member_snapshot_cannot_be_restored(env):
    backup = memory_backup.backup_store(env.paths["alice"])
    with pytest.raises(ValueError, match="another member"):
        memory_backup.restore_from_backup(backup, "member-bob")
    assert not (member_backup.backup_directory(env.paths["bob"]) / member_backup.PENDING).exists()
    assert env.tiers["bob"].get_semantic("project.database") is not None


def rewrite_bundle(source, target, mutate):
    with zipfile.ZipFile(source) as original:
        content = {name: original.read(name) for name in original.namelist()}
    mutate(content)
    with zipfile.ZipFile(target, "w") as modified:
        for name, data in content.items():
            # ZipInfo's Windows constructor normalizes backslashes. Set the
            # raw archive spelling explicitly to exercise hostile ZIP input.
            info = zipfile.ZipInfo("placeholder")
            info.filename = name
            info.orig_filename = name
            modified.writestr(info, data)


@pytest.mark.parametrize(
    "key,value",
    [("private_memory_version", "broken"), ("owner_member", "bob"), ("owner_member", None)],
)
def test_snapshot_private_marker_is_checked_before_displacing_current_memory(env, key, value):
    backup = memory_backup.backup_store(env.paths["alice"])
    damaged = backup.parent / "damaged-identity.zip"

    def mutate(content):
        database = env.root / "edited-snapshot.db"
        database.write_bytes(content["memory.db"])
        with closing(member_backup.sqlite3.connect(database)) as db, db:
            if value is None:
                db.execute("DELETE FROM memory_meta WHERE key=?", (key,))
            else:
                db.execute("UPDATE memory_meta SET value=? WHERE key=?", (value, key))
        content["memory.db"] = database.read_bytes()
        manifest = json.loads(content[member_backup.MANIFEST])
        manifest["files"]["memory.db"] = hashlib.sha256(content["memory.db"]).hexdigest()
        content[member_backup.MANIFEST] = json.dumps(manifest).encode()

    rewrite_bundle(backup, damaged, mutate)
    with pytest.raises(ValueError, match="private database ownership"):
        memory_backup.restore_from_backup(damaged, "member-alice")
    assert not (backup.parent / member_backup.PENDING).exists()
    assert not list(backup.parent.glob("superseded-*"))
    assert read_value(env.paths["alice"]) == "PostgreSQL"


def test_snapshot_before_durable_private_marker_remains_restorable(env):
    backup = memory_backup.backup_store(env.paths["alice"])
    legacy = backup.parent / "legacy-identity.zip"

    def mutate(content):
        database = env.root / "legacy-snapshot.db"
        database.write_bytes(content["memory.db"])
        with closing(member_backup.sqlite3.connect(database)) as db, db:
            db.execute(
                "DELETE FROM memory_meta WHERE key IN ('private_memory_version', 'owner_member')"
            )
        content["memory.db"] = database.read_bytes()
        manifest = json.loads(content[member_backup.MANIFEST])
        manifest["files"]["memory.db"] = hashlib.sha256(content["memory.db"]).hexdigest()
        content[member_backup.MANIFEST] = json.dumps(manifest).encode()

    rewrite_bundle(backup, legacy, mutate)
    memory_backup.restore_from_backup(legacy, "member-alice")
    env.tiers["alice"].close()
    memory_backup.apply_pending_member_restores()
    assert read_value(env.paths["alice"]) == "PostgreSQL"


@pytest.mark.parametrize(
    "unsafe", ["../escape.md", "/absolute.md", "memory/../../escape.md", "memory\\preferences.md"]
)
def test_unsafe_bundle_paths_are_refused_before_activation(env, unsafe):
    backup = memory_backup.backup_store(env.paths["alice"])
    bad = backup.parent / "hostile.zip"

    def mutate(content):
        content[unsafe] = b"hostile"
        manifest = json.loads(content[member_backup.MANIFEST])
        manifest["files"][unsafe] = hashlib.sha256(b"hostile").hexdigest()
        content[member_backup.MANIFEST] = json.dumps(manifest).encode()

    rewrite_bundle(backup, bad, mutate)
    with pytest.raises(ValueError, match="unsafe path"):
        memory_backup.restore_from_backup(bad, "member-alice")
    assert not (backup.parent / member_backup.PENDING).exists()
    assert not (env.root / "escape.md").exists()


def test_checksum_corruption_is_refused_without_changing_live_data(env):
    backup = memory_backup.backup_store(env.paths["alice"])
    bad = backup.parent / "corrupt.zip"
    rewrite_bundle(
        backup, bad, lambda content: content.update({"memory/preferences.md": b"tampered"})
    )
    with pytest.raises(ValueError, match="checksum"):
        memory_backup.restore_from_backup(bad, "member-alice")
    assert (env.home / "memory" / "preferences.md").read_text() == "Use concise answers"


def test_archive_symlink_is_refused(env):
    backup = memory_backup.backup_store(env.paths["alice"])
    bad = backup.parent / "symlink.zip"
    with zipfile.ZipFile(backup) as original, zipfile.ZipFile(bad, "w") as rewritten:
        for info in original.infolist():
            if info.filename == "memory/preferences.md":
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            rewritten.writestr(info, original.read(info.filename))
    with pytest.raises(ValueError, match="links"):
        memory_backup.restore_from_backup(bad, "member-alice")


def test_restore_rename_failure_rolls_back_and_can_be_retried(env, monkeypatch):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    env.tiers["alice"].set_semantic("project.database", "SQLite", 1.0, "user_explicit")
    memory_backup.restore_from_backup(backup, "member-alice")
    env.tiers["alice"].close()
    original = member_backup.replace_with_retry

    def fail_switch(source, target, *args, **kwargs):
        if source.name.startswith("restore-"):
            raise OSError("injected switch failure")
        return original(source, target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(member_backup, "replace_with_retry", fail_switch)
        with pytest.raises(memory_backup.MemoryBackupFailed, match="Previous memory is preserved"):
            memory_backup.apply_pending_member_restores()
    assert read_value(path) == "SQLite"
    memory_backup.apply_pending_member_restores()
    assert read_value(path) == "PostgreSQL"


def test_pending_restore_refuses_an_open_v2_generation(env):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    env.tiers["alice"].set_semantic("project.database", "SQLite", 1.0, "user_explicit")
    memory_backup.restore_from_backup(backup, "member-alice")

    with pytest.raises(memory_backup.MemoryBackupFailed, match="Previous memory is preserved"):
        memory_backup.apply_pending_member_restores()
    value = env.tiers["alice"].get_semantic("project.database")
    assert json.loads(value["value_json"]) == "SQLite"
    assert member_backup.pending_restore_status(path)["pending"]

    env.tiers["alice"].close()
    assert "member-alice" in memory_backup.apply_pending_member_restores()
    assert read_value(path) == "PostgreSQL"


@pytest.mark.asyncio
async def test_markdown_only_private_open_holds_restore_admission(env):
    from kiro_crew.context import cached_vector_store_entries, release_cached_memory_store
    from kiro_crew.dashboard.handlers._shared import (
        markdown_memory_for_store,
        release_markdown_memory_store,
    )

    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    env.tiers["alice"].set_semantic("project.database", "SQLite", 1.0, "user_explicit")
    memory_backup.restore_from_backup(backup, "member-alice")
    env.tiers["alice"].close()
    state = SimpleNamespace()

    memory = await markdown_memory_for_store(state, "member-alice")
    memory.write_preferences("Keep the live generation")
    assert "member-alice" in dict(cached_vector_store_entries())
    try:
        with pytest.raises(memory_backup.MemoryBackupFailed, match="Previous memory is preserved"):
            memory_backup.apply_pending_member_restores()
        assert memory.read_preferences() == "Keep the live generation"
        assert member_backup.pending_restore_status(path)["pending"]
    finally:
        await release_markdown_memory_store(state, "member-alice")
        release_cached_memory_store("member-alice")

    assert "member-alice" in memory_backup.apply_pending_member_restores()
    assert read_value(path) == "PostgreSQL"


def test_failed_v2_init_releases_its_store_use_lock(env, monkeypatch):
    tier = VectorMemoryStore(db_path=env.paths["alice"])
    released = []
    monkeypatch.setattr(member_backup, "acquire_store_use_lock", lambda path: 123)
    monkeypatch.setattr(member_backup, "release_store_use_lock", released.append)
    monkeypatch.setattr(
        tier,
        "_init_database",
        lambda: (_ for _ in ()).throw(OSError("bad db")),
    )

    with pytest.raises(OSError, match="bad db"):
        tier.init()
    assert released == [123]
    assert tier._store_use_lock_fd is None


def test_startup_recovers_crash_between_directory_renames(env):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    memory_backup.restore_from_backup(backup, "member-alice")
    env.tiers["alice"].close()
    journal = json.loads((backup.parent / member_backup.PENDING).read_text())
    aside = backup.parent / journal["aside"]
    assert aside.resolve().is_relative_to(env.root.resolve())
    assert path.parent.resolve().is_relative_to(env.root.resolve())
    member_backup.replace_with_retry(path.parent, aside)
    assert not path.exists()
    assert memory_backup.apply_pending_member_restores()["member-alice"] == aside.name
    assert read_value(path) == "PostgreSQL"


def test_staged_content_tampering_refuses_startup(env):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    memory_backup.restore_from_backup(backup, "member-alice")
    journal = json.loads((backup.parent / member_backup.PENDING).read_text())
    stage = backup.parent / journal["stage"]
    (stage / "memory" / "preferences.md").write_text("tampered", encoding="utf-8")
    env.tiers["alice"].close()
    with pytest.raises(memory_backup.MemoryBackupFailed, match="content changed"):
        memory_backup.apply_pending_member_restores()
    assert (env.home / "memory" / "preferences.md").read_text() == "Use concise answers"


def test_startup_recovers_crash_after_switch_before_journal_cleanup(env, monkeypatch):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    memory_backup.restore_from_backup(backup, "member-alice")
    env.tiers["alice"].close()
    pending = backup.parent / member_backup.PENDING
    original = Path.unlink

    def fail_cleanup(self, *args, **kwargs):
        if self == pending:
            raise OSError("injected crash before journal cleanup")
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_cleanup)
        with pytest.raises(memory_backup.MemoryBackupFailed):
            memory_backup.apply_pending_member_restores()
    assert pending.exists()
    assert "member-alice" in memory_backup.apply_pending_member_restores()
    assert not pending.exists()
    assert read_value(path) == "PostgreSQL"


def test_second_restore_does_not_replace_pending_owner_choice(env):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    memory_backup.restore_from_backup(backup, "member-alice")
    pending = backup.parent / member_backup.PENDING
    first = pending.read_bytes()
    with pytest.raises(ValueError, match="already pending"):
        memory_backup.restore_from_backup(backup, "member-alice")
    assert pending.read_bytes() == first


def test_private_zip_retention_is_per_store(env):
    stamp = datetime(2026, 9, 7, tzinfo=timezone.utc)
    for offset in range(3):
        memory_backup.backup_store(env.paths["alice"], now=stamp + timedelta(days=offset))
    bob = memory_backup.backup_store(env.paths["bob"], now=stamp)
    assert memory_backup.prune_backups(env.paths["alice"], keep=2) == 1
    assert len(memory_backup.list_backups(env.paths["alice"])) == 2
    assert memory_backup.list_backups(env.paths["bob"]) == [bob]


def test_pending_status_cancel_and_retry_preserve_live_memory_and_backup(env):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    member_backup.stage_restore(backup, path)
    state = member_backup.pending_restore_status(path)
    assert state["pending"] and state["restart_required"]
    assert state["pending_restore"]["backup_name"] == backup.name
    assert state["pending_restore"]["staged_at"]
    env.tiers["alice"].set_semantic("project.database", "SQLite", 1.0, "user_explicit")
    assert member_backup.cancel_pending_restore(path)
    assert member_backup.pending_restore_status(path) == {
        "pending": False,
        "restart_required": False,
        "pending_restore": None,
    }
    assert not member_backup.cancel_pending_restore(path)
    assert backup.exists()
    assert read_value(path) == "SQLite"
    assert member_backup.apply_pending_restore(path) is None
    member_backup.stage_restore(backup, path)
    assert member_backup.pending_restore_status(path)["pending"]


def test_cancel_refuses_owner_mismatch_or_started_activation(env):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    member_backup.stage_restore(backup, path)
    out = member_backup.backup_directory(path)
    pending = out / member_backup.PENDING
    journal = json.loads(pending.read_text())
    journal["owner_member"] = "bob"
    pending.write_text(json.dumps(journal))
    with pytest.raises(ValueError, match="ownership"):
        member_backup.cancel_pending_restore(path)
    journal["owner_member"] = "alice"
    pending.write_text(json.dumps(journal))
    (out / journal["aside"]).mkdir()
    with pytest.raises(ValueError, match="activation has started"):
        member_backup.cancel_pending_restore(path)
    assert pending.exists() and backup.exists()
    assert read_value(path) == "PostgreSQL"


def test_cancel_commits_before_best_effort_stage_cleanup(env, monkeypatch):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    member_backup.stage_restore(backup, path)
    with monkeypatch.context() as patch:
        patch.setattr(
            member_backup.shutil, "rmtree", lambda *_: (_ for _ in ()).throw(OSError("busy"))
        )
        assert member_backup.cancel_pending_restore(path)
    assert member_backup.apply_pending_restore(path) is None
    assert member_backup.pending_restore_status(path)["pending"] is False
    assert backup.exists() and read_value(path) == "PostgreSQL"


def test_cli_cancel_reports_unchanged_active_memory(env, capsys):
    from kiro_crew.cli_commands import _memory_backup_cmd

    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    member_backup.stage_restore(backup, path)
    _memory_backup_cmd("restore", SimpleNamespace(store="member-alice", cancel_pending=True))
    assert "Staged restore cancelled" in capsys.readouterr().out
    assert not member_backup.pending_restore_status(path)["pending"]
    assert backup.exists()


def test_same_timestamp_snapshots_never_replace_prior_backup(env):
    stamp = datetime(2026, 9, 7, 12, 34, 56, tzinfo=timezone.utc)
    first = memory_backup.backup_store(env.paths["alice"], now=stamp)
    original = first.read_bytes()
    env.tiers["alice"].set_semantic("project.database", "SQLite", 1.0, "user_explicit")
    second = memory_backup.backup_store(env.paths["alice"], now=stamp)
    assert first != second and first.read_bytes() == original
    assert set(memory_backup.list_backups(env.paths["alice"])) == {first, second}
    assert member_backup.snapshot_time(first) == stamp
    assert member_backup.snapshot_time(Path("memory.20260907T123456Z.zip")) == stamp
    assert memory_backup._age_hours(first, stamp + timedelta(hours=2)) == 2


def test_deleted_member_binding_still_allows_inspection_and_untouched_cancel(env):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    member_backup.stage_restore(backup, path)
    env.tiers["alice"].close()
    before = path.read_bytes()
    config_path = env.root / "config.json"
    config = json.loads(config_path.read_text())
    del config["agents"]["alice"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    loader._invalidate_config_cache()
    status = member_backup.pending_restore_status(path)
    assert status["pending"] and status["restore_error"]
    assert status["recovery"]["journal"] == member_backup.PENDING
    with pytest.raises(ValueError, match="exclusively bound"):
        member_backup.apply_pending_restore(path)
    assert member_backup.cancel_pending_restore(path)
    assert not member_backup.pending_restore_status(path)["pending"]
    assert path.read_bytes() == before
    assert backup.exists()


def test_bad_member_journal_is_preserved_and_never_supplies_cleanup_paths(env):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    member_backup.stage_restore(backup, path)
    out = member_backup.backup_directory(path)
    pending = out / member_backup.PENDING
    staged = out / json.loads(pending.read_text())["stage"]
    bad = b'{"stage":"../../member-bob","aside":'
    pending.write_bytes(bad)
    assert member_backup.pending_restore_status(path)["restore_error"]
    assert member_backup.cancel_pending_restore(path)
    quarantined = list(out.glob("cancelled-restore-*.json"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == bad
    assert staged.is_dir() and backup.exists()
    assert read_value(path) == "PostgreSQL"
    assert read_value(env.paths["bob"]) == "PostgreSQL"


@pytest.mark.parametrize("ambiguous", ["missing_current", "prior_copy"])
def test_bad_member_journal_preserves_ambiguous_recovery_state(env, ambiguous):
    path = env.paths["alice"]
    backup = memory_backup.backup_store(path)
    member_backup.stage_restore(backup, path)
    out = member_backup.backup_directory(path)
    pending = out / member_backup.PENDING
    bad = b"broken restore journal"
    pending.write_bytes(bad)
    env.tiers["alice"].close()
    if ambiguous == "missing_current":
        path.unlink()
    else:
        (out / ("superseded-" + "a" * 32)).mkdir()
    status = member_backup.pending_restore_status(path)
    assert status["pending"] and status["restore_error"]
    assert status["recovery"]["staged_copies"]
    if ambiguous == "prior_copy":
        assert status["recovery"]["previous_copies"] == ["superseded-" + "a" * 32]
    with pytest.raises(ValueError):
        member_backup.cancel_pending_restore(path)
    assert pending.read_bytes() == bad
    assert not list(out.glob("cancelled-restore-*.json"))
    assert backup.exists()
    if ambiguous == "missing_current":
        assert not path.exists()


@pytest.mark.parametrize("without_namespace", [False, True])
def test_pending_activation_holds_namespace_while_publishing(env, monkeypatch, without_namespace):
    path = env.paths["alice"]
    backup = member_backup.backup_store(path)
    member_backup.stage_restore(backup, path)
    env.tiers["alice"].close()
    publish = member_backup.replace_with_retry
    observed = []
    root = (env.root / memory_stores.MEMORY_STORES_DIR_NAME).resolve()

    def observe(source, destination, *args, **kwargs):
        observed.append(root in getattr(memory_stores._NAMESPACE_LOCK_STATE, "roots", set()))
        return publish(source, destination, *args, **kwargs)

    monkeypatch.setattr(member_backup, "replace_with_retry", observe)
    activate = member_backup.apply_pending_restore
    if without_namespace:
        activate = activate.__wrapped__
    activate(path)
    assert observed and all(held is not without_namespace for held in observed)
    assert not member_backup.pending_restore_status(path)["pending"]

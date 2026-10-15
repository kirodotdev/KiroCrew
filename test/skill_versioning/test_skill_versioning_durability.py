"""Lifecycle namespace durability ordering and claimed-inode hardening.

Covers parent-sync ordering before each public mutation and authority step,
forward-publication and rollback source/destination swaps, reserved-namespace
deletion guards, and the snapshot file / aggregate / depth limits plus the
Windows snapshot identity checks.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from pathlib import Path

import pytest

import kiro_crew.skills as skills_mod

from .skill_versioning_support import (
    _prov,
    _restat,
    _stage_update,
    _tagged_identity_for_path,
    _write_live,
)

# This module's auto-skill boundaries initialize explicitly through the
# ``loader`` fixture; every ``uninitialized_loader`` path must see production
# fail closed.
pytestmark = pytest.mark.usefixtures("no_auto_skill_authority_startup")


# ── lifecycle namespace durability ordering ──


def test_create_capable_authority_children_sync_parent_before_yield(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    """Every authority name is durable before a caller can mutate through it."""
    real_pin_child = loader._pin_skill_child_parent
    real_sync = loader._sync_pinned_parent
    synced: list[Path] = []
    observed: list[tuple[Path, Path]] = []

    def recording_sync(parent):
        synced.append(parent.path)
        return real_sync(parent)

    @contextlib.contextmanager
    def asserting_pin(parent, name, *, create, created_out=None):
        with real_pin_child(
            parent,
            name,
            create=create,
            created_out=created_out,
        ) as child:
            if create:
                assert synced and synced[-1] == parent.path
                observed.append((child.path, parent.path))
            yield child

    monkeypatch.setattr(loader, "_sync_pinned_parent", recording_sync)
    monkeypatch.setattr(loader, "_pin_skill_child_parent", asserting_pin)

    def assert_complete_authority_hierarchy(*, creates_private_root: bool) -> None:
        observed.clear()
        synced.clear()
        if creates_private_root:
            home = loader._private_root().parents[1]
            binding = loader._ensure_private_authority(
                home,
                configured_home=home,
                create=True,
            )
            with skills_mod._AUTHORITY_HOME_IDENTITIES_LOCK:
                skills_mod._STARTUP_AUTHORITY_BINDING = binding
        with loader._pin_private_state(create=True) as state:
            expected = {
                (state.auto.path, state.auto.path.parent),
                (state.pending.path, state.auto.path),
                (state.quarantine.path, state.auto.path),
                (state.live_quarantine.path, state.auto.path),
                (state.claims.path, state.private.path),
                (state.evidence.path, state.private.path),
                (state.locks.path, state.private.path),
                (state.claim_locks.path, state.locks.path),
            }
            if creates_private_root:
                expected.update(
                    {
                        (
                            state.private.path,
                            state.data_home.path / skills_mod._AUTHORITY_PROVENANCE_PARENT,
                        ),
                        (
                            state.data_home.path / skills_mod._AUTHORITY_PROVENANCE_PARENT,
                            state.data_home.path,
                        ),
                    }
                )
            assert set(observed) == expected

    # First pass creates every authority child. The second proves an existing
    # child on a create-capable path repairs an interrupted earlier creation.
    assert_complete_authority_hierarchy(creates_private_root=True)
    assert_complete_authority_hierarchy(creates_private_root=False)


def test_claim_lock_ancestor_sync_failure_precedes_public_mutation(loader, monkeypatch):
    target = "claim-lock-ancestor-sync-target"
    slug = f"{target}-update"
    live = _write_live(loader, target, version=1, body="ORIGINAL")
    _stage_update(loader, slug, target=f"auto/{target}")
    pending = loader._pending_root() / slug
    claim_locks = loader._locks_root() / skills_mod.AUTO_CLAIMS_DIRNAME
    claim_locks.rmdir()
    real_sync = loader._sync_pinned_parent

    def fail_claim_locks_parent(parent):
        if parent.path == loader._locks_root():
            raise OSError("injected claim-lock ancestor sync failure")
        return real_sync(parent)

    def forbid_rename(*_args, **_kwargs):
        pytest.fail("authority rename ran before the claim-lock ancestor was durable")

    def forbid_materialization(*_args, **_kwargs):
        pytest.fail("candidate materialization ran before the claim-lock ancestor was durable")

    monkeypatch.setattr(loader, "_sync_pinned_parent", fail_claim_locks_parent)
    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", forbid_rename)
    monkeypatch.setattr(loader, "_materialize_skill_tree_snapshot", forbid_materialization)

    assert loader._claim_pending_update(slug) is None
    assert pending.is_dir()
    assert "ORIGINAL" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert not list(loader._quarantine_root().glob(f"{slug}--*"))
    assert not list(loader._claims_root().glob(f"{slug}--*"))


def test_claim_lock_and_quarantine_parents_sync_before_materialization(loader, monkeypatch):
    slug = "claim-namespace-sync-order"
    _stage_update(loader, slug, target="auto/claim-namespace-target")
    pending_parent = loader._pending_root()
    quarantine_parent = loader._quarantine_root()
    claim_locks_parent = loader._locks_root() / "claims"
    events: list[tuple[str, str]] = []
    real_sync = loader._sync_pinned_parent
    real_rename = loader._rename_skill_child_no_replace
    real_materialize = loader._materialize_skill_tree_snapshot

    def recording_sync(parent):
        if parent.path in {pending_parent, quarantine_parent, claim_locks_parent}:
            events.append(("sync", str(parent.path)))
        return real_sync(parent)

    def recording_rename(source, source_name, destination, destination_name, **kwargs):
        if source.path == pending_parent and destination.path == quarantine_parent:
            events.append(("rename", "pending->quarantine"))
            assert ("sync", str(claim_locks_parent)) in events
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    def recording_materialize(snapshot, destination):
        events.append(("authority", "materialize"))
        return real_materialize(snapshot, destination)

    monkeypatch.setattr(loader, "_sync_pinned_parent", recording_sync)
    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", recording_rename)
    monkeypatch.setattr(loader, "_materialize_skill_tree_snapshot", recording_materialize)

    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    try:
        renamed = events.index(("rename", "pending->quarantine"))
        materialized = events.index(("authority", "materialize"))
        assert renamed < events.index(("sync", str(quarantine_parent))) < materialized
        assert renamed < events.index(("sync", str(pending_parent))) < materialized
    finally:
        loader._restore_claimed_update(claim, fd, slug, snapshot)
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)


def test_claim_lock_parent_sync_failure_cleans_before_public_mutation(loader, monkeypatch):
    slug = "claim-lock-sync-failure"
    _stage_update(loader, slug, target="auto/claim-lock-sync-target")
    pending = loader._pending_root() / slug
    claim_locks_parent = loader._locks_root() / "claims"
    real_sync = loader._sync_pinned_parent
    real_rename = loader._rename_skill_child_no_replace

    def fail_claim_lock_parent(parent):
        if parent.path == claim_locks_parent:
            raise OSError("injected claim-lock parent sync failure")
        return real_sync(parent)

    def refuse_public_move(source, source_name, destination, destination_name, **kwargs):
        if source.path == loader._pending_root() and destination.path == loader._quarantine_root():
            pytest.fail("public candidate moved before its fresh claim-lock name was durable")
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    monkeypatch.setattr(loader, "_sync_pinned_parent", fail_claim_lock_parent)
    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", refuse_public_move)

    assert loader._claim_pending_update(slug) is None
    assert pending.is_dir()
    assert not list(claim_locks_parent.glob(f"{slug}--*.lock"))
    assert not list(loader._quarantine_root().glob(f"{slug}--*"))


def test_existing_skill_lock_is_not_reported_as_fresh(loader):
    assert loader._private_state_roots_safe(create=True) is True
    lock_name = "existing-claim.lock"
    lock_path = loader._locks_root() / "claims" / lock_name
    lock_path.write_bytes(b"existing")

    with loader._pin_private_state(create=False) as state:
        created: list[bool] = []
        fd = loader._open_skill_lock(
            state.claim_locks,
            lock_name,
            created_out=created,
        )
    os.close(fd)

    assert created == [False]
    assert lock_path.read_bytes() == b"existing"


def test_forward_publication_syncs_both_parents_before_each_authority_step(loader, monkeypatch):
    assert loader._private_state_roots_safe(create=True) is True
    live = _write_live(loader, "forward-sync-order", version=1, body="BEFORE")
    stage, backup = loader._publication_paths("forward-sync-order--claim")
    shutil.copytree(live, stage)
    staged_skill = stage / "SKILL.md"
    staged_skill.write_text(
        staged_skill.read_text(encoding="utf-8").replace("BEFORE", "AFTER"),
        encoding="utf-8",
    )
    before_hash = loader._skill_tree_hash(live)
    after_hash = loader._skill_tree_hash(stage)
    backup_identity = _tagged_identity_for_path(loader, live)
    assert before_hash is not None and after_hash is not None

    events: list[tuple[str, str]] = []
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        real_rename = loader._rename_skill_child_no_replace
        real_sync = loader._sync_pinned_parent
        real_hash = loader._skill_tree_hash_child

        def recording_rename(source, source_name, destination, destination_name, **kwargs):
            events.append(("rename", f"{source_name}->{destination_name}"))
            return real_rename(source, source_name, destination, destination_name, **kwargs)

        def recording_sync(parent):
            events.append(("sync", str(parent.path)))
            return real_sync(parent)

        def recording_hash(parent, name):
            if (
                parent.native_identity == state.live_quarantine.native_identity
                and name == backup.name
            ):
                events.append(("authority", "hash-backup"))
            return real_hash(parent, name)

        monkeypatch.setattr(loader, "_rename_skill_child_no_replace", recording_rename)
        monkeypatch.setattr(loader, "_sync_pinned_parent", recording_sync)
        monkeypatch.setattr(loader, "_skill_tree_hash_child", recording_hash)

        assert (
            loader._publish_prepared_skill_tree(
                state=state,
                live_dir=live,
                stage=stage,
                backup=backup,
                backup_identity=backup_identity,
                before_hash=before_hash,
                after_hash=after_hash,
            )
            == "published"
        )

    live_to_backup = events.index(("rename", f"{live.name}->{backup.name}"))
    first_backup_hash = events.index(("authority", "hash-backup"))
    stage_to_live = events.index(("rename", f"{stage.name}->{live.name}"))
    assert live_to_backup < events.index(("sync", str(backup.parent))) < first_backup_hash
    assert live_to_backup < events.index(("sync", str(live.parent))) < first_backup_hash
    assert first_backup_hash < stage_to_live
    second_backup_hash = next(
        index
        for index in range(first_backup_hash + 1, len(events))
        if events[index] == ("authority", "hash-backup")
    )
    assert events.index(("sync", str(live.parent)), stage_to_live + 1) < second_backup_hash
    assert events.index(("sync", str(stage.parent)), stage_to_live + 1) < second_backup_hash


@pytest.mark.skipif(os.name == "nt", reason="Windows parent handles prohibit the injected rename")
def test_publication_refuses_transient_auto_parent_replacement(loader, monkeypatch):
    assert loader._private_state_roots_safe(create=True) is True
    live = _write_live(loader, "publication-parent-swap", version=1, body="BEFORE")
    stage, backup = loader._publication_paths("publication-parent-swap--claim")
    shutil.copytree(live, stage)
    staged_skill = stage / "SKILL.md"
    staged_skill.write_text(
        staged_skill.read_text(encoding="utf-8").replace("BEFORE", "AFTER"),
        encoding="utf-8",
    )
    before_hash = loader._skill_tree_hash(live)
    after_hash = loader._skill_tree_hash(stage)
    backup_identity = _tagged_identity_for_path(loader, live)
    assert before_hash is not None and after_hash is not None
    auto = live.parent
    detached = auto.with_name("auto-detached-during-publication")

    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        real_rename = loader._rename_skill_child_no_replace
        swapped = False

        def replace_restore_auto(source, source_name, destination, destination_name, **kwargs):
            nonlocal swapped
            if source.identity == state.auto.identity and source_name == live.name and not swapped:
                auto.rename(detached)
                auto.mkdir()
                shutil.copytree(detached / live.name, auto / live.name)
                swapped = True
                try:
                    return real_rename(source, source_name, destination, destination_name, **kwargs)
                finally:
                    shutil.rmtree(auto)
                    detached.rename(auto)
            return real_rename(source, source_name, destination, destination_name, **kwargs)

        monkeypatch.setattr(loader, "_rename_skill_child_no_replace", replace_restore_auto)
        outcome = loader._publish_prepared_skill_tree(
            state=state,
            live_dir=live,
            stage=stage,
            backup=backup,
            backup_identity=backup_identity,
            before_hash=before_hash,
            after_hash=after_hash,
        )

    assert swapped is True
    assert outcome == "incomplete"
    assert "BEFORE" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert "AFTER" in (stage / "SKILL.md").read_text(encoding="utf-8")
    assert not backup.exists()
    assert not detached.exists()


def test_post_publish_backup_drift_keeps_committed_live_without_reverse_rename(loader, monkeypatch):
    assert loader._private_state_roots_safe(create=True) is True
    live = _write_live(loader, "drift-sync-order", version=1, body="BEFORE")
    stage, backup = loader._publication_paths("drift-sync-order--claim")
    shutil.copytree(live, stage)
    staged_skill = stage / "SKILL.md"
    staged_skill.write_text(
        staged_skill.read_text(encoding="utf-8").replace("BEFORE", "AFTER"),
        encoding="utf-8",
    )
    before_hash = loader._skill_tree_hash(live)
    after_hash = loader._skill_tree_hash(stage)
    backup_identity = _tagged_identity_for_path(loader, live)
    assert before_hash is not None and after_hash is not None

    events: list[tuple[str, str]] = []
    backup_hashes = 0
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        real_rename = loader._rename_skill_child_no_replace
        real_hash = loader._skill_tree_hash_child

        def recording_rename(source, source_name, destination, destination_name, **kwargs):
            events.append(("rename", f"{source_name}->{destination_name}"))
            return real_rename(source, source_name, destination, destination_name, **kwargs)

        def force_second_backup_hash_to_drift(parent, name):
            nonlocal backup_hashes
            result = real_hash(parent, name)
            if (
                parent.native_identity == state.live_quarantine.native_identity
                and name == backup.name
            ):
                backup_hashes += 1
                if backup_hashes == 2:
                    return "0" * 64
            return result

        monkeypatch.setattr(loader, "_rename_skill_child_no_replace", recording_rename)
        monkeypatch.setattr(
            loader,
            "_skill_tree_hash_child",
            force_second_backup_hash_to_drift,
        )

        assert (
            loader._publish_prepared_skill_tree(
                state=state,
                live_dir=live,
                stage=stage,
                backup=backup,
                backup_identity=backup_identity,
                before_hash=before_hash,
                after_hash=after_hash,
            )
            == "published"
        )

    assert "AFTER" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert backup.is_dir()
    assert not stage.exists()
    assert events == [
        ("rename", f"{live.name}->{backup.name}"),
        ("rename", f"{stage.name}->{live.name}"),
    ]


def test_reconcile_backup_restore_syncs_both_parents_before_classification(loader, monkeypatch):
    assert loader._private_state_roots_safe(create=True) is True
    target = "reconcile-sync-order"
    live = _write_live(loader, target, version=1, body="BEFORE")
    claim_name = "reconcile-sync-order--claim"
    claim = loader._claims_root() / claim_name
    claim.mkdir()
    stage, backup = loader._publication_paths(claim_name)
    before_hash = loader._skill_tree_hash(live)
    backup_identity = _tagged_identity_for_path(loader, live)
    shutil.copytree(live, stage)
    staged_skill = stage / "SKILL.md"
    staged_skill.write_text(
        staged_skill.read_text(encoding="utf-8").replace("BEFORE", "AFTER"),
        encoding="utf-8",
    )
    after_hash = loader._skill_tree_hash(stage)
    assert before_hash is not None and after_hash is not None

    journal = {
        "state": "prepared",
        "format": 2,
        "claim": claim_name,
        "kind": "update",
        "target": target,
        "before": before_hash,
        "after": after_hash,
        "live_backup_identity": loader._identity_payload(backup_identity),
        "snapshot": 1,
        "version": 2,
    }
    events: list[tuple[str, str]] = []
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        loader._rename_skill_child_no_replace(
            state.auto,
            target,
            state.live_quarantine,
            backup.name,
            expected_identity=backup_identity,
        )
        real_rename = loader._rename_skill_child_no_replace
        real_sync = loader._sync_pinned_parent
        real_hash = loader._skill_tree_hash_child

        def recording_rename(source, source_name, destination, destination_name, **kwargs):
            events.append(("rename", f"{source_name}->{destination_name}"))
            return real_rename(source, source_name, destination, destination_name, **kwargs)

        def recording_sync(parent):
            events.append(("sync", str(parent.path)))
            return real_sync(parent)

        def recording_hash(parent, name):
            result = real_hash(parent, name)
            if parent.identity == state.auto.identity and name == target:
                events.append(("authority", "classify-live"))
            return result

        monkeypatch.setattr(loader, "_authenticated_claim_publication", lambda *_args: journal)
        monkeypatch.setattr(loader, "_rename_skill_child_no_replace", recording_rename)
        monkeypatch.setattr(loader, "_sync_pinned_parent", recording_sync)
        monkeypatch.setattr(loader, "_skill_tree_hash_child", recording_hash)

        assert (
            loader._reconcile_prepared_claim(
                claim,
                -1,
                loader._claim_lock_path(claim_name),
                private_state=state,
            )
            is False
        )

    restored = events.index(("rename", f"{backup.name}->{live.name}"))
    classified = events.index(("authority", "classify-live"))
    assert restored < events.index(("sync", str(live.parent)), restored + 1) < classified
    assert restored < events.index(("sync", str(backup.parent)), restored + 1) < classified


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename destination-swap injection")
def test_private_publish_destination_swap_leaves_newer_live_public(loader, monkeypatch):
    target = "post-rename-destination-swap"
    claim_name = f"{target}--claim"
    stage, _backup = loader._publication_paths(claim_name)
    stage.mkdir()
    (stage / "SKILL.md").write_text("AUTHENTIC AFTER", encoding="utf-8")
    after_hash = loader._skill_tree_hash(stage)
    assert after_hash is not None
    live = loader._dir / "auto" / target
    displaced = loader._dir / "auto" / f"{target}-authenticated-displaced"
    real_rename = skills_mod.platform_compat.rename_noreplace
    swapped = False

    def rename_then_swap_destination(src, dst, *, src_dir_fd, dst_dir_fd):
        nonlocal swapped
        result = real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if src == stage.name and dst == target and not swapped:
            os.rename(dst, displaced.name, src_dir_fd=dst_dir_fd, dst_dir_fd=dst_dir_fd)
            live.mkdir()
            (live / "SKILL.md").write_text("NEWER PUBLIC LIVE", encoding="utf-8")
            swapped = True
        return result

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "rename_noreplace",
        rename_then_swap_destination,
    )
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        outcome = loader._publish_prepared_skill_tree(
            state=state,
            live_dir=live,
            stage=stage,
            backup=None,
            backup_identity=None,
            before_hash=None,
            after_hash=after_hash,
        )

    assert swapped is True
    assert outcome == "incomplete"
    assert (live / "SKILL.md").read_text(encoding="utf-8") == "NEWER PUBLIC LIVE"
    assert (displaced / "SKILL.md").read_text(encoding="utf-8") == "AUTHENTIC AFTER"
    assert not stage.exists()
    assert live.parent == loader._dir / "auto"
    assert displaced.parent == loader._dir / "auto"


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename source-swap injection")
def test_forward_source_swap_never_evacuates_replacement_live(loader, monkeypatch):
    live = _write_live(loader, "forward-source-swap", version=1, body="ORIGINAL")
    stage, backup = loader._publication_paths("forward-source-swap--claim")
    shutil.copytree(live, stage)
    (stage / "SKILL.md").write_text("AFTER", encoding="utf-8")
    before_hash = loader._skill_tree_hash(live)
    after_hash = loader._skill_tree_hash(stage)
    expected_identity = _tagged_identity_for_path(loader, live)
    original_name = "forward-source-swap-original"
    real_rename = skills_mod.platform_compat.rename_noreplace
    swapped = False

    def swap_source_then_rename(src, dst, *, src_dir_fd, dst_dir_fd):
        nonlocal swapped
        if src == live.name and dst == backup.name and not swapped:
            os.rename(
                src,
                original_name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )
            shutil.copytree(live.parent / original_name, live)
            (live / "SKILL.md").write_text("CONCURRENT LIVE", encoding="utf-8")
            swapped = True
        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(skills_mod.platform_compat, "rename_noreplace", swap_source_then_rename)
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        outcome = loader._publish_prepared_skill_tree(
            state=state,
            live_dir=live,
            stage=stage,
            backup=backup,
            backup_identity=expected_identity,
            before_hash=before_hash,
            after_hash=after_hash,
        )

    assert swapped is True
    assert outcome == "incomplete"
    assert (live / "SKILL.md").read_text(encoding="utf-8") == "CONCURRENT LIVE"
    assert "ORIGINAL" in (live.parent / original_name / "SKILL.md").read_text(encoding="utf-8")
    assert not backup.exists()
    assert stage.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename source-swap injection")
def test_rollback_source_swap_never_leaves_replacement_live_on_restart(loader, monkeypatch):
    target = "rollback-source-swap"
    live = _write_live(loader, target, version=1, body="BEFORE")
    stage, backup = loader._publication_paths("rollback-source-swap--claim")
    shutil.copytree(live, stage)
    (stage / "SKILL.md").write_text("AFTER", encoding="utf-8")
    before_hash = loader._skill_tree_hash(live)
    after_hash = loader._skill_tree_hash(stage)
    backup_identity = _tagged_identity_for_path(loader, live)
    claim_name = backup.name
    journal = {
        "state": "prepared",
        "format": 2,
        "claim": claim_name,
        "kind": "update",
        "target": target,
        "before": before_hash,
        "after": after_hash,
        "live_backup_identity": loader._identity_payload(backup_identity),
        "snapshot": 1,
        "version": 2,
    }
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        loader._rename_skill_child_no_replace(
            state.auto,
            target,
            state.live_quarantine,
            backup.name,
            expected_identity=backup_identity,
        )
    original_name = f"{backup.name}-original"
    real_rename = skills_mod.platform_compat.rename_noreplace
    swapped = False

    def swap_source_then_rename(src, dst, *, src_dir_fd, dst_dir_fd):
        nonlocal swapped
        if src == backup.name and dst == target and not swapped:
            os.rename(
                src,
                original_name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=src_dir_fd,
            )
            shutil.copytree(backup.parent / original_name, backup)
            (backup / "SKILL.md").write_text("REPLACEMENT EVIDENCE", encoding="utf-8")
            swapped = True
        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(skills_mod.platform_compat, "rename_noreplace", swap_source_then_rename)
    monkeypatch.setattr(loader, "_authenticated_claim_publication", lambda *_args: journal)
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        assert loader._reconcile_prepared_claim_pinned(journal, state, claim_name) is None
        assert loader._reconcile_prepared_claim_pinned(journal, state, claim_name) is None

    assert swapped is True
    assert not live.exists()
    assert "REPLACEMENT EVIDENCE" in (backup / "SKILL.md").read_text(encoding="utf-8")
    assert "BEFORE" in (backup.parent / original_name / "SKILL.md").read_text(encoding="utf-8")
    assert stage.is_dir()


# ── reserved namespace and claimed-inode hardening ──


@pytest.mark.parametrize(
    "name",
    [
        "auto",
        "AUTO",
        "Auto",
        "aUtO",
        # Win32 strips trailing dots/spaces from path components, so these
        # aliases open the ``auto`` directory on Windows filesystems.
        "auto.",
        "auto ",
        "auto. .",
        "AUTO.",
    ],
)
def test_delete_refuses_bare_reserved_auto_namespace(loader, monkeypatch, name):
    live_dir = _write_live(loader, "namespace-survivor", version=1, body="KEEP")
    audit_events: list[dict] = []

    class AuditSink:
        def log_tool_invocation(self, **kwargs):
            audit_events.append(kwargs)

    def unexpected_delete(*_args, **_kwargs):
        pytest.fail("reserved auto namespace reached the recursive delete helper")

    monkeypatch.setattr(skills_mod, "sel", lambda: AuditSink())
    monkeypatch.setattr(shutil, "rmtree", unexpected_delete)

    assert loader.delete_skill(name) is False
    assert (live_dir / "SKILL.md").exists()
    assert audit_events == [
        {
            "session_key": "skills",
            "tool_name": "skill_mutation",
            "tool_kind": "permission",
            "outcome": "denied",
            "metadata": {
                "target": name,
                "reason": "reserved_auto_namespace",
            },
        }
    ]


def test_non_reserved_missing_skill_does_not_emit_reserved_namespace_audit(loader, monkeypatch):
    audit_events: list[dict] = []

    class AuditSink:
        def log_tool_invocation(self, **kwargs):
            audit_events.append(kwargs)

    monkeypatch.setattr(skills_mod, "sel", lambda: AuditSink())

    assert loader.delete_skill("ordinary") is False
    assert audit_events == []


def _select_snapshot_reader(monkeypatch, reader: str) -> None:
    if reader == "pinned":
        if not skills_mod.pinned_fs.supports_pinned_tree_walk():
            pytest.skip("descriptor-pinned tree reads are unavailable")
        return
    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)


@pytest.mark.parametrize("reader", ["pinned", "fallback"])
def test_skill_snapshot_file_limit_accepts_boundary_and_refuses_before_read(
    loader, monkeypatch, tmp_path, reader
):
    _select_snapshot_reader(monkeypatch, reader)
    root = tmp_path / f"file-bound-{reader}"
    root.mkdir()
    leaf = root / "SKILL.md"
    boundary = "ééa".encode("utf-8")
    assert len(boundary) == 5
    leaf.write_bytes(boundary)
    platform_mode = stat.S_IMODE(leaf.stat().st_mode)
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_FILE_BYTES", len(boundary))
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_TOTAL_BYTES", len(boundary) * 2)

    snapshot = loader._skill_tree_snapshot(root)

    assert snapshot is not None
    assert snapshot.files[Path("SKILL.md")] == boundary
    assert snapshot.file_modes[Path("SKILL.md")] == platform_mode

    leaf.write_bytes(boundary + b"x")

    def unexpected_payload_read(*_args, **_kwargs):
        pytest.fail("oversized file reached payload allocation")

    monkeypatch.setattr(
        skills_mod.SkillsLoader,
        "_stable_file_payload",
        staticmethod(unexpected_payload_read),
    )
    assert loader._skill_tree_snapshot(root) is None


@pytest.mark.parametrize("reader", ["pinned", "fallback"])
def test_skill_snapshot_aggregate_limit_accepts_boundary_and_refuses_excess(
    loader, monkeypatch, tmp_path, reader
):
    _select_snapshot_reader(monkeypatch, reader)
    root = tmp_path / f"aggregate-bound-{reader}"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"12345")
    (root / ".meta.json").write_bytes(b"67890")
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_FILE_BYTES", 10)
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_TOTAL_BYTES", 10)

    snapshot = loader._skill_tree_snapshot(root)

    assert snapshot is not None
    assert sum(len(payload) for payload in snapshot.files.values()) == 10

    (root / "extra").write_bytes(b"x")
    assert loader._skill_tree_snapshot(root) is None


@pytest.mark.parametrize("reader", ["pinned", "fallback"])
def test_skill_snapshot_entry_and_depth_limits_accept_boundary_and_refuse_excess(
    loader, monkeypatch, tmp_path, reader
):
    _select_snapshot_reader(monkeypatch, reader)
    root = tmp_path / f"shape-bound-{reader}"
    root.mkdir()
    (root / "a").write_bytes(b"")
    (root / "b").write_bytes(b"")
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_ENTRIES", 2)
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_DEPTH", 2)

    assert loader._skill_tree_snapshot(root) is not None
    (root / "c").write_bytes(b"")
    assert loader._skill_tree_snapshot(root) is None

    (root / "c").unlink()
    (root / "a").unlink()
    (root / "b").unlink()
    (root / "d").mkdir()
    (root / "d" / "at-boundary").write_bytes(b"")
    monkeypatch.setattr(skills_mod, "_SKILL_SNAPSHOT_MAX_ENTRIES", 10)
    assert loader._skill_tree_snapshot(root) is not None

    (root / "d" / "too-deep").mkdir()
    (root / "d" / "too-deep" / "leaf").write_bytes(b"")
    assert loader._skill_tree_snapshot(root) is None


@pytest.mark.parametrize("reader", ["pinned", "fallback"])
def test_skill_snapshot_refuses_file_that_expands_during_read(
    loader, monkeypatch, tmp_path, reader
):
    _select_snapshot_reader(monkeypatch, reader)
    root = tmp_path / f"expanding-{reader}"
    root.mkdir()
    leaf = root / "SKILL.md"
    leaf.write_bytes(b"body")
    real_read = skills_mod.os.read
    expanded = False

    def expanding_read(fd, size):
        nonlocal expanded
        data = real_read(fd, size)
        if data and not expanded:
            with leaf.open("ab") as handle:
                handle.write(b"x")
            expanded = True
        return data

    monkeypatch.setattr(skills_mod.os, "read", expanding_read)

    assert loader._skill_tree_snapshot(root) is None
    assert expanded is True


def test_skill_snapshot_fallback_uses_held_no_reparse_handles(loader, tmp_path):
    root = tmp_path / "windows-fallback"
    nested = root / "scripts"
    nested.mkdir(parents=True)
    leaf = nested / "run.py"
    leaf.write_text("print('✓')\n", encoding="utf-8")
    expected = leaf.read_bytes()
    file_mode = stat.S_IMODE(leaf.stat().st_mode)
    dir_mode = stat.S_IMODE(nested.stat().st_mode)
    native_snapshot_reader = skills_mod.pinned_fs.supports_pinned_tree_walk
    real_pin = skills_mod.platform_compat.pin_directory
    real_open = skills_mod.platform_compat.open_file_no_reparse
    pinned: list[Path] = []
    opened: list[Path] = []

    def recording_pin(path):
        pinned.append(Path(path))
        return real_pin(path)

    def recording_open(path, *, nonblocking=False):
        opened.append(Path(path))
        return real_open(path, nonblocking=nonblocking)

    with pytest.MonkeyPatch.context() as fallback:
        fallback.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        fallback.setattr(skills_mod.platform_compat, "pin_directory", recording_pin)
        fallback.setattr(
            skills_mod.platform_compat,
            "open_file_no_reparse",
            recording_open,
        )

        snapshot = loader._skill_tree_snapshot(root)

        assert snapshot is not None
        assert pinned == [root, nested]
        assert opened == [leaf]
        assert snapshot.files[Path("scripts/run.py")] == expected
        assert snapshot.file_modes[Path("scripts/run.py")] == file_mode
        assert snapshot.dir_modes[Path("scripts")] == dir_mode

    assert skills_mod.pinned_fs.supports_pinned_tree_walk is native_snapshot_reader
    assert loader._skill_tree_snapshot(root) is not None


def test_windows_snapshot_path_alias_uses_opened_identity(loader, tmp_path):
    root = tmp_path / "windows-path-alias"
    root.mkdir()
    skill = root / "SKILL.md"
    skill.write_bytes(b"body")
    identity_checks: list[tuple[int, str]] = []

    def same_opened_object(fd, expected):
        identity_checks.append((fd, os.fspath(expected)))
        return True

    with pytest.MonkeyPatch.context() as windows_fallback:
        windows_fallback.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
        windows_fallback.setattr(
            skills_mod.pinned_fs,
            "supports_pinned_tree_walk",
            lambda: False,
        )
        # Native Windows validates handle identity without requiring a
        # descriptor-derived path. That API can be unreadable while the exact
        # handle identity remains valid.
        windows_fallback.setattr(skills_mod.pinned_fs, "fd_real_path", lambda _fd: None)
        windows_fallback.setattr(
            skills_mod.platform_compat,
            "opened_path_identity_matches",
            same_opened_object,
        )

        snapshot = loader._skill_tree_snapshot(root)

    assert snapshot is not None
    assert snapshot.files[Path("SKILL.md")] == b"body"
    assert len(identity_checks) == 4


def test_windows_snapshot_path_identity_mismatch_refuses(loader, tmp_path, monkeypatch):
    root = tmp_path / "windows-path-mismatch"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"body")
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: False,
    )

    assert loader._skill_tree_snapshot(root) is None


def test_windows_snapshot_same_handle_ignores_unstable_crt_inode(loader, tmp_path, monkeypatch):
    root = tmp_path / "windows-unstable-crt-inode"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"body")
    real_fstat = skills_mod.os.fstat
    file_reads = 0

    def unstable_regular_inode(fd):
        nonlocal file_reads
        result = real_fstat(fd)
        if not stat.S_ISREG(result.st_mode):
            return result
        file_reads += 1
        fields = list(result)
        fields[1] = int(result.st_ino) + file_reads
        return os.stat_result(fields)

    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.setattr(skills_mod.os, "fstat", unstable_regular_inode)

    snapshot = loader._skill_tree_snapshot(root)

    assert snapshot is not None
    assert snapshot.files[Path("SKILL.md")] == b"body"
    assert file_reads >= 2


def test_windows_snapshot_native_identity_overrides_stale_posix_inode_fields(
    loader, tmp_path, monkeypatch
):
    root = tmp_path / "windows-stale-inode-fields"
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"body")
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    # CRT ``st_dev``/``st_ino`` values can disagree between an lstat and a
    # Windows handle even when native file IDs authenticate the same object.
    monkeypatch.setattr(skills_mod.os.path, "samestat", lambda _before, _opened: False)

    snapshot = loader._skill_tree_snapshot(root)

    assert snapshot is not None
    assert snapshot.files[Path("SKILL.md")] == b"body"


def test_windows_snapshot_hardlink_still_refuses(loader, tmp_path, monkeypatch):
    root = tmp_path / "windows-hardlink"
    root.mkdir()
    skill = root / "SKILL.md"
    skill.write_bytes(b"body")
    os.link(skill, tmp_path / "skill-alias")
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )

    assert loader._skill_tree_snapshot(root) is None


def test_windows_snapshot_scandir_zero_nlink_still_reads(loader, tmp_path, monkeypatch):
    """os.DirEntry.stat() sets st_ino/st_dev/st_nlink to 0 on Windows because the
    directory-enumeration data carries no link count. The fallback snapshot must
    not fail-close every candidate on that zeroed pre-open link count -- the
    single-link requirement is enforced on the OPENED descriptor, whose fstat
    reads the real link count on Windows. Under the pre-fix ``before.st_nlink !=
    1`` gate this returns None for every regular file and collapses the whole
    pending pipeline (empty descriptions, get_pending None, approve None)."""
    root = tmp_path / "windows-scandir-zero-nlink"
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_bytes(b"body")
    (root / "scripts" / "run.py").write_bytes(b"print(1)\n")

    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )

    real_scandir = skills_mod.os.scandir

    class _WinEntry:
        """A DirEntry whose stat() zeroes st_ino/st_dev/st_nlink like Windows.

        Everything else is the real entry's: the patch is live for every scandir
        caller until teardown, and shutil.rmtree's Windows walk asks is_junction().
        """

        def __init__(self, real):
            self._real = real
            self.name = real.name
            self.path = real.path

        def __getattr__(self, name):
            return getattr(self._real, name)

        def is_symlink(self):
            return self._real.is_symlink()

        def is_dir(self, *, follow_symlinks=True):
            return self._real.is_dir(follow_symlinks=follow_symlinks)

        def stat(self, *, follow_symlinks=True):
            real = self._real.stat(follow_symlinks=follow_symlinks)
            return _restat(real, st_ino=0, st_dev=0, st_nlink=0)

    with real_scandir(root) as entries:
        stand_in = _WinEntry(next(iter(entries)))
    assert all(hasattr(stand_in, name) for name in dir(os.DirEntry) if not name.startswith("_"))

    def windows_scandir(path):
        inner = real_scandir(path)

        class _Scanner:
            def __enter__(self):
                inner.__enter__()
                return self

            def __exit__(self, *exc):
                return inner.__exit__(*exc)

            def __iter__(self):
                return self

            def __next__(self):
                return _WinEntry(next(inner))

        return _Scanner()

    monkeypatch.setattr(skills_mod.os, "scandir", windows_scandir)

    snapshot = loader._skill_tree_snapshot(root)
    assert snapshot is not None
    assert snapshot.files[Path("SKILL.md")] == b"body"
    assert snapshot.files[Path("scripts/run.py")] == b"print(1)\n"

    # The single-link requirement is preserved: a genuine hardlink still refuses
    # via the opened descriptor's real link count, even though scandir reports 0
    # for it too.
    os.link(root / "SKILL.md", tmp_path / "skill-alias")
    assert loader._skill_tree_snapshot(root) is None


def test_approval_refuses_preclaim_hardlink_candidate(loader):
    slug = "hardlinked-candidate"
    assert (
        loader.stage_skill_candidate(
            slug,
            description="hardlink isolation",
            triggers="hardlink",
            procedure_md="## Steps\n\nORIGINAL",
            provenance=_prov(),
        )
        == f"auto/{slug}"
    )
    pending_skill = loader._pending_root() / slug / "SKILL.md"
    original = pending_skill.read_bytes()
    alias = loader._dir / "candidate-hardlink-alias"
    os.link(pending_skill, alias)
    assert pending_skill.stat().st_nlink == 2

    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_skill_checked(slug)
    assert refused.value.reason == "invalid_layout"
    assert pending_skill.read_bytes() == original
    assert alias.read_bytes() == original
    assert not (loader._dir / "auto" / slug).exists()


def test_candidate_inode_guard_fails_closed_on_open_error(loader, monkeypatch):
    slug = "unstatable-candidate"
    assert (
        loader.stage_skill_candidate(
            slug,
            description="stat failure",
            triggers="stat",
            procedure_md="## Steps\n\nBODY",
            provenance=_prov(),
        )
        == f"auto/{slug}"
    )
    skill_file = loader._pending_root() / slug / "SKILL.md"
    real_open = skills_mod.platform_compat.open_file_no_reparse

    def fail_candidate_open(path, *args, **kwargs):
        if Path(path) == skill_file:
            raise PermissionError("injected candidate open failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(skills_mod.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "open_file_no_reparse",
        fail_candidate_open,
    )

    # The snapshot every claim and preview captures refuses the whole tree, so
    # approval fails closed and leaves the candidate queued.
    assert loader._skill_tree_snapshot(loader._pending_root() / slug) is None
    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_skill_checked(slug)
    assert refused.value.reason == "promotion_failed"
    assert skill_file.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows snapshots use CreateFileW no-reparse identity, not os.open",
)
def test_skill_tree_hash_authenticates_opened_inode_without_nofollow(loader, monkeypatch):
    live_dir = _write_live(loader, "tree-hash-inode", version=1, body="SAFE")
    live_skill = live_dir / "SKILL.md"
    victim = loader._dir / "tree-hash-victim"
    victim.write_text("VICTIM", encoding="utf-8")
    real_open = skills_mod.os.open

    def open_swapped_inode(path, flags, *args, **kwargs):
        if os.fspath(path) == os.fspath(live_skill):
            return real_open(victim, flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.delattr(skills_mod.os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(skills_mod.os, "open", open_swapped_inode)

    assert loader._skill_tree_hash(live_dir) is None
    assert victim.read_text(encoding="utf-8") == "VICTIM"


def test_staging_binds_validated_bytes_not_file_reread(loader, monkeypatch):
    """A concurrent overwrite of the PUBLIC pending SKILL.md landing inside the
    staging window (after the content write, before staging completes) must not
    be vouched for: the binding hashes the in-memory validated bytes, never a
    re-read of the publicly writable file, so the unattended promotion refuses
    the swapped candidate instead of installing unvalidated prose."""
    _write_live(loader, "stage-tamper", version=1, body="OLD")
    binding: list[str] = []
    real_write = loader._write_pinned_new_file
    slug = "stage-tamper-update"
    tampered = False

    def meta_write_then_tamper(parent, name, payload, *, mode=0o666):
        nonlocal tampered
        result = real_write(parent, name, payload, mode=mode)
        if name == ".meta.json" and parent.path.name == slug:
            # The concurrent writer reaches the PUBLIC name, the way another process
            # would; a descriptor-relative open is also unavailable on Windows.
            fd = os.open(
                parent.path / "SKILL.md",
                os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.ftruncate(fd, 0)
                os.write(fd, b"## Steps\n\nINJECTED\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            tampered = True
        return result

    monkeypatch.setattr(loader, "_write_pinned_new_file", meta_write_then_tamper)
    _stage_update(
        loader,
        slug,
        target="auto/stage-tamper",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )

    assert tampered is True
    # The tamper IS on disk (the file is public), but the binding must not
    # vouch for it: promotion verifies claimed bytes against the binding and
    # refuses.
    assert (
        loader.auto_apply_pending_update(
            slug,
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert loader.get_auto_skill_version("auto/stage-tamper") == 1
    assert "INJECTED" not in (loader._dir / "auto" / "stage-tamper" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def test_attended_update_promotes_fresh_validated_inode(loader, monkeypatch):
    """A post-validation mutation cannot change the published generation."""
    _write_live(loader, "attended-inode", version=1, body="OLD")
    _stage_update(
        loader,
        "attended-inode-update",
        target="auto/attended-inode",
        body="## Steps\n\nSAFE-ATTENDED-BODY",
    )
    pending_body = loader._pending_root() / "attended-inode-update" / "SKILL.md"
    retained_fd = None if os.name == "nt" else os.open(pending_body, os.O_WRONLY)
    real_validate = loader._validate_and_redact_candidate

    def validate_then_mutate_claim(src, name, **kwargs):
        result = real_validate(src, name, **kwargs)
        if result is not None:
            if retained_fd is None:
                (src / "SKILL.md").write_bytes(b"## Steps\r\n\r\nPATH-TAMPER\r\n")
            else:
                os.lseek(retained_fd, 0, os.SEEK_SET)
                os.ftruncate(retained_fd, 0)
                os.write(retained_fd, b"## Steps\n\nRETAINED-HANDLE-TAMPER\n")
        return result

    monkeypatch.setattr(loader, "_validate_and_redact_candidate", validate_then_mutate_claim)
    try:
        assert (
            loader.approve_pending_update_checked("attended-inode-update") == "auto/attended-inode"
        )
    finally:
        if retained_fd is not None:
            os.close(retained_fd)

    live = (loader._dir / "auto" / "attended-inode" / "SKILL.md").read_text(encoding="utf-8")
    assert "SAFE-ATTENDED-BODY" in live
    assert "RETAINED-HANDLE-TAMPER" not in live
    assert "PATH-TAMPER" not in live

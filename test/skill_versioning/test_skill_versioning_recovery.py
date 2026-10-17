"""Concurrent approvals, restore ordering, restart recovery and backup drift.

Covers serialization of concurrent new-skill approvals and same-target
promotions, restore paths that preserve a direct or late writer, crash recovery
across the live-publish boundary, and post-publish backup-drift reconciliation.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import kiro_crew.skills as skills_mod

from .skill_versioning_support import (
    _THREAD_WAIT_CEILING_SECS,
    _linked_pending_candidate,
    _open_lock_for_test,
    _prov,
    _stage_update,
    _write_live,
)

# This module's auto-skill boundaries initialize explicitly through the
# ``loader`` fixture; every ``uninitialized_loader`` path must see production
# fail closed.
pytestmark = pytest.mark.usefixtures("no_auto_skill_authority_startup")


def test_concurrent_new_skill_approvals_serialize_and_preserve_replacement(loader, monkeypatch):
    slug = "concurrent-new-approval"
    loader.stage_skill_candidate(
        slug,
        description="original candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )
    pending = loader._pending_root() / slug
    live = loader._dir / "auto" / slug
    first_inspection = threading.Event()
    allow_first = threading.Event()
    second_claimed = threading.Event()
    unexpected_second_inspection = threading.Event()
    real_claim = loader._claim_pending_update
    real_validate = loader._validate_and_redact_snapshot
    count_lock = threading.Lock()
    claim_count = 0
    validation_count = 0

    def recording_claim(candidate_slug):
        nonlocal claim_count
        result = real_claim(candidate_slug)
        if result is not None:
            with count_lock:
                claim_count += 1
                if claim_count == 2:
                    second_claimed.set()
        return result

    def blocking_first_validation(tree, name, **kwargs):
        nonlocal validation_count
        with count_lock:
            validation_count += 1
            current = validation_count
        if current == 1:
            first_inspection.set()
            assert allow_first.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        else:
            unexpected_second_inspection.set()
        return real_validate(tree, name, **kwargs)

    monkeypatch.setattr(loader, "_claim_pending_update", recording_claim)
    monkeypatch.setattr(
        loader,
        "_validate_and_redact_snapshot",
        blocking_first_validation,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(loader.approve_pending_skill_checked, slug)
        assert first_inspection.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        pending.mkdir()
        (pending / "SKILL.md").write_text("REPLACEMENT", encoding="utf-8")
        (pending / ".meta.json").write_text("{}", encoding="utf-8")
        scripts = pending / "scripts"
        scripts.mkdir()
        (scripts / "late.py").write_text("print('late')\n", encoding="utf-8")
        second = pool.submit(loader.approve_pending_skill_checked, slug)
        assert second_claimed.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        assert not second.done()
        assert not unexpected_second_inspection.is_set()
        allow_first.set()
        assert first.result(timeout=_THREAD_WAIT_CEILING_SECS) == f"auto/{slug}"
        with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
            second.result(timeout=_THREAD_WAIT_CEILING_SECS)
        assert refused.value.reason == "live_exists"

    assert "ORIGINAL" in (live / "SKILL.md").read_text(encoding="utf-8")
    assert not any(child.name.startswith(f"{slug}--") for child in live.iterdir())
    assert (pending / "scripts" / "late.py").exists()
    assert not loader._claims_root().exists() or not list(loader._claims_root().iterdir())


def test_restore_no_replace_preserves_direct_writer(loader, monkeypatch):
    slug = "restore-direct-writer"
    _stage_update(loader, slug, target="auto/restore-target")
    pending_before_claim = loader._pending_root() / slug
    original_skill = (pending_before_claim / "SKILL.md").read_bytes()
    metadata_file = pending_before_claim / ".meta.json"
    normalized_metadata = metadata_file.read_bytes().replace(b"\r\n", b"\n")
    original_metadata = normalized_metadata.replace(b"\n", b"\r\n")
    metadata_file.write_bytes(original_metadata)
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    pending = loader._pending_root() / slug
    entered_restore = threading.Event()
    allow_restore = threading.Event()
    real_rename = loader._rename_skill_child_no_replace
    blocked = False

    def blocking_rename(source, source_name, destination, destination_name, **kwargs):
        nonlocal blocked
        if (
            source.path == loader._quarantine_root()
            and source_name == claim.name
            and destination_name == slug
            and not blocked
        ):
            blocked = True
            entered_restore.set()
            assert allow_restore.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", blocking_rename)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(loader._restore_claimed_update, claim, fd, slug, snapshot)
        assert entered_restore.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        pending.mkdir()
        (pending / "SKILL.md").write_text("DIRECT REPLACEMENT", encoding="utf-8")
        allow_restore.set()
        restored = future.result(timeout=_THREAD_WAIT_CEILING_SECS)
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    assert restored is not None and restored.name == f"{slug}-2"
    assert (pending / "SKILL.md").read_text(encoding="utf-8") == "DIRECT REPLACEMENT"
    assert (restored / "SKILL.md").read_bytes() == original_skill
    assert (restored / ".meta.json").read_bytes() == original_metadata
    assert not (loader._evidence_root() / claim.name).exists()
    assert not (loader._quarantine_root() / claim.name).exists()
    assert not claim.exists()
    loader._cleanup_claim_lock(claim.name)
    assert not loader._claim_lock_path(claim.name).exists()


def test_claim_refuses_before_public_move_when_no_replace_is_unsupported(
    loader, monkeypatch, caplog
):
    slug = "unsupported-no-replace"
    _stage_update(loader, slug, target="auto/no-replace-target")
    pending = loader._pending_root() / slug

    def unsupported(_source, _source_name, _destination, _destination_name, **_kwargs):
        raise OSError(errno.ENOTSUP, "atomic no-replace rename unavailable")

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", unsupported)

    assert loader._claim_pending_update(slug) is None
    assert pending.is_dir()
    assert "new steps" in (pending / "SKILL.md").read_text(encoding="utf-8")
    assert not list(loader._claims_root().glob(f"{slug}--*"))
    assert not list((loader._locks_root() / "claims").glob(f"{slug}--*.lock"))
    assert "Move KIROCREW_HOME to a local filesystem" in caplog.text
    assert "renameat2(RENAME_NOREPLACE)" in caplog.text


def test_restart_finishes_private_evidence_move_after_durable_state(loader, monkeypatch):
    slug = "evidence-move-recovery"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
    )
    real_rename = loader._rename_skill_child_no_replace
    refused = False

    def refuse_first_evidence_move(source, source_name, destination, destination_name, **kwargs):
        nonlocal refused
        if destination.path == loader._evidence_root() and not refused:
            refused = True
            raise OSError("injected evidence move interruption")
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", refuse_first_evidence_move)

    assert loader.approve_pending_skill_checked(slug) == f"auto/{slug}"
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    quarantines = list(loader._quarantine_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    assert len(quarantines) == 1
    claim = claims[0]
    lock_path = loader._claim_lock_path(claim.name)
    fd = _open_lock_for_test(loader, lock_path)
    try:
        assert loader._authenticated_claim_evidence_state(fd, lock_path, claim.name) is not None
        assert loader._authenticated_completion_marker(claim) is True
    finally:
        os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert not claim.exists()
    assert (restarted._evidence_root() / claim.name).is_dir()
    assert quarantines[0].is_dir()
    assert lock_path.is_file()


def test_dismissed_candidate_retains_separated_evidence(loader):
    slug = "dismiss-evidence"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )

    assert loader.dismiss_pending_skill(slug) is True

    evidence = list(loader._evidence_root().glob(f"{slug}--*"))
    quarantine = list(loader._quarantine_root().glob(f"{slug}--*"))
    assert len(evidence) == 1
    assert len(quarantine) == 1
    assert loader._authenticated_completion_marker(evidence[0]) is True
    assert loader._claim_lock_path(evidence[0].name).is_file()
    assert loader.list_pending_skills() == []


def test_consumed_quarantine_evidence_releases_slug_for_later_update(loader):
    slug = "retained-evidence-reusable"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )
    assert loader.approve_pending_skill_checked(slug) == f"auto/{slug}"
    assert loader._pending_slug_claimed(slug) is False

    _stage_update(loader, slug, target=f"auto/{slug}", base_version=1)

    assert (loader._pending_root() / slug).is_dir()
    assert not (loader._pending_root() / f"{slug}-2").exists()
    assert loader.approve_pending_update_checked(slug) == f"auto/{slug}"
    assert loader.get_auto_skill_version(f"auto/{slug}") == 2


@pytest.mark.parametrize(
    "active_state",
    ["materialized", "quarantine-only", "linked", "lock-held-evidence", "unjournaled"],
)
def test_active_quarantine_reserves_slug_for_sibling_stage(loader, active_state):
    slug = f"active-quarantine-{active_state}"
    claim = None
    claim_fd = None
    snapshot = None
    if active_state == "unjournaled":
        assert loader._private_state_roots_safe(create=True) is True
        quarantine = loader._quarantine_root() / f"{slug}--0123456789abcdef"
        quarantine.mkdir()
        (quarantine / "SKILL.md").write_text("UNTRUSTED\n", encoding="utf-8")
    else:
        if active_state == "linked":
            _linked_pending_candidate(loader, slug)
        else:
            _stage_update(loader, slug, target="auto/active-target")
        claimed = loader._claim_pending_update(slug)
        assert claimed is not None
        claim, claim_fd, _consumed_at, snapshot = claimed
        quarantine = loader._quarantine_root() / claim.name
        if active_state == "quarantine-only":
            assert loader._discard_trusted_claim(claim) is True
        elif active_state == "lock-held-evidence":
            assert loader._commit_claim_consumption(claim, claim_fd) is True
    quarantine_info = os.lstat(quarantine)

    try:
        staged = loader.stage_skill_candidate(
            slug,
            description="distinct candidate",
            triggers=slug,
            procedure_md="## Steps\n\nDISTINCT",
            provenance=_prov(),
        )
        assert staged == f"auto/{slug}-2"
        assert not (loader._pending_root() / slug).exists()
        assert (loader._pending_root() / f"{slug}-2").is_dir()
        assert os.path.samestat(quarantine_info, os.lstat(quarantine))
    finally:
        if claim is not None and claim_fd is not None and snapshot is not None:
            try:
                if active_state != "lock-held-evidence":
                    assert loader._restore_claimed_update(
                        claim,
                        claim_fd,
                        slug,
                        snapshot,
                    ) == (loader._pending_root() / slug)
            finally:
                skills_mod.platform_compat.release_lock(claim_fd)
                os.close(claim_fd)


def test_pending_slug_claim_requires_exact_double_hyphen_prefix(loader):
    assert loader._private_state_roots_safe(create=True) is True
    claim = loader._claims_root() / "foo--x--0123456789abcdef"
    claim.mkdir()

    assert loader._pending_slug_claimed("foo--x") is True
    assert loader._pending_slug_claimed("foo") is False
    assert (
        loader.stage_skill_candidate(
            "foo",
            description="distinct candidate",
            triggers="foo",
            procedure_md="body",
            provenance=_prov(),
        )
        == "auto/foo"
    )


def test_dismiss_pending_serializes_with_atomic_claim(loader, monkeypatch):
    _write_live(loader, "dismiss-race", version=1, body="OLD")
    _stage_update(loader, "dismiss-race-update", target="auto/dismiss-race")
    pending = loader._pending_root() / "dismiss-race-update"
    entered_rename = threading.Event()
    allow_rename = threading.Event()
    claim_started = threading.Event()
    real_rename = loader._rename_skill_child_no_replace

    def blocking_rename(source, source_name, destination, destination_name, **kwargs):
        if source_name == pending.name:
            entered_rename.set()
            assert allow_rename.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    def claim():
        claim_started.set()
        return loader._claim_pending_update("dismiss-race-update")

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", blocking_rename)
    with ThreadPoolExecutor(max_workers=2) as pool:
        dismissed = pool.submit(loader.dismiss_pending_skill, "dismiss-race-update")
        assert entered_rename.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        claimed = pool.submit(claim)
        assert claim_started.wait(timeout=_THREAD_WAIT_CEILING_SECS)
        assert not claimed.done()
        allow_rename.set()
        assert dismissed.result(timeout=_THREAD_WAIT_CEILING_SECS) is True
        assert claimed.result(timeout=_THREAD_WAIT_CEILING_SECS) is None

    assert not pending.exists()
    assert not loader._claims_root().exists() or not list(loader._claims_root().iterdir())


def test_failed_claim_attempt_removes_unique_lock_file(loader):
    assert loader._claim_pending_update("missing-update") is None

    claim_locks = loader._locks_root() / "claims"
    assert not claim_locks.exists() or not list(claim_locks.glob("*.lock"))


def test_restore_claimed_update_does_not_follow_symlinked_meta(loader, monkeypatch):
    _write_live(loader, "symlinked-meta", version=1, body="OLD")
    _stage_update(loader, "symlinked-meta-update", target="auto/symlinked-meta")
    claimed = loader._claim_pending_update("symlinked-meta-update")
    assert claimed is not None
    claim, fd, _consumed_at, claim_snapshot = claimed
    meta_file = claim / ".meta.json"
    path_type = type(meta_file)
    real_read_text = path_type.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path == meta_file:
            raise AssertionError("restore reopened trusted candidate metadata")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "read_text", guarded_read_text)

    try:
        loader._restore_claimed_update(claim, fd, "symlinked-meta-update", claim_snapshot)
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    assert (loader._pending_root() / "symlinked-meta-update").is_dir()
    assert not claim.exists()
    assert not (loader._evidence_root() / claim.name).exists()
    assert not (loader._quarantine_root() / claim.name).exists()


@pytest.mark.parametrize("flow", ["update", "new", "refusal", "dismiss", "restart"])
@pytest.mark.parametrize("mutation", ["regular-swap", "symlink", "reparse"])
def test_every_flow_isolated_from_public_quarantine_metadata_mutation(
    loader,
    monkeypatch,
    tmp_path,
    flow,
    mutation,
):
    if mutation == "symlink" and os.name == "nt":
        pytest.skip("file-symlink creation needs Windows developer mode")
    slug = f"quarantine-{flow}-{mutation}"
    if flow in {"new", "dismiss"}:
        loader.stage_skill_candidate(
            slug,
            description="original description",
            triggers="original-trigger",
            procedure_md="## Steps\n\nORIGINAL",
            provenance=_prov(),
            notify=False,
        )
        target = slug
        expected_description = "original description"
    else:
        target = f"target-{flow}-{mutation}"
        if flow in {"update", "refusal"}:
            _write_live(loader, target, version=1, body="OLD")
        _stage_update(loader, slug, target=f"auto/{target}", notify=False)
        expected_description = f"updated {slug}"

    secret_file = tmp_path / f"{slug}-secret.json"
    secret_file.write_text('{"secret":"unchanged"}', encoding="utf-8")
    secret_dir = tmp_path / f"{slug}-secret-dir"
    secret_dir.mkdir()
    sentinel = secret_dir / "sentinel"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
    real_rename = loader._rename_skill_child_no_replace
    claim_names: list[str] = []

    def rename_then_mutate(source, source_name, destination, destination_name, **kwargs):
        result = real_rename(source, source_name, destination, destination_name, **kwargs)
        if source_name != slug or destination.path != loader._quarantine_root():
            return result
        claim_names.append(destination_name)
        metadata = destination.path / destination_name / ".meta.json"
        metadata.unlink()
        if mutation == "regular-swap":
            metadata.write_text(
                json.dumps(
                    {
                        "kind": "update",
                        "target": "auto/untrusted-target",
                        "description": "UNTRUSTED LATE MUTATION",
                    }
                ),
                encoding="utf-8",
            )
        elif mutation == "symlink":
            os.symlink(secret_file, metadata)
        else:
            skills_mod.platform_compat.symlink_or_junction(secret_dir, metadata)
        return result

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", rename_then_mutate)

    if flow == "update":
        assert loader.approve_pending_update_checked(slug) == f"auto/{target}"
    elif flow == "new":
        assert loader.approve_pending_skill_checked(slug) == f"auto/{slug}"
    elif flow == "refusal":
        # Refuse AFTER the claim rename, where the restore under test runs; the
        # advisory pre-claim checks would otherwise answer before any claim.
        monkeypatch.setattr(loader, "_approve_claimed_update_locked", lambda *_a, **_k: None)
        with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
            loader.approve_pending_update_checked(slug)
        assert refused.value.reason == "promotion_failed"
    elif flow == "dismiss":
        assert loader.dismiss_pending_skill(slug) is True
    else:
        claimed = loader._claim_pending_update(slug)
        assert claimed is not None
        _claim, fd, _consumed_at, _snapshot = claimed
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)
        assert [row["slug"] for row in loader.list_pending_skills()] == [slug]

    assert len(claim_names) == 1
    claim_name = claim_names[0]
    if flow in {"update", "new", "dismiss"}:
        quarantine = loader._quarantine_root() / claim_name
        evidence = loader._evidence_root() / claim_name
        assert quarantine.is_dir()
        assert evidence.is_dir()
        trusted_meta = json.loads((evidence / ".meta.json").read_text(encoding="utf-8"))
        assert trusted_meta["description"] == expected_description
        mutated_meta = quarantine / ".meta.json"
    else:
        assert not (loader._quarantine_root() / claim_name).exists()
        assert not (loader._claims_root() / claim_name).exists()
        assert not (loader._evidence_root() / claim_name).exists()
        mutated_meta = loader._pending_root() / slug / ".meta.json"

    if mutation == "regular-swap":
        assert json.loads(mutated_meta.read_text(encoding="utf-8"))["description"] == (
            "UNTRUSTED LATE MUTATION"
        )
    else:
        assert skills_mod.platform_compat.is_link_or_junction(mutated_meta)
    assert secret_file.read_text(encoding="utf-8") == '{"secret":"unchanged"}'
    assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits renaming an opened parent")
@pytest.mark.parametrize(
    "phase",
    ["claim-pending", "claim-quarantine", "restore-pending", "restore-quarantine"],
)
@pytest.mark.parametrize("replacement_kind", ["directory", "symlink", "none"])
def test_claim_and_restore_revalidate_captured_parent_matrix(
    loader,
    monkeypatch,
    tmp_path,
    phase,
    replacement_kind,
):
    """A swapped source or destination parent can only refuse the operation."""
    slug = f"parent-swap-{phase}-{replacement_kind}"
    _stage_update(loader, slug, target="auto/parent-swap-target")
    claim = None
    claim_fd = None
    claim_snapshot = None
    if phase.startswith("restore"):
        claimed = loader._claim_pending_update(slug)
        assert claimed is not None
        claim, claim_fd, _consumed_at, claim_snapshot = claimed

    real_rename = loader._rename_skill_child_no_replace
    swapped: dict[str, Path] = {}
    sentinel_path: list[Path] = []

    def swap_parent(parent: Path) -> None:
        detached = parent.with_name(f"{parent.name}-detached-{secrets.token_hex(4)}")
        parent.rename(detached)
        replacement = tmp_path / f"protected-{phase}-{replacement_kind}"
        protected_claim = replacement / (claim.name if claim is not None else "profiles")
        protected_claim.mkdir(parents=True)
        sentinel = protected_claim / "sentinel"
        sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
        if replacement_kind == "symlink":
            parent.symlink_to(replacement, target_is_directory=True)
        else:
            replacement.rename(parent)
            sentinel = parent / protected_claim.relative_to(replacement) / "sentinel"
        swapped["detached"] = detached
        sentinel_path.append(sentinel)

    def swap_then_rename(source, source_name, destination, destination_name, **kwargs):
        should_swap = (
            replacement_kind != "none"
            and not swapped
            and (
                (phase == "claim-pending" and source_name == slug)
                or (phase == "claim-quarantine" and source_name == slug)
                or (
                    phase in {"restore-pending", "restore-quarantine"}
                    and claim is not None
                    and source_name == claim.name
                )
            )
        )
        if should_swap:
            if phase == "claim-pending":
                parent = source.path
            elif phase == "claim-quarantine":
                parent = destination.path
            elif phase == "restore-pending":
                parent = destination.path
            else:
                parent = source.path
            swap_parent(parent)
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", swap_then_rename)

    if phase.startswith("claim"):
        result = loader._claim_pending_update(slug)
        if replacement_kind == "none":
            assert result is not None
            claimed_path, claimed_fd, _consumed_at, snapshot = result
            try:
                assert (
                    loader._restore_claimed_update(
                        claimed_path,
                        claimed_fd,
                        slug,
                        snapshot,
                    )
                    is not None
                )
            finally:
                skills_mod.platform_compat.release_lock(claimed_fd)
                os.close(claimed_fd)
        else:
            assert result is None
    else:
        assert claim is not None and claim_fd is not None and claim_snapshot is not None
        try:
            restored = loader._restore_claimed_update(
                claim,
                claim_fd,
                slug,
                claim_snapshot,
            )
        finally:
            skills_mod.platform_compat.release_lock(claim_fd)
            os.close(claim_fd)
        if replacement_kind == "none":
            assert restored == loader._pending_root() / slug
        else:
            assert restored is None

    if replacement_kind != "none":
        assert sentinel_path[0].read_text(encoding="utf-8") == "DO NOT TOUCH"
        assert list(sentinel_path[0].parent.iterdir()) == [sentinel_path[0]]
        detached = swapped["detached"]
        if phase == "claim-pending":
            assert (detached / slug / "SKILL.md").is_file()
        elif phase == "claim-quarantine":
            assert (loader._pending_root() / slug / "SKILL.md").is_file()
        elif phase == "restore-pending":
            assert claim is not None and claim.is_dir()
        else:
            assert any(child.name.startswith(f"{slug}--") for child in detached.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits renaming an opened parent")
@pytest.mark.parametrize("replacement_kind", ["directory", "symlink", "none"])
def test_completed_claim_delete_revalidates_parent_matrix(
    loader,
    monkeypatch,
    tmp_path,
    replacement_kind,
):
    """Cleanup deletes the captured claim or nothing, never a replacement tree."""
    slug = f"delete-parent-swap-{replacement_kind}"
    _stage_update(loader, slug, target="auto/delete-parent-swap")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, claim_fd, _consumed_at, _snapshot = claimed
    assert loader._write_completion_marker(claim) is True
    assert (
        loader._commit_claim_lock_state(
            claim_fd,
            loader._claim_lock_path(claim.name),
            claim.name,
        )
        is True
    )

    real_remove = skills_mod.pinned_fs.remove_tree_pinned
    swapped: dict[str, Path] = {}
    sentinel_path: list[Path] = []

    def swap_then_remove(resolved_path, **kwargs):
        if replacement_kind != "none" and not swapped:
            claims_root = loader._claims_root()
            detached = claims_root.with_name(f"{claims_root.name}-detached-{secrets.token_hex(4)}")
            claims_root.rename(detached)
            replacement = tmp_path / f"protected-delete-{replacement_kind}"
            protected_claim = replacement / claim.name
            protected_claim.mkdir(parents=True)
            sentinel = protected_claim / "sentinel"
            sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
            if replacement_kind == "symlink":
                claims_root.symlink_to(replacement, target_is_directory=True)
            else:
                replacement.rename(claims_root)
                sentinel = claims_root / claim.name / "sentinel"
            swapped["detached"] = detached
            sentinel_path.append(sentinel)
        return real_remove(resolved_path, **kwargs)

    monkeypatch.setattr(skills_mod.pinned_fs, "remove_tree_pinned", swap_then_remove)
    try:
        cleaned = loader._cleanup_completed_claim(claim, claim_fd)
    finally:
        skills_mod.platform_compat.release_lock(claim_fd)
        os.close(claim_fd)

    if replacement_kind == "none":
        assert cleaned is True
        assert not claim.exists()
    else:
        assert cleaned is False
        sentinel = sentinel_path[0]
        assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"
        assert list(sentinel.parent.iterdir()) == [sentinel]
        assert not (sentinel.parent / ".promoted").exists()
        assert (swapped["detached"] / claim.name / "SKILL.md").is_file()


def test_spoofed_abandoned_completion_marker_restores_claim(loader):
    _write_live(loader, "spoofed-recovery", version=1, body="OLD")
    _stage_update(
        loader,
        "spoofed-recovery-update",
        target="auto/spoofed-recovery",
    )
    claimed = loader._claim_pending_update("spoofed-recovery-update")
    assert claimed is not None
    claim, fd, _consumed_at, _generation = claimed
    (claim / ".promoted").write_text("not-this-claim\n", encoding="utf-8")
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    assert [row["slug"] for row in loader.list_pending_skills()] == ["spoofed-recovery-update"]
    restored = loader._pending_root() / "spoofed-recovery-update"
    assert restored.is_dir()
    assert not (restored / ".promoted").exists()
    assert not claim.exists()


def test_promoted_abandoned_claim_is_retained_as_separated_evidence(loader):
    _write_live(loader, "promoted-recovery", version=1, body="OLD")
    _stage_update(
        loader,
        "promoted-recovery-update",
        target="auto/promoted-recovery",
    )
    claimed = loader._claim_pending_update("promoted-recovery-update")
    assert claimed is not None
    claim, fd, _consumed_at, _generation = claimed
    quarantine = loader._quarantine_root() / claim.name
    evidence = loader._evidence_root() / claim.name
    assert loader._write_completion_marker(claim) is True
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    assert loader.list_pending_skills() == []
    assert not claim.exists()
    assert quarantine.is_dir()
    assert evidence.is_dir()
    assert loader._claim_lock_path(claim.name).is_file()


def test_restart_recovers_crash_before_live_publish_without_duplicate(loader, monkeypatch):
    """Prepared-before-publish restores once and removes its orphan snapshot."""
    _write_live(loader, "crash-before", version=1, body="OLD")
    slug = "crash-before-update"
    _stage_update(
        loader,
        slug,
        target="auto/crash-before",
        base_version=None,
        scripts=[{"filename": "run.py", "content": "print('after')\n"}],
    )
    live = loader._dir / "auto" / "crash-before"
    real_rename = loader._rename_skill_child_no_replace

    class InjectedCrash(BaseException):
        pass

    def crash_at_generation_publish(source, source_name, destination, destination_name, **kwargs):
        if (
            destination.path == live.parent
            and destination_name == live.name
            and source_name.startswith(".publish-")
        ):
            raise InjectedCrash()
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_rename_skill_child_no_replace",
            crash_at_generation_publish,
        )
        # A real process exit does not execute the in-process refusal restore.
        patch.setattr(loader, "_restore_claimed_update", lambda _claim, _slug, *_rest: None)
        with pytest.raises(InjectedCrash):
            loader.approve_pending_update_checked(slug)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert (restarted._pending_root() / slug / "scripts" / "run.py").read_text(
        encoding="utf-8"
    ) == "print('after')\n"
    assert restarted.get_auto_skill_version("auto/crash-before") == 1
    versions = restarted._versions_root("crash-before")
    assert not versions.exists() or not list(versions.glob("v*-SKILL.md"))
    assert not restarted._claims_root().exists() or not list(restarted._claims_root().iterdir())


def test_restart_requeues_prepared_new_skill_when_live_target_is_missing(loader, monkeypatch):
    slug = "new-recovery-missing"
    loader.stage_skill_candidate(
        slug,
        description="new recovery",
        triggers="new recovery",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
    )
    live_dir = loader._dir / "auto" / slug
    real_rename = loader._rename_skill_child_no_replace

    class InjectedCrash(BaseException):
        pass

    def crash_before_new_publish(source, source_name, destination, destination_name, **kwargs):
        if source_name.startswith(".publish-") and (
            destination.path == live_dir.parent and destination_name == live_dir.name
        ):
            raise InjectedCrash()
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_rename_skill_child_no_replace",
            crash_before_new_publish,
        )
        patch.setattr(loader, "_restore_failed_promotion_claim", lambda *_args: None)
        with pytest.raises(InjectedCrash):
            loader.approve_pending_skill_checked(slug)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)

    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert not live_dir.exists()
    assert not restarted._claims_root().exists() or not list(restarted._claims_root().iterdir())


def test_restart_preserves_concurrent_new_skill_live_target(loader, monkeypatch):
    slug = "new-recovery-occupied"
    loader.stage_skill_candidate(
        slug,
        description="new recovery",
        triggers="new recovery",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
    )
    live_dir = loader._dir / "auto" / slug
    real_rename = loader._rename_skill_child_no_replace

    class InjectedCrash(BaseException):
        pass

    def crash_before_new_publish(source, source_name, destination, destination_name, **kwargs):
        if source_name.startswith(".publish-") and (
            destination.path == live_dir.parent and destination_name == live_dir.name
        ):
            raise InjectedCrash()
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_rename_skill_child_no_replace",
            crash_before_new_publish,
        )
        patch.setattr(loader, "_restore_failed_promotion_claim", lambda *_args: None)
        with pytest.raises(InjectedCrash):
            loader.approve_pending_skill_checked(slug)

    _write_live(loader, slug, version=9, body="CONCURRENT NEW TARGET")
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    stage, _backup = loader._publication_paths(claims[0].name)
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)

    assert restarted.list_pending_skills() == []
    assert "CONCURRENT NEW TARGET" in (live_dir / "SKILL.md").read_text(encoding="utf-8")
    assert restarted.get_auto_skill_version(f"auto/{slug}") == 9
    assert claims[0].is_dir()
    assert restarted._skill_tree_hash(stage) is not None


def test_restart_consumes_crash_after_live_publish_without_resurrection(loader, monkeypatch):
    """A published update is retained as evidence, never re-queued or reapplied."""
    _write_live(loader, "crash-after", version=1, body="OLD")
    slug = "crash-after-update"
    _stage_update(
        loader,
        slug,
        target="auto/crash-after",
        base_version=None,
        scripts=[{"filename": "run.py", "content": "print('after')\n"}],
    )

    class InjectedCrash(BaseException):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_write_completion_marker",
            lambda _claim: (_ for _ in ()).throw(InjectedCrash()),
        )
        with pytest.raises(InjectedCrash):
            loader.approve_pending_update_checked(slug)

    assert loader.get_auto_skill_version("auto/crash-after") == 2
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert restarted.list_pending_skills() == []
    assert restarted.get_auto_skill_version("auto/crash-after") == 2
    live_script = restarted._dir / "auto" / "crash-after" / "scripts" / "run.py"
    assert live_script.read_text(encoding="utf-8") == "print('after')\n"
    if os.name != "nt":
        assert live_script.stat().st_mode & 0o111
    snapshots = list(restarted._versions_root("crash-after").glob("v*-SKILL.md"))
    assert [path.name for path in snapshots] == ["v1-SKILL.md"]
    assert not restarted._claims_root().exists() or not list(restarted._claims_root().iterdir())
    evidence = list(restarted._evidence_root().glob(f"{slug}--*"))
    assert len(evidence) == 1
    _stage, retained = restarted._publication_paths(evidence[0].name)
    assert "OLD" in (retained / "SKILL.md").read_text(encoding="utf-8")
    assert restarted._claim_lock_path(evidence[0].name).is_file()


@pytest.mark.parametrize(
    "mutation",
    ["script-bytes", "script-missing", "script-mode", "metadata", "version"],
)
def test_restart_retains_ambiguous_live_generation_without_discard(loader, monkeypatch, mutation):
    target = f"partial-{mutation}"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    live_scripts = live_dir / "scripts"
    live_scripts.mkdir()
    old_script = live_scripts / "run.py"
    old_script.write_text("print('before')\n", encoding="utf-8")
    before_hash = loader._skill_tree_hash(live_dir)
    _stage_update(
        loader,
        slug,
        target=f"auto/{target}",
        scripts=[{"filename": "run.py", "content": "print('after')\n"}],
    )

    class InjectedCrash(BaseException):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_write_completion_marker",
            lambda _claim: (_ for _ in ()).throw(InjectedCrash()),
        )
        with pytest.raises(InjectedCrash):
            loader.approve_pending_update_checked(slug)

    published_script = live_dir / "scripts" / "run.py"
    assert loader.get_auto_skill_version(f"auto/{target}") == 2
    if mutation == "script-bytes":
        published_script.write_text("print('partial')\n", encoding="utf-8")
    elif mutation == "script-missing":
        published_script.unlink()
    elif mutation == "script-mode":
        current = stat.S_IMODE(published_script.stat().st_mode)
        if os.name == "nt":
            os.chmod(published_script, stat.S_IREAD if current & stat.S_IWRITE else stat.S_IWRITE)
        else:
            os.chmod(published_script, current ^ stat.S_IXUSR)
    else:
        live_skill = live_dir / "SKILL.md"
        body = live_skill.read_text(encoding="utf-8")
        if mutation == "metadata":
            body = body.replace("created_at: 2020-01-01", "created_at: 2030-01-01")
        else:
            body = body.replace("version: 2", "version: 99")
        live_skill.write_text(body, encoding="utf-8")

    ambiguous_hash = loader._skill_tree_hash(live_dir)
    assert ambiguous_hash is not None and ambiguous_hash != before_hash
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)

    assert restarted.list_pending_skills() == []
    assert restarted.list_pending_skills() == []
    assert restarted._skill_tree_hash(live_dir) == ambiguous_hash
    claims = list(restarted._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    stage, backup = restarted._publication_paths(claims[0].name)
    assert not stage.exists()
    assert restarted._skill_tree_hash(backup) == before_hash


def test_recovery_retains_live_edit_landing_after_hash_snapshot(loader, monkeypatch):
    target = "recovery-hash-race"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    before_hash = loader._skill_tree_hash(live_dir)
    _stage_update(loader, slug, target=f"auto/{target}")

    class InjectedCrash(BaseException):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_write_completion_marker",
            lambda _claim: (_ for _ in ()).throw(InjectedCrash()),
        )
        with pytest.raises(InjectedCrash):
            loader.approve_pending_update_checked(slug)

    marker = live_dir / "concurrent-edit"
    marker.write_text("before hash\n", encoding="utf-8")
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    _stage, backup = loader._publication_paths(claims[0].name)
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    real_hash = restarted._skill_tree_hash_child
    edit_landed = False

    def hash_then_edit(parent, name):
        nonlocal edit_landed
        result = real_hash(parent, name)
        if parent.path == live_dir.parent and name == target and not edit_landed:
            marker.write_text("after hash\n", encoding="utf-8")
            edit_landed = True
        return result

    monkeypatch.setattr(restarted, "_skill_tree_hash_child", hash_then_edit)

    assert restarted.list_pending_skills() == []
    assert edit_landed is True
    assert marker.read_text(encoding="utf-8") == "after hash\n"
    assert claims[0].is_dir()
    assert restarted._skill_tree_hash(backup) == before_hash


def test_recovery_no_replace_preserves_concurrent_recreated_live_target(loader, monkeypatch):
    target = "recovery-recreated-live"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    before_hash = loader._skill_tree_hash(live_dir)
    _stage_update(loader, slug, target=f"auto/{target}")
    real_rename = loader._rename_skill_child_no_replace

    class InjectedCrash(BaseException):
        pass

    def crash_before_stage_publish(source, source_name, destination, destination_name, **kwargs):
        if source_name.startswith(".publish-") and (
            destination.path == live_dir.parent and destination_name == live_dir.name
        ):
            raise InjectedCrash()
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            loader,
            "_rename_skill_child_no_replace",
            crash_before_stage_publish,
        )
        patch.setattr(loader, "_restore_failed_promotion_claim", lambda *_args: None)
        with pytest.raises(InjectedCrash):
            loader.approve_pending_update_checked(slug)

    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    stage, backup = loader._publication_paths(claims[0].name)
    assert not live_dir.exists()
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    real_restarted_rename = restarted._rename_skill_child_no_replace
    writer_landed = False

    def writer_wins_restore(source, source_name, destination, destination_name, **kwargs):
        nonlocal writer_landed
        if (
            source.path == backup.parent
            and source_name == backup.name
            and destination.path == live_dir.parent
            and destination_name == live_dir.name
            and not writer_landed
        ):
            _write_live(loader, target, version=7, body="CONCURRENT")
            writer_landed = True
        return real_restarted_rename(source, source_name, destination, destination_name)

    monkeypatch.setattr(
        restarted,
        "_rename_skill_child_no_replace",
        writer_wins_restore,
    )

    assert restarted.list_pending_skills() == []
    assert writer_landed is True
    assert "CONCURRENT" in (live_dir / "SKILL.md").read_text(encoding="utf-8")
    assert restarted.get_auto_skill_version(f"auto/{target}") == 7
    assert claims[0].is_dir()
    assert restarted._skill_tree_hash(stage) is not None
    assert restarted._skill_tree_hash(backup) == before_hash


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permits a writable descriptor to survive the live-tree rename",
)
def test_in_process_rollback_retains_late_backup_write_after_recovery_hash(
    loader,
    monkeypatch,
):
    target = "rollback-late-backup-write"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    retained_fd = os.open(live_dir / "SKILL.md", os.O_WRONLY)
    _stage_update(loader, slug, target=f"auto/{target}")
    real_rename = loader._rename_skill_child_no_replace
    real_hash = loader._skill_tree_hash_child
    recreated_live = False
    backup_path: Path | None = None
    backup_hashes = 0
    late_generation = b"LATE RETAINED BACKUP WRITE\n"

    def recreate_before_generation(source, source_name, destination, destination_name, **kwargs):
        nonlocal recreated_live, backup_path
        if (
            source_name.startswith(".publish-")
            and destination.path == live_dir.parent
            and destination_name == live_dir.name
            and not recreated_live
        ):
            backup_path = loader._live_quarantine_root() / source_name.removeprefix(".publish-")
            shutil.copytree(
                backup_path, destination.path / destination_name, copy_function=shutil.copy2
            )
            recreated_live = True
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    def hash_then_write_through_retained_descriptor(parent, name):
        nonlocal backup_hashes
        result = real_hash(parent, name)
        if parent.path == loader._live_quarantine_root():
            backup_hashes += 1
            if backup_hashes == 2:
                os.lseek(retained_fd, 0, os.SEEK_SET)
                os.ftruncate(retained_fd, 0)
                os.write(retained_fd, late_generation)
                os.fsync(retained_fd)
        return result

    monkeypatch.setattr(
        loader,
        "_rename_skill_child_no_replace",
        recreate_before_generation,
    )
    monkeypatch.setattr(
        loader,
        "_skill_tree_hash_child",
        hash_then_write_through_retained_descriptor,
    )
    try:
        with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
            loader.approve_pending_update_checked(slug)
        assert refused.value.reason == "promotion_failed"
    finally:
        os.close(retained_fd)

    assert recreated_live is True
    assert backup_hashes == 2
    assert backup_path is not None
    claim_name = backup_path.name
    claim = loader._claims_root() / claim_name
    stage, backup = loader._publication_paths(claim_name)
    quarantine = loader._quarantine_root() / claim_name
    lock_path = loader._claim_lock_path(claim_name)
    assert backup == backup_path
    assert (backup / "SKILL.md").read_bytes() == late_generation
    assert claim.is_dir()
    assert stage.is_dir()
    assert quarantine.is_dir()
    assert lock_path.is_file()
    assert not (loader._pending_root() / slug).exists()
    journal_fd = _open_lock_for_test(loader, lock_path)
    try:
        journal = loader._authenticated_claim_publication(
            journal_fd,
            lock_path,
            claim_name,
        )
    finally:
        os.close(journal_fd)
    assert journal is not None and journal["state"] == "prepared"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permits a writable descriptor to survive a gateway restart",
)
def test_restart_rollback_retains_late_backup_write_after_recovery_hash(
    loader,
    monkeypatch,
):
    target = "restart-late-backup-write"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    retained_fd = os.open(live_dir / "SKILL.md", os.O_WRONLY)
    _stage_update(loader, slug, target=f"auto/{target}")
    real_rename = loader._rename_skill_child_no_replace
    backup_path: Path | None = None
    recreated_live = False

    def recreate_before_generation(source, source_name, destination, destination_name, **kwargs):
        nonlocal backup_path, recreated_live
        if (
            source_name.startswith(".publish-")
            and destination.path == live_dir.parent
            and destination_name == live_dir.name
            and not recreated_live
        ):
            backup_path = loader._live_quarantine_root() / source_name.removeprefix(".publish-")
            shutil.copytree(
                backup_path, destination.path / destination_name, copy_function=shutil.copy2
            )
            recreated_live = True
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    with monkeypatch.context() as initial_process:
        initial_process.setattr(
            loader,
            "_rename_skill_child_no_replace",
            recreate_before_generation,
        )
        initial_process.setattr(
            loader,
            "_restore_failed_promotion_claim",
            lambda *_args: False,
        )
        with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
            loader.approve_pending_update_checked(slug)
        assert refused.value.reason == "promotion_failed"

    assert recreated_live is True
    assert backup_path is not None
    claim_name = backup_path.name
    claim = loader._claims_root() / claim_name
    stage, backup = loader._publication_paths(claim_name)
    quarantine = loader._quarantine_root() / claim_name
    lock_path = loader._claim_lock_path(claim_name)
    assert backup == backup_path
    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    real_restarted_hash = restarted._skill_tree_hash_child
    wrote_late_generation = False
    late_generation = b"RESTART RETAINED BACKUP WRITE\n"

    def hash_then_write_through_retained_descriptor(parent, name):
        nonlocal wrote_late_generation
        result = real_restarted_hash(parent, name)
        if parent.path == backup.parent and name == backup.name and not wrote_late_generation:
            os.lseek(retained_fd, 0, os.SEEK_SET)
            os.ftruncate(retained_fd, 0)
            os.write(retained_fd, late_generation)
            os.fsync(retained_fd)
            wrote_late_generation = True
        return result

    monkeypatch.setattr(
        restarted,
        "_skill_tree_hash_child",
        hash_then_write_through_retained_descriptor,
    )
    try:
        pending = restarted.list_pending_skills()
    finally:
        os.close(retained_fd)

    assert pending == []
    assert wrote_late_generation is True
    assert (backup / "SKILL.md").read_bytes() == late_generation
    assert claim.is_dir()
    assert stage.is_dir()
    assert quarantine.is_dir()
    assert lock_path.is_file()
    assert not (restarted._pending_root() / slug).exists()
    journal_fd = _open_lock_for_test(restarted, lock_path)
    try:
        journal = restarted._authenticated_claim_publication(
            journal_fd,
            lock_path,
            claim_name,
        )
    finally:
        os.close(journal_fd)
    assert journal is not None and journal["state"] == "prepared"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permits a writable descriptor to survive the live-tree rename",
)
@pytest.mark.parametrize("mutation", ["edit", "replace"])
def test_post_publish_backup_drift_preserves_newer_live_and_ambiguous_artifacts(
    loader,
    monkeypatch,
    mutation,
):
    target = f"combined-backup-live-{mutation}"
    slug = f"{target}-update"
    live_dir = _write_live(loader, target, version=1, body="OLD")
    retained_fd = os.open(live_dir / "SKILL.md", os.O_WRONLY)
    _stage_update(loader, slug, target=f"auto/{target}")
    real_hash = loader._skill_tree_hash_child
    backup_hashes = 0
    race_landed = False
    late_backup = b"LATE BACKUP GENERATION\n"
    detached_live = live_dir.with_name(f"{target}-detached-after")

    def race_before_second_backup_hash(parent, name):
        nonlocal backup_hashes, race_landed
        if parent.path == loader._live_quarantine_root():
            backup_hashes += 1
            if backup_hashes == 2:
                os.lseek(retained_fd, 0, os.SEEK_SET)
                os.ftruncate(retained_fd, 0)
                os.write(retained_fd, late_backup)
                os.fsync(retained_fd)
                if mutation == "edit":
                    (live_dir / "SKILL.md").write_text(
                        "CONCURRENT LIVE EDIT\n",
                        encoding="utf-8",
                    )
                else:
                    live_dir.rename(detached_live)
                    _write_live(loader, target, version=7, body="CONCURRENT LIVE REPLACEMENT")
                race_landed = True
        return real_hash(parent, name)

    monkeypatch.setattr(
        loader,
        "_skill_tree_hash_child",
        race_before_second_backup_hash,
    )
    try:
        with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
            loader.approve_pending_update_checked(slug)
        assert refused.value.reason == "promotion_failed"
    finally:
        os.close(retained_fd)

    assert race_landed is True
    expected_live = "EDIT" if mutation == "edit" else "REPLACEMENT"
    assert expected_live in (live_dir / "SKILL.md").read_text(encoding="utf-8")
    if mutation == "replace":
        assert "new steps" in (detached_live / "SKILL.md").read_text(encoding="utf-8")

    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    claim = claims[0]
    stage, backup = loader._publication_paths(claim.name)
    quarantine = loader._quarantine_root() / claim.name
    lock_path = loader._claim_lock_path(claim.name)
    assert not stage.exists()
    assert (backup / "SKILL.md").read_bytes() == late_backup
    assert claim.is_dir()
    assert quarantine.is_dir()
    assert lock_path.is_file()
    assert not (loader._pending_root() / slug).exists()

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert expected_live in (live_dir / "SKILL.md").read_text(encoding="utf-8")
    assert (backup / "SKILL.md").read_bytes() == late_backup
    assert claim.is_dir()
    assert quarantine.is_dir()
    assert lock_path.is_file()
    if mutation == "replace":
        assert "new steps" in (detached_live / "SKILL.md").read_text(encoding="utf-8")

"""Whole-publication invariants and lock order on fail-closed hosts.

Covers the candidate-inode and tree-hash guards, final evidence-transition
revalidation, completion-marker parent-sync ordering, the one documented
lock-acquisition order every auto-skill path takes, the delegated-sandbox
fail-closed cases, and retained old-live evidence across a rebase.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import pytest

import kiro_crew.skills as skills_mod
from kiro_crew.skills import ClaimRefusal

from .skill_versioning_support import (
    _REAL_AUTHORITY_SANDBOX_REFUSAL,
    _authority_path,
    _open_lock_for_test,
    _prov,
    _stage_update,
    _write_live,
)

# This module's auto-skill boundaries initialize explicitly through the
# ``loader`` fixture; every ``uninitialized_loader`` path must see production
# fail closed.
pytestmark = pytest.mark.usefixtures("no_auto_skill_authority_startup")


# ── whole-publication invariant regressions ──


def test_live_drift_at_generation_swap_is_restored_without_overwrite(loader, monkeypatch):
    """A live edit landing at the mutation boundary wins over the candidate."""
    live_dir = _write_live(loader, "swap-drift", version=1, body="ORIGINAL")
    live_file = live_dir / "SKILL.md"
    slug = "swap-drift-update"
    _stage_update(loader, slug, target="auto/swap-drift", base_version=1)
    edited = live_file.read_text(encoding="utf-8").replace("ORIGINAL", "CONCURRENT EDIT")
    real_rename = loader._rename_skill_child_no_replace
    injected = False

    def edit_before_capture(source, source_name, destination, destination_name, **kwargs):
        nonlocal injected
        if (
            source.path == live_dir.parent
            and source_name == live_dir.name
            and destination.path == loader._live_quarantine_root()
        ):
            assert injected is False
            injected = True
            live_file.write_text(edited, encoding="utf-8")
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", edit_before_capture)

    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked(slug)
    assert refused.value.reason == "promotion_failed"
    assert injected is True
    assert live_file.read_text(encoding="utf-8") == edited
    assert not (live_dir / ".versions").exists()
    assert (loader._pending_root() / slug / "SKILL.md").is_file()


@pytest.mark.parametrize("evidence_kind", ["candidate", "old-live"])
@pytest.mark.parametrize("evidence_change", ["missing", "replacement"])
def test_final_evidence_transition_revalidates_all_public_identities(
    loader,
    monkeypatch,
    evidence_kind,
    evidence_change,
):
    target = f"final-evidence-{evidence_kind}-{evidence_change}"
    slug = f"{target}-update"
    _write_live(loader, target, version=1, body="BEFORE")
    _stage_update(loader, slug, target=f"auto/{target}")
    real_rename = loader._rename_skill_child_no_replace
    displaced: list[Path] = []

    def move_private_then_change_public(
        source,
        source_name,
        destination,
        destination_name,
        **kwargs,
    ):
        result = real_rename(
            source,
            source_name,
            destination,
            destination_name,
            **kwargs,
        )
        if source.path == loader._claims_root() and destination.path == loader._evidence_root():
            _stage, backup = loader._publication_paths(source_name)
            public = loader._quarantine_root() / source_name
            if evidence_kind == "old-live":
                public = backup
            moved = public.with_name(f"{public.name}-{evidence_kind}-{evidence_change}")
            public.rename(moved)
            displaced.append(moved)
            if evidence_change == "replacement":
                public.mkdir()
                (public / "SKILL.md").write_text("REPLACEMENT", encoding="utf-8")
        return result

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", move_private_then_change_public)

    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked(slug)
    assert refused.value.reason == "promotion_failed"
    assert loader.get_auto_skill_version(f"auto/{target}") == 2
    assert len(displaced) == 1 and displaced[0].is_dir()
    evidence_claims = list(loader._evidence_root().glob(f"{slug}--*"))
    assert len(evidence_claims) == 1
    claim = evidence_claims[0]
    lock_path = loader._claim_lock_path(claim.name)
    assert lock_path.is_file()

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert claim.is_dir()
    assert lock_path.is_file()
    assert displaced[0].is_dir()
    _stage, old_live = restarted._publication_paths(claim.name)
    candidate = restarted._quarantine_root() / claim.name
    changed_path = candidate if evidence_kind == "candidate" else old_live
    if evidence_change == "replacement":
        assert (changed_path / "SKILL.md").read_text(encoding="utf-8") == "REPLACEMENT"
    else:
        assert not changed_path.exists()


@pytest.mark.parametrize("evidence_change", ["missing", "replacement"])
def test_update_commit_refuses_changed_old_live_evidence_before_retention(
    loader,
    monkeypatch,
    evidence_change,
):
    target = f"commit-evidence-{evidence_change}"
    slug = f"{target}-update"
    _write_live(loader, target, version=1, body="BEFORE")
    _stage_update(loader, slug, target=f"auto/{target}")
    real_marker = loader._write_completion_marker
    displaced: list[Path] = []

    def marker_then_change_evidence(claim, expected_claim_identity=None):
        completed = real_marker(claim, expected_claim_identity)
        _stage, backup = loader._publication_paths(claim.name)
        moved = backup.with_name(f"{backup.name}-{evidence_change}")
        backup.rename(moved)
        displaced.append(moved)
        if evidence_change == "replacement":
            backup.mkdir()
            (backup / "SKILL.md").write_text("REPLACEMENT", encoding="utf-8")
        return completed

    monkeypatch.setattr(loader, "_write_completion_marker", marker_then_change_evidence)
    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked(slug)
    assert refused.value.reason == "promotion_failed"
    assert loader.get_auto_skill_version(f"auto/{target}") == 2
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    claim = claims[0]
    lock_path = loader._claim_lock_path(claim.name)
    assert lock_path.is_file()
    assert (claim / ".promoted").is_file()
    assert displaced and displaced[0].is_dir()

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert claim.is_dir()
    assert lock_path.is_file()
    assert displaced[0].is_dir()
    if evidence_change == "replacement":
        _stage, expected_name = restarted._publication_paths(claim.name)
        assert (expected_name / "SKILL.md").read_text(encoding="utf-8") == "REPLACEMENT"


def test_marker_parent_sync_failure_preserves_prepared_journal_for_consumption(loader, monkeypatch):
    """A failed parent sync cannot become a completion or evidence transition."""
    _write_live(loader, "marker-fallback", version=1, body="OLD")
    slug = "marker-fallback-update"
    _stage_update(loader, slug, target="auto/marker-fallback", base_version=1)
    real_sync = loader._sync_pinned_parent
    failed = False

    def fail_claim_parent_once(parent):
        nonlocal failed
        if (
            not failed
            and parent.path.parent == loader._claims_root()
            and parent.path.name.startswith(f"{slug}--")
        ):
            failed = True
            raise OSError("injected marker parent sync failure")
        return real_sync(parent)

    monkeypatch.setattr(loader, "_sync_pinned_parent", fail_claim_parent_once)
    monkeypatch.setattr(
        loader,
        "_commit_claim_evidence_state",
        lambda *_args: pytest.fail("evidence transition ran without a durable marker"),
    )

    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked(slug)
    assert refused.value.reason == "promotion_failed"
    assert failed is True
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    claim = claims[0]
    assert not (claim / ".promoted").exists()
    lock_path = loader._claim_lock_path(claim.name)
    fd = _open_lock_for_test(loader, lock_path)
    acquired = skills_mod.platform_compat.try_acquire_lock(fd, exclusive=True)
    try:
        assert acquired is True
        journal = loader._authenticated_claim_publication(fd, lock_path, claim.name)
        assert journal is not None and journal["state"] == "prepared"
    finally:
        if acquired:
            skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert restarted.get_auto_skill_version("auto/marker-fallback") == 2
    assert not claim.exists()


def test_completion_marker_parent_sync_precedes_evidence_transition(loader, monkeypatch):
    slug = "marker-sync-order"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
    )
    events: list[str] = []
    real_sync = loader._sync_pinned_parent
    real_evidence = loader._commit_claim_evidence_state

    def record_claim_parent_sync(parent):
        result = real_sync(parent)
        if parent.path.parent == loader._claims_root() and parent.path.name.startswith(f"{slug}--"):
            events.append("marker-parent-synced")
        return result

    def record_evidence_transition(*args):
        events.append("evidence-transition")
        assert "marker-parent-synced" in events
        return real_evidence(*args)

    monkeypatch.setattr(loader, "_sync_pinned_parent", record_claim_parent_sync)
    monkeypatch.setattr(loader, "_commit_claim_evidence_state", record_evidence_transition)

    assert loader.approve_pending_skill_checked(slug) == f"auto/{slug}"
    assert events.index("marker-parent-synced") < events.index("evidence-transition")


def test_restart_recovers_crash_after_durable_marker_before_evidence_transition(
    loader, monkeypatch
):
    slug = "marker-power-loss"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
    )

    class InjectedCrash(BaseException):
        pass

    def crash_before_evidence(*_args):
        raise InjectedCrash()

    monkeypatch.setattr(loader, "_commit_claim_evidence_state", crash_before_evidence)
    with pytest.raises(InjectedCrash):
        loader.approve_pending_skill_checked(slug)

    claims = list(loader._claims_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    claim = claims[0]
    assert loader._authenticated_completion_marker(claim) is True
    lock_path = loader._claim_lock_path(claim.name)
    fd = _open_lock_for_test(loader, lock_path)
    try:
        journal = loader._authenticated_claim_publication(fd, lock_path, claim.name)
        assert journal is not None and journal["state"] == "prepared"
    finally:
        os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert (restarted._dir / "auto" / slug / "SKILL.md").is_file()
    assert not claim.exists()
    assert (restarted._evidence_root() / claim.name).is_dir()


def test_evidence_state_failure_uses_authenticated_marker(loader, monkeypatch):
    """A committed marker makes a failed evidence-state write recoverable."""
    slug = "journal-fallback"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nNEW",
        provenance=_prov(),
    )
    monkeypatch.setattr(loader, "_commit_claim_evidence_state", lambda *_args: False)

    assert loader.approve_pending_skill_checked(slug) == "auto/journal-fallback"
    claims = list(loader._claims_root().glob(f"{slug}--*"))
    quarantines = list(loader._quarantine_root().glob(f"{slug}--*"))
    assert len(claims) == 1
    assert len(quarantines) == 1
    assert loader._authenticated_completion_marker(claims[0]) is True

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert restarted.get_auto_skill_version("auto/journal-fallback") == 1
    assert not claims[0].exists()
    assert (restarted._evidence_root() / claims[0].name).is_dir()
    assert quarantines[0].is_dir()


def test_string_false_has_scripts_is_not_a_boolean_true(loader, snapshot_reader):
    """Untrusted JSON strings never opt a candidate into script handling."""
    _write_live(loader, "strict-bool", version=1, body="OLD")
    binding: list[str] = []
    slug = "strict-bool-update"
    _stage_update(
        loader,
        slug,
        target="auto/strict-bool",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    metadata = loader._pending_root() / slug / ".meta.json"
    raw = json.loads(metadata.read_text(encoding="utf-8"))
    raw["has_scripts"] = "false"
    metadata.write_text(json.dumps(raw), encoding="utf-8")

    pending = next(row for row in loader.list_pending_skills() if row["slug"] == slug)
    assert pending["has_scripts"] is False
    assert loader.auto_apply_pending_update(
        slug,
        expected_candidate_binding=binding[0],
    ) == ("auto/strict-bool", 2)


def test_sync_skill_tree_uses_authenticated_writable_handle_on_windows(
    loader, monkeypatch, tmp_path
):
    """Windows durability uses O_RDWR without dropping no-follow/inode checks."""
    root = tmp_path / "windows-sync"
    root.mkdir()
    leaf = root / "SKILL.md"
    leaf.write_text("body", encoding="utf-8")
    real_open = skills_mod.os.open
    opened_flags: list[int] = []

    def recording_open(path, flags, *args, **kwargs):
        if Path(path) == leaf:
            opened_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.delattr(os, "O_ACCMODE", raising=False)
    monkeypatch.setattr(skills_mod.os, "open", recording_open)

    loader._sync_skill_tree(root)

    assert len(opened_flags) == 1
    access_mode_mask = getattr(os, "O_ACCMODE", os.O_WRONLY | os.O_RDWR)
    assert opened_flags[0] & access_mode_mask == os.O_RDWR
    if getattr(os, "O_NOFOLLOW", 0):
        assert opened_flags[0] & os.O_NOFOLLOW


def test_sync_skill_tree_refuses_inode_substitution(loader, monkeypatch, tmp_path):
    """The Windows-compatible writable open still authenticates the staged inode."""
    root = tmp_path / "inode-sync"
    root.mkdir()
    leaf = root / "SKILL.md"
    leaf.write_text("STAGED", encoding="utf-8")
    victim = tmp_path / "victim"
    victim.write_text("DO NOT TOUCH", encoding="utf-8")
    real_open = skills_mod.os.open

    def substituted_open(path, flags, *args, **kwargs):
        if Path(path) == leaf:
            return real_open(victim, flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    def native_identity_matches(fd, path):
        return os.path.samestat(os.fstat(fd), os.stat(path, follow_symlinks=False))

    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        native_identity_matches,
    )
    monkeypatch.setattr(skills_mod.os, "open", substituted_open)

    with pytest.raises(OSError, match="changed during durable open"):
        loader._sync_skill_tree(root)
    assert victim.read_text(encoding="utf-8") == "DO NOT TOUCH"


# ── rebase answers: lock order, fail-closed hosts, retained old-live evidence ──


_LOCK_RANK = {name: rank for rank, name in enumerate(skills_mod.AUTO_SKILL_LOCK_ORDER)}


def _record_lock_nesting(loader, monkeypatch) -> list[tuple[str, tuple[str, ...]]]:
    """Record every auto-skill lock acquisition with the locks already held.

    Each entry is ``(acquired, held_before)``. The claim lock is the one taken
    without a context manager: it is held from ``_claim_pending_update`` returning
    until its descriptor is released.
    """
    held: list[str] = []
    seen: list[tuple[str, tuple[str, ...]]] = []
    claim_fds: set[int] = set()

    def tracked(kind, real):
        @contextlib.contextmanager
        def wrapper(*args, **kwargs):
            name = kind(*args) if callable(kind) else kind
            if name is None:
                with real(*args, **kwargs) as acquired:
                    yield acquired
                return
            seen.append((name, tuple(held)))
            with real(*args, **kwargs) as acquired:
                held.append(name)
                try:
                    yield acquired
                finally:
                    held.remove(name)

        return wrapper

    monkeypatch.setattr(
        loader,
        "_auto_slug_claim_lock",
        tracked("slug-claim", loader._auto_slug_claim_lock),
    )
    monkeypatch.setattr(loader, "_promotion_lock", tracked("target", loader._promotion_lock))
    monkeypatch.setattr(
        loader,
        "_file_lock",
        tracked(
            lambda name, *_a: "namespace" if name == "pending.lock" else None, loader._file_lock
        ),
    )
    real_claim = loader._claim_pending_update

    def claim(slug):
        result = real_claim(slug)
        if result is not None:
            seen.append(("claim", tuple(held)))
            held.append("claim")
            claim_fds.add(result[1])
        return result

    real_release = skills_mod.platform_compat.release_lock

    def release(fd):
        if fd in claim_fds:
            claim_fds.discard(fd)
            held.remove("claim")
        return real_release(fd)

    monkeypatch.setattr(loader, "_claim_pending_update", claim)
    monkeypatch.setattr(skills_mod.platform_compat, "release_lock", release)
    return seen


def test_every_auto_skill_path_takes_its_locks_in_the_one_documented_order(loader, monkeypatch):
    seen = _record_lock_nesting(loader, monkeypatch)

    assert loader.create_auto_skill(
        "order-live", description="d", triggers="t", procedure_md="p", provenance=_prov()
    )
    assert loader.archive_auto_skill("auto/order-live")
    assert loader.restore_auto_skill("order-live") == "auto/order-live"
    assert loader.stage_skill_candidate(
        "order-new", description="d", triggers="t", procedure_md="p", provenance=_prov()
    )
    assert loader.approve_pending_skill_checked("order-new") == "auto/order-new"
    _stage_update(loader, "order-live-update", target="auto/order-live")
    assert loader.approve_pending_update_checked("order-live-update") == "auto/order-live"
    assert loader.stage_skill_candidate(
        "order-gone", description="d", triggers="t", procedure_md="p", provenance=_prov()
    )
    assert loader.dismiss_pending_skill("order-gone") is True

    acquired = {name for name, _held in seen}
    assert acquired == set(_LOCK_RANK), acquired
    nested = [(name, held) for name, held in seen if held]
    assert nested, "no path nested two locks, so the order was never exercised"
    for name, held in seen:
        assert all(_LOCK_RANK[outer] < _LOCK_RANK[name] for outer in held), (name, held)


def test_live_publish_takes_the_target_lock_inside_the_slug_claim(loader, monkeypatch):
    seen = _record_lock_nesting(loader, monkeypatch)
    assert loader.create_auto_skill(
        "nested-order", description="d", triggers="t", procedure_md="p", provenance=_prov()
    )
    assert ("target", ("slug-claim",)) in seen


def test_a_late_write_to_the_old_live_generation_survives_publication_cleanup(loader, monkeypatch):
    """Cleanup removes private trees only; the public old-live inode is evidence.

    A writer holding a descriptor into the replaced live generation can still write
    after the last hash publication takes. That write lands in the public
    ``.live-quarantine`` entry, which no cleanup ever deletes.
    """
    if os.name == "nt":
        pytest.skip("a retained write handle blocks the rename on Windows")
    target = "late-old-live"
    live = _write_live(loader, target, version=1, body="OLD")
    retained = os.open(live / "SKILL.md", os.O_WRONLY)
    real_commit = loader._commit_claim_consumption
    late = b"LATE WRITE AFTER THE FINAL HASH\n"

    def write_then_commit(claim, claim_fd):
        # Publication has hashed and renamed both generations; this write is
        # the one no hash can see, and the commit's cleanup runs right after.
        os.lseek(retained, 0, os.SEEK_SET)
        os.ftruncate(retained, 0)
        os.write(retained, late)
        return real_commit(claim, claim_fd)

    monkeypatch.setattr(loader, "_commit_claim_consumption", write_then_commit)
    _stage_update(loader, f"{target}-update", target=f"auto/{target}")
    try:
        assert loader.approve_pending_update_checked(f"{target}-update") == f"auto/{target}"
    finally:
        os.close(retained)

    (old_live,) = list(loader._live_quarantine_root().iterdir())
    assert (old_live / "SKILL.md").read_bytes() == late
    assert b"LATE WRITE" not in (live / "SKILL.md").read_bytes()


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
def test_sandbox_off_has_a_distinct_refusal_and_never_provisions(
    uninitialized_loader,
    monkeypatch,
):
    from kiro_crew import sandbox

    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "off")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: mode != "off")

    with pytest.raises(OSError, match="sandbox_off"):
        skills_mod.initialize_gateway_auto_skill_private_authority()

    assert skills_mod._STARTUP_AUTHORITY_BINDING is None
    assert skills_mod.auto_skill_promotion_disabled_reason() == "sandbox_off"
    assert not _authority_path(uninitialized_loader._private_root().parents[1]).exists()
    pending = uninitialized_loader._pending_root() / "queued-before-off"
    pending.mkdir(parents=True)
    (pending / "SKILL.md").write_text("## Steps\n\nqueued\n", encoding="utf-8")
    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        uninitialized_loader.approve_pending_skill_checked("queued-before-off")
    assert refused.value.reason == "promotion_disabled"
    assert (pending / "SKILL.md").exists()


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
def test_live_mode_flip_off_revokes_the_next_promotion(uninitialized_loader, monkeypatch):
    masked = True
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        lambda: None if masked else skills_mod._SANDBOX_OFF_REFUSAL,
    )
    skills_mod.initialize_gateway_auto_skill_private_authority()
    assert (
        uninitialized_loader.stage_skill_candidate(
            "mode-flip",
            description="d",
            triggers="t",
            procedure_md="p",
            provenance=_prov(),
        )
        == "auto/mode-flip"
    )

    masked = False
    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        uninitialized_loader.approve_pending_skill_checked("mode-flip")

    assert refused.value.reason == "promotion_disabled"
    assert skills_mod._STARTUP_AUTHORITY_BINDING is None
    assert skills_mod.auto_skill_promotion_disabled_reason() == "sandbox_off"
    assert (uninitialized_loader._pending_root() / "mode-flip" / "SKILL.md").exists()


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
def test_turning_on_a_delegated_sandbox_after_startup_revokes_the_next_promotion(
    uninitialized_loader,
    monkeypatch,
):
    """The delegation predicate (``kiro_internal_sandbox_enabled`` on macOS) is read
    uncached at every authority-backed use, not only at startup certification."""
    from kiro_crew import sandbox

    delegated = False
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: delegated)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "strict")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: mode == "strict")
    skills_mod.initialize_gateway_auto_skill_private_authority()
    assert (
        uninitialized_loader.stage_skill_candidate(
            "delegation-flip",
            description="d",
            triggers="t",
            procedure_md="p",
            provenance=_prov(),
        )
        == "auto/delegation-flip"
    )

    delegated = True
    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        uninitialized_loader.approve_pending_skill_checked("delegation-flip")

    assert refused.value.reason == "promotion_disabled"
    assert skills_mod._STARTUP_AUTHORITY_BINDING is None
    assert "does not build" in (skills_mod.auto_skill_promotion_disabled_reason() or "")


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
def test_gateway_certifies_authority_when_the_protecting_mask_applies(
    uninitialized_loader,
    monkeypatch,
):
    from kiro_crew import sandbox

    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "strict")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: mode == "strict")

    binding = skills_mod.initialize_gateway_auto_skill_private_authority()

    assert binding == skills_mod._STARTUP_AUTHORITY_BINDING
    assert skills_mod.auto_skill_promotion_disabled_reason() is None
    assert _authority_path(uninitialized_loader._private_root().parents[1]).is_dir()


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
class TestDelegatedSandboxFailsClosed:
    """Where Kiro Crew does not build the agent sandbox, auto-skill promotion is off.

    Pending maintainer confirmation of the supported platform contract: native
    Windows and macOS with the Kiro CLI internal sandbox cannot hide the authority
    from an agent's shell, so the gateway never creates or certifies it there.
    Live auto-skill edits and dismissals keep working because the authority root
    is absent, regardless of the sandbox refusal that prevented its creation.
    """

    @pytest.fixture()
    def delegated(self, uninitialized_loader, monkeypatch):
        monkeypatch.setattr(
            skills_mod,
            "_auto_skill_authority_sandbox_refusal",
            lambda: skills_mod._DELEGATED_SANDBOX_REFUSAL,
        )
        monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: True)
        return uninitialized_loader

    def test_the_gateway_never_provisions_authority(self, delegated):
        with pytest.raises(OSError, match="does not build"):
            skills_mod.initialize_gateway_auto_skill_private_authority()
        assert skills_mod._STARTUP_AUTHORITY_BINDING is None
        assert not _authority_path(delegated._private_root().parents[1]).exists()
        assert "does not build" in (skills_mod.auto_skill_promotion_disabled_reason() or "")

    def test_staging_and_approval_refuse_with_the_reason(self, delegated):
        with pytest.raises(OSError):
            skills_mod.initialize_gateway_auto_skill_private_authority()
        refusal = ClaimRefusal()
        assert (
            delegated.stage_skill_candidate(
                "delegated-new",
                description="d",
                triggers="t",
                procedure_md="p",
                provenance=_prov(),
                refusal=refusal,
            )
            is None
        )
        assert refusal.retryable is False
        pending = delegated._pending_root() / "pre-upgrade"
        pending.mkdir(parents=True)
        (pending / "SKILL.md").write_text("## Steps\n\nqueued before\n", encoding="utf-8")
        with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
            delegated.approve_pending_skill_checked("pre-upgrade")
        assert refused.value.reason == "promotion_disabled"
        assert (pending / "SKILL.md").exists()

    def test_live_edits_and_dismissal_keep_working(self, delegated):
        with pytest.raises(OSError):
            skills_mod.initialize_gateway_auto_skill_private_authority()
        _write_live(delegated, "delegated-live", version=1, body="OLD")
        assert delegated.set_pinned("auto/delegated-live", True) is True
        pending = delegated._pending_root() / "pre-upgrade"
        pending.mkdir(parents=True)
        (pending / "SKILL.md").write_text("## Steps\n\nqueued before\n", encoding="utf-8")
        assert delegated.dismiss_pending_skill("pre-upgrade") is True
        assert not pending.exists()

    def test_unreviewed_publication_is_off_under_the_same_gate(self, delegated):
        """``approval_required=false`` publishes through ``create_auto_skill``; the
        gate that turns the reviewed path off must turn it off too, or the less
        safe configuration keeps a feature the safer one lost."""
        with pytest.raises(OSError):
            skills_mod.initialize_gateway_auto_skill_private_authority()

        assert (
            delegated.create_auto_skill(
                "delegated-direct",
                description="d",
                triggers="t",
                procedure_md="## Steps\n\nrun\n",
                provenance=_prov(),
            )
            is None
        )
        assert not (delegated._dir / skills_mod.AUTO_SKILL_NAMESPACE / "delegated-direct").exists()

    def test_pre_upgrade_candidates_survive_ttl_pruning(self, delegated):
        with pytest.raises(OSError):
            skills_mod.initialize_gateway_auto_skill_private_authority()
        pending = delegated._pending_root() / "pre-upgrade-old"
        pending.mkdir(parents=True)
        (pending / "SKILL.md").write_text("## Steps\n\nqueued before\n", encoding="utf-8")
        queued_at = pending.stat().st_mtime

        pruned = delegated.prune_pending(1, now=queued_at + 30 * 86400)

        assert pruned == 0
        assert (pending / "SKILL.md").exists()
        assert [entry["slug"] for entry in delegated.list_pending_skills()] == ["pre-upgrade-old"]


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
def test_startup_refusal_does_not_override_root_absence_after_mode_flips_back(
    uninitialized_loader,
    monkeypatch,
):
    masked = False
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        lambda: None if masked else skills_mod._SANDBOX_OFF_REFUSAL,
    )

    with pytest.raises(OSError, match="sandbox_off"):
        skills_mod.initialize_gateway_auto_skill_private_authority()

    masked = True
    _write_live(uninitialized_loader, "refused-live", version=1, body="OLD")
    assert skills_mod._STARTUP_AUTHORITY_REFUSAL == skills_mod._SANDBOX_OFF_REFUSAL
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    assert uninitialized_loader.set_pinned("auto/refused-live", True) is True
    assert not _authority_path(uninitialized_loader._private_root().parents[1]).exists()


@pytest.mark.usefixtures("no_auto_skill_authority_startup")
def test_a_process_that_never_certified_still_requires_the_lock(uninitialized_loader, monkeypatch):
    """A fresh CLI process must not bypass a root another process can promote through."""
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    skills_mod.initialize_auto_skill_private_authority(
        skills_root=uninitialized_loader._dir,
        data_home=uninitialized_loader._private_root().parents[1],
    )
    skills_mod._reset_auto_skill_private_authority_for_tests()
    _write_live(uninitialized_loader, "cli-live", version=1, body="OLD")
    assert skills_mod._auto_skill_promotion_ruled_out() is False
    assert uninitialized_loader.set_pinned("auto/cli-live", True) is False

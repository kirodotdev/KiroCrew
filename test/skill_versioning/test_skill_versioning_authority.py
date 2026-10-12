"""Private-state authority roots, Windows identity, and linked-path hardening.

Covers fail-closed claim roots, Windows lock preparation and native-identity
rename checks, legacy private-state migration, authority creation / provenance /
retargeting, refusal of linked or swapped private-state ancestors, and the
linked-claim restore, dismissal and quarantine-evidence recovery paths.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

import kiro_crew.skills as skills_mod
from kiro_crew.skills import SkillsLoader

from .skill_versioning_support import (
    _HOST_DIR_FD_SUPPORTED,
    _authority_path,
    _different_identity,
    _initialize_test_authority,
    _linked_pending_candidate,
    _open_lock_for_test,
    _prov,
    _restat,
    _simulate_windows_native_handles,
    _stage_update,
    _tagged_identity_for_path,
    _write_live,
    requires_renamable_pinned_directory,
)

# This module's auto-skill boundaries initialize explicitly through the
# ``loader`` fixture; every ``uninitialized_loader`` path must see production
# fail closed.
pytestmark = pytest.mark.usefixtures("no_auto_skill_authority_startup")


def test_claim_fails_closed_outside_agent_denied_root(tmp_path, monkeypatch):
    unprotected_home = tmp_path / "unprotected-home"
    unprotected_home.mkdir()
    monkeypatch.setattr(skills_mod, "config_dir", lambda: unprotected_home)
    unprotected = SkillsLoader(
        skills_path=unprotected_home / "skills",
        install_builtins=False,
    )
    pending = unprotected._pending_root() / "unprotected-candidate"
    pending.mkdir(parents=True)
    skill_bytes = b"---\nname: auto/unprotected-candidate\n---\nORIGINAL\n"
    metadata_bytes = b'{"kind":"new","notify_suppressed":false}\r\n'
    (pending / "SKILL.md").write_bytes(skill_bytes)
    (pending / ".meta.json").write_bytes(metadata_bytes)
    assert not os.path.lexists(unprotected._private_root())

    assert unprotected._claim_pending_update("unprotected-candidate") is None

    assert not os.path.lexists(unprotected._private_root())
    assert (pending / "SKILL.md").read_bytes() == skill_bytes
    assert (pending / ".meta.json").read_bytes() == metadata_bytes
    assert {entry.name for entry in pending.parent.parent.iterdir()} == {
        skills_mod.AUTO_PENDING_DIRNAME
    }


def test_skill_lock_files_are_prepared_for_windows(uninitialized_loader, monkeypatch):
    loader = uninitialized_loader
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "restrict_dir_to_owner",
        lambda _path: None,
    )
    monkeypatch.setattr(
        skills_mod.platform_compat, "try_acquire_lock", lambda _fd, exclusive=False: True
    )
    monkeypatch.setattr(skills_mod.platform_compat, "release_lock", lambda _fd: None)
    _initialize_test_authority(loader)
    loader.stage_skill_candidate(
        "windows-lock-byte",
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )
    pending_lock = loader._locks_root() / "pending.lock"
    assert pending_lock.read_bytes() == b"\0"

    claimed = loader._claim_pending_update("windows-lock-byte")
    assert claimed is not None
    claim, fd, _consumed_at, claim_snapshot = claimed
    claim_lock = loader._claim_lock_path(claim.name)
    assert loader._authenticated_claim_snapshot_state(fd, claim_lock, claim.name) == claim_snapshot
    loader._restore_claimed_update(claim, fd, "windows-lock-byte", claim_snapshot)
    os.close(fd)
    assert (loader._pending_root() / "windows-lock-byte").is_dir()
    assert not claim.exists()
    assert not (loader._evidence_root() / claim.name).exists()
    assert not (loader._quarantine_root() / claim.name).exists()


def test_skill_lock_refuses_hardlink_without_touching_target(loader):
    assert loader._private_state_roots_safe(create=True) is True
    victim = loader._locks_root() / "hardlink-victim"
    victim.write_bytes(b"DO NOT TOUCH")
    lock_path = loader._locks_root() / "hardlinked.lock"
    os.link(victim, lock_path)

    with pytest.raises(OSError):
        _open_lock_for_test(loader, lock_path)

    assert victim.read_bytes() == b"DO NOT TOUCH"
    assert lock_path.read_bytes() == b"DO NOT TOUCH"


@pytest.mark.skipif(os.name == "nt", reason="simulated Windows race runs on POSIX")
def test_simulated_windows_staged_directory_rollback_preserves_replacement(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    _simulate_windows_native_handles(monkeypatch)
    _initialize_test_authority(loader)
    assert loader._private_state_roots_safe(create=True)
    replaced = False
    displaced: Path | None = None

    def replace_then_unlink(path, expected_identity, *, directory):
        nonlocal replaced, displaced
        path = Path(path)
        if path.name == "windows-rollback-stage" and not replaced:
            displaced = path.with_name(f"{path.name}-original")
            path.rename(displaced)
            path.mkdir()
            (path / "sentinel").write_text("NEWER DIRECTORY", encoding="utf-8")
            replaced = True
        fd = skills_mod.platform_compat.open_path_no_reparse(path)
        try:
            current_identity = skills_mod.platform_compat.opened_file_identity(fd)
        finally:
            os.close(fd)
        if current_identity != expected_identity:
            return False
        pytest.fail("replacement directory identity was accepted for unlink")

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "unlink_path_if_identity",
        replace_then_unlink,
    )

    with loader._pin_private_state(create=False) as state:
        with pytest.raises(OSError, match="force staged rollback"):
            with loader._create_pinned_child_exclusive(
                state.pending,
                "windows-rollback-stage",
            ):
                raise OSError("force staged rollback")

    replacement = loader._pending_root() / "windows-rollback-stage"
    assert replaced is True
    assert displaced is not None and displaced.is_dir()
    assert (replacement / "sentinel").read_text(encoding="utf-8") == "NEWER DIRECTORY"


@pytest.mark.skipif(os.name != "nt", reason="native Windows FileIdInfo race")
def test_native_windows_staged_directory_rollback_preserves_replacement(loader, monkeypatch):
    real_unlink = skills_mod.platform_compat.unlink_path_if_identity
    replaced = False
    displaced: Path | None = None

    def replace_then_native_unlink(path, expected_identity, *, directory):
        nonlocal replaced, displaced
        path = Path(path)
        if path.name == "native-windows-rollback-stage" and not replaced:
            displaced = path.with_name(f"{path.name}-original")
            path.rename(displaced)
            path.mkdir()
            (path / "sentinel").write_text("NEWER NATIVE DIRECTORY", encoding="utf-8")
            replaced = True
        return real_unlink(path, expected_identity, directory=directory)

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "unlink_path_if_identity",
        replace_then_native_unlink,
    )

    # ``_pin_private_state(create=False)`` pins existing roots only; the simulated
    # sibling above materializes them first, and so must this one.
    assert loader._private_state_roots_safe(create=True)
    with loader._pin_private_state(create=False) as state:
        with pytest.raises(OSError, match="force native rollback"):
            with loader._create_pinned_child_exclusive(
                state.pending,
                "native-windows-rollback-stage",
            ):
                raise OSError("force native rollback")

    replacement = loader._pending_root() / "native-windows-rollback-stage"
    assert replaced is True
    assert displaced is not None and displaced.is_dir()
    assert (replacement / "sentinel").read_text(encoding="utf-8") == ("NEWER NATIVE DIRECTORY")


def test_windows_private_cleanup_retries_readonly_regular_file(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    _simulate_windows_native_handles(monkeypatch)
    _initialize_test_authority(loader)
    assert loader._private_state_roots_safe(create=True) is True
    target = loader._claims_root() / "readonly-private-file"
    target.write_bytes(b"private")
    target.chmod(stat.S_IREAD)
    calls = 0

    def identity_bound_unlink(path, expected_identity, *, directory):
        nonlocal calls
        calls += 1
        path = Path(path)
        fd = skills_mod.platform_compat.open_path_no_reparse(path)
        try:
            current_identity = skills_mod.platform_compat.opened_file_identity(fd)
        finally:
            os.close(fd)
        if current_identity != expected_identity:
            return False
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        path.unlink()
        return True

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "unlink_path_if_identity",
        identity_bound_unlink,
    )

    with loader._pin_private_state(create=False) as private_state:
        expected = loader._stat_pinned_child(private_state.claims, target.name)
        expected_identity = loader._pinned_child_identity(private_state.claims, target.name)
        assert expected_identity is not None
        assert loader._unlink_skill_child(
            private_state.claims,
            target.name,
            expected=expected,
            expected_identity=expected_identity,
        )

    assert calls == 1
    assert not target.exists()


def test_windows_unstable_crt_projection_does_not_break_promotion_or_restart_restore(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "restrict_dir_to_owner",
        lambda _path: None,
    )
    _initialize_test_authority(loader)
    real_fstat = skills_mod.os.fstat
    projection = 0

    def unstable_fstat(fd):
        nonlocal projection
        result = real_fstat(fd)
        projection += 1
        return _restat(result, st_ino=int(result.st_ino) + projection)

    monkeypatch.setattr(skills_mod.os, "fstat", unstable_fstat)
    target = "windows-unstable-end-to-end"
    _write_live(loader, target, version=1, body="BEFORE")
    _stage_update(loader, f"{target}-update", target=f"auto/{target}")
    assert loader.approve_pending_update_checked(f"{target}-update") == f"auto/{target}"

    pending_slug = "windows-unstable-restart"
    loader.stage_skill_candidate(
        pending_slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nrestore",
        provenance=_prov(),
    )
    claimed = loader._claim_pending_update(pending_slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [pending_slug]
    assert not claim.exists()


def test_private_state_migration_refuses_cross_device(uninitialized_loader, monkeypatch):
    loader = uninitialized_loader
    legacy = loader._legacy_private_root()
    legacy.mkdir(parents=True)
    sentinel = legacy / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    real_pin_child = loader._pin_skill_child_parent

    @contextlib.contextmanager
    def cross_device_legacy(parent, name, *, create, created_out=None):
        with real_pin_child(
            parent,
            name,
            create=create,
            created_out=created_out,
        ) as child:
            if parent.path == legacy.parent and name == skills_mod.AUTO_PRIVATE_DIRNAME:
                child = skills_mod._PinnedSkillParent(
                    child.path,
                    child.fd,
                    child.identity,
                    skills_mod._TaggedFileIdentity(
                        child.native_identity.kind,
                        child.native_identity.volume + 1,
                        child.native_identity.object_id,
                    ),
                )
            yield child

    monkeypatch.setattr(loader, "_pin_skill_child_parent", cross_device_legacy)

    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not loader._private_root().exists()


def test_populated_legacy_private_state_refuses_untouched(uninitialized_loader):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    legacy = loader._legacy_private_root()
    claim = legacy / "claims" / "candidate--token"
    claim.mkdir(parents=True)
    sentinel = claim / "SKILL.md"
    sentinel.write_text("retained authority", encoding="utf-8")
    legacy_identity = legacy.stat().st_ino

    with pytest.raises(
        OSError,
        match="populated legacy auto-skill private state requires stopped-installation migration",
    ):
        skills_mod.initialize_auto_skill_private_authority(
            data_home=home,
            configured_home=home,
        )

    assert legacy.stat().st_ino == legacy_identity
    assert sentinel.read_text(encoding="utf-8") == "retained authority"
    assert not loader._private_root().exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX retains parent traversal on open dirs")
def test_empty_legacy_state_refuses_while_retained_descriptor_reaches_parent(
    uninitialized_loader,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    legacy = loader._legacy_private_root()
    legacy.mkdir(parents=True)
    retained_fd = os.open(legacy, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    legacy_identity = os.fstat(retained_fd).st_ino
    auto_identity = legacy.parent.stat().st_ino
    try:
        with pytest.raises(
            OSError,
            match="empty legacy auto-skill private state requires stopped-installation migration",
        ):
            skills_mod.initialize_auto_skill_private_authority(
                data_home=home,
                configured_home=home,
            )
        assert legacy.stat().st_ino == legacy_identity
        assert not loader._private_root().exists()

        parent_fd = os.open(
            "..",
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            dir_fd=retained_fd,
        )
        try:
            assert os.fstat(parent_fd).st_ino == auto_identity
        finally:
            os.close(parent_fd)
    finally:
        os.close(retained_fd)


def test_transitional_direct_private_state_refuses_untouched(uninitialized_loader):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    direct = home / skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME
    direct.mkdir()
    sentinel = direct / "sentinel"
    sentinel.write_text("retained direct authority", encoding="utf-8")
    direct_identity = direct.stat().st_ino

    with pytest.raises(
        OSError,
        match="populated transitional direct auto-skill private state requires "
        "stopped-installation recovery",
    ):
        skills_mod.initialize_auto_skill_private_authority(
            data_home=home,
            configured_home=home,
        )

    assert direct.stat().st_ino == direct_identity
    assert sentinel.read_text(encoding="utf-8") == "retained direct authority"
    assert not loader._private_root().exists()


def test_no_legacy_state_builds_the_ordinary_authority_hierarchy(loader):
    assert not os.path.lexists(loader._legacy_private_root())

    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True

    assert loader._private_root().is_dir()
    assert loader._authority_provenance_path().is_file()
    assert loader._claims_root().is_dir()
    assert loader._evidence_root().is_dir()
    assert (loader._locks_root() / skills_mod.AUTO_CLAIMS_DIRNAME).is_dir()


@pytest.mark.parametrize(
    "record_case",
    [
        "missing",
        "inside-only",
        "malformed",
        "legacy-v1",
        "bad-mac",
        "home-mismatch",
        "root-mismatch",
    ],
)
def test_existing_authority_without_matching_external_provenance_refuses_untouched(
    uninitialized_loader,
    record_case,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    root = loader._private_root()
    root.mkdir(parents=True)
    sentinel = root / "sentinel"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
    root_identity = root.stat().st_ino
    provenance = loader._authority_provenance_path()

    if record_case == "inside-only":
        (root / skills_mod._AUTHORITY_PROVENANCE_NAME).write_text("forged", encoding="utf-8")
    elif record_case != "missing":
        provenance.parent.mkdir(parents=True, exist_ok=True)
        if record_case == "malformed":
            provenance.write_text("{", encoding="utf-8")
        else:
            with (
                loader._pin_skill_parent(home) as home_parent,
                loader._pin_skill_parent(root) as root_parent,
            ):
                home_identity = home_parent.native_identity
                authority_identity = root_parent.native_identity
            if record_case == "home-mismatch":
                home_identity = _different_identity(home_identity)
            if record_case == "root-mismatch":
                authority_identity = _different_identity(authority_identity)
            if record_case == "legacy-v1":
                body = {
                    "version": 1,
                    "selected_home": loader._identity_payload(home_identity),
                    "authority_root": loader._identity_payload(authority_identity),
                }
            else:
                body = loader._authority_record_body(
                    home,
                    home,
                    home_identity,
                    authority_identity,
                )
            body["mac"] = (
                "0" * 64 if record_case == "bad-mac" else loader._authority_record_mac(body)
            )
            provenance.write_text(json.dumps(body), encoding="utf-8")

    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
    assert root.stat().st_ino == root_identity
    assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"


@pytest.mark.skipif(os.name == "nt", reason="simulated Windows race runs on POSIX")
@pytest.mark.parametrize("record_case", ["home-mismatch", "root-mismatch"])
def test_simulated_windows_identity_mismatch_refuses_untouched(
    uninitialized_loader,
    monkeypatch,
    record_case,
):
    """The mismatch cases again, with native 128-bit file ids instead of inodes.

    Native Windows hands the provenance record a 16-byte file id. A fixture that
    can only perturb an integer inode never reaches that record shape on POSIX.
    """
    _simulate_windows_native_handles(monkeypatch)
    test_existing_authority_without_matching_external_provenance_refuses_untouched(
        uninitialized_loader,
        record_case,
    )


def test_authority_creation_crash_leaves_uncertified_root_inert(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    root = loader._private_root()
    provenance = loader._authority_provenance_path()
    real_write = loader._write_pinned_new_file

    def fail_provenance(parent, name, payload, **kwargs):
        if name == skills_mod._AUTHORITY_PROVENANCE_NAME:
            raise OSError("injected provenance write crash")
        return real_write(parent, name, payload, **kwargs)

    with monkeypatch.context() as crash:
        crash.setattr(loader, "_write_pinned_new_file", fail_provenance)
        with pytest.raises(OSError):
            loader._ensure_private_authority(
                root.parents[1],
                configured_home=root.parents[1],
                create=True,
            )

    assert root.is_dir()
    root_identity = root.stat().st_ino
    assert not provenance.exists()
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
    assert root.stat().st_ino == root_identity
    assert not provenance.exists()


def test_ordinary_operation_before_initializer_creates_no_authority(uninitialized_loader):
    loader = uninitialized_loader
    root = loader._private_root()
    provenance = loader._authority_provenance_path()

    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
    assert (
        loader.stage_skill_candidate(
            "before-initializer",
            description="candidate",
            triggers="candidate",
            procedure_md="## Steps\n\nwait",
            provenance=_prov(),
        )
        is None
    )
    assert not root.exists()
    assert not provenance.exists()


def test_valid_authority_provenance_reuses_exact_root(loader):
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    root_identity = loader._private_root().stat().st_ino
    record = loader._authority_provenance_path().read_bytes()

    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    assert loader._private_root().stat().st_ino == root_identity
    assert loader._authority_provenance_path().read_bytes() == record


def test_root_swap_after_provenance_before_use_refuses(loader, monkeypatch):
    root = loader._private_root()
    displaced = root.with_name("auto-skill-private-after-verify")
    real_require = skills_mod.require_auto_skill_private_authority
    swapped = False

    def require_then_swap():
        nonlocal swapped
        binding = real_require()
        if not swapped:
            root.rename(displaced)
            root.mkdir()
            (root / "replacement").write_text("KEEP", encoding="utf-8")
            swapped = True
        return binding

    monkeypatch.setattr(skills_mod, "require_auto_skill_private_authority", require_then_swap)
    try:
        assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
        assert swapped is True
        assert (root / "replacement").read_text(encoding="utf-8") == "KEEP"
        assert not (root / skills_mod.AUTO_CLAIMS_DIRNAME).exists()
    finally:
        if swapped:
            if root.exists():
                shutil.rmtree(root)
            if displaced.exists():
                displaced.rename(root)


@requires_renamable_pinned_directory
def test_fresh_root_swap_after_provenance_sync_refuses(uninitialized_loader, monkeypatch):
    loader = uninitialized_loader
    root = loader._private_root()
    displaced = root.with_name("auto-skill-private-after-sync")
    real_sync = loader._sync_pinned_parent
    swapped = False

    def sync_then_swap(parent):
        nonlocal swapped
        result = real_sync(parent)
        if (
            not swapped
            and parent.path.name == skills_mod._AUTHORITY_PROVENANCE_PARENT
            and loader._authority_provenance_path().exists()
        ):
            root.rename(displaced)
            root.mkdir()
            (root / "replacement").write_text("KEEP", encoding="utf-8")
            swapped = True
        return result

    monkeypatch.setattr(loader, "_sync_pinned_parent", sync_then_swap)
    with pytest.raises(OSError, match="changed after provenance durability"):
        loader._ensure_private_authority(
            root.parents[1],
            configured_home=root.parents[1],
            create=True,
        )
    try:
        assert swapped is True
        assert (root / "replacement").read_text(encoding="utf-8") == "KEEP"
        assert displaced.is_dir()
    finally:
        shutil.rmtree(root)
        displaced.rename(root)


def test_selected_home_symlink_retarget_refuses_new_target(tmp_path, monkeypatch):
    configured = tmp_path / "configured-home"
    first = tmp_path / "home-a"
    second = tmp_path / "home-b"
    first.mkdir()
    second.mkdir()
    configured.symlink_to(first, target_is_directory=True)
    monkeypatch.setattr(skills_mod, "config_dir", lambda: configured)
    denied = {
        _authority_path(first),
        first / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod._AUTHORITY_PROVENANCE_NAME,
        _authority_path(second),
        second / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod._AUTHORITY_PROVENANCE_NAME,
    }
    monkeypatch.setattr(skills_mod, "is_sensitive_path", lambda path: Path(path) in denied)

    binding = skills_mod.initialize_auto_skill_private_authority(
        data_home=configured,
        configured_home=configured,
    )
    assert binding.canonical_home == first

    configured.unlink()
    configured.symlink_to(second, target_is_directory=True)
    with pytest.raises(OSError, match="path or identity changed"):
        skills_mod.initialize_auto_skill_private_authority(
            data_home=configured,
            configured_home=configured,
        )
    assert not _authority_path(second).exists()
    assert not (
        second / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod._AUTHORITY_PROVENANCE_NAME
    ).exists()


def test_selected_home_same_inode_relocation_refuses_retarget(tmp_path, monkeypatch):
    configured = tmp_path / "configured-home"
    first = tmp_path / "home-a"
    second = tmp_path / "home-b"
    first.mkdir()
    configured.symlink_to(first, target_is_directory=True)
    monkeypatch.setattr(skills_mod, "config_dir", lambda: configured)
    denied = {
        _authority_path(first),
        first / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod._AUTHORITY_PROVENANCE_NAME,
        _authority_path(second),
        second / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod._AUTHORITY_PROVENANCE_NAME,
    }
    monkeypatch.setattr(skills_mod, "is_sensitive_path", lambda path: Path(path) in denied)

    binding = skills_mod.initialize_auto_skill_private_authority(
        data_home=configured,
        configured_home=configured,
    )
    home_identity = first.stat().st_ino
    root_identity = _authority_path(first).stat().st_ino

    first.rename(second)
    first.symlink_to(second, target_is_directory=True)

    assert second.stat().st_ino == home_identity
    assert _authority_path(second).stat().st_ino == root_identity
    with pytest.raises(OSError, match="retargeted after startup certification"):
        skills_mod.verify_auto_skill_private_authority(binding)
    assert _authority_path(second).is_dir()


def test_certified_authority_refuses_root_replacement(loader):
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    root = loader._private_root()
    displaced = root.with_name("auto-skill-private-certified")
    root.rename(displaced)
    root.mkdir()
    sentinel = root / "replacement"
    sentinel.write_text("KEEP", encoding="utf-8")
    try:
        assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
        assert sentinel.read_text(encoding="utf-8") == "KEEP"
        assert displaced.is_dir()
    finally:
        root.rmdir() if not any(root.iterdir()) else shutil.rmtree(root)
        displaced.rename(root)


def test_certified_authority_refuses_selected_home_replacement(loader):
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    home = loader._private_root().parents[1]
    displaced = home.with_name(f"{home.name}-certified")
    home.rename(displaced)
    home.mkdir()
    try:
        assert loader._private_state_roots_safe(create=True, require_sensitive=True) is False
        assert list(home.iterdir()) == []
        assert _authority_path(displaced).is_dir()
    finally:
        home.rmdir()
        displaced.rename(home)


@pytest.mark.parametrize(
    "identity",
    [
        skills_mod._TaggedFileIdentity("posix-dev-ino", 7, 11),
        skills_mod._TaggedFileIdentity("windows-file-id-128", 13, bytes(range(16))),
    ],
)
def test_tagged_identity_payload_round_trips(identity):
    payload = SkillsLoader._identity_payload(identity)
    assert SkillsLoader._identity_from_payload(payload) == identity


@pytest.mark.parametrize(
    "payload",
    [None, [1, 2], {"kind": "windows-file-id-128", "volume": 1, "file_id": "AA=="}],
)
def test_legacy_or_malformed_identity_payload_fails_closed(payload):
    assert SkillsLoader._identity_from_payload(payload) is None
    fields = {
        "claim_generation": "a" * 64,
        "claim_metadata": None,
        "quarantine_identity": payload,
    }
    assert SkillsLoader._claim_snapshot_from_fields(fields) is None


@pytest.mark.parametrize(
    "identity",
    [
        skills_mod._TaggedFileIdentity("posix-dev-ino", 17, 19),
        skills_mod._TaggedFileIdentity("windows-file-id-128", 23, b"w" * 16),
    ],
)
def test_claim_snapshot_json_round_trips_tagged_identity(identity):
    snapshot = skills_mod._ClaimSnapshot("b" * 64, b"{}", quarantine_identity=identity)
    fields = SkillsLoader._claim_snapshot_fields(snapshot)
    decoded = json.loads(json.dumps(fields))
    assert SkillsLoader._claim_snapshot_from_fields(decoded) == snapshot


def test_simulated_windows_rename_uses_native_identity_not_crt_samestat(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "restrict_dir_to_owner",
        lambda _path: None,
    )
    _initialize_test_authority(loader)
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        source = state.claims.path / "native-rename-source"
        source.mkdir()
        expected = _tagged_identity_for_path(loader, source)
        monkeypatch.setattr(
            skills_mod.os.path,
            "samestat",
            lambda *_args: pytest.fail("CRT samestat became Windows rename authority"),
        )
        loader._rename_skill_child_no_replace(
            state.claims,
            source.name,
            state.evidence,
            "native-rename-destination",
            expected_identity=expected,
        )
        assert (
            loader._pinned_child_identity(
                state.evidence,
                "native-rename-destination",
            )
            == expected
        )


def test_simulated_windows_rename_refuses_changed_native_destination(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    monkeypatch.setattr(skills_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "opened_path_identity_matches",
        lambda _fd, _path: True,
    )
    monkeypatch.setattr(
        skills_mod.platform_compat,
        "restrict_dir_to_owner",
        lambda _path: None,
    )
    _initialize_test_authority(loader)
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        source = state.claims.path / "native-collision-source"
        source.mkdir()
        expected = _tagged_identity_for_path(loader, source)
        real_identity = loader._pinned_child_identity

        def replaced_identity(parent, name):
            identity = real_identity(parent, name)
            if parent.path == state.evidence.path and name == "native-collision-destination":
                return skills_mod._TaggedFileIdentity(
                    "windows-file-id-128",
                    expected.volume,
                    b"x" * 16,
                )
            return identity

        monkeypatch.setattr(loader, "_pinned_child_identity", replaced_identity)
        with pytest.raises(OSError, match="changed during rename"):
            loader._rename_skill_child_no_replace(
                state.claims,
                source.name,
                state.evidence,
                "native-collision-destination",
                expected_identity=expected,
            )
        assert not (state.evidence.path / "native-collision-destination").exists()
        assert (state.claims.path / source.name).is_dir()


def test_private_roots_refuse_linked_auto_namespace_ancestor(
    loader,
    tmp_path,
    caplog,
):
    """A symlinked ``auto`` namespace must fail closed before any mkdir.

    Certified first: with no certificate the refusal is the missing authority,
    which must not be reported with the link remedy.
    """
    auto_root = loader._dir / skills_mod.AUTO_SKILL_NAMESPACE
    loader._dir.mkdir(parents=True, exist_ok=True)
    if auto_root.exists():
        shutil.rmtree(auto_root)
    elsewhere = tmp_path / "elsewhere-auto"
    elsewhere.mkdir()
    auto_root.symlink_to(elsewhere, target_is_directory=True)
    try:
        assert loader._private_state_roots_safe(create=True) is False
        # Nothing may be created after public ancestry fails authentication.
        assert not loader._claims_root().exists()
        assert "link the Kiro Crew data-home root instead" in caplog.text
    finally:
        auto_root.unlink()


def test_a_missing_certificate_is_not_reported_as_a_link_problem(uninitialized_loader, caplog):
    assert uninitialized_loader._private_state_roots_safe(create=True) is False
    assert "Remove links or junctions" not in caplog.text
    assert "no gateway-startup certificate" in caplog.text


def test_private_roots_refuse_linked_pending_ancestor(uninitialized_loader, tmp_path):
    loader = uninitialized_loader
    """A linked ``.pending`` sibling must fail closed before private mkdir."""
    pending_root = loader._pending_root()
    pending_root.parent.mkdir(parents=True, exist_ok=True)
    assert not os.path.lexists(loader._private_root())
    elsewhere = tmp_path / "elsewhere-pending"
    elsewhere.mkdir()
    sentinel = elsewhere / "sentinel"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
    skills_mod.platform_compat.symlink_or_junction(elsewhere, pending_root)
    try:
        assert loader._private_state_roots_safe(create=True) is False
        assert loader._claim_pending_update("any-slug") is None
        assert not os.path.lexists(loader._private_root())
        assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"
        assert list(elsewhere.iterdir()) == [sentinel]
    finally:
        skills_mod.platform_compat.unlink_link_or_junction(pending_root)


def test_private_roots_allow_linked_operator_prefix(tmp_path, monkeypatch):
    """Links ABOVE the skills dir (operator-controlled) must stay allowed."""
    real_home = tmp_path / "real-home"
    (real_home / "skills").mkdir(parents=True)
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    denied = {
        _authority_path(real_home),
        real_home / skills_mod._AUTHORITY_PROVENANCE_PARENT / skills_mod._AUTHORITY_PROVENANCE_NAME,
    }
    monkeypatch.setattr(skills_mod, "is_sensitive_path", lambda path: Path(path) in denied)
    monkeypatch.setattr(skills_mod, "config_dir", lambda: linked_home)
    loader = skills_mod.SkillsLoader(linked_home / "skills", install_builtins=False)
    binding = skills_mod.initialize_auto_skill_private_authority(
        skills_root=loader._dir,
        data_home=linked_home,
        configured_home=linked_home,
    )
    assert binding.canonical_home == real_home
    skills_mod.verify_auto_skill_private_authority(binding)


def test_private_root_creation_refuses_swapped_data_home(
    uninitialized_loader,
    monkeypatch,
    private_state_mode,
):
    loader = uninitialized_loader
    loader._dir.mkdir(parents=True, exist_ok=True)
    auto_root = loader._dir / skills_mod.AUTO_SKILL_NAMESPACE
    auto_root.mkdir()
    (auto_root / skills_mod.AUTO_PENDING_DIRNAME).mkdir()
    data_home = loader._private_root().parents[1]
    displaced = data_home.with_name(f"{data_home.name}-before-private-swap-{private_state_mode}")
    real_pin_child = loader._pin_skill_child_parent
    swapped = False

    @contextlib.contextmanager
    def swap_before_private_create(parent, name, *, create, created_out=None):
        nonlocal swapped
        if name == skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME and not swapped:
            data_home.rename(displaced)
            swapped = True
            data_home.mkdir(exist_ok=True)
        with real_pin_child(
            parent,
            name,
            create=create,
            created_out=created_out,
        ) as child:
            yield child

    monkeypatch.setattr(loader, "_pin_skill_child_parent", swap_before_private_create)

    with pytest.raises(OSError):
        loader._ensure_private_authority(
            data_home,
            configured_home=data_home,
            create=True,
        )
    if swapped:
        assert (displaced / "skills/auto/.pending").is_dir()
        assert not _authority_path(displaced).exists()
        assert not loader._private_root().exists()
    else:
        assert not displaced.exists()
        assert (data_home / "skills/auto/.pending").is_dir()
        assert not loader._private_root().exists()


def test_skill_lock_creation_refuses_swapped_locks_parent(
    loader,
    monkeypatch,
    private_state_mode,
):
    assert loader._private_state_roots_safe(create=True) is True
    locks_root = loader._locks_root()
    displaced = locks_root.with_name("locks-before-swap")
    real_open_lock = loader._open_skill_lock
    swapped = False

    def swap_before_lock_create(parent, name):
        nonlocal swapped
        if not swapped:
            locks_root.rename(displaced)
            locks_root.mkdir()
            swapped = True
        return real_open_lock(parent, name)

    monkeypatch.setattr(loader, "_open_skill_lock", swap_before_lock_create)

    with loader._file_lock("ancestor-swap.lock") as acquired:
        assert acquired is False

    # POSIX permits the injected rename and then fails identity revalidation.
    # Windows no-delete-sharing pins may refuse the rename before the hook
    # reaches its assignment; both outcomes prove the replacement is untouched.
    assert swapped is (os.name != "nt")
    if swapped:
        assert list(locks_root.iterdir()) == []
        assert (displaced / skills_mod.AUTO_CLAIMS_DIRNAME).is_dir()
    else:
        assert not displaced.exists()
        assert {entry.name for entry in locks_root.iterdir()} == {skills_mod.AUTO_CLAIMS_DIRNAME}
    assert not (locks_root / "ancestor-swap.lock").exists()


def test_claim_creation_refuses_swapped_claims_parent(
    loader,
    monkeypatch,
    private_state_mode,
):
    slug = "claim-ancestor-swap"
    if private_state_mode == "fallback":
        # POSIX stages only descriptor-relative, so the candidate is staged with the
        # host's real primitives before claims fall back. Windows has no dir_fd and
        # stages by name, so forcing True there reaches POSIX-only opens.
        with pytest.MonkeyPatch.context() as staging_patch:
            staging_patch.setattr(skills_mod, "_DIR_FD_SUPPORTED", _HOST_DIR_FD_SUPPORTED)
            _stage_update(loader, slug, target="auto/claim-target")
    else:
        _stage_update(loader, slug, target="auto/claim-target")
    claims_root = loader._claims_root()
    displaced = claims_root.with_name("claims-before-swap")
    real_rename = loader._rename_skill_child_no_replace
    swapped = False

    def swap_before_claim_create(source, source_name, destination, destination_name, **kwargs):
        nonlocal swapped
        if destination_name.startswith(f"{slug}--") and not swapped:
            claims_root.rename(displaced)
            claims_root.mkdir()
            swapped = True
        return real_rename(source, source_name, destination, destination_name, **kwargs)

    monkeypatch.setattr(loader, "_rename_skill_child_no_replace", swap_before_claim_create)

    assert loader._claim_pending_update(slug) is None
    # POSIX permits the injected rename and then fails identity revalidation.
    # Windows no-delete-sharing pins may refuse the rename before the hook
    # reaches its assignment; both outcomes prove the replacement is untouched.
    assert swapped is (os.name != "nt")
    assert list(claims_root.iterdir()) == []
    assert (loader._pending_root() / slug / "SKILL.md").exists()
    assert not list((loader._locks_root() / "claims").glob(f"{slug}--*.lock"))


def test_skill_lock_detects_opened_inode_swap_without_nofollow(loader, monkeypatch):
    assert loader._private_state_roots_safe(create=True) is True
    lock_path = loader._locks_root() / "swapped.lock"
    lock_path.write_bytes(b"")
    victim = loader._locks_root() / "swap-victim"
    victim.write_bytes(b"DO NOT TOUCH")
    real_open = skills_mod.os.open

    def open_swapped_inode(path, flags, *args, **kwargs):
        if Path(path) == lock_path:
            return real_open(victim, flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.delattr(skills_mod.os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(skills_mod, "_DIR_FD_SUPPORTED", False)
    monkeypatch.setattr(skills_mod.os, "open", open_swapped_inode)

    with pytest.raises(OSError):
        _open_lock_for_test(loader, lock_path)

    assert lock_path.read_bytes() == b""
    assert victim.read_bytes() == b"DO NOT TOUCH"


def test_linked_claim_restores_without_touching_target_metadata(loader):
    link, victim, metadata = _linked_pending_candidate(loader, "linked-approval")

    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_skill_checked("linked-approval")
    assert refused.value.reason == "invalid_layout"

    assert skills_mod.platform_compat.is_link_or_junction(link)
    assert (victim / ".meta.json").read_text(encoding="utf-8") == metadata
    assert not (victim / ".promoted").exists()


def test_linked_claim_dismissal_unlinks_only_claim(loader):
    link, victim, metadata = _linked_pending_candidate(loader, "linked-dismissal")

    assert loader.dismiss_pending_skill("linked-dismissal") is True

    assert not skills_mod.platform_compat.is_link_or_junction(link)
    assert (victim / "SKILL.md").read_text(encoding="utf-8") == "VICTIM"
    assert (victim / ".meta.json").read_text(encoding="utf-8") == metadata
    assert not (victim / ".promoted").exists()


def test_linked_claim_dismissal_unlink_failure_restores_exact_link(loader, monkeypatch):
    slug = "linked-dismissal-failure"
    link, victim, metadata = _linked_pending_candidate(loader, slug)
    real_unlink = loader._unlink_skill_child

    def fail_quarantine_unlink(parent, name, **kwargs):
        if parent.path == loader._quarantine_root() and name.startswith(f"{slug}--"):
            return False
        return real_unlink(parent, name, **kwargs)

    monkeypatch.setattr(loader, "_unlink_skill_child", fail_quarantine_unlink)

    assert loader.dismiss_pending_skill(slug) is False

    assert skills_mod.platform_compat.is_link_or_junction(link)
    assert (victim / "SKILL.md").read_text(encoding="utf-8") == "VICTIM"
    assert (victim / ".meta.json").read_text(encoding="utf-8") == metadata
    assert not (victim / ".promoted").exists()
    assert not list(loader._quarantine_root().iterdir())


@pytest.mark.skipif(os.name == "nt", reason="simulated Windows race runs on POSIX")
def test_simulated_windows_linked_dismissal_preserves_replacement(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    _simulate_windows_native_handles(monkeypatch)
    _initialize_test_authority(loader)
    assert loader._private_state_roots_safe(create=True)
    slug = "windows-linked-dismissal-replacement"
    _link, victim, _metadata = _linked_pending_candidate(loader, slug)
    replaced = False
    real_unlink = skills_mod.platform_compat.unlink_path_if_identity

    def replace_then_unlink(path, expected_identity, *, directory):
        nonlocal replaced
        path = Path(path)
        if path.parent != loader._quarantine_root():
            return real_unlink(path, expected_identity, directory=directory)
        if not replaced:
            original = path.with_name(f"{path.name}-original")
            path.rename(original)
            path.write_text("NEWER QUARANTINE STATE", encoding="utf-8")
            replaced = True
        fd = skills_mod.platform_compat.open_path_no_reparse(path)
        try:
            current_identity = skills_mod.platform_compat.opened_file_identity(fd)
        finally:
            os.close(fd)
        if current_identity != expected_identity:
            return False
        pytest.fail("replacement identity was accepted for unlink")

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "unlink_path_if_identity",
        replace_then_unlink,
    )

    assert loader.dismiss_pending_skill(slug) is False
    assert replaced is True
    quarantined = list(loader._quarantine_root().glob(f"{slug}--*"))
    replacements = [entry for entry in quarantined if entry.is_file() and not entry.is_symlink()]
    originals = [
        entry for entry in quarantined if skills_mod.platform_compat.is_link_or_junction(entry)
    ]
    assert len(replacements) == 1
    assert len(originals) == 1
    assert replacements[0].read_text(encoding="utf-8") == "NEWER QUARANTINE STATE"
    assert (victim / "SKILL.md").read_text(encoding="utf-8") == "VICTIM"


@pytest.mark.skipif(
    not hasattr(os, "O_PATH"),
    reason="models macOS's open flags with Linux's O_PATH; macOS runs the test above itself",
)
def test_simulated_windows_linked_dismissal_on_macos_open_flags(
    uninitialized_loader,
    monkeypatch,
):
    """The shim's link-opening branch for a host without O_PATH, run on Linux.

    macOS has no O_PATH and opens a link itself only through O_SYMLINK. Modelled
    here as that flag set, the simulated linked dismissal must still reach the
    quarantine unlink rather than refusing at the first open of the link.
    """
    open_link_itself = os.O_PATH | os.O_NOFOLLOW
    monkeypatch.delattr(os, "O_PATH")
    monkeypatch.setattr(os, "O_SYMLINK", open_link_itself, raising=False)
    test_simulated_windows_linked_dismissal_preserves_replacement(
        uninitialized_loader,
        monkeypatch,
    )


@pytest.mark.skipif(os.name != "nt", reason="native Windows FileIdInfo race")
def test_native_windows_linked_dismissal_preserves_replacement(loader, monkeypatch):
    slug = "native-windows-linked-dismissal"
    _link, victim, _metadata = _linked_pending_candidate(loader, slug)
    real_unlink = skills_mod.platform_compat.unlink_path_if_identity
    replaced = False

    def replace_then_native_unlink(path, expected_identity, *, directory):
        nonlocal replaced
        path = Path(path)
        if path.parent == loader._quarantine_root() and not replaced:
            original = path.with_name(f"{path.name}-original")
            path.rename(original)
            path.write_text("NEWER NATIVE WINDOWS STATE", encoding="utf-8")
            replaced = True
        return real_unlink(path, expected_identity, directory=directory)

    monkeypatch.setattr(
        skills_mod.platform_compat,
        "unlink_path_if_identity",
        replace_then_native_unlink,
    )

    assert loader.dismiss_pending_skill(slug) is False
    assert replaced is True
    quarantined = list(loader._quarantine_root().glob(f"{slug}--*"))
    replacements = [entry for entry in quarantined if entry.is_file() and not entry.is_symlink()]
    originals = [
        entry for entry in quarantined if skills_mod.platform_compat.is_link_or_junction(entry)
    ]
    assert len(replacements) == 1
    assert len(originals) == 1
    assert replacements[0].read_text(encoding="utf-8") == "NEWER NATIVE WINDOWS STATE"
    assert (victim / "SKILL.md").read_text(encoding="utf-8") == "VICTIM"


@pytest.mark.parametrize(
    ("method_name", "expected", "link_remains"),
    [
        ("approve_pending_skill_checked", "invalid_layout", True),
        ("dismiss_pending_skill", True, False),
    ],
)
def test_completion_marker_check_does_not_follow_linked_claim_parent(
    loader, method_name, expected, link_remains
):
    slug = f"linked-marker-parent-{method_name}"
    link, victim, metadata = _linked_pending_candidate(loader, slug)
    victim_marker = victim / ".promoted"
    victim_marker.write_text("VICTIM MARKER\n", encoding="utf-8")

    try:
        outcome = getattr(loader, method_name)(slug)
    except skills_mod.PendingApprovalRefused as refused:
        outcome = refused.reason
    assert outcome == expected

    assert skills_mod.platform_compat.is_link_or_junction(link) is link_remains
    assert (victim / "SKILL.md").read_text(encoding="utf-8") == "VICTIM"
    assert (victim / ".meta.json").read_text(encoding="utf-8") == metadata
    assert victim_marker.read_text(encoding="utf-8") == "VICTIM MARKER\n"


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_dismiss_refuses_preplanted_completion_marker_without_touching_target(
    loader, tmp_path, link_kind
):
    slug = f"marker-{link_kind}"
    loader.stage_skill_candidate(
        slug,
        description="candidate",
        triggers="candidate",
        procedure_md="## Steps\n\nORIGINAL",
        provenance=_prov(),
    )
    marker = loader._pending_root() / slug / ".promoted"
    victim = tmp_path / f"{link_kind}-victim"
    victim.write_text("DO NOT TOUCH", encoding="utf-8")
    if link_kind == "symlink":
        os.symlink(victim, marker)
    else:
        os.link(victim, marker)

    assert loader.dismiss_pending_skill(slug) is False

    assert victim.read_text(encoding="utf-8") == "DO NOT TOUCH"
    restored = loader._pending_root() / slug
    assert restored.is_dir()
    assert not os.path.lexists(restored / ".promoted")


def test_non_object_metadata_is_restored_without_rewrite(loader):
    _stage_update(loader, "list-metadata", target="auto/target")
    meta_file = loader._pending_root() / "list-metadata" / ".meta.json"
    meta_file.write_text("[]", encoding="utf-8")

    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked("list-metadata")
    assert refused.value.reason == "not_found"

    assert meta_file.read_text(encoding="utf-8") == "[]"
    assert not loader._claims_root().exists() or not list(loader._claims_root().iterdir())


def test_auto_apply_claim_failure_emits_suppressed_staged_notification(loader, monkeypatch):
    _write_live(loader, "claim-notify", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "claim-notify-update",
        target="auto/claim-notify",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    seen = []
    monkeypatch.setattr(loader, "_claim_pending_update", lambda _slug: None)
    monkeypatch.setattr(loader, "emit_pending_staged", seen.append)

    assert (
        loader.auto_apply_pending_update(
            "claim-notify-update",
            expected_candidate_binding=binding[0],
        )
        is None
    )
    assert seen == ["claim-notify-update"]


def test_failed_prepared_restore_discards_mutated_trusted_materialization(loader, monkeypatch):
    _write_live(loader, "immutable-restore", version=1, body="OLD")
    slug = "immutable-restore-update"
    _stage_update(
        loader,
        slug,
        target="auto/immutable-restore",
        scripts=[{"filename": "safe.py", "content": "print('safe')\n"}],
    )
    mutated_claims: list[Path] = []

    def mutate_claim_and_refuse(**_kwargs):
        claim = next(loader._claims_root().glob(f"{slug}--*"))
        (claim / "SKILL.md").write_text("MUTATED TRUSTED COPY\n", encoding="utf-8")
        mutated_claims.append(claim)
        return "incomplete"

    monkeypatch.setattr(loader, "_publish_prepared_skill_tree", mutate_claim_and_refuse)

    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked(slug)
    assert refused.value.reason == "promotion_failed"

    pending = loader._pending_root() / slug
    assert "new steps" in (pending / "SKILL.md").read_text(encoding="utf-8")
    assert (pending / "scripts" / "safe.py").read_text(encoding="utf-8") == "print('safe')\n"
    assert len(mutated_claims) == 1
    claim = mutated_claims[0]
    assert not claim.exists()
    assert not (loader._evidence_root() / claim.name).exists()
    assert not (loader._quarantine_root() / claim.name).exists()
    assert not loader._claim_lock_path(claim.name).exists()


@pytest.mark.skipif(os.name == "nt", reason="retained POSIX descriptors survive rename")
@pytest.mark.parametrize("late_bytes", [b"LATE PUBLIC WRITE\n", b""])
def test_failure_restoration_preserves_late_writes_and_concurrent_replacement(
    loader,
    late_bytes,
):
    slug = "restore-exact-inode"
    _stage_update(loader, slug, target="auto/restore-target")
    pending = loader._pending_root() / slug
    retained_fd = os.open(pending / "SKILL.md", os.O_WRONLY)
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    quarantine_identity = os.stat(quarantine)
    os.lseek(retained_fd, 0, os.SEEK_SET)
    os.ftruncate(retained_fd, 0)
    os.write(retained_fd, late_bytes)
    os.fsync(retained_fd)
    os.close(retained_fd)
    pending.mkdir()
    (pending / "SKILL.md").write_text("CONCURRENT REPLACEMENT\n", encoding="utf-8")
    try:
        restored = loader._restore_claimed_update(claim, fd, slug, snapshot)
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    assert restored == loader._pending_root() / f"{slug}-2"
    assert (pending / "SKILL.md").read_text(encoding="utf-8") == "CONCURRENT REPLACEMENT\n"
    assert (restored / "SKILL.md").read_bytes() == late_bytes
    assert os.path.samestat(quarantine_identity, os.stat(restored))
    assert not quarantine.exists()
    assert not claim.exists()


def test_recovery_never_adopts_unjournaled_public_quarantine(loader):
    assert loader._private_state_roots_safe(create=True) is True
    planted = loader._quarantine_root() / "planted--0123456789abcdef"
    planted.mkdir()
    (planted / "SKILL.md").write_text("UNTRUSTED\n", encoding="utf-8")

    loader._recover_abandoned_claims()

    assert planted.is_dir()
    assert not (loader._pending_root() / "planted").exists()
    assert not loader._claim_lock_path(planted.name).exists()
    assert not (loader._claims_root() / planted.name).exists()
    assert not (loader._evidence_root() / planted.name).exists()


@pytest.mark.parametrize(
    "slug",
    ["quarantine-receipt-recovery", "quarantine--receipt-recovery"],
)
def test_restart_finishes_quarantine_restore_receipt(loader, slug):
    _stage_update(loader, slug, target="auto/recovery-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    lock_path = loader._claim_lock_path(claim.name)
    assert loader._write_claim_restore_state(
        fd,
        lock_path,
        claim.name,
        restore_slug=slug,
        restore_identity=_tagged_identity_for_path(
            loader,
            loader._quarantine_root() / claim.name,
        ),
    )
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert not quarantine.exists()
    assert not claim.exists()
    assert not lock_path.exists()


def test_restart_honors_recorded_alternate_after_original_slot_frees(loader):
    slug = "recorded-alternate-free-original"
    restore_slug = f"{slug}-2"
    _stage_update(loader, slug, target="auto/recovery-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    quarantine_info = os.stat(quarantine)
    original = loader._pending_root() / slug
    original.mkdir()
    (original / "SKILL.md").write_text("TEMPORARY OCCUPANT\n", encoding="utf-8")
    assert loader._write_claim_restore_state(
        fd,
        loader._claim_lock_path(claim.name),
        claim.name,
        restore_slug=restore_slug,
        restore_identity=_tagged_identity_for_path(
            loader,
            loader._quarantine_root() / claim.name,
        ),
    )
    shutil.rmtree(original)
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [restore_slug]
    restored = restarted._pending_root() / restore_slug
    assert os.path.samestat(quarantine_info, os.stat(restored))
    assert not original.exists()
    assert not quarantine.exists()
    assert not claim.exists()


def test_restart_recorded_destination_collision_has_no_fallback(loader):
    slug = "recorded-alternate-occupied"
    restore_slug = f"{slug}-2"
    _stage_update(loader, slug, target="auto/recovery-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    quarantine_info = os.stat(quarantine)
    assert loader._write_claim_restore_state(
        fd,
        loader._claim_lock_path(claim.name),
        claim.name,
        restore_slug=restore_slug,
        restore_identity=_tagged_identity_for_path(
            loader,
            loader._quarantine_root() / claim.name,
        ),
    )
    occupant = loader._pending_root() / restore_slug
    occupant.mkdir()
    sentinel = occupant / "SKILL.md"
    sentinel.write_text("DO NOT REPLACE\n", encoding="utf-8")
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    restarted.list_pending_skills()

    assert sentinel.read_text(encoding="utf-8") == "DO NOT REPLACE\n"
    assert os.path.samestat(quarantine_info, os.stat(quarantine))
    assert claim.is_dir()
    assert not (restarted._pending_root() / slug).exists()
    assert not (restarted._pending_root() / f"{slug}-3").exists()
    assert restarted._claim_lock_path(claim.name).is_file()


@pytest.mark.parametrize(
    "slug",
    ["quarantine-before-materialization", "quarantine--before-materialization"],
)
def test_restart_restores_quarantine_when_private_materialization_is_missing(loader, slug):
    _stage_update(loader, slug, target="auto/recovery-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    assert loader._discard_trusted_claim(claim) is True
    skills_mod.platform_compat.release_lock(fd)
    os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert [row["slug"] for row in restarted.list_pending_skills()] == [slug]
    assert not quarantine.exists()
    assert not claim.exists()
    assert not restarted._claim_lock_path(claim.name).exists()


def test_private_evidence_refuses_same_name_quarantine_replacement(loader):
    slug = "quarantine-identity-replacement"
    _stage_update(loader, slug, target="auto/identity-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    detached = quarantine.with_name(f"{quarantine.name}.detached")
    quarantine.rename(detached)
    quarantine.mkdir()
    (quarantine / "SKILL.md").write_text("REPLACEMENT\n", encoding="utf-8")
    try:
        assert loader._retain_claim_evidence(claim, fd) is False
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    assert claim.is_dir()
    assert detached.is_dir()
    assert quarantine.is_dir()
    assert not (loader._evidence_root() / claim.name).exists()
    assert loader._claim_lock_path(claim.name).is_file()


@pytest.mark.parametrize(
    "slug",
    ["evidence-name-collision", "evidence--name-collision"],
)
def test_private_evidence_collision_preserves_claim_quarantine_and_collision(loader, slug):
    _stage_update(loader, slug, target="auto/evidence-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, _snapshot = claimed
    quarantine = loader._quarantine_root() / claim.name
    collision = loader._evidence_root() / claim.name
    collision.mkdir()
    sentinel = collision / "sentinel"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
    try:
        assert loader._commit_claim_consumption(claim, fd) is True
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    restarted = loader.__class__(skills_path=loader._dir, install_builtins=False)
    assert restarted.list_pending_skills() == []
    assert claim.is_dir()
    assert quarantine.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"
    assert restarted._claim_lock_path(claim.name).is_file()


def test_recovery_migrates_existing_private_hierarchy_without_evidence(loader):
    assert loader._private_state_roots_safe(create=True) is True
    evidence = loader._evidence_root()
    evidence.rmdir()
    assert not evidence.exists()

    loader._recover_abandoned_claims()

    assert evidence.is_dir()
    assert loader._private_state_roots_safe(create=False) is True


def test_recovery_refuses_linked_legacy_evidence_namespace(loader, tmp_path):
    assert loader._private_state_roots_safe(create=True) is True
    evidence = loader._evidence_root()
    evidence.rmdir()
    outside = tmp_path / "outside-evidence"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")
    skills_mod.platform_compat.symlink_or_junction(outside, evidence)

    loader._recover_abandoned_claims()

    assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH"
    assert sorted(child.name for child in outside.iterdir()) == ["sentinel"]


def test_rolled_back_cleanup_failure_is_not_reported_fully_live(loader, monkeypatch, caplog):
    slug = "rollback-cleanup-state"
    _stage_update(loader, slug, target="auto/rollback-cleanup-target")
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, fd, _consumed_at, snapshot = claimed
    monkeypatch.setattr(
        loader, "_authenticated_claim_publication", lambda *_args: {"kind": "update"}
    )
    monkeypatch.setattr(loader, "_reconcile_prepared_claim", lambda *_args: False)
    monkeypatch.setattr(loader, "_cleanup_publication_artifacts", lambda _name: False)
    monkeypatch.setattr(
        loader,
        "_restore_claimed_update",
        lambda *_args: pytest.fail("cleanup failure must retain the rolled-back claim"),
    )
    try:
        with caplog.at_level("INFO"):
            assert loader._restore_failed_promotion_claim(claim, fd, slug, snapshot) is False
    finally:
        skills_mod.platform_compat.release_lock(fd)
        os.close(fd)

    assert "rolled back but artifact cleanup failed" in caplog.text
    assert "fully live" not in caplog.text
    assert claim.is_dir()
    assert loader._claim_lock_path(claim.name).is_file()


@pytest.mark.parametrize(
    ("suppressed", "expected"),
    [
        (True, ["strict-notify-update"]),
        (False, []),
        ("true", []),
        ("false", []),
    ],
)
def test_restore_notification_suppression_requires_literal_true(
    loader, monkeypatch, suppressed, expected
):
    slug = "strict-notify-update"
    _write_live(loader, "notify-target", version=1, body="OLD")
    _stage_update(loader, slug, target="auto/notify-target")
    metadata_path = loader._pending_root() / slug / ".meta.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["notify_suppressed"] = suppressed
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(
        loader,
        "_emit_pending_staged_metadata",
        lambda staged_slug, _meta: seen.append(staged_slug),
    )
    # Refuse after the claim, so the refusal restore decides the notification.
    monkeypatch.setattr(loader, "_approve_claimed_update_locked", lambda *_a, **_k: None)

    with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
        loader.approve_pending_update_checked(slug)
    assert refused.value.reason == "promotion_failed"
    assert seen == expected

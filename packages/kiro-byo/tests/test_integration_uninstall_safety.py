"""Uninstall must not use stale or untrusted integration metadata to alter Crew."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kiro_byo import integration_manager as manager

REL = "src/kiro_crew/handler.py"


def installed_state(tmp_path: Path, *, generated: list[str] | None = None, created: list[str] | None = None):
    home = tmp_path / "app-home"
    crew = tmp_path / "site-packages" / "kiro_crew"
    crew.mkdir(parents=True)
    backup = home / "data" / "backups" / "packaged-0.12.0"
    overlay = home / "data" / "integration-runtime" / "0.12.0" / "overlay"
    backup.mkdir(parents=True)
    overlay.mkdir(parents=True)
    live = crew / "handler.py"
    live.write_text("BYO PATCH\n", encoding="utf-8")
    (backup / "handler.py").write_text("ORIGINAL\n", encoding="utf-8")
    (overlay / REL).parent.mkdir(parents=True)
    (overlay / REL).write_text("BYO PATCH\n", encoding="utf-8")
    state = {
        "crew_package": str(crew), "site_packages": str(crew.parent),
        "detected_version": "0.12.0", "backup_root": str(backup),
        "runtime_overlay_root": str(overlay), "generated_files": [REL] if generated is None else generated,
        "dashboard_assets": [], "created_files": [] if created is None else created,
    }
    state_path = home / "data" / "integration.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    runtime = crew.parent / "kiro_byo"
    runtime.mkdir()
    (runtime / "_bootstrap_state.json").write_text(json.dumps({
        "kiro_byo_home": str(home), "installed_via": "kirocrew-app",
    }), encoding="utf-8")
    (crew.parent / "kiro_byo_bootstrap.pth").write_text(
        "import kiro_byo.bootstrap_patch\n", encoding="ascii",
    )
    return home, crew, live, state_path, overlay


def test_uninstall_restores_owned_files_and_removes_runtime(tmp_path):
    home, crew, live, state_path, _ = installed_state(tmp_path)
    result = manager.uninstall_from_app(tmp_path, home)
    assert result["ok"] and result["restored_files"] == [REL]
    assert live.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert not state_path.exists()
    assert not (crew.parent / "kiro_byo").exists()
    assert not (crew.parent / "kiro_byo_bootstrap.pth").exists()


@pytest.mark.parametrize("malicious", [
    "src/kiro_crew/../outside.py",
    "src/kiro_crew/../../outside.py",
    "src/kiro_crew/nested/../../outside.py",
    "src/kiro_crew//handler.py",
    "src/kiro_crew/./handler.py",
    "src/kiro_crew/C:/outside.py",
    "src/kiro_crew/",
])
def test_uninstall_rejects_untrusted_path_without_mutation(tmp_path, malicious):
    home, crew, live, state_path, _ = installed_state(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("FOREIGN", encoding="utf-8")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["generated_files"] = [malicious]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(manager.IntegrationError, match="invalid recorded Crew seam path"):
        manager.uninstall_from_app(tmp_path, home)
    assert live.read_text(encoding="utf-8") == "BYO PATCH\n"
    assert outside.read_text(encoding="utf-8") == "FOREIGN"
    assert state_path.exists()


def test_uninstall_rejects_duplicate_or_conflicting_actions(tmp_path):
    home, _, live, state_path, _ = installed_state(tmp_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["created_files"] = [REL]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(manager.IntegrationError, match="duplicate recorded Crew seam"):
        manager.uninstall_from_app(tmp_path, home)
    assert live.read_text(encoding="utf-8") == "BYO PATCH\n"


@pytest.mark.parametrize("field, invalid", [
    ("generated_files", "src/kiro_crew/handler.py"),
    ("dashboard_assets", [REL, 42]),
    ("created_files", {REL: "owned"}),
])
def test_uninstall_rejects_malformed_manifest_without_deleting_runtime(tmp_path, field, invalid):
    home, crew, live, state_path, _ = installed_state(tmp_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state[field] = invalid
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(manager.IntegrationError, match="invalid recorded .* manifest"):
        manager.uninstall_from_app(tmp_path, home)
    assert live.read_text(encoding="utf-8") == "BYO PATCH\n"
    assert (crew.parent / "kiro_byo").is_dir()
    assert state_path.exists()


@pytest.mark.parametrize("target", ["runtime", "pth"])
def test_uninstall_refuses_symlinked_owned_runtime(tmp_path, target):
    home, crew, live, state_path, _ = installed_state(tmp_path)
    victim = crew.parent / ("kiro_byo" if target == "runtime" else "kiro_byo_bootstrap.pth")
    foreign = tmp_path / ("foreign_dir" if target == "runtime" else "foreign.pth")
    if target == "runtime":
        foreign.mkdir()
        (foreign / "_bootstrap_state.json").write_text("FOREIGN", encoding="utf-8")
        shutil.rmtree(victim)
    else:
        foreign.write_text("FOREIGN", encoding="utf-8")
        victim.unlink()
    try:
        victim.symlink_to(foreign, target_is_directory=target == "runtime")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(manager.IntegrationError, match="symlinked"):
        manager.uninstall_from_app(tmp_path, home)
    assert live.read_text(encoding="utf-8") == "BYO PATCH\n"
    assert victim.is_symlink() and foreign.exists() and state_path.exists()


def test_uninstall_rejects_symlink_without_touching_foreign_file(tmp_path):
    home, crew, live, state_path, _ = installed_state(tmp_path)
    foreign = tmp_path / "external.py"
    foreign.write_text("FOREIGN", encoding="utf-8")
    live.unlink()
    try:
        live.symlink_to(foreign)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(manager.IntegrationError, match="symlinked"):
        manager.uninstall_from_app(tmp_path, home)
    assert foreign.read_text(encoding="utf-8") == "FOREIGN"
    assert live.is_symlink() and state_path.exists()


def test_uninstall_rejects_race_after_snapshot_without_rollback_over_foreign_change(
    tmp_path, monkeypatch,
):
    home, crew, live, state_path, _ = installed_state(tmp_path)
    original_copy = shutil.copy2
    changed = False

    def concurrent_update(src, dst, *args, **kwargs):
        nonlocal changed
        result = original_copy(src, dst, *args, **kwargs)
        if Path(src) == live and "integration-transactions" in str(dst) and not changed:
            changed = True
            live.write_text("OTHER EXTENSION\n", encoding="utf-8")
        return result

    monkeypatch.setattr(manager.shutil, "copy2", concurrent_update)
    with pytest.raises(manager.IntegrationError, match="changed during uninstall preflight"):
        manager.uninstall_from_app(tmp_path, home)
    assert changed
    assert live.read_text(encoding="utf-8") == "OTHER EXTENSION\n"
    assert state_path.exists()
    assert (crew.parent / "kiro_byo").is_dir()


def test_uninstall_rejects_new_created_file_before_delete(tmp_path, monkeypatch):
    created_rel = "src/kiro_crew/created.py"
    home, crew, live, state_path, overlay = installed_state(
        tmp_path, created=[created_rel],
    )
    # The created path was absent at preflight; another process now owns it.
    original_copy = shutil.copy2
    created_path = crew / "created.py"

    def introduce_foreign_file(src, dst, *args, **kwargs):
        result = original_copy(src, dst, *args, **kwargs)
        if Path(src) == live and "integration-transactions" in str(dst):
            created_path.write_text("FOREIGN\n", encoding="utf-8")
        return result

    monkeypatch.setattr(manager.shutil, "copy2", introduce_foreign_file)
    with pytest.raises(manager.IntegrationError, match="appeared during uninstall preflight"):
        manager.uninstall_from_app(tmp_path, home)
    assert created_path.read_text(encoding="utf-8") == "FOREIGN\n"
    assert live.read_text(encoding="utf-8") == "BYO PATCH\n"
    assert state_path.exists()


def test_uninstall_rolls_back_earlier_file_when_later_restore_fails(tmp_path, monkeypatch):
    second_rel = "src/kiro_crew/other.py"
    home, crew, first, state_path, overlay = installed_state(
        tmp_path, generated=[REL, second_rel],
    )
    second = crew / "other.py"
    second.write_text("SECOND PATCH\n", encoding="utf-8")
    (overlay / second_rel).write_text("SECOND PATCH\n", encoding="utf-8")
    backup = home / "data" / "backups" / "packaged-0.12.0"
    (backup / "other.py").write_text("SECOND ORIGINAL\n", encoding="utf-8")
    original_copy = manager._atomic_copy

    def fail_second_restore(src, dst):
        if Path(src) == backup / "other.py":
            raise OSError("injected restore failure")
        return original_copy(src, dst)

    monkeypatch.setattr(manager, "_atomic_copy", fail_second_restore)
    with pytest.raises(manager.IntegrationError, match="injected restore failure"):
        manager.uninstall_from_app(tmp_path, home)
    # Restoring the first seam must not survive a failed multi-file uninstall.
    assert first.read_text(encoding="utf-8") == "BYO PATCH\n"
    assert second.read_text(encoding="utf-8") == "SECOND PATCH\n"
    assert state_path.exists()
    assert (crew.parent / "kiro_byo").is_dir()


def test_uninstall_rollback_preserves_unmodified_foreign_sibling(tmp_path, monkeypatch):
    """Rollback never restores another seam which this operation did not touch."""
    other_rel = "src/kiro_crew/other.py"
    home, crew, first, state_path, overlay = installed_state(
        tmp_path, generated=[REL, other_rel],
    )
    second = crew / "other.py"
    second.write_text("SECOND PATCH\n", encoding="utf-8")
    (overlay / other_rel).write_text("SECOND PATCH\n", encoding="utf-8")
    backup = home / "data" / "backups" / "packaged-0.12.0"
    (backup / "other.py").write_text("SECOND ORIGINAL\n", encoding="utf-8")
    original_copy = manager._atomic_copy

    def fail_after_first_restore(src, dst):
        result = original_copy(src, dst)
        if Path(dst) == first and Path(src) == backup / "handler.py":
            second.write_text("OTHER EXTENSION\n", encoding="utf-8")
            raise OSError("injected failure after first restore")
        return result

    monkeypatch.setattr(manager, "_atomic_copy", fail_after_first_restore)
    with pytest.raises(manager.IntegrationError, match="injected failure after first restore"):
        manager.uninstall_from_app(tmp_path, home)
    assert first.read_text(encoding="utf-8") == "BYO PATCH\n"
    assert second.read_text(encoding="utf-8") == "OTHER EXTENSION\n"
    assert state_path.exists()


def test_uninstall_restores_metadata_when_unlink_reports_post_delete_failure(tmp_path, monkeypatch):
    home, crew, live, state_path, _ = installed_state(tmp_path)
    original_unlink = Path.unlink

    def unlink_then_raise(self, *args, **kwargs):
        result = original_unlink(self, *args, **kwargs)
        if self == state_path:
            raise OSError("injected post-unlink failure")
        return result

    monkeypatch.setattr(Path, "unlink", unlink_then_raise)
    with pytest.raises(manager.IntegrationError, match="injected post-unlink failure"):
        manager.uninstall_from_app(tmp_path, home)
    assert state_path.exists()
    assert live.read_text(encoding="utf-8") == "BYO PATCH\n"
    assert (crew.parent / "kiro_byo").is_dir()

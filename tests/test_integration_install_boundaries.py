"""Contract tests for the install preflight phases extracted from the transaction.

These tests deliberately inspect the plans without writing Crew files. Rollback
and full installation are exercised in test_app_package.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_byo import integration_manager as mod

REL = "src/kiro_crew/seam.py"


def make_target(tmp_path: Path, *, created: bool = False) -> tuple[mod.IntegrationTarget, Path, Path]:
    crew = tmp_path / "site" / "kiro_crew"
    crew.mkdir(parents=True)
    seam = crew / "seam.py"
    app_home = tmp_path / "home"
    adapter = tmp_path / "adapter"
    target = mod.IntegrationTarget(
        "0.11.0-rc.5", crew, crew.parent, adapter, "test",
        {"files": [REL]},
        {"generated_files": [] if created else [REL],
         "created_files": [REL] if created else [], "transformer": "test"},
    )
    return target, app_home, seam


def inspect(_rel: str, source: str):
    if source.startswith("PATCH:"):
        return SimpleNamespace(state="patched", detail="")
    if source.startswith("OLD:"):
        return SimpleNamespace(state="upgradeable", detail="")
    if source.startswith("BROKEN:"):
        return SimpleNamespace(state="unknown", detail="unrecognized overlay")
    return SimpleNamespace(state="clean", detail="")


def transform(_rel: str, source: str) -> str:
    return "PATCH:" + source.removeprefix("OLD:")


def transformer():
    return ({REL}, RuntimeError, inspect, transform)


def test_clean_generated_seam_preflight_creates_plan_without_writes(tmp_path: Path):
    target, home, seam = make_target(tmp_path)
    seam.write_text("BASE\n", encoding="utf-8")
    with patch.object(mod, "_transformer_for", return_value=transformer()):
        plans, outputs, hashes, recovered, missing = mod._prepare_seam_install_plans(
            target, home, "0.11.0-rc.5"
        )
    assert outputs[REL] == "PATCH:BASE\n"
    assert hashes[REL] == mod._sha_text("BASE\n")
    assert recovered == missing == []
    assert plans[0]["backup_text"] == "BASE\n"
    assert seam.read_text(encoding="utf-8") == "BASE\n"
    assert not plans[0]["persistent_backup"].exists()


def test_existing_patched_without_backup_is_not_reclassified_foreign(tmp_path: Path):
    target, home, seam = make_target(tmp_path)
    seam.write_text("PATCH:BASE\n", encoding="utf-8")
    with patch.object(mod, "_transformer_for", return_value=transformer()):
        plans, outputs, hashes, recovered, missing = mod._prepare_seam_install_plans(
            target, home, "0.11.0-rc.5"
        )
    assert outputs[REL] == "PATCH:BASE\n"
    assert hashes[REL] == ""
    assert missing == [REL]
    assert recovered == []
    assert plans[0]["backup_text"] is None


def test_existing_patched_can_recover_hash_from_saved_compatibility(tmp_path: Path):
    target, home, seam = make_target(tmp_path)
    seam.write_text("PATCH:BASE\n", encoding="utf-8")
    previous = home / "data" / "integration-runtime" / "0.11.0-rc.5" / "compatibility.json"
    previous.parent.mkdir(parents=True)
    previous.write_text(json.dumps({"base_sha256": {REL: "abc"}}), encoding="utf-8")
    with patch.object(mod, "_transformer_for", return_value=transformer()):
        _, _, hashes, recovered, missing = mod._prepare_seam_install_plans(
            target, home, "0.11.0-rc.5"
        )
    assert hashes == {REL: "abc"}
    assert recovered == missing == []


def test_foreign_backup_is_rejected_without_touching_target(tmp_path: Path):
    target, home, seam = make_target(tmp_path)
    seam.write_text("PATCH:BASE\n", encoding="utf-8")
    backup = mod._backup_for(home, target.crew_version, REL)
    backup.parent.mkdir(parents=True)
    backup.write_text("PATCH:FOREIGN\n", encoding="utf-8")
    with patch.object(mod, "_transformer_for", return_value=transformer()):
        with pytest.raises(mod.IntegrationError, match="not clean"):
            mod._prepare_seam_install_plans(target, home, "0.11.0-rc.5")
    assert seam.read_text(encoding="utf-8") == "PATCH:BASE\n"


def test_partial_seam_without_pristine_recovery_fails_closed(tmp_path: Path):
    target, home, seam = make_target(tmp_path)
    seam.write_text("BROKEN:PATCH\n", encoding="utf-8")
    with patch.object(mod, "_transformer_for", return_value=transformer()), patch.object(
        mod, "_clean_recovery_text", return_value=None,
    ):
        with pytest.raises(mod.IntegrationError, match="refusing to transform"):
            mod._prepare_seam_install_plans(target, home, "0.11.0-rc.5")
    assert seam.read_text(encoding="utf-8") == "BROKEN:PATCH\n"


def test_created_seam_foreign_and_missing_guard(tmp_path: Path):
    target, home, seam = make_target(tmp_path, created=True)
    overlay = target.overlay_root / REL
    overlay.parent.mkdir(parents=True)
    overlay.write_text("BYO OWNED\n", encoding="utf-8")
    plans, outputs, _, _, _ = mod._prepare_seam_install_plans(
        target, home, "0.11.0-rc.5"
    )
    assert plans[0]["existed_before"] is False
    assert outputs[REL] == overlay
    assert not seam.exists()
    seam.write_text("FOREIGN\n", encoding="utf-8")
    with patch.object(mod, "_created_file_is_owned", return_value=False):
        with pytest.raises(mod.IntegrationError, match="foreign file"):
            mod._prepare_seam_install_plans(target, home, "0.11.0-rc.5")
    assert seam.read_text(encoding="utf-8") == "FOREIGN\n"


def test_commit_verifier_rejects_byte_mismatch(tmp_path: Path):
    target, _, seam = make_target(tmp_path)
    seam.write_text("OTHER\n", encoding="utf-8")
    with pytest.raises(mod.IntegrationError, match="integration commit verification failed"):
        mod._verify_installed_seams(
            target, [{"rel": REL, "dst": seam, "output_text": "PATCH:BASE\n"}],
            target.crew_package / "missing-dist",
        )
    assert seam.read_text(encoding="utf-8") == "OTHER\n"


def test_commit_verifier_requires_semantic_closure(tmp_path: Path):
    target, _, seam = make_target(tmp_path)
    seam.write_text("OLD:BASE\n", encoding="utf-8")
    with patch.object(mod, "_transformer_for", return_value=transformer()):
        with pytest.raises(mod.IntegrationError, match="semantic commit verification failed"):
            mod._verify_installed_seams(
                target, [{"rel": REL, "dst": seam, "output_text": "OLD:BASE\n"}],
                target.crew_package / "missing-dist",
            )


def _install_with_single_seam(tmp_path: Path, monkeypatch, *, precommit_issue=None):
    """Isolated transaction wiring: exercise real install rollback, not Crew discovery."""
    target, home, seam = make_target(tmp_path)
    seam.write_text("ORIGINAL\n", encoding="utf-8")
    plan = {
        "rel": REL,
        "dst": seam,
        "output_text": "PATCHED\n",
        "existed_before": True,
        "persistent_backup": home / "backup" / "seam.py",
        "backup_text": None,
    }
    monkeypatch.setattr(mod, "resolve_target", lambda *_: target)
    monkeypatch.setattr(mod, "migrate_legacy_data", lambda *_: None)
    monkeypatch.setattr(mod, "_prepare_seam_install_plans", lambda *_: ([plan], {}, {}, [], []))
    monkeypatch.setattr(mod, "_append_dashboard_install_plans", lambda *args: ([], target.crew_package))
    monkeypatch.setattr(mod, "_persist_install_baselines", lambda *_: None)
    monkeypatch.setattr(mod, "_verify_installed_seams", lambda *_: None)
    # This fixture has ONE synthetic file, not a complete Crew source set.
    # Import closure is exercised separately against the real 20-file plan.
    monkeypatch.setattr(mod, "byo_import_closure_issues", lambda *_: [])
    monkeypatch.setattr(mod, "_write_runtime_snapshot", lambda *_: (tmp_path, tmp_path, tmp_path))
    monkeypatch.setattr(mod, "_commit_runtime_and_install_metadata", lambda **_: None)
    if precommit_issue is not None:
        monkeypatch.setattr(mod, "_install_plan_precommit_issue", precommit_issue)
    else:
        monkeypatch.setattr(mod, "_install_plan_precommit_issue", lambda *_: None)
    return home, seam


def test_install_precommit_race_does_not_rollback_foreign_edit(tmp_path, monkeypatch):
    """No transaction write => no right to restore the old Crew snapshot."""
    def foreign_change(plan, *_):
        plan["dst"].write_text("OTHER EXTENSION\n", encoding="utf-8")
        return "Crew seam changed after preflight"

    home, seam = _install_with_single_seam(tmp_path, monkeypatch, precommit_issue=foreign_change)
    with pytest.raises(mod.IntegrationError, match="changed after preflight"):
        mod.install_from_app(tmp_path, home, version="0.3.2")
    assert seam.read_text(encoding="utf-8") == "OTHER EXTENSION\n"


def test_install_rolls_back_a_write_that_raises_after_replacement(tmp_path, monkeypatch):
    home, seam = _install_with_single_seam(tmp_path, monkeypatch)
    original_write = mod._atomic_write_text

    def write_then_raise(text, path):
        original_write(text, path)
        raise OSError("injected post-replace failure")

    monkeypatch.setattr(mod, "_atomic_write_text", write_then_raise)
    with pytest.raises(mod.IntegrationError, match="injected post-replace failure"):
        mod.install_from_app(tmp_path, home, version="0.3.2")
    assert seam.read_text(encoding="utf-8") == "ORIGINAL\n"

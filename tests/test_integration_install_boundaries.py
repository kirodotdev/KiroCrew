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

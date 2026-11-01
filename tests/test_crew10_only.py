"""Crew 0.9 retirement: keep the supported 0.10 install surface fail-closed."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_byo.integration_manager import IntegrationError, _transformer_for, _version_audited, resolve_target


ROOT = Path(__file__).resolve().parents[1]


def test_only_crew10_adapter_is_distributed() -> None:
    profiles = ROOT / "integration" / "kiro_crew"
    assert {p.name for p in profiles.iterdir() if p.is_dir()} == {"v0.10-adaptive", "v0.11-rc1"}
    assert not (ROOT / "src/kiro_byo/crew09_patch.py").exists()
    assert not (ROOT / "scripts/verify_crew09_integration.py").exists()
    assert json.loads((ROOT / "app.json").read_text())["minKiroCrewVersion"] == "0.11.0-rc.1"


def test_retired_transformer_is_rejected() -> None:
    with pytest.raises(IntegrationError, match="unsupported semantic transformer"):
        _transformer_for({"transformer": "adaptive09"})


@pytest.mark.parametrize("version", ["0.9.0", "0.9.0-insider.6", "0.9.1", "0.10.0", "unknown"])
def test_unsupported_crew_never_reaches_patch_preflight(tmp_path: Path, version: str) -> None:
    # Reject even if a stray/foreign legacy adapter has been placed in the App.
    fake_app = tmp_path / "app"
    legacy = fake_app / "integration/kiro_crew/v0.9-adaptive"
    legacy.mkdir(parents=True)
    (legacy / "manifest.json").write_text('{"files": []}', encoding="utf-8")
    (legacy / "compatibility.json").write_text(
        '{"transformer":"adaptive09","mode":"adaptive_semantic","min_version":"0.9.0"}',
        encoding="utf-8",
    )
    crew_pkg = tmp_path / "site-packages/kiro_crew"
    crew_pkg.mkdir(parents=True)
    with patch("kiro_byo.integration_manager.running_crew", return_value=(version, crew_pkg, crew_pkg.parent)):
        with pytest.raises(IntegrationError, match="not supported"):
            resolve_target(fake_app)
    assert list(crew_pkg.iterdir()) == []


def test_crew11_family_reaches_source_compatibility_checks(tmp_path: Path) -> None:
    # 0.10 is not blindly accepted: the upstream source still needs semantic proof.
    crew_pkg = tmp_path / "site-packages/kiro_crew"
    crew_pkg.mkdir(parents=True)
    with patch("kiro_byo.integration_manager.running_crew", return_value=("0.11.0-rc.1", crew_pkg, crew_pkg.parent)):
        with pytest.raises(IntegrationError, match="does not match a bundled"):
            resolve_target(ROOT)


def test_insider5_is_eligible_for_semantic_checks_but_not_false_claimed_as_audited() -> None:
    compat = json.loads((ROOT / "integration/kiro_crew/v0.10-adaptive/compatibility.json").read_text())
    assert _version_audited(compat, "0.10.0-insider.4")
    assert not _version_audited(compat, "0.10.0-insider.5")
    assert not _version_audited(compat, "0.10.0")
    assert not _version_audited(compat, "0.11.0")


def test_stable_011_reaches_source_validation_not_legacy_adapter(tmp_path: Path) -> None:
    crew_pkg = tmp_path / "site-packages/kiro_crew"
    crew_pkg.mkdir(parents=True)
    with patch("kiro_byo.integration_manager.running_crew", return_value=("0.11.0", crew_pkg, crew_pkg.parent)):
        with pytest.raises(IntegrationError, match="does not match a bundled"):
            resolve_target(ROOT)

"""Provisional RC2: syntactic version is never sufficient for BYO injection."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import pytest
from kiro_byo.integration_manager import (
    _adapter_matches, resolve_target, IntegrationError, _dashboard_discovery,
    _version_audited,
)

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "integration/kiro_crew/v0.11-rc1"


@pytest.mark.parametrize("version", ["0.11.0-rc.2", "0.11.0rc2", "0.11.1-rc.2"])
def test_rc2_known_candidate_but_not_verified(version):
    compat = json.loads((ADAPTER / "compatibility.json").read_text())
    assert __import__("re").fullmatch(compat["semantic_candidate_regex"], version)
    assert version not in compat["allowed_crew_versions"]
    assert not _version_audited(compat, version)
    assert _dashboard_discovery(SimpleNamespace(crew_version=version)).__module__.endswith("crew11_model_labels")


def test_candidate_rejects_absent_source_and_dashboard(tmp_path):
    crew = tmp_path / "kiro_crew"
    crew.mkdir()
    ok, issues, _, _ = _adapter_matches(ADAPTER, "0.11.0-rc.2", crew)
    assert not ok
    assert any("missing overlay dependency" in x for x in issues)
    assert any("candidate model-picker contract changed" in x for x in issues)
    assert not (crew / "acp").exists()  # source preflight never writes


def test_rc2_does_not_bypass_any_semantic_or_ui_contract(tmp_path):
    crew = tmp_path / "kiro_crew"
    crew.mkdir()
    meta = json.loads((ADAPTER / "compatibility.json").read_text())
    for rel in meta["generated_files"]:
        path = crew / rel.removeprefix("src/kiro_crew/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n", encoding="utf-8")

    # Mock only the *shape* of an audited source inspector. Actual RC2 bytes
    # are unavailable, so these tests do not assert real RC2 compatibility.
    patched = SimpleNamespace(state="patched")
    with patch("kiro_byo.integration_manager.compatibility_contract_issues", return_value=[]), \
         patch("kiro_byo.integration_manager._transformer_for", return_value=(
             frozenset(meta["generated_files"]), RuntimeError, lambda _r, _t: patched, lambda _r, t: t
         )), \
         patch("kiro_byo.integration_manager.crew11_model_labels.discover_dashboard_patches", return_value=[]) as ui:
        ok, issues, _, _ = _adapter_matches(ADAPTER, "0.11.0-rc.2", crew)
        assert ok, issues
        ui.assert_called_once_with(crew, timing_asset=ROOT / "src" / "kiro_byo" / "assets" / "crew11_ui_timing.js")
        # The selected candidate may proceed ONLY because every required
        # source and model UI check above was explicitly passed.
        with patch("kiro_byo.integration_manager.running_crew", return_value=(
            "0.11.0-rc.2", crew, tmp_path
        )):
            assert resolve_target(ROOT).crew_version == "0.11.0-rc.2"

    with patch("kiro_byo.integration_manager.compatibility_contract_issues", return_value=[]), \
         patch("kiro_byo.integration_manager._transformer_for", return_value=(
             frozenset(meta["generated_files"]), RuntimeError, lambda _r, _t: patched, lambda _r, t: t
         )), \
         patch("kiro_byo.integration_manager.crew11_model_labels.discover_dashboard_patches", side_effect=RuntimeError("test UI drift")):
        # Reject unknown unexpected errors rather than silently fallback.
        with pytest.raises(RuntimeError, match="test UI drift"):
            _adapter_matches(ADAPTER, "0.11.0-rc.2", crew)


def test_rc2_missing_contract_is_rejected_by_target_resolver(tmp_path):
    crew = tmp_path / "kiro_crew"
    crew.mkdir()
    with patch("kiro_byo.integration_manager.running_crew", return_value=("0.11.0-rc.2", crew, tmp_path)):
        with pytest.raises(IntegrationError, match="does not match"):
            resolve_target(ROOT)

"""Unreleased Crew RCs are *candidates* only, never automatically compatible."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json

import pytest

from kiro_byo.integration_manager import (
    IntegrationError,
    _adapter_matches,
    _crew11_semantic_candidate,
    _dashboard_discovery,
    _version_audited,
    resolve_target,
)

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "integration/kiro_crew/v0.11-rc1"


@pytest.mark.parametrize("version", [
    "0.11.0-rc.2", "0.11.0rc2", "0.11.0-rc.3", "0.11.0rc3",
    "0.11.0-rc.4", "0.11.0-rc.10", "0.11.0rc12",
    "0.11.1-rc.1", "0.11.1rc1", "0.11.1-rc.2", "0.11.1rc2", "0.11.1-rc.10",
])
def test_all_later_011_rc_versions_are_only_semantic_candidates(version: str) -> None:
    meta = json.loads((ADAPTER / "compatibility.json").read_text(encoding="utf-8"))
    assert _crew11_semantic_candidate(version)
    assert not _version_audited(meta, version)
    assert _dashboard_discovery(SimpleNamespace(crew_version=version)).__module__.endswith("crew11_model_labels")


@pytest.mark.parametrize("version", [
    "0.11.1-rc.0", "0.11.1-rc.01", "0.11.1-preview.2", "0.12.0-rc.3", "0.10.0", "0.11.0-rc.0",
    "0.11.0-rc.01", "0.11.0-rc.03", "0.11.0-rc.3-debug", "0.11.0-preview.3", "unknown",
])
def test_candidate_family_does_not_expand_to_unrelated_versions(version: str) -> None:
    assert not _crew11_semantic_candidate(version)


@pytest.mark.parametrize("version", ["0.11.0-rc.3", "0.11.0rc3", "0.11.0-rc.10", "0.11.1-rc.2"])
def test_rc_candidate_with_missing_real_source_must_fail_closed(tmp_path: Path, version: str) -> None:
    crew = tmp_path / "kiro_crew"
    crew.mkdir()
    (crew / "safe.txt").write_text("unchanged", encoding="utf-8")
    ok, issues, _, _ = _adapter_matches(ADAPTER, version, crew)
    assert not ok
    assert any("missing" in issue for issue in issues)
    assert any("candidate model-picker contract changed" in issue for issue in issues)
    with patch("kiro_byo.integration_manager.running_crew", return_value=(version, crew, tmp_path)):
        with pytest.raises(IntegrationError, match="does not match"):
            resolve_target(ROOT)
    assert (crew / "safe.txt").read_text(encoding="utf-8") == "unchanged"
    assert not (crew / "acp").exists()


def test_rc3_requires_semantic_source_and_dashboard_both_to_pass(tmp_path: Path) -> None:
    crew = tmp_path / "kiro_crew"
    crew.mkdir()
    meta = json.loads((ADAPTER / "compatibility.json").read_text(encoding="utf-8"))
    for rel in meta["generated_files"]:
        path = crew / rel.removeprefix("src/kiro_crew/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n", encoding="utf-8")

    # Synthetic inspector only proves that the version gate invokes the real
    # contract pipeline. It does not certify RC3 source or Windows E2E.
    patched = SimpleNamespace(state="patched")
    semantic = (frozenset(meta["generated_files"]), RuntimeError,
                lambda _name, _text: patched, lambda _name, source: source)
    with patch("kiro_byo.integration_manager.compatibility_contract_issues", return_value=[]), \
         patch("kiro_byo.integration_manager._transformer_for", return_value=semantic), \
         patch("kiro_byo.integration_manager.crew11_model_labels.discover_dashboard_patches", return_value=[]) as ui:
        ok, issues, _, _ = _adapter_matches(ADAPTER, "0.11.0-rc.3", crew)
        assert ok, issues
        ui.assert_called_once_with(crew, timing_asset=ROOT / "src" / "kiro_byo" / "assets" / "crew11_ui_timing.js")
        with patch("kiro_byo.integration_manager.running_crew", return_value=("0.11.0-rc.3", crew, tmp_path)):
            assert resolve_target(ROOT).crew_version == "0.11.0-rc.3"

    with patch("kiro_byo.integration_manager.compatibility_contract_issues", return_value=[]), \
         patch("kiro_byo.integration_manager._transformer_for", return_value=semantic), \
         patch("kiro_byo.integration_manager.crew11_model_labels.discover_dashboard_patches", side_effect=OSError("picker changed")):
        ok, issues, _, _ = _adapter_matches(ADAPTER, "0.11.0-rc.3", crew)
        assert not ok
        assert any("picker changed" in issue for issue in issues)


def test_stable_011_is_source_gated_but_not_verified(tmp_path: Path) -> None:
    meta = json.loads((ADAPTER / "compatibility.json").read_text(encoding="utf-8"))
    assert _crew11_semantic_candidate("0.11.0")
    assert not _version_audited(meta, "0.11.0")
    assert _dashboard_discovery(SimpleNamespace(crew_version="0.11.0")).__module__.endswith("crew11_model_labels")
    crew = tmp_path / "kiro_crew"
    crew.mkdir()
    (crew / "sentinel.txt").write_text("unchanged")
    ok, issues, _, _ = _adapter_matches(ADAPTER, "0.11.0", crew)
    assert not ok and any("missing" in issue for issue in issues)
    with patch("kiro_byo.integration_manager.running_crew", return_value=("0.11.0", crew, tmp_path)):
        with pytest.raises(IntegrationError, match="does not match"):
            resolve_target(ROOT)
    assert (crew / "sentinel.txt").read_text() == "unchanged"


def test_0111_is_provisional_source_candidate_and_fails_without_real_source(tmp_path: Path) -> None:
    from kiro_byo.integration_manager import _crew11_semantic_candidate, _version_audited
    crew = tmp_path / 'kiro_crew'
    crew.mkdir()
    meta = json.loads((ADAPTER / 'compatibility.json').read_text(encoding='utf-8'))
    assert _crew11_semantic_candidate('0.11.1')
    assert not _version_audited(meta, '0.11.1')
    ok, issues, _, _ = _adapter_matches(ADAPTER, '0.11.1', crew)
    assert not ok
    assert any('missing' in issue for issue in issues)
    with patch('kiro_byo.integration_manager.running_crew', return_value=('0.11.1', crew, tmp_path)):
        with pytest.raises(IntegrationError, match='does not match'):
            resolve_target(ROOT)

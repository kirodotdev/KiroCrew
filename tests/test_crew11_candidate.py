"""0.2.5: the exact Crew RC1 candidate is guarded by source-contract proof.

Synthetic tests do NOT substitute for the tagged upstream source or real
Windows Conductor/Worker integration tests.
"""
from __future__ import annotations
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch
import pytest

from kiro_byo.integration_manager import (
    IntegrationError, resolve_target, _adapter_matches, _version_audited, _transformer_for,
)

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "integration/kiro_crew/v0.11-rc1"


def test_rc1_profile_exact_source_pinned_not_e2e_verified() -> None:
    meta = json.loads((ADAPTER / "compatibility.json").read_text())
    assert meta["transformer"] == "adaptive11"
    assert meta["allowed_crew_versions"] == ["0.11.0-rc.1", "0.11.0rc1"]
    assert meta["semantic_candidate_regex"] == r"^0\.11\.(?:0(?:-rc\.|rc)(?:[2-9]|[1-9][0-9]+)|1(?:-rc\.|rc)(?:[1-9]|[1-9][0-9]+))$"
    assert not _version_audited(meta, "0.11.0-rc.2")
    assert _version_audited(meta, "0.11.0-rc.1")
    assert len(meta["pinned_source_sha256"]) == 21
    assert len(meta["generated_files"]) == 23
    assert set(meta["semantic_clock_seams_unpinned"]) == set(meta["generated_files"]) - set(meta["pinned_source_sha256"])
    assert "src/kiro_crew/config/loader_builders.py" in meta["generated_files"]
    assert "mate_worker_projection" in meta["required_contract_symbols"]["src/kiro_crew/acp/harness/kas.py"]
    assert _transformer_for(meta)[0]  # an explicit transformer is required
    app = json.loads((ROOT / "app.json").read_text())
    assert app["version"] == "0.3.0"
    assert app["minKiroCrewVersion"] == "0.11.0-rc.1"


@pytest.mark.parametrize("version", ["0.10.0", "0.10.0-insider.9", "0.11.0-rc.0", "0.11.0-rc.03", "0.11.0-preview.3", "0.11.2", "0.12.0", "unknown"])
def test_unpinned_version_cannot_patch(tmp_path: Path, version: str) -> None:
    crew = tmp_path / "kiro_crew"
    crew.mkdir()
    with patch("kiro_byo.integration_manager.running_crew", return_value=(version, crew, tmp_path)):
        with pytest.raises(IntegrationError, match="not supported"):
            resolve_target(ROOT)


@pytest.mark.parametrize("version", ["0.11.0-rc.1", "0.11.0rc1"])
def test_pinned_version_still_requires_actual_source(tmp_path: Path, version: str) -> None:
    crew = tmp_path / "kiro_crew"
    crew.mkdir()
    (crew / "sentinel.txt").write_text("KEEP", encoding="utf-8")
    with patch("kiro_byo.integration_manager.running_crew", return_value=(version, crew, tmp_path)):
        with pytest.raises(IntegrationError, match="does not match"):
            resolve_target(ROOT)
    assert (crew / "sentinel.txt").read_text() == "KEEP"
    assert sorted(p.name for p in crew.iterdir()) == ["sentinel.txt"]


def test_old_profile_not_eligible_for_rc1(tmp_path: Path) -> None:
    crew = tmp_path / "kiro_crew"
    crew.mkdir()
    old = ROOT / "integration/kiro_crew/v0.10-adaptive"
    ok, issues, _, _ = _adapter_matches(old, "0.11.0-rc.1", crew)
    assert not ok
    assert any("ceiling" in issue for issue in issues)


def test_mate2_linkage_requires_verified_worker_kit_projection() -> None:
    routes = (ROOT / "backend/routes.py").read_text(encoding="utf-8")
    overlay = (ADAPTER / "overlay/src/kiro_crew/acp/harness/kiro_byo.py").read_text(encoding="utf-8")
    patcher = (ROOT / "src/kiro_byo/crew11_patch.py").read_text(encoding="utf-8")
    assert "_linkage_write_supported" in routes
    assert "ACP_BACKENDS_MATE_WORKER_KIT" in routes
    assert "_reject_crew11_mate_linkage_write" not in routes
    assert "resolve_projected_spec" in overlay
    assert "mate_worker_projection" in overlay
    assert "MATE_WORKER_AGENT" in overlay
    assert "ACP_BACKENDS_MATE_WORKER_KIT" in patcher


def test_no_upstream_source_preflight_fails_without_side_effects(tmp_path: Path) -> None:
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from verify_crew11_integration import preflight
        missing = tmp_path / "missing"
        result = preflight(missing, "0.11.0-rc.1")
        assert not result["eligible_to_attempt_install"]
        assert not missing.exists()
        assert result["desktop_e2e_verified"] is False
    finally:
        sys.path.remove(str(ROOT / "scripts"))

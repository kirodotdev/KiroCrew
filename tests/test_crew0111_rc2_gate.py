"""Crew 0.11.1-rc.2 may be audited but not blindly patched."""
from pathlib import Path
from unittest.mock import patch
import pytest

from kiro_byo.integration_manager import (
    IntegrationError, _crew11_semantic_candidate, _adapter_matches,
    _version_audited, resolve_target,
)

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "integration/kiro_crew/v0.11-rc1"


def test_rc2_does_not_carry_source_audit_claim():
    assert _crew11_semantic_candidate("0.11.1-rc.2")
    import json
    compat = json.loads((ADAPTER / "compatibility.json").read_text())
    assert not _version_audited(compat, "0.11.1-rc.2")


def test_rc2_without_crew_source_must_reject_without_writing(tmp_path):
    crew = tmp_path / "kiro_crew"
    crew.mkdir()
    (crew / "sentinel.txt").write_text("unchanged")
    valid, reasons, _, _ = _adapter_matches(ADAPTER, "0.11.1-rc.2", crew)
    assert not valid
    assert any("missing overlay dependency" in reason for reason in reasons)
    assert any("candidate model-picker contract changed" in reason for reason in reasons)
    with patch("kiro_byo.integration_manager.running_crew", return_value=("0.11.1-rc.2",crew,tmp_path)):
        with pytest.raises(IntegrationError, match="does not match"):
            resolve_target(ROOT)
    assert list(crew.iterdir()) == [crew / "sentinel.txt"]
    assert (crew / "sentinel.txt").read_text() == "unchanged"


@pytest.mark.parametrize("version", ["0.11.2-rc.1", "0.11.2", "0.12.0-rc.1", "0.11.1-rc.0", "0.11.1-rc.02"] )
def test_unreviewed_family_cannot_enter(version):
    assert not _crew11_semantic_candidate(version)

"""R74: retire the optional Crew tool_gate display branch without changing auth."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_byo import crew12_patch, crew_current_patch
from kiro_byo.crew_contract_checks import retired_tool_gate_contract_issues
from kiro_byo.integration_manager import IntegrationError, _prepare_seam_install_plans

ROOT = Path(__file__).resolve().parents[1]
REL = "src/kiro_crew/agent_sdk/tool_gate.py"


@pytest.fixture
def source_gate() -> str:
    # Use the official 0.12.0 owner's text for a true before/after comparison.
    source = os.getenv("KIROCREW_012_SOURCE")
    if not source:
        pytest.skip("provide KIROCREW_012_SOURCE for real Crew test")
    return (Path(source) / REL).read_text(encoding="utf-8")


def test_tool_gate_no_longer_in_active_mutation_inventory():
    profile = ROOT / "integration/kiro_crew/v0.12-plus"
    compat = json.loads((profile / "compatibility.json").read_text())
    manifest = json.loads((profile / "manifest.json").read_text())
    assert REL not in crew12_patch.TARGET_FILES
    assert REL not in compat["generated_files"]
    assert REL not in manifest["files"]
    assert REL in compat["retired_generated_files"]
    # Was 20; reduced to 17 after retiring the turn-timing patches and
    # member_sessions.py (see compatibility.json retired_generated_files).
    assert len(compat["generated_files"]) == 17


def test_unmodified_host_has_audited_indeterminate_fallback(tmp_path, source_gate):
    pkg = tmp_path / "kiro_crew"
    path = pkg / "agent_sdk/tool_gate.py"
    path.parent.mkdir(parents=True)
    path.write_text(source_gate)
    assert retired_tool_gate_contract_issues(pkg) == []
    # The old R73 display-only injection is not an enforcement mechanism.
    old_patched = crew_current_patch.patch_text(REL, source_gate)
    assert old_patched != source_gate
    path.write_text(old_patched)
    assert retired_tool_gate_contract_issues(pkg) == []


@pytest.mark.parametrize("attack", ["promote", "default_routed", "no_default", "unconditional_enforce", "computed_enforcement"])
def test_contract_rejects_a_silent_permission_promotion(tmp_path, source_gate, attack):
    pkg = tmp_path / "kiro_crew"
    path = pkg / "agent_sdk/tool_gate.py"
    path.parent.mkdir(parents=True)
    changed = source_gate
    if attack == "promote":
        changed = changed.replace("Routing.SESSION_CONFIG,", "Routing.ACP_PERMISSION_GATE,\n        Routing.SESSION_CONFIG,", 1)
    elif attack == "default_routed":
        marker = '"Kiro Crew has not established how this harness routes tool calls",'
        assert marker in changed
        changed = changed.replace('        Verdict.INDETERMINATE,\n        ' + marker, '        Verdict.ROUTED,\n        ' + marker)
    elif attack == "no_default":
        marker = '"Kiro Crew has not established how this harness routes tool calls",'
        assert marker in changed
        changed = changed.replace('    return (\n        Verdict.INDETERMINATE,\n        ' + marker + '\n    )', '    return None')
    elif attack == "computed_enforcement":
        changed = changed.replace("ENFORCED_ROUTINGS: frozenset = frozenset(",
                                  "ENFORCED_ROUTINGS: frozenset = set(", 1)
    else:
        changed = changed.replace("return routing_for(backend) in ENFORCED_ROUTINGS", "return True")
    assert changed != source_gate
    path.write_text(changed)
    assert retired_tool_gate_contract_issues(pkg)


def _prior_install(tmp_path, source_gate):
    pkg = tmp_path / "crew" / "kiro_crew"
    path = pkg / "agent_sdk" / "tool_gate.py"
    path.parent.mkdir(parents=True)
    patched = crew_current_patch.patch_text(REL, source_gate)
    assert patched != source_gate
    path.write_text(patched)
    home = tmp_path / "home"
    backup = home / "data/backups/packaged-0.12.0/agent_sdk/tool_gate.py"
    backup.parent.mkdir(parents=True)
    backup.write_text(source_gate)
    snap = home / "data/integration-runtime/0.12.0/overlay" / REL
    snap.parent.mkdir(parents=True)
    snap.write_text(patched)
    receipt = home / "data/integration.json"
    receipt.write_text(json.dumps({
        "detected_version":"0.12.0", "crew_package":str(pkg),
        "runtime_overlay_root":str(home / "data/integration-runtime/0.12.0/overlay"),
        "generated_files":[REL]
    }))
    target = SimpleNamespace(crew_package=pkg, crew_version="0.12.0",
        generated_files=frozenset(), created_files=frozenset(),
        compatibility={"retired_generated_files":[REL]},manifest={"files":[]})
    return target, home, path, backup, snap, receipt


def test_r73_to_r74_retirement_restores_original_only_with_proven_receipt(tmp_path, source_gate):
    target, home, path, backup, snap, receipt = _prior_install(tmp_path, source_gate)
    plans, *_ = _prepare_seam_install_plans(target, home, "0.12.0")
    assert len(plans) == 1 and plans[0]["retired_file"]
    assert plans[0]["output_text"] == source_gate
    assert path.read_text() != source_gate  # planning is read-only
    path.write_text(plans[0]["output_text"])
    plans, *_ = _prepare_seam_install_plans(target, home, "0.12.0")
    assert plans == []


@pytest.mark.parametrize("attack", ["no_receipt", "foreign_edit", "bad_snapshot", "bad_backup", "other_host"])
def test_r73_to_r74_retirement_rejects_unproven_owned_file(tmp_path, source_gate, attack):
    target, home, path, backup, snap, receipt = _prior_install(tmp_path, source_gate)
    if attack == "no_receipt": receipt.unlink()
    elif attack == "foreign_edit": path.write_text(path.read_text() + "\n# external change\n")
    elif attack == "bad_snapshot": snap.write_text("bad")
    elif attack == "bad_backup": backup.write_text("bad")
    else:
        data = json.loads(receipt.read_text())
        data["crew_package"] = str(tmp_path / "other" / "kiro_crew")
        receipt.write_text(json.dumps(data))
    with pytest.raises(IntegrationError):
        _prepare_seam_install_plans(target, home, "0.12.0")

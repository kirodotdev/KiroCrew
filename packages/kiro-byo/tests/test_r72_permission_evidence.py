"""No static ACP permission declaration may be reported as real Host enforcement."""
from __future__ import annotations

from scripts.verify_crew12_source_subset import permission_evidence


def _write_gate(tmp_path, enforced: bool):
    gate = tmp_path / "agent_sdk" / "tool_gate.py"
    gate.parent.mkdir(parents=True)
    member = "Routing.ACP_PERMISSION_GATE, " if enforced else ""
    gate.write_text(f'''class Routing:
    ACP_PERMISSION_GATE = "acp_permission_gate"
    SESSION_CONFIG = "session_config"
class Verdict:
    ROUTED = "routed"
ENFORCED_ROUTINGS = frozenset({{{member}Routing.SESSION_CONFIG}})
def routing_verdict(backend):
    if backend == Routing.ACP_PERMISSION_GATE:
        return Verdict.ROUTED, "declared"
''')


def test_unverified_gate_is_exposed_not_certified(tmp_path, monkeypatch):
    from scripts import verify_crew12_source_subset as module
    _write_gate(tmp_path, False)
    monkeypatch.setattr(module.crew12_patch, "patch_text", lambda rel, source: source)
    evidence = permission_evidence(tmp_path)
    assert evidence["byoPermissionBranchPresent"] is True
    assert evidence["byoInHostEnforcedRoutings"] is False
    assert evidence["runtimeDenyAllowVerified"] is False
    assert evidence["status"] == "runtime_permission_evidence_missing"


def test_enforced_enum_is_still_not_e2e_proof(tmp_path, monkeypatch):
    from scripts import verify_crew12_source_subset as module
    _write_gate(tmp_path, True)
    monkeypatch.setattr(module.crew12_patch, "patch_text", lambda rel, source: source)
    evidence = permission_evidence(tmp_path)
    assert evidence["byoInHostEnforcedRoutings"] is True
    assert evidence["runtimeDenyAllowVerified"] is False


def test_absent_tool_gate_is_explicitly_unknown(tmp_path):
    assert permission_evidence(tmp_path) == {
        "status": "source_unavailable", "runtimeDenyAllowVerified": False,
    }

"""Native Harness registration inspection must not be confused with approval."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from kiro_byo.crew_native_probe import inspect_native_registration, inspect_native_sources
from scripts.inspect_crew_host import archive_native_report

ROOT = Path(__file__).resolve().parents[1]
CORE = '''\nACP_BACKENDS_KNOWN = {"kas", "codex"}\ndef register_selectable_backend(backend_id):\n    if backend_id not in ACP_BACKENDS_KNOWN:\n        raise ValueError("unknown backend")\n'''
ROUTING = '''ACP_BACKEND_ROUTING = {"codex": "verified"}\n'''


def test_unknown_third_party_backend_does_not_become_selectable():
    report = inspect_native_sources(CORE, ROUTING)
    assert report["status"] == "core_known_backend_gate"
    assert report["has_registry_function"]
    assert report["has_permission_routing_source"]
    assert report["native_integration_verified"] is False


def test_backend_literal_only_allows_static_candidate_not_certification():
    report = inspect_native_sources(CORE.replace('"codex"', '"codex", "kiro-byo"'), ROUTING)
    assert report["status"] == "registration_not_demonstrated"
    assert report["byo_known_member"]
    assert not report["byo_routing_member"]
    assert report["native_integration_verified"] is False


def test_unknown_or_invalid_source_is_fail_closed(tmp_path):
    pkg = tmp_path / "kiro_crew"
    assert inspect_native_registration(pkg)["status"] == "source_unavailable"
    (pkg / "agent_sdk").mkdir(parents=True)
    (pkg / "agent_sdk/backends.py").write_text("def broken(:\n")
    assert inspect_native_registration(pkg)["status"] == "source_invalid"


def test_archive_without_writing_to_host(tmp_path):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("KiroCrew-0.12.0/src/kiro_crew/agent_sdk/backends.py", CORE)
        zf.writestr("KiroCrew-0.12.0/src/kiro_crew/agent_sdk/backends_routing.py", ROUTING)
    before = set(tmp_path.iterdir())
    result = archive_native_report(archive)
    assert result["status"] == "core_known_backend_gate"
    assert not result["source_tag_authenticated"]
    assert result["missing_or_rejected_members"] == {}
    assert set(tmp_path.iterdir()) == before


def test_archive_ambiguous_members_do_not_gain_trust(tmp_path):
    archive = tmp_path / "ambiguous.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("A/src/kiro_crew/agent_sdk/backends.py", CORE)
        zf.writestr("B/src/kiro_crew/agent_sdk/backends.py", CORE)
        zf.writestr("A/src/kiro_crew/agent_sdk/backends_routing.py", ROUTING)
    result = archive_native_report(archive)
    assert result["status"] == "source_unavailable"
    assert "backends.py" in next(iter(result["missing_or_rejected_members"]))


def test_cli_diagnostics_never_announce_installed_compatibility(tmp_path):
    pkg = tmp_path / "kiro_crew"
    sdk = pkg / "agent_sdk"
    sdk.mkdir(parents=True)
    (sdk / "backends.py").write_text(CORE)
    (sdk / "backends_routing.py").write_text(ROUTING)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/inspect_crew_host.py"),
                             "--crew-package", str(pkg)], capture_output=True, text=True,
                            check=True, cwd=ROOT)
    report = json.loads(result.stdout)
    assert report["status"] == "core_known_backend_gate"
    assert report["host_e2e_verified"] is False


def test_zip_traversal_and_symlink_cannot_impersonate_source(tmp_path):
    archive = tmp_path / "hostile.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../src/kiro_crew/agent_sdk/backends.py", CORE)
        link = zipfile.ZipInfo("source/src/kiro_crew/agent_sdk/backends_routing.py")
        link.create_system = 3
        link.external_attr = (0o120777 << 16)
        zf.writestr(link, ROUTING)
    result = archive_native_report(archive)
    assert result["status"] == "source_unavailable"
    assert len(result["missing_or_rejected_members"]) == 2


def test_existing_source_contract_preflight_includes_native_evidence(tmp_path):
    pkg = tmp_path / "kiro_crew"
    sdk = pkg / "agent_sdk"
    sdk.mkdir(parents=True)
    (sdk / "backends.py").write_text(CORE)
    (sdk / "backends_routing.py").write_text(ROUTING)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_crew12_integration.py"),
                             "--crew-package", str(pkg), "--crew-version", "0.12.0"],
                            capture_output=True, text=True, cwd=ROOT)
    report = json.loads(result.stdout)
    assert result.returncode == 2  # Missing host seams; never fake success.
    assert report["source_contract_status"] == "refused"
    assert report["native_harness_registration"]["status"] == "core_known_backend_gate"
    assert report["native_harness_registration"]["native_integration_verified"] is False


def test_native_candidate_requires_actual_known_membership_and_route():
    core = CORE.replace('"kas", "codex"', '"kas", ACP_BACKEND_KIRO_BYO')
    core = 'ACP_BACKEND_KIRO_BYO = "kiro-byo"\n' + core
    route = 'ACP_BACKEND_ROUTING = {ACP_BACKEND_KIRO_BYO: "acp_permission_gate"}\n'
    result = inspect_native_sources(core, route)
    assert result["status"] == "core_registration_candidate"
    assert result["byo_known_member"] and result["byo_routing_member"]
    assert not result["native_integration_verified"]


def test_stray_literal_and_comment_cannot_impersonate_native_registry():
    core = CORE + '\n# ACP_BACKENDS_KNOWN = {"kiro-byo"}\n' + 'UNRELATED_MESSAGE = "kiro-byo"\n'
    route = ROUTING + '\n# ACP_BACKEND_ROUTING = {"kiro-byo": "verified"}\n'
    result = inspect_native_sources(core, route)
    assert result["status"] == "core_known_backend_gate"
    assert not result["byo_known_member"]
    assert not result["byo_routing_member"]


def test_byo_known_but_no_permissions_still_is_not_candidate():
    core = CORE.replace('"kas", "codex"', '"kas", "kiro-byo"')
    result = inspect_native_sources(core, ROUTING)
    assert result["status"] == "registration_not_demonstrated"
    assert result["byo_known_member"]
    assert not result["byo_routing_member"]
    assert any("permission" in x for x in result["blockers"])


def test_invalid_routing_source_is_rejected():
    result = inspect_native_sources(CORE, "ACP_BACKEND_ROUTING = {\n")
    assert result["status"] == "source_invalid"
    assert not result["native_integration_verified"]


def test_frozenset_symbolic_membership_is_recognized_without_execution():
    core = """ACP_BACKEND_KIRO_BYO = "kiro-byo"
ACP_BACKENDS_KNOWN = frozenset({"kas", ACP_BACKEND_KIRO_BYO})
def register_selectable_backend(x): return x
"""
    routing = """ACP_BACKEND_KIRO_BYO = "kiro-byo"
ACP_BACKEND_ROUTING = {ACP_BACKEND_KIRO_BYO: "acp_permission_gate"}
"""
    result = inspect_native_sources(core, routing)
    assert result["status"] == "core_registration_candidate"
    assert result["native_integration_verified"] is False

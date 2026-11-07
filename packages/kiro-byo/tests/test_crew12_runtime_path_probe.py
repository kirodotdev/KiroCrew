"""A successful registry probe must never be promoted into a host ACP session."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.verify_crew12_runtime_path import audit


def fake_crew(tmp_path: Path) -> Path:
    sdk = tmp_path / "kiro_crew" / "agent_sdk"
    sdk.mkdir(parents=True)
    (sdk / "backends.py").write_text("# synthetic fixture", encoding="utf-8")
    return tmp_path


def known_registry() -> dict:
    return {"backend": "kiro-byo", "selectable": True,
            "hostLaunchDescriptionPresent": True,
            "hostNonRoutedRefusalEnforced": False}


def test_missing_package_rejected(tmp_path):
    with pytest.raises(ValueError, match="crew-src"):
        audit(tmp_path)


def test_runtime_missing_dependency_does_not_report_session(tmp_path):
    root = fake_crew(tmp_path)
    from subprocess import CompletedProcess
    with patch("scripts.verify_crew12_runtime_path.probe_registry", return_value=known_registry()), \
         patch("scripts.verify_crew12_runtime_path.subprocess.run", return_value=CompletedProcess([], 0, '{"runtimeImport":"blocked_dependency","dependency":"croniter"}', "")):
        result = audit(root)
    assert result["status"] == "blocked"
    assert result["runtime"]["dependency"] == "croniter"
    assert result["actualSessionNew"] == "not_run"
    assert result["toolPermissionE2E"] == "not_verified"
    assert result["nativeWithoutCorePatch"] is False


def test_constructor_success_still_does_not_certify_permissions(tmp_path):
    root = fake_crew(tmp_path)
    from subprocess import CompletedProcess
    child = '{"runtimeImport":"pass","adapterConstructed":true,"runtimeConstructed":true}'
    with patch("scripts.verify_crew12_runtime_path.probe_registry", return_value=known_registry()), \
         patch("scripts.verify_crew12_runtime_path.subprocess.run", return_value=CompletedProcess([], 0, child, "")):
        result = audit(root)
    assert result["status"] == "runtime_constructor_pass"
    assert result["toolPermissionE2E"] == "not_verified"
    assert result["actualSessionNew"] == "not_run"


def test_registry_rejection_cannot_be_masked_by_import_success(tmp_path):
    root = fake_crew(tmp_path)
    from subprocess import CompletedProcess
    child = '{"runtimeImport":"pass","adapterConstructed":true,"runtimeConstructed":true}'
    registry = known_registry() | {"selectable": False}
    with patch("scripts.verify_crew12_runtime_path.probe_registry", return_value=registry), \
         patch("scripts.verify_crew12_runtime_path.subprocess.run", return_value=CompletedProcess([], 0, child, "")):
        assert audit(root)["status"] == "blocked"


def test_invalid_child_response_is_failure(tmp_path):
    root = fake_crew(tmp_path)
    from subprocess import CompletedProcess
    with patch("scripts.verify_crew12_runtime_path.probe_registry", return_value=known_registry()), \
         patch("scripts.verify_crew12_runtime_path.subprocess.run", return_value=CompletedProcess([], 1, "garbage", "broken import")):
        result = audit(root)
    assert result["status"] == "blocked"
    assert result["runtime"]["errorType"] == "InvalidChildResponse"


def test_tool_path_executes_no_session_creation():
    from scripts import verify_crew12_runtime_path as module
    assert "session/new" not in module._CHILD
    assert "runtime.spawn(" not in module._CHILD

"""Installed signature refusals share one payload-free, best-effort audit boundary."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, call, create_autospec

import pytest

from kiro_crew.apps import admission, execution
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.sel import SecurityEventLog

_SECRET = "audit-publisher-secret"
_SCRIPT = "echo private-script-text"
_DESCRIPTION = "private-manifest-description"
_REFUSALS = {
    "missing": "app 'probe' signature invalid or unsigned",
    "signature": "app 'probe' signature invalid or unsigned",
    "tree": (
        "app 'probe' files do not match its signature: "
        "backend.py does not match its signed digest"
    ),
    "script": "app 'probe' script is not one its signed manifest declares",
}


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> Mock:
    sink = create_autospec(SecurityEventLog, instance=True, spec_set=True)
    monkeypatch.setattr(admission, "sel", lambda: sink)
    monkeypatch.setattr(execution, "sel", lambda: sink)
    return sink


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from kiro_crew.apps.signed_content import sign_app_dir

    root = tmp_path / "apps" / "probe"
    root.mkdir(parents=True)
    (root / "backend.py").write_text("VALUE = 'published'\n", encoding="utf-8")
    (root / "app.json").write_text(
        json.dumps(
            {
                "name": "probe",
                "version": "1.0.0",
                "description": _DESCRIPTION,
                "setup": {"onDisable": _SCRIPT},
                "backend": {"entryPoint": "backend.py", "type": "python"},
            }
        ),
        encoding="utf-8",
    )
    sign_app_dir(root, signer="publisher", secret=_SECRET)
    policy_home = tmp_path / "policy"
    policy_home.mkdir()
    (policy_home / "app_admission.json").write_text(
        json.dumps({"require_signature": True, "trust_keys": {"publisher": _SECRET}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(admission, "config_dir", lambda: policy_home)
    monkeypatch.setattr(admission, "_installed_root", lambda name: root.parent / name)
    monkeypatch.setattr(execution, "is_builtin_app", lambda **_kwargs: False)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest",
        lambda name: AppManifest.from_json_file(root.parent / name / "app.json"),
    )
    return root


def _break_content(root: Path, failure: str, monkeypatch: pytest.MonkeyPatch) -> str | None:
    if failure == "missing":
        monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda name: None)
    elif failure == "signature":
        document = json.loads((root / "app.json").read_text(encoding="utf-8"))
        document["description"] = "changed-" + _DESCRIPTION
        (root / "app.json").write_text(json.dumps(document), encoding="utf-8")
    elif failure == "tree":
        (root / "backend.py").write_text("VALUE = 'changed'\n", encoding="utf-8")
    elif failure == "script":
        return "undeclared-" + _SCRIPT
    return None


def _assert_denial_audit(audit: Mock, executed: Path, reason: str) -> None:
    audit.log_api_access.assert_called_once_with(
        caller="app_admission",
        operation="installed_content_verification",
        outcome="denied",
        source="app",
        resources=f"app='probe' executed={str(executed)!r}",
        error=reason,
    )
    recorded = repr(audit.mock_calls)
    for private in (_SECRET, _SCRIPT, _DESCRIPTION, "fileDigests", "trust_keys"):
        assert private not in recorded


@pytest.mark.parametrize("wrapper", [False, True], ids=["direct", "wrapper"])
@pytest.mark.parametrize("failure", list(_REFUSALS))
def test_each_refusal_is_audited_once(
    installed: Path, audit: Mock, monkeypatch: pytest.MonkeyPatch, wrapper: bool, failure: str
) -> None:
    runs = _break_content(installed, failure, monkeypatch)
    executed = installed / "backend.py"
    reason = _REFUSALS[failure]
    if wrapper:
        assert admission.installed_content_refusal("probe", executed, runs=runs) == reason
    else:
        assert admission.verified_installed_manifest("probe", executed, runs=runs) == (reason, None)
    _assert_denial_audit(audit, executed, reason)


@pytest.mark.parametrize("wrapper", [False, True], ids=["direct", "wrapper"])
def test_the_callers_held_manifest_is_audited(installed: Path, audit: Mock, wrapper: bool) -> None:
    document = json.loads((installed / "app.json").read_text(encoding="utf-8"))
    document["description"] = "changed-" + _DESCRIPTION
    held = AppManifest.from_dict(document)
    reason = _REFUSALS["signature"]
    if wrapper:
        assert admission.installed_content_refusal("probe", installed, held) == reason
    else:
        assert admission.verified_installed_manifest("probe", installed, held) == (reason, None)
    _assert_denial_audit(audit, installed, reason)


@pytest.mark.parametrize("runs", [None, _SCRIPT])
def test_verified_content_emits_no_denial(installed: Path, audit: Mock, runs: str | None) -> None:
    held = AppManifest.from_json_file(installed / "app.json")
    assert admission.verified_installed_manifest("probe", installed, held, runs) == (None, held)
    assert admission.installed_content_refusal("probe", installed, held, runs) is None
    audit.log_api_access.assert_not_called()


@pytest.mark.parametrize("exemption", ["builtin", "outside", "no_policy"])
def test_exempt_content_is_not_read_or_audited(
    installed: Path, audit: Mock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exemption: str
) -> None:
    executed = installed
    if exemption == "builtin":
        monkeypatch.setattr(execution, "is_builtin_app", lambda **_kwargs: True)
    elif exemption == "outside":
        executed = tmp_path / "shipped" / "backend.py"
    else:
        monkeypatch.setattr(admission, "config_dir", lambda: tmp_path / "absent-policy")
        monkeypatch.setattr(
            admission, "_installed_root", Mock(side_effect=AssertionError("read installed root"))
        )
    unread = Mock(side_effect=AssertionError("read exempt app content"))
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", unread)
    monkeypatch.setattr(admission, "_signature_valid", unread)
    monkeypatch.setattr(admission, "signed_content_refusal", unread)
    assert admission.verified_installed_manifest("probe", executed) == (None, None)
    assert admission.installed_content_refusal("probe", executed) is None
    unread.assert_not_called()
    audit.log_api_access.assert_not_called()


@pytest.mark.parametrize("wrapper", [False, True], ids=["direct", "wrapper"])
@pytest.mark.parametrize("failure", list(_REFUSALS))
@pytest.mark.parametrize("audit_failure", ["initialization", "submission"])
def test_audit_failure_preserves_the_denial(
    installed: Path,
    audit: Mock,
    monkeypatch: pytest.MonkeyPatch,
    wrapper: bool,
    failure: str,
    audit_failure: str,
) -> None:
    runs = _break_content(installed, failure, monkeypatch)
    if audit_failure == "initialization":
        failed = Mock(side_effect=OSError("audit unavailable"))
        monkeypatch.setattr(admission, "sel", failed)
    else:
        failed = audit.log_api_access
        failed.side_effect = OSError("audit unavailable")
    reason = _REFUSALS[failure]
    if wrapper:
        assert admission.installed_content_refusal("probe", installed, runs=runs) == reason
    else:
        assert admission.verified_installed_manifest("probe", installed, runs=runs) == (
            reason,
            None,
        )
    failed.assert_called_once()


@pytest.mark.asyncio
async def test_on_disable_records_content_denial_after_execution_policy_allows(
    installed: Path, audit: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import lifecycle_scripts

    _break_content(installed, "tree", monkeypatch)
    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: installed.parent)
    monkeypatch.setattr(
        execution,
        "_execution_admission",
        lambda *args, **kwargs: (None, "provenance=trusted_grant"),
    )
    spawn = Mock(side_effect=AssertionError("spawned refused lifecycle script"))
    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", spawn)
    reason = _REFUSALS["tree"]
    assert await lifecycle_scripts.run_lifecycle_script("probe", _SCRIPT, action="onDisable") == {
        "output": reason,
        "failed": True,
        "denied": True,
    }
    assert audit.log_api_access.call_args_list == [
        call(
            caller="dashboard",
            operation="app_execution_admission",
            outcome="allowed",
            resources="action='onDisable' provenance=trusted_grant",
        ),
        call(
            caller="app_admission",
            operation="installed_content_verification",
            outcome="denied",
            source="app",
            resources=f"app='probe' executed={str(installed)!r}",
            error=reason,
        ),
    ]
    spawn.assert_not_called()
    assert _SCRIPT not in repr(audit.mock_calls)


@pytest.mark.parametrize(
    "surface",
    ["backend", "hook", "agent_register", "skill_register", "cron_register", "mcp_register"],
)
def test_executable_surfaces_reach_the_shared_denial_audit(
    installed: Path, audit: Mock, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    _break_content(installed, "tree", monkeypatch)
    reason = _REFUSALS["tree"]
    held = AppManifest.from_json_file(installed / "app.json")
    executed = installed
    if surface == "backend":
        from kiro_crew.apps import backend

        monkeypatch.setattr(backend, "app_dir", lambda name: installed)
        monkeypatch.setattr(backend, "app_execution_denied", lambda *args, **kwargs: None)
        monkeypatch.setattr(backend, "is_builtin_app", lambda **kwargs: False)
        spawn = Mock(side_effect=AssertionError("prepared refused backend spawn"))
        monkeypatch.setattr(backend, "wrap_argv", spawn)
        assert backend._start_app_backend_body("probe", held) is None
        spawn.assert_not_called()
        executed = installed / "backend.py"
    elif surface == "hook":
        from kiro_crew.apps import module_loader

        cleanup = Mock(side_effect=AssertionError("prepared refused hook source"))
        monkeypatch.setattr(module_loader._signed_imports, "clear_app_bytecode", cleanup)
        executed = installed / "backend.py"
        assert module_loader._prepare_signed_hook("probe", installed, executed) == (reason, None)
        cleanup.assert_not_called()
    else:
        from kiro_crew.apps import bridges_sources

        monkeypatch.setattr(bridges_sources, "app_execution_denied", lambda *args, **kwargs: None)
        assert (
            bridges_sources._registration_denied(
                "probe", action=surface, app_root=installed, manifest=held
            )
            == reason
        )
    _assert_denial_audit(audit, executed, reason)

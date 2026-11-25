"""kiro-cli's user permission rule files are read-only in the sandbox while fenced.

``mcp.protect_kiro_permission_rules`` refuses the agent's file-edit tools on
``~/.kiro/settings/permissions.yaml`` and ``permissions.json``. This pins its OS half:
while the switch is on, a sandboxed shell cannot write them either. Off (the default)
changes nothing, so the agent may edit them as before.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox, sandbox_seals
from kiro_crew.sandbox_plan import (
    BACKEND_NAMESPACE,
    BACKEND_SEATBELT,
    SandboxRequest,
    plan_confinement,
)
from kiro_crew.security import paths as security_paths

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")


def _fence(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    rows = [".kiro/settings/permissions.yaml"] if on else []
    monkeypatch.setattr(security_paths, "_permission_rule_rows", lambda: list(rows))


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    settings = home / ".kiro" / "settings"
    settings.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    return settings


@_POSIX_ONLY
def test_off_by_default_adds_no_target_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    _fence(monkeypatch, on=False)
    assert sandbox._resolved_kiro_permission_rule_targets() == []
    assert sandbox._materialize_kiro_permission_rules() == []
    assert not (settings / "permissions.yaml").exists()


@_POSIX_ONLY
def test_off_leaves_the_plan_host_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    (settings / "permissions.yaml").write_text("rules: []\n", encoding="utf-8")
    _fence(monkeypatch, on=False)
    host = sandbox._live_plan_host(SandboxRequest(tier="standard"))
    assert str(settings / "permissions.yaml") not in host.kiro_agents_targets
    assert str(settings / "permissions.json") not in host.kiro_agents_targets


@_POSIX_ONLY
def test_on_targets_both_rule_files_in_every_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    _fence(monkeypatch, on=True)
    targets = sandbox._resolved_kiro_permission_rule_targets()
    for root in (tmp_path / "home" / ".kiro", tmp_path / "kiro"):
        assert str(root / "settings" / "permissions.yaml") in targets
        assert str(root / "settings" / "permissions.json") in targets
    assert len(targets) == len(set(targets))


@_POSIX_ONLY
def test_a_fence_read_that_raises_seals(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[str]:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(security_paths, "_permission_rule_rows", boom)
    assert sandbox_seals._kiro_permission_rules_fenced() is True


@_POSIX_ONLY
@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_on_the_rule_files_are_read_only_on_both_launchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    _fence(monkeypatch, on=True)
    request = SandboxRequest(tier="standard", backend=backend)
    host = sandbox._live_plan_host(request)
    plan = plan_confinement(request, host)
    assert str(settings / "permissions.yaml") in plan.readonly
    assert str(settings / "permissions.json") in plan.readonly
    assert str(settings) in host.readonly_ancestor_guards


@_POSIX_ONLY
def test_on_the_mcp_registry_and_agents_tree_stay_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    _fence(monkeypatch, on=True)
    host = sandbox._live_plan_host(SandboxRequest(tier="standard"))
    assert str(settings / "mcp.json") in host.kiro_agents_targets


@_POSIX_ONLY
def test_on_an_absent_pair_gets_a_no_rules_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    _fence(monkeypatch, on=True)
    target = settings / "permissions.yaml"
    assert sandbox._materialize_kiro_permission_rules() == [str(target)]
    assert target.read_bytes() == b"rules: []\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not (settings / "permissions.json").exists()
    # A second spawn finds the file and leaves it alone.
    assert sandbox._materialize_kiro_permission_rules() == []


@_POSIX_ONLY
@pytest.mark.parametrize("existing", ["permissions.yaml", "permissions.json"])
def test_on_an_existing_rule_file_is_never_touched_or_shadowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: str
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    body = "rules:\n  - effect: allow\n    capability: mcp\n"
    (settings / existing).write_text(body, encoding="utf-8")
    _fence(monkeypatch, on=True)
    assert sandbox._materialize_kiro_permission_rules() == []
    assert (settings / existing).read_text(encoding="utf-8") == body
    other = {"permissions.yaml", "permissions.json"} - {existing}
    assert not (settings / other.pop()).exists()


@_POSIX_ONLY
def test_on_a_link_at_a_rule_name_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    (settings / "permissions.yaml").symlink_to(tmp_path / "elsewhere.yaml")
    _fence(monkeypatch, on=True)
    assert sandbox._materialize_kiro_permission_rules() == []
    assert not (tmp_path / "elsewhere.yaml").exists()


@_POSIX_ONLY
def test_on_without_a_settings_folder_nothing_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".kiro").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    _fence(monkeypatch, on=True)
    assert sandbox._materialize_kiro_permission_rules() == []
    assert not (home / ".kiro" / "settings").exists()


@_POSIX_ONLY
def test_a_publish_failure_never_fails_the_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(tmp_path, monkeypatch)
    _fence(monkeypatch, on=True)
    monkeypatch.setattr(sandbox_seals, "_publish_empty_ceiling", lambda *a, **k: False)
    assert sandbox._materialize_kiro_permission_rules() == []
    assert os.path.isdir(tmp_path / "home" / ".kiro" / "settings")

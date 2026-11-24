"""kiro-cli's user MCP registry (``settings/mcp.json``) is read-only in every sandbox.

The file tools were already refused on it (``security.paths_lists``); this pins the OS
half, so a sandboxed shell cannot write it either. Only the person changes it, through
the MCP page or a ``request_server`` card. The seal never fails a spawn: a registry that
cannot be created is left unsealed and logged.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox, sandbox_seatbelt
from kiro_crew.sandbox_plan import (
    BACKEND_NAMESPACE,
    BACKEND_SEATBELT,
    PlanHost,
    SandboxRequest,
    plan_confinement,
)

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")

HOME = "/srv/u"
REGISTRY = f"{HOME}/.kiro/settings/mcp.json"


def _host(**overrides: object) -> PlanHost:
    fields: dict[str, object] = {
        "home": HOME,
        "cwd": "/work",
        "tier_dirs": (".aws",),
        "crew_readonly_targets": (".kiro/crew/config.json",),
        "kiro_agents_targets": (f"{HOME}/.kiro/agents", REGISTRY),
        "uid": 1000,
        "gid": 1000,
    }
    fields.update(overrides)
    return PlanHost(**fields)  # type: ignore[arg-type]


@_POSIX_ONLY
def test_both_spellings_of_the_registry_are_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    targets = sandbox._resolved_kiro_mcp_registry_targets()
    assert str(tmp_path / "home" / ".kiro" / "settings" / "mcp.json") in targets
    assert str(tmp_path / "kiro" / "settings" / "mcp.json") in targets


@_POSIX_ONLY
@pytest.mark.parametrize("check", ["targets", "ancestor_guards"])
def test_a_symlinked_kiro_home_keeps_the_registry_and_link_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check: str
) -> None:
    real_home = tmp_path / "kiro-real"
    real_home.mkdir()
    linked_home = tmp_path / "kiro-link"
    linked_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIRO_HOME", str(linked_home))
    monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])

    if check == "targets":
        targets = sandbox._resolved_kiro_mcp_registry_targets()
        assert str(linked_home / "settings" / "mcp.json") in targets
        assert str(real_home.resolve() / "settings" / "mcp.json") in targets
        assert len(targets) == len(set(targets))
    else:
        host = sandbox._live_plan_host(SandboxRequest(tier="standard", backend=BACKEND_SEATBELT))
        assert str(linked_home) in host.readonly_ancestor_guards


@_POSIX_ONLY
@pytest.mark.parametrize("override", ["/", "/usr"])
def test_an_invalid_kiro_home_override_adds_no_registry_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIRO_HOME", override)
    assert sandbox._resolved_kiro_mcp_registry_targets() == [
        str(tmp_path / "home" / ".kiro" / "settings" / "mcp.json")
    ]


@_POSIX_ONLY
def test_the_live_adapter_seals_the_registry_beside_the_agents_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [f"{HOME}/.kiro/agents"])
    monkeypatch.setattr(sandbox, "_resolved_kiro_mcp_registry_targets", lambda: [REGISTRY])
    host = sandbox._live_plan_host(SandboxRequest(tier="standard"))
    assert host.kiro_agents_targets == (f"{HOME}/.kiro/agents", REGISTRY)


@_POSIX_ONLY
@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_the_registry_is_read_only_on_both_launchers(backend: str) -> None:
    plan = plan_confinement(SandboxRequest(tier="standard", backend=backend), _host())
    assert REGISTRY in plan.readonly


@_POSIX_ONLY
def test_the_seatbelt_profile_denies_writing_the_registry_even_when_absent() -> None:
    plan = plan_confinement(SandboxRequest(tier="standard", backend=BACKEND_SEATBELT), _host())
    profile = sandbox_seatbelt.render_seatbelt_profile(plan)
    assert f'(deny file-write* (literal "{REGISTRY}"))' in profile


@_POSIX_ONLY
def test_the_delegated_workspace_guard_does_not_name_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A workspace that merely holds the file (a home directory) must keep working on the
    # delegated platforms; only the agents tree is on that guard.
    monkeypatch.setattr(sandbox, "_resolved_kiro_mcp_registry_targets", lambda: [REGISTRY])
    assert REGISTRY not in sandbox._resolved_kiro_agents_targets()


def _registry_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".kiro").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    return home / ".kiro" / "settings" / "mcp.json"


@_POSIX_ONLY
@pytest.mark.parametrize("check", ["targets", "seatbelt"])
def test_a_symlinked_settings_directory_keeps_both_registry_paths_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check: str
) -> None:
    registry = _registry_home(tmp_path, monkeypatch)
    settings = tmp_path / "dotfiles"
    settings.mkdir()
    canonical = settings / "mcp.json"
    canonical.write_text('{"mcpServers": {}}', encoding="utf-8")
    registry.parent.symlink_to(settings, target_is_directory=True)
    assert not registry.is_symlink()
    monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])

    if check == "targets":
        targets = sandbox._resolved_kiro_mcp_registry_targets()
        assert str(registry) in targets
        assert str(canonical.resolve()) in targets
        assert targets.index(str(registry)) < targets.index(str(canonical.resolve()))
        assert len(targets) == len(set(targets))
    else:
        request = SandboxRequest(tier="standard", backend=BACKEND_SEATBELT)
        host = sandbox._live_plan_host(request)
        plan = plan_confinement(request, host)
        assert str(registry) in plan.readonly
        assert str(canonical.resolve()) in plan.readonly
        assert str(registry.parent) in host.readonly_ancestor_guards


@_POSIX_ONLY
def test_an_absent_registry_is_created_empty_so_the_linux_seal_has_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry_home(tmp_path, monkeypatch)
    assert sandbox._materialize_kiro_mcp_registry() == [str(registry)]
    assert json.loads(registry.read_text(encoding="utf-8")) == {"mcpServers": {}}
    assert registry.stat().st_mode & 0o777 == 0o600
    assert registry.parent.stat().st_mode & 0o777 == 0o700


@_POSIX_ONLY
def test_an_existing_registry_and_a_link_are_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry_home(tmp_path, monkeypatch)
    registry.parent.mkdir()
    registry.write_text('{"mcpServers": {"x": {"command": "x"}}}', encoding="utf-8")
    assert sandbox._materialize_kiro_mcp_registry() == []
    assert "x" in registry.read_text(encoding="utf-8")

    registry.unlink()
    elsewhere = tmp_path / "dotfiles-mcp.json"
    registry.symlink_to(elsewhere)
    assert sandbox._materialize_kiro_mcp_registry() == [str(elsewhere)]
    assert registry.is_symlink()
    assert registry.readlink() == elsewhere
    assert json.loads(elsewhere.read_text(encoding="utf-8")) == {"mcpServers": {}}
    assert elsewhere.stat().st_mode & 0o777 == 0o600
    elsewhere.write_text('{"mcpServers": {"x": {"command": "x"}}}', encoding="utf-8")
    assert sandbox._materialize_kiro_mcp_registry() == []
    assert "x" in elsewhere.read_text(encoding="utf-8")


@_POSIX_ONLY
@pytest.mark.parametrize("relative", [False, True])
def test_a_registry_symlink_referent_is_a_separate_seal_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    registry = _registry_home(tmp_path, monkeypatch)
    registry.parent.mkdir()
    elsewhere = tmp_path / "dotfiles-mcp.json"
    registry.symlink_to(os.path.relpath(elsewhere, registry.parent) if relative else elsewhere)

    targets = sandbox._resolved_kiro_mcp_registry_targets()
    assert str(registry) in targets
    assert str(elsewhere) in targets
    assert len(targets) == len(set(targets))
    assert not elsewhere.exists()
    monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])
    host = sandbox._live_plan_host(SandboxRequest(tier="standard", backend=BACKEND_NAMESPACE))
    plan = plan_confinement(SandboxRequest(tier="standard", backend=BACKEND_NAMESPACE), host)
    assert str(elsewhere) in plan.readonly
    assert str(elsewhere.parent) in host.readonly_ancestor_guards


@_POSIX_ONLY
@pytest.mark.parametrize("parent_name", ["missing", "missing/nested"])
def test_a_registry_symlink_with_a_missing_parent_warns_without_creating_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    parent_name: str,
) -> None:
    registry = _registry_home(tmp_path, monkeypatch)
    registry.parent.mkdir()
    elsewhere = tmp_path / parent_name / "mcp.json"
    registry.symlink_to(elsewhere)

    with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
        assert sandbox._materialize_kiro_mcp_registry() == []
    assert registry.is_symlink()
    assert registry.readlink() == elsewhere
    assert not (tmp_path / "missing").exists()
    assert str(registry) in caplog.text
    assert "left unsealed for this spawn" in caplog.text


@_POSIX_ONLY
def test_a_registry_symlink_publish_failure_warns_without_failing_the_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    registry = _registry_home(tmp_path, monkeypatch)
    registry.parent.mkdir()
    elsewhere = tmp_path / "dotfiles-mcp.json"
    registry.symlink_to(elsewhere)

    def refuse(*_a: object, **_k: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "link", refuse)
    with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
        assert sandbox._materialize_kiro_mcp_registry() == []
    assert registry.is_symlink()
    assert registry.readlink() == elsewhere
    assert not elsewhere.exists()
    assert str(registry) in caplog.text
    assert "left unsealed for this spawn" in caplog.text


@_POSIX_ONLY
def test_no_kiro_home_means_nothing_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "bare"))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    (tmp_path / "bare").mkdir()
    assert sandbox._materialize_kiro_mcp_registry() == []
    assert not (tmp_path / "bare" / ".kiro").exists()


@_POSIX_ONLY
def test_a_registry_that_cannot_be_created_never_fails_the_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    registry = _registry_home(tmp_path, monkeypatch)

    def refuse(*_a: object, **_k: object) -> int:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "open", refuse)
    with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
        assert sandbox._materialize_kiro_mcp_registry() == []
    assert not registry.exists()
    assert "could not create" in caplog.text


@_POSIX_ONLY
def test_a_failed_write_leaves_no_registry_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A full disk mid-write must leave the name absent, not an empty file that every
    # later spawn skips and every reader calls corrupt.
    registry = _registry_home(tmp_path, monkeypatch)

    def full(*_a: object, **_k: object) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", full)
    with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
        assert sandbox._materialize_kiro_mcp_registry() == []
    assert not os.path.lexists(registry)
    assert os.listdir(registry.parent) == []
    assert "could not create" in caplog.text

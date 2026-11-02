"""Descriptor-pinned workspace cli.json reads and writes for native skill projection."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import native_skill_projection_helpers
import pytest

from conftest import requires_symlinks
from kiro_crew import atomic_write as atomic_write_module
from kiro_crew.acp import skill_projection as projection
from kiro_crew.workspace_cli_settings import CLI_JSON_REFUSED

native_tree = native_skill_projection_helpers.native_tree

requires_pinned_replace = pytest.mark.skipif(
    not atomic_write_module.pinned_parent_replace_supported(),
    reason="descriptor-relative replacement is unavailable",
)
_OUTSIDE_SENTINEL = b'{"outside": "unchanged"}'


def _outside_cli_json(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "cli.json"
    target.write_bytes(_OUTSIDE_SENTINEL)
    return target


def _workspace_cli_json(project: Path) -> Path:
    cli_json = project / ".kiro" / "settings" / "cli.json"
    cli_json.parent.mkdir(parents=True, exist_ok=True)
    return cli_json


def _hardlinked_cli_json(project: Path, tmp_path: Path) -> tuple[Path, Path]:
    """Plant a workspace cli.json that is a hardlink to a file outside the work dir."""
    target = _outside_cli_json(tmp_path)
    cli_json = _workspace_cli_json(project)
    os.link(target, cli_json)
    return cli_json, target


def _symlinked_cli_json(project: Path, tmp_path: Path) -> tuple[Path, Path]:
    """Plant a workspace cli.json that is a symlink to a file outside the work dir."""
    target = _outside_cli_json(tmp_path)
    cli_json = _workspace_cli_json(project)
    cli_json.symlink_to(target)
    return cli_json, target


refused_cli_json = pytest.mark.parametrize(
    ("plant", "symlinked"),
    [
        (_hardlinked_cli_json, False),
        pytest.param(_symlinked_cli_json, True, marks=requires_symlinks),
    ],
    ids=["hardlink", "outside-symlink"],
)


def _replace_settings_with_outside_link(project: Path, tmp_path: Path) -> tuple[Path, Path]:
    settings = project / ".kiro" / "settings"
    held_settings = project / ".kiro" / "settings-held"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cli.json").write_bytes(_OUTSIDE_SENTINEL)
    settings.rename(held_settings)
    settings.symlink_to(outside, target_is_directory=True)
    return held_settings, outside


@requires_symlinks
@requires_pinned_replace
def test_projection_refuses_settings_folder_swap_before_publish(
    native_tree, tmp_path, monkeypatch, caplog
):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    cli_json = project / ".kiro" / "settings" / "cli.json"
    cli_json.parent.mkdir(parents=True)
    original = b'{"operator.setting": true}'
    cli_json.write_bytes(original)
    real_carry = projection.carry_effort_ownership
    swapped: list[tuple[Path, Path]] = []

    def swap_before_publish(document, file_mtime):
        if not swapped:
            swapped.append(_replace_settings_with_outside_link(project, tmp_path))
        return real_carry(document, file_mtime)

    monkeypatch.setattr(projection, "carry_effort_ownership", swap_before_publish)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.skill_projection"):
        prepared = projection.prepare_native_skill_projection(project)

    assert prepared is None
    assert swapped
    held_settings, outside = swapped[0]
    assert (outside / "cli.json").read_bytes() == _OUTSIDE_SENTINEL
    assert (held_settings / "cli.json").read_bytes() == original
    assert "workspace settings or lease lock unavailable" in caplog.text


@requires_symlinks
@requires_pinned_replace
def test_rollback_refuses_settings_folder_swap_before_restore(
    native_tree, tmp_path, monkeypatch, caplog
):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    cli_json = project / ".kiro" / "settings" / "cli.json"
    original = cli_json.read_bytes()
    real_carry = projection.carry_effort_ownership
    swapped: list[tuple[Path, Path]] = []

    def swap_before_restore(document, file_mtime):
        if not swapped:
            swapped.append(_replace_settings_with_outside_link(project, tmp_path))
        return real_carry(document, file_mtime)

    monkeypatch.setattr(projection, "carry_effort_ownership", swap_before_restore)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.skill_projection"):
        assert projection.prepare_native_skill_projection(project, enabled=False) is None

    assert swapped
    held_settings, outside = swapped[0]
    assert (outside / "cli.json").read_bytes() == _OUTSIDE_SENTINEL
    assert (held_settings / "cli.json").read_bytes() == original
    assert "workspace settings unavailable during rollback" in caplog.text


@requires_pinned_replace
@refused_cli_json
def test_refused_cli_json_skips_the_projection(native_tree, tmp_path, caplog, plant, symlinked):
    """A cli.json the pinned read refuses costs the spawn its projection, not the spawn."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    cli_json, target = plant(project, tmp_path)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.skill_projection"):
        assert projection.prepare_native_skill_projection(project, enabled=True) is None

    assert CLI_JSON_REFUSED in caplog.text
    assert not list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert cli_json.read_bytes() == _OUTSIDE_SENTINEL
    assert target.read_bytes() == _OUTSIDE_SENTINEL
    assert cli_json.is_symlink() is symlinked


@requires_pinned_replace
@refused_cli_json
def test_refused_cli_json_skips_the_rollback(native_tree, tmp_path, caplog, plant, symlinked):
    """The same refusal on the rollback path leaves the file to whoever owns it."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    cli_json, target = plant(project, tmp_path)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.skill_projection"):
        assert projection.prepare_native_skill_projection(project, enabled=False) is None

    assert CLI_JSON_REFUSED in caplog.text
    assert cli_json.read_bytes() == _OUTSIDE_SENTINEL
    assert target.read_bytes() == _OUTSIDE_SENTINEL
    assert cli_json.is_symlink() is symlinked


@pytest.mark.parametrize("enabled", [True, False], ids=["projection", "rollback"])
@refused_cli_json
def test_by_name_floor_refuses_linked_cli_json(
    native_tree, tmp_path, monkeypatch, caplog, plant, symlinked, enabled
):
    """The by-name floor refuses the same linked workspace files as the pinned path."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    cli_json, target = plant(project, tmp_path)
    original_cli = os.lstat(cli_json)
    original_target = os.stat(target)
    monkeypatch.setattr(atomic_write_module, "pinned_parent_replace_supported", lambda: False)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.skill_projection"):
        assert projection.prepare_native_skill_projection(project, enabled=enabled) is None

    assert CLI_JSON_REFUSED in caplog.text
    assert not list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert cli_json.read_bytes() == _OUTSIDE_SENTINEL
    assert target.read_bytes() == _OUTSIDE_SENTINEL
    current_cli = os.lstat(cli_json)
    current_target = os.stat(target)
    assert (current_cli.st_dev, current_cli.st_ino, current_cli.st_nlink) == (
        original_cli.st_dev,
        original_cli.st_ino,
        original_cli.st_nlink,
    )
    assert (current_target.st_dev, current_target.st_ino, current_target.st_nlink) == (
        original_target.st_dev,
        original_target.st_ino,
        original_target.st_nlink,
    )
    assert cli_json.is_symlink() is symlinked
    if not symlinked:
        assert os.path.samefile(cli_json, target)


@requires_pinned_replace
def test_locked_cli_json_that_keeps_changing_skips_projection(native_tree, monkeypatch, caplog):
    """An exhausted locked metadata race skips projection instead of escaping."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    cli_json = _workspace_cli_json(project)
    original = b'{"operator.setting": true}'
    cli_json.write_bytes(original)
    real_reader = projection.read_pinned_cli_json
    calls: list[Path] = []

    def changed_reader(settings_fd, path, *, max_bytes):
        result = real_reader(settings_fd, path, max_bytes=max_bytes)
        assert result is not None
        raw, before_read, after_read = result
        calls.append(path)
        changed_after = SimpleNamespace(
            st_dev=after_read.st_dev,
            st_ino=after_read.st_ino,
            st_size=after_read.st_size,
            st_mtime=after_read.st_mtime,
            st_mtime_ns=after_read.st_mtime_ns + 1,
            st_ctime_ns=after_read.st_ctime_ns,
        )
        return raw, before_read, changed_after

    monkeypatch.setattr(projection, "read_pinned_cli_json", changed_reader)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.skill_projection"):
        assert projection.prepare_native_skill_projection(project, enabled=True) is None

    assert calls == [cli_json, cli_json]
    assert "changed while it was read" in caplog.text
    assert not list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert cli_json.read_bytes() == original

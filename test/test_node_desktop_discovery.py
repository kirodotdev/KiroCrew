"""Desktop Node discovery and its CLI override."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from kiro_crew import env as env_mod
from kiro_crew import platform_compat


def _fake_node_bin(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    names = (
        ("node.exe", "npm.cmd", "npx.cmd") if platform_compat.IS_WINDOWS else ("node", "npm", "npx")
    )
    for name in names:
        executable = path / name
        body = "@echo off\n" if name.endswith(".cmd") else "#!/bin/sh\nexit 0\n"
        executable.write_text(body, encoding="utf-8")
        executable.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def _clear_node_caches():
    env_mod.node_bin_dirs.cache_clear()
    env_mod._macos_node_bin_dirs.cache_clear()
    yield
    env_mod.node_bin_dirs.cache_clear()
    env_mod._macos_node_bin_dirs.cache_clear()


@pytest.fixture
def isolated_node_resolver(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.delenv("KIROCREW_NODE_BIN_DIR", raising=False)
    monkeypatch.setattr(env_mod, "_NODE_MANAGER_GLOBS", (), raising=True)
    monkeypatch.setattr(env_mod, "_NODE_MANAGER_DIRS", (), raising=True)
    monkeypatch.setattr(env_mod, "_marker_node_bin_dir", lambda: None, raising=True)
    return home


def test_macos_path_helper_registry_is_a_node_fallback(
    isolated_node_resolver, tmp_path, monkeypatch
):
    node_dir = _fake_node_bin(tmp_path / "package-manager" / "bin")
    paths_dir = tmp_path / "etc" / "paths.d"
    paths_dir.mkdir(parents=True)
    (paths_dir / "node-provider").write_text(f"{node_dir}\n", encoding="utf-8")
    monkeypatch.setattr(env_mod.platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(env_mod, "_MACOS_PATHS_FILE", tmp_path / "etc" / "paths")
    monkeypatch.setattr(env_mod, "_MACOS_PATHS_DIR", paths_dir)
    env_mod._macos_node_bin_dirs.cache_clear()

    found = env_mod.find_node_tool("npx", os.pathsep.join(("/usr/bin", "/bin")))

    assert found is not None
    assert Path(found).parent == node_dir


def test_inherited_path_wins_over_macos_path_helper_fallback(
    isolated_node_resolver, tmp_path, monkeypatch
):
    inherited = _fake_node_bin(tmp_path / "inherited" / "bin")
    fallback = _fake_node_bin(tmp_path / "fallback" / "bin")
    paths_file = tmp_path / "etc" / "paths"
    paths_file.parent.mkdir(parents=True)
    paths_file.write_text(f"{fallback}\n", encoding="utf-8")
    monkeypatch.setattr(env_mod.platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(env_mod, "_MACOS_PATHS_FILE", paths_file)
    monkeypatch.setattr(env_mod, "_MACOS_PATHS_DIR", tmp_path / "etc" / "paths.d")
    env_mod._macos_node_bin_dirs.cache_clear()

    found = env_mod.find_node_tool("node", str(inherited))

    assert found is not None
    assert Path(found).parent == inherited


def test_macos_path_helper_registry_ignores_invalid_entries(
    isolated_node_resolver, tmp_path, monkeypatch
):
    paths_file = tmp_path / "etc" / "paths"
    paths_file.parent.mkdir(parents=True)
    paths_file.write_text(f"{tmp_path / 'empty'}\nrelative/bin\n", encoding="utf-8")
    monkeypatch.setattr(env_mod.platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(env_mod, "_MACOS_PATHS_FILE", paths_file)
    monkeypatch.setattr(env_mod, "_MACOS_PATHS_DIR", tmp_path / "etc" / "paths.d")
    env_mod._macos_node_bin_dirs.cache_clear()

    assert env_mod._macos_node_bin_dirs() == ()


def test_set_node_bin_dir_validates_and_writes_marker(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    node_dir = _fake_node_bin(tmp_path / "custom" / "bin")
    monkeypatch.setattr(env_mod, "data_home", lambda: data_dir, raising=True)

    saved = env_mod.set_node_bin_dir(str(node_dir))

    assert saved == str(node_dir.resolve())
    assert (data_dir / "node-bin-dir").read_text(encoding="utf-8") == f"{saved}\n"


def test_set_node_bin_dir_rejects_directory_without_node(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    empty_dir = tmp_path / "empty" / "bin"
    empty_dir.mkdir(parents=True)
    monkeypatch.setattr(env_mod, "data_home", lambda: data_dir, raising=True)

    with pytest.raises(ValueError, match="Node is not executable"):
        env_mod.set_node_bin_dir(str(empty_dir))

    assert not (data_dir / "node-bin-dir").exists()


def test_clear_node_bin_dir_removes_marker(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    marker = data_dir / "node-bin-dir"
    marker.write_text("/opt/node/bin\n", encoding="utf-8")
    monkeypatch.setattr(env_mod, "data_home", lambda: data_dir, raising=True)

    assert env_mod.clear_node_bin_dir() is True
    assert env_mod.clear_node_bin_dir() is False
    assert not marker.exists()


def test_node_path_parser_dispatches(tmp_path, monkeypatch):
    from kiro_crew import cli

    dispatched = []
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "boot_platform", lambda _config: None)
    monkeypatch.setattr(cli, "_node_cmd", lambda args: dispatched.append(args) or 0)
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "node", "path", "/opt/node/bin"])

    cli.main()

    assert len(dispatched) == 1
    assert dispatched[0].node_action == "path"
    assert dispatched[0].bin_dir == "/opt/node/bin"
    assert dispatched[0].clear is False


def test_node_path_show_prints_resolved_directory(tmp_path, monkeypatch, capsys):
    from kiro_crew import cli

    node = tmp_path / "node-bin" / "node"
    monkeypatch.setattr(env_mod, "find_node_tool", lambda _name: str(node))

    rc = cli._node_cmd(argparse.Namespace(bin_dir=None, clear=False))

    assert rc == 0
    assert capsys.readouterr().out.strip() == str(node.parent)


def test_node_path_rejects_bin_dir_with_clear(capsys):
    from kiro_crew import cli

    rc = cli._node_cmd(argparse.Namespace(bin_dir="/opt/node/bin", clear=True))

    assert rc == 2
    assert "cannot be used with --clear" in capsys.readouterr().err

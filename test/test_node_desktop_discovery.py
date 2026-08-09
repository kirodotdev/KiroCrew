"""Desktop Node discovery and runtime validation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiro_crew import env as env_mod
from kiro_crew import platform_compat


def _fake_node_bin(path: Path, version: str = "20.19.0") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    names = (
        ("node.exe", "npm.cmd", "npx.cmd") if platform_compat.IS_WINDOWS else ("node", "npm", "npx")
    )
    for name in names:
        executable = path / name
        body = (
            f"@echo off\r\necho v{version}\r\n"
            if name.endswith(".cmd")
            else f"#!/bin/sh\necho v{version}\n"
        )
        executable.write_text(body, encoding="utf-8")
        executable.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def _clear_node_caches():
    env_mod.node_bin_dirs.cache_clear()
    yield
    env_mod.node_bin_dirs.cache_clear()


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
    monkeypatch.setattr(env_mod, "_node_version_supported", lambda _node: True)
    return home


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("v17.9.1", False),
        ("v18.0.0", True),
        ("v20.18.0", True),
        ("v21.7.3", True),
        ("v23.0.0", True),
    ],
)
def test_node_version_supported_matches_playwright_floor(version, supported, monkeypatch):
    monkeypatch.setattr(
        env_mod.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["node", "--version"], returncode=0, stdout=version
        ),
    )

    assert env_mod._node_version_supported("/node") is supported


def test_ensure_node_defers_marker_refresh_until_cache_reset(
    isolated_node_resolver, tmp_path, monkeypatch
):
    marker_value = {"bin_dir": None}
    monkeypatch.setattr(
        env_mod, "_marker_node_bin_dir", lambda: marker_value["bin_dir"], raising=True
    )
    monkeypatch.setattr(env_mod, "_ensure_node_script", lambda: None, raising=True)
    monkeypatch.setattr(env_mod.platform_compat, "IS_MACOS", False)
    monkeypatch.setenv("PATH", "")

    assert env_mod.ensure_node() is None

    node_dir = _fake_node_bin(tmp_path / "external-marker-writer" / "bin")
    marker_value["bin_dir"] = str(node_dir)

    assert env_mod.ensure_node() is None

    env_mod.node_bin_dirs.cache_clear()
    node = env_mod.ensure_node()

    assert node is not None
    assert Path(node).parent == node_dir


def test_custom_data_home_does_not_run_undiscoverable_bootstrap(
    isolated_node_resolver, tmp_path, monkeypatch
):
    script = tmp_path / "ensure-node.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "custom-home"))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(env_mod, "_ensure_node_script", lambda: script, raising=True)
    monkeypatch.setattr(
        env_mod.subprocess,
        "run",
        lambda argv, **_kwargs: calls.append(argv),
    )

    assert env_mod.ensure_node() is None
    assert calls == []

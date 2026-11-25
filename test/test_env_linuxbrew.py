"""Linuxbrew's bin dirs must be on the MCP binary search path.

``brew shellenv`` puts Linuxbrew on a terminal's ``PATH`` only, so a gateway
started as a service inherits a ``PATH`` without it. An MCP server declared by
the bare name of a Linuxbrew-installed launcher then resolved nowhere, and the
agent config dropped it.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys

import pytest

from kiro_crew import env as env_mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty HOME and a service-style PATH, with nothing contributed."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    monkeypatch.delenv("MISE_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    monkeypatch.setattr(env_mod, "_PINNED_GATEWAY_SPEC_PATH", None)
    monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
    monkeypatch.setattr(env_mod, "_config_path_dirs", ())
    env_mod._node_all_bin_dirs.cache_clear()
    yield home
    env_mod._node_all_bin_dirs.cache_clear()


def test_a_per_user_linuxbrew_launcher_resolves_by_bare_name(home) -> None:
    bin_dir = home / ".linuxbrew" / "bin"
    bin_dir.mkdir(parents=True)
    name = "kc-linuxbrew-test-mcp"
    launcher = bin_dir / (f"{name}.cmd" if sys.platform == "win32" else name)
    launcher.write_text("@echo off\n" if sys.platform == "win32" else "#!/bin/sh\n")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)

    found = shutil.which(name, path=env_mod.mcp_search_path(""))

    assert found is not None
    assert os.path.normcase(os.path.normpath(found)) == os.path.normcase(str(launcher))


@pytest.mark.skipif(sys.platform == "win32", reason="Linuxbrew's system prefix is a POSIX path")
def test_the_system_linuxbrew_prefix_is_searched(home) -> None:
    # Creating /home/linuxbrew needs root, so check the composed path instead.
    dirs = env_mod.mcp_search_path("").split(os.pathsep)

    assert "/home/linuxbrew/.linuxbrew/bin" in dirs
    assert dirs.index("/home/linuxbrew/.linuxbrew/bin") < dirs.index("/usr/bin")


def test_linuxbrew_outranks_n_like_the_other_homebrew_dirs() -> None:
    extra = env_mod._EXTRA_PATH_DIRS
    for brew_dir in ("/home/linuxbrew/.linuxbrew/bin", "{home}/.linuxbrew/bin"):
        for n_dir in ("{home}/n/bin", "{home}/.n/bin"):
            assert extra.index(brew_dir) < extra.index(n_dir)

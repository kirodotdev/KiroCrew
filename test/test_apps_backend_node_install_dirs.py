"""An app backend finds Node.js where the gateway's own PATH does not (#39108).

Opened from the macOS Dock, the gateway inherits launchd's
``/usr/bin:/bin:/usr/sbin:/sbin``. A Node.js from Homebrew, the nodejs.org
installer, Volta, fnm or asdf is in none of those, so ``_find_node_binary``
(nvm, then ``PATH``) found nothing and every node-backend app answered 502.
The MCP and build lookups already search those directories through
``env.augmented_path``; the backend lookup now falls back to the same list.
"""

from __future__ import annotations

import os
import stat

import pytest

from kiro_crew import env
from kiro_crew.apps import backend as bmod

_NAMES = (
    {"node": "node.exe", "npm": "npm.cmd"} if os.name == "nt" else {"node": "node", "npm": "npm"}
)


def _plant(directory, *names: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        tool = directory / _NAMES[name]
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return str(directory)


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """A gateway with no nvm and a PATH that holds no Node.js."""
    system = tmp_path / "launchd-path"
    system.mkdir()
    monkeypatch.setenv("PATH", str(system))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bmod, "_resolve_nvm_path", lambda _name: None)
    return tmp_path


def _found_in(found: str | None, directory: str) -> bool:
    return found is not None and os.path.normcase(os.path.dirname(found)) == os.path.normcase(
        directory
    )


def test_node_and_npm_are_found_in_an_install_directory_off_path(gateway, monkeypatch) -> None:
    homebrew = _plant(gateway / "opt" / "homebrew" / "bin", "node", "npm")
    monkeypatch.setattr(env, "augmented_path", lambda base_path="", **_k: homebrew)

    node = bmod._find_node_binary()
    npm = bmod._find_npm_binary()

    assert _found_in(node, homebrew), node
    assert _found_in(npm, homebrew), npm
    assert os.path.isabs(node) and os.path.isabs(npm)


def test_a_node_on_path_still_wins_over_the_install_directories(gateway, monkeypatch) -> None:
    on_path = _plant(gateway / "on-path", "node")
    monkeypatch.setenv("PATH", on_path)
    homebrew = _plant(gateway / "opt" / "homebrew" / "bin", "node")
    monkeypatch.setattr(env, "augmented_path", lambda base_path="", **_k: homebrew)

    assert _found_in(bmod._find_node_binary(), on_path)


def test_the_fallback_never_answers_with_the_working_directory(gateway, monkeypatch) -> None:
    """A relative entry would name the working directory, which on Windows is
    also where a bare-name lookup looks first; neither may supply the node."""
    _plant(gateway, "node")
    empty = gateway / "empty"
    empty.mkdir()
    monkeypatch.setattr(
        env, "augmented_path", lambda base_path="", **_k: os.pathsep.join([".", str(empty)])
    )

    assert bmod._find_node_binary() is None


def test_the_real_install_directory_list_reaches_a_volta_node(gateway, monkeypatch) -> None:
    """Through the real ``augmented_path``: a home-relative Node.js manager
    directory (Volta here) is searched without being on PATH."""
    home = gateway / "home"
    volta = _plant(home / ".volta" / "bin", "node")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("KIROCREW_NODE_BIN_DIR", raising=False)

    entries = [os.path.normcase(d) for d in env.augmented_path("").split(os.pathsep) if d]
    assert os.path.normcase(volta) in entries

    found = bmod._find_node_binary()

    # The host may hold a Node.js in a directory ranked ahead of Volta's (its own
    # Homebrew, say); either way the answer comes from the list, never PATH.
    assert found is not None
    parent = os.path.normcase(os.path.dirname(found))
    assert parent in entries[: entries.index(os.path.normcase(volta)) + 1]

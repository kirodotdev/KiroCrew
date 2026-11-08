"""An operator-chosen Node outranks nvm's newest on the MCP and agent search path.

Amazon Linux 2 ships glibc 2.26 and the official Node 18+ builds need 2.28, so
``nvm install 22`` leaves a ``node`` that does not load. An operator who unpacks
a glibc-2.17 build and points ``KIROCREW_NODE_BIN_DIR`` at it must get that Node
for an ``npx`` MCP server, whose ``#!/usr/bin/env node`` resolves against the
same PATH. With no override, the glibc-2.17 tree ``ensure-node.sh`` unpacks into
the data home must still outrank every nvm version dir.

The data home is the conftest's per-test ``KIROCREW_HOME`` pin; ``~`` is a
``tmp_path`` dir through a patched ``expanduser``, so no real version manager or
``npx`` on the runner can reach the verdict.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

import kiro_crew.env as env_mod
from kiro_crew.config.paths import peek_data_home
from kiro_crew.env import augmented_path, mcp_runtime_path, mcp_search_path

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the anchoring and the service-style PATH these pin are POSIX only",
)


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _node_bin(d: Path) -> Path:
    """*d* holding an executable ``node`` and ``npx``, named as this platform runs them."""
    d.mkdir(parents=True, exist_ok=True)
    win = sys.platform == "win32"
    for name in ("node.exe", "npx.cmd") if win else ("node", "npx"):
        f = d / name
        f.write_text("@echo off\n" if win else "#!/bin/sh\nexit 0\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return d


def _index(dirs: list[str], target: Path) -> int:
    return [_norm(d) for d in dirs].index(_norm(target))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake ``~`` holding nvm's v18 and v22 installs; nothing contributed to the MCP path."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(h) if p == "~" else p)
    for var in ("KIROCREW_NODE_BIN_DIR", "MISE_DATA_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(env_mod, "_config_path_dirs", ())
    monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
    nvm = h / ".nvm" / "versions" / "node"
    _node_bin(nvm / "v18.0.0" / "bin")
    _node_bin(nvm / "v22.0.0" / "bin")
    env_mod._node_all_bin_dirs.cache_clear()
    yield h
    env_mod._node_all_bin_dirs.cache_clear()


@pytest.fixture
def only_test_executables(tmp_path, monkeypatch):
    """Every executable check outside ``tmp_path`` and the data home reports absent.

    A runner's own ``/usr/local/bin/npx`` or ``/opt/homebrew/bin/npx`` then
    cannot answer a ``shutil.which`` these tests make.
    """
    roots = tuple(_norm(r) + os.sep for r in (tmp_path, peek_data_home()))
    real_access = os.access

    def _access(path, mode, *args, **kwargs):
        if not _norm(os.path.abspath(os.fspath(path))).startswith(roots):
            return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "access", _access)


def _nvm22(home: Path) -> Path:
    return home / ".nvm" / "versions" / "node" / "v22.0.0" / "bin"


def _service_style_base(home: Path) -> str:
    """The shape ``service_path()`` writes: the installer's dirs land behind ``/usr/bin``."""
    return os.pathsep.join([f"{home}/.local/bin", "/usr/local/bin", "/usr/bin", "/bin"])


@_POSIX_ONLY
@pytest.mark.usefixtures("only_test_executables")
class TestOverrideLeads:
    """``KIROCREW_NODE_BIN_DIR`` is the Node every ``npx`` resolution reaches first."""

    @pytest.fixture
    def override(self, home, monkeypatch):
        d = _node_bin(home / "node22" / "bin")
        monkeypatch.setenv("KIROCREW_NODE_BIN_DIR", str(d))
        return d

    def test_npx_resolves_to_the_override_not_nvms_newest(self, home, override):
        path = augmented_path(_service_style_base(home))
        dirs = path.split(os.pathsep)
        assert _index(dirs, override) < _index(dirs, _nvm22(home))
        found = shutil.which("npx", path=path)
        assert found is not None and _norm(found) == _norm(override / "npx")

    def test_every_mcp_composition_inherits_the_override(self, home, override, monkeypatch):
        base = _service_style_base(home)
        monkeypatch.setenv("PATH", base)
        for path in (mcp_search_path(""), mcp_runtime_path(base)):
            found = shutil.which("npx", path=path)
            assert found is not None and _norm(found) == _norm(override / "npx")

    def test_override_leads_an_nvm_dir_the_inherited_path_lists_first(self, home, override):
        """A login shell that sources ``nvm.sh`` lists nvm's bin ahead of ``/usr/bin``.

        That entry is an anchor and keeps its place, but the override is not on
        the inherited PATH at all, so it is prepended ahead of it.
        """
        base = os.pathsep.join([str(_nvm22(home)), _service_style_base(home)])
        dirs = augmented_path(base).split(os.pathsep)
        assert _index(dirs, override) < _index(dirs, _nvm22(home))
        found = shutil.which("npx", path=os.pathsep.join(dirs))
        assert found is not None and _norm(found) == _norm(override / "npx")

    def test_an_override_the_inherited_path_lists_behind_nvm_is_never_an_anchor(
        self, home, override
    ):
        """``nvm.sh`` sourced last: nvm's bin, then the override, then ``/usr/bin``.

        Kept as an anchor, the override would stay behind nvm, and every guess
        ranked after it, ``~/.local/bin`` first, would fall in behind both. It is
        prepended instead, once, and the guesses keep the rank they had without it.
        """
        base = os.pathsep.join([str(_nvm22(home)), str(override), "/usr/bin", "/bin"])
        dirs = augmented_path(base).split(os.pathsep)
        assert _index(dirs, override) < _index(dirs, _nvm22(home))
        assert _index(dirs, home / ".local" / "bin") < _index(dirs, _nvm22(home))
        assert [_norm(d) for d in dirs].count(_norm(override)) == 1
        found = shutil.which("npx", path=os.pathsep.join(dirs))
        assert found is not None and _norm(found) == _norm(override / "npx")

    def test_a_second_augmentation_keeps_this_install_ahead_of_the_override(
        self, home, override, monkeypatch
    ):
        """A script cron's child augments the PATH the gateway already augmented.

        The override may hold an npm-installed ``kirocrew`` of its own, so this
        install's dir must stay ahead of it on the second pass as on the first.
        """
        own = _node_bin(home / "own" / "bin")
        monkeypatch.setattr(env_mod, "_own_kirocrew_dirs", lambda: [str(own)])
        once = augmented_path(_service_style_base(home))
        twice = augmented_path(once).split(os.pathsep)
        assert _index(twice, own) < _index(twice, override) < _index(twice, _nvm22(home))
        assert [_norm(d) for d in twice].count(_norm(own)) == 1

    def test_a_pinned_home_gets_no_override(self, home, override):
        """Another account's caller has no claim to this process's override."""
        dirs = augmented_path(_service_style_base(home), home=str(home)).split(os.pathsep)
        assert _norm(override) not in {_norm(d) for d in dirs}


def test_an_unusable_override_changes_nothing(home, monkeypatch):
    """A relative value, a smuggled second dir, or a dir with no ``node`` is not a Node pin."""
    base = str(home / "sys" / "bin")
    want = augmented_path(base)
    empty = home / "empty" / "bin"
    empty.mkdir(parents=True)
    for value in ("node22/bin", os.pathsep.join([str(empty), str(_nvm22(home))]), str(empty)):
        monkeypatch.setenv("KIROCREW_NODE_BIN_DIR", value)
        assert augmented_path(base) == want, value


def test_glibc217_tree_ranks_ahead_of_every_nvm_version(home):
    """No override: ``ensure-node.sh``'s glibc-2.17 tree beats nvm's newest.

    It is an unsealed data-home dir, so it is a fallback at the manager dirs'
    rank and stays behind ``~/.local/bin``.
    """
    tree = _node_bin(peek_data_home() / "node-glibc217" / "bin")
    dirs = augmented_path(str(home / "sys" / "bin")).split(os.pathsep)
    nvm = home / ".nvm" / "versions" / "node"
    assert _index(dirs, tree) < _index(dirs, nvm / "v22.0.0" / "bin")
    assert _index(dirs, tree) < _index(dirs, nvm / "v18.0.0" / "bin")
    assert _index(dirs, home / ".local" / "bin") < _index(dirs, tree)
    pinned = augmented_path("", home=str(home)).split(os.pathsep)
    assert _norm(tree) not in {_norm(d) for d in pinned}


@_POSIX_ONLY
def test_neither_present_leaves_the_composition_unchanged(home):
    """Own dir, the fixed guesses, every version dir, then the interpreter: no new entry."""
    want = [
        *env_mod._own_kirocrew_dirs(),
        *env_mod.user_tool_bin_dirs(str(home)),
        *env_mod.node_all_bin_dirs(),
        *env_mod._interpreter_script_dirs(),
    ]
    assert augmented_path("").split(os.pathsep) == want
    nvm = home / ".nvm" / "versions" / "node"
    assert _index(want, nvm / "v22.0.0" / "bin") < _index(want, nvm / "v18.0.0" / "bin")

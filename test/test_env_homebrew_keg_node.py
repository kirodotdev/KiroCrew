"""Homebrew keg-only node bins must be on the MCP binary search path.

``brew install node@20`` is keg-only: it is never linked into
``/opt/homebrew/bin``, so a global npm MCP launcher installed under it lived in
``/opt/homebrew/opt/node@20/bin`` -- a directory no search tier covered, so a
server declared by bare name never launched.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import env as env_mod


@pytest.fixture
def keg_root(tmp_path, monkeypatch):
    root = tmp_path / "homebrew" / "opt"
    root.mkdir(parents=True)
    # An empty HOME, so no real manager install on this host leaks in.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    monkeypatch.delenv("MISE_DATA_DIR", raising=False)
    monkeypatch.setattr(env_mod, "_HOMEBREW_NODE_KEG_ROOT", str(root), raising=False)
    env_mod._node_all_bin_dirs.cache_clear()
    yield root
    env_mod._node_all_bin_dirs.cache_clear()


def test_keg_only_node_bin_is_included(keg_root):
    (keg_root / "node@20" / "bin").mkdir(parents=True)

    assert str(keg_root / "node@20" / "bin") in env_mod.node_all_bin_dirs()


def test_kegs_are_ordered_node_then_newest_major_and_deduped(keg_root):
    for keg in ("node@18", "node@22", "node"):
        (keg_root / keg / "bin").mkdir(parents=True)

    dirs = [d for d in env_mod.node_all_bin_dirs() if str(keg_root) in d]

    assert dirs == [str(keg_root / k / "bin") for k in ("node", "node@22", "node@18")]
    assert len(dirs) == len(set(dirs))


def test_absent_or_unrelated_kegs_are_not_included(keg_root):
    (keg_root / "node@20").mkdir()  # keg without a bin dir
    (keg_root / "nodenv" / "bin").mkdir(parents=True)  # not a node keg

    assert not [d for d in env_mod.node_all_bin_dirs() if str(keg_root) in d]


def test_keg_bin_reaches_the_augmented_path(keg_root):
    (keg_root / "node@20" / "bin").mkdir(parents=True)

    dirs = env_mod.augmented_path("/usr/bin").split(os.pathsep)

    assert str(keg_root / "node@20" / "bin") in dirs

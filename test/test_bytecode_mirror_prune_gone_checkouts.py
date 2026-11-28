"""Which sibling checkout mirrors the session-end bytecode prune retires.

The ``sys.pycache_prefix`` mirror is keyed on each source's absolute path, so every
worktree and every scratch clone gets its own copy. The prune drops a sibling's copy when
its source is gone, or when it is a checkout idle for two weeks, and never the chain the
run sits on. Expected mirror paths come from ``importlib.util.cache_from_source``, the
interpreter's own mapping, so a wrong mapping in the prune cannot pass here.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time

import pytest
from conftest_floor_configure import (
    _MIRROR_IDLE_SECONDS,
    _prune_bytecode_mirror_of_gone_checkouts,
)

_OLD = _MIRROR_IDLE_SECONDS + 3600


def _mirror_with_pyc(source):
    pyc = importlib.util.cache_from_source(os.path.join(str(source), "pkg", "m.py"))
    os.makedirs(os.path.dirname(pyc), exist_ok=True)
    with open(pyc, "wb") as handle:
        handle.write(b"\x00")
    return os.path.dirname(os.path.dirname(pyc))


def _age(mirror, seconds):
    stamp = time.time() - seconds
    for dirpath, _dirs, _files in os.walk(mirror):
        os.utime(dirpath, (stamp, stamp))


@pytest.fixture
def world(tmp_path, monkeypatch):
    prefix = tmp_path / "pycache"
    repo = tmp_path / "repo"
    worktrees = repo / ".worktrees"
    here = worktrees / "mine"
    for directory in (prefix, here, worktrees / "live", worktrees / "stale", repo / ".venv"):
        directory.mkdir(parents=True)
    for checkout in (here, worktrees / "live", worktrees / "stale"):
        (checkout / ".git").write_text("gitdir: elsewhere\n")
    monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
    return repo, worktrees, here


def test_a_gone_sibling_worktree_mirror_is_removed(world):
    _repo, worktrees, here = world
    gone = _mirror_with_pyc(worktrees / "removed")
    live = _mirror_with_pyc(worktrees / "live")
    mine = _mirror_with_pyc(here)
    _prune_bytecode_mirror_of_gone_checkouts(str(here))
    assert not os.path.exists(gone)
    assert os.path.isdir(live)
    assert os.path.isdir(mine)


def test_an_idle_sibling_checkout_is_removed_and_a_fresh_one_kept(world):
    _repo, worktrees, here = world
    stale = _mirror_with_pyc(worktrees / "stale")
    _age(stale, _OLD)
    fresh = _mirror_with_pyc(worktrees / "live")
    _age(fresh, _OLD)
    os.utime(os.path.join(fresh, "pkg"))  # one recent write keeps the whole tree
    _prune_bytecode_mirror_of_gone_checkouts(str(here))
    assert not os.path.exists(stale)
    assert os.path.isdir(fresh)


def test_an_idle_child_that_is_not_a_checkout_is_kept(world):
    """A warm shared ``.venv`` is only read, so its mirror stops changing while in use."""
    repo, _worktrees, here = world
    venv = _mirror_with_pyc(repo / ".venv")
    _age(venv, _OLD)
    _prune_bytecode_mirror_of_gone_checkouts(str(here))
    assert os.path.isdir(venv)


def test_the_grandparent_level_drops_a_gone_scratch_dir_but_not_the_runs_own(world):
    repo, worktrees, here = world
    gone = _mirror_with_pyc(repo / "deleted-scratch-dir")
    own_chain = _mirror_with_pyc(here)
    _age(os.path.dirname(own_chain), _OLD)
    _prune_bytecode_mirror_of_gone_checkouts(str(here))
    assert not os.path.exists(gone)
    assert os.path.isdir(own_chain)


def test_nothing_happens_without_a_prefix(world, monkeypatch):
    _repo, worktrees, here = world
    gone = _mirror_with_pyc(worktrees / "removed")
    monkeypatch.setattr(sys, "pycache_prefix", None)
    _prune_bytecode_mirror_of_gone_checkouts(str(here))
    assert os.path.isdir(gone)

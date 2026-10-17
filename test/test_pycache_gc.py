"""Tests for the bytecode-cache GC (``pycache_gc.prune_pycache``).

The cache under ``<data home>/cache/pycache`` is a PEP 3147 mirror the desktop
app points ``PYTHONPYCACHEPREFIX`` at; CPython only ever adds entries, so the
gateway prunes by TTL and an oldest-first total-size cap. Deleting is always
safe (a ``.pyc`` regenerates on the next import), so these tests pin the
selection policy, not any preservation guarantee.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew.pycache_gc import _fd_traversal_supported, prune_pycache

_no_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs elevation on Windows"
)

# The prune only runs where the traversal can be anchored to no-follow
# directory handles; elsewhere (Windows) it is a fail-closed no-op, pinned by
# test_platform_without_dir_fd_support_is_a_noop instead of these.
_requires_fd_traversal = pytest.mark.skipif(
    not _fd_traversal_supported(),
    reason="prune is a fail-closed no-op without dir_fd-anchored traversal",
)

_DAY = 86400.0
_NOW = 2_000_000_000.0


def _write(root: Path, rel: str, *, size: int = 10, age_days: float = 0.0) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    mtime = _NOW - age_days * _DAY
    os.utime(path, (mtime, mtime))
    return path


@_requires_fd_traversal
def test_expired_entries_removed_fresh_kept(tmp_path):
    tmp_path = tmp_path.resolve()
    old = _write(tmp_path, "Users/x/lib/python3.11/tarfile.cpython-311.pyc", age_days=45)
    fresh = _write(tmp_path, "Users/x/lib/python3.11/pathlib.cpython-311.pyc", age_days=1)

    removed, freed = prune_pycache(tmp_path, max_age_days=30, now=_NOW)

    assert removed == 1
    assert freed == 10
    assert not old.exists()
    assert fresh.exists()


@_requires_fd_traversal
def test_size_cap_evicts_oldest_first(tmp_path):
    tmp_path = tmp_path.resolve()
    oldest = _write(tmp_path, "a/m1.pyc", size=40, age_days=3)
    middle = _write(tmp_path, "b/m2.pyc", size=40, age_days=2)
    newest = _write(tmp_path, "c/m3.pyc", size=40, age_days=1)

    removed, freed = prune_pycache(tmp_path, max_age_days=30, max_total_bytes=80, now=_NOW)

    # 120 bytes total > 80 cap: exactly the oldest goes, leaving 80.
    assert removed == 1
    assert freed == 40
    assert not oldest.exists()
    assert middle.exists()
    assert newest.exists()


@_requires_fd_traversal
def test_only_pyc_files_are_touched(tmp_path):
    tmp_path = tmp_path.resolve()
    foreign = _write(tmp_path, "sub/notes.txt", age_days=400)
    expired = _write(tmp_path, "sub/m.cpython-311.pyc", age_days=400)

    removed, _freed = prune_pycache(tmp_path, max_age_days=30, now=_NOW)

    assert removed == 1
    assert not expired.exists()
    assert foreign.exists()
    # The foreign file pins its directory through the empty-dir sweep.
    assert foreign.parent.is_dir()


@_requires_fd_traversal
def test_emptied_directories_pruned_root_kept(tmp_path):
    tmp_path = tmp_path.resolve()
    _write(tmp_path, "deep/nested/tree/m.pyc", age_days=90)

    prune_pycache(tmp_path, max_age_days=30, now=_NOW)

    assert tmp_path.is_dir()
    assert not (tmp_path / "deep").exists()


def test_missing_root_is_a_noop(tmp_path):
    tmp_path = tmp_path.resolve()
    assert prune_pycache(tmp_path / "absent") == (0, 0)


@_no_symlinks
def test_symlinked_root_refused(tmp_path):
    tmp_path = tmp_path.resolve()
    real = tmp_path / "real"
    victim = _write(real, "m.pyc", age_days=400)
    link = tmp_path / "link"
    link.symlink_to(real)

    assert prune_pycache(link, max_age_days=30, now=_NOW) == (0, 0)
    assert victim.exists()


@_no_symlinks
def test_symlinked_pyc_not_followed(tmp_path):
    tmp_path = tmp_path.resolve()
    target = tmp_path / "outside-target.bin"
    target.write_bytes(b"payload")
    link = tmp_path / "cache" / "evil.pyc"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    mtime = _NOW - 400 * _DAY
    os.utime(link, (mtime, mtime), follow_symlinks=False)

    removed, _freed = prune_pycache(tmp_path / "cache", max_age_days=30, now=_NOW)

    # lstat classifies the entry as a symlink, not a regular file: skipped.
    assert removed == 0
    assert target.exists()


@_no_symlinks
def test_symlinked_subdirectory_not_descended_into(tmp_path):
    """A symlinked directory *inside* the cache root must not be walked.

    This is the general form of the Windows-junction concern the root-level
    refusal alone doesn't cover: a reparse point partway down the tree must
    not let the walk cross into files outside the cache root's real storage.
    """
    tmp_path = tmp_path.resolve()
    outside = tmp_path / "outside"
    victim = _write(outside, "victim.pyc", age_days=400)

    root = tmp_path / "cache"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    removed, freed = prune_pycache(root, max_age_days=30, now=_NOW)

    assert (removed, freed) == (0, 0)
    assert victim.exists()


def test_platform_without_dir_fd_support_is_a_noop(tmp_path, monkeypatch):
    """No raceable fallback: without dir_fd anchoring the prune must not run.

    A path-based walk validated up front can be redirected mid-walk by
    swapping a directory for a symlink/junction, so on platforms that cannot
    anchor the traversal to no-follow directory handles the GC fails closed
    instead of degrading to the racy walk.
    """
    tmp_path = tmp_path.resolve()
    from kiro_crew import pycache_gc

    victim = _write(tmp_path, "m.pyc", age_days=400)
    monkeypatch.setattr(pycache_gc, "_fd_traversal_supported", lambda: False)

    assert pycache_gc.prune_pycache(tmp_path, max_age_days=30, now=_NOW) == (0, 0)
    assert victim.exists()


@_no_symlinks
def test_size_cap_sweep_revalidates_root(tmp_path, monkeypatch):
    """The second (size-cap) sweep must not trust the first pass's root.

    Swap the root for a symlink between the TTL pass and the size-cap pass:
    the re-open with O_NOFOLLOW fails and nothing outside is touched.
    """
    tmp_path = tmp_path.resolve()
    from kiro_crew import pycache_gc

    root = tmp_path / "cache"
    _write(root, "a/m1.pyc", size=40, age_days=3)
    _write(root, "b/m2.pyc", size=40, age_days=2)
    outside = tmp_path / "outside"
    victim = _write(outside, "v.pyc", size=40, age_days=5)

    real_sweep_root = pycache_gc._sweep_root
    calls = {"n": 0}

    def swapping_sweep_root(r, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            # Between the passes the agent replaces the root with a link.
            import shutil

            shutil.rmtree(root)
            root.symlink_to(outside, target_is_directory=True)
        return real_sweep_root(r, **kwargs)

    monkeypatch.setattr(pycache_gc, "_sweep_root", swapping_sweep_root)

    removed, _freed = pycache_gc.prune_pycache(root, max_age_days=30, max_total_bytes=40, now=_NOW)

    # TTL pass removed nothing (all fresh); size-cap pass refused the
    # swapped root, so only the outside victim's survival matters.
    assert victim.exists()


@_no_symlinks
def test_symlinked_ancestor_component_refused(tmp_path):
    """A symlink swapped into a writable ANCESTOR of the root is refused.

    O_NOFOLLOW guards only the final component of a single open, so the root
    must be opened component by component: here `cache/` is a symlink and the
    prune target is `cache/pycache` — a real directory *inside the link
    target*. A one-shot open of the full path would traverse the link and
    delete outside the cache.
    """
    tmp_path = tmp_path.resolve()
    outside = tmp_path / "outside"
    victim = _write(outside / "pycache", "v.pyc", age_days=400)

    (tmp_path / "cache").symlink_to(outside, target_is_directory=True)

    result = prune_pycache(tmp_path / "cache" / "pycache", max_age_days=30, now=_NOW)

    assert result == (0, 0)
    assert victim.exists()


def test_arbitrary_pycache_prefix_is_not_adopted(tmp_path, monkeypatch):
    """A user-set PYTHONPYCACHEPREFIX must never become the deletion root.

    Only Kiro Crew's own configured cache directory is ever pruned; an active
    prefix pointing elsewhere is ignored (those mirrors are not ours to
    manage).
    """
    from kiro_crew import pycache_gc

    configured = tmp_path / "crew" / "cache" / "pycache"
    foreign = tmp_path / "somewhere" / "else"
    monkeypatch.setattr(pycache_gc, "config_dir", lambda: tmp_path / "crew")
    monkeypatch.setattr(pycache_gc.sys, "pycache_prefix", str(foreign))

    assert pycache_gc.pycache_cache_dir() == configured


def test_matching_pycache_prefix_form_is_honored(tmp_path, monkeypatch):
    """When the active prefix IS the configured dir, its path form wins."""
    from kiro_crew import pycache_gc

    configured = tmp_path / "crew" / "cache" / "pycache"
    configured.mkdir(parents=True)
    monkeypatch.setattr(pycache_gc, "config_dir", lambda: tmp_path / "crew")
    monkeypatch.setattr(pycache_gc.sys, "pycache_prefix", str(configured))

    assert pycache_gc.pycache_cache_dir() == configured

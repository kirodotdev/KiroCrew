"""rmtree_force on POSIX trees of read-only directories, and the sweep's warning volume.

A Go module cache is ``0555`` directories: on POSIX an entry is removed only
through a writable parent, so these trees never left the scratch quarantine and
every sweep logged a line per stuck path.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from kiro_crew import agent_scratch
from kiro_crew import platform_compat_files as pcf

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory modes")
#: Owner read and search only: the read-only pose a Go module cache gives its directories.
_READ_ONLY = stat.S_IRUSR | stat.S_IXUSR
_WRITABLE = stat.S_IRWXU

_NEEDS_DENIAL = pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0, reason="root ignores directory modes"
)


def _read_only_module_cache(root: Path, *, modules: int = 2, files: int = 3) -> int:
    """Build a Go-style cache under *root*: every directory ``0555``. Returns the file count."""
    count = 0
    for m in range(modules):
        module = root / "pkg" / "mod" / f"example.com@v1.{m}.0"
        (module / "sub").mkdir(parents=True)
        for f in range(files):
            (module / f"f{f}.go").write_text("package x\n")
            (module / "sub" / f"g{f}.go").write_text("package y\n")
            count += 2
    for current, dirs, _files in os.walk(root, topdown=False):
        for name in dirs:
            os.chmod(os.path.join(current, name), _READ_ONLY)
    return count


def _restore_writable(root: Path) -> None:
    if not root.exists():
        return
    for current, dirs, _files in os.walk(root):
        for name in dirs:
            os.chmod(os.path.join(current, name), _WRITABLE)
    os.chmod(root, _WRITABLE)


@_POSIX_ONLY
@_NEEDS_DENIAL
def test_a_tree_of_read_only_directories_is_removed(tmp_path: Path) -> None:
    tree = tmp_path / "gomodcache"
    tree.mkdir()
    _read_only_module_cache(tree)
    os.chmod(tree, _READ_ONLY)
    try:
        assert pcf.rmtree_force(tree) is True
        assert not tree.exists()
    finally:
        _restore_writable(tree)


@_POSIX_ONLY
@_NEEDS_DENIAL
def test_a_symlink_out_of_the_tree_is_never_chmodded_or_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    os.chmod(outside, _READ_ONLY)
    tree = tmp_path / "tree"
    locked = tree / "locked"
    locked.mkdir(parents=True)
    (locked / "link").symlink_to(outside, target_is_directory=True)
    (locked / "file").write_text("x")
    os.chmod(locked, _READ_ONLY)
    try:
        assert pcf.rmtree_force(tree) is True
        assert stat.S_IMODE(outside.stat().st_mode) == _READ_ONLY
        assert (outside / "keep").read_text() == "x"
    finally:
        os.chmod(outside, _WRITABLE)
        _restore_writable(tree)


@_POSIX_ONLY
@_NEEDS_DENIAL
def test_the_root_s_own_parent_is_never_chmodded(tmp_path: Path) -> None:
    holder = tmp_path / "holder"
    tree = holder / "tree"
    tree.mkdir(parents=True)
    os.chmod(holder, _READ_ONLY)
    try:
        assert pcf.rmtree_force(tree) is False
        assert stat.S_IMODE(holder.stat().st_mode) == _READ_ONLY
        assert tree.is_dir()
    finally:
        os.chmod(holder, _WRITABLE)


def _outside_lookalike(tmp_path: Path) -> Path:
    """A read-only directory outside the tree holding ``node_modules/pkg/file``."""
    outside = tmp_path / "outside"
    (outside / "node_modules" / "pkg").mkdir(parents=True)
    (outside / "node_modules" / "pkg" / "file").write_text("keep")
    os.chmod(outside / "node_modules" / "pkg", _READ_ONLY)
    return outside


def _outside_untouched(outside: Path) -> bool:
    pkg = outside / "node_modules" / "pkg"
    return stat.S_IMODE(pkg.stat().st_mode) == _READ_ONLY and (pkg / "file").read_text() == "keep"


def _retry(target: Path, root: Path, root_fd: int) -> list[str]:
    failed: list[str] = []
    pcf._clear_readonly_and_retry(
        os.unlink,
        str(target),
        PermissionError(13, "denied"),
        root=str(root),
        root_fd=root_fd,
        failed=failed,
    )
    return failed


@_POSIX_ONLY
@_NEEDS_DENIAL
def test_an_ancestor_of_the_root_swapped_before_the_retry_is_never_followed(
    tmp_path: Path,
) -> None:
    # The sweep removes tree/a/node_modules; after rmtree opened it, tree/a is
    # swapped for a link to a lookalike outside the tree.
    outside = _outside_lookalike(tmp_path)
    root = tmp_path / "tree" / "a" / "node_modules"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "file").write_text("x")
    os.chmod(root / "pkg", _READ_ONLY)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    moved = tmp_path / "moved"
    try:
        (tmp_path / "tree" / "a").rename(moved)
        (tmp_path / "tree" / "a").symlink_to(outside, target_is_directory=True)
        failed = _retry(root / "pkg" / "file", root, root_fd)
        assert failed == []  # retried in the tree rmtree opened
        assert not (moved / "node_modules" / "pkg" / "file").exists()
        assert _outside_untouched(outside)
    finally:
        os.close(root_fd)
        os.chmod(outside / "node_modules" / "pkg", _WRITABLE)
        _restore_writable(moved)


@_POSIX_ONLY
@_NEEDS_DENIAL
def test_a_component_inside_the_root_swapped_before_the_retry_is_refused(
    tmp_path: Path,
) -> None:
    # rmtree refused root/node_modules/pkg/file; before the retry, node_modules
    # becomes a link to the lookalike outside the tree.
    outside = _outside_lookalike(tmp_path)
    root = tmp_path / "tree"
    root.mkdir()
    (root / "node_modules").symlink_to(outside / "node_modules", target_is_directory=True)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    target = root / "node_modules" / "pkg" / "file"
    try:
        assert _retry(target, root, root_fd) == [str(target)]
        assert _outside_untouched(outside)
    finally:
        os.close(root_fd)
        os.chmod(outside / "node_modules" / "pkg", _WRITABLE)


@_POSIX_ONLY
@_NEEDS_DENIAL
def test_a_root_that_is_no_longer_the_chosen_directory_is_left_alone(tmp_path: Path) -> None:
    # The sweep chose tree/a/node_modules; before rmtree_force opens it, tree/a
    # is swapped for a link to a lookalike outside the tree.
    outside = _outside_lookalike(tmp_path)
    root = tmp_path / "tree" / "a" / "node_modules"
    root.mkdir(parents=True)
    chosen = os.lstat(root)
    (tmp_path / "tree" / "a").rename(tmp_path / "moved")
    (tmp_path / "tree" / "a").symlink_to(outside, target_is_directory=True)
    failures: list[str] = []
    try:
        assert (
            pcf.rmtree_force(root, failures=failures, expect=(chosen.st_dev, chosen.st_ino))
            is False
        )
        assert failures == [str(root)]
        assert _outside_untouched(outside)
    finally:
        os.chmod(outside / "node_modules" / "pkg", _WRITABLE)


def test_stuck_entries_are_reported_as_one_counted_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tree = tmp_path / "work"
    for name in ("a", "b"):
        (tree / name / "node_modules" / "pkg").mkdir(parents=True)
        for i in range(3):
            (tree / name / "node_modules" / "pkg" / f"f{i}").write_text("x")
    real_unlink = os.unlink

    def _refuse(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)).startswith("f"):
            raise PermissionError(13, "denied", os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", _refuse)
    with caplog.at_level(logging.DEBUG):
        agent_scratch._strip_regenerable(tree)
    monkeypatch.setattr(os, "unlink", real_unlink)

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    # Per tree: three refused files, then pkg and node_modules left non-empty.
    assert "could not remove 10 entries" in warnings[0]
    assert str(tree) in warnings[0]


def test_rmtree_force_logs_one_counted_line_without_a_collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tree = tmp_path / "t"
    tree.mkdir()
    for i in range(4):
        (tree / f"f{i}").write_text("x")
    monkeypatch.setattr(
        os, "unlink", lambda p, *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied", p))
    )
    with caplog.at_level(logging.WARNING, logger=pcf.logger.name):
        assert pcf.rmtree_force(tree) is False
    messages = [r.getMessage() for r in caplog.records]
    # Four refused files, then the non-empty root.
    assert messages == [f"Cannot remove 5 entries under {tree}"]

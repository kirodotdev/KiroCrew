"""``fsync_open_dir`` syncs an already-open directory descriptor the caller owns.

The guarantee is the same as :func:`kiro_crew.atomic_write.fsync_dir` -- a crash after a
rename must not come back to the old name -- for a caller that already holds the
directory open rather than a path to open. The behaviour worth guarding is WHICH
``fsync`` errors it tolerates: a directory that the filesystem cannot sync at all
(network mounts answer ``EINVAL``/``ENOTSUP``) is a no-op, because the rename plus the
file fsync are the durability available there, but a genuine ``EIO`` -- the device did
not take the write -- still propagates. It also must never close the descriptor it did
not open.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.atomic_write import _DIR_SYNC_UNSUPPORTED, fsync_open_dir

pytestmark = pytest.mark.skipif(
    platform_compat.IS_WINDOWS, reason="no directory descriptor to open on Windows"
)


def _open_dir(path: Path) -> int:
    return os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))


def _raise(code: int):
    def _fsync(_fd: int) -> None:
        raise OSError(code, "injected")

    return _fsync


@pytest.mark.parametrize("code", sorted(_DIR_SYNC_UNSUPPORTED))
def test_a_filesystem_that_cannot_sync_a_directory_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Every unsupported errno is swallowed, so an apply on such a mount still finishes."""
    fd = _open_dir(tmp_path)
    try:
        monkeypatch.setattr(os, "fsync", _raise(code))
        # Returns without raising; the caller's write is treated as durable.
        fsync_open_dir(fd, tmp_path)
    finally:
        os.close(fd)


def test_a_genuine_io_failure_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``EIO`` means the device refused the write; it is NOT in the tolerated set."""
    assert errno.EIO not in _DIR_SYNC_UNSUPPORTED
    fd = _open_dir(tmp_path)
    try:
        monkeypatch.setattr(os, "fsync", _raise(errno.EIO))
        with pytest.raises(OSError) as caught:
            fsync_open_dir(fd, tmp_path)
        assert caught.value.errno == errno.EIO
    finally:
        os.close(fd)


def test_best_effort_downgrades_even_a_genuine_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller already past its point of no return can ask for warn-not-raise."""
    fd = _open_dir(tmp_path)
    try:
        monkeypatch.setattr(os, "fsync", _raise(errno.EIO))
        fsync_open_dir(fd, tmp_path, best_effort=True)  # no raise
    finally:
        os.close(fd)


def test_a_clean_sync_leaves_the_descriptor_open(tmp_path: Path) -> None:
    """It syncs the fd the caller owns and does NOT close it -- the caller's finally does."""
    fd = _open_dir(tmp_path)
    try:
        fsync_open_dir(fd, tmp_path)
        # Still usable: fstat would raise EBADF on a closed descriptor.
        assert os.fstat(fd).st_ino == os.stat(tmp_path).st_ino
    finally:
        os.close(fd)

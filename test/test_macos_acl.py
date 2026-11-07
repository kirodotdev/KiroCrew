"""What ``macos_acl`` does with each answer libSystem can give.

The real ``acl_*`` calls exist only on macOS, where
test_atomic_write_acl_preservation.py drives them through ``chmod +a``. Here
``ctypes.CDLL`` hands the module a stand-in that keeps each file's ACL as a tuple
of entries, so the carry and cleanup rules run on every POSIX host.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
from types import SimpleNamespace

import pytest

from kiro_crew import macos_acl, platform_compat
from kiro_crew.owner_only_files import OWNER_ONLY_FILE_MODE

pytestmark = [
    pytest.mark.usefixtures("fresh_darwin_libc_handles"),
    pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="descriptor-relative POSIX calls"),
]

_DENY_READ = ("user:nobody deny read",)


class _FakeLibSystem:
    """The four ``acl_*`` calls over ACLs held per inode.

    A handle is an int naming one copied ACL. ``fail`` maps a call's name to the
    errno it fails with; ``sets`` records the file's mode and the entries of every
    set, and ``freed`` every handle released.
    """

    def __init__(self, acls: dict[int, tuple[str, ...]]) -> None:
        self.acls = acls
        self.fail: dict[str, int] = {}
        self.handles: dict[int, tuple[str, ...]] = {}
        self.sets: list[tuple[int, tuple[str, ...]]] = []
        self.freed: list[int] = []

    def _new_handle(self, entries: tuple[str, ...]) -> int:
        handle = len(self.handles) + 1
        self.handles[handle] = entries
        return handle

    def acl_get_fd_np(self, fd: int, acl_type: int) -> int | None:
        assert acl_type == macos_acl._ACL_TYPE_EXTENDED
        entries = self.acls.get(os.fstat(fd).st_ino)
        if "acl_get_fd_np" in self.fail or entries is None:
            ctypes.set_errno(self.fail.get("acl_get_fd_np", errno.ENOENT))
            return None
        return self._new_handle(entries)

    def acl_set_fd_np(self, fd: int, handle: int, acl_type: int) -> int:
        assert acl_type == macos_acl._ACL_TYPE_EXTENDED
        if "acl_set_fd_np" in self.fail:
            ctypes.set_errno(self.fail["acl_set_fd_np"])
            return -1
        info = os.fstat(fd)
        self.sets.append((stat.S_IMODE(info.st_mode), self.handles[handle]))
        self.acls[info.st_ino] = self.handles[handle]
        return 0

    def acl_init(self, count: int) -> int:
        return self._new_handle(())

    def acl_free(self, handle: int) -> int:
        self.freed.append(handle)
        return 0

    def cdll(self, name: str | None, *, use_errno: bool = False) -> SimpleNamespace:
        assert name is None and use_errno

        def prototyped(call):
            # A bound method refuses the argtypes/restype the module assigns.
            return lambda *args: call(*args)

        calls = ("acl_get_fd_np", "acl_set_fd_np", "acl_init", "acl_free")
        return SimpleNamespace(**{n: prototyped(getattr(self, n)) for n in calls})


@pytest.fixture
def files(tmp_path, monkeypatch):
    """A source carrying ``_DENY_READ`` and a destination with no ACL, open."""
    src, dst = tmp_path / "src.txt", tmp_path / "dst.txt"
    src.write_text("old", encoding="utf-8")
    dst.write_text("new", encoding="utf-8")
    lib = _FakeLibSystem({src.stat().st_ino: _DENY_READ})
    monkeypatch.setattr(ctypes, "CDLL", lib.cdll)
    src_fd, dst_fd = os.open(src, os.O_RDONLY), os.open(dst, os.O_RDONLY)
    try:
        yield lib, src_fd, dst_fd, dst.stat().st_ino
    finally:
        os.close(src_fd)
        os.close(dst_fd)


def test_copy_gives_the_destination_the_source_acl_and_frees_it(files):
    lib, src_fd, dst_fd, dst_ino = files

    macos_acl.copy_extended_acl(src_fd, dst_fd)

    assert lib.acls[dst_ino] == _DENY_READ
    assert lib.freed == [1]


@pytest.mark.parametrize("code", sorted({errno.ENOENT, errno.ENOTSUP, errno.EOPNOTSUPP}))
def test_copy_leaves_the_destination_alone_when_there_is_nothing_to_carry(files, code):
    lib, src_fd, dst_fd, dst_ino = files
    lib.fail["acl_get_fd_np"] = code

    macos_acl.copy_extended_acl(src_fd, dst_fd)

    assert dst_ino not in lib.acls
    assert lib.sets == []


def test_copy_raises_when_the_lookup_fails_for_another_reason(files):
    """A failed lookup is not proof there is no ACL to lose."""
    lib, src_fd, dst_fd, dst_ino = files
    lib.fail["acl_get_fd_np"] = errno.EIO

    with pytest.raises(OSError) as raised:
        macos_acl.copy_extended_acl(src_fd, dst_fd)

    assert raised.value.errno == errno.EIO
    assert dst_ino not in lib.acls


def test_copy_raises_when_the_set_is_refused_and_still_frees_the_handle(files):
    lib, src_fd, dst_fd, dst_ino = files
    lib.fail["acl_set_fd_np"] = errno.EPERM

    with pytest.raises(PermissionError):
        macos_acl.copy_extended_acl(src_fd, dst_fd)

    assert dst_ino not in lib.acls
    assert lib.freed == [1]


@pytest.fixture
def temp(tmp_path, monkeypatch):
    """A 0644 temp carrying ``_DENY_READ``, and its pinned folder."""
    leaf = tmp_path / "doc.md.tmp"
    leaf.write_text("new", encoding="utf-8")
    leaf.chmod(0o644)
    lib = _FakeLibSystem({leaf.stat().st_ino: _DENY_READ})
    monkeypatch.setattr(ctypes, "CDLL", lib.cdll)
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield lib, leaf, dir_fd
    finally:
        os.close(dir_fd)


def test_unlink_makes_the_temp_owner_only_before_emptying_its_acl(temp):
    """The deny entry may be all that keeps another account out of the content."""
    lib, leaf, dir_fd = temp

    macos_acl.unlink_clearing_acl(leaf.name, dir_fd=dir_fd)

    assert not leaf.exists()
    assert lib.sets == [(OWNER_ONLY_FILE_MODE, ())]
    assert sorted(lib.freed) == sorted(lib.handles)


def test_a_refused_unlink_sets_the_temp_acl_back(temp, monkeypatch):
    lib, leaf, dir_fd = temp
    ino = leaf.stat().st_ino

    def refuse(name, *, dir_fd=None):
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM), name)

    monkeypatch.setattr(os, "unlink", refuse)
    with pytest.raises(PermissionError):
        macos_acl.unlink_clearing_acl(leaf.name, dir_fd=dir_fd)

    assert leaf.exists()
    assert lib.acls[ino] == _DENY_READ
    assert sorted(lib.freed) == sorted(lib.handles)

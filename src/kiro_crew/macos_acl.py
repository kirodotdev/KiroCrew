"""The macOS extended ACL, read and set through descriptors.

macOS keeps a file's ACL behind libSystem's ``acl_*_fd_np`` calls rather than the
xattr API ``atomic_write`` carries on Linux, so a replacement installed by rename
would otherwise keep only the entries its folder hands down. Darwin only: callers
gate on ``platform_compat.IS_MACOS``. libSystem is loaded per call, so no handle is
cached across tests or callers.
"""

from __future__ import annotations

import ctypes
import errno
import os

from kiro_crew.owner_only_files import OWNER_ONLY_FILE_MODE

#: ``ACL_TYPE_EXTENDED`` from ``<sys/acl.h>``.
_ACL_TYPE_EXTENDED = 0x00000100


def _libsystem() -> ctypes.CDLL:
    lib = ctypes.CDLL(None, use_errno=True)
    lib.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.acl_get_fd_np.restype = ctypes.c_void_p
    lib.acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    lib.acl_set_fd_np.restype = ctypes.c_int
    lib.acl_init.argtypes = [ctypes.c_int]
    lib.acl_init.restype = ctypes.c_void_p
    lib.acl_free.argtypes = [ctypes.c_void_p]
    lib.acl_free.restype = ctypes.c_int
    return lib


def _raise_errno() -> None:
    err = ctypes.get_errno()
    raise OSError(err, os.strerror(err))


def _set_and_free(lib: ctypes.CDLL, fd: int, acl: int) -> None:
    try:
        if lib.acl_set_fd_np(fd, acl, _ACL_TYPE_EXTENDED) != 0:
            _raise_errno()
    finally:
        lib.acl_free(acl)


def copy_extended_acl(src_fd: int, dst_fd: int) -> None:
    """Give *dst_fd* exactly *src_fd*'s extended ACL, inherited entries included.

    A source with no extended ACL (``ENOENT``), or on a volume without ACLs, has
    nothing to carry and *dst_fd* is left alone. Any other failure raises: a lookup
    that failed is not proof there is no ACL to lose.
    """
    lib = _libsystem()
    acl = lib.acl_get_fd_np(src_fd, _ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() in (errno.ENOENT, errno.ENOTSUP, errno.EOPNOTSUPP):
            return
        _raise_errno()
    _set_and_free(lib, dst_fd, acl)


def unlink_clearing_acl(name: str, *, dir_fd: int | None) -> None:
    """Remove *name* after emptying its extended ACL, whose deny entries can refuse it.

    The file is made owner-only first, since a deny entry may be all that keeps
    another account out of its content. If the removal is still refused, the ACL
    it had is set back, so a file left behind keeps every entry it carried. One
    that has no ACL, whose ACL cannot be read, or whose mode cannot be narrowed
    keeps its ACL.
    """
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        lib = _libsystem()
        saved = lib.acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
        if not saved:
            _raise_errno()
        try:
            os.fchmod(fd, OWNER_ONLY_FILE_MODE)
            empty = lib.acl_init(0)
            if not empty:
                _raise_errno()
            _set_and_free(lib, fd, empty)
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                lib.acl_set_fd_np(fd, saved, _ACL_TYPE_EXTENDED)
                raise
        finally:
            lib.acl_free(saved)
    finally:
        os.close(fd)

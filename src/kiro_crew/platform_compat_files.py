"""Filesystem operations whose meaning differs by platform.

Permission changes that never raise, a tree removal that defeats Windows read-only
files, the no-replace rename and directory publication, a path's mount identity, the
log-tail and lock-sweep opens that leave the file renamable and deletable while held,
owner access restored to every directory of a copied tree, the macOS TCC folders a
home-rooted walk prunes, and the remote-volume, executable-file and read-only checks.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import contextlib
import ctypes.util
import errno
import functools
import logging
import os
import platform
import shutil
import stat
import struct
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from kiro_crew import windows_acl
from kiro_crew.platform_compat_links import (
    _WIN_GENERIC_READ,
    _WIN_GENERIC_WRITE,
    _WIN_OPEN_EXISTING,
    is_link_or_junction,
    lstat_is_name_surrogate,
)
from kiro_crew.platform_compat_native import IS_LINUX, IS_MACOS, IS_POSIX, IS_WINDOWS

if not IS_POSIX:
    import msvcrt  # type: ignore[import-not-found]

# The whole compatibility layer logs under one name, which operator log filters and
# the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.platform_compat")


# Python's os.rename() replaces an existing empty directory on POSIX. Directory
# publication sometimes needs the stronger create-if-absent contract, which the
# kernel exposes but the stdlib does not: renameat2(RENAME_NOREPLACE) on Linux
# and renameatx_np(RENAME_EXCL) on macOS. Resolve the native seam once so callers
# can advertise the capability honestly and fail closed everywhere else.
_RENAME_NOREPLACE_FN: Any = None
_RENAME_NOREPLACE_FLAG = 0

#: ``SYS_renameat2`` numbers per ``platform.machine()``. The syscall has existed
#: in Linux since 3.15 (2014), but the glibc *wrapper* symbol was only added in
#: glibc 2.28. On glibc 2.26/2.27 (e.g. Amazon Linux 2) the kernel supports the
#: call yet ``getattr(libc, "renameat2")`` raises ``AttributeError`` — so we
#: reach the kernel directly through the generic ``syscall()`` entry point,
#: which every glibc exposes, keyed by this table. Numbers are arch-stable ABI.
_SYS_RENAMEAT2_BY_MACHINE: dict[str, int] = {
    "x86_64": 316,
    "i386": 353,
    "i686": 353,
    "aarch64": 276,
    "armv7l": 382,
    "armv6l": 382,
    "ppc64le": 357,
    "ppc64": 357,
    "s390x": 347,
    "riscv64": 276,
}


def _build_renameat2_via_syscall(libc: "ctypes.CDLL") -> Any:
    """Return a renameat2(2) callable via the raw ``syscall()`` seam, or None.

    Used only when the glibc ``renameat2`` wrapper symbol is absent but the
    running kernel supports the syscall (glibc 2.26/2.27 on a modern kernel).
    The returned callable matches the wrapper's 5-argument shape
    ``(olddirfd, oldpath, newdirfd, newpath, flags)`` and preserves errno so
    :func:`rename_noreplace`'s EEXIST / ENOSYS handling is unchanged. Returns
    ``None`` when the architecture's syscall number is unknown, so the caller
    still fails closed rather than issuing a wrong-numbered syscall.
    """
    nr = _SYS_RENAMEAT2_BY_MACHINE.get(platform.machine())
    if nr is None:
        return None
    try:
        _syscall = libc.syscall
    except AttributeError:
        return None
    _syscall.restype = ctypes.c_long
    # syscall() is variadic; ctypes needs the long syscall number typed, and the
    # trailing args are passed positionally with the same ctypes types the
    # wrapper used. c_long for the number, then int/char_p/int/char_p/uint.
    _syscall.argtypes = [
        ctypes.c_long,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]

    def _renameat2(
        olddirfd: int,
        oldpath: bytes,
        newdirfd: int,
        newpath: bytes,
        flags: int,
    ) -> int:
        return _syscall(nr, olddirfd, oldpath, newdirfd, newpath, flags)

    return _renameat2


if IS_LINUX or IS_MACOS:
    try:
        _rename_libc = ctypes.CDLL(None, use_errno=True)
        _rename_symbol = "renameat2" if IS_LINUX else "renameatx_np"
        try:
            _RENAME_NOREPLACE_FN = getattr(_rename_libc, _rename_symbol)
            _RENAME_NOREPLACE_FN.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            _RENAME_NOREPLACE_FN.restype = ctypes.c_int
        except AttributeError:
            # glibc < 2.28 lacks the renameat2 wrapper symbol. On Linux the
            # syscall itself is available on any kernel >= 3.15, so reach it
            # directly; macOS has no such fallback (renameatx_np is the only
            # path), so it stays None and fails closed there.
            _RENAME_NOREPLACE_FN = _build_renameat2_via_syscall(_rename_libc) if IS_LINUX else None
        if _RENAME_NOREPLACE_FN is not None:
            _RENAME_NOREPLACE_FLAG = 1 if IS_LINUX else 4
    except OSError:
        _RENAME_NOREPLACE_FN = None

RENAME_NOREPLACE_AVAILABLE: bool = _RENAME_NOREPLACE_FN is not None


# ── Mount identity ──
#: ``SYS_statx`` numbers per ``platform.machine()``. As with renameat2 above, the
#: kernel call (Linux 4.11) is older than glibc's ``statx`` wrapper (2.28), so a
#: host on glibc 2.26/2.27 reaches it through ``syscall()``. Arch-stable ABI.
_SYS_STATX_BY_MACHINE: dict[str, int] = {
    "x86_64": 332,
    "i386": 383,
    "i686": 383,
    "aarch64": 291,
    "armv7l": 397,
    "armv6l": 397,
    "ppc64le": 383,
    "ppc64": 383,
    "s390x": 379,
    "riscv64": 291,
}
_AT_FDCWD = -100
_AT_EMPTY_PATH = 0x1000
_STATX_INO = 0x100
_STATX_MNT_ID = 0x1000
#: ``struct statx`` is 256 bytes; the fields read are ``stx_mask`` (u32 at 0),
#: ``stx_ino`` (u64 at 32), ``stx_dev_major`` / ``stx_dev_minor`` (u32 at 136 and
#: 140) and ``stx_mnt_id`` (u64 at 144).
_STATX_SIZE = 256
#: Set once a kernel answered without ``STATX_MNT_ID`` (before Linux 5.8): the
#: field never appears on that kernel, so later calls skip the syscall.
_statx_mnt_id_unreported = False


@functools.lru_cache(maxsize=1)
def _statx_call() -> Any:
    """The ``statx(dirfd, path, flags, mask, buf)`` callable, or None off Linux."""
    if not IS_LINUX:
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    wrapper = getattr(libc, "statx", None)
    if wrapper is not None:
        wrapper.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_void_p,
        ]
        wrapper.restype = ctypes.c_int
        return wrapper
    nr = _SYS_STATX_BY_MACHINE.get(platform.machine())
    syscall = getattr(libc, "syscall", None)
    if nr is None or syscall is None:
        return None
    syscall.restype = ctypes.c_long
    syscall.argtypes = [
        ctypes.c_long,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]

    def _statx(dirfd: int, path: bytes, flags: int, mask: int, buf: Any) -> int:
        return syscall(nr, dirfd, path, flags, mask, buf)

    return _statx


def mount_identity(path: str, *, dir_fd: int | None = None) -> tuple[int, int, int, int] | None:
    """``(mount id, device major, device minor, inode)`` of what *path* names, or None.

    The mount id is what tells two mounts of one directory apart: a bind mount
    shares its source's device and inode, so ``os.stat`` alone cannot. Symlinks,
    ``/proc`` magic links included, are followed. With *dir_fd* and an empty
    *path* the descriptor itself is identified (``AT_EMPTY_PATH``).

    ``None`` whenever the identity is unknown: not Linux, no ``statx`` for this
    architecture, a call that failed for any reason (the errno is not reported),
    or a kernel older than 5.8, which does not fill in the mount id.
    """
    global _statx_mnt_id_unreported
    if _statx_mnt_id_unreported:
        return None
    call = _statx_call()
    if call is None:
        return None
    buf = ctypes.create_string_buffer(_STATX_SIZE)
    flags = _AT_EMPTY_PATH if dir_fd is not None and path == "" else 0
    if call(
        _AT_FDCWD if dir_fd is None else dir_fd,
        os.fsencode(path),
        flags,
        _STATX_INO | _STATX_MNT_ID,
        buf,
    ):
        return None
    (mask,) = struct.unpack_from("<I", buf.raw, 0)
    if not mask & _STATX_MNT_ID:
        _statx_mnt_id_unreported = True
        return None
    if not mask & _STATX_INO:
        return None
    (ino,) = struct.unpack_from("<Q", buf.raw, 32)
    dev_major, dev_minor = struct.unpack_from("<II", buf.raw, 136)
    (mnt_id,) = struct.unpack_from("<Q", buf.raw, 144)
    return mnt_id, dev_major, dev_minor, ino


# ── macOS TCC-protected home subdirectories ──
# macOS gates these home subdirectories behind TCC (Transparency, Consent and
# Control). The FIRST read of any one of them by a given app triggers a modal
# "…would like to access files in your Downloads folder" prompt, and consent is
# recorded PER (app, folder) pair — so incidentally touching three of them
# during one operation produces THREE separate prompts, not one.
#
# Nothing Kiro Crew does at startup needs these folders: they are only ever
# reached INCIDENTALLY, by a breadth-first walk that was rooted at $HOME as a
# catch-all fallback (the @-mention file picker's search root). Pruning them
# from such unscoped walks removes the prompts entirely, which is strictly
# better than pre-declaring NS*FolderUsageDescription strings — those change
# the prompt's wording but still prompt, once per folder.
#
# This does NOT restrict a user's EXPLICIT navigation: an operation whose root
# the user named (a project dir, or a browse request for ~/Downloads itself) is
# scoped by definition and never consults this set. macOS still shows its own
# one-time prompt for that deliberate access, which is the expected contract.
#
# Names only (no leading path): matched against a single path component so the
# same set works for both os.walk dirname pruning and scandir entry filtering.
TCC_PROTECTED_HOME_DIRS: frozenset[str] = frozenset(
    {
        "Downloads",
        "Documents",
        "Desktop",
        "Pictures",
        "Movies",
        "Music",
    }
)
# ``Library`` is deliberately absent from the set ABOVE, but it is not
# unpruned — see TCC_LIBRARY_WALKABLE_CHILDREN. It cannot be a plain member
# here because it must be *descended into* to reach the cloud-drive mounts,
# which a top-level name prune would make unreachable.

#: Children of ``~/Library`` that stay walkable; every other child is pruned
#: from a home-rooted walk. This is an ALLOWLIST on purpose.
#:
#: Much of ``~/Library`` is gated behind Full Disk Access — ``Mail``,
#: ``Messages``, ``Safari``, ``Calendars``, ``HomeKit``, ``Cookies``,
#: ``IdentityServices``, ``Suggestions``, ``PersonalizationPortrait``,
#: ``Metadata/CoreSpotlight``, ``Containers/com.apple.*`` and several
#: ``Application Support`` leaves (``AddressBook``, ``CallHistoryDB``,
#: ``MobileSync``, ``com.apple.TCC``) — and Apple keeps ADDING to that list
#: with each release. A denylist would therefore go stale and silently start
#: leaking prompts again on the next macOS version, so the rule is inverted:
#: name the two paths worth reaching and drop the rest.
#:
#: The two kept entries are the modern cloud-drive mount points, which are
#: common project homes and hold real search hits:
#: ``~/Library/CloudStorage/<Provider>/`` (OneDrive / Google Drive / Dropbox)
#: and ``~/Library/Mobile Documents/`` (iCloud Drive).
TCC_LIBRARY_WALKABLE_CHILDREN: frozenset[str] = frozenset(
    {
        "CloudStorage",
        "Mobile Documents",
    }
)

#: The single ``~/Library`` component name, kept as a constant because the walk
#: pruner compares it positionally rather than by membership.
_LIBRARY_DIR = "Library"


def rename_noreplace(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    """Atomically rename *src* to an absent *dst*, or raise.

    Unlike :func:`os.rename`, an existing destination is never replaced. Both
    names are resolved relative to caller-pinned directory descriptors. A
    filesystem or platform that cannot preserve that contract raises
    :class:`NotImplementedError`; callers must not fall back to a check followed
    by ordinary rename because another writer can create the destination between
    those two operations.
    """
    fn = _RENAME_NOREPLACE_FN
    if fn is None:
        raise NotImplementedError("atomic no-replace rename is unavailable")
    src_bytes = os.fsencode(src)
    dst_bytes = os.fsencode(dst)
    ctypes.set_errno(0)
    if (
        fn(
            src_dir_fd,
            src_bytes,
            dst_dir_fd,
            dst_bytes,
            _RENAME_NOREPLACE_FLAG,
        )
        == 0
    ):
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, os.strerror(error), os.fspath(dst))
    unsupported = {errno.ENOSYS, errno.EINVAL}
    unsupported.add(getattr(errno, "EOPNOTSUPP", errno.EINVAL))
    unsupported.add(getattr(errno, "ENOTSUP", errno.EINVAL))
    if error in unsupported:
        raise NotImplementedError("filesystem lacks atomic no-replace rename")
    raise OSError(error, os.strerror(error), os.fspath(dst))


def publish_dir_noreplace(src: str | os.PathLike, dst: str | os.PathLike) -> None:
    """Atomically rename directory *src* to an ABSENT *dst*, never replacing.

    POSIX ``os.rename`` silently replaces an EMPTY destination directory, so a
    check-then-rename publish can destroy a racer's just-created directory and
    its metadata. This wrapper closes that window: on POSIX it uses
    :func:`rename_noreplace` with both names pinned to their shared parent's
    directory descriptor; on Windows plain ``os.rename`` already refuses any
    existing destination. Raises :class:`FileExistsError` when *dst* exists,
    and :class:`ValueError` when the two paths do not share a parent (the
    staging-sibling contract every caller follows).

    Hosts without the no-replace primitive (glibc < 2.28, and NFS/SMB/FUSE
    filesystems that reject RENAME_NOREPLACE with ENOSYS/EINVAL/EOPNOTSUPP)
    fall back to an atomic-exclusive ``os.mkdir`` CLAIM of the destination
    followed by a plain rename: the mkdir raises :class:`FileExistsError` when
    the destination is occupied, and the only directory the rename can then
    replace is the empty claim this very call created -- the no-replace
    guarantee for creation races is preserved, and the operation keeps working
    instead of crashing with ``NotImplementedError``.
    """
    if IS_WINDOWS:
        os.rename(src, dst)
        return
    src_abs = os.path.abspath(os.fspath(src))
    dst_abs = os.path.abspath(os.fspath(dst))
    parent = os.path.dirname(dst_abs)
    if os.path.dirname(src_abs) != parent:
        raise ValueError("publish_dir_noreplace requires sibling src and dst")
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            rename_noreplace(
                os.path.basename(src_abs),
                os.path.basename(dst_abs),
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            return
        except NotImplementedError:
            pass
    finally:
        os.close(parent_fd)
    os.mkdir(dst_abs)
    try:
        os.rename(src_abs, dst_abs)
    except BaseException:
        # Drop OUR claim so a retry is not permanently blocked by an orphaned
        # empty directory. os.rmdir only ever removes an empty directory, so
        # the worst it can touch is the claim this call just created; its own
        # failure is suppressed in favour of the original rename error.
        with contextlib.suppress(OSError):
            os.rmdir(dst_abs)
        raise


def tcc_protected_dirs_for_walk(root: str | os.PathLike) -> frozenset[str]:
    """Return the TCC-protected dir names to prune when walking *root*.

    Empty off macOS (no TCC), and empty unless *root* is the user's home
    directory itself: a walk the user explicitly scoped to ``~/Downloads`` (or
    to a project that happens to live under it) must still see its own
    contents. Only the incidental ``$HOME``-as-fallback walk is pruned.

    Callers MUST pass the same ``str`` they hand to :func:`os.walk` — the
    returned names are compared against ``os.walk``'s ``dirpath``, which is
    byte-identical to the ``top`` argument it was given.

    On any resolution failure this returns the empty set, i.e. it prunes
    nothing. That degrades to today's behavior (a prompt may appear) rather
    than silently hiding a directory the caller asked for.
    """
    if not IS_MACOS:
        return frozenset()
    try:
        if os.path.realpath(root) != os.path.realpath(os.path.expanduser("~")):
            return frozenset()
    except (OSError, ValueError):
        # OSError: EACCES / ELOOP / ENAMETOOLONG on an exotic path.
        # ValueError: realpath() rejects a path containing a null byte — NOT an
        # OSError subclass, so it would otherwise escape to the caller and 500
        # the /api/file-search request (same class as agent.py's guard).
        return frozenset()
    return TCC_PROTECTED_HOME_DIRS


def tcc_prune_walk_dirs(root: str, dirpath: str, dirnames: list[str]) -> list[str]:
    """Return *dirnames* minus the TCC-gated entries for this walk position.

    Single entry point for ``os.walk`` pruning: call it with the ``top`` passed
    to :func:`os.walk` plus the ``dirpath``/``dirnames`` of the current step and
    assign the result back into ``dirnames[:]``.

    Two positions prune, and only when *root* is the user's home directory
    itself (see :func:`tcc_protected_dirs_for_walk` for why an explicitly
    scoped root is never pruned):

    * at *root* — drop the gated top-level folders (``Downloads``, ``Desktop``,
      ...). ``Library`` is NOT dropped here, so the walk can reach the cloud
      mounts below it.
    * at ``<root>/Library`` — keep only TCC_LIBRARY_WALKABLE_CHILDREN and drop
      every other child, most of which is Full-Disk-Access gated.

    Every deeper position returns *dirnames* untouched. A name that merely
    matches a gated folder further down the tree (a project's own
    ``Documents/``) is not gated and stays walkable.

    Callers MUST pass the same ``str`` they hand to :func:`os.walk`: the
    positional comparisons rely on ``dirpath`` being built by joining onto
    ``top`` verbatim, which is what ``os.walk`` guarantees.
    """
    if not IS_MACOS:
        return dirnames
    # Positional gate FIRST: only two walk positions can prune, so every other
    # directory in a large tree returns without paying the realpath syscall
    # that the home check below costs.
    at_root = dirpath == root
    at_library = not at_root and dirpath == os.path.join(root, _LIBRARY_DIR)
    if not (at_root or at_library):
        return dirnames
    # Doubles as the "is root the home directory" test and inherits that
    # helper's failure handling (an unresolvable root prunes nothing).
    protected = tcc_protected_dirs_for_walk(root)
    if not protected:
        return dirnames
    if at_root:
        return [d for d in dirnames if d not in protected]
    return [d for d in dirnames if d in TCC_LIBRARY_WALKABLE_CHILDREN]


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------


def fchmod_safe(fd: int, mode: int) -> None:
    """Apply ``mode`` to ``fd``. Logs warning on failure.
    No-op on Windows (no POSIX perms).
    """
    if IS_POSIX:
        try:
            os.fchmod(fd, mode)
        except OSError:
            logger.warning("Cannot set permissions on fd %d", fd)


def chmod_safe(path: str | os.PathLike, mode: int) -> None:
    """Apply ``mode`` to ``path``. Logs warning on failure.
    No-op on Windows.
    """
    if IS_POSIX:
        try:
            os.chmod(path, mode)
        except OSError:
            logger.warning("Cannot set permissions on %s", path)


def _clear_readonly_and_retry(func: Any, path: str, _exc: BaseException) -> None:
    """``shutil.rmtree`` error hook: drop the read-only bit, then retry once.

    Windows checks the read-only ATTRIBUTE on the file being deleted, whereas
    POSIX consults the parent directory's write bit. So a mode-``444`` file — of
    which a git checkout is full, since loose objects are written read-only —
    cannot be unlinked on Windows even when its directory is writable.

    Only Windows gets the chmod, and only for an entry that is not a link. On
    POSIX the entry's own mode never decides whether it can be removed, so a
    chmod there buys nothing, and ``os.chmod`` follows a symlink: a tree an
    agent wrote could aim one at any file this process can reach and have its
    mode rewritten. The write bit is ADDED to the current mode, never set in
    its place, so a failed retry leaves a directory still listable.

    Only a removal (``unlink``/``rmdir``) is retried. ``rmtree`` also reports a
    directory it could not open or list (``os.open``, ``os.scandir``, ...);
    the read-only bit is not why those failed, and ``os.open(path)`` called
    with ``path`` alone raises ``TypeError``, which would unwind past
    ``rmtree_force``'s "never raises" contract. Those are logged and left.
    """
    if func not in (os.unlink, os.remove, os.rmdir):
        logger.warning("Cannot remove %s", path)
        return
    try:
        if not IS_POSIX:
            info = os.lstat(path)
            if not (stat.S_ISLNK(info.st_mode) or lstat_is_name_surrogate(info)):
                os.chmod(path, stat.S_IMODE(info.st_mode) | stat.S_IWRITE)
        func(path)
    except OSError:
        logger.warning("Cannot remove %s", path)


def rmtree_force(path: str | os.PathLike) -> bool:
    """Remove a directory tree, defeating Windows read-only files. Never raises.

    Returns True when *path* is gone afterwards.

    ``shutil.rmtree(..., ignore_errors=True)`` is the usual spelling and is WRONG
    for any tree that may contain a git checkout: on Windows the read-only loose
    objects under ``.git/objects`` refuse to unlink, ``ignore_errors`` swallows
    every one of those failures, and the caller reports success over a tree that
    is still on disk — so the project name stays taken and the next create
    answers 409.

    The return value is what lets a caller tell a real deletion from a partial
    one; the boolean is derived from the filesystem rather than from the hook,
    because a surviving file is the only thing that actually matters.
    """
    # `onexc` replaced `onerror` in 3.12 and the old name warns; this project
    # still supports 3.9+, so pick by capability rather than by version number.
    kwarg = "onexc" if sys.version_info >= (3, 12) else "onerror"
    try:
        if kwarg == "onerror":  # pragma: no cover - exercised on Python < 3.12

            def _legacy(func: Any, target: str, exc_info: Any) -> None:
                _clear_readonly_and_retry(func, target, exc_info[1])

            shutil.rmtree(path, onerror=_legacy)
        else:
            shutil.rmtree(path, onexc=_clear_readonly_and_retry)  # type: ignore[call-arg]
    except FileNotFoundError:
        # A missing ROOT is success. A nested entry can disappear during rmtree while
        # the root survives, especially through the Python <3.12 onerror path.
        return not os.path.lexists(path)
    except OSError:
        logger.warning("Cannot remove %s", path)
        return False
    return not os.path.lexists(path)


_WIN_FILE_SHARE_READ_WRITE_DELETE = 0x00000001 | 0x00000002 | 0x00000004


def open_log_file_for_tail(path: str | os.PathLike) -> int:
    """Open a binary read fd without blocking the log writer's rename/rotation.

    Windows CRT opens omit FILE_SHARE_DELETE. This separate log-only helper
    permits rotation even during a read; security pinning helpers must not.
    The caller owns the returned descriptor and must close it.
    """
    if IS_POSIX:
        return os.open(os.fspath(path), os.O_RDONLY)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        os.fspath(path),
        _WIN_GENERIC_READ,
        _WIN_FILE_SHARE_READ_WRITE_DELETE,
        None,
        _WIN_OPEN_EXISTING,
        0,
        None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        return msvcrt.open_osfhandle(  # type: ignore[attr-defined]
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        # Ownership transfers only when the CRT descriptor is created.
        kernel32.CloseHandle(handle)
        raise


def open_lock_file_for_sweep(path: "str | os.PathLike[str]") -> int:
    """Open an ``<alias>.lock`` for the sweeper to lock AND unlink while held.

    The orphan-lock sweep removes a lock file only while holding its own
    exclusive lock on it: the held lock is the proof no launcher owns the inode,
    so a launcher that still holds (or re-takes) it is left alone, and the unlink
    happens WITHOUT first releasing — releasing before the unlink reopens the
    very race the proof closes (another launcher could lock a fresh inode at the
    same name in the gap).

    * POSIX: ``os.open(O_RDWR | O_NOFOLLOW)``. ``unlink`` of a file with open
      descriptors is routine; the inode lives until the last fd closes, and the
      held ``flock`` keeps a contender out meanwhile. This is exactly the open
      the sweep was doing, named.
    * Windows: ``os.open`` routes through the CRT, which opens with
      ``FILE_SHARE_READ | FILE_SHARE_WRITE`` and **omits** ``FILE_SHARE_DELETE``
      — so while that descriptor lives, ``os.unlink`` of the same name fails with
      a sharing violation (``PermissionError``/``WinError 32``). The sweep then
      declines to remove genuine residue, because its own verification handle is
      what blocks the delete. ``CreateFileW`` with
      ``FILE_SHARE_READ_WRITE_DELETE`` lets the delete land while this handle is
      still open (and still holding the ``msvcrt`` byte-range lock), preserving
      the hold-across-unlink guarantee on both platforms. ``OPEN_EXISTING``
      never creates — the sweep only ever removes names already present. No
      ``OPEN_REPARSE_POINT``: the sweep's caller has already refused a reparse
      point at the name (``is_link_or_junction``) and re-checks identity under
      the lock, and following here would still only reach a file this same user
      owns.

    Returns the raw integer fd; the caller owns it and must ``os.close`` it.
    """
    if IS_POSIX:
        return os.open(os.fspath(path), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        os.fspath(path),
        _WIN_GENERIC_READ | _WIN_GENERIC_WRITE,
        _WIN_FILE_SHARE_READ_WRITE_DELETE,
        None,
        _WIN_OPEN_EXISTING,
        0,
        None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        return msvcrt.open_osfhandle(  # type: ignore[attr-defined]
            handle, os.O_RDWR | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        # Ownership transfers only when the CRT descriptor is created.
        kernel32.CloseHandle(handle)
        raise


def path_volume_is_remote(path: str | os.PathLike) -> bool | None:
    """Is *path* on a NETWORK volume? True, False for local, None for unknown.

    The Windows half of "which kind of filesystem holds this file", for a caller
    that already answers the question from ``/proc/mounts`` or ``statfs``
    elsewhere: Windows exposes no mount table, so the volume ROOT's drive type
    is the source (:func:`windows_acl.volume_is_remote`, ``GetDriveTypeW``),
    which reports a UNC root and a mapped network drive alike as remote.

    None -- never False -- for every case where nothing was established: off
    Windows, where the caller has its own mount-table source and must not read
    this as "local"; a volume the OS reports as ``DRIVE_UNKNOWN`` or
    ``DRIVE_NO_ROOT_DIR``; and a failed query. Root-only, so it costs no SMB
    round trip and is safe for a path that does not exist yet.
    """
    if not IS_WINDOWS:
        return None
    try:
        return windows_acl.volume_is_remote(path)
    except (windows_acl.AclUnavailable, OSError, ValueError):
        logger.debug("could not classify the volume holding a path", exc_info=True)
        return None


# Hook-script extensions treated as runnable on Windows (where there is no
# POSIX execute bit). A hook is a small script Kiro Crew shells out to; on
# Windows its runnability is decided by extension + interpreter at exec time,
# not a filesystem bit.
_WINDOWS_RUNNABLE_HOOK_SUFFIXES = (".sh", ".ps1", ".cmd", ".bat", ".py", ".exe")


def is_executable_file(
    path: str | os.PathLike,
    *,
    platform_name: str | None = None,
) -> bool:
    """Should this file be treated as a runnable hook/script for *this* platform?

    POSIX: the file must carry an execute bit (``os.access(X_OK)``) — unchanged
    behavior, so a ``chmod -x`` still disables a hook.

    Windows: there is NO execute bit (every file reports the same mode), so a
    POSIX X_OK check would reject EVERY hook and silently disable the whole
    kiro-hooks autoimport (observed: no preToolUse hook ever registered). On
    Windows we instead accept a regular file whose extension is a known script
    type (``.sh``/``.ps1``/``.cmd``/``.bat``/``.py``/``.exe``) — runnability is
    determined when Kiro Crew actually invokes it, not by a meaningless bit.
    ``platform_name`` lets cross-platform discovery apply the target platform's
    rules instead of the host's. A Windows host cannot represent POSIX execute
    bits, so an existing regular file is accepted for an explicit POSIX target.
    """
    try:
        if not os.path.isfile(path):
            return False
        target_is_windows = IS_WINDOWS if platform_name is None else platform_name == "win32"
        if target_is_windows:
            suffix = os.path.splitext(str(path))[1].lower()
            return suffix in _WINDOWS_RUNNABLE_HOOK_SUFFIXES
        return not IS_POSIX or os.access(path, os.X_OK)
    except OSError:
        return False


def is_readonly_filesystem(path: Path) -> bool:
    """Confirm a Linux readonly mount; absence or probe failure grants nothing."""
    if sys.platform != "linux":
        return False
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def ensure_owner_rwx_dirs(root: str | os.PathLike) -> None:
    """OR owner read/write/execute onto *root* and every directory below it.

    ``shutil.copytree`` preserves source modes verbatim. A copy made from a
    hardened install source can therefore lack owner write or execute (for
    example, mode ``0o455``). Creating a file needs a writable AND searchable
    parent, so callers that write a marker into a fresh copy run this first.

    Only directories are touched, and only by adding ``S_IRWXU``. Copied files
    remain exactly as shipped, so a file-mode customization still diverges
    skill fingerprints. On Windows ``os.chmod`` honours only the read-only
    flag: adding owner write clears it, while the extra owner bits are a no-op.
    Never raises: a directory this cannot repair surfaces as the original
    ``PermissionError`` at the caller's write site, which every caller handles.
    """

    def _add_owner_rwx(entry: str) -> None:
        try:
            mode = os.lstat(entry).st_mode
            if stat.S_ISDIR(mode) and mode & stat.S_IRWXU != stat.S_IRWXU:
                os.chmod(entry, stat.S_IMODE(mode) | stat.S_IRWXU)
        except OSError:
            logger.debug("could not add owner rwx to %s", entry, exc_info=True)

    top = os.fspath(root)
    if is_link_or_junction(top):
        return
    _add_owner_rwx(top)
    for dirpath, dirnames, _filenames in os.walk(top):
        for dname in list(dirnames):
            entry = os.path.join(dirpath, dname)
            if is_link_or_junction(entry):
                # os.walk(followlinks=False) skips POSIX symlinks, but a
                # Windows junction lstats as a plain directory and WOULD be
                # descended -- and chmodded THROUGH, touching its target
                # tree. Prune both so the walk never leaves *root*.
                dirnames.remove(dname)
                continue
            _add_owner_rwx(entry)


def claude_code_managed_settings_dir() -> Path:
    """The directory Claude Code reads managed (organisation policy) settings from on this OS.

    It holds ``managed-settings.json`` and the ``managed-settings.d/`` drop-in directory.
    """
    if IS_MACOS:
        return Path("/Library/Application Support/ClaudeCode")
    if IS_WINDOWS:
        return Path("C:\\Program Files\\ClaudeCode")
    return Path("/etc/claude-code")

"""Filesystem operations whose meaning differs by platform.

The no-replace rename and directory publication, and the macOS TCC folders a
home-rooted walk prunes.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import contextlib
import ctypes.util
import errno
import os
import platform
from typing import Any

from kiro_crew.platform_compat_native import IS_LINUX, IS_MACOS, IS_WINDOWS

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

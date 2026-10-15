"""Cross-platform compatibility shims for POSIX-only APIs.

All helpers are safe no-ops (or best-effort fallbacks) on Windows where
the underlying syscall does not exist. Callers should use these instead
of raw ``os.*`` / ``fcntl`` / ``signal`` calls for anything that is
POSIX-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes.util
import enum  # noqa: F401
import errno
import functools  # noqa: F401
import hashlib  # noqa: F401
import importlib
import io  # noqa: F401
import ipaddress  # noqa: F401
import logging
import ntpath  # noqa: F401
import os
import pathlib
import platform  # noqa: F401
import re  # noqa: F401
import shutil
import signal  # noqa: F401
import site  # noqa: F401
import stat
import struct  # noqa: F401
import subprocess
import sys
import sys as _sys
import threading  # noqa: F401
import time
import types
from asyncio import subprocess as aio_subprocess
from ctypes import wintypes  # type aliases only; imports cleanly on every platform
from pathlib import Path
from typing import (  # noqa: F401
    TYPE_CHECKING,
    Any,
    Callable,
    Coroutine,
    Iterator,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
)

# Loaded with this module, so each owner is complete before the mirror at the end of
# this file indexes the family and before any reader can reach it.
from kiro_crew import platform_lock_compat, platform_owner_compat, windows_acl  # noqa: F401
from kiro_crew.executors import subprocess_executor  # noqa: F401
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)


# The carved owners' names this module binds itself: the ones its own code reads, the
# ones the lock and owner-only modules import from here when they run, and the few a
# test reads from this module's namespace or patches with unpacked keyword arguments.
# One per line, so a line diff of the split keeps the code around them as unchanged
# context.
from kiro_crew.platform_compat_files import publish_dir_noreplace  # noqa: E402,F401
from kiro_crew.platform_compat_identity import _windows_process_handle_identity  # noqa: E402
from kiro_crew.platform_compat_identity import process_matches  # noqa: E402
from kiro_crew.platform_compat_kill import kill_pid  # noqa: E402,F401
from kiro_crew.platform_compat_liveness import process_thread_count  # noqa: E402
from kiro_crew.platform_compat_native import _ACCESS_HONOURS_EFFECTIVE_IDS  # noqa: E402,F401
from kiro_crew.platform_compat_native import _PROCESS_QUERY_LIMITED_INFORMATION  # noqa: E402
from kiro_crew.platform_compat_native import IS_LINUX  # noqa: E402
from kiro_crew.platform_compat_native import IS_MACOS  # noqa: E402
from kiro_crew.platform_compat_native import IS_POSIX  # noqa: E402
from kiro_crew.platform_compat_native import IS_WINDOWS  # noqa: E402
from kiro_crew.platform_compat_native import _close_process_handle  # noqa: E402
from kiro_crew.platform_compat_native import _JobObjectExtendedLimitInformation  # noqa: E402
from kiro_crew.platform_compat_native import _MemoryStatusEx  # noqa: E402
from kiro_crew.platform_compat_native import _open_process_query_handle  # noqa: E402
from kiro_crew.platform_compat_native import _ProcessMemoryCounters  # noqa: E402
from kiro_crew.platform_compat_native import _ThreadEntry32  # noqa: E402
from kiro_crew.platform_compat_native import _TokenUser  # noqa: E402,F401
from kiro_crew.platform_compat_native import _windows_last_error  # noqa: E402
from kiro_crew.platform_compat_proctable import _darwin_process_cpu_nanos  # noqa: E402
from kiro_crew.platform_compat_proctable import _darwin_sysctl_handle  # noqa: E402
from kiro_crew.platform_compat_proctable import _proc_children  # noqa: E402
from kiro_crew.platform_compat_proctable import _proc_cpu_jiffies  # noqa: E402
from kiro_crew.platform_compat_proctable import _proc_status_rss_kb  # noqa: E402
from kiro_crew.platform_compat_winproc import _open_process_termination_handle  # noqa: E402
from kiro_crew.platform_compat_winproc import close_process_handle  # noqa: E402
from kiro_crew.platform_compat_winproc import descendant_termination_handles  # noqa: E402
from kiro_crew.platform_compat_wintree import _PENDING_WINDOWS_TREE_CLEANUPS  # noqa: E402
from kiro_crew.platform_compat_wintree import _PENDING_WINDOWS_TREE_CLEANUPS_LOCK  # noqa: E402
from kiro_crew.platform_compat_wintree import _WINDOWS_CAPTURE_MEASURED_VERSIONS  # noqa: E402
from kiro_crew.platform_compat_wintree import _WINDOWS_TREE_ADMISSIONS  # noqa: E402
from kiro_crew.platform_compat_wintree import _PendingWindowsTreeCleanup  # noqa: E402

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


def symlink_or_junction(target: str | os.PathLike, link: str | os.PathLike) -> None:
    """Create a directory link at *link* pointing to *target*.

    POSIX: a plain ``os.symlink``.

    Windows: ``os.symlink`` needs SeCreateSymbolicLinkPrivilege — held only by
    an elevated process or one running with Developer Mode on — so it raises
    ``OSError WinError 1314`` for the ordinary non-admin user, silently breaking
    every feature that links a directory into place (app skills, etc.). A
    directory JUNCTION needs no privilege, is followed transparently by reads
    and by ``os.path.realpath`` / ``Path.resolve()`` (so app-root containment
    checks still hold), and is the standard no-elevation substitute. Fall back
    to it, and only if the symlink attempt fails, so the POSIX-identical path is
    unchanged where symlinks are permitted.

    ``target`` must be an existing directory on Windows (junctions are
    directory-only). Raises if neither a symlink nor a junction can be made.
    """
    if IS_POSIX:
        os.symlink(str(target), str(link))
        return
    try:
        # target_is_directory=True is required on Windows: a directory link made
        # without it is a FILE-type symlink pointing at a directory, which is not
        # traversable. Ignored on POSIX. This helper only ever links directories.
        os.symlink(str(target), str(link), target_is_directory=True)
    except OSError:
        # No symlink privilege (the common non-admin case) — use a junction,
        # which requires none. _winapi.CreateJunction exists on all supported
        # CPython builds on Windows.
        import _winapi

        # _winapi.CreateJunction is Windows-only; typeshed omits it on the POSIX
        # stub, so ignore the attr error mypy raises when checking on Linux.
        _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


# os.path.isjunction is 3.12+; fall back to the reparse-tag check below on the
# 3.10/3.11 interpreters this project still supports, or the junction guard is a
# silent no-op there. Constants mirror CPython's own isjunction.
_ISJUNCTION = getattr(os.path, "isjunction", None)
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def _is_junction_fallback(path: str | os.PathLike) -> bool:
    """``os.path.isjunction`` for Python 3.10/3.11, which lack it.

    A junction is a reparse point (``FILE_ATTRIBUTE_REPARSE_POINT``) whose tag is
    ``IO_REPARSE_TAG_MOUNT_POINT``. Both fields are Windows-only additions to
    ``os.stat_result``, so their absence off Windows makes this False — correct,
    since junctions do not exist there. ``follow_symlinks=False``: the question
    is what THIS name is, not what it points at.
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except (OSError, ValueError, TypeError):
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    if not attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return getattr(info, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT


#: ``IsReparseTagNameSurrogate``: set on a reparse tag whose entry stands for
#: ANOTHER name (a symlink, a junction), clear on one that stores its own data in
#: place (a cloud-files placeholder, a dedup or container-isolation directory).
_IO_REPARSE_TAG_NAME_SURROGATE = 0x20000000


def lstat_is_name_surrogate(info: os.stat_result) -> bool:
    """True when an ``lstat`` result is a Windows link to another name.

    A tree walk that must stay inside its tree skips exactly these: ``lstat``
    reports a junction as a plain directory, so ``S_ISDIR`` alone walks into
    its target. A reparse directory WITHOUT the name-surrogate bit holds its
    own contents, which a walk must still see -- skipping it would hide writes
    made inside it. ``st_reparse_tag`` is Windows-only, so this is False on
    POSIX, where ``lstat`` already reports a symlink as a link.
    """
    return bool(getattr(info, "st_reparse_tag", 0) & _IO_REPARSE_TAG_NAME_SURROGATE)


def strip_extended_length_prefix(path: Path) -> Path:
    r"""*path* without Windows' extended-length prefix; unchanged elsewhere.

    ``Path.resolve()`` on a file that another thread is replacing at that exact
    moment comes back as ``\\?\C:\...``: ``ntpath.realpath`` drops the prefix
    only after re-checking the stripped spelling, and that re-check fails when
    the file has just been swapped out. The directory resolved separately comes
    back plain, so a containment comparison reads the prefix alone as an escape.

    The fold is LEXICAL and must stay that way: resolving again here could bless
    a redirect, which is the thing the caller is trying to detect. Both
    extended spellings are handled -- ``\\?\UNC\host\share`` becomes the
    ordinary ``\\host\share`` -- so both sides of a comparison are spelled the
    same way whatever ``realpath`` returned.
    """
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        return type(path)("\\\\" + text[8:])
    if text.startswith("\\\\?\\"):
        return type(path)(text[4:])
    return path


def is_link_or_junction(path: str | os.PathLike) -> bool:
    """True if *path* is a symlink OR (on Windows) a directory junction.

    ``os.path.islink`` returns False for a junction, so a caller that only
    checks ``islink`` would treat a junction as a real directory and
    ``rmtree`` THROUGH it, destroying the target's contents. Pair with
    :func:`unlink_link_or_junction` to remove one safely.
    """
    if os.path.islink(path):
        return True
    if _ISJUNCTION is not None:
        try:
            return bool(_ISJUNCTION(path))
        except (OSError, ValueError):
            return False
    return _is_junction_fallback(path)


def first_linked_ancestor(path: str | os.PathLike) -> str | None:
    r"""First ANCESTOR of *path* that is a symlink/junction, or None.

    :func:`is_link_or_junction` tests one path, so a caller that checks only
    the path it was handed still resolves through a linked PARENT. That gap is
    not cosmetic on Windows: an ancestor link whose target is ``\\host\share``
    turns the first innocent-looking ``is_dir()`` on a LOCAL-looking path into
    an outbound SMB connection that authenticates as this process. A lexical
    UNC screen cannot catch it, because the path being probed is not itself
    UNC-shaped -- only the link's target is.

    Ancestors are tested ROOT-FIRST and the walk stops at the first hit. That
    order is the safety property, not a detail: each ``lstat`` runs only after
    every ancestor above it is known not to be a link, so the probe itself
    never traverses one. Returns the offending ancestor for logging; callers
    deciding whether to REJECT should not put it in a user-facing message,
    since which ancestor is a link is filesystem layout the caller supplied a
    path to guess at.

    The leaf is deliberately excluded -- pair this with
    :func:`is_link_or_junction` on the path itself.
    """
    for ancestor in reversed(pathlib.Path(os.fspath(path)).parents):
        if is_link_or_junction(ancestor):
            return str(ancestor)
    return None


def unlink_link_or_junction(path: str | os.PathLike) -> None:
    """Remove a symlink or directory junction WITHOUT touching its target.

    A symlink is removed with ``unlink``; a Windows junction is a directory
    reparse point removed with ``rmdir`` (which unlinks the junction itself,
    never the target it points at).
    """
    if os.path.islink(path):
        os.unlink(path)
        return
    is_junction = _ISJUNCTION(path) if _ISJUNCTION is not None else _is_junction_fallback(path)
    if is_junction:
        os.rmdir(path)
        return
    # Neither — let the caller's own logic handle a real file/dir.
    os.unlink(path)


#: ``CreateFileW`` arguments for :func:`pin_directory`. ``BACKUP_SEMANTICS`` is
#: what lets a directory be opened at all; ``OPEN_REPARSE_POINT`` opens the
#: reparse point ITSELF instead of following it, so a junction planted at the
#: name is seen for what it is rather than silently traversed. The share mode
#: deliberately omits ``FILE_SHARE_DELETE``: that omission is the pin.
_WIN_GENERIC_READ = 0x80000000
_WIN_GENERIC_WRITE = 0x40000000
_WIN_FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
#: ``FILE_SHARE_READ`` alone. Omitting ``FILE_SHARE_WRITE`` as well makes Windows
#: refuse any OTHER process's attempt to open the object for writing while this
#: descriptor lives, which is the one thing a descriptor cannot do on POSIX: there
#: a held descriptor fixes WHICH inode a name reaches and says nothing about that
#: inode's contents, so a same-UID process can still rewrite the bytes in place.
#: Callers that hand a filename to a child and need the bytes to be the bytes they
#: checked ask for this; a read by the child is still allowed, which is what makes
#: it usable for exactly that case.
_WIN_FILE_SHARE_READ = 0x00000001
_WIN_OPEN_EXISTING = 3
_WIN_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WIN_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WIN_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WIN_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


def pin_directory(path: str | os.PathLike) -> int:
    """Open *path* as a directory and return a descriptor that PINS it.

    For code that must let a child process write to ``<dir>/<name>`` by path:
    the string is re-resolved at the child's open, so a same-UID watcher that
    renames ``<dir>`` away and plants a link at its name between our check and
    that open redirects the write. Holding the directory open closes the
    window differently on each platform:

    * Windows: the handle is opened without ``FILE_SHARE_DELETE``, and a
      directory with such a handle open can be neither renamed nor deleted --
      nor can any directory above it -- for as long as the handle lives. The
      open itself refuses to follow a reparse point, so a junction already
      sitting at the name fails here instead of being pinned in its target's
      place.
    * POSIX: ``O_DIRECTORY | O_NOFOLLOW`` refuses a symlink or a file at the
      name, and the returned descriptor is usable as ``dir_fd`` so the caller's
      own opens resolve against the directory it inspected. Holding it does not
      block a rename (POSIX has no such lock); callers rely on the sandbox mask
      for that and use the descriptor for their own opens.

    Refuses anything that is not a real directory: a file or a Windows reparse
    point raises ``NotADirectoryError``; a POSIX symlink fails with whichever
    of ``ENOTDIR`` / ``ELOOP`` the kernel reports for ``O_DIRECTORY |
    O_NOFOLLOW``. Release with ``os.close``.
    """
    if IS_POSIX:
        return os.open(os.fspath(path), pinned_dir_flags())

    fd = _win_open_without_following(path)
    try:
        attrs = getattr(os.fstat(fd), "st_file_attributes", 0)
        if not attrs & _WIN_FILE_ATTRIBUTE_DIRECTORY or attrs & _WIN_FILE_ATTRIBUTE_REPARSE_POINT:
            raise NotADirectoryError(errno.ENOTDIR, "not a real directory", os.fspath(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


def pinned_dir_flags() -> int:
    """POSIX open flags for a pinned directory: read-only, a directory, never a link.

    ``O_NOFOLLOW`` is part of the requirement rather than an extra: without it each
    open would happily traverse whatever link sits at the name, which is the hole the
    pin exists to close. Called rather than captured at import, because the
    Windows-simulation tests delete ``os.O_NOFOLLOW`` at runtime and a frozen constant
    would keep offering a flag the platform does not have.

    This is the same triple ``pinned_fs.dir_flags()`` publishes, and it is spelled
    again here because of the import direction, not by preference: ``pinned_fs``
    imports THIS module for its own Windows no-reparse open, so this layer cannot
    import it back. ``test_pinned_directory.py`` asserts the two are equal, so the
    copy cannot drift silently -- a flag added on either side reddens that test.
    """
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


# Levels a pin CHAIN may descend below the directory it was opened on. The
# recursion this class invites is bounded here rather than at each call site,
# because the hazard belongs to the chain: every level holds a descriptor for as
# long as the level below it is in use, so a planted chain costs both stack frames
# and file descriptors, and the inputs are untrusted content (an agent-writable
# artifact store, a hand-built snapshot archive). Unbounded, a deep chain turns a
# delete into a swallowed RecursionError/EMFILE whose only visible effect is the
# root refusing to go with ENOTEMPTY, and an import into an unclassified crash.
# The refusal is an OSError from :meth:`PinnedDirectory.child`, so each caller's
# EXISTING error policy classifies it -- warn-and-continue where residue is
# already reported, propagate where the operation must fail closed.
#
# 64 is ``skills._PROJECT_SKILL_MAX_DEPTH``'s number and its reasoning: past any
# legitimate tree, far short of the interpreter's recursion limit and of any
# descriptor soft limit. A caller whose domain is shallower states its own tighter
# cap (``skills._PENDING_SCRIPT_MAX_DEPTH`` is 8) and reaches it first.
PINNED_TREE_MAX_DEPTH = 64


class PinnedDirectory:
    """Act on the ENTRIES of the directory this was opened on, never on its name.

    :func:`pin_directory` hands back a descriptor; this is the operations that go
    with it, because holding the descriptor is only half of what a caller needs. A
    screen and the act that follows it must reach the same object, and the two
    platforms reach that property by OPPOSITE routes:

    * POSIX: every call is ``dir_fd=``-relative, so the descriptor IS the
      directory whatever its name now resolves to. It must be that way, because
      the pin does NOT stop a rename here -- a name re-resolved after the screen
      is exactly the hole.
    * Windows: there are no ``dir_fd`` operations at all (``os.open``,
      ``os.listdir``, ``os.unlink`` and ``os.rmdir`` are in neither
      ``os.supports_fd`` nor ``os.supports_dir_fd``), so every call goes by path
      -- and that is sound only because the pin makes the path stable: the handle
      is opened without ``FILE_SHARE_DELETE``, so while it lives this directory
      and every ancestor refuse a rename and a delete.

    So neither route works on the other platform, and a caller written in terms of
    one of them is broken on the other. That asymmetry is the whole reason this
    exists rather than each site branching on ``IS_POSIX`` itself.

    A child is opened THROUGH the parent, and the parent stays pinned while the
    child is in use, so a chain of these pins the whole path. :meth:`child`
    refuses a link at the name on both platforms, which is what makes a
    screen-then-descend sequence safe: the refusal happens in the open, not in a
    check before it.

    Use it as a context manager; the descriptor is closed on exit. Removing the
    directory ITSELF is the parent's job (``parent.rmdir(name)``), both because a
    pinned directory on Windows cannot be removed while the handle lives and
    because a by-name removal is the thing this class exists to avoid.

    A chain is bounded: :meth:`child` refuses past ``PINNED_TREE_MAX_DEPTH`` levels
    below the directory the chain started on. See that constant for why the bound
    lives here and not in each caller.
    """

    __slots__ = ("_depth", "_fd", "_path")

    def __init__(self, fd: int, path: str, depth: int = 0) -> None:
        self._fd = fd
        self._path = path
        self._depth = depth

    @property
    def path(self) -> str:
        """The path this was opened on -- for MESSAGES, not for operations."""
        return self._path

    def __enter__(self) -> PinnedDirectory:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        os.close(self._fd)

    def names(self) -> list[str]:
        """The entry names, read through the pin."""
        if IS_POSIX:
            return os.listdir(self._fd)
        return os.listdir(self._path)

    def names_bounded(self, limit: int) -> list[str] | None:
        """Up to *limit* entry names, or None when the directory holds more than that.

        The counterpart to :meth:`names` for a directory whose contents are written by
        an agent. Scanning stops at ``limit + 1``, so an attacker-sized directory is
        never materialized in one allocation the way ``sorted(os.listdir(...))`` would:
        that eager list is itself the exhaustion, spent BEFORE any budget the caller
        applies afterwards could refuse it.

        None means "over budget", deliberately not a truncated list -- a caller handed
        the first *limit* names would act on a partial view of the directory while
        believing it saw all of it. Refusing is the only honest answer.
        """
        out: list[str] = []
        with os.scandir(self._fd if IS_POSIX else self._path) as scanner:
            for entry in scanner:
                out.append(entry.name)
                if len(out) > limit:
                    return None
        return out

    def _lstat(self, name: str) -> os.stat_result | None:
        """``lstat`` of *name* in this directory, or None if it cannot be read."""
        try:
            if IS_POSIX:
                return os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            return os.lstat(os.path.join(self._path, name))
        except OSError:
            return None

    def is_link(self, name: str) -> bool:
        """Whether *name* is a symlink or (on Windows) a directory junction."""
        if IS_POSIX:
            info = self._lstat(name)
            return info is not None and stat.S_ISLNK(info.st_mode)
        return is_link_or_junction(os.path.join(self._path, name))

    def is_dir(self, name: str) -> bool:
        """Whether *name* is a real directory -- a link answers False, not its target's shape."""
        if self.is_link(name):
            return False
        info = self._lstat(name)
        return info is not None and stat.S_ISDIR(info.st_mode)

    def unlink(self, name: str) -> None:
        """Remove the non-directory *name*. A link is removed, never its target."""
        if IS_POSIX:
            os.unlink(name, dir_fd=self._fd)
            return
        unlink_link_or_junction(os.path.join(self._path, name))

    def rmdir(self, name: str) -> None:
        """Remove the EMPTY directory *name* in this directory."""
        if IS_POSIX:
            os.rmdir(name, dir_fd=self._fd)
            return
        os.rmdir(os.path.join(self._path, name))

    def child(self, name: str) -> PinnedDirectory:
        """Pin the child directory *name*, reached through this pin.

        Raises ``NotADirectoryError`` for a link or a non-directory at the name --
        the refusal is the open itself, so there is no window between deciding the
        name is a real directory and having it open.

        Raises ``OSError`` with ``ENAMETOOLONG`` past ``PINNED_TREE_MAX_DEPTH``
        levels below where the chain started, BEFORE opening anything, so a planted
        chain cannot spend another frame or another descriptor. A caller that
        dispatches on what is at the name -- the shape every consumer here uses --
        re-raises this for a real directory, which is the intended outcome: too deep
        is a refusal to be classified by the caller, never a link to be removed.
        """
        depth = self._depth + 1
        if depth > PINNED_TREE_MAX_DEPTH:
            raise OSError(
                errno.ENAMETOOLONG,
                f"pinned traversal deeper than {PINNED_TREE_MAX_DEPTH} levels",
                os.path.join(self._path, name),
            )
        if IS_POSIX:
            fd = os.open(name, pinned_dir_flags(), dir_fd=self._fd)
            return PinnedDirectory(fd, os.path.join(self._path, name), depth)
        child_path = os.path.join(self._path, name)
        return PinnedDirectory(pin_directory(child_path), child_path, depth)

    def child_if_real_dir(self, name: str) -> PinnedDirectory | None:
        """Pin the child directory *name*, or None when *name* is not a real directory.

        The screen-then-descend fallback, in one place. Three callers need it and each
        one does something DIFFERENT with the answer, so what is shared is the
        question, not the action: two of them remove the entry, one deliberately
        leaves it alone. Hoisting the question and leaving the action at the call site
        is what keeps the difference visible.

        The subtle part is here rather than copied: when :meth:`child` refuses, the
        dispatch asks what is at the name NOW and never keys on the exception class.
        Linux answers ENOTDIR for ``O_DIRECTORY | O_NOFOLLOW`` on a symlink but ELOOP
        is equally permitted, and the Windows open raises ``NotADirectoryError`` for a
        reparse point; a caller keying on one class silently takes the wrong branch
        wherever the kernel picks another.

        A real directory that still refuses to open RE-RAISES, which is also how the
        ``ENAMETOOLONG`` refusal :meth:`child` makes past ``PINNED_TREE_MAX_DEPTH``
        reaches the caller: a tree nested past the bound fails the operation instead of
        being treated as an entry to delete.
        """
        try:
            return self.child(name)
        except OSError:
            if self.is_link(name) or not self.is_dir(name):
                return None
            raise

    def _open_file(self, name: str) -> int:
        """Open the regular file *name* in this directory for reading.

        The leaf counterpart to :meth:`child`, and the reason a caller can both
        JUDGE and READ an entry in one traversal instead of screening names and
        re-resolving them afterwards. A link at the name is refused by the open
        itself, so there is no check-to-read window for an adversary to aim at.

        ``O_NONBLOCK`` is set so the OPEN cannot block: a FIFO at the name would
        otherwise wait for a writer, and a hung read is a lost test RUN rather than
        a failed one. It has no effect on a regular file. What was opened is then a
        question for ``fstat`` on the descriptor, which is why :meth:`_read_bytes`
        asserts there rather than predicting here.

        Raises ``OSError``/``NotADirectoryError`` for a link, ``IsADirectoryError``
        for a directory. Release the descriptor with ``os.close``.
        """
        if IS_POSIX:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            return os.open(name, flags, dir_fd=self._fd)
        return open_file_no_reparse(os.path.join(self._path, name), nonblocking=True)

    def _read_bytes(self, name: str, max_bytes: int | None = None) -> bytes:
        """The bytes of the regular file *name*, read through this pin.

        Refuses a link at the name, as :meth:`_open_file` does, so the bytes come
        from the entry that was inspected rather than from wherever its name points
        by the time the read happens. Every further question is asked of the
        DESCRIPTOR, which is a fact about what was opened rather than a prediction
        about what a later open would find:

        * not a REGULAR file -- a directory, a device, a FIFO -- is refused.
        * ``st_nlink > 1`` is refused. A hardlink is invisible to every
          path-based guard because it shares its target's inode while carrying its
          own name, so a sensitive file hardlinked into a tree the caller believes
          it owns would otherwise be read out through it. This is the refusal
          ``pinned_fs.refuse_hardlink_alias`` makes for that module's write and copy
          paths, applied to a READ because what this serves is an agent-written tree
          going out through an API.
        * over *max_bytes*, when given, is refused with ``EFBIG`` -- and refused from
          that same ``fstat`` rather than from a stat taken before the open, so the
          size belongs to the file actually being read. The READ is bounded too, not
          just the size check: a file that grows between the two stops at the cap
          instead of being followed upward.
        """
        fd = self._open_file(name)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise OSError(
                    errno.EINVAL,
                    "not a private regular file",
                    os.path.join(self._path, name),
                )
            if max_bytes is not None and info.st_size > max_bytes:
                raise OSError(
                    errno.EFBIG,
                    f"larger than the {max_bytes}-byte cap",
                    os.path.join(self._path, name),
                )
            chunks: list[bytes] = []
            held = 0
            while True:
                block = os.read(fd, 1 << 16)
                if not block:
                    return b"".join(chunks)
                held += len(block)
                if max_bytes is not None and held > max_bytes:
                    raise OSError(
                        errno.EFBIG,
                        f"grew past the {max_bytes}-byte cap while being read",
                        os.path.join(self._path, name),
                    )
                chunks.append(block)
        finally:
            os.close(fd)

    def read_text(self, name: str, encoding: str = "utf-8", max_bytes: int | None = None) -> str:
        """The text of the regular file *name*, read through this pin.

        The whole read surface: a link at the name is refused by the open, and the
        descriptor's own ``fstat`` rejects a non-regular entry, a hardlink, and
        anything over *max_bytes* when the caller sets one, so the bytes come from the
        entry that was inspected and cannot exceed what the caller agreed to hold. The
        layers under this one are private because nothing outside needs them -- a
        caller reaching for a raw descriptor here would be operating outside the pin
        this class exists to hold.

        Newlines are translated exactly as ``Path.read_text`` translates them. That
        is not cosmetic: a caller swapping a by-path ``read_text`` for this must not
        begin serving ``\\r\\n`` to its own consumers on Windows, where the bytes on
        disk carry it and the old read silently normalised it away.
        """
        text = self._read_bytes(name, max_bytes).decode(encoding)
        return text.replace("\r\n", "\n").replace("\r", "\n")


def pinned_directory(path: str | os.PathLike) -> PinnedDirectory:
    """Open *path* as a :class:`PinnedDirectory`. Refuses a link at the name."""
    target = os.fspath(path)
    return PinnedDirectory(pin_directory(target), target)


def _win_open_without_following(path: str | os.PathLike, *, deny_write: bool = False) -> int:
    """``CreateFileW`` *path* for reading, opening a reparse point INSTEAD of following it.

    Shared by :func:`pin_directory` and :func:`open_file_no_reparse` so the two do
    not carry separate copies of the same security-critical flags. What each of
    them then asserts about the descriptor differs; how the object is reached must
    not.

    *deny_write* drops ``FILE_SHARE_WRITE`` from the share mode as well, so no other
    process may open the object for writing while this descriptor lives. A caller
    that must hand a child process a FILENAME needs it: the child re-resolves the
    name, and a descriptor alone fixes only which inode that name reaches, so
    without this a same-UID process rewrites the bytes in place and the child sends
    them. Reads by the child are still permitted. Not the default, because for a
    DIRECTORY handle it would also refuse other processes' writes into it.

    ``OPEN_REPARSE_POINT`` is the whole point: a junction or symlink at the name is
    opened AS ITSELF, so the caller sees what is really there and the target is
    never touched. That is what makes the refusal atomic rather than a check
    followed by an open -- and on Windows the difference is not academic, because
    resolving a reparse point aimed at a UNC share is itself an outbound SMB
    authentication. ``BACKUP_SEMANTICS`` is what allows a directory to be opened at
    all and is harmless on a file. The share mode omits ``FILE_SHARE_DELETE``: while
    this descriptor lives, the object cannot be renamed or deleted -- and for a
    directory, neither can anything above it.

    The handle is wrapped in a CRT descriptor so ``os.fstat`` can read the
    attributes of what was actually opened and ``os.close`` can release it.

    ``O_BINARY`` is part of that wrapping, not a detail. A CRT descriptor in TEXT
    mode translates CRLF and stops at the first ``0x1A``, and a caller reading
    with a raw ``os.read`` gets that translation: on a body of 55 bytes holding
    one ``0x1A``, such a descriptor yields 12. ``os.fdopen(fd, "rb")`` hides the
    difference because ``io.FileIO`` sets the mode itself, so only a raw-read
    caller is exposed -- which is precisely the caller that copies media files.
    Naming the flag here makes the descriptor's contract the same for both.
    """
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
    handle = kernel32.CreateFileW(
        os.fspath(path),
        _WIN_GENERIC_READ,
        _WIN_FILE_SHARE_READ if deny_write else _WIN_FILE_SHARE_READ_WRITE,
        None,
        _WIN_OPEN_EXISTING,
        _WIN_FILE_FLAG_BACKUP_SEMANTICS | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    return msvcrt.open_osfhandle(  # type: ignore[attr-defined]
        handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
    )


def open_file_no_reparse(
    path: str | os.PathLike,
    *,
    nonblocking: bool = False,
    links_only: bool = False,
    deny_write: bool = False,
) -> int:
    """Open a regular FILE for reading, refusing a reparse point at the final name.

    The leaf counterpart to :func:`pin_directory`. ``pin_directory`` freezes the
    ancestors so the path cannot be re-pointed underneath a caller; this one settles
    the last component, and it has to do so in the SAME operation that opens it --
    an ``is_symlink()`` (or ``lstat``) check followed by ``os.open`` is a
    check-to-open window, and an adversary that can plant the link chooses when.

    * POSIX: ``O_NOFOLLOW`` already refuses a link at the final component; this is
      exactly the open the callers were doing, named.
    * Windows: there is no ``O_NOFOLLOW`` -- ``getattr(os, "O_NOFOLLOW", 0)`` is 0,
      so ``os.open`` FOLLOWS a reparse point at the name. ``CreateFileW`` with
      ``FILE_FLAG_OPEN_REPARSE_POINT`` opens the reparse point itself instead, and
      the attribute read off the resulting descriptor is therefore a fact about what
      was opened, not a prediction about what a later open will find. Refused with
      ``ELOOP``, matching what POSIX reports for the same shape.

    Refuses a directory with ``IsADirectoryError`` (POSIX reports ``EISDIR`` from the
    read, Windows from ``os.open``; the two are made to agree here). Release the
    descriptor with ``os.close``.

    ``nonblocking`` adds ``O_NONBLOCK`` on POSIX so a caller can reject a FIFO
    with ``fstat`` before an open waits for a writer. Regular file reads are
    unaffected. Windows has no POSIX FIFO open; its handle checks stay the same.

    ``links_only`` narrows the Windows refusal to a reparse point that stands for
    another name (:func:`win_fd_is_link`), so a regular file carrying a
    cloud-files or dedup tag opens as the file it is. POSIX is unaffected: a
    link is the only thing ``O_NOFOLLOW`` refuses there.

    ``deny_write`` asks that no other process be able to open the file for writing
    while this descriptor lives. It is honoured on WINDOWS ONLY, and the asymmetry
    is the point rather than an omission: POSIX has no mandatory locking, so the
    request cannot be expressed there and is silently not made. A caller whose
    safety depends on it must therefore not treat a POSIX descriptor as carrying
    it -- on POSIX the equivalent protection comes from removing the writer, not
    from refusing its open.
    """
    if IS_POSIX:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if nonblocking:
            flags |= getattr(os, "O_NONBLOCK", 0)
        return os.open(os.fspath(path), flags)

    fd = _win_open_without_following(path, deny_write=deny_write)
    try:
        attrs = getattr(os.fstat(fd), "st_file_attributes", 0)
        if _win_reparse_refused(fd, attrs, links_only=links_only):
            raise OSError(errno.ELOOP, "reparse point at the final component", os.fspath(path))
        if attrs & _WIN_FILE_ATTRIBUTE_DIRECTORY:
            raise IsADirectoryError(errno.EISDIR, "is a directory", os.fspath(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


#: ``CreateFileW`` arguments only :func:`win_open_no_reparse` uses. A directory is
#: opened for ``LIST_DIRECTORY`` (data access, so it takes part in sharing) with
#: read-only sharing (``_WIN_FILE_SHARE_READ``, defined above); a file read-write,
#: open-or-create, sharing read and write.
_WIN_FILE_LIST_DIRECTORY = 0x00000001
_WIN_OPEN_ALWAYS = 4
#: ``FILE_INFO_BY_HANDLE_CLASS.FileAttributeTagInfo``.
_WIN_FILE_ATTRIBUTE_TAG_INFO = 9


def win_fd_is_link(fd: int) -> bool:  # pragma: no cover - Windows
    """Windows: True unless the object open on *fd* is positively not a link.

    Reads the reparse tag of the opened object itself through
    ``GetFileInformationByHandleEx(FileAttributeTagInfo)``, because ``os.fstat``
    leaves ``st_reparse_tag`` at zero. A tag with the name-surrogate bit (a
    symbolic link, a junction) stands for another name, as
    :func:`lstat_is_name_surrogate` reads it from an ``lstat``; a tag without it
    (a cloud-files placeholder, a deduplicated file) holds its own data. Fails
    CLOSED: a tag that cannot be read counts as a link.
    """

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    info = _AttributeTagInfo()
    try:
        handle = msvcrt.get_osfhandle(fd)  # type: ignore[attr-defined]
    except OSError:
        return True
    if not kernel.GetFileInformationByHandleEx(
        handle, _WIN_FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)
    ):
        return True
    return bool(info.ReparseTag & _IO_REPARSE_TAG_NAME_SURROGATE)


def _win_reparse_refused(fd: int, attributes: int, *, links_only: bool) -> bool:
    """Whether a Windows open that did not follow a reparse point must refuse *fd*."""
    if not attributes & _WIN_FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return not links_only or win_fd_is_link(fd)


def win_open_no_reparse(
    path: str | os.PathLike, *, directory: bool, links_only: bool = False
) -> int:  # pragma: no cover
    """Windows: open the object at *path* ITSELF, never through a reparse point.

    A file is opened read-write and created when absent; a directory is opened
    for listing and must already exist. ``FILE_FLAG_OPEN_REPARSE_POINT`` opens a
    junction or symlink at the name as itself, and the descriptor's own ``fstat``
    then refuses it with ``ELOOP`` -- so nothing is created, read or written
    through it. The share mode omits ``FILE_SHARE_DELETE``: while the descriptor
    lives, the object cannot be renamed or deleted. A directory variant refuses a
    non-directory with ``NotADirectoryError``. ``links_only`` narrows the refusal
    to a reparse point that stands for another name, as for
    :func:`open_file_no_reparse`. The native handle is transferred to a CRT
    descriptor only on success; release it with ``os.close``.
    """
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(
        os.fspath(path),
        _WIN_FILE_LIST_DIRECTORY if directory else _WIN_GENERIC_READ | _WIN_GENERIC_WRITE,
        _WIN_FILE_SHARE_READ if directory else _WIN_FILE_SHARE_READ_WRITE,
        None,
        _WIN_OPEN_EXISTING if directory else _WIN_OPEN_ALWAYS,
        _WIN_FILE_FLAG_BACKUP_SEMANTICS | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        fd = msvcrt.open_osfhandle(  # type: ignore[attr-defined]
            handle, (os.O_RDONLY if directory else os.O_RDWR) | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        info = os.fstat(fd)
        if _win_reparse_refused(fd, getattr(info, "st_file_attributes", 0), links_only=links_only):
            raise OSError(errno.ELOOP, "reparse point at the final component", os.fspath(path))
        if directory and not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", os.fspath(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_create_no_reparse(path: str | os.PathLike, mode: int = 0o600) -> int:
    """Open *path* read-write, creating it when absent, never through a link at the name.

    The read-write, creating counterpart to :func:`open_file_no_reparse`, for a
    caller that writes a file it also locks. POSIX:
    :func:`open_create_or_existing` with ``O_NOFOLLOW``, so a link at the final
    component is refused with ``ELOOP`` and never created through, and the open
    is race-safe against a sibling creator. Windows:
    :func:`win_open_no_reparse` with ``links_only``, refusing a link or junction
    at the name with ``ELOOP`` the same way while a regular file carrying a
    cloud-files or dedup tag still opens. Never truncates. What was opened is still the
    caller's to check (``fstat``): a directory, FIFO or socket at the name can
    open or fail with its own errno. Release the descriptor with ``os.close``.
    """
    if IS_POSIX:
        return platform_lock_compat.open_create_or_existing(
            path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), mode
        )
    return win_open_no_reparse(path, directory=False, links_only=True)


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
# POSIX execute bit). A hook is a small script KiroCrew shells out to; on
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
    determined when KiroCrew actually invokes it, not by a meaningless bit.
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


def _is_windows_store_python_stub(path: str) -> bool:
    """True if *path* is the Microsoft Store ``python`` App Execution Alias stub.

    On Windows, ``shutil.which("python"/"python3")`` resolves a 0-byte reparse
    point under ``%LOCALAPPDATA%\\Microsoft\\WindowsApps`` when no real CPython
    is installed/on PATH. Spawning it does NOT run Python — it prints "Python was
    not found; run without arguments to install from the Microsoft Store…" and
    exits 9009. Detect it by its WindowsApps location so callers never execute
    it (which otherwise floods logs on every probe). Mirrors install.ps1's
    Find-RealPython, which rejects the same stub.
    """
    if not IS_WINDOWS:
        return False
    norm = path.replace("/", "\\").lower()
    return "\\microsoft\\windowsapps\\" in norm


def find_python_interpreter(reject: Optional[Callable[[str], bool]] = None) -> str | None:
    """Resolve a real CPython >= 3.12 interpreter, or None.

    Single source of truth for "where is a usable system python" on every
    platform. Prefers the exact tested version (``python3.12``), then bare
    ``python``/``python3``, with free-threaded-prone ``python3.13`` LAST so a
    usable 3.12 wins first. Rejects
    Brazil-path/build interpreters and — critically on Windows — the Microsoft
    Store alias stub (see :func:`_is_windows_store_python_stub`): running that
    stub is what emits the "Python was not found" nag, so we must never spawn it.

    ``reject`` is an optional predicate run against each >= 3.12 candidate path;
    return True to skip it and FALL THROUGH to the next candidate (not abort).
    Callers with extra constraints the shared resolver can't express — e.g. the
    STT prereq probe needs pip and a non-free-threaded build — pass it here so a
    single unusable interpreter does not short-circuit the whole search.

    Returns the interpreter path, or None when none is usable. Callers that just
    need *an* interpreter to re-exec KiroCrew itself should prefer
    ``sys.executable``; this is for finding a SEPARATE system python (e.g. STT /
    whisper installs that must not land in the gateway's venv).
    """
    names = (
        ("python3.12", "python", "python3")
        if IS_WINDOWS
        else ("python3.12", "python3", "python3.13")
    )
    for name in names:
        p = shutil.which(name)
        if not p or "brazil-path" in p or "build/private" in p:
            continue
        if _is_windows_store_python_stub(p):
            continue
        try:
            # -I isolates the probe from the caller's environment: without it,
            # ``site`` imports any ``sitecustomize.py`` found on the caller's
            # PYTHONPATH at child startup, and that module can monkeypatch
            # ``sys.version_info`` to steer WHICH interpreter this loop selects.
            # Because -I implies -E (PYTHON* env vars ignored), the UTF-8 pin
            # must ride the argv as ``-X utf8``, matching
            # ``dep_sync._probe_interpreter``.
            out = subprocess.check_output(
                [p, "-I", "-X", "utf8", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                timeout=5,
                stderr=subprocess.DEVNULL,
                **UTF8_TEXT,
            ).strip()
            major, _, minor = out.partition(".")
            if not (int(major) == 3 and int(minor) >= 12):
                continue
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        # >= 3.12 and resolvable. Let the caller veto it (e.g. free-threaded /
        # no pip) and keep searching the remaining candidates.
        if reject is not None and reject(p):
            continue
        return p
    return None


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------

#: ``task_info`` flavor selector for ``mach_task_basic_info``
#: (``<mach/task_info.h>``). Chosen over the legacy ``TASK_BASIC_INFO`` because
#: its sizes are 64-bit, so a footprint above 4 GiB is not truncated.
_MACH_TASK_BASIC_INFO = 20


class _MachTimeValue(ctypes.Structure):
    """``time_value_t`` (``<mach/time_value.h>``).

    Never read; present only so the fields after it in
    :class:`_MachTaskBasicInfo` land at the offsets the kernel writes them to.
    """

    _fields_ = [("seconds", ctypes.c_int32), ("microseconds", ctypes.c_int32)]


class _MachTaskBasicInfo(ctypes.Structure):
    """``mach_task_basic_info`` (``<mach/task_info.h>``), in kernel order.

    ``resident_size`` is the task's CURRENT resident footprint in bytes and
    falls when pages are released; ``resident_size_max`` is the high-water mark
    that never falls. Reading the wrong one of the two is exactly the bug this
    layout exists to avoid, so both are named rather than indexed.

    Module scope is load-bearing: ``ctypes.POINTER(T)`` memoises T in a
    module-level dict inside ctypes that is never evicted, so declaring this
    inside the probe would pin a fresh pair of type objects on every call — and
    this probe is polled by the dashboard's system-metrics endpoint.
    """

    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("resident_size_max", ctypes.c_uint64),
        ("user_time", _MachTimeValue),
        ("system_time", _MachTimeValue),
        ("policy", ctypes.c_int),
        ("suspend_count", ctypes.c_int),
    ]


#: ``task_info`` takes and returns a count in ``natural_t``-sized elements
#: (``MACH_TASK_BASIC_INFO_COUNT``). Derived from the layout so it cannot go
#: stale if a field is added above.
_MACH_TASK_BASIC_INFO_COUNT = ctypes.sizeof(_MachTaskBasicInfo) // ctypes.sizeof(ctypes.c_int)


def _scale_ru_maxrss(ru_maxrss: int) -> int:
    """``ru_maxrss`` -> bytes. macOS reports bytes; Linux and other POSIX KiB.

    The unit differs by platform with nothing in the value to tell them apart,
    so every reader of ``ru_maxrss`` goes through here.
    """
    return ru_maxrss if sys.platform == "darwin" else ru_maxrss * 1024


def _ru_maxrss_bytes() -> int | None:
    """Peak (high-water) RSS in bytes from ``getrusage``, or None on failure.

    POSIX only, and NOT the Linux reader: there ``execve`` folds the pre-exec
    image's high-water mark into the new process's ``ru_maxrss`` (``fs/exec.c``
    ``exec_mmap`` -> ``setmax_mm_hiwater_rss``), so a gateway started from a
    large parent -- a launcher, a test runner, a bloated shell -- reports that
    parent's peak as its own for life. :func:`_linux_peak_rss_bytes` is the
    Linux source; :func:`_posix_peak_rss_bytes` picks. This is a **peak**, not
    a live reading: ``ru_maxrss`` never decreases for the life of the process.
    """
    try:
        return _scale_ru_maxrss(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError, AttributeError):
        return None


#: Where Linux reports THIS process's own peak resident size: ``VmHWM`` in
#: ``/proc/self/status`` is the current ``mm``'s high-water mark, which a fresh
#: image starts from zero, unlike ``ru_maxrss`` (see :func:`_ru_maxrss_bytes`).
_LINUX_STATUS_PATH = Path("/proc/self/status")
_LINUX_PEAK_RSS_FIELD = "VmHWM:"
#: The highest ``VmHWM`` this process has read. The kernel answers ``VmHWM``
#: with ``max(hiwater_rss, live RSS)`` but folds the live figure into
#: ``hiwater_rss`` only at unmap/exit, from per-thread counters it syncs in
#: batches, so a reading taken while a mapping is live can sit a few hundred KiB
#: above what the next reading, after the unmap, reports (measured 136-376 KiB
#: on a 128 MiB mapping). A peak that never decreases is the contract, so the
#: reader keeps its own floor.
_LINUX_PEAK_RSS_FLOOR = 0


def _peak_rss_from_status(status: str) -> int | None:
    """Parse ``VmHWM`` out of a ``/proc/<pid>/status`` text, in bytes.

    The kernel prints the field as ``VmHWM:\\t   11432 kB`` -- always kB, so any
    other shape (a missing field, a unit that is not kB, a non-numeric value) is
    unreadable rather than a guess. ``kiro_crew.pdf_extract_child`` carries the
    same parser by design: that module keeps its imports minimal because it runs
    under a capped address space, so it does not import this one.
    """
    for line in status.splitlines():
        if not line.startswith(_LINUX_PEAK_RSS_FIELD):
            continue
        parts = line.split()
        if len(parts) == 3 and parts[2] == "kB" and parts[1].isdigit():
            return int(parts[1]) * 1024
        return None
    return None


def _linux_peak_rss_bytes() -> int | None:
    """This process's OWN peak RSS in bytes from ``VmHWM``, or None if unreadable.

    Monotonic across calls (see :data:`_LINUX_PEAK_RSS_FLOOR`); an unreadable
    status file is None even when a floor exists, so a failure reads as one.
    """
    global _LINUX_PEAK_RSS_FLOOR
    try:
        # ``errors="replace"``: the ``Name:`` line is this process's raw comm.
        peak = _peak_rss_from_status(
            _LINUX_STATUS_PATH.read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, ValueError):
        return None
    if peak is None:
        return None
    if peak > _LINUX_PEAK_RSS_FLOOR:
        _LINUX_PEAK_RSS_FLOOR = peak
    return _LINUX_PEAK_RSS_FLOOR


def _posix_peak_rss_bytes() -> int | None:
    """Peak RSS in bytes for THIS process, or None where it cannot be read.

    Linux reads its own ``VmHWM``; an unreadable ``/proc`` is None, never the
    inherited ``ru_maxrss`` -- a wrong number is worse than a missing one in a
    figure an operator uses to size a host. Every other POSIX platform reads
    ``ru_maxrss`` in its unit.
    """
    if sys.platform.startswith("linux"):
        return _linux_peak_rss_bytes()
    return _ru_maxrss_bytes()


def _linux_current_rss_bytes() -> int | None:
    """Current RSS in bytes from ``/proc/self/statm``, or None if unreadable.

    Field 1 of ``statm`` is the resident page count — the same quantity
    ``/proc/self/status``'s ``VmRSS`` and ``ps -o rss=`` report, so the
    dashboard's figure reconciles with what an operator measures by hand.
    """
    try:
        fields = Path("/proc/self/statm").read_text().split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def _macos_current_rss_bytes() -> int | None:
    """Current RSS in bytes via Mach ``task_info``, or None on any failure.

    ``proc_rss_bytes_for_pid`` has no ctypes-only route for an ARBITRARY pid
    (it needs a task port it cannot obtain), but ``mach_task_self()`` hands out
    a port for THIS task unconditionally, so the self-only reading below is
    always available — no subprocess, which matters because the macOS app
    sandbox can deny spawning ``ps``.

    Returns ``resident_size`` (what ``ps -o rss=`` reports), not
    ``phys_footprint``: every other platform branch here reports RSS, and the
    payload field it feeds is named for RSS. Activity Monitor's "Memory" column
    is the phys_footprint variant and will read somewhat differently; that is a
    separate accounting question from the peak-vs-current bug.
    """
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
    except OSError:
        return None  # not macOS / libSystem unavailable
    try:
        libc.mach_task_self.restype = ctypes.c_uint
        libc.task_info.restype = ctypes.c_int
        libc.task_info.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(_MachTaskBasicInfo),
            ctypes.POINTER(ctypes.c_uint),
        ]
        info = _MachTaskBasicInfo()
        count = ctypes.c_uint(_MACH_TASK_BASIC_INFO_COUNT)
        # mach_task_self() returns a port name owned by the task itself, not a
        # fresh send right, so unlike mach_host_self() it must NOT be deallocated.
        kern_return = libc.task_info(
            libc.mach_task_self(),
            _MACH_TASK_BASIC_INFO,
            ctypes.byref(info),
            ctypes.byref(count),
        )
    except (AttributeError, OSError, ValueError):
        return None
    if kern_return != 0:  # non-zero kern_return_t -> failure
        return None
    return int(info.resident_size)


def _windows_memory_counters() -> "_ProcessMemoryCounters | None":
    """psapi ``PROCESS_MEMORY_COUNTERS`` for this process, or None on failure."""
    try:

        psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # argtypes/restype are load-bearing on 64-bit: without them ctypes
        # defaults GetCurrentProcess's return to a 32-bit int and TRUNCATES the
        # pseudo-handle, so GetProcessMemoryInfo fails and this returned 0 for
        # every process — silently disabling the watchdog's RSS-recycle ceiling.
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        if psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return counters
        return None
    except Exception:
        return None


def proc_rss_bytes() -> int:
    """Return this process's CURRENT resident set size in bytes, or 0 on failure.

    "Current" is the contract, not an implementation detail: this feeds an
    operator-facing live memory figure, so it must FALL when the gateway
    releases memory and must reconcile with ``ps -o rss=``.

    - Linux: ``/proc/self/statm`` resident pages.
    - macOS: Mach ``task_info(MACH_TASK_BASIC_INFO).resident_size``.
    - Windows: ``GetProcessMemoryInfo().WorkingSetSize``.
    - Last resort on POSIX only: the process's own peak (``VmHWM`` on Linux,
      ``getrusage(RUSAGE_SELF).ru_maxrss`` elsewhere), which never decreases.
      It is here so an unavailable ``libSystem`` or a ``statm`` that will not
      parse still yields an order-of-magnitude number rather than 0, and it
      over-reports by construction — see :func:`proc_peak_rss_bytes` for the
      peak as a deliberate reading.
    """
    if IS_POSIX:
        current = (
            _macos_current_rss_bytes() if sys.platform == "darwin" else _linux_current_rss_bytes()
        )
        if current is not None:
            return current
        return _posix_peak_rss_bytes() or 0
    counters = _windows_memory_counters()
    return 0 if counters is None else int(counters.WorkingSetSize)


HEAP_TRIM_INTERVAL_SECONDS = 10 * 60.0
HEAP_TRIM_RSS_THRESHOLD_BYTES = 1536 * 1024 * 1024
HEAP_TRIM_LOG_THRESHOLD_BYTES = 16 * 1024 * 1024
# Must remain below the heartbeat interval.  A queued or wedged default-executor
# worker forfeits this maintenance pass instead of starving the liveness beat.
HEAP_TRIM_TIMEOUT_SECONDS = 2.0


def _malloc_trim() -> bool:
    """Ask glibc to return wholly-free heap pages, or no-op elsewhere."""
    try:
        libc = ctypes.CDLL(None)
        getattr(libc, "gnu_get_libc_version")
        trim = libc.malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return bool(trim(0))
    except (AttributeError, OSError):
        return False


# glibc's ``M_ARENA_MAX`` mallopt parameter (malloc.h). Glibc otherwise creates
# up to 8 x cores per-thread arenas, each reserving 64 MiB heaps that keep freed
# memory per thread: on a 32-core host a pod gateway under dashboard load grew
# ~90 such heaps holding ~2.2 GB. A/B on paired seeded pods:
# a cap of 4 cut peak RSS ~4.0 -> ~3.1 GB and heap RSS ~2.2 -> ~1.1 GB with no
# throughput or p95 regression; a cap of 2 saved slightly more but cost ~3%
# throughput and +5-7% p95 on /api/sessions under saturation, so 4 is the default.
_M_ARENA_MAX = -8
GATEWAY_MALLOC_ARENA_MAX = 4
#: Operator override for the gateway's arena cap: a positive integer replaces the
#: default, ``0`` (or any non-positive value) leaves glibc's own default alone.
MALLOC_ARENA_MAX_ENV = "KIROCREW_MALLOC_ARENA_MAX"


def _glibc_mallopt() -> Callable[[int, int], int] | None:
    """glibc's ``mallopt``, or ``None`` on musl / non-glibc / no libc."""
    try:
        libc = ctypes.CDLL(None)
        libc.gnu_get_libc_version  # noqa: B018 - glibc probe; musl lacks it
        fn = libc.mallopt
        fn.argtypes = [ctypes.c_int, ctypes.c_int]
        fn.restype = ctypes.c_int
        return fn
    except (AttributeError, OSError):
        return None


class MallocArenaCap(NamedTuple):
    """What :func:`limit_malloc_arenas` did.

    ``applied`` is the cap known to be in force, else 0. ``requested`` is the
    cap handed to ``mallopt`` (0 when none was), and ``threads`` the OS thread
    count just before that call. ``requested`` without ``applied`` means glibc
    accepted the value but it may not hold: see :func:`limit_malloc_arenas`.
    """

    applied: int = 0
    requested: int = 0
    threads: int | None = None


def limit_malloc_arenas(
    *,
    environ: Mapping[str, str] | None = None,
    mallopt: Callable[[int, int], int] | None = None,
    thread_count: Callable[[], int | None] | None = None,
) -> MallocArenaCap:
    """Cap glibc malloc arenas for THIS process.

    Called first thing in the gateway command, so it holds for every launcher
    (systemd unit, pod, detached start, Dev Fleet re-exec) without depending on
    each one exporting ``MALLOC_ARENA_MAX`` before exec. ``mallopt`` is used
    rather than the env var because glibc reads that variable once, at its first
    allocation -- long before any Python code runs.

    glibc fixes its arena limit for good the first time a thread needs a new
    arena while more than 8 exist; ``mallopt(M_ARENA_MAX)`` after that still
    returns success but changes nothing. A single-threaded process has only the
    main arena, so the cap is reported as ``applied`` only when this process
    had exactly one OS thread at the call. With more threads (or an unreadable
    count) the value is still handed to glibc, but reported as ``requested``
    only, because whether it holds cannot be seen.

    An operator's own ``MALLOC_ARENA_MAX`` or ``glibc.malloc.arena_max`` tunable
    already took effect at process start and wins: nothing is changed. No-op on
    every non-Linux platform and on a non-glibc libc. Never raises.
    """
    if not IS_LINUX:
        return MallocArenaCap()
    env = os.environ if environ is None else environ
    if env.get("MALLOC_ARENA_MAX", "").strip():
        return MallocArenaCap()
    if "glibc.malloc.arena_max" in env.get("GLIBC_TUNABLES", ""):
        return MallocArenaCap()
    value = GATEWAY_MALLOC_ARENA_MAX
    raw = env.get(MALLOC_ARENA_MAX_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = GATEWAY_MALLOC_ARENA_MAX
    if value <= 0:
        return MallocArenaCap()
    try:
        fn = mallopt or _glibc_mallopt()
        if fn is None:
            return MallocArenaCap()
        threads = thread_count() if thread_count else process_thread_count(os.getpid())
        if fn(_M_ARENA_MAX, value) != 1:
            return MallocArenaCap(threads=threads)
        return MallocArenaCap(value if threads == 1 else 0, value, threads)
    except Exception:  # noqa: BLE001 - an allocator hint must never stop the boot
        return MallocArenaCap()


def log_malloc_arena_cap(result: MallocArenaCap) -> None:
    """Log *result* once logging is configured; silent when nothing was asked."""
    if result.applied:
        logger.info("glibc malloc arenas capped at %d", result.applied)
    elif result.requested:
        logger.warning(
            "glibc malloc arena cap %d may not hold: %s OS threads were already "
            "running when it was set",
            result.requested,
            "an unknown number of" if result.threads is None else result.threads,
        )


def trim_heap_if_needed(
    *,
    rss_reader: Callable[[], int | None] | None = None,
    trimmer: Callable[[], bool] | None = None,
) -> int:
    """Return bytes released from a large Linux gateway heap.

    A high threshold avoids allocator-wide work on healthy gateways. This must
    use Linux's current RSS directly: :func:`proc_rss_bytes` deliberately falls
    back to peak RSS when procfs is unavailable, which would turn one historic
    spike into repeated trim attempts. Unsupported libc and probe failures are
    harmless because reclamation is optional.
    """
    if not IS_LINUX:
        return 0
    try:
        read_rss = rss_reader or _linux_current_rss_bytes
        before = read_rss()
        if before is None:
            return 0
        if before < HEAP_TRIM_RSS_THRESHOLD_BYTES:
            return 0
        if not (trimmer or _malloc_trim)():
            return 0
        after = read_rss()
        if after is None:
            return 0
    except Exception:  # noqa: BLE001 - optional maintenance must not stop the heartbeat
        return 0
    return max(0, before - after)


class HeapTrimMaintainer:
    """Self-gate bounded, best-effort heap reclamation for the gateway.

    The heartbeat calls :meth:`maybe_trim` on every tick. Cadence and in-flight
    ownership live here so the heartbeat stays cadence-free. A timed-out worker
    may continue running because Python cannot cancel native work already in a
    thread; ``_inflight`` prevents another from being submitted until that
    worker returns. If cancellation wins before the worker starts, maintenance
    remains disabled for this object, which is the safe failure mode.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        trim: Callable[[], int] = trim_heap_if_needed,
    ) -> None:
        self._clock = clock
        self._trim = trim
        self._next_trim = clock() + HEAP_TRIM_INTERVAL_SECONDS
        self._inflight = False

    async def maybe_trim(self) -> int:
        """Return bytes released, or zero when skipped, timed out, or failed."""
        try:
            if not IS_LINUX:
                return 0
            now = self._clock()
            if now < self._next_trim or self._inflight:
                return 0
            self._next_trim = now + HEAP_TRIM_INTERVAL_SECONDS
            self._inflight = True
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._run_trim),
                    timeout=HEAP_TRIM_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, TimeoutError):
                logger.debug("gateway heap trim timed out; maintenance pass skipped")
                return 0
        except Exception:  # noqa: BLE001 - maintenance must not stop the heartbeat
            logger.debug("gateway heap trim failed", exc_info=True)
            return 0

    def _run_trim(self) -> int:
        """Worker-thread wrapper that releases the single in-flight slot."""
        try:
            return self._trim()
        finally:
            self._inflight = False


def proc_peak_rss_bytes() -> int:
    """Return this process's PEAK resident set size in bytes, or 0 on failure.

    The high-water mark since the process started: it never decreases, which is
    what makes it useful for diagnosing a transient spike that a live reading
    has already forgotten — and useless as the live reading itself. Linux reads
    this process's own ``/proc/self/status`` ``VmHWM`` (``ru_maxrss`` there is
    inherited across ``execve`` from the parent, see :func:`_ru_maxrss_bytes`);
    other POSIX reads ``getrusage(RUSAGE_SELF).ru_maxrss``; Windows reads
    ``GetProcessMemoryInfo().PeakWorkingSetSize``.
    """
    if IS_POSIX:
        return _posix_peak_rss_bytes() or 0
    counters = _windows_memory_counters()
    return 0 if counters is None else int(counters.PeakWorkingSetSize)


# Per-process fd directories, in preference order: /proc/self/fd (Linux),
# /dev/fd (macOS/BSD; also present on Linux as a symlink to the former).
_FD_DIRS = ("/proc/self/fd", "/dev/fd")


def count_open_fds() -> int | None:
    """Return this process's open file descriptor count, or None if unavailable.

    The one shared probe behind both the ``kirocrew.process.open_fds`` gauge
    (``metrics/process_gauges.py``) and gatewayd's zombie-diagnostic
    ``fd_count`` snapshot field, so the two figures cannot drift apart.

    - POSIX: entry count of ``/proc/self/fd`` (Linux) or ``/dev/fd``
      (macOS/BSD), minus one because enumerating the directory opens one fd
      itself (the directory handle) — callers want the steady state.
    - Windows: ``GetProcessHandleCount`` — kernel HANDLEs, not fds, so the
      semantics are platform-dependent (callers document this). Returned raw:
      the query opens no extra handle, so no correction applies.

    Returns None when no probe works; each caller maps its own sentinel.
    """
    for fd_dir in _FD_DIRS:
        try:
            return max(0, len(os.listdir(fd_dir)) - 1)
        except OSError:
            continue
    if not IS_WINDOWS:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        # argtypes/restype are load-bearing on 64-bit: without them ctypes
        # defaults GetCurrentProcess's return to a 32-bit int and TRUNCATES the
        # pseudo-handle (see _windows_memory_counters).
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        handle_count = wintypes.DWORD()
        if kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(handle_count)):
            return int(handle_count.value)
        return None
    except Exception:
        return None


def proc_rss_bytes_for_pid(pid: int) -> int | None:
    """Resident set size (bytes) of an ARBITRARY *pid*, or None if unavailable.

    Unlike :func:`proc_rss_bytes` (self only), this measures another process so
    the watchdog can sum a spawned agent's whole tree. Linux reads
    ``/proc/<pid>/statm``; Windows opens the PID and calls
    ``GetProcessMemoryInfo``; macOS has no ctypes-only per-pid path, so it
    returns None and the caller keeps its ``ps`` route.
    """

    if sys.platform == "linux":
        try:
            fields = Path(f"/proc/{pid}/statm").read_text().split()
            # statm resident pages * page size.
            return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None
    if not IS_WINDOWS:
        return None
    try:

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


# --- /proc process-subtree sampling ----------------------------------------
#
# ONE walk for its two callers, ``mcp_gateway.pool`` and ``subagent``: a
# per-caller BFS over ``/proc/<pid>/task/<tid>/children`` with its own ``256``
# ceiling would let a fix to either policy reach only one surface.
# :func:`proc_subtree_sample` is the
# single entry point for BOTH, built from the per-process ``/proc`` reads in
# ``kiro_crew.platform_compat_proctable`` -- private to the walk, except
# :func:`read_proc_stat`, the stat reader for callers elsewhere. Pure stdlib: on
# a host without ``/proc`` every
# access raises ``OSError`` and each reading degrades to its own sentinel.
#
# NOT the only way this repository walks a process tree, and deliberately so.
# ``session_pid._build_child_map`` sums a session's tree from a full ``/proc``
# scan of every process's ``stat`` ``PPid`` field, precisely because the
# ``children`` file this walk reads needs ``CONFIG_PROC_CHILDREN`` and is
# documented as reliable only for frozen tasks -- for a live task it can return
# an incomplete child set and silently drop a descendant subtree from the sum.
# That trade is the right one there (an under-counted tree would make the RSS
# watchdog no-op) and the wrong one here: these two callers sample per backend
# and per live agent on a timer, where a whole-machine scan per sample is the
# larger cost, and an under-count degrades a displayed number rather than
# disabling a protection. Reconsidering the method for these two surfaces is a
# behaviour change to figures users already read, not part of this
# consolidation -- but it is now ONE place to reconsider instead of two.

#: Upper bound on processes walked in one subtree sample. A real tree is tiny
#: (a launcher plus a handful of workers); the cap only guards against a
#: pathological or looping ``/proc`` graph.
#:
#: It bounds THIS WALK's work; it is not a display ceiling for a count, and a
#: surface that already enumerates its own tree does not adopt it. Truncating a
#: displayed count at this number would hand the card a plain integer no
#: consumer can tell from a complete one, where ``procs``/``matched`` reserve
#: ``None`` for "unmeasurable". A walk that needs bounding elsewhere wants a
#: budget that yields ``None``, not a silent truncation.
_SUBTREE_MAX_PROCS = 256


def proc_cpu_nanos_for_pid(pid: int) -> int | None:
    """Total (user+system) CPU time of *pid* in NANOSECONDS, or None.

    The per-pid, cross-platform counterpart to :func:`proc_cpu_seconds`, which
    can only measure the CALLING process. A caller samples this twice and
    consumes the DELTA as evidence that a process is doing work; ``None`` means
    no per-pid counter is readable on this host, which is evidence of neither
    work nor death.

    - Linux: ``_proc_cpu_jiffies`` scaled by ``SC_CLK_TCK``. Never None — an
      unreadable pid reads 0 there, which a delta correctly sees as flat.
    - macOS: ``libproc.proc_pidinfo(PROC_PIDTASKINFO)``, already nanoseconds.
    - Windows: ``GetProcessTimes`` kernel+user (100-ns units) over a query-only
      handle.
    - Any other platform: None.

    Root pid ONLY — deliberately no subtree walk here: this probe runs on a read
    loop's cadence and Windows has no child enumeration cheap enough for that. A
    caller that needs the subtree total uses :func:`proc_subtree_sample` on Linux,
    or walks :func:`darwin_child_pids` and sums this per pid on macOS (the
    liveness oracle's darwin backend does exactly that).
    """
    if type(pid) is not int or pid <= 0:
        return None
    if IS_LINUX:
        try:
            ticks = os.sysconf("SC_CLK_TCK") or 100
        except (AttributeError, OSError, ValueError):
            ticks = 100
        return _proc_cpu_jiffies(pid) * (1_000_000_000 // ticks)
    if IS_MACOS:
        return _darwin_process_cpu_nanos(pid)
    if not IS_WINDOWS:
        return None
    handle = _open_process_query_handle(pid)
    if handle is None:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        creation = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            wintypes.HANDLE(handle),
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None

        def _hundred_ns(value: "wintypes.FILETIME") -> int:
            return (value.dwHighDateTime << 32) | value.dwLowDateTime

        return (_hundred_ns(kernel) + _hundred_ns(user)) * 100
    except Exception:
        return None
    finally:
        _close_process_handle(handle)


class SubtreeSample(NamedTuple):
    """Every reading one subtree walk can produce, from ONE frontier.

    Each field keeps its own sentinel, because the readings are unmeasurable in
    different ways and collapsing any of them into zero is a bug class in its own
    right:

    * ``rss_kb`` — summed KiB, or ``-1`` when the root pid's own status is
      unreadable (it is gone, or the host has no ``/proc``).
    * ``jiffies`` — summed utime+stime clock ticks; an unreadable pid contributes
      0, since a *delta* of jiffies is what a caller consumes.
    * ``procs`` / ``matched`` — how many processes the subtree carries, and how
      many of their command lines contain one of the caller's ``needles``.
      ``None`` means UNMEASURABLE, never zero: rendering "0 processes" for a live
      tree would be a lie, so a surface renders ``None`` as an em dash instead.
    * ``pss_kb`` — summed proportional set size (``smaps_rollup``), or ``-1``
      when not requested or the root's is unreadable (non-Linux included).
    """

    rss_kb: int
    jiffies: int
    procs: Optional[int]
    matched: Optional[int]
    pss_kb: int = -1


def _proc_pss_kb(pid: int) -> int:
    """Proportional set size (KiB) of one *pid* from ``smaps_rollup``, or -1."""
    try:
        with open(f"/proc/{pid}/smaps_rollup", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return -1


def proc_subtree_sample(
    pid: Optional[int],
    *,
    rss: bool = True,
    jiffies: bool = True,
    counts: bool = False,
    needles: tuple[str, ...] = (),
    pss: bool = False,
) -> SubtreeSample:
    """Walk *pid*'s process subtree ONCE and return every requested reading.

    A tracked process is frequently a thin launcher whose real memory lives in a
    child, so every reading describes the whole subtree rather than the root pid
    alone.

    The point of one pass is not only the fewer ``/proc`` reads: separate readers
    run at separate instants, so a process that exits between them is counted by
    one and missed by another. Reading every metric off a single frontier is what
    makes "the same set of processes" true of the *result* and not merely of the
    walk rules.

    ``rss`` / ``jiffies`` / ``counts`` let a caller skip the per-process reads it
    does not want, so sharing this walk costs each caller what its own walk cost:
    a CPU-only caller pays no ``status`` read, and an RSS-only caller pays no
    ``stat`` read. A skipped reading comes back as its own sentinel. When nothing
    remains to accumulate — RSS unreadable at the root, no jiffies, nothing
    countable — the descendants are not walked at all.

    ``counts`` is Linux-only (it matches command lines via
    :func:`process_matches`) and yields ``(None, None)`` elsewhere. ``pss`` is
    Linux-only too (``smaps_rollup``, kernel 4.14+): summed PSS charges each
    shared page to its sharers proportionally, so it is what the tree costs the
    host once, where summed VmRSS counts the code pages a tree of node /
    MCP-server processes shares once per process. ``smaps_rollup`` walks page
    tables, so it costs far more than a ``status`` read: for an occasional
    one-off reading, never a periodic sweep.

    Coverage caveat for a new caller: the walk reads
    ``/proc/<pid>/task/<tid>/children``, which needs ``CONFIG_PROC_CHILDREN`` and
    is documented as reliable only for frozen tasks, so a live tree can come back
    short. That is acceptable for the periodic per-process figures the two
    callers display; a reading that must not under-count (the RSS watchdog's
    recycle decision) uses ``session_pid._build_child_map`` instead, which pays a
    full ``/proc`` scan for completeness.

    Blocking: reads a handful of ``/proc`` entries per process in the subtree, so
    it belongs on an executor thread, never on the event loop.
    """
    if not pid:
        return SubtreeSample(-1, 0, None, None)
    # The counts share RSS's liveness probe: a root pid whose own status cannot
    # be read has nothing to attribute, so there is nothing to count either.
    own_rss = _proc_status_rss_kb(pid) if (rss or counts) else -1
    countable = counts and IS_LINUX and own_rss >= 0
    rss_total = own_rss if (rss and own_rss >= 0) else -1
    total_jiffies = _proc_cpu_jiffies(pid) if jiffies else 0
    procs = 1
    matched = 1 if countable and process_matches(pid, needles) else 0
    pss_total = _proc_pss_kb(pid) if (pss and IS_LINUX) else -1
    if rss_total < 0 and not jiffies and not countable and pss_total < 0:
        # Nothing a descendant could add — do not pay for the walk.
        return SubtreeSample(-1, total_jiffies, None, None)
    seen = {pid}
    frontier = [pid]
    while frontier and len(seen) < _SUBTREE_MAX_PROCS:
        nxt: list[int] = []
        for parent in frontier:
            for child in _proc_children(parent):
                if child in seen:
                    continue
                seen.add(child)
                if rss_total >= 0:
                    kb = _proc_status_rss_kb(child)
                    if kb > 0:
                        rss_total += kb
                if jiffies:
                    total_jiffies += _proc_cpu_jiffies(child)
                if pss_total >= 0:
                    kb = _proc_pss_kb(child)
                    if kb > 0:
                        pss_total += kb
                if countable:
                    procs += 1
                    if process_matches(child, needles):
                        matched += 1
                nxt.append(child)
        frontier = nxt
    if not countable:
        return SubtreeSample(rss_total, total_jiffies, None, None, pss_total)
    return SubtreeSample(rss_total, total_jiffies, procs, matched, pss_total)


def proc_rss_tree_mb_for_pid(pid: int) -> float | None:
    """Sum RSS (MiB) of *pid* and its LINEAGE-VALIDATED Windows descendants.

    Windows-only; returns None on other platforms (callers keep their /proc or
    ps route). The naive way to sum a Windows tree — walk Toolhelp's
    ``th32ParentProcessID`` map — is unsafe for a kill/health decision: that
    field is never cleared when a parent dies and Windows recycles PIDs
    aggressively, so a raw walk sums unrelated subtrees rooted at a recycled
    PID. This reuses :func:`descendant_termination_handles`, which validates
    every parent->child edge against exact creation/exit times across two
    snapshots, so only genuine descendants are counted. RSS that cannot be read
    for a given descendant (another session / higher integrity) is skipped, but
    the root itself always contributes, so the result is never a phantom-low
    tree total attached to a recycled root.

    Returns None if even the root's RSS is unavailable, matching the "unknown,
    do not judge" contract the RSS staleness probe relies on.
    """

    if not IS_WINDOWS:
        return None
    if type(pid) is not int or pid <= 1:
        return None
    root_handle = _open_process_termination_handle(pid)
    if root_handle is None:
        # Cannot even anchor the root — fall back to the single-process read so a
        # readable self still yields a number rather than a spurious None.
        rss = proc_rss_bytes_for_pid(pid)
        return None if rss is None else rss / (1024 * 1024)
    descendants: dict[int, int] = {}
    try:
        identity = _windows_process_handle_identity(root_handle)
        if identity is None or identity[0] != pid:
            rss = proc_rss_bytes_for_pid(pid)
            return None if rss is None else rss / (1024 * 1024)
        try:
            descendants = descendant_termination_handles(pid, root_handle=root_handle)
        except Exception:
            # Enumeration failed (transient snapshot race): measure the root
            # alone rather than an unvalidated tree.
            descendants = {}
        total_bytes = 0
        found = False
        for member in (pid, *descendants):
            member_rss = proc_rss_bytes_for_pid(member)
            if member_rss is not None:
                total_bytes += member_rss
                found = True
        return total_bytes / (1024 * 1024) if found else None
    finally:
        for handle in descendants.values():
            close_process_handle(handle)
        close_process_handle(root_handle)


def proc_cpu_seconds() -> float:
    """Return total (user+system) CPU seconds consumed by this process, or 0.0.

    POSIX: ``resource.getrusage`` user+system times.
    Windows: ``GetProcessTimes`` kernel+user times (100-ns units).
    """
    if IS_POSIX:
        try:

            ru = resource.getrusage(resource.RUSAGE_SELF)
            return ru.ru_utime + ru.ru_stime
        except (ImportError, OSError, ValueError):
            return 0.0
    try:

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # argtypes/restype are load-bearing on 64-bit: without them ctypes
        # defaults GetCurrentProcess's return to a 32-bit int and truncates the
        # pseudo-handle, so GetProcessTimes fails and this reads 0.0 (mirrors the
        # proc_rss_bytes fix — same truncation, same cause).
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        creation = wintypes.FILETIME()
        exit_ = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            kernel32.GetCurrentProcess(),
            ctypes.byref(creation),
            ctypes.byref(exit_),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return 0.0

        def _to_secs(ft: "wintypes.FILETIME") -> float:
            return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

        return _to_secs(kernel) + _to_secs(user)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# System-wide metrics (Windows). POSIX callers read /proc or sysctl directly;
# Windows has neither, so route through Win32 via ctypes.
# ---------------------------------------------------------------------------

# Prev-sample state for the Windows system-CPU delta (GetSystemTimes).
_prev_win_sys_cpu: dict[str, float] = {"idle": 0.0, "total": 0.0}


def system_memory() -> "tuple[int, int] | None":
    """Return (total_bytes, available_bytes) of physical RAM on Windows.

    Uses ``GlobalMemoryStatusEx``. Returns ``None`` on non-Windows (POSIX
    callers read ``/proc/meminfo`` or ``sysctl hw.memsize`` themselves) or on
    any failure, so the caller can fall back.
    """
    if not IS_WINDOWS:
        return None
    try:

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return int(stat.ullTotalPhys), int(stat.ullAvailPhys)
        return None
    except Exception:
        return None


def system_cpu_percent() -> "float | None":
    """Return system-wide CPU utilization percent since the previous call.

    Windows only, via ``GetSystemTimes`` (idle/kernel/user FILETIMEs; the
    kernel time INCLUDES idle). Returns ``None`` on non-Windows, the first
    (pre-delta) sample, or failure. Stateful — keeps the previous sample in a
    module global, so callers should poll it periodically.
    """
    if not IS_WINDOWS:
        return None
    try:

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        idle = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None

        def _ticks(ft: "wintypes.FILETIME") -> float:
            return float((ft.dwHighDateTime << 32) | ft.dwLowDateTime)

        idle_t = _ticks(idle)
        # kernel already includes idle, so kernel+user is the full busy+idle.
        total_t = _ticks(kernel) + _ticks(user)
        prev_idle = _prev_win_sys_cpu["idle"]
        prev_total = _prev_win_sys_cpu["total"]
        _prev_win_sys_cpu["idle"] = idle_t
        _prev_win_sys_cpu["total"] = total_t
        if prev_total <= 0:
            return None  # first sample, no delta yet
        dtotal = total_t - prev_total
        if dtotal <= 0:
            return None
        busy = dtotal - (idle_t - prev_idle)
        return min(100.0, max(0.0, round(busy / dtotal * 100.0, 1)))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Available physical memory, on every platform
#
# "How much RAM can a new process take without pushing this machine into swap"
# has a different answer, and a different interface, on each OS: Linux publishes
# the number outright, macOS publishes page counters and leaves the composition
# to the caller, Windows has a Win32 call. A caller that reads only one of them
# does not get a conservative answer on the others -- it gets NO answer, which
# is why this lives here rather than at each call site.
# ---------------------------------------------------------------------------

_MIB_BYTES = 1024 * 1024

#: ``natural_t`` is 32-bit on macOS, including on Apple silicon.
_NATURAL_T = ctypes.c_uint

#: ``host_statistics64`` flavor selector for ``vm_statistics64_data_t``.
_HOST_VM_INFO64 = 4


class _VMStatistics64(ctypes.Structure):
    """``vm_statistics64_data_t`` (``<mach/vm_statistics.h>``), in kernel order.

    Declared in full even though few fields are read, so the element count handed
    to ``host_statistics64`` is exact and the trailing fields land at the offsets
    the kernel writes them to.

    Module scope is load-bearing: ``ctypes.POINTER(T)`` memoises T in a
    module-level dict inside ctypes that is never evicted, so declaring this
    inside the probe would pin a fresh pair of type objects on every call.
    """

    _fields_ = [
        ("free_count", _NATURAL_T),
        ("active_count", _NATURAL_T),
        ("inactive_count", _NATURAL_T),
        ("wire_count", _NATURAL_T),
        ("zero_fill_count", ctypes.c_uint64),
        ("reactivations", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64),
        ("pageouts", ctypes.c_uint64),
        ("faults", ctypes.c_uint64),
        ("cow_faults", ctypes.c_uint64),
        ("lookups", ctypes.c_uint64),
        ("hits", ctypes.c_uint64),
        ("purges", ctypes.c_uint64),
        ("purgeable_count", _NATURAL_T),
        ("speculative_count", _NATURAL_T),
        ("decompressions", ctypes.c_uint64),
        ("compressions", ctypes.c_uint64),
        ("swapins", ctypes.c_uint64),
        ("swapouts", ctypes.c_uint64),
        ("compressor_page_count", _NATURAL_T),
        ("throttled_count", _NATURAL_T),
        ("external_page_count", _NATURAL_T),
        ("internal_page_count", _NATURAL_T),
        ("total_uncompressed_pages_in_compressor", ctypes.c_uint64),
    ]


#: How many ``natural_t``-sized elements the kernel must report having filled
#: before ``external_page_count`` holds anything. ``host_statistics64`` writes
#: the count back, and an older kernel that predates the field leaves it zero --
#: indistinguishable from "no file-backed pages" unless the count is checked.
#: Derived from the layout so it cannot go stale if a field is added above.
_EXTERNAL_PAGE_COUNT_ELEMENTS = (
    _VMStatistics64.external_page_count.offset + _VMStatistics64.external_page_count.size
) // ctypes.sizeof(ctypes.c_int)


def macos_vm_statistics() -> "tuple[_VMStatistics64, int] | None":
    """Mach ``host_statistics64(HOST_VM_INFO64)``, or ``None`` on any failure.

    Returns the filled struct and the element count the kernel wrote back, which
    a caller needs to know whether the trailing (later-revision) fields are
    meaningful. macOS-only; returns ``None`` everywhere else.

    Reads in-process through ``ctypes``/``libSystem`` -- **no subprocess**. That
    is not merely faster: the macOS app sandbox can deny spawning ``vm_stat`` or
    ``sysctl``, and this probe runs on the gateway event loop.
    """
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
    except OSError:
        return None  # not macOS / libSystem unavailable

    try:
        libc.mach_host_self.restype = ctypes.c_uint
        libc.mach_task_self.restype = ctypes.c_uint
        libc.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
        libc.host_statistics64.restype = ctypes.c_int
        libc.host_statistics64.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(_VMStatistics64),
            ctypes.POINTER(ctypes.c_uint),
        ]
        host_port = libc.mach_host_self()
        try:
            stats = _VMStatistics64()
            count = ctypes.c_uint(ctypes.sizeof(_VMStatistics64) // ctypes.sizeof(ctypes.c_int))
            kern_return = libc.host_statistics64(
                host_port,
                _HOST_VM_INFO64,
                ctypes.byref(stats),
                ctypes.byref(count),
            )
        finally:
            # Release the send right from mach_host_self so the port reference is
            # not leaked per probe. Guarded so a missing symbol still returns
            # None cleanly below rather than raising out of a memory reading.
            try:
                libc.mach_port_deallocate(libc.mach_task_self(), host_port)
            except (AttributeError, OSError, ValueError):
                pass
    except (AttributeError, OSError, ValueError):
        return None
    if kern_return != 0:  # non-zero kern_return_t -> failure
        return None
    return stats, int(count.value)


def _macos_available_mib() -> int:
    """RAM in MiB a new process can take on macOS without swapping, or 0.

    macOS publishes no ``MemAvailable``; it publishes page counters, and which
    ones count as available is a decision. Each term here is one:

    * ``free_count`` ALREADY INCLUDES ``speculative_count`` -- Darwin's own
      ``vm_stat`` prints ``free_count - speculative_count`` as its "Pages free"
      line. Adding speculative on top double-counts it, which inflates the
      reading on exactly the loaded machine where it must not.
    * ``purgeable_count`` is volatile memory the kernel may drop outright, with
      no I/O, so it is genuinely available.
    * ``inactive_count`` is NOT all reclaimable: it mixes clean file-backed pages
      with DIRTY ANONYMOUS pages that cannot be handed over without compressing
      or swapping them. ``HOST_VM_INFO64`` publishes no inactive-AND-file
      counter, so the intersection is not computable -- but
      ``min(inactive, external_page_count)`` is an upper bound on the file-backed
      share, and it is strictly tighter than ``inactive``. That tightening is
      what stops a browser's gigabytes of inactive anonymous memory reading as
      free.

    Compressed pages are occupied, so the compressor counts are excluded.

    ``0`` means UNKNOWN, and callers skip an unknown reading rather than treating
    it as zero memory. A read that SUCCEEDED but computed nothing therefore
    returns 0 too: a host with no free, purgeable or file-backed pages at all is
    not a reading anyone should act on.
    """
    probe = macos_vm_statistics()
    if probe is None:
        return 0
    stats, filled = probe
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return 0
    if page_size <= 0:
        return 0
    inactive = int(stats.inactive_count)
    if filled >= _EXTERNAL_PAGE_COUNT_ELEMENTS:
        inactive = min(inactive, int(stats.external_page_count))
    pages = int(stats.free_count) + int(stats.purgeable_count) + inactive
    if pages <= 0:
        return 0
    # max(1, ...) only after the >0 check above, so a real but sub-MiB reading
    # stays distinguishable from "unknown".
    return max(1, pages * page_size // _MIB_BYTES)


def _linux_available_mib() -> int:
    """``MemAvailable`` in MiB, or 0 when ``/proc/meminfo`` cannot be read.

    The kernel's own estimate of what a new allocation can use without swapping.
    It counts reclaimable page cache, which ``MemFree`` and ``SC_AVPHYS_PAGES``
    both omit -- on a host that has read any files those understate badly (they
    match ``MemFree`` exactly, measured 43,574 MiB against ``MemAvailable``'s
    74,768 MiB on the same idle host).
    """
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if not line.startswith("MemAvailable:"):
                    continue
                # "MemAvailable:   107374182 kB" -- the unit is always kB.
                return int(line.split()[1]) * 1024 // _MIB_BYTES
    except (OSError, IndexError, ValueError):
        return 0
    return 0


def host_total_mib() -> int:
    """Total physical RAM in MiB, or 0 when it cannot be determined.

    POSIX ``sysconf`` first, then the Win32 reading -- the same order
    ``sandbox._default_max_memory_mb`` uses, and for the same reason: ``os.sysconf``
    does not EXIST on Windows, so a probe written against it alone does not return a
    conservative number there, it returns nothing. Paired with
    :func:`host_available_mib` so both halves of a memory budget answer on every
    platform; a budget with only one of them silently stops bounding anything.
    """
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return pages * page_size // _MIB_BYTES
    except (AttributeError, OSError, ValueError):
        pass
    mem = system_memory()  # GlobalMemoryStatusEx: (total, available)
    return (mem[0] // _MIB_BYTES) if mem else 0


def host_available_mib() -> int:
    """RAM in MiB actually free for a new process right now, or 0 when unknown.

    **MiB, not GiB, and the unit is load-bearing.** In GiB every reading under
    1 GiB truncates to ``0``, which is also this function's "could not
    determine" answer -- so on the starved host the reading exists to protect,
    860 MiB free would read as "unknown" and the bound built on it would vanish.

    ``0`` is returned only when the platform genuinely cannot be read, so a
    caller can distinguish "no headroom" from "no reading" and fail open on the
    latter.
    """
    if IS_LINUX:
        return _linux_available_mib()
    if IS_MACOS:
        return _macos_available_mib()
    if IS_WINDOWS:
        mem = system_memory()  # GlobalMemoryStatusEx: (total, available)
        return (mem[1] // _MIB_BYTES) if mem else 0
    return 0


#: The values ``kern.memorystatus_vm_pressure_level`` answers in. XNU's handler
#: (``bsd/kern/kern_memorystatus_notify.c``) converts its internal level through
#: ``convert_internal_pressure_level_to_dispatch_level`` before answering, so
#: the reading is ``NOTE_MEMORYSTATUS_PRESSURE_*`` from ``sys/event_private.h``.
#: Those are the same numbers as libdispatch's public
#: ``DISPATCH_MEMORYPRESSURE_NORMAL`` / ``_WARN`` / ``_CRITICAL``. The internal
#: "urgent" level reports as WARN.
MEMORY_PRESSURE_NORMAL = 1
MEMORY_PRESSURE_WARN = 2
MEMORY_PRESSURE_CRITICAL = 4
_MEMORY_PRESSURE_NAMES = {
    MEMORY_PRESSURE_NORMAL: "NORMAL",
    MEMORY_PRESSURE_WARN: "WARN",
    MEMORY_PRESSURE_CRITICAL: "CRITICAL",
}
_MEMORY_PRESSURE_SYSCTL = b"kern.memorystatus_vm_pressure_level"


def memory_pressure_level() -> int | None:
    """The macOS kernel's memory-pressure level, or ``None`` when unknown.

    Answers one of :data:`MEMORY_PRESSURE_NORMAL`, :data:`MEMORY_PRESSURE_WARN`
    or :data:`MEMORY_PRESSURE_CRITICAL`, the level Activity Monitor's
    memory-pressure graph shows. It is the kernel's own verdict, and it LAGS
    (docs/system-specs/modules/subagent.md gives the measurement), so it is no
    measure of available memory and cannot replace a figure such as
    :func:`host_available_mib`. A caller uses it as a backstop
    beside such a figure: when the kernel does say WARN, the host is short
    whatever the page counters add up to.

    ``None`` everywhere other than macOS, and on any failure: no ``libc``, a
    failed call, a wrong-size answer, or a value that is not one of the three
    levels. A caller treats ``None`` as "no reading" and fails
    open, the same contract as ``host_available_mib``'s 0.

    Reads in-process through the cached :func:`_darwin_sysctl_handle`, with no
    ``sysctl`` subprocess, for the reason given in :func:`macos_vm_statistics`.
    On macOS the sysctl needs no privilege; XNU checks one only on other Apple
    platforms.
    """
    if not IS_MACOS:
        return None
    libc = _darwin_sysctl_handle()
    if libc is None:
        return None
    level = ctypes.c_uint32(0)
    size = ctypes.c_size_t(ctypes.sizeof(level))
    try:
        result = libc.sysctlbyname(
            _MEMORY_PRESSURE_SYSCTL, ctypes.byref(level), ctypes.byref(size), None, 0
        )
    except (OSError, ValueError, ctypes.ArgumentError):
        return None
    if result != 0 or size.value != ctypes.sizeof(level):
        return None
    value = int(level.value)
    return value if value in _MEMORY_PRESSURE_NAMES else None


def memory_pressure_name(level: int | None) -> str:
    """``"NORMAL"`` / ``"WARN"`` / ``"CRITICAL"`` for *level*, ``""`` when unknown."""
    return _MEMORY_PRESSURE_NAMES.get(level, "") if level is not None else ""


# ---------------------------------------------------------------------------
# strftime portability
# ---------------------------------------------------------------------------


def strftime(dt: "object", fmt: str) -> str:
    """``dt.strftime(fmt)`` with the GNU/BSD no-pad directives made portable.

    ``%-I`` / ``%-d`` / ``%-m`` etc. (strip leading zero) are glibc/BSD
    extensions that raise ``ValueError`` on Windows' MSVCRT strftime, which
    spells the same thing ``%#I``. Translate the POSIX form to the Windows
    form on win32 so format strings written for macOS/Linux keep working.
    """
    if IS_WINDOWS:
        out = []
        i = 0
        while i < len(fmt):
            if fmt[i] == "%" and i + 1 < len(fmt):
                nxt = fmt[i + 1]
                if nxt == "-" and i + 2 < len(fmt):
                    out.append("%#" + fmt[i + 2])
                    i += 3
                    continue
                out.append(fmt[i : i + 2])
                i += 2
                continue
            out.append(fmt[i])
            i += 1
        fmt = "".join(out)
    return dt.strftime(fmt)  # type: ignore[attr-defined]


def raise_nofile_soft_limit(target: int) -> None:
    """Best-effort raise of the open-file soft limit toward ``target``.

    POSIX: ``resource.setrlimit(RLIMIT_NOFILE)`` capped at the hard limit.
    Windows: no-op — there is no per-process descriptor rlimit; the C runtime
    uses a fixed handle table sized via ``_setmaxstdio`` which does not apply
    to sockets, so nothing to do.
    """
    if not IS_POSIX:
        return
    try:

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(target, hard), hard))
    except (ValueError, OSError, ImportError):
        logger.debug("Could not raise RLIMIT_NOFILE", exc_info=True)


def nofile_soft_limit() -> int:
    """This process's open-file soft limit, or ``0`` where there is none.

    POSIX: ``resource.getrlimit(RLIMIT_NOFILE)`` soft value; ``RLIM_INFINITY``
    reads as ``0``. Windows: ``0`` — there is no per-process descriptor rlimit
    (see :func:`raise_nofile_soft_limit`), so a caller sizing an fd budget from
    this value leaves the dimension unbounded, which matches the platform.
    """
    if not IS_POSIX:
        return 0
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError, ImportError):
        return 0
    if soft == resource.RLIM_INFINITY:
        return 0
    return max(0, int(soft))


def python_launcher_hops() -> int:
    """How many EXTRA processes a Python child spawned from ``sys.executable`` occupies.

    On Windows a virtual environment's ``Scripts\\python.exe`` is not an
    interpreter: it is the venv redirector, which reads ``pyvenv.cfg``,
    ``CreateProcess``-es the base interpreter as its own child, and stays alive
    as that child's parent until it exits. A ``-m`` child launched through
    ``sys.executable`` from a venv-hosted gateway is therefore TWO live processes
    in the child's Job, not one. A caller sizing an ``ActiveProcessLimit`` for
    "this child and nothing else" has to count that hop, or the redirector's own
    ``CreateProcess`` is what the limit refuses (``ERROR_NOT_ENOUGH_QUOTA``, which
    the redirector reports as ``Unable to create process using ...`` and exit
    101) and the child never runs at all.

    Returns ``1`` when ``sys.executable`` is such a redirector -- the interpreter
    Python actually runs is ``sys._base_executable`` and it is a different file --
    and ``0`` everywhere else: on POSIX a venv's
    ``bin/python`` is a symlink or a copy of the real interpreter and spawns
    nothing. Over-counting is harmless (a ceiling of two instead of one still
    bounds a fork bomb); under-counting is the defect this exists to remove.
    """
    if not IS_WINDOWS:
        return 0
    base = getattr(sys, "_base_executable", None)
    if not base:
        return 0
    try:
        same = os.path.normcase(os.path.realpath(sys.executable)) == os.path.normcase(
            os.path.realpath(base)
        )
    except OSError:
        # Cannot resolve either path: fall back to the unresolved spellings and
        # fail toward the hop, since over-counting is harmless and under-counting
        # is the defect this exists to remove.
        same = os.path.normcase(sys.executable) == os.path.normcase(base)
    return 0 if same else 1


# ---------------------------------------------------------------------------
# Windows Job objects — the cgroup-v2-scope analogue
# ---------------------------------------------------------------------------
# On Linux, ``sandbox.cgroup_scope_argv`` bounds an agent subprocess AND all its
# descendants as one cgroup (``TasksMax`` = fork-bomb ceiling, ``MemoryMax`` =
# RSS-balloon ceiling). That wrapper is a no-op on Windows (there is no systemd)
# and logs a one-time loud SECURITY warning, so Windows had NO fork-bomb and NO
# memory ceiling on the agent tree at all.
#
# A Job object is the native equivalent: limits apply to every process in the
# job, and descendants of a job member join the job automatically. Unlike the
# cgroup path this cannot be expressed as an argv prefix — there is no wrapper
# binary to prepend — so it is applied to an already-spawned pid instead. See the
# race note in :func:`apply_job_limits` for why that pid must be SUSPENDED.
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000) is deliberately NOT set. It would
# terminate the agent tree as soon as the last job handle closed, changing
# process LIFECYCLE (a gateway exit would kill running agents) rather than merely
# adding a resource ceiling. Omitting it also means the handle need not be held:
# a job object stays alive while processes are assigned to it, so the limits keep
# being enforced after CloseHandle. That makes this fire-and-forget, with no
# handle registry and no teardown semantics to get wrong.
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9  # JobObjectExtendedLimitInformation


def apply_job_limits(pid: int, *, max_procs: int, max_memory_bytes: int) -> bool:
    """Bound *pid* and its descendants with a Windows Job object.

    The Windows analogue of ``sandbox.cgroup_scope_argv``:

    ==============================  ====================================
    cgroup v2                       Job object
    ==============================  ====================================
    ``TasksMax`` (fork bomb)        ``ActiveProcessLimit``
    ``MemoryMax`` (RSS balloon)     ``JobMemoryLimit``
    ==============================  ====================================

    The memory row is a true equivalent; the process row is NOT one-for-one.
    ``TasksMax`` counts tasks — every thread — while ``ActiveProcessLimit``
    counts processes, so the same numeric budget is a LOOSER bound here: a
    tree of N processes holds at least N tasks and usually many more. It still
    bounds a fork bomb, which is the control's purpose, but do not read the two
    limits as equal strictness, and do not "fix" the gap by scaling the number
    without deciding what a process budget should be — the units differ, so
    there is no arithmetic conversion between them.

    Enforcement is by DENIAL, matching the cgroup tier's practical behavior:
    once ``ActiveProcessLimit`` is reached the member's ``CreateProcess`` calls
    fail with ``ERROR_NOT_ENOUGH_QUOTA``, and an allocation past
    ``JobMemoryLimit`` fails, rather than the tree being killed outright.
    Nothing about process lifetime changes (see the ``KILL_ON_JOB_CLOSE`` note
    above).

    Returns ``True`` when the limits were applied. Returns ``False`` — never
    raises — on POSIX (where ``cgroup_scope_argv`` owns this), on a non-positive
    limit, or on any Win32 failure; the caller treats that as "no ceiling
    enforced" exactly as it already treats the cgroup probe failing.

    Race-free ONLY when paired with :data:`CREATE_SUSPENDED`. Job membership
    covers a member's FUTURE descendants but not ones it already spawned, so
    assigning a *running* child leaves a window in which it could have forked
    something that escapes the job. Callers therefore create the child with
    ``creationflags |= CREATE_SUSPENDED`` — a suspended process has executed no
    instructions and so provably has no descendants — call this, then
    :func:`resume_process_main_thread`. That closes the window by construction
    rather than merely making it small. This function still works on an
    already-running pid (the ceiling then applies from that moment on); the
    suspended handshake is what makes it airtight.
    """
    if IS_POSIX:
        return False
    if max_procs <= 0 or max_memory_bytes <= 0:
        logger.debug(
            "apply_job_limits: skipping non-positive limits (procs=%s, mem=%s)",
            max_procs,
            max_memory_bytes,
        )
        return False
    job = None
    proc_handle = None
    kernel32 = None
    # pragma: no cover below — the ctypes plumbing is Windows-only, and the
    # Windows CI shards run with --no-cov (only the Ubuntu 3.12 shards measure
    # coverage), so these statements are unmeasurable ANYWHERE rather than
    # merely untested. Charging them to the denominator understates the file's
    # real rate, the same reasoning setup.cfg records for the CI-deselected
    # suites it omits. Everything above stays measured: the POSIX early-out is
    # exercised by test_windows_job_limits.py's ungated inertness tests, and the
    # Windows behavior itself is asserted against the live kernel there.
    try:  # pragma: no cover
        _PROCESS_SET_QUOTA = 0x0100  # noqa: N806 — Windows API constant
        _PROCESS_TERMINATE = 0x0001  # noqa: N806 — AssignProcessToJobObject needs it
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        # Anonymous job (NULL name): nothing else can open it by name.
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.warning(
                "SECURITY: CreateJobObject failed (err=%s); fork-bomb / memory-DoS "
                "ceilings are NOT enforced for pid %d",
                _windows_last_error(),
                pid,
            )
            return False

        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_ACTIVE_PROCESS | _JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        info.BasicLimitInformation.ActiveProcessLimit = max_procs
        info.JobMemoryLimit = max_memory_bytes
        if not kernel32.SetInformationJobObject(
            job,
            _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            logger.warning(
                "SECURITY: SetInformationJobObject failed (err=%s); ceilings NOT "
                "enforced for pid %d",
                _windows_last_error(),
                pid,
            )
            return False

        proc_handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not proc_handle:
            logger.warning(
                "SECURITY: OpenProcess(SET_QUOTA|TERMINATE) failed for pid %d (err=%s); "
                "ceilings NOT enforced",
                pid,
                _windows_last_error(),
            )
            return False
        if not kernel32.AssignProcessToJobObject(job, proc_handle):
            logger.warning(
                "SECURITY: AssignProcessToJobObject failed for pid %d (err=%s); "
                "ceilings NOT enforced",
                pid,
                _windows_last_error(),
            )
            return False
        logger.info(
            "Job object ceilings applied to pid %d (max_procs=%d, max_mem=%dMB)",
            pid,
            max_procs,
            max_memory_bytes // (1024 * 1024),
        )
        return True
    except Exception:
        logger.warning("apply_job_limits failed for pid %s", pid, exc_info=True)
        return False
    finally:
        # Safe to close BOTH handles: without KILL_ON_JOB_CLOSE the job object
        # outlives our handle for as long as processes remain assigned, so the
        # limits stay in force. Leaking these would be a per-spawn handle leak in
        # a long-lived gateway.
        for handle in (proc_handle, job):
            if handle and kernel32 is not None:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    logger.debug("CloseHandle failed", exc_info=True)


_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_INVALID_HANDLE_VALUE = -1
# ResumeThread returns the thread's PREVIOUS suspend count, or (DWORD)-1 on
# failure. Compared as an unsigned 32-bit value because the restype is DWORD.
_RESUME_THREAD_FAILED = 0xFFFFFFFF


def resume_process_main_thread(pid: int) -> bool:
    """Resume every suspended thread of *pid*. Returns True iff one was resumed.

    The other half of race-free Job object assignment. A child spawned with
    :data:`CREATE_SUSPENDED` has executed no instructions, so
    :func:`apply_job_limits` can put it in a job knowing no descendant escaped;
    this then lets it run.

    kernel32 has no ``ResumeProcess``, so the main thread has to be reached by
    ID: snapshot the system thread list
    (``CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD)``), select entries whose
    ``th32OwnerProcessID`` matches, and ``ResumeThread`` each. A freshly created
    suspended process has exactly one thread, but every match is resumed rather
    than only the first — resuming a thread that is not suspended is a harmless
    no-op (its suspend count is already 0), whereas guessing wrong about which
    thread is "main" would leave the process wedged forever.

    Returns ``False`` — never raises — on POSIX (nothing is ever suspended there)
    or on any Win32 failure. A ``False`` return is SERIOUS for the caller: the
    child is alive but frozen, and the only safe response is to kill it rather
    than let a suspended process masquerade as a running agent. See
    ``acp.client.finish_suspended_spawn``, which implements that policy.
    """
    if IS_POSIX:
        return False
    snapshot = None
    kernel32 = None
    # Windows-only ctypes plumbing, unmeasurable on every runner — see the note
    # in :func:`apply_job_limits`.
    try:  # pragma: no cover
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
            logger.error(
                "resume_process_main_thread: thread snapshot failed for pid %d (err=%s)",
                pid,
                _windows_last_error(),
            )
            return False
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(_ThreadEntry32)
        resumed = 0
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    try:
                        if kernel32.ResumeThread(thread) != _RESUME_THREAD_FAILED:
                            resumed += 1
                        else:
                            logger.error(
                                "ResumeThread failed for tid %d of pid %d (err=%s)",
                                entry.th32ThreadID,
                                pid,
                                _windows_last_error(),
                            )
                    finally:
                        kernel32.CloseHandle(thread)
                else:
                    logger.error(
                        "OpenThread(SUSPEND_RESUME) failed for tid %d of pid %d (err=%s)",
                        entry.th32ThreadID,
                        pid,
                        _windows_last_error(),
                    )
            # Thread32Next overwrites the entry, dwSize included, so it must be
            # reset before every call or the next iteration fails.
            entry.dwSize = ctypes.sizeof(_ThreadEntry32)
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        if not resumed:
            logger.error("resume_process_main_thread: no threads resumed for pid %d", pid)
        return resumed > 0
    except Exception:
        logger.error("resume_process_main_thread failed for pid %s", pid, exc_info=True)
        return False
    finally:
        if snapshot and snapshot != _INVALID_HANDLE_VALUE and kernel32 is not None:
            try:
                kernel32.CloseHandle(snapshot)
            except Exception:
                logger.debug("CloseHandle(snapshot) failed", exc_info=True)


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


def windows_tree_cleanup_pending(pid: int, start: str | None) -> bool:
    """A pending pin outranks numeric liveness, including a dead root PID."""
    if not IS_WINDOWS:
        return False
    try:
        key = (pid, int(start)) if start is not None else None
    except (TypeError, ValueError):
        return False
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        if key is None:
            return any(state.root_pid == pid for state in _PENDING_WINDOWS_TREE_CLEANUPS.values())
        return key in _PENDING_WINDOWS_TREE_CLEANUPS


async def _create_windows_subprocess_owned(
    owner: _PendingWindowsTreeCleanup, program: str, *args: str, limit: int = 2**16, **kwargs: Any
) -> asyncio.subprocess.Process:
    """Run CPython's subprocess implementation with per-call creation capture.

    Only this call sees the substituted native API and transport constructors.
    The interpreter's functions, globals, event loop and Popen stay untouched.
    Capture happens at CreateProcess return, BEFORE _close_pipe_fds, Handle
    publication, thread-handle close, proactor registration or pipe connection.
    """
    # Private Windows APIs have no Linux typeshed declarations. Runtime identity
    # checks below refuse a different transport implementation before creation.
    windows_events: Any = importlib.import_module("asyncio.windows_events")
    windows_utils: Any = importlib.import_module("asyncio.windows_utils")
    loop = asyncio.get_running_loop()
    make_transport = getattr(getattr(loop, "_make_subprocess_transport"), "__func__", None)
    subprocess_exec = getattr(loop.subprocess_exec, "__func__", None)
    if (
        make_transport is not windows_events.ProactorEventLoop._make_subprocess_transport
        or subprocess_exec is not asyncio.BaseEventLoop.subprocess_exec
    ):
        raise RuntimeError(
            "Windows session start refused: this interpreter does not expose the CPython "
            "Proactor subprocess path the tree-cleanup capture binds to; run the gateway on "
            f"a measured interpreter ({_WINDOWS_CAPTURE_MEASURED_VERSIONS})"
        )
    execute_child = getattr(subprocess.Popen, "_execute_child")
    start_transport = windows_events._WindowsSubprocessTransport._start
    if (
        not {"_winapi", "CreateProcess", "Handle"}.issubset(execute_child.__code__.co_names)
        or not {"windows_utils", "Popen"}.issubset(start_transport.__code__.co_names)
        or "_WindowsSubprocessTransport" not in make_transport.__code__.co_names
    ):
        # Same reasoning as the quarantine refusal: an operator meeting this sees a
        # failed session start and no context, and the remedy is not guessable from
        # the shape names. The interpreter is the whole cause, so it is the remedy.
        raise RuntimeError(
            "Windows session start refused: this interpreter's private subprocess bindings "
            "differ from the shapes the tree-cleanup capture is measured against; run the "
            f"gateway on a measured interpreter ({_WINDOWS_CAPTURE_MEASURED_VERSIONS})"
        )
    with _PENDING_WINDOWS_TREE_CLEANUPS_LOCK:
        if owner not in _WINDOWS_TREE_ADMISSIONS or owner.retired or owner.handles:
            raise RuntimeError("Windows subprocess requires an unused cleanup reservation")

    def clone(function: Any, **bindings: Any) -> Any:
        cloned = types.FunctionType(
            function.__code__,
            dict(function.__globals__, **bindings),
            function.__name__,
            function.__defaults__,
            function.__closure__,
        )
        cloned.__kwdefaults__ = function.__kwdefaults__
        return cloned

    native = getattr(subprocess, "_winapi")
    handle_type = getattr(subprocess, "Handle")

    class CreationAPI:
        def __init__(self) -> None:
            self.thread_pin: Any = None

        def __getattr__(self, name: str) -> Any:
            return getattr(native, name)

        def CloseHandle(self, value: int) -> None:
            if self.thread_pin is not None and int(self.thread_pin) == value:
                self.thread_pin.Close()
                self.thread_pin = None
            else:
                native.CloseHandle(value)

        def CreateProcess(self, *values: Any, **options: Any) -> Any:
            result = native.CreateProcess(*values, **options)
            hp, ht, pid, _tid = result
            # Record the raw exact process value before wrapping handles: even
            # a later wrapper/setup exception must leave the reservation owed.
            owner.root_pid = pid
            owner.key = (pid, 0)
            owner.handles[pid] = hp
            # If pipe-fd cleanup raises before CPython closes ht, this local
            # reference-counted pin closes it when the failed launch unwinds.
            self.thread_pin = handle_type(ht)
            # Keep the reference-counted object itself. Popen receives THIS
            # object below, not a second Handle owning the same native value.
            pin = handle_type(hp)
            owner.raw_root_pin = pin
            return result

    def shared_handle(value: int) -> Any:
        if owner.raw_root_pin is not None and int(owner.raw_root_pin) == value:
            return owner.raw_root_pin
        return handle_type(value)

    class OwnedPopen(windows_utils.Popen):
        _execute_child = clone(
            getattr(subprocess.Popen, "_execute_child"), _winapi=CreationAPI(), Handle=shared_handle
        )

    class OwnedTransport(windows_events._WindowsSubprocessTransport):
        _start = clone(
            windows_events._WindowsSubprocessTransport._start,
            windows_utils=types.SimpleNamespace(Popen=OwnedPopen),
        )

    class SpawnLoop:
        def __getattr__(self, name: str) -> Any:
            return getattr(loop, name)

        _make_subprocess_transport = staticmethod(
            types.MethodType(
                clone(make_transport, _WindowsSubprocessTransport=OwnedTransport),
                loop,
            )
        )

    def protocol_factory() -> Any:
        return aio_subprocess.SubprocessStreamProtocol(limit=limit, loop=loop)

    transport, protocol = await subprocess_exec(
        SpawnLoop(), protocol_factory, program, *args, **kwargs
    )
    return aio_subprocess.Process(transport, protocol, loop)


# --------------------------------------------------------------------------- #
# Compatibility facade. What this module defined is carved into the owner modules in
# ``_CARVED_OWNERS``; the cross-process file locks live in
# ``kiro_crew.platform_lock_compat`` and the owner-only access helpers (the process
# token's SID, the writability checks, ``restrict_to_owner`` and its directory form) in
# ``kiro_crew.platform_owner_compat``. Every moved name stays readable as
# ``kiro_crew.platform_compat.<name>``: a name this module binds is read here, and every
# other one (``_EXPORTS_BY_OWNER``) is FORWARDED -- the mirror's module class reads it
# from its owner, resolved from :data:`sys.modules` on each use. A write or delete
# through this module reaches every module that holds the name: a lock or owner-only
# name lands on that owner alone, as it did before the carve, and any other name lands on
# this module (when it binds the name) and on every carved owner that binds the same
# object (``kiro_crew._facade_mirror``), so a patch of ``kiro_crew.platform_compat.<name>``
# reaches every reader it reached when this was one module, however the test spells it. A
# forwarded name is absent from this module's namespace on purpose -- a binding here
# would shadow the owner for every later read.
#
# The lock and owner-only modules read what they need from this module -- the platform
# flags, the lock modules, the clock, ``ctypes`` and the Win32 struct layouts -- through
# a function-local import when they run, so a test that rebinds one of those here still
# reaches them; this module keeps binding each of those names. No carved owner imports
# this module: the carved owners import each other lowest layer first.
# ``test/test_platform_compat_refactor_facade.py`` pins the forwarding, the mirror and
# that direction.
# --------------------------------------------------------------------------- #
# The lock modules the lock helpers read from here, bound where the platform has them.
if IS_POSIX:
    import fcntl  # noqa: F401 — re-exported
    import resource  # noqa: F401 — POSIX-only; read by the lock and resource helpers
else:
    import msvcrt  # type: ignore[import-not-found]  # noqa: F401

# The no-replace rename's libc handle and symbol name, bound where the files owner binds
# them: on Linux and macOS, when the C library loaded.
if IS_LINUX or IS_MACOS:
    with contextlib.suppress(ImportError):
        from kiro_crew.platform_compat_files import _rename_libc, _rename_symbol  # noqa: F401

from kiro_crew import _facade_mirror  # noqa: E402

#: The modules carved out of this one, lowest layer first: the first one that binds a
#: name answers a forwarded read of it. They are imported here, before the mirror
#: indexes them, so no reader can find one half-built.
_CARVED_OWNERS: tuple[str, ...] = (
    "kiro_crew.platform_compat_native",
    "kiro_crew.platform_compat_proctable",
    "kiro_crew.platform_compat_runtime",
    "kiro_crew.platform_compat_files",
    "kiro_crew.platform_compat_trust",
    "kiro_crew.platform_compat_liveness",
    "kiro_crew.platform_compat_identity",
    "kiro_crew.platform_compat_ports",
    "kiro_crew.platform_compat_descendants",
    "kiro_crew.platform_compat_winproc",
    "kiro_crew.platform_compat_wintree",
    "kiro_crew.platform_compat_kill",
)


def _import_carved_owners() -> None:
    for dotted in _CARVED_OWNERS:
        importlib.import_module(dotted)


_import_carved_owners()

#: The names moved to the lock and owner-only modules, by owner. They owned these names
#: before the carve, so each name is read from, written to and deleted on its one owner,
#: and a write through this module never reaches another module.
_FORWARDS_BY_OWNER: dict[str, tuple[str, ...]] = {
    "kiro_crew.platform_lock_compat": (
        "_LOCK_POLL_SECS",
        "_LOCK_POLL_MAX_SECS",
        "_LOCK_TIMEOUT_SECS",
        "_WIN_LOCK_POLL_SECS",
        "_WIN_LOCK_TIMEOUT_SECS",
        "_on_event_loop",
        "_lock_timeout_message",
        "_posix_acquire_blocking",
        "_win_acquire_blocking",
        "file_lock",
        "flock_exclusive",
        "open_lock_file",
        "open_create_or_existing",
        "acquire_lock",
        "release_lock",
        "try_acquire_lock",
        "try_acquire_lock_or_raise",
        "probe_file_persistence",
        "tempfile",
    ),
    "kiro_crew.platform_owner_compat": (
        "_OWNER_RIGHTS_SID",
        "_TOKEN_QUERY",
        "_TOKEN_USER_CLASS",
        "_process_token_sid",
        "_process_token_sid_unguarded",
        "process_owner_sid",
        "_TOKEN_SID_CACHE",
        "current_user_sid",
        "make_owner_only_dir",
        "local_user_id",
        "stat_owned_by_current_user",
        "stat_writable_by_current_user",
        "path_writable_by_current_user",
        "restrict_to_owner",
        "restrict_dir_to_owner",
        "_apply_owner_only_dacl",
    ),
}


def _index_forwards() -> dict[str, str]:
    """Invert the lock and owner-only table into forwarded name -> owner."""
    return {name: owner for owner, names in _FORWARDS_BY_OWNER.items() for name in names}


#: Forwarded name -> the dotted NAME of its owner, never the module object: the owner
#: is read from :data:`sys.modules` on each use, so a module purged and imported again
#: is seen at once instead of this table forwarding to the old copy. The carved names
#: come from the mirror's index, the lock and owner-only names from their table.
_EXPORTS: dict[str, str] = {}
_MIRROR = _facade_mirror.install(
    _sys.modules[__name__], _CARVED_OWNERS, excluded=(), forwards=_index_forwards()
)
_EXPORTS.update(_MIRROR.served)


def _index_by_owner() -> dict[str, tuple[str, ...]]:
    """Group the forwarded names by their owner."""
    grouped: dict[str, list[str]] = {}
    for name, owner in _EXPORTS.items():
        grouped.setdefault(owner, []).append(name)
    return {owner: tuple(sorted(names)) for owner, names in grouped.items()}


#: Owner module -> every name this module forwards to it.
_EXPORTS_BY_OWNER: dict[str, tuple[str, ...]] = _index_by_owner()


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

# ``from kiro_crew.platform_compat import *`` binds only the names this list holds:
# nothing can enumerate what the mirror would serve, so a forwarded name reaches a star
# importer only by being listed here. The list is DERIVED from what this module binds
# plus the forwarding table, so it is not a third list of names to keep in step. The
# bindings only the forwarding needs stay out of it, so a star import binds what it
# bound before the owners existed.
_NOT_EXPORTED = frozenset({"TYPE_CHECKING", "platform_lock_compat", "platform_owner_compat"})
__all__ = sorted(
    name
    for name in set(globals()) | set(_EXPORTS)
    if not name.startswith("_") and name not in _NOT_EXPORTED
)

if TYPE_CHECKING:  # the forwarded names, visible to type checkers and IDEs
    from kiro_crew.platform_compat_descendants import (  # noqa: F401
        _WINDOWS_CLEANUP_IDENTITY_LIMIT,
        _WINDOWS_CLEANUP_ROOT_LIMIT,
        _WINDOWS_CLEANUP_SNAPSHOT_LIMIT,
        ProcessDescendantIdentity,
        WindowsCleanupCapacityError,
        _atomic_process_descendant_identities,
        _descendants_from_parent_map,
        _direct_child_pids_for_identity_walk,
        _linux_direct_child_pids,
        _ordered_posix_descendant_identities,
        _posix_process_identity_map,
        _posix_process_parent_map,
        _posix_process_snapshot,
        _PosixProcessSnapshotRow,
        _process_start_order,
        _ProcessStartOrder,
        _windows_chain_to_root,
        _windows_process_descendant_identities,
        _windows_process_parent_map,
        _WindowsTreeOverflow,
        attributed_descendants,
        created_after,
        process_descendant_identities,
        process_descendants,
    )
    from kiro_crew.platform_compat_files import (  # noqa: F401
        _LIBRARY_DIR,
        _RENAME_NOREPLACE_FLAG,
        _RENAME_NOREPLACE_FN,
        _SYS_RENAMEAT2_BY_MACHINE,
        RENAME_NOREPLACE_AVAILABLE,
        TCC_LIBRARY_WALKABLE_CHILDREN,
        TCC_PROTECTED_HOME_DIRS,
        _build_renameat2_via_syscall,
        rename_noreplace,
        tcc_protected_dirs_for_walk,
        tcc_prune_walk_dirs,
    )
    from kiro_crew.platform_compat_identity import (  # noqa: F401
        _ANCESTRY_MAX_DEPTH,
        _OWN_START_TIME,
        _PYTHON_INTERPRETER_NAME,
        _START_TIME_PS_TIMEOUT,
        _WINDOWS_EXIT_FILETIME_POLL_SECS,
        _WINDOWS_EXIT_FILETIME_TIMEOUT_SECS,
        ProcessIdentitySource,
        _is_python_interpreter_path,
        _linux_boot_id,
        _own_identity_token,
        _process_lstart,
        _win_process_image_name,
        effective_uid,
        get_ppid,
        get_process_start_id,
        get_process_start_identity,
        is_exec_supervisor_of_this_process,
        linux_process_name,
        own_process_start_time,
        process_argv_fingerprint,
        process_argv_matches_exact,
        process_can_read_under_sandbox,
        process_cgroups_match,
        process_command_line,
        process_cwd,
        process_executable_path,
        process_image_name,
        process_incarnation_id,
        process_is_sandboxed,
        process_namespaces_match,
        process_owner_uid,
        process_start_id_for_source,
        process_start_time,
    )
    from kiro_crew.platform_compat_kill import (  # noqa: F401
        REAP_TIMEOUT_SECS,
        _discard_output_until_exit,
        _isolated_group_of_live_child,
        _raise_taskkill_error,
        _run_cleanup_shielded,
        _shares_own_process_group,
        kill_and_reap,
        kill_pid_async,
        kill_pid_pinned,
        kill_popen_tree,
        kill_process_group,
        kill_process_tree,
        kill_process_tree_async,
        kill_process_tree_pinned,
        pgroup_exists,
        terminate_and_reap,
        terminate_and_reap_sync,
    )
    from kiro_crew.platform_compat_liveness import (  # noqa: F401
        PID_ALIVE,
        PID_DEAD,
        PID_UNSIGNALABLE,
        is_thread_group_leader,
        live_thread_group_leaders,
        pgroup_of,
        pgroup_of_leader,
        pid_exists,
        pid_is_zombie,
        pid_liveness,
    )
    from kiro_crew.platform_compat_native import (  # noqa: F401
        _OWN_PGID,
        _SUBPROCESS_NO_WINDOW,
        _WINDOWS_PID_MAX,
        CREATE_NEW_PROCESS_GROUP,
        CREATE_SUSPENDED,
        DETACHED_PROCESS,
        SIGHUP,
        SIGKILL,
        SIGTERM,
        _IoCounters,
        _JobObjectBasicLimitInformation,
        _ProcessEntry32,
        _SidAndAttributes,
    )
    from kiro_crew.platform_compat_ports import (  # noqa: F401
        _LOOPBACK_COVERING_ADDRESSES,
        _LSOF_TIMEOUT_SECS,
        _WINDOWS_ERROR_INSUFFICIENT_BUFFER,
        _WINDOWS_TCP_STATE_ESTABLISHED,
        _WINDOWS_TCP_STATE_LISTEN,
        _WINDOWS_TCP_TABLE_MAX_BYTES,
        _WINDOWS_TCP_TABLE_OWNER_PID_CONNECTIONS,
        _WINDOWS_TCP_TABLE_OWNER_PID_LISTENER,
        PortListener,
        _linux_loopback_listener_inodes,
        _linux_tcp_peer_pid,
        _macos_tcp_peer_pid,
        _normalize_local_address,
        _normalized_tcp_endpoint,
        _windows_loopback_listener_owner_pids,
        _windows_tcp_owner_rows,
        _WindowsTcpOwnerRow,
        address_covers_loopback,
        find_listening_pids,
        find_port_listeners,
        get_tcp_peer_pid,
        listening_pid_tool,
        listening_pid_tool_available,
        loopback_owner_pids,
        probe_port_listeners,
        process_owns_loopback_listener,
    )
    from kiro_crew.platform_compat_proctable import (  # noqa: F401
        _DARWIN_BSDINFO_SIZE,
        _DARWIN_CHILD_LIST_INITIAL,
        _DARWIN_CTL_KERN,
        _DARWIN_IN6_LOOPBACK,
        _DARWIN_IN6_V4MAPPED_PREFIX,
        _DARWIN_INI_IPV4,
        _DARWIN_INI_IPV6,
        _DARWIN_INSI_FADDR_OFFSET,
        _DARWIN_INSI_VFLAG_OFFSET,
        _DARWIN_KERN_PROC,
        _DARWIN_KERN_PROC_PGRP,
        _DARWIN_KERN_PROC_PID,
        _DARWIN_KERN_PROCARGS2,
        _DARWIN_KINFO_PGRP_ATTEMPTS,
        _DARWIN_KINFO_PGRP_SLACK,
        _DARWIN_KINFO_PROC_SIZE,
        _DARWIN_KP_PID_OFFSET,
        _DARWIN_KP_START_TVSEC_OFFSET,
        _DARWIN_KP_START_TVUSEC_OFFSET,
        _DARWIN_KP_STAT_OFFSET,
        _DARWIN_MAX_FDS,
        _DARWIN_MAXPATHLEN,
        _DARWIN_OFF_START_TVSEC,
        _DARWIN_OFF_START_TVUSEC,
        _DARWIN_PBI_PPID_OFFSET,
        _DARWIN_PBI_START_TVSEC_OFFSET,
        _DARWIN_PBI_START_TVUSEC_OFFSET,
        _DARWIN_PBI_STATUS_OFFSET,
        _DARWIN_PIDPATH_MAXSIZE,
        _DARWIN_PROC_BSDINFO_SIZE,
        _DARWIN_PROC_FDINFO_SIZE,
        _DARWIN_PROC_PIDFDSOCKETINFO,
        _DARWIN_PROC_PIDLISTFDS,
        _DARWIN_PROC_PIDTASKINFO,
        _DARWIN_PROC_PIDTBSDINFO,
        _DARWIN_PROC_PIDVNODEPATHINFO,
        _DARWIN_PROC_TASKINFO_SIZE,
        _DARWIN_PROC_VNODEPATHINFO_SIZE,
        _DARWIN_PROCARGS_BUFSIZE,
        _DARWIN_PROCARGS_ENV_BUFSIZE,
        _DARWIN_PROX_FDTYPE_SOCKET,
        _DARWIN_PTI_TOTAL_SYSTEM_OFFSET,
        _DARWIN_PTI_TOTAL_USER_OFFSET,
        _DARWIN_RI_PHYS_FOOTPRINT_OFFSET,
        _DARWIN_RUSAGE_INFO_V2,
        _DARWIN_RUSAGE_INFO_V2_SIZE,
        _DARWIN_SOCKET_FDINFO_SIZE,
        _DARWIN_SOCKINFO_TCP,
        _DARWIN_SOI_KIND_OFFSET,
        _DARWIN_SZOMB,
        _DARWIN_TCPSI_STATE_OFFSET,
        _DARWIN_TSI_S_ESTABLISHED,
        _DARWIN_VNODE_INFO_PATH_SIZE,
        _DARWIN_VNODE_INFO_SIZE,
        _LINUX_EXITED_STATES,
        _STAT_TOKEN_MAX_DIGITS,
        DarwinKinfoProc,
        DarwinProcessFacts,
        ProcessStartIdentity,
        ProcStat,
        _darwin_kinfo_proc_parse,
        _darwin_kinfo_query,
        _darwin_kinfo_size_mismatch_logged,
        _darwin_libc_sysctl,
        _darwin_libc_sysctl_loaded,
        _darwin_libc_sysctl_lock,
        _darwin_libproc,
        _darwin_libproc_fd_bound,
        _darwin_libproc_fd_handle,
        _darwin_libproc_handle,
        _darwin_libproc_loaded,
        _darwin_libproc_start_id,
        _darwin_libproc_tree_bound,
        _darwin_libproc_tree_handle,
        _darwin_peer_is_loopback,
        _darwin_process_cwd,
        _darwin_process_phys_footprint_bytes,
        _darwin_process_start_identity,
        _darwin_process_start_microtime,
        _DarwinLibprocUnavailable,
        _descriptors_on,
        _file_identity,
        _flock_acquirer_in_fdinfo,
        _flock_acquirer_in_proc_locks,
        _flock_acquirer_row,
        _linux_proc_root,
        _parse_cpu_jiffies,
        _parse_ppid,
        _stat_token_int,
        _stat_tokens,
        darwin_child_pids,
        darwin_established_tcp_count,
        darwin_footprint_tree_mb,
        darwin_kinfo_proc,
        darwin_libproc_available,
        darwin_pgroup_members,
        darwin_pid_is_zombie,
        darwin_process_argv,
        darwin_process_environ,
        darwin_process_facts,
        darwin_process_path,
        flock_owner_pid,
        linux_pgroup_members,
        parent_pid,
        pids_holding_file,
        proc_child_map,
        proc_cpu_jiffies_for_pids,
        proc_phys_footprint_bytes_for_pid,
        read_proc_stat,
        read_proc_status_int,
    )
    from kiro_crew.platform_compat_runtime import (  # noqa: F401
        _EXIT_FLUSH_DEADLINE_SECS,
        _KEPT_FOR_REEXEC,
        _POST_DRAIN_EXEC_FAILURE_EXIT,
        _PYTHON_OPTIONS_WITH_VALUES,
        _UTF8_PROCESS_ENV,
        _WHEEL_APPLY_MODULE,
        _WINDOWS_ARM_MACHINES,
        BUNDLED_BACKEND_DIST_DIRNAME,
        _disarm_process_alarm_before_exec,
        _drain_event_log_for_exit,
        _ensure_utf8_process_environment,
        arm_process_alarm,
        awake_now,
        boottime_now,
        cancel_wheel_applies_in_flight,
        ensure_line_buffered_stdout,
        ensure_utf8_console,
        execv_target_available,
        exit_after_failed_restart_exec,
        hard_exit,
        is_bundled_interpreter,
        is_windows_on_arm,
        isolated_python_argv,
        keep_for_reexec,
        kept_for_reexec,
        process_age_secs,
        process_alarm_available,
        process_start_boot_secs,
        reexec_launcher,
        reexec_python_module,
    )
    from kiro_crew.platform_compat_trust import (  # noqa: F401
        _ACL_GROUP,
        _ACL_MASK,
        _ACL_USER,
        _ACL_WRITE,
        _ACL_XATTR_VERSION,
        _FOLDERID_DOCUMENTS,
        _LOCAL_SYSTEM_BIN_DIR,
        _MAX_SYMLINK_HOPS,
        _NO_ACL_ERRNOS,
        _POWERSHELL_USER_PROFILES,
        _TRUSTED_SYSTEM_BIN_DIRS,
        _UNPINNED_TOOL_PROBED,
        _UNTRUSTED_AWS_BIN_LOGGED,
        _WINDOWS_BIN_SUFFIXES,
        _WINDOWS_GIT_DIRS,
        _acl_admits_another_account,
        _folded_env_allowlist,
        _group_shared_with_another_account,
        _is_native_program,
        _is_root_owned_path,
        _local_aws_bin_candidate,
        _local_aws_bin_is_trusted,
        _log_tool_outside_trusted_dirs,
        _reveal_env,
        _root_owned_entry,
        _trusted_bin_search,
        _windows_system_dirs,
        aws_bin_declined_on_ownership,
        env_key_allowed,
        group_write_admits_another_account,
        open_with_default_app,
        reveal_in_file_manager,
        short_path_name,
        tool_outside_trusted_dirs,
        traversed_components,
        traversed_components_and_links,
        trusted_aws_bin,
        trusted_git_bin,
        trusted_system_bin,
        trusted_system_bin_quiet,
        trusted_system_path,
        windows_documents_dir,
        windows_powershell_profile_paths,
    )
    from kiro_crew.platform_compat_winproc import (  # noqa: F401
        _windows_descendant_failure_details,
        _windows_foreign_descendant_pids,
        _windows_lineage_matches_lifetimes,
        _windows_process_query_creation,
        _windows_process_query_diagnostic,
        descendant_termination_handles_async,
        duplicate_asyncio_process_handle,
        open_process_termination_handle,
    )
    from kiro_crew.platform_compat_wintree import (  # noqa: F401
        _PENDING_WINDOWS_TREE_RETRY_LIMIT,
        _WINDOWS_TERMINATE_REFUSAL_WAIT_MS,
        _WINDOWS_TREE_REAP_POLL_SECS,
        _WINDOWS_TREE_REAP_TIMEOUT_SECS,
        WindowsTreeDrainPending,
        _advance_owned_windows_tree,
        _bind_windows_cleanup_process,
        _drain_windows_process_tree,
        _manual_windows_tree,
        create_windows_cleanup_owned_process,
        finish_windows_cleanup_owned_spawn,
        process_handle_active,
        release_windows_tree_reservation,
        reserve_windows_tree_cleanup,
        retry_pending_windows_process_trees,
        terminate_process_handle,
        terminate_windows_asyncio_tree,
        terminate_windows_process_tree_owned,
    )
    from kiro_crew.platform_lock_compat import (  # noqa: F401
        _LOCK_POLL_MAX_SECS,
        _LOCK_POLL_SECS,
        _LOCK_TIMEOUT_SECS,
        _WIN_LOCK_POLL_SECS,
        _WIN_LOCK_TIMEOUT_SECS,
        _lock_timeout_message,
        _on_event_loop,
        _posix_acquire_blocking,
        _win_acquire_blocking,
        acquire_lock,
        file_lock,
        flock_exclusive,
        open_create_or_existing,
        open_lock_file,
        probe_file_persistence,
        release_lock,
        tempfile,
        try_acquire_lock,
        try_acquire_lock_or_raise,
    )
    from kiro_crew.platform_owner_compat import (  # noqa: F401
        _OWNER_RIGHTS_SID,
        _TOKEN_QUERY,
        _TOKEN_SID_CACHE,
        _TOKEN_USER_CLASS,
        _apply_owner_only_dacl,
        _process_token_sid,
        _process_token_sid_unguarded,
        current_user_sid,
        local_user_id,
        make_owner_only_dir,
        path_writable_by_current_user,
        process_owner_sid,
        restrict_dir_to_owner,
        restrict_to_owner,
        stat_owned_by_current_user,
        stat_writable_by_current_user,
    )

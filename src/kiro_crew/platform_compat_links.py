"""Links and junctions, and opens that never follow one at the name.

Creating, detecting and removing a directory symlink or Windows junction, the first
linked ancestor of a path, the extended-length prefix folded off a path before a
containment comparison, a pinned directory that acts on its entries through its
descriptor, and the opens that never follow a link at the final name (``O_NOFOLLOW``
on POSIX, ``CreateFileW`` refusing a reparse point on Windows).

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import ctypes.util
import errno
import os
import pathlib
import stat
from ctypes import wintypes
from pathlib import Path
from typing import TypeVar

from kiro_crew import platform_lock_compat
from kiro_crew.platform_compat_native import IS_POSIX

if not IS_POSIX:
    import msvcrt  # type: ignore[import-not-found]


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


_StrOrPath = TypeVar("_StrOrPath", str, Path)


def strip_extended_length_prefix(path: _StrOrPath) -> _StrOrPath:
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
    imports ``kiro_crew.platform_compat``, and with it this module, for its own
    Windows no-reparse open, so this layer cannot import it back.
    ``test_pinned_directory.py`` asserts the two are equal, so the copy cannot drift
    silently -- a flag added on either side reddens that test.
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

"""Owner-only modes for the files and directories Kiro Crew keeps in the data home.

The data home holds chat transcripts, the knowledge base, logs and configuration.
Under the usual ``022`` umask a file created without an explicit mode lands at
``0644`` and a directory at ``0755``, which any other local account can read
wherever the path to it is traversable. The policy is therefore: **every file
under the data home is ``0600`` and every directory ``0700``**, enforced in
three layers that do not depend on each other:

1. **At creation.** The writers create with the mode rather than ``chmod``-ing
   after the write, so no file is ever briefly readable:
   :func:`owner_only_opener` for ``open()`` / logging handlers,
   :func:`prepare_owner_only_sqlite` before a SQLite connect,
   :func:`mkdirs_owner_only` for directories, and ``atomic_write``'s default
   mode for any target inside the data home.
2. **The root.** ``config.paths.ensure_data_home`` makes the data home itself
   ``0700`` in every CLI prologue, which closes the whole tree in one place.
3. **What already exists.** :func:`tighten_data_home` runs once per gateway
   start, on a worker thread after the dashboard listener is serving, and drops
   group/other bits from the stores an earlier version left behind. It touches
   only a fixed list of Kiro Crew's own stores (:data:`STARTUP_SWEEP_STORES`)
   and never walks the rest of the data home, so the user's own work there
   keeps its modes without the sweep having to recognise it.

Changing the mode of a file that already exists never opens it for I/O. Closing
ANY descriptor on a file releases every POSIX record lock the process holds on
it, and SQLite locks a database and its ``-shm`` index that way, so an
``open`` + ``fchmod`` + ``close`` of a database another connection of the same
process has open would silently drop that connection's locks. See
:func:`_chmod_file_to_owner` for how the mode is changed instead.

What this module deliberately does NOT do is change the process umask. A
``umask(0o077)`` is inherited by every child process, so files an agent writes
into the user's own repositories would come out ``0600`` too, and a scoped umask
is not scoped at all in a threaded process: it is one value for every thread.

POSIX only. On Windows the mode bits carry no access control; the data home's
inheritable owner-only DACL (``restrict_dir_to_owner`` in ``ensure_data_home``)
is what new files there inherit, and every helper here is a no-op that leaves
the previous behaviour untouched.
"""

from __future__ import annotations

import errno
import fnmatch
import functools
import logging
import os
import secrets
import stat
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

#: The mode every file under the data home is created with.
OWNER_ONLY_FILE_MODE = 0o600
#: The mode every directory under the data home is created with.
OWNER_ONLY_DIR_MODE = 0o700
#: The permission bits the policy removes: group and other, read/write/execute.
GROUP_OTHER_BITS = 0o077

#: SQLite's sidecar files. The unix VFS creates each one with the permission bits
#: of the database file itself (``findCreateFileMode`` for the journal and the WAL,
#: the database's ``fstat`` mode for the ``-shm`` index), fchmod-ing past the umask
#: when they differ, so an owner-only database yields owner-only sidecars. A sidecar
#: that ALREADY exists keeps whatever mode it was created with, so the existing ones
#: are tightened alongside the database.
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_PATH = getattr(os, "O_PATH", 0)

#: Read-only, no-follow open of a directory, so a symlink at the name is refused
#: (``ELOOP``) instead of being entered. Directories carry no SQLite locks, so
#: holding and closing a real descriptor on one releases nothing.
_DIR_OPEN_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC

#: The errnos an ``O_NOFOLLOW`` open answers with when the name is a symlink:
#: ``ELOOP`` on Linux and macOS, ``EMLINK`` on FreeBSD, ``ENOTDIR`` when
#: ``O_DIRECTORY`` met the link first.
_SYMLINK_REFUSALS = frozenset({errno.ELOOP, errno.EMLINK, errno.ENOTDIR})

#: Whether ``os.link`` takes directory descriptors (``linkat``), which
#: :func:`_prepare_database_in` publishes a new database with, and whether it can
#: be told not to follow a symlink at the source; read once, at import.
_LINK_IN_DIRECTORY = os.link in os.supports_dir_fd
_LINK_NOFOLLOW = os.link in os.supports_follow_symlinks


def owner_only_opener(path: str, flags: int) -> int:
    """An ``opener=`` for :func:`open` that creates the file ``0600``.

    ``open(path, "a", opener=owner_only_opener)`` behaves exactly like the plain
    call except for the creation mode: the builtin passes ``0o666`` (masked by
    the umask to ``0o644``), this passes ``0o600``. An EXISTING file keeps its
    mode -- ``os.open`` only applies a mode when it creates -- and that is what
    the startup sweep is for. Inheritance is unchanged: ``io.FileIO`` adds
    ``O_CLOEXEC`` to *flags* before calling an opener and marks the descriptor
    non-inheritable after.

    Safe on Windows: there ``0o600`` carries ``S_IWRITE``, so the file is created
    writable, which is all the mode argument decides on that platform.
    """
    return os.open(path, flags, OWNER_ONLY_FILE_MODE)


def owner_only_opener_for(
    path: str | os.PathLike[str],
) -> Callable[[str, int], int] | None:
    """:func:`owner_only_opener` for a target inside the data home, else None.

    For ``open(..., opener=owner_only_opener_for(path))`` at the generic sites
    whose target is USUALLY in the data home but may be configured elsewhere:
    ``None`` is ``open``'s own default, so a file outside the home is created
    exactly as before.
    """
    return owner_only_opener if is_owner_only_target(path) else None


def write_text_owner_only(
    path: str | os.PathLike[str], text: str, *, encoding: str = "utf-8"
) -> None:
    """``Path.write_text`` that creates the file ``0600``.

    Same semantics otherwise -- truncate in place, follow a symlink at the name
    -- so a site can switch to it without changing what it writes or where.
    """
    with open(path, "w", encoding=encoding, opener=owner_only_opener) as handle:
        handle.write(text)


def write_text_owner_only_in_home(
    path: str | os.PathLike[str], text: str, *, encoding: str = "utf-8"
) -> None:
    """:func:`write_text_owner_only` inside the data home, plain ``Path.write_text`` outside.

    For the generic sites whose target is usually, but not necessarily, in the
    data home: a file anywhere else is written exactly as before.
    """
    if is_owner_only_target(path):
        write_text_owner_only(path, text, encoding=encoding)
    else:
        Path(path).write_text(text, encoding=encoding)


def mkdirs_owner_only(path: str | os.PathLike[str]) -> None:
    """``mkdir -p`` where EVERY directory it creates is ``0700``.

    :meth:`pathlib.Path.mkdir` and :func:`os.makedirs` apply *mode* to the last
    component only and create the intermediate ones at the umask default, so a
    fresh ``sessions/<key>/`` would get an owner-only leaf under a ``0755``
    parent. Existing directories are left exactly as they are: this decides how
    a directory is born, not what an existing one becomes (the startup sweep's
    job). Raises like ``Path.mkdir(parents=True, exist_ok=True)``, including
    ``FileExistsError`` when a non-directory sits at a name it needs. Like
    pathlib, any ``OSError`` on a name that already is a directory is accepted:
    a sandbox or read-only mount can answer ``EPERM``/``EACCES``/``EROFS`` ahead
    of ``EEXIST`` for a directory that exists and is usable. On Windows no mode
    is passed: CPython maps a ``0o700`` ``mkdir`` there to a protected DACL that
    would cut the directory off from the data home's inheritable owner grant.
    """
    if not platform_compat.IS_POSIX:
        Path(path).mkdir(parents=True, exist_ok=True)
        return
    target = os.path.abspath(os.fspath(path))
    try:
        os.mkdir(target, OWNER_ONLY_DIR_MODE)
    except FileNotFoundError:
        parent = os.path.dirname(target)
        if parent == target:
            raise
        mkdirs_owner_only(parent)
        try:
            os.mkdir(target, OWNER_ONLY_DIR_MODE)
        except OSError:
            if not os.path.isdir(target):
                raise
    except OSError:
        if not os.path.isdir(target):
            raise


def ensure_directory(path: str | os.PathLike[str]) -> None:
    """``mkdir -p`` that is owner-only inside the data home and unchanged outside it.

    For the generic sites that create a directory which is USUALLY in the data
    home but may be configured elsewhere -- the agent work dir, an artifact
    root, an app's data dir. Inside the data home every directory it creates is
    ``0700`` (:func:`mkdirs_owner_only`); anywhere else it is exactly
    ``Path.mkdir(parents=True, exist_ok=True)``, so a user's own project
    directory is never given a mode it did not ask for.
    """
    if is_owner_only_target(path):
        mkdirs_owner_only(path)
    else:
        Path(path).mkdir(parents=True, exist_ok=True)


def _tightenable(st: os.stat_result) -> bool:
    """True for a regular file this process owns, with one name, that has group/other bits.

    The link count matters because a mode lives on the inode: a hard link planted
    in the data home to a file elsewhere would otherwise turn a tightening into a
    ``chmod`` of that file. An already owner-only file answers False, so callers
    that check this first never touch it at all.
    """
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == os.geteuid()
        and st.st_nlink == 1
        and bool(stat.S_IMODE(st.st_mode) & GROUP_OTHER_BITS)
    )


@functools.lru_cache(maxsize=1)
def _proc_fd_chmod_available() -> bool:
    """Linux with ``O_PATH`` and a mounted ``/proc``: see :func:`_chmod_file_to_owner`."""
    return bool(_O_PATH) and sys.platform.startswith("linux") and os.path.isdir("/proc/self/fd")


def _file_chmod_supported(*, with_dir_fd: bool) -> bool:
    """Whether :func:`_chmod_file_to_owner` has a way to change a file's mode here."""
    if _proc_fd_chmod_available():
        return True
    if os.chmod not in os.supports_follow_symlinks:
        return False
    return not with_dir_fd or os.chmod in os.supports_dir_fd


def _chmod_file_to_owner(name: str, expected: os.stat_result, *, dir_fd: int | None = None) -> bool:
    """Drop group/other bits from the regular file *name*, without opening it for I/O.

    *expected* is the caller's no-follow ``stat`` of *name* and must already pass
    :func:`_tightenable`. Returns True when the mode changed and False when it
    could not be changed safely (no mechanism on this platform, or the name
    names a different inode from the one the caller checked). Raises the ``chmod``'s own
    ``OSError`` -- ``EPERM``, ``EROFS`` -- so a refusal is never mistaken for a
    success.

    It does not ``open`` + ``fchmod`` + ``close``, because closing any descriptor on
    a file releases every POSIX record lock this process holds on it (the
    SQLite-corruption hazard in the module docstring). Instead:

    * **Linux:** an ``O_PATH | O_NOFOLLOW`` descriptor, which opens nothing for
      I/O and whose close releases no record lock (the kernel skips lock removal
      for ``O_PATH`` files). It is re-checked by inode, and the mode is changed
      through ``/proc/self/fd/<n>``, which reaches exactly that inode. A symlink at
      the name yields a descriptor on the link itself, which the re-check refuses;
      a FIFO cannot block an ``O_PATH`` open.
    * **Elsewhere** (macOS, the BSDs): ``chmod(..., follow_symlinks=False)`` by
      name, with no descriptor at all. There is no inode re-check on this path,
      so a name swapped between the caller's ``stat`` and the ``chmod`` is
      changed in its place; a symlink swapped in gets its own mode changed, never
      its target's.
    """
    if _proc_fd_chmod_available():
        fd = os.open(name, _O_PATH | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=dir_fd)
        try:
            st = os.fstat(fd)
            if not _tightenable(st) or (st.st_dev, st.st_ino) != (expected.st_dev, expected.st_ino):
                return False
            os.chmod(f"/proc/self/fd/{fd}", stat.S_IMODE(st.st_mode) & ~GROUP_OTHER_BITS)
        finally:
            os.close(fd)  # an O_PATH descriptor: closing it releases no lock
        return True
    if not _file_chmod_supported(with_dir_fd=dir_fd is not None):
        return False
    os.chmod(
        name,
        stat.S_IMODE(expected.st_mode) & ~GROUP_OTHER_BITS,
        dir_fd=dir_fd,
        follow_symlinks=False,
    )
    return True


def _tighten_entry(name: str, dir_fd: int | None = None) -> bool:
    """Tighten one existing file by name (relative to *dir_fd*). Never raises.

    ``stat``-s first, so an already owner-only file, a symlink, a hard-linked
    file and a missing name cost one ``lstat`` and are never opened.
    """
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    if not _tightenable(st):
        return False
    try:
        return _chmod_file_to_owner(name, st, dir_fd=dir_fd)
    except OSError:
        logger.debug("could not tighten %s to owner-only", name, exc_info=True)
        return False


def prepare_owner_only_sqlite(db_path: str | os.PathLike[str]) -> None:
    """Make sure SQLite opens *db_path* as an owner-only file. Call before ``connect``.

    SQLite creates a missing database at ``0644 & ~umask`` and gives its
    ``-wal``/``-shm``/``-journal`` sidecars the database's own mode (see
    :data:`SQLITE_SIDECAR_SUFFIXES`). So a database that already exists as an
    empty ``0600`` file when the connect runs comes out owner-only together with
    every sidecar SQLite creates for it, with no window in which any of them is
    readable. An empty file is a valid empty database to SQLite.

    * Missing database: created empty and ``0600`` under a staging name, then
      published with a hard link (:func:`_prepare_database_in`), so this call
      never holds a descriptor on the published name, a name that appears
      concurrently is never replaced, and nothing is created through a symlink.
    * Existing database and sidecars: group/other bits dropped without opening
      them (:func:`_chmod_file_to_owner`), so the locks of a connection this
      process already has on them survive. An already owner-only file is only
      ``lstat``-ed.
    * A symlink at the database name is left alone, as is its target: the
      operator pointed it somewhere on purpose. Note that the caller's
      ``connect`` then follows that link like any other open and, if the target
      is missing, SQLite creates it with its default mode -- the operator's
      redirection, not a file this policy owns.
    * Inside the data home, a symlinked DIRECTORY between the home and the
      database is treated the same way: the files behind it are left alone, so
      a link planted under the home cannot turn this into a ``chmod`` (or a
      create) somewhere else. The home itself may be a link (a relocated data
      home). Outside the data home the directory is the caller's own configured
      location and is opened by name.
    * A path with a ``..`` component is left alone: folding it lexically could
      name a different file from the one ``connect`` opens through a symlink.

    Call it immediately before a write-capable ``connect``, after any
    ``exists()`` check the caller makes, since it creates the file. Do not call
    it for a ``mode=ro`` / ``mode=rw`` URI, which must not create the file.
    In-memory and URI names are ignored. Never raises: a database whose mode
    cannot be set must still open. A no-op on Windows.
    """
    if not platform_compat.IS_POSIX:
        return
    name = os.fspath(db_path)
    if not name or name == ":memory:" or name.startswith("file:"):
        return
    # Absolute WITHOUT normalising: ``abspath`` folds ``link/..`` away
    # lexically, while the kernel (and so the caller's connect) resolves it
    # through the link, so the two would name different files. A path with a
    # ``..`` component is left alone rather than guessed at.
    absolute = name if os.path.isabs(name) else os.path.join(os.getcwd(), name)
    if os.pardir in Path(absolute).parts:
        logger.debug("not preparing %s: the path has a '..' component", name)
        return
    parent, leaf = os.path.split(absolute)
    if not leaf:
        return
    try:
        dir_fd = _open_database_directory(parent)
    except OSError:
        # A missing parent, a permission problem: SQLite reports the real
        # failure itself when it opens the file.
        logger.debug("could not open the directory of %s", name, exc_info=True)
        return
    if dir_fd is None:
        return
    try:
        _prepare_database_in(dir_fd, leaf)
        for suffix in SQLITE_SIDECAR_SUFFIXES:
            _tighten_entry(leaf + suffix, dir_fd)
    finally:
        os.close(dir_fd)


def _open_database_directory(parent: str) -> int | None:
    """Open *parent* for :func:`prepare_owner_only_sqlite`; None when it must be left alone.

    Inside the data home the directory is reached from the home one component
    at a time with ``O_NOFOLLOW``, and a symlink at any component answers None.
    Outside it, *parent* is opened by name. Raises ``OSError`` for anything else
    (a missing directory, a permission refusal).
    """
    home = _policy_home()
    target = Path(parent)
    if home is None or not (target == home or home in target.parents):
        return os.open(parent, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    fd = os.open(str(home), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    for part in target.relative_to(home).parts:
        try:
            child = os.open(part, _DIR_OPEN_FLAGS, dir_fd=fd)
        except OSError as exc:
            os.close(fd)
            if exc.errno in _SYMLINK_REFUSALS:
                return None
            raise
        os.close(fd)
        fd = child
    return fd


def _prepare_database_in(dir_fd: int, leaf: str) -> None:
    """Create *leaf* ``0600`` in *dir_fd*, or tighten it when it already exists.

    A missing database is never created by opening *leaf*. Once the name exists,
    another connection of this process can open and lock it at any moment, and
    closing a creation descriptor on that inode afterwards would release those
    locks (the hazard in the module docstring). So the empty ``0600`` file is
    created under a staging name only this call knows, that descriptor is closed
    while nothing else can have the file open, and the file is then published at
    *leaf* with a hard link, which never replaces a name that exists by then.

    Like any create-then-rename write (``atomic_write``'s ``.tmp``), a hard kill
    inside the few system calls between the staging create and its unlink leaves
    the staging name behind: an empty ``0600`` file, or a second ``0600`` name
    for the new database. Each later call stages under a fresh name, so it is
    one leftover per such kill, never a loop.
    """
    try:
        os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("could not stat %s", leaf, exc_info=True)
        return
    else:
        _tighten_entry(leaf, dir_fd)
        return
    if not _LINK_IN_DIRECTORY:
        return  # SQLite creates it; the next prepare narrows it (_tighten_entry)
    staging = f".{leaf}.{secrets.token_hex(8)}.owner-only"
    try:
        fd = os.open(
            staging,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
            OWNER_ONLY_FILE_MODE,
            dir_fd=dir_fd,
        )
    except OSError:
        # A read-only volume, a permission problem: SQLite will report the real
        # failure itself when it opens the file.
        logger.debug("could not pre-create %s owner-only", leaf, exc_info=True)
        return
    os.close(fd)  # the staging name is private to this call: no lock can be held on it
    try:
        os.link(
            staging,
            leaf,
            src_dir_fd=dir_fd,
            dst_dir_fd=dir_fd,
            follow_symlinks=not _LINK_NOFOLLOW,
        )
    except FileExistsError:
        _tighten_entry(leaf, dir_fd)  # it appeared after the stat; never replaced
    except OSError:
        # A filesystem without hard links, a permission problem: SQLite creates
        # the database itself, and the next prepare narrows it.
        logger.debug("could not publish %s owner-only", leaf, exc_info=True)
    finally:
        try:
            os.unlink(staging, dir_fd=dir_fd)
        except OSError:
            logger.debug("could not remove the staging file for %s", leaf, exc_info=True)


@dataclass
class TightenReport:
    """What one :func:`tighten_stores_to_owner` pass did."""

    visited: int = 0
    tightened: int = 0
    skipped_links: int = 0
    skipped_hard_linked: int = 0
    skipped_foreign: int = 0
    unsupported: int = 0
    errors: int = 0
    complete: bool = False
    stopped: str = ""


#: Deepest directory level the sweep descends to below a store. Bounds the
#: descriptors held open (one per level) as well as the walk; the stores' real
#: layout is a handful of levels deep.
MAX_SWEEP_DEPTH = 48

#: Consecutive permission refusals after which the sweep stops: a volume that
#: refuses every ``chmod`` (macOS ``com.apple.provenance``, a foreign owner)
#: would otherwise spend the whole budget learning nothing new.
_MAX_CONSECUTIVE_REFUSALS = 64

#: :attr:`TightenReport.stopped` when the caller's *should_stop* ended the sweep.
SWEEP_STOPPED_FOR_SHUTDOWN = "the gateway is stopping"

#: The characters that make a store name's last component an ``fnmatch`` pattern.
_WILDCARD_CHARACTERS = frozenset("*?[")


class _SweepStopped(Exception):
    """Ends a sweep early; :attr:`TightenReport.stopped` already says why."""


def _is_pattern(name: str) -> bool:
    return not _WILDCARD_CHARACTERS.isdisjoint(name)


def _store_groups(stores: Iterable[str]) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Group *stores* by the directory they sit in: ``[(parent parts, leaf names)]``.

    Raises ``ValueError`` for a name that is not a plain relative path, or that
    has a pattern anywhere but in its last component: the directories leading
    to a store are always named, so the sweep never lists a directory to find
    one.
    """
    groups: dict[tuple[str, ...], list[str]] = {}
    for store in stores:
        parts = PurePosixPath(store).parts
        if (
            not parts
            or store.startswith("/")
            or "\\" in store
            or os.pardir in parts
            or any(_is_pattern(part) for part in parts[:-1])
        ):
            raise ValueError(f"not a store path relative to the data home: {store!r}")
        groups.setdefault(parts[:-1], []).append(parts[-1])
    return [(parent, tuple(leaves)) for parent, leaves in groups.items()]


def _open_root(root: str | os.PathLike[str]) -> tuple[int, os.stat_result] | TightenReport:
    """Open *root* by name (it may be a symlink: a relocated data home); a report on failure."""
    try:
        fd = os.open(os.fspath(root), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    except OSError as exc:
        return TightenReport(errors=1, stopped=f"cannot open the root: {exc.strerror or exc}")
    try:
        return fd, os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        return TightenReport(errors=1, stopped=f"cannot stat the root: {exc.strerror or exc}")


class _Sweep:
    """The state of one bounded, no-follow pass (:func:`tighten_stores_to_owner`).

    Every name -- a store, or an entry found below a store directory -- goes
    through :meth:`entry`; :meth:`drain` walks the directories it queued. A spent
    budget, a read-only filesystem or a run of refusals raises
    :class:`_SweepStopped` with :attr:`TightenReport.stopped` set.
    """

    def __init__(
        self,
        root_st: os.stat_result,
        *,
        max_seconds: float,
        max_entries: int,
        clock: Callable[[], float],
        should_stop: Callable[[], bool] | None,
    ) -> None:
        self.report = TightenReport()
        self._max_seconds = max_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._deadline = clock() + max_seconds
        self._should_stop = should_stop
        self._uid = os.geteuid()
        self._root_dev = root_st.st_dev
        self._refusals = 0
        self._files_supported = _file_chmod_supported(with_dir_fd=True)
        # One frame per open directory: (descriptor, its entry iterator, depth).
        # Depth-first, so the descriptors held open are two per level (the
        # directory and the iterator's own duplicate), never one per pending
        # sibling.
        self._stack: list[tuple[int, Iterator[os.DirEntry[str]], int]] = []

    def _stop(self, why: str) -> NoReturn:
        self.report.stopped = why
        raise _SweepStopped(why)

    def _tick(self) -> None:
        self.report.visited += 1
        if self.report.visited > self._max_entries:
            self._stop(f"entry budget of {self._max_entries} reached")
        if self._clock() > self._deadline:
            self._stop(f"time budget of {self._max_seconds:g}s reached")
        if self._should_stop is not None and self._should_stop():
            self._stop(SWEEP_STOPPED_FOR_SHUTDOWN)

    def _failed(self, exc: OSError) -> None:
        """Count one refused change; stop on ``EROFS`` or a run of permission refusals."""
        self.report.errors += 1
        if exc.errno == errno.EROFS:
            self._stop("read-only filesystem")
        self._refusals = self._refusals + 1 if exc.errno in (errno.EACCES, errno.EPERM) else 0
        if self._refusals >= _MAX_CONSECUTIVE_REFUSALS:
            self._stop("permission refused repeatedly")

    def open_parent(self, home_fd: int, parts: tuple[str, ...]) -> int | None:
        """Open the directory a store sits in, ``O_NOFOLLOW`` at every level below the home.

        None when it is absent, reached through a symlink, another account's, or
        on another filesystem: a store is looked for only where Kiro Crew keeps
        it. The directory itself is never changed.
        """
        fd = os.dup(home_fd)
        for part in parts:
            try:
                child = os.open(part, _DIR_OPEN_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in _SYMLINK_REFUSALS:
                    self.report.skipped_links += 1
                elif exc.errno != errno.ENOENT:
                    self.report.errors += 1
                os.close(fd)
                return None
            os.close(fd)
            fd = child
        try:
            st = os.fstat(fd)
        except OSError:
            self.report.errors += 1
            os.close(fd)
            return None
        if st.st_uid != self._uid or st.st_dev != self._root_dev:
            self.report.skipped_foreign += 1
            os.close(fd)
            return None
        return fd

    def names(self, parent_fd: int, parent: tuple[str, ...], leaves: tuple[str, ...]) -> list[str]:
        """The names *leaves* select in *parent_fd*: each literal one, then each pattern's matches.

        Only a pattern lists the directory. A directory that cannot be listed
        leaves the report incomplete, and the other stores are still swept.
        """
        names = list(dict.fromkeys(leaf for leaf in leaves if not _is_pattern(leaf)))
        patterns = [leaf for leaf in leaves if _is_pattern(leaf)]
        if not patterns:
            return names
        seen = set(names)
        try:
            with os.scandir(parent_fd) as entries:
                for item in entries:
                    # Listing is work too: a huge directory spends the same
                    # entry, time and shutdown budget as the walk below it.
                    self._tick()
                    if item.name not in seen and any(
                        fnmatch.fnmatchcase(item.name, pattern) for pattern in patterns
                    ):
                        seen.add(item.name)
                        names.append(item.name)
        except OSError:
            self.report.errors += 1
            where = "/".join(parent) or "the data home"
            self.report.stopped = self.report.stopped or f"cannot list {where}"
        return names

    def entry(self, dir_fd: int, name: str, depth: int) -> None:
        """Tighten *name* in *dir_fd*; a directory is narrowed and queued for :meth:`drain`."""
        self._tick()
        report = self.report
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return  # a store this install never created, or an entry removed meanwhile
        except OSError:
            report.errors += 1
            return
        if stat.S_ISLNK(st.st_mode):
            report.skipped_links += 1
            return
        is_dir = stat.S_ISDIR(st.st_mode)
        if not is_dir and not stat.S_ISREG(st.st_mode):
            return  # FIFO, socket, device: not a store, never opened
        if st.st_uid != self._uid or st.st_dev != self._root_dev:
            report.skipped_foreign += 1
            return
        if is_dir:
            self._directory(dir_fd, name, st, depth)
        else:
            self._file(dir_fd, name, st)

    def _file(self, dir_fd: int, name: str, st: os.stat_result) -> None:
        report = self.report
        if not stat.S_IMODE(st.st_mode) & GROUP_OTHER_BITS:
            return
        if st.st_nlink != 1:
            report.skipped_hard_linked += 1
            return
        if not self._files_supported:
            report.unsupported += 1
            return
        try:
            if _chmod_file_to_owner(name, st, dir_fd=dir_fd):
                report.tightened += 1
            else:
                report.errors += 1  # the name names a different inode from the lstat's
            self._refusals = 0
        except OSError as exc:
            self._failed(exc)

    def _directory(self, dir_fd: int, name: str, st: os.stat_result, depth: int) -> None:
        try:
            fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=dir_fd)
        except OSError as exc:
            self._failed(exc)
            return
        handed_over = False
        try:
            fst = os.fstat(fd)
            if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                self.report.errors += 1
                return
            if stat.S_IMODE(fst.st_mode) & GROUP_OTHER_BITS:
                os.fchmod(fd, stat.S_IMODE(fst.st_mode) & ~GROUP_OTHER_BITS)
                self.report.tightened += 1
            self._refusals = 0
            if depth + 1 < MAX_SWEEP_DEPTH:
                handed_over = True  # _push owns the descriptor from here, listed or not
                self._push(fd, depth + 1)
        except OSError as exc:
            self._failed(exc)
        finally:
            if not handed_over:
                os.close(fd)

    def _push(self, fd: int, depth: int) -> None:
        try:
            entries = os.scandir(fd)
        except OSError:
            self.report.errors += 1
            os.close(fd)
            return
        self._stack.append((fd, entries, depth))

    def drain(self) -> None:
        """Walk every directory :meth:`entry` queued, depth-first."""
        while self._stack:
            dir_fd, entries, depth = self._stack[-1]
            try:
                item = next(entries, None)
            except OSError:
                self.report.errors += 1
                item = None
            if item is None:
                self._stack.pop()
                entries.close()  # type: ignore[attr-defined]
                os.close(dir_fd)
                continue
            self.entry(dir_fd, item.name, depth)

    def close(self) -> None:
        """Release the descriptors of a sweep that stopped part-way."""
        for dir_fd, entries, _depth in self._stack:
            try:
                entries.close()  # type: ignore[attr-defined]
                os.close(dir_fd)
            except OSError:
                pass
        self._stack.clear()


def tighten_stores_to_owner(
    home: str | os.PathLike[str],
    stores: Iterable[str],
    *,
    max_seconds: float,
    max_entries: int,
    clock: Callable[[], float] = time.monotonic,
    should_stop: Callable[[], bool] | None = None,
) -> TightenReport:
    """Remove group/other permission bits from the named *stores* below *home*. Best effort.

    Each store is a path relative to *home*, written with ``/``. Its last
    component may be an ``fnmatch`` pattern (``gateway.log*``); the others are
    literal names. A store that is a file is tightened; one that is a directory
    is narrowed and everything below it is tightened. Nothing else below *home*
    is touched -- not the directories the stores sit in (``workspace/`` itself),
    and not any name the list does not select -- so whatever else a user or an
    agent keeps in the data home keeps the modes its own tools gave it. A store
    that does not exist is not an error.

    *home* itself is opened by name and may be a symlink (a relocated data home
    is a supported setup); its own mode is not changed here. Below it nothing is
    ever followed:

    * the directories leading to a store are opened with ``O_NOFOLLOW`` at every
      level, and a store reached through a symlink, another account's directory
      or another filesystem is skipped;
    * every entry is ``lstat``-ed relative to its parent's descriptor, and only
      regular files and directories are considered -- a symlink, FIFO, socket or
      device is skipped, and neither a symlink nor its target is modified;
    * a directory is entered with ``O_DIRECTORY | O_NOFOLLOW`` relative to the
      parent descriptor and re-checked by ``(st_dev, st_ino)``, so a name swapped
      between the ``lstat`` and the open is refused rather than followed;
    * a file is never opened for I/O: its mode is changed through
      :func:`_chmod_file_to_owner`, so the walk releases no record lock a
      database connection of this process holds;
    * a file with more than one hard link is skipped, because its mode is
      shared with a name that may live outside the data home;
    * an entry owned by another account, or a directory on another filesystem
      (a mount point), is skipped and not descended into.

    Directories are read incrementally, so a huge one costs budget as it is
    read rather than all at once. All the stores share one budget: *max_entries*
    and *max_seconds* (checked per entry, read from *clock*), plus
    :data:`MAX_SWEEP_DEPTH` below each store. The sweep stops early on ``EROFS``,
    a run of permission refusals, or *should_stop* answering True (also checked
    per entry). Whatever the filesystem answers, it never raises and logs
    nothing per entry; the report says how far it got. Entries already
    owner-only cost one ``lstat`` and nothing else. A store list that is not
    made of relative paths raises ``ValueError`` (:func:`_store_groups`). A no-op
    on Windows, where the mode bits carry no access control.
    """
    groups = _store_groups(stores)
    if not platform_compat.IS_POSIX:
        return TightenReport(stopped="not a POSIX platform")
    opened = _open_root(home)
    if isinstance(opened, TightenReport):
        return opened
    home_fd, home_st = opened
    sweep = _Sweep(
        home_st,
        max_seconds=max_seconds,
        max_entries=max_entries,
        clock=clock,
        should_stop=should_stop,
    )
    try:
        for parent, leaves in groups:
            parent_fd = sweep.open_parent(home_fd, parent)
            if parent_fd is None:
                continue
            try:
                for name in sweep.names(parent_fd, parent, leaves):
                    sweep.entry(parent_fd, name, 0)
                    sweep.drain()
            finally:
                os.close(parent_fd)
        sweep.report.complete = not sweep.report.stopped
    except _SweepStopped:
        pass
    finally:
        sweep.close()
        os.close(home_fd)
    return sweep.report


#: The startup sweep's budget. The sweep runs on a worker thread once the
#: dashboard listener is serving, so this bounds background I/O, not the time to
#: readiness: large enough that a real data home is covered in one pass (an entry
#: that is already owner-only costs one ``lstat``), small enough that a
#: pathological tree -- a slow network volume -- stops. Whatever a stopped sweep
#: did not reach keeps its mode until a later start, inside a data home that is
#: itself ``0700``.
STARTUP_SWEEP_MAX_SECONDS = 30.0
STARTUP_SWEEP_MAX_ENTRIES = 1_000_000

#: What the startup sweep repairs: Kiro Crew's own stores, by path relative to
#: the data home (:func:`tighten_stores_to_owner` says how a name is read). Each
#: is a store that an older version created at the umask default and that holds
#: something worth keeping private: conversations, memory, knowledge, logs,
#: configuration. The list is fixed on purpose. The sweep never walks the data
#: home looking for what to change, so it never needs to recognise a user's or
#: an agent's own work in order to spare it: a project in ``workspace/``, a
#: session workspace root ``kirocrew setup`` placed in the home, a data home
#: chosen as the working directory, ``scratch/`` and every app's files outside
#: the meetings store are simply not on it. ``skills/`` is not on it either: the
#: builtin-skill sync (``kiro_crew.skills``) fingerprints each installed entry's
#: permission bits to tell an untouched install from a user edit, so narrowing
#: them needs the fingerprint changed first.
#:
#: The writers converted alongside this list create their files owner-only, so
#: it only has to cover what they wrote before, and it leaves out the low-value
#: leftovers an older version also wrote (pid files, lock files, migration
#: markers): those keep their modes inside a data home that is itself ``0700``.
#: Every database a converted store opens is narrowed again on each open
#: (:func:`prepare_owner_only_sqlite`). Names that mirror a module constant are
#: spelled out because those modules import this one;
#: ``test_the_store_list_matches_the_stores_constants`` keeps them equal.
STARTUP_SWEEP_STORES: tuple[str, ...] = (
    # Conversations, agents, artifacts and schedules.
    "sessions",
    "subagents",
    "crew-log",
    "members",
    "artifacts",
    "cron-history",
    "crons.json",
    # Memory, knowledge and research.
    "memory.db*",
    "memory_index.db*",
    "skill_search_index.sqlite3*",
    "memory_stores",
    "backups",
    "workspace/memory",
    "workspace/knowledge",
    "workspace/research",
    "workspace/memory_index.db*",
    "workspace/HEARTBEAT.md",
    "workspace/ledger.jsonl",
    "apps/meetings/data",
    # Logs, audit, notifications and diagnostics.
    "gateway.log*",
    "audit.log",
    "notifications.jsonl",
    "security_events.jsonl",
    "security_events.d",
    "logs",
    "diag",
    "metrics",
    "usage",
    # Configuration.
    "config.json",
    "config.json.bak",
    "config.local.json",
)


def tighten_data_home(
    home: str | os.PathLike[str], *, should_stop: Callable[[], bool] | None = None
) -> TightenReport:
    """Run the once-per-start sweep of :data:`STARTUP_SWEEP_STORES` and log what it did.

    Never raises. The gateway runs this on a worker thread after its dashboard
    listener is serving (``dashboard.server_runtime.maintenance._kick_owner_only_sweep``),
    never on the boot path or the event loop, with *should_stop* reading the
    process shutdown flag so a stopping gateway does not wait out the budget.
    Nothing waits for it: every file and directory the services create is
    owner-only from creation, and the mode changes here never open a file, so
    the services' own connections keep their locks.

    There is no switch to keep wider modes on these stores: the home itself is
    made ``0700`` on every CLI start (``config.paths.ensure_data_home``), and
    while it is, a group or other bit on an entry below it grants no account any
    access, so the sweep removes nothing a reader can rely on.
    """
    try:
        report = tighten_stores_to_owner(
            home,
            STARTUP_SWEEP_STORES,
            max_seconds=STARTUP_SWEEP_MAX_SECONDS,
            max_entries=STARTUP_SWEEP_MAX_ENTRIES,
            should_stop=should_stop,
        )
    except Exception:  # never let a permission sweep take the gateway down
        logger.warning("owner-only sweep of the data home failed", exc_info=True)
        return TightenReport(stopped="failed")
    if report.tightened:
        logger.info(
            "Removed group/other access from %d file(s) and director(ies) in the data home",
            report.tightened,
        )
    if not report.complete and report.stopped not in (
        "not a POSIX platform",
        SWEEP_STOPPED_FOR_SHUTDOWN,
    ):
        logger.warning(
            "Owner-only sweep of the data home stopped early (%s) after %d entries; "
            "entries it did not reach keep their current permissions",
            report.stopped,
            report.visited,
        )
    elif report.errors:
        logger.warning(
            "Owner-only sweep of the data home could not tighten %d entr(ies); "
            "they keep their current permissions",
            report.errors,
        )
    if report.unsupported:
        logger.warning(
            "Owner-only sweep of the data home left %d file(s) as they are: this "
            "platform has no lock-safe way to change a file's mode (no /proc/self/fd "
            "and no no-follow chmod)",
            report.unsupported,
        )
    return report


def _policy_home() -> Path | None:
    """The data home as an absolute LEXICAL path, without creating it."""
    from kiro_crew.config.paths import peek_data_home

    try:
        return Path(os.path.abspath(peek_data_home()))
    except (OSError, RuntimeError, ValueError):
        return None


def is_owner_only_target(path: str | os.PathLike[str], home: Path | None = None) -> bool:
    """True when *path* lies inside the data home (lexically, after ``abspath``).

    The cheap containment test the writers use to decide whether the owner-only
    policy applies. Lexical on purpose: it costs no syscall on a hot write path,
    and every writer that targets the data home builds its path from the
    resolved home. A path that reaches the home through some other spelling
    falls back to the writer's previous mode, which the root's ``0700`` covers
    (and, for a listed store, the startup sweep). *home* defaults to the current
    data home.
    """
    if home is None:
        home = _policy_home()
        if home is None:
            return False
    try:
        candidate = Path(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError):
        return False
    return candidate == home or home in candidate.parents

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
3. **What already exists.** :func:`tighten_tree_to_owner` runs once per gateway
   start and drops group/other bits from everything an earlier version, a
   writer that has not been converted, or a foreign tool left behind.

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
import logging
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_NOCTTY = getattr(os, "O_NOCTTY", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

#: Read-only, no-follow, non-blocking open used to address one existing entry by
#: descriptor. ``O_NONBLOCK`` so a FIFO swapped in for a regular file cannot hang
#: the caller; ``O_NOCTTY`` so a terminal device can never become the controlling
#: terminal; ``O_NOFOLLOW`` so a symlink at the name is refused (``ELOOP``) instead
#: of handing its target to ``fchmod``.
_ENTRY_OPEN_FLAGS = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_NOCTTY | _O_CLOEXEC
_DIR_OPEN_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC


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


def write_text_owner_only(
    path: str | os.PathLike[str], text: str, *, encoding: str = "utf-8"
) -> None:
    """``Path.write_text`` that creates the file ``0600``.

    Same semantics otherwise -- truncate in place, follow a symlink at the name
    -- so a site can switch to it without changing what it writes or where.
    """
    with open(path, "w", encoding=encoding, opener=owner_only_opener) as handle:
        handle.write(text)


def mkdirs_owner_only(path: str | os.PathLike[str]) -> None:
    """``mkdir -p`` where EVERY directory it creates is ``0700``.

    :meth:`pathlib.Path.mkdir` and :func:`os.makedirs` apply *mode* to the last
    component only and create the intermediate ones at the umask default, so a
    fresh ``sessions/<key>/`` would get an owner-only leaf under a ``0755``
    parent. Existing directories are left exactly as they are: this decides how
    a directory is born, not what an existing one becomes (the startup sweep's
    job). Raises like ``Path.mkdir(parents=True, exist_ok=True)``, including
    ``FileExistsError`` when a non-directory sits at a name it needs.
    """
    target = os.path.abspath(os.fspath(path))
    try:
        os.mkdir(target, OWNER_ONLY_DIR_MODE)
    except FileExistsError:
        if not os.path.isdir(target):
            raise
    except FileNotFoundError:
        parent = os.path.dirname(target)
        if parent == target:
            raise
        mkdirs_owner_only(parent)
        try:
            os.mkdir(target, OWNER_ONLY_DIR_MODE)
        except FileExistsError:
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


def _drop_group_other(fd: int, expected: os.stat_result | None = None) -> bool:
    """``fchmod`` the regular file open on *fd* to its mode minus group/other.

    Returns True when the mode changed. Refuses -- returns False without touching
    anything -- unless the descriptor is a regular file this process owns with
    exactly one name (``st_nlink == 1``), and, when *expected* is given, the same
    inode the caller ``lstat``-ed. The link count matters because a mode lives on
    the inode: a hard link planted in the data home to a file elsewhere would
    otherwise turn this into a ``chmod`` of that file.
    """
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1:
        return False
    if expected is not None and (st.st_dev, st.st_ino) != (expected.st_dev, expected.st_ino):
        return False
    mode = stat.S_IMODE(st.st_mode)
    if not mode & GROUP_OTHER_BITS:
        return False
    platform_compat.fchmod_safe(fd, mode & ~GROUP_OTHER_BITS)
    return True


def tighten_file_to_owner(path: str | os.PathLike[str]) -> bool:
    """Drop group/other bits from the existing regular file at *path*, best effort.

    Never follows a symlink at the name (``O_NOFOLLOW``), never touches a file
    with another hard link or another owner, and never raises: a missing file,
    a link, ``EPERM`` or ``EROFS`` all answer False. A no-op returning False on
    Windows.
    """
    if not platform_compat.IS_POSIX:
        return False
    try:
        fd = os.open(os.fspath(path), _ENTRY_OPEN_FLAGS)
    except OSError:
        return False
    try:
        return _drop_group_other(fd)
    except OSError:
        logger.debug("could not tighten %s to owner-only", path, exc_info=True)
        return False
    finally:
        os.close(fd)


def prepare_owner_only_sqlite(db_path: str | os.PathLike[str]) -> None:
    """Make sure SQLite opens *db_path* as an owner-only file. Call before ``connect``.

    SQLite creates a missing database at ``0644 & ~umask`` and gives its
    ``-wal``/``-shm``/``-journal`` sidecars the database's own mode (see
    :data:`SQLITE_SIDECAR_SUFFIXES`). So a database that already exists as an
    empty ``0600`` file when the connect runs comes out owner-only together with
    every sidecar SQLite creates for it, with no window in which any of them is
    readable. An empty file is a valid empty database to SQLite.

    * Missing database: created ``0600`` with ``O_EXCL | O_NOFOLLOW``, so a name
      that appears concurrently, or a dangling symlink, is not created through.
    * Existing database and sidecars: group/other bits dropped through a
      no-follow descriptor (:func:`tighten_file_to_owner`).
    * A symlink at the database name is left alone, as is its target: the
      operator pointed it somewhere on purpose.

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
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
            OWNER_ONLY_FILE_MODE,
        )
    except FileExistsError:
        tighten_file_to_owner(name)
    except OSError:
        # A missing parent, a read-only volume, a permission problem: SQLite will
        # report the real failure itself when it opens the file.
        logger.debug("could not pre-create %s owner-only", name, exc_info=True)
    else:
        os.close(fd)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        tighten_file_to_owner(name + suffix)


@dataclass
class TightenReport:
    """What one :func:`tighten_tree_to_owner` pass did."""

    visited: int = 0
    tightened: int = 0
    skipped_links: int = 0
    skipped_hard_linked: int = 0
    skipped_foreign: int = 0
    errors: int = 0
    complete: bool = False
    stopped: str = ""


#: Deepest directory level the sweep descends to. Bounds the descriptors held
#: open (one per level) as well as the walk; the data home's real layout is a
#: handful of levels deep.
MAX_SWEEP_DEPTH = 48

#: Consecutive permission refusals after which the sweep stops: a volume that
#: refuses every ``chmod`` (macOS ``com.apple.provenance``, a foreign owner)
#: would otherwise spend the whole budget learning nothing new.
_MAX_CONSECUTIVE_REFUSALS = 64


def tighten_tree_to_owner(
    root: str | os.PathLike[str],
    *,
    max_seconds: float,
    max_entries: int,
    skip_top_level: frozenset[str] = frozenset(),
    clock: Callable[[], float] = time.monotonic,
) -> TightenReport:
    """Remove group/other permission bits from everything below *root*. Best effort.

    *root* itself is opened by name and may be a symlink (a relocated data home
    is a supported setup); its own mode is not changed here. Below it the walk
    never follows a link:

    * every entry is ``lstat``-ed relative to its parent's descriptor, and only
      regular files and directories are considered -- a symlink, FIFO, socket or
      device is skipped, and neither a symlink nor its target is modified;
    * a directory is entered with ``O_DIRECTORY | O_NOFOLLOW`` relative to the
      parent descriptor and re-checked by ``(st_dev, st_ino)``, and a file is
      tightened through an ``O_NOFOLLOW`` descriptor re-checked the same way,
      so a name swapped between the ``lstat`` and the open is refused rather
      than followed;
    * a file with more than one hard link is skipped, because its mode is
      shared with a name that may live outside the data home;
    * an entry owned by another account, or a directory on another filesystem
      (a mount point), is skipped and not descended into.

    Bounded by *max_entries* and *max_seconds* (checked per entry, read from
    *clock*) and by :data:`MAX_SWEEP_DEPTH`, and stops early on ``EROFS`` or a
    run of permission refusals. Never raises; the report says how far it got.
    Entries already owner-only cost one ``lstat`` and nothing else. Names in
    *skip_top_level* are not entered when they appear directly under *root*.
    A no-op on Windows, where the mode bits carry no access control.
    """
    report = TightenReport()
    if not platform_compat.IS_POSIX:
        report.stopped = "not a POSIX platform"
        return report
    deadline = clock() + max_seconds
    uid = os.geteuid()
    try:
        root_fd = os.open(os.fspath(root), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    except OSError as exc:
        report.errors += 1
        report.stopped = f"cannot open the root: {exc.strerror or exc}"
        return report
    try:
        root_dev = os.fstat(root_fd).st_dev
    except OSError as exc:
        os.close(root_fd)
        report.errors += 1
        report.stopped = f"cannot stat the root: {exc.strerror or exc}"
        return report

    # One frame per open directory: (descriptor, names still to visit, depth).
    # Depth-first, so the descriptors held open are one per level, never one per
    # pending sibling.
    stack: list[tuple[int, list[str], int]] = []
    refusals = 0

    def _push(fd: int, depth: int) -> bool:
        try:
            names = os.listdir(fd)
        except OSError:
            report.errors += 1
            os.close(fd)
            return False
        names.sort(reverse=True)  # pop() then visits in name order
        stack.append((fd, names, depth))
        return True

    try:
        _push(root_fd, 0)
        while stack:
            dir_fd, names, depth = stack[-1]
            if not names:
                stack.pop()
                os.close(dir_fd)
                continue
            name = names.pop()
            if depth == 0 and name in skip_top_level:
                continue
            report.visited += 1
            if report.visited > max_entries:
                report.stopped = f"entry budget of {max_entries} reached"
                return report
            if clock() > deadline:
                report.stopped = f"time budget of {max_seconds:g}s reached"
                return report
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                report.errors += 1
                continue
            if stat.S_ISLNK(st.st_mode):
                report.skipped_links += 1
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            if not is_dir and not stat.S_ISREG(st.st_mode):
                continue  # FIFO, socket, device: not a store, never opened
            if st.st_uid != uid or st.st_dev != root_dev:
                report.skipped_foreign += 1
                continue
            needs_tightening = bool(stat.S_IMODE(st.st_mode) & GROUP_OTHER_BITS)
            if not is_dir:
                if not needs_tightening:
                    continue
                if st.st_nlink != 1:
                    report.skipped_hard_linked += 1
                    continue
            try:
                fd = os.open(name, _DIR_OPEN_FLAGS if is_dir else _ENTRY_OPEN_FLAGS, dir_fd=dir_fd)
            except OSError as exc:
                report.errors += 1
                refusals = refusals + 1 if exc.errno in (errno.EACCES, errno.EPERM) else 0
                if refusals >= _MAX_CONSECUTIVE_REFUSALS:
                    report.stopped = "permission refused repeatedly"
                    return report
                continue
            keep_open = False
            try:
                if is_dir:
                    fst = os.fstat(fd)
                    if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                        report.errors += 1
                        continue
                    if stat.S_IMODE(fst.st_mode) & GROUP_OTHER_BITS:
                        platform_compat.fchmod_safe(
                            fd, stat.S_IMODE(fst.st_mode) & ~GROUP_OTHER_BITS
                        )
                        report.tightened += 1
                    if depth + 1 < MAX_SWEEP_DEPTH:
                        keep_open = _push(fd, depth + 1)
                        # _push closes the descriptor itself when it cannot list it.
                        fd = -1 if not keep_open else fd
                elif _drop_group_other(fd, expected=st):
                    report.tightened += 1
                refusals = 0
            except OSError as exc:
                report.errors += 1
                if exc.errno == errno.EROFS:
                    report.stopped = "read-only filesystem"
                    return report
                refusals = refusals + 1 if exc.errno in (errno.EACCES, errno.EPERM) else 0
                if refusals >= _MAX_CONSECUTIVE_REFUSALS:
                    report.stopped = "permission refused repeatedly"
                    return report
            finally:
                if not keep_open and fd >= 0:
                    os.close(fd)
        report.complete = True
        return report
    finally:
        for dir_fd, _names, _depth in stack:
            try:
                os.close(dir_fd)
            except OSError:
                pass


#: The startup sweep's budget. Generous for a real data home (a long-lived one
#: holds a few thousand entries and takes milliseconds) while keeping a pathological
#: tree -- a huge cloned repository in the workspace, a slow network volume -- from
#: holding up the gateway's start. Whatever a stopped sweep did not reach keeps its
#: mode until a later start, inside a data home that is itself ``0700``.
STARTUP_SWEEP_MAX_SECONDS = 3.0
STARTUP_SWEEP_MAX_ENTRIES = 250_000

#: Top-level data-home entries the startup sweep leaves alone. ``skills/`` holds
#: the installed skill trees, and the builtin-skill sync (``kiro_crew.skills``)
#: fingerprints every entry's permission bits to tell an untouched install from
#: a user-edited one: removing group/other bits there would make every
#: installed builtin read as "edited", so the next sync would back each one up
#: and stop treating it as its own. Tightening that tree needs the fingerprint
#: to ignore group/other bits first, which is a change to the skill sync, not
#: to this sweep. Until then those files keep their packaged modes inside a
#: data home that is itself 0700.
STARTUP_SWEEP_SKIPPED = frozenset({"skills"})


def tighten_data_home(home: str | os.PathLike[str]) -> TightenReport:
    """Run the once-per-start sweep over *home* and log what it did. Never raises.

    The gateway calls this from its synchronous CLI prologue, after
    ``ensure_data_home`` made the home itself ``0700`` and before any service
    starts, so the walk never runs on the event loop.
    """
    try:
        report = tighten_tree_to_owner(
            home,
            max_seconds=STARTUP_SWEEP_MAX_SECONDS,
            max_entries=STARTUP_SWEEP_MAX_ENTRIES,
            skip_top_level=STARTUP_SWEEP_SKIPPED,
        )
    except Exception:  # never let a permission sweep stop the gateway from starting
        logger.warning("owner-only sweep of the data home failed", exc_info=True)
        return TightenReport(stopped="failed")
    if report.tightened:
        logger.info(
            "Removed group/other access from %d file(s) and director(ies) in the data home",
            report.tightened,
        )
    if not report.complete and report.stopped != "not a POSIX platform":
        logger.warning(
            "Owner-only sweep of the data home stopped early (%s) after %d entries; "
            "entries it did not reach keep their current permissions",
            report.stopped,
            report.visited,
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
    falls back to the writer's previous mode, which the root's ``0700`` and the
    startup sweep still cover. *home* defaults to the current data home.
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

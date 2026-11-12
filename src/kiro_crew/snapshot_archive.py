"""The bundle format: how a tree is staged into an archive, and how an archive is screened.

Writing side: the pinned tree copy every staging, install and rollback path uses, the
refusal where the platform cannot pin, and the consistent capture of a live SQLite
database. Reading side: the member filter extraction runs through, the size bound checked
before anything is extracted, and the manifest readers. A name that comes out of an
archive is escaped here before it reaches a terminal.

What a bundle RECORDS is composed by the snapshot build
(:func:`kiro_crew.snapshot_build._build_snapshot`), which writes ``MANIFEST.json``.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat as _stat
import tarfile
import zlib
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Callable

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME
from kiro_crew.snapshot_components import (
    COMPONENTS,
    SECURITY_SENSITIVE_FILES,
    _facade,
    _is_host_local,
    _never_ships,
    _tree_roots_replace_clears,
    is_product_tree_database,
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _escape_one(ch: str) -> str:
    """Render one stripped character in a form that cannot be re-interpreted.

    Two widths, because a single `\\xNN` spelling would be a lie for a code point above
    0xFF: `\\x202e` reads as `\\x20` followed by a literal `2e`, which is exactly the kind
    of ambiguous output this function exists to prevent.
    """
    code = ord(ch)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def _safe_name(value: object, default: str = "unknown", limit: int = 300) -> str:
    """Render a name that came out of an ARCHIVE printable.

    Tar member names, manifest keys and archive root directories are all chosen by
    whoever wrote the bundle. Printing one raw means the terminal INTERPRETS whatever
    escape sequences it holds: the cursor moves, lines get overwritten, and a hostile
    archive can dress itself up as a different, expected one -- right above the prompt
    where the operator decides whether to restore it. Two of these sites print while
    REJECTING a hostile entry, so the raw name there is precisely the attacker's payload.

    What is escaped is a name out of an untrusted archive, so the helper lives with its
    caller and its reason is stated in those terms. The length is capped so one very
    long name cannot flood the view.
    """
    cleaned = _CONTROL_CHARS.sub(lambda m: _escape_one(m.group()), str(value if value else default))
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…(truncated)"
    return cleaned


def _rejection_recording_filter(
    rejected: list[str],
) -> "Callable[..., tarfile.TarInfo | None]":
    """`_data_filter`, plus a record of every entry it DROPPED for a structural reason.

    Extraction warns, drops a rejected entry and continues, so a bundle can arrive missing
    part of its payload while the manifest still declares it. In replace mode that deletes
    data: the memory trees are cleared unconditionally and nothing replaces the dropped one.

    Only extraction can tell the two cases apart. At the mutation phase a rejected link and
    an archive that never carried the tree leave the same state (the staged tree is absent).

    A `_never_ships` drop is NOT recorded: those are deliberate. Nor is a rejection outside
    a tree that replace clears: there a hostile entry is dropped and the restore succeeds
    (`test_symlink_filtered_out`). Only inside a cleared tree does a drop delete the
    operator's own tree.
    """
    cleared_trees = _tree_roots_replace_clears()

    def _f(info: tarfile.TarInfo, dest: str = "") -> tarfile.TarInfo | None:
        kept = _data_filter(info, dest)
        if kept is None and not _never_ships(info.name):
            # Entries are `<bundle-root>/<tree>/...`; the tree is what decides.
            parts = PurePosixPath(info.name).parts
            if len(parts) > 1 and parts[1] in cleared_trees:
                rejected.append(_safe_name(info.name))
        return kept

    return _f


def _data_filter(info: tarfile.TarInfo, _dest: str = "") -> tarfile.TarInfo | None:
    """Equivalent to tarfile ``"data"`` filter (Python 3.12+), with 3.10 fallback.

    Also rejects path traversal, symlinks, hardlinks, and special files
    (FIFO and device members) to eliminate TOCTOU race between pre-scan
    and extraction.
    Excludes sel_hmac.key (must be regenerated on restore, not shipped).
    Security-sensitive files get 0o600 permissions.
    """
    # Reject path traversal. POSIX checks apply everywhere; the Windows-syntax
    # checks (backslash separators, drive letters — incl. the drive-RELATIVE
    # `C:foo` form is_absolute() misses, which resolves against the drive CWD
    # at extraction) apply ONLY when extracting on Windows, where tarfile
    # honors '\' as a native separator. They must NOT run on POSIX: ':' and
    # '\' are legal characters in Linux/macOS filenames, so a workspace file
    # named `a:1` or `notes..\old` would be silently dropped from a
    # Linux-to-Linux restore.
    name = info.name
    traversal = (
        name.startswith("/")
        or ".." in PurePosixPath(name).parts
        or PurePosixPath(name).is_absolute()
    )
    if not traversal and platform_compat.IS_WINDOWS:
        traversal = (
            name.startswith("\\")
            or ".." in PureWindowsPath(name).parts
            or PureWindowsPath(name).is_absolute()
            or bool(PureWindowsPath(name).drive)
        )
    if traversal:
        print(f"⚠️  Rejecting path traversal entry: {_safe_name(info.name)}")
        return None
    # Reject symlinks and hardlinks
    if info.issym() or info.islnk():
        print(f"⚠️  Rejecting symlink/hardlink entry: {_safe_name(info.name)}")
        return None
    # Reject special files (FIFO, character/block device), as the "data" filter does. A
    # staged FIFO blocks any later pass that opens it for reading, and bundle content is
    # only ever regular files and directories.
    if not (info.isreg() or info.isdir()):
        print(f"⚠️  Rejecting special file entry: {_safe_name(info.name)}")
        return None
    # Never ship these — each must be regenerated on the restoring host, or is that
    # host's own runtime state (the host-local half of ``memory_stores/``).
    basename = PurePosixPath(info.name).name
    if _never_ships(info.name):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    # Security-sensitive files get restricted permissions. So does everything under
    # memory_stores/: a store is provisioned owner-only on the writing host, and a restore
    # should land it the way provisioning would rather than at the tar default.
    parts = PurePosixPath(info.name).parts
    private_store = len(parts) >= 2 and parts[1] == MEMORY_STORES_DIR_NAME
    if info.isdir():
        info.mode = 0o700 if private_store else 0o755
    elif private_store or basename in SECURITY_SENSITIVE_FILES:
        info.mode = 0o600
    else:
        info.mode = 0o644
    return info


#: The manifest format this build writes. Bumped when a bundle's SHAPE changes in a way
#: a restore has to know about, not per release: v3 introduced the component map; v4
#: bundles carry the ``memory_stores/`` tree under `memory`. Replace mode reads it to
#: tell "the source had no named stores" from "the writer did not know about them" --
#: see `_bundle_carries_named_stores`.
MANIFEST_VERSION = 4


_FIRST_VERSION_WITH_NAMED_STORES = 4


#: The dashboard export's zip manifest (`portability.create_export_zip`) is a separate
#: line with its own history; v3 is where it gained the ``memory_stores/`` tree. Owned
#: here rather than in `portability` because that module imports this one, and the
#: restore side has to read the threshold.
EXPORT_MANIFEST_VERSION = 3


_FIRST_EXPORT_VERSION_WITH_NAMED_STORES = 3


# SQLite sidecars are excluded from every staged tree. They describe the SOURCE
# database's in-flight transaction state; shipping them next to a consistent backup
# copy would invite the restoring host to replay a journal that does not match it.
#
# Not redundant with _restage_databases, though it looks that way: re-opening a
# staged database makes SQLite discard the copied sidecars as a side effect, so for a
# real database either mechanism alone appears to work. This glob is what covers the
# case _restage_databases SKIPS — a file named .db that SQLite cannot open, whose
# stray sidecars would otherwise ride.
_DB_SIDECAR_GLOBS = ("*.db-wal", "*.db-shm", "*.db-journal", "*.sqlite3-wal", "*.sqlite3-shm")


# Suffixes treated as SQLite databases when found inside a staged tree.
_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")


class DatabaseCopyFailed(Exception):
    """A readable database could not be copied consistently.

    Carries the source path so the command boundary can name the file. Raised rather
    than absorbed because the staged copy at that point is a raw byte copy without its
    WAL sidecars — shipping it would put a torn database in a bundle that reports
    success — and typed rather than bare so the failure exits with a message instead of
    a traceback.
    """

    def __init__(self, path: Path, cause: Exception) -> None:
        super().__init__(f"{path}: {cause}")
        self.path = path


def _chain_is_link_free(root: Path, rel_parts: tuple[str, ...]) -> bool:
    """Is every component of *rel_parts* under *root* a real directory, not a link?

    Walked with descriptors: each directory is opened relative to the previous one with
    ``O_NOFOLLOW``, so a component that is a link -- or one swapped for a link while this
    pass runs -- fails its own open instead of redirecting the walk. The final component is
    the file itself and is checked as a regular file through its pinned parent.

    Verifying a path and then re-walking it by name are two resolutions of one string: the
    second can land somewhere the first never inspected. ``resolve()`` and a late
    ``realpath()`` are both that second walk.

    Where the platform cannot open relative to a directory descriptor (Windows: no
    ``O_NOFOLLOW``, and ``os.open`` with ``dir_fd`` raises ``NotImplementedError``), this
    returns True and the caller relies on the by-name screening it already did. That is the
    same degradation ``_staging_is_pinned`` applies elsewhere; returning False would refuse
    every database on that platform.
    """
    if not pinned_fs.supports_pinned_walk():
        return True
    try:
        fd = pinned_fs.open_dir_pinned(root, what="database source root")
    except OSError:
        # Gone or unopenable, like an intermediate component below: unverifiable. A root lost
        # to a concurrent rename arrives as `FileNotFoundError`, which `_build_snapshot`'s try
        # does not handle; returning False routes it through the caller's `require_database`
        # split (named refusal for a declared file, recorded skip for a tree file).
        # `PinnedPathRefusal` is NOT caught: `snapshot_main` audits it as `unpinnable_staging`.
        return False
    try:
        for part in rel_parts[:-1]:
            try:
                nxt = os.open(part, _dir_flags_nofollow(), dir_fd=fd)
            except OSError:
                return False  # a link, or gone: either way this file is not reachable safely
            os.close(fd)
            fd = nxt
        return pinned_fs.is_regular_at(fd, rel_parts[-1])
    except pinned_fs.PinnedPathRefusal:
        return False
    finally:
        os.close(fd)


def _dir_flags_nofollow() -> int:
    """``O_RDONLY|O_DIRECTORY|O_NOFOLLOW``, with the flags that only exist on some platforms
    added when present."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return flags | getattr(os, "O_CLOEXEC", 0)


# Outcomes of `_copy_database_consistently`. Three, not a bool, because the two
# non-success cases need OPPOSITE handling from the caller and collapsing them is how a
# bundle ends up carrying a database nobody copied consistently while reporting success:
#
#   COPIED         the backup API produced a consistent copy at the destination.
#   NOT_A_DATABASE the file is positively NOT SQLite, so the caller should stage its bytes
#                  -- a non-database named `.db` is still the operator's file.
#   UNSAFE_SOURCE  the source could not be verified as reachable without traversing a
#                  link, so nothing was read from it. The caller must NOT substitute a
#                  byte copy: that is the read this refusal exists to prevent.
#
# Anything else raises `DatabaseCopyFailed`. A database that IS readable but could not be
# copied is never degraded to a byte copy, because this module excludes `-wal`/`-shm` and
# such a copy would be a torn database shipped as a whole one.
DB_COPIED = "copied"


DB_NOT_A_DATABASE = "not_a_database"


DB_UNSAFE_SOURCE = "unsafe_source"


# Recorded in MANIFEST.json when a database was staged as bytes rather than consistently.
# An archive whose manifest names the degradation is recoverable information; a quietly
# inconsistent database in an archive that reports success is not.
SKIP_DB_UNPINNED_SOURCE = "db_unpinned_source"


def _make_staging_copy_writable(dst: Path) -> None:
    """Add owner write to *dst* when it is a plain file this is about to overwrite.

    *dst* is either a byte copy the tree walk placed in this process's own staging tree
    or a name nothing occupies yet. A walk-placed copy carries the source file's mode, so
    one made from a store that is read-only by permission is itself read-only, and the
    read-write open that writes the consistent copy over it is refused. Owner write is
    added through a no-follow ``lstat``/``chmod`` on that private copy; a name with nothing
    at it, a link, or anything not a plain file is left for the open itself to answer.
    Windows honours only the read-only flag, which adding owner write clears.
    """
    try:
        mode = os.lstat(dst).st_mode
    except OSError:
        return
    if _stat.S_ISREG(mode) and not mode & _stat.S_IWUSR:
        try:
            os.chmod(dst, _stat.S_IMODE(mode) | _stat.S_IWUSR)
        except OSError:
            # Left for the read-write open below to answer, named against the database.
            pass


def _copy_database_consistently(
    src: Path,
    dst: Path,
    *,
    root: Path,
    rel_parts: tuple[str, ...],
    require_database: bool = False,
) -> str:
    """Copy the live SQLite database *src* to *dst* through the backup API, read-only.

    The one place this module reads a live database on the creation path. Both staging
    paths (the core-file list and the tree re-stage pass) call it so the hardening below
    exists once.

    **The chain is verified through descriptors, not by name.** Every component from *root*
    down is opened relative to the previous one with ``O_NOFOLLOW``, so a link, or one
    swapped in while this runs, fails its own open. ``resolve()`` or ``realpath()`` would
    be a second by-name walk that can land on a database this function never inspected.

    **The URI is percent-escaped** with ``as_uri()``: a ``?`` or ``#`` in a POSIX name would
    otherwise start the query or fragment and open a DIFFERENT database. The probe and the
    copy share one URI.

    **The connection is ``mode=ro``.** A read-write open MUTATES the live database: it
    replays an unreplayed ``-wal`` into the main file and unlinks the log, and hands a
    swapped-in database a writable handle. When ``mode=ro`` is refused because the view
    cannot hold a closed WAL database's index, the file is read ``immutable=1`` instead
    (:func:`_immutable_read_may_stand_in`) under the lock an ordinary reader holds.

    **"Not a database" is told apart from "cannot read this database"** by probing
    separately from copying, not by matching the error: ``sqlite_errorname`` is 3.11+ (this
    package supports 3.10), and "database is locked" is also a ``DatabaseError``, so a
    broad match would byte-copy a locked database as if it were consistent.

    Not closed here: SQLite takes a PATH, not a descriptor (it refuses ``/proc/self/fd/N``
    and ``/dev/fd/N``), so a same-uid swap between the check and SQLite's own open is not
    detectable. Closing it needs a descriptor-taking VFS.

    No checkpoint is needed: the backup API reads a consistent snapshot that INCLUDES rows
    living only in the ``-wal``, so there is no check-then-copy race.
    """
    if not _facade()._chain_is_link_free(root, rel_parts):
        if require_database:
            # Same reason as the not-a-database case below: a snapshot that omitted a
            # REQUIRED database must not succeed, or `--keep N` prunes the last complete
            # archive in favour of one missing the database it claims.
            raise DatabaseCopyFailed(
                src,
                pinned_fs.PinnedPathRefusal(
                    f"{'/'.join(rel_parts)} could not be verified as reachable without "
                    "following a link, so it was not read"
                ),
            )
        return DB_UNSAFE_SOURCE
    ro_uri = f"{src.absolute().as_uri()}?mode=ro"
    immutable_for: Exception | None = None
    try:
        try:
            with closing(_facade().sqlite3.connect(ro_uri, uri=True)) as probe:
                probe.execute("PRAGMA schema_version").fetchone()
        except _facade().sqlite3.DatabaseError as refused:
            if not _immutable_read_may_stand_in(src, refused):
                raise
            immutable_for = refused
            ro_uri += "&immutable=1"
            with closing(_facade().sqlite3.connect(ro_uri, uri=True)) as probe:
                probe.execute("PRAGMA schema_version").fetchone()
    except _facade().sqlite3.DatabaseError as e:
        # `sqlite_errorname` is read defensively for 3.10 and the message is the
        # documented fallback. Either way the DEFAULT is to raise: an error this code
        # cannot classify is not evidence the file is safe to copy byte-for-byte.
        name = getattr(e, "sqlite_errorname", "")
        not_a_database = name == "SQLITE_NOTADB" or (
            not name and "not a database" in str(e).lower()
        )
        if not not_a_database:
            raise DatabaseCopyFailed(src, e) from e
        if require_database:
            # A DECLARED component file, so this is a hard failure. A corrupt `memory.db`
            # staged as bytes would make the snapshot succeed, `--keep N` would prune a real
            # backup for it, and restore would refuse it: no restorable backup left.
            # A `.db` found by the TREE walk is incidental (some file in the workspace), and
            # refusing the whole snapshot over it would be an outage, so it is staged as bytes.
            raise DatabaseCopyFailed(src, e) from e
        return DB_NOT_A_DATABASE
    # The connects are INSIDE the try, not just `backup()`. Between the probe above and
    # this open the file can disappear -- and `mode=ro` makes that a hard failure where a
    # read-write open would silently CREATE an empty database, so the window matters more,
    # not less. `sqlite3.connect` then raises `OperationalError`, which `snapshot_main`
    # does not catch (it handles PinnedPathRefusal, UnsafeComponentRoot, DatabaseCopyFailed
    # and _ArchiveTooLarge), so letting it escape exits on a traceback instead of naming
    # the database.
    # The destination is a byte copy the tree walk already placed, or a name this is the
    # first to create. Where the walk placed one, it carried the SOURCE file's mode across
    # -- and a store a reader may open but not write (read-only by permission, not by a
    # read-only mount, where the files keep an ordinary mode) is staged read-only too. A
    # read-write `sqlite3.connect` on that copy then answers "attempt to write a readonly
    # database" on the DESTINATION, failing a snapshot the source read already handled.
    # The copy is this process's own file in its private staging tree, so owner write is
    # restored on it before the consistent copy overwrites it; the source is never touched.
    _make_staging_copy_writable(dst)
    try:
        with (
            closing(_facade().sqlite3.connect(ro_uri, uri=True)) as src_conn,
            closing(_facade().sqlite3.connect(str(dst))) as dst_conn,
            ExitStack() as reader_lock,
        ):
            # An immutable read takes no lock, so the reader lock is held from before the
            # copy until after the check that follows it, and the source connection stays
            # open across both: closing any descriptor on the file releases that lock.
            immutable_from = (
                None
                if immutable_for is None
                else reader_lock.enter_context(_hold_reader_lock(src, immutable_for))
            )
            # The file is a readable database, so a failure here means the consistent copy
            # did not happen. Absorbing it would leave the caller's byte copy -- taken
            # WITHOUT the `-wal` this module excludes -- passing for a whole database, so
            # it is raised, but typed and naming the file so the command boundary reports
            # which database failed instead of exiting on a traceback.
            src_conn.backup(dst_conn)
            if immutable_from is not None:
                _refuse_if_changed_since(src, immutable_from, dst)
    except _facade().sqlite3.Error as e:
        raise DatabaseCopyFailed(src, e) from e
    if require_database:
        _refuse_unsound_required_capture(src, dst)
    return DB_COPIED


def _refuse_if_changed_since(src: Path, before: os.stat_result, dst: Path) -> None:
    """Raise unless *src* still has the identity its immutable copy started from.

    Runs while :func:`_hold_reader_lock` is still held. A writer that opened the
    database through another view during the copy leaves a ``-wal`` or a ``-journal``
    beside the file, and that lock keeps its last close from removing either, so the
    sidecar check finds it whatever the file's timestamps show. A checkpoint into the
    main file also changes its size, mtime or ctime, and the staged copy *dst* must
    pass SQLite's ``quick_check``. Rows such a writer left only in its ``-wal`` were
    committed after the copy began and are not owed to it.
    """

    def identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    try:
        unchanged = identity(os.lstat(src)) == identity(before) and not any(
            os.path.lexists(f"{src}{sidecar}") for sidecar in ("-wal", "-journal")
        )
    except OSError as e:
        raise DatabaseCopyFailed(src, e) from e
    changed = OSError("the database changed while it was being copied; run it again")
    if not unchanged:
        raise DatabaseCopyFailed(src, changed)
    try:
        with closing(_facade().sqlite3.connect(str(dst))) as check:
            verdict = check.execute("PRAGMA quick_check").fetchone()[0]
    except _facade().sqlite3.Error as e:
        raise DatabaseCopyFailed(src, e) from e
    if verdict != "ok":
        raise DatabaseCopyFailed(src, changed)


def _immutable_read_may_stand_in(src: Path, refused: Exception) -> bool:
    """Whether an immutable read of *src* may stand in for the refused ordinary one.

    ``mode=ro`` is not enough where this process's view refuses writes beside the file,
    such as the read-only ``memory_stores/`` a script cron's sandbox binds. Opening a WAL
    database needs its ``-shm`` index, which SQLite creates beside the file, so a WAL
    database no other connection holds open refuses even a read-only open there. The
    decision rests on that open's own result code, not on a prediction of the view's
    writability: SQLite answers ``SQLITE_READONLY`` (its extended code names the cause),
    ``SQLITE_CANTOPEN`` or ``SQLITE_PERM``, and every other refusal keeps its error.
    With neither a ``-wal`` nor a ``-journal`` beside it, the main file alone holds every
    committed row, and ``immutable=1`` reads it without the index. A ``-wal`` holds rows
    the main file lacks and a ``-journal`` is a rollback in progress, so either keeps
    the refusal. The copy itself also needs :func:`_hold_reader_lock`.
    """
    driver = _facade().sqlite3
    code = getattr(refused, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    if code & 0xFF not in (driver.SQLITE_READONLY, driver.SQLITE_CANTOPEN, driver.SQLITE_PERM):
        return False
    return not any(os.path.lexists(f"{src}{sidecar}") for sidecar in ("-wal", "-journal"))


@contextmanager
def _hold_reader_lock(src: Path, refused: Exception) -> Iterator[os.stat_result]:
    """Hold on *src* the lock an ordinary SQLite reader holds, and yield its identity.

    An ``immutable=1`` read takes no lock of its own, so this one is held for it from
    before the copy until after :func:`_refuse_if_changed_since`. While it is held a
    rollback-journal writer cannot change the file, and a WAL writer's last close
    cannot remove its ``-wal``, so a write during the copy leaves that ``-wal`` for the
    check to find. A writer already holding a lock that conflicts refuses the copy.
    Where the lock cannot be taken at all -- no POSIX record locks on this platform or
    this filesystem -- the copy has no writer exclusion, so *refused*, the ordinary
    open's own error, stands. The identity yielded is read under the lock, after the
    sidecar check, and is what the copy is checked against.

    The lock belongs to this process and the file's inode, so closing ANY descriptor
    this process holds on *src* releases it. The caller keeps its SQLite connection to
    *src* open until the check has run, and opens no other.
    """
    if not platform_compat.IS_POSIX:
        raise DatabaseCopyFailed(src, refused)
    # SQLite's unix VFS locks a database with POSIX record locks on these bytes of the
    # file. A reader read-locks the pending byte, then the shared range, then drops the
    # pending byte; a writer write-locks the shared range before it changes the file, and
    # a WAL connection's last close write-locks it before it removes its `-wal`.
    pending_byte = 0x40000000
    shared_first, shared_size = pending_byte + 2, 510
    fcntl = platform_compat.fcntl
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise DatabaseCopyFailed(src, e) from e
    try:
        try:
            fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB, 1, pending_byte)
            fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB, shared_size, shared_first)
            fcntl.lockf(fd, fcntl.LOCK_UN, 1, pending_byte)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                busy = OSError(e.errno, "a writer holds the database; run it again")
                raise DatabaseCopyFailed(src, busy) from e
            raise DatabaseCopyFailed(src, refused) from e
        try:
            held, named = os.fstat(fd), os.lstat(src)
        except OSError as e:
            raise DatabaseCopyFailed(src, e) from e
        if not _stat.S_ISREG(held.st_mode) or (held.st_dev, held.st_ino) != (
            named.st_dev,
            named.st_ino,
        ):
            moved = OSError("the database changed while it was being copied; run it again")
            raise DatabaseCopyFailed(src, moved)
        if any(os.path.lexists(f"{src}{sidecar}") for sidecar in ("-wal", "-journal")):
            raise DatabaseCopyFailed(src, refused)
        yield named
    finally:
        os.close(fd)


def _refuse_unsound_required_capture(src: Path, dst: Path) -> None:
    """Raise unless a REQUIRED database was captured soundly.

    ``PRAGMA schema_version`` only proves the file parses. The two unsound cases it misses
    are checked at OPPOSITE ends; either would let ``--keep N`` prune a real backup for one
    restore cannot use.

    **Page corruption: checked on the STAGED COPY.** A database with overwritten interior
    pages and an intact header passes ``schema_version`` and ``backup()``, and fails
    ``integrity_check`` on source and copy alike. The copy is what restore validates, and
    checking it keeps this path read-only with respect to live data.

    **A zero-byte source: checked on the SOURCE, the only place it shows.** SQLite opens a
    zero-byte file as a valid empty database, and ``backup()`` turns it into a 4096-byte
    copy that passes ``integrity_check``. Restore's zero-byte guard reads the archived size,
    so it would install an empty database over live data while reporting success.

    ``integrity_check`` is fast next to the ``backup()`` and gzip passes already paid.

    Raises ``DatabaseCopyFailed``, not restore's ``SourceComponentUnsound``: the try around
    ``_build_snapshot`` does not handle the restore-side type, so it would exit on a
    traceback.
    """
    try:
        if src.stat().st_size == 0:
            raise DatabaseCopyFailed(
                src,
                _facade().sqlite3.DatabaseError(
                    "the live database is EMPTY (zero bytes). SQLite opens such a file as "
                    "a valid empty database and the staged copy passes an integrity check, "
                    "so this would archive nothing and a later restore would install "
                    "nothing over live data while reporting success"
                ),
            )
    except OSError as e:
        raise DatabaseCopyFailed(src, e) from e
    try:
        with closing(_facade().sqlite3.connect(str(dst))) as check:
            result = check.execute("PRAGMA integrity_check;").fetchone()[0]
    except _facade().sqlite3.Error as e:
        # Severe corruption makes the pragma RAISE ("database disk image is malformed"), so
        # this arm is the common path for a page-corrupt database.
        raise DatabaseCopyFailed(src, e) from e
    if result != "ok":
        raise DatabaseCopyFailed(
            src,
            _facade().sqlite3.DatabaseError(
                f"integrity check on the staged copy failed ({result})"
            ),
        )


def _restage_databases(
    src_dir: Path,
    dst_dir: Path,
    *,
    bundle_root: Path,
    on_skip: pinned_fs.SkipReporter | None = None,
) -> None:
    """Re-copy every SQLite database under *src_dir* through the backup API.

    The plain tree copy already placed a byte copy there; this replaces it with a
    consistent one. A second pass, not a filter on the tree walk, so a database newly
    appearing in a tree is covered without registering it.

    A file named like a database that SQLite cannot open keeps its byte copy, UNLESS
    ``is_product_tree_database`` claims its bundle-relative path: then the snapshot fails,
    because restore validates those strictly and would refuse the archive after ``--keep N``
    pruned a real backup for it. *bundle_root* makes that path comparable; a tree-relative
    path would never match. A stray ``Thumbs.db`` that is not ours still rides the bundle.

    A database the tree copy did NOT stage is left alone: the walk declined it (a hardlink
    alias, a symlink, a non-regular entry), and rebuilding it from the source inode would
    put, say, an external hardlinked database's rows in the bundle. The PARENT existing is
    not enough, because it exists for every sibling that copied fine.

    *on_skip* records in ``MANIFEST.json`` a source that could not be verified link-free:
    its byte copy stays (deleting it is data loss), but the bundle then holds a database
    that was not copied consistently.
    """
    for src in sorted(src_dir.rglob("*")):
        if src.suffix not in _DB_SUFFIXES:
            continue
        dst = dst_dir / src.relative_to(src_dir)
        # The DESTINATION first, before the source is stat-ed: an entry the walk declined
        # has no byte copy here, and the walk already reported it, so it is not recorded
        # in MANIFEST.json twice. `lstat`, not `exists()`, so a link planted at the name
        # does not answer for its target. Only ABSENT is skipped: any other failure leaves
        # it unknown whether a byte copy sits there, and skipping would ship a product
        # database without its WAL-only rows, which restore's integrity check accepts.
        try:
            dst_st = dst.lstat()
        except FileNotFoundError:
            continue
        if not _stat.S_ISREG(dst_st.st_mode):
            continue
        # `lstat` in a try, not `is_file()`, which can raise out of the loop on the LIVE
        # tree. Vanished is tolerated; any other error propagates on purpose. It is only
        # reachable when the file became unreadable after the walk copied it, and skipping
        # would ship a product database without its WAL-only rows. Failing closed publishes
        # nothing, so retention cannot prune a bundle that does restore.
        try:
            src_st = src.lstat()
        except FileNotFoundError:
            continue
        if not _stat.S_ISREG(src_st.st_mode):
            continue
        # Spelled `relative_to(bundle_root).as_posix()` to match the restore side's own
        # key for the same set, so the two ends of the invariant read the same.
        rel = dst.relative_to(bundle_root).as_posix()
        outcome = _copy_database_consistently(
            src,
            dst,
            root=src_dir,
            rel_parts=src.relative_to(src_dir).parts,
            require_database=is_product_tree_database(rel),
        )
        if outcome == DB_UNSAFE_SOURCE:
            # The byte copy the walk already made stays -- it is the operator's data and
            # this pass only ever REPLACES a copy, never creates one. Recorded so the
            # archive does not silently claim a consistent database.
            if on_skip is not None:
                on_skip(SKIP_DB_UNPINNED_SOURCE, str(src))
        elif outcome == DB_NOT_A_DATABASE:
            print(
                f"⚠️  {_safe_name(src.name)} is not a readable SQLite database "
                "— copied as a plain file"
            )


def _terminal_safe(value: object) -> str:
    """Render *value* so an untrusted string cannot drive the terminal.

    A restore accepts an arbitrary ``.tar.gz``, so every string that comes back out of one
    -- a manifest field, a member name -- is attacker-controlled input being written to a
    terminal. ANSI and OSC sequences in it are executed by the terminal, not displayed, so
    a crafted archive can rewrite what the operator appears to be reading, or worse.

    Control characters are escaped rather than stripped, so the value stays diagnosable
    (an operator can see the file really is named with an escape) instead of silently
    reading as a different, innocuous name. ``str.isprintable()`` is False for exactly the
    C0/C1 range plus the separators, and True for ordinary text in any language, so a
    non-ASCII path is unharmed.
    """
    return "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in str(value))


def _report_skip(reason: str, path: str) -> None:
    """Word a primitive's skip classification in this module's existing voice.

    The primitive classifies and never prints, so these strings stay byte-identical
    to what snapshot/restore printed before the migration.

    The path is rendered through :func:`_terminal_safe` because on the RESTORE side these
    names come out of the archive: the walk is over an extracted tree whose member names
    the archive chose, and `_data_filter` screens traversal, not escape bytes.
    """
    safe = _terminal_safe(path)
    if reason == pinned_fs.SKIP_SYMLINK:
        print(f"⚠️  Skipping symlink in source tree: {safe}")
    elif reason == pinned_fs.SKIP_VANISHED:
        print(f"⚠️  Skipping vanished entry during snapshot copy: {safe}")
    elif reason == pinned_fs.SKIP_UNREADABLE_ENTRY:
        print(f"⚠️  Skipping entry this process may not read: {safe}")
    elif reason == pinned_fs.SKIP_MASKED_BY_SANDBOX:
        print(
            f"⚠️  Not carrying {safe}: this run is inside Kiro Crew's sandbox, which hides "
            "it. A `kirocrew snapshot` run from your own terminal carries it."
        )
    else:
        print(f"⚠️  Skipping hardlinked or non-regular file during snapshot copy: {safe}")


def _staging_is_pinned(*, allow_unpinned: bool, what: str) -> bool:
    """Whether staging may proceed, and whether it will be descriptor-pinned.

    Returns True for a pinned traversal, False for the by-name traversal the caller
    explicitly asked for. Raises rather than returning False when the platform cannot
    pin and no one said that is acceptable.

    This is the whole "refuse rather than fall back" rule, in one place. The reason it
    is a refusal and not a warning: a by-name walk is not a slightly weaker version of
    a pinned walk, it is the mechanism whose failure closed two pull requests. An
    operator who needs a snapshot on a platform without ``dir_fd`` can still have one,
    but they say so on the command line and the archive records that they did, so the
    weaker mode is never something the tool chose on their behalf.
    """
    if pinned_fs.supports_pinned_tree_walk():
        return True
    if allow_unpinned:
        return False
    raise pinned_fs.PinnedPathRefusal(
        f"refusing to stage the {what}: this platform cannot open a directory "
        "relative to a descriptor, so every component would be re-opened by name and "
        "an ancestor swapped mid-walk could redirect the copy into a credential "
        "store. Re-run with --allow-unpinned-staging to accept a by-name traversal; "
        "the archive will record that it was staged unpinned."
    )


def _staging_ignore(tree: str, root: Path) -> Callable[[str, list[str]], set[str]]:
    """The names the staging walk leaves out of the component tree *tree* rooted at *root*.

    Two rules composed. The by-NAME rule is the one every tree always had: SQLite sidecars
    (the backup API re-copies each database whole, so a ``-wal`` next to that copy would be
    replayed into a database it does not belong to) and the two dev-only patterns. The
    by-POSITION rule is `is_host_local_store_state`, which needs to know WHERE in the data
    home a listing sits -- ``backups`` is host-local at ``memory_stores/<store>/backups``
    and an ordinary folder anywhere else -- so the tree's own path is prepended to the
    directory's path below its root before the predicate is asked.

    Receives ``(directory_by_name, contents)`` exactly as ``shutil.copytree``'s ``ignore``
    does; both staging walks call it that way.
    """
    by_name = shutil.ignore_patterns("hygiene_data", "insert_facts*.py", *_DB_SIDECAR_GLOBS)
    tree_parts = PurePosixPath(tree).parts
    root_str = str(root)

    def _ignore(directory: str, contents: list[str]) -> set[str]:
        skipped = set(by_name(directory, contents))
        rel = os.path.relpath(directory, root_str)
        below = () if rel == os.curdir else Path(rel).parts
        for name in contents:
            if _is_host_local((*tree_parts, *below, name)):
                skipped.add(name)
        return skipped

    return _ignore


def _copytree_safe(
    src: Path,
    dst: Path,
    *,
    allow_unpinned: bool = False,
    on_skip: pinned_fs.SkipReporter | None = None,
    must_create: bool = False,
    skip_unreadable: bool = False,
    **kwargs,
) -> None:
    """Copy a tree for staging, with the source traversal pinned where possible.

    The pinned path (:func:`kiro_crew.pinned_fs.stage_tree_pinned`) pins every
    ancestor, so a directory swapped for a link mid-copy cannot redirect deeper opens;
    a by-name ``islink`` screen only sees the final component.

    ``dirs_exist_ok`` is accepted and dropped: *must_create* decides whether an existing
    destination is tolerated, on both branches. Any other keyword is rejected.

    *must_create*: the caller REPLACES its destination, so an existing root is refused.
    It is passed, never derived: the snapshot's own staging root always exists.

    *on_skip* lets a caller record skips (the snapshot path writes them to the manifest);
    it defaults to printing only.

    *skip_unreadable* turns an unreadable file, unlistable directory or root into a
    recorded skip. Only snapshot creation sets it, so one protected path does not block
    a whole backup; restore and merge leave it off, since there it is archive content.
    """
    report = on_skip or _facade()._report_skip
    outer_ignore = kwargs.pop("ignore", None)
    kwargs.pop("dirs_exist_ok", None)
    if kwargs:
        raise TypeError(f"_copytree_safe got unexpected keyword arguments: {sorted(kwargs)}")

    if _facade()._staging_is_pinned(allow_unpinned=allow_unpinned, what=f"tree {src.name!r}"):
        pinned_fs.stage_tree_pinned(
            src,
            dst,
            what=f"tree {src.name!r}",
            ignore=outer_ignore,
            on_skip=report,
            must_create=must_create,
            skip_unreadable=skip_unreadable,
        )
        return

    # By-name traversal: an ancestor swapped mid-walk can still redirect it, the risk the
    # operator opted into. Per-file screens still apply: plain `copytree` dereferences
    # hardlinks and follows Windows junctions, so each file goes through copy_file_pinned
    # and the screen rejects reparse points, which `islink` misses on Windows.
    def _refuses_listing(path: str) -> bool:
        """Whether *path* is a directory this process may not list.

        Asked by ATTEMPTING the listing rather than with ``os.access``, which answers
        for the real uid and ignores the ACL that is the case worth catching here.
        Anything that is not a permission refusal answers False, so a vanished entry
        or a failing device still reaches the walk and is decided there.
        """
        try:
            with os.scandir(path) as entries:
                next(iter(entries), None)
        except PermissionError:
            return True
        except OSError:
            return False
        return False

    def _ignore_unsafe(directory, contents):
        skipped = set()
        for entry in contents:
            full = os.path.join(directory, entry)
            # Explicit lstat FIRST: `isdir`/`islink` answer False for a path they cannot
            # stat, so a refused directory would reach `copytree`, which collects the
            # error as a string and raises `shutil.Error` only after staging everything.
            try:
                st: os.stat_result | None = os.lstat(full)
            except PermissionError:
                if not skip_unreadable:
                    raise
                skipped.add(entry)
                report(pinned_fs.SKIP_UNREADABLE_ENTRY, full)
                continue
            except OSError:
                # Vanished, or a failure that is not this screen's to rule on. Left to
                # the walk, which is where every other errno is decided.
                st = None
            if os.path.islink(full) or pinned_fs.is_reparse_point(full):
                skipped.add(entry)
                report(pinned_fs.SKIP_SYMLINK, full)
            elif (
                skip_unreadable
                and st is not None
                and _stat.S_ISDIR(st.st_mode)
                and _refuses_listing(full)
            ):
                # A directory that stats fine and refuses to be LISTED. Screened here
                # for the same reason as above, and probed only for a directory: a
                # file's refusal surfaces at its own copy.
                skipped.add(entry)
                report(pinned_fs.SKIP_UNREADABLE_ENTRY, full)
        if outer_ignore:
            skipped |= set(outer_ignore(directory, contents))
        return skipped

    def _copy_screened(source: str, target: str, **_kw) -> None:
        # The flag is handed DOWN, not wrapped: a wrapper cannot tell which end was
        # refused and would record a destination-side denial as an unreadable source.
        pinned_fs.copy_file_pinned(source, target, on_skip=report, skip_unreadable=skip_unreadable)

    # `dirs_exist_ok` follows `must_create`: on a platform that cannot pin (Windows), a
    # root recreated after the rmtree would otherwise be merged into, leaving stale files.
    # The root's own listing is checked here because `_ignore_unsafe` never sees it, and a
    # wrapper could not tell it from a failure to CREATE the destination. Asked first, any
    # refusal `copytree` raises is destination-side and propagates.
    if skip_unreadable and _refuses_listing(str(src)):
        report(pinned_fs.SKIP_UNREADABLE_ENTRY, str(src))
        return
    try:
        shutil.copytree(
            str(src),
            str(dst),
            ignore=_ignore_unsafe,
            copy_function=_copy_screened,
            dirs_exist_ok=not must_create,
        )
    except FileExistsError as exc:
        # Same refusal type and same sentence as the pinned branch, so a caller has one
        # thing to contain and the operator reads the same explanation on either platform.
        raise pinned_fs.PinnedPathRefusal(
            f"refusing to use the tree {src.name!r} destination: {dst.name!r} already "
            "exists, and this operation replaces its destination rather than merging "
            "into it. Something recreated that directory after it was removed, so "
            "staging into it would leave files the archive does not contain while "
            "reporting a replacement. Remove it and re-run with the gateway stopped."
        ) from exc


class ManifestUnreadable(Exception):
    """A bundle's manifest exists but cannot be trusted to say what it carries."""


class _ArchiveTooLarge(Exception):
    """An archive declares more content than a memory bundle can justify.

    *kind* names the bound that refused it: ``"bytes"`` for the uncompressed-content
    bound, which ``--max-archive-gib`` raises, or ``"entries"`` for the member-count
    bound, which no flag moves. A caller reads it to offer only a remedy that can work.
    """

    def __init__(self, *args: object, kind: str = "bytes") -> None:
        super().__init__(*args)
        self.kind = kind


# Generous next to a real memory bundle (megabytes, a few thousand members) and still
# far below what would fill a disk. Both bounds are needed: total size alone misses an
# archive whose damage is a huge member COUNT, and count alone misses one member that
# declares a terabyte.
_MAX_ARCHIVE_MEMBERS = 200_000


_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024

#: Highest value ``--max-archive-gib`` accepts. The override is for an install that is
#: honestly larger than the default bound, not a way to switch the bound off: a ceiling
#: keeps a typo (``40000``) from turning the decompression guard into no guard.
MAX_ARCHIVE_GIB_CEILING = 1024


def archive_byte_bound(max_archive_gib: int | None) -> int:
    """The declared-content bound in bytes: the operator's ``--max-archive-gib`` or the default.

    Read through the facade when no override is given, so a test that lowers
    ``snapshot._MAX_ARCHIVE_BYTES`` lowers the bound every reader and the writer enforce.
    The caller validates the override against :data:`MAX_ARCHIVE_GIB_CEILING` first.
    """
    if max_archive_gib is None:
        return int(_facade()._MAX_ARCHIVE_BYTES)
    return int(max_archive_gib) * (1024**3)


def _bound_gib(max_bytes: int) -> str:
    """The bound as the operator would type it, never rounding a sub-GiB test bound to 0."""
    gib = max_bytes / (1024**3)
    return f"{gib:.0f}" if gib >= 1 else f"{gib:.3g}"


def _refuse_oversized_tree(stage: Path, *, max_bytes: int | None = None) -> None:
    """Refuse a staged bundle the archive bound would refuse, BEFORE it is archived.

    The writer's half of :func:`_refuse_oversized_archive`: it counts the same entries
    and regular-file bytes, so what passes here restores at the same bound, and a refusal
    costs no compression pass. Links count as entries and are never followed. The message
    names the largest top-level entry, the cheapest one to drop with ``--components``.
    """
    facade = _facade()
    max_count = facade._MAX_ARCHIVE_MEMBERS
    if max_bytes is None:
        max_bytes = facade._MAX_ARCHIVE_BYTES
    total = 0
    count = 1  # the bundle's root directory is a member of its own
    by_top: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(stage):
        count += len(dirnames) + len(filenames)
        if count > max_count:
            raise _ArchiveTooLarge(
                f"This snapshot declares more than {max_count:,} entries, which "
                "`kirocrew restore` refuses",
                kind="entries",
            )
        rel_dir = Path(dirpath).relative_to(stage)
        for fn in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, fn))
            except FileNotFoundError:
                continue
            if not _stat.S_ISREG(st.st_mode):
                continue
            total += st.st_size
            top = rel_dir.parts[0] + "/" if rel_dir.parts else fn
            by_top[top] = by_top.get(top, 0) + st.st_size
    if total > max_bytes:
        largest, largest_bytes = max(by_top.items(), key=lambda kv: kv[1])
        raise _ArchiveTooLarge(
            f"This snapshot declares more than {_bound_gib(max_bytes)} GiB of uncompressed "
            f"content ({total / (1024**3):.2f} GiB; largest entry {_safe_name(largest)} at "
            f"{largest_bytes / (1024**3):.2f} GiB), which `kirocrew restore` refuses at that "
            "bound"
        )


def _refuse_oversized_archive(probe: tarfile.TarFile, *, max_bytes: int | None = None) -> int:
    """Refuse an archive that would not fit, before anything is extracted.

    Compressed size says nothing about extracted size, so this reads the member headers
    BEFORE ``extractall``. It runs on every archive read, local bundles included: a local
    file can be hostile or wrong. Members are walked one at a time, not via
    ``getmembers()``, so a million-member index cannot exhaust memory.

    *max_bytes* is the ``--max-archive-gib`` override in bytes (default
    ``_MAX_ARCHIVE_BYTES``); the member-count bound never moves. Returns the declared
    regular-file total so a caller that raised the bound can check free space.
    """
    facade = _facade()
    if max_bytes is None:
        max_bytes = facade._MAX_ARCHIVE_BYTES
    total = 0
    count = 0
    while (member := probe.next()) is not None:
        count += 1
        if count > facade._MAX_ARCHIVE_MEMBERS:
            raise _ArchiveTooLarge(
                f"This archive declares more than {facade._MAX_ARCHIVE_MEMBERS:,} "
                "entries, which `kirocrew restore` refuses",
                kind="entries",
            )
        # Only regular files carry payload; a directory or link header declares a size
        # that extraction never writes, so counting those would refuse honest archives.
        if member.isfile():
            total += max(member.size, 0)
            if total > max_bytes:
                raise _ArchiveTooLarge(
                    "This archive declares more than "
                    f"{_bound_gib(max_bytes)} GiB of uncompressed content, "
                    "over the bound `kirocrew restore` holds it to. If you made it with "
                    "`kirocrew snapshot --max-archive-gib N`, pass "
                    "--max-archive-gib N (or larger) here"
                )
    return total


def _manifest_components(snap: Path) -> list[str] | None:
    """Return the component names a bundle's manifest says it carries.

    ``None`` means "this bundle predates the component map", which is the signal to
    keep the historical all-components behaviour — such a bundle really did hold every
    component. That fallback is reserved for a manifest that is READABLE and simply
    has no map; a manifest that cannot be parsed raises :class:`ManifestUnreadable`
    instead, because "we could not read it" must never resolve to the most destructive
    interpretation available.

    Names not in :data:`COMPONENTS` are dropped: the manifest travels with the bundle,
    so a restore must not act on a name this build cannot resolve. The remaining list
    is returned even when EMPTY — that means "declares components, none understood
    here", which must restore nothing.
    """
    mf = snap / "MANIFEST.json"
    if not mf.is_file():
        return None
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise ManifestUnreadable(f"MANIFEST.json is present but unreadable: {e}") from e
    if not isinstance(manifest, dict):
        raise ManifestUnreadable("MANIFEST.json is not an object")
    comps = manifest.get("components")
    if comps is None:
        return None
    if not isinstance(comps, dict):
        raise ManifestUnreadable(f"MANIFEST.json 'components' is {type(comps).__name__}, not a map")
    known = [c for c in comps if c in COMPONENTS]
    dropped = sorted(set(comps) - set(known))
    if dropped:
        print(
            "⚠️  Manifest names unknown component(s), ignoring: "
            + ", ".join(_safe_name(d) for d in dropped)
        )
    return known


#: The largest ``MANIFEST.json`` the readers below parse. A real one is a few kilobytes
#: even with a long ``skipped`` list; the bound keeps a hostile member out of memory.
_MANIFEST_READ_LIMIT = 16 * 1024 * 1024


def _skips_in(raw: bytes) -> frozenset[PurePath] | None:
    """The data-home-relative paths the manifest *raw* lists under ``skipped``.

    ``None`` when *raw* is not a manifest object with a ``skipped`` list.
    Both slash spellings are accepted so a Windows omission preserves live data on POSIX.
    """
    try:
        manifest = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    skipped = manifest.get("skipped") if isinstance(manifest, dict) else None
    if not isinstance(skipped, list):
        return None
    return frozenset(
        PurePath(PureWindowsPath(entry["path"]).as_posix())
        for entry in skipped
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"]
    )


def _recorded_skips(snap: Path) -> frozenset[PurePath] | None:
    """What the EXTRACTED bundle *snap* lists as skipped; ``None`` with no skip record.

    A replace restore asks this before it removes a live document the bundle lacks.
    ``None`` (no manifest, or one with no ``skipped`` list) keeps the ordinary reading of
    that absence: the source had none. The restore has already parsed this manifest whole
    (``_manifest_components``), so it is read here with no size bound and no swallowed
    error: either would turn a record this reader could not take in into that removal.
    An ``OSError`` propagates, and the replace rolls back instead.
    """
    mf = snap / "MANIFEST.json"
    if not mf.is_file():
        return None
    return _skips_in(mf.read_bytes())


def _records_omission_of(skips: frozenset[PurePath] | None, rel: str) -> bool:
    """Whether *skips* lists the data-home-relative *rel*, or a directory above it."""
    if not skips:
        return False
    path = PurePath(rel)
    return any(path == skip or skip in path.parents for skip in skips)


def _bundle_lacks_all(bundle: Path, paths: list[str]) -> bool:
    """Whether the published *bundle* is known to hold nothing at or under any of *paths*.

    The ``--keep`` prune asks this of an OLDER bundle after a run that omitted *paths*
    (data-home-relative). Its manifest must be readable with a ``skipped`` list, and no
    regular-file member may sit at or under any requested path. A skip record alone
    does not prove absence: ``db_unpinned_source`` can accompany a staged byte copy.

    False otherwise, an unreadable archive or manifest included: a surplus bundle costs
    disk, and a prune on a guess can cost the last copy of the data.
    """
    wanted = [PurePath(p) for p in paths]
    manifest_read = False
    try:
        with tarfile.open(bundle) as tf:
            for member in tf:
                parts = PurePosixPath(member.name).parts
                if len(parts) < 2:
                    continue
                if len(parts) == 2 and parts[1] == "MANIFEST.json" and not manifest_read:
                    if not member.isfile() or member.size > _MANIFEST_READ_LIMIT:
                        return False
                    handle = tf.extractfile(member)
                    skips = _skips_in(handle.read()) if handle is not None else None
                    if skips is None:
                        return False
                    manifest_read = True
                    continue
                rel = PurePath(*parts[1:])
                if member.isfile() and any(rel == p or p in rel.parents for p in wanted):
                    return False
    except (OSError, EOFError, tarfile.TarError, zlib.error):
        return False
    return manifest_read


def _bundle_carries_named_stores(snap: Path) -> bool:
    """Was *snap* written by a build that stages the ``memory_stores/`` tree?

    Replace clears every memory tree and refills it from the archive. Bundles older than
    `_FIRST_VERSION_WITH_NAMED_STORES` never carry this tree, so clearing it on their word
    would delete every crew's private memory; such a bundle leaves the live tree alone.

    Reads the manifest leniently (`_manifest_components` already refused an unreadable
    one): a missing or non-integer ``version`` means "older than this tree".

    The dashboard export (`portability`) writes its own version line, marked
    ``"format": "zip"``, and uses the same replace pass, so both thresholds live here.
    """
    mf = snap / "MANIFEST.json"
    if not mf.is_file():
        return False
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    if not isinstance(manifest, dict):
        return False
    version = manifest.get("version")
    if not isinstance(version, int):
        return False
    if manifest.get("format") == "zip":
        return version >= _FIRST_EXPORT_VERSION_WITH_NAMED_STORES
    return version >= _FIRST_VERSION_WITH_NAMED_STORES


def _print_manifest(snap: Path) -> None:
    mf = snap / "MANIFEST.json"
    if not mf.is_file():
        return
    try:
        m = json.loads(mf.read_text(encoding="utf-8"))
        print("📋 Snapshot info:")
        # Every string below comes out of an archive the caller supplied, so all of them
        # go through _terminal_safe: a function that sanitizes two of five
        # attacker-controlled fields would be worse than either extreme.
        print(f"  Created: {_terminal_safe(m.get('created_at', 'unknown'))}")
        print(
            f"  From: {_terminal_safe(m.get('user', 'unknown'))}"
            f"@{_terminal_safe(m.get('hostname', 'unknown'))}"
        )
        c = m.get("contents", {})
        # Absent in bundles written before the purpose seam existed. Say so rather than
        # printing a default, so an old bundle is never read as a declared one.
        print(f"  Purpose: {_safe_name(m.get('purpose'), 'undeclared (pre-seam bundle)')}")
        comps = m.get("components")
        if isinstance(comps, dict) and comps:
            rendered = ", ".join(
                f"{_safe_name(k, '?')} [{_safe_name(v, '?')}]" for k, v in sorted(comps.items())
            )
            print(f"  Components: {rendered}")
        print(f"  Memory DB: {c.get('memory_db', 0) // 1024} KB")
        print(f"  Crons: {c.get('crons_json', 0) // 1024} KB")
        print(f"  Workspace files: {c.get('workspace_files', 0)}")
        print(f"  Skills: {c.get('skill_count', 0)}")
        print(f"  Notifications: {c.get('notifications_jsonl', 0) // 1024} KB")
        print(f"  Plan memory files: {c.get('plan_memory_files', 0)}")
        if "memory_store_count" in c:
            print(f"  Named memory stores: {c.get('memory_store_count', 0)}")
        # Both of these are the record that makes an incomplete or weaker archive
        # visible. A value written but never displayed is only findable by untarring
        # the archive by hand, which is not a reader -- so they are shown here, where
        # anyone inspecting a snapshot before restoring it already looks.
        if m.get("staging") == "unpinned":
            print("  ⚠️  Staged by path name (unpinned): see --allow-unpinned-staging")
        for entry in m.get("skipped") or ():
            reason = _terminal_safe(entry.get("reason", "?"))
            omitted = _terminal_safe(entry.get("path", "?"))
            print(f"  ⚠️  Omitted ({reason}): {omitted}")
    except Exception as e:
        print(f"  (Could not read manifest: {e})")

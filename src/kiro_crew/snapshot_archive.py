"""The bundle format: how a tree is staged into an archive, and how an archive is screened.

Writing side: the pinned tree copy every staging, install and rollback path uses, the
refusal where the platform cannot pin, and the consistent capture of a live SQLite
database. Reading side: the member filter extraction runs through, the size bound checked
before anything is extracted, and the manifest readers. A name that comes out of an
archive is escaped here before it reaches a terminal.

What a bundle RECORDS is composed by the snapshot command itself
(:func:`kiro_crew.snapshot._build_snapshot`), which writes ``MANIFEST.json``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat as _stat
import tarfile
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath
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

    Extraction prints a warning and drops a rejected entry, then extraction continues -- so a
    bundle can arrive at the restore missing part of its payload while the manifest still
    declares it. In replace mode that is destructive rather than merely incomplete: the memory
    trees are cleared unconditionally (a tree the archive lacks must not be kept) and nothing
    replaces the one that was dropped.

    This is the ONLY layer where the two cases are distinguishable. Measured: at the mutation
    phase a rejected link and an archive that never carried the tree are byte for byte the same
    state -- the staged tree is simply absent -- so the prescribed check there ("clear only when
    the source is a directory") cannot tell them apart, and it would revert the unconditional
    clear that a documented defect required. Extraction, by contrast, knows.

    A `_never_ships` drop is NOT recorded: those are deliberate and expected. Nor is a
    rejection anywhere OUTSIDE a tree that replace clears -- and that limit is the point.
    `test_symlink_filtered_out` states the contract for those: a hostile entry injected into an
    otherwise sound bundle is dropped and the restore SUCCEEDS (`assert ret == 0`). Refusing on
    any rejection at all was the prescribed shape and breaks three of those tests. What makes the
    cleared trees different is that dropping an entry there converts into DELETION of the
    operator's own tree, rather than merely into an absence.
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

    Also rejects path traversal, symlinks, and hardlinks to eliminate TOCTOU
    race between pre-scan and extraction.
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

    This exists because verifying a path and then RE-WALKING it by name are two different
    resolutions of the same string: the second can land somewhere the first never inspected.
    ``resolve()`` and a late ``realpath()`` are both that second walk.

    Requires descriptor pinning, and SAYS SO rather than pretending: where the platform
    cannot open relative to a directory descriptor (``os.open`` absent from
    ``os.supports_dir_fd``, or no ``O_NOFOLLOW`` -- which is Windows), this returns True and
    the caller proceeds on the by-name screening the loop already did, the file being a
    regular file and not a link or reparse point. That is weaker, and it is the same
    degradation snapshot and restore apply everywhere else through ``_staging_is_pinned``.
    Returning False instead would refuse every database on that platform, turning a
    hardening into an outage.

    The first version omitted that gate and passed ``dir_fd`` unconditionally, which is not
    merely weaker on Windows -- ``os.open`` RAISES ``NotImplementedError`` there, so the pass
    crashed. ``supports_pinned_walk`` exists for exactly this.
    """
    if not pinned_fs.supports_pinned_walk():
        return True
    try:
        fd = pinned_fs.open_dir_pinned(root, what="database source root")
    except OSError:
        # Gone, or otherwise unopenable: the same answer the intermediate components below
        # already give, and for the same reason -- a root this pass cannot open is a file it
        # cannot verify as reachable without following a link.
        #
        # `open_dir_pinned` translates only `ELOOP`/`ENOTDIR` into `PinnedPathRefusal` and
        # re-raises every other `OSError`, so a root lost to a concurrent rename or removal
        # arrives as `FileNotFoundError`. Letting it escape from a call site under
        # `_build_snapshot` exits on a traceback: that enclosing try handles
        # `PinnedPathRefusal`, `UnsafeComponentRoot` and `DatabaseCopyFailed`, and its
        # `OSError` arm belongs to a later, separate try. The declared path owes a named
        # refusal and the tree path owes a recorded skip, so returning False routes both
        # through the `require_database` asymmetry the caller already implements.
        #
        # `PinnedPathRefusal` is deliberately NOT caught here: `snapshot_main` handles it and
        # audits it as `unpinnable_staging`. That is a decision the operator should see, not
        # a source that went missing.
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


def _copy_database_consistently(
    src: Path,
    dst: Path,
    *,
    root: Path,
    rel_parts: tuple[str, ...],
    require_database: bool = False,
) -> str:
    """Copy the live SQLite database *src* to *dst* through the backup API, read-only.

    THE one place this module reads a live database on the creation path. Both staging
    paths -- the fixed core-file list and the tree re-stage pass -- call it, because a
    second, structurally identical copy of the hardening below is how one of the two
    drifts back.

    Four properties, each closing a failure that reported success:

    **The chain is verified through descriptors, not by name.** Every component from
    *root* down is opened relative to the previous one with ``O_NOFOLLOW``, so a directory
    that is a link -- or one swapped for a link while this runs -- fails its own open
    instead of redirecting the read. ``src.resolve()`` and a late ``realpath()`` are both
    a SECOND by-name walk and were both wrong here: they can land on a database this
    function never inspected, whose rows then ride in the bundle under an innocuous name.

    **The URI is percent-escaped.** ``as_uri()``, not interpolation: a POSIX filename
    containing ``?`` or ``#`` was otherwise parsed as the start of the URI's query or
    fragment, truncating the path so the copy opened a DIFFERENT database and stored it
    under the requested name. Built once and shared by the probe and the copy, because two
    spellings of one URI is how they diverge.

    **The connection is ``mode=ro``.** Staging only ever needs to read, and a read-write
    open is not merely more authority than required -- it MUTATES the live database.
    Measured on a fixture with a ``-wal`` left unreplayed by a killed writer: the
    read-write open recovered the log into the main file (8192 -> 16384 bytes, different
    hash) and unlinked the 836 KB ``-wal``; ``mode=ro`` left both byte-identical and
    captured exactly the same rows. So a backup command was rewriting the data it was
    asked to read, and in the window before SQLite's own open a swapped-in database was
    handed a writable handle. ``mode=ro`` refuses the write outright.

    **"Not a database" is told apart from "cannot read this database".** They are
    distinguished by probing readability separately from copying, not by matching the
    error, because ``sqlite_errorname`` is 3.11+ while this package supports 3.10 and
    message text changes with any SQLite release. The broad form was wrong in the
    dangerous direction: "database is locked" is also a ``DatabaseError``, so an exclusive
    writer made the probe report "not a database" and a raw byte copy shipped as if it
    were consistent.

    What this does NOT close, stated rather than implied: SQLite's API takes a PATH and
    cannot be pointed at a held descriptor -- probed, it refuses ``/proc/self/fd/N`` and
    ``/dev/fd/N`` alike -- so SQLite re-resolves the final name itself, and a same-uid
    swap in the window between the check above and that open is not detectable here. A
    post-hoc identity re-check does not close it either, since swapping back defeats the
    check. Closing it needs a descriptor-taking VFS, which is a different change.

    The WAL question the issue this came from also raises is NOT handled by checkpointing
    and copying bytes, and deliberately so: the backup API already reads a consistent
    snapshot that INCLUDES rows living only in the ``-wal``. Measured against a
    cross-process writer with ``wal_autocheckpoint=0`` and a 1.5 MB log, the copy
    contained all 421 rows -- 371 of them WAL-resident -- and passed ``integrity_check``.
    A check-then-copy-bytes design has the race; this one has no check to race.
    """
    if not _facade()._chain_is_link_free(root, rel_parts):
        if require_database:
            # Same reasoning as the not-a-database case below, and it has to apply to
            # BOTH: applying it to only one lets a snapshot that omitted a REQUIRED
            # database succeed, so `--keep N` prunes the last complete archive in favour
            # of one missing the database it claims. A recorded omission is "recoverable
            # information" only while the operator still HAS the archive it could be
            # recovered from.
            raise DatabaseCopyFailed(
                src,
                pinned_fs.PinnedPathRefusal(
                    f"{'/'.join(rel_parts)} could not be verified as reachable without "
                    "following a link, so it was not read"
                ),
            )
        return DB_UNSAFE_SOURCE
    ro_uri = f"{src.absolute().as_uri()}?mode=ro"
    try:
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
            # A DECLARED component file, so this is a hard failure. The asymmetry with the
            # tree pass below is deliberate, and an earlier revision of this change got it
            # backwards by "unifying" the two -- review caught the consequence:
            #
            # A corrupt `memory.db` staged as bytes makes the snapshot SUCCEED. `--keep N`
            # then counts that archive as the newest backup and prunes a real one, while
            # restore refuses the new archive outright at its strict database validation.
            # The operator is left with no restorable backup, having run a command that
            # printed success. The module already names this hazard class where `--keep`
            # is handled: an empty bundle "would count as the newest backup and prune a
            # real one".
            #
            # A `.db` found by the TREE walk is incidental -- some file the operator
            # happens to keep in their workspace -- and refusing the whole snapshot over
            # it would be an outage, not a safeguard. Declared and load-bearing versus
            # discovered and incidental is a real difference, so the two paths get
            # different answers on purpose.
            raise DatabaseCopyFailed(src, e) from e
        return DB_NOT_A_DATABASE
    # The connects are INSIDE the try, not just `backup()`. Between the probe above and
    # this open the file can disappear -- and `mode=ro` makes that a hard failure where a
    # read-write open would silently CREATE an empty database, so the window matters more,
    # not less. `sqlite3.connect` then raises `OperationalError`, which `snapshot_main`
    # does not catch (it handles PinnedPathRefusal, UnsafeComponentRoot, DatabaseCopyFailed
    # and _ArchiveTooLarge), so letting it escape exits on a traceback instead of naming
    # the database.
    try:
        with (
            closing(_facade().sqlite3.connect(ro_uri, uri=True)) as src_conn,
            closing(_facade().sqlite3.connect(str(dst))) as dst_conn,
        ):
            # The file is a readable database, so a failure here means the consistent copy
            # did not happen. Absorbing it would leave the caller's byte copy -- taken
            # WITHOUT the `-wal` this module excludes -- passing for a whole database, so
            # it is raised, but typed and naming the file so the command boundary reports
            # which database failed instead of exiting on a traceback.
            src_conn.backup(dst_conn)
    except _facade().sqlite3.Error as e:
        raise DatabaseCopyFailed(src, e) from e
    if require_database:
        _refuse_unsound_required_capture(src, dst)
    return DB_COPIED


def _refuse_unsound_required_capture(src: Path, dst: Path) -> None:
    """Raise unless a REQUIRED database was captured soundly.

    ``PRAGMA schema_version`` above only proves the file parses, which is a much weaker
    claim than restore's, and the two unsoundnesses it misses need checking at OPPOSITE
    ends. Both were measured rather than reasoned about.

    **Page corruption -- checked on the STAGED COPY.** With a database's interior pages
    overwritten and the header left intact, ``schema_version`` answered normally,
    ``backup()`` succeeded and faithfully staged all 192512 bytes, and ``integrity_check``
    reported "database disk image is malformed" on the source AND the copy. So the archive
    reported success and restore was guaranteed to refuse it; ``--keep N`` then counts it as
    the newest backup and prunes a real one, so the operator loses their last restorable
    copy at the one moment they reach for it. The copy is the right end to check: it is what
    goes in the archive and what restore validates, checking it keeps this path read-only
    with respect to live data, and it also catches a copy damaged in transit. Source
    corruption still surfaces, because ``backup()`` reproduces it.

    **A zero-byte source -- checked on the SOURCE, and ONLY visible there.** SQLite opens a
    zero-byte file as a valid EMPTY database, so ``integrity_check`` answers ``ok``. Worse,
    ``backup()`` does not preserve the emptiness: measured, a 0-byte source produced a
    4096-byte staged copy that ``integrity_check`` called ``ok``. Restore's own zero-byte
    guard reads the size of the ARCHIVED file, so it sees 4096 and accepts -- meaning that
    for this path restore does NOT refuse the archive, it RESTORES it and installs an empty
    database over live data while reporting success. That is why the check cannot be
    deferred to the copy or to restore: the "captured nothing" condition exists only at the
    source, which is the same reason restore reads size before opening.

    The cost is not a reason to hesitate: ``integrity_check`` measured 1285 MB/s against
    415 MB/s for the ``backup()`` and 227 MB/s for the gzip this command already performs
    over the same bytes unconditionally, so it adds about a tenth of work already paid.

    Raises ``DatabaseCopyFailed`` rather than restore's ``SourceComponentUnsound``: the try
    around ``_build_snapshot`` handles ``PinnedPathRefusal``, ``UnsafeComponentRoot`` and
    ``DatabaseCopyFailed``, so the restore-side type would leave here as a traceback --
    the same escape the chain check avoids.
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
        # Severe corruption makes the pragma RAISE rather than answer -- the measurement
        # above got `DatabaseError: database disk image is malformed` here -- so this arm is
        # the common path for a page-corrupt database, not a defensive afterthought.
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
    consistent one. Done as a second pass rather than by filtering the tree walk, so
    the copy logic stays in one place and a database newly appearing in a tree is
    covered without anyone remembering to register it.

    A file whose suffix says database but which SQLite cannot open is left as the byte
    copy already made -- UNLESS ``is_product_tree_database`` claims its bundle-relative path,
    in which case the snapshot fails. That predicate's own contract is that everything it
    claims is "validated as strictly as ``memory.db``", and the restore side already enforces
    exactly that (``_refuse_unless_sound(..., strict=is_product_tree_database(rel))``). Staging a
    corrupt ``workspace/knowledge/knowledge.db`` as bytes and reporting success therefore
    produces an archive that restore is guaranteed to refuse, which is worse than failing:
    ``--keep N`` counts the new archive as the newest backup and prunes a real one, so the
    operator loses their last restorable copy to a snapshot that "succeeded".

    *bundle_root* is what makes that key comparable. A product path is spelled
    relative to a bundle root, while this pass walks one tree, so a tree-relative path would
    never match any entry and the strictness would be silently vacuous.

    A non-database that happens to be named ``.db`` and is NOT one of ours is still the
    operator's file and must ride the bundle: refusing a whole snapshot over a stray
    ``Thumbs.db`` would be an outage rather than a safeguard.

    A database the tree copy did NOT stage is left alone. This pass exists to REPLACE a
    byte copy with a consistent one, so a destination that does not already hold that byte
    copy means the walk deliberately declined the source -- a hardlink alias, a symlink, a
    non-regular entry -- and recreating it here from the source inode would reinstate
    exactly what the walk refused. Reproduced before this guard existed: a `.db` hardlinked
    to a database outside the component tree was skipped as `not_regular` and then rebuilt
    by this pass, putting the external database's rows in the bundle. Checking the PARENT
    directory is not enough, because the parent exists for every sibling that copied fine.

    *on_skip* is how a degradation reaches ``MANIFEST.json``. A source that cannot be
    verified link-free leaves the tree walk's byte copy in place, which is the right call
    -- deleting it is data loss -- but the bundle then holds a database that was never
    copied consistently, and that belongs on the record rather than only in the console
    scrollback of whoever ran the command.
    """
    for src in sorted(src_dir.rglob("*")):
        if src.suffix not in _DB_SUFFIXES:
            continue
        dst = dst_dir / src.relative_to(src_dir)
        # The DESTINATION is asked about first, before the source is stat-ed at all.
        # Ordering, not style: an entry the tree walk declined has no byte copy here,
        # so asking the destination first ends this iteration without touching a source
        # the walk has ALREADY reported -- which is what keeps one refused file out of
        # MANIFEST.json twice.
        #
        # `lstat`, not `exists()`: the latter follows a link, so a link planted at the
        # destination name would answer for its target and be treated as a staged copy.
        #
        # ABSENT only. A destination the tree walk did not stage is nothing to replace,
        # so it is skipped -- but any other failure to read it means this pass cannot
        # tell whether a byte copy is sitting there, and skipping then leaves a product
        # database in the bundle without its checkpointed rows. Restore's strict
        # integrity check accepts such a copy, so the rows living only in the
        # write-ahead log are silently gone and retention may prune the bundle that
        # still had them. Same reasoning, and the same narrowness, as the source stat
        # below.
        try:
            dst_st = dst.lstat()
        except FileNotFoundError:
            continue
        if not _stat.S_ISREG(dst_st.st_mode):
            continue
        # `lstat` in a try, not `is_file()`: this walks the LIVE tree, and `is_file()`
        # raised a refusal out of the loop and ended the whole snapshot. Vanished is
        # tolerated; a REFUSAL is not, deliberately.
        #
        # A refusal can only be reached here by the narrow race where the tree walk read
        # this file and it became unreadable afterwards -- a statically unreadable entry
        # is skipped by that walk, so no byte copy exists and the destination check above
        # already ended the iteration. In that race, skipping would leave the walk's byte
        # copy of a PRODUCT database in the bundle without its checkpointed rows, and
        # restore's strict integrity check passes such a copy: rows present only in the
        # write-ahead log are then silently gone. A recorded note does not prevent that.
        # Failing closed is both the safe direction and what this call did before the
        # tolerance elsewhere in this change existed. Nothing is published on the way
        # out, so no retention pass can prune a bundle that does restore.
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
    Raised in review against the omission list this change added.

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

    Was: ``shutil.copytree`` with an ignore callback that tested ``os.path.islink`` on
    a NAME. That screened the final component of each entry and nothing else, so an
    ancestor directory swapped for a link between the listing and the copy redirected
    every deeper open, and the screen had nothing to report -- what it found inside
    the replaced tree was an ordinary file. Now the traversal is descriptor-pinned by
    :func:`kiro_crew.pinned_fs.stage_tree_pinned`, including the chain above the root.

    ``dirs_exist_ok`` is accepted and dropped -- it is a ``shutil.copytree`` keyword and
    callers may pass it out of habit -- but it is not meaningless generally: whether an
    existing destination is tolerated is decided by *must_create*, and it is decided
    identically on both branches. Every other keyword is rejected rather than silently
    dropped.

    *must_create* says the caller REPLACES its destination rather than merging into it, so
    a root that exists is refused. Only a caller that removed the tree it is about to write
    knows this, so it is passed, never derived -- deriving it from ``skip_existing`` refused
    every snapshot, because the snapshot's own staging root already exists.

    *on_skip* lets a caller both print and RECORD what was skipped. It defaults to
    printing only, which is right for restore; the snapshot path passes a recorder so
    an incomplete archive says so in its own manifest instead of only in the console
    output of whoever ran it.

    *skip_unreadable* says an entry this process may not read is one of those
    recorded skips rather than the end of the operation -- a file, a directory that
    refuses to be listed, and the tree's own root alike, on both traversals. Only SNAPSHOT creation sets
    it: a data home can hold a platform-protected path, and refusing to produce any
    backup because of one file the bundle was never going to need is worse than a
    bundle whose manifest names the gap. Restore and merge leave it off, because
    there the unreadable name is the archive's own content.
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

    # Declared by-name traversal. The TRAVERSAL is the weakness the operator opted into --
    # an ancestor swapped mid-walk can still redirect it, and nothing here can prevent
    # that without the descriptor support the platform lacks. The PER-FILE screens are a
    # different matter and review was right that they had been left behind: plain
    # `copytree` dereferences a hardlink into ordinary bytes and follows a Windows
    # junction, so a credential aliased into a staged tree would have ridden along even
    # though the pinned path refuses exactly that. Each file now goes through
    # copy_file_pinned (same fstat screens, minus the pinned ancestors) and the screen
    # rejects reparse points, which `islink` alone does not report on Windows.
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
            # Classified by an explicit stat FIRST, before anything asks what kind of
            # entry this is. `os.path.isdir` and `os.path.islink` both answer False for
            # a path they cannot stat, so asking either one first reads a refusal as
            # 'an ordinary file that is not a link' -- and for a DIRECTORY `copytree`
            # then descends on the directory entry's own type, meets the refusal at its
            # scandir, collects it, and raises `shutil.Error` only after staging
            # everything else. So the whole snapshot is built and then thrown away, and
            # the collected errors are strings, so nothing downstream can tell a
            # permission refusal from a failing disk.
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
        # The flag is handed DOWN rather than the call being wrapped. A wrapper here
        # cannot tell which end was refused, so a destination-side denial would be
        # recorded as an unreadable source -- an omission blamed on the operator's file
        # rather than on the failure to write it, in a bundle reporting success.
        pinned_fs.copy_file_pinned(source, target, on_skip=report, skip_unreadable=skip_unreadable)

    # `dirs_exist_ok` has to follow `must_create`, not be hardcoded. Review found the gap:
    # `must_create` reached the pinned walk and stopped there, so on a platform that cannot
    # pin -- Windows, the dashboard's replace -- a root recreated after the rmtree was still
    # merged into and stale files survived a successful replace. That is the same mistake as
    # the earlier Windows import refusal in this PR: a guard added to the pinned path and not
    # carried to the by-name one. Every mutating path gets the gate or the gate is decorative.
    # The tree's OWN directory, which `_ignore_unsafe` never sees: `copytree` lists the
    # root before consulting it. Asked HERE rather than by wrapping the call, because a
    # wrapper cannot tell the root's own refusal from a failure to CREATE the
    # destination -- and reporting the latter as an unreadable source published a bundle
    # missing the whole selected tree while reporting success. With the question asked
    # first, every refusal `copytree` raises is a destination-side one and propagates.
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
    """An archive declares more content than a memory bundle can justify."""


# Generous next to a real memory bundle (megabytes, a few thousand members) and still
# far below what would fill a disk. Both bounds are needed: total size alone misses an
# archive whose damage is a huge member COUNT, and count alone misses one member that
# declares a terabyte.
_MAX_ARCHIVE_MEMBERS = 200_000


_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024


def _refuse_oversized_archive(probe: tarfile.TarFile) -> None:
    """Refuse an archive that would not fit, before anything is extracted.

    A compressed archive can declare orders of magnitude more content than it occupies,
    so size on disk says nothing about what extraction would write. The check has to run
    against the member headers, and it has to run BEFORE ``extractall``: once extraction
    starts, the damage is already on the filesystem.

    Applied on every path that reads an archive — staging a bundle for upload, a bundle
    fetched from object storage, and a local bundle handed to `restore`. A local file is
    not trustworthy by virtue of being local; it can be hostile or simply wrong.

    Members are walked one at a time rather than through ``getmembers()``, because
    materialising the whole index is itself the denial of service an archive with
    millions of members performs. Bailing on the member that crosses the bound means the
    work is bounded by the bound, not by what the archive claims.
    """
    facade = _facade()
    total = 0
    count = 0
    while (member := probe.next()) is not None:
        count += 1
        if count > facade._MAX_ARCHIVE_MEMBERS:
            raise _ArchiveTooLarge(
                f"This archive declares more than {facade._MAX_ARCHIVE_MEMBERS:,} "
                "entries, which no memory bundle produces"
            )
        # Only regular files carry payload; a directory or link header declares a size
        # that extraction never writes, so counting those would refuse honest archives.
        if member.isfile():
            total += max(member.size, 0)
            if total > facade._MAX_ARCHIVE_BYTES:
                raise _ArchiveTooLarge(
                    "This archive declares more than "
                    f"{facade._MAX_ARCHIVE_BYTES // (1024 ** 3)} GiB of uncompressed content, "
                    "which no memory bundle produces"
                )


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


def _bundle_carries_named_stores(snap: Path) -> bool:
    """Was *snap* written by a build that stages the ``memory_stores/`` tree?

    Replace clears every memory tree unconditionally and refills it from the archive,
    which is right when the archive is a faithful copy of its source. For this ONE tree
    the archive can be silent for a second reason: bundles written before the tree was
    a component do not carry it however many stores the source had, so clearing it on
    their word would delete every crew's private memory to restore a backup that never
    claimed to hold it. The manifest version is what tells the two silences apart --
    ``version`` reached `_FIRST_VERSION_WITH_NAMED_STORES` when the tree became part of
    the `memory` component -- so a pre-v4 bundle leaves the live tree alone and says so.

    Reads the manifest leniently: an unreadable manifest is refused long before this is
    asked (`_manifest_components` raises), so what is left here is a readable one whose
    ``version`` may be absent (a pre-v3 bundle) or a non-integer (a hand-edited one), and
    both mean "older than this tree".

    The dashboard export (`portability`) writes a manifest on its own version line,
    marked ``"format": "zip"``, and hands its extracted tree to the same replace pass;
    `EXPORT_MANIFEST_VERSION` is that line's current value and the zip threshold below
    is where it gained the tree. Both thresholds live here so the one decision -- did
    the writer know about named stores -- has one home.
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
        # go through _terminal_safe. Review named the omission list this change added;
        # created_at, user and hostname are the same class and pre-date this diff. They
        # are fixed here rather than left as a matching hole three lines away, because
        # the renderer makes each one a one-word change and shipping a function that
        # sanitizes two of five attacker-controlled fields would be worse than either
        # extreme. Named as a drive-by rather than smuggled in.
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

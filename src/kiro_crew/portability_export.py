"""What an export reads from the live home.

The walk that reads the live home by descriptor and refuses a link at any level, the
SQLite backups taken from a checkpointed database, and the session transcripts and their
companions within the manifest's budget: the session planner and writer
(:func:`_plan_sessions`, :func:`_write_sessions`) that ``kiro_crew.portability_stream``
drives when it builds the archive.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING

from kiro_crew import pinned_fs, platform_compat
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.chat_attachments import ATTACHMENTS_DIR_SUFFIX
from kiro_crew.history import (
    ARCHIVE_DIR_NAME,
    SESSIONS_DIR_NAME,
    THREADS_DIR_NAME,
    THREADS_SIDECAR_SUFFIX,
    ConversationLog,
)
from kiro_crew.memory_stores import is_host_local_store_state
from kiro_crew.portability_archive import (
    _MAX_IMPORT_MEMBERS,
    _MAX_IMPORT_UNCOMPRESSED,
    _MAX_SETTINGS_DOCUMENT_BYTES,
    _TRANSCRIPT_SUFFIX,
    _chat_stem,
    _entry_is_link,
    _is_excluded,
    _is_rotation_header,
    _keep_top_level_transcript,
    _read_first_line_fd,
    _rotation_segment_for_stem,
    _transcript_header_verdict,
    logger,
)
from kiro_crew.security import is_sensitive_path
from kiro_crew.snapshot import _DB_SIDECAR_GLOBS

if TYPE_CHECKING:
    from typing import IO


def _wal_checkpoint(db_path: Path) -> None:
    if db_path.is_file():
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.close()
        except Exception:
            logger.debug("WAL checkpoint failed for %s", db_path)


def _sqlite_logical_size(src: Path) -> int:
    """The database's logical byte size (``page_count * page_size``), WAL included.

    This is the size :func:`_backup_sqlite` writes into the archive -- the backup
    copies every committed page, so a store with committed WAL growth is larger
    than its main file's ``st_size``. The streaming export's pre-pass budgets each
    database by this, not ``st_size``, so an active store is not undercounted and
    the planned selection cannot plan an archive that import would then refuse. A
    read-only connection; a size that cannot be read falls back to ``st_size``.
    """
    try:
        conn = sqlite3.connect(src.absolute().as_uri() + "?mode=ro", uri=True)
        try:
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            return int(page_count) * int(page_size)
        finally:
            conn.close()
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return src.stat().st_size


#: Read size when a database backup is copied out of its temp file. The copy is
#: the only buffer the backup holds, so this bounds its memory whatever the size of
#: the database.
_SQLITE_COPY_CHUNK_BYTES = 1024 * 1024


def _backup_sqlite(src: Path, dst: IO[bytes]) -> int:
    """Write a consistent copy of the SQLite database *src* to *dst*; return its size.

    The backup API copies the committed pages straight into a private temp file,
    which is then streamed to *dst* in fixed-size reads. The export passes the open
    archive member as *dst*, so a database of any size costs one copy buffer of
    memory, not the database. The temp file is removed before this
    returns, whether it succeeds or fails.
    """
    src_conn = sqlite3.connect(src.absolute().as_uri() + "?mode=ro", uri=True)
    try:
        fd, tmp_name = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            disk_conn = sqlite3.connect(tmp_name)
            try:
                src_conn.backup(disk_conn)
            finally:
                disk_conn.close()
            with open(tmp_name, "rb") as copy:
                shutil.copyfileobj(copy, dst, _SQLITE_COPY_CHUNK_BYTES)
                return copy.tell()
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
    finally:
        src_conn.close()


def _keep_for_export(rel: PurePath) -> bool:
    """The old loop's by-name filters, unchanged and in the same order.

    Purely lexical, and it has to stay that way: this runs on a child the walk
    has not verified yet, so a filesystem call here would be the very probe the
    walk exists to remove.

    The old loop also asked ``is_sensitive_path`` at this point, about the
    PATHNAME. That call is gone rather than moved, because resolving an
    unverified name is the probe. The same question is still asked -- in
    :func:`_open_verified`, of the descriptor's real path -- and that was always
    the load-bearing one: a name can be re-pointed between the check and the
    open, a descriptor cannot.
    """
    # ``PurePosixPath(*rel.parts)``, never ``PurePosixPath(str(rel))``: on Windows
    # ``str(rel)`` is backslash-separated, so ``PurePosixPath`` parses the whole
    # relative path as a SINGLE component. ``.name`` is then the entire path and
    # ``.parts`` has length one, so the ``EXPORT_EXCLUDE`` basename set and the
    # ``EXCLUDE_DIRS`` walk both stop matching -- ``workspace/notes/.env`` was
    # exported on Windows. Rebuilding from ``parts`` keeps the separator the
    # exclusion rules are written against.
    if _is_excluded(PurePosixPath(*rel.parts)):
        return False
    return not (rel.parts[0] == "skills" and "auto" in rel.parts)


def _keep_store_for_export(rel: PurePath) -> bool:
    """`_keep_for_export` for the ``memory_stores/`` tree, plus that tree's own two rules.

    Host-local state -- the member signing key, execution logs, local backup directories --
    stays on this host (`memory_stores.is_host_local_store_state`, the same predicate the
    snapshot applies). SQLite sidecars stay too: a store's databases are copied through the
    backup API below, which yields a self-contained file, and a ``-wal`` archived next to
    that copy would be replayed into a database it never belonged to. Lexical only, like
    the filter it extends.
    """
    if not _keep_for_export(rel):
        return False
    posix = PurePosixPath(*rel.parts)
    if is_host_local_store_state(posix.parts):
        return False
    return not any(posix.match(glob) for glob in _DB_SIDECAR_GLOBS)


def _walk_contained(
    root_real: str,
    rel_dir: PurePath,
    keep: Callable[[PurePath], bool],
    *,
    fenced_ok: bool = False,
) -> Iterator[tuple[PurePath, int]]:
    """Yield ``(rel, fd)`` for every regular file under ``root_real / rel_dir``.

    The export's own enumeration, in place of ``rglob``. ``rglob`` DESCENDS a
    Windows junction, and every by-name question then asked about what it yields
    resolves that junction before any guard has run. Measured on this module
    before the change, exporting a workspace holding one pre-planted junction:
    twelve resolving calls went out through it -- four ``os.path.realpath``,
    eight ``ntpath._getfinalpathname`` -- while the export correctly archived
    nothing from behind it. Nothing being archived was never the question. If the
    junction names a UNC share those resolutions are the outbound authentication,
    and containment refusing the bytes afterwards has already paid the cost it
    exists to prevent.

    So this descends and verifies in one motion, root first:

    1. the directory is PINNED. :func:`platform_compat.pin_directory` opens it
       with ``OPEN_REPARSE_POINT``, so a junction sitting at the name fails here
       instead of being traversed -- the refusal and the open are one operation,
       not a check followed by an open. On Windows the handle also omits
       ``FILE_SHARE_DELETE``, so while it lives neither that directory nor
       anything above it can be renamed or deleted;
    2. only then is it listed, and each child classified from the listing itself
       (:func:`_entry_is_link`) -- nothing resolves a child;
    3. a child directory is descended only by re-entering at step 1, so the path
       the kernel walks to reach component *n* runs entirely through components
       already opened and verified;
    4. a child file is opened by :func:`_open_verified`, which does not follow a
       reparse point at the final name, while its whole parent chain is still
       held.

    Each pin is held for as long as the subtree under it is being produced --
    a generator frame stays alive across ``yield``, so an outer level's handle
    outlives the inner walk -- and released in ``finally``, which also runs when
    the consumer abandons the walk. Depth, not breadth, bounds how many are open.

    *keep* is asked about relative paths only and must not touch the filesystem.
    It is applied to FILES only, exactly where the old loop applied it: matching
    a directory NAME does not prune its contents, because ``_is_excluded``
    decides that through ``parts`` and did so before.

    On POSIX ``pin_directory`` is ``O_RDONLY | O_DIRECTORY | O_NOFOLLOW``. Its
    refusal of a symlinked directory is real there and is what ``rglob`` already
    did, so the set of exported files is unchanged; the anti-rename property is
    NOT real there -- POSIX has no such lock -- and nothing here relies on it.
    Containment on POSIX rests where it always did, on ``_open_verified``
    checking the descriptor's real path.
    """
    yield from _walk_pinned(root_real, PurePath(), rel_dir.parts, keep, fenced_ok=fenced_ok)


def _walk_pinned(
    root_real: str,
    rel_dir: PurePath,
    descend: tuple[str, ...],
    keep: Callable[[PurePath], bool],
    *,
    fenced_ok: bool = False,
) -> Iterator[tuple[PurePath, int]]:
    """Pin ``root_real / rel_dir``, then walk it -- or step into *descend*'s first name.

    One frame per directory, and the frame that lists a directory is the frame
    that pinned it, so nothing is ever listed by a frame that did not verify it.
    The chain therefore starts at the crew root itself: the kernel walks that name
    to reach every candidate, so leaving it unpinned would leave the whole export
    hanging off a component that could still be renamed away.

    *descend* carries the components between the root and the tree being exported
    (``workspace``, ``plan_memory``, ``skills``). They are stepped through by NAME
    with no classification, which is safe for the same reason the walk needs no
    pre-check anywhere else: :func:`platform_compat.pin_directory` refuses a
    reparse point at the name itself, so a junction planted at ``workspace``
    fails at its own open rather than being followed.
    """
    here = os.path.join(root_real, *rel_dir.parts)
    try:
        pin = platform_compat.pin_directory(here)
    except OSError:
        return  # not a real directory, or a reparse point: refused, not followed
    try:
        if descend:
            yield from _walk_pinned(
                root_real, rel_dir / descend[0], descend[1:], keep, fenced_ok=fenced_ok
            )
            return
        try:
            with os.scandir(here) as scan:
                entries = sorted(scan, key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if _entry_is_link(entry):
                continue
            rel = rel_dir / entry.name
            if entry.is_dir(follow_symlinks=False):
                yield from _walk_pinned(root_real, rel, (), keep, fenced_ok=fenced_ok)
            elif entry.is_file(follow_symlinks=False) and keep(rel):
                fd = _open_verified(
                    os.path.join(root_real, *rel.parts), root_real, fenced_ok=fenced_ok
                )
                if fd is not None:
                    yield rel, fd
    finally:
        with contextlib.suppress(OSError):
            os.close(pin)


def _open_verified(target: str, root_real: str, *, fenced_ok: bool = False) -> int | None:
    """Open *target* without following a link at its name, and vet the descriptor.

    Split out so :func:`_open_inside` can hold the ancestor pins across the whole
    of it: the descriptor checks below are only worth anything while the path they
    were reached through is still the path that was verified.

    *fenced_ok* admits a file the agent fence (`is_sensitive_path`) covers, and is set
    for exactly one walk: ``memory_stores/``. That whole tree is fenced so a crew's agent
    cannot read another crew's memory; the export is the OPERATOR downloading their own
    install, and a private store is their memory as much as the default one is. The
    fence is not the only screen, so lifting it lifts nothing else: containment in the
    data home, the regular-file and single-link checks and the tree's own lexical filter
    all still apply, and the filter is what keeps the one credential that lives under
    the tree (the member signing key) out of the archive.
    """
    try:
        fd = platform_compat.open_file_no_reparse(target, nonblocking=True)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_nlink > 1:
            # A hardlink is invisible to every path-based guard: it shares the
            # target's inode, so the fd's real path is the ALIAS's own name --
            # `fd_real_path` reports the path the descriptor was opened by, not a
            # canonical one -- and `is_sensitive_path` is then asked about an
            # innocent workspace name while the bytes behind it are a credential
            # file's. `O_NOFOLLOW` has no link to refuse, because there is no
            # symlink. Only the link COUNT, read off this descriptor, sees it.
            #
            # Refused rather than resolved: there is no way to ask "which of my
            # names is the sensitive one?", and the cost is honest and small --
            # a workspace file that legitimately has a second link is left out of
            # the export. Same rule and same reasoning as
            # `pinned_fs.refuse_hardlink_alias` and
            # `hooks.safe_read_file_bytes_nolink`.
            return None
        real = pinned_fs.fd_real_path(fd)
        if real is None:
            return None  # cannot witness containment -> fail closed
        try:
            if os.path.commonpath([real, root_real]) != root_real:
                return None
        except ValueError:  # different drives on Windows
            return None
        if not fenced_ok and is_sensitive_path(real):
            return None
    except OSError:
        return None
    else:
        held, fd = fd, -1
        return held
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)


@contextlib.contextmanager
def _open_contained_file(root_real: str, rel: PurePath) -> Iterator[int | None]:
    """Open one export file with every ancestor pinned until its descriptor closes."""
    with contextlib.ExitStack() as held:
        here = root_real
        try:
            for part in ("", *rel.parts[:-1]):
                here = os.path.join(here, part)
                pin = platform_compat.pin_directory(here)
                held.callback(os.close, pin)
        except OSError:
            yield None
            return
        fd = _open_verified(os.path.join(root_real, *rel.parts), root_real)
        if fd is not None:
            held.callback(os.close, fd)
        yield fd


# ZIP's DOS date fields have a seven-bit year and two-second resolution.
_ZIP_DATE_MIN = (1980, 1, 1, 0, 0, 0)
_ZIP_DATE_MAX = (2107, 12, 31, 23, 59, 58)


def _zip_date_time(epoch: float) -> tuple[int, int, int, int, int, int]:
    """Clamp header time only; session manifests retain the descriptor's true epoch."""
    try:
        date_time = time.localtime(epoch)[:6]
    except (OSError, OverflowError, ValueError):
        # Some platforms cannot convert negative or very large filesystem epochs.
        return _ZIP_DATE_MIN if epoch < 0 else _ZIP_DATE_MAX
    return min(_ZIP_DATE_MAX, max(_ZIP_DATE_MIN, date_time))


def _add_from_fd(zf: zipfile.ZipFile, fd: int, arcname: str) -> None:
    """Stream the bytes behind *fd* into *zf* as *arcname*.

    ``ZipFile.write`` takes a NAME and opens it itself, which is the re-open
    :func:`_open_inside` exists to remove, so the entry is built by hand instead.
    Streaming rather than reading the file whole is deliberate: a workspace file
    has no size bound here, and the export copies one of any size.

    The entry's timestamp and mode come from the same descriptor, so the metadata
    describes the bytes actually archived — not whatever the name pointed at when
    the header was built.

    ``force_zip64`` is not optional here. ``ZipFile.write`` took a name, stat'd it,
    and turned ZIP64 on by itself for a large source; a streamed entry does not know
    its size when the header is written, so without this a file over
    ``zipfile.ZIP64_LIMIT`` raises ``RuntimeError`` part-way through and the export
    endpoint answers 500. A multi-GiB file under ``workspace/`` is ordinary — a
    dataset, a model artifact — and must export, so leaving this off would trade
    one defect for another.
    """
    st = os.fstat(fd)
    info = zipfile.ZipInfo(arcname, date_time=_zip_date_time(st.st_mtime))
    info.compress_type = zf.compression
    info.external_attr = (st.st_mode & 0xFFFF) << 16
    os.lseek(fd, 0, os.SEEK_SET)
    with (
        os.fdopen(os.dup(fd), "rb", closefd=True) as src,
        zf.open(info, "w", force_zip64=True) as dest,
    ):
        shutil.copyfileobj(src, dest)


def _add_session_from_fd(
    zf: zipfile.ZipFile, fd: int, arcname: str, mtimes: dict[str, float]
) -> None:
    """Record absolute activity time from the same descriptor as the archived bytes."""
    mtime = os.fstat(fd).st_mtime
    _add_from_fd(zf, fd, arcname)
    mtimes[arcname] = mtime


#: Minimum manifest allowance in the archive's uncompressed-byte budget. The actual
#: envelope is measured too, including the three counters at their maximum width.
_MANIFEST_BASE_RESERVE = 64 * 1024
#: Per record, beyond its JSON-encoded name: a float takes at most 24 ASCII bytes;
#: indentation, colon, comma and newline take 8. This allowance covers both.
_MANIFEST_PER_MEMBER_RESERVE = 64


#: A frozen chat selection a streaming export computes before any body byte:
#: The stem order a streaming export's pre-pass hands the writer, most-recent-first.
#: The writer judges each stem authoritatively (privacy + running import budget), so
#: this only steers order; ``None`` lets the writer discover and sort the stems.
_SessionOrder = list[str]


def _write_sessions(
    zf: zipfile.ZipFile,
    mc_real: str,
    prefix: str,
    mtimes: dict[str, float],
    *,
    manifest: dict,
    order: list[str] | None = None,
) -> tuple[int, int, int]:
    """Write the persistent dashboard chats into *zf*; ``(exported, withheld, skipped)``.

    Each transcript is reopened and judged under its canonical history lock, which
    stays held through its threads and images so a deleted chat's private
    replacement cannot donate companions. The judgment -- privacy header and
    whether the chat still fits the archive's import budget -- happens under that
    same lock, immediately before the bytes are written, so a chat turned private
    or grown past the cap after the pre-pass is left out of the archive, not
    exported stale. The returned counts describe what the archive actually holds.

    The archive must pass this build's own import inventory check, so chats are
    budgeted against the caps import enforces (`_MAX_IMPORT_MEMBERS` and
    `_MAX_IMPORT_UNCOMPRESSED`), counting every member already in *zf* and a reserve
    for the manifest. The budget is charged from what each chat ACTUALLY adds to
    *zf* (``file_size`` is the uncompressed size the import check sums, and a SQLite
    backup's size includes its committed WAL pages), so a chat that grew since the
    pre-pass cannot push the archive over the ceiling.

    *order* is the stem order to attempt, most-recent-first (the streaming export's
    pre-pass hands its planned order so the header's skipped count is an estimate of
    this one). ``None`` discovers and sorts the candidates here.
    """
    stems = order if order is not None else _session_candidates(mc_real)

    manifest_base = _session_manifest_base(manifest, len(stems))
    manifest_bytes_left = _MAX_SETTINGS_DOCUMENT_BYTES - manifest_base
    infos = zf.infolist()
    members_left = _MAX_IMPORT_MEMBERS - len(infos) - 1
    bytes_left = (
        _MAX_IMPORT_UNCOMPRESSED
        - sum(i.file_size for i in infos)
        - max(_MANIFEST_BASE_RESERVE, manifest_base)
    )

    history = ConversationLog(base_dir=Path(mc_real) / SESSIONS_DIR_NAME)
    exported = withheld = skipped = 0
    for stem in stems:
        with history.locked_stems([stem]):
            rel = PurePath(SESSIONS_DIR_NAME, f"{stem}{_TRANSCRIPT_SUFFIX}")
            with _open_contained_file(mc_real, rel) as transcript_fd:
                if transcript_fd is None:
                    continue
                # Recheck privacy under the write lock: a chat planned while
                # persistent can be turned incognito/temporary before the stream
                # reaches it, and a restricted transcript (and its companions) must
                # never travel.
                if _transcript_header_verdict(_read_first_line_fd(transcript_fd)) is not None:
                    withheld += 1
                    continue
                members = 1
                manifest_need = _manifest_entry_size(f"{prefix}/{rel.as_posix()}")
                need = os.fstat(transcript_fd).st_size
                for comp_rel, fd in _chat_companions(mc_real, stem):
                    members += 1
                    need += os.fstat(fd).st_size
                    manifest_need += _manifest_entry_size(f"{prefix}/{comp_rel.as_posix()}")
                need += manifest_need
                if (
                    members > members_left
                    or need > bytes_left
                    or manifest_need > manifest_bytes_left
                ):
                    skipped += 1
                    continue
                before = len(zf.infolist())
                _add_session_from_fd(zf, transcript_fd, f"{prefix}/{rel.as_posix()}", mtimes)
            for comp_rel, fd in _chat_companions(mc_real, stem):
                _add_session_from_fd(zf, fd, f"{prefix}/{comp_rel.as_posix()}", mtimes)
            # Charge what was actually written, so a chat that grew (or a companion
            # that appeared) between sizing and write still counts against the cap.
            written = zf.infolist()[before:]
            members_left -= len(written)
            written_manifest_bytes = sum(_manifest_entry_size(i.filename) for i in written)
            manifest_bytes_left -= written_manifest_bytes
            bytes_left -= sum(i.file_size for i in written) + written_manifest_bytes
            exported += 1
    return exported, withheld, skipped


def _session_candidates(mc_real: str) -> list[str]:
    """The dashboard chat stems, most recently active first (transcript mtime)."""
    candidates: list[tuple[float, str]] = []
    for rel, fd in _walk_contained(
        mc_real, PurePath(SESSIONS_DIR_NAME), _keep_top_level_transcript
    ):
        try:
            mtime = os.fstat(fd).st_mtime
        finally:
            os.close(fd)
        stem = _chat_stem(rel.parts[1])
        if stem is not None:
            candidates.append((mtime, stem))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [stem for _mtime, stem in candidates]


def _session_manifest_base(manifest: dict, candidate_count: int) -> int:
    """The manifest envelope's byte reserve, with the three counters at full width.

    Each after-selection counter is at most the candidate count. Measure the real
    envelope (host/user strings included) at those upper bounds, using the writer's
    indent and default ASCII escaping.
    """
    envelope = {
        **manifest,
        "contents": {
            **manifest["contents"],
            "session_count": candidate_count,
            "sessions_withheld": candidate_count,
            "sessions_skipped_size": candidate_count,
        },
        "session_mtimes": {},
    }
    # A nonempty nested map adds a newline plus two closing-brace indent spaces.
    return len(json.dumps(envelope, indent=2).encode("utf-8")) + 3


def _plan_sessions(
    mc_real: str,
    prefix: str,
    *,
    base_members: int,
    base_bytes: int,
    manifest: dict,
) -> tuple[list[str], int]:
    """Estimate the chat selection stat-only; ``(order, skipped)``.

    Runs before the first body byte of a streaming export, so the handler can send
    the ``X-Kirocrew-Sessions-Skipped-Size`` header. ``order`` is EVERY candidate
    stem, most-recent-first -- the writer judges each one authoritatively (privacy +
    the real import budget) in :func:`_write_sessions` under its write lock and
    counts the withheld and skipped totals the manifest records. ``skipped`` here is
    only this pass's size estimate for the header: the writer can leave out MORE
    chats than this predicted (a chat turned private, or grown past the cap) or
    FEWER (a chat deleted or shrunk between this pass and the write), so the header
    is an ESTIMATE of what the archive leaves out, not an exact count either way.

    *base_members* / *base_bytes* describe the non-session members this export will
    carry, measured stat-only (:func:`_measure_nonsession_footprint`). A SQLite
    member is sized by its logical page total (committed WAL included), the same
    bytes its backup writes, so the estimate does not undercount an active store.
    """
    candidates = _session_candidates(mc_real)

    manifest_base = _session_manifest_base(manifest, len(candidates))
    manifest_bytes_left = _MAX_SETTINGS_DOCUMENT_BYTES - manifest_base
    members_left = _MAX_IMPORT_MEMBERS - base_members - 1
    bytes_left = _MAX_IMPORT_UNCOMPRESSED - base_bytes - max(_MANIFEST_BASE_RESERVE, manifest_base)

    history = ConversationLog(base_dir=Path(mc_real) / SESSIONS_DIR_NAME)
    skipped = 0
    for stem in candidates:
        with history.locked_stems([stem]):
            rel = PurePath(SESSIONS_DIR_NAME, f"{stem}{_TRANSCRIPT_SUFFIX}")
            with _open_contained_file(mc_real, rel) as transcript_fd:
                if transcript_fd is None:
                    continue
                if _transcript_header_verdict(_read_first_line_fd(transcript_fd)) is not None:
                    continue  # the writer counts the authoritative withheld total
                members = 1
                manifest_need = _manifest_entry_size(f"{prefix}/{rel.as_posix()}")
                need = os.fstat(transcript_fd).st_size
                for comp_rel, fd in _chat_companions(mc_real, stem):
                    members += 1
                    need += os.fstat(fd).st_size
                    manifest_need += _manifest_entry_size(f"{prefix}/{comp_rel.as_posix()}")
                need += manifest_need
                if (
                    members > members_left
                    or need > bytes_left
                    or manifest_need > manifest_bytes_left
                ):
                    skipped += 1
                    continue
            members_left -= members
            manifest_bytes_left -= manifest_need
            bytes_left -= need
    return candidates, skipped


def _manifest_entry_size(arcname: str) -> int:
    """Reserve a record using the manifest writer's default JSON string escaping."""
    return len(json.dumps(arcname).encode("utf-8")) + _MANIFEST_PER_MEMBER_RESERVE


def _chat_companions(mc_real: str, stem: str) -> Iterator[tuple[PurePath, int]]:
    """``(rel, fd)`` for each companion of *stem* that travels; each fd closes on resume.

    The reply-thread sidecar, the flat image directory and the retained rotation
    segments. Callers hold the chat's history lock across the whole iteration.
    """
    sidecar = PurePath(SESSIONS_DIR_NAME, THREADS_DIR_NAME, f"{stem}{THREADS_SIDECAR_SUFFIX}")
    with _open_contained_file(mc_real, sidecar) as sidecar_fd:
        if sidecar_fd is not None:
            yield sidecar, sidecar_fd
    # sessions/<stem>.attachments/ is flat: only its direct files travel.
    images = PurePath(SESSIONS_DIR_NAME, f"{stem}{ATTACHMENTS_DIR_SUFFIX}")
    for rel, fd in _walk_contained(
        mc_real,
        images,
        lambda rel: len(rel.parts) == 3 and rel.parts[1] == f"{stem}{ATTACHMENTS_DIR_SUFFIX}",
    ):
        try:
            yield rel, fd
        finally:
            os.close(fd)
    archive = PurePath(SESSIONS_DIR_NAME, ARCHIVE_DIR_NAME)
    for rel, fd in _walk_contained(
        mc_real,
        archive,
        lambda rel: len(rel.parts) == 3 and _rotation_segment_for_stem(rel.name, stem),
    ):
        try:
            if _is_rotation_header(_read_first_line_fd(fd)):
                yield rel, fd
        finally:
            os.close(fd)

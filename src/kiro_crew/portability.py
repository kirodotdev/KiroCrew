"""Portable zip export/import for KiroCrew state (dashboard endpoint).

Creates a zip archive of all KiroCrew settings and memory for download
via the dashboard, and restores from uploaded zip archives. Designed to
work over HTTP for remote users (e.g. Linux Cloud Desktop → macOS browser).

Credentials (.env, session secrets) are always excluded from exports.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import socket
import stat
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePath, PurePosixPath

try:
    import pysqlite3 as sqlite3
except ImportError:
    import sqlite3

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_cron import _log_cron_denial, _vet_shell_command
from kiro_crew.security import is_sensitive_path
from kiro_crew.snapshot import (
    NotificationCopyUnsupported,
    _copy_notifications,
    _copy_tree_no_overwrite,
    _do_replace,
    _merge_crons,
    _merge_memory,
    _merge_notifications,
    _staging_is_pinned,
)
from kiro_crew.zip_vet import ZipInventoryRejected, vet_zip_inventory

logger = logging.getLogger(__name__)

EXPORT_EXCLUDE = frozenset(
    {
        ".env",
        ".local_secret",
        "sel_hmac.key",
        "telemetry_salt",
        # NOTE: the beacon's per-install identity files (beacon_install_id /
        # beacon_last_sent) are deliberately NOT listed here. This set is matched by
        # BASENAME and `_is_excluded` runs over the workspace/, plan_memory/ and
        # skills/ trees, so an entry here would silently drop any USER file that
        # happens to share the name. They need no entry: root-level export is a
        # hard-coded allowlist (config.json, hooks.json, crons.json,
        # notifications.jsonl, project_dir, workspace_dir), so a root beacon file is
        # never selected in the first place.
        "session_map.json",
        "kiro_session_pids.txt",
        "kiro_pids.txt",
    }
)

EXCLUDE_DIRS = frozenset(
    {
        "snapshots",
        "outbox",
        "uploads",
        "__pycache__",
    }
)


def _mc_dir() -> Path:
    return Path(os.environ.get("KIROCREW_HOME", config_dir()))


def _is_excluded(rel_path: PurePosixPath) -> bool:
    if rel_path.name in EXPORT_EXCLUDE:
        return True
    if rel_path.name.endswith(".pid"):
        return True
    for part in rel_path.parts:
        if part in EXCLUDE_DIRS:
            return True
    return False


def _wal_checkpoint(db_path: Path) -> None:
    if db_path.is_file():
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.close()
        except Exception:
            logger.debug("WAL checkpoint failed for %s", db_path)


def _backup_sqlite(src: Path, dst_buffer: io.BytesIO) -> None:
    """Use SQLite backup API for a consistent copy."""
    src_conn = sqlite3.connect(str(src))
    mem_conn = sqlite3.connect(":memory:")
    try:
        src_conn.backup(mem_conn)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        try:
            disk_conn = sqlite3.connect(tmp.name)
            try:
                mem_conn.backup(disk_conn)
            finally:
                disk_conn.close()
            dst_buffer.write(Path(tmp.name).read_bytes())
        finally:
            os.unlink(tmp.name)
    finally:
        src_conn.close()
        mem_conn.close()


#: A Windows junction is a reparse point whose tag is not the symlink tag, so
#: ``islink``/``DirEntry.is_symlink`` are False for one. The ATTRIBUTE is what
#: every kind of reparse point has in common. Absent off Windows, where the
#: concept does not exist.
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _entry_is_link(entry: os.DirEntry) -> bool:
    """True if *entry* is a symlink or, on Windows, any other reparse point.

    Answered entirely from the directory listing the kernel has ALREADY
    returned: ``FindFirstFileW`` carries ``dwFileAttributes`` and the reparse tag
    inline, so ``entry.stat(follow_symlinks=False)`` reads cached bytes rather
    than issuing a lookup -- and, decisively, never a lookup THROUGH the child.
    That is the property this walk is built on. The ordinary way to ask the same
    question, ``Path(child).is_file()`` or ``is_sensitive_path(str(child))``,
    resolves the name; if a junction there aims at ``\\\\host\\share`` that
    resolution IS an outbound SMB authentication, and no later refusal recalls
    it.

    ``DirEntry.is_symlink()`` alone is not enough and is exactly how ``rglob``
    came to descend a junction: a junction's tag is ``IO_REPARSE_TAG_MOUNT_POINT``
    rather than ``IO_REPARSE_TAG_SYMLINK``, so that method reports False for one
    while ``is_dir(follow_symlinks=False)`` reports True.

    Every reparse point is treated the same rather than only link-shaped ones,
    because that is already what the layer below refuses --
    :func:`platform_compat.pin_directory` rejects any reparse point at a
    directory name and :func:`platform_compat.open_file_no_reparse` rejects one
    at a file name. This classifies what those opens would refuse anyway: a cheap
    skip, never the enforcement. On POSIX the attribute check is skipped
    entirely; ``is_symlink()`` uses ``d_type`` and costs nothing, and asking for
    a stat there would add a real syscall for a concept the platform lacks.
    """
    if entry.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return True  # unclassifiable -> refuse to walk into it
    return bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


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


def _walk_contained(
    root_real: str,
    rel_dir: PurePath,
    keep: Callable[[PurePath], bool],
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
    yield from _walk_pinned(root_real, PurePath(), rel_dir.parts, keep)


def _walk_pinned(
    root_real: str,
    rel_dir: PurePath,
    descend: tuple[str, ...],
    keep: Callable[[PurePath], bool],
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
            yield from _walk_pinned(root_real, rel_dir / descend[0], descend[1:], keep)
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
                yield from _walk_pinned(root_real, rel, (), keep)
            elif entry.is_file(follow_symlinks=False) and keep(rel):
                fd = _open_verified(os.path.join(root_real, *rel.parts), root_real)
                if fd is not None:
                    yield rel, fd
    finally:
        with contextlib.suppress(OSError):
            os.close(pin)


def _open_verified(target: str, root_real: str) -> int | None:
    """Open *target* without following a link at its name, and vet the descriptor.

    Split out so :func:`_open_inside` can hold the ancestor pins across the whole
    of it: the descriptor checks below are only worth anything while the path they
    were reached through is still the path that was verified.
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
        if is_sensitive_path(real):
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
    info = zipfile.ZipInfo(arcname, date_time=time.localtime(st.st_mtime)[:6])
    info.compress_type = zf.compression
    info.external_attr = (st.st_mode & 0xFFFF) << 16
    os.lseek(fd, 0, os.SEEK_SET)
    with (
        os.fdopen(os.dup(fd), "rb", closefd=True) as src,
        zf.open(info, "w", force_zip64=True) as dest,
    ):
        shutil.copyfileobj(src, dest)


def create_export_zip() -> tuple[bytes, dict]:
    """Create a zip archive of KiroCrew state. Returns (zip_bytes, manifest_dict)."""
    mc = _mc_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"kirocrew-export-{ts}"

    _wal_checkpoint(mc / "memory.db")
    _wal_checkpoint(mc / "memory_index.db")

    buf = io.BytesIO()
    contents_summary: dict = {}

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # Core JSON/text files
        for fname in (
            "config.json",
            "hooks.json",
            "crons.json",
            "notifications.jsonl",
            "project_dir",
            "workspace_dir",
        ):
            src = mc / fname
            if src.is_file() and not src.is_symlink():
                zf.write(str(src), f"{prefix}/{fname}")
                contents_summary[fname] = src.stat().st_size

        # SQLite databases via backup API
        for db_name in ("memory.db", "memory_index.db"):
            src = mc / db_name
            if src.is_file() and not src.is_symlink():
                db_buf = io.BytesIO()
                _backup_sqlite(src, db_buf)
                zf.writestr(f"{prefix}/{db_name}", db_buf.getvalue())
                contents_summary[db_name] = db_buf.tell()

        # Directory trees: workspace, plan_memory, skills
        #
        # ``mc`` is resolved once, before the walk, and the walk is anchored on the
        # RESOLVED root: ``$KIROCREW_HOME`` may itself legitimately be a link, and
        # ``pin_directory`` refuses a reparse point at a name, so pinning the
        # configured spelling would return an empty archive on such a host.
        mc_real = os.path.realpath(mc)
        dir_counts: dict[str, int] = {}
        for dirname in ("workspace", "plan_memory", "skills"):
            count = 0
            for rel, fd in _walk_contained(mc_real, PurePath(dirname), _keep_for_export):
                try:
                    # ``as_posix()`` for the same reason the filter rebuilds from
                    # parts: a zip member name is POSIX-separated by spec.
                    # ``ZipInfo`` happens to rewrite ``os.sep`` today, so this is
                    # not a fix for a live bug -- it stops the archive name
                    # depending on that, next to a filter that must not.
                    _add_from_fd(zf, fd, f"{prefix}/{rel.as_posix()}")
                finally:
                    os.close(fd)
                count += 1
            dir_counts[dirname] = count
        contents_summary["workspace_files"] = dir_counts.get("workspace", 0)
        contents_summary["plan_memory_files"] = dir_counts.get("plan_memory", 0)
        contents_summary["skill_count"] = dir_counts.get("skills", 0)

        # Manifest
        manifest = {
            "version": 2,
            "format": "zip",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hostname": socket.gethostname(),
            "user": os.environ.get("USER", "unknown"),
            "contents": contents_summary,
        }
        zf.writestr(f"{prefix}/MANIFEST.json", json.dumps(manifest, indent=2))

    return buf.getvalue(), manifest


# Zip-bomb guards for import archives (CWE-409). A real personal snapshot is far
# below these ceilings; a decompression bomb (huge declared uncompressed size, or
# millions of entries) is rejected before extraction rather than filling the
# host disk.
_MAX_IMPORT_MEMBERS = 50_000
_MAX_IMPORT_UNCOMPRESSED = 2 * 1024**3  # 2 GiB


def _is_link_entry(info: zipfile.ZipInfo) -> bool:
    """True when a zip member declares itself a symlink or hardlink.

    CPython's ``ZipFile.extract`` does NOT honor S_IFLNK -- it writes the link
    target as ordinary file content -- so a link member cannot currently redirect
    a later write outside the extraction root. This guard exists because that is a
    property of the extraction backend rather than of the archive: swapping in
    ``shutil.unpack_archive``, an external ``unzip``, or a future stdlib that
    honors the mode bit would silently turn a link member into a real symlink and
    make the escape reachable (CWE-22 via CWE-59). Rejecting these members keeps
    the guarantee at the archive boundary, where it does not depend on which
    extractor runs. Legitimate archives carry none: the export side skips symlinks.
    """
    mode = info.external_attr >> 16
    return bool(mode) and stat.S_ISLNK(mode)


def validate_import_zip(zip_path: Path) -> tuple[bool, str, dict]:
    """Validate a zip file for import.

    Returns (ok, error_message, manifest_dict).
    """
    try:
        vet_zip_inventory(zip_path, max_members=_MAX_IMPORT_MEMBERS)
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            names = zf.namelist()

            # Check for path traversal
            for name in names:
                parts = PurePosixPath(name).parts
                if ".." in parts or name.startswith("/"):
                    return False, f"Rejected path traversal: {name}", {}

            # Zip-bomb guard: bound entry count and total uncompressed size.
            infos = zf.infolist()
            if len(infos) > _MAX_IMPORT_MEMBERS:
                return (
                    False,
                    f"Rejected: archive has too many entries ({len(infos)} > {_MAX_IMPORT_MEMBERS})",
                    {},
                )
            total_uncompressed = sum(i.file_size for i in infos)
            if total_uncompressed > _MAX_IMPORT_UNCOMPRESSED:
                return (
                    False,
                    (
                        f"Rejected: uncompressed size {total_uncompressed} exceeds cap "
                        f"{_MAX_IMPORT_UNCOMPRESSED} (possible zip bomb)"
                    ),
                    {},
                )

            # Link members can redirect a later write outside the extraction root
            # even when every name passes the traversal check above.
            for info in infos:
                if _is_link_entry(info):
                    return False, f"Rejected link entry: {info.filename}", {}

            # Find manifest
            manifest_entries = [n for n in names if n.endswith("MANIFEST.json")]
            if not manifest_entries:
                return False, "No MANIFEST.json found in archive", {}

            manifest_data = json.loads(zf.read(manifest_entries[0]))
            version = manifest_data.get("version")
            if version not in (1, 2):
                return False, f"Unsupported manifest version: {version}", {}

            return True, "", manifest_data
    except ZipInventoryRejected as exc:
        if exc.reason == "too_many_members":
            return False, f"Rejected: archive has too many entries ({exc})", {}
        if exc.reason in {"cdir_too_large", "zip64_saturated"}:
            return False, f"Rejected: archive inventory exceeds cap ({exc}; possible zip bomb)", {}
        return False, "Invalid zip file", {}
    except zipfile.BadZipFile:
        return False, "Invalid zip file", {}
    except (json.JSONDecodeError, KeyError) as e:
        return False, f"Invalid manifest: {e}", {}


#: Stands in for a job name when the whole store had to be replaced, so the
#: import summary can say something happened without inventing a name.
_UNREADABLE_STORE = "<the whole cron store was unreadable>"


def _sanitize_imported_crons(crons_path: Path) -> tuple[list[str], list[str]]:
    """Make an imported cron store safe to load and safe to run.

    Returns ``(dropped, paused)`` — two lists, because they are two different
    outcomes and a caller that conflates them tells the user the wrong thing. A
    dropped job is gone; a paused one is fully restored and simply waiting to be
    switched on. Rewrites *crons_path* in place. A missing file is left alone.

    Three rules, each closing a different way an archive can act on the host:

    1. A job that is not an object, or whose ``schedule`` is not one, is DROPPED.
       ``CronService._load`` skips such a record with a warning and it is then
       silently dropped from the store on the next write — dead weight with an
       invisible deadline. Dropping it at import, with the drop REPORTED to the
       user, is the honest version of the same outcome.

    2. A ``command`` is vetted with ``mcp_cron._vet_shell_command``, so it is
       judged exactly as the same command would be at ``cron_add`` (deny-list,
       sensitive-path, credential-path and exfiltration checks). A failure DROPS
       the job, and so does the vet itself raising — an unverifiable command must
       not be scheduled.

    3. A job that survives with a ``command``, and any job naming a ``script``,
       is imported DISABLED (``user_paused``) rather than live, and reported as
       PAUSED, not rejected. The vet bounds what a command may do, not whether the
       user asked for THIS command on THIS machine, and a ``script`` cannot be
       vetted at all: the export never carries the ``crons/`` directory, so the
       name resolves against whatever the target already has there. Both become an
       ambush if they start running on their own. Disabling keeps the restore — the
       jobs, their schedules and their history are all still there — while making
       the first run an explicit human action. Message-only jobs are untouched:
       they prompt an agent, they do not execute anything on the host.
    """
    if not crons_path.is_file():
        return [], []
    try:
        data = json.loads(crons_path.read_text())
    except (ValueError, OSError):
        # Unparseable bytes are not installable as a cron store either, but they
        # are also not something this function can reason about — an empty store
        # is the only safe thing to hand the loader.
        crons_path.write_text(json.dumps({"jobs": []}, indent=2))
        return [_UNREADABLE_STORE], []
    # A store whose top level is not an object, or whose `jobs` is not a list, is
    # REPLACED rather than left alone. `CronService._load` treats such a document
    # as unsalvageable (empty registry, warning) — replacing it here means the
    # user is TOLD the store was unreadable at import time, instead of the
    # gateway silently starting with an empty schedule later.
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        crons_path.write_text(json.dumps({"jobs": []}, indent=2))
        return [_UNREADABLE_STORE], []
    jobs = data["jobs"]

    kept: list = []
    dropped: list[str] = []
    paused: list[str] = []
    changed = False

    def _name_of(job: object) -> str:
        name = job.get("name") if isinstance(job, dict) else None
        return str(name) if name else "<unnamed>"

    for job in jobs:
        # Rule 1: a shape unfit for the store must not survive the import. This
        # predicate is deliberately STRICTER than the loader's (it also demands
        # str-typed id/name/message, which `_job_from_record` would accept
        # untyped): the loader skips a bad record with a warning and the next
        # write drops it for good, so importing anything questionable only
        # manufactures dead weight with an invisible deadline. Dropping it here,
        # reported in the import summary, tells the user it happened.
        if (
            not isinstance(job, dict)
            or not all(isinstance(job.get(f), str) for f in ("id", "name", "message"))
            or not isinstance(job.get("schedule"), dict)
            or not isinstance(job["schedule"].get("kind"), str)
        ):
            dropped.append(_name_of(job))
            changed = True
            continue

        command = job.get("command", "")
        script = job.get("script", "")

        # Rule 2: the command is judged exactly as `cron_add` would judge it.
        if command:
            try:
                reason = _vet_shell_command(command)
            except Exception:  # noqa: BLE001 — unverifiable command must fail closed
                reason = "command could not be verified"
            if reason is not None:
                dropped.append(_name_of(job))
                changed = True
                # Same audit obligation as a `cron_add` denial: the dropped
                # command never reaches the ACP permission/hook flow, so this is
                # the only place the denial can be recorded. Named for where it
                # happened, so an import-time drop is not read as an attempted
                # `cron_add`.
                _log_cron_denial("settings_import", reason)
                continue

        # Rule 3: anything that EXECUTES arrives paused, awaiting a human.
        if (command or script) and not job.get("user_paused", False):
            job["user_paused"] = True
            job["enabled"] = False
            changed = True
            paused.append(_name_of(job))
            _log_cron_denial(
                "settings_import",
                "Error: an imported job that runs a command or script is "
                "restored paused until it is enabled by hand",
            )
        kept.append(job)

    if changed:
        data["jobs"] = kept
        crons_path.write_text(json.dumps(data, indent=2))
    return dropped, paused


def apply_import_zip(zip_path: Path, mode: str = "merge") -> dict:
    """Extract and apply an import zip.

    Args:
        zip_path: Path to validated zip file.
        mode: "merge" (default, non-destructive) or "replace" (overwrites).

    Returns summary dict of what was imported.
    """
    try:
        vet_zip_inventory(zip_path, max_members=_MAX_IMPORT_MEMBERS)
    except ZipInventoryRejected as exc:
        if exc.reason == "too_many_members":
            raise ValueError(f"Import archive has too many entries ({exc})") from exc
        if exc.reason in {"cdir_too_large", "zip64_saturated"}:
            raise ValueError(
                f"Import archive inventory exceeds cap ({exc}; possible zip bomb)"
            ) from exc
        raise zipfile.BadZipFile("Invalid zip file") from exc

    mc = _mc_dir()
    # Asked once, at the top, before anything is extracted or written. Both branches
    # below mutate the data home, and the merge branch writes core files with
    # shutil.copy2 BEFORE it reaches the first tree call -- so gating inside the tree
    # helpers let a merge on a platform that cannot pin half-apply the core files and
    # then raise, against this function's own "fails loudly, nothing was written"
    # contract. Raised in review. This is the third site of the same defect (after
    # _build_snapshot and _do_merge), which is why the gate now lives at the entry of
    # every operation that mutates rather than next to the individual writes.
    #
    # There is no flag to pass here: an import is a UI action, not a command line. I first
    # concluded from that that the gate should refuse, and it was the wrong conclusion drawn
    # from a correct observation -- CI proved it by failing four PRE-EXISTING portability
    # tests on Windows. Refuse-by-default only means "ask the user" where a consent surface
    # exists. Where none does, it means removing the feature on that platform, which is not
    # a security decision anyone made.
    #
    # So this path PERMITS a by-name traversal and records that it happened, while snapshot
    # and restore keep refusing -- because they have `--allow-unpinned-staging` and can
    # actually ask. The per-entry screens still apply either way: the copy opens with
    # O_NOFOLLOW and the walk rejects links and reparse points, so what is given up here is
    # ancestor-swap resistance, not link resistance.
    staging_pinned = _staging_is_pinned(allow_unpinned=True, what=f"{mode} import")

    # What this field may honestly say depends on the MODE, not only the platform.
    # Reporting "pinned" for a merge whose core files and skills are still copied by
    # name with `shutil` is true of the platform, false of the operation, and this
    # field exists to tell a reader what actually happened.
    #
    # replace delegates the whole apply to `_do_replace`, which is pinned throughout. merge
    # routes only its tree copy through the primitive; its core files (including the
    # databases, deliberately out of scope) and its skills copy are by name. So merge
    # on a pinnable platform is MIXED, and saying so is the point.
    if not staging_pinned:
        staging_mode = "unpinned"
    elif mode == "replace":
        staging_mode = "pinned"
    else:
        staging_mode = "mixed"
    if not staging_pinned:
        logger.warning(
            "%s import staged by name: this platform cannot open a directory relative to a "
            "descriptor, so an ancestor swapped mid-import could redirect the copy. The "
            "summary records staging=unpinned.",
            mode,
        )
    summary: dict = {
        "mode": mode,
        "items": [],
        "staging": staging_mode,
    }

    with tempfile.TemporaryDirectory() as work_str:
        work = Path(work_str)

        with zipfile.ZipFile(str(zip_path), "r") as zf:
            infos = zf.infolist()
            # Zip-bomb guard (defense-in-depth; validate_import_zip also checks).
            if len(infos) > _MAX_IMPORT_MEMBERS:
                raise ValueError(
                    f"Import archive has too many entries ({len(infos)} > {_MAX_IMPORT_MEMBERS})"
                )
            total_uncompressed = sum(i.file_size for i in infos)
            if total_uncompressed > _MAX_IMPORT_UNCOMPRESSED:
                raise ValueError(
                    f"Import archive uncompressed size {total_uncompressed} exceeds cap "
                    f"{_MAX_IMPORT_UNCOMPRESSED} (possible zip bomb)"
                )
            for info in infos:
                parts = PurePosixPath(info.filename).parts
                if ".." in parts or info.filename.startswith("/"):
                    continue
                if _is_link_entry(info):
                    continue
                zf.extract(info, work)

        snap_dirs = [d for d in work.iterdir() if d.is_dir()]
        if len(snap_dirs) != 1:
            raise ValueError(f"Expected 1 top-level directory in zip, found {len(snap_dirs)}")
        snap = snap_dirs[0]

        # Re-vet imported cron commands before ANY path below consumes
        # crons.json (merge, copy, or replace). An import archive is
        # attacker-influenced — the threat is a "settings backup" a user is
        # talked into importing — and a cron ``command`` is a free-form shell
        # string the scheduler later runs via ``sh -c``, entirely outside the
        # ACP permission/hook flow. ``cron_add`` guards exactly that out-of-band
        # execution with ``_vet_shell_command`` at storage time; the import path
        # wrote crons.json directly and so skipped it, turning a crafted archive
        # into arbitrary command execution with no CSRF, prompt injection, or
        # auth bypass required (CWE-502, CWE-862). Apply the identical guard here
        # and drop any job that fails, so a mostly-benign backup still restores
        # its safe jobs instead of the whole import aborting.
        dropped_crons, paused_crons = _sanitize_imported_crons(snap / "crons.json")
        if dropped_crons:
            summary["rejected_crons"] = dropped_crons
        # Reported separately: these are restored in full and only need switching
        # on, so calling them "rejected" would tell the user their jobs are gone.
        if paused_crons:
            summary["paused_crons"] = paused_crons

        if mode == "replace":
            # Strip sensitive files and skills/auto/ from snapshot before replace
            for excluded_name in EXPORT_EXCLUDE:
                excluded_file = snap / excluded_name
                if excluded_file.exists():
                    excluded_file.unlink()
            for fpath in snap.rglob("*"):
                if fpath.is_file() and is_sensitive_path(str(fpath)):
                    fpath.unlink()
            auto_dir = snap / "skills" / "auto"
            if auto_dir.is_dir():
                shutil.rmtree(str(auto_dir))
            # A platform that cannot pin a directory by descriptor refuses this
            # staging pass, and the refusal is allowed to propagate. An earlier
            # revision caught it and returned the summary, which was worse than the
            # crash it avoided: the caller reads a returned summary as success, so the
            # dashboard rendered "Import complete" over a data home nothing had been
            # written to. Raised in review. Propagating reaches the existing error
            # path, which is the one that tells the user the import did not happen.
            _do_replace(snap, mc, None, allow_unpinned=not staging_pinned)
            summary["items"].append("full replace")
        else:
            # Merge mode
            if (snap / "memory.db").is_file():
                if not (mc / "memory.db").is_file():
                    shutil.copy2(str(snap / "memory.db"), str(mc / "memory.db"))
                    if (snap / "memory_index.db").is_file():
                        shutil.copy2(str(snap / "memory_index.db"), str(mc / "memory_index.db"))
                    summary["items"].append("memory (copied)")
                else:
                    _merge_memory(snap / "memory.db", mc / "memory.db")
                    summary["items"].append("memory (merged)")

            if (snap / "crons.json").is_file():
                if (mc / "crons.json").is_file():
                    if _merge_crons(snap / "crons.json", mc / "crons.json"):
                        summary["items"].append("crons (merged)")
                    else:
                        # A refused merge imported zero jobs. Appending
                        # "crons (merged)" here regardless would render a
                        # success over a restore that brought no job back.
                        # The refusal is named in the
                        # items and flagged machine-readably so the handler can
                        # log the import as partial rather than a flat ok.
                        summary["items"].append("crons (skipped: unreadable or invalid cron store)")
                        summary.setdefault("refused_merges", []).append("crons")
                else:
                    shutil.copy2(str(snap / "crons.json"), str(mc / "crons.json"))
                    summary["items"].append("crons (copied)")

            if (snap / "hooks.json").is_file():
                if not (mc / "hooks.json").is_file():
                    shutil.copy2(str(snap / "hooks.json"), str(mc / "hooks.json"))
                    summary["items"].append("hooks (copied)")
                else:
                    summary["items"].append("hooks (skipped, already exists)")

            if (snap / "config.json").is_file() and not (mc / "config.json").is_file():
                shutil.copy2(str(snap / "config.json"), str(mc / "config.json"))
                summary["items"].append("config (restored)")

            if (snap / "notifications.jsonl").is_file():
                if (mc / "notifications.jsonl").is_file():
                    _merge_notifications(snap / "notifications.jsonl", mc / "notifications.jsonl")
                    summary["items"].append("notifications (merged)")
                else:
                    # Not `copy2`: it installed records the live file's own reader
                    # refuses, and that reader loses the whole file to one of them.
                    # Same abort posture as the merge branch above.
                    #
                    # The platform refusal is NOT that abort: it says this platform
                    # can never do this safely, so it skips one item and lets the
                    # import proceed. Recorded in the summary as skipped WITH the
                    # reason -- reporting "copied" for a refusal, or saying nothing,
                    # would be the silent-install bug class this change removes.
                    try:
                        _copy_notifications(
                            snap / "notifications.jsonl", mc / "notifications.jsonl"
                        )
                        summary["items"].append("notifications (copied)")
                    except NotificationCopyUnsupported as exc:
                        summary["items"].append(f"notifications (SKIPPED: {exc})")

            for dirname in ("workspace", "plan_memory"):
                sd = snap / dirname
                if sd.is_dir():
                    dd = mc / dirname
                    dd.mkdir(parents=True, exist_ok=True)
                    # The permission decided once at entry flows down; otherwise the inner
                    # gate re-asks and refuses, which is the same platform outage by a
                    # longer route.
                    _copy_tree_no_overwrite(sd, dd, allow_unpinned=not staging_pinned)
                    summary["items"].append(f"{dirname} (merged)")

            if (snap / "skills").is_dir():
                (mc / "skills").mkdir(parents=True, exist_ok=True)
                # Skip skills/auto/ — those must go through SkillsLoader APIs
                for item in (snap / "skills").iterdir():
                    if item.name == "auto":
                        continue
                    target = mc / "skills" / item.name
                    if item.is_dir() and not target.exists():
                        shutil.copytree(str(item), str(target))
                    elif item.is_file() and not target.exists():
                        shutil.copy2(str(item), str(target))
                summary["items"].append("skills (merged, auto/ skipped)")

    return summary

"""Merge restore: import a bundle into a live data home without overwriting what is there.

Additive by construction. Memory rows merge through an allowlisted SQL path, cron jobs by
name, notification records byte for byte, and trees copy in only what the destination
lacks. The merge driver, :func:`kiro_crew.snapshot_merge_driver._do_merge`, sequences these per
component. It skips a component whose data cannot be merged file by file, and refuses a
selection of nothing else (:data:`kiro_crew.snapshot_components._REPLACE_ONLY_COMPONENTS`);
replace restores those.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import stat as _stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.jsonl_util import RECORD_CAP, UndecodableRecord, UnreadableRecord
from kiro_crew.memory_stores import (
    MEMORY_STORES_DIR_NAME,
    is_host_local_store_state,
    memory_store_namespace_lock,
)
from kiro_crew.snapshot_archive import (
    _safe_name,
)
from kiro_crew.snapshot_components import (
    _facade,
    is_product_tree_database,
)

# Must match ``handlers_system._get_telemetry_salt`` (``secrets.token_bytes(32)``).
# ``_copy_locked`` loads telemetry_salt into memory, so a planted giant in an
# untrusted snapshot would OOM restore after earlier merge steps.
_TELEMETRY_SALT_BYTES = 32


def _copy_tree_no_overwrite(src: Path, dst: Path, *, allow_unpinned: bool = False) -> None:
    """Merge *src* into *dst* without overwriting, with both ends pinned.

    A by-name ``copy2`` walk leaves *dst*'s ancestors unpinned: a component swapped
    for a link after ``mkdir`` redirects the write. ``stage_tree_pinned`` with
    ``skip_existing=True`` creates each file exclusively, so "absent" and "created
    by this call" are one atomic fact. Use that one primitive, not a second walk.
    """
    facade = _facade()
    if not facade._staging_is_pinned(
        allow_unpinned=allow_unpinned, what=f"restore of {dst.name!r}"
    ):
        # Top-down with pruning, not `rglob("*")`: `rglob` still yields a link's
        # descendants after the link is skipped. Editing `dirnames` in place stops descent.
        #
        # Check both link kinds: `is_symlink()` is False for a Windows junction, and this
        # unpinned branch runs mainly on Windows (`--allow-unpinned-staging`).
        def _is_link(path: Path) -> bool:
            return path.is_symlink() or pinned_fs.is_reparse_point(path)

        for parent, dirnames, filenames in os.walk(src, topdown=True):
            here = Path(parent)
            kept = [d for d in dirnames if not _is_link(here / d)]
            for pruned in [d for d in dirnames if d not in kept]:
                facade._report_skip(pinned_fs.SKIP_SYMLINK, str(here / pruned))
            dirnames[:] = kept
            for name in kept:
                (dst / (here / name).relative_to(src)).mkdir(parents=True, exist_ok=True)
            for name in filenames:
                item = here / name
                if _is_link(item):
                    facade._report_skip(pinned_fs.SKIP_SYMLINK, str(item))
                    continue
                target = dst / item.relative_to(src)
                target.parent.mkdir(parents=True, exist_ok=True)
                # Not `copy2` behind `not target.exists()`: a link planted at the name in
                # between is followed. O_CREAT|O_EXCL|O_NOFOLLOW refuses the link and
                # gives skip-if-present without the race.
                pinned_fs.copy_file_pinned(
                    str(item), str(target), skip_existing=True, on_skip=facade._report_skip
                )
        return

    pinned_fs.stage_tree_pinned(
        src,
        dst,
        what=f"restore of {dst.name!r}",
        on_skip=facade._report_skip,
        skip_existing=True,
    )


def _install_core_file_if_absent(src: Path, dst: Path) -> bool:
    """Copy one top-level core file into the data home only where nothing is there.

    Returns True when the bundle's bytes were installed, False when the copy was
    skipped (the destination name is occupied, or the source is a link, a
    hardlink alias or not a regular file; a source skip is reported).

    Not ``is_file()`` then ``copy2``: ``copy2`` follows a dangling link out of the
    data home, and a staged source swapped between check and copy (say a hardlink
    to a credential) passes every name check. ``copy_file_pinned`` judges the
    source descriptor and creates the destination ``O_EXCL|O_NOFOLLOW``.
    """
    report = _facade()._report_skip
    src_fd: int | None = None
    if not pinned_fs.supports_pinned_walk():
        # No O_NOFOLLOW here (Windows): `copy_file_pinned`'s own by-name source open
        # would follow a reparse point at the staged name and judge the TARGET, which
        # passes the regular/one-link checks. `open_file_no_reparse` settles the final
        # name in the same operation that opens it, and the descriptor is handed over
        # so the source is never re-opened by name.
        try:
            src_fd = platform_compat.open_file_no_reparse(src, nonblocking=True)
        except OSError as exc:
            if exc.errno != errno.ELOOP:
                raise
            report(pinned_fs.SKIP_SYMLINK, str(src))
            return False
    return pinned_fs.copy_file_pinned(
        str(src), str(dst), src_fd=src_fd, skip_existing=True, on_skip=report
    )


def _core_file_not_installed(dst: Path, *, index: bool = False, dashboard: bool = False) -> str:
    """Say why ``_install_core_file_if_absent`` returned False for ``dst``.

    A refused source was already reported by the skip hook; an occupied name is a
    silent EEXIST. The name is looked up again only to pick the sentence.

    ``index`` is for ``memory_index.db``: an existing index is kept, so search misses
    the imported rows until the gateway rebuilds it on its next start
    (``MemoryStore.rebuild_index``).

    ``dashboard`` gives the import summary's item form: no "(see above)", and a
    parenthetical opener the Portability tab's ``NOT_APPLIED_ITEM`` reads as not applied.
    """
    occupied = os.path.lexists(dst)
    if index and dashboard:
        why = "kept the existing one" if occupied else "skipped: the archive's copy was refused"
        return f"memory search index ({why}; rebuilt when the gateway restarts)"
    if index:
        if occupied:
            return (
                f"{dst.name}: existing index kept, not replaced; the gateway rebuilds it "
                "on its next start"
            )
        return (
            f"{dst.name}: not installed; the bundle's copy was refused (see above); "
            "the gateway rebuilds it on its next start"
        )
    if occupied:
        return (
            f"{dst.name}: not restored; the existing entry at that name is not a "
            "regular file and is left as it is."
        )
    return f"{dst.name}: not restored; the bundle's copy was refused (see above)."


_MERGE_ALLOWED_TABLES = frozenset(
    {
        "semantic_memory",
        "episodic_memories",
        "knowledge_facts",
        "knowledge_edges",
    }
)


_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """Validate a SQL identifier against allowlist pattern. Raises ValueError if invalid."""
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def _merge_memory(src_db: Path, dst_db: Path) -> bool:
    """Row-merge src_db into dst_db; False when the source is refused and nothing is imported."""
    # The integrity check runs on the attached database, so the staged name is resolved
    # once and a database swapped in at that name after the check is never imported.
    # SQLite takes only a path, never a descriptor.
    facade = _facade()
    conn = facade.sqlite3.connect(str(dst_db))
    attached = False
    try:
        # Before BEGIN: an unreadable live DB raises here, not as the source's; no snapshot held.
        conn.execute("SELECT count(*) FROM main.sqlite_schema").fetchone()
        conn.execute("BEGIN")
        try:
            conn.execute("ATTACH DATABASE ? AS src", (str(src_db),))
            attached = True
            result = conn.execute("PRAGMA src.integrity_check;").fetchone()[0]
        except Exception as e:
            conn.rollback()
            print(f"  ⚠️  Source DB unreadable: {e} — skipping merge")
            return False
        if result != "ok":
            conn.rollback()
            print(f"  ⚠️  Source DB integrity check failed: {result} — skipping merge")
            return False
        for table, cols, where in [
            (
                "semantic_memory",
                "key, value_json, confidence, source, created_at, updated_at, embedding",
                "WHERE is_deleted=0",
            ),
            (
                "episodic_memories",
                "id, conversation_id, text, embedding, tags, importance, created_at, last_accessed_at",
                "WHERE is_deleted=0",
            ),
            ("knowledge_facts", "subject, predicate, object, episode_id, created_at", ""),
            (
                "knowledge_edges",
                "source_key, target_key, relation, weight, metadata, created_at",
                "",
            ),
        ]:
            if table not in _MERGE_ALLOWED_TABLES:
                raise ValueError(f"Table {table!r} not in merge allowlist")
            for col in cols.split(", "):
                _validate_identifier(col.strip())
            try:
                before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({cols}) "
                    f"SELECT {cols} FROM src.{table} {where}"
                )
                after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                label = table.replace("_", " ").title()
                print(f"  {label} imported: {after - before}")
            except facade.sqlite3.OperationalError as e:
                import logging

                # The snapshot command's logger: every snapshot/restore diagnostic reports
                # under the one name, whichever module the code sits in.
                logging.getLogger("kiro_crew.snapshot").warning("Skipping table %s: %s", table, e)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if attached:
            try:
                conn.execute("DETACH DATABASE src")
            except Exception:
                pass
        conn.close()
    return True


def _usable_cron_shape(parsed: object, path: Path) -> bool:
    """Refuse a crons file that parsed but is not shaped like a cron file.

    The merge looks ``jobs`` up on the result and calls ``.get`` on every
    entry, so valid JSON of the wrong shape -- a top level that is not an
    object, a ``jobs`` that is not a list, a job that is not an object, or a
    present name that is not encodable text -- would raise TypeError,
    AttributeError, or UnicodeEncodeError a line or two further down: the
    same crash the read guard exists to prevent, just moved. Only the structure the merge itself relies on is
    checked; the fields of a job are the cron loader's business, not this
    one's. A missing ``jobs`` key keeps its existing meaning of "no jobs".
    """
    if not isinstance(parsed, dict):
        print(f"  ⚠️  {path} is not a cron file — skipping cron merge")
        return False
    jobs = parsed.get("jobs", [])
    if not isinstance(jobs, list) or not all(
        isinstance(job, dict)
        and (
            "name" not in job
            or (
                isinstance(job["name"], str)
                # json.loads accepts lone-surrogate escapes, and a present name
                # is UTF-8 encoded when the import id is hashed: a surrogate
                # would raise UnicodeEncodeError there, the same crash moved.
                and not any("\ud800" <= ch <= "\udfff" for ch in job["name"])
            )
        )
        for job in jobs
    ):
        print(f"  ⚠️  {path} has an unusable job list — skipping cron merge")
        return False
    return True


def _merge_crons(src_path: Path, dst_path: Path) -> bool:
    """Merge the archive's cron jobs into the live store.

    Returns ``True`` when the merged store was written, ``False`` when the
    merge was refused: an unreadable source, an unreadable destination, or an
    unusable cron shape on either side. The refusal diagnostics stay on
    stdout, but a print is invisible to a caller with no terminal — the
    dashboard import reported "crons (merged)" over a refusal that imported
    zero jobs — so the outcome is also returned for the caller to report.
    A failing WRITE of the merged store is not a refusal: the ``OSError``
    propagates, which is the loud behavior the caller's error path expects.
    """
    # Each name is resolved ONCE. A by-name read followed by a by-name rewrite is two
    # resolutions: a link planted at the live name between them redirects the rewrite
    # into whatever it points at, and a link or hardlink alias in the staging tree is
    # read as the bundle's cron store. So each file is opened once, judged on its
    # descriptor, and read through it; the rewrite is an atomic replace inside the
    # SAME pinned directory (or, where the ACL cannot be carried, a write through the
    # read descriptor itself: `_cron_rewrite_in_place`), after checking the name still
    # holds the inode that was read. A rename replaces the directory entry and never
    # follows it, so no planted link can redirect the write.
    dst_dir_fd: int | None = None
    dst_fd: int | None = None
    in_place = False
    try:
        try:
            src = json.loads(_read_cron_store_once(src_path, what="bundle's cron store")[0])
        except (OSError, ValueError) as exc:
            print(f"  ⚠️  Could not read {src_path}: {exc} — skipping cron merge")
            return False
        try:
            if pinned_fs.supports_pinned_walk():
                dst_dir_fd = pinned_fs.pin_parent(
                    os.path.realpath(dst_path.parent), what="live cron store", refusal=OSError
                )
            in_place = dst_dir_fd is not None and _cron_rewrite_in_place()
            dst_text, dst_seen, dst_fd = _read_cron_store_once(
                dst_path,
                what="live cron store",
                dir_fd=dst_dir_fd,
                keep_open=dst_dir_fd is not None,
                writable=in_place,
            )
            dst = json.loads(dst_text)
        except (OSError, ValueError) as exc:
            print(f"  ⚠️  Could not read {dst_path}: {exc} — skipping cron merge")
            return False
        if not _usable_cron_shape(src, src_path) or not _usable_cron_shape(dst, dst_path):
            return False
        return _write_merged_crons(
            src, dst, dst_path, dst_seen, dst_dir_fd, dst_fd, in_place=in_place
        )
    finally:
        for fd in (dst_fd, dst_dir_fd):
            if fd is not None:
                os.close(fd)


def _cron_rewrite_in_place() -> bool:
    """Whether the cron merge rewrites the live store through its read descriptor.

    True where the store can be pinned but its access control cannot be carried
    onto a fresh inode: macOS, whose native ACLs sit behind ``acl_get_fd`` rather
    than the xattr API ``atomic_write`` carries (``ACCESS_CONTROL_XATTRS_SUPPORTED``
    is False there). An atomic replace on that platform would publish an inode
    without a deny entry the owner set, so the merge truncates and rewrites the
    inode it read and validated instead, which keeps its ACL. The cost is crash
    safety: a crash or a full disk mid-write can leave a torn store on that
    platform. Linux keeps the atomic replace with the ACL carried; Windows (no
    pinning) keeps it too.
    """
    from kiro_crew import atomic_write as _aw

    return pinned_fs.supports_pinned_walk() and not _aw.ACCESS_CONTROL_XATTRS_SUPPORTED


def _read_cron_store_once(
    path: Path,
    *,
    what: str,
    dir_fd: int | None = None,
    keep_open: bool = False,
    writable: bool = False,
) -> tuple[str, os.stat_result, int | None]:
    """Open *path* once, judge the descriptor, and read the text through it.

    Returns the text, the descriptor's ``fstat`` (the identity the caller re-checks
    before replacing the name) and, with *keep_open*, the descriptor, which the caller
    then owns. Keeping it open makes the re-check sound: an open inode cannot be freed,
    so its number cannot be reused by a file planted at the name. The rewrite also
    copies the store's access-control xattrs from it.

    Where the platform can pin, *path* is opened relative to *dir_fd* (or a parent
    pinned here) with ``O_NOFOLLOW``; elsewhere ``open_file_no_reparse`` refuses a link
    at the final name in the open itself. ``O_NONBLOCK`` keeps a FIFO from hanging the
    restore. The file must be regular with ONE link: only the link count exposes a
    hardlink alias of a credential. Refusals raise ``OSError``.

    *writable* opens read-write for ``_cron_rewrite_in_place``; it applies only where
    the platform can pin.
    """
    own_dir_fd = False
    if dir_fd is None and pinned_fs.supports_pinned_walk():
        dir_fd = pinned_fs.pin_parent(os.path.realpath(path.parent), what=what, refusal=OSError)
        own_dir_fd = True
    try:
        if dir_fd is not None:
            flags = (
                (os.O_RDWR if writable else os.O_RDONLY)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            fd = os.open(path.name, flags, dir_fd=dir_fd)
        else:
            fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    finally:
        if own_dir_fd and dir_fd is not None:
            os.close(dir_fd)
    try:
        seen = os.fstat(fd)
        if not _stat.S_ISREG(seen.st_mode):
            raise OSError(f"the {what} is not a regular file: {str(path)!r}")
    except BaseException:
        os.close(fd)
        raise
    # Closes *fd* itself before raising, so it sits outside the close-on-error arms.
    pinned_fs.refuse_hardlink_alias(fd, what=what, name=str(path), refusal=OSError)
    try:
        chunks = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        # Cron job names are operator-authored text and routinely non-ASCII, so the
        # locale codepage is the wrong decoder for this file on any host.
        text = b"".join(chunks).decode("utf-8")
    except BaseException:
        os.close(fd)
        raise
    if not keep_open:
        os.close(fd)
        return text, seen, None
    return text, seen, fd


def _write_merged_crons(
    src: dict[str, Any],
    dst: dict[str, Any],
    dst_path: Path,
    dst_seen: os.stat_result,
    dst_dir_fd: int | None,
    dst_fd: int | None,
    *,
    in_place: bool = False,
) -> bool:
    """Fold *src*'s new jobs into *dst* and write the live store.

    *dst_fd* is the live store's descriptor, still open from the read. By default
    the store is replaced atomically and the replacement carries its mode and its
    access-control xattrs (a named POSIX ACL) from *dst_fd*, so the merge never
    hands back a store protected more loosely than the one it replaced; an ACL that
    cannot be carried refuses the write and leaves the store as it was. With
    *in_place* (see ``_cron_rewrite_in_place``) the document is written through
    *dst_fd* itself, opened read-write, so the inode and its ACL are kept.
    """
    from kiro_crew.atomic_write import atomic_write, pinned_parent_replace_supported
    from kiro_crew.cron_service.zone_default_migration import settle_documents_before_merge

    # A backup from before "local" became the default keeps its unzoned jobs on UTC.
    if not settle_documents_before_merge(src, dst):
        print(
            "  ⚠️  config.json could not be read, so cron jobs from before the local "
            "time zone default cannot be settled — skipping cron merge"
        )
        return False
    existing = {j.get("name") for j in dst.get("jobs", [])}
    imported = 0
    for job in src.get("jobs", []):
        name = job.get("name")
        if not name or name in existing:
            continue
        job["id"] = hashlib.md5(f"{name}-imported".encode(), usedforsecurity=False).hexdigest()[:8]
        dst.setdefault("jobs", []).append(job)
        imported += 1
    document = json.dumps(dst, indent=2)
    seen_ident = (dst_seen.st_dev, dst_seen.st_ino)
    mode = _stat.S_IMODE(dst_seen.st_mode)
    # The name must still hold the inode that was read: a replacement swapped in
    # between the read and this write is not the store these jobs were merged into.
    # A refusal imports nothing, which the caller reports. The rename itself never
    # follows a link, so the remaining check-to-rename window can at worst replace
    # a swapped-in entry with the merged store -- never write through it.
    if dst_dir_fd is not None:
        now = pinned_fs.stat_at(dst_dir_fd, dst_path.name)
    else:
        now = pinned_fs.lstat_by_name(dst_path)
    if now is None or not _stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != seen_ident:
        print(f"  ⚠️  {dst_path} was replaced while the cron merge read it — skipping cron merge")
        return False
    if in_place and dst_fd is not None:
        data = document.encode("utf-8")
        os.lseek(dst_fd, 0, os.SEEK_SET)
        os.ftruncate(dst_fd, 0)
        view = memoryview(data)
        while view:
            view = view[os.write(dst_fd, view) :]
        os.fsync(dst_fd)
        total = len(src.get("jobs", []))
        print(f"  Cron jobs imported: {imported} (skipped {total - imported} duplicates)")
        return True
    pinned = dst_dir_fd is not None and pinned_parent_replace_supported()
    atomic_write(
        dst_path,
        document,
        fsync=True,
        mode=mode,
        # Windows (no dir_fd) has no xattrs to carry, and an extra open handle there
        # makes os.replace fail -- the reason `open_access_control_source` returns
        # None on that platform. The read descriptor is the carry source elsewhere.
        preserve_access_control_from=dst_fd if pinned else None,
        parent_dir_fd=dst_dir_fd if pinned else None,
    )
    total = len(src.get("jobs", []))
    print(f"  Cron jobs imported: {imported} (skipped {total - imported} duplicates)")
    return True


_TERMINATORS = (b"\n", b"\r")


# Longest single notification record either side of the merge will materialise.
# Both trees are agent-writable and the read feeds an append to a durable file,
# so an over-cap record aborts rather than being skipped. Named here, and read at
# call time, so a test can move the dial instead of writing a 128 MiB fixture --
# the same reason `subagent_cost` names its own.
_NOTIFICATION_RECORD_CAP = RECORD_CAP


# Largest notification SOURCE file this will hold, in bytes (the record cap above
# bounds one record, not a file of many). The live file never holds more than 400
# records (trimmed past 2 * _MAX_PERSISTED_NOTIFICATIONS after every append), so a
# larger source costs the operator nothing. 400 of the largest measured record
# (20,821 bytes) is ~8.3 MB; this is ~4x that.
#
# The read grows in 1 MiB chunks, so peak memory is about twice the source size,
# not twice this cap.
#
# Over-cap is a refusal naming the size, never a truncation: dropping the tail
# would be a partial install reported as success.
_NOTIFICATION_SOURCE_CAP = 32 * 1024 * 1024


def _notification_key(record: bytes, path: Path) -> tuple[Any, ...] | None:
    """The dedupe key for one raw notification record, or ``None`` if it has none.

    Hashable for every record shape: a non-object record or a list/dict ``ts``
    must not abort the restore.

    A truthy, hashable ``ts`` is keyed under a kind tag coarser than its type:
    ``1`` and ``1.0`` share ``"num"`` (a serializer artefact must still dedupe),
    while ``bool`` gets its own tag so ``True`` does not collide with ``1``.

    A record that parses but has no usable ``ts`` is keyed on its raw bytes,
    never stripped: stripping makes distinct bytes share a key, so a fragment
    ending in ``\r`` would match a crash-truncated row and be dropped. A record
    that does not parse gets ``None`` and is always appended, because the
    fragments of a split record are exactly the unparseable ones. Duplicating a
    row is recoverable; deleting one is not. ``"raw"`` is never a ``json.loads``
    type name, so byte keys cannot collide with ``ts`` keys.

    Raises :class:`UndecodableRecord` for invalid UTF-8. The decode only
    validates; the appended bytes are always the originals.
    """
    try:
        text = record.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UndecodableRecord(f"record is not valid UTF-8 in {path!r}") from exc
    try:
        parsed = json.loads(text)
    except ValueError:
        # Unkeyed, so never skipped: split-record fragments are the unparseable ones.
        return None
    ts = parsed.get("ts") if isinstance(parsed, dict) else None
    # An absent, empty or zero ts falls through to the raw-bytes key.
    if ts:
        try:
            hash(ts)
        except TypeError:
            pass
        else:
            # `bool` first: it is a subclass of `int`, so the numeric arm would
            # otherwise swallow it and re-create the True/1 collision.
            if isinstance(ts, bool):
                kind = "bool"
            elif isinstance(ts, (int, float)):
                kind = "num"
            else:
                kind = type(ts).__name__
            return (kind, ts)
    # Parsed, so a whole record: key on its raw bytes, never stripped.
    #
    # Key the bytes that LAND: the append terminates an unterminated record, so a
    # re-import must compare the terminated form or it appends a second copy. Add
    # the terminator, never `rstrip`: stripping maps `X\r` and `X` onto one key.
    return ("raw", record if record.endswith(_TERMINATORS) else record + b"\n")


def _open_notification_file(path: Path, flags: int, *, what: str) -> int:
    """Open *path* exactly once and judge the DESCRIPTOR, never the name again.

    Where the platform can pin, ``pinned_fs.open_in_pinned_parent`` walks the
    parent chain with ``O_NOFOLLOW``, so an agent-writable ancestor swapped for a
    link is refused too. Elsewhere (Windows) no open can settle the name for the
    read-write append this needs, so it raises :class:`NotificationCopyUnsupported`,
    which callers turn into a loud skip. A link/FIFO/hardlink on a capable
    platform raises ``OSError`` and aborts: that is a hostile or corrupt source.

    ``O_NONBLOCK`` keeps a name that became a FIFO from hanging the open;
    ``O_BINARY`` keeps the copy byte-exact on Windows. The descriptor must be a
    regular file with one link (a hardlink to a credential passes every link
    screen). Paths are embedded ``!r`` because archive names can carry ANSI
    controls and the caller prints the exception.
    """
    flags = (
        flags
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if not pinned_fs.supports_pinned_walk():
        # A platform incapacity, not a bad archive: callers skip loudly, not abort.
        raise NotificationCopyUnsupported(
            f"this platform ({os.name}) has no O_NOFOLLOW, so the {what} cannot be "
            f"opened with any guarantee that the name checked is the file read -- a "
            f"by-name reparse check followed by a by-name open is a window a "
            f"concurrent writer chooses the timing of, and fstat's S_ISREG does not "
            f"close it because a reparse point to a regular file passes it: "
            f"{str(path)!r} was NOT merged"
        )
    # Resolved by the caller ONCE, as pin_parent requires: resolving inside the
    # walk would re-follow whatever an ancestor points at by now.
    fd = pinned_fs.open_in_pinned_parent(
        os.path.realpath(str(path.parent)),
        path.name,
        flags=flags,
        mode=0o600,
        what=what,
        refusal=OSError,
    )
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{what} is not a regular file: {str(path)!r}")
    except BaseException:
        os.close(fd)
        raise
    # Closes *fd* itself before raising, so it sits outside the close-on-error arm.
    pinned_fs.refuse_hardlink_alias(fd, what=what, name=str(path), refusal=OSError)
    return fd


def _merge_notifications(src_path: Path, dst_path: Path) -> None:
    """Append the snapshot's notification records to the live file, byte for byte.

    Both handles are binary and framing comes from :func:`strict_raw_records`:
    text mode drops the ``\\r`` of ``\\r\\n``, splits a record at a bare ``\\r``,
    and under a single-byte locale lets invalid UTF-8 into the live file, after
    which its loader returns no rows at all.

    The posture is abort (raise), never skip or warn: a skipped record is a
    deleted one, and callers (``apply_import_zip``, the dashboard handler) report
    success unless this raises. ``_merge_crons`` may warn and return because it
    writes nothing on refusal; this one may already have appended a prefix.

    * A destination-scan failure is a no-op: nothing is written until both scans
      finish.
    * The source is validated whole before anything is appended, so a source
      refusal is a no-op too; otherwise a retry would re-append every row with no
      ``ts``, which cannot be deduplicated.
    * A failure during the copy keeps its prefix; rolling back would be a second
      unvalidated write. The held descriptor stops a name swap between passes,
      not an in-place rewrite of the same inode.

    Each name is opened once through :func:`_open_notification_file` and the
    descriptor is reused, so the bytes validated are the bytes copied; a by-name
    reopen would let an agent swap in a symlink and append its target into the
    live file.

    Every appended record ends with a terminator, and an unterminated last record
    in the destination gains one first, so two records never glue into one line.
    """
    facade = _facade()
    existing: set[tuple[Any, ...]] = set()
    dst_unterminated = False
    try:
        # One O_RDWR|O_APPEND descriptor for both the scan and the append. No
        # O_CREAT: the caller routes here only when the live file exists.
        dst_fd = _open_notification_file(
            dst_path, os.O_RDWR | os.O_APPEND, what="live notification file"
        )
    except OSError as exc:
        # No `_safe_name`: this is the operator's live data home, not an archive name.
        print(f"  ⚠️  Could not read {dst_path}: {exc} — merge aborted")
        raise
    try:
        out = os.fdopen(dst_fd, "r+b")
    except Exception:
        os.close(dst_fd)
        raise
    with out:
        try:
            for record in facade.strict_raw_records(
                out, dst_path, cap=facade._NOTIFICATION_RECORD_CAP
            ):
                key = facade._notification_key(record, dst_path)
                if key is not None:
                    existing.add(key)
                dst_unterminated = not record.endswith(_TERMINATORS)
        except (OSError, UnreadableRecord) as exc:
            # Same no-wrap rationale as the open above: the live path is the
            # operator's own, not archive-derived.
            print(f"  ⚠️  Could not read {dst_path}: {exc} — merge aborted")
            raise
        # The ENTIRE source is validated before anything is appended. Without this
        # pass, a source whose Nth record is undecodable or over-cap has already
        # appended N-1 records by the time it aborts -- and a retry re-appends
        # every identity-less one of those, because a row with no ``ts`` cannot be
        # deduplicated by construction. Validating first makes the source side
        # all-or-nothing in the ordinary case, so there is no prefix to duplicate.
        try:
            src_fd = _open_notification_file(
                src_path, os.O_RDONLY, what="snapshot notification file"
            )
        except OSError as exc:
            # The PATH goes through `_safe_name` because a bundle chooses its own
            # inner root, so an archive-derived path can carry ANSI controls -- and
            # printing one raw lets a hostile archive move the cursor and overwrite
            # lines right above the prompt where the operator decides whether to
            # trust the restore. The exception is safe to print raw:
            # `_open_notification_file`'s own refusals embed the path with `!r`,
            # and an `OSError` raised by the OS renders its filename repr-escaped.
            print(f"  ⚠️  Could not read {_safe_name(src_path)}: {exc} — merge aborted")
            raise
        try:
            f = os.fdopen(src_fd, "rb")
        except Exception:
            os.close(src_fd)
            raise
        with f:
            try:
                for record in facade.strict_raw_records(
                    f, src_path, cap=facade._NOTIFICATION_RECORD_CAP
                ):
                    facade._notification_key(record, src_path)
            except (OSError, UnreadableRecord) as exc:
                # The EXCEPTION deliberately skips `_safe_name`, and the invariant is
                # worth stating because it is what makes the wrapper unnecessary
                # rather than forgotten: both types this arm catches already render
                # an embedded path with repr-style escaping -- `OSError.__str__` does
                # it for its filename, and `jsonl_util` uses `{path!r}` for the
                # reason its own comment gives. Measured: a control character in a
                # directory name reaches neither exception's `str()` raw. Widening
                # this `except` tuple means re-checking that, because a type
                # formatting a path with `str()` would need the wrapper.
                print(f"  ⚠️  Could not read {_safe_name(src_path)}: {exc} — merge aborted")
                raise
            imported = 0
            try:
                # The SAME descriptor the validation pass just read, rewound -- never
                # a second `open(src_path)`: re-resolving the name here is the exact
                # defect this function is written to avoid.
                f.seek(0)
                out.seek(0, os.SEEK_END)
                if dst_unterminated:
                    out.write(b"\n")
                for record in facade.strict_raw_records(
                    f, src_path, cap=facade._NOTIFICATION_RECORD_CAP
                ):
                    key = facade._notification_key(record, src_path)
                    # A `None` key means the record did not PARSE, so it may be a
                    # framing fragment rather than a record -- nothing it could be a
                    # duplicate OF. Both `existing.add` sites refuse `None`, which is
                    # what keeps it out of this membership test; an unconditional
                    # `key is not None` here would be dead, and a mutation proved it
                    # unobservable. A ts-less row that DOES parse is keyed on its raw
                    # bytes and deduplicates normally; see _notification_key for why
                    # raw and not stripped.
                    if key in existing:
                        continue
                    out.write(record if record.endswith(_TERMINATORS) else record + b"\n")
                    if key is not None:
                        existing.add(key)
                    imported += 1
                # Flush AND close INSIDE this handler so a delayed-writeback
                # failure -- ordinary on a network or fuse filesystem, where the
                # error surfaces at close(2) after a clean flush -- is reported as
                # a stopped merge with the count, not lost at the `with out:`
                # block exit outside this arm. The block's own close then sees an
                # already-closed writer and is a no-op; on an error path before
                # here it still closes the descriptor.
                out.flush()
                out.close()
            except (OSError, UnreadableRecord) as exc:
                # The prefix already appended stays: rolling it back would be a
                # second unvalidated write. Names the count so an operator knows a
                # prefix landed, and identity-less rows in it will re-append on a
                # retry.
                print(
                    f"  ⚠️  Stopped merging {_safe_name(src_path)} after {imported} record(s): {exc}"
                )
                raise
    print(f"  Notifications imported: {imported}")


def _serialise_with_notification_writes(work: Callable[[], None]) -> None:
    """Run *work* in FIFO order with the dashboard's own notification writes.

    ``O_APPEND`` alone is not enough: a row the dashboard appends mid-copy lands BEFORE
    the archive's buffered rows, and ``_load_notifications`` keeps the last
    ``_MAX_PERSISTED_NOTIFICATIONS`` rows by position, so a full 200-record import
    silently pushes the live row out. Notification writes run on ONE worker in
    submission order (``_notification_io_executor``); running the copy there makes a
    queued append land after it, at the end of the file.

    This ACQUIRES the executor (creating it if needed) rather than asking whether one
    exists: a fresh gateway has no pool yet, but a delivery arriving mid-copy creates
    one and races the copy. Only two cases run inline:

    1. Already on the worker: submitting would deadlock on a queue only this call drains.
    2. The dashboard module does not import (the CLI restore): no writer can exist.
       Narrowed to ``ImportError`` so any other import failure surfaces.

    ``result()`` re-raises, so a failing *work* still releases the worker. The
    executor's lazy init is locked in ``dashboard/state_parts/notification_log.py``,
    so two callers cannot build two unordered pools.
    """
    try:
        from kiro_crew.dashboard import state as dashboard_state
    except ImportError:
        work()
        return
    if threading.current_thread().name.startswith("notif-io"):
        work()
        return
    dashboard_state._notification_io_executor().submit(work).result()


class NotificationCopyUnsupported(Exception):
    """This platform cannot install notification records safely, so it does not.

    Deliberately not an ``OSError``: the copy's own error arm catches ``OSError`` and
    re-raises it as a failed import, which is the wrong shape for "this platform was
    never able to do this". A distinct type lets the callers report a SKIP, and an
    unhandled one still aborts rather than passing silently.
    """


def _install_notifications(src_path: Path, dst_path: Path) -> None:
    """Install the snapshot's notification records where the live file does not exist yet.

    The no-live-file sibling of ``_merge_notifications``, with the same posture: ABORT on
    a record the destination's reader cannot decode, never install it.
    ``_load_notifications`` decodes the whole file in one ``try`` that returns ``[]``, so
    one invalid byte hides every row, and the next ``_rewrite_notifications`` (any
    delete, ack or clear) persists that empty view.

    It does not call the merge with an empty destination: the merge opens the
    destination for read first (a missing one would hit its fail-closed arm), and it
    deduplicates, which would drop source records that share a ``ts`` or are identical.

    Every path is resolved to a descriptor EXACTLY ONCE: the source opened
    ``O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_BINARY``, the destination created
    ``O_CREAT|O_EXCL|O_WRONLY|O_APPEND|O_NOFOLLOW|O_BINARY``. Later uses of the paths
    (``_safe_name``, the ``path`` argument to ``strict_raw_records`` and
    ``_notification_key``) only go into messages. Without ``O_NOFOLLOW`` (Windows) a
    by-name reparse check cannot close the check-to-open window, so
    :func:`_copy_notifications` refuses the copy before this is reached.

    The caller's ``is_file()`` checks are advisory: a destination that filled since fails
    ``O_EXCL`` (a dangling symlink included), and a source that became a link or FIFO
    fails ``O_NOFOLLOW`` or the ``S_ISREG`` check on the descriptor. The ancestor chain
    is not pinned, like every other core-file copy here.

    Order:

    1. Read the source ONCE, whole, under ``_NOTIFICATION_SOURCE_CAP``, and never touch
       it again. With the bytes in memory nothing can swap, truncate or extend the
       source between validation and install.
    2. Validate every record. A refusal leaves the destination never created; unlinking
       it afterwards would be unsafe, because the live gateway's sink may already have
       appended a notification.
    3. Create the destination and write the validated bytes.

    Peak memory is about twice the source size (15.9 MiB for the 8.3 MB worst case). A
    source over the cap is refused with its size named, never partly imported. No temp
    file: publishing one through a name lets a same-user process swap in unvalidated
    bytes; an ``O_EXCL`` destination has no intermediate name. ``O_APPEND`` still keeps
    concurrent destination writers from being overwritten.

    Records are written verbatim, except an unterminated final record gains a terminator:
    ``_persist_notification`` appends ``json.dumps(note) + "\\n"``, which would otherwise
    glue onto it. The merge makes the same repair through ``dst_unterminated``.
    """
    facade = _facade()
    # `_notification_key` is called only for the `UndecodableRecord` it raises: reusing
    # the merge's predicate keeps both branches' acceptance criteria identical.
    #
    # `O_NOFOLLOW` refuses a link at the name (e.g. a symlink to `.env` swapped in after
    # extraction), as `_backup_and_copy` skips archive symlinks. `O_NONBLOCK` keeps a
    # name that became a FIFO from hanging the restore. The kind is then judged on the
    # descriptor with `fstat`, never by re-checking the name.
    src_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    # 0o666 so the kernel applies the umask, giving the same mode as the `open(path,
    # "a")` in `_persist_notification`: a restored file must not be tighter than one
    # the product wrote itself.
    #
    # `O_APPEND` because the dashboard's sink appends to this file too: a buffered write
    # at this handle's stale offset would overwrite a notification delivered mid-copy.
    #
    # `O_BINARY` on both, as the repo requires for `os.open` on Windows; absent on POSIX.
    # `test_a_clean_source_is_copied_record_for_record` checks `\\r\\n` survives.
    dst_flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    written = 0
    opened_dst = False
    try:
        # A platform without `O_NOFOLLOW` never gets here (`_copy_notifications` refuses
        # it), so the open itself decides the refusal. No `lstat`-then-`fstat` identity
        # check: it would only narrow the window.
        with os.fdopen(os.open(src_path, src_flags), "rb") as src:
            if not _stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                raise OSError("source is not a regular file")
            # One chunked read: `read(cap + 1)` would preallocate a cap-sized buffer,
            # so 1 MiB chunks keep the peak proportional to the source. The cap is
            # checked on bytes already read (`st_size` can be stale), inside the loop.
            acc = bytearray()
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                if len(acc) + len(chunk) > facade._NOTIFICATION_SOURCE_CAP:
                    raise OSError(
                        f"notification source is at least "
                        f"{len(acc) + len(chunk)} bytes, over the "
                        f"{facade._NOTIFICATION_SOURCE_CAP} byte limit -- refusing rather "
                        "than importing part of it"
                    )
                acc.extend(chunk)
            blob = bytes(acc)
        # Both loops pass over the same immutable bytes, so nothing can change the
        # source between validation and write. Framing uses `strict_raw_records`, the
        # merge branch's splitter, so both branches agree on record boundaries.
        with io.BytesIO(blob) as buf:
            for record in facade.strict_raw_records(
                buf, src_path, cap=facade._NOTIFICATION_RECORD_CAP
            ):
                facade._notification_key(record, src_path)
        with os.fdopen(os.open(dst_path, dst_flags, 0o666), "wb") as out:
            opened_dst = True
            with io.BytesIO(blob) as buf:
                for record in facade.strict_raw_records(
                    buf, src_path, cap=facade._NOTIFICATION_RECORD_CAP
                ):
                    out.write(record if record.endswith(_TERMINATORS) else record + b"\n")
                    written += 1
    except (OSError, UnreadableRecord) as exc:
        # Either nothing was written, or a validated prefix STAYS: unlinking could take
        # a concurrent writer's rows, so the count is named instead.
        #
        # `_safe_name` on the path: an archive-derived path can carry ANSI controls. The
        # exception needs none; both caught types render paths with repr escaping.
        tail = f"{written} imported" if opened_dst else "notifications not imported"
        print(f"  ⚠️  Could not copy {_safe_name(src_path)}: {exc} — {tail}")
        raise


def _copy_locked(src: Path, dst: Path) -> bool:
    """Copy *src* onto a missing *dst*, owner-only before the name is published.

    Merge restore only copies when *dst* is absent. Publish with ``os.link``
    (the create-only shape ``_get_telemetry_salt`` uses) so a dest that
    appears in the window — ``--force`` restore racing a live gateway
    creating ``telemetry_salt`` — raises ``FileExistsError`` and the live
    file is left alone. ``os.replace`` / ``atomic_write`` would clobber it.
    The temp is locked down before any payload is written. Failures to
    publish (unsupported hard links, a restrict error, a size mismatch)
    skip this file without raising, because merge restore has already
    applied earlier components and ``_get_telemetry_salt`` regenerates a
    missing salt. Return True only when this call published *dst*.
    """
    if dst.exists():
        return False
    if src.name == "telemetry_salt" or dst.name == "telemetry_salt":
        size = src.stat().st_size
        if size != _TELEMETRY_SALT_BYTES:
            return False
    payload = src.read_bytes()
    if dst.exists():
        return False
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    tmp_path = Path(tmp)
    try:
        platform_compat.restrict_to_owner(str(tmp_path))
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(f"short write restoring {src.name}")
            view = view[written:]
        # Drop the fd from finally before close: a close-time writeback
        # error must not be retried on an already-released descriptor,
        # because a second OSError in finally would replace the skip and
        # abort merge after earlier components were applied.
        pending = fd
        fd = -1
        os.close(pending)
        os.link(str(tmp_path), str(dst))
        return True
    except OSError:
        # FileExistsError: live dest won. EXDEV / EPERM / no-hardlink /
        # restrict / short write / close: dest stays missing and
        # `_get_telemetry_salt` regenerates. Raising here aborts merge
        # after earlier components were already applied.
        return False
    finally:
        if fd >= 0:
            pending = fd
            fd = -1
            try:
                os.close(pending)
            except OSError:
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _merge_named_stores(
    src_root: Path, dst_root: Path, *, allow_unpinned: bool = False
) -> list[str]:
    """Install absent stores whole and return the names kept whole at the destination.

    A manifest and its databases belong to one generation: filling missing files in an
    existing store can combine a V2 manifest with a V1 database that cannot open as V2.
    """
    facade = _facade()
    with memory_store_namespace_lock(dst_root):
        platform_compat.make_owner_only_dir(dst_root)
        kept: list[str] = []
        for src in sorted(src_root.iterdir()):
            if is_host_local_store_state((MEMORY_STORES_DIR_NAME, src.name)) or not src.is_dir():
                continue
            dst = dst_root / src.name
            if dst.exists():
                kept.append(src.name)
                continue
            facade._copytree_safe(
                src,
                dst,
                allow_unpinned=allow_unpinned,
                must_create=True,
                on_skip=pinned_fs.fatal_skip_reporter(f"merge of store {src.name!r}"),
            )
            platform_compat.make_owner_only_dir(dst)
        return kept


def _report_unmerged_databases(src_tree: Path, dst_tree: Path, tree: str) -> None:
    """Say when merge is about to KEEP a product database rather than merge it.

    Merge copies trees without overwriting, which keeps a newer local markdown file but
    silently drops a database's incoming rows. A real merge of `knowledge.db` (FTS5 index,
    foreign keys across `sources`, `items`, `mentions`, `source_locations`) needs key
    remapping and an identity rule, like `_merge_memory`'s per-table merge; none exists
    yet, so the kept database is named instead of reporting silent success.

    Named stores are handled separately by `_merge_named_stores`, which keeps an
    existing store whole rather than copying missing files into its generation.
    """
    prefix = f"{tree}/"
    for src in sorted(src_tree.rglob("*")):
        if not src.is_file():
            continue
        leaf = src.relative_to(src_tree).as_posix()
        rel = prefix + leaf
        if not is_product_tree_database(rel) or not (dst_tree / leaf).is_file():
            continue
        print(
            f"  ⚠️  {_safe_name(rel)}: kept the existing database; the bundle's copy was NOT "
            "merged into it.\n"
            "      Merge mode does not combine this database's rows. To take the "
            "bundle's copy instead, use --mode replace."
        )

"""Complete private-member snapshots and startup-only recoverable restore.

Global V1 does not call this module. A restore request stages a validated tree;
only the gateway startup barrier may activate it before opening any memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from kiro_crew import memory_stores, platform_compat
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.atomic_write import atomic_write, replace_with_retry

logger = logging.getLogger(__name__)

BUNDLE_FORMAT = "kirocrew-member-memory"
BUNDLE_VERSION = 2
MANIFEST = "snapshot-manifest.json"
PENDING = "pending-restore.json"
MAX_BUNDLE_BYTES = 1024 * 1024 * 1024
MAX_BUNDLE_FILES = 8192
MAX_MANIFEST_BYTES = 1024 * 1024
_HISTORY = re.compile(r"memory/history/\d{4}-\d{2}-\d{2}\.md\Z")
_STAGE_NAME = re.compile(r"restore-[0-9a-f]{32}\Z")
_ASIDE_NAME = re.compile(r"superseded-[0-9a-f]{32}\Z")
_FILES = frozenset({"memory.db", "memory/preferences.md", "memory/projects.md", "lessons.jsonl"})
_STORE_USE_LOCK = ".store-use.lock"


def snapshot_time(backup: Path) -> datetime:
    """Read both current collision-safe and original second-resolution V2 names."""
    stamp = backup.stem.rsplit(".", 1)[-1].split("-", 1)[0]
    if not re.fullmatch(r"\d{8}T(?:\d{6}|\d{12})Z", stamp):
        raise ValueError("Invalid member backup timestamp")
    pattern = "%Y%m%dT%H%M%SZ" if len(stamp) == 16 else "%Y%m%dT%H%M%S%fZ"
    return datetime.strptime(stamp, pattern).replace(tzinfo=timezone.utc)


def is_member_store(db_path: Path) -> bool:
    """Backup/restore classification, including a lost directory with an owned config.

    This does not authorize ordinary memory use. The recovery path separately
    validates exclusive configuration ownership and the snapshot manifest.
    """
    name = memory_stores.named_store_of_db(db_path)
    if not name:
        return False
    if memory_stores.memory_store_version(name) == 2:
        return True
    from kiro_crew.config.loader import KiroCrewConfig

    record = KiroCrewConfig.load().memory_stores.get(name)
    return bool(
        record and getattr(record, "memory_version", 1) == 2 and getattr(record, "owner_member", "")
    )


def backup_directory(db_path: Path) -> Path:
    """Outside the swappable store tree, inside the same protected home leaf."""
    root = memory_stores.memory_stores_root().resolve()
    name = db_path.parent.name
    memory_stores.validate_memory_store_name(name)
    target = root / ".member-backups" / name
    if target.resolve() != target:
        raise ValueError("Member backup directory is redirected")
    return target


def _open_store_use_lock(db_path: Path) -> int:
    """Open the stable owner-only lock outside the directory restore swaps."""
    out = backup_directory(db_path)
    platform_compat.make_owner_only_dir(out)
    lock_path = out / _STORE_USE_LOCK
    if lock_path.resolve() != lock_path.absolute():
        raise ValueError("Member memory use lock is redirected")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Member memory use lock is not a private regular file")
        platform_compat.restrict_to_owner(lock_path)
        return fd
    except BaseException:
        os.close(fd)
        raise


def acquire_store_use_lock(db_path: Path) -> int | None:
    """Hold one V2 store generation open on POSIX until its SQLite close.

    POSIX permits renaming an open SQLite database, so every V2
    :class:`VectorMemoryStore` holds this shared lock for its connection's
    lifetime. Windows SQLite handles already deny renaming their containing
    directory; taking the existing exclusive fallback there would serialize all
    readers and writers in the process, so the native sharing denial remains the
    lifetime guard on that platform.
    """
    if not platform_compat.IS_POSIX:
        return None
    fd = _open_store_use_lock(db_path)
    try:
        platform_compat.acquire_lock(fd, exclusive=False)
        return fd
    except BaseException:
        os.close(fd)
        raise


def release_store_use_lock(fd: int | None) -> None:
    """Release a lifetime lock returned by :func:`acquire_store_use_lock`."""
    if fd is None:
        return
    try:
        platform_compat.release_lock(fd)
    finally:
        os.close(fd)


@contextmanager
def _restore_store_admission(db_path: Path) -> Iterator[None]:
    """Refuse a restore while another process can still use the old tree."""
    fd = _open_store_use_lock(db_path)
    try:
        try:
            with platform_compat.file_lock(fd, exclusive=True, required=True, wait=False):
                yield
        except BlockingIOError as exc:
            raise ValueError(
                "Member memory is still open in another process; stop it before restarting "
                "to apply this restore"
            ) from exc
    finally:
        os.close(fd)


def _identity(db_path: Path, *, allow_missing: bool = False) -> tuple[str, str]:
    from kiro_crew.config.loader import KiroCrewConfig

    name = db_path.parent.name
    memory_stores.validate_memory_store_name(name)
    root = memory_stores.memory_stores_root().resolve()
    if db_path.parent != root / name or db_path.parent.resolve() != root / name:
        raise ValueError("Member memory directory is redirected")
    config = KiroCrewConfig.load()
    record = config.memory_stores.get(name)
    owner = getattr(record, "owner_member", "")
    if not owner or getattr(record, "memory_version", 1) != 2:
        raise ValueError("Memory has no private V2 owner")
    memory_stores.require_member_memory_not_archived(name, expected_owner=owner)
    if [key for key, value in config.agents.items() if value.memory_store == name] != [owner]:
        raise ValueError("Memory is not exclusively bound to its owner")
    marker = db_path.parent / memory_stores.MEMBER_MEMORY_MANIFEST
    if marker.exists() or not allow_missing or db_path.parent.exists():
        value = _read_json(marker)
        if value.get("owner_member") != owner or value.get("memory_version") != 2:
            raise ValueError("Member memory ownership does not match its configuration")
    return name, owner


def _read_json(path: Path) -> dict:
    if (
        path.resolve() != path.absolute()
        or not path.is_file()
        or path.stat().st_size > MAX_MANIFEST_BYTES
    ):
        raise ValueError("Invalid member snapshot metadata")
    with path.open("rb") as handle:
        value = json.loads(handle.read(MAX_MANIFEST_BYTES + 1))
    if not isinstance(value, dict):
        raise ValueError("Invalid member snapshot metadata")
    return value


def _allowed(name: str) -> bool:
    return name in _FILES or _HISTORY.fullmatch(name) is not None


def _discard_unpublished_stage(stage: Path, out: Path) -> None:
    """Clean only this verified temporary tree, never a current/prior member."""
    if (
        stage.resolve() != stage.absolute()
        or stage.parent.resolve() != out.resolve()
        or not _STAGE_NAME.fullmatch(stage.name)
        or (out / PENDING).exists()
    ):
        return
    try:
        shutil.rmtree(stage)
    except OSError:
        logger.warning("Unable to remove an unpublished member restore stage", exc_info=True)


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _check_database(path: Path, name: str, owner: str) -> None:
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Member snapshot database failed integrity checking")
        if db.execute(
            "SELECT type FROM sqlite_master WHERE name='memory_items'"  # wokeignore:rule=master
        ).fetchone() != ("table",):
            raise ValueError("Member snapshot does not contain a V2 database")
        row = db.execute("SELECT value FROM memory_meta WHERE key='store_name'").fetchone()
        if row != (name,):
            raise ValueError("Member snapshot database belongs to a different store")
        from kiro_crew import memory_schema

        identity = dict(db.execute("SELECT key, value FROM memory_meta"))
        version = identity.get(memory_schema.PRIVATE_MEMORY_VERSION_META_KEY)
        recorded_owner = identity.get(memory_schema.OWNER_MEMBER_META_KEY)
        # Older member snapshots predate the durable marker. Their outer
        # bundle/store identity still authorizes restore, and init stamps them
        # after activation. A present marker must satisfy init's same contract
        # before a healthy current directory can be displaced.
        if (version is not None and (version != "2" or recorded_owner != owner)) or (
            recorded_owner is not None and recorded_owner != owner
        ):
            raise ValueError("Member snapshot private database ownership is invalid")


def _manifest_valid(manifest: dict, name: str, owner: str) -> dict[str, str]:
    if (
        manifest.get("format") != BUNDLE_FORMAT
        or manifest.get("version") != BUNDLE_VERSION
        or manifest.get("store") != name
        or manifest.get("owner_member") != owner
    ):
        raise ValueError("Snapshot belongs to another member or uses an unsupported format")
    files = manifest.get("files")
    if not isinstance(files, dict) or "memory.db" not in files or len(files) > MAX_BUNDLE_FILES:
        raise ValueError("Snapshot file inventory is invalid")
    for filename, digest in files.items():
        if (
            not isinstance(filename, str)
            or not _allowed(filename)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("Snapshot file inventory contains an unsafe path or checksum")
    return files


def backup_store(db_path: Path, *, now: datetime | None = None) -> Path:
    from kiro_crew.memory_startup import require_memory_ready

    require_memory_ready(memory_stores.named_store_of_db(db_path))
    name, owner = _identity(db_path)
    out = backup_directory(db_path)
    platform_compat.make_owner_only_dir(out)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    target = out / f"memory.{stamp}-{uuid4().hex}.zip"
    partial = out / f".{target.name}.{uuid4().hex}.partial"
    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=out) as temporary:
        stage = Path(temporary)
        if stage.resolve().parent != out.resolve():
            raise ValueError("Snapshot staging directory escaped its parent")
        copied_db = stage / "memory.db"
        with (
            closing(sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True)) as src,
            closing(sqlite3.connect(str(copied_db))) as dst,
        ):
            src.backup(dst)
        _check_database(copied_db, name, owner)
        files = {"memory.db": copied_db}
        home = db_path.parent.resolve()
        for relative in sorted(_FILES - {"memory.db"}):
            candidate = home / relative
            if candidate.exists():
                files[relative] = candidate
        history = home / "memory" / "history"
        if history.exists():
            if history.resolve() != history:
                raise ValueError("Member history directory is redirected")
            for candidate in history.glob("*.md"):
                relative = candidate.relative_to(home).as_posix()
                if _allowed(relative):
                    files[relative] = candidate
        if len(files) > MAX_BUNDLE_FILES:
            raise ValueError("Member snapshot has too many files")
        hashes = {}
        total = 0
        try:
            with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
                for relative, path in files.items():
                    if path.resolve() != path or not path.is_file():
                        raise ValueError("Member snapshot source is redirected or unreadable")
                    if total + path.stat().st_size > MAX_BUNDLE_BYTES:
                        raise ValueError("Member snapshot exceeds its size limit")
                    # Read a source once: the hash and archive must describe the
                    # same bytes even if a Markdown writer replaces its file.
                    digest = hashlib.sha256()
                    with path.open("rb") as source, bundle.open(relative, "w") as dest:
                        while chunk := source.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_BUNDLE_BYTES:
                                raise ValueError("Member snapshot exceeds its size limit")
                            digest.update(chunk)
                            dest.write(chunk)
                    hashes[relative] = digest.hexdigest()
                manifest = {
                    "format": BUNDLE_FORMAT,
                    "version": BUNDLE_VERSION,
                    "store": name,
                    "owner_member": owner,
                    "created_at": stamp,
                    "files": hashes,
                }
                bundle.writestr(MANIFEST, json.dumps(manifest, sort_keys=True))
            platform_compat.restrict_to_owner(partial)
            replace_with_retry(partial, target)
        finally:
            partial.unlink(missing_ok=True)
    return target


def stage_restore(backup: Path, db_path: Path) -> Path:
    """Validate and stage; never touch live member data or activate a restore."""
    from kiro_crew.memory_startup import require_memory_ready

    require_memory_ready(memory_stores.named_store_of_db(db_path), allow_failed=True)
    name, owner = _identity(db_path, allow_missing=True)
    out = backup_directory(db_path)
    platform_compat.make_owner_only_dir(out)
    with (out / ".restore.lock").open("a+b") as lock:
        with platform_compat.file_lock(lock.fileno(), required=True, wait=False):
            if (out / PENDING).exists():
                raise ValueError("A member restore is already pending gateway restart")
            stage_name = "restore-" + uuid4().hex
            stage = out / stage_name
            stage.mkdir(mode=0o700)
            platform_compat.restrict_dir_to_owner(stage)
            try:
                try:
                    with zipfile.ZipFile(backup) as bundle:
                        infos = bundle.infolist()
                        if any(info.orig_filename != info.filename for info in infos):
                            raise ValueError("Snapshot contains an unsafe path spelling")
                        names = [info.filename for info in infos]
                        if len(names) != len(set(names)) or len(names) > MAX_BUNDLE_FILES + 1:
                            raise ValueError("Snapshot has duplicate entries or too many files")
                        if (
                            MANIFEST not in names
                            or bundle.getinfo(MANIFEST).file_size > MAX_MANIFEST_BYTES
                        ):
                            raise ValueError("Snapshot manifest is missing or oversized")
                        manifest = json.loads(bundle.read(MANIFEST))
                        if not isinstance(manifest, dict):
                            raise ValueError("Snapshot manifest is invalid")
                        files = _manifest_valid(manifest, name, owner)
                        if (
                            set(names) != set(files) | {MANIFEST}
                            or sum(info.file_size for info in infos) > MAX_BUNDLE_BYTES
                        ):
                            raise ValueError("Snapshot inventory or uncompressed size is invalid")
                        for info in infos:
                            if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                                raise ValueError(
                                    "Snapshot links and directory entries are not allowed"
                                )
                        for relative, checksum in files.items():
                            path = stage / relative
                            path.parent.mkdir(parents=True, exist_ok=True)
                            if path.resolve() != path:
                                raise ValueError("Snapshot extraction path is redirected")
                            digest = hashlib.sha256()
                            with bundle.open(relative) as source, path.open("xb") as dest:
                                while chunk := source.read(1024 * 1024):
                                    digest.update(chunk)
                                    dest.write(chunk)
                            if digest.hexdigest() != checksum:
                                raise ValueError("Snapshot file checksum does not match")
                except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
                    raise ValueError("Member snapshot is invalid") from exc
                _check_database(stage / "memory.db", name, owner)
                (stage / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
                (stage / memory_stores.MEMBER_MEMORY_MANIFEST).write_text(
                    json.dumps({"owner_member": owner, "memory_version": 2}), encoding="utf-8"
                )
                journal = {
                    "store": name,
                    "owner_member": owner,
                    "stage": stage_name,
                    "aside": "superseded-" + uuid4().hex,
                    "prior_existed": db_path.parent.exists(),
                    "backup_name": backup.name,
                    "staged_at": datetime.now(timezone.utc).isoformat(),
                }
                atomic_write(out / PENDING, json.dumps(journal), restrict_to_owner=True)
            except BaseException:
                _discard_unpublished_stage(stage, out)
                raise
    return db_path


def _pending_journal(db_path: Path, *, validate_owner: bool = True) -> tuple[Path, dict | None]:
    """Read pending authority without opening the live database."""
    out = backup_directory(db_path)
    if not (out / PENDING).exists():
        return out, None
    journal = _read_json(out / PENDING)
    name = db_path.parent.name
    if db_path.parent != memory_stores.memory_stores_root().resolve() / name:
        raise ValueError("Pending restore directory is not the canonical member store")
    if journal.get("store") != name:
        raise ValueError("Pending restore ownership no longer matches")
    if validate_owner:
        _, owner = _identity(db_path, allow_missing=True)
        if journal.get("owner_member") != owner:
            raise ValueError("Pending restore ownership no longer matches")
    for field, pattern in (("stage", _STAGE_NAME), ("aside", _ASIDE_NAME)):
        value = journal.get(field)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError("Pending restore paths are invalid")
        path = out / value
        if path.resolve() != path:
            raise ValueError("Pending restore path is redirected")
    return out, journal


def pending_restore_status(db_path: Path) -> dict:
    """Persisted owner-facing state, containing no private filesystem paths."""
    from kiro_crew.memory_startup import memory_restore_startup_status

    activation = memory_restore_startup_status(memory_stores.named_store_of_db(db_path))
    try:
        _, journal = _pending_journal(db_path)
    except (ValueError, OSError, sqlite3.Error) as exc:
        return {
            "pending": True,
            "restart_required": True,
            "pending_restore": None,
            "restore_error": str(exc),
            "recovery": _recovery_details(backup_directory(db_path)),
            **activation,
        }
    details = None
    if journal is not None:
        # Older staged journals remain activatable; absent display fields do
        # not invent a backup identity or timestamp.
        raw = journal.get("backup_name")
        name = raw if isinstance(raw, str) and raw == Path(raw).name and "\\" not in raw else ""
        details = {"backup_name": name, "staged_at": str(journal.get("staged_at", ""))}
    return {
        "pending": journal is not None,
        "restart_required": journal is not None,
        "pending_restore": details,
        **activation,
    }


def _recovery_details(out: Path) -> dict:
    """Bounded names, never paths or a malformed journal's claimed targets."""
    stages: list[str] = []
    previous: list[str] = []
    try:
        for item in out.iterdir():
            if _STAGE_NAME.fullmatch(item.name) and len(stages) < 8:
                stages.append(item.name)
            elif _ASIDE_NAME.fullmatch(item.name) and len(previous) < 8:
                previous.append(item.name)
    except OSError:
        # Status must retain the original recovery reason even when the
        # surrounding directory also cannot be enumerated.
        pass
    return {
        "journal": PENDING,
        "staged_copies": sorted(stages),
        "previous_copies": sorted(previous),
        "instruction": (
            "Cancel only while the current store is intact and activation has not started. "
            "If cancellation is refused, stop the gateway, preserve the journal and listed copies, "
            "and recover a complete validated store before repairing the journal and restarting."
        ),
    }


def _quarantine_invalid_pending(db_path: Path, out: Path) -> bool:
    """Revoke unreadable intent only with independent proof of intact live data."""
    pending = out / PENDING
    if pending.resolve() != pending or not pending.is_file():
        raise ValueError(
            "Pending restore journal is redirected or unreadable; cancellation refused"
        )
    if any(_ASIDE_NAME.fullmatch(item.name) for item in out.iterdir()):
        raise ValueError(
            "Cannot cancel an unreadable pending-restore.json while superseded copies exist. "
            "Preserve the journal and copies listed in restore status, stop the gateway, "
            "and recover a complete validated store before repairing the journal."
        )
    name, owner = _identity(db_path)
    try:
        _check_database(db_path, name, owner)
    except sqlite3.Error as exc:
        raise ValueError(
            "Current member memory is absent or unreadable; preserve the pending journal and copies"
        ) from exc
    # The untrusted journal's stage/aside values are never followed. Leave all
    # staged bytes intact and preserve the original journal for diagnosis.
    replace_with_retry(pending, out / ("cancelled-restore-" + uuid4().hex + ".json"))
    return True


def cancel_pending_restore(db_path: Path) -> bool:
    """Cancel a not-yet-activated owned stage; preserve live data and backup.

    The startup barrier shares this lock. Once activation has moved a tree,
    cancellation refuses so the journal remains available for crash recovery.
    """
    out = backup_directory(db_path)
    if not (out / PENDING).exists():
        return False
    with (out / ".restore.lock").open("a+b") as lock:
        with platform_compat.file_lock(lock.fileno(), required=True, wait=False):
            try:
                out, journal = _pending_journal(db_path, validate_owner=False)
            except (ValueError, OSError, sqlite3.Error):
                return _quarantine_invalid_pending(db_path, out)
            if journal is None:
                return False
            # Configuration need not bind the member, but a surviving
            # identity file still prevents cancelling another owner's intent.
            marker = db_path.parent / memory_stores.MEMBER_MEMORY_MANIFEST
            if marker.exists():
                identity = _read_json(marker)
                if identity.get("owner_member") != journal.get("owner_member"):
                    raise ValueError("Pending restore ownership no longer matches")
            stage, aside = out / journal["stage"], out / journal["aside"]
            if aside.exists() or not stage.is_dir():
                raise ValueError(
                    "Restore activation has started; restart the gateway to finish recovery before cancelling"
                )
            # Revoking the activation journal is the atomic cancellation point.
            # A crash during cleanup can leave an inert protected temporary
            # tree, never an activatable half-deleted snapshot.
            (out / PENDING).unlink()
            _discard_unpublished_stage(stage, out)
            return True


def apply_pending_restore(db_path: Path) -> str | None:
    """Startup barrier only. Keep the prior tree and recover interrupted renames."""
    out = backup_directory(db_path)
    pending = out / PENDING
    if not pending.exists():
        return None
    with (out / ".restore.lock").open("a+b") as lock:
        with (
            platform_compat.file_lock(lock.fileno(), required=True, wait=False),
            _restore_store_admission(db_path),
        ):
            if not pending.exists():
                return None
            name, owner = _identity(db_path, allow_missing=True)
            journal = _read_json(pending)
            if journal.get("store") != name or journal.get("owner_member") != owner:
                raise ValueError("Pending restore ownership no longer matches")
            stage_name, aside_name = journal.get("stage", ""), journal.get("aside", "")
            if (
                not isinstance(stage_name, str)
                or not _STAGE_NAME.fullmatch(stage_name)
                or not isinstance(aside_name, str)
                or not _ASIDE_NAME.fullmatch(aside_name)
            ):
                raise ValueError("Pending restore paths are invalid")
            stage, aside, target = out / stage_name, out / aside_name, db_path.parent
            prior_existed = journal.get("prior_existed", True)
            if not isinstance(prior_existed, bool):
                raise ValueError("Pending restore prior-directory state is invalid")
            if stage.resolve() != stage or aside.resolve() != aside:
                raise ValueError("Pending restore path is redirected")
            # Crash after the second rename but before journal removal: the
            # staged manifest inside target proves that activation completed.
            if not stage.exists() and target.exists() and (aside.exists() or not prior_existed):
                manifest = _read_json(target / MANIFEST)
                _manifest_valid(manifest, name, owner)
                pending.unlink()
                return aside.name if prior_existed else ""
            manifest = _read_json(stage / MANIFEST)
            files = _manifest_valid(manifest, name, owner)
            for relative, checksum in files.items():
                path = stage / relative
                if path.resolve() != path or not path.is_file() or _digest(path) != checksum:
                    raise ValueError("Staged restore content changed; activation refused")
            _check_database(stage / "memory.db", name, owner)
            if target.exists():
                if not prior_existed:
                    raise ValueError(
                        "Member memory appeared after the restore was staged; activation refused"
                    )
                if aside.exists():
                    raise ValueError("Restore has conflicting current and displaced trees")
                replace_with_retry(target, aside)
            elif prior_existed and not aside.exists():
                raise ValueError("Restore lost its prior member directory")
            elif not prior_existed and aside.exists():
                raise ValueError("Restore has an unexpected displaced member directory")
            try:
                replace_with_retry(stage, target)
            except BaseException:
                if not target.exists() and aside.exists():
                    replace_with_retry(aside, target)
                raise
            pending.unlink()
            return aside.name if prior_existed else ""

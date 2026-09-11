"""Rotating hot backups of every memory store.

The gap this closes is not subtle. Memory is the one thing here that cannot be
rebuilt from anywhere else — config can be retyped and sessions replayed, but a
superseded preference nobody remembers stating is gone — and until now its whole
durability story was a manual ``kirocrew snapshot``. An operator who never ran it
had no copy, which is exactly how a 36 MB store became 29 bytes with nothing to
restore from.

Deliberately NOT the snapshot machinery. That is a tar of many components with
redaction, upload paths and a purpose model, aimed at moving an install somewhere
else; this is one cheap file copy aimed at surviving the next corruption. Sharing
code would drag redaction and component resolution onto a path that must be able to
run unattended every day and finish in milliseconds.

Uses SQLite's ONLINE BACKUP API (``Connection.backup``), not a file copy. That is
the difference between a backup and a coin flip: the gateway holds the store open
under WAL, so ``shutil.copy`` of ``memory.db`` alone captures a file whose committed
tail lives in a ``-wal`` sibling it did not take, and the result parses cleanly while
missing recent writes. The backup API walks a consistent snapshot with the writer
still running, and produces a single self-contained file with no WAL to pair.

Backups live beside the store they came from, inside ``memory_stores/<name>/`` for a
silo, so a store's backups inherit the fence that store already sits behind and no
new sensitive-path entry is needed. The default store's go under the data home in
their own directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from uuid import uuid4

from kiro_crew import platform_compat
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.atomic_write import atomic_write, replace_with_retry
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    MEMORY_DB_FILE,
    active_store_names,
    named_store_of_db,
    owned_store_path,
    resolve_store_path,
)

logger = logging.getLogger(__name__)

#: Directory name holding a store's backups, created beside that store's own file.
BACKUP_DIR_NAME = "backups"

#: Original ``<stem>.<UTC timestamp>.db`` names remain readable for restore and
#: retention. Current names add microseconds and a UUID so two accepted manual or
#: concurrent backups cannot resolve to the same recovery point.
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_CURRENT_STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"

#: How many backups of one store to keep. Bounded because this runs unattended: a
#: 36 MB store at one copy a day is ~1 GB a month with no ceiling.
DEFAULT_KEEP = 7

#: Shortest gap between two backups of one store. The heartbeat's tick counter is
#: PER PROCESS and resets on every gateway start, so without this the pass runs once
#: per restart: a gateway restarted five times a day writes five copies and the
#: DEFAULT_KEEP window collapses from seven days to a day and a half. The retention
#: window IS the feature, so a restart must not be able to shrink it.
#:
#: An interval rather than a same-calendar-day test: a boot at 23:50 reaches the
#: offset tick after midnight, and a day compare would take a second copy that night.
MIN_BACKUP_INTERVAL_HOURS = 20

_V1_PENDING = "pending-v1-restore.json"
_V1_SIDECARS = ("", "-wal", "-shm")


class MemoryBackupFailed(RuntimeError):
    """A store's copy was attempted and did not succeed.

    Distinct from "there was nothing to copy", which :func:`backup_store` reports as
    ``None``. The two need OPPOSITE handling — one is routine, the other means
    durability has stopped for that store — and collapsing them is how a store that
    fails its copy every single day gets counted as "skipped" and logged as nothing.
    """


def backup_dir_for(db_path: Path) -> Path:
    """Where *db_path*'s backups live. Does not create anything."""
    from kiro_crew import member_memory_backup

    if member_memory_backup.is_member_store(db_path):
        return member_memory_backup.backup_directory(db_path)
    return db_path.parent / BACKUP_DIR_NAME


def _stamp(now: datetime | None = None) -> str:
    taken = now or datetime.now(timezone.utc)
    return f"{taken.strftime(_CURRENT_STAMP_FORMAT)}-{uuid4().hex}"


def snapshot_time(backup: Path) -> datetime:
    """Read current collision-safe and original second-resolution V1 names."""
    token = backup.stem.rsplit(".", 1)[-1]
    if re.fullmatch(r"\d{8}T\d{6}Z", token):
        stamp = token
    else:
        current = re.fullmatch(r"(\d{8}T\d{12}Z)-[0-9a-f]{32}", token)
        if current is None:
            raise ValueError("Invalid V1 backup timestamp")
        stamp = current.group(1)
    pattern = _STAMP_FORMAT if len(stamp) == 16 else _CURRENT_STAMP_FORMAT
    return datetime.strptime(stamp, pattern).replace(tzinfo=timezone.utc)


def _read_only_uri(path: Path) -> str:
    """A read-only SQLite URI for *path*, percent-escaped.

    ``as_uri()`` rather than interpolating the path into ``file:...``: a filename
    containing ``?`` or ``#`` is otherwise parsed as the start of the URI's query or
    fragment, truncating the path so the connection opens a DIFFERENT database. Store
    names are shape-validated, but ``KIROCREW_HOME`` is operator-chosen, so the
    truncation is reachable through the data home. One helper because two spellings of
    one URI is how they diverge.
    """
    return f"{path.absolute().as_uri()}?mode=ro"


def backup_store(db_path: Path, *, now: datetime | None = None) -> Path | None:
    """Copy *db_path* consistently into its backup directory. ``None`` if there is nothing.

    Three outcomes, not two: a ``Path`` on success, ``None`` when there was nothing to
    copy (a store never opened, an empty file — routine on a timer), and
    :class:`MemoryBackupFailed` when a copy was attempted and did not land. The caller
    counts the third; folding it into ``None`` is what made its failure counter
    unreachable and left a permanently failing store logging nothing at all.

    The copy is written to a ``.partial`` name and RENAMED, so an interrupted run
    cannot leave a truncated file that looks like a backup. Rename is atomic within a
    directory, which is why the temporary sits beside the target rather than in a temp
    root on another filesystem.
    """
    from kiro_crew import member_memory_backup
    from kiro_crew.memory_startup import require_memory_ready

    require_memory_ready(named_store_of_db(db_path))
    if member_memory_backup.is_member_store(db_path):
        try:
            return member_memory_backup.backup_store(db_path, now=now)
        except Exception as exc:
            raise MemoryBackupFailed(f"private member backup failed: {exc}") from exc
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return None

    out_dir = backup_dir_for(db_path)
    # Owner-only, and created before the first child so the Windows grants carry
    # (OI)(CI) and files landing inside inherit them rather than the default DACL.
    platform_compat.make_owner_only_dir(out_dir)

    target = out_dir / f"{db_path.stem}.{_stamp(now)}.db"
    partial = out_dir / f".{target.name}.{uuid4().hex}.partial"
    src: sqlite3.Connection | None = None
    dst: sqlite3.Connection | None = None
    try:
        # Read-only URI so a backup can never be the thing that writes to the store.
        src = sqlite3.connect(_read_only_uri(db_path), uri=True)
        dst = sqlite3.connect(str(partial))
        src.backup(dst)
        dst.close()
        dst = None
        # Verify the STAGED copy before it becomes a backup. Without this an unsound
        # copy is renamed into place, `prune_backups` counts it as the newest, and a
        # real one is deleted to make room -- so the operator loses their last
        # restorable copy at the moment they reach for it. The restore path already
        # checks; checking only there is checking at the wrong end.
        with closing(sqlite3.connect(_read_only_uri(partial), uri=True)) as probe:
            if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MemoryBackupFailed(f"the staged copy of {db_path} failed its integrity check")
        platform_compat.restrict_to_owner(partial)
        replace_with_retry(partial, target)
        return target
    except MemoryBackupFailed:
        raise
    except Exception as exc:
        raise MemoryBackupFailed(f"memory backup of {db_path} failed: {exc}") from exc
    finally:
        for conn in (dst, src):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.debug("closing a backup connection failed", exc_info=True)
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            logger.debug("removing a partial backup failed", exc_info=True)


def list_backups(db_path: Path) -> list[Path]:
    """*db_path*'s backups, NEWEST FIRST.

    Sorted by the timestamp in the NAME rather than mtime, because a copied or restored
    file carries whatever mtime the copy gave it while its name still says when its
    contents were taken.
    """
    out_dir = backup_dir_for(db_path)
    if not out_dir.is_dir():
        return []
    from kiro_crew import member_memory_backup

    if member_memory_backup.is_member_store(db_path):

        def stamp_key(path: Path) -> tuple[float, str]:
            try:
                return member_memory_backup.snapshot_time(path).timestamp(), path.name
            except ValueError:
                return 0.0, path.name

        return sorted(out_dir.glob(f"{db_path.stem}.*.zip"), key=stamp_key, reverse=True)

    def v1_stamp_key(path: Path) -> tuple[float, str]:
        try:
            return snapshot_time(path).timestamp(), path.name
        except ValueError:
            # An unfamiliar name sorts first, so the freshness guard takes a new
            # backup instead of trusting it.
            return float("inf"), path.name

    return sorted(out_dir.glob(f"{db_path.stem}.*.db"), key=v1_stamp_key, reverse=True)


def prune_backups(db_path: Path, keep: int = DEFAULT_KEEP) -> int:
    """Delete all but the *keep* newest backups of *db_path*. Returns how many went.

    ``keep`` below 1 is treated as 1. Retention exists to bound disk, and a policy that
    can empty the directory turns the feature into a scheduled deletion — the opposite
    of the point.
    """
    keep = max(1, keep)
    victims = list_backups(db_path)[keep:]
    removed = 0
    for old in victims:
        try:
            old.unlink()
            removed += 1
        except OSError:
            logger.warning("could not remove old memory backup %s", old, exc_info=True)
    return removed


def _stores_to_back_up() -> list[Path]:
    """Every active store's vector file, default first.

    Archived private stores remain visible to owner recovery, but routine
    backups do not repeatedly copy an unbound member's data. Both halves are
    shared: :func:`memory_stores.active_store_names` supplies active bindings,
    and :func:`memory_stores.owned_store_path` is the one
    resolve-then-confirm (so a name absent from the memoized view cannot resolve
    onto the DEFAULT store's file and get copied into a silo's backup directory).
    """
    paths: list[Path] = []
    for name in active_store_names():
        path = owned_store_path(name)
        if path is None:
            logger.warning("memory store %r is not backed up", name)
            continue
        paths.append(path)
    return paths


def back_up_all_stores(
    keep: int = DEFAULT_KEEP,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
    private_only: bool = False,
) -> dict[str, int]:
    """Back up and prune active stores, optionally limited to private V2 stores.

    FAIL SOFT PER STORE, and that is the whole reason the loop is here rather than at
    the caller: one unreadable silo must not cost the default store its backup. Counted
    rather than raised so the periodic caller can log a number without a try of its own,
    and so a store that starts failing is visible as a non-zero count.
    """
    from kiro_crew import member_memory_backup
    from kiro_crew.memory_startup import require_memory_prepared, require_memory_ready

    require_memory_prepared()
    result = {"backed_up": 0, "skipped": 0, "pruned": 0, "failed": 0}
    stamp = now or datetime.now(timezone.utc)
    for path in _stores_to_back_up():
        if should_stop is not None and should_stop():
            break
        try:
            if private_only and not member_memory_backup.is_member_store(path):
                result["skipped"] += 1
                continue
            require_memory_ready(named_store_of_db(path))
            existing = list_backups(path)
            if existing and _age_hours(existing[0], stamp) < MIN_BACKUP_INTERVAL_HOURS:
                result["skipped"] += 1
                continue
            if backup_store(path, now=stamp) is None:
                result["skipped"] += 1
                continue
            result["backed_up"] += 1
            if should_stop is not None and should_stop():
                break
            result["pruned"] += prune_backups(path, keep)
        except MemoryBackupFailed:
            result["failed"] += 1
            logger.warning("memory backup failed for %s", path, exc_info=True)
        except Exception:
            result["failed"] += 1
            logger.warning("memory backup pass failed for %s", path, exc_info=True)
    return result


def _age_hours(backup: Path, now: datetime) -> float:
    """How old *backup* is, read from its STAMPED NAME rather than its mtime.

    A copied or restored file carries whatever mtime the copy gave it while its name
    still says when its contents were taken — and the interval guard has to answer
    about the contents. An unparseable name answers ``inf`` so the guard never SKIPS on
    a name it did not understand: erring toward taking a backup is the safe direction.
    """
    try:
        if backup.suffix == ".zip":
            from kiro_crew.member_memory_backup import snapshot_time as member_snapshot_time

            return (now - member_snapshot_time(backup)).total_seconds() / 3600.0
        return (now - snapshot_time(backup)).total_seconds() / 3600.0
    except ValueError:
        return float("inf")


def newest_backup(store: str = DEFAULT_MEMORY_STORE) -> Path | None:
    """The most recent backup of *store*, or ``None``. The restore entry point's input."""
    try:
        db_path = resolve_store_path(store)
    except Exception:
        return None
    backups = list_backups(db_path)
    return backups[0] if backups else None


def _require_v1_restore_database(db: sqlite3.Connection, store: str) -> None:
    """Accept legacy memory shapes without importing private ownership."""
    from kiro_crew import memory_schema

    lineage = memory_schema.detect_lineage(db)
    if lineage != memory_schema.LINEAGE_V1 and not (
        store != DEFAULT_MEMORY_STORE and lineage == memory_schema.LINEAGE_CREW
    ):
        raise ValueError("V1 restore requires a V1 memory database")
    has_meta = db.execute(
        "SELECT 1 FROM sqlite_schema WHERE type IN ('table', 'view') AND name='memory_meta'"
    ).fetchone()
    if (
        has_meta
        and db.execute(
            "SELECT 1 FROM memory_meta WHERE key IN (?, ?) LIMIT 1",
            (memory_schema.PRIVATE_MEMORY_VERSION_META_KEY, memory_schema.OWNER_MEMBER_META_KEY),
        ).fetchone()
    ):
        raise ValueError("V1 restore requires a V1 memory database without private ownership")


def restore_from_backup(backup: Path, store: str = DEFAULT_MEMORY_STORE) -> Path:
    """Stage a validated restore; activation requires the gateway startup barrier.

    Live files and WAL writers remain untouched. At startup the prior V1 database
    and its sidecars are preserved together before installing the staged copy.
    V2 retains its complete-directory restore lifecycle.
    """
    from kiro_crew.memory_startup import require_memory_ready

    require_memory_ready(store, allow_failed=True)
    if not backup.is_file():
        raise FileNotFoundError(f"backup {backup} does not exist")
    from kiro_crew import member_memory_backup

    if store != DEFAULT_MEMORY_STORE:
        member_target = resolve_store_path(store)
        if member_memory_backup.is_member_store(member_target):
            return member_memory_backup.stage_restore(backup, member_target)
    target = resolve_store_path(store)
    if target.name != MEMORY_DB_FILE:  # pragma: no cover - resolver contract
        raise ValueError(f"{target} is not a memory store file")
    out = backup_dir_for(target)
    platform_compat.make_owner_only_dir(out)
    with (out / ".restore.lock").open("a+b") as lock:
        with platform_compat.file_lock(lock.fileno(), required=True, wait=False):
            if (out / _V1_PENDING).exists():
                raise ValueError("A restore is already pending; restart or cancel it first")
            stage = out / ("restore-" + uuid4().hex + ".db")
            try:
                with (
                    closing(sqlite3.connect(_read_only_uri(backup), uri=True)) as src,
                    closing(sqlite3.connect(str(stage))) as dst,
                ):
                    if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Backup fails its integrity check; refusing to restore")
                    _require_v1_restore_database(src, store)
                    src.backup(dst)
                with closing(sqlite3.connect(_read_only_uri(stage), uri=True)) as probe:
                    if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Staged backup fails its integrity check")
                    _require_v1_restore_database(probe, store)
                platform_compat.restrict_to_owner(stage)
                _write_v1_journal(
                    out,
                    {
                        "stage": stage.name,
                        "sha256": _file_digest(stage),
                        "aside": target.name + ".superseded." + uuid4().hex,
                        "backup_name": backup.name,
                        "staged_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except BaseException:
                if not (out / _V1_PENDING).exists():
                    stage.unlink(missing_ok=True)
                raise
    return target


def _file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write_v1_journal(out: Path, journal: dict) -> None:
    atomic_write(out / _V1_PENDING, json.dumps(journal), restrict_to_owner=True)


def _v1_journal(db_path: Path) -> tuple[Path, dict | None]:
    out = backup_dir_for(db_path)
    pending = out / _V1_PENDING
    if not pending.exists():
        return out, None
    if (
        pending.resolve() != pending.parent.resolve() / pending.name
        or pending.stat().st_size > 16_384
    ):
        raise ValueError("Invalid pending memory restore journal")
    journal = json.loads(pending.read_text(encoding="utf-8"))
    if not isinstance(journal, dict):
        raise ValueError("Invalid pending memory restore journal")
    for key, pattern in (
        ("stage", r"restore-[0-9a-f]{32}\.db"),
        ("aside", re.escape(db_path.name) + r"\.superseded\.[0-9a-f]{32}"),
        ("sha256", r"[0-9a-f]{64}"),
    ):
        if not isinstance(journal.get(key), str) or not re.fullmatch(pattern, journal[key]):
            raise ValueError("Invalid pending memory restore paths or checksum")
    prior = journal.get("prior_files")
    if prior is not None and (
        not isinstance(prior, list)
        or any(suffix not in _V1_SIDECARS for suffix in prior)
        or len(prior) != len(set(prior))
    ):
        raise ValueError("Invalid pending memory restore prior files")
    for path in (db_path, out / journal["stage"], db_path.with_name(journal["aside"])):
        for suffix in _V1_SIDECARS:
            candidate = Path(str(path) + suffix)
            if candidate.resolve() != candidate.parent.resolve() / candidate.name:
                raise ValueError("Pending memory restore path is redirected")
    return out, journal


def pending_restore_status(db_path: Path) -> dict:
    """Report staged recovery for either lineage without opening live memory."""
    from kiro_crew import member_memory_backup
    from kiro_crew.memory_startup import memory_restore_startup_status

    if member_memory_backup.is_member_store(db_path):
        return member_memory_backup.pending_restore_status(db_path)
    try:
        _, journal = _v1_journal(db_path)
    except (ValueError, OSError, sqlite3.Error) as exc:
        out = backup_dir_for(db_path)
        return {
            "pending": True,
            "restart_required": True,
            "pending_restore": None,
            "restore_error": str(exc),
            **memory_restore_startup_status(named_store_of_db(db_path)),
            "recovery": {
                "journal": _V1_PENDING,
                "staged_copies": [p.name for p in islice(out.glob("restore-*.db"), 8)],
                "previous_copies": [
                    p.name for p in islice(db_path.parent.glob(db_path.name + ".superseded.*"), 8)
                ],
                "instruction": (
                    "If cancellation is refused, stop the gateway and preserve the journal, "
                    "staged database and superseded database/WAL/SHM files. Recover the complete "
                    "previous database and its sidecars together, then repair the journal before restarting."
                ),
            },
        }
    return {
        "pending": journal is not None,
        "restart_required": journal is not None,
        "pending_restore": (
            {
                "backup_name": journal.get("backup_name", ""),
                "staged_at": journal.get("staged_at", ""),
            }
            if journal
            else None
        ),
        **memory_restore_startup_status(named_store_of_db(db_path)),
    }


def _quarantine_invalid_v1_pending(db_path: Path, out: Path) -> bool:
    """Never discard ambiguous move state or create an empty Global database."""
    from kiro_crew.memory_stores import named_store_of_db

    pending = out / _V1_PENDING
    name = named_store_of_db(db_path) or DEFAULT_MEMORY_STORE
    if (
        owned_store_path(name) != db_path
        or db_path.resolve() != db_path.parent.resolve() / db_path.name
        or pending.resolve() != pending.parent.resolve() / pending.name
        or not pending.is_file()
    ):
        raise ValueError("Pending V1 restore is outside its canonical store; cancellation refused")
    if any(db_path.parent.glob(db_path.name + ".superseded.*")):
        raise ValueError(
            "Cannot cancel an unreadable pending-v1-restore.json while superseded files exist. "
            "Stop the gateway and preserve the journal and database/WAL/SHM copies listed in "
            "restore status; recover a complete previous database before repairing the journal."
        )
    # mode=ro refuses an absent file, rather than manufacturing empty memory.
    try:
        with closing(sqlite3.connect(_read_only_uri(db_path), uri=True)) as db:
            _require_v1_restore_database(db, name)
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError(
                    "Current V1 memory is not intact; pending restore must be repaired"
                )
    except sqlite3.Error as exc:
        raise ValueError(
            "Current V1 memory is absent or unreadable; preserve the pending journal and copies"
        ) from exc
    replace_with_retry(pending, out / ("cancelled-v1-restore-" + uuid4().hex + ".json"))
    return True


def cancel_pending_restore(db_path: Path) -> bool:
    """Cancel an unpublished restore, retaining current memory and source backup."""
    from kiro_crew import member_memory_backup

    if member_memory_backup.is_member_store(db_path):
        return member_memory_backup.cancel_pending_restore(db_path)
    out = backup_dir_for(db_path)
    if not (out / _V1_PENDING).exists():
        return False
    with (out / ".restore.lock").open("a+b") as lock:
        with platform_compat.file_lock(lock.fileno(), required=True, wait=False):
            try:
                _, journal = _v1_journal(db_path)
            except (ValueError, OSError, sqlite3.Error):
                return _quarantine_invalid_v1_pending(db_path, out)
            if journal is None:
                return False
            if "prior_files" in journal:
                raise ValueError("Restore activation has started; restart before cancelling")
            (out / _V1_PENDING).unlink()
            try:
                (out / journal["stage"]).unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove a cancelled restore stage", exc_info=True)
            return True


def _apply_pending_v1_restore(db_path: Path) -> str | None:
    """Startup only: preserve the closed database/WAL pair and recover partial moves.

    The journal precedes every move, and the staged file is consumed last. A
    failed installation rolls prior files back; a crash resumes from whichever
    side of each atomic rename survived. Never call while writers are running.
    """
    out, journal = _v1_journal(db_path)
    if journal is None:
        return None
    with (out / ".restore.lock").open("a+b") as lock:
        with platform_compat.file_lock(lock.fileno(), required=True, wait=False):
            _, journal = _v1_journal(db_path)
            if journal is None:
                return None
            stage = out / journal["stage"]
            aside = db_path.with_name(journal["aside"])
            if not stage.exists():
                if "prior_files" not in journal or _file_digest(db_path) != journal["sha256"]:
                    raise ValueError("Pending restore lost its staged database")
                (out / _V1_PENDING).unlink()
                return aside.name if "" in journal["prior_files"] else ""
            if _file_digest(stage) != journal["sha256"]:
                raise ValueError("Staged restore changed; activation refused")
            if "prior_files" not in journal:
                # Capture at activation, not staging: all writes acknowledged
                # before shutdown, including an uncheckpointed WAL, belong here.
                journal["prior_files"] = [
                    suffix for suffix in _V1_SIDECARS if Path(str(db_path) + suffix).exists()
                ]
                _write_v1_journal(out, journal)
            if "" not in journal["prior_files"] and db_path.exists():
                raise ValueError("Current memory changed after restore activation began")
            if not any(Path(str(aside) + suffix).exists() for suffix in _V1_SIDECARS):
                # No move has started, or rollback completed. Opening SQLite
                # between attempts can checkpoint and remove the saved WAL;
                # recapture its current pair only in this fully returned state.
                # During a partial move every recorded component stays required.
                if "" in journal["prior_files"] and not db_path.exists():
                    raise ValueError("Pending restore lost its previous database")
                prior_files = [
                    suffix for suffix in _V1_SIDECARS if Path(str(db_path) + suffix).exists()
                ]
                if prior_files != journal["prior_files"]:
                    journal["prior_files"] = prior_files
                    _write_v1_journal(out, journal)
            # A diagnostic SQLite read after a rolled-back attempt can create
            # fresh sidecars. Preserve those too, never leave them beside the
            # replacement database or discard them as supposedly empty.
            new_sidecars = [
                suffix
                for suffix in _V1_SIDECARS[1:]
                if suffix not in journal["prior_files"] and Path(str(db_path) + suffix).exists()
            ]
            if new_sidecars:
                journal["prior_files"].extend(new_sidecars)
                _write_v1_journal(out, journal)
            try:
                for suffix in journal["prior_files"]:
                    current, preserved = Path(str(db_path) + suffix), Path(str(aside) + suffix)
                    if preserved.exists():
                        if current.exists():
                            raise ValueError("Restore has conflicting current and preserved files")
                    else:
                        replace_with_retry(current, preserved)
                replace_with_retry(stage, db_path)
            except BaseException:
                for suffix in journal["prior_files"]:
                    current, preserved = Path(str(db_path) + suffix), Path(str(aside) + suffix)
                    if preserved.exists() and not current.exists():
                        replace_with_retry(preserved, current)
                raise
            (out / _V1_PENDING).unlink()
            return aside.name if "" in journal["prior_files"] else ""


def apply_pending_member_restores(
    *,
    should_stop: Callable[[], bool] | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
) -> dict[str, str]:
    """Blocking restore pass for the deferred gateway worker, before memory opens.

    Unlike a read/CLI store opener this explicit lifecycle hook may activate a
    staged restore. Each failed activation is reported to the gateway's store
    fence while later stores still restore. Without that callback the pass
    raises its first error after visiting the remaining stores. The external
    journals remain authoritative across crashes and gateway restarts.
    """
    from kiro_crew import member_memory_backup
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.memory_stores import memory_stores_root, validate_memory_store_name

    # Load structural configuration before changing any store. A failure here
    # cannot be assigned safely to one store, unlike a journal/activation error.
    config = KiroCrewConfig.load()
    applied: dict[str, str] = {}
    errors: list[Exception] = []

    def failed(name: str, error: Exception, *, declaration: bool = False) -> None:
        failure = MemoryBackupFailed(
            (
                f"Invalid memory store declaration {name!r}: {error}. "
                "Repair this memory_stores entry in config.json, then restart."
            )
            if declaration
            else (
                f"Pending memory restore for {name!r} could not be activated: {error}. "
                "Previous memory is preserved; inspect or cancel the pending restore, then restart."
            )
        )
        if on_error is not None:
            on_error(name, failure)
        else:
            errors.append(failure)

    if should_stop is not None and should_stop():
        return applied
    try:
        preserved = _apply_pending_v1_restore(resolve_store_path(DEFAULT_MEMORY_STORE))
    except Exception as exc:
        failed(DEFAULT_MEMORY_STORE, exc)
    else:
        if preserved is not None:
            applied[DEFAULT_MEMORY_STORE] = preserved
    for name, record in config.memory_stores.items():
        if should_stop is not None and should_stop():
            return applied
        try:
            # A malformed declared key belongs to that unusable store.  Keep it
            # inside the same per-store boundary as missing paths and invalid
            # restore journals so one bad config entry cannot convert the
            # otherwise healthy Global store into a structural startup failure.
            validate_memory_store_name(name)
        except ValueError as exc:
            failed(name, exc, declaration=True)
            continue
        try:
            if name == DEFAULT_MEMORY_STORE:
                continue
            if getattr(record, "memory_version", 1) != 2 or not getattr(record, "owner_member", ""):
                path = owned_store_path(name)
                if path is not None:
                    preserved = _apply_pending_v1_restore(path)
                else:
                    raise ValueError("The declared store path is unavailable")
            else:
                path = memory_stores_root().resolve() / name / MEMORY_DB_FILE
                preserved = member_memory_backup.apply_pending_restore(path)
            if preserved is not None:
                applied[name] = preserved
        except Exception as exc:
            failed(name, exc)
    if errors:
        raise errors[0]
    return applied

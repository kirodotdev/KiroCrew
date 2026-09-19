"""Coordinated mutation of the protected, current-version process record directory."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.asset_downloader import PinnedTargetDir
from kiro_crew.atomic_write import atomic_write, atomic_write_at

logger = logging.getLogger(__name__)

LOCK_NAME = ".reclaim.lock"
SCAN_BUDGET = 256
MAX_RECORD_BYTES = 4096
#: Publishers wait this long for a CONTENDED lock before giving this attempt up.
#: The longest holder is the maintenance sweep, which keeps the lock for one
#: SCAN_BUDGET pass of lstat/read/probe (its duration is not measured here; the
#: bound is a ceiling, not an expectation). The ceiling keeps a publisher on a
#: worker thread from waiting indefinitely behind a stuck holder, which POSIX
#: ``flock`` without ``LOCK_NB`` would do. Reclamation itself never waits.
LOCK_WAIT_SECS = 2.0
_LOCK_POLL_SECS = 0.02
_RECORD_NAME = re.compile(r"([1-9][0-9]{0,9})(\.namespace)?\.json\Z")


class RecordLockContended(BlockingIOError):
    """The stable lock stayed held by another publisher/reclaimer past ``wait``."""


def _require_owner(fd: int, path: Path, *, directory: bool = False) -> os.stat_result:
    info = os.fstat(fd)
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise OSError("not a regular protected object")
    if not directory and info.st_nlink != 1:
        raise OSError("hardlinked protected record")
    if platform_compat.IS_POSIX:
        if info.st_uid != platform_compat.local_user_id() or info.st_mode & 0o022:
            raise OSError("unsafe process record owner or permissions")
    else:
        from kiro_crew import windows_acl

        sid = platform_compat.current_user_sid()
        try:
            security = windows_acl.describe(path)
        except windows_acl.AclUnavailable as exc:
            raise OSError("process record ACL unavailable") from exc
        trusted = {sid, *windows_acl.WELL_KNOWN_TRUSTED_SIDS}
        if (
            not sid
            or not security.volume_is_local
            or security.owner_sid not in trusted
            or security.null_dacl
            or security.unparsable_ace_types
            or any(
                # Owner Rights refers to the actual owner validated above.
                w.sid not in trusted and w.sid != "S-1-3-4"
                for w in security.writers
            )
        ):
            raise OSError("unsafe process record ACL")
    return info


@contextmanager
def record_directory(home: Path, *, create: bool = False) -> Iterator[PinnedTargetDir]:
    """Resolve the supported home alias once; pin every protected descendant."""
    root = home.resolve()
    with ExitStack() as stack:
        if platform_compat.IS_POSIX:
            fd = pinned_fs.pin_parent(str(root), what="process record home", refusal=OSError)
        else:
            # Each earlier handle blocks ancestor substitution at the next open.
            for parent in reversed(root.parents):
                pin = platform_compat.pin_directory(parent)
                stack.callback(os.close, pin)
            fd = platform_compat.pin_directory(root)
        stack.callback(os.close, fd)
        _require_owner(fd, root, directory=True)
        path = root
        for name in ("member-memory-bindings", "pids"):
            path = path / name
            if create:
                try:
                    if platform_compat.IS_POSIX:
                        os.mkdir(name, 0o700, dir_fd=fd)
                    else:
                        os.mkdir(path, 0o700)
                        platform_compat.restrict_dir_to_owner(path)
                except FileExistsError:
                    pass
            fd = (
                os.open(name, pinned_fs.dir_flags(), dir_fd=fd)
                if platform_compat.IS_POSIX
                else platform_compat.pin_directory(path)
            )
            stack.callback(os.close, fd)
            _require_owner(fd, path, directory=True)
        yield PinnedTargetDir(path, fd, relative=platform_compat.IS_POSIX)


def _same_named(directory: PinnedTargetDir, name: str, seen: os.stat_result) -> bool:
    now = directory.lstat(name)
    return bool(
        now
        and stat.S_ISREG(now.st_mode)
        and now.st_nlink == 1
        and (now.st_dev, now.st_ino) == (seen.st_dev, seen.st_ino)
    )


@contextmanager
def record_lock(directory: PinnedTargetDir, *, wait: float = 0.0) -> Iterator[None]:
    """One stable lock, never replaced/unlinked; failure never enters the body.

    ``wait`` bounds a retry on CONTENTION only (``BlockingIOError``: another
    publisher or the sweep holds the lock right now). Every other refusal -- an
    unsafe or replaced lock object, an unavailable root -- raises at once. The
    retry never blocks the platform lock call itself, so the bound holds on POSIX
    (where a waiting ``flock`` is unbounded) and on Windows alike.
    """
    with ExitStack() as stack:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NONBLOCK", 0)
        if platform_compat.IS_POSIX:
            fd = directory.open(LOCK_NAME, flags | os.O_NOFOLLOW)
        else:
            try:
                fd = directory.open(LOCK_NAME, flags | os.O_EXCL)
            except FileExistsError:
                # Pin the no-reparse leaf before the CRT opens it read/write.
                pin = platform_compat.open_file_no_reparse(directory.describe(LOCK_NAME))
                stack.callback(os.close, pin)
                _require_owner(pin, directory.describe(LOCK_NAME))
                fd = directory.open(LOCK_NAME, os.O_RDWR)
        stack.callback(os.close, fd)
        seen = _require_owner(fd, directory.describe(LOCK_NAME))
        if not _same_named(directory, LOCK_NAME, seen):
            raise OSError("process record lock changed")
        deadline = time.monotonic() + wait
        while True:
            try:
                stack.enter_context(
                    platform_compat.file_lock(fd, exclusive=True, required=True, wait=False)
                )
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise RecordLockContended(*exc.args) from exc
                time.sleep(_LOCK_POLL_SECS)
        if not _same_named(directory, LOCK_NAME, seen):
            raise OSError("process record lock changed")
        yield


@contextmanager
def _open_record(directory: PinnedTargetDir, name: str) -> Iterator[int]:
    fd = (
        directory.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if platform_compat.IS_POSIX
        else platform_compat.open_file_no_reparse(directory.describe(name), nonblocking=True)
    )
    try:
        _require_owner(fd, directory.describe(name))
        yield fd
    finally:
        os.close(fd)


def read_record(
    directory: PinnedTargetDir, name: str
) -> tuple[dict[str, Any], os.stat_result] | None:
    try:
        with _open_record(directory, name) as fd:
            seen = os.fstat(fd)
            if seen.st_size > MAX_RECORD_BYTES:
                return None
            data = os.read(fd, MAX_RECORD_BYTES + 1)
            if len(data) > MAX_RECORD_BYTES:
                return None
            row = json.loads(data.decode("utf-8"))
            return (row, seen) if isinstance(row, dict) else None
    except (OSError, ValueError):
        return None


def publish_binding(home: Path, pid: int, session_key: str, store: str) -> None:
    """Optional publication/invalidation, serialized with every other mutation.

    Runs on a worker thread, never the event loop, so it may wait a bounded
    ``LOCK_WAIT_SECS`` for a sweep in progress instead of skipping this
    publication. An unsafe or unavailable root still skips at once.
    """
    try:
        with (
            record_directory(home, create=True) as directory,
            record_lock(directory, wait=LOCK_WAIT_SECS),
        ):
            name = f"{pid}.json"
            # Probe inside the lock: a delayed publisher must not act on an old token.
            start = platform_compat.get_process_start_id(pid)
            existing = directory.lstat(name)
            if existing is not None:
                with _open_record(directory, name) as fd:
                    seen = os.fstat(fd)
                if not _same_named(directory, name, seen):
                    return
            if not start or not isinstance(session_key, str) or not session_key:
                directory.unlink(name)
                return
            payload = json.dumps(
                {
                    "version": 2,
                    "session_key": session_key,
                    "process_start": start,
                    "memory_store": store,
                }
            )
            if platform_compat.IS_POSIX:
                atomic_write_at(directory.fd, name, payload, mode=0o600)
            else:
                atomic_write(directory.describe(name), payload, restrict_to_owner=True)
    except RecordLockContended:
        # Contention outlived the bound: nothing was written, so the record keeps
        # whatever the previous publication left (a still-valid row, a stale row
        # or none); the next turn boundary republishes. Logged because the caller
        # cannot otherwise tell this from the silent unsafe/unavailable skip.
        logger.warning(
            "member binding for pid %s not published: process record lock held for "
            "more than %gs by another publisher or the reclaim sweep",
            pid,
            LOCK_WAIT_SECS,
        )
    except (OSError, ValueError):
        # Missing authority never grants access; a later publication may retry.
        return


def _record_identity(name: str, row: dict[str, Any]) -> tuple[int, str] | None:
    match = _RECORD_NAME.fullmatch(name)
    if match is None or not 1 < int(match[1]) <= 0xFFFFFFFF:
        return None
    start = row.get("process_start")
    if not isinstance(start, str) or not start:
        return None
    if match[2]:
        if set(row) != {"process_start", "namespaces", "private_memory"}:
            return None
        pairs = row["namespaces"]
        if (
            type(row["private_memory"]) is not bool
            or not isinstance(pairs, list)
            or len(pairs) != 2
            or any(
                not isinstance(pair, list)
                or len(pair) != 2
                or any(type(n) is not int or n < 0 for n in pair)
                for pair in pairs
            )
        ):
            return None
    elif (
        set(row) != {"version", "session_key", "process_start", "memory_store"}
        or type(row["version"]) is not int
        or row["version"] not in (1, 2)
        or not isinstance(row["session_key"], str)
        or not row["session_key"]
        or not isinstance(row["memory_store"], str)
        or row["memory_store"] == "default"
        or (row["version"] == 1 and not row["memory_store"])
    ):
        return None
    return int(match[1]), start


def _owner_gone(pid: int, start: str) -> bool:
    live = platform_compat.get_process_start_id(pid)
    if live and live != start:
        return True
    return platform_compat.pid_confirmed_absent(pid)


@dataclass
class RecordScan:
    entries: Any
    identity: tuple[int, int]

    def close(self) -> None:
        self.entries.close()


def reclaim_stale_member_bindings(
    *, data_home: Path, cursor: RecordScan | None = None
) -> tuple[int, RecordScan | None]:
    """Advance at most SCAN_BUDGET directory entries, including non-record names.

    The caller retains the streaming cursor between maintenance ticks and closes
    it at shutdown. Every candidate is re-read under the same lock as writers;
    directory replacement resets enumeration, never authorizes an old unlink.
    """
    removed = 0
    try:
        with record_directory(data_home) as directory, record_lock(directory):
            info = os.fstat(directory.fd)
            identity = (info.st_dev, info.st_ino)
            if cursor is not None and cursor.identity != identity:
                cursor.close()
                cursor = None
            if cursor is None:
                cursor = RecordScan(
                    os.scandir(directory.fd if platform_compat.IS_POSIX else directory.directory),
                    identity,
                )
            for _ in range(SCAN_BUDGET):
                try:
                    entry = next(cursor.entries, None)
                except OSError:
                    cursor.close()
                    return removed, None
                if entry is None:
                    cursor.close()
                    return removed, None
                name = entry.name
                if _RECORD_NAME.fullmatch(name) is None:
                    continue
                record = read_record(directory, name)
                if record is None:
                    continue
                row, seen = record
                owner = _record_identity(name, row)
                if owner is not None and _owner_gone(*owner) and _same_named(directory, name, seen):
                    directory.unlink(name)
                    removed += 1
    except (OSError, ValueError):
        pass
    return removed, cursor

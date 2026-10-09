"""Move a damaged SQLite file aside so its store can start empty.

One rule for "is this file damaged" and one mover, shared by every store that
recovers from a damaged file at open (the task store and the knowledge store).
"""

from __future__ import annotations

import os
import sqlite3 as _stdlib_sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.owner_only_files import SQLITE_SIDECAR_SUFFIXES

# SQLite's own verdicts that the FILE is damaged. Exact phrases: a bare substring
# such as ``corrupt`` could match a path or a wrapped message.
_DAMAGE_MARKERS = (
    "file is not a database",
    "database disk image is malformed",
    "malformed database schema",
)
# Another writer or the host, never the file: a store must keep refusing on these.
_NOT_DAMAGE_MARKERS = ("locked", "busy", "disk is full", "database or disk is full", "readonly")


def is_damaged_database_error(exc: BaseException) -> bool:
    """True only when SQLite says the database file itself is damaged.

    A lock, a full disk or a read-only file is not damage, and neither is any
    other error (a schema newer than this build, a permission error): moving a
    VALID file aside would lose data. Accepts errors from either driver this
    tree binds.
    """
    if not isinstance(exc, (_stdlib_sqlite3.DatabaseError, sqlite3.DatabaseError)):
        return False
    text = str(exc).lower()
    if any(marker in text for marker in _NOT_DAMAGE_MARKERS):
        return False
    return any(marker in text for marker in _DAMAGE_MARKERS)


def move_without_overwrite(src: Path, dst: Path) -> None:
    """Rename that FAILS on an existing destination on every platform.

    POSIX ``rename`` replaces silently and Windows refuses, so neither is the
    portable form. A hard link never replaces anywhere: link, then unlink the
    source. A filesystem without links gets a rename behind an existence
    check, which is the best that filesystem offers.
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        if dst.exists():
            raise FileExistsError(str(dst))
        src.rename(dst)
        return
    src.unlink()


def reserve_quarantine_name(path: Path) -> Path:
    """Create ``<name>.corrupt-<utc>`` beside *path* exclusively and return it.

    ``O_EXCL`` is the collision check: an existing name -- an earlier boot in
    the same microsecond, another process, an operator's copy -- fails the
    create, and the counter suffix moves on to the next name.
    """
    now = time.time()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    base = f"{path.name}.corrupt-{stamp}.{int((now % 1) * 1_000_000):06d}Z-{os.getpid()}"
    for n in range(10_000):
        candidate = path.with_name(base if n == 0 else f"{base}-{n}")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise FileExistsError(f"no free quarantine name beside {path}")


@dataclass
class Quarantine:
    """Where a damaged file went, and any sidecar that could not follow it."""

    target: Path
    moved: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)


def quarantine_sqlite_file(path: Path) -> Quarantine:
    """Move *path* and its sidecars aside under a name nothing else holds.

    Never overwrites: each copy is recovery evidence. A hot ``-journal`` left
    beside a recreated database would be rolled into it, so it moves too. The
    base name is RESERVED before anything moves, so two boots -- or two
    processes -- cannot pick the same one. Sidecars move first and the
    database LAST, because the database's absence is what the reopen keys on:
    a sidecar that will not move is reported in ``left``, never a reason to
    keep the damaged database in place. Raises ``OSError`` only when the
    database itself cannot move (or no free name exists); the reserved
    placeholder is removed first.
    """
    target = reserve_quarantine_name(path)
    result = Quarantine(target=target)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        src = path.with_name(path.name + suffix)
        if not src.exists():
            continue
        dst = target.with_name(target.name + suffix)
        try:
            move_without_overwrite(src, dst)
            result.moved.append(dst.name)
        except OSError as exc:
            result.left.append(f"{src.name} ({exc})")
    try:
        # Onto the placeholder this call created: the one replace that is ours
        # to make.
        os.replace(path, target)
    except OSError:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    result.moved.append(target.name)
    return result

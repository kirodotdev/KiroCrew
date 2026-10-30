"""The owner-only staged copy of a ``.crewmate`` file (rfc-crewmate-share.md §5.4).

A ``crewmate.import`` card holds at most 64 KiB, so it cannot hold the files a
bundle carries. When ``POST /api/members/import`` has checked a file, the local
backend keeps the checked bytes here, on the owner's own machine, until the
card is applied, cancelled or expires:

* ``<data home>/run/crewmate-imports/<sha256>.crewmate``. The ``run/`` subtree is
  fenced from agent file tools, the directory is ``0700`` and the file ``0600``,
  and the name is the content's own digest: nothing the bundle says becomes a
  path.
* The copy is untrusted data. It is only ever re-read by
  :func:`kiro_crew.crewmate_bundle.read_crewmate_file`; nothing executes or
  imports it.
* :func:`load` refuses a copy whose bytes do not hash to its name.

``written/<member_id>.json`` beside it records what the import's file step wrote
for one new crewmate, so that step's Undo removes exactly those files and no
other. It is written by core only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

from kiro_crew.config.paths import data_home

SUFFIX = ".crewmate"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_LEDGER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MAX_BYTES = 8 * 1024 * 1024
_LEDGER_MAX_BYTES = 256 * 1024


class StageError(Exception):
    """The staged copy is missing, unreadable or not the file the card names."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def stage_dir() -> Path:
    """Resolved per call, so a test's or a pod's data home is the one used."""
    return data_home() / "run" / "crewmate-imports"


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=stat.S_IRWXU)
    if os.name != "nt":
        os.chmod(path, stat.S_IRWXU)  # owner-only: rwx------
    return path


def _path_for(sha: str) -> Path:
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise StageError("invalid_sha256", "not a sha256")
    return stage_dir() / f"{sha}{SUFFIX}"


def _write_private(target: Path, data: bytes) -> None:
    """Write *data* at *target* as a ``0600`` file, atomically, never through a link."""
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:12]}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _read_regular(path: Path, cap: int) -> bytes:
    """Read a regular file at *path*, refusing a link and anything over *cap*."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise StageError("staged_copy_missing", "the checked file is no longer kept") from None
    except OSError:
        raise StageError("staged_copy_unreadable", "the checked file cannot be read") from None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise StageError("staged_copy_unreadable", "the checked file is not a plain file")
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise StageError("staged_copy_unreadable", "the checked file is too large")
    return data


def stage(data: bytes) -> str:
    """Keep the checked bytes of one ``.crewmate`` file; return their sha256."""
    sha = hashlib.sha256(data).hexdigest()
    _private_dir(stage_dir())
    _write_private(_path_for(sha), bytes(data))
    return sha


def load(sha: str) -> bytes:
    """The staged bytes named *sha*. Raises :class:`StageError` on any mismatch."""
    data = _read_regular(_path_for(sha), _MAX_BYTES)
    if hashlib.sha256(data).hexdigest() != sha:
        raise StageError("staged_copy_changed", "the checked file changed since the card")
    return data


def discard(sha: str) -> None:
    """Delete the staged copy named *sha*; nothing when there is none."""
    try:
        _path_for(sha).unlink(missing_ok=True)
    except (StageError, OSError):
        pass


def sweep(keep: set[str]) -> list[str]:
    """Delete every staged copy whose sha256 is not in *keep*; return the removed names.

    *keep* is the bundle digests of the import cards still pending or applying.
    A leftover temp file is removed too. The ``written/`` ledgers are left alone:
    they belong to applied cards whose Undo still needs them.
    """
    removed: list[str] = []
    root = stage_dir()
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return removed
    except OSError:
        return removed
    for entry in entries:
        name = entry.name
        if entry.is_dir(follow_symlinks=False):
            continue
        sha = name[: -len(SUFFIX)] if name.endswith(SUFFIX) else ""
        stale_tmp = name.startswith(".") and name.endswith(".tmp")
        if (sha and _SHA_RE.fullmatch(sha) and sha not in keep) or stale_tmp:
            try:
                os.unlink(entry.path)
                removed.append(name)
            except OSError:
                pass
    return removed


def _ledger_path(member_id: str) -> Path:
    if not isinstance(member_id, str) or not _LEDGER_ID_RE.fullmatch(member_id):
        raise StageError("invalid_member", "not a member id")
    return stage_dir() / "written" / f"{member_id}.json"


def write_ledger(member_id: str, record: dict[str, Any]) -> None:
    """Record what the file step wrote for *member_id* (core's own record, ``0600``)."""
    path = _ledger_path(member_id)
    _private_dir(stage_dir())
    _private_dir(path.parent)
    _write_private(path, json.dumps(record, ensure_ascii=False).encode("utf-8"))


def read_ledger(member_id: str) -> dict[str, Any] | None:
    try:
        raw = _read_regular(_ledger_path(member_id), _LEDGER_MAX_BYTES)
    except StageError as exc:
        if exc.code == "staged_copy_missing":
            return None
        raise
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise StageError("ledger_unreadable", "the import record cannot be read") from None
    if not isinstance(record, dict):
        raise StageError("ledger_unreadable", "the import record cannot be read")
    return record


def drop_ledger(member_id: str) -> None:
    try:
        _ledger_path(member_id).unlink(missing_ok=True)
    except (StageError, OSError):
        pass

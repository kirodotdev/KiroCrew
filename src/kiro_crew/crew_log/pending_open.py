"""Entries held for a session unit whose crew log does not exist yet.

After a reset the slot's newest unit may have no crew log until its first turn, which
alone knows the header. An entry about that unit waits in an owner-only file under
``crew-log/pending-open/`` (at most :data:`PER_UNIT_LIMIT` records) and the unit's next
opening append writes it behind ``session/opened``. The older unit is never written.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.crew_log.emit_runtime import _handle, _report, _submit
from kiro_crew.crew_log.emit_state import _KIND, _crew_log, _note_eager, enabled

if TYPE_CHECKING:  # pragma: no cover
    from kiro_crew.crew_log.store import CrewLog

logger = logging.getLogger(__name__)

#: Records held per unit; past it a write is refused and counted in ``dropped_writes``.
PER_UNIT_LIMIT = 32

_warned: "set[str]" = set()  # units already warned about at the bound


def _root() -> Path:
    from kiro_crew.config.paths import data_home

    return data_home() / "crew-log" / "pending-open"


def path_for(session_id: str) -> Path:
    from kiro_crew.session_ledger import _store_name

    return _root() / f"{_store_name(session_id)}.jsonl"


def _marker_for(session_id: str) -> Path:
    return path_for(session_id).with_suffix(".draining")


def write_when_opened(
    session_id: str, entry_type: str, data: dict[str, Any], *, src: str, hold: bool = True
) -> None:
    """Append to *session_id*'s crew log, or (``hold``) keep the entry until it opens.

    Decided on the writer thread. Entries still held for the unit are appended first.
    """
    if not session_id or not enabled():
        return

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            if hold and not _keep(session_id, {"type": entry_type, "data": data, "src": src}):
                raise _crew_log().CrewLogError(
                    "pending-open bound reached; entry cannot be held",
                    code=_crew_log().CODE_NO_LEDGER,
                )
            return
        # Held entries go first, in order. A failed replay raises, so the writer retries
        # this whole job and the new entry never lands ahead of the held ones.
        if path_for(session_id).exists():
            _drain(session_id, log)
        entry = log.append(entry_type, data, src=src)
        _note_eager(entry, entry_type, session_id, data)

    _submit(_job, f"appending {entry_type} once its crew log exists", session_id)


def _keep(session_id: str, record: dict[str, Any]) -> bool:
    """Append *record* in one write, cut back on failure. False at the bound."""
    from kiro_crew.owner_only_files import mkdirs_owner_only, owner_only_opener

    path = path_for(session_id)
    mkdirs_owner_only(path.parent)
    line = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    if not _fits(line):
        _warn_once(session_id, "an entry is over the crew log's line ceiling and is dropped")
        return False
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    fd = owner_only_opener(str(path), flags)
    try:
        held = os.read(fd, os.fstat(fd).st_size) if os.fstat(fd).st_size else b""
        if held.count(b"\n") >= PER_UNIT_LIMIT:
            _warn_once(session_id, f"{PER_UNIT_LIMIT} entries already wait; more are dropped")
            return False
        # Bytes after the last newline are a write that never finished: cut them off.
        size = held.rfind(b"\n") + 1
        if size != len(held):
            os.ftruncate(fd, size)
        try:
            if os.write(fd, line) != len(line):
                raise OSError(errno.ENOSPC, "short write to the pending-open file")
            os.fsync(fd)
            _sync_dir(path)  # the file's name, not only its bytes, must survive a power loss
        except OSError:
            os.ftruncate(fd, size)  # cut back, so the writer's retry holds exactly one
            raise
    finally:
        try:
            os.close(fd)
        except OSError:
            # Past the fsyncs the record is kept; raising would make the retry hold it twice.
            logger.warning("crew log %s: closing the pending file failed", session_id)
    return True


def _fits(line: bytes) -> bool:
    """Whether *line* leaves room for the entry envelope under the crew log's ceiling."""
    from kiro_crew.crew_log.emit_text import _ENVELOPE_HEADROOM

    return len(line) + _ENVELOPE_HEADROOM <= _crew_log().MAX_ENTRY_BYTES


def _warn_once(session_id: str, why: str) -> None:
    if session_id not in _warned:
        _warned.add(session_id)
        logger.warning("crew log %s: %s", session_id, why)


def _sync_dir(path: Path) -> None:
    from kiro_crew.atomic_write import fsync_dir

    fsync_dir(path.parent)


def drain(session_id: str, log: "CrewLog") -> None:
    """Append the held records in order, then remove the files. Never raises.

    A failure leaves the files; the next drain skips what this one appended.
    """
    try:
        _drain(session_id, log)
    except Exception as exc:
        _report("pending open drain", exc, op="pending-open")


def _drain(session_id: str, log: "CrewLog") -> None:
    path, marker = path_for(session_id), _marker_for(session_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        marker.unlink(missing_ok=True)
        return
    records = []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        if not _fits(line + b"\n"):  # the append would refuse it on every retry
            _warn_once(session_id, "a held entry is over the line ceiling and is dropped")
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            logger.warning("crew log %s: a pending entry was unreadable", session_id)
    done = 0
    if marker.exists():
        mark = json.loads(marker.read_bytes())
        done = int(mark.get("done") or 0)
        done += _already_appended(log, mark, records[done:])
    if done < len(records):
        # The marker always names the CURRENT tail and what earlier attempts committed,
        # so entries appended between attempts never hide a record already written.
        _write_marker(session_id, log, marker, done)
    for record in records[done:]:
        entry = log.append(record["type"], record["data"], src=record["src"])
        _note_eager(entry, record["type"], session_id, record["data"])
    path.unlink(missing_ok=True)
    marker.unlink(missing_ok=True)
    _sync_dir(path)


def _write_marker(session_id: str, log: "CrewLog", marker: Path, done: int) -> None:
    """Where *log* ends and how many records are already in it, durably, before replay."""
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.crew_log.store_paths import crew_log_dir, newest_segment

    start = log.last_seq
    point: "dict[str, Any] | None" = None
    segment = newest_segment(crew_log_dir(_KIND, session_id))
    if segment is not None:
        point = {
            "segment": segment.name,
            "offset": segment.stat().st_size,
            "seq": start + 1,
            "cuts": log.cuts(),
        }
    body = json.dumps({"from_seq": start, "resume": point, "done": done}).encode("ascii")
    # Temp file, length-checked, fsynced, then renamed: a failed write leaves no marker.
    atomic_write(marker, body, fsync=True, mode=0o600)
    _sync_dir(marker)  # the rename is durable only once the directory is synced


def _already_appended(log: "CrewLog", mark: dict[str, Any], records: list[Any]) -> int:
    """How many *records* a cut-short drain appended, read from the marker's position.

    Only the unbroken run right after the marker counts: the drain appends nothing
    else in between, so the first entry that differs ends what it wrote.
    """
    from kiro_crew.crew_log.prefix_extent import resume_point_from

    start = int(mark.get("from_seq") or 0)
    if log.last_seq <= start:
        return 0
    done = 0
    for entry in log.iter_from(start + 1, resume=resume_point_from(mark.get("resume"))):
        if done == len(records) or (entry.type, entry.data) != (
            records[done]["type"],
            records[done]["data"],
        ):
            break
        done += 1
    return done


def sweep(removed: "list[str]", cutoff_ms: int) -> None:
    """Remove retired units' files, and any pending file older than *cutoff_ms*."""
    for session_id in removed:
        path_for(session_id).unlink(missing_ok=True)
        _marker_for(session_id).unlink(missing_ok=True)
    try:
        children = list(_root().iterdir())
    except OSError:
        return
    for child in children:
        try:
            if child.is_file() and child.stat().st_mtime * 1000 < cutoff_ms:
                child.unlink()
        except OSError:
            logger.debug("crew log retention: kept %s", child.name)

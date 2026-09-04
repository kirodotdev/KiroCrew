"""Cron execution history store.

Persists run records as JSONL files per job, with a global index for
cross-job queries.  Uses fcntl advisory locking for cross-process safety.

Storage layout:
    ~/.kiro/crew/cron-history/
        {job_id}.jsonl      — full records (including trace) for one job
        _index.jsonl        — lightweight index (no trace) for list queries

History is BEST-EFFORT.  When the directory cannot be created or written the
store constructs anyway with ``enabled`` False: reads return empty, writes are
dropped, and nothing raises.  A runtime failure on a store that DID construct
degrades the same way (``_degrade``) rather than propagating, so the invariant
holds at the store itself instead of relying on every caller to wrap it.
Losing run records must never take scheduling down with it — see
``_prepare_dir`` for how usability is decided, and ``prepare()`` for why a
loop-bound caller must resolve that off the event loop.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_SUMMARY_CAP = 200
_TRACE_CAP = 50 * 1024  # 50KB
_MAX_RECORDS_PER_JOB = 100
_MAX_INDEX_RECORDS = 2000

#: Errnos that mean "this store is refused", not "this attempt failed". Only
#: these disable history for the process's lifetime — see ``_degrade``.
_DENIAL_ERRNOS = frozenset({errno.EPERM, errno.EACCES, errno.EROFS})


@dataclass
class CronRunRecord:
    """Single cron execution record."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    job_id: str = ""
    trigger: str = "scheduled"  # "scheduled" | "manual"
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: int = 0
    status: str = "success"  # "success" | "failure" | "timeout" | "cancelled"
    summary: str = ""
    trace: str = ""
    error: str = ""

    def to_dict(self, include_trace: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not include_trace:
            d.pop("trace", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CronRunRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class CronHistoryStore:
    """JSONL-per-job execution history with global index."""

    def __init__(
        self,
        base_dir: Path | None = None,
        cron_summary_cap: int = _SUMMARY_CAP,
        cron_trace_cap_kb: int = _TRACE_CAP // 1024,
        cron_max_records_per_job: int = _MAX_RECORDS_PER_JOB,
        cron_max_index_records: int = _MAX_INDEX_RECORDS,
        *,
        _defer_prepare: bool = False,
    ):
        self._dir = (base_dir or config_dir()) / "cron-history"
        self._index_path = self._dir / "_index.jsonl"
        self._summary_cap = cron_summary_cap
        self._trace_cap = cron_trace_cap_kb * 1024
        self._max_records_per_job = cron_max_records_per_job
        self._max_index_records = cron_max_index_records
        # Directory setup does synchronous filesystem I/O, so a caller on an
        # event loop MUST defer it and run prepare() in a worker thread — see
        # prepare() and CronService.create(). Deferred starts DISABLED so a read
        # racing the prepare degrades rather than touching an unprepared store.
        self._prepared = False
        self._enabled = False
        if not _defer_prepare:
            self.prepare()

    @property
    def enabled(self) -> bool:
        """False when the history directory is unusable, or not yet prepared.

        Every read returns empty and every write is dropped, so a caller never
        has to branch on it: history degrades, scheduling does not.
        """
        return self._enabled

    def prepare(self) -> None:
        """Resolve whether history is usable. Blocking; idempotent.

        Off the event loop only. ``CronService.create()`` runs this via
        ``asyncio.to_thread``, mirroring how it defers ``_load()``.
        """
        if self._prepared:
            return
        self._prepared = True
        self._enabled = self._prepare_dir()

    def _prepare_dir(self) -> bool:
        """Ensure the history directory is usable. Never raises.

        Returns True when records can be persisted, False to disable history.
        """
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            return True
        except OSError as exc:
            # A denial scoped to this one leaf answers EPERM to BOTH os.mkdir
            # and the os.stat behind Path.is_dir(), and pathlib consults
            # is_dir() to decide whether exist_ok applies — so mkdir raises
            # even though the directory is already there. A failed mkdir
            # therefore does not by itself mean the store is unusable.
            if self._probe_usable():
                logger.debug(
                    "cron history: mkdir refused for existing %s (%s); the "
                    "store's own syscalls work, continuing",
                    self._dir,
                    exc,
                )
                return True
            logger.warning(
                "cron history disabled: %s is unusable (%s). Scheduling is "
                "unaffected; run records will not be persisted.",
                self._dir,
                exc,
            )
            return False

    def _probe_usable(self) -> bool:
        """True when the syscalls this store's own paths depend on all work.

        Opening the lock file is NOT sufficient evidence that a store whose
        mkdir was refused is usable. The read and rotate paths go through
        ``Path.exists()`` (an ``os.stat``) and ``Path.glob()`` (a directory
        scan), and pathlib RE-RAISES EPERM out of both rather than reporting
        False — ``_ignore_error`` covers ENOENT/ENOTDIR/EBADF/ELOOP, not
        EPERM. A probe that only opened the lock file therefore reported a
        stat-denied directory as usable, and ``rotate_all()`` — awaited
        unguarded by ``CronService.start()`` — then raised straight back out
        of service startup, which is the failure this class exists to prevent.

        So require the two capabilities that discriminate: stat the directory
        and open the lock file. Both are O(1) and fail fast. Enumerating the
        directory is deliberately NOT part of the probe — its cost grows with
        the number of jobs, and ``_degrade()`` already turns a directory-scan
        failure at runtime into disabled history rather than a raise, so
        proving ``glob()`` works up front buys nothing.
        """
        try:
            os.stat(self._dir)
            fd = os.open(str(self._lock_path()), os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError:
            return False
        os.close(fd)
        return True

    def _degrade(self, operation: str, exc: OSError) -> None:
        """Handle a runtime failure. Never raises.

        Only a DENIAL disables the store. A denial is a standing condition — the
        sandbox profile that refused us will refuse us for this process's whole
        life — so continuing to attempt writes just logs the same error per run.

        Every other ``OSError`` is treated as transient and costs ONE record:
        a full disk, an fd exhaustion, or a transient I/O error clears on its
        own, and disabling history for the process's remaining lifetime over a
        momentary ENOSPC would lose every subsequent run's record for no reason
        — strictly worse than the pre-existing behaviour, where each call site
        caught its own failure and the next run wrote normally.
        """
        if exc.errno in _DENIAL_ERRNOS:
            if self._enabled:
                self._enabled = False
                logger.warning(
                    "cron history disabled: %s was denied on %s (%s). Scheduling "
                    "is unaffected; run records will no longer be persisted.",
                    operation,
                    self._dir,
                    exc,
                )
            return
        logger.warning(
            "cron history: %s failed on %s (%s); dropping this record and "
            "staying enabled. Scheduling is unaffected.",
            operation,
            self._dir,
            exc,
        )

    def _job_path(self, job_id: str) -> Path:
        path = (self._dir / f"{job_id}.jsonl").resolve()
        if path.parent != self._dir.resolve():
            raise ValueError(f"Path traversal blocked: {job_id!r}")
        return path

    def _lock_path(self) -> Path:
        return self._dir / ".history.lock"

    def _lock(self) -> int:
        """Acquire advisory lock, return fd."""
        fd = os.open(str(self._lock_path()), os.O_WRONLY | os.O_CREAT, 0o600)
        platform_compat.acquire_lock(fd, exclusive=True)
        return fd

    def _unlock(self, fd: int) -> None:
        platform_compat.release_lock(fd)
        os.close(fd)

    async def append(self, record: CronRunRecord) -> None:
        """Write record to job file and index."""
        # Cap fields
        record.summary = record.summary[:self._summary_cap]
        if len(record.trace) > self._trace_cap:
            record.trace = record.trace[:self._trace_cap] + "\n...[truncated]"

        if not self._enabled:
            return
        try:
            await asyncio.to_thread(self._append_sync, record)
        except OSError as exc:
            self._degrade("append", exc)

    def _append_sync(self, record: CronRunRecord) -> None:
        fd = self._lock()
        try:
            job_path = self._job_path(record.job_id)
            wfd = os.open(str(job_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(wfd, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(include_trace=True)) + "\n")
            ifd = os.open(str(self._index_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(ifd, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(include_trace=False)) + "\n")
        finally:
            self._unlock(fd)

    async def get_job_history(
        self, job_id: str, offset: int = 0, limit: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        """Return (records_without_trace, total_count) for a job, newest first."""
        if not self._enabled:
            return [], 0
        try:
            return await asyncio.to_thread(self._get_job_history_sync, job_id, offset, limit)
        except OSError as exc:
            self._degrade("get_job_history", exc)
            return [], 0

    # Reads are lock-free (eventually-consistent): a concurrent append may
    # produce a partial final line, silently skipped by the JSONDecodeError handler.
    def _get_job_history_sync(
        self, job_id: str, offset: int, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        job_path = self._job_path(job_id)
        if not job_path.exists():
            return [], 0
        try:
            lines = job_path.read_text(encoding="utf-8").strip().splitlines()
        except FileNotFoundError:
            return [], 0
        total = len(lines)
        lines.reverse()
        page = lines[offset : offset + limit]
        results = []
        for line in page:
            try:
                d = json.loads(line)
                d.pop("trace", None)
                results.append(d)
            except json.JSONDecodeError:
                continue
        return results, total

    async def get_all_history(
        self, offset: int = 0, limit: int = 20, job_id: str | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        """Return records from global index, newest first, optionally filtered."""
        if not self._enabled:
            return [], 0
        try:
            return await asyncio.to_thread(self._get_all_history_sync, offset, limit, job_id)
        except OSError as exc:
            self._degrade("get_all_history", exc)
            return [], 0

    # Reads are lock-free (eventually-consistent): a concurrent append may
    # produce a partial final line, silently skipped by the JSONDecodeError handler.
    def _get_all_history_sync(
        self, offset: int, limit: int, job_id: str | None
    ) -> tuple[list[dict[str, Any]], int]:
        if not self._index_path.exists():
            return [], 0
        try:
            lines = self._index_path.read_text(encoding="utf-8").strip().splitlines()
        except FileNotFoundError:
            return [], 0
        if job_id:
            filtered = []
            for line in lines:
                try:
                    if json.loads(line).get("job_id") == job_id:
                        filtered.append(line)
                except json.JSONDecodeError:
                    continue
            lines = filtered
        total = len(lines)
        lines.reverse()
        page = lines[offset : offset + limit]
        results = []
        for line in page:
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return results, total

    async def get_run_detail(self, job_id: str, run_id: str) -> dict[str, Any] | None:
        """Return full record (with trace) for a specific run."""
        if not self._enabled:
            return None
        try:
            return await asyncio.to_thread(self._get_run_detail_sync, job_id, run_id)
        except OSError as exc:
            self._degrade("get_run_detail", exc)
            return None

    def _get_run_detail_sync(self, job_id: str, run_id: str) -> dict[str, Any] | None:
        job_path = self._job_path(job_id)
        if not job_path.exists():
            return None
        try:
            lines = job_path.read_text(encoding="utf-8").strip().splitlines()
        except FileNotFoundError:
            return None
        for line in lines:
            try:
                d = json.loads(line)
                if d.get("run_id") == run_id:
                    return d
            except json.JSONDecodeError:
                continue
        return None

    async def rotate(self, job_id: str) -> None:
        """Trim job file to last _MAX_RECORDS_PER_JOB records."""
        if not self._enabled:
            return
        try:
            await asyncio.to_thread(self._rotate_sync, job_id)
        except OSError as exc:
            self._degrade("rotate", exc)

    def _rotate_sync(self, job_id: str) -> None:
        job_path = self._job_path(job_id)
        if not job_path.exists():
            return
        fd = self._lock()
        try:
            lines = job_path.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) <= self._max_records_per_job:
                return
            keep = lines[-self._max_records_per_job:]
            tmp = job_path.with_suffix(".tmp")
            wfd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(wfd, "w", encoding="utf-8") as f:
                f.write("\n".join(keep) + "\n")
            os.replace(tmp, job_path)
        finally:
            self._unlock(fd)

    async def rotate_all(self) -> None:
        """Rotate all job files and trim the global index."""
        if not self._enabled:
            return
        try:
            await asyncio.to_thread(self._rotate_all_sync)
        except OSError as exc:
            self._degrade("rotate_all", exc)

    def _rotate_all_sync(self) -> None:
        fd = self._lock()
        try:
            for p in self._dir.glob("*.jsonl"):
                if p.name == "_index.jsonl":
                    continue
                lines = p.read_text(encoding="utf-8").strip().splitlines()
                if len(lines) > self._max_records_per_job:
                    keep = lines[-self._max_records_per_job:]
                    tmp = p.with_suffix(".tmp")
                    wfd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(wfd, "w", encoding="utf-8") as f:
                        f.write("\n".join(keep) + "\n")
                    os.replace(tmp, p)
            if self._index_path.exists():
                lines = self._index_path.read_text(encoding="utf-8").strip().splitlines()
                if len(lines) > self._max_index_records:
                    keep = lines[-self._max_index_records:]
                    tmp = self._index_path.with_suffix(".tmp")
                    wfd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(wfd, "w", encoding="utf-8") as f:
                        f.write("\n".join(keep) + "\n")
                    os.replace(tmp, self._index_path)
        finally:
            self._unlock(fd)

    async def delete_job_history(self, job_id: str) -> bool:
        """Remove all history for a job."""
        if not self._enabled:
            return False
        try:
            return await asyncio.to_thread(self._delete_job_history_sync, job_id)
        except OSError as exc:
            self._degrade("delete_job_history", exc)
            return False

    def _delete_job_history_sync(self, job_id: str) -> bool:
        fd = self._lock()
        try:
            job_path = self._job_path(job_id)
            removed = job_path.exists()
            if removed:
                job_path.unlink()
            if self._index_path.exists():
                lines = self._index_path.read_text(encoding="utf-8").strip().splitlines()
                filtered = []
                for line in lines:
                    try:
                        if json.loads(line).get("job_id") != job_id:
                            filtered.append(line)
                    except json.JSONDecodeError:
                        continue
                if len(filtered) != len(lines):
                    tmp = self._index_path.with_suffix(".tmp")
                    content = "\n".join(filtered) + "\n" if filtered else ""
                    wfd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(wfd, "w", encoding="utf-8") as f:
                        f.write(content)
                    os.replace(tmp, self._index_path)
            return removed
        finally:
            self._unlock(fd)

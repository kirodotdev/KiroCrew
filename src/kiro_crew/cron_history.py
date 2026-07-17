"""Cron execution history store.

Persists run records as JSONL files per job, with a global index for
cross-job queries.  Uses fcntl advisory locking for cross-process safety.

Storage layout:
    ~/.kirocrew/cron-history/
        {job_id}.jsonl      — full records (including trace) for one job
        _index.jsonl        — lightweight index (no trace) for list queries
"""

from __future__ import annotations

import asyncio
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


@dataclass
class CronRunRecord:
    """Single cron execution record."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    job_id: str = ""
    trigger: str = "scheduled"  # "scheduled" | "manual"
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: int = 0
    status: str = "success"  # "success" | "failure" | "timeout"
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
    ):
        self._dir = (base_dir or config_dir()) / "cron-history"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "_index.jsonl"
        self._summary_cap = cron_summary_cap
        self._trace_cap = cron_trace_cap_kb * 1024
        self._max_records_per_job = cron_max_records_per_job
        self._max_index_records = cron_max_index_records

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

        await asyncio.to_thread(self._append_sync, record)

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
        return await asyncio.to_thread(self._get_job_history_sync, job_id, offset, limit)

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
        return await asyncio.to_thread(self._get_all_history_sync, offset, limit, job_id)

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
        return await asyncio.to_thread(self._get_run_detail_sync, job_id, run_id)

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
        await asyncio.to_thread(self._rotate_sync, job_id)

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
        await asyncio.to_thread(self._rotate_all_sync)

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
        return await asyncio.to_thread(self._delete_job_history_sync, job_id)

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

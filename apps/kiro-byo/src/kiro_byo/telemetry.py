"""Metadata-only telemetry for the KiroCrew ACP backend.

This is deliberately *not* a generic proxy usage ledger.  Kiro BYO's primary
client is KiroCrew, and these records exist to explain backend behaviour: which
provider/model actually served a Crew turn, how long the turn spent upstream or
in tools, and which token counters the upstream chose to report.

Prompt text, assistant text, tool arguments/results, API keys and full workspace
paths are never written here.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .paths import app_home


@dataclass(frozen=True)
class TurnTelemetry:
    timestamp: float
    session_id: str
    outcome: str
    requested_model: str
    provider_id: str
    served_model: str
    effort: str
    total_ms: float
    first_visible_ms: float
    upstream_ms: float
    tool_ms: float
    mcp_wait_ms: float
    tool_calls: int
    native_calls: int
    tool_search_calls: int
    active_tools: int
    tool_schema_bytes: int
    stream_chunks: int
    stream_chars: int
    thinking_mode: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cache_usage_reported: bool = False
    cache_write_usage_reported: bool = False
    reasoning_usage_reported: bool = False
    error_kind: str = ""
    client: str = "kiro-crew"
    schema_version: int = 1


class TurnTelemetryStore:
    """Append/read a bounded metadata-only turn diagnostic ledger.

    The ledger is deliberately finite.  Diagnostics are for recent backend
    troubleshooting, not long-term analytics, so retaining an unbounded JSONL
    file would be both surprising to users and unnecessary disk growth.
    """

    DEFAULT_MAX_RECORDS = 500

    # Routes construct a fresh TurnTelemetryStore per request, so a per-instance
    # lock would not serialize concurrent append/trim/clear on the same file —
    # _trim_locked's read-then-replace could drop a just-appended line. Share one
    # lock per resolved path across all instances in this process.
    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Path | None = None, *, max_records: int = DEFAULT_MAX_RECORDS) -> None:
        self.path = path or (app_home() / "data" / "telemetry" / "turns.jsonl")
        self.max_records = max(20, min(int(max_records), 10_000))
        try:
            key = str(self.path.resolve())
        except OSError:
            key = str(self.path)
        with TurnTelemetryStore._locks_guard:
            self._lock = TurnTelemetryStore._locks.setdefault(key, threading.Lock())

    def record(self, turn: TurnTelemetry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(turn)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
            self._trim_locked()

    def _trim_locked(self) -> None:
        """Keep at most ``max_records`` valid/invalid lines, preserving the newest tail."""
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                tail = deque(handle, maxlen=self.max_records + 1)
        except OSError:
            return
        if len(tail) <= self.max_records:
            return
        kept = list(tail)[-self.max_records :]
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.writelines(kept)
            tmp.replace(self.path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def clear(self) -> int:
        """Delete the diagnostic ledger and return the number of records removed."""
        with self._lock:
            if not self.path.exists():
                return 0
            count = 0
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    count = sum(1 for _ in handle)
            except OSError:
                count = 0
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                # Truncation is a safe fallback when unlink is blocked by host
                # filesystem semantics or a transient scanner on Windows.
                try:
                    self.path.write_text("", encoding="utf-8")
                except OSError:
                    return 0
            return count

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        if not self.path.exists():
            return []
        # Read only a bounded tail so a long-running install does not make the
        # UI allocate the entire ledger.
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    raw_lines = deque(handle, maxlen=limit)
            except OSError:
                return []
        out: list[dict[str, Any]] = []
        for line in raw_lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def summary(self, limit: int = 200) -> dict[str, Any]:
        rows = self.recent(limit)
        success = sum(1 for row in rows if row.get("outcome") == "success")
        failed = sum(1 for row in rows if row.get("outcome") == "error")
        totals = [float(row.get("total_ms") or 0.0) for row in rows if row.get("total_ms") is not None]
        firsts = [
            float(row.get("first_visible_ms") or 0.0)
            for row in rows
            if float(row.get("first_visible_ms") or -1.0) >= 0.0
        ]
        input_tokens = sum(int(row.get("input_tokens") or 0) for row in rows)
        output_tokens = sum(int(row.get("output_tokens") or 0) for row in rows)
        cached_tokens = sum(int(row.get("cached_tokens") or 0) for row in rows)
        cache_write_tokens = sum(int(row.get("cache_write_tokens") or 0) for row in rows)
        reasoning_tokens = sum(int(row.get("reasoning_tokens") or 0) for row in rows)
        # Old records predate the explicit flags. A positive counter proves the
        # provider reported it; a historical zero remains unknown rather than
        # being mislabelled as a real 0% cache hit rate.
        cache_reported_turns = sum(
            1 for row in rows
            if bool(row.get("cache_usage_reported")) or int(row.get("cached_tokens") or 0) > 0
        )
        cache_write_reported_turns = sum(
            1 for row in rows
            if bool(row.get("cache_write_usage_reported")) or int(row.get("cache_write_tokens") or 0) > 0
        )
        reasoning_reported_turns = sum(
            1 for row in rows
            if bool(row.get("reasoning_usage_reported")) or int(row.get("reasoning_tokens") or 0) > 0
        )
        cache_hit_rate = (cached_tokens / input_tokens) if input_tokens > 0 and cache_reported_turns > 0 else None
        return {
            "turns": len(rows),
            "success": success,
            "failed": failed,
            "median_total_ms": _median(totals),
            "median_first_visible_ms": _median(firsts),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cache_usage_reported_turns": cache_reported_turns,
            "cache_write_usage_reported_turns": cache_write_reported_turns,
            "reasoning_usage_reported_turns": reasoning_reported_turns,
            "cache_hit_rate": cache_hit_rate,
            "window": len(rows),
        }


def _median(values: list[float]) -> float:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return 0.0
    middle = len(clean) // 2
    if len(clean) % 2:
        return clean[middle]
    return (clean[middle - 1] + clean[middle]) / 2.0


def now_timestamp() -> float:
    return time.time()

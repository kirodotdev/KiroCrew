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
import os
import math
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .paths import app_home
from .state_io import _file_transaction_lock


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
    error_category: str = ""
    # Filename/function/line only. No stack locals, secrets or prompt text.
    error_site: str = ""
    transport_retries: int = 0
    fallbacks: int = 0
    cancel_source: str = ""
    # Durable Run id is the stable correlation key between the compact turn
    # summary and the lower-level runtime event ledger. Older rows simply lack
    # it and remain readable.
    run_id: str = ""
    turn_id: str = ""
    # Routing metadata is content-free and lets Tiga learn from its own recent
    # runtime without another Router LLM. Empty values keep old/concrete turns
    # backwards compatible.
    mode_pack: str = ""
    mode_type: str = ""
    task_profile: str = ""
    routing_score: float = 0.0
    routing_confidence: str = ""
    # Diagnostics that explain *why* a turn used a Type/model. ``timestamp``
    # remains the completion timestamp for backward compatibility.
    started_at: float = 0.0
    completed_at: float = 0.0
    selection_kind: str = ""
    selection_source: str = ""
    selection_requested: str = ""
    thinking_preference: str = ""
    client: str = "kiro-crew"
    # Bounded, content-free feature vector recorded *with* the completed turn.
    # This is not a separate ledger: clearing or aging out a turn clears its
    # routing evidence, and dashboard results cannot outlive their source.
    route_signals: dict[str, float] | None = None
    route_diagnostics: dict[str, Any] | None = None
    # Explicit Crew lightweight-role provenance also covers pinned models;
    # selection_source=background_auto only covers Auto routing.
    is_background_role: bool = False
    schema_version: int = 5


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
        with self._lock, _file_transaction_lock(self.path):
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
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
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
        with self._lock, _file_transaction_lock(self.path):
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
        limit = max(1, min(int(limit), self.max_records, 500))
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
        cancelled = sum(1 for row in rows if row.get("outcome") == "cancelled")
        interrupted = sum(1 for row in rows if row.get("outcome") == "interrupted")
        error_categories: dict[str, int] = {}
        for row in rows:
            category = str(row.get("error_category") or "").strip()
            if row.get("outcome") == "error" and category:
                error_categories[category] = error_categories.get(category, 0) + 1
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
        # Rolling 24-hour buckets. Hour boundaries are relative to the current
        # hour and empty bins are explicit (no fabricated smoothing).
        now_hour = int(time.time() // 3600)
        buckets = [
            {"hour": (now_hour - 23 + i) * 3600, "requests": 0, "success": 0,
             "failed": 0, "total_ms": 0.0, "observed_ms": 0}
            for i in range(24)
        ]
        for row in rows:
            hour = int(float(row.get("timestamp") or 0.0) // 3600)
            index = hour - (now_hour - 23)
            if 0 <= index < 24:
                bucket = buckets[index]
                bucket["requests"] += 1
                outcome = str(row.get("outcome") or "")
                if outcome == "success":
                    bucket["success"] += 1
                elif outcome in {"error", "failed"}:
                    bucket["failed"] += 1
                # Keep the latency trend consistent with the successful-turn
                # average above it. Cancelled/failed turns can run for a long
                # time without producing a final answer; their elapsed time is
                # diagnostic data, not a completed response latency sample.
                if outcome == "success":
                    latency = float(row.get("total_ms") or 0.0)
                    if math.isfinite(latency) and latency > 0:
                        bucket["total_ms"] += latency
                        bucket["observed_ms"] += 1
        for bucket in buckets:
            bucket["avg_ms"] = (
                bucket["total_ms"] / bucket["observed_ms"]
                if bucket["observed_ms"] else None
            )
            del bucket["total_ms"]
            del bucket["observed_ms"]
        last24 = [row for row in rows if float(row.get("timestamp") or 0.0) >= (now_hour - 23) * 3600]
        completed24 = [row for row in last24 if str(row.get("outcome") or "") == "success"]
        durations24 = [float(row.get("total_ms")) for row in completed24
                       if isinstance(row.get("total_ms"), (int, float)) and float(row["total_ms"]) > 0]
        last24_stats = {
            "requests": len(last24),
            "success": len(completed24),
            "failed": sum(1 for row in last24 if str(row.get("outcome") or "") in {"error", "failed"}),
            "avg_total_ms": sum(durations24) / len(durations24) if durations24 else None,
            "median_upstream_ms": _median([float(row.get("upstream_ms") or 0.0) for row in completed24 if float(row.get("upstream_ms") or 0.0) > 0]) or None,
            "median_tool_ms": _median([float(row.get("tool_ms") or 0.0) for row in completed24 if float(row.get("tool_ms") or 0.0) > 0]) or None,
        }
        provider_health: dict[str, dict[str, Any]] = {}
        for row in rows:
            provider_id = str(row.get("provider_id") or "").strip()
            served_model = str(row.get("served_model") or "").strip()
            if not provider_id or not served_model:
                continue
            key = f"{provider_id}::{served_model}"
            item = provider_health.setdefault(key, {
                "provider_id": provider_id, "model": served_model, "turns": 0,
                "success": 0, "failed": 0, "cancelled": 0,
                "transport_errors": 0, "transport_retries": 0, "fallbacks": 0,
            })
            item["turns"] += 1
            outcome = str(row.get("outcome") or "")
            if outcome == "success":
                item["success"] += 1
            elif outcome == "error":
                item["failed"] += 1
            elif outcome == "cancelled":
                item["cancelled"] += 1
            if str(row.get("error_category") or "") == "transport":
                item["transport_errors"] += 1
            item["transport_retries"] += int(row.get("transport_retries") or 0)
            item["fallbacks"] += int(row.get("fallbacks") or 0)
        for item in provider_health.values():
            turns = int(item["turns"])
            item["success_rate"] = (float(item["success"]) / turns) if turns else 0.0

        return {
            "turns": len(rows),
            "success": success,
            "failed": failed,
            "cancelled": cancelled,
            "interrupted": interrupted,
            "error_categories": error_categories,
            "provider_health": sorted(
                provider_health.values(),
                key=lambda item: (-int(item["turns"]), str(item["provider_id"]), str(item["model"])),
            ),
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
            "hourly": buckets,
            "last24": last24_stats,
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

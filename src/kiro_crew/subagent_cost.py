"""Append-only learned per-agent cost store for dynamic sub-agent sizing.

One JSONL line per completed run, written via atomic ``O_APPEND`` (race-free,
no lock). ``read_cap_costs`` feeds the memory-sized parallel count (the
median across agents of each agent's p50 over its last N samples, less one
reserve of ``max(per-agent p90)``): the TaskRunner's auto value always, and
the subagent auto cap only when the spawn floor is disabled
(``agent.spawn_min_memory_gb <= 0``). The log is FIFO-trimmed to the last N
per agent both at startup and periodically.

See ``dynamic-subagent-sizing.md`` §4.2 (storage) / §4.3 (aggregation).
"""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
import tempfile
import time
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config.paths import config_dir
from kiro_crew.jsonl_util import RECORD_CAP, UnreadableRecord, strict_records

logger = logging.getLogger(__name__)

_DEFAULT_AGENT = "kirocrew"  # key for unnamed/default-agent runs (§4.3 keying)
_DEFAULT_WINDOW = 50  # samples retained + considered per agent
_DEFAULT_MIN_SAMPLES = 3  # before trusting learned over the configured fallback
_DEFAULT_PERCENTILE = 0.90
# How long a bucket's learned settled figure lasts after its NEWEST reading. A
# bucket priced out of admission records no new readings, so a figure learned under
# a heavier roster would otherwise hold its starts back for good; past this age the
# bucket is priced at the unlearned default until it relearns. Any reading inside
# the horizon keeps the whole window, so a rarely run agent keeps its figure.
SETTLED_SAMPLE_MAX_AGE_SECS = 7 * 24 * 3600
# Bounds on what the agent-writable log can put into memory. While PARSING, an
# over-long bucket key is dropped, each bucket keeps at most ``window`` values and
# at most _PARSE_BUCKET_CEILING buckets are held, first seen first kept. What is
# RETURNED is the heaviest _MAX_BUCKETS of those (``cap_buckets``), so the parse
# ceiling never decides which agents are priced.
_BUCKET_KEY_CAP = 128
_MAX_BUCKETS = 64
_PARSE_BUCKET_CEILING = 1024

# Longest single RECORD the reader will materialise. The read feeds
# compact_cost_log's rewrite of the same file, so an over-cap record aborts the
# read rather than being skipped. Named here so a test can move the dial.
_RECORD_CAP = RECORD_CAP


def _cost_log_path() -> Path:
    return config_dir() / "subagents" / "cost_samples.jsonl"


def append_cost_sample(
    agent: str,
    mem_gb: float,
    cpu_cores: float,
    *,
    shared: bool = False,
    settled_gb: float = 0.0,
) -> None:
    """Append one ``{agent, mem_gb, cpu_cores, ts[, shared][, settled_gb]}`` line.

    ``shared`` marks a run inside a shared runtime: its figures are a per-session
    share of that runtime. :func:`compact_cost_log` keeps one window per
    ``(agent, shared)`` so shared samples never evict dedicated history. Written
    only when true; a record without it reads as dedicated.

    ``settled_gb`` is the run's settled-runtime reading (the first quiet subtree
    sample after its session answered, no tool in flight). The admission gate's
    dedicated start projection learns from it, and so does :func:`read_cap_costs`
    when the spawn floor is disabled. Written only when measured. ``mem_gb`` stays
    the whole-run peak that sizes the TaskRunner's auto value.
    """
    if mem_gb <= 0 and cpu_cores <= 0 and settled_gb <= 0:
        return  # nothing was measured
    rec: dict[str, object] = {
        "agent": agent or _DEFAULT_AGENT,  # normalize empty → default agent
        "mem_gb": round(float(mem_gb), 4),
        "cpu_cores": round(float(cpu_cores), 4),
        "ts": int(time.time()),
    }
    if shared:
        rec["shared"] = True
    if settled_gb > 0:
        rec["settled_gb"] = round(float(settled_gb), 4)
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    path = _cost_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND makes each small write atomic across concurrent agents.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        logger.debug("Failed to append cost sample", exc_info=True)


class _ReadStatus:
    """What a streamed read learned about the log besides its records.

    ``complete`` is False when :func:`~kiro_crew.jsonl_util.strict_records`
    refused a record (over :data:`_RECORD_CAP`, or undecodable) and the read
    stopped there, or when the log exists but could not be read: either way the
    unseen records are MISSING, not absent. An absent log is complete and empty.
    """

    __slots__ = ("complete",)

    def __init__(self) -> None:
        self.complete = True


def _iter_samples(status: _ReadStatus) -> Iterator[dict]:
    """Yield the log's records one at a time, never holding the file in memory.

    The log is agent-writable, so retention must not scale with the file:
    consumers fold each record into bounded per-bucket state. Corrupt lines are
    skipped; a refused record or a failed open marks *status* incomplete and
    ends the read.
    """
    try:
        path = _cost_log_path()
        with open(path, "rb") as fh:
            try:
                for raw in strict_records(fh, path, cap=_RECORD_CAP):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue  # skip corrupt line, keep going
                    if isinstance(rec, dict):
                        yield rec
            except UnreadableRecord:
                status.complete = False
                # "unreadable", not "over-cap": UnreadableRecord also covers
                # invalid UTF-8.
                logger.warning("cost log has a record that could not be read; read as incomplete")
    except FileNotFoundError:
        return  # absent: complete and empty
    except OSError:
        # Present but not readable right now (EACCES, EMFILE, EIO): the
        # records exist and were not seen, which is incomplete, not empty.
        status.complete = False
        logger.warning("cost log present but could not be read; read as incomplete", exc_info=True)


def _read_samples_checked() -> tuple[list[dict], bool]:
    """Every record, materialised, plus the completeness flag (see
    :class:`_ReadStatus`). For tests and small utilities only: the production
    readers stream through :func:`_iter_samples`."""
    status = _ReadStatus()
    rows = list(_iter_samples(status))
    return rows, status.complete


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (nearest-rank for tiny lists)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = pct * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (rank - lo)


def _group_by_agent(
    samples: Iterable[dict],
    key: str,
    *,
    window: int = _DEFAULT_WINDOW,
    dedicated_only: bool = False,
    not_before: float | None = None,
    not_after: float | None = None,
) -> dict[str, list[float]]:
    """Bucket the values of *key* by agent, bounded WHILE streaming.

    The log is agent-writable, so a read holds at most ``window`` values per
    bucket (the tail the percentile reads) and at most ``_PARSE_BUCKET_CEILING``
    buckets, first seen first kept; records past the ceiling are dropped and one
    WARNING counts them.
    ``dedicated_only`` skips records marked ``shared``: a shared run's reading is
    a share of a runtime other sessions also used, not one process's size.
    ``not_before`` (epoch seconds) drops every bucket whose newest record was
    written before it. A record whose ``ts`` is not a finite number, or is later
    than ``not_after`` (the log is agent-writable, and a future stamp would keep
    a bucket recent for good), counts as a value but cannot show the bucket is
    recent.
    """
    by_agent: dict[str, deque[float]] = {}
    newest: dict[str, float] = {}
    overflow = 0
    for rec in samples:
        if dedicated_only and rec.get("shared") is True:
            continue
        agent = str(rec.get("agent") or _DEFAULT_AGENT)
        if len(agent) > _BUCKET_KEY_CAP:
            continue  # not an agent name; the log is agent-writable
        v = rec.get(key)
        if not (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0):
            continue
        vals = by_agent.get(agent)
        if vals is None:
            if len(by_agent) >= _PARSE_BUCKET_CEILING:
                overflow += 1
                continue
            vals = by_agent[agent] = deque(maxlen=max(1, window))
        vals.append(float(v))
        if not_before is not None:
            ts = rec.get("ts")
            # Compared as given, never coerced: an agent-written integer too large
            # for a float must not raise here.
            if (
                (type(ts) is int or (type(ts) is float and math.isfinite(ts)))
                and (not_after is None or ts <= not_after)
                and ts > newest.get(agent, not_before - 1)
            ):
                newest[agent] = ts
    if not_before is not None:
        for agent in [a for a in by_agent if newest.get(a, not_before - 1) < not_before]:
            del by_agent[agent]
    if overflow:
        logger.warning(
            "cost log holds more than %d distinct buckets; %d record(s) beyond the ceiling "
            "were not read",
            _PARSE_BUCKET_CEILING,
            overflow,
        )
    return {agent: list(vals) for agent, vals in by_agent.items()}


def read_learned_costs_checked(
    key: str,
    *,
    dedicated_only: bool = False,
    window: int = _DEFAULT_WINDOW,
    min_samples: int = _DEFAULT_MIN_SAMPLES,
    percentile: float = _DEFAULT_PERCENTILE,
    max_age_secs: float | None = None,
    now: float | None = None,
) -> tuple[dict[str, float], bool]:
    """Per-agent p90 of the last ``window`` samples for *key*, plus completeness.

    Agents with fewer than ``min_samples`` are omitted; empty when nothing
    qualifies. The log is streamed, never held whole; a refused record ends the
    read and the buckets seen up to it are what this returns, with the bool
    (:class:`_ReadStatus`'s ``complete``) saying so. ``dedicated_only`` skips
    shared runs' records. ``settled_gb`` read this way is the per-bucket settled
    runtime size the admission gate prices a dedicated start at; ``mem_gb`` is
    the whole-run peak that sizes the TaskRunner's auto value. ``max_age_secs`` leaves
    out every bucket whose newest sample is older than that before *now*
    (default: the wall clock); a bucket with any recent sample keeps its window.
    """
    status = _ReadStatus()
    not_before = not_after = None
    if max_age_secs is not None:
        not_after = time.time() if now is None else now
        not_before = not_after - max(0.0, max_age_secs)
    by_agent = _group_by_agent(
        _iter_samples(status),
        key,
        window=window,
        dedicated_only=dedicated_only,
        not_before=not_before,
        not_after=not_after,
    )
    out = {
        agent: _percentile(vals, percentile)
        for agent, vals in by_agent.items()
        if len(vals) >= min_samples
    }
    return cap_buckets(out), status.complete


def learned_settled_for(costs: Mapping[str, float] | None, bucket: str) -> float | None:
    """The learned settled RSS (GiB) for ONE bucket, or None; never another's.

    *bucket* is :func:`kiro_crew.subagent._cost_bucket`'s key; empty normalizes to
    the default agent exactly as :func:`append_cost_sample` does on write.
    """
    if not costs:
        return None
    return costs.get(bucket or _DEFAULT_AGENT)


def cap_buckets(costs: Mapping[str, float]) -> dict[str, float]:
    """The heaviest ``_MAX_BUCKETS`` of *costs*: the bound a held map stays under."""
    if len(costs) <= _MAX_BUCKETS:
        return dict(costs)
    heaviest = sorted(costs.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_BUCKETS]
    return dict(heaviest)


def read_cap_costs(
    key: str,
    *,
    window: int = _DEFAULT_WINDOW,
    min_samples: int = _DEFAULT_MIN_SAMPLES,
    dedicated_only: bool = False,
    max_age_secs: float | None = None,
    now: float | None = None,
) -> tuple[float | None, float | None]:
    """Return ``(typical, heavy_peak)`` for *key* from one pass over the log.

    Per agent, over the last ``window`` samples (only if it has at least
    ``min_samples``): ``typical`` is the median across agents of each agent's
    p50, and ``heavy_peak`` is the max across agents of each agent's p90.
    Both are ``None`` when no agent qualifies, and the caller falls back to the
    configured first-boot cost. Medians at both levels keep ``typical`` steady
    against a few heavy runs or one heavy agent. Each qualifying agent counts
    once however often it runs, so the busiest agent's mix cannot set every
    slot's price; the ``heavy_peak`` reserve covers the heavy one.
    ``dedicated_only`` and ``max_age_secs`` filter exactly as in
    :func:`read_learned_costs_checked`.
    """
    not_before = not_after = None
    if max_age_secs is not None:
        not_after = time.time() if now is None else now
        not_before = not_after - max(0.0, max_age_secs)
    by_agent = _group_by_agent(
        _iter_samples(_ReadStatus()),
        key,
        window=window,
        dedicated_only=dedicated_only,
        not_before=not_before,
        not_after=not_after,
    )
    qualifying = [vals for vals in by_agent.values() if len(vals) >= min_samples]
    if not qualifying:
        return None, None
    typical = statistics.median(_percentile(vals, 0.5) for vals in qualifying)
    heavy_peak = max(_percentile(vals, _DEFAULT_PERCENTILE) for vals in qualifying)
    return typical, heavy_peak


def compact_cost_log(window: int = _DEFAULT_WINDOW) -> None:
    """FIFO-trim the log to the last ``window`` samples per (agent, shared) (atomic).

    Safe to call anytime; a sample appended in the brief read→replace window
    may be dropped, which is harmless for an approximate p90. No-op when the
    log is already within bounds.

    Fails closed on an incomplete read: this REPLACES the log with the records it
    parsed, so trimming from a partial read would delete the records the reader
    never saw. Untrimmed costs bounded disk; compacting would cost data.
    """
    # One window per (agent, shared), so shared samples never evict dedicated ones.
    # Streamed into bounded deques: the rewrite holds only the records it keeps.
    status = _ReadStatus()
    by_agent: dict[tuple[str, bool], deque[dict]] = {}
    total = 0
    for rec in _iter_samples(status):
        total += 1
        bucket = (str(rec.get("agent") or _DEFAULT_AGENT), rec.get("shared") is True)
        vals = by_agent.get(bucket)
        if vals is None:
            if len(by_agent) >= _PARSE_BUCKET_CEILING:
                # Beyond the ceiling the rewrite would DROP records it never
                # held; refuse to compact rather than lose them.
                logger.warning("cost log holds more buckets than the ceiling; skipping compaction")
                return
            vals = by_agent[bucket] = deque(maxlen=max(1, window))
        vals.append(rec)
    if not status.complete:
        logger.warning("cost log unreadable in full; skipping compaction to avoid data loss")
        return
    if total == 0:
        return
    kept: list[dict] = [rec for vals in by_agent.values() for rec in vals]
    if len(kept) >= total:
        return  # nothing to trim
    kept.sort(key=lambda r: r.get("ts", 0))  # preserve chronological order
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept)
    path = _cost_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            replace_with_retry(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        logger.debug("Failed to compact cost log", exc_info=True)

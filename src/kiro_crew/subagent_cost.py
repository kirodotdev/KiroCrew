"""Append-only learned per-agent cost store for dynamic sub-agent sizing.

One JSONL line per completed run, written via atomic ``O_APPEND`` (race-free,
no lock). The cap is computed at startup from ``read_learned_cost`` =
``max(per-agent p90)`` over the last N samples; the log is FIFO-trimmed to the
last N per agent both at startup and periodically.

See ``dynamic-subagent-sizing.md`` §4.2 (storage) / §4.3 (aggregation).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_DEFAULT_AGENT = "kirocrew"  # key for unnamed/default-agent runs (§4.3 keying)
_DEFAULT_WINDOW = 50  # samples retained + considered per agent
_DEFAULT_MIN_SAMPLES = 3  # before trusting learned over the configured fallback
_DEFAULT_PERCENTILE = 0.90


def _cost_log_path() -> Path:
    return config_dir() / "subagents" / "cost_samples.jsonl"


def append_cost_sample(agent: str, mem_gb: float, cpu_cores: float) -> None:
    """Append one ``{agent, mem_gb, cpu_cores, ts}`` line (atomic O_APPEND)."""
    if mem_gb <= 0 and cpu_cores <= 0:
        return  # nothing was measured
    rec = {
        "agent": agent or _DEFAULT_AGENT,  # normalize empty → default agent
        "mem_gb": round(float(mem_gb), 4),
        "cpu_cores": round(float(cpu_cores), 4),
        "ts": int(time.time()),
    }
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


def _read_samples() -> list[dict]:
    """Read all valid JSONL records; skip corrupt lines; [] if missing."""
    out: list[dict] = []
    try:
        with open(_cost_log_path(), encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue  # skip corrupt line, keep going
                if isinstance(rec, dict):
                    out.append(rec)
    except (FileNotFoundError, OSError):
        return []
    return out


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


def _group_by_agent(samples: list[dict], key: str) -> dict[str, list[float]]:
    by_agent: dict[str, list[float]] = {}
    for rec in samples:
        v = rec.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            by_agent.setdefault(str(rec.get("agent") or _DEFAULT_AGENT), []).append(float(v))
    return by_agent


def read_learned_cost(
    key: str,
    *,
    window: int = _DEFAULT_WINDOW,
    min_samples: int = _DEFAULT_MIN_SAMPLES,
    percentile: float = _DEFAULT_PERCENTILE,
) -> float | None:
    """Return ``max(per-agent p90)`` for *key* (``mem_gb``/``cpu_cores``), or None.

    Per agent, take the p90 of the last ``window`` samples (only if it has at
    least ``min_samples``), then the max across agents. Returns None when no
    agent qualifies — the caller falls back to the configured first-boot cost.
    A percentile is outlier-robust, so a single pathological run can't dominate.
    """
    by_agent = _group_by_agent(_read_samples(), key)
    best: float | None = None
    for vals in by_agent.values():
        recent = vals[-window:]
        if len(recent) < min_samples:
            continue
        p = _percentile(recent, percentile)
        if best is None or p > best:
            best = p
    return best


def compact_cost_log(window: int = _DEFAULT_WINDOW) -> None:
    """FIFO-trim the log to the last ``window`` samples per agent (atomic).

    Safe to call anytime; a sample appended in the brief read→replace window
    may be dropped, which is harmless for an approximate p90. No-op when the
    log is already within bounds.
    """
    samples = _read_samples()
    if not samples:
        return
    by_agent: dict[str, list[dict]] = {}
    for rec in samples:
        by_agent.setdefault(str(rec.get("agent") or _DEFAULT_AGENT), []).append(rec)
    kept: list[dict] = []
    for vals in by_agent.values():
        kept.extend(vals[-window:])
    if len(kept) >= len(samples):
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
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        logger.debug("Failed to compact cost log", exc_info=True)

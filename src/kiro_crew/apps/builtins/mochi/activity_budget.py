"""Activity budget — how much the background agent may spend, per tier.

This is deliberately a separate axis from ``mode``: ``mode`` is the pet's
personality (bubbles, moods, interruptions) and costs nothing; the activity
tier is the token budget for unattended agent runs. Neither may branch on the
other — a user making the pet chattier must not silently start paying more.

Each tier is a concrete contract the Settings UI can show verbatim:
  * ``max_spawns_per_hour`` — hard cap on background agent runs.
  * ``watch_min_interval_ms`` — a FLOOR under per-item watch intervals, not an
    override: an item asking for 5-minute checks keeps its schedule on any tier
    whose floor is ≤ 5min, and is honestly slowed (not silently dropped) on a
    stricter tier.
  * ``max_watch_batch`` — due items folded into ONE agent run. Raising the
    batch trades latency for tokens while keeping coverage, which is why the
    cheap tier gets a LARGER batch, not just a longer interval.

The plan horizon is NOT a tier knob: ``planned_until`` is written by the
planner agent itself, so promising it here would be a setting that does
nothing. The cap above bounds plan cycles the same as everything else.

``SpawnLedger`` is the budget's memory: hourly buckets persisted to the data
dir, so a restart cannot reset the meter, and the usage display reads the SAME
buckets the cap enforces — the number shown can never disagree with the number
limited.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

LEDGER_FILE = "mochi-spawn-ledger.json"

#: Hourly buckets kept for the usage display (7 days) plus slack.
_LEDGER_KEEP_HOURS = 8 * 24


@dataclass(frozen=True)
class ActivityBudget:
    """One tier's contract. Frozen: a budget is resolved, never mutated."""

    tier: str
    max_spawns_per_hour: int
    watch_min_interval_ms: int
    max_watch_batch: int


#: The tier table. "balanced" preserves the vendored constants' behaviour
#: (5-per-10min storm window ≈ 30/h worst case was the OLD ceiling; balanced
#: deliberately sits below it, and "active" restores it).
TIERS: dict[str, ActivityBudget] = {
    "economy": ActivityBudget(
        tier="economy",
        max_spawns_per_hour=4,
        watch_min_interval_ms=30 * 60_000,
        max_watch_batch=10,
    ),
    "balanced": ActivityBudget(
        tier="balanced",
        max_spawns_per_hour=12,
        watch_min_interval_ms=10 * 60_000,
        max_watch_batch=5,
    ),
    "active": ActivityBudget(
        tier="active",
        max_spawns_per_hour=30,
        watch_min_interval_ms=5 * 60_000,
        max_watch_batch=5,
    ),
}

#: "unlimited" is deliberately NOT in TIERS: it resolves to None, which
#: disengages the budget system entirely — the poller falls back to the
#: vendored constants, i.e. the original's exact behaviour (including its
#: runaway storm breaker, which is a bug guard the original always had,
#: not a budget).
TIER_UNLIMITED = "unlimited"

DEFAULT_TIER = "balanced"


def resolve_activity_budget(settings: dict) -> ActivityBudget | None:
    """Map the persisted settings to a budget. Unknown/missing → default.

    Falling back (rather than raising) is deliberate: this runs inside the
    poller loop, and a corrupt settings file must degrade to the default
    budget, not kill the autonomous side.
    """
    tier = settings.get("activityTier")
    if tier == TIER_UNLIMITED:
        return None
    if isinstance(tier, str) and tier in TIERS:
        return TIERS[tier]
    return TIERS[DEFAULT_TIER]


def _now_ms() -> int:
    return int(time.time() * 1000)


class SpawnLedger:
    """Persisted hourly spawn counts — enforcement and display share it.

    File shape: ``{"buckets": {"<epoch-hour>": count}}``. Epoch-hour keys make
    pruning and windowing arithmetic (no timezone parsing), and hourly
    granularity is exactly what both consumers need: the cap is per-hour and
    the display aggregates hours.
    """

    def __init__(self, data_dir: str | Path, clock=None) -> None:
        self._path = Path(data_dir) / LEDGER_FILE
        self._clock = clock or _now_ms
        # record_spawn is a read-modify-write and atomic_write only makes the
        # WRITE atomic. Today that is not yet a problem: the function is fully
        # synchronous with no await inside it, and every caller runs on the
        # gateway's event loop thread, so no other coroutine can interleave
        # partway through — and the separate MCP server process never touches
        # this file. The lock is here so none of that stays LOAD-BEARING: the
        # day a caller wraps this in asyncio.to_thread, or an await appears
        # between the read and the write, a lost increment would silently raise
        # the effective hourly ceiling — and because this same file is also the
        # usage display's source, the UI would agree with the wrong number and
        # nothing would look broken.
        self._lock = threading.Lock()

    def record_spawn(self) -> None:
        """Count one spawn in the current hour bucket (and prune old hours)."""
        with self._lock:
            now_hour = self._hour(self._clock())
            buckets = self._read()
            buckets[str(now_hour)] = buckets.get(str(now_hour), 0) + 1
            cutoff = now_hour - _LEDGER_KEEP_HOURS
            buckets = {k: v for k, v in buckets.items() if int(k) >= cutoff}
            atomic_write(self._path, json.dumps({"buckets": buckets}))

    def _read(self) -> dict[str, int]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            buckets = raw.get("buckets")
            if isinstance(buckets, dict):
                return {
                    str(k): int(v)
                    for k, v in buckets.items()
                    if str(k).isdigit() and isinstance(v, (int, float))
                }
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 — a corrupt meter must not stop spawns
            logger.warning("[mochi] spawn ledger unreadable, starting fresh")
        return {}

    def _hour(self, at_ms: int) -> int:
        return at_ms // 3_600_000

    def spawns_in_current_hour(self) -> int:
        return self._read().get(str(self._hour(self._clock())), 0)

    def spawns_in_last_hours(self, hours: int) -> int:
        cutoff = self._hour(self._clock()) - hours + 1
        return sum(v for k, v in self._read().items() if int(k) >= cutoff)

    def usage_summary(self) -> dict:
        """The Settings display payload — same buckets the cap enforces."""
        return {
            "runsThisHour": self.spawns_in_current_hour(),
            "runsToday": self.spawns_in_last_hours(24),
            "runs7d": self.spawns_in_last_hours(7 * 24),
        }

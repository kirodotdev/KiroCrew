"""Fairness lanes for the durable task queue (RFC overload-resilience §6, §13 Q5).

A *lane* groups the rows that one caller owes progress to. Every interactive
root session is its own lane, keyed by its session key; cron, hook,
heartbeat and background roots share the ``system`` lane. Rows of a nested
tree inherit their ROOT's lane, so a subagent's children compete under the
session that started the tree, not under a lane of their own.

Across lanes the dispatcher runs a smooth weighted round-robin: every pick
adds each contending lane's weight to its credit, takes from the lane with
the most credit and charges it the total weight. With equal weights that is
plain round-robin; a lane whose weight is 3 gets three picks per round,
spread out rather than in a burst. Inside a lane the order stays FIFO by
``created_at``, which is what keeps one session's own ordering intact.

Everything here is pure: no store, no clock, no manager.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

#: Lane shared by every non-interactive root (cron jobs, hooks, heartbeat,
#: background workers). Weight ``agent.lane_weights["system"]`` (default 1).
SYSTEM_LANE = "system"

#: Session-key prefixes that mean "automation, not a person".
_SYSTEM_PREFIXES: tuple[str, ...] = ("cron:", "cron_", "hook:", "webhook:")
#: Whole session keys that mean the same.
_SYSTEM_KEYS: frozenset[str] = frozenset({"_hb", "_bg"})

#: Prefix of a subagent's own session key; a row under it belongs to the
#: lane of the tree's root, which the store resolves through ``parent_id``.
SUBAGENT_PREFIX = "subagent:"

DEFAULT_WEIGHT = 1
MAX_WEIGHT = 64

T = TypeVar("T")


def lane_key_for(session_key: str | None) -> str:
    """The lane a ROOT row with *session_key* belongs to.

    Automation keys (cron, hook, heartbeat, background) and the empty key
    map to :data:`SYSTEM_LANE`. A ``subagent:<id>`` key is returned as-is: the
    caller that knows the parent row replaces it with the root's lane, and a
    row whose parent is gone keeps a lane of its own rather than being
    guessed into somebody else's.
    """
    key = str(session_key or "")
    if not key or key in _SYSTEM_KEYS or key.startswith(_SYSTEM_PREFIXES):
        return SYSTEM_LANE
    return key


def is_system_lane(lane: str) -> bool:
    return lane == SYSTEM_LANE


def clamp_weight(value: object, default: int = DEFAULT_WEIGHT) -> int:
    try:
        w = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_WEIGHT, w))


def lane_weight(
    lane: str,
    *,
    weights: Mapping[str, object] | None = None,
    system_weight: int = DEFAULT_WEIGHT,
) -> int:
    """Weight of *lane*: an explicit ``lane_weights`` entry, else the system
    weight for the system lane, else 1."""
    if weights and lane in weights:
        return clamp_weight(weights[lane])
    if is_system_lane(lane):
        return clamp_weight(system_weight)
    return DEFAULT_WEIGHT


@dataclass
class LaneScheduler:
    """Smooth weighted round-robin over lanes with pending work.

    ``credit`` is the per-lane running balance. It is carried across calls
    so fairness holds over time, not just within one drain; a lane that has
    nothing pending is dropped from the balance (``forget``), so a lane that
    comes back later starts even, never owed a burst.
    """

    weights: Mapping[str, object] = field(default_factory=dict)
    system_weight: int = DEFAULT_WEIGHT
    credit: dict[str, int] = field(default_factory=dict)

    def weight_of(self, lane: str) -> int:
        return lane_weight(lane, weights=self.weights, system_weight=self.system_weight)

    def pick(self, contending: Iterable[str]) -> str | None:
        """Choose the next lane among *contending* (lanes with pending rows).

        *contending* is read in order and a tie on credit goes to the lane
        listed first, so callers pass lanes oldest-head-first: the lane whose
        next row has waited longest wins an even split.
        """
        lanes: list[str] = []
        for raw in contending:
            lane = str(raw)
            if lane not in lanes:
                lanes.append(lane)
        if not lanes:
            return None
        total = 0
        for lane in lanes:
            w = self.weight_of(lane)
            total += w
            self.credit[lane] = self.credit.get(lane, 0) + w
        chosen = max(lanes, key=lambda lane: (self.credit[lane], -lanes.index(lane)))
        self.credit[chosen] -= total
        return chosen

    def forget(self, lanes: Iterable[str]) -> None:
        for lane in lanes:
            self.credit.pop(str(lane), None)

    def interleave(
        self,
        by_lane: Mapping[str, Sequence[T]],
        *,
        limit: int,
        age_of: Callable[[T], Any] | None = None,
    ) -> list[T]:
        """Merge per-lane FIFO lists into one dispatch order, at most *limit* long.

        *age_of* orders the lanes for tie-breaks by their current head (smaller
        is older); without it the mapping's own order is used.
        """
        cursors = {lane: 0 for lane, rows in by_lane.items() if rows}
        out: list[T] = []
        while cursors and len(out) < limit:
            order: list[str] = list(cursors)
            if age_of is not None:
                order.sort(key=lambda lane: age_of(by_lane[lane][cursors[lane]]))
            lane = self.pick(order)
            if lane is None:
                break
            rows = by_lane[lane]
            out.append(rows[cursors[lane]])
            cursors[lane] += 1
            if cursors[lane] >= len(rows):
                del cursors[lane]
        return out

    def pick_index(
        self,
        entries: Sequence[T],
        *,
        lane_of: Callable[[T], str],
        eligible: Callable[[T], bool] | None = None,
    ) -> int | None:
        """Index of the next entry to take from an in-memory FIFO window.

        The window is one list in arrival order; each lane's oldest eligible
        entry is its head, and the weighted round-robin picks which head goes.
        """
        heads: dict[str, int] = {}
        for idx, entry in enumerate(entries):
            if eligible is not None and not eligible(entry):
                continue
            heads.setdefault(lane_of(entry), idx)
        # Insertion order is arrival order: the lane with the oldest head is
        # listed first and wins a tie.
        chosen = self.pick(heads)
        if chosen is None:
            return None
        return heads[chosen]

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {
            lane: {"weight": self.weight_of(lane), "credit": credit}
            for lane, credit in sorted(self.credit.items())
        }

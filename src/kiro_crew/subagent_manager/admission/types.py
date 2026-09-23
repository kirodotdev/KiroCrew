"""Admission value types: settings, the prepared row, the claim and defer points, the capacity reading."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from kiro_crew import taskq as _taskq
    from kiro_crew.subagent import SubagentInfo

#: ``SubagentInfo.error_code`` for a spawn refused because the task store could
#: not commit the row. ``POST /api/spawn`` forwards it as ``code`` so a caller
#: can tell "nothing was accepted, retry later" from a policy refusal.
TASK_STORE_UNAVAILABLE_CODE = "task_store_unavailable"


def tombstone_terminal_state(cause: str) -> str | None:
    """The terminal task state a tombstone cause proves, loaded on first use."""
    from kiro_crew import taskq

    return {
        "delivered": taskq.DONE,
        "user_stop": taskq.CANCELLED,
        "cancelled": taskq.CANCELLED,
        "error": taskq.FAILED,
        "timeout": taskq.FAILED,
        "turn_limit": taskq.FAILED,
        "child_escalation_limit": taskq.FAILED,
    }.get(cause)


#: How long the fairness settings read from config are reused by the pump
#: before they are re-read (seconds). Config changes are picked up within it;
#: :meth:`SpawnAdmissionCoordinator.set_fairness_settings` applies at once.
FAIRNESS_SETTINGS_TTL_SECS = 2.0


@dataclass(frozen=True)
class FairnessSettings:
    """The dispatcher's fairness knobs (``agent.*`` config), resolved once."""

    lane_weights: Mapping[str, int] = field(default_factory=dict)
    child_reserve: int = 1
    adaptive_floor: int = 1

    @classmethod
    def from_agent_config(cls, agent: Any) -> "FairnessSettings":
        from kiro_crew.taskq import lanes as _lanes

        raw_weights = getattr(agent, "lane_weights", None) or {}
        weights = {}
        if isinstance(raw_weights, Mapping):
            weights = {str(k): _lanes.clamp_weight(v) for k, v in raw_weights.items() if k}
        try:
            reserve = max(0, min(8, int(getattr(agent, "child_reserve", 1))))
        except (TypeError, ValueError):
            reserve = 1
        try:
            floor = max(1, min(64, int(getattr(agent, "adaptive_floor", 1))))
        except (TypeError, ValueError):
            floor = 1
        return cls(
            lane_weights=weights,
            child_reserve=reserve,
            adaptive_floor=floor,
        )


@dataclass
class PreparedSpawn:
    """A spawn that passed every policy gate but has not been persisted yet.

    ``SubagentManager.spawn_async`` writes ``record`` on the store's writer
    thread, then re-enters ``spawn(**params, _preassigned_id=agent_id,
    _store_accepted=True)``: the row exists before the caller is acked, and
    the SQLite lock wait never runs on the event loop.
    """

    agent_id: str
    params: dict[str, Any]
    record: "_taskq.TaskRecord"


@dataclass(frozen=True)
class ClaimPoint:
    """``spawn_impl(_stop_before_claim=True)``: every gate passed and the row
    is about to be claimed. The SLOT IS RESERVED at this point -- the running
    count and the stagger token were taken synchronously, before any await --
    so a concurrent admission during the claim sees the cap already spent. The
    event-loop dispatcher takes the claim on the store's writer thread and
    re-enters with ``_claimed``, which CONSUMES the reservation (registration
    does not count the run a second time); every non-start exit of that
    re-entry releases it (:meth:`SpawnAdmissionCoordinator.release_reservation`).
    Boundary identity crosses the await so cancellation can be revalidated
    immediately before registration instead of trusting a stale claim result."""

    agent_id: str
    parent_session_key: str = ""
    boundary_owner: str = ""


@dataclass(frozen=True)
class DeferPoint:
    """A drained row the pressure gate parked, with its ``store.defer`` still
    unwritten. NO slot is reserved here -- the gate returns before the claim --
    so nothing is leaked if the write raises.

    Both answers are decided already: *queued* if the write reports a row,
    *refused* if it reports none, because ``_from_queue`` does not prove a row
    exists (``_queue`` also holds entries that never reached the store). The
    coroutine dispatcher takes the write on the store's writer thread and
    picks between them (:meth:`SpawnAdmissionCoordinator.finish_parked_defer`),
    so the boolean survives while the two-second busy wait never runs on the
    event loop.
    """

    agent_id: str
    reason: str
    parent_session_key: str
    batch_id: str
    queued: "SubagentInfo"
    refused: "SubagentInfo"


@dataclass(frozen=True)
class CapacityView:
    """One reading of the execution cap as the dispatcher sees it.

    ``cap_total`` is the manager's effective cap, lifted to
    ``min(user_max, adaptive_floor + child_reserve)`` while a parent waits on
    children AND an adaptive squeeze is in force -- the reserve is honoured by
    the adaptive cap too, never above the user's ceiling. ``roots_cap`` is
    what a depth-0 start may fill: ``cap_total - child_reserve`` while the
    reserve is active (a nested row or a resume is waiting for a slot), else
    the whole cap -- and never more than the unlifted cap.
    """

    cap_total: int
    running: int
    child_reserve: int
    reserve_active: bool
    waiting_parents: int
    lifted_from: int | None = None

    @property
    def roots_cap(self) -> int:
        # A lifted cap is for nested starts and resumes only: roots never see
        # more than the cap the controller actually set.
        base = self.cap_total if self.lifted_from is None else min(self.cap_total, self.lifted_from)
        if not self.reserve_active:
            return base
        return max(0, min(base, self.cap_total - self.child_reserve))

    @property
    def any_slot(self) -> bool:
        return self.running < self.cap_total

    @property
    def root_slot(self) -> bool:
        return self.running < self.roots_cap

    def to_dict(self) -> dict[str, Any]:
        return {
            "cap_total": self.cap_total,
            "roots_cap": self.roots_cap,
            "running": self.running,
            "child_reserve": self.child_reserve,
            "reserve_active": self.reserve_active,
            "waiting_parents": self.waiting_parents,
            "lifted_from": self.lifted_from,
        }

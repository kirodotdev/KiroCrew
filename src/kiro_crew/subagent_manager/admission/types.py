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

#: The shortest delay an admission re-check timer whose delay derives from
#: ``admit_wait_secs`` or a row's wake is armed with: the pump's waiting-row
#: wake and the retained-claim and boundary-cancel retries. A delay that reaches
#: 0 would re-run the same pass on the next loop turn, which is a spin when that
#: pass cannot make progress.
MIN_RECHECK_DELAY_SECS = 0.05

#: The window entry key that marks an entry the refill hydrated from a
#: ``recovering`` row: a run being rebuilt after its owner was lost, which the
#: queue-depth chip does not count as waiting to start. It is also ``spawn``'s
#: keyword of the same name, so the pump hands it on with the rest of the entry
#: and a gate that re-queues the still-unclaimed row (stagger, cap, child
#: reserve) puts the mark back on the entry it appends. Never persisted.
WINDOW_ENTRY_RECOVERING = "_recovering_row"

#: A ``_queue`` entry's monotonic not-before time: a start with no durable row
#: that did not fit the memory floor waits in the in-memory window, and the
#: pump skips it until then -- the in-memory twin of a durable row's
#: ``next_run_at``. Popped with ``_lane`` before the entry reaches ``spawn``.
MEMORY_WAIT_UNTIL_KEY = "_memory_wait_until"


def outcome_task_state(outcome: str) -> str | None:
    """The terminal task state of a run's recorded outcome (``SubagentInfo.outcome``).

    ONE table for the live settle (``taskq_settle``) and the boot probe, so the
    two cannot disagree about the same ending. Its keys are the outcome
    vocabulary (``subagent_persistence._PANEL_OUTCOMES``);
    ``test_taskq_reconcile.py`` pins that every outcome maps.
    """
    from kiro_crew import taskq

    return {
        "completed": taskq.DONE,
        "stopped": taskq.CANCELLED,
        "failed": taskq.FAILED,
    }.get(outcome)


def tombstone_terminal_state(cause: str, outcome: str = "") -> str | None:
    """The terminal task state a tombstone proves, loaded on first use.

    The ending the writer recorded (``outcome``) decides first, exactly as the
    live settle decided it (:func:`outcome_task_state`); the coarser ``cause``
    answers for a tombstone that recorded none. ``gateway_restart`` proves
    nothing by itself, and is the one cause missing here
    (``test_every_tombstone_cause_has_a_terminal_state`` pins that).
    """
    from kiro_crew import taskq
    from kiro_crew.subagent import _NEUTRAL_REAP_REASONS

    recorded = outcome_task_state(outcome)
    if recorded is not None:
        return recorded
    if cause in _NEUTRAL_REAP_REASONS:
        # A user stop and a parent end are deliberate stops, written by the
        # same reap: the row they leave behind is cancelled, not a run to
        # recover on the next boot. Read from the set that makes the live record
        # neutral (``SubagentInfo.stop_is_neutral``), so the two cannot drift.
        return taskq.CANCELLED
    return {
        "delivered": taskq.DONE,
        # ``stage_cancel`` tombstones written by the retired chat Autopilot can
        # still sit on disk, and they read as the deliberate stop they were.
        "stage_cancel": taskq.CANCELLED,
        "cancelled": taskq.CANCELLED,
        "error": taskq.FAILED,
        "timeout": taskq.FAILED,
        "turn_limit": taskq.FAILED,
        "child_escalation_limit": taskq.FAILED,
        "reaped": taskq.FAILED,
        "startup_timeout": taskq.FAILED,
        "start_queue_saturated": taskq.FAILED,
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
class MemoryReadPoint:
    """``spawn_impl(_stop_before_memory_read=True)``: every policy gate passed
    and the memory floor's bar (*min_gb*: the floor plus this start's price
    plus what warming starts still owe) is known, but the host has not been
    read. The reading walks cgroup files, so an event-loop caller takes it on
    a worker thread and re-enters ``spawn(**params, _memory_reading=...)``.
    The re-entry recomputes the bar on the loop and decides against that, so a
    start admitted while the read ran is charged. It re-runs the policy gates
    (a governance change made during the read must hold), on a row
    ``spawn_async`` already committed (``_store_accepted``) too, whose refusal
    fails that row. NOTHING is reserved here: no slot
    and no row write. A batch member's submission is counted by this pass, as
    on any first entry, and the re-entry does not count it again."""

    min_gb: float
    params: dict[str, Any]


@dataclass(frozen=True)
class ClaimPoint:
    """``spawn_impl(_stop_before_claim=True)``: every gate passed and the row
    is about to be claimed. The SLOT IS RESERVED at this point -- the running
    count and the stagger token were taken synchronously, before any await --
    so a concurrent admission during the claim sees the cap already spent. The
    event-loop dispatcher takes the claim on the store's writer thread and
    re-enters with ``_claimed``, which CONSUMES the reservation (registration
    does not count the run a second time); every non-start exit of that
    re-entry releases it (:meth:`SpawnAdmissionCoordinator.release_reservation`)."""

    agent_id: str
    parent_session_key: str = ""


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
    # The gate's label for the wait (``reason`` kind plus the memory figures),
    # published on the ``subagent_queued`` emit that follows a SUCCESSFUL
    # defer write -- never before it, so a refused row leaves no label behind.
    wait: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QueuedRun:
    """An accepted spawn that has no run yet.

    It waits in the dispatch window or only as a task-store row: deferred by the
    memory gate, queued behind capacity, or claimed and not yet registered. The
    registry (``SubagentManager.get`` / ``all_agents``) cannot name it, so this is
    what ``GET /api/spawn/{id}`` and ``GET /api/spawn`` report for it instead of
    "not found".

    ``reason`` is the parent's current wait label (a ``QUEUED_REASON_*`` kind).
    It is per parent, last writer wins, like the ``subagent_queued`` event it
    comes from (``_emit_queue_depth``). ``reason_detail`` is the gate's own
    sentence from the row's latest ``deferred`` event, present only while that
    deferral is in force and newer than the row's last claim or transition.

    ``resuming`` is set for a run that already STARTED and waits to go on: a
    ``recovering`` row after a gateway restart (``RESUMING_AFTER_RESTART``) or a
    ``retry_wait`` row that ran before (``RESUMING_RETRY``). It is not "not
    started", and a reader must not call it that.
    """

    id: str
    task: str
    parent_session_key: str
    agent: str = ""
    app: str = ""
    accepted_at: float = 0.0
    reason: str = ""
    reason_detail: str = ""
    resuming: str = ""


class QueuedReadUnavailable(Exception):
    """The task store could not say whether an id is queued (an outage, or a
    row this build cannot model). Distinct from "not queued": a reader answers
    it as transient (503), never as a definitive "not found". Defined here, not
    in ``taskq``, so a route can catch it without loading the task queue."""


@dataclass(frozen=True)
class QueuedRunListing:
    """One read of the accepted spawns no run exists for yet, oldest first.

    ``partial`` is True when the listing cannot be every such spawn: the store
    held more rows than one listing returns (``taskq_bridge.QUEUED_LISTING_CAP``)
    or could not be read at all. A reader then says the list is partial instead
    of presenting it as every queued spawn.
    """

    runs: tuple[QueuedRun, ...]
    partial: bool = False


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

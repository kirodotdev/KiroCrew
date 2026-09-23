"""Durable task records and the one validated state machine they move through.

A task is any unit of accepted work the gateway owes an outcome for -- a
subagent run, a workflow agent, a TaskRunner step -- regardless of which entry
point accepted it. The record is what survives a gateway crash; the transition
table is what keeps every writer (dispatch, run loop, cancel, reconcile) from
inventing its own lifecycle vocabulary. ``TaskStore`` is the only module that
mutates rows, and it consults :func:`check_transition` on every state write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Mapping

from kiro_crew.recovery.policy import (
    DEFAULT_BACKOFF_BASE_SECS,
    DEFAULT_BACKOFF_MAX_SECS,
    LayerPolicy,
)

#: The bare shared schedule (no layer specialisation): the store retries a row,
#: it does not own a rung of the ladder.
_RECOVERY_SCHEDULE = LayerPolicy(layer="taskq", trigger="lost owner / retry_wait", max_attempts=1)

# ── vocabulary ────────────────────────────────────────────────────────────────

QUEUED = "queued"
ADMITTED = "admitted"
STARTING = "starting"
RUNNING = "running"
WAITING_CHILDREN = "waiting_children"
WAITING_PERMISSION = "waiting_permission"
WAITING_DEPENDENCY = "waiting_dependency"
WAITING_INPUT = "waiting_input"
WAITING_INFRA = "waiting_infra"
RETRY_WAIT = "retry_wait"
RECOVERING = "recovering"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
UNKNOWN_SIDE_EFFECT = "unknown_side_effect"

#: ``succeeded`` in the owner's vocabulary is ``done`` here; one spelling in
#: the table, the alias for readers that speak the addendum's.
SUCCEEDED = DONE

STATES: frozenset[str] = frozenset(
    {
        QUEUED,
        ADMITTED,
        STARTING,
        RUNNING,
        WAITING_CHILDREN,
        WAITING_PERMISSION,
        WAITING_DEPENDENCY,
        WAITING_INPUT,
        WAITING_INFRA,
        RETRY_WAIT,
        RECOVERING,
        DONE,
        FAILED,
        CANCELLED,
        UNKNOWN_SIDE_EFFECT,
    }
)

#: Terminal states never regress. ``unknown_side_effect`` is terminal-pending:
#: an external operation may have happened, and only a reconciling adapter that
#: can query the external system may move it (to ``done`` or ``failed``).
TERMINAL: frozenset[str] = frozenset({DONE, FAILED, CANCELLED, UNKNOWN_SIDE_EFFECT})

#: The waits a LIVE run enters from ``running`` and returns from without a
#: re-dispatch: the runtime (session handle, process, blocked tool) stays
#: resident, the lane slot is released, and a ``WaitRecord`` says why and
#: what wakes it. ``retry_wait`` is deliberately not here: it holds no runtime
#: and is re-dispatched through ``claim``.
WAITING: frozenset[str] = frozenset(
    {WAITING_CHILDREN, WAITING_PERMISSION, WAITING_DEPENDENCY, WAITING_INPUT}
)

#: States the dispatcher may claim: the row holds no runtime and no lease that
#: a live owner still depends on. ``recovering`` is included so a row whose
#: runtime was lost (lease lapsed, owner gone) is rebuilt by re-dispatch rather
#: than left to a second, parallel recovery mechanism.
CLAIMABLE: frozenset[str] = frozenset({QUEUED, RETRY_WAIT, RECOVERING})

#: States in which some incarnation OWNS the row: a slot, a runtime, or a
#: pending rebuild. A boot-time reconcile examines exactly these.
ACTIVE: frozenset[str] = frozenset({ADMITTED, STARTING, RUNNING, RECOVERING}) | WAITING

#: States whose row has a runtime EXECUTING under it: the step, agent call or
#: run is making progress, so nothing but its owning run can stop the work.
#: ``admitted`` is deliberately NOT one -- it is claimed and never started, the
#: statement the boot reconciler spends when it requeues a row (§ The claim/start
#: boundary), and the ``starting`` write is what ends membership here.
EXECUTING: frozenset[str] = frozenset({STARTING, RUNNING})

#: Non-terminal states with nothing executing: queued, deferred, in a recovery
#: backoff, or parked on a wake a caller delivers. What a wait-level cancel may
#: end, and the predicate ``TaskStore.cancel(only_from=)`` fences that cancel
#: with, so a row that LEAVES this set between an operator's read and the write
#: is refused rather than cancelled under a live executor whose own settlement
#: the generation bump would then fence out.
PARKED: frozenset[str] = STATES - TERMINAL - EXECUTING

_NON_TERMINAL_TARGETS = frozenset({CANCELLED, FAILED})
_ACTIVE_TERMINAL_TARGETS = _NON_TERMINAL_TARGETS | frozenset({DONE, UNKNOWN_SIDE_EFFECT})
#: What a live wait may become: woken back to ``running``, handed to the
#: recovery ladder, parked for a re-dispatch when its runtime was reclaimed
#: (``retry_wait``), or ended. ``done`` is reachable because a wait whose
#: runtime finished before the wake write landed is settled by the artifacts.
_WAIT_TARGETS = frozenset({RUNNING, RECOVERING, RETRY_WAIT}) | _ACTIVE_TERMINAL_TARGETS

#: The single transition table. ``cancelled`` beats every non-terminal state
#: and ``failed`` is reachable from each of them (auth re-validation, a
#: dispatcher that cannot build the runtime). Every OWNED state can also end in
#: ``done`` or ``unknown_side_effect``: a run may finish before its ``running``
#: write landed, and the reconciler settles what the artifacts prove from
#: whatever state the crash left. A wait may move to another wait kind only
#: through ``running`` (one reason per record).
TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({ADMITTED, WAITING_INFRA}) | _NON_TERMINAL_TARGETS,
    ADMITTED: frozenset({STARTING, QUEUED, WAITING_INFRA}) | _ACTIVE_TERMINAL_TARGETS,
    STARTING: frozenset({RUNNING, WAITING_INFRA, RECOVERING, RETRY_WAIT})
    | _ACTIVE_TERMINAL_TARGETS,
    RUNNING: WAITING | frozenset({RECOVERING, RETRY_WAIT}) | _ACTIVE_TERMINAL_TARGETS,
    WAITING_CHILDREN: _WAIT_TARGETS,
    WAITING_PERMISSION: _WAIT_TARGETS,
    WAITING_DEPENDENCY: _WAIT_TARGETS,
    WAITING_INPUT: _WAIT_TARGETS,
    WAITING_INFRA: frozenset({RETRY_WAIT, QUEUED}) | _NON_TERMINAL_TARGETS,
    RETRY_WAIT: frozenset({QUEUED, ADMITTED}) | _NON_TERMINAL_TARGETS,
    RECOVERING: frozenset({ADMITTED, RUNNING, RETRY_WAIT}) | _ACTIVE_TERMINAL_TARGETS,
    DONE: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
    # Only a reconciling adapter resolves this, and only to a real terminal.
    UNKNOWN_SIDE_EFFECT: frozenset({DONE, FAILED}),
}

KIND_SUBAGENT = "subagent"
KIND_WORKFLOW_AGENT = "workflow_agent"
KIND_TASKRUNNER_STEP = "taskrunner_step"
KIND_CHAT_TURN = "chat_turn"
KIND_CRON = "cron"
KIND_HOOK = "hook"
KINDS: frozenset[str] = frozenset(
    {
        KIND_SUBAGENT,
        KIND_WORKFLOW_AGENT,
        KIND_TASKRUNNER_STEP,
        KIND_CHAT_TURN,
        KIND_CRON,
        KIND_HOOK,
    }
)

SIDE_EFFECT_NONE = "none"
SIDE_EFFECT_IDEMPOTENT_KEY = "idempotent_key"
SIDE_EFFECT_UNKNOWN = "unknown"
SIDE_EFFECT_CLASSES: frozenset[str] = frozenset(
    {SIDE_EFFECT_NONE, SIDE_EFFECT_IDEMPOTENT_KEY, SIDE_EFFECT_UNKNOWN}
)

#: Lease a claim takes on a row. Long enough that a healthy run loop renews it
#: comfortably (every third of it), short enough that a dead owner's row is
#: re-dispatchable within a minute of the reconciler noticing.
LEASE_SECS = 60.0

#: Recovery backoff for a re-dispatched row: the shared recovery ladder's
#: schedule (``recovery.policy``: ``base * 2**(attempt-1)``, capped, equal
#: jitter), so a task re-dispatch, a backend respawn and a runtime rebuild retry
#: on one clock. The two names are kept for readers; the values are the
#: ladder's defaults (``agent.recovery_backoff_base_secs`` /
#: ``agent.recovery_backoff_max_secs``), and the store reads the schedule
#: through :func:`recovery_backoff_secs` only.
RECOVERY_BACKOFF_BASE_SECS = DEFAULT_BACKOFF_BASE_SECS
RECOVERY_BACKOFF_MAX_SECS = DEFAULT_BACKOFF_MAX_SECS


def recovery_backoff_secs(attempts: int) -> float:
    """Seconds to wait before re-dispatching a row on its ``attempts``-th retry.

    ``attempts`` is the row's failure count so far (0 on the first retry);
    the ladder's 1-based ``attempt`` is that plus one. Deterministic here (no
    jitter): the store's ``next_run_at`` is what the dispatcher orders rows
    by, and the dispatcher applies the jitter when it wakes them.
    """
    return _RECOVERY_SCHEDULE.raw_backoff_secs(int(attempts) + 1)


class InvalidTransition(ValueError):
    """A state write the transition table forbids.

    Raised for an unknown state as well as a disallowed edge, so a typo in a
    caller is a loud error rather than a silently unreachable row.
    """

    def __init__(self, old: str, new: str) -> None:
        super().__init__(f"task transition {old!r} -> {new!r} is not allowed")
        self.old = old
        self.new = new


def check_transition(old: str, new: str) -> None:
    """Raise :class:`InvalidTransition` unless ``old -> new`` is in the table."""
    if old not in STATES or new not in STATES:
        raise InvalidTransition(old, new)
    if new not in TRANSITIONS[old]:
        raise InvalidTransition(old, new)


def steps_to(old: str, new: str) -> tuple[str, ...]:
    """The table's OWN path from *old* to *new*, at most one state long.

    ``(new,)`` when the table has that edge. ``(starting, new)`` when it does
    not but the skipped ``starting`` step reaches it -- ``admitted -> starting
    -> running`` is the case this exists for: a caller whose earlier
    ``starting`` write never committed (a locked database) would otherwise ask
    for an edge the table forbids and be refused for good, leaving a LIVE row
    in ``admitted``. ``()`` when there is no such path, when a name is unknown,
    or when *old* is already *new* -- the table has no self-edges, so an
    identity is never a write.

    ``starting`` is the ONLY intermediate this helper will insert, and the
    restriction is the safety property, not an implementation limit: a
    ``starting`` write lost between the claim and the first mark is the one
    missed step a live row can carry, while every other pair the table joins in
    two steps walks a live row BACKWARDS into a state a dispatcher may take
    (``running -> retry_wait -> queued``, ``running -> recovering ->
    admitted``) -- the re-dispatch-after-work class the callers of this helper
    exist to close. Direction is therefore the HELPER's to enforce, never a
    docstring asking callers not to ask: the dangerous move is not expressible.
    The intermediate is also never TERMINAL (``starting`` is not), so no replay
    routes a live row through an outcome, and every element is an edge
    ``check_transition`` accepts, so this widens nothing.
    """
    if old not in STATES or new not in STATES or old == new:
        return ()
    if new in TRANSITIONS[old]:
        return (new,)
    if STARTING in TRANSITIONS[old] and new in TRANSITIONS[STARTING]:
        return (STARTING, new)
    return ()


def is_terminal(state: str) -> bool:
    return state in TERMINAL


# ── record ────────────────────────────────────────────────────────────────────


@dataclass
class TaskRecord:
    """One durable task row.

    ``params`` is the full spawn argument set the entry point needs to
    re-dispatch the task from nothing but this row -- for a subagent that is
    exactly the dict shape the manager's in-memory queue entries carry.
    ``scope_ref`` names the permission and memory boundaries the run must be
    re-validated against at admit time (memory store, allowed tools, approval
    scope, owning app); it is a reference, never a grant.
    """

    id: str
    kind: str
    session_key: str = ""
    parent_id: str | None = None
    root_id: str = ""
    harness: str = ""
    provider: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    workspace: str | None = None
    scope_ref: dict[str, Any] = field(default_factory=dict)
    state: str = QUEUED
    attempts: int = 0
    next_run_at: float | None = None
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    generation: int = 0
    progress: dict[str, Any] | None = None
    result_ref: str | None = None
    deadline_at: float | None = None
    idempotency_key: str | None = None
    side_effect_class: str = SIDE_EFFECT_UNKNOWN
    created_at: float = 0.0
    updated_at: float = 0.0
    #: The ``WaitRecord`` (``taskq.waits``) as a plain dict while the row is in
    #: a ``WAITING`` state; None otherwise. Kept as a dict here so the model
    #: module owns no wait semantics -- ``waits.WaitRecord.from_dict`` does.
    wait: dict[str, Any] | None = None
    #: Fairness lane (``taskq.lanes``): the lane the row is dispatched under,
    #: a root session key or ``system``; a nested row inherits its root's
    #: lane. Empty means "derive at accept" (``TaskStore._lane_in_tx``).
    lane: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("a task record needs a non-empty id")
        if self.kind not in KINDS:
            raise ValueError(f"unknown task kind {self.kind!r}")
        if self.state not in STATES:
            raise ValueError(f"unknown task state {self.state!r}")
        if self.side_effect_class not in SIDE_EFFECT_CLASSES:
            raise ValueError(f"unknown side-effect class {self.side_effect_class!r}")
        if not self.root_id:
            self.root_id = self.parent_id or self.id

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    # Column order is the schema's; ``to_row``/``from_row`` are the only two
    # places that know it, so a column added to the schema is added here once.
    COLUMNS: ClassVar[tuple[str, ...]] = (
        "id",
        "parent_id",
        "root_id",
        "session_key",
        "kind",
        "harness",
        "provider",
        "params_json",
        "workspace",
        "scope_ref",
        "state",
        "attempts",
        "next_run_at",
        "lease_owner",
        "lease_expires_at",
        "generation",
        "progress_json",
        "result_ref",
        "deadline_at",
        "idempotency_key",
        "side_effect_class",
        "created_at",
        "updated_at",
        "wait_json",
        "lane",
    )

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.id,
            self.parent_id,
            self.root_id,
            self.session_key,
            self.kind,
            self.harness,
            self.provider,
            json.dumps(self.params, sort_keys=True, default=str),
            self.workspace,
            json.dumps(self.scope_ref, sort_keys=True, default=str),
            self.state,
            int(self.attempts),
            self.next_run_at,
            self.lease_owner,
            self.lease_expires_at,
            int(self.generation),
            json.dumps(self.progress, sort_keys=True, default=str) if self.progress else None,
            self.result_ref,
            self.deadline_at,
            self.idempotency_key,
            self.side_effect_class,
            self.created_at,
            self.updated_at,
            json.dumps(self.wait, sort_keys=True, default=str) if self.wait else None,
            self.lane,
        )

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TaskRecord":
        def _obj(raw: Any) -> Any:
            if raw in (None, ""):
                return None
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return None

        params = _obj(row["params_json"])
        scope = _obj(row["scope_ref"])
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        wait = _obj(row["wait_json"]) if "wait_json" in keys else None
        lane = str(row["lane"] or "") if "lane" in keys else ""
        return cls(
            id=str(row["id"]),
            parent_id=row["parent_id"],
            root_id=str(row["root_id"] or ""),
            session_key=str(row["session_key"] or ""),
            kind=str(row["kind"]),
            harness=str(row["harness"] or ""),
            provider=row["provider"],
            params=params if isinstance(params, dict) else {},
            workspace=row["workspace"],
            scope_ref=scope if isinstance(scope, dict) else {},
            state=str(row["state"]),
            attempts=int(row["attempts"] or 0),
            next_run_at=row["next_run_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            generation=int(row["generation"] or 0),
            progress=_obj(row["progress_json"]),
            result_ref=row["result_ref"],
            deadline_at=row["deadline_at"],
            idempotency_key=row["idempotency_key"],
            side_effect_class=str(row["side_effect_class"] or SIDE_EFFECT_UNKNOWN),
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
            wait=wait if isinstance(wait, dict) else None,
            lane=lane,
        )

    def public(self) -> dict[str, Any]:
        """The row as the ``/api/tasks`` and doctor surfaces read it: no params."""
        out = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "params"}
        out["terminal"] = self.terminal
        return out


@dataclass(frozen=True)
class TaskEvent:
    """One append-only ``task_events`` row.

    ``kind`` is a small enum-like vocabulary (``accepted``, ``claimed``,
    ``transition``, ``deferred``, ``stale_result``, ``rejected_transition``,
    ``deliver``, ``reconciled``, ``imported``, ``wait``, ``wake``,
    ``start_attempt``); ``data`` is the free-form detail.
    """

    task_id: str
    seq: int
    ts: float
    kind: str
    data: dict[str, Any]

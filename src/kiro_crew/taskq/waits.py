"""Waits with a reason: the ``WaitRecord`` and the ledger that moves rows through them.

A live run that cannot make progress -- its children are still running, a
dependency answered 429, a command wants a password, an approval is pending --
does not sit in ``running`` holding a lane slot. It enters one of the
``WAITING`` states with a :class:`WaitRecord` that says WHY, since WHEN, WHAT
wakes it, what cancelling it means, and WHO produced the evidence. Three things
are kept apart on purpose (SPEC-ADDENDUM §2):

* **identity** -- the row, its generation and its wait record, on disk;
* **the execution quota** -- the lane slot, RELEASED on entry so admission can
  hand it to another task (``slot_released``);
* **real resources** -- the process, session handle, FDs and memory the runtime
  still holds. Those stay charged to the host budget until the runtime is
  actually reclaimed (``residency_charged``); no counter is ever decremented
  for a process that still exists.

A wake never bypasses admission: :meth:`WaitLedger.wake` writes ``running``
under a NEW generation and the caller re-enters the lane queue (``resume``
entry) before it may continue -- so a dependency recovering for five hundred
waiting trees produces five hundred rows eligible for re-admission, not five
hundred simultaneous runtimes (RFC §14.3).

Nested propagation S -> A -> B: B waits; A enters ``waiting_children`` only
when it has no runnable work of its own; the wake fires on the LAST awaited
child; each level is re-admitted individually. Child failure follows the
parent's ``on_child_failure`` policy (``continue`` by default, ``fail_parent``
to fail fast); a parent cancel cascades to its children; a child cancel counts
as a failed child for the policy; a wait deadline ends the row ``failed`` with
the reason recorded. Completed siblings stay ``done``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping

from .model import (
    CANCELLED,
    FAILED,
    RETRY_WAIT,
    RUNNING,
    TERMINAL,
    WAITING,
    WAITING_CHILDREN,
    WAITING_DEPENDENCY,
    WAITING_INPUT,
    WAITING_PERMISSION,
    TaskRecord,
)
from .store import TaskStore, TaskStoreUnavailable

logger = logging.getLogger(__name__)

# ── vocabulary ────────────────────────────────────────────────────────────────

RESUME_AT_TIME = "at_time"
RESUME_SIGNAL = "signal"
RESUME_CHILDREN = "children"
RESUME_INPUT = "input"
RESUME_PERMISSION = "permission"
RESUME_KINDS: frozenset[str] = frozenset(
    {RESUME_AT_TIME, RESUME_SIGNAL, RESUME_CHILDREN, RESUME_INPUT, RESUME_PERMISSION}
)

#: Which resume kinds each waiting state admits. One reason per record: a
#: children wait is ``waiting_children``, never ``waiting_dependency`` with
#: child ids tucked into the key.
_KINDS_FOR_STATE: dict[str, frozenset[str]] = {
    WAITING_CHILDREN: frozenset({RESUME_CHILDREN}),
    WAITING_PERMISSION: frozenset({RESUME_PERMISSION}),
    WAITING_INPUT: frozenset({RESUME_INPUT}),
    WAITING_DEPENDENCY: frozenset({RESUME_AT_TIME, RESUME_SIGNAL}),
}

CANCEL_CALL = "cancel_call"  # cancel the blocked tool call; the task continues
CANCEL_TASK = "cancel_task"  # cancel this task only
CANCEL_TREE = "cancel_tree"  # cancel this task and every child it is waiting on
CANCEL_SEMANTICS: frozenset[str] = frozenset({CANCEL_CALL, CANCEL_TASK, CANCEL_TREE})

EVIDENCE_EXECUTION_LAYER = "execution_layer"
EVIDENCE_LIVENESS_ORACLE = "liveness_oracle"
EVIDENCE_DEPENDENCY_ADAPTER = "dependency_adapter"
#: Named so a reader can see it is the one source that is NEVER sufficient: a
#: model saying "I am waiting" is text, not a wait. Refused on its own.
EVIDENCE_MODEL_TEXT = "model_text"
EVIDENCE_SOURCES: frozenset[str] = frozenset(
    {
        EVIDENCE_EXECUTION_LAYER,
        EVIDENCE_LIVENESS_ORACLE,
        EVIDENCE_DEPENDENCY_ADAPTER,
        EVIDENCE_MODEL_TEXT,
    }
)

ON_CHILD_FAILURE_CONTINUE = "continue"
ON_CHILD_FAILURE_FAIL_PARENT = "fail_parent"
ON_CHILD_FAILURE_POLICIES: frozenset[str] = frozenset(
    {ON_CHILD_FAILURE_CONTINUE, ON_CHILD_FAILURE_FAIL_PARENT}
)
#: The params key a task carries its policy under; absent means ``continue``.
ON_CHILD_FAILURE_PARAM = "on_child_failure"

WAIT_REASON_DEADLINE = "wait_deadline"
WAIT_REASON_CHILD_FAILED = "child_failed"
WAIT_REASON_PARENT_TERMINAL = "parent_terminal"


def on_child_failure_policy(params: Mapping[str, Any] | None) -> str:
    """The parent's policy from its params; an unknown value reads as ``continue``."""
    raw = str((params or {}).get(ON_CHILD_FAILURE_PARAM) or "")
    return raw if raw in ON_CHILD_FAILURE_POLICIES else ON_CHILD_FAILURE_CONTINUE


# ── record ────────────────────────────────────────────────────────────────────


@dataclass
class ResumeCondition:
    """What ends the wait.

    ``kind`` picks the reading: ``at_time`` -> ``at`` is the wall-clock instant;
    ``signal`` -> ``key`` is the dependency scope or signal name; ``children``
    -> ``ids`` are the child task ids still awaited; ``input`` / ``permission``
    -> ``key`` is the tool call id or approval id the answer must be routed to.
    """

    kind: str
    at: float | None = None
    key: str = ""
    ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind not in RESUME_KINDS:
            raise ValueError(f"unknown resume condition kind {self.kind!r}")
        self.ids = [str(i) for i in self.ids if i]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResumeCondition":
        return cls(
            kind=str(raw.get("kind") or ""),
            at=float(raw["at"]) if raw.get("at") is not None else None,
            key=str(raw.get("key") or ""),
            ids=list(raw.get("ids") or []),
        )


@dataclass
class WaitRecord:
    """Why a row waits, and what its wait costs and releases.

    ``residency_charged`` is always True for a wait a live run enters: the
    runtime is still resident and the host budget keeps charging it. The flag
    is on the record so the invariant is visible to readers, not so anyone can
    flip it -- reclaiming the runtime is a separate act (``park``) that ends
    the wait rather than editing it.
    """

    state: str
    reason: str
    since: float
    resume_condition: ResumeCondition
    dependency_scope: str | None = None
    cancel_semantics: str = CANCEL_TASK
    evidence_source: str = EVIDENCE_EXECUTION_LAYER
    checkpoint_ref: str | None = None
    tool_call_id: str = ""
    deadline_at: float | None = None
    slot_released: bool = True
    residency_charged: bool = True

    def __post_init__(self) -> None:
        if self.state not in WAITING:
            raise ValueError(f"{self.state!r} is not a waiting state")
        if self.resume_condition.kind not in _KINDS_FOR_STATE[self.state]:
            raise ValueError(
                f"resume condition {self.resume_condition.kind!r} does not fit {self.state!r}"
            )
        if self.cancel_semantics not in CANCEL_SEMANTICS:
            raise ValueError(f"unknown cancel semantics {self.cancel_semantics!r}")
        if self.evidence_source not in EVIDENCE_SOURCES:
            raise ValueError(f"unknown evidence source {self.evidence_source!r}")
        if self.evidence_source == EVIDENCE_MODEL_TEXT:
            # The one source that is never trusted alone (RFC §14.5).
            raise ValueError(
                "model text is not evidence of a wait; it needs an execution-layer source"
            )
        if self.state == WAITING_CHILDREN and not self.resume_condition.ids:
            raise ValueError("a children wait names at least one awaited child")
        if not self.residency_charged:
            raise ValueError(
                "a live wait keeps its residency charge; reclaim the runtime with park() instead"
            )
        self.reason = str(self.reason or "")[:500]

    # -- shapes ---------------------------------------------------------------

    @classmethod
    def children(
        cls,
        ids: Iterable[str],
        *,
        since: float,
        tool_call_id: str = "",
        deadline_at: float | None = None,
        reason: str = "",
    ) -> "WaitRecord":
        ids = [str(i) for i in ids]
        return cls(
            state=WAITING_CHILDREN,
            reason=reason or f"waiting on {len(ids)} subagent(s)",
            since=since,
            resume_condition=ResumeCondition(RESUME_CHILDREN, ids=ids),
            cancel_semantics=CANCEL_TREE,
            evidence_source=EVIDENCE_EXECUTION_LAYER,
            tool_call_id=tool_call_id,
            deadline_at=deadline_at,
        )

    @classmethod
    def dependency(
        cls,
        scope: str,
        *,
        since: float,
        retry_at: float | None = None,
        reason: str = "",
        deadline_at: float | None = None,
        source: str = EVIDENCE_DEPENDENCY_ADAPTER,
    ) -> "WaitRecord":
        cond = (
            ResumeCondition(RESUME_AT_TIME, at=retry_at, key=scope)
            if retry_at is not None
            else ResumeCondition(RESUME_SIGNAL, key=scope)
        )
        return cls(
            state=WAITING_DEPENDENCY,
            reason=reason or f"dependency {scope} unavailable",
            since=since,
            resume_condition=cond,
            dependency_scope=scope,
            cancel_semantics=CANCEL_TASK,
            evidence_source=source,
            deadline_at=deadline_at,
        )

    @classmethod
    def input(
        cls,
        tool_call_id: str,
        *,
        since: float,
        reason: str = "",
        deadline_at: float | None = None,
        source: str = EVIDENCE_EXECUTION_LAYER,
    ) -> "WaitRecord":
        return cls(
            state=WAITING_INPUT,
            reason=reason or "a command is waiting for real user input",
            since=since,
            resume_condition=ResumeCondition(RESUME_INPUT, key=tool_call_id),
            cancel_semantics=CANCEL_CALL,
            evidence_source=source,
            tool_call_id=tool_call_id,
            deadline_at=deadline_at,
        )

    @classmethod
    def permission(
        cls,
        approval_id: str,
        *,
        since: float,
        tool_call_id: str = "",
        reason: str = "",
        deadline_at: float | None = None,
    ) -> "WaitRecord":
        return cls(
            state=WAITING_PERMISSION,
            reason=reason or "a tool call is waiting for approval",
            since=since,
            resume_condition=ResumeCondition(RESUME_PERMISSION, key=approval_id),
            cancel_semantics=CANCEL_CALL,
            evidence_source=EVIDENCE_EXECUTION_LAYER,
            tool_call_id=tool_call_id,
            deadline_at=deadline_at,
        )

    # -- serialization ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["resume_condition"] = self.resume_condition.to_dict()
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "WaitRecord | None":
        if not raw:
            return None
        try:
            cond_raw = raw.get("resume_condition") or {}
            return cls(
                state=str(raw.get("state") or ""),
                reason=str(raw.get("reason") or ""),
                since=float(raw.get("since") or 0.0),
                resume_condition=ResumeCondition.from_dict(cond_raw),
                dependency_scope=raw.get("dependency_scope"),
                cancel_semantics=str(raw.get("cancel_semantics") or CANCEL_TASK),
                evidence_source=str(raw.get("evidence_source") or EVIDENCE_EXECUTION_LAYER),
                checkpoint_ref=raw.get("checkpoint_ref"),
                tool_call_id=str(raw.get("tool_call_id") or ""),
                deadline_at=(
                    float(raw["deadline_at"]) if raw.get("deadline_at") is not None else None
                ),
                slot_released=bool(raw.get("slot_released", True)),
                residency_charged=bool(raw.get("residency_charged", True)),
            )
        except (TypeError, ValueError, AttributeError):
            logger.debug("unreadable wait record %r", raw, exc_info=True)
            return None

    # -- queries ---------------------------------------------------------------

    def remaining_children(self, terminal_ids: Iterable[str]) -> list[str]:
        done = {str(i) for i in terminal_ids}
        return [i for i in self.resume_condition.ids if i not in done]

    def deadline_passed(self, now: float) -> bool:
        return self.deadline_at is not None and now >= self.deadline_at

    def due(self, now: float) -> bool:
        """For an ``at_time`` wait: whether the retry instant has arrived."""
        cond = self.resume_condition
        return cond.kind == RESUME_AT_TIME and cond.at is not None and now >= cond.at


# ── ledger ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChildOutcome:
    """What one child's terminal state means for its parent."""

    parent_id: str | None
    #: The parent is in ``waiting_children`` and this was the LAST awaited child.
    wake_parent: bool
    #: The parent's policy says a failed child fails the parent.
    fail_parent: bool
    #: Children the parent still awaits after this one.
    remaining: tuple[str, ...]
    #: Siblings that should be cancelled because the parent failed fast.
    cancel_siblings: tuple[str, ...] = ()


@dataclass
class RebuildReport:
    woken: list[str] = field(default_factory=list)
    cancelled_orphans: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)


class WaitLedger:
    """Store-backed wait operations for one gateway process.

    Every method is a thin, generation-fenced write on :class:`TaskStore`; the
    ledger keeps no state of its own, so a restart rebuilds everything it needs
    from the rows (:meth:`rebuild`). ``clock`` is injectable for tests.
    """

    def __init__(self, store: TaskStore, *, clock: Callable[[], float] | None = None) -> None:
        self._store = store
        self._clock = clock or store.now

    @property
    def store(self) -> TaskStore:
        return self._store

    def now(self) -> float:
        return float(self._clock())

    # -- entry / exit ----------------------------------------------------------

    def enter(self, task_id: str, record: WaitRecord, *, generation: int | None = None) -> bool:
        """``running -> record.state`` with the record on the row; False when refused."""
        return self._store.enter_wait(task_id, record.to_dict(), generation=generation)

    def wake(
        self,
        task_id: str,
        *,
        reason: str,
        generation: int | None = None,
        resident: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> int | None:
        """End a wait under a NEW generation; returns it, or None.

        ``resident=True`` means the caller holds the lane slot AND the runtime
        is still resident, so the row goes straight to ``running`` (admission's
        ``resume_grant``). The default lands in ``retry_wait``, claimable now:
        whoever re-admits the task (the dispatcher, the runner's own
        re-admission) claims it and writes ``running`` only once capacity was
        granted -- a crash in between leaves a claimable row, not a dead-owner
        ``running`` row. The new generation fences callbacks from the wait
        period either way. ``detail`` is recorded on the ``wake`` event (the
        durable place for an answer delivered to a ``waiting_input`` row).
        """
        return self._store.wake_wait(
            task_id,
            reason=reason,
            generation=generation,
            to=RUNNING if resident else RETRY_WAIT,
            detail=detail,
        )

    def park(
        self,
        task_id: str,
        *,
        next_run_at: float,
        reason: str,
        generation: int | None = None,
    ) -> bool:
        """``waiting_* -> retry_wait``: the runtime was reclaimed; re-dispatch later.

        This is the one write that ends a wait's residency charge, because it
        is only made once the runtime is actually gone.
        """
        return self._store.transition(
            task_id,
            RETRY_WAIT,
            generation=generation,
            next_run_at=next_run_at,
            detail={"reason": reason, "residency_released": True},
        )

    def fail(
        self, task_id: str, *, reason: str, error: str = "", generation: int | None = None
    ) -> bool:
        return self._store.transition(
            task_id,
            FAILED,
            generation=generation,
            detail={"reason": reason, "error": (error or reason)[:500]},
        )

    def record_of(self, task_id: str) -> WaitRecord | None:
        rec = self._store.get(task_id)
        return WaitRecord.from_dict(rec.wait) if rec is not None else None

    # -- children --------------------------------------------------------------

    def children_of(self, parent_id: str) -> list[TaskRecord]:
        return self._store.children_of(parent_id)

    def outstanding_children(self, parent_id: str) -> list[str]:
        """Ids of the parent's children not yet terminal."""
        return [c.id for c in self._store.children_of(parent_id) if c.state not in TERMINAL]

    def on_child_terminal(
        self, child_id: str, child_state: str, *, defer_wake: bool = False
    ) -> ChildOutcome:
        """Apply a child's terminal state to its parent's wait; the caller acts on it.

        A parent not in ``waiting_children`` is left alone (it has runnable work
        of its own); one whose LAST awaited child just ended is woken here --
        to ``retry_wait`` (claimable, the dispatcher re-dispatches it) unless
        ``defer_wake`` is set, in which case the row STAYS ``waiting_children``
        and the caller (admission, for a LIVE resident parent) writes
        ``running`` itself once the pump has granted the slot back. Either way
        ``wake_parent`` tells the caller to re-admit. ``fail_parent`` policy:
        the parent is failed with ``reason=child_failed`` and its remaining
        children are named for cancellation; the caller performs the cancels so
        cancel ordering (children first) stays in one place.
        """
        child = self._store.get(child_id)
        if child is None or not child.parent_id:
            return ChildOutcome(None, False, False, ())
        parent = self._store.get(child.parent_id)
        if parent is None or parent.terminal:
            return ChildOutcome(child.parent_id, False, False, ())
        failed_child = child_state in (FAILED, CANCELLED)
        policy = on_child_failure_policy(parent.params)
        if failed_child and policy == ON_CHILD_FAILURE_FAIL_PARENT:
            siblings = tuple(
                c.id for c in self._store.children_of(parent.id) if c.state not in TERMINAL
            )
            self._store.transition(
                parent.id,
                FAILED,
                detail={
                    "reason": WAIT_REASON_CHILD_FAILED,
                    "child": child_id,
                    "child_state": child_state,
                    "policy": policy,
                },
            )
            return ChildOutcome(parent.id, False, True, (), cancel_siblings=siblings)
        if parent.state != WAITING_CHILDREN:
            return ChildOutcome(parent.id, False, False, ())
        record = WaitRecord.from_dict(parent.wait)
        if record is None:
            return ChildOutcome(parent.id, False, False, ())
        terminal_ids = {c.id for c in self._store.children_of(parent.id) if c.state in TERMINAL}
        terminal_ids.add(child_id)
        remaining = tuple(record.remaining_children(terminal_ids))
        if remaining:
            self._store.append_event(
                parent.id, "child_settled", {"child": child_id, "remaining": list(remaining)}
            )
            return ChildOutcome(parent.id, False, False, remaining)
        if defer_wake:
            self._store.append_event(
                parent.id,
                "children_settled",
                {"child": child_id, "state": child_state, "resume": "through admission"},
            )
            return ChildOutcome(parent.id, True, False, ())
        gen = self.wake(parent.id, reason=f"last awaited child {child_id} {child_state}")
        return ChildOutcome(parent.id, gen is not None, False, ())

    def cancel_tree(self, task_id: str, *, reason: str) -> list[str]:
        """Cancel ``task_id`` and every non-terminal descendant, children first.

        Returns the ids actually cancelled (terminal rows are untouched).
        Children first so a child that completes during the cascade cannot
        wake a parent that is about to be cancelled.
        """
        cancelled: list[str] = []
        for child in self._store.children_of(task_id):
            if child.state in TERMINAL:
                continue
            cancelled.extend(self.cancel_tree(child.id, reason=reason))
        if self._store.cancel(task_id, reason=reason) is not None:
            cancelled.append(task_id)
        return cancelled

    # -- clocks ----------------------------------------------------------------

    def expire(self, now: float | None = None) -> list[str]:
        """Fail every waiting row whose ``deadline_at`` has passed; returns their ids."""
        ts = self.now() if now is None else now
        out: list[str] = []
        for rec in self._store.waiting_rows():
            record = WaitRecord.from_dict(rec.wait)
            if record is None or not record.deadline_passed(ts):
                continue
            if self.fail(
                rec.id,
                reason=WAIT_REASON_DEADLINE,
                error=f"{rec.state} deadline passed after {ts - record.since:.0f}s: {record.reason}",
            ):
                out.append(rec.id)
        return out

    def due_dependency_waits(self, now: float | None = None) -> list[TaskRecord]:
        """``waiting_dependency`` rows whose ``at_time`` has arrived (not yet woken)."""
        ts = self.now() if now is None else now
        out: list[TaskRecord] = []
        for rec in self._store.waiting_rows(state=WAITING_DEPENDENCY):
            record = WaitRecord.from_dict(rec.wait)
            if record is not None and record.due(ts):
                out.append(rec)
        return out

    def signal(self, scope: str, *, reason: str = "") -> list[str]:
        """Wake every ``waiting_dependency`` row for *scope*; returns woken ids.

        The DependencyCoordinator (wave 2 L) calls this by capacity; the ledger
        itself wakes in row order and lets admission meter the re-entries.
        """
        woken: list[str] = []
        for rec in self._store.waiting_rows(state=WAITING_DEPENDENCY):
            record = WaitRecord.from_dict(rec.wait)
            if record is None or record.dependency_scope != scope:
                continue
            if self.wake(rec.id, reason=reason or f"dependency {scope} recovered") is not None:
                woken.append(rec.id)
        return woken

    # -- boot ------------------------------------------------------------------

    def rebuild(self, now: float | None = None) -> RebuildReport:
        """Reconcile waits after a restart from the rows alone.

        * a ``waiting_children`` parent whose awaited children are all terminal
          is woken (its wake was lost with the old process);
        * a non-terminal child whose parent is terminal is cancelled: nothing
          will ever collect its result;
        * a wait past its deadline is failed.
        Idempotent: a second call finds nothing to do.
        """
        ts = self.now() if now is None else now
        report = RebuildReport()
        report.expired = self.expire(ts)
        for rec in self._store.waiting_rows(state=WAITING_CHILDREN):
            record = WaitRecord.from_dict(rec.wait)
            if record is None:
                continue
            terminal_ids = {c.id for c in self._store.children_of(rec.id) if c.state in TERMINAL}
            if record.remaining_children(terminal_ids):
                continue
            if self.wake(rec.id, reason="rebuild: awaited children already terminal") is not None:
                report.woken.append(rec.id)
        for rec in self._store.orphaned_children():
            try:
                if self._store.cancel(rec.id, reason=WAIT_REASON_PARENT_TERMINAL) is not None:
                    report.cancelled_orphans.append(rec.id)
            except TaskStoreUnavailable:
                logger.debug("orphan cancel of %s failed", rec.id, exc_info=True)
        if report.woken or report.cancelled_orphans or report.expired:
            logger.info(
                "taskq waits rebuilt: woken=%d orphans_cancelled=%d expired=%d",
                len(report.woken),
                len(report.cancelled_orphans),
                len(report.expired),
            )
        return report


__all__ = [
    "CANCEL_CALL",
    "CANCEL_SEMANTICS",
    "CANCEL_TASK",
    "CANCEL_TREE",
    "EVIDENCE_DEPENDENCY_ADAPTER",
    "EVIDENCE_EXECUTION_LAYER",
    "EVIDENCE_LIVENESS_ORACLE",
    "EVIDENCE_MODEL_TEXT",
    "EVIDENCE_SOURCES",
    "ON_CHILD_FAILURE_CONTINUE",
    "ON_CHILD_FAILURE_FAIL_PARENT",
    "ON_CHILD_FAILURE_PARAM",
    "ON_CHILD_FAILURE_POLICIES",
    "RESUME_AT_TIME",
    "RESUME_CHILDREN",
    "RESUME_INPUT",
    "RESUME_KINDS",
    "RESUME_PERMISSION",
    "RESUME_SIGNAL",
    "WAIT_REASON_CHILD_FAILED",
    "WAIT_REASON_DEADLINE",
    "WAIT_REASON_PARENT_TERMINAL",
    "ChildOutcome",
    "RebuildReport",
    "ResumeCondition",
    "WaitLedger",
    "WaitRecord",
    "on_child_failure_policy",
]

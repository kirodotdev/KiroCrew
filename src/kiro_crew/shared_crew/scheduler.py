"""The fair admission scheduler: per-principal bounded queues, deficit round robin.

Pure and synchronous: no I/O, no event loop, an injectable clock. Every method
runs under one lock, so a submit, a release and an expiry are each atomic and a
race at the limit admits exactly as many tickets as there are slots.

Two work classes, with separate limits, because they wait differently:

``ROOT``
    A turn, task, workflow or cron run: work a person starts. Admitted when the
    principal has fewer than ``principal_active`` and the Crew fewer than
    ``crew_active`` root slots held; otherwise queued in the principal's FIFO,
    bounded by ``principal_queue`` and ``crew_queue``.
``CHILD``
    Work started by running work (a subagent). Its queue is the durable task
    queue the subagent manager already owns, so the scheduler only bounds how
    many a principal and the Crew have outstanding (``principal_children``,
    ``crew_children``) and refuses past that. It never queues a child here: a
    second queue would be a second scheduler for the same rows.

Fairness across principals is deficit round robin (DRR). Principals with queued
roots sit in a ring. A visit adds ``quantum`` to the principal's deficit and
admits queued tickets while the deficit covers the head ticket's ``weight`` and
both the principal and the Crew have a free slot. A principal that cannot run
(its own slots are full) is skipped and its deficit reset, so credit never
accumulates while ineligible and a principal returning later starts even. One
principal's queue is FIFO, so order within a session is kept.

Starvation bound. Let ``P`` be the number of principals with an eligible head,
``Q`` the quantum and ``W`` the largest ticket weight. While principal B has an
eligible head, every other principal is admitted at most ``Q + W - 1`` times
between two consecutive admissions of B, so B's head is admitted within
``(P - 1) * (Q + W - 1)`` admissions of others. With the defaults (``Q = W =
1``) that is one admission per other eligible principal. Each admission needs
a slot to free, and every admitted root ends within its run deadline, so B's
wall-clock wait is at most ``ceil(((P - 1) * (Q + W - 1) + 1) / crew_active)``
run deadlines. A ticket whose wait would exceed ``queue_expiry`` expires, and
that is reported, never hidden.
"""

from __future__ import annotations

import enum
import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from kiro_crew.shared_crew.admission import CapacityScope, ReasonCode, ScopeUsage

DEFAULT_QUANTUM = 1


class WorkClass(str, enum.Enum):
    ROOT = "root"
    CHILD = "child"


class Decision(str, enum.Enum):
    ADMITTED = "admitted"
    QUEUED = "queued"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SchedulerLimits:
    principal_active: int
    principal_children: int
    principal_queue: int
    crew_active: int
    crew_children: int
    crew_queue: int
    quantum: int = DEFAULT_QUANTUM

    def __post_init__(self) -> None:
        for name in ("principal_active", "principal_children", "crew_active", "crew_children"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.principal_queue < 0 or self.crew_queue < 0 or self.quantum < 1:
            raise ValueError("queue depths must be non-negative and quantum positive")


@dataclass
class Ticket:
    """One unit of work the scheduler accounts for, keyed by its ``work_id``."""

    work_id: str
    budget_key: str
    session_key: str
    work_class: WorkClass = WorkClass.ROOT
    slots: int = 1
    weight: int = 1
    expires_at: float = float("inf")
    enqueued_at: float = 0.0
    seq: int = 0


@dataclass(frozen=True)
class SubmitResult:
    decision: Decision
    ticket: Ticket
    reason: ReasonCode | None = None
    limiting_scope: CapacityScope | None = None
    #: Other queued tickets this call admitted as a side effect, in admission order.
    also_admitted: tuple[Ticket, ...] = ()


@dataclass(frozen=True)
class ReleaseResult:
    #: False when the work id held nothing: a second release is a no-op.
    released: bool
    admitted: tuple[Ticket, ...] = ()


@dataclass
class _Principal:
    queue: deque[Ticket] = field(default_factory=deque)
    deficit: int = 0
    root_active: int = 0
    child_active: int = 0
    #: Parked tickets (waiting on their own session's turn, not on capacity).
    parked: int = 0


class FairScheduler:
    """Principal- and Crew-level admission with DRR across principals."""

    def __init__(self, limits: SchedulerLimits, *, clock: Callable[[], float] = time.time) -> None:
        self._limits = limits
        self._clock = clock
        self._lock = threading.RLock()
        self._principals: dict[str, _Principal] = {}
        #: Principals with queued roots, in DRR visiting order.
        self._ring: deque[str] = deque()
        #: work_id -> ticket for every ticket holding a slot.
        self._active: dict[str, Ticket] = {}
        #: work_id -> ticket for every queued ticket.
        self._queued: dict[str, Ticket] = {}
        #: work_id -> ticket for every parked ticket: counted against queue depth,
        #: never dispatched until :meth:`submit_parked` hands it to the queue.
        self._parked: dict[str, Ticket] = {}
        self._crew_root_active = 0
        self._crew_child_active = 0
        self._seq = itertools.count(1)

    # -- configuration ------------------------------------------------------

    @property
    def limits(self) -> SchedulerLimits:
        return self._limits

    def set_limits(self, limits: SchedulerLimits) -> tuple[Ticket, ...]:
        """Install new limits; a widening admits queued work right away."""
        with self._lock:
            self._limits = limits
            return tuple(self._dispatch())

    # -- the three verbs ----------------------------------------------------

    def submit(self, ticket: Ticket, *, allow_queue: bool = True) -> SubmitResult:
        """Admit, queue or refuse *ticket*. A known work id answers its current place.

        ``allow_queue=False`` is for work that cannot wait (a background run the
        caller starts and returns from): where it would queue it is refused with
        the active-limit reason instead.
        """
        with self._lock:
            if ticket.work_id in self._active:
                return SubmitResult(Decision.ADMITTED, self._active[ticket.work_id])
            if ticket.work_id in self._queued:
                return SubmitResult(Decision.QUEUED, self._queued[ticket.work_id])
            ticket.slots = max(1, int(ticket.slots))
            ticket.weight = max(1, int(ticket.weight))
            if ticket.work_class is WorkClass.CHILD:
                return self._submit_child(ticket)
            if not allow_queue:
                return self._submit_root_now(ticket)
            return self._submit_root(ticket)

    def release(self, work_id: str) -> ReleaseResult:
        """Return *work_id*'s slots and admit whatever now fits. Idempotent."""
        with self._lock:
            ticket = self._active.pop(work_id, None)
            if ticket is None:
                return ReleaseResult(False)
            p = self._principals.get(ticket.budget_key)
            if ticket.work_class is WorkClass.CHILD:
                self._crew_child_active -= ticket.slots
                if p is not None:
                    p.child_active -= ticket.slots
            else:
                self._crew_root_active -= ticket.slots
                if p is not None:
                    p.root_active -= ticket.slots
            self._forget_if_idle(ticket.budget_key)
            return ReleaseResult(True, tuple(self._dispatch()))

    def cancel_queued(self, work_id: str) -> Ticket | None:
        """Drop a queued ticket. Returns it, or None when it was not queued."""
        with self._lock:
            if work_id in self._parked:
                return self._unpark(work_id)
            ticket = self._queued.pop(work_id, None)
            if ticket is None:
                return None
            p = self._principals.get(ticket.budget_key)
            if p is not None:
                try:
                    p.queue.remove(ticket)
                except ValueError:
                    pass
                if not p.queue:
                    self._leave_ring(ticket.budget_key)
                self._forget_if_idle(ticket.budget_key)
            return ticket

    def expire(self, now: float | None = None) -> tuple[Ticket, ...]:
        """Remove and return every queued ticket whose expiry has passed."""
        with self._lock:
            at = self._clock() if now is None else now
            expired = [t for t in self._queued.values() if t.expires_at <= at]
            for ticket in sorted(expired, key=lambda t: t.seq):
                self.cancel_queued(ticket.work_id)
            return tuple(sorted(expired, key=lambda t: t.seq))

    def hold(self, ticket: Ticket) -> None:
        """Count *ticket* as holding capacity without any limit check.

        For reconciliation only: durable work a restart found in a
        capacity-holding state keeps its slot until it is proven stopped, even
        when that leaves a scope over its limit. Nothing new is admitted into a
        scope until it is back under.
        """
        with self._lock:
            if ticket.work_id in self._active:
                return
            ticket.slots = max(1, int(ticket.slots))
            ticket.seq = ticket.seq or next(self._seq)
            self._activate(ticket)

    def park(self, ticket: Ticket) -> SubmitResult:
        """Count *ticket* against queue depth while it waits on its own session.

        A message sent to a session whose turn is running waits for that turn,
        not for capacity, so it is not dispatched; but it is bounded by the same
        principal and Crew queue depths and shows as queued. Its session's drain
        hands it to the fair queue with :meth:`submit_parked`.
        """
        with self._lock:
            if ticket.work_id in self._parked:
                return SubmitResult(Decision.QUEUED, self._parked[ticket.work_id])
            lim = self._limits
            p = self._principal(ticket.budget_key)
            if len(p.queue) + p.parked >= lim.principal_queue:
                self._forget_if_idle(ticket.budget_key)
                return SubmitResult(
                    Decision.REJECTED,
                    ticket,
                    ReasonCode.PRINCIPAL_QUEUE_FULL,
                    CapacityScope.PRINCIPAL,
                )
            if len(self._queued) + len(self._parked) >= lim.crew_queue:
                self._forget_if_idle(ticket.budget_key)
                return SubmitResult(
                    Decision.REJECTED, ticket, ReasonCode.CREW_QUEUE_FULL, CapacityScope.CREW
                )
            ticket.slots = max(1, int(ticket.slots))
            ticket.weight = max(1, int(ticket.weight))
            ticket.seq = next(self._seq)
            ticket.enqueued_at = self._clock()
            self._parked[ticket.work_id] = ticket
            p.parked += 1
            return SubmitResult(Decision.QUEUED, ticket)

    def submit_parked(self, work_id: str, *, expires_at: float) -> SubmitResult | None:
        """Move a parked ticket into the fair queue (or straight to a slot). None if not parked.

        Atomic: the ticket's own queue place is given up and retaken under one
        lock, so a full queue cannot refuse work it already counted.
        """
        with self._lock:
            ticket = self._parked.get(work_id)
            if ticket is None:
                return None
            self._unpark(work_id)
            ticket.expires_at = expires_at
            return self._submit_root(ticket)

    def is_parked(self, work_id: str) -> bool:
        with self._lock:
            return work_id in self._parked

    def parked_tickets(self) -> tuple[Ticket, ...]:
        with self._lock:
            return tuple(sorted(self._parked.values(), key=lambda t: t.seq))

    # -- reads --------------------------------------------------------------

    def is_active(self, work_id: str) -> bool:
        with self._lock:
            return work_id in self._active

    def is_queued(self, work_id: str) -> bool:
        with self._lock:
            return work_id in self._queued

    def usage(self, budget_key: str) -> tuple[ScopeUsage, ScopeUsage]:
        """``(principal, crew)`` root usage with limits, for a capacity answer."""
        with self._lock:
            p = self._principals.get(budget_key) or _Principal()
            lim = self._limits
            principal = ScopeUsage(
                active=p.root_active,
                queued=len(p.queue) + p.parked,
                active_limit=lim.principal_active,
                queue_limit=lim.principal_queue,
            )
            crew = ScopeUsage(
                active=self._crew_root_active,
                queued=len(self._queued) + len(self._parked),
                active_limit=lim.crew_active,
                queue_limit=lim.crew_queue,
            )
            return principal, crew

    def snapshot(self) -> dict[str, object]:
        """Counts for diagnostics. Principal ids are opaque digests already."""
        with self._lock:
            return {
                "crew_root_active": self._crew_root_active,
                "crew_child_active": self._crew_child_active,
                "queued": len(self._queued),
                "parked": len(self._parked),
                "ring": list(self._ring),
                "principals": {
                    key: {
                        "root_active": p.root_active,
                        "child_active": p.child_active,
                        "queued": len(p.queue),
                        "deficit": p.deficit,
                    }
                    for key, p in sorted(self._principals.items())
                },
            }

    def active_tickets(self) -> tuple[Ticket, ...]:
        with self._lock:
            return tuple(self._active.values())

    # -- internals ----------------------------------------------------------

    def _principal(self, key: str) -> _Principal:
        p = self._principals.get(key)
        if p is None:
            p = self._principals[key] = _Principal()
        return p

    def _submit_child(self, ticket: Ticket) -> SubmitResult:
        lim = self._limits
        p = self._principal(ticket.budget_key)
        if p.child_active + ticket.slots > lim.principal_children:
            self._forget_if_idle(ticket.budget_key)
            return SubmitResult(
                Decision.REJECTED,
                ticket,
                ReasonCode.PRINCIPAL_QUEUE_FULL,
                CapacityScope.PRINCIPAL,
            )
        if self._crew_child_active + ticket.slots > lim.crew_children:
            self._forget_if_idle(ticket.budget_key)
            return SubmitResult(
                Decision.REJECTED, ticket, ReasonCode.CREW_QUEUE_FULL, CapacityScope.CREW
            )
        ticket.seq = next(self._seq)
        self._activate(ticket)
        return SubmitResult(Decision.ADMITTED, ticket)

    def _submit_root_now(self, ticket: Ticket) -> SubmitResult:
        lim = self._limits
        p = self._principal(ticket.budget_key)
        if p.root_active + ticket.slots > lim.principal_active or p.queue:
            self._forget_if_idle(ticket.budget_key)
            return SubmitResult(
                Decision.REJECTED,
                ticket,
                ReasonCode.PRINCIPAL_ACTIVE_LIMIT,
                CapacityScope.PRINCIPAL,
            )
        if self._crew_root_active + ticket.slots > lim.crew_active:
            self._forget_if_idle(ticket.budget_key)
            return SubmitResult(
                Decision.REJECTED, ticket, ReasonCode.CREW_ACTIVE_LIMIT, CapacityScope.CREW
            )
        ticket.seq = next(self._seq)
        self._activate(ticket)
        return SubmitResult(Decision.ADMITTED, ticket)

    def _submit_root(self, ticket: Ticket) -> SubmitResult:
        lim = self._limits
        p = self._principal(ticket.budget_key)
        principal_room = p.root_active + ticket.slots <= lim.principal_active
        crew_room = self._crew_root_active + ticket.slots <= lim.crew_active
        if principal_room and crew_room and not p.queue:
            # Nothing of this principal's is waiting, so an immediate admission
            # cannot overtake its own earlier work; any other principal's queued
            # work is waiting on its own limit, or the Crew would be full.
            ticket.seq = next(self._seq)
            self._activate(ticket)
            return SubmitResult(Decision.ADMITTED, ticket)
        if len(p.queue) + p.parked >= lim.principal_queue:
            self._forget_if_idle(ticket.budget_key)
            return SubmitResult(
                Decision.REJECTED,
                ticket,
                ReasonCode.PRINCIPAL_QUEUE_FULL,
                CapacityScope.PRINCIPAL,
            )
        if len(self._queued) + len(self._parked) >= lim.crew_queue:
            self._forget_if_idle(ticket.budget_key)
            return SubmitResult(
                Decision.REJECTED, ticket, ReasonCode.CREW_QUEUE_FULL, CapacityScope.CREW
            )
        ticket.seq = next(self._seq)
        ticket.enqueued_at = self._clock()
        p.queue.append(ticket)
        self._queued[ticket.work_id] = ticket
        if ticket.budget_key not in self._ring:
            self._ring.append(ticket.budget_key)
        admitted = self._dispatch()
        if ticket.work_id in self._active:
            others = tuple(t for t in admitted if t.work_id != ticket.work_id)
            return SubmitResult(Decision.ADMITTED, ticket, also_admitted=others)
        reason, scope = (
            (ReasonCode.PRINCIPAL_ACTIVE_LIMIT, CapacityScope.PRINCIPAL)
            if p.root_active + ticket.slots > lim.principal_active
            else (ReasonCode.CREW_ACTIVE_LIMIT, CapacityScope.CREW)
        )
        return SubmitResult(Decision.QUEUED, ticket, reason, scope, tuple(admitted))

    def _activate(self, ticket: Ticket) -> None:
        p = self._principal(ticket.budget_key)
        self._active[ticket.work_id] = ticket
        if ticket.work_class is WorkClass.CHILD:
            p.child_active += ticket.slots
            self._crew_child_active += ticket.slots
        else:
            p.root_active += ticket.slots
            self._crew_root_active += ticket.slots

    def _dispatch(self) -> list[Ticket]:
        """Admit queued roots in DRR order while the Crew has room."""
        lim = self._limits
        admitted: list[Ticket] = []
        while self._ring and self._crew_root_active < lim.crew_active:
            progressed = False
            for _ in range(len(self._ring)):
                if not self._ring or self._crew_root_active >= lim.crew_active:
                    break
                key = self._ring[0]
                p = self._principals[key]
                head = p.queue[0] if p.queue else None
                if head is None:
                    self._ring.popleft()
                    p.deficit = 0
                    continue
                if p.root_active + head.slots > lim.principal_active:
                    p.deficit = 0
                    self._ring.rotate(-1)
                    continue
                p.deficit += lim.quantum
                while (
                    p.queue
                    and p.deficit >= p.queue[0].weight
                    and p.root_active + p.queue[0].slots <= lim.principal_active
                    and self._crew_root_active + p.queue[0].slots <= lim.crew_active
                ):
                    ticket = p.queue.popleft()
                    self._queued.pop(ticket.work_id, None)
                    p.deficit -= ticket.weight
                    self._activate(ticket)
                    admitted.append(ticket)
                    progressed = True
                if not p.queue:
                    p.deficit = 0
                    self._ring.popleft()
                else:
                    self._ring.rotate(-1)
            if not progressed:
                break
        return admitted

    def _leave_ring(self, key: str) -> None:
        try:
            self._ring.remove(key)
        except ValueError:
            pass
        p = self._principals.get(key)
        if p is not None:
            p.deficit = 0

    def _unpark(self, work_id: str) -> Ticket | None:
        ticket = self._parked.pop(work_id, None)
        if ticket is None:
            return None
        p = self._principals.get(ticket.budget_key)
        if p is not None:
            p.parked = max(0, p.parked - 1)
            self._forget_if_idle(ticket.budget_key)
        return ticket

    def _forget_if_idle(self, key: str) -> None:
        p = self._principals.get(key)
        if (
            p is not None
            and not p.queue
            and not p.root_active
            and not p.child_active
            and not p.parked
        ):
            self._principals.pop(key, None)
            self._leave_ring(key)


__all__ = [
    "DEFAULT_QUANTUM",
    "Decision",
    "FairScheduler",
    "ReleaseResult",
    "SchedulerLimits",
    "SubmitResult",
    "Ticket",
    "WorkClass",
]

"""The fair admission scheduler: limits, bounded queues, DRR fairness, idempotent release."""

from __future__ import annotations

import threading

import pytest

from kiro_crew.shared_crew.admission import CapacityScope, ReasonCode
from kiro_crew.shared_crew.scheduler import (
    Decision,
    FairScheduler,
    SchedulerLimits,
    Ticket,
    WorkClass,
)
from kiro_crew.testing.clock import ManualClock

A, B, C = "p_alice", "p_bob", "p_carol"


def _limits(**kw: int) -> SchedulerLimits:
    base = dict(
        principal_active=2,
        principal_children=3,
        principal_queue=4,
        crew_active=4,
        crew_children=6,
        crew_queue=10,
    )
    base.update(kw)
    return SchedulerLimits(**base)


def _t(work_id: str, who: str, session: str = "s", cls: WorkClass = WorkClass.ROOT, **kw) -> Ticket:
    return Ticket(work_id=work_id, budget_key=who, session_key=session, work_class=cls, **kw)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(start=1_000.0)


def test_one_principal_across_sessions_shares_one_budget(clock):
    s = FairScheduler(_limits(), clock=clock.time)
    # Three tabs / sessions of the same person: the third waits.
    assert s.submit(_t("w1", A, "tab1")).decision is Decision.ADMITTED
    assert s.submit(_t("w2", A, "tab2")).decision is Decision.ADMITTED
    third = s.submit(_t("w3", A, "tab3"))
    assert third.decision is Decision.QUEUED
    assert third.reason is ReasonCode.PRINCIPAL_ACTIVE_LIMIT
    assert third.limiting_scope is CapacityScope.PRINCIPAL
    # Another person is not affected by Alice's limit.
    assert s.submit(_t("w4", B)).decision is Decision.ADMITTED


def test_crew_cap_queues_with_crew_reason(clock):
    s = FairScheduler(_limits(principal_active=4, crew_active=2), clock=clock.time)
    assert s.submit(_t("a1", A)).decision is Decision.ADMITTED
    assert s.submit(_t("b1", B)).decision is Decision.ADMITTED
    r = s.submit(_t("c1", C))
    assert (r.decision, r.reason, r.limiting_scope) == (
        Decision.QUEUED,
        ReasonCode.CREW_ACTIVE_LIMIT,
        CapacityScope.CREW,
    )


def test_bounded_queue_rejects_with_reason_and_crew_queue_too(clock):
    s = FairScheduler(
        _limits(principal_active=1, principal_queue=2, crew_queue=3), clock=clock.time
    )
    assert s.submit(_t("a0", A)).decision is Decision.ADMITTED
    assert s.submit(_t("a1", A)).decision is Decision.QUEUED
    assert s.submit(_t("a2", A)).decision is Decision.QUEUED
    full = s.submit(_t("a3", A))
    assert (full.decision, full.reason) == (Decision.REJECTED, ReasonCode.PRINCIPAL_QUEUE_FULL)
    assert not s.is_queued("a3") and not s.is_active("a3")
    assert s.submit(_t("b0", B)).decision is Decision.ADMITTED
    assert s.submit(_t("b1", B)).decision is Decision.QUEUED  # crew queue now 3
    assert s.submit(_t("c1", C)).decision is Decision.ADMITTED  # C has room, the crew slots
    s2 = FairScheduler(_limits(principal_active=1, crew_active=1, crew_queue=1), clock=clock.time)
    s2.submit(_t("x0", A))
    assert s2.submit(_t("x1", B)).decision is Decision.QUEUED
    r = s2.submit(_t("x2", C))
    assert (r.decision, r.reason, r.limiting_scope) == (
        Decision.REJECTED,
        ReasonCode.CREW_QUEUE_FULL,
        CapacityScope.CREW,
    )


def test_release_is_idempotent_and_admits_next_in_fifo_within_session(clock):
    s = FairScheduler(_limits(principal_active=1), clock=clock.time)
    s.submit(_t("a0", A, "s1"))
    s.submit(_t("a1", A, "s1"))
    s.submit(_t("a2", A, "s1"))
    first = s.release("a0")
    assert first.released and [t.work_id for t in first.admitted] == ["a1"]
    again = s.release("a0")
    assert not again.released and again.admitted == ()
    assert s.usage(A)[0].active == 1
    assert [t.work_id for t in s.release("a1").admitted] == ["a2"]


def test_children_count_outstanding_and_never_queue(clock):
    s = FairScheduler(_limits(principal_children=2, crew_children=3), clock=clock.time)
    assert s.submit(_t("c1", A, cls=WorkClass.CHILD)).decision is Decision.ADMITTED
    assert s.submit(_t("c2", A, cls=WorkClass.CHILD)).decision is Decision.ADMITTED
    r = s.submit(_t("c3", A, cls=WorkClass.CHILD))
    assert (r.decision, r.reason) == (Decision.REJECTED, ReasonCode.PRINCIPAL_QUEUE_FULL)
    assert s.submit(_t("c4", B, cls=WorkClass.CHILD)).decision is Decision.ADMITTED
    crew = s.submit(_t("c5", C, cls=WorkClass.CHILD))
    assert (crew.decision, crew.reason) == (Decision.REJECTED, ReasonCode.CREW_QUEUE_FULL)
    # Children do not consume root slots.
    assert s.usage(A)[0].active == 0
    s.release("c1")
    assert s.submit(_t("c6", A, cls=WorkClass.CHILD)).decision is Decision.ADMITTED


def test_fairness_flooder_cannot_starve_a_second_principal(clock):
    """A floods 12 sessions; B's work is admitted within the published bound."""
    lim = _limits(principal_active=3, principal_queue=16, crew_active=3, crew_queue=64)
    s = FairScheduler(lim, clock=clock.time)
    for i in range(3):
        assert s.submit(_t(f"a{i}", A, f"sa{i}")).decision is Decision.ADMITTED
    for i in range(3, 15):
        assert s.submit(_t(f"a{i}", A, f"sa{i}")).decision is Decision.QUEUED
    b = s.submit(_t("b0", B, "sb"))
    assert b.decision is Decision.QUEUED and b.reason is ReasonCode.CREW_ACTIVE_LIMIT
    admitted_order: list[str] = []
    # Free one slot at a time, oldest first, recording who gets each.
    running = ["a0", "a1", "a2"]
    while "b0" not in admitted_order:
        done = running.pop(0)
        new = [t.work_id for t in s.release(done).admitted]
        admitted_order += new
        running += new
    others_before_b = admitted_order.index("b0")
    # (eligible principals - 1) * (quantum + max weight - 1) = 1 with weight 1.
    assert others_before_b <= 1


def test_fairness_three_principals_round_robin(clock):
    s = FairScheduler(
        _limits(principal_active=4, crew_active=1, crew_queue=64, principal_queue=16),
        clock=clock.time,
    )
    s.submit(_t("hold", C))
    for i in range(4):
        s.submit(_t(f"a{i}", A))
    for i in range(2):
        s.submit(_t(f"b{i}", B))
    order: list[str] = []
    current = "hold"
    while True:
        nxt = s.release(current).admitted
        if not nxt:
            break
        current = nxt[0].work_id
        order.append(current)
    # A, B alternate while both have work; A's surplus runs after B is drained.
    assert order == ["a0", "b0", "a1", "b1", "a2", "a3"]


def test_ineligible_principal_does_not_bank_credit(clock):
    s = FairScheduler(
        _limits(principal_active=1, crew_active=2, principal_queue=8), clock=clock.time
    )
    s.submit(_t("a0", A))
    s.submit(_t("a1", A))  # queued behind A's own limit
    s.submit(_t("b0", B))
    s.submit(_t("b1", B))  # queued behind B's own limit
    assert s.snapshot()["principals"][A]["deficit"] == 0
    # B's slot frees; A is still at its limit so B goes, and A gained nothing.
    assert [t.work_id for t in s.release("b0").admitted] == ["b1"]
    assert s.snapshot()["principals"][A]["deficit"] == 0


def test_race_at_the_limit_admits_exactly_the_slot_count(clock):
    s = FairScheduler(
        _limits(principal_active=50, crew_active=5, crew_queue=500, principal_queue=500),
        clock=clock.time,
    )
    barrier = threading.Barrier(20)
    results: list[Decision] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait(timeout=30)
        r = s.submit(_t(f"w{i}", f"p_{i % 4}"))
        with lock:
            results.append(r.decision)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)
        assert not th.is_alive()
    assert results.count(Decision.ADMITTED) == 5
    assert results.count(Decision.QUEUED) == 15


def test_duplicate_submission_answers_current_place(clock):
    s = FairScheduler(_limits(principal_active=1), clock=clock.time)
    s.submit(_t("a0", A))
    q = s.submit(_t("a1", A))
    again = s.submit(_t("a1", A))
    assert q.decision is again.decision is Decision.QUEUED
    assert s.usage(A)[0].queued == 1


def test_expiry_removes_only_expired_queued_tickets(clock):
    s = FairScheduler(_limits(principal_active=1), clock=clock.time)
    s.submit(_t("a0", A))
    s.submit(_t("a1", A, expires_at=clock.time() + 300))
    s.submit(_t("a2", A, expires_at=clock.time() + 600))
    clock.advance(299)
    assert s.expire() == ()
    clock.advance(1)
    assert [t.work_id for t in s.expire()] == ["a1"]
    assert s.is_queued("a2") and s.is_active("a0")


def test_hold_counts_reconciling_work_over_the_limit(clock):
    s = FairScheduler(_limits(principal_active=1, crew_active=1), clock=clock.time)
    s.hold(_t("r0", A))
    s.hold(_t("r1", B))
    assert s.usage(A)[1].active == 2  # over the crew limit until proven stopped
    assert s.submit(_t("n0", C)).decision is Decision.QUEUED
    assert s.release("r0").admitted == ()  # still at the limit with r1 held
    assert [t.work_id for t in s.release("r1").admitted] == ["n0"]


def test_widening_limits_admits_queued_work(clock):
    s = FairScheduler(_limits(principal_active=1), clock=clock.time)
    s.submit(_t("a0", A))
    s.submit(_t("a1", A))
    admitted = s.set_limits(_limits(principal_active=2))
    assert [t.work_id for t in admitted] == ["a1"]


def test_limits_validation():
    with pytest.raises(ValueError):
        _limits(crew_active=0)
    with pytest.raises(ValueError):
        _limits(principal_queue=-1)


# -- park, background starts, reads ---------------------------------------------


def test_park_counts_against_queue_depth_and_never_dispatches(clock):
    s = FairScheduler(_limits(principal_queue=2, crew_queue=3), clock=clock.time)
    first = s.park(_t("p1", A))
    assert first.decision is Decision.QUEUED
    assert s.park(_t("p1", A)).ticket is first.ticket  # known id answers its place
    assert s.is_parked("p1") and not s.is_queued("p1") and not s.is_active("p1")
    assert s.park(_t("p2", A)).decision is Decision.QUEUED
    full = s.park(_t("p3", A))
    assert (full.decision, full.reason) == (Decision.REJECTED, ReasonCode.PRINCIPAL_QUEUE_FULL)
    assert s.park(_t("p4", B)).decision is Decision.QUEUED
    crew = s.park(_t("p5", C))
    assert (crew.decision, crew.reason, crew.limiting_scope) == (
        Decision.REJECTED,
        ReasonCode.CREW_QUEUE_FULL,
        CapacityScope.CREW,
    )
    principal, crew_usage = s.usage(A)
    assert principal.queued == 2 and crew_usage.queued == 3
    assert [t.work_id for t in s.parked_tickets()] == ["p1", "p2", "p4"]
    assert s.snapshot()["parked"] == 3
    # Parked work holds no slot: a root still starts at once.
    assert s.submit(_t("r1", A)).decision is Decision.ADMITTED


def test_submit_parked_moves_ticket_into_the_fair_queue(clock):
    s = FairScheduler(_limits(principal_active=1, principal_queue=1), clock=clock.time)
    assert s.submit(_t("r1", A)).decision is Decision.ADMITTED
    assert s.park(_t("p1", A)).decision is Decision.QUEUED
    # A full queue does not refuse work it already counted.
    moved = s.submit_parked("p1", expires_at=2_000.0)
    assert moved is not None and moved.decision is Decision.QUEUED
    assert moved.ticket.expires_at == 2_000.0
    assert s.is_queued("p1") and not s.is_parked("p1")
    assert s.submit_parked("p1", expires_at=1.0) is None
    released = s.release("r1")
    assert [t.work_id for t in released.admitted] == ["p1"]


def test_cancel_parked_ticket_and_unknown(clock):
    s = FairScheduler(_limits(), clock=clock.time)
    s.park(_t("p1", A))
    cancelled = s.cancel_queued("p1")
    assert cancelled is not None and cancelled.work_id == "p1"
    assert not s.is_parked("p1")
    assert s.cancel_queued("p1") is None
    assert s.snapshot()["principals"] == {}


def test_background_root_starts_now_or_is_refused(clock):
    s = FairScheduler(_limits(principal_active=1, crew_active=2), clock=clock.time)
    assert s.submit(_t("a1", A), allow_queue=False).decision is Decision.ADMITTED
    mine = s.submit(_t("a2", A), allow_queue=False)
    assert (mine.decision, mine.reason) == (Decision.REJECTED, ReasonCode.PRINCIPAL_ACTIVE_LIMIT)
    assert s.submit(_t("b1", B), allow_queue=False).decision is Decision.ADMITTED
    crew = s.submit(_t("c1", C), allow_queue=False)
    assert (crew.decision, crew.reason, crew.limiting_scope) == (
        Decision.REJECTED,
        ReasonCode.CREW_ACTIVE_LIMIT,
        CapacityScope.CREW,
    )
    assert not s.is_queued("c1")
    # Queued work of the same principal is never overtaken by a background start.
    s.release("a1")
    s.release("b1")
    s2 = FairScheduler(_limits(principal_active=1), clock=clock.time)
    s2.submit(_t("x1", A))
    s2.submit(_t("x2", A))
    assert s2.submit(_t("x3", A), allow_queue=False).decision is Decision.REJECTED


def test_known_ids_answer_their_place_and_reads(clock):
    s = FairScheduler(_limits(principal_active=1), clock=clock.time)
    s.submit(_t("a1", A))
    s.submit(_t("a2", A))
    assert s.submit(_t("a1", A)).decision is Decision.ADMITTED
    assert s.submit(_t("a2", A)).decision is Decision.QUEUED
    assert s.limits.principal_active == 1
    assert [t.work_id for t in s.active_tickets()] == ["a1"]


def test_set_limits_widening_admits_queued(clock):
    s = FairScheduler(_limits(principal_active=1), clock=clock.time)
    s.submit(_t("a1", A))
    s.submit(_t("a2", A))
    admitted = s.set_limits(_limits(principal_active=2))
    assert [t.work_id for t in admitted] == ["a2"]


def test_hold_is_idempotent_and_ignores_limits(clock):
    s = FairScheduler(_limits(principal_active=1), clock=clock.time)
    s.hold(_t("h1", A))
    s.hold(_t("h2", A))
    s.hold(_t("h1", A))
    principal, _ = s.usage(A)
    assert principal.active == 2

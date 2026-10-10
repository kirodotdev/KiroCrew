"""Start priority (``kiro_crew.start_priority``): the primitive and who claims it.

The primitive's ordering, reserve and drain are pinned directly. Each queue that
uses it is pinned by the priority that reaches ``PrioritySemaphore.acquire``, each
origin rule by its decision, and each leaf claimer by its call site. The
``session/new`` scenario through the real ``create_session`` lives in
``test_session_start_gate.py``.

Every wait is bounded and fails by name (``until`` / ``bounded``), and every test
that takes permits or starts tasks gives them back in a bounded ``finally`` and
checks the semaphore is whole again (testing-conventions class 6).
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar
from unittest.mock import MagicMock

import pytest
from test_session import _mock_provider_factory

import kiro_crew.acp.runtime as runtime_mod
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.runtime_start import _ColdStartAdmission
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.chat_runner import _actor_for_queue_items, _turn_start_priority
from kiro_crew.messaging.dispatch import ChannelTurn
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.session import SessionManager
from kiro_crew.session_allocation import (
    FOREGROUND_COLD_START_RESERVE,
    MAX_CONCURRENT_COLD_STARTS,
    new_cold_start_semaphore,
)
from kiro_crew.start_priority import (
    _FOREGROUND_BYPASS_LIMIT,
    START_QUEUE_ADMISSION,
    START_QUEUE_COLD_START,
    PrioritySemaphore,
    StartPriority,
    person_priority,
)

#: This module drives real cold starts through ``SessionManager``, and one test
#: reaches ``SubagentManager.spawn``, which refuses while the host looks short of
#: memory -- the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

FG = StartPriority.FOREGROUND
BG = StartPriority.BACKGROUND
T = TypeVar("T")

#: Upper bound on a wait a real signal ends. Shared with test_session_start_gate.py.
SETTLE_BACKSTOP = 10.0

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


async def until(predicate: Callable[[], bool], what: str) -> None:
    """Yield until *predicate* holds; fail naming *what* after the backstop."""

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(_poll(), timeout=SETTLE_BACKSTOP)
    except asyncio.TimeoutError:
        raise AssertionError(f"never reached: {what}") from None


async def bounded(awaitable: Awaitable[T], what: str) -> T:
    """Await *awaitable*; fail naming *what* after the backstop."""
    try:
        return await asyncio.wait_for(awaitable, timeout=SETTLE_BACKSTOP)
    except asyncio.TimeoutError:
        raise AssertionError(f"never finished: {what}") from None


async def settle_tasks(tasks: Iterable[asyncio.Task], what: str) -> None:
    """Cancel what is still running and wait for all of it, bounded."""
    pending = list(tasks)
    for task in pending:
        if not task.done():
            task.cancel()
    await bounded(asyncio.gather(*pending, return_exceptions=True), f"cleanup of {what}")


class _Order:
    """Holds permits of *sem* and queues acquires, recording who got in and when.

    Every permit it holds -- taken up front or granted to a queued task -- is
    remembered with its priority, so ``grant`` and ``close`` release at the
    priority the permit was acquired at.
    """

    def __init__(self, sem: PrioritySemaphore) -> None:
        self.sem = sem
        self.order: list[str] = []
        self.tasks: list[asyncio.Task] = []
        self.holds: list[StartPriority] = []

    async def take(self, priority: StartPriority, count: int = 1) -> None:
        for _ in range(count):
            await bounded(self.sem.acquire(priority), f"take {priority.name}")
            self.holds.append(priority)

    def queue(self, name: str, priority: StartPriority) -> None:
        async def _take() -> None:
            await self.sem.acquire(priority)
            self.holds.append(priority)
            self.order.append(name)

        self.tasks.append(asyncio.create_task(_take()))

    async def settle(self, expected_queued: int) -> None:
        await until(lambda: self.sem.queued == expected_queued, f"{expected_queued} queued")

    async def grant(self, count: int = 1) -> None:
        """Release the *count* oldest holds; wait for that many queued acquires."""
        for _ in range(count):
            before = len(self.order)
            self.sem.release(self.holds.pop(0))
            await until(lambda: len(self.order) == before + 1, "the next grant")

    async def close(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await settle_tasks(self.tasks, "queued acquires")
        while self.holds:
            self.sem.release(self.holds.pop(0))
        assert self.sem._value == self.sem._limit, "a permit was not returned"


# ── the primitive ─────────────────────────────────────────────────────────────


def test_start_priority_has_exactly_the_two_members_the_semaphore_queues():
    """A third member would have no queue to wait in (one queue per member)."""
    assert list(StartPriority) == [FG, BG]


@pytest.mark.parametrize(("value", "reserve"), [(0, 0), (1, 1), (2, 2), (4, 4), (3, 5), (2, -1)])
def test_a_width_and_reserve_that_leave_background_nothing_are_refused(value, reserve):
    with pytest.raises(ValueError):
        PrioritySemaphore(value, foreground_reserve=reserve)


@pytest.mark.asyncio
async def test_a_foreground_waiter_is_served_before_earlier_background_ones():
    order = _Order(PrioritySemaphore(2))
    try:
        await order.take(BG, 2)
        for name in ("bg1", "bg2", "bg3"):
            order.queue(name, BG)
        await order.settle(3)
        order.queue("fg", FG)
        await order.settle(4)
        await order.grant()
        assert order.order == ["fg"]
        await order.grant(3)
        assert order.order == ["fg", "bg1", "bg2", "bg3"]
    finally:
        await order.close()


@pytest.mark.asyncio
async def test_background_proceeds_once_the_foreground_queue_is_served():
    order = _Order(PrioritySemaphore(1))
    try:
        await order.take(BG)
        order.queue("bg", BG)
        order.queue("fg1", FG)
        order.queue("fg2", FG)
        await order.settle(3)
        await order.grant(3)
        assert order.order == ["fg1", "fg2", "bg"]
    finally:
        await order.close()


@pytest.mark.asyncio
async def test_a_steady_foreground_stream_cannot_starve_a_background_start():
    """However many foreground starts keep arriving, a queued background start is
    served after at most ``_FOREGROUND_BYPASS_LIMIT`` of them."""
    sem = PrioritySemaphore(1)
    order = _Order(sem)
    waves = _FOREGROUND_BYPASS_LIMIT * 3
    try:
        await order.take(BG)
        order.queue("bg", BG)
        for i in range(waves):
            order.queue(f"fg{i}", FG)
        await order.settle(waves + 1)
        await order.grant(waves + 1)
        assert order.order.index("bg") == _FOREGROUND_BYPASS_LIMIT
        # Foreground leads again once the background waiter is served.
        assert order.order[_FOREGROUND_BYPASS_LIMIT + 1] == f"fg{_FOREGROUND_BYPASS_LIMIT}"
        assert sem.bypasses == _FOREGROUND_BYPASS_LIMIT
    finally:
        await order.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("idle_between", [False, True])
async def test_a_cancelled_background_waiter_leaves_no_stale_streak(idle_between):
    """The streak counts foreground grants since the oldest QUEUED background
    waiter arrived: when the waiter it was counting against is cancelled, a later
    background arrival does not inherit it and jump an earlier person's start."""
    sem = PrioritySemaphore(1)
    order = _Order(sem)
    try:
        await order.take(BG)
        order.queue("bg1", BG)
        for i in range(1, _FOREGROUND_BYPASS_LIMIT + 1):
            order.queue(f"fg{i}", FG)
        await order.settle(_FOREGROUND_BYPASS_LIMIT + 1)
        await order.grant(_FOREGROUND_BYPASS_LIMIT)
        order.tasks[0].cancel()
        await settle_tasks(order.tasks[:1], "bg1")
        if idle_between:
            sem.release(order.holds.pop(0))  # fg4 leaves: the semaphore is idle
            for _ in range(3):  # later uncontended foreground starts take the fast path
                await bounded(sem.acquire(FG), "fast-path fg")
                sem.release(FG)
            await order.take(BG)  # a background start fills the width again
        order.queue("fg_person", FG)
        await order.settle(1)
        order.queue("bg2", BG)
        await order.settle(2)
        await order.grant()
        assert order.order[-1] == "fg_person"
    finally:
        await order.close()


@pytest.mark.asyncio
async def test_no_argument_and_async_with_are_background():
    sem = PrioritySemaphore(1)
    order = _Order(sem)

    async def _plain() -> None:
        await sem.acquire()
        order.holds.append(BG)
        order.order.append("plain")

    async def _context() -> None:
        async with sem:
            order.order.append("context")

    try:
        await order.take(BG)
        order.tasks.append(asyncio.create_task(_plain()))
        await order.settle(1)
        order.tasks.append(asyncio.create_task(_context()))
        await order.settle(2)
        order.queue("fg", FG)
        await order.settle(3)
        await order.grant()
        assert order.order == ["fg"]
        await order.grant()
        assert order.order == ["fg", "plain"]
        sem.release(order.holds.pop(0))  # context takes it and releases it on exit
        await until(lambda: order.order[-1] == "context", "context ran")
        await until(lambda: sem._value == 1, "context released on exit")
    finally:
        await order.close()


@pytest.mark.asyncio
async def test_a_waiter_cancelled_after_its_grant_hands_the_permit_on():
    sem = PrioritySemaphore(1)
    order = _Order(sem)
    granted = asyncio.create_task(sem.acquire(FG))
    try:
        await order.take(BG)
        order.queue("next", BG)
        await order.settle(2)
        sem.release(order.holds.pop(0))  # granted to the foreground waiter ...
        granted.cancel()  # ... which is cancelled before it resumes
        with pytest.raises(asyncio.CancelledError):
            await bounded(granted, "granted waiter")
        await until(lambda: order.order == ["next"], "the permit reached the next waiter")
    finally:
        await settle_tasks([granted], "granted waiter")
        await order.close()


@pytest.mark.asyncio
async def test_a_waiter_cancelled_in_the_queue_takes_no_permit():
    sem = PrioritySemaphore(1)
    order = _Order(sem)
    cancelled = asyncio.create_task(sem.acquire(FG))
    try:
        await order.take(BG)
        order.queue("bg", BG)
        await order.settle(2)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bounded(cancelled, "cancelled waiter")
        assert sem.queued_by_priority() == (0, 1)
        await order.grant()
        assert order.order == ["bg"]
    finally:
        await settle_tasks([cancelled], "cancelled waiter")
        await order.close()


@pytest.mark.asyncio
async def test_a_release_naming_a_class_that_holds_nothing_is_refused():
    """What the semaphore can catch on its own. It cannot tell WHICH holder is
    releasing, so a mismatch while the named class holds another permit is
    indistinguishable from that holder's own release -- which is why every
    production site releases at the priority it acquired, through ``held`` /
    ``drain`` or an explicit release that names it."""
    sem = PrioritySemaphore(2, foreground_reserve=1)
    await bounded(sem.acquire(BG), "bg")
    with pytest.raises(RuntimeError):
        sem.release(FG)
    sem.release(BG)
    with pytest.raises(RuntimeError):
        sem.release(BG)
    assert sem._value == 2


@pytest.mark.asyncio
async def test_held_releases_at_its_priority_on_exit():
    sem = PrioritySemaphore(2, foreground_reserve=1)

    async def _inside() -> None:
        async with sem.held(BG):
            assert sem.locked(BG) and not sem.locked(FG)

    await bounded(_inside(), "held(BG)")
    assert sem._value == 2 and not sem.locked(BG)


@pytest.mark.asyncio
async def test_acquire_refuses_a_value_that_is_not_a_priority():
    with pytest.raises(TypeError):
        await PrioritySemaphore(1).acquire("fg")  # type: ignore[arg-type]


# ── the cold-start reserve ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_background_keeps_its_full_cold_start_width():
    """The reserve is additive: with no person starting, background still runs
    ``MAX_CONCURRENT_COLD_STARTS`` cold starts at once."""
    sem = new_cold_start_semaphore()
    order = _Order(sem)
    try:
        await order.take(BG, MAX_CONCURRENT_COLD_STARTS)
        assert sem.locked(BG) and not sem.locked(FG)
        assert sem._value == FOREGROUND_COLD_START_RESERVE
    finally:
        await order.close()


@pytest.mark.asyncio
async def test_the_first_person_start_gets_a_permit_at_once_during_a_full_fan_out():
    sem = new_cold_start_semaphore()
    order = _Order(sem)
    try:
        await order.take(BG, MAX_CONCURRENT_COLD_STARTS)
        order.queue("bg_queued", BG)
        await order.settle(1)
        await order.take(FG)  # bounded: it must not wait
        # A second concurrent person start waits -- but ahead of background.
        order.queue("fg_second", FG)
        await order.settle(2)
        await order.grant()  # a background holder leaves
        assert order.order == ["fg_second"]
    finally:
        await order.close()


@pytest.mark.asyncio
async def test_a_freed_reserved_permit_is_not_handed_to_background():
    sem = new_cold_start_semaphore()
    order = _Order(sem)
    try:
        await order.take(BG, MAX_CONCURRENT_COLD_STARTS)
        await order.take(FG)
        order.queue("bg_queued", BG)
        await order.settle(1)
        sem.release(order.holds.pop())  # the person's start leaves
        await asyncio.sleep(0)
        assert order.order == []
        await order.grant()  # a background holder leaves
        assert order.order == ["bg_queued"]
    finally:
        await order.close()


# ── the drain ─────────────────────────────────────────────────────────────────


async def _drain_until(sem: PrioritySemaphore, inside: asyncio.Event, leave: asyncio.Event):
    async with sem.drain():
        inside.set()
        await leave.wait()


@pytest.mark.asyncio
async def test_a_drain_outranks_foreground_waiters_and_they_proceed_after_it():
    sem = new_cold_start_semaphore()
    order = _Order(sem)
    inside, leave = asyncio.Event(), asyncio.Event()
    barrier = asyncio.create_task(_drain_until(sem, inside, leave))
    try:
        await order.take(BG, MAX_CONCURRENT_COLD_STARTS)
        await until(lambda: sem._drain_held == FOREGROUND_COLD_START_RESERVE, "drain took free")
        for i in range(3):
            order.queue(f"fg{i}", FG)
        await order.settle(3)
        while order.holds:
            sem.release(order.holds.pop(0))
        await bounded(inside.wait(), "the drain completing")
        assert order.order == [], "no waiter is served while a drain is pending"
        leave.set()
        await until(lambda: len(order.order) == 3, "foreground served after the drain")
        assert order.order == ["fg0", "fg1", "fg2"]
    finally:
        leave.set()
        await settle_tasks([barrier], "drain")
        await order.close()


@pytest.mark.asyncio
async def test_a_drain_cancelled_part_way_hands_back_what_it_held():
    sem = PrioritySemaphore(4)
    order = _Order(sem)
    inside, leave = asyncio.Event(), asyncio.Event()
    barrier = asyncio.create_task(_drain_until(sem, inside, leave))
    try:
        await order.take(BG, 2)
        await until(lambda: sem._drain_held == 2, "the drain took the free permits")
        sem.release(order.holds.pop(0))
        await until(lambda: sem._drain_held == 3, "the drain took a released permit")
        barrier.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bounded(barrier, "cancelled drain")
        assert sem._value == 3 and sem._drain_held is None
    finally:
        await settle_tasks([barrier], "drain")
        await order.close()


@pytest.mark.asyncio
async def test_a_second_concurrent_drain_is_refused():
    sem = PrioritySemaphore(1)
    inside, leave = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(_drain_until(sem, inside, leave))
    try:
        await bounded(inside.wait(), "first drain")

        async def _second() -> None:
            async with sem.drain():
                pass

        with pytest.raises(RuntimeError):
            await bounded(_second(), "the second drain")
    finally:
        leave.set()
        await settle_tasks([first], "drain")
    assert sem._value == 1


@pytest.mark.asyncio
async def test_the_identity_sweep_barrier_finishes_under_a_foreground_stream():
    """The sweep drains ``_start_sem`` ahead of both priorities: while background
    holds its full width and person-started cold starts keep queueing, it completes
    as soon as the holders it waits for let go."""
    mgr = SessionManager(KiroCrewConfig(), provider_factory=_mock_provider_factory())
    sem = mgr._start_sem
    order = _Order(sem)
    sweep: asyncio.Task | None = None
    try:
        await order.take(BG, MAX_CONCURRENT_COLD_STARTS)
        sweep = asyncio.create_task(mgr.retire_kiro_identity_sessions("fp"))
        await until(lambda: sem._drain_held is not None, "the sweep's drain started")
        for i in range(6):
            order.queue(f"fg{i}", FG)
        await order.settle(6)
        while order.holds:
            sem.release(order.holds.pop(0))
        await bounded(sweep, "identity sweep")
        await until(lambda: len(order.order) == sem._limit, "foreground after the sweep")
    finally:
        if sweep is not None:
            await settle_tasks([sweep], "identity sweep")

        async def _drain_order() -> None:
            # Granted waiters keep arriving as permits come back: return each one.
            while order.holds or any(not t.done() for t in order.tasks):
                if order.holds:
                    sem.release(order.holds.pop(0))
                await asyncio.sleep(0)

        try:
            await bounded(_drain_order(), "returning the granted permits")
        finally:
            await order.close()
        await bounded(mgr.close_all(), "close_all")


# ── the dashboard runner's origin rule ────────────────────────────────────────


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        # A typed turn, a Resume/Continue press and the runner's requeue of a
        # person's failed turn all resolve to the user actor with user origin.
        (dict(user_origin=True, crew_log_actor="user", provenance_restored=False), FG),
        # session_send / broadcast and every injector that calls the runner bare.
        (dict(user_origin=False, crew_log_actor="user", provenance_restored=False), BG),
        # An auto-nudge / monitor wake, a cron or sub-agent completion, an app turn.
        (dict(user_origin=True, crew_log_actor="autonudge", provenance_restored=False), BG),
        (dict(user_origin=False, crew_log_actor="cron", provenance_restored=False), BG),
        (dict(user_origin=True, crew_log_actor="app", provenance_restored=False), BG),
        # A queue entry restored from a previous process: its provenance is a file.
        (dict(user_origin=True, crew_log_actor="user", provenance_restored=True), BG),
    ],
)
def test_a_dashboard_turn_is_foreground_only_when_a_person_sent_it(flags, expected):
    assert _turn_start_priority(**flags) is expected


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        # A Resume/Continue press (chat_handlers inserts it with user origin).
        ({"kind": "synthetic_recovery", "content": ""}, FG),
        # The runner's requeue of a person's failed turn, stamped with its actor.
        ({"content": "hi", "meta": {"turn_actor": "user"}}, FG),
        ({"kind": "cron_notification", "content": "x"}, BG),
        ({"kind": "subagent_completion", "content": "x"}, BG),
    ],
)
def test_queue_entries_resolve_through_the_real_actor_rule(item, expected, monkeypatch):
    from kiro_crew.dashboard import chat_runner

    kind = item.get("kind")
    if kind:
        real = {
            "synthetic_recovery": chat_runner.SYNTHETIC_RECOVERY_KIND,
            "cron_notification": chat_runner.CRON_NOTIFICATION_KIND,
            "subagent_completion": chat_runner.SUBAGENT_COMPLETION_KIND,
        }[kind]
        item = {**item, "kind": real}
    if "meta" in item:
        item = {**item, "meta": {chat_runner.TURN_ACTOR_META_KEY: "user"}}
    actor = _actor_for_queue_items([item]) or "user"
    assert (
        _turn_start_priority(user_origin=True, crew_log_actor=actor, provenance_restored=False)
        is expected
    )


# ── the priority that reaches each queue ──────────────────────────────────────


class _RecordingSemaphore(PrioritySemaphore):
    def __init__(self, value: int, **kwargs: Any) -> None:
        super().__init__(value, **kwargs)
        self.acquired: list[StartPriority] = []

    async def acquire(self, priority: StartPriority = BG) -> bool:
        self.acquired.append(priority)
        return await super().acquire(priority)


def _recording_cold_start_semaphore() -> _RecordingSemaphore:
    return _RecordingSemaphore(
        MAX_CONCURRENT_COLD_STARTS + FOREGROUND_COLD_START_RESERVE,
        foreground_reserve=FOREGROUND_COLD_START_RESERVE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "explicit", "expected"),
    [
        # No key is foreground by itself: automation runs on chat keys too.
        ("dashboard:chat-1-1", None, BG),
        ("slack:C1:1700000000.000100", None, BG),
        ("subagent:a1", None, BG),
        ("dashboard:chat-1-1", FG, FG),
        ("cron:job-1", FG, FG),
    ],
)
async def test_the_cold_start_semaphore_gets_the_callers_priority(key, explicit, expected):
    providers: list = []
    base = _mock_provider_factory()

    def factory(session_key=None, **kwargs):
        provider = base(session_key=session_key, **kwargs)
        providers.append(provider)
        return provider

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    sem = _recording_cold_start_semaphore()
    mgr._start_sem = sem
    kwargs = {} if explicit is None else {"start_priority": explicit}
    try:
        await bounded(mgr.get_or_create(key, **kwargs), "get_or_create")
        assert sem.acquired == [expected]
        assert providers[-1].start_priority is expected, "the provider's queues read it too"
    finally:
        await bounded(mgr.close_all(), "close_all")


def test_the_cold_start_semaphore_refuses_a_plain_semaphore():
    mgr = SessionManager(KiroCrewConfig(), provider_factory=_mock_provider_factory())
    with pytest.raises(TypeError):
        mgr._start_sem = asyncio.Semaphore(1)  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_the_cold_start_semaphore_wait_brackets_the_start_clock():
    """A subagent's start clock pauses while it waits for a cold-start permit."""
    events: list[tuple] = []
    mgr = SessionManager(KiroCrewConfig(), provider_factory=_mock_provider_factory())
    try:
        await bounded(
            mgr.get_or_create(
                "subagent:a1",
                on_gate_queued=lambda queue: events.append(("queued", queue)),
                on_gate_acquired=lambda ms, queue: events.append(("acquired", queue)),
            ),
            "get_or_create",
        )
        assert events == [("queued", START_QUEUE_COLD_START), ("acquired", START_QUEUE_COLD_START)]
    finally:
        await bounded(mgr.close_all(), "close_all")


@pytest.mark.asyncio
async def test_a_raising_cold_start_callback_returns_the_permit():
    mgr = SessionManager(KiroCrewConfig(), provider_factory=_mock_provider_factory())

    def _boom(ms: float, queue: str) -> None:
        raise KeyboardInterrupt

    try:
        with pytest.raises(KeyboardInterrupt):
            await bounded(mgr.get_or_create("subagent:a1", on_gate_acquired=_boom), "goc")
        assert mgr._start_sem._value == mgr._start_sem._limit
    finally:
        await bounded(mgr.close_all(), "close_all")


@pytest.mark.asyncio
@pytest.mark.parametrize("priority", [FG, BG])
async def test_the_spawn_admission_gets_the_spawn_priority(monkeypatch, priority):
    admission = _ColdStartAdmission(limit=2)
    admission.semaphore = _RecordingSemaphore(2)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)
    events: list[Any] = []

    async def _spawned(self) -> None:
        events.append("spawned")

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted_rederiving_once", _spawned)
    await bounded(
        AcpRuntime().spawn(
            start_priority=priority,
            on_gate_queued=lambda queue: events.append(("queued", queue)),
            on_gate_acquired=lambda ms, queue: events.append(("acquired", queue)),
        ),
        "spawn",
    )
    assert admission.semaphore.acquired == [priority]
    assert events == [
        ("queued", START_QUEUE_ADMISSION),
        ("acquired", START_QUEUE_ADMISSION),
        "spawned",
    ]
    assert admission.semaphore._value == 2


@pytest.mark.asyncio
async def test_a_bare_spawn_is_background_and_a_raising_callback_returns_the_permit(monkeypatch):
    admission = _ColdStartAdmission(limit=2)
    admission.semaphore = _RecordingSemaphore(2)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)

    async def _spawned(self) -> None:
        return None

    def _boom(ms: float, queue: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted_rederiving_once", _spawned)
    await bounded(AcpRuntime().spawn(), "spawn")
    with pytest.raises(KeyboardInterrupt):
        await bounded(AcpRuntime().spawn(on_gate_acquired=_boom), "spawn")
    assert admission.semaphore.acquired == [BG, BG]
    assert admission.semaphore._value == 2 and admission.active == 0


# ── the nested queues ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_chat_is_not_stuck_behind_background_holders_of_the_outer_semaphore():
    """The real nesting: every background cold start holds a ``_start_sem`` permit
    while it waits at an inner ``session/new`` gate behind a shared fan-out. The
    chat still gets an outer permit at once (the reserve) and is the next
    ``session/new`` on the wire (priority at the inner gate)."""
    gate = runtime_mod.SessionStartGate(2)
    entered: list[str] = []
    answer: dict[str, asyncio.Event] = {}

    def _event(name: str) -> asyncio.Event:
        return answer.setdefault(name, asyncio.Event())

    base = _mock_provider_factory()

    def factory(session_key=None, **kwargs):
        provider = base(session_key=session_key, **kwargs)
        name = str(session_key)

        async def start() -> None:
            permit = await gate.acquire(provider.start_priority)
            entered.append(name)
            try:
                await _event(name).wait()
            finally:
                permit.release()

        provider.start = start
        return provider

    async def fan_out(name: str) -> None:
        permit = await gate.acquire(BG)
        entered.append(name)
        try:
            await _event(name).wait()
        finally:
            permit.release()

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    tasks: list[asyncio.Task] = []
    shared = [f"share{i}" for i in range(4)]
    crons = [f"cron:job-{i}" for i in range(MAX_CONCURRENT_COLD_STARTS)]
    try:
        tasks += [asyncio.create_task(fan_out(n)) for n in shared]
        await until(lambda: len(entered) == 2 and gate.queued == 2, "fan-out at the gate")
        tasks += [asyncio.create_task(mgr.get_or_create(k)) for k in crons]
        await until(lambda: gate.queued == 2 + len(crons), "every cron start at the gate")
        assert mgr._start_sem.locked(BG), "background holds every outer permit it may"
        chat = asyncio.create_task(mgr.get_or_create("dashboard:chat-1-1", start_priority=FG))
        tasks.append(chat)
        await until(lambda: gate.queued == 3 + len(crons), "the chat reached the inner gate")
        _event(entered[0]).set()
        await until(lambda: len(entered) == 3, "the next session/new")
        assert entered[2] == "dashboard:chat-1-1"
    finally:
        for name in [*shared, *crons, "dashboard:chat-1-1"]:
            _event(name).set()
        await settle_tasks(tasks, "nested starts")
        await bounded(mgr.close_all(), "close_all")


# ── channels ──────────────────────────────────────────────────────────────────


def test_person_priority_is_foreground_only_on_evidence():
    assert person_priority(True) is FG
    assert person_priority(False) is BG


def test_an_inbound_message_is_not_a_persons_until_a_transport_says_so():
    msg = InboundMessage(channel_type="slack", user_id="U1", conversation_id="C1", text="hi")
    assert msg.person_origin is False
    assert ChannelTurn.__dataclass_fields__["start_priority"].default is BG


# ── the leaf claimers, pinned at their call sites ─────────────────────────────


def _calls_in(path: str, function: str, callee: str) -> list[ast.Call]:
    tree = ast.parse((_SRC / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            return [
                call
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and (
                    (isinstance(call.func, ast.Attribute) and call.func.attr == callee)
                    or (isinstance(call.func, ast.Name) and call.func.id == callee)
                )
            ]
    raise AssertionError(f"{function} not found in {path}")


def _priority_args(path: str, function: str, callee: str) -> list[str]:
    return [
        ast.unparse(keyword.value)
        for call in _calls_in(path, function, callee)
        for keyword in call.keywords
        if keyword.arg == "start_priority"
    ]


@pytest.mark.parametrize(
    ("path", "function", "callee", "expected"),
    [
        # Each of these serves a person's own request, so it claims the priority
        # from who asked -- never a bare FOREGROUND, which an app token would take
        # too (``owner_start_priority``).
        (
            "dashboard/handlers/optimizer.py",
            "handle_optimize",
            "get_or_create",
            "owner_start_priority(request)",
        ),
        (
            "dashboard/handlers/side.py",
            "api_side_turn",
            "_dispatch_side_turn",
            "owner_start_priority(request)",
        ),
        (
            "dashboard/chat_threads.py",
            "api_chat_thread_reply",
            "_run_thread_turn",
            "owner_start_priority(request)",
        ),
        (
            "dashboard/handlers/taskrunner.py",
            "api_taskrunner_plan",
            "plan",
            "owner_start_priority(request)",
        ),
        (
            "dashboard/handlers/workflows.py",
            "api_workflow_author",
            "author",
            "owner_start_priority(request)",
        ),
        (
            "apps/builtins/issue_radar/backend/http_routes/ai.py",
            "_run_oneshot_model",
            "get_or_create",
            "owner_start_priority(request)",
        ),
        ("dashboard/stt_stream.py", "_classify", "run_bg_oneliner", "StartPriority.FOREGROUND"),
        # The dashboard runner hands every start the turn's own priority.
        ("dashboard/chat_runner.py", "_run_chat", "run_bg_oneliner", "_turn_priority"),
        # The turn's eager respawn runs in its tail, from the priority the turn
        # recorded in its outcome.
        (
            "dashboard/chat_runner.py",
            "_end_turn_tail",
            "schedule_eager_spawn",
            "outcome.start_priority",
        ),
        # A turn's own background work passes the priority it was given.
        ("dashboard/handlers/side.py", "_run_side_turn", "get_or_create", "start_priority"),
        ("dashboard/chat_threads.py", "_run_thread_turn", "get_or_create", "start_priority"),
        ("dashboard/handlers/taskrunner.py", "_run_refine", "get_or_create", "start_priority"),
    ],
)
def test_a_claimer_passes_its_priority(path, function, callee, expected):
    assert _priority_args(path, function, callee) == [expected]


@pytest.mark.parametrize(
    ("path", "function", "callee"),
    [
        # Refine's own priority rides a positional argument, so pin the owner call.
        ("dashboard/handlers/taskrunner.py", "api_taskrunner_refine", "_run_refine"),
    ],
)
def test_a_claimer_passes_the_owner_priority_positionally(path, function, callee):
    calls = _calls_in(path, function, callee)
    assert [ast.unparse(a) for call in calls for a in call.args][-1:] == [
        "owner_start_priority(request)"
    ]


def test_an_unattended_stream_claims_nothing():
    """Meeting translation is a per-line stream nobody waits on, so it names no
    priority at all: the BACKGROUND default is the whole statement."""
    assert (
        _priority_args(
            "apps/builtins/meetings/backend/domain/translate.py",
            "run_oneshot_translation",
            "get_or_create",
        )
        == []
    )


def test_the_dashboard_runner_allocates_at_the_turns_priority():
    tree = ast.parse((_SRC / "dashboard/chat_runner.py").read_text(encoding="utf-8"))
    run_chat = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_chat"
    )
    kwargs = [
        n
        for n in ast.walk(run_chat)
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "_allocation_kwargs"
    ]
    assert kwargs, "_allocation_kwargs not found"
    call = kwargs[0].value
    assert isinstance(call, ast.Call)
    assert [ast.unparse(k.value) for k in call.keywords if k.arg == "start_priority"] == [
        "_turn_priority"
    ]


@pytest.mark.parametrize(
    ("path", "function"),
    [
        # The create route's body; ``api_chat_slot_create`` only takes the
        # per-name wait around it.
        ("dashboard/chat_handlers.py", "_create_chat_slot"),
        ("dashboard/chat_handlers.py", "api_chat_slot_project"),
    ],
)
def test_an_owner_slot_action_arms_its_eager_spawn_by_who_asked(path, function):
    assert _priority_args(path, function, "schedule_eager_spawn") == [
        "owner_start_priority(request)"
    ]


def test_the_agent_switch_arms_its_eager_spawn_by_who_asked():
    # The agent route's transaction lives in ``switch_slot_agent`` (the in-turn
    # ``/agent <name>`` command shares it), so it reads the caller's owner bit
    # rather than a request. The route derives that bit from the same owner gate
    # ``owner_start_priority`` reads; a turn's command never claims it.
    assert _priority_args(
        "dashboard/chat_handlers.py", "switch_slot_agent", "schedule_eager_spawn"
    ) == ["person_priority(caller.owner)"]


class _Request(dict):
    """Just enough of an aiohttp request for ``is_owner_dashboard_request``."""

    def __init__(self, owner_id: str, **fields: str) -> None:
        super().__init__(fields)
        self.app = {"state": MagicMock(owner_id=owner_id)}


@pytest.mark.parametrize(
    ("request_fields", "expected"),
    [
        (dict(app="", user="local-app"), FG),  # the dashboard owner, no owner configured
        (dict(app="some-app", user="local-app"), BG),  # an app token
        (dict(app="", user=""), BG),  # an internal caller: no user
    ],
)
def test_a_slot_action_arms_its_eager_spawn_by_who_asked(request_fields, expected):
    from kiro_crew.dashboard.request_priority import owner_start_priority

    assert owner_start_priority(_Request("", **request_fields)) is expected


@pytest.mark.parametrize(
    ("path", "function"),
    [
        ("dashboard/ws.py", "_handle_slot_focused"),
        ("dashboard/chat_handlers.py", "reload_slot_session"),
    ],
)
def test_the_focus_prefetch_and_reload_arm_background_eager_spawns(path, function):
    assert _calls_in(path, function, "schedule_eager_spawn"), "the call moved"
    assert _priority_args(path, function, "schedule_eager_spawn") == []


def test_the_channel_pipeline_starts_the_session_at_the_askers_priority():
    # Both claims: a monitor wake's non-waiting one and an inbound turn's.
    assert _priority_args("messaging/dispatch.py", "_run", "get_or_create") == [
        "asker.start_priority",
        "asker.start_priority",
    ]


@pytest.mark.parametrize(
    "transport",
    [
        "imessage",
        "webex",
        "weixin",
        "feishu",
        "wecom",
        "telegram",
        "teams",
        "whatsapp",
        "discord",
    ],
)
def test_every_transport_marks_what_it_receives_as_a_persons(transport):
    """Slack is not in the list: its live inbound path is ``slack/events.py``,
    which claims the priority itself (``test_slack_automation_paths_name_no_priority``)."""
    source = (_SRC / transport / "transport.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    receive = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "receive"
    )
    marks = [
        n
        for n in ast.walk(receive)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "person_origin" for t in n.targets)
    ]
    assert len(marks) == 1 and ast.unparse(marks[0].value) == "True"


@pytest.mark.parametrize(
    ("path", "callee", "count"),
    [
        ("feishu/transport_dispatch.py", "ChannelTurn", 1),
        ("imessage/transport_dispatch.py", "ChannelTurn", 1),
        ("teams/transport_dispatch.py", "ChannelTurn", 1),
        ("webex/transport_dispatch.py", "ChannelTurn", 1),
        ("weixin/transport_dispatch.py", "ChannelTurn", 1),
        ("wecom/transport_dispatch.py", "ChannelTurn", 1),
        ("whatsapp/transport_dispatch.py", "ChannelTurn", 1),
        ("discord/transport_dispatch.py", "Asker", 1),
        ("telegram/transport_dispatch.py", "get_or_create", 1),
    ],
)
def test_a_channel_dispatcher_starts_a_turn_at_the_inbounds_origin(path, callee, count):
    tree = ast.parse((_SRC / path).read_text(encoding="utf-8"))
    values = [
        ast.unparse(k.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == callee)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == callee)
        )
        for k in n.keywords
        if k.arg == "start_priority"
    ]
    assert len(values) == count
    assert all(v.startswith("person_priority(") and v.endswith(".person_origin)") for v in values)


def test_slack_automation_paths_name_no_priority():
    """The gateway's own Slack wakes (auto-nudge, monitor) and its completion
    injections take the BACKGROUND default; only the event and interaction paths,
    which carry a person's message, claim one."""
    source = (_SRC / "slack/gateway.py").read_text(encoding="utf-8")
    assert "start_priority" not in source
    events = (_SRC / "slack/events.py").read_text(encoding="utf-8")
    assert events.count("start_priority=person_priority(not ") == 4


# ── the background singleton's queues ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("priority", [FG, BG])
async def test_the_background_session_start_carries_its_callers_priority(priority):
    """``get_bg_session`` can wait at the shared runtime's (re)spawn admission and
    at its ``session/new``; a caller a person waits on (STT endpointing, the
    poisoned-conversation canary) must not queue BACKGROUND at the first of them."""
    seen: dict = {}

    class _Runtime:
        def is_alive(self) -> bool:
            return True

        async def _is_stale(self):
            return None

        async def create_session(self, **kwargs):
            seen.update(kwargs)
            return object()

    mgr = SessionManager(KiroCrewConfig(), provider_factory=_mock_provider_factory())
    bg = mgr._background_runtime
    bg._bg_runtime = _Runtime()
    try:
        await bounded(mgr.get_bg_session(priority), "get_bg_session")
        assert seen["start_priority"] is priority
    finally:
        bg._bg_runtime = None
        await bounded(mgr.close_all(), "close_all")


def test_the_background_respawn_and_the_provider_path_take_the_same_priority():
    """Both of ``get_bg_session``'s other queues read the caller's priority: the
    shared runtime's respawn, and -- on a backend with no shared runtime -- the
    provider-backed entry's own cold start."""
    source = (_SRC / "session_background.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    spawns = [
        [k.arg for k in n.keywords]
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "spawn"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "replacement"
    ]
    assert spawns == [["start_priority"], ["start_priority"]]
    # The implementation, not the Protocol stub of the same name.
    ensure = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_ensure_background"
        and "_start_sem" in ast.unparse(n)
    )
    acquires = [
        ast.unparse(a)
        for n in ast.walk(ensure)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"acquire", "release"}
        for a in n.args
    ]
    assert acquires == ["start_priority", "start_priority"]


def test_the_oneliner_does_not_cancel_the_shared_runtimes_own_start():
    """``run_bg_oneliner``'s ``timeout`` bounds the DRIVE only. Cancelling the
    acquisition would kill the shared ``_bg`` (re)spawn every later caller reuses,
    and leak a backend session whose ``session/new`` had already gone out."""
    tree = ast.parse((_SRC / "llm_helpers.py").read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_bg_oneliner"
    )
    waits = [
        ast.unparse(n)
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "wait_for"
        and "get_bg_session" in ast.unparse(n)
    ]
    assert waits == []


# ── the startup clock ─────────────────────────────────────────────────────────


def test_the_startup_deadline_covers_every_phase_the_clock_runs_through():
    """One clock spans the spawn handshake, ``session/new`` and the collector
    wait, pausing only in a queue -- so the window must include the ``initialize``
    budget, once per handshake round of a retried start, or the watchdog reaps a
    start whose own budgets have not expired."""
    from kiro_crew.config import live as _live
    from kiro_crew.constants import INITIALIZE_TIMEOUT_SECS
    from kiro_crew.subagent import (
        _STARTUP_COLLECT_GRACE_SECS,
        _STARTUP_LAUNCH_MARGIN_SECS,
        SubagentManager,
    )

    cfg = KiroCrewConfig()
    cfg.agent.session_start_timeout_secs = 900
    cfg.agent.start_collect_timeout_secs = 10
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(_live, "snapshot", lambda: cfg)
        assert mgr._startup_deadline == int(
            2 * (900 + INITIALIZE_TIMEOUT_SECS)
            + 10
            + _STARTUP_COLLECT_GRACE_SECS
            + _STARTUP_LAUNCH_MARGIN_SECS
        )


def test_a_recovery_respawn_starts_with_an_empty_queue_total():
    """``_run_inner`` re-runs the same ``SubagentInfo`` on a recovery respawn. The
    previous attempt's queue total (or a mark it was cancelled inside) must not be
    subtracted from a clock that never paid it."""
    source = (_SRC / "subagent_manager" / "run.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    reset_fields = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        body = ast.unparse(fn)
        if "info._exec_started = time.time()" in body or "info._exec_started = now" in body:
            reset_fields.append(
                (
                    "info._start_queue_wait_ms = 0.0" in body,
                    "info._gate_wait_started = None" in body,
                )
            )
    assert reset_fields and all(
        queue_total and mark for queue_total, mark in reset_fields
    ), "every site that (re)stamps _exec_started must clear the paused-clock fields"


# --------------------------------------------------------------------------- #
# widen_to: a bound sized after it was built
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_widen_to_hands_the_new_permits_to_queued_waiters() -> None:
    sem = PrioritySemaphore(2, foreground_reserve=1)
    await sem.acquire(StartPriority.BACKGROUND)
    queued = [asyncio.ensure_future(sem.acquire(StartPriority.BACKGROUND)) for _ in range(3)]
    await asyncio.sleep(0)
    assert not any(t.done() for t in queued)  # background cap is 1

    sem.widen_to(4)
    await asyncio.sleep(0)

    assert sum(t.done() for t in queued) == 2
    assert sem.limit == 4
    assert sem._background_cap == 3  # the reserve is still one permit
    assert sem.locked(StartPriority.BACKGROUND)
    assert not sem.locked(StartPriority.FOREGROUND)
    for t in queued:
        t.cancel()
    await asyncio.gather(*queued, return_exceptions=True)


def test_widen_to_never_shrinks() -> None:
    sem = PrioritySemaphore(5)
    sem.widen_to(3)
    sem.widen_to(5)
    assert sem.limit == 5
    assert sem._value == 5


@pytest.mark.asyncio
async def test_a_pending_drain_waits_for_the_widened_total() -> None:
    sem = PrioritySemaphore(2)
    await sem.acquire(StartPriority.BACKGROUND)
    entered = asyncio.Event()
    leave = asyncio.Event()
    task = asyncio.ensure_future(_drain_until(sem, entered, leave))
    await asyncio.sleep(0)
    sem.widen_to(4)
    await asyncio.sleep(0)
    assert not entered.is_set()  # the holder is still inside
    assert sem._drain_held == 3
    sem.release(StartPriority.BACKGROUND)
    await asyncio.wait_for(entered.wait(), 5)
    assert sem._drain_held == 4
    leave.set()
    await asyncio.wait_for(task, 5)
    assert sem._value == 4

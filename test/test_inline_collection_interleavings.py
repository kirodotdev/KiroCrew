"""Seeded random interleavings of the inline-collection registry's delivery.

Each case runs the real ``InlineCollections`` bound to a manager double, through a
random sequence of the events that move a held result: members spawned and
completing, the blocking call's claim (returned, cancelled, or partly returned;
landing at once, after the commit that should follow it, or never), the
dispatcher's word on the response (written, dropped, or never reported),
parent teardown, run-record eviction, collection and claim expiry, the parent's
turn staying busy, the original terminal report still finishing, and the
parent's route failing to allocate, raising, or queueing the announce. A
gateway restart at any point loses only the in-memory registry, so restart
recovery replays exactly what is not marked delivered.

Three properties hold in every case:

* no retired parent receives output: nothing reaches a parent's route for a
  member that was in flight when that parent was torn down;
* nothing marked delivered was undelivered: every ``delivered`` settle names a
  member whose result reached its parent, by a written response or by a route
  that took it, and none is settled twice. A claim settles only once its
  response is written, never at the claim, and a held result whose response
  was written is always settled, even when its parent is torn down after;
* every result is delivered or replayable: a held result whose response was
  dropped or never reported is not marked, and goes out by the ordinary route;
* exactly once after a confirmed write: a result a written response carried to
  a live parent never reaches that parent again, by the route or as an ordinary
  completion, whether its claim landed first, late, or not at all.

The original run record a hold parked is never touched by a redelivery, and once
every wait ends the registry holds nothing.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.subagent_inline_collection import (
    CLAIM_TTL_SECS,
    COLLECTED_TTL_SECS,
    COLLECTION_GRACE_SECS,
    InlineCollections,
)

CASES = 400


def _hold(reg: Any, parent: str, aid: str, info: Any = None) -> bool:
    """``reg.hold`` with *info* as the live run its release reads back.

    The registry keeps only the id, so the run record a test hands in is what
    the bound manager's ``get`` answers for it, as ``_agents`` does live.
    """
    if info is not None:
        _live_runs(reg)[aid] = info
    return reg.hold(parent, aid)


def _live_runs(reg: Any) -> dict:
    mgr = reg._manager
    runs = getattr(mgr, "_test_live_runs", None)
    if not isinstance(runs, dict):
        runs = {}
        previous = getattr(mgr, "get", None)

        def get(aid: str) -> Any:
            if aid in runs:
                return runs[aid]
            return previous(aid) if callable(previous) else None

        mgr._test_live_runs = runs
        mgr.get = get
    return runs


@dataclass
class _Info:
    id: str
    parent_session_key: str
    outcome: str = "completed"
    elapsed: float = 1.0
    credits: float = 0.0
    _delivery_queued: bool = False
    _report_undelivered: bool = False
    _report_owed: bool = False
    # Read back from the live run at release.
    result: str = "r" * 64
    result_path: str = ""


@dataclass
class _World:
    rng: random.Random
    registry: InlineCollections
    now: list[float]
    busy: dict[str, bool] = field(default_factory=dict)
    # member -> parent, and the parent's conversation generation it belongs to
    members: dict[str, str] = field(default_factory=dict)
    generation: dict[str, int] = field(default_factory=dict)
    member_gen: dict[str, int] = field(default_factory=dict)
    known: set[str] = field(default_factory=set)
    completed: set[str] = field(default_factory=set)
    held: dict[str, _Info] = field(default_factory=dict)
    reached: set[str] = field(default_factory=set)
    marked: list[str] = field(default_factory=list)
    route_fault: dict[str, str] = field(default_factory=dict)
    reports: dict[str, asyncio.Event] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    # Held results the call's close returned: always settled.
    returned_held: set[str] = field(default_factory=set)
    # Parents torn down since the main loop last looked; their open calls end.
    retired_parents: set[str] = field(default_factory=set)
    # Claims whose response the dispatcher has not reported on yet:
    # (parent, the ids the result names, every member of the call).
    claims: list[tuple[str, list[str], list[str]]] = field(default_factory=list)
    # Claims sent but not yet processed by the gateway (slow, retried).
    late_claims: list[tuple[str, list[str], list[str]]] = field(default_factory=list)
    # Members a written response carried to a live parent, with when.
    confirmed: dict[str, float] = field(default_factory=dict)

    def retired(self, aid: str) -> bool:
        parent = self.members[aid]
        return self.member_gen[aid] < self.generation.get(parent, 0)


def _manager(world: _World) -> Any:
    mgr = MagicMock()
    mgr._report_owners = {}

    def _is_busy(parent: str) -> bool:
        if world.rng.random() < 0.05:
            # The turn ends because its session was removed: the teardown runs
            # synchronously inside the delivery's idle check.
            _retire(world, parent)
        return world.busy.get(parent, False)

    mgr._sessions.is_busy = _is_busy

    async def _on_done(ticket: Any) -> None:
        if ticket.id in world.confirmed:
            world.violations.append(f"{ticket.id}: delivered again after a confirmed write")
        # The gateway's route asks the registry first; a ticket must pass.
        if world.registry.hold(ticket.parent_session_key, ticket.id):
            world.violations.append(f"{ticket.id}: the ticket was held again")
        if world.registry.consume_collected(ticket.parent_session_key, ticket.id):
            world.violations.append(f"{ticket.id}: the ticket was consumed as returned")
        if ticket.id in world.held and ticket is world.held[ticket.id]:
            world.violations.append(f"{ticket.id}: redelivered on the original record")
        # The route does synchronous work before its first await (events,
        # logging), and again after each of its awaits.
        if world.retired(ticket.id):
            world.violations.append(f"{ticket.id}: the route ran for a retired parent")
        fault = world.route_fault.pop(ticket.parent_session_key, "")
        # The route's own awaits (memory store, session allocation): a teardown
        # can land in any of them.
        for _ in range(world.rng.randrange(4)):
            if world.rng.random() < 0.1:
                # Torn down from inside the route, at this await point.
                _retire(world, ticket.parent_session_key)
            await asyncio.sleep(0)
        if world.retired(ticket.id):
            world.violations.append(f"{ticket.id}: output reached a retired parent")
        if fault == "allocation":
            ticket._report_undelivered = True
        elif fault == "raise":
            raise RuntimeError("route failed")
        elif fault == "queued":
            ticket._delivery_queued = True
        else:
            world.reached.add(ticket.id)

    async def _settle(deliveries: list[Any]) -> None:
        world.marked.extend(d.agent_id for d in deliveries)

    mgr._on_done = _on_done
    mgr.settle_queued_delivery = _settle
    return mgr


def _retire(world: _World, parent: str) -> None:
    """The parent-end teardown snapshot."""
    world.registry.retire(parent)
    world.generation[parent] = world.generation.get(parent, 0) + 1
    world.retired_parents.add(parent)


async def _yield(world: _World) -> None:
    for _ in range(world.rng.randrange(4)):
        await asyncio.sleep(0)


def _complete(world: _World, aid: str) -> None:
    """The run's terminal report reaching the gateway (``inline_collection_owns``)."""
    parent = world.members[aid]
    info = _Info(aid, parent)
    if world.rng.random() < 0.2:
        info.outcome = "expired"
        info._report_owed = True
    world.completed.add(aid)
    reg = world.registry
    if _hold(reg, parent, aid, info):
        info._delivery_queued = True
        world.held[aid] = info
        if world.rng.random() < 0.4:
            # The report that took the hold is still finishing.
            gate = asyncio.Event()

            async def _report() -> None:
                await gate.wait()

            task = asyncio.get_running_loop().create_task(_report())
            reg._manager._report_owners[task] = info
            world.reports[aid] = gate
        return
    if reg.consume_collected(parent, aid):
        return
    since = world.confirmed.get(aid)
    if since is not None and not world.retired(aid) and world.now[0] - since < COLLECTED_TTL_SECS:
        world.violations.append(f"{aid}: injected after a confirmed write")
    # An ordinary completion: the terminal report's own fence (outside this
    # registry) drops one whose parent ended; neither is this registry's output.


def _close(world: _World, parent: str, call: list[str]) -> None:
    """The call's claim: it names what its result returns, and settles nothing."""
    rng = world.rng
    shape = rng.random()
    if shape < 0.3:
        returned: list[str] = []  # cancelled
    elif shape < 0.7:
        returned = list(call)
    else:
        returned = [aid for aid in call if rng.random() < 0.5]
    landing = rng.random()
    if landing < 0.15:
        pass  # every claim attempt was lost: only the commit can end the call
    elif landing < 0.3:
        world.late_claims.append((parent, returned, call))  # overtaken by its commit
    else:
        _land_claim(world, parent, returned, call)
    if returned:
        world.claims.append((parent, returned, call))


def _land_claim(world: _World, parent: str, returned: list[str], call: list[str]) -> None:
    """The gateway processing a claim, at once or late."""
    reg = world.registry
    marked = len(world.marked)
    reg.finish(parent, call, returned)
    if len(world.marked) != marked:
        world.violations.append(f"{parent}: the claim settled something")


def _answer(world: _World, index: int) -> list[asyncio.Task[str]]:
    """The dispatcher's word on one claimed response: written, dropped or lost."""
    parent, returned, call = world.claims.pop(index)
    roll = world.rng.random()
    if roll < 0.15:
        return []  # the report was lost: the claim expires into ordinary delivery
    written = roll < 0.65
    reg = world.registry
    records = reg._records.get(parent, {})
    if written:
        for aid in returned:
            rec = records.get(aid)
            if (
                rec is not None
                and rec.state in ("collecting", "held", "claimed")
                and not rec.retired
            ):
                world.reached.add(aid)  # the written response carried it
                world.confirmed[aid] = world.now[0]
                if rec.held:
                    world.returned_held.add(aid)
    return reg.commit(parent, returned, written, released=call)


async def _run_case(seed: int) -> _World:
    rng = random.Random(seed)
    now = [0.0]
    registry = InlineCollections(clock=lambda: now[0])
    world = _World(rng=rng, registry=registry, now=now)
    registry.bind(_manager(world))
    parents = [f"cron:job{i}:run" for i in range(rng.randint(1, 3))]
    calls: dict[str, list[list[str]]] = {p: [] for p in parents}
    pending: list[asyncio.Task[str]] = []
    serial = 0

    for _ in range(rng.randint(8, 40)):
        parent = rng.choice(parents)
        op = rng.random()
        if op < 0.18:
            call = []
            for _ in range(rng.randint(1, 3)):
                serial += 1
                aid = f"m{serial}"
                if registry.reserve(parent, aid, rng.choice([60, 600])):
                    world.members[aid] = parent
                    world.member_gen[aid] = world.generation.get(parent, 0)
                    world.known.add(aid)
                    call.append(aid)
            if call:
                calls[parent].append(call)
        elif op < 0.40:
            todo = [a for a, p in world.members.items() if p == parent and a not in world.completed]
            if todo:
                _complete(world, rng.choice(todo))
        elif op < 0.50 and calls[parent]:
            _close(world, parent, calls[parent].pop(rng.randrange(len(calls[parent]))))
            if rng.random() < 0.25:
                _retire(world, parent)  # torn down between the claim and the answer
        elif op < 0.58 and world.claims:
            pending.extend(_answer(world, rng.randrange(len(world.claims))))
            if rng.random() < 0.25:
                _retire(world, parent)  # torn down before the commit's settles ran
        elif op < 0.62:
            _retire(world, parent)
        elif op < 0.68 and world.known:
            world.known.discard(rng.choice(sorted(world.known)))  # retention eviction
        elif op < 0.73:
            now[0] += rng.choice([30.0, CLAIM_TTL_SECS + 1, 600 + COLLECTION_GRACE_SECS + 1])
            registry._expire(parent)
        elif op < 0.82:
            world.busy[parent] = not world.busy.get(parent, False)
        elif op < 0.88:
            world.route_fault[parent] = rng.choice(["allocation", "raise", "queued"])
        elif op < 0.94 and world.reports:
            world.reports.pop(rng.choice(sorted(world.reports))).set()
        elif op < 0.98 and world.late_claims:
            _land_claim(world, *world.late_claims.pop(rng.randrange(len(world.late_claims))))
        await _yield(world)
        for gone in world.retired_parents:
            calls[gone] = []  # the parent's turn ended with it
        world.retired_parents.clear()

    # Quiesce: every report ends, every parent goes idle, the remaining calls
    # are cancelled, members still running finish, every bound passes.
    for gate in world.reports.values():
        gate.set()
    world.busy.clear()
    for parent, open_calls in calls.items():
        for call in open_calls:
            registry.finish(parent, call, [])
    while world.claims:
        pending.extend(_answer(world, 0))
    while world.late_claims:
        _land_claim(world, *world.late_claims.pop(0))
    for aid in [a for a in world.members if a not in world.completed]:
        _complete(world, aid)
    for gate in world.reports.values():
        gate.set()  # including the reports of members completed just now
    now[0] += 10 * (7200 + COLLECTION_GRACE_SECS)
    for parent in parents:
        registry._expire(parent)
    for _ in range(20):
        live = [*pending, *registry._tasks]
        if not live:
            break
        await asyncio.wait_for(asyncio.gather(*live, return_exceptions=True), timeout=10)
        pending = []
    for parent in parents:
        registry._expire(parent)
    await asyncio.sleep(0)  # the finished deliveries' done-callbacks
    return world


@pytest.mark.asyncio
async def test_no_retired_parent_receives_output_and_nothing_undelivered_is_marked() -> None:
    with patch("kiro_crew.subagent_inline_collection._ORPHAN_IDLE_POLL_SECS", 0):
        for seed in range(CASES):
            world = await _run_case(seed)
            assert not world.violations, f"seed {seed}: {world.violations}"
            marked = world.marked
            assert len(marked) == len(set(marked)), f"seed {seed}: settled twice: {marked}"
            assert set(marked) <= world.reached, (
                f"seed {seed}: marked delivered but never reached its parent: "
                f"{sorted(set(marked) - world.reached)}"
            )
            # A redelivery never touched the record the hold parked.
            assert all(info._delivery_queued for info in world.held.values()), f"seed {seed}"
            assert not any(info._report_undelivered for info in world.held.values())
            unsettled = world.returned_held - set(marked)
            assert not unsettled, f"seed {seed}: returned but never settled: {sorted(unsettled)}"
            assert world.registry._records == {}, f"seed {seed}: {world.registry._records}"
            assert not world.registry._tasks

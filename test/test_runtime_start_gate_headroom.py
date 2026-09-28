"""The session-start gate always keeps a permit for a start that has not gone out.

A timed-out ``session/new`` hands its gate permit to a
:class:`~kiro_crew.acp.runtime.StartCollector`, which holds it for
``agent.start_collect_timeout_secs`` (default 300 s). At the default limit of 2
that let two slow starts park BOTH permits for five minutes, so every
``session/new`` on the gateway queued behind requests that had already given up
-- and the retries those failures produced timed out too and created more
collectors. These tests pin the reservation that bounds the collecting
population instead of letting it become the whole gate.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.acp.runtime import SessionStartGate


@pytest.mark.asyncio
async def test_a_collector_may_park_a_permit_while_headroom_remains():
    gate = SessionStartGate(2)
    permit = await gate.acquire()

    assert permit.hold_for_collector() is True
    assert gate.collector_holds == 1
    assert permit.collector_held is True


@pytest.mark.asyncio
async def test_collectors_can_never_take_the_last_permit():
    """The starvation itself: both permits parked by callers that gave up."""
    gate = SessionStartGate(2)
    first = await gate.acquire()
    second = await gate.acquire()

    assert first.hold_for_collector() is True
    assert second.hold_for_collector() is False, "a collector took the reserved permit"
    assert gate.collector_holds == 1
    assert second.collector_held is False


@pytest.mark.asyncio
async def test_a_denied_permit_still_frees_its_slot_for_a_new_start():
    """The consequence that matters: a fresh session/new is not made to queue.

    The denied permit is released by its caller, so the gate has a free slot
    even though a collector is parked on the other one -- which is exactly the
    case that produced 15 consecutive ``session/new timed out`` cycles with no
    agent process ever being spawned.
    """
    gate = SessionStartGate(2)
    parked = await gate.acquire()
    denied = await gate.acquire()
    assert parked.hold_for_collector() is True
    assert denied.hold_for_collector() is False
    denied.release()

    fresh = await asyncio.wait_for(gate.acquire(), timeout=1.0)

    assert fresh.queue_wait_ms >= 0.0
    assert gate.active == 2  # the parked collector plus this new start


@pytest.mark.asyncio
async def test_a_single_permit_gate_parks_nothing():
    """``limit == 1`` admits only one reading of "keep one free": park nothing."""
    gate = SessionStartGate(1)
    permit = await gate.acquire()

    assert gate.collector_hold_ceiling == 0
    assert permit.hold_for_collector() is False


@pytest.mark.asyncio
async def test_releasing_a_parked_permit_returns_the_collector_hold():
    """Otherwise the ceiling is reached once and never recovers."""
    gate = SessionStartGate(3)
    first = await gate.acquire()
    second = await gate.acquire()
    third = await gate.acquire()
    assert first.hold_for_collector() is True
    assert second.hold_for_collector() is True
    assert third.hold_for_collector() is False

    first.release()

    assert gate.collector_holds == 1
    assert third.hold_for_collector() is True


@pytest.mark.asyncio
async def test_release_stays_idempotent_and_decrements_the_hold_once():
    gate = SessionStartGate(2)
    permit = await gate.acquire()
    assert permit.hold_for_collector() is True

    assert permit.release() is True
    assert permit.release() is False

    assert gate.collector_holds == 0
    assert gate.releases == 1


@pytest.mark.asyncio
async def test_an_already_released_permit_cannot_be_parked():
    """A double book-keep here would leak a hold the gate can never return."""
    gate = SessionStartGate(2)
    permit = await gate.acquire()
    permit.release()

    assert permit.hold_for_collector() is False
    assert gate.collector_holds == 0


@pytest.mark.asyncio
async def test_parking_the_same_permit_twice_counts_one_hold():
    gate = SessionStartGate(4)
    permit = await gate.acquire()

    assert permit.hold_for_collector() is True
    assert permit.hold_for_collector() is False

    assert gate.collector_holds == 1

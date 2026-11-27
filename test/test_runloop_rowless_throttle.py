"""A sub-agent with no task row (incognito, temporary) waits out a provider throttle
on the dependency scope, the way a persistent run does.

The coordinator holds such a run in memory (``report(..., hold_rowless=True)``):
its wake comes back through admission and nothing about it reaches the store.
Helpers are the run-loop integration suite's.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import patch

import pytest
from overload_fakes import backoff
from test_runloop_integration import (
    _HELD_PARK_SECS,
    _LOST_RUN_CEILING_SECS,
    AcpFakeThrottle,
    _complete,
    _drain_posted,
    _FakeController,
    _fast_coordinator,
    _mock_sessions,
    _ready_manager,
    _row,
    _text,
)

from kiro_crew.acp.types import STOP_REASON_END_TURN
from kiro_crew.subagent import SubagentInfo
from kiro_crew.taskq import dependency as dep_mod
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")

SCOPE = "provider:acp"


def _pump(monkeypatch, off_loop: bool) -> None:
    """Run the pump on the store's writer thread (production) or inline."""
    from kiro_crew.subagent_manager import admission as admission_mod

    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", off_loop)


@pytest.mark.asyncio
@pytest.mark.parametrize("off_loop", [True, False], ids=["worker_pump", "inline_pump"])
async def test_two_temporary_runs_park_on_one_scope_and_wake_by_capacity(monkeypatch, off_loop):
    """Two temporary runs throttled together join ONE schedule, release their lane
    slots, are woken one at a time through admission and finish. Neither leaves a
    row or an event behind."""
    calls: list[str] = []

    def factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            tag = "A" if "throttled A" in msg else "B"
            if calls.count(msg) == 1:
                raise AcpFakeThrottle("ThrottlingException: Rate exceeded")
            yield _text(f"{tag} ok")
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    _pump(monkeypatch, off_loop)
    mgr = await _ready_manager(_mock_sessions(factory))
    mgr._max_concurrent = 2
    wake_order: list[tuple[str, int]] = []
    coordinator_ref: dict = {}
    orig_wake_through = mgr._monitor.taskq_wake_through

    def _wake_through(task_id: str, generation):
        wake_order.append((task_id, len(coordinator_ref["c"].waiters(SCOPE))))
        return orig_wake_through(task_id, generation)

    # Held, not timed: nothing wakes a waiter until both are observed parked.
    coordinator = _fast_coordinator(
        mgr, backoff=backoff(_HELD_PARK_SECS, _HELD_PARK_SECS), wake_through=_wake_through
    )
    coordinator_ref["c"] = coordinator
    controller = _FakeController()
    monkeypatch.setattr("kiro_crew.adaptive.controller.current", lambda: controller)
    store = mgr._admission.taskq_store()

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        a = mgr.spawn("throttled A", _memory_mode="temporary")
        b = mgr.spawn("throttled B", _memory_mode="temporary")
        assert a is not None and b is not None
        await async_wait_until(
            lambda: sorted(coordinator.waiters(SCOPE)) == sorted([a.id, b.id])
            and mgr._running_count == 0,
            describe=lambda: (f"waiters={coordinator.waiters(SCOPE)} running={mgr._running_count}"),
        )
        assert _row(mgr, a.id) is None and _row(mgr, b.id) is None, "premise: no rows"
        assert coordinator.recovered(SCOPE)
        mgr._taskq_pump()
        await await_until(
            asyncio.gather(mgr._tasks[a.id], mgr._tasks[b.id]), what="both runs finish"
        )

    assert a.outcome == "completed" and b.outcome == "completed"
    assert a.result == "A ok" and b.result == "B ok"
    assert [parked for _, parked in wake_order] == [1, 0]
    assert controller.throttles == [SCOPE, SCOPE]
    assert list(store.events(a.id)) == [] and list(store.events(b.id)) == []
    assert coordinator.scopes() == []
    assert mgr._running_count == 0


@pytest.mark.asyncio
async def test_a_temporary_run_parked_inside_a_pump_pass_is_woken_through_admission(
    monkeypatch,
):
    """The worker-path pass snapshots ``live_waiters`` while the park still awaits
    its report. A run with no row reports generation ``None``; the snapshot must
    carry the same value, or the run is dropped from the schedule and waits on a
    resume nothing will request."""
    from kiro_crew.subagent_manager import admission as admission_mod

    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    mgr = await _ready_manager(_mock_sessions(lambda *a, **k: None))
    coordinator = _fast_coordinator(mgr)
    store = mgr._admission.taskq_store()
    info = SubagentInfo(id="temporary-inside-a-pass", task="t")
    info.memory_mode = "temporary"
    mgr._agents[info.id] = info
    mgr._running_count = 1

    expire_started = threading.Event()
    report_queued = threading.Event()
    tick_queued = threading.Event()
    real_expire = mgr._admission.taskq_expire_waits_store
    real_report = coordinator.report
    real_run = store.run

    def _expire_once_the_report_is_queued(_admission: Any) -> Any:
        expire_started.set()
        assert report_queued.wait(_LOST_RUN_CEILING_SECS), "the park never queued its report"
        return real_expire()

    def _report_once_the_tick_is_queued(*args: Any, **kwargs: Any) -> Any:
        assert tick_queued.wait(_LOST_RUN_CEILING_SECS), "the pass never queued its tick"
        return real_report(*args, **kwargs)

    async def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
        if fn == coordinator.tick:
            tick_queued.set()
        return await real_run(fn, *args, **kwargs)

    monkeypatch.setattr(
        type(mgr._admission), "taskq_expire_waits_store", _expire_once_the_report_is_queued
    )
    monkeypatch.setattr(coordinator, "report", _report_once_the_tick_is_queued)
    monkeypatch.setattr(store, "run", _run)
    signal = dep_mod.DependencySignal(
        kind=dep_mod.KIND_RATE_LIMITED,
        dependency_scope=SCOPE,
        source="acp_provider",
        retry_at=coordinator.now(),
    )

    with patch("kiro_crew.subagent.sel"):
        mgr._taskq_pump()
        pump = mgr._taskq_tick_task
        await async_wait_until(
            expire_started.is_set, describe=lambda: "the pump pass never reached its expire"
        )
        park = asyncio.ensure_future(mgr._yield_for_dependency(info, signal))
        try:
            await async_wait_until(
                lambda: info._taskq_running_marked, describe=lambda: "the park never reported"
            )
            report_queued.set()
            await asyncio.wait_for(pump, timeout=_LOST_RUN_CEILING_SECS)
            assert await asyncio.wait_for(park, timeout=_LOST_RUN_CEILING_SECS) is True
        finally:
            park.cancel()
        await _drain_posted(mgr)

    assert info._slot_released is False and mgr._running_count == 1
    assert _row(mgr, info.id) is None
    assert list(store.events(info.id)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("off_loop", [True, False], ids=["worker_pump", "inline_pump"])
async def test_a_scope_give_up_ends_a_temporary_run(monkeypatch, off_loop):
    """A scope past its attempts cap releases the parked temporary run through
    ``on_fail``; the run ends ``failed`` with the provider error instead of
    retrying in turn, and no row is written for it."""
    calls: list[str] = []

    def factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            raise AcpFakeThrottle("ThrottlingException: Rate exceeded")
            yield  # noqa: unreachable

        return _gen()

    _pump(monkeypatch, off_loop)
    mgr = await _ready_manager(_mock_sessions(factory))
    coordinator = _fast_coordinator(mgr, max_attempts=1)
    store = mgr._admission.taskq_store()
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("do work", _memory_mode="temporary")
        assert info is not None
        await await_until(mgr._tasks[info.id], what="the run finishing")

    assert info.outcome == "failed"
    assert "Rate exceeded" in info.error
    # First report waits; the probe's second report exceeds max_attempts=1.
    assert len(calls) == 2
    assert _row(mgr, info.id) is None
    assert list(store.events(info.id)) == []
    assert coordinator.scopes() == []
    assert mgr._running_count == 0 and not info._resume_pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "scope", "outcome"),
    [
        (dep_mod.KIND_RATE_LIMITED, SCOPE, "wait"),
        (dep_mod.KIND_CONCURRENCY_EXCEEDED, SCOPE, "wait"),
        (dep_mod.KIND_DEPENDENCY_UNAVAILABLE, SCOPE, "no_row"),
        (dep_mod.KIND_CONCURRENCY_EXCEEDED, "mcp_gateway:tools", "no_row"),
    ],
)
async def test_only_a_provider_throttle_holds_a_run_with_no_row(kind, scope, outcome):
    """A run with no row is held on a provider throttle only. A 5xx or a lost
    connection keeps the in-turn ladder, and our own gateway scope keeps the L1
    rung's answer (the completion is surfaced), as before."""
    mgr = await _ready_manager(_mock_sessions(lambda *a, **k: None))
    coordinator = _fast_coordinator(mgr)
    info = SubagentInfo(id="temporary-verdict", task="t")
    info.memory_mode = "temporary"
    mgr._agents[info.id] = info
    signal = dep_mod.DependencySignal(kind=kind, dependency_scope=scope, source="test")

    verdict = await mgr._run_events._dependency_verdict(coordinator, info, signal)

    assert verdict.outcome == outcome
    assert (coordinator.waiters(scope) == [info.id]) is (outcome == "wait")
    coordinator.forget(info.id)

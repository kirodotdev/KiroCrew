"""Run-loop integration: the wave-1/2 pieces as ONE system (X1 glue).

Every test drives the REAL ``SubagentManager.spawn -> _run -> _run_inner``
path with only the ACP provider faked (a stream factory), a real durable task
store under ``$KIROCREW_HOME`` and the real admission pump, so the assertions
are about the production event handling, not about a rebuilt model of it.

What is pinned here:

* an in-place stop recovery yields the LANE slot through admission
  (``waiting_dependency`` row, ``subagent_waiting``) and comes back through the
  pump (``subagent_resumed``, generation+1) -- never by polling the running
  count;
* the durable row is ``running`` from the run's FIRST stream event, not from
  execution start;
* a provider throttle parks the run on the dependency coordinator's ONE
  per-scope schedule (two runs, one scope, staged wakes), the lane slot is
  released meanwhile, and the adaptive controller sees the typed throttle;
* a coordinator that fails the scope ends the run instead of parking it forever;
* a tool call the gateway refused for capacity is retried in place through the
  ladder's L1 rung and the same coordinator;
* a typed ``waiting_input`` status releases the lane slot and steers the
  recovery prompt (M's protocol, not the evidence text);
* the main chat floors its transient backoff by the shared scope schedule and
  reports the throttle to the controller; its pipe-death budget and the
  sub-agent's stop budget read ONE ladder constant;
* gatewayd's backend respawn is the ladder's L2 rung; ``session/new`` outcomes
  reach ``AdaptiveController.record_start``.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from overload_fakes import backoff, settle_store_writes

from kiro_crew.acp.types import (
    STATUS_ORIGIN_LIVENESS_ORACLE,
    STOP_REASON_END_TURN,
    STOP_REASON_TOOL_STALL,
    STOP_RECOVERY_MAX_RETRIES,
    WAIT_REASON_INPUT,
    StructuredStatus,
)
from kiro_crew.dashboard.state import REFUSAL_RECOVERY_PREFIX, TOOL_STALL_RECOVERY_PREFIX
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.recovery import ladder as ladder_mod
from kiro_crew.recovery.ladder import (
    L1_TOOL_CALL,
    L2_BACKEND,
    SESSION_RECOVERY_MAX_ATTEMPTS,
    InfraError,
    default_ladder,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.monitoring import OrphanStallMonitor
from kiro_crew.taskq import RUNNING, WAITING_DEPENDENCY, WAITING_INPUT
from kiro_crew.taskq import dependency as dep_mod
from kiro_crew.taskq.dependency import DependencyCoordinator

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_STALL_EVIDENCE = "verdict=dead; idle_secs=700; tool=execute_bash"


# ── harness ──────────────────────────────────────────────────────────────────


class AcpFakeThrottle(Exception):
    """Duck-typed AcpError (name starts with ``Acp``) carrying the transient verdict."""

    transient = True


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=text, runtime_global=False)


def _complete(stop_reason: str, text: str = "", status: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        kind=EVENT_COMPLETE,
        stop_reason=stop_reason,
        text=text,
        title="execute_bash",
        tool_input="pytest -q > run.log 2>&1",
        runtime_global=False,
        refusal=None,
        status=status,
    )


def _status_event(status: StructuredStatus) -> SimpleNamespace:
    from kiro_crew.acp.types import EVENT_STRUCTURED_STATUS

    return SimpleNamespace(
        kind=EVENT_STRUCTURED_STATUS,
        text="",
        tool_call_id=status.tool_call_id,
        runtime_global=False,
        status=status,
    )


def _mock_sessions(stream_factory) -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=stream_factory)
    # A MagicMock exposes every attribute; the run loop keys on ``isinstance``.
    provider.last_infra_error = None
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    sessions._provider = provider
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(side_effect=lambda msg, *a, **k: (msg, None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    return ctx


def _manager(sessions: MagicMock) -> SubagentManager:
    """Construct only; a caller without a running loop gets the store inline."""
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder())
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    mgr._spawn_stagger_secs = 0.0
    return mgr


async def _ready_manager(sessions: MagicMock) -> SubagentManager:
    """A loop caller's manager opens its store on a worker: wait for the attach."""
    mgr = _manager(sessions)
    await mgr.wait_taskq_ready()
    assert mgr._admission.taskq_store() is not None, "these tests need the durable store"
    return mgr


def _fast_coordinator(mgr: SubagentManager, **overrides: Any) -> DependencyCoordinator:
    """The manager's coordinator with a millisecond backoff, wired like production."""
    monitor = mgr._monitor
    params: dict[str, Any] = dict(
        backoff=backoff(0.01, 0.02),
        wake_spacing_secs=0.0,
        wake_per_tick=1,
        on_wake=monitor.taskq_on_wake,
        wake_through=monitor.taskq_wake_through,
        on_fail=monitor.taskq_on_wait_failed,
    )
    params.update(overrides)
    coordinator = DependencyCoordinator(mgr._admission.taskq_store(), **params)
    mgr._taskq_dependency_coordinator = coordinator
    dep_mod.register_coordinator(coordinator)
    return coordinator


def _spy_events(mgr: SubagentManager) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    _orig = mgr._fire_event

    async def _spy(etype, info, extra=None):
        events.append((etype, dict(extra or {})))
        await _orig(etype, info, extra)

    mgr._fire_event = _spy
    return events


async def _spawn_and_wait(mgr: SubagentManager, task: str = "do work") -> SubagentInfo:
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn(task)
        assert info is not None
        await asyncio.wait_for(mgr._tasks[info.id], timeout=20.0)
    return info


def _row(mgr: SubagentManager, agent_id: str):
    store = mgr._admission.taskq_store()
    assert store is not None
    return store.get(agent_id)


async def _drain_posted(mgr: SubagentManager) -> None:
    """Let every POSTED store write land -- a SIGNAL, never a sleep.

    Wait on the tracked write set until it is empty (a posted write can post
    another), then flush the store's single writer thread behind them.

    Two different failures, and each is named where it happens. The round count
    bounds a CHAIN of posted writes, never wall-clock time -- each round AWAITS
    the writes it found -- so a slower store needs no more rounds, and the
    exhausted loop re-reads the set before it reports, because the sample the
    last round awaited says nothing about what that round's writes then posted.
    Each round's wait is BOUNDED and never cancels what it waited on: an
    unbounded await on a write that never lands is a hang, and a hang is a lost
    run under ``--max-worker-restart=0`` rather than a failure anyone can read.
    """
    for _ in range(_POSTED_WRITE_ROUNDS):
        pending = [t for t in set(mgr._report_tasks) if not t.done()]
        if not pending:
            break
        done, unfinished = await asyncio.wait(pending, timeout=_POSTED_WRITE_CEILING_SECS)
        for task in done:
            if not task.cancelled():
                # Retrieved, never raised: a posted write's own failure is the
                # subject of whichever assertion the caller makes about the row
                # it wrote, never of the drain that waited for it.
                task.exception()
        assert not unfinished, (
            f"{len(unfinished)} posted store write(s) never landed within "
            f"{_POSTED_WRITE_CEILING_SECS}s: {[repr(t) for t in unfinished]}"
        )
    else:
        stalled = [t for t in set(mgr._report_tasks) if not t.done()]
        assert not stalled, (
            f"{len(stalled)} posted store write(s) still unfinished after "
            f"{_POSTED_WRITE_ROUNDS} rounds of chained posts: {[repr(t) for t in stalled]}"
        )
    store = mgr._admission.taskq_store()
    if store is not None:
        await settle_store_writes(store)


#: Rounds :func:`_drain_posted` gives a chain of posted writes to settle.
_POSTED_WRITE_ROUNDS = 20

#: Ceiling on ONE :func:`_drain_posted` round. A lost-run guard, never the
#: barrier: the round returns the moment its writes land, so only a write that
#: never lands reaches it.
_POSTED_WRITE_CEILING_SECS = 30.0

#: Ceiling on the parked-state wait below. Generous on purpose: only a park that
#: never happens trips it, and a trip then names the state it observed -- never a
#: silent return, because the assertions after the barrier would then fail on a
#: consequence of the missing park instead of on the park.
_PARKED_CEILING_SECS = 30.0


def _park_snapshot(
    mgr: SubagentManager,
    coordinator: DependencyCoordinator,
    scope: str,
    agent_ids: tuple[str, ...],
    *,
    rows: bool = True,
) -> tuple[list[str], int, dict[str, str | None]]:
    """The whole park conjunction as ONE reading: waiters, lane use, row states.

    Stated once so the barrier below and its verdict cannot disagree about what
    a park is.

    ``rows=False`` leaves every state unread as ``None``, which is never
    ``WAITING_DEPENDENCY``, so the verdict is the same one either way. Each row
    IS read through a synchronous ``store.get`` on the gateway loop -- the take
    ``on_loop_db`` guards and ``store.loop_thread_calls`` counts -- inside the
    very window whose timing this barrier exists to observe, so the poll pays
    for the rows only once the two in-memory halves already agree, and the
    ceiling's verdict pays for them always.
    """
    states: dict[str, str | None] = {agent_id: None for agent_id in agent_ids}
    if rows:
        states = {agent_id: getattr(_row(mgr, agent_id), "state", None) for agent_id in agent_ids}
    return (coordinator.waiters(scope), mgr._running_count, states)


async def _await_parked(
    mgr: SubagentManager, coordinator: DependencyCoordinator, scope: str, *agent_ids: str
) -> None:
    """Wait until every id is parked on *scope* AND its lane slot is back.

    A park is not one write. The coordinator registers the waiter on the store's
    WRITER THREAD, and the lane slot is yielded by the continuation that the
    thread's wake schedules on the loop -- so between those two moments a reader
    sees the waiter already registered and the slot still held. A barrier that
    stops at the waiter count therefore samples the running count mid-park, which
    is invisible where a cross-thread wake costs microseconds and reads as
    ``1 == 0`` where it costs tens of milliseconds. The signal is the whole
    conjunction, so that is what is waited for.

    The ceiling is a lost-run guard, never the barrier, and reaching it RAISES
    with the conjunction it last read: a barrier that returns on a park that
    never happened hands its caller a state the caller never asked about, and the
    failure then reads as whichever later assertion happens to touch it first.
    """
    ids = tuple(agent_ids)
    deadline = time.monotonic() + _PARKED_CEILING_SECS
    while time.monotonic() < deadline:
        waiters, running, _ = _park_snapshot(mgr, coordinator, scope, ids, rows=False)
        if len(waiters) == len(ids) and running == 0:
            _, _, states = _park_snapshot(mgr, coordinator, scope, ids)
            if all(state == WAITING_DEPENDENCY for state in states.values()):
                return
        await asyncio.sleep(0.01)
    waiters, running, states = _park_snapshot(mgr, coordinator, scope, ids)
    raise AssertionError(
        f"no park of {len(ids)} run(s) on {scope!r} within {_PARKED_CEILING_SECS}s: "
        f"waiters={waiters} (want {list(ids)}), running_count={running} (want 0), "
        f"rows={states} (want every state {WAITING_DEPENDENCY!r}), "
        f"scopes={coordinator.scopes()}"
    )


def _transitions(mgr: SubagentManager, agent_id: str) -> list[tuple[str, str, str]]:
    """``(kind, from, to)`` for every transition event on a row, in order."""
    store = mgr._admission.taskq_store()
    assert store is not None
    return [
        (ev.kind, str(ev.data.get("from") or ""), str(ev.data.get("to") or ""))
        for ev in store.events(agent_id)
        if ev.kind in ("transition", "rejected_transition")
    ]


@pytest.fixture(autouse=True)
def _fresh_ladder_and_coordinator():
    ladder_mod._reset_default_ladder_for_tests()
    dep_mod.register_coordinator(None)
    yield
    ladder_mod._reset_default_ladder_for_tests()
    dep_mod.register_coordinator(None)


# ── 1. stop recovery goes through admission ──────────────────────────────────


@pytest.mark.asyncio
async def test_stop_recovery_yields_and_resumes_through_admission():
    """A stall yields the LANE slot (row ``waiting_dependency``) and comes back
    through the pump (``subagent_resumed``, generation+1) -- no count polling."""
    states_at_stream: list[tuple[str, int]] = []
    calls: list[str] = []
    mgr_ref: dict = {}

    def factory(msg: str, *a, **kw):
        calls.append(msg)
        mgr = mgr_ref["mgr"]
        info = next(iter(mgr._agents.values()))
        row = _row(mgr, info.id)
        states_at_stream.append((row.state, row.generation))

        async def _gen():
            if len(calls) == 1:
                yield _text("partial ")
                yield _complete(STOP_REASON_TOOL_STALL, _STALL_EVIDENCE)
            else:
                yield _text("done")
                yield _complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = await _ready_manager(_mock_sessions(factory))
    mgr_ref["mgr"] = mgr
    events = _spy_events(mgr)
    info = await _spawn_and_wait(mgr)

    assert info.outcome == "completed" and info.result == "partial done"
    assert calls[1].startswith(TOOL_STALL_RECOVERY_PREFIX)
    kinds = [k for k, _ in events]
    assert "subagent_waiting" in kinds and "subagent_resumed" in kinds
    waiting = next(e for k, e in events if k == "subagent_waiting")
    assert waiting["state"] == WAITING_DEPENDENCY
    assert waiting["reason"].startswith("stalled completion")
    assert waiting["residency_charged"] is True
    # The nudge streamed under a NEW generation: the wake fenced the wait. (At
    # the first stream CALL the row is still ``starting`` -- ``running`` is
    # written on the first event, see the next test.)
    assert states_at_stream[0][0] == "starting"
    assert states_at_stream[1][0] == RUNNING
    assert states_at_stream[1][1] == states_at_stream[0][1] + 1
    row = _row(mgr, info.id)
    kinds_in_store = [ev.kind for ev in mgr._admission.taskq_store().events(info.id)]
    assert "wake" in kinds_in_store and row.state == "done"
    assert mgr._running_count == 0 and not info._resume_pending


def test_stop_recovery_no_longer_polls_the_running_count():
    """Source pin for the deleted duplicate: the yield must not sleep-poll."""
    import inspect

    from kiro_crew.subagent_manager import run as run_mod

    src = inspect.getsource(run_mod.RunEventCoordinator._yield_for_stop_recovery_impl)
    assert "asyncio.sleep(0.25)" not in src
    assert "_running_count < " not in src
    assert "_await_lane_resume" in src


# ── 2. running mark at the first stream event ────────────────────────────────


@pytest.mark.asyncio
async def test_row_is_running_from_the_first_stream_event_not_exec_start():
    # Suite-wide ``pump_off_loop=False`` (test/conftest.py) makes the running
    # mark INLINE here, so ``after_first_event`` is a statement about program
    # order on that path -- not about production, where the mark is posted.
    # ``test_a_wait_in_the_first_frame_batch_lands_behind_the_running_mark``
    # is the one that covers the posted ordering.
    seen: dict[str, str] = {}
    mgr_ref: dict = {}

    def factory(msg: str, *a, **kw):
        mgr = mgr_ref["mgr"]
        info = next(iter(mgr._agents.values()))
        seen["at_stream_call"] = _row(mgr, info.id).state

        async def _gen():
            seen["at_first_event"] = _row(mgr, info.id).state
            yield _text("x")
            seen["after_first_event"] = _row(mgr, info.id).state
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = await _ready_manager(_mock_sessions(factory))
    mgr_ref["mgr"] = mgr
    info = await _spawn_and_wait(mgr)
    assert info.outcome == "completed"
    assert seen["at_stream_call"] == "starting"
    assert seen["at_first_event"] == "starting"
    assert seen["after_first_event"] == RUNNING
    assert info._taskq_running_marked is True


@pytest.mark.asyncio
async def test_a_wait_in_the_first_frame_batch_lands_behind_the_running_mark(monkeypatch):
    """W4 carried by the run's FIRST frame batch, with the production off-loop
    pump: the wait write is ordered behind the posted ``running`` mark.

    ``session_handle`` yields every event of ONE update back to back and appends
    the structured status to that same list, so there is no loop iteration
    between the frame that marks the row ``running`` and the status that records
    the wait. An INLINE wait write reaches a still-``starting`` row, is refused,
    and the late mark leaves the row ``running`` with no durable wait reason.
    """
    from kiro_crew.subagent_manager import admission as admission_mod

    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    status = StructuredStatus(
        phase="waiting",
        wait_reason=WAIT_REASON_INPUT,
        tool_call_id="call-1",
        cancellable=True,
        origin=STATUS_ORIGIN_LIVENESS_ORACLE,
    )
    seen: dict[str, Any] = {}
    mgr_ref: dict = {}

    def factory(msg: str, *a, **kw):
        async def _gen():
            mgr = mgr_ref["mgr"]
            info = next(iter(mgr._agents.values()))
            yield _text("partial ")
            yield _status_event(status)
            # The barrier is AFTER both frames, never between them: the two
            # writes must already be queued in submission order.
            await _drain_posted(mgr)
            store = mgr._admission.taskq_store()
            seen["row"] = await store.run(store.get, info.id)
            seen["events"] = _transitions(mgr, info.id)
            seen["wait_record"] = dict(info._wait_record or {})
            seen["running_count"] = mgr._running_count
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = await _ready_manager(_mock_sessions(factory))
    mgr_ref["mgr"] = mgr
    info = await _spawn_and_wait(mgr)
    await _drain_posted(mgr)

    assert info.outcome == "completed"
    assert seen["row"].state == WAITING_INPUT
    assert seen["row"].wait is not None
    assert seen["row"].wait["resume_condition"]["kind"] == "input"
    assert seen["wait_record"]["state"] == WAITING_INPUT
    assert seen["running_count"] == 0
    # The whole point: no refused edge on the way there.
    assert [e for e in seen["events"] if e[0] == "rejected_transition"] == []
    assert [(f, t) for _k, f, t in seen["events"]] == [
        ("admitted", "starting"),
        ("starting", RUNNING),
        (RUNNING, WAITING_INPUT),
    ]


# ── 3. dependency waits ──────────────────────────────────────────────────────


class _FakeController:
    def __init__(self) -> None:
        self.throttles: list[str] = []
        self.starts: list[dict] = []

    def record_provider_throttle(self, scope: str) -> None:
        self.throttles.append(scope)

    def record_start(self, duration_ms: float, **kw: Any) -> None:
        self.starts.append({"duration_ms": duration_ms, **kw})


@pytest.mark.asyncio
async def test_throttle_parks_two_runs_on_one_scope_and_wakes_by_capacity(monkeypatch):
    """Two runs throttled by the same provider join ONE schedule, release
    their lane slots while parked, are woken one at a time (probe, then the
    rest) and finish; the controller is told about the typed throttle."""
    calls: list[str] = []
    waiting_seen: list[tuple[str, int]] = []
    mgr_ref: dict = {}

    def factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            mgr = mgr_ref["mgr"]
            tag = "A" if "throttled A" in msg else "B"
            if calls.count(msg) == 1:
                raise AcpFakeThrottle("ThrottlingException: Rate exceeded")
            waiting_seen.append((tag, mgr._running_count))
            yield _text(f"{tag} ok")
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = await _ready_manager(_mock_sessions(factory))
    mgr_ref["mgr"] = mgr
    mgr._max_concurrent = 2
    # The wake ORDER is observed at the coordinator's own seam (how many
    # waiters were still parked when each wake left the schedule), not from
    # ``_running_count`` inside the resumed generators: whether the second
    # grant lands before the first coroutine gets loop time is scheduling
    # latency, which a loaded xdist worker changes freely.
    wake_order: list[tuple[str, int]] = []
    coordinator_ref: dict = {}
    orig_wake_through = mgr._monitor.taskq_wake_through

    def _wake_through(task_id: str, generation):
        wake_order.append((task_id, len(coordinator_ref["c"].waiters("provider:acp"))))
        return orig_wake_through(task_id, generation)

    # A 0.3s backoff keeps the parked state observable before the wake.
    coordinator = _fast_coordinator(mgr, backoff=backoff(0.3, 0.3), wake_through=_wake_through)
    coordinator_ref["c"] = coordinator
    controller = _FakeController()
    monkeypatch.setattr("kiro_crew.adaptive.controller.current", lambda: controller)
    events = _spy_events(mgr)

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        a = mgr.spawn("throttled A")
        b = mgr.spawn("throttled B")
        assert a is not None and b is not None
        # Both parked: one scope, two waiters, both lane slots released.
        await _await_parked(mgr, coordinator, "provider:acp", a.id, b.id)
        assert coordinator.scopes() == ["provider:acp"]
        assert mgr._running_count == 0
        assert _row(mgr, a.id).state == WAITING_DEPENDENCY
        assert _row(mgr, b.id).state == WAITING_DEPENDENCY
        assert _row(mgr, a.id).wait["dependency_scope"] == "provider:acp"
        await asyncio.wait_for(asyncio.gather(mgr._tasks[a.id], mgr._tasks[b.id]), timeout=20.0)

    assert a.outcome == "completed" and b.outcome == "completed"
    assert a.result == "A ok" and b.result == "B ok"
    # Woken one at a time: the probe left while the other waiter was still
    # parked, the ramp woke the last one; both replays ran under a held slot
    # within the cap.
    assert [parked for _, parked in wake_order] == [1, 0]
    assert {task_id for task_id, _ in wake_order} == {a.id, b.id}
    assert len(waiting_seen) == 2 and all(1 <= c <= 2 for _, c in waiting_seen)
    assert controller.throttles == ["provider:acp", "provider:acp"]
    waits = [e for k, e in events if k == "subagent_waiting"]
    assert len(waits) == 2 and all(w["state"] == WAITING_DEPENDENCY for w in waits)
    assert [k for k, _ in events].count("subagent_resumed") == 2
    assert coordinator.scopes() == []  # forget() on terminal cleared the scope
    assert mgr._running_count == 0


@pytest.mark.asyncio
async def test_a_throttle_before_the_first_frame_waits_under_the_run_lease(monkeypatch):
    """A 429 on the very first prompt, with the production off-loop pump: the
    row goes ``starting -> running -> waiting_dependency`` and its terminal
    write lands.

    Nothing marked the row ``running`` yet (no frame arrived), and the
    coordinator's wait write is INLINE because its result chooses wait-vs-park,
    so the mark has to share its thread. Without that the wait is refused, the
    row is PARKED in ``retry_wait`` -- which has an edge to neither ``running``
    nor ``done`` -- and the completed run's ``done`` write is refused too,
    leaving the row claimable for the dispatcher to re-run finished work.
    """
    from kiro_crew.subagent_manager import admission as admission_mod

    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    calls: list[str] = []
    mgr_ref: dict = {}
    parked: dict[str, Any] = {}

    def factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                raise AcpFakeThrottle("ThrottlingException: Rate exceeded")
            mgr = mgr_ref["mgr"]
            parked["events_at_replay"] = _transitions(mgr, next(iter(mgr._agents.values())).id)
            yield _text("ok")
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = await _ready_manager(_mock_sessions(factory))
    mgr_ref["mgr"] = mgr
    _fast_coordinator(mgr)
    info = await _spawn_and_wait(mgr)
    await _drain_posted(mgr)

    assert info.outcome == "completed" and info.result == "ok"
    assert [(f, t) for _k, f, t in parked["events_at_replay"]] == [
        ("admitted", "starting"),
        ("starting", RUNNING),
        (RUNNING, WAITING_DEPENDENCY),
    ]
    assert [e for e in parked["events_at_replay"] if e[0] == "rejected_transition"] == []
    row = _row(mgr, info.id)
    assert row.state == "done" and row.terminal is True
    assert row.next_run_at in (None, 0.0)  # not claimable: the work is finished


@pytest.mark.asyncio
async def test_the_throttle_park_and_wake_cycle_never_touches_the_store_on_the_loop(monkeypatch):
    """The whole wait/wake boundary under the STRICT on-loop guard.

    Covers, in one run, every store touch the boundary makes: the ``running``
    mark, the coordinator's wait write, the slot yield, the pump's wake
    (``resume_grant``) and the terminal settle with its child propagation. The
    guard raises on a loop-thread connection take, so a site that slips back
    reds here instead of costing an operator a 25 s watchdog kill.
    """
    from kiro_crew.subagent_manager import admission as admission_mod
    from kiro_crew.taskq import store as store_mod

    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    calls: list[str] = []

    def factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                raise AcpFakeThrottle("ThrottlingException: Rate exceeded")
            yield _text("ok")
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = await _ready_manager(_mock_sessions(factory))
    _fast_coordinator(mgr)
    store = mgr._admission.taskq_store()
    before = store.loop_thread_calls
    monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
    try:
        # ``spawn_async`` -- the gateway's own entry point -- not the sync
        # ``spawn`` API, whose accept is inline by design for a caller with no
        # loop to offload onto.
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = await mgr.spawn_async("throttled")
            assert info is not None
            await asyncio.wait_for(mgr._tasks[info.id], timeout=20.0)
        await _drain_posted(mgr)
    finally:
        monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)

    assert info.outcome == "completed" and len(calls) == 2
    assert store.loop_thread_calls == before  # the reads below are the test's own
    assert _row(mgr, info.id).state == "done"
    # The wake back to ``running`` is a ``wake`` event, not a transition.
    assert [t for _k, _f, t in _transitions(mgr, info.id)] == [
        "starting",
        RUNNING,
        WAITING_DEPENDENCY,
        "done",
    ]
    wakes = [ev for ev in store.events(info.id) if ev.kind == "wake"]
    assert [ev.data["to"] for ev in wakes] == [RUNNING]


@pytest.mark.asyncio
async def test_dependency_scope_failure_ends_the_run():
    """A scope past its attempts cap fails the row and releases the parked run
    (``on_fail``), which ends ``failed`` with the provider error, not stuck."""
    calls: list[str] = []

    def factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            raise AcpFakeThrottle("ThrottlingException: Rate exceeded")
            yield  # noqa: unreachable

        return _gen()

    mgr = await _ready_manager(_mock_sessions(factory))
    coordinator = _fast_coordinator(mgr, max_attempts=1)
    info = await _spawn_and_wait(mgr)
    assert info.outcome == "failed"
    assert "Rate exceeded" in info.error
    # First report waits; the probe's second report exceeds max_attempts=1.
    assert len(calls) == 2
    assert _row(mgr, info.id).state == "failed"
    assert coordinator.scopes() == []
    assert mgr._running_count == 0 and not info._resume_pending


@pytest.mark.asyncio
async def test_unclassified_transient_keeps_the_in_turn_ladder():
    """An error no adapter knows is not a dependency wait: the bounded in-turn
    retry (same prompt, ``subagent_retrying``) still handles it."""

    class _Odd(Exception):
        transient = True

    calls: list[str] = []

    def factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                raise _Odd("weird hiccup")
            yield _text("ok")
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = await _ready_manager(_mock_sessions(factory))
    coordinator = _fast_coordinator(mgr)
    events = _spy_events(mgr)
    with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
        info = await _spawn_and_wait(mgr)
    assert info.outcome == "completed"
    assert len(calls) == 2 and calls[0] == calls[1]  # same prompt replayed
    assert any(k == "subagent_retrying" for k, _ in events)
    assert not any(k == "subagent_waiting" for k, _ in events)
    assert coordinator.scopes() == []


# ── 4. L1: gateway capacity refusal retried in place ─────────────────────────


@pytest.mark.asyncio
async def test_capacity_refused_tool_call_is_retried_in_place_via_ladder_and_scope():
    calls: list[str] = []
    provider_ref: dict = {}

    def factory(msg: str, *a, **kw):
        calls.append(msg)
        provider = provider_ref["provider"]

        async def _gen():
            if len(calls) == 1:
                provider.last_infra_error = InfraError("capacity", retry_after_secs=0.0)
                yield _text("first half ")
                yield _complete(STOP_REASON_END_TURN)
            else:
                provider.last_infra_error = None
                yield _text("second half")
                yield _complete(STOP_REASON_END_TURN)

        return _gen()

    sessions = _mock_sessions(factory)
    provider_ref["provider"] = sessions._provider
    mgr = await _ready_manager(sessions)
    coordinator = _fast_coordinator(mgr)
    events = _spy_events(mgr)
    info = await _spawn_and_wait(mgr)
    assert info.outcome == "completed"
    assert info.result == "first half second half"
    assert len(calls) == 2 and calls[1].startswith(REFUSAL_RECOVERY_PREFIX)
    assert "capacity" in calls[1]
    # The ladder counted the L1 attempt for this run's unit; the scope is the
    # gateway's, shared by every refused run.
    assert default_ladder().attempts(L1_TOOL_CALL, f"subagent:{info.id}") == 1
    waits = [e for k, e in events if k == "subagent_waiting"]
    assert len(waits) == 1 and waits[0]["state"] == WAITING_DEPENDENCY
    store_events = mgr._admission.taskq_store().events(info.id)
    scopes = {
        ev.data.get("dependency_scope") for ev in store_events if ev.kind == "dependency_wait"
    }
    assert scopes == {"mcp_gateway:capacity"}
    assert coordinator.scopes() == []


@pytest.mark.asyncio
async def test_capacity_refusal_budget_spent_surfaces_the_normal_completion():
    calls: list[str] = []

    def factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            yield _text("x")
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    sessions = _mock_sessions(factory)
    sessions._provider.last_infra_error = InfraError("capacity", retry_after_secs=None)
    mgr = await _ready_manager(sessions)
    _fast_coordinator(mgr)
    # Spend the L1 budget beforehand: the run must not retry, only complete.
    for _ in range(default_ladder().layer_policy(L1_TOOL_CALL).max_attempts):
        default_ladder().observe_failure(L1_TOOL_CALL, "subagent:PENDING")
    with patch.object(SubagentManager, "_yield_for_infra_retry", autospec=True) as fake_retry:
        fake_retry.return_value = None
        info = await _spawn_and_wait(mgr)
    assert info.outcome == "completed"
    assert len(calls) == 1
    assert fake_retry.await_count == 1


# ── 5. typed waiting_input status (M) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_waiting_input_status_releases_lane_slot_and_steers_the_nudge():
    calls: list[str] = []
    status = StructuredStatus(
        phase="waiting",
        wait_reason=WAIT_REASON_INPUT,
        tool_call_id="call-7",
        cancellable=True,
        safe_retry=True,
        origin=STATUS_ORIGIN_LIVENESS_ORACLE,
    )

    def factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                yield _text("partial ")
                yield _status_event(status)
                # Evidence text deliberately carries NO stuck_input marker: the
                # typed status is what must steer the prompt.
                yield _complete(STOP_REASON_TOOL_STALL, "verdict=unknown; idle_secs=5", status)
            else:
                yield _text("done")
                yield _complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = await _ready_manager(_mock_sessions(factory))
    events = _spy_events(mgr)
    info = await _spawn_and_wait(mgr)
    assert info.outcome == "completed"
    waits = [e for k, e in events if k == "subagent_waiting"]
    assert len(waits) == 1
    assert waits[0]["state"] == WAITING_INPUT and waits[0]["resume"] == "input"
    assert "Re-run it non-interactively" in calls[1]
    row_events = mgr._admission.taskq_store().events(info.id)
    wait_ev = next(ev for ev in row_events if ev.kind == "transition" and ev.data.get("wait"))
    assert wait_ev.data["to"] == WAITING_INPUT
    assert mgr._running_count == 0


# ── 6. pump + coordinator wiring ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pump_arms_a_timer_at_the_coordinator_deadline():
    mgr = await _ready_manager(_mock_sessions(lambda *a, **k: None))
    coordinator = _fast_coordinator(mgr, backoff=backoff(0.5, 0.5))
    signal = dep_mod.DependencySignal(
        kind=dep_mod.KIND_RATE_LIMITED, dependency_scope="github:api", source="t"
    )
    # A parked row nobody runs: the coordinator wakes it itself on tick.
    coordinator.report("ghost-task", signal)
    mgr._taskq_pump()
    timer = mgr._taskq_pump_timer
    assert timer is not None and not timer.cancelled()
    loop = asyncio.get_running_loop()
    assert 0.0 < timer.when() - loop.time() <= 0.5 + 0.01


#: The Windows ``monotonic()`` tick, and that platform's loop
#: ``_clock_resolution``: asyncio pops any handle within this of now.
_WINDOWS_CLOCK_RESOLUTION_SECS = 0.015625

#: The resolution the pin below installs: four Windows ticks, deliberately
#: COARSER than the pump's own 0.05s arm floor. The hazard needs a loop wake
#: inside ``(when - resolution, when)``, so widening the resolution past the arm
#: delay makes that window the WHOLE wait rather than its last tick, and every
#: wake after the arm pops the handle early. At Windows' own 15.625 ms the window
#: is only the last tick of those 50 ms and the pop is a lottery on when the loop
#: happens to wake: 1 of 15 runs starved on one busy core never saw it, and a pin
#: that misses its own precondition under load is a flake, not a defect.
_EARLY_FIRE_RESOLUTION_SECS = 4 * _WINDOWS_CLOCK_RESOLUTION_SECS

#: Poll for the pin below, and shorter than that window is what makes it the
#: mechanism rather than a guess at latency -- which is why the file's own "a
#: SIGNAL, never a sleep" rule stops here. An idle loop is exactly what must not
#: happen: the pop is early only while the loop is AWAKE before the handle's
#: ``when``, so a poll longer than the wait leaves the loop asleep until the timer
#: is overdue and the pin goes green having exercised nothing. The precondition
#: assertion below is what keeps that coupling honest.
_EARLY_FIRE_POLL_SECS = 0.01


@pytest.mark.asyncio
async def test_the_ramp_is_woken_when_the_pump_timer_fires_inside_the_clock_resolution(monkeypatch):
    """Every rung after the probe is armed even when the pump's own one-shot
    fired before reaching its ``when``.

    ``BaseEventLoop._run_once`` runs a timer whose ``when`` is within
    ``loop._clock_resolution`` of now -- 15.625 ms where ``monotonic()`` rides
    the system tick (Windows), ~1 ns here -- so the pass re-entering from that
    handle reads ``loop.time() < handle.when()`` for its own spent timer. The
    one-shot is the only tick source between reaper sweeps, so a pass that
    mistakes it for an armed one arms nothing and every waiter behind the probe
    is stranded on a schedule that is due and never ticked.

    The resolution alone does not produce that early fire: the loop also has to
    be awake before the handle's ``when``, which is what
    ``_EARLY_FIRE_RESOLUTION_SECS`` and ``_EARLY_FIRE_POLL_SECS`` buy between
    them. So the pin asserts its own precondition -- that some pass really did
    read a SPENT handle (``_scheduled`` false) as future-dated -- because a pin
    whose defect is reachable only at one poll length goes GREEN the moment
    someone lengthens the poll, and a green that means nothing is worse than no
    pin at all.
    """
    mgr = await _ready_manager(_mock_sessions(lambda *a, **k: None))
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "_clock_resolution", _EARLY_FIRE_RESOLUTION_SECS)
    armed: list[asyncio.TimerHandle] = []
    early: list[float] = []
    real_arm_tick = OrphanStallMonitor.taskq_arm_tick

    def _arm_tick(
        monitor: OrphanStallMonitor, woken_ids: list[str], deadline: float | None
    ) -> None:
        # Read where production reads it, and from a handle list of our own: the
        # hazard is a one-shot asyncio already popped whose ``when`` is still in
        # the future, and production keeps the manager's attribute clear of a
        # spent handle precisely so no pass can see one there.
        #
        # Only a pass that HAD a rung to arm counts, and only a margin inside the
        # delay it wanted: production's dedup is `now < when <= now + delay`, so a
        # spent handle seen on the final `deadline is None` pass, or one further
        # out than this pass would have armed, loses that comparison and strands
        # nothing. Counting those would certify a precondition the defect never
        # needed, which is the same vacuous green this assertion exists to refuse.
        arm_delay = None if deadline is None else max(0.05, deadline - time.time())
        for handle in armed:
            if handle._scheduled or handle.cancelled():
                continue
            margin = handle.when() - loop.time()
            if margin > 0 and arm_delay is not None and margin <= arm_delay:
                early.append(margin)
        real_arm_tick(monitor, woken_ids, deadline)
        pump_timer = mgr._taskq_pump_timer
        if pump_timer is not None and not any(handle is pump_timer for handle in armed):
            armed.append(pump_timer)

    monkeypatch.setattr(OrphanStallMonitor, "taskq_arm_tick", _arm_tick)
    woken: list[str] = []
    # A backoff well inside the pump's own 0.05s arm floor, so the scope comes due
    # while the ramp still needs a rung armed for it: with one wake per tick, the
    # last waiter leaves only on a pass some earlier pass armed.
    coordinator = _fast_coordinator(mgr, backoff=backoff(0.02, 0.02), on_wake=woken.append)
    signal = dep_mod.DependencySignal(
        kind=dep_mod.KIND_RATE_LIMITED, dependency_scope="github:api", source="t"
    )
    coordinator.report("probe-task", signal)
    coordinator.report("ramp-task", signal)
    mgr._taskq_pump()
    deadline = time.monotonic() + 5.0
    while len(woken) < 2 and time.monotonic() < deadline:
        await asyncio.sleep(_EARLY_FIRE_POLL_SECS)
    assert early, (
        "the early fire never happened, so this pin exercised nothing: no pump pass read a "
        f"spent one-shot as future-dated across {len(armed)} armed timer(s), with "
        f"_clock_resolution={_EARLY_FIRE_RESOLUTION_SECS}s and a {_EARLY_FIRE_POLL_SECS}s poll "
        f"(a poll shorter than the wait is the precondition); woke {woken}"
    )
    # The scope can be gone by here (its last waiter left), and a diagnostic that
    # needs the schedule to exist fails as an AttributeError over the assertion
    # it was written to explain.
    retry_at = getattr(coordinator.schedule("github:api"), "retry_at", None)
    due = f"{time.time() - retry_at:.3f}s ago" if retry_at is not None else "on no live schedule"
    assert woken == ["probe-task", "ramp-task"], (
        f"woke {woken} and stopped, with {coordinator.waiters('github:api')} still parked "
        f"on a scope due {due}; pump timer {mgr._taskq_pump_timer!r}; early fire(s) "
        f"{[round(margin, 4) for margin in early]}s before their own when"
    )


def test_manager_builds_its_coordinator_from_config_and_registers_it():
    mgr = _manager(_mock_sessions(lambda *a, **k: None))
    coordinator = mgr._dependency_coordinator()
    assert isinstance(coordinator, DependencyCoordinator)
    assert mgr._dependency_coordinator() is coordinator  # cached
    assert dep_mod.current_coordinator() is coordinator
    assert coordinator._capacity() == mgr._max_concurrent


@pytest.mark.asyncio
async def test_wake_through_only_owns_live_yielded_runs():
    mgr = await _ready_manager(_mock_sessions(lambda *a, **k: None))
    monitor = mgr._monitor
    assert monitor.taskq_wake_through("nobody", 1) is False
    info = SubagentInfo(id="live-1", task="t")
    info._slot_released = True
    event = asyncio.Event()
    info._resume_event = event
    mgr._agents[info.id] = info
    before = mgr._running_count
    assert monitor.taskq_wake_through(info.id, 1) is True
    # Stagger 0 and a free slot: the pump granted the resume at once (and
    # retired the one-shot event).
    assert info._slot_released is False and info._resume_pending is False
    assert mgr._running_count == before + 1 and event.is_set()
    assert info._resume_event is None
    # A run holding its slot is not this seam's business.
    held = SubagentInfo(id="held-1", task="t")
    mgr._agents[held.id] = held
    assert monitor.taskq_wake_through(held.id, 1) is False


@pytest.mark.asyncio
async def test_on_fail_releases_a_run_parked_on_its_resume_event():
    mgr = await _ready_manager(_mock_sessions(lambda *a, **k: None))
    info = SubagentInfo(id="parked-1", task="t")
    info._slot_released = True
    info._resume_event = asyncio.Event()
    mgr._agents[info.id] = info
    mgr._monitor.taskq_on_wait_failed(info.id, "scope gave up")
    assert info._resume_event.is_set() and info._wait_failed == "scope gave up"
    ok = await mgr._await_lane_resume(info, reason="x", timeout=0.05)
    assert ok is False and info._resume_pending is False


# ── 7. main chat: shared cooldown + one ladder constant ──────────────────────


def test_chat_runner_floors_transient_delay_by_the_shared_scope_schedule(monkeypatch):
    from kiro_crew.dashboard import chat_runner

    controller = _FakeController()
    monkeypatch.setattr("kiro_crew.adaptive.controller.current", lambda: controller)
    coordinator = DependencyCoordinator(None, backoff=backoff(5.0, 5.0))
    dep_mod.register_coordinator(coordinator)
    # A server-stated retry_at is honoured exactly (the ladder's equal-jitter
    # backoff would draw in [2.5s, 5s], which would make the floor random).
    server_retry_at = time.time() + 5.0
    coordinator.report(
        "sub-1",
        dep_mod.DependencySignal(
            kind=dep_mod.KIND_RATE_LIMITED,
            dependency_scope="provider:acp",
            source="acp_provider",
            retry_at=server_retry_at,
        ),
    )
    exc = AcpFakeThrottle("ThrottlingException: Rate exceeded")
    delay = chat_runner._shared_dependency_delay(exc, 0.1, slot_key="s")
    assert coordinator.schedule("provider:acp").retry_at == server_retry_at
    assert delay >= (server_retry_at - time.time()) - 0.05 and delay > 1.0
    assert controller.throttles == ["provider:acp"]
    # No schedule for the scope, or an unclassified error: the local delay stands.
    assert chat_runner._shared_dependency_delay(RuntimeError("x"), 0.3, slot_key="s") == 0.3


def test_pipe_death_and_stop_recovery_budgets_share_the_ladder_constant():
    import inspect

    from kiro_crew.dashboard import chat_runner

    src = inspect.getsource(chat_runner)
    for literal in (
        "_acp_pipe_death_retries < 3",
        "_acp_pipe_death_retries >= 3",
        "_acp_pipe_death_retries <= 3",
        "_acp_pipe_death_retries > 3",
    ):
        assert literal not in src, literal
    assert "_acp_pipe_death_retries < SESSION_RECOVERY_MAX_ATTEMPTS" in src
    assert STOP_RECOVERY_MAX_RETRIES is SESSION_RECOVERY_MAX_ATTEMPTS
    assert SESSION_RECOVERY_MAX_ATTEMPTS == 3


# ── 8. gatewayd L2 rung, runtime record_start ────────────────────────────────


@pytest.mark.asyncio
async def test_backend_respawn_is_the_ladder_l2_rung(monkeypatch):
    from kiro_crew.mcp_gateway import gatewayd as gw

    key = SimpleNamespace(server_name="fake-server", human_readable=lambda: "fake")
    args = (
        MagicMock(),
        key,
        MagicMock(),
        "stub-uuid-1234",
        MagicMock(),
        None,
        MagicMock(),
        None,
        None,
    )
    monkeypatch.setattr(gw, "_respawn_backend_for_stub_unrecorded", AsyncMock(return_value=None))
    assert await gw._respawn_backend_for_stub(*args) is None
    assert default_ladder().attempts(L2_BACKEND, "fake-server") == 1
    ok = (MagicMock(), MagicMock(), MagicMock())
    monkeypatch.setattr(gw, "_respawn_backend_for_stub_unrecorded", AsyncMock(return_value=ok))
    assert await gw._respawn_backend_for_stub(*args) == ok
    assert default_ladder().attempts(L2_BACKEND, "fake-server") == 0  # success reset it


def test_session_start_outcomes_reach_the_controller(monkeypatch):
    from kiro_crew.acp import runtime as rt

    controller = _FakeController()
    monkeypatch.setattr("kiro_crew.adaptive.controller.current", lambda: controller)
    t0 = time.monotonic() - 0.2
    rt._record_session_start(t0, ok=True)
    rt._record_session_start(t0, ok=False, attributable_timeout=True)
    rt._record_session_start(t0, ok=False)
    span_ms = (time.monotonic() - t0) * 1000.0
    assert [s["ok"] for s in controller.starts] == [True, False, False]
    assert [s["attributable_timeout"] for s in controller.starts] == [False, True, False]
    assert all(s["key"] == "acp:session/new" for s in controller.starts)
    # Measured from t0 and in MILLISECONDS -- the 200 ms head start dominates the
    # floor, and no sample can outrun the span this test just measured itself.
    # Bounded rather than pinned at ``>= 200``: where ``monotonic()`` advances in
    # ~15.6 ms ticks (Windows through 3.12) both reads fall inside one tick and
    # return the same float, leaving exactly 0.2 s round-tripped through a
    # subtraction -- 199.999... at some uptimes, which is not about the unit.
    assert all(100.0 <= s["duration_ms"] <= span_ms for s in controller.starts)
    monkeypatch.setattr("kiro_crew.adaptive.controller.current", lambda: None)
    rt._record_session_start(t0, ok=True)  # no controller: silent no-op
    assert len(controller.starts) == 3

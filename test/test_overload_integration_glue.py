"""Integration glue pinned by the final overload-resilience pass.

- ``DependencyCoordinator.subscribe`` fans wake / give-up hooks out to the
  runner adapters beside the subagent manager's own hook.
- The gateway attaches ONE ``RunnerAdmission`` to the TaskRunner and the
  WorkflowService, subscribed to the manager's coordinator (or handing its
  ``tick`` to the reaper pump when there is no coordinator).
- That attach is TWO passes over one admission, because the store opens off the
  loop: the second, idempotent one binds the coordinator, re-reads the waiting
  rows and runs the adoption sweep the store-less pass could not.
- ``continue_conversation`` / ``/api/spawn/*`` answer the typed
  ``native_child_not_resumable`` for a harness-native child id.
- ``POST /api/tasks/{id}`` (``answer_input`` / ``cancel_wait``) and the runner
  cancel adapter behind ``POST /api/tasks/{id}/cancel``, including the verdict
  every state gets and the RACE the verdict has to survive: a real admission
  claiming the row between the handler's read and its cancel.
- ``TaskStore.list_rows(lane=)`` is a SQL predicate.
- A resume window entry skips the spawn stagger.
- ``agent.task_store_journal_mode`` reaches ``TaskStore``.
- ``session_health`` mirrors uncharged native children.

The store's off-loop pins are in ``test_overload_integration_glue_store_off_loop.py``, the
manager's spawn path against the durable queue in
``test_overload_integration_glue_spawn_queue.py``, and the shared fixtures in
``overload_integration_glue_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from overload_fakes import Clock, backoff, settle_store_writes
from overload_integration_glue_helpers import (  # noqa: F401 -- fixtures are requested by name
    _Runner,
    clock_fixture,
    no_memory_pressure_fixture,
    store_fixture,
)

from kiro_crew.acp.session_handle import NATIVE_CHILD_NOT_RESUMABLE
from kiro_crew.dashboard import session_health
from kiro_crew.dashboard.handlers import tasks as tasks_mod
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.subagent_manager import admission as admission_mod
from kiro_crew.subagent_manager.continuation import ContinuationCoordinator
from kiro_crew.taskq import dependency as dep_mod
from kiro_crew.taskq import model
from kiro_crew.taskq.adapters import runner as runner_mod
from kiro_crew.taskq.store import TaskStore
from kiro_crew.taskq.waits import WaitRecord
from kiro_crew.testing.wait import await_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# ── coordinator.subscribe ────────────────────────────────────────────────────


def test_coordinator_subscribe_fans_out_wake_and_fail(store: TaskStore, clock: Clock) -> None:
    primary: list[str] = []
    extra: list[str] = []
    failed: list[tuple[str, str]] = []
    coordinator = dep_mod.DependencyCoordinator(
        store,
        clock=clock,
        backoff=backoff(1.0, 1.0),
        max_attempts=1,
        wake_spacing_secs=0.0,
        on_wake=primary.append,
    )
    coordinator.subscribe(on_wake=extra.append, on_fail=lambda t, r: failed.append((t, r)))
    store.accept([model.TaskRecord(id="w-1", kind=model.KIND_WORKFLOW_AGENT, params={})])
    assert store.claim("w-1") is not None
    assert store.transition("w-1", model.STARTING)
    assert store.transition("w-1", model.RUNNING)
    verdict = coordinator.report(
        "w-1",
        dep_mod.DependencySignal(
            kind=dep_mod.KIND_RATE_LIMITED,
            dependency_scope="github:api",
            source="gh",
            retry_at=clock.t + 5.0,
        ),
    )
    assert verdict.outcome == "wait"
    clock.t += 5.5
    woken = coordinator.tick()
    assert woken == ["w-1"]
    assert primary == ["w-1"] and extra == ["w-1"]
    # The probe fails past max_attempts=1: every fail listener hears it.
    verdict = coordinator.report(
        "w-1",
        dep_mod.DependencySignal(
            kind=dep_mod.KIND_RATE_LIMITED, dependency_scope="github:api", source="gh"
        ),
    )
    assert verdict.outcome != "wait"
    assert failed and failed[0][0] == "w-1"


def test_subscribe_listener_errors_do_not_break_the_wake(store: TaskStore, clock: Clock) -> None:
    seen: list[str] = []

    def _boom(_task_id: str) -> None:
        raise RuntimeError("listener broke")

    coordinator = dep_mod.DependencyCoordinator(store, clock=clock, wake_spacing_secs=0.0)
    coordinator.subscribe(on_wake=_boom)
    coordinator.subscribe(on_wake=seen.append)
    store.accept([model.TaskRecord(id="w-2", kind=model.KIND_TASKRUNNER_STEP, params={})])
    assert store.claim("w-2") is not None
    assert store.transition("w-2", model.STARTING)
    assert store.transition("w-2", model.RUNNING)
    coordinator.report(
        "w-2",
        dep_mod.DependencySignal(
            kind=dep_mod.KIND_RATE_LIMITED,
            dependency_scope="s",
            source="x",
            retry_at=clock.t + 1.0,
        ),
    )
    clock.t += 2.0
    assert coordinator.tick() == ["w-2"]
    assert seen == ["w-2"]


class _Mgr:
    """The manager surface the runner wiring uses, in the real ORDER.

    ``monitoring.taskq_coordinator`` answers None for as long as the store is
    still opening off the loop, so a coordinator only exists once ``_taskq``
    does; the gateway's first wiring pass can therefore run without one.
    """

    def __init__(self, coordinator: Any, store: Any) -> None:
        self._coordinator = coordinator
        self._taskq = store
        self.max_concurrent = 3
        self.cap_raise_listener: Any = None

    def dependency_coordinator(self) -> Any:
        return self._coordinator if self._taskq is not None else None

    async def dependency_coordinator_async(self) -> Any:
        return self.dependency_coordinator()

    def set_cap_raise_listener(self, listener: Any) -> None:
        self.cap_raise_listener = listener


def _orch(coordinator: Any, *, store: Any = None) -> GatewayOrchestrator:
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch.subagent_mgr = _Mgr(coordinator, store)
    orch._cfg = SimpleNamespace(agent=SimpleNamespace(adaptive_concurrency_mode="aimd"))
    orch.task_runner = _Runner()
    orch.dashboard_state = SimpleNamespace(workflow_service=_Runner())
    orch._runner_admission = None
    orch._runner_admission_adopted = False
    return orch


def test_gateway_attaches_one_admission_to_both_consumers(store: TaskStore, clock: Clock) -> None:
    coordinator = dep_mod.DependencyCoordinator(store, clock=clock)
    orch = _orch(coordinator, store=store)
    orch._wire_runner_admission()
    adm = orch._runner_admission
    assert isinstance(adm, runner_mod.RunnerAdmission)
    assert orch.task_runner.attached == [adm]
    assert orch.dashboard_state.workflow_service.attached == [adm]
    assert adm.coordinator is coordinator
    assert adm.lane.ceiling == 3
    # The lane's raise edge: the manager's cap is its ceiling, and a waiter
    # parked at cap 0 has no holder whose release would wake it.
    assert orch.subagent_mgr.cap_raise_listener == adm.lane.pump
    # The admission's wake hook is a subscriber of the manager's coordinator.
    assert adm.on_wake in coordinator._wake_listeners
    assert len(coordinator._fail_listeners) == 1
    orch._unwire_runner_admission()
    assert orch._runner_admission is None
    assert orch.task_runner.attached[-1] is None
    assert orch.dashboard_state.workflow_service.attached[-1] is None
    assert orch.subagent_mgr.cap_raise_listener is None


def test_gateway_without_a_coordinator_hands_tick_to_the_reaper_pump() -> None:
    orch = _orch(None)
    orch._wire_runner_admission()
    adm = orch._runner_admission
    assert adm is not None and adm.coordinator is None
    assert orch.subagent_mgr._runner_admission_tick == adm.tick
    assert orch.subagent_mgr.cap_raise_listener == adm.lane.pump
    orch._unwire_runner_admission()
    assert not hasattr(orch.subagent_mgr, "_runner_admission_tick")
    assert orch.subagent_mgr.cap_raise_listener is None


def test_gateway_without_a_manager_wires_nothing() -> None:
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch.subagent_mgr = None
    orch._runner_admission = None
    orch._wire_runner_admission()
    assert orch._runner_admission is None


async def _park_on_a_dependency(
    adm: Any, store: TaskStore, clock: Clock, task_id: str, *, retry_in: float
) -> asyncio.Task[bool]:
    """Admit a step row and park it on a rate-limited scope; return its task."""
    store.accept_one(model.TaskRecord(id=task_id, kind=model.KIND_TASKRUNNER_STEP, params={}))
    handle = await adm.admit(task_id, kind=model.KIND_TASKRUNNER_STEP)
    await handle.running_async()
    signal = dep_mod.DependencySignal(
        kind=dep_mod.KIND_RATE_LIMITED,
        dependency_scope="github:api",
        source="gh",
        retry_at=clock.t + retry_in,
    )
    parked = asyncio.create_task(adm.yield_dependency(handle, signal))
    await settle_store_writes(store)
    assert store.get(task_id).state == model.WAITING_DEPENDENCY
    return parked


@pytest.mark.asyncio
async def test_store_ready_binds_the_coordinator_and_adopts_once(
    store: TaskStore, clock: Clock, no_memory_pressure: None
) -> None:
    coordinator = dep_mod.DependencyCoordinator(store, clock=clock)
    orch = _orch(coordinator, store=None)  # the store is still opening off-loop
    orch._wire_runner_admission()
    adm = orch._runner_admission
    assert adm is not None and adm.coordinator is None
    assert orch.subagent_mgr._runner_admission_tick == adm.tick
    # Readiness is not held up by the open: both consumers already hold the
    # admission, and a request arriving now gets the typed refusal.
    assert orch.task_runner.attached == [adm]
    assert orch.dashboard_state.workflow_service.attached == [adm]
    with pytest.raises(runner_mod.RunnerAdmissionRefused):
        await adm.admit("taskrunner:r0:task1")

    orch.subagent_mgr._taskq = store  # the open worker attached it
    await orch._runner_admission_store_ready()

    assert orch._runner_admission is adm  # repaired, not replaced
    assert adm.coordinator is coordinator
    assert orch.dashboard_state.workflow_service.attached[-1] is adm
    assert adm.on_wake in coordinator._wake_listeners
    assert len(coordinator._fail_listeners) == 1
    assert not hasattr(orch.subagent_mgr, "_runner_admission_tick")
    # The sweep the store-less first pass could not run, run exactly once.
    assert orch.task_runner.attached == [adm, adm]
    await orch._runner_admission_store_ready()
    assert orch.task_runner.attached == [adm, adm]
    assert len(coordinator._wake_listeners) == 1


@pytest.mark.asyncio
async def test_store_ready_changes_nothing_when_the_first_pass_had_a_store(
    store: TaskStore, clock: Clock
) -> None:
    coordinator = dep_mod.DependencyCoordinator(store, clock=clock)
    orch = _orch(coordinator, store=store)
    orch._wire_runner_admission()
    adm = orch._runner_admission
    await orch._runner_admission_store_ready()
    assert adm.coordinator is coordinator
    assert len(coordinator._wake_listeners) == 1 and len(coordinator._fail_listeners) == 1
    assert orch.task_runner.attached == [adm]
    assert orch.dashboard_state.workflow_service.attached == [adm]


@pytest.mark.asyncio
async def test_store_ready_keeps_the_fallback_tick_while_the_queue_stays_off() -> None:
    orch = _orch(None)
    orch._wire_runner_admission()
    adm = orch._runner_admission
    await orch._runner_admission_store_ready()
    assert adm.coordinator is None
    assert orch.subagent_mgr._runner_admission_tick == adm.tick
    assert orch.task_runner.attached == [adm]


@pytest.mark.asyncio
async def test_store_ready_after_a_shutdown_binds_nothing(store: TaskStore, clock: Clock) -> None:
    coordinator = dep_mod.DependencyCoordinator(store, clock=clock)
    orch = _orch(coordinator, store=None)
    orch._wire_runner_admission()
    orch._unwire_runner_admission()
    orch.subagent_mgr._taskq = store
    await orch._runner_admission_store_ready()
    assert orch._runner_admission is None
    assert coordinator._wake_listeners == []
    assert orch.task_runner.attached[-1] is None and len(orch.task_runner.attached) == 2
    assert orch.subagent_mgr.cap_raise_listener is None


@pytest.mark.asyncio
async def test_a_wait_parked_before_the_store_was_ready_is_woken_after_it(
    store: TaskStore, clock: Clock, no_memory_pressure: None
) -> None:
    """The window this whole boundary exists for.

    A step parked through the ledger while the admission had no coordinator has
    exactly one wake path -- the admission's own ``tick`` -- and the reaper pump
    stops calling it the moment the store exists. Binding the coordinator has to
    take the row over, not merely stop scanning it.
    """
    coordinator = dep_mod.DependencyCoordinator(store, clock=clock, wake_spacing_secs=0.0)
    orch = _orch(coordinator, store=None)
    orch._wire_runner_admission()
    adm = orch._runner_admission
    orch.subagent_mgr._taskq = store  # the open landed; no coordinator yet
    assert coordinator.rebuild() == 0  # built before the step parked

    parked = await _park_on_a_dependency(adm, store, clock, "taskrunner:r7:task1", retry_in=5.0)
    clock.t += 6.0
    assert coordinator.tick() == []  # the scope is not in the schedule
    await asyncio.sleep(0)
    assert not parked.done()

    await orch._runner_admission_store_ready()

    assert coordinator.tick() == ["taskrunner:r7:task1"]
    assert await await_until(parked, what="taskrunner task parked on dependency") is True
    assert store.get("taskrunner:r7:task1").state == model.RUNNING


# ── native child refusal ─────────────────────────────────────────────────────


class _Handle:
    def __init__(self, children: set[str], sid: str = "parent-1") -> None:
        self._children = children
        self._sid = sid

    def native_child_resume_refusal(self, conversation_id: str) -> str | None:
        if conversation_id not in self._children:
            return None
        return f"{NATIVE_CHILD_NOT_RESUMABLE}: {conversation_id} is a child of {self._sid}"


def _continuation(sessions: dict[str, Any]) -> ContinuationCoordinator:
    manager = SimpleNamespace(_sessions=SimpleNamespace(_sessions=sessions))
    return ContinuationCoordinator(manager)


def test_native_child_refusal_asks_every_live_handle() -> None:
    sessions = {
        "chat:a": SimpleNamespace(provider=SimpleNamespace(client=_Handle({"kid-1"}))),
        "chat:b": SimpleNamespace(provider=SimpleNamespace(client=object())),
        "chat:c": SimpleNamespace(provider=_Handle({"kid-2"}, sid="p2")),
    }
    cont = _continuation(sessions)
    assert cont.native_child_resume_refusal("kid-1").startswith(NATIVE_CHILD_NOT_RESUMABLE)
    assert "p2" in cont.native_child_resume_refusal("kid-2")
    assert cont.native_child_resume_refusal("nobody") is None


def test_native_child_refusal_without_a_registry_is_none() -> None:
    manager = SimpleNamespace(_sessions=SimpleNamespace())
    assert ContinuationCoordinator(manager).native_child_resume_refusal("x") is None


@pytest.mark.asyncio
async def test_api_spawn_continue_and_steer_answer_409_for_a_native_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard.handlers import messaging

    refusal = f"{NATIVE_CHILD_NOT_RESUMABLE}: kid-1 is a child of parent-1"

    class _Subagents:
        max_concurrent = 4

        def recorded_cwd(self, conv_id: str) -> None:
            return None

        def continue_conversation(self, conv_id: str, task: str, **kw: Any) -> Any:
            return SimpleNamespace(id="n1", done=True, error=refusal)

        async def steer_run(self, agent_id: str, message: str) -> tuple[bool, str]:
            return False, "not_found"

        def native_child_resume_refusal(self, conversation_id: str) -> str | None:
            return refusal if conversation_id == "kid-1" else None

    app = web.Application()
    state = SimpleNamespace(subagents=_Subagents())
    app["state"] = state

    async def _no_refusal(request: Any, *, claimed_session: str = "") -> None:
        return None

    monkeypatch.setattr(messaging, "_spawn_scope_refusal", _no_refusal)
    req = make_mocked_request(
        "POST",
        "/api/spawn/kid-1/continue",
        match_info={"agent_id": "kid-1"},
        app=app,
        payload=None,
    )
    req.json = lambda: _as_future({"task": "go on"})  # type: ignore[method-assign]
    resp = await messaging.api_spawn_continue(req)
    assert resp.status == 409
    assert json.loads(resp.text)["code"] == NATIVE_CHILD_NOT_RESUMABLE

    req2 = make_mocked_request(
        "POST", "/api/spawn/kid-1/steer", match_info={"agent_id": "kid-1"}, app=app
    )
    req2.json = lambda: _as_future({"message": "stop"})  # type: ignore[method-assign]
    resp2 = await messaging.api_spawn_steer(req2)
    assert resp2.status == 409
    assert json.loads(resp2.text)["code"] == NATIVE_CHILD_NOT_RESUMABLE

    req3 = make_mocked_request(
        "POST", "/api/spawn/other/steer", match_info={"agent_id": "other"}, app=app
    )
    req3.json = lambda: _as_future({"message": "stop"})  # type: ignore[method-assign]
    resp3 = await messaging.api_spawn_steer(req3)
    assert resp3.status == 404


def _as_future(value: Any) -> "asyncio.Future[Any]":
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


# ── /api/tasks actions + runner cancel adapter ───────────────────────────────


class _Admission:
    """Records what the route asked for and then answers through the REAL
    admission, so the verdict under test is never a fake's own rule."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.answers: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self._real = runner_mod.RunnerAdmission(store)

    def answer_input(self, task_id: str, answer: str, *, generation: int | None = None) -> bool:
        self.answers.append((task_id, answer))
        return self._real.answer_input(task_id, answer, generation=generation)

    def cancel_wait(self, task_id: str, *, reason: str = "", generation: int | None = None) -> bool:
        self.cancelled.append(task_id)
        return self._real.cancel_wait(task_id, reason=reason, generation=generation)


def _runner_state(store: TaskStore, admission: Any = None) -> SimpleNamespace:
    adm = admission if admission is not None else _Admission(store)
    runner = SimpleNamespace(task_admission=adm, cancel_calls=[])
    runner.cancel = lambda run_id, exact=False: runner.cancel_calls.append((run_id, exact))
    service = SimpleNamespace(_task_admission=adm, cancel_calls=[])

    async def _svc_cancel(run_id: str) -> bool:
        service.cancel_calls.append(run_id)
        return True

    service.cancel = _svc_cancel
    subagents = SimpleNamespace(_taskq=store)

    async def _mgr_cancel(agent_id: str) -> bool:
        return False

    subagents.cancel = _mgr_cancel
    return SimpleNamespace(
        subagents=subagents, task_runner=runner, workflow_service=service, _slots={}
    )


def _req(method: str, path: str, state: Any, *, match: dict, body: Any = None):
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(method, path, match_info=match, app=app)
    if body is not None:
        req.json = lambda: _as_future(body)  # type: ignore[method-assign]
    return req


def _seed_runner_rows(store: TaskStore, clock: Clock) -> None:
    store.accept(
        [
            model.TaskRecord(id="taskrunner:r1:task2", kind=model.KIND_TASKRUNNER_STEP, params={}),
            model.TaskRecord(id="workflow:w1:agent0", kind=model.KIND_WORKFLOW_AGENT, params={}),
            model.TaskRecord(id="taskrunner:r2:task1", kind=model.KIND_TASKRUNNER_STEP, params={}),
        ]
    )
    for tid in ("taskrunner:r1:task2", "workflow:w1:agent0", "taskrunner:r2:task1"):
        assert store.claim(tid) is not None
        assert store.transition(tid, model.STARTING)
        assert store.transition(tid, model.RUNNING)
    inp = WaitRecord.input("call-1", since=clock.t, reason="a prompt")
    assert store.enter_wait("taskrunner:r1:task2", inp.to_dict())


@pytest.mark.asyncio
async def test_answer_input_wakes_a_waiting_runner_row(store: TaskStore, clock: Clock) -> None:
    _seed_runner_rows(store, clock)
    st = _runner_state(store)
    resp = await tasks_mod.api_task_action(
        _req(
            "POST",
            "/api/tasks/taskrunner:r1:task2",
            st,
            match={"task_id": "taskrunner:r1:task2"},
            body={"action": "answer_input", "answer": "yes"},
        )
    )
    assert resp.status == 200, resp.text
    assert st.task_runner.task_admission.answers == [("taskrunner:r1:task2", "yes")]
    # Claimable until the runner's re-admission is granted a slot:
    # the answer is on the wake event, the row is not ``running``.
    assert store.state_of("taskrunner:r1:task2") == model.RETRY_WAIT


@pytest.mark.asyncio
async def test_answer_input_refuses_a_row_not_waiting_for_input(
    store: TaskStore, clock: Clock
) -> None:
    _seed_runner_rows(store, clock)
    st = _runner_state(store)
    resp = await tasks_mod.api_task_action(
        _req(
            "POST",
            "/api/tasks/workflow:w1:agent0",
            st,
            match={"task_id": "workflow:w1:agent0"},
            body={"action": "answer_input", "answer": "yes"},
        )
    )
    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "not_waiting_input"
    empty = await tasks_mod.api_task_action(
        _req(
            "POST",
            "/api/tasks/taskrunner:r1:task2",
            st,
            match={"task_id": "taskrunner:r1:task2"},
            body={"action": "answer_input", "answer": "  "},
        )
    )
    assert empty.status == 400 and json.loads(empty.text)["code"] == "answer_required"
    bad = await tasks_mod.api_task_action(
        _req("POST", "/api/tasks/x", st, match={"task_id": "x"}, body={"action": "nope"})
    )
    assert bad.status == 400 and json.loads(bad.text)["code"] == "bad_action"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["answer_input", "cancel_wait"])
async def test_a_settled_row_reads_the_same_code_whichever_side_of_the_window_it_landed(
    store: TaskStore, clock: Clock, action: str
) -> None:
    """The refusal names the row's state, so losing the race costs no clarity.

    Both arms answer through two paths -- a pre-check over the row this request
    read, and ``_refused_code`` over the row as it stands after a refused write --
    and an operator cannot tell which one served them. So the two must agree for
    every state, or the same row is named differently depending on a timing the
    operator has no way to see. A terminal row is the pair that can differ,
    because only one of the two paths tests TERMINAL first unless both do.
    """
    _seed_runner_rows(store, clock)
    st = _runner_state(store)
    task_id = "taskrunner:r1:task2"
    assert store.cancel(task_id) is not None  # settled BEFORE this request reads it

    resp = await tasks_mod.api_task_action(
        _req(
            "POST",
            f"/api/tasks/{task_id}",
            st,
            match={"task_id": task_id},
            body={"action": action, "answer": "yes"},
        )
    )

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == "terminal"
    # The other path, over the same terminal state, has to say the same word.
    assert tasks_mod._refused_code(action, model.CANCELLED) == "terminal"


@pytest.mark.asyncio
async def test_cancel_wait_ends_the_wait_without_an_answer(store: TaskStore, clock: Clock) -> None:
    _seed_runner_rows(store, clock)
    st = _runner_state(store)
    resp = await tasks_mod.api_task_action(
        _req(
            "POST",
            "/api/tasks/taskrunner:r1:task2",
            st,
            match={"task_id": "taskrunner:r1:task2"},
            body={"action": "cancel_wait"},
        )
    )
    assert resp.status == 200
    assert store.state_of("taskrunner:r1:task2") == model.CANCELLED
    again = await tasks_mod.api_task_action(
        _req(
            "POST",
            "/api/tasks/taskrunner:r1:task2",
            st,
            match={"task_id": "taskrunner:r1:task2"},
            body={"action": "cancel_wait"},
        )
    )
    assert again.status == 409 and json.loads(again.text)["code"] == "terminal"
    # A row still parked in ``admit`` (accepted, never claimed) holds no
    # runtime either, so the wait-level cancel is its lever too.
    store.accept(
        [model.TaskRecord(id="taskrunner:r4:task0", kind=model.KIND_TASKRUNNER_STEP, params={})]
    )
    parked = await tasks_mod.api_task_action(
        _req(
            "POST",
            "/api/tasks/taskrunner:r4:task0",
            st,
            match={"task_id": "taskrunner:r4:task0"},
            body={"action": "cancel_wait"},
        )
    )
    assert parked.status == 200, parked.text
    assert store.state_of("taskrunner:r4:task0") == model.CANCELLED


@pytest.mark.asyncio
async def test_cancel_wait_refuses_a_row_whose_runtime_is_live(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_runner_rows(store, clock)
    store.accept(
        [model.TaskRecord(id="taskrunner:r3:task1", kind=model.KIND_TASKRUNNER_STEP, params={})]
    )
    assert store.claim("taskrunner:r3:task1") is not None
    assert store.transition("taskrunner:r3:task1", model.STARTING)
    st = _runner_state(store)
    # The route is wait-only: for a live row it must reach no store writer,
    # neither directly nor through the admission's wait-level cancel.
    for name in ("cancel", "transition", "finish", "defer", "wake_wait", "enter_wait"):

        def _trip(*a: Any, _n: str = name, **kw: Any) -> Any:
            raise AssertionError(f"handler wrote the store via {_n} for a live row")

        monkeypatch.setattr(store, name, _trip)
    for task_id in ("taskrunner:r2:task1", "workflow:w1:agent0", "taskrunner:r3:task1"):
        resp = await tasks_mod.api_task_action(
            _req(
                "POST",
                f"/api/tasks/{task_id}",
                st,
                match={"task_id": task_id},
                body={"action": "cancel_wait"},
            )
        )
        assert resp.status == 409, resp.text
        assert json.loads(resp.text)["code"] == "not_waiting"
    assert st.task_runner.task_admission.cancelled == []
    # Nor does it reach the owner behind the operator's stop button.
    assert st.task_runner.cancel_calls == [] and st.workflow_service.cancel_calls == []
    assert store.state_of("taskrunner:r2:task1") == model.RUNNING
    assert store.state_of("workflow:w1:agent0") == model.RUNNING
    assert store.state_of("taskrunner:r3:task1") == model.STARTING


def _row_in_state(store: TaskStore, clock: Clock, state: str, task_id: str) -> None:
    """Drive a fresh runner row to *state* over the transition table's own edges."""
    store.accept([model.TaskRecord(id=task_id, kind=model.KIND_TASKRUNNER_STEP, params={})])
    if state == model.QUEUED:
        return
    if state == model.WAITING_INFRA:
        assert store.transition(task_id, model.WAITING_INFRA, next_run_at=clock.t + 1.0)
        return
    assert store.claim(task_id) is not None
    if state == model.ADMITTED:
        return
    assert store.transition(task_id, model.STARTING)
    if state in (model.STARTING, model.RETRY_WAIT, model.RECOVERING):
        if state != model.STARTING:
            assert store.transition(task_id, state, next_run_at=clock.t)
        return
    assert store.transition(task_id, model.RUNNING)
    if state == model.RUNNING:
        return
    if state == model.CANCELLED:
        assert store.cancel(task_id, reason="seeded terminal") is not None
        return
    if state in model.TERMINAL:
        assert store.finish(task_id, state)
        return
    assert store.enter_wait(
        task_id,
        {
            model.WAITING_CHILDREN: WaitRecord.children(["kid-1"], since=clock.t),
            model.WAITING_PERMISSION: WaitRecord.permission("appr-1", since=clock.t),
            model.WAITING_DEPENDENCY: WaitRecord.dependency(
                "github:api", since=clock.t, retry_at=clock.t + 5.0
            ),
            model.WAITING_INPUT: WaitRecord.input("call-1", since=clock.t),
        }[state].to_dict(),
    )


@pytest.mark.asyncio
async def test_every_state_gets_the_route_verdict_its_own_set_names(
    store: TaskStore, clock: Clock
) -> None:
    """``cancel_wait`` over every state: no state falls in none of the three
    sets, every PARKED one still cancels through the real route, and the
    EXECUTING ones are the only non-terminal refusals.

    The store's fence is ``model.PARKED``, so a state added to the table without
    a set lands here rather than in an operator's lost cancel.
    """
    assert model.PARKED | model.EXECUTING | model.TERMINAL == model.STATES
    assert not (model.PARKED & model.EXECUTING)
    assert not (model.PARKED & model.TERMINAL)
    assert not (model.EXECUTING & model.TERMINAL)
    st = _runner_state(store)
    for state in sorted(model.STATES):
        task_id = f"taskrunner:{state}:task0"
        _row_in_state(store, clock, state, task_id)
        assert store.state_of(task_id) == state, f"seed for {state}"
        resp = await tasks_mod.api_task_action(
            _req(
                "POST",
                f"/api/tasks/{task_id}",
                st,
                match={"task_id": task_id},
                body={"action": "cancel_wait"},
            )
        )
        body = json.loads(resp.text)
        if state in model.PARKED:
            assert resp.status == 200, (state, resp.text)
            assert store.state_of(task_id) == model.CANCELLED, state
        elif state in model.EXECUTING:
            assert (resp.status, body["code"]) == (409, "not_waiting"), (state, resp.text)
            assert store.state_of(task_id) == state, state
        else:
            assert (resp.status, body["code"]) == (409, "terminal"), (state, resp.text)
            assert store.state_of(task_id) == state, state


@pytest.mark.asyncio
async def test_cancel_wait_refuses_a_row_a_concurrent_admission_took_live(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's READ authorizes nothing; the store's write is the fence.

    The interleaving is forced, not slept for: the handler's own row read is held
    open at the point where it has judged the row parked, and the REAL
    ``RunnerAdmission.admit`` claims that row and takes it ``running`` before the
    read returns. Cancelling there would bump the generation and fence the live
    worker out of its OWN settlement -- a ``cancelled`` row with work running
    under it that nothing will ever settle -- so the write is refused, the
    operator is told what the row is now, and the live worker's ``done`` lands.
    """
    adm = runner_mod.RunnerAdmission(store, lane=runner_mod.RunnerLane(1), clock=clock)
    rec = adm.accept(kind=model.KIND_TASKRUNNER_STEP, task_id="taskrunner:race:task0")
    assert rec is not None and store.state_of(rec.id) == model.QUEUED
    st = _runner_state(store, admission=adm)
    read_open = threading.Event()
    let_read_return = threading.Event()
    real_get = store.get

    def _gated_get(task_id: str) -> Any:
        row = real_get(task_id)
        if task_id == rec.id and not read_open.is_set():
            read_open.set()
            assert let_read_return.wait(30), "the interleaving never got its turn"
        return row

    monkeypatch.setattr(store, "get", _gated_get)
    action = asyncio.create_task(
        tasks_mod.api_task_action(
            _req(
                "POST",
                f"/api/tasks/{rec.id}",
                st,
                match={"task_id": rec.id},
                body={"action": "cancel_wait"},
            )
        )
    )
    assert await asyncio.to_thread(read_open.wait, 30), "the route never read the row"
    handle = await adm.admit(rec.id)
    assert handle.running() and store.state_of(rec.id) == model.RUNNING
    let_read_return.set()
    resp = await action
    body = json.loads(resp.text)
    # One tuple, so a failure names every fact at once: the verdict, the row the
    # cancel did NOT take, and the live worker's own later settlement -- which a
    # cancel landing here would refuse as ``stale_result``, leaving a
    # ``cancelled`` row with the step still running and nothing left to settle it.
    assert (
        resp.status,
        body.get("code", "ok"),
        store.state_of(rec.id),
        handle.done(),
        store.state_of(rec.id),
    ) == (409, "not_waiting", model.RUNNING, True, model.DONE), resp.text
    assert body["task"]["state"] == model.RUNNING
    assert [e.kind for e in store.events(rec.id)].count("stale_result") == 0
    assert st.task_runner.task_admission is adm


@pytest.mark.asyncio
async def test_cancel_routes_a_starting_row_through_its_owner(store: TaskStore) -> None:
    store.accept(
        [model.TaskRecord(id="taskrunner:r5:task0", kind=model.KIND_TASKRUNNER_STEP, params={})]
    )
    assert store.claim("taskrunner:r5:task0") is not None
    assert store.transition("taskrunner:r5:task0", model.STARTING)
    st = _runner_state(store)
    resp = await tasks_mod.api_task_cancel(
        _req(
            "POST",
            "/api/tasks/taskrunner:r5:task0/cancel",
            st,
            match={"task_id": "taskrunner:r5:task0"},
        )
    )
    assert resp.status == 200, resp.text
    # ``starting`` means the handle reached the step and the runtime is coming
    # up: the run is the lever, not the wait.
    assert st.task_runner.cancel_calls == [("r5", True)]
    assert st.task_runner.task_admission.cancelled == []


@pytest.mark.asyncio
async def test_action_on_a_subagent_row_has_no_input_adapter(
    store: TaskStore, clock: Clock
) -> None:
    store.accept([model.TaskRecord(id="sub-1", kind=model.KIND_SUBAGENT, params={})])
    st = _runner_state(store)
    resp = await tasks_mod.api_task_action(
        _req(
            "POST",
            "/api/tasks/sub-1",
            st,
            match={"task_id": "sub-1"},
            body={"action": "cancel_wait"},
        )
    )
    assert resp.status == 409 and json.loads(resp.text)["code"] == "no_input_adapter"


@pytest.mark.asyncio
async def test_cancel_routes_runner_rows_to_their_owner(store: TaskStore, clock: Clock) -> None:
    _seed_runner_rows(store, clock)
    st = _runner_state(store)
    # A parked (waiting_input) step: ended through cancel_wait, the run untouched.
    resp = await tasks_mod.api_task_cancel(
        _req(
            "POST",
            "/api/tasks/taskrunner:r1:task2/cancel",
            st,
            match={"task_id": "taskrunner:r1:task2"},
        )
    )
    assert resp.status == 200 and json.loads(resp.text)["cancelled"] is True
    assert st.task_runner.task_admission.cancelled == ["taskrunner:r1:task2"]
    assert st.task_runner.cancel_calls == []
    # A RUNNING step: its run is cancelled (exact id); the run settles the row.
    resp = await tasks_mod.api_task_cancel(
        _req(
            "POST",
            "/api/tasks/taskrunner:r2:task1/cancel",
            st,
            match={"task_id": "taskrunner:r2:task1"},
        )
    )
    assert resp.status == 200
    assert st.task_runner.cancel_calls == [("r2", True)]
    # A RUNNING workflow agent call: the workflow run is cancelled.
    resp = await tasks_mod.api_task_cancel(
        _req(
            "POST",
            "/api/tasks/workflow:w1:agent0/cancel",
            st,
            match={"task_id": "workflow:w1:agent0"},
        )
    )
    assert resp.status == 200
    assert st.workflow_service.cancel_calls == ["w1"]


def test_owner_of_parses_runner_ids() -> None:
    assert runner_mod.owner_of("taskrunner:r1") == ("taskrunner", "r1")
    assert runner_mod.owner_of("taskrunner:r1:task3") == ("taskrunner", "r1")
    assert runner_mod.owner_of("taskrunner:r1:task3~2") == ("taskrunner", "r1")
    assert runner_mod.owner_of("workflow:wf_000012:agent4") == ("workflow", "wf_000012")
    assert runner_mod.owner_of("workflow:wf_000012:agent4~1") == ("workflow", "wf_000012")
    assert runner_mod.owner_of("abc123") is None


def test_lane_for_is_the_shared_lane_key_policy() -> None:
    from kiro_crew.taskq import lanes

    assert runner_mod.lane_for("cron:job-1", "chat") == lanes.SYSTEM_LANE
    assert runner_mod.lane_for("_hb", "") == lanes.SYSTEM_LANE
    assert runner_mod.lane_for("web-1", "cron") == lanes.SYSTEM_LANE
    assert runner_mod.lane_for("web-1", "chat") == "web-1"


# ── store: list_rows(lane=) ──────────────────────────────────────────────────


def test_list_rows_lane_is_a_store_predicate(store: TaskStore) -> None:
    store.accept(
        [
            model.TaskRecord(id="a", kind=model.KIND_SUBAGENT, session_key="web-a", params={}),
            model.TaskRecord(id="b", kind=model.KIND_SUBAGENT, session_key="web-b", params={}),
            model.TaskRecord(id="c", kind=model.KIND_CRON, session_key="", params={}),
        ]
    )
    assert [r.id for r in store.list_rows(lane="web-a")] == ["a"]
    assert [r.id for r in store.list_rows(lane="system")] == ["c"]
    assert [r.id for r in store.list_rows(lane="web-a", state=model.QUEUED)] == ["a"]
    assert store.list_rows(lane="nope") == []


# ── admission: a resume skips the stagger ────────────────────────────────────


class _Info:
    def __init__(self, id: str) -> None:
        self.id = id
        self.done = False
        self.reaped = False
        self.user_stopped = False
        self._slot_released = True
        self._resume_pending = True
        self._wait_record = {"reason": "x"}
        self._resume_event: Any = None
        self._taskq_generation = 0
        self.parent_session_key = "web-a"
        self.batch_id = ""


def test_resume_entry_is_granted_before_the_stagger_and_does_not_consume_it() -> None:
    import time as _t

    from kiro_crew.subagent_manager.admission import CapacityView

    info = _Info("r-1")
    mgr = SimpleNamespace(
        _queue=[{"_resume_id": "r-1", "_preassigned_id": "r-1", "reason": "woke"}],
        _agents={"r-1": info},
        _running_count=0,
        _max_concurrent=2,
        _spawn_stagger_secs=2.0,
        _last_spawn_ts=_t.monotonic(),  # a start happened just now
        _drain_queue=lambda: None,
        _emit_queue_depth=lambda *a, **k: None,
    )
    calls: list[str] = []

    class _Glue(admission_mod.SpawnAdmissionCoordinator):
        def taskq_store(self):  # type: ignore[override]
            return None

        def taskq_refill_window(self, *, children_only: bool = False) -> int:  # type: ignore[override]
            return 0

        def capacity_view(self) -> CapacityView:  # type: ignore[override]
            return CapacityView(
                cap_total=2,
                running=mgr._running_count,
                child_reserve=0,
                reserve_active=False,
                waiting_parents=0,
            )

        def _maybe_resume_paused_parent(self, info: Any) -> None:  # type: ignore[override]
            return None

        def pick_window_index(self, view: Any = None) -> int | None:  # type: ignore[override]
            return None

    glue = _Glue.__new__(_Glue)
    glue._manager = mgr
    mgr._admission = glue

    async def _fire_event(etype: str, info: Any, extra: Any = None) -> None:
        calls.append(etype)

    mgr._fire_event = _fire_event
    before = mgr._last_spawn_ts
    glue._drain_queue_impl()
    assert mgr._queue == []
    assert info._slot_released is False and info._resume_pending is False
    assert mgr._running_count == 1
    assert mgr._last_spawn_ts == before  # a resume is not a process start


# ── admission: which row state may refuse a resume, and with which retry ─────

#: One live wait of every kind in ``model.WAITING``, so the sweep below is the
#: whole set rather than a hand-picked sample.
_WAIT_SHAPES = {
    model.WAITING_CHILDREN: lambda now: WaitRecord.children(["kid-1"], since=now),
    model.WAITING_DEPENDENCY: lambda now: WaitRecord.dependency("github:api", since=now),
    model.WAITING_INPUT: lambda now: WaitRecord.input("call-1", since=now),
    model.WAITING_PERMISSION: lambda now: WaitRecord.permission(
        "approval-1", since=now, tool_call_id="call-1"
    ),
}


def _park_a_running_row(store: TaskStore, task_id: str, state: str) -> tuple[Any, int]:
    """Dispatch a row and park it in *state*: ``(wait record, its generation)``.

    The generation is the one ``gate`` hands the run at its claim, and neither
    ``enter_wait`` nor ``update_wait`` bumps it, so it is what the run carries
    for as long as it owns the row. Safe on the writer thread.
    """
    store.accept([model.TaskRecord(id=task_id, kind=model.KIND_SUBAGENT, params={})])
    assert store.claim(task_id) is not None
    assert store.transition(task_id, model.STARTING)
    assert store.transition(task_id, model.RUNNING)
    generation = store.get(task_id).generation
    record = _WAIT_SHAPES[state](store.now())
    assert store.enter_wait(task_id, record.to_dict(), generation=generation) is True
    return record, generation


def _resume_harness(store: TaskStore, info: Any) -> Any:
    """A real admission coordinator over *store* with one free lane slot.

    ``wakes`` records what each ``_resume_publish`` was handed and ``rearmed``
    every ``_rearm_resume`` reason, so a refusal's retry is observable without
    waiting out ``_RESUME_REARM_SECS``.
    """
    wakes: list[Any] = []
    rearmed: list[str] = []
    mgr = SimpleNamespace(
        _queue=[],
        _agents={info.id: info},
        _running_count=0,
        _max_concurrent=2,
        _spawn_stagger_secs=0.0,
        _last_spawn_ts=0.0,
        _drain_queue=lambda: None,
        _emit_queue_depth=lambda *a, **k: None,
    )

    class _Glue(admission_mod.SpawnAdmissionCoordinator):
        def taskq_store(self):  # type: ignore[override]
            return store

        def _resume_publish(self, run: Any, wake: Any) -> bool:  # type: ignore[override]
            wakes.append(wake)
            return super()._resume_publish(run, wake)

        def _rearm_resume(self, run: Any, why: str) -> None:  # type: ignore[override]
            rearmed.append(why)

    glue = _Glue.__new__(_Glue)
    glue._manager = mgr
    mgr._admission = glue

    async def _fire_event(etype: str, run: Any, extra: Any = None) -> None:
        return None

    mgr._fire_event = _fire_event
    return SimpleNamespace(glue=glue, mgr=mgr, wakes=wakes, rearmed=rearmed)


def test_a_resume_wake_lands_on_every_wait_the_run_itself_entered(store: TaskStore) -> None:
    """``_resume_publish``'s row-state refusal cannot fire for a wait the run is
    STILL PARKED IN, which is what makes its ``retry=False`` correct.

    ``wake_wait``'s default fence is ``model.WAITING`` itself, so a resume grant
    carrying the generation the run owns wakes every one of those states to
    ``running`` -- ``waiting_input`` and ``waiting_dependency`` no differently
    from ``waiting_children``. The refusal therefore only ever reads a row that
    moved on; a wait state can reach it only under a generation this run lost
    (``test_a_wait_a_re_dispatch_entered_refuses_the_resume_without_a_retry``).
    The sweep is keyed on ``model.WAITING`` so a fifth wait kind cannot be added
    without answering for it here.
    """
    assert set(_WAIT_SHAPES) == model.WAITING
    for state in sorted(model.WAITING):
        info = _Info(f"sub-{state}")
        info._resume_pending = False
        record, generation = _park_a_running_row(store, info.id, state)
        info._taskq_generation = generation
        info._wait_record = record.to_dict()
        harness = _resume_harness(store, info)

        assert harness.glue.request_resume(info) is True
        entry = harness.mgr._queue.pop(0)
        assert harness.glue.resume_grant(entry) is True, f"{state} refused a resume"

        assert harness.wakes == [(generation + 1, model.RUNNING, True)]
        row = store.get(info.id)
        assert (row.state, row.generation) == (model.RUNNING, generation + 1)
        assert info._slot_released is False and info._resume_pending is False
        assert info._taskq_generation == row.generation and info._wait_record is None
        assert harness.rearmed == [] and harness.mgr._running_count == 1


@pytest.mark.asyncio
async def test_a_wait_a_re_dispatch_entered_refuses_the_resume_without_a_retry(
    store: TaskStore,
) -> None:
    """The one interleaving that reports a WAIT STATE back to
    ``_resume_publish``, and the reason its refusal must not retry.

    The ledger woke the row itself (``retry_wait``), the dispatcher re-claimed
    it, and the new dispatch parked it in a children wait of its own. The state
    the first run's grant reads back is ``waiting_children`` -- but at a
    generation that run never held, so the wait is the RE-DISPATCH's. Re-arming
    would ask the pump for a lane slot on another owner's row every second until
    the waiter's own bound gave up; the reservation goes back and the run's
    bounded waiter (``_await_lane_resume``) is what ends it.
    """
    info = _Info("sub-redispatch")
    info._resume_pending = False
    record, generation = await store.run(
        _park_a_running_row, store, info.id, model.WAITING_CHILDREN
    )
    info._taskq_generation = generation
    info._wait_record = record.to_dict()
    fresh = await store.run(_re_dispatch_into_a_new_wait, store, info.id, generation)
    assert fresh > generation

    harness = _resume_harness(store, info)
    assert harness.glue.request_resume(info) is True
    entry = harness.mgr._queue.pop(0)
    assert harness.glue.resume_reserve(entry) is True
    assert await harness.glue.resume_grant_async(entry) is False

    # A wait state, and the refusal read it as one: the generation is the whole
    # difference between this and the sweep above.
    assert harness.wakes == [(None, model.WAITING_CHILDREN, True)]
    assert harness.rearmed == [], "another dispatch's wait must never be re-armed"
    # The run stays parked exactly as it was, and the reservation went back.
    assert info._slot_released is True and info._resume_pending is False
    assert info._wait_record == record.to_dict()
    assert harness.mgr._running_count == 0
    row = await store.run(store.get, info.id)
    assert (row.state, row.generation) == (model.WAITING_CHILDREN, fresh)
    assert row.generation > info._taskq_generation

    # Positive control for the same seam: the refusal that DOES retry records
    # one, so the empty ``rearmed`` above is the code's answer, not a dead hook.
    assert harness.glue._resume_publish(info, (None, None, False)) is False
    assert harness.rearmed == ["the task store could not be reached"]


def _re_dispatch_into_a_new_wait(store: TaskStore, task_id: str, generation: int) -> int:
    """Wake *task_id*'s row to ``retry_wait``, re-claim it, and park the new
    dispatch in a children wait of its own; returns its generation.

    Safe on the writer thread.
    """
    assert store.wake_wait(task_id, reason="last awaited child done", generation=generation)
    assert store.claim(task_id) is not None
    assert store.transition(task_id, model.STARTING)
    assert store.transition(task_id, model.RUNNING)
    fresh = store.get(task_id).generation
    record = WaitRecord.children(["kid-2"], since=store.now())
    assert store.enter_wait(task_id, record.to_dict(), generation=fresh) is True
    return fresh


# ── Stop all: a resident resumed run is not a queued spawn ───────────────────


async def _resume_stop_manager() -> Any:
    """A real manager on a real store, one lane slot, no stagger."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx, max_concurrent=1)
    await mgr.wait_taskq_ready()
    mgr._spawn_stagger_secs = 0.0
    mgr._last_spawn_ts = 0.0
    assert mgr._admission.taskq_store() is not None
    return mgr


@pytest.mark.asyncio
async def test_stop_all_reaps_a_resident_resumed_run_it_never_treats_as_queued() -> None:
    """Stop all, against a run that yielded its lane slot and is waiting for it
    back: the ``_resume_id`` entry in ``_queue`` names a RESIDENT run, carries
    that run's own ``_preassigned_id`` and its parent key, and so matches both
    terms of the parent sweep's queued scan.

    Three consequences are pinned together because the queued-stop path breaks
    all three at once: the real ``cancel``/reap runs (``reaped``, which no
    synthetic terminal sets), the ``_agents`` record is still the SAME object
    the run holds (a synthetic ``queued=True`` replacement is what
    ``resume_grant`` would later refuse to hand the slot back to), and the
    coroutine is actually stopped rather than left executing behind a "Queued
    stop" the parent was told about.
    """
    from unittest.mock import patch

    from kiro_crew.subagent import SubagentManager

    mgr = await _resume_stop_manager()
    store = mgr._admission.taskq_store()

    async def _park_forever(self: Any, info: Any) -> None:
        await asyncio.sleep(3600)

    try:
        with patch.object(SubagentManager, "_run", new=_park_forever):
            resident = mgr.spawn("resident", parent_session_key="dash:stop-all")
            sibling = mgr.spawn("sibling", parent_session_key="dash:stop-all")
            await asyncio.sleep(0.05)
            assert sibling.queued is True, "max_concurrent=1: the sibling waits"
            # The run's first frame marks the row running; then it blocks on its
            # child and yields the LANE slot, which the sibling drains into.
            await store.run(store.transition, resident.id, model.RUNNING)
            assert mgr._admission.yield_slot(
                resident, WaitRecord.children([sibling.id], since=store.now())
            )
            await asyncio.sleep(0.05)
            # The drain re-enters ``spawn`` under the same preassigned id, so the
            # started sibling is the record in ``_agents``, not the queued handle.
            assert sibling.id in mgr._tasks and mgr._running_count == 1
            # The wake condition is met but the slot is taken, so the resume
            # entry stays in the window: the state "Stop all" walks into.
            assert mgr._admission.request_resume(resident) is True
            await asyncio.sleep(0.05)
            assert [p.get("_resume_id") for p in mgr._queue] == [resident.id]
            assert mgr._agents[resident.id] is resident
            assert resident.queued is False and resident.done is False
            assert (await store.run(store.get, resident.id)).state == model.WAITING_CHILDREN

            running_stopped, queued_stopped = await mgr.cancel_for_parent("dash:stop-all")
            await asyncio.sleep(0.05)

        # The real reap ran on the real record: `reaped` is the reap's own
        # marker, and identity is what a synthetic queued terminal destroys.
        assert mgr._agents[resident.id] is resident
        assert resident.reaped is True and resident.done is True
        assert resident.queued is False and resident.user_stopped is True
        # And the coroutine is gone, not still executing under a stopped card.
        assert resident.id not in mgr._tasks
        # Counted as the running run it is, never as work that never started.
        assert (running_stopped, queued_stopped) == (2, 0)
    finally:
        await mgr.cancel_all()


@pytest.mark.asyncio
async def test_cancel_never_reports_a_queued_stop_over_a_run_that_already_ended() -> None:
    """The other door into the same misclassification: ``cancel(id)`` for a run
    whose own terminal already landed while its resume entry is still in the
    window (nothing on the terminal path withdraws one).

    ``cancel`` falls through to ``_unqueue`` for a record that is ``done``, so an
    id match alone would hand that resume entry to the queued-stop path and
    replace an already-reported record — its result, cost and elapsed — with a
    synthetic "(stopped before start)" one, plus a SECOND terminal to the parent.
    A run that has already ended is not cancellable: the answer is False.
    """
    from kiro_crew.subagent import SubagentInfo

    mgr = await _resume_stop_manager()
    try:
        info = SubagentInfo(id="lingering", task="real work", parent_session_key="dash:one")
        info._slot_released = True
        mgr._agents[info.id] = info
        mgr._running_count = 1  # the one lane slot is taken: no grant, no withdraw
        assert mgr._admission.request_resume(info) is True
        await asyncio.sleep(0.05)
        assert [p.get("_resume_id") for p in mgr._queue] == [info.id]
        # Its own terminal lands, result and all.
        info.done = True
        info.reaped = True
        info.result = "the real result"
        reported: list[str] = []
        mgr._spawn_terminal_report = lambda i, **kw: reported.append(i.id)  # type: ignore[assignment]

        assert await mgr.cancel(info.id) is False
        assert mgr._agents[info.id] is info and info.result == "the real result"
        assert reported == [], "a finished run must not be reported stopped a second time"
    finally:
        await mgr.cancel_all()


@pytest.mark.asyncio
async def test_stop_all_never_takes_the_queued_stop_path_for_a_claimable_resident_row() -> None:
    """A resident run's ROW is claimable for a moment while its resume entry
    waits: ``wake_wait``'s default lands every wake that still has to reacquire
    a lane in ``retry_wait``, which is a state ``_resume_publish`` refuses a
    grant for (and, the row being claimable by then, refuses without a retry).

    ``taskq_cancel_queued`` accepts a claimable row, so handing it this id
    cancels a LIVE run's row durably AND returns params, which puts the
    queued-stop path back in business behind the window scan that skips the
    entry itself. The id must not reach ``_unqueue`` at all.
    """
    from unittest.mock import patch

    from kiro_crew.subagent import SubagentManager

    mgr = await _resume_stop_manager()
    store = mgr._admission.taskq_store()

    async def _park_forever(self: Any, info: Any) -> None:
        await asyncio.sleep(3600)

    reported: list[str] = []
    mgr._report_queued_stop = lambda params, **_kw: reported.append(  # type: ignore[assignment]
        str(params.get("_preassigned_id") or "")
    )
    try:
        with patch.object(SubagentManager, "_run", new=_park_forever):
            resident = mgr.spawn("resident", parent_session_key="dash:claimable")
            sibling = mgr.spawn("sibling", parent_session_key="dash:claimable")
            await asyncio.sleep(0.05)
            await store.run(store.transition, resident.id, model.RUNNING)
            assert mgr._admission.yield_slot(
                resident, WaitRecord.children([sibling.id], since=store.now())
            )
            await asyncio.sleep(0.05)
            assert mgr._admission.request_resume(resident) is True
            await asyncio.sleep(0.05)
            # The wake landed on the row before the pump could grant the slot.
            assert await store.run(store.wake_wait, resident.id, reason="child ended") is not None
            assert (await store.run(store.get, resident.id)).state == model.RETRY_WAIT

            await mgr.cancel_for_parent("dash:claimable")
            await asyncio.sleep(0.05)

        assert reported == [], "a resident run is never reported stopped before start"
        assert mgr._agents[resident.id] is resident
        assert resident.reaped is True and resident.queued is False
    finally:
        await mgr.cancel_all()


# ── config: task_store_journal_mode ──────────────────────────────────────────


def test_journal_mode_key_reaches_the_store(tmp_path: Path) -> None:
    from kiro_crew import taskq

    home = tmp_path / "home"
    (home / "tasks").mkdir(parents=True)
    s = taskq.open_default_store(home, journal_mode="delete", import_legacy_records=False)
    try:
        assert s.journal_mode == "delete"
        assert any("task_store_journal_mode" in w for w in s.warnings)
    finally:
        s.close()
    s2 = taskq.open_default_store(home, journal_mode="wal", import_legacy_records=False)
    try:
        assert s2.journal_mode == "wal"
        assert s2.warnings == []
    finally:
        s2.close()


def test_journal_mode_config_key_parses_and_clamps(tmp_path: Path) -> None:
    import unittest.mock

    from kiro_crew.config.loader import KiroCrewConfig

    def _loaded(data: dict) -> KiroCrewConfig:
        (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
        with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            return KiroCrewConfig.load()

    assert (
        _loaded({"agent": {"task_store_journal_mode": "DELETE"}}).agent.task_store_journal_mode
        == "delete"
    )
    assert (
        _loaded({"agent": {"task_store_journal_mode": "bogus"}}).agent.task_store_journal_mode
        == "auto"
    )
    assert _loaded({}).agent.task_store_journal_mode == "auto"


# ── session_health: uncharged native children ────────────────────────────────


def test_session_health_mirrors_uncharged_native_children() -> None:
    mirror = session_health.uncharged_mirror()
    mirror.report_uncharged("native_children", 0, label="s-old")

    class _H:
        def report_native_children(self, budget: Any) -> int:
            budget.report_uncharged("native_children", 3, label="s-1")
            return 3

    slot = SimpleNamespace(key="web-1", running=True, _acp_client=_H(), messages=[])
    snap = session_health.snapshot_slot(slot)
    assert snap.native_children == 3
    assert mirror.uncharged("native_children") >= 3
    monitor = session_health.SessionHealthMonitor(include_log_scan=False)
    payload = monitor.compute(session_health.HealthSnapshot(slots=[snap]))
    assert payload["uncharged"]["native_children"] >= 3
    assert payload["slots"]["web-1"]["native_children"] == 3
    mirror.report_uncharged("native_children", 0, label="s-1")

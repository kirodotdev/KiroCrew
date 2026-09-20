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
- The store is never read on the loop: the lanes endpoint, the wave sweeps and
  the first dependency coordinator, each pinned by ``loop_thread_calls`` as well
  as by the armed guard, because a caller inside ``except Exception`` swallows
  the guard's raise.
- A closed admission neither refills the window nor grants a resume, and both
  land once it reopens.
- A resume window entry skips the spawn stagger.
- ``agent.task_store_journal_mode`` reaches ``TaskStore``.
- ``session_health`` mirrors uncharged native children.
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

from kiro_crew.acp.session_handle import NATIVE_CHILD_NOT_RESUMABLE
from kiro_crew.dashboard import session_health
from kiro_crew.dashboard.handlers import tasks as tasks_mod
from kiro_crew.on_loop_db import OnLoopStoreError
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.subagent_manager import admission as admission_mod
from kiro_crew.subagent_manager.continuation import ContinuationCoordinator
from kiro_crew.taskq import dependency as dep_mod
from kiro_crew.taskq import model
from kiro_crew.taskq import store as store_mod
from kiro_crew.taskq.adapters import runner as runner_mod
from kiro_crew.taskq.store import TaskStore
from kiro_crew.taskq.waits import WaitRecord

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock):
    s = TaskStore(tmp_path / "tasks.db", clock=clock).open()
    yield s
    s.close()


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


# ── gateway wiring ───────────────────────────────────────────────────────────


class _Runner:
    def __init__(self) -> None:
        self.attached: list[Any] = []

    def attach_task_admission(self, adm: Any) -> None:
        self.attached.append(adm)


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


# ── gateway wiring: the store-ready boundary ─────────────────────────────────


@pytest.fixture
def no_memory_pressure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the probe ``runner_admission_for`` reads for itself.

    ``healthy_host_memory`` pins the copy ``subagent`` imported; the runner
    admission imports its own from ``resource_status``, so without this a
    loaded machine turns an admission into a defer.
    """
    from kiro_crew import resource_status

    monkeypatch.setattr(
        resource_status,
        "cached_admission_check",
        lambda: SimpleNamespace(admitted=True, reason=""),
    )


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
    assert await asyncio.wait_for(parked, timeout=5) is True
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
    mgr._report_queued_stop = lambda params: reported.append(  # type: ignore[assignment]
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


# ── store off-loop: the store never blocks the event loop ────────────────────


class TestStoreOffLoop:
    @pytest.mark.asyncio
    async def test_run_executes_on_the_writer_thread(self, store: TaskStore) -> None:
        import threading

        seen: dict[str, Any] = {}

        def _probe() -> int:
            seen["thread"] = threading.current_thread().name
            seen["loop_running"] = TaskStore._on_running_loop_thread()
            return store.count_pending(model.KIND_SUBAGENT)

        assert await store.run(_probe) == 0
        assert seen["thread"].startswith("taskq-writer")
        assert seen["loop_running"] is False
        assert store.loop_thread_calls == 0

    @pytest.mark.asyncio
    async def test_run_does_not_take_the_connection_lock_on_the_loop(
        self, store: TaskStore
    ) -> None:
        """``run`` reaches its executor without ``_lock``, so the seam every
        loop caller is told to use cannot itself freeze the loop.

        ``_lock`` is held by the writer thread across ``BEGIN IMMEDIATE``'s busy
        wait -- exactly the wait ``run`` exists to move off the loop -- so a
        ``run`` that took it to fetch its executor handed the loop that wait
        anyway, and invisibly: the take is not a :meth:`TaskStore._c`, so
        ``loop_thread_calls`` shows nothing.

        Pinned by WHO released the holder, not by a wall clock: the holder is
        released by a coroutine scheduled before the ``run``, so a loop that
        kept control releases it at once and a frozen loop leaves it to time
        out on its own. The timeout is the failure's cost, never the pin.
        """
        import threading

        holding = threading.Event()
        release = threading.Event()
        released_by_the_loop: list[bool] = []

        def _hold() -> None:
            with store._lock:
                holding.set()
                released_by_the_loop.append(release.wait(3.0))

        holder = threading.Thread(target=_hold, name="lock-holder", daemon=True)
        holder.start()
        try:
            await asyncio.get_running_loop().run_in_executor(None, holding.wait, 5.0)

            async def _unblock() -> None:
                await asyncio.sleep(0)
                release.set()

            asyncio.get_running_loop().create_task(_unblock())
            await store.run(lambda: None)
        finally:
            release.set()
            holder.join(5.0)
        assert released_by_the_loop == [True], "the loop was frozen behind the connection lock"

    @pytest.mark.asyncio
    async def test_strict_guard_refuses_a_direct_call_on_the_loop(
        self, store: TaskStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control for the guard the async paths are pinned against."""
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        with pytest.raises(OnLoopStoreError, match="taken on the event loop"):
            store.count_pending(model.KIND_SUBAGENT)
        # The same call through run() is fine.
        assert await store.run(store.count_pending, model.KIND_SUBAGENT) == 0

    @pytest.mark.asyncio
    async def test_spawn_async_writes_the_row_off_loop_before_starting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``/api/spawn`` -> ``spawn_async``: the accept (``BEGIN IMMEDIATE``) runs
        on the writer thread, the row exists BEFORE the run starts, and the
        started run carries the id the row was accepted under."""
        import threading
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        provider = AsyncMock()
        provider.stream = MagicMock(side_effect=lambda *a, **k: iter(()))
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_approval_policy = MagicMock(return_value="auto")
        sessions.has_session = MagicMock(return_value=True)
        ctx = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True
        ctx.hooks.auto_approve_subagent_tools = False
        mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
        await mgr.wait_taskq_ready()
        mgr._should_use_session_sharing = MagicMock(return_value=False)
        mgr._spawn_stagger_secs = 0.0
        real_store = mgr._admission.taskq_store()
        assert real_store is not None

        accepts: list[dict[str, Any]] = []
        original_accept = real_store.accept_one

        def _spy(record: Any) -> str:
            accepts.append(
                {
                    "id": record.id,
                    "thread": threading.current_thread().name,
                    "loop_running": TaskStore._on_running_loop_thread(),
                    "started_before_accept": record.id in mgr._agents,
                }
            )
            return original_accept(record)

        monkeypatch.setattr(real_store, "accept_one", _spy)
        # End to end through the ``/api/spawn`` glue, with the strict guard
        # ARMED: the accept, the window decision, the claim and the
        # registration writes all run on the writer thread; a store call on
        # the loop raises here instead of passing silently.
        from kiro_crew.dashboard.handlers import messaging

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        before = real_store.loop_thread_calls
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        state = SimpleNamespace(subagents=mgr)
        info = await messaging._spawn_on_loop(state, "hello", parent_session_key="web-1", agent="")
        for _ in range(10):
            await asyncio.sleep(0.01)  # let the posted registration writes land
        monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
        # (The fake session makes the run itself fail right after it starts;
        # what this pins is the accept path, not the run.)
        assert info is not None and info.id and not info.queued
        assert accepts and accepts[0]["id"] == info.id
        assert accepts[0]["thread"].startswith("taskq-writer")
        assert accepts[0]["loop_running"] is False
        assert accepts[0]["started_before_accept"] is False
        assert real_store.loop_thread_calls == before  # the test's own reads come after
        assert real_store.get(info.id) is not None
        await mgr.cancel_all()

    @staticmethod
    async def _manager_with_store(monkeypatch: pytest.MonkeyPatch) -> Any:
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        provider = AsyncMock()
        provider.stream = MagicMock(side_effect=lambda *a, **k: iter(()))
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_approval_policy = MagicMock(return_value="auto")
        sessions.has_session = MagicMock(return_value=True)
        ctx = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True
        ctx.hooks.auto_approve_subagent_tools = False
        mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
        await mgr.wait_taskq_ready()
        mgr._should_use_session_sharing = MagicMock(return_value=False)
        mgr._spawn_stagger_secs = 0.0
        assert mgr._admission.taskq_store() is not None
        return mgr

    @pytest.mark.asyncio
    async def test_drain_refill_reads_the_store_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pump on a running loop: every store read of the window refill and
        the wait-expiry sweep happens on the writer thread. Pinned with the
        strict guard armed, so a read that slipped back onto the loop raises."""
        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        # Two rows waiting on disk: the pump refills the window
        # (pending_lanes / fetch_dispatchable_fair) off-loop, then picks.
        for i in range(2):
            rec = mgr._admission.taskq_build_record(
                f"row{i}",
                {"task": f"t{i}", "parent_session_key": "web-1"},
                parent_session_key="web-1",
                memory_store="",
                app="",
                model="",
                allowed_tools=None,
                approval_mode=None,
            )
            assert mgr._admission.taskq_accept_record(rec) is None
        # Nothing is stubbed: the pump's own reads (expiry sweep, pending_lanes,
        # fetch), the dispatch (``store.claim`` via ``ClaimPoint``), the
        # registration writes (``taskq_mark``), the run's terminal write
        # (``taskq_settle``) and the queue-depth chip's ``count_pending`` all
        # run under the strict guard, which raises on any loop-thread call.
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        before = store.loop_thread_calls
        mgr._drain_queue()
        task = getattr(mgr, "_drain_task", None)
        assert task is not None, "on a running loop with a store the pump is a coroutine"
        await task
        for _ in range(20):  # let the started runs finish and settle
            await asyncio.sleep(0.01)
        await mgr._drain_queue_async()
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert store.loop_thread_calls == before
        monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
        states = {rid: store.state_of(rid) for rid in ("row0", "row1")}
        # Both rows were claimed and started (the fake session makes the run
        # fail, which is itself a settle through the writer thread).
        assert all(st not in (model.QUEUED, model.ADMITTED) for st in states.values()), states
        assert any(st in model.TERMINAL for st in states.values()), states
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_pick_resolves_a_lane_off_loop_not_on_the_parent_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The drain's LANE question is answered off the loop.

        A window entry that carries no ``_lane`` resolves its parent chain, and a
        parent with no live run is read from the store -- which the pick, a
        coroutine, would otherwise do on the loop. The entry here is the shape
        that forces it: a nested key whose parent exists only as a row.

        Counted, not raised: the pump body is wrapped in ``except Exception``,
        so the strict guard's raise would be swallowed and the pass would look
        green over the freeze.
        """
        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        assert store is not None
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        parent = mgr._admission.taskq_build_record(
            "ghostparent",
            {"task": "a parent this process never ran", "parent_session_key": "web-7"},
            parent_session_key="web-7",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(parent) is None
        mgr._agents.pop("ghostparent", None)
        # A nested window entry with NO ``_lane``: the pick has to resolve
        # ``subagent:ghostparent`` to its root's lane, and the only place that
        # answer lives is the parent's row.
        mgr._queue.clear()
        mgr._queue.append(
            {"task": "nested, lane unresolved", "parent_session_key": "subagent:ghostparent"}
        )
        before = store.loop_thread_calls
        resolved = await mgr._admission.resolve_window_lanes_async()
        assert resolved == {"subagent:ghostparent": "web-7"}, resolved
        await mgr._drain_queue_async()
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert store.loop_thread_calls == before
        await mgr.cancel_all()

    @staticmethod
    def _critical() -> Any:
        from kiro_crew import resource_status as rs

        return rs.AdmissionDecision(
            admitted=False,
            posture=rs.POSTURE_CRITICAL,
            available_gb=1.2,
            reason="host memory is critical (~1.2 GB free, critical ≤ 2 GB)",
        )

    @pytest.mark.asyncio
    async def test_the_drained_defer_writes_off_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A drained row the pressure gate parks: the ``store.defer`` runs on the
        writer thread, and its BOOLEAN still decides the answer.

        Counted, not raised: this write sits inside the pump's
        ``except Exception``, which swallows the armed guard's ``OnLoopStoreError``
        and would report a green pass over a two-second freeze.
        """
        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        assert store is not None
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        rec = mgr._admission.taskq_build_record(
            "held0",
            {"task": "parked under pressure", "parent_session_key": "web-1"},
            parent_session_key="web-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(rec) is None
        monkeypatch.setattr(
            "kiro_crew.subagent.cached_admission_check", lambda *a, **k: self._critical()
        )
        before = store.loop_thread_calls
        await mgr._drain_queue_async()
        await settle_store_writes(store)
        # The row is parked, not refused: the defer landed and moved the row's
        # eligibility out, which is what the boolean reported. Read through
        # ``run``, so these checks leave the counter below where the pump left it.
        row = await store.run(store.get, "held0")
        assert row is not None and row.state == model.QUEUED, row
        assert row.next_run_at > store.now(), row.next_run_at
        kinds = [e.kind for e in await store.run(store.events, "held0")]
        assert kinds.count("deferred") == 1, kinds
        assert store.loop_thread_calls == before
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_drained_defer_refuses_a_legacy_window_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A window entry with no row behind it is REFUSED, never parked.

        ``_from_queue`` does not prove a row exists, so the defer's boolean is
        what separates the two; a parked handle for a row nothing will ever
        dispatch is a spawn the requester never hears about again. Off-loop for
        this answer too -- a missing row still takes ``BEGIN IMMEDIATE``.
        """
        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        assert store is not None
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        announced: list[Any] = []

        async def _collect(info: Any) -> None:
            announced.append(info)

        mgr._on_done = _collect
        mgr._queue = [
            {
                "task": "no row behind it",
                "parent_session_key": "web-1",
                "_preassigned_id": "ghost0",
            }
        ]
        monkeypatch.setattr(
            "kiro_crew.subagent.cached_admission_check", lambda *a, **k: self._critical()
        )
        before = store.loop_thread_calls
        await mgr._drain_queue_async()
        await settle_store_writes(store)
        for _ in range(5):
            await asyncio.sleep(0)
        assert await store.run(store.get, "ghost0") is None
        assert [i.id for i in announced] == ["ghost0"], announced
        assert announced[0].done is True and not announced[0].queued
        assert "critical" in (announced[0].error or "")
        # Last, and through ``run`` above, so the only thing this can catch is
        # the defer sliding back onto the loop.
        assert store.loop_thread_calls == before
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_continuation_and_app_spawn_accept_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two remaining accept paths -- ``continue_conversation_async`` and
        the app ``SpawnSDK`` -- write their row through ``spawn_async``."""
        import threading

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        threads: list[str] = []
        original_accept = store.accept_one

        def _spy(record: Any) -> str:
            threads.append(threading.current_thread().name)
            return original_accept(record)

        monkeypatch.setattr(store, "accept_one", _spy)
        # continuation: the prelude's own checks pass for an unknown conversation
        # only through the manager's recorded state; stub it to the spawn step.
        monkeypatch.setattr(
            mgr,
            "_continue_prelude",
            lambda *a, **k: {"task": "again", "parent_session_key": "web-1"},
        )
        info = await mgr.continue_conversation_async("conv-1", "again", parent_session_key="web-1")
        assert info is not None and not info.error, info
        # app SpawnSDK
        from kiro_crew.apps import spawn_sdk

        class _Agent:
            name = "demo--worker"
            filename = "demo--worker.json"

        monkeypatch.setattr(spawn_sdk, "list_agents", lambda: [_Agent()])
        impl = spawn_sdk.build_spawn_impl(mgr)
        agent_id = await impl("do it", "demo--worker", True, "", "demo")
        assert agent_id
        assert threads and all(t.startswith("taskq-writer") for t in threads), threads
        assert len(threads) == 2
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_channel_keyword_spawn_door_accepts_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The FOURTH accept path: ``spawn <task>`` typed into a channel.

        Slack's ``_handle_spawn_command`` and Telegram's ``spawn_task_reply``
        both run inside an async channel handler, so the ``BEGIN IMMEDIATE`` the
        accept takes must land on the writer thread. Nothing is stubbed and the
        strict guard is armed, so a spawn that fell back to the synchronous
        ``spawn`` raises here.
        """
        import threading

        from kiro_crew.messaging import commands as messaging_commands
        from kiro_crew.slack import handler as slack_handler

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        threads: list[str] = []
        original_accept = store.accept_one

        def _spy(record: Any) -> str:
            threads.append(threading.current_thread().name)
            return original_accept(record)

        monkeypatch.setattr(store, "accept_one", _spy)
        before = store.loop_thread_calls
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        try:
            slack_reply = await slack_handler._handle_spawn_command(
                "spawn index the corpus", mgr, "slack:C1:1"
            )
            telegram_reply = await messaging_commands.spawn_task_reply(
                "index it again", mgr, "telegram:kirocrew:direct:7"
            )
            for _ in range(10):
                await asyncio.sleep(0.01)  # let the posted registration writes land
        finally:
            monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)

        assert slack_reply and "Spawned subagent" in slack_reply
        assert telegram_reply and "Spawned subagent" in telegram_reply
        assert len(threads) == 2 and all(t.startswith("taskq-writer") for t in threads), threads
        assert store.loop_thread_calls == before  # the reads below are the test's own
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_queue_depth_and_parent_cancel_read_the_store_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The queue-depth reads made from coroutines: the cron reset-deferral
        guards (``queued_count_for`` / ``has_pending_work_for``), the chat
        slot-mode gate, and ``cancel_for_parent``'s pending-id read. Each one
        counts rows that live only on disk, so each has to ask the store, and
        under the armed guard it has to ask it off the loop.
        """
        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        for i in range(2):
            rec = mgr._admission.taskq_build_record(
                f"depth{i}",
                {"task": f"t{i}", "parent_session_key": "cron:j1:planner"},
                parent_session_key="cron:j1:planner",
                memory_store="",
                app="",
                model="",
                allowed_tools=None,
                approval_mode=None,
            )
            assert mgr._admission.taskq_accept_record(rec) is None

        before = store.loop_thread_calls
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        try:
            assert await mgr.queued_count_for_async("cron:j1:planner") == 2
            assert await mgr.has_pending_work_for_async("cron:j1:planner") is True
            assert await mgr.has_pending_work_for_async("cron:j1:idle") is False
            pending = await mgr._admission.taskq_pending_ids_for_async("cron:j1:planner")
            assert sorted(pending) == ["depth0", "depth1"]
        finally:
            monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
        assert store.loop_thread_calls == before

        # ``cancel_for_parent`` reads the same ids through that same seam and
        # then stops each row. Its remaining on-loop take is ``_unqueue``'s
        # ``taskq_cancel_queued``, which is NOT a drop-in offload: the row has to
        # be cancelled before a drain can claim it AND out of the window before
        # a stagger timer can start it, and an await between those two is a race
        # in either order. Tracked in ``taskq.store``'s backlog note, so this
        # call runs unarmed -- with the SYNC read wrapped, so a regression that
        # takes it there again is caught rather than merely warned about.
        sync_reads: list[str] = []
        bridge = type(mgr._admission)
        original_sync = bridge.taskq_pending_ids_for
        monkeypatch.setattr(
            bridge,
            "taskq_pending_ids_for",
            lambda self, key: sync_reads.append(key) or original_sync(self, key),
        )
        assert await mgr.cancel_for_parent("cron:j1:planner") == (0, 2)
        assert sync_reads == [], "the parent cancel must take the off-loop read"
        assert store.state_of("depth0") == model.CANCELLED
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_attached_children_guard_reads_the_store_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``chat_utils``' attached-children guard, the predicate every session
        teardown and dispatch route shares.

        Its queued half counts this parent's rows that live only on disk -- the
        term that makes a reset after a restart see children the registry cannot
        -- so it has to ask the store, and every caller of it (a reset consume,
        a 409 route, the RSS sweep, the completion frame) is a coroutine on the
        gateway loop.
        """
        from kiro_crew.dashboard import chat_utils

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        assert store is not None
        rec = mgr._admission.taskq_build_record(
            "ondisk0",
            {"task": "left by the previous incarnation", "parent_session_key": "dashboard:chat-1"},
            parent_session_key="dashboard:chat-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(rec) is None
        # No live run and nothing in the window: the store row is the ONLY
        # evidence, so a guard that skipped the read would answer "no children".
        mgr._agents.pop("ondisk0", None)
        mgr._queue.clear()
        state = SimpleNamespace(subagents=mgr, _slots={})

        # Counted, NEVER armed: this guard wraps its queued probe in
        # ``except Exception``, so the strict guard's raise would come back as
        # ``queued = 1`` -- a fail-closed ANSWER for every session, which is
        # both a green ``pytest.raises`` and a wrong verdict. The counter is the
        # only signal that cannot be swallowed.
        before = store.loop_thread_calls
        attached = await chat_utils.subagents_attached_async(state, None, "dashboard:chat-1", "pin")
        elsewhere = await chat_utils.subagents_attached_async(
            state, None, "dashboard:chat-9", "pin"
        )
        assert attached is True, "the store-only row is a child the guard must see"
        assert elsewhere is False, "another session's row is not this session's child"
        assert store.loop_thread_calls == before
        await mgr.cancel_all()

    def test_no_coroutine_calls_the_synchronous_attached_children_guard(self) -> None:
        """The guard has a sync entry for sync callers; no ``async def`` may use it.

        A ratchet over the whole package, not a list of the call sites this pass
        converted: the sync entry reads the task store on whatever thread calls
        it, so ONE coroutine left on it puts the busy wait back on the loop, and
        the next such caller would be added without a second thought.

        ``_queued_depth`` is covered by the same walk, with exactly one
        coroutine allowed to name it: ``_queued_depth_off_loop``, which reaches
        it only for a manager double that has no async sibling and therefore no
        store to block on.
        """
        import ast

        allowed = {("chat_utils.py", "_queued_depth_off_loop", "_queued_depth")}
        src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        offenders: list[str] = []
        for path in sorted(src.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))

            class _Visitor(ast.NodeVisitor):
                def __init__(self) -> None:
                    self.frames: list[tuple[str, bool]] = []

                def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                    self.frames.append((node.name, False))
                    self.generic_visit(node)
                    self.frames.pop()

                def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                    self.frames.append((node.name, True))
                    self.generic_visit(node)
                    self.frames.pop()

                def visit_Lambda(self, node: ast.Lambda) -> None:
                    self.frames.append(("<lambda>", False))
                    self.generic_visit(node)
                    self.frames.pop()

                def visit_Call(self, node: ast.Call) -> None:
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    frame = self.frames[-1] if self.frames else ("<module>", False)
                    if (
                        name in ("subagents_attached", "_queued_depth")
                        and frame[1]
                        and (path.name, frame[0], name) not in allowed
                    ):
                        offenders.append(f"{path.name}:{node.lineno} {frame[0]}() calls {name}()")
                    self.generic_visit(node)

            _Visitor().visit(tree)
        assert offenders == [], offenders

    @pytest.mark.asyncio
    async def test_the_pump_grants_a_resume_off_loop_and_releases_a_refused_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pump's resume seam: the lane slot is RESERVED on the loop (where
        the pump's own capacity re-check reads it), ``wake_wait`` runs on the
        writer thread, and the run state plus the resume signal are published
        only from its result.

        ``_drain_queue`` is deliberately NOT stubbed: a mock there is exactly
        what hides the write the wake triggers.
        """
        import threading

        from kiro_crew.subagent import SubagentInfo

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        threads: list[str] = []
        original_wake = store.wake_wait

        def _spy(*a: Any, **kw: Any) -> Any:
            threads.append(threading.current_thread().name)
            return original_wake(*a, **kw)

        monkeypatch.setattr(store, "wake_wait", _spy)

        async def _park(task_id: str) -> tuple[SubagentInfo, int]:
            rec = model.TaskRecord(id=task_id, kind=model.KIND_SUBAGENT, params={"task": task_id})
            await store.run(store.accept_one, rec)
            claimed = await store.run(store.claim, task_id)
            gen = claimed.generation
            await store.run(store.transition, task_id, model.STARTING, generation=gen)
            await store.run(store.transition, task_id, model.RUNNING, generation=gen)
            record = WaitRecord.input("call-1", since=store.now())
            assert await store.run(store.enter_wait, task_id, record.to_dict(), generation=gen)
            info = SubagentInfo(id=task_id, task="t")
            info._taskq_generation = gen
            info._slot_released = True
            info._wait_record = record.to_dict()
            info._resume_event = asyncio.Event()
            mgr._agents[task_id] = info
            return info, gen

        try:
            granted, gen = await _park("resume-ok")
            before = store.loop_thread_calls
            assert mgr._admission.request_resume(granted, reason="answered") is True
            await mgr._drain_task
            assert store.loop_thread_calls == before
            assert threads and all(t.startswith("taskq-writer") for t in threads), threads
            row = await store.run(store.get, granted.id)
            assert row.state == model.RUNNING
            assert granted._taskq_generation == gen + 1 == row.generation
            assert granted._slot_released is False and granted._resume_event is None

            # A refused wake gives the reservation back and leaves the run parked.
            refused, _gen = await _park("resume-refused")
            count_before = mgr._running_count
            monkeypatch.setattr(store, "wake_wait", lambda *a, **kw: None)
            assert mgr._admission.request_resume(refused, reason="answered") is True
            await mgr._drain_task
            assert mgr._running_count == count_before
            assert refused._slot_released is True and refused._resume_pending is False
            assert not refused._resume_event.is_set()
            assert (await store.run(store.get, refused.id)).state == model.WAITING_INPUT
        finally:
            mgr._agents.clear()
            await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_a_run_that_ends_inside_its_reservation_window_gives_the_slot_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reservation is not a grant, so the publish re-tests the run.

        The pump reserves EVERY queued resume in one synchronous pass and only
        then grants them one at a time with an await between, so a run whose
        entry sits behind another's ends while its own slot is already reserved.
        ``resume_reserve``'s liveness gate ran on the near side of that await and
        ``yield_slot`` already spent the run's one-shot release token, so a
        publish onto a finished run charges a lane slot no terminal path can hand
        back -- the cap falls by one per such resume until the lane is dead.

        Two entries, and the ending is driven from inside the LEAD's ``wake_wait``
        so it lands in the real window rather than in a race between two clocks.
        The victim's wake still lands (the store is reached and its row is
        waiting), which is why the generation it produced has to be adopted even
        though the grant is refused: the run's own terminal write is fenced by
        the generation the ROW now carries, not by the stale one it went in with.
        """
        from kiro_crew.subagent import SubagentInfo

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        assert store is not None
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)

        async def _park(task_id: str) -> SubagentInfo:
            rec = model.TaskRecord(id=task_id, kind=model.KIND_SUBAGENT, params={"task": task_id})
            await store.run(store.accept_one, rec)
            claimed = await store.run(store.claim, task_id)
            gen = claimed.generation
            await store.run(store.transition, task_id, model.STARTING, generation=gen)
            await store.run(store.transition, task_id, model.RUNNING, generation=gen)
            record = WaitRecord.input("call-1", since=store.now())
            assert await store.run(store.enter_wait, task_id, record.to_dict(), generation=gen)
            info = SubagentInfo(id=task_id, task="t")
            info._taskq_generation = gen
            info._wait_record = record.to_dict()
            info._slot_released = True  # the token yield_slot spends
            info._resume_event = asyncio.Event()
            mgr._agents[task_id] = info
            return info

        try:
            lead = await _park("lead-1")
            victim = await _park("victim-1")
            assert mgr._running_count == 0
            for info in (lead, victim):
                assert mgr._admission.request_resume(info, reason="answered") is True

            original_wake = store.wake_wait
            ended = False

            def _end_the_victim_inside_the_window(*a: Any, **kw: Any) -> Any:
                nonlocal ended
                if not ended:
                    ended = True
                    # Exactly what the run's own terminal path does for a run
                    # that ended while parked: the release token is already
                    # spent, so nothing is given back, and the resume entry it
                    # withdraws was popped by the pump one step ago.
                    victim.done = True
                    assert mgr._release_slot(victim) is False
                    victim._resume_pending = False
                    victim._resume_event = None
                return original_wake(*a, **kw)

            monkeypatch.setattr(store, "wake_wait", _end_the_victim_inside_the_window)
            assert mgr._drain_task is not None
            await mgr._drain_task
            for _ in range(5):  # the refusal's own re-drain, if it scheduled one
                await asyncio.sleep(0)
                if mgr._drain_task is not None and not mgr._drain_task.done():
                    await mgr._drain_task
            monkeypatch.setattr(store, "wake_wait", original_wake)

            assert ended, "the lead's wake never ran: this pin never reached the window"
            assert lead._slot_released is False, "the live run was granted its slot"
            assert victim._slot_released is True, "a finished run was published as a slot holder"
            assert mgr._running_count == 1, "the finished run's reservation was never given back"
            # The landed wake is adopted even on the refusal, so the victim's own
            # terminal write is not fenced out by the row it left behind.
            row = await store.run(store.get, victim.id)
            assert victim._taskq_generation == row.generation
            mgr._admission.taskq_settle(victim)
            for _ in range(20):
                await asyncio.sleep(0.01)
            assert (await store.run(store.get, victim.id)).state in model.TERMINAL
        finally:
            mgr._agents.clear()
            await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_a_closed_admission_neither_refills_the_window_nor_grants_a_resume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The updater's boundary reaches the pump's two re-registrations.

        While admission is CLOSED the window takes no store row (it would only be
        refused by the spawn gate) and a resume reserves no slot (the run stays
        parked, its wait intact). Both are HELD, not dropped: the same two steps
        land once admission reopens, which is what keeps the guard from trading a
        loosened census for stranded work.
        """
        from kiro_crew.subagent import SubagentInfo

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        rec = mgr._admission.taskq_build_record(
            "closed-row",
            {"task": "t", "parent_session_key": "web-1"},
            parent_session_key="web-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(rec) is None
        parked = SubagentInfo(id="parked-1", task="t")
        parked._slot_released = True
        parked._resume_event = asyncio.Event()
        mgr._agents["parked-1"] = parked
        try:
            mgr._sessions.admission_closed = True
            running_before = mgr._running_count
            assert await mgr._admission.taskq_refill_window_async() >= 1
            assert mgr._queue == []  # the row stayed on disk
            assert mgr._admission.resume_reserve({"_resume_id": "parked-1"}) is False
            assert mgr._running_count == running_before
            assert parked._slot_released is True and parked._resume_pending is False
            assert not parked._resume_event.is_set()
            assert (await store.run(store.get, "closed-row")).state == model.QUEUED

            mgr._sessions.admission_closed = False
            assert await mgr._admission.taskq_refill_window_async() >= 1
            assert [p.get("_preassigned_id") for p in mgr._queue] == ["closed-row"]
            assert mgr._admission.resume_reserve({"_resume_id": "parked-1"}) is True
            assert mgr._running_count == running_before + 1
        finally:
            mgr._queue.clear()
            mgr._agents.clear()
            await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_lanes_endpoint_reads_the_store_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GET /api/spawn/lanes``: BOTH store halves of the snapshot run on the
        writer thread -- the rows waiting per lane, and the lane a live run whose
        parent is absent from this process resolves to
        (``lane_for_session`` -> ``store.get``).

        Pinned two ways because one is not enough anywhere near this guard: the
        counter (which no ``except`` can swallow) and the armed guard (this route
        has no exception arm, so an on-loop take would answer 500).
        """
        from kiro_crew.dashboard.handlers import spawn_resume
        from kiro_crew.subagent import SubagentInfo

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        # One row waiting on disk, outside the window: the ``pending_lanes`` read.
        rec = mgr._admission.taskq_build_record(
            "lane-row",
            {"task": "t", "parent_session_key": "web-1"},
            parent_session_key="web-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(rec) is None
        # One live run under a parent this manager never knew: the ``store.get``.
        mgr._agents["kid-1"] = SubagentInfo(
            id="kid-1", task="t", parent_session_key="subagent:ghost-1"
        )
        app = web.Application()
        app["state"] = SimpleNamespace(subagents=mgr)
        request = make_mocked_request("GET", "/api/spawn/lanes", app=app)
        try:
            monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
            before = store.loop_thread_calls
            response = await spawn_resume.api_spawn_lanes(request)
            assert store.loop_thread_calls == before
            monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
            body = json.loads(response.text)
            # Both reads are IN the answer, so the offload is of live work.
            assert body["lanes"]["web-1"]["queued"] == 1
            assert sum(lane["running"] for lane in body["lanes"].values()) == 1
            assert body["capacity"]["cap_total"] >= 1
        finally:
            mgr._agents.clear()
            await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_wave_reads_run_off_loop_for_every_coroutine_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``batch_members_pending`` and the reaper's two wave sweeps: the
        store-only membership read (``fetch_pending_by_batch``) runs on the
        writer thread.

        The COUNTER is the pin here, not the raise: every coroutine caller of
        these sits inside ``except Exception`` -- the reaper's per-sweep guard and
        the gateway consumer's ``_last`` fallback -- so an armed guard would only
        flip a wave to "not pending" and skip the sweep silently.
        """
        import inspect

        from kiro_crew.subagent import SubagentInfo
        from kiro_crew.subagent_manager import monitoring as monitoring_mod

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        rec = mgr._admission.taskq_build_record(
            "wave-row",
            {"task": "t", "parent_session_key": "web-1", "batch_id": "wv"},
            parent_session_key="web-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        rec.params["batch_id"] = "wv"
        assert mgr._admission.taskq_accept_record(rec) is None
        # ``wv``: submissions outstanding and no in-memory trace, so the sweep
        # reaches the store. ``held``: a finished member still holding its digest.
        mgr._batch_submitted["wv"] = [1, 2]
        mgr._batch_progress_ts["wv"] = 0.0
        held = SubagentInfo(id="held-1", task="t", batch_id="held", done=True)
        held._digest_held_at = 1.0
        mgr._agents["held-1"] = held
        mgr._on_done = lambda info: None
        try:
            monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
            before = store.loop_thread_calls
            assert await mgr.batch_members_pending_async("wv") is True
            assert await mgr.batch_members_pending_async("held") is False
            await mgr._sweep_stuck_waves_async(1e9)
            await mgr._sweep_digest_holds_async(1e9)
            assert store.loop_thread_calls == before
            monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
            # The store-only member held the wave open, so nothing was reconciled.
            assert mgr._batch_submitted["wv"] == [1, 2]
            # And the two call sites take the awaited form, not the sync entry.
            reaper = inspect.getsource(monitoring_mod.OrphanStallMonitor._reaper_loop_impl)
            assert "await self._manager._sweep_stuck_waves_async(now)" in reaper
            assert "await self._manager._sweep_digest_holds_async(now)" in reaper
            consumer = inspect.getsource(GatewayOrchestrator._init_subagents)
            assert "await _subagent_batch_pending(self.subagent_mgr, _batch_id)" in consumer
        finally:
            mgr._agents.clear()
            mgr._on_done = None
            await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_first_dependency_coordinator_is_built_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The FIRST coordinator rebuilds the schedule from every waiting row, so
        a coroutine takes ``dependency_coordinator_async``.

        The sync entry is the negative control, and it is only a control because
        ``taskq_coordinator`` re-raises ``OnLoopStoreError`` out of its rebuild
        guard: while that arm swallowed it, an armed run reported a failed rebuild
        and restored NOTHING instead of naming the caller.
        """
        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        record = WaitRecord.dependency("github:api", since=store.now(), retry_at=store.now() + 30)
        rec = model.TaskRecord(id="dep-1", kind=model.KIND_SUBAGENT, params={"task": "t"})
        await store.run(store.accept_one, rec)
        claimed = await store.run(store.claim, "dep-1")
        await store.run(store.transition, "dep-1", model.STARTING, generation=claimed.generation)
        await store.run(store.transition, "dep-1", model.RUNNING, generation=claimed.generation)
        assert await store.run(
            store.enter_wait, "dep-1", record.to_dict(), generation=claimed.generation
        )
        try:
            monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
            with pytest.raises(OnLoopStoreError, match="taken on the event loop"):
                mgr.dependency_coordinator()
            assert getattr(mgr, "_taskq_dependency_coordinator", None) is None
            before = store.loop_thread_calls
            coordinator = await mgr.dependency_coordinator_async()
            assert coordinator is not None
            assert store.loop_thread_calls == before
            monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
            # The waiting row rejoined its scope, which is what the rebuild is for.
            assert coordinator.next_deadline() is not None
        finally:
            dep_mod.register_coordinator(None)
            await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_a_cancel_inside_the_parked_defers_window_is_announced_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Taking the defer off the loop opens a window; one id still gets ONE
        terminal event.

        The window is real, not simulated: the writer thread is held inside
        ``store.defer`` while the loop runs the cancel a dashboard Stop runs, so
        the write returns False for a row that EXISTS and is cancelled. That is a
        different fact from the write's other False -- no row at all, a ``_queue``
        entry that never reached the store -- and only the second owes the
        requester a rejection. Announcing one for the first would tell a single id
        both that the user stopped it and that the host refused it, which is what
        the pre-existing ``ClaimPoint`` window already avoids.
        """
        import threading

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        assert store is not None
        rec = mgr._admission.taskq_build_record(
            "row0",
            {"task": "t", "parent_session_key": "web-1"},
            parent_session_key="web-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        store.accept([rec])
        announced: list[Any] = []
        monkeypatch.setattr(
            type(mgr), "_announce_rejection", lambda self, info: announced.append(info) or info
        )
        # Pressure refuses every spawn, so the drain takes the parked-defer path.
        from kiro_crew import resource_status as rs
        from kiro_crew import subagent as subagent_mod

        monkeypatch.setattr(
            subagent_mod,
            "cached_admission_check",
            lambda *a, **k: rs.AdmissionDecision(
                admitted=False,
                posture=rs.POSTURE_CRITICAL,
                available_gb=1.2,
                reason="host memory is critical",
            ),
        )
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)

        parked = threading.Event()
        release = threading.Event()
        real_defer = store.defer

        def _park_inside_the_write(*a: Any, **kw: Any) -> bool:
            parked.set()
            assert release.wait(10)
            return bool(real_defer(*a, **kw))

        monkeypatch.setattr(store, "defer", _park_inside_the_write)
        try:
            drain = asyncio.ensure_future(mgr._drain_queue_async())
            assert await asyncio.to_thread(parked.wait, 10)
            assert await mgr.cancel("row0") is True
            release.set()
            await asyncio.wait_for(drain, 20)
            assert store.state_of("row0") == model.CANCELLED
            assert announced == [], (
                "a row the canceller already reported was announced again, so one id "
                "carries two contradictory terminal events"
            )
        finally:
            release.set()
            await mgr.cancel_all()


# ── a claim the store cannot take never starts an untracked run ─────────────


@pytest.mark.asyncio
async def test_claim_unavailable_leaves_the_row_queued_instead_of_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import taskq as _taskq
    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_or_create = AsyncMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    await mgr.wait_taskq_ready()
    mgr._spawn_stagger_secs = 0.0
    store = mgr._admission.taskq_store()
    assert store is not None

    def _busy(*_a: Any, **_k: Any) -> Any:
        raise _taskq.TaskStoreUnavailable("database is locked")

    monkeypatch.setattr(store, "claim", _busy)
    gen, proceed, reason = mgr._admission.taskq_claim("x1")
    assert (gen, proceed, reason) == (0, False, mgr._admission.CLAIM_UNAVAILABLE)

    info = mgr.spawn("do it", parent_session_key="web-1")
    assert info is not None and info.queued and not info.done, info
    assert info.id not in mgr._agents, "an unclaimed row must not start"
    assert mgr._running_count == 0
    assert store.state_of(info.id) == _taskq.QUEUED
    await mgr.cancel_all()


# ── mutable gates run once per submission; a drained refusal cancels its row ─


@pytest.mark.asyncio
async def test_store_accepted_reentry_skips_the_mutable_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``spawn_async`` commits the row after ``prepare_spawn`` passed the policy
    gates; the ``_store_accepted`` re-entry must not evaluate them again, so a
    governance/cwd change during the commit cannot refuse a committed row."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import subagent as subagent_mod
    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    provider = AsyncMock()
    provider.stream = MagicMock(side_effect=lambda *a, **k: iter(()))
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    await mgr.wait_taskq_ready()
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    mgr._spawn_stagger_secs = 0.0
    store = mgr._admission.taskq_store()
    assert store is not None

    calls: list[str] = []
    verdict = {"deny": False}

    def _gov(*_a: Any, **_k: Any) -> str | None:
        calls.append("gov")
        return "spawn disabled" if verdict["deny"] else None

    monkeypatch.setattr(subagent_mod, "_vet_spawn_governance", _gov)
    original_accept = store.accept_one

    def _flip_then_accept(record: Any) -> str:
        verdict["deny"] = True  # governance changes DURING the commit
        return original_accept(record)

    monkeypatch.setattr(store, "accept_one", _flip_then_accept)
    info = await mgr.spawn_async("hello", parent_session_key="web-1")
    assert info is not None and not info.error, info
    assert calls == ["gov"], "the gate ran exactly once, in prepare_spawn"
    assert info.id in mgr._agents and not mgr._agents[info.id].done
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_a_drained_row_refused_by_the_pump_is_failed_in_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew import subagent as subagent_mod
    from kiro_crew import taskq as _taskq
    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_or_create = AsyncMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    await mgr.wait_taskq_ready()
    mgr._spawn_stagger_secs = 0.0
    store = mgr._admission.taskq_store()
    assert store is not None
    rec = mgr._admission.taskq_build_record(
        "drained1",
        {"task": "t", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode=None,
    )
    assert mgr._admission.taskq_accept_record(rec) is None
    monkeypatch.setattr(subagent_mod, "_vet_spawn_governance", lambda *a, **k: "spawn disabled")
    info = mgr.spawn("t", parent_session_key="web-1", _from_queue=True, _preassigned_id="drained1")
    assert info is not None and info.done and "governance" in (info.error or "")
    assert (
        store.state_of("drained1") == _taskq.FAILED
    ), "the caller's refusal is the store's verdict"
    await mgr.cancel_all()


# ── wave accounting counts each member exactly once ──────────────────────────


@pytest.mark.asyncio
async def test_batch_members_are_counted_once_including_a_prepare_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A three-member wave through ``spawn_async``: one member is refused in
    ``prepare_spawn`` (bad cwd). Every member is counted exactly once, so the
    wave can close, and ``counted: true`` on ``/api/spawn`` is true for the
    refused member too."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    provider = AsyncMock()
    provider.stream = MagicMock(side_effect=lambda *a, **k: iter(()))
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    await mgr.wait_taskq_ready()
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    mgr._spawn_stagger_secs = 0.0
    assert mgr._admission.taskq_store() is not None

    ok1 = await mgr.spawn_async("a", parent_session_key="web-1", batch_id="w1", batch_total=3)
    bad = await mgr.spawn_async(
        "b", parent_session_key="web-1", batch_id="w1", batch_total=3, cwd="/definitely/not/here"
    )
    ok2 = await mgr.spawn_async("c", parent_session_key="web-1", batch_id="w1", batch_total=3)
    assert ok1 is not None and ok2 is not None and not ok1.queued and not ok2.queued
    assert bad is not None and bad.done and bad.error and "cwd" in bad.error
    assert mgr._batch_submitted["w1"][0] == 3, mgr._batch_submitted
    await mgr.cancel_all()


# ── an enabled queue with no store REFUSES ───────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_is_refused_typed_when_the_enabled_store_is_unavailable() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.subagent import SubagentManager
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_or_create = AsyncMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    await mgr.wait_taskq_ready()
    # The queue is enabled but its store did not open (what taskq_open records).
    mgr._taskq = None
    mgr._taskq_unavailable = "durable task queue unavailable: disk I/O error"
    assert mgr._admission.taskq_required_but_unavailable()
    for entry in (mgr.spawn, mgr.spawn_async):
        result = entry("do it", parent_session_key="web-1")
        info = await result if asyncio.iscoroutine(result) else result
        assert info is not None and info.done
        assert info.error_code == SpawnAdmissionCoordinator.TASK_STORE_UNAVAILABLE_CODE
        assert "disk I/O error" in (info.error or "")
        assert info.id not in mgr._agents or mgr._agents[info.id].done
    assert mgr._queue == []
    assert mgr._running_count == 0


# ── a committed app spawn queues on capacity; a refused one fails its row ────


async def _app_manager(monkeypatch: pytest.MonkeyPatch) -> Any:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.subagent import SubagentManager

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    provider = AsyncMock()
    provider.stream = MagicMock(side_effect=lambda *a, **k: iter(()))
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx, max_concurrent=1)
    await mgr.wait_taskq_ready()
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    mgr._spawn_stagger_secs = 0.0
    assert mgr._admission.taskq_store() is not None
    return mgr


@pytest.mark.asyncio
async def test_committed_prevalidated_app_spawn_queues_instead_of_refusing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity is a scheduling fact: an accepted (committed) app spawn that
    finds no slot QUEUES like any other row, with its prevalidation dropped so
    the drain re-validates the agent and re-proves app ownership."""
    from kiro_crew import taskq as _taskq

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    mgr._running_count = mgr._max_concurrent  # no slot
    info = await mgr.spawn_async(
        "bg work",
        parent_session_key="web-1",
        agent="demo--worker",
        app="demo",
        _agent_prevalidated=True,
    )
    assert info is not None and info.queued and not info.done, info
    assert store.state_of(info.id) == _taskq.QUEUED
    entry = next(p for p in mgr._queue if p["_preassigned_id"] == info.id)
    assert entry["_agent_prevalidated"] is False
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_drained_app_spawn_reproves_ownership_or_fails_its_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace as NS

    from kiro_crew import subagent as subagent_mod
    from kiro_crew import taskq as _taskq

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(
        subagent_mod,
        "list_agents",
        lambda project_dir=None: [NS(name="other--bg", filename="other--bg.json")],
    )
    mgr._running_count = mgr._max_concurrent
    info = await mgr.spawn_async(
        "bg work",
        parent_session_key="web-1",
        agent="demo--worker",
        app="demo",
        _agent_prevalidated=True,
    )
    assert info is not None and info.queued
    mgr._running_count = 0
    # The drain re-enters the row: the agent is not one of the app's own agents.
    drained = mgr.spawn(
        **{k: v for k, v in mgr._queue[0].items() if k != "_lane"}, _from_queue=True
    )
    assert drained is not None and drained.done and "only spawn its OWN" in (drained.error or "")
    assert store.state_of(info.id) == _taskq.FAILED, "a genuine refusal of a committed row fails it"
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_durable_row_never_carries_prevalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store row of a prevalidated app spawn has no ``_agent_prevalidated``:
    a start rebuilt from the row (window refill, restart) runs the gates."""
    from types import SimpleNamespace as NS

    from kiro_crew import subagent as subagent_mod
    from kiro_crew import taskq as _taskq
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    mgr._running_count = mgr._max_concurrent
    info = await mgr.spawn_async(
        "bg work",
        parent_session_key="web-1",
        agent="demo--worker",
        app="demo",
        _agent_prevalidated=True,
    )
    assert info is not None and info.queued
    rec = store.get(info.id)
    assert rec is not None and "_agent_prevalidated" not in rec.params
    # The same holds for the sync accept path (``spawn`` off the loop).
    record = mgr._admission.taskq_build_record(
        "sub-sync",
        {"task": "t", "_agent_prevalidated": True, "cwd": ""},
        parent_session_key="web-1",
        memory_store="",
        app="demo",
        model=None,
        allowed_tools=None,
        approval_mode=None,
    )
    assert "_agent_prevalidated" not in record.params
    # A row written by a build that still stored the flag rebuilds without it.
    legacy = _taskq.TaskRecord(
        id="sub-legacy",
        kind=_taskq.KIND_SUBAGENT,
        session_key="web-1",
        params={"task": "t", "agent": "demo--worker", "app": "demo", "_agent_prevalidated": True},
    )
    assert SpawnAdmissionCoordinator._window_entry(legacy).get("_agent_prevalidated") is None
    # The row rebuilt from the store reaches the ownership gate: with a foreign
    # same-named agent under the app's filename the drained start FAILS.
    monkeypatch.setattr(
        subagent_mod,
        "list_agents",
        lambda project_dir=None: [NS(name="other--bg", filename="other--bg.json")],
    )
    mgr._queue.clear()
    mgr._running_count = 0
    entry = SpawnAdmissionCoordinator._window_entry(rec)
    drained = mgr.spawn(**{k: v for k, v in entry.items() if k != "_lane"}, _from_queue=True)
    assert drained is not None and drained.done and "only spawn its OWN" in (drained.error or "")
    assert store.state_of(info.id) == _taskq.FAILED
    await mgr.cancel_all()


# ── a drain request landing mid-drain is not lost ────────────────────────────


@pytest.mark.asyncio
async def test_drain_request_during_a_drain_runs_a_second_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity released while the coroutine pump is mid-pass: the request is
    coalesced into ``_drain_again`` and the SAME task runs one more pass, so
    the row that became startable is started without waiting for another
    trigger."""
    mgr = await _app_manager(monkeypatch)
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    passes: list[int] = []
    original = mgr._drain_queue_pass

    async def _counted_pass() -> None:
        passes.append(len(passes))
        if len(passes) == 1:
            mgr._drain_queue()  # a release lands while this pass runs
        await original()

    monkeypatch.setattr(mgr, "_drain_queue_pass", _counted_pass)
    mgr._drain_queue()
    task = getattr(mgr, "_drain_task")
    await task
    assert passes == [0, 1], "the coalesced request produced exactly one more pass"
    assert getattr(mgr, "_drain_again") is False
    await mgr.cancel_all()


# ── reserve-then-commit: the claim await cannot overshoot the cap ────────────


@pytest.mark.asyncio
async def test_a_spawn_during_the_parked_claim_queues_instead_of_overshooting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ClaimPoint`` reserves the slot BEFORE the claim is awaited. A spawn that
    lands while the claim is parked on the writer thread sees the cap spent and
    queues; when the claim returns, the re-entry consumes the reservation, so
    ``_running_count`` never exceeds ``_max_concurrent``."""
    import threading

    mgr = await _app_manager(monkeypatch)  # max_concurrent=1
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    rec = mgr._admission.taskq_build_record(
        "parked",
        {"task": "t", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode=None,
    )
    assert mgr._admission.taskq_accept_record(rec) is None
    parked = threading.Event()
    release = threading.Event()
    real_claim = store.claim

    def _slow_claim(task_id, *a, **kw):
        parked.set()
        assert release.wait(5), "the test never released the claim"
        return real_claim(task_id, *a, **kw)

    monkeypatch.setattr(store, "claim", _slow_claim)
    mgr._drain_queue()
    drain = getattr(mgr, "_drain_task")
    while not parked.is_set():
        await asyncio.sleep(0.005)
    # The claim is parked on the writer thread; the reservation is held.
    assert mgr._running_count == 1 == mgr._max_concurrent
    loser = mgr.spawn("late", parent_session_key="web-1")  # a sync accept-path spawn
    assert loser is not None and loser.queued and not loser.done, loser
    assert loser.id not in mgr._agents
    assert mgr._running_count == 1
    release.set()
    await drain
    for _ in range(10):
        await asyncio.sleep(0.01)
    assert "parked" in mgr._agents
    assert mgr._running_count <= mgr._max_concurrent
    await mgr.cancel_all()


@pytest.mark.asyncio
async def test_a_refused_or_unavailable_claim_releases_the_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import taskq as _taskq

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    for rid in ("r-unavailable", "r-cancelled"):
        rec = mgr._admission.taskq_build_record(
            rid,
            {"task": "t", "parent_session_key": "web-1"},
            parent_session_key="web-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(rec) is None

    def _busy(*_a, **_k):
        raise _taskq.TaskStoreUnavailable("locked")

    # Scoped for the same reason the taskq admission integration test is: a
    # blanket ``monkeypatch.undo()`` here would also revert this module's
    # ``healthy_host_memory`` pins and everything ``_app_manager`` patched, so
    # the second dispatch below would read the runner's real free memory.
    params = {"task": "t", "parent_session_key": "web-1", "_preassigned_id": "r-unavailable"}
    with monkeypatch.context() as busy_claim:
        busy_claim.setattr(store, "claim", _busy)
        info = await mgr._dispatch_async(params)
        assert info is not None and info.queued and not info.done
        assert mgr._running_count == 0, "an unavailable claim gives the reserved slot back"
    store.cancel("r-cancelled", reason="user stop")
    info = await mgr._dispatch_async({**params, "_preassigned_id": "r-cancelled"})
    assert info is not None and info.done and info.user_stopped
    assert mgr._running_count == 0, "a refused claim gives the reserved slot back"
    await mgr.cancel_all()


# ── posted store writes are drained by cancel_all ───────────────────────────


@pytest.mark.asyncio
async def test_posted_terminal_writes_are_tracked_and_drained_by_cancel_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``taskq_settle`` posted to the writer thread is a ``_report_tasks``
    member: ``cancel_all`` waits for it, so a gateway stopping right after a
    run ended does not leave a ``running`` row for the next boot to re-run."""
    import threading

    from kiro_crew import taskq as _taskq

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    rec = mgr._admission.taskq_build_record(
        "done1",
        {"task": "t", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode=None,
    )
    assert mgr._admission.taskq_accept_record(rec) is None
    claimed = store.claim("done1")
    assert claimed is not None
    from kiro_crew.subagent import SubagentInfo

    info = SubagentInfo(
        id="done1", task="t", parent_session_key="web-1", done=True, user_stopped=True
    )
    info._taskq_generation = claimed.generation
    gate = threading.Event()
    real_finish = store.finish

    def _slow_finish(*a, **kw):
        assert gate.wait(5)
        return real_finish(*a, **kw)

    monkeypatch.setattr(store, "finish", _slow_finish)
    mgr._admission.taskq_settle(info)
    posted = [t for t in mgr._report_tasks if not t.done()]
    assert posted, "the posted settle is tracked"
    gate.set()
    await mgr.cancel_all()
    assert all(t.done() for t in posted)
    assert store.state_of("done1") == _taskq.CANCELLED


# ── the W3 branch (nested child, parent blocked in spawn_sub_agents) off-loop ──


@pytest.mark.asyncio
async def test_nested_child_of_a_blocked_parent_runs_w3_off_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child accepted through ``/api/spawn`` for a parent parked in
    ``spawn_sub_agents``: the parent yields its slot (``waiting_children``)
    with the ledger read, the deadline read and the wait write all on the
    writer thread -- pinned with the strict guard armed for the whole call."""
    from types import SimpleNamespace as NS

    from kiro_crew import taskq as _taskq
    from kiro_crew.dashboard.handlers import messaging

    mgr = await _app_manager(monkeypatch)
    mgr._max_concurrent = 2
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    # The parent: a live run (a hand-built record, so the fake session cannot
    # end it), in the store as ``running``, blocked in the blocking tool.
    from kiro_crew.subagent import SubagentInfo

    rec = mgr._admission.taskq_build_record(
        "parent1",
        {"task": "parent", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode=None,
    )
    assert mgr._admission.taskq_accept_record(rec) is None
    claimed = store.claim("parent1")
    assert claimed is not None
    store.transition("parent1", _taskq.STARTING, generation=claimed.generation)
    store.transition("parent1", _taskq.RUNNING, generation=claimed.generation)
    live = SubagentInfo(id="parent1", task="parent", parent_session_key="web-1")
    live._taskq_generation = claimed.generation
    live._inflight_tool = NS(tool_name="@kirocrew-core/spawn_sub_agents", title="call-1")
    mgr._agents["parent1"] = live
    mgr._running_count = 1
    parent = live

    async def _hang(info: Any) -> None:  # the child stays live for the scenario
        await asyncio.Event().wait()

    monkeypatch.setattr(mgr, "_run", _hang)
    before = store.loop_thread_calls
    monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
    child = await messaging._spawn_on_loop(
        NS(subagents=mgr), "child", parent_session_key=f"subagent:{parent.id}"
    )
    for _ in range(20):
        await asyncio.sleep(0.01)  # posted writes land (still under the guard)
    # The accept AND the W3 branch (ledger read, deadline read, enter_wait)
    # completed without a loop-thread store call.
    assert store.loop_thread_calls == before
    monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
    assert child is not None and child.id in mgr._agents
    assert live._slot_released is True, "the parent yielded on the loop, synchronously"
    assert (live._wait_record or {}).get("state") == _taskq.WAITING_CHILDREN
    assert child.id in live._wait_record["resume_condition"]["ids"]
    rec = store.get(parent.id)
    assert rec is not None and rec.state == _taskq.WAITING_CHILDREN
    await mgr.cancel_all()


# ── a raise inside the re-entry never leaks the reserved slot ────────────────


@pytest.mark.asyncio
async def test_a_raise_inside_reenter_releases_the_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import taskq as _taskq

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    rec = mgr._admission.taskq_build_record(
        "boom1",
        {"task": "t", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode=None,
    )
    assert mgr._admission.taskq_accept_record(rec) is None
    before = mgr._running_count
    point = admission_mod.ClaimPoint("boom1")
    mgr._running_count += 1  # what spawn_impl does when it hands back a ClaimPoint

    def _reenter(_claimed):
        raise RuntimeError("unwrapped agents-dir scan")

    with pytest.raises(RuntimeError):
        await mgr._admission.claim_and_start(point, _reenter)
    assert mgr._running_count == before, "the reservation is released when re-entry raises"
    assert "boom1" not in mgr._agents
    # The claim itself landed; the row is leased to this incarnation and the
    # pump's next claim keeps that lease (not lost, not a phantom run).
    assert store.state_of("boom1") == _taskq.ADMITTED
    assert mgr._admission.taskq_lease_is_ours("boom1")
    # The pump path swallows the same raise without leaking either.
    monkeypatch.setattr(
        mgr,
        "spawn",
        lambda **kw: (
            (_ for _ in ()).throw(RuntimeError("boom"))
            if kw.get("_claimed")
            else (
                admission_mod.ClaimPoint(kw["_preassigned_id"])
                if not (mgr.__dict__.__setitem__("_running_count", mgr._running_count + 1))
                else None
            )
        ),
    )
    rec2 = mgr._admission.taskq_build_record(
        "boom2",
        {"task": "t", "parent_session_key": "web-1"},
        parent_session_key="web-1",
        memory_store="",
        app="",
        model="",
        allowed_tools=None,
        approval_mode=None,
    )
    assert mgr._admission.taskq_accept_record(rec2) is None
    mgr._queue.append({"task": "t", "parent_session_key": "web-1", "_preassigned_id": "boom2"})
    await mgr._drain_queue_async()
    assert mgr._running_count == before
    await mgr.cancel_all()


# ── the pump sees a deferred row only after its defer landed ─────────────────


@pytest.mark.asyncio
async def test_admitting_id_is_held_until_the_posted_defer_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from kiro_crew import subagent as subagent_mod

    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    monkeypatch.setattr(subagent_mod, "check_memory_available", lambda min_gb: (False, 0.5))
    gate = threading.Event()
    real_defer = store.defer
    seen: dict[str, Any] = {}

    def _slow_defer(task_id, until, *, reason):
        assert gate.wait(5)
        seen["until"] = until
        return real_defer(task_id, until, reason=reason)

    monkeypatch.setattr(store, "defer", _slow_defer)
    task = asyncio.ensure_future(mgr.spawn_async("pressure", parent_session_key="web-1"))
    for _ in range(20):
        await asyncio.sleep(0.005)
    admitting = getattr(mgr, "_admitting_ids")
    assert (
        len(admitting) == 1
    ), "the row stays excluded from the refill while its defer is in flight"
    (aid,) = tuple(admitting)
    assert aid in mgr._admission.taskq_excluded_ids()
    gate.set()
    info = await task
    assert info is not None and info.queued
    assert not admitting, "cleared only once the defer landed"
    rec = store.get(info.id)
    assert rec is not None and rec.next_run_at == seen["until"] > store.now()
    await mgr.cancel_all()


# ── a corrupt tasks.db does not fail-close spawns: quarantined, recreated ─────


@pytest.mark.asyncio
async def test_corrupt_tasks_db_is_quarantined_and_spawns_proceed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from kiro_crew.taskq.store import TaskStore as _TS

    home = Path(os.environ["KIROCREW_HOME"])
    path = _TS.default_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"garbage, not sqlite\n" * 32)
    mgr = await _app_manager(monkeypatch)  # opens the default store at construction
    store = mgr._admission.taskq_store()
    assert store is not None, "the queue is not fail-closed by a corrupt file"
    assert store.quarantined_to is not None and store.quarantined_to.exists()
    assert mgr._admission.taskq_required_but_unavailable() is None
    info = await mgr.spawn_async("after corruption", parent_session_key="web-1")
    assert info is not None and not info.done and info.id in mgr._agents
    assert store.get(info.id) is not None
    await mgr.cancel_all()


# ── a pump request landing mid-sweep is not lost ──────────────────────────────


@pytest.mark.asyncio
async def test_a_scope_armed_during_a_pump_sweep_gets_its_own_one_shot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run parks on a scope the in-flight sweep has already read past.

    The sweep arms from the deadline it read, so the request the park makes
    (``_taskq_pump`` right after ``coordinator.report``) is the ONLY thing that
    can arm the new instant. Dropping it leaves every waiter behind that scope
    to the 60s reaper backstop; coalescing it runs one more pass, which reads
    the schedule the park wrote and arms that scope's own one-shot.
    """
    mgr = await _app_manager(monkeypatch)
    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    store = mgr._admission.taskq_store()
    coordinator = await mgr.dependency_coordinator_async()
    assert coordinator is not None

    def _seed() -> int:
        store.accept_one(model.TaskRecord(id="pump-row", kind=model.KIND_TASKRUNNER_STEP))
        claimed = store.claim("pump-row")
        assert claimed is not None
        assert store.transition("pump-row", model.STARTING, generation=claimed.generation)
        assert store.transition("pump-row", model.RUNNING, generation=claimed.generation)
        return int(claimed.generation)

    generation = await store.run(_seed)
    loop = asyncio.get_running_loop()
    reads: list[float | None] = []
    verdicts: list[Any] = []
    real_next_deadline = coordinator.next_deadline

    def _next_deadline() -> float | None:
        value = real_next_deadline()
        reads.append(value)
        if len(reads) == 1:
            # One writer thread, so this park is strictly AFTER the read the
            # first pass arms from, and its pump request reaches the loop
            # before the sweep's own resumption does: the production order of
            # ``run.py``'s park -> ``_taskq_pump``, made deterministic.
            verdicts.append(
                coordinator.report(
                    "pump-row",
                    dep_mod.DependencySignal(
                        kind=dep_mod.KIND_RATE_LIMITED,
                        dependency_scope="github:api",
                        source="gh",
                        retry_at=coordinator.now() + 30.0,
                    ),
                    generation=generation,
                    from_state=model.RUNNING,
                )
            )
            loop.call_soon_threadsafe(mgr._taskq_pump)
        return value

    monkeypatch.setattr(coordinator, "next_deadline", _next_deadline)
    before = store.loop_thread_calls
    try:
        mgr._taskq_pump()
        await asyncio.wait_for(getattr(mgr, "_taskq_tick_task"), timeout=10)
        assert verdicts and verdicts[0].outcome == "wait"
        assert reads[0] is None, "the first pass read the schedule before the park"
        timer = getattr(mgr, "_taskq_pump_timer", None)
        assert timer is not None, "the scope armed mid-sweep got no one-shot timer"
        assert timer.when() <= loop.time() + 30.0
        assert len(reads) == 2, "the request that landed mid-sweep ran exactly one more pass"
        assert reads[1] is not None
        assert store.loop_thread_calls == before
    finally:
        timer = getattr(mgr, "_taskq_pump_timer", None)
        if timer is not None:
            timer.cancel()
        await mgr.cancel_all()


# ── the pump replays a terminal write the store refused ──────────────────────


@pytest.mark.asyncio
async def test_the_wired_pump_replays_a_terminal_write_the_store_refused(
    monkeypatch: pytest.MonkeyPatch, no_memory_pressure: None
) -> None:
    """An owed terminal write is accepted work: the row stays ``running`` under
    this incarnation's lease until it lands, and nothing else can take a running
    row, so something in the LIVE process has to replay it. A bound coordinator
    is what stops ``RunnerAdmission.tick`` ever running again, so that something
    is the reaper pump -- on the store's writer thread, never the loop.
    """
    mgr = await _app_manager(monkeypatch)
    store = mgr._admission.taskq_store()
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch.subagent_mgr = mgr
    orch._cfg = SimpleNamespace(agent=SimpleNamespace(adaptive_concurrency_mode="aimd"))
    orch.task_runner = _Runner()
    orch.dashboard_state = SimpleNamespace(workflow_service=_Runner())
    orch._runner_admission = None
    orch._runner_admission_adopted = False
    assert await mgr.dependency_coordinator_async() is not None
    orch._wire_runner_admission()
    await orch._runner_admission_store_ready()
    adm = orch._runner_admission
    assert adm is not None and adm.coordinator is not None
    assert not hasattr(mgr, "_runner_admission_tick"), "the fallback tick is dropped"

    rec = await adm.accept_async(kind=model.KIND_TASKRUNNER_STEP, task_id="taskrunner:r9:task1")
    assert rec is not None
    handle = await adm.admit(rec.id, kind=model.KIND_TASKRUNNER_STEP)
    await handle.running_async()
    real_finish = store.finish

    def _down(*_a: Any, **_kw: Any) -> Any:
        raise store_mod.TaskStoreUnavailable("store down")

    monkeypatch.setattr(store, "finish", _down)
    assert await handle.done_async() is False
    assert adm.stats()["pending_terminal_writes"] == 1
    assert (await store.run(store.get, rec.id)).state == model.RUNNING
    monkeypatch.setattr(store, "finish", real_finish)

    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    before = store.loop_thread_calls
    try:
        mgr._taskq_pump()
        await asyncio.wait_for(getattr(mgr, "_taskq_tick_task"), timeout=10)
        assert adm.stats()["pending_terminal_writes"] == 0, "the owed write was never replayed"
        assert (await store.run(store.get, rec.id)).state == model.DONE
        assert store.loop_thread_calls == before
    finally:
        timer = getattr(mgr, "_taskq_pump_timer", None)
        if timer is not None:
            timer.cancel()
        orch._unwire_runner_admission()
        await mgr.cancel_all()

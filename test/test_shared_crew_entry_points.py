"""Shared Crew repair: model work outside ``_run_chat`` runs through fair admission.

``turn_gate.run_admitted`` is the one wrapper side chat, thread replies, the
hooks agent, a remote crew turn, dashboard channel agents, the heartbeat and the
builtin apps use; an app's background spawn is admitted as a child; the task
runner's plan, retry and execute paths admit like ``/start``. A refused turn
never runs and its surface is told why.
"""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import turn_gate
from kiro_crew.shared_crew.admission import Liveness, OperationKind, OperationState, ReasonCode
from kiro_crew.shared_crew.containment import ContainmentReport, SliceManager
from kiro_crew.shared_crew.contracts import EnforcementLevel, Principal
from kiro_crew.shared_crew.work_store import WorkStore
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.clock import ManualClock

S = OperationState
ALICE = Principal.system("alice-entry").id


@pytest.fixture
def service(tmp_path, monkeypatch):
    clock = ManualClock(start=70_000.0)
    store = WorkStore(TaskStore(tmp_path / "t.db", clock=clock.time).open(), clock=clock.time)
    limits = effective_limits(
        shared_crew_config_from(
            {"admission_enabled": True, "principal_concurrency": 1, "crew_concurrency": 2}
        ),
        auto_crew_concurrency=8,
    )
    report = ContainmentReport(
        platform="linux",
        available=True,
        reason="",
        cpu=True,
        memory=True,
        pids=True,
        level=EnforcementLevel.BEST_EFFORT,
        hard_ready=True,
        slices_enabled=True,
    )
    svc = ag.AdmissionService(
        store,
        limits=limits,
        clock=clock.time,
        containment_probe=lambda: report,
        slices=SliceManager(
            runner=lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""),
            systemctl="/usr/bin/systemctl",
        ),
        liveness=lambda ref: Liveness.STOPPED,
    )

    async def _current():
        return svc

    monkeypatch.setattr(ag, "current_service", _current)
    monkeypatch.setattr(ag, "_SERVICE", svc)
    return svc


def _refusing(monkeypatch, reason=ReasonCode.CREW_QUEUE_FULL):
    async def refuse(**_kw):
        return ag.Admission(None, "rejected", reason=reason)

    monkeypatch.setattr(turn_gate, "admit_turn", refuse)


# -- the wrapper --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_admitted_runs_charged_to_the_initiator_and_settles(service):
    async def work():
        return "answer"

    with turn_gate.initiated_by(ALICE):
        result = await turn_gate.run_admitted(work(), session_key="side:tab", text="q")
    assert result == "answer"
    [record] = await service.list_work(session_key="side:tab")
    assert record.budget_key == ALICE and record.state is S.COMPLETED


@pytest.mark.asyncio
async def test_run_admitted_without_a_principal_charges_automation_or_unknown(service):
    async def work():
        return None

    await turn_gate.run_admitted(work(), session_key="hook:a", automation=True)
    await turn_gate.run_admitted(work(), session_key="hook:b")
    [auto] = await service.list_work(session_key="hook:a")
    [unknown] = await service.list_work(session_key="hook:b")
    assert auto.budget_key == ag.AUTOMATION_PRINCIPAL.id
    assert unknown.budget_key == Principal.unknown().id


@pytest.mark.asyncio
async def test_refused_work_never_runs_and_its_surface_is_told(monkeypatch):
    _refusing(monkeypatch)
    ran: list[str] = []
    told: list[object] = []

    async def work():
        ran.append("x")

    async def note(reason):
        told.append(reason)

    assert await turn_gate.run_admitted(work(), session_key="s", refused=note) is None
    assert ran == [] and told == [ReasonCode.CREW_QUEUE_FULL]
    with pytest.raises(turn_gate.TurnRefused, match="crew queue full"):
        await turn_gate.run_admitted_or_raise(work(), session_key="s")
    assert ran == []


@pytest.mark.asyncio
async def test_without_a_gateway_service_work_runs_unchanged(monkeypatch):
    monkeypatch.setattr(ag, "_SERVICE", None)

    async def work():
        return 7

    assert await turn_gate.run_admitted(work(), session_key="s") == 7


# -- the call sites -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refused_thread_reply_frees_its_slot_and_says_why(monkeypatch):
    from kiro_crew.dashboard import chat_threads

    _refusing(monkeypatch)
    frames: list[dict] = []
    body_ran: list[str] = []

    async def body(*_a, **_kw):
        body_ran.append("x")

    monkeypatch.setattr(chat_threads, "_thread_turn_body", body)
    monkeypatch.setattr(chat_threads, "broadcast_thread_reply", lambda _s, **kw: frames.append(kw))
    chat_threads._in_flight.add("tab:m1")
    await chat_threads._run_thread_turn(
        SimpleNamespace(), SimpleNamespace(key="tab"), "m1", "run-1", "hi", {}, [], "tab:m1", None
    )
    assert body_ran == []
    assert "tab:m1" not in chat_threads._in_flight
    assert frames[-1]["is_error"] and frames[-1]["final"]
    assert "Crew capacity" in frames[-1]["content"]


@pytest.mark.asyncio
async def test_refused_side_turn_settles_the_sidecar(monkeypatch):
    from kiro_crew.dashboard.handlers import side

    _refusing(monkeypatch)
    frames: list[dict] = []
    ran: list[str] = []

    async def fake_turn(*_a, **_kw):
        ran.append("x")

    monkeypatch.setattr(side, "_run_side_turn", fake_turn)
    monkeypatch.setattr(side, "broadcast_side_result", lambda _s, **kw: frames.append(kw))
    sidecar = SimpleNamespace(
        messages=[], last_run_id="", is_complete=True, task=None, append_user=lambda _q, **_kw: None
    )
    slot = SimpleNamespace(key="tab", _side=sidecar)
    state = SimpleNamespace(_background_tasks=set())
    run_id = side._dispatch_side_turn(state, slot, "why?")
    await asyncio.gather(*list(state._background_tasks) or [sidecar.task])
    assert ran == []
    assert sidecar.is_complete and sidecar.last_run_id == run_id and sidecar.task is None
    assert frames[-1]["is_error"] and frames[-1]["final"]


@pytest.mark.asyncio
async def test_remote_crew_turn_refusal_answers_the_coded_envelope(monkeypatch):
    from kiro_crew.dashboard import handlers_crew_turn, handlers_instances
    from kiro_crew.dashboard.handlers import source_providers

    _refusing(monkeypatch, ReasonCode.PRINCIPAL_ACTIVE_LIMIT)
    reached: list[str] = []

    async def inner(_request):
        reached.append("x")

    monkeypatch.setattr(handlers_instances, "_guard", lambda _r, _op: None)
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: True)
    monkeypatch.setattr(handlers_crew_turn, "_crew_turn", inner)
    request = make_mocked_request("POST", "/api/instances/i-1/crew-turn", match_info={"id": "i-1"})
    response = await handlers_crew_turn.api_crew_turn(request)
    assert reached == []
    assert response.status == 429
    assert b'"principal_active_limit"' in response.body


@pytest.mark.asyncio
async def test_app_spawn_is_an_admitted_child_or_a_host_decline(service, monkeypatch):
    from kiro_crew.apps import spawn_sdk

    monkeypatch.setattr(
        spawn_sdk,
        "list_agents",
        lambda: [SimpleNamespace(name="demo--bg", filename="demo--bg.json")],
    )
    monkeypatch.setattr(spawn_sdk, "is_internal_agent_spec", lambda _a: False)
    monkeypatch.setattr(spawn_sdk, "_app_spawn_model", lambda _app, _agent: "")
    spawned: list[str] = []

    class _Manager:
        _agents: dict = {}

        def spawn(self, task, **_kw):
            spawned.append(task)
            return SimpleNamespace(id="sub-1", error="", done=False)

        def get(self, _id):
            return None

    impl = spawn_sdk.build_spawn_impl(_Manager())
    assert await impl("do it", "demo--bg", True, "", "demo") == "sub-1"
    [child] = await service.list_work(session_key="app:demo")
    assert child.operation is OperationKind.SUBAGENT
    assert child.state is S.RUNNING and child.extra.get("execution_id") == "sub-1"
    assert child.budget_key == Principal.system("app:demo").id

    async def refuse(**_kw):
        return ag.Admission(service, "rejected", reason=ReasonCode.PRINCIPAL_ACTIVE_LIMIT)

    monkeypatch.setattr(service, "admit_child", refuse)
    with pytest.raises(spawn_sdk.SpawnError) as exc:
        await impl("again", "demo--bg", True, "", "demo")
    assert exc.value.declined and spawned == ["do it"]


@pytest.mark.asyncio
async def test_taskrunner_retry_is_admitted_like_start(monkeypatch):
    from kiro_crew.dashboard.handlers import taskrunner

    async def owner(_r, _op):
        return None

    async def refuse(_request, _state, _task_id, **_kwargs):
        return ag.Admission(None, "rejected", reason=ReasonCode.PRINCIPAL_ACTIVE_LIMIT)

    retried: list[str] = []

    async def retry_from_task(task_id, *_a, **_kw):
        retried.append(task_id)

    runner = SimpleNamespace(_runs={"t-1": object()}, _agent="", retry_from_task=retry_from_task)
    monkeypatch.setattr(taskrunner, "_require_taskrunner_owner", owner)
    monkeypatch.setattr(taskrunner, "_admit_existing_run", refuse)
    request = make_mocked_request(
        "POST",
        "/api/taskrunner/t-1/retry",
        match_info={"task_id": "t-1"},
        app={"state": SimpleNamespace(task_runner=runner)},
    )
    response = await taskrunner.api_taskrunner_retry(request)
    assert response.status == 429 and retried == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["retry", "execute_plan"])
@pytest.mark.parametrize("outcome", ["success", "failure", "passthrough"])
async def test_taskrunner_resume_inherits_admitted_principal(monkeypatch, endpoint, outcome):
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard.handlers import taskrunner
    from kiro_crew.shared_crew import containment
    from kiro_crew.testing.wait import await_until

    expected = None if outcome == "passthrough" else ALICE
    admission = SimpleNamespace(
        rejected=False,
        replayed=False,
        work=None if expected is None else SimpleNamespace(budget_key=expected),
        abandon=AsyncMock(),
        attach_child=AsyncMock(),
    )
    seen = []

    async def child():
        seen.append(containment._SCOPE_PRINCIPAL.get())

    async def run(*_args, **_kwargs):
        await await_until(asyncio.create_task(child()))
        if outcome == "failure":
            raise ValueError("cannot resume")

    runner = SimpleNamespace(
        _runs={"t-1": object()}, _agent="", retry_from_task=run, execute_plan=run
    )
    monkeypatch.setattr(taskrunner, "_require_taskrunner_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(taskrunner, "_admit_existing_run", AsyncMock(return_value=admission))
    monkeypatch.setattr(taskrunner, "_gate_auto_approve", AsyncMock(return_value=False))
    request = make_mocked_request(
        "POST",
        f"/api/taskrunner/t-1/{endpoint}",
        match_info={"task_id": "t-1"},
        app={"state": SimpleNamespace(task_runner=runner)},
    )
    with containment.principal_scope("p_other"):
        response = await getattr(taskrunner, f"api_taskrunner_{endpoint}")(request)
        assert containment._SCOPE_PRINCIPAL.get() == "p_other"
    assert seen == [expected]
    if outcome == "failure":
        assert response.status == 400
        admission.abandon.assert_awaited_once()
        admission.attach_child.assert_not_awaited()
    else:
        assert response.status == 200
        admission.abandon.assert_not_awaited()
        admission.attach_child.assert_awaited_once_with("t-1", session_key="taskrunner:t-1")


def _admission_request(state, body, *, path="/api/taskrunner/start"):
    import json
    from unittest.mock import AsyncMock

    from body_stream_helpers import BodyStreamPayload

    raw = json.dumps(body).encode("utf-8")
    request = make_mocked_request(
        "POST",
        path,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
            "Idempotency-Key": "task-request-0001",
        },
        payload=BodyStreamPayload(raw),
        match_info={"task_id": "t-1"},
        app={"state": state},
    )
    request["app"] = ""
    request["user"] = "local-app"
    request.json = AsyncMock(return_value=body)
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"spec": "__inline__:# Different work"},
        {"agent": "other-agent"},
        {"name": "other-name"},
        {"workspace_dir": "other-workspace"},
        {"source": "mcp"},
        {"auto_approve": True},
    ],
    ids=["spec", "agent", "name", "workspace", "source", "auto-approve"],
)
async def test_taskrunner_start_digest_binds_requested_work(
    service, monkeypatch, tmp_path, changed
):
    import json
    from unittest.mock import MagicMock

    from handlers_taskrunner_coverage_helpers import _runner, _state

    from kiro_crew.dashboard.handlers import taskrunner

    runner = _runner(tmp_path)
    state = _state(runner)
    monkeypatch.setattr(taskrunner, "app_disabled", lambda _app: False)
    monkeypatch.setattr(taskrunner, "_sel", MagicMock())
    body = {"spec": "__inline__:# Original work", "source": "dashboard"}
    first = await taskrunner.api_taskrunner_start(_admission_request(state, body))
    replay = await taskrunner.api_taskrunner_start(
        _admission_request(state, dict(reversed(body.items())))
    )
    conflict = await taskrunner.api_taskrunner_start(_admission_request(state, {**body, **changed}))
    assert first.status == replay.status == 200
    assert json.loads(replay.body)["replayed"] is True
    assert conflict.status == 409
    assert json.loads(conflict.body)["code"] == "idempotency_conflict"
    runner.start_background.assert_awaited_once()
    assert len(list(runner._work_dir.glob("TASK_*.md"))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_action,second_action,changed",
    [
        ("retry", "retry", {"from_step": 2}),
        ("retry", "execute_plan", {}),
        ("execute_plan", "retry", {}),
        ("execute_plan", "execute_plan", {"agent": "other-agent"}),
        ("execute_plan", "execute_plan", {"fresh": True}),
        ("execute_plan", "execute_plan", {"workspace_dir": "other-workspace"}),
        ("execute_plan", "execute_plan", {"auto_approve": True}),
    ],
    ids=["step", "retry-execute", "execute-retry", "agent", "fresh", "workspace", "auto-approve"],
)
async def test_taskrunner_resume_digest_binds_action_and_options(
    service, monkeypatch, tmp_path, first_action, second_action, changed
):
    import json
    from unittest.mock import MagicMock

    from handlers_taskrunner_coverage_helpers import _runner, _state

    from kiro_crew.dashboard.handlers import taskrunner

    runner = _runner(tmp_path)
    runner._runs = {"t-1": SimpleNamespace(status="planned")}
    state = _state(runner)
    monkeypatch.setattr(taskrunner, "_sel", MagicMock())
    first_handler = getattr(taskrunner, f"api_taskrunner_{first_action}")
    second_handler = getattr(taskrunner, f"api_taskrunner_{second_action}")
    first = await first_handler(_admission_request(state, {}))
    replay = await first_handler(_admission_request(state, {}))
    conflict = await second_handler(_admission_request(state, changed))
    assert first.status == replay.status == 200
    assert json.loads(replay.body)["replayed"] is True
    assert conflict.status == 409
    assert json.loads(conflict.body)["code"] == "idempotency_conflict"
    assert runner.retry_from_task.await_count + runner.execute_plan.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("uncharged", ["reopen", "invalid", "raced-reopen"])
async def test_slot_create_rate_charges_only_new_slots(service, monkeypatch, tmp_path, uncharged):
    import json
    from unittest.mock import MagicMock

    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.shared_crew import audit

    state = _make_state(tmp_path)
    monkeypatch.setattr(chat_handlers.KiroCrewConfig, "load", lambda: None)
    monkeypatch.setattr(chat_handlers, "default_project_dir", lambda _workspace: str(tmp_path))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *_a, **_kw: None)
    monkeypatch.setattr(audit, "schedule_action_audit", lambda *_a, **_kw: None)
    service._create_rate.set_rate(2)
    existing = state.get_or_create_slot("existing")

    async def post(body):
        return await chat_handlers.api_chat_slot_create(
            _admission_request(state, body, path="/api/chat/slots")
        )

    if uncharged == "raced-reopen":

        async def claim(*_args, **_kwargs):
            state.get_or_create_slot("raced")
            return False

        with monkeypatch.context() as raced:
            raced.setattr(chat_handlers, "_app_claim_refused", claim)
            assert (await post({"name": "raced"})).status == 200
    else:
        body = {"name": "existing"}
        if uncharged == "invalid":
            body["memory_mode"] = "invalid"
        for _ in range(3):
            response = await post(body)
            assert response.status == (400 if uncharged == "invalid" else 200)
    assert (await post({"name": "new-one"})).status == 200
    assert (await post({"name": "new-two"})).status == 200
    mint = MagicMock(wraps=state.get_or_create_slot)
    monkeypatch.setattr(state, "get_or_create_slot", mint)
    refused = await post({"name": "over-limit"})
    assert refused.status == 429
    assert json.loads(refused.body)["code"] == "session_create_rate"
    mint.assert_not_called()
    assert "over-limit" not in state._slots
    assert (await post({"name": "existing"})).status == 200
    assert state._slots["existing"] is existing

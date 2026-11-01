"""Worker session keys inherit their run's admission binding; apps cannot cancel work."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import routes_admission


def _service() -> ag.AdmissionService:
    limits = effective_limits(
        shared_crew_config_from({"admission_enabled": True}), auto_crew_concurrency=2
    )
    return ag.AdmissionService(None, limits=limits)


@pytest.mark.parametrize(
    ("bound", "worker"),
    [
        ("taskrunner:t1", "taskrunner:t1:runtime"),
        ("taskrunner:t1", "taskrunner:t1:autoapprove"),
        ("cron:job1", "cron:job1:run-7"),
        ("wf:r1", "wf:r1:3"),
        ("wf-pool:r1", "wf-pool:r1:0"),
        ("wf-worker:r1", "wf-worker:r1:9f86d081"),
    ],
)
def test_a_run_worker_key_inherits_the_run_binding(bound, worker):
    svc = _service()
    svc.bind_session(bound, "p_alice", "w-root")
    assert svc.budget_for_session(worker) == "p_alice"
    assert svc.root_for_session(worker) == "w-root"


@pytest.mark.parametrize(
    ("bound", "other"),
    [
        ("slack:C1", "slack:C1:1700000000.1"),
        ("dashboard:tab", "dashboard:tab:x"),
        ("taskrunner:t1", "taskrunner:t10:runtime"),
    ],
)
def test_other_keys_never_inherit(bound, other):
    svc = _service()
    svc.bind_session(bound, "p_alice", "w-root")
    assert svc.budget_for_session(other) is None
    assert svc.root_for_session(other) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "handler"),
    [
        ("POST", "/api/shared-crew/work/w1/cancel", routes_admission.api_work_cancel),
        ("GET", "/api/shared-crew/work?session_key=dashboard:bob", routes_admission.api_work),
    ],
)
async def test_an_app_token_cannot_cancel_or_list_other_sessions_work(method, path, handler):
    request = make_mocked_request(method, path, match_info={"work_id": "w1"})
    request["app"] = "demo"
    resp = await handler(request)
    assert resp.status == 403
    assert json.loads(resp.text)["code"] == "app_token_not_allowed"


def test_every_built_service_gets_the_hosts_child_hooks(monkeypatch):
    """A service rebuilt after a restart can settle recovered children before any spawn."""
    from types import SimpleNamespace

    from kiro_crew.shared_crew import host_hooks

    monkeypatch.setattr(ag, "_SERVICE", None)
    monkeypatch.setattr(ag, "_HOOKS", ag.HostHooks())
    run = SimpleNamespace(done=True, user_stopped=False, error="")
    state = SimpleNamespace(subagents=SimpleNamespace(_agents={"run-1": run}))
    host_hooks.install(state)
    svc = _service()
    try:
        ag.install_service(svc)
        assert svc._child_probe is not None and svc._child_cancel is not None
        assert svc._child_probe("run-1") is ag.OperationState.COMPLETED
    finally:
        ag.install_service(None)
        host_hooks.uninstall()


def test_both_gateway_entrypoints_prepare_admission_after_the_bind():
    """A headless ``--slack-only`` gateway builds admission like the dashboard does."""
    import ast
    import inspect

    from kiro_crew.dashboard.server_runtime import api_entrypoint, session_restore

    for module in (api_entrypoint, session_restore):
        tree = ast.parse(inspect.getsource(module))
        awaited = {
            node.value.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        }
        assert "prepare_gateway" in awaited, module.__name__


def test_a_delayed_child_start_keeps_its_own_admitted_principal(monkeypatch):
    """The parent session's next sender never moves an attached child's slice."""
    from types import SimpleNamespace

    from kiro_crew.shared_crew import containment

    svc = _service()
    monkeypatch.setattr(ag, "_SERVICE", svc)
    info = SimpleNamespace(id="run-7", parent_session_key="dashboard:tab", conversation_key="")
    svc.bind_session("dashboard:tab", "p_alice")
    # Before the attach the parent session decides.
    assert containment.run_principal(info) == "p_alice"
    assert containment.run_principal(info, own_only=True) is None
    svc.bind_session("subagent:run-7", "p_alice")
    # Bob sends into the parent session afterwards.
    svc.bind_session("dashboard:tab", "p_bob")
    assert containment.run_principal(info) == "p_alice"
    assert containment.run_principal(info, own_only=True) == "p_alice"


def test_a_workflow_attach_names_every_worker_namespace():
    from kiro_crew.dashboard.handlers import workflows

    keys = workflows._workflow_worker_keys("r1")
    assert keys == ("wf:r1", "wf-pool:r1", "wf-unpooled:r1", "wf-worker:r1")
    svc = _service()
    for key in keys:
        svc.bind_session(key, "p_alice", "w-root")
    for worker in ("wf:r1:0", "wf-pool:r1:2", "wf-unpooled:r1:1", "wf-worker:r1:ab"):
        assert svc.budget_for_session(worker) == "p_alice"


def test_an_http_launch_runs_in_the_admitted_principal_scope():
    """A parentless spawn's task copies the admitted budget before the attach."""
    from types import SimpleNamespace

    from kiro_crew.shared_crew import admission_entry, containment

    admitted = ag.Admission(None, "admitted", work=SimpleNamespace(budget_key="p_alice"))
    with containment.principal_scope("p_outer"):
        with admission_entry.admitted_scope(admitted):
            assert containment._SCOPE_PRINCIPAL.get() == "p_alice"
        with admission_entry.admitted_scope(ag.Admission(None, "passthrough")):
            assert containment._SCOPE_PRINCIPAL.get() == "p_outer"


@pytest.mark.parametrize(
    ("reason", "noted"),
    [
        (ag.ReasonCode.QUEUE_EXPIRED, True),
        (ag.ReasonCode.CANCELLED_BY_USER, False),
    ],
)
def test_an_admission_that_ends_before_the_turn_hands_the_queue_off(reason, noted):
    """The dispatch is marked unstarted so its done callback ends the chat cycle."""
    from types import SimpleNamespace

    from kiro_crew.shared_crew import turn_gate

    rows: list[tuple] = []
    slot = SimpleNamespace(
        _turn_generation=7,
        _unstarted_generation=-1,
        append=lambda *a, **_k: rows.append(a),
    )
    admission = ag.Admission(None, "queued", work=SimpleNamespace(reason=reason))
    turn_gate.note_admission_ended(slot, admission)
    assert slot._unstarted_generation == 7
    assert bool(rows) is noted

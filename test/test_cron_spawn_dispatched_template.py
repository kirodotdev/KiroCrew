"""``POST /api/spawn`` from a member cron vets and hands down the agent the fire runs.

``/api/spawn`` reads a cron parent's scheduler capture, which a 1.0 capture spells
``kirocrew`` while the fire dispatches the member's own agent and writes it into
the session record. The route applies that agent's ``availableAgents``, and an
agent-less child inherits it, with the capture's member, store and mode.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from handlers_messaging_coverage_helpers import _payload, _Req, _run, _state

import kiro_crew.dashboard.handlers.messaging as messaging
import kiro_crew.subagent as subagent_ns
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef, bind_session_execution
from kiro_crew.subagent_parts import agent_policy

PARENT = "cron:hourly"
CAPTURE = ExecutionContext("alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "kirocrew")


@pytest.fixture
def cron_spawn(monkeypatch):
    from kiro_crew.dashboard.handlers import _shared
    from kiro_crew.dashboard.handlers._shared_member_scope import MemberScope
    from kiro_crew.shared_crew import admission_entry
    from kiro_crew.shared_crew.admission_gate import Admission

    async def _passthrough(*_a, **_kw):
        return Admission(None, "passthrough")

    async def _persistent(*_a):
        return "persistent"

    async def _scheduler_scope(_request):
        return MemberScope(PARENT, True, CAPTURE.store.legacy_name, CAPTURE)

    monkeypatch.setattr(admission_entry, "admit_subagent", _passthrough)
    monkeypatch.setattr(messaging, "_spawn_request_memory_mode", _persistent)
    monkeypatch.setattr(messaging, "warm_project_agents_for_spawn", AsyncMock())
    monkeypatch.setattr(_shared, "member_request_scope", _scheduler_scope)
    monkeypatch.setattr(
        subagent_ns,
        "_resolve_parent_spawn_allowlists",
        lambda t, *_a, **_k: ((("scout",),) if t == "helper" else (), ()),
    )

    def call() -> Any:
        mgr = MagicMock()
        mgr.max_concurrent = 4
        mgr.spawn.return_value = SimpleNamespace(
            id="a1", done=False, error="", error_code="", queued_reason=""
        )
        sessions = SimpleNamespace(_pool_cwd="", get_agent_selection=MagicMock())
        state = _state(subagents=mgr, sessions=sessions, conversation_log=None)
        response = _run(messaging.api_spawn, _Req(state, {"task": "x", "parent_session": PARENT}))
        return response, mgr

    return call


def test_an_agentless_child_of_a_member_cron_runs_the_dispatched_agent(cron_spawn) -> None:
    bind_session_execution(PARENT, replace(CAPTURE, template_id="helper"))
    response, mgr = cron_spawn()

    assert response.status == 200, _payload(response)
    kwargs = mgr.spawn.call_args.kwargs
    admitted = kwargs["_execution_context"]
    assert admitted["template_id"] == "helper"
    assert (admitted["member_id"], admitted["selection_kind"]) == ("alpha", "member")
    assert kwargs["_parent_spawn_policy"][:2] == ("helper", (("scout",),))


def test_an_unreadable_cron_record_leaves_the_dispatched_policy_unknown(monkeypatch) -> None:
    def unreadable(_key):
        raise OSError("corrupt")

    monkeypatch.setattr(subagent_ns, "read_session_execution", unreadable)
    policy = agent_policy.with_cron_dispatched_policy(PARENT, CAPTURE, ("kirocrew", (), ()))

    assert policy[1] is None
    assert agent_policy._vet_parent_available_agents(policy, "other")


def test_a_bots_worker_record_leaves_the_scheduler_policy_and_child_as_on_main(
    cron_spawn,
) -> None:
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    bind_session_execution(PARENT, CAPTURE.with_template(MATE_WORKER_AGENT, MATE_WORKER_AGENT))
    response, mgr = cron_spawn()

    assert response.status == 200, _payload(response)
    kwargs = mgr.spawn.call_args.kwargs
    assert kwargs["_execution_context"]["template_id"] == "kirocrew"
    assert kwargs["_parent_spawn_policy"][:2] == ("kirocrew", ())


def test_a_conductor_capture_is_judged_by_the_agent_the_fire_dispatches() -> None:
    from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

    capture = replace(CAPTURE, template_id=MATE_CONDUCTOR_AGENT)
    bind_session_execution(PARENT, replace(CAPTURE, template_id="helper"))
    policy = agent_policy.with_cron_dispatched_policy(
        PARENT, capture, (MATE_CONDUCTOR_AGENT, (), ())
    )

    assert policy[0] == "helper"
    assert agent_policy._vet_parent_available_agents(policy, "scout") is None

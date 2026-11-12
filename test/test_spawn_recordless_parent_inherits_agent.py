"""``POST /api/spawn`` gives the child of a parent with no execution record the parent's agent.

A Slack, Discord or Telegram conversation has no execution record until its first
turn is saved, so a sub-agent spawned in that turn has a parent with none.
For that parent the route takes the parent's live selection
(``SessionManager.get_agent_selection``), the fallback ``resolve_spawn_execution``
applies to the same parent, so the child runs the parent's agent spec (its prompt,
tools, hooks and MCP servers) instead of the default ``kirocrew``. Only a request
with no parent, or a parent whose live selection is the default template, keeps
``kirocrew``.
"""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from handlers_messaging_coverage_helpers import _payload, _Req, _run, _state

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as messaging

#: A Slack thread session: the channel inbound path writes no execution record.
SLACK_PARENT = "slack:C1:1700000000.1"


def _info() -> Any:
    return SimpleNamespace(id="a1", done=False, error="", error_code="", queued_reason="")


@pytest.fixture
def spawn_route(monkeypatch):
    """Drive ``api_spawn`` with a parent whose live selection the test sets.

    Shared Crew fair admission and the request's memory mode are passed through:
    neither decides which agent the child runs, and both read state this double
    does not model. Returns ``call(body, selection)`` -> ``(response, mgr,
    get_agent_selection)``.
    """
    from kiro_crew.shared_crew import admission_entry
    from kiro_crew.shared_crew.admission_gate import Admission

    async def _passthrough(*_a, **_kw):
        return Admission(None, "passthrough")

    async def _persistent(*_a):
        return "persistent"

    monkeypatch.setattr(admission_entry, "admit_subagent", _passthrough)
    monkeypatch.setattr(messaging, "_spawn_request_memory_mode", _persistent)
    monkeypatch.setattr(messaging, "warm_project_agents_for_spawn", AsyncMock())

    def call(body: dict, selection: Any = ("template", "")):
        mgr = MagicMock()
        mgr.max_concurrent = 4
        mgr.spawn.return_value = _info()
        get_agent_selection = MagicMock(return_value=selection)
        sessions = SimpleNamespace(_pool_cwd="", get_agent_selection=get_agent_selection)
        state = _state(subagents=mgr, sessions=sessions, conversation_log=None)
        return _run(messaging.api_spawn, _Req(state, body)), mgr, get_agent_selection

    return call


def _admitted(mgr: MagicMock) -> dict:
    mgr.spawn.assert_called_once()
    return mgr.spawn.call_args.kwargs["_execution_context"]


def test_a_channel_parent_without_a_record_hands_its_template_to_the_child(spawn_route) -> None:
    response, mgr, selection = spawn_route(
        {"task": "search the code", "parent_session": SLACK_PARENT},
        ("template", "amzn-builder"),
    )
    assert response.status == 200, _payload(response)
    admitted = _admitted(mgr)
    assert admitted["template_id"] == "amzn-builder"
    assert admitted["selection_kind"] == "template"
    assert mgr.spawn.call_args.kwargs["agent"] == ""
    selection.assert_called_once_with(SLACK_PARENT)


def test_a_channel_parent_on_a_crewmate_hands_its_member_to_the_child(spawn_route) -> None:
    cfg = loader.KiroCrewConfig.load()
    cfg.agents["alice"] = loader.KiroCrewAgentConfig(kiro_agent="alice-agent")
    cfg.save()
    response, mgr, _ = spawn_route(
        {"task": "review this", "parent_session": SLACK_PARENT}, ("member", "alice")
    )
    assert response.status == 200, _payload(response)
    admitted = _admitted(mgr)
    assert admitted["selection_kind"] == "member"
    assert admitted["selection_name"] == "alice"
    assert admitted["template_id"] == "alice-agent"


@pytest.mark.parametrize(
    "selection", [("template", "amzn-builder"), ("member", "alice")], ids=["template", "member"]
)
def test_the_route_and_the_spawn_gate_hand_down_the_same_execution(spawn_route, selection) -> None:
    from kiro_crew.subagent_manager.admission.gate import _GateMixin

    cfg = loader.KiroCrewConfig.load()
    cfg.agents["alice"] = loader.KiroCrewAgentConfig(kiro_agent="alice-agent")
    cfg.save()
    response, mgr, _ = spawn_route({"task": "x", "parent_session": SLACK_PARENT}, selection)
    assert response.status == 200, _payload(response)
    gate = _GateMixin.resolve_spawn_execution(
        SimpleNamespace(),
        parent_session_key=SLACK_PARENT,
        _record=None,
        _inherited_selection=selection,
    )
    admitted = _admitted(mgr)
    for field in ("selection_kind", "selection_name", "template_id", "member_id"):
        assert admitted[field] == getattr(gate, field), field
    assert admitted["store"] == asdict(gate.store)


@pytest.mark.parametrize(
    "selection",
    [("member", "gone"), ("unknown", "amzn-builder"), None],
    ids=["missing_member", "unknown_kind", "not_a_tuple"],
)
def test_an_unusable_live_selection_is_refused_rather_than_run_as_kirocrew(
    spawn_route, selection
) -> None:
    """The same refusal ``spawn_async`` gives this parent: never a silent default."""
    response, mgr, _ = spawn_route({"task": "x", "parent_session": SLACK_PARENT}, selection)
    assert response.status == 409
    assert _payload(response)["code"] == "member_identity_unavailable"
    mgr.spawn.assert_not_called()


def test_a_parent_on_the_default_template_keeps_kirocrew(spawn_route) -> None:
    response, mgr, _ = spawn_route({"task": "x", "parent_session": SLACK_PARENT})
    assert response.status == 200, _payload(response)
    assert _admitted(mgr)["template_id"] == "kirocrew"


def test_an_explicit_agent_wins_and_the_selection_is_not_read(spawn_route) -> None:
    response, mgr, selection = spawn_route(
        {"task": "x", "parent_session": SLACK_PARENT, "agent": "reviewer"},
        ("template", "amzn-builder"),
    )
    assert response.status == 200, _payload(response)
    selection.assert_not_called()
    assert _admitted(mgr)["template_id"] == "reviewer"


def test_a_parentless_request_keeps_kirocrew(spawn_route) -> None:
    response, mgr, selection = spawn_route({"task": "x"}, ("template", "amzn-builder"))
    assert response.status == 200, _payload(response)
    selection.assert_not_called()
    assert _admitted(mgr)["template_id"] == "kirocrew"


def test_a_parent_with_a_record_keeps_the_recorded_template(spawn_route) -> None:
    from kiro_crew.execution_context import bind_session_execution, execution_for_store

    parent = "dashboard:chat-1"
    bind_session_execution(parent, execution_for_store("", template_id="recorded-agent"))
    response, mgr, selection = spawn_route(
        {"task": "x", "parent_session": parent}, ("template", "amzn-builder")
    )
    assert response.status == 200, _payload(response)
    selection.assert_not_called()
    assert _admitted(mgr)["template_id"] == "recorded-agent"

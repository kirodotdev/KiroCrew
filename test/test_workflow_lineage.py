"""``dashboard/workflow_lineage.py``: which session spawned a sub-agent, and
which workflow run a session works for, as ``WorkflowService`` asks."""

from __future__ import annotations

from types import SimpleNamespace

from kiro_crew.dashboard.workflow_lineage import (
    lineage_lookups,
    subagent_parent_session,
    workflow_run_of_session,
)


class _Subagents:
    def __init__(self, records: dict[str, object]) -> None:
        self._records = records

    def get(self, sub_id: str):
        return self._records.get(sub_id)


def _state(**kw):
    return SimpleNamespace(
        **{"subagents": None, "task_runner": None, "workflow_service": None, **kw}
    )


def test_a_subagent_maps_to_the_session_that_spawned_it():
    state = _state(subagents=_Subagents({"s1": SimpleNamespace(parent_session_key="chat-1")}))
    assert subagent_parent_session(state, "subagent:s1") == "chat-1"


def test_an_unknown_or_unparented_subagent_maps_to_none():
    state = _state(subagents=_Subagents({"s2": SimpleNamespace(parent_session_key="")}))
    assert subagent_parent_session(state, "subagent:gone") is None
    assert subagent_parent_session(state, "subagent:s2") is None


def test_a_non_subagent_key_or_no_manager_maps_to_none():
    state = _state(subagents=_Subagents({"s1": SimpleNamespace(parent_session_key="chat-1")}))
    assert subagent_parent_session(state, "chat-1") is None
    assert subagent_parent_session(_state(), "subagent:s1") is None


def test_a_step_session_without_a_task_runner_maps_to_none():
    assert workflow_run_of_session(_state(), "taskrunner:t1:task0") is None


def test_a_dashboard_session_is_none_while_the_ledger_tracks_no_opened_sessions():
    nesting = SimpleNamespace(tracks_opened_sessions=False)
    state = _state(workflow_service=SimpleNamespace(nesting=nesting))
    assert workflow_run_of_session(state, "chat-1") is None


def test_the_lookups_answer_for_the_state_they_were_bound_to():
    runs = {"t1": SimpleNamespace(workflow_run_id="wf_1")}
    state = _state(
        subagents=_Subagents({"s1": SimpleNamespace(parent_session_key="chat-1")}),
        task_runner=SimpleNamespace(_runs=runs),
    )
    parent_session_of, run_of_session = lineage_lookups(state)
    assert parent_session_of("subagent:s1") == "chat-1"
    assert run_of_session("taskrunner:t1:task0") == "wf_1"

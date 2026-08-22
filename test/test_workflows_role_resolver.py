from __future__ import annotations

import asyncio

import pytest

from kiro_crew.crew_catalog import RoleDefinition
from kiro_crew.workflows import BudgetExceeded
from kiro_crew.workflows.role_resolver import (
    ROLE_BLOCKED,
    ROLE_COMPLETED,
    RoleResolutionError,
    execute_role,
    resolve_role,
)

_ROLE = RoleDefinition(
    id="software-engineer",
    version="0.1.0",
    mission="Implement an approved change in a candidate workspace.",
    agent="software-engineer",
    skills=("implementation",),
    tool_scopes=("read_project", "candidate_write"),
    profile="candidate-write",
    input_schema="implementation_request",
    output_schema="implementation_result",
    handoff="implementation_result",
    quality_gates=("tests_recorded",),
    side_effects="candidate-write",
)

_SCHEMAS = {
    "implementation_request": {"type": "object"},
    "implementation_result": {
        "type": "object",
        "properties": {"changed_paths": {"type": "array"}},
        "required": ["changed_paths"],
    },
}


class _FakeContext:
    now = "2026-08-16T00:00:00Z"

    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def agent(self, prompt: str, **opts):
        self.calls.append((prompt, dict(opts)))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _resolved():
    return resolve_role(
        _ROLE,
        crew_id="software-delivery",
        workflow_id="wf_role_1",
        schemas=_SCHEMAS,
    )


def test_resolve_role_maps_schema_resources_and_event_identity() -> None:
    resolved = _resolved()

    assert resolved.input_schema == _SCHEMAS["implementation_request"]
    assert resolved.output_schema == _SCHEMAS["implementation_result"]
    assert resolved.event_label == (
        "crew:software-delivery workflow:wf_role_1 role:software-engineer@0.1.0"
    )
    assert resolved.event_phase == "role:software-engineer"


def test_missing_declared_schema_fails_before_execution() -> None:
    with pytest.raises(RoleResolutionError) as caught:
        resolve_role(
            _ROLE,
            crew_id="software-delivery",
            workflow_id="wf_role_1",
            schemas={"implementation_result": _SCHEMAS["implementation_result"]},
        )

    assert caught.value.code == "role.input_schema.unavailable"


def test_execute_role_forwards_agent_and_returns_structured_handoff() -> None:
    ctx = _FakeContext({"changed_paths": ["src/example.py"]})

    invocation = asyncio.run(
        execute_role(
            ctx,
            _resolved(),
            prompt="Implement the approved change.",
            handoff_id="h_role_1",
            handoff_schema_version="1",
            source_session="session-a",
        )
    )

    assert invocation.status == ROLE_COMPLETED
    assert invocation.blocked_reason == ""
    assert invocation.handoff is not None
    assert invocation.handoff.handoff_id == "h_role_1"
    assert invocation.handoff.source_role == "software-engineer"
    assert invocation.handoff.source_session == "session-a"
    assert invocation.handoff.artifact_type == "implementation_result"
    assert invocation.handoff.schema_version == "1"
    assert invocation.handoff.quality_status == "schema_validated"
    assert invocation.handoff.payload == {"changed_paths": ["src/example.py"]}

    prompt, opts = ctx.calls[0]
    assert "Implement the approved change." in prompt
    assert "Mission: Implement an approved change" in prompt
    assert opts["agent"] == "software-engineer"
    assert opts["schema"] == _SCHEMAS["implementation_result"]
    assert opts["label"] == invocation.resolved.event_label
    assert opts["phase"] == "role:software-engineer"


def test_none_from_ctx_agent_becomes_blocked_without_handoff() -> None:
    ctx = _FakeContext(None)

    invocation = asyncio.run(
        execute_role(
            ctx,
            _resolved(),
            prompt="Try the change.",
            handoff_id="h_role_2",
            handoff_schema_version="1",
        )
    )

    assert invocation.status == ROLE_BLOCKED
    assert invocation.result is None
    assert invocation.handoff is None
    assert invocation.blocked_reason == "agent_returned_no_result"


def test_handoff_metadata_is_required_before_agent_call() -> None:
    ctx = _FakeContext({"changed_paths": []})

    with pytest.raises(RoleResolutionError) as caught:
        asyncio.run(
            execute_role(
                ctx,
                _resolved(),
                prompt="Do not run.",
                handoff_id="",
                handoff_schema_version="1",
            )
        )

    assert caught.value.code == "handoff_id.required"
    assert ctx.calls == []


def test_budget_exceeded_is_preserved_for_workflow_runner() -> None:
    ctx = _FakeContext(BudgetExceeded("budget exhausted"))

    with pytest.raises(BudgetExceeded):
        asyncio.run(
            execute_role(
                ctx,
                _resolved(),
                prompt="Budgeted work.",
                handoff_id="h_role_3",
                handoff_schema_version="1",
            )
        )

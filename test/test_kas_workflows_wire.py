"""KAS workflow wire: tolerant parsing and the run-row control rules."""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk.capabilities import UNKNOWN_BACKEND_CAPABILITIES, capabilities_for
from kiro_crew.kas_workflows import wire
from kiro_crew.kas_workflows.engine import ENGINE_CREW, ENGINE_KAS, engine_for


def test_only_kas_has_a_native_workflow_engine() -> None:
    assert capabilities_for("kas").native_workflows is True
    for backend in ("", "claude", "codex", "goose", "opencode", "nope"):
        assert capabilities_for(backend).native_workflows is False, backend
    assert UNKNOWN_BACKEND_CAPABILITIES.native_workflows is False


def test_the_engine_is_chosen_by_capability() -> None:
    assert engine_for("kas") == ENGINE_KAS
    assert engine_for("") == ENGINE_CREW
    assert engine_for("some-harness-nobody-registered") == ENGINE_CREW


def test_every_notification_is_namespaced_and_distinct_from_requests() -> None:
    for name in wire.NOTIFICATION_METHODS | wire.REQUEST_METHODS:
        assert name.startswith("_kiro/workflow/")
    assert not wire.NOTIFICATION_METHODS & wire.REQUEST_METHODS
    assert wire.NOTIFY_RECIPES_CHANGED not in wire.RUN_NOTIFICATIONS


# ── Recipes ──


def test_a_recipe_parses_with_its_tier_inputs_and_plan() -> None:
    recipe = wire.parse_recipe(
        {
            "name": "review-loop",
            "source": "/home/u/.kiro/workflows/review-loop.workflow.json",
            "description": "Code then review",
            "inputs": {"brief": "string", "count": 3},
            "plan": [
                {"type": "step", "id": "code", "agent": "wf-coder"},
                {
                    "type": "repeat",
                    "id": "loop",
                    "maxIterations": 3,
                    "children": [{"type": "step", "id": "review", "agent": "wf-code-reviewer"}],
                },
            ],
            "_meta": {"kiro": {"resource": {"source": "user-local"}}},
            "somethingNew": True,
        }
    )
    assert recipe is not None
    assert recipe.tier == "user-local"
    assert recipe.inputs == (("brief", "string"), ("count", "string"))
    assert not recipe.read_only
    payload = recipe.to_payload()
    assert payload["stepCount"] == 2
    assert payload["loop"] == {"nodeId": "loop", "maxIterations": 3}


def test_bundled_recipes_are_read_only() -> None:
    recipe = wire.parse_recipe({"name": "goal", "source": "bundled://goal"})
    assert recipe is not None and recipe.read_only


@pytest.mark.parametrize(
    "raw",
    [None, [], {"source": "bundled://x"}, {"name": "", "source": "a"}],
)
def test_a_recipe_without_a_name_is_dropped(raw: object) -> None:
    assert wire.parse_recipe(raw) is None


def test_a_validation_error_object_reads_as_its_message() -> None:
    recipe = wire.parse_recipe(
        {"name": "bad", "source": "/x.workflow.json", "validationError": {"message": "no steps"}}
    )
    assert recipe is not None and recipe.validation_error == "no steps"


def test_plan_nodes_with_unknown_types_or_no_id_are_dropped() -> None:
    plan = wire.parse_plan(
        [
            {"type": "step", "id": "a"},
            {"type": "if", "id": "b"},
            {"type": "step"},
            "junk",
            {"type": "parallel", "id": "p", "branches": [{"type": "step", "id": "c"}]},
        ]
    )
    assert [node.id for node in plan] == ["a", "p"]
    assert [node.id for node in wire.iter_steps(plan)] == ["a", "c"]


def test_a_hostile_deep_plan_does_not_recurse_without_end() -> None:
    node: dict = {"type": "step", "id": "leaf"}
    for i in range(wire.MAX_PLAN_DEPTH + 10):
        node = {"type": "sequence", "id": f"s{i}", "children": [node]}
    plan = wire.parse_plan([node])
    assert len(plan) == 1
    assert list(wire.iter_steps(plan)) == []


# ── Runs ──


def test_a_run_summary_needs_an_id_and_a_known_status() -> None:
    assert wire.parse_run_summary({"workflowId": "wf_1", "status": "exploded"}) is None
    assert wire.parse_run_summary({"status": "running"}) is None
    run = wire.parse_run_summary(
        {
            "workflowId": "wf_1",
            "status": "running",
            "workflowName": "investigate",
            "pausePending": {"initiator": "user", "requestedAt": "t"},
            "attention": [
                {"kind": "permission", "nodeId": "n1", "sessionId": "s1"},
                {"kind": "telepathy", "nodeId": "n2"},
            ],
        }
    )
    assert run is not None
    assert run.name == "investigate"
    assert run.pause_pending is True
    assert [a.node_id for a in run.attention] == ["n1"]


def test_node_paths_arrive_as_lists_or_slash_strings() -> None:
    (a,) = wire.parse_attention([{"kind": "question", "nodeId": "n", "nodePath": "loop/review"}])
    (b,) = wire.parse_attention(
        [{"kind": "question", "nodeId": "n", "nodePath": ["loop", "review"]}]
    )
    assert a.node_path == b.node_path == ("loop", "review")


# ── Requests ──


def test_a_new_run_requires_a_parent_session() -> None:
    with pytest.raises(ValueError):
        wire.new_run_params(source="bundled://goal", parent_session_id="", inputs={})
    params = wire.new_run_params(
        source="bundled://goal", parent_session_id="sess", inputs={"n": 3}, run_label="x"
    )
    assert params == {
        "workflowPath": "bundled://goal",
        "parentSessionId": "sess",
        "inputs": {"n": "3"},
        "runLabel": "x",
    }


def test_a_control_reason_is_trimmed_to_what_kas_accepts() -> None:
    params = wire.user_control_params("wf_1", "  " + "r" * 900)
    assert params["initiator"] == "user"
    assert len(params["reason"]) == wire.MAX_CONTROL_REASON_CHARS
    assert "reason" not in wire.user_control_params("wf_1", "   ")


# ── Controls ──

_ASK_OPENABLE = (wire.Attention(kind="permission", node_id="n", session_id="s"),)
_ASK_UNOPENABLE = (wire.Attention(kind="question", node_id="n"),)


@pytest.mark.parametrize(
    "status,pause_pending,attention,display,primary",
    [
        ("running", False, (), "running", "pause"),
        ("running", True, (), "pausing", None),
        ("paused", False, (), "paused", "resume"),
        # A tool approval on a running run keeps it running, with Pause (gap 7).
        ("running", False, _ASK_OPENABLE, "running", "pause"),
        ("paused", False, _ASK_OPENABLE, "paused", "open"),
        ("running", False, _ASK_UNOPENABLE, "paused", "pause"),
        ("paused", False, _ASK_UNOPENABLE, "paused", "resume"),
    ],
)
def test_the_primary_slot_follows_desktops_order(
    status: str, pause_pending: bool, attention: tuple, display: str, primary: str | None
) -> None:
    controls = wire.run_controls(status, pause_pending=pause_pending, attention=attention)
    assert (controls.display, controls.primary) == (display, primary)
    assert controls.can_stop is True
    assert controls.can_delete is False


@pytest.mark.parametrize(
    "status,display,can_retry",
    [("completed", "completed", False), ("failed", "failed", True), ("aborted", "stopped", True)],
)
def test_a_terminal_run_offers_no_primary_and_no_stop(
    status: str, display: str, can_retry: bool
) -> None:
    controls = wire.run_controls(status, pause_pending=True, attention=_ASK_OPENABLE)
    assert controls.display == display
    assert controls.primary is None
    assert controls.can_stop is False
    assert controls.can_retry is can_retry
    assert controls.can_delete is True
    assert controls.needs_attention is False


def test_a_run_time_descriptor_names_its_node_nodeid_and_its_agent_agentname():
    """KAS's ``WorkflowNodeDescriptor`` (inspect ``nodePlan``, run_start ``nodeTree``)."""
    plan = wire.parse_plan(
        [
            {"nodeId": "investigate", "type": "step", "agentName": "wf-planner"},
            {
                "nodeId": "loop",
                "type": "repeat",
                "maxIterations": 3,
                "steps": [{"nodeId": "review", "type": "step", "agentName": "wf-code-reviewer"}],
            },
        ]
    )
    assert [(n.id, n.type, n.agent) for n in plan] == [
        ("investigate", "step", "wf-planner"),
        ("loop", "repeat", None),
    ]
    assert plan[1].max_iterations == 3
    assert [(c.id, c.agent) for c in plan[1].children] == [("review", "wf-code-reviewer")]


def test_a_step_descriptor_carries_its_resolved_model_and_effort():
    [step, inherited] = wire.parse_plan(
        [
            {
                "nodeId": "a",
                "type": "step",
                "agentName": "wf-planner",
                "modelId": "claude-opus-5",
                "effortLevel": "high",
            },
            {"nodeId": "b", "type": "step", "modelId": "auto"},
        ]
    )
    assert (step.model, step.effort) == ("claude-opus-5", "high")
    assert step.to_payload()["model"] == "claude-opus-5"
    assert (inherited.model, inherited.effort) == (None, None), "'auto' means inherit"

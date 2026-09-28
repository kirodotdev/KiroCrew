"""Exercise actor/event routing and ensure malformed fleet copies are detected."""

from __future__ import annotations

import ast
import json

import pytest
import test_ci_fleet_routing_expression_parity as parity
import yaml

_REPO = "kirodotdev/KiroCrew"
_FORK = "contributor/KiroCrew"
_ACTORS = '["101", "202"]'
_EXPRESSIONS = {
    "push-pr": parity._CANONICAL_ROUTING_EXPR,
    "push": parity._CANONICAL_PUSH_EXPR,
    "push-dispatch": parity._CANONICAL_DISPATCH_EXPR,
}


def _evaluate(expression: str, context: dict[str, object]) -> object:
    """Interpret the routing subset without executing expression text as Python.

    These fixtures use concrete strings, booleans and arrays, not Actions' loose
    numeric coercion. Unknown syntax or context fails, including on a dead branch.
    This is not a general Actions interpreter or remote execution evidence.
    """
    source = expression.removeprefix("${{").removesuffix("}}").strip()
    tree = ast.parse(source.replace("&&", " and ").replace("||", " or "), mode="eval")
    functions = {
        "fromJSON": json.loads,
        "contains": lambda values, value: value in values,
        "format": lambda template, *args: template.format(*args),
    }

    def context_key(node):
        if isinstance(node, ast.Name):
            return node.id
        assert isinstance(node, ast.Attribute), ast.dump(node)
        return context_key(node.value) + "." + node.attr

    allowed = (
        ast.Expression,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.Compare,
        ast.Eq,
        ast.Constant,
        ast.Call,
        ast.Name,
        ast.Attribute,
        ast.Load,
    )
    keys = set(context)
    prefixes = {key.rsplit(".", depth)[0] for key in keys for depth in range(1, key.count(".") + 1)}
    for node in ast.walk(tree):
        assert isinstance(node, allowed), ast.dump(node)
        if isinstance(node, (ast.Attribute, ast.Name)):
            assert context_key(node) in keys | prefixes | functions.keys(), ast.dump(node)
        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name) and node.func.id in functions
            assert not node.keywords
        if isinstance(node, ast.Compare):
            assert len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Attribute):
            return context[context_key(node)]
        if isinstance(node, ast.BoolOp):
            value = visit(node.values[0])
            for operand in node.values[1:]:
                if (isinstance(node.op, ast.And) and not value) or (
                    isinstance(node.op, ast.Or) and value
                ):
                    return value
                value = visit(operand)
            return value
        if isinstance(node, ast.Compare):
            return visit(node.left) == visit(node.comparators[0])
        if isinstance(node, ast.Call):
            return functions[node.func.id](*(visit(arg) for arg in node.args))
        raise AssertionError(ast.dump(node))

    return visit(tree)


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("'' || 'hosted'", "hosted"),
        ("'fleet' || fromJSON('invalid')", "fleet"),
        ("'' && fromJSON('invalid')", ""),
        ("'a' == 'b' && 'fleet' || 'hosted'", "hosted"),
        ("contains(fromJSON('[\"123\"]'), '12')", False),
        ("contains(fromJSON('[\"123\"]'), '123')", True),
        ("format('runner-{0}-{1}', '42', '2')", "runner-42-2"),
    ],
)
def test_expression_interpreter_operand_returns_and_short_circuit(expression, expected):
    assert _evaluate(expression, {}) == expected


@pytest.mark.parametrize(
    "expression",
    ["unknown('x')", "'x'.upper()", "[x for x in 'abc']", "'x' + 'y'", "'ok' || runner.os"],
)
def test_expression_interpreter_rejects_unknown_syntax_and_context(expression):
    with pytest.raises(AssertionError):
        _evaluate(expression, {})


@pytest.mark.parametrize("policy", _EXPRESSIONS)
@pytest.mark.parametrize(
    "action",
    ["opened", "synchronize", "edited", "reopened", "labeled", "unlabeled", "ready_for_review", ""],
)
@pytest.mark.parametrize(
    "event,repo,head,actor,actors,eligible_policies",
    [
        ("push", _REPO, "", "101", _ACTORS, {"push-pr", "push", "push-dispatch"}),
        ("push", _REPO, "", "202", _ACTORS, {"push-pr", "push", "push-dispatch"}),
        ("pull_request", _REPO, _REPO, "101", _ACTORS, {"push-pr"}),
        ("workflow_dispatch", _REPO, "", "101", _ACTORS, {"push-dispatch"}),
        ("push", _FORK, "", "101", _ACTORS, set()),
        ("pull_request", _REPO, _FORK, "101", _ACTORS, set()),
        ("pull_request", _REPO, "", "101", _ACTORS, set()),
        ("pull_request", _FORK, _FORK, "101", _ACTORS, set()),
        ("push", _REPO, "", "999", _ACTORS, set()),
        ("pull_request", _REPO, _REPO, "999", _ACTORS, set()),
        ("workflow_dispatch", _REPO, "", "999", _ACTORS, set()),
        ("push", _REPO, "", "10", _ACTORS, set()),
        ("push", _REPO, "", "1010", _ACTORS, set()),
        ("push", _REPO, "", "", _ACTORS, set()),
        ("push", _REPO, "", "101", "", set()),
        ("pull_request", _REPO, _REPO, "101", "", set()),
        ("workflow_dispatch", _REPO, "", "101", "", set()),
        ("push", _REPO, "", "101", "[]", set()),
        ("schedule", _REPO, "", "101", _ACTORS, set()),
        ("issues", _REPO, "", "101", _ACTORS, set()),
        ("issue_comment", _REPO, "", "101", _ACTORS, set()),
        ("pull_request_target", _REPO, _REPO, "101", _ACTORS, set()),
        ("workflow_run", _REPO, _REPO, "101", _ACTORS, set()),
        ("workflow_call", _REPO, "", "101", _ACTORS, set()),
        ("merge_group", _REPO, "", "101", _ACTORS, set()),
    ],
)
def test_actor_and_event_truth_table(
    policy, action, event, repo, head, actor, actors, eligible_policies
):
    context = {
        "github.repository": repo,
        "github.event_name": event,
        "github.event.action": action,
        "github.event.pull_request.head.repo.full_name": head,
        "github.actor_id": actor,
        "vars.CODEBUILD_ACTOR_IDS": actors,
        "github.run_id": "456",
        "github.run_attempt": "3",
    }
    eligible = policy in eligible_policies and (
        event != "pull_request" or action in {"opened", "synchronize"}
    )
    expected = "codebuild-kirocrew-gha-linux-456-3" if eligible else "ubuntu-latest"
    assert _evaluate(_EXPRESSIONS[policy], context) == expected


@pytest.mark.parametrize("shape", ["no-actor", "wrong-fork", "literal-array", "folded"])
def test_parity_detector_rejects_corrupted_routes(tmp_path, monkeypatch, shape):
    expression = parity._CANONICAL_ROUTING_EXPR
    if shape == "wrong-fork":
        expression = expression.replace("head.repo.full_name ==", "head.repo.full_name !=")
    else:
        expression = expression.replace(parity._ACTOR_PREDICATE + " && ", "")
    value = ["codebuild-kirocrew-gha-linux-456-3"] if shape == "literal-array" else expression
    path = tmp_path / "fast-gate.yml"
    if shape == "folded":
        path.write_text(f"jobs:\n  gate:\n    runs-on: >-\n      {value}\n", encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump({"jobs": {"gate": {"runs-on": value}}}), encoding="utf-8")
    monkeypatch.setattr(parity, "_all_workflow_files", lambda: [path])
    with pytest.raises(AssertionError, match="fleet routing expression drift"):
        parity.test_every_copy_of_the_routing_expression_matches_the_canonical_one()


@pytest.mark.parametrize(
    "filename,job_id",
    [
        ("code-review.yml", "autosde-rules"),
        ("code-review.yml", "inclusive-language"),
        ("code-review.yml", "pr-hygiene"),
        ("pr-merge-conflict-label.yml", "label"),
        ("build-wheel.yml", "build-wheel"),
        ("dependency-vulnerability.yml", "audit-production-dependencies"),
    ],
)
def test_additional_routes_have_no_container_or_cloud_credential_steps(filename, job_id):
    path = parity._WORKFLOWS_DIR / filename
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    job = workflow["jobs"][job_id]
    assert job["runs-on"] == parity._expected_inline_expression(filename)
    assert "container" not in job
    assert "services" not in job
    assert job.get("permissions", workflow["permissions"]).get("id-token") != "write"
    for step in job["steps"]:
        assert "configure-aws-credentials" not in step.get("uses", "")
        assert "claude-code-action" not in step.get("uses", "")


@pytest.mark.parametrize("filename", ["build-wheel.yml", "dependency-vulnerability.yml"])
def test_reusable_routes_keep_schedule_callers_hosted(filename):
    workflow = yaml.safe_load((parity._WORKFLOWS_DIR / filename).read_text(encoding="utf-8"))
    assert "workflow_call" in workflow.get("on", workflow.get(True))
    for caller in ("nightly.yml", "release.yml"):
        parent = yaml.safe_load((parity._WORKFLOWS_DIR / caller).read_text(encoding="utf-8"))
        assert any(
            job.get("uses") == f"./.github/workflows/{filename}" for job in parent["jobs"].values()
        ), f"{caller} must exercise the reusable route"
    for job in workflow["jobs"].values():
        assert job["runs-on"] == parity._CANONICAL_PUSH_EXPR

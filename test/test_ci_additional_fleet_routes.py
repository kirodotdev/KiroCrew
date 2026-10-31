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
    # Named for its workflow rather than its events, unlike the three above:
    # exactly one workflow uses it. It is covered here and has deliberately NOT
    # moved to the larger-runner group, for the reason given in
    # test_the_ratchet_audit_route_stays_off_the_larger_runner_group below.
    # The truth table alone does NOT hold it there -- it reads its expectation
    # out of the expression under test -- so that separate assertion is what
    # does.
    "ratchet-audit": parity._CANONICAL_DISPATCH_MERGE_GROUP_EXPR,
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
        ("push", _REPO, "", "101", _ACTORS, {"push-pr", "push", "push-dispatch", "ratchet-audit"}),
        ("push", _REPO, "", "202", _ACTORS, {"push-pr", "push", "push-dispatch", "ratchet-audit"}),
        ("pull_request", _REPO, _REPO, "101", _ACTORS, {"push-pr"}),
        ("workflow_dispatch", _REPO, "", "101", _ACTORS, {"push-dispatch", "ratchet-audit"}),
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
        ("merge_group", _REPO, "", "101", _ACTORS, {"ratchet-audit"}),
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
    # The non-fleet destination is read off the policy's own expression rather
    # than hardcoded per policy, so a policy that moves to the group later is
    # covered here without touching this line, and one that has not moved still
    # asserts the hosted image. Dropping the fork branch from a moved policy
    # fails this assertion, which is the point: a group label is unreachable
    # from a fork, and a job asking for one there waits until GitHub cancels it.
    moved = parity._GROUP_THEN_HOSTED_TAIL in _EXPRESSIONS[policy]
    if eligible:
        expected = "codebuild-kirocrew-gha-linux-456-3"
    elif moved and repo == _REPO:
        expected = "kirocrew-linux-4-core"
    else:
        expected = "ubuntu-latest"
    assert _evaluate(_EXPRESSIONS[policy], context) == expected


@pytest.mark.parametrize(
    "event,repo,pr_head,run_head,actor,actors,eligible",
    [
        # A same-repository PR head from a listed actor uses the fleet.
        ("pull_request_target", _REPO, _REPO, "", "101", _ACTORS, True),
        ("pull_request_target", _REPO, _REPO, "", "202", _ACTORS, True),
        # A fork head misses the fleet and takes the group; the workflow_run
        # field never stands in.
        ("pull_request_target", _REPO, _FORK, "", "101", _ACTORS, False),
        ("pull_request_target", _REPO, "", _REPO, "101", _ACTORS, False),
        # The merge-guard hold path misses the fleet and takes the group,
        # same-repo head or not.
        ("workflow_run", _REPO, "", _REPO, "202", _ACTORS, False),
        ("workflow_run", _REPO, _REPO, _REPO, "101", _ACTORS, False),
        ("workflow_run", _REPO, "", _FORK, "101", _ACTORS, False),
        # No head in the payload: the sweep's dispatch and a manual one.
        ("workflow_dispatch", _REPO, "", "", "101", _ACTORS, False),
        ("workflow_dispatch", _REPO, "", "", "41898282", _ACTORS, False),
        # Unlisted actor, missing or empty list, or another repository.
        ("pull_request_target", _REPO, _REPO, "", "999", _ACTORS, False),
        ("workflow_run", _REPO, "", _REPO, "999", _ACTORS, False),
        ("workflow_run", _REPO, "", _REPO, "101", "", False),
        ("pull_request_target", _REPO, _REPO, "", "101", "[]", False),
        ("pull_request_target", _FORK, _FORK, "", "101", _ACTORS, False),
        ("pull_request", _REPO, _REPO, "", "101", _ACTORS, False),
    ],
)
def test_readiness_route_truth_table(event, repo, pr_head, run_head, actor, actors, eligible):
    context = {
        "github.repository": repo,
        "github.event_name": event,
        "github.event.pull_request.head.repo.full_name": pr_head,
        "github.event.workflow_run.head_repository.full_name": run_head,
        "github.actor_id": actor,
        "vars.CODEBUILD_ACTOR_IDS": actors,
        "github.run_id": "456",
        "github.run_attempt": "3",
    }
    # Pinned here as literals, deliberately NOT read off the expression under
    # test the way the policy table above does it. This lane publishes the
    # repository's one required status and its workflow_run leg carries the
    # merge-guard hold, so moving any of these destinations has to be done on
    # purpose in two places; deriving the expectation from the constant would
    # let a future edit to both carry its own expectation along. Only a run
    # executing OUTSIDE this repository reaches the hosted image, because a
    # fork's own copy cannot reach an organization runner group.
    if eligible:
        expected = "codebuild-kirocrew-gha-linux-456-3"
    elif repo == _REPO:
        expected = "kirocrew-linux-4-core"
    else:
        expected = "ubuntu-latest"
    assert _evaluate(parity._CANONICAL_READINESS_EXPR, context) == expected


def test_readiness_never_checks_out_pr_code():
    """The fleet route rests on this: every checkout is the default branch."""
    workflow = yaml.safe_load(
        (parity._WORKFLOWS_DIR / "pr-readiness.yml").read_text(encoding="utf-8")
    )
    (job,) = workflow["jobs"].values()
    checkouts = [s for s in job["steps"] if "actions/checkout" in s.get("uses", "")]
    assert checkouts, "the disposition evaluator checkout must exist"
    for step in checkouts:
        assert step["with"]["ref"] == (
            "${{ github.event.repository.default_branch || github.ref_name }}"
        )
        assert step["with"]["persist-credentials"] is False


@pytest.mark.parametrize(
    "event,repo,pr_head,actor,actors,admitted",
    [
        # A same-repository head from a listed actor uses the fleet, and the
        # review event is admitted too: this policy names no event.
        ("pull_request", _REPO, _REPO, "101", _ACTORS, True),
        ("pull_request_review", _REPO, _REPO, "202", _ACTORS, True),
        # Executing here but not admitted. On a review event the actor is the
        # REVIEWER, who is commonly outside the list, so this is the ordinary
        # path rather than an edge case.
        ("pull_request", _REPO, _REPO, "999", _ACTORS, False),
        ("pull_request_review", _REPO, _REPO, "999", _ACTORS, False),
        ("pull_request", _REPO, _REPO, "101", "", False),
        ("pull_request", _REPO, _REPO, "101", "[]", False),
        # A fork pull request against this repository executes HERE, so the
        # group is reachable even though the head is a fork's.
        ("pull_request", _REPO, _FORK, "101", _ACTORS, False),
        # A fork running this workflow in its OWN repository cannot reach an
        # organization group. Dropping the middle branch's repository guard
        # fails these two rows, which is the point: a job asking there for a
        # label no runner carries waits out GitHub's pending limit.
        ("pull_request", _FORK, _FORK, "101", _ACTORS, False),
        ("pull_request_review", _FORK, _FORK, "101", _ACTORS, False),
    ],
)
def test_sensitive_change_review_route_truth_table(event, repo, pr_head, actor, actors, admitted):
    context = {
        "github.repository": repo,
        "github.event_name": event,
        "github.event.pull_request.head.repo.full_name": pr_head,
        "github.actor_id": actor,
        "vars.CODEBUILD_ACTOR_IDS": actors,
        "github.run_id": "456",
        "github.run_attempt": "3",
    }
    # Read off the policy's own expression, the same way the shared truth table
    # above does, so rolling this policy back to the hosted tail needs no edit
    # here -- and so a dropped fork branch still fails the fork rows.
    moved = parity._GROUP_THEN_HOSTED_TAIL in parity._CANONICAL_BASE_ONLY_PR_EXPR
    if admitted:
        expected = "codebuild-kirocrew-gha-linux-456-3"
    elif moved and repo == _REPO:
        expected = "kirocrew-linux-4-core"
    else:
        expected = "ubuntu-latest"
    assert _evaluate(parity._CANONICAL_BASE_ONLY_PR_EXPR, context) == expected


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
        ("code-review.yml", "sast"),
        ("pr-merge-conflict-label.yml", "label"),
        ("build-wheel.yml", "build-wheel"),
        ("dependency-vulnerability.yml", "audit-production-dependencies"),
        ("pr-readiness.yml", "readiness"),
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


def test_the_ratchet_audit_route_stays_off_the_larger_runner_group():
    """`main-ratchet-audit.yml` keeps its hosted fallback until the merge-group
    leg is dealt with on purpose.

    `merge-queue-readiness.yml` lists that workflow in its polled set. It polls
    every merge group, counts a queued run as pending, and fails the group
    closed when its budget runs out. So routing this policy's non-fleet path to
    `kirocrew-runners` puts merge capacity behind the pull-request lanes that
    already compete for that group's cap.

    Pinned DIRECTLY, and not left to the truth table above, because that table
    reads its expected value out of the expression under test. A change that
    edits this constant and the workflow together -- which is the shape a real
    move takes -- carries the expectation along with it and passes. So the table
    catches a workflow edited alone and nothing else. Deleting this assertion is
    the deliberate act that decision ought to require.
    """
    assert parity._GROUP_THEN_HOSTED_TAIL not in parity._CANONICAL_DISPATCH_MERGE_GROUP_EXPR, (
        "main-ratchet-audit.yml's non-fleet path must stay on the hosted image: "
        "merge-queue-readiness.yml polls that workflow on every merge group and fails "
        "the group closed on a run left queued, so a capped group puts merge capacity "
        "behind the PR lanes. Settle the merge-group leg before changing this."
    )


@pytest.mark.parametrize("filename", ["build-wheel.yml", "dependency-vulnerability.yml"])
def test_reusable_routes_keep_schedule_callers_off_the_fleet(filename):
    workflow = yaml.safe_load((parity._WORKFLOWS_DIR / filename).read_text(encoding="utf-8"))
    assert "workflow_call" in workflow.get("on", workflow.get(True))
    for caller in ("nightly.yml", "release.yml"):
        parent = yaml.safe_load((parity._WORKFLOWS_DIR / caller).read_text(encoding="utf-8"))
        assert any(
            job.get("uses") == f"./.github/workflows/{filename}" for job in parent["jobs"].values()
        ), f"{caller} must exercise the reusable route"
    for job in workflow["jobs"].values():
        assert job["runs-on"] == parity._CANONICAL_PUSH_EXPR

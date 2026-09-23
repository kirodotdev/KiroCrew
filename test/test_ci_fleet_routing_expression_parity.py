"""Pin every inline fleet route and resolver consumer to its event policy.

Actor admission here is an availability decision: an unlisted actor or missing
repository variable gets a hosted runner, not a queued job the webhook rejects.
AWS webhook filtering remains the security boundary. Fast Gate computes this
inline because its gates must not depend on another job or skip themselves.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

_ACTOR_PREDICATE = "contains(fromJSON(vars.CODEBUILD_ACTOR_IDS || '[]'), github.actor_id)"
_CANONICAL_ROUTING_EXPR = (
    "${{ github.repository == 'kirodotdev/KiroCrew' && "
    + _ACTOR_PREDICATE
    + " && (github.event_name == 'push' || (github.event_name == 'pull_request' && "
    "(github.event.action == 'opened' || github.event.action == 'synchronize') && "
    "github.event.pull_request.head.repo.full_name == github.repository)) && "
    "format('codebuild-kirocrew-gha-linux-{0}-{1}', github.run_id, github.run_attempt) "
    "|| 'ubuntu-latest' }}"
)
_CANONICAL_PUSH_EXPR = (
    "${{ github.repository == 'kirodotdev/KiroCrew' && "
    + _ACTOR_PREDICATE
    + " && github.event_name == 'push' && "
    "format('codebuild-kirocrew-gha-linux-{0}-{1}', github.run_id, github.run_attempt) "
    "|| 'ubuntu-latest' }}"
)
_CANONICAL_DISPATCH_EXPR = (
    "${{ github.repository == 'kirodotdev/KiroCrew' && "
    + _ACTOR_PREDICATE
    + " && (github.event_name == 'push' || github.event_name == 'workflow_dispatch') && "
    "format('codebuild-kirocrew-gha-linux-{0}-{1}', github.run_id, github.run_attempt) "
    "|| 'ubuntu-latest' }}"
)

# These lanes remain hosted: untrusted-content model execution, schedule/issue
# triggers, or an isolation contract tied to hosted paths. The scope-review
# generate job's action-code Write fence is one such path-sensitive contract.
_PERMANENT_EXCEPTIONS = {
    ("security-scope-review.yml", "generate"),
    ("security-scope-review.yml", "validate"),
    ("security-scope-review.yml", "publish"),
    ("issue-summary.yml", "summarize"),
    ("issue-triage.yml", "triage"),
    ("ai-review-human-override.yml", "record"),
    ("disposition-deferral-check.yml", "validate-deferral"),
    ("nightly.yml", "version"),
    ("connections-l0.yml", "probe"),
    ("memory-benchmark.yml", "accept"),
    ("fix-loop-analysis.yml", "metrics"),
    ("fix-loop-analysis.yml", "analyze"),
    ("deferred-findings-audit.yml", "audit"),
    ("add-contributor.yml", "add"),
    ("ship-report.yml", "report"),
    ("first-principles-review.yml", "first-principles-review"),
    ("ux-review.yml", "ux-review"),
    ("design-review.yml", "design-review"),
    ("code-review.yml", "sast"),
    ("ci-runner-watchdog.yml", "watchdog"),
}

# Fixed expectations, never inferred from the workflow contents: a route
# silently removed or a new unreviewed fleet job must fail the inventory check.
_EXPECTED_ROUTED_JOBS = {
    ("fast-gate.yml", "vendor-manifest"),
    ("fast-gate.yml", "brand-lint"),
    ("fast-gate.yml", "comment-history-lint"),
    ("fast-gate.yml", "focus-cue-lint"),
    ("fast-gate.yml", "feature-map-lint"),
    ("fast-gate.yml", "changelog-history"),
    ("fast-gate.yml", "decision-ledger-history"),
    ("fast-gate.yml", "builtin-skill-scope"),
    ("fast-gate.yml", "loop-bound-locks"),
    ("fast-gate.yml", "testpaths-coverage"),
    ("fast-gate.yml", "harness-parity"),
    ("fast-gate.yml", "memory-store-seam"),
    ("fast-gate.yml", "docs-lint"),
    ("build.yml", "build-wheel"),
    ("build.yml", "desktop-matrix"),
    ("main-ratchet-audit.yml", "ratchet-gates"),
    ("main-ratchet-audit.yml", "frontend-ceiling"),
    ("main-ratchet-audit.yml", "report"),
    ("release.yml", "version"),
    ("release.yml", "resolve-promotion"),
    ("release.yml", "stable-gate"),
    ("release.yml", "github-release"),
    ("release.yml", "record-promotion"),
    ("pages.yml", "build"),
    ("pages.yml", "deploy"),
    ("cross-platform.yml", "cross-platform"),
    ("dependency-review.yml", "license-gate"),
    ("pr-scope.yml", "pr-scope"),
    ("screenshot-evidence.yml", "screenshot-evidence"),
    ("macos-on-demand.yml", "decide"),
    ("ci.yml", "changes"),
    ("ci.yml", "await-fast-gate"),
    ("code-review.yml", "autosde-rules"),
    ("code-review.yml", "inclusive-language"),
    ("code-review.yml", "pr-hygiene"),
    ("pr-merge-conflict-label.yml", "label"),
    ("build-wheel.yml", "build-wheel"),
    ("dependency-vulnerability.yml", "audit-production-dependencies"),
}
_PUSH_ONLY_WORKFLOWS = {
    "release.yml",
    "pr-merge-conflict-label.yml",
    "build-wheel.yml",
    "dependency-vulnerability.yml",
}
_DISPATCH_WORKFLOWS = {"pages.yml", "main-ratchet-audit.yml"}

# Resolver consumers pin their complete expressions, including hosted fallbacks.
# Backend shards, backend lint, frontend tests and bundle size use large Linux
# compute; Windows and boot-matrix consumers pin their respective OS mappings.
# A literal fleet label must never bypass the shared actor/event/fork admission.
_CANONICAL_CONSUMER_EXPR = "${{ needs.changes.outputs.linux_runner || 'ubuntu-latest' }}"
_CANONICAL_CONSUMER_EXPR_LARGE = (
    "${{ needs.changes.outputs.linux_runner_large || 'ubuntu-latest' }}"
)
_CANONICAL_WINDOWS_CONSUMER_EXPR = "${{ needs.changes.outputs.windows_runner || 'windows-latest' }}"
_CANONICAL_BOOT_MATRIX_EXPR = (
    "${{ matrix.os == 'ubuntu-latest' && "
    "(needs.changes.outputs.linux_runner_large || 'ubuntu-latest') || "
    "matrix.os == 'windows-latest' && "
    "(needs.changes.outputs.windows_runner || 'windows-latest') || matrix.os }}"
)
_EXPECTED_RESOLVER_CONSUMER_JOBS = {
    ("ci.yml", "backend-lint"): _CANONICAL_CONSUMER_EXPR_LARGE,
    ("ci.yml", "backend-test"): _CANONICAL_CONSUMER_EXPR_LARGE,
    ("ci.yml", "backend-test-windows"): _CANONICAL_WINDOWS_CONSUMER_EXPR,
    ("ci.yml", "backend-test-windows-fail-closed"): _CANONICAL_WINDOWS_CONSUMER_EXPR,
    ("ci.yml", "backend-test-crew-container"): _CANONICAL_CONSUMER_EXPR,
    ("ci.yml", "coverage-combine"): _CANONICAL_CONSUMER_EXPR,
    ("ci.yml", "coverage-gate"): _CANONICAL_CONSUMER_EXPR,
    ("ci.yml", "frontend-lint"): _CANONICAL_CONSUMER_EXPR,
    ("ci.yml", "lockfile-engines-floor"): _CANONICAL_CONSUMER_EXPR,
    ("ci.yml", "cfn-lint"): _CANONICAL_CONSUMER_EXPR,
    ("ci.yml", "electron-test"): _CANONICAL_CONSUMER_EXPR,
    ("ci.yml", "electron-test-windows"): _CANONICAL_WINDOWS_CONSUMER_EXPR,
    ("ci.yml", "frontend-test"): _CANONICAL_CONSUMER_EXPR_LARGE,
    ("ci.yml", "frontend-coverage-merge"): _CANONICAL_CONSUMER_EXPR,
    ("ci.yml", "bundle-size"): _CANONICAL_CONSUMER_EXPR_LARGE,
    ("ci.yml", "e2e"): _CANONICAL_CONSUMER_EXPR_LARGE,
    ("ci.yml", "e2e-boot-matrix"): _CANONICAL_BOOT_MATRIX_EXPR,
    ("ci.yml", "real-adapter-contract"): _CANONICAL_CONSUMER_EXPR,
}


def _all_workflow_files() -> list[Path]:
    return sorted(_WORKFLOWS_DIR.glob("*.yml"))


def _expected_inline_expression(workflow_name: str) -> str:
    if workflow_name in _PUSH_ONLY_WORKFLOWS:
        return _CANONICAL_PUSH_EXPR
    if workflow_name in _DISPATCH_WORKFLOWS:
        return _CANONICAL_DISPATCH_EXPR
    return _CANONICAL_ROUTING_EXPR


def test_every_copy_of_the_routing_expression_matches_the_canonical_one() -> None:
    drifted: list[str] = []
    found_any = False
    for path in _all_workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, spec in workflow["jobs"].items():
            value = spec.get("runs-on")
            # Parse the value, not a line regex: folded YAML and label arrays
            # must not hide a fleet route from this check. Anchor on the fleet
            # prefix, never on the predicate whose correctness we are checking.
            if "codebuild-" not in str(value):
                continue
            found_any = True
            if value != _expected_inline_expression(path.name):
                drifted.append(f"{path.name}:{job_id}: {value}")
    assert found_any, "no fleet routes found; the inventory must not pass vacuously"
    assert not drifted, "fleet routing expression drift:\n" + "\n".join(drifted)


def test_every_job_using_the_routing_expression_is_accounted_for() -> None:
    """Both removed routes and unreviewed additions fail, as do missing exceptions."""
    routed: set[tuple[str, str]] = set()
    exception_runs_on: dict[tuple[str, str], object] = {}
    for path in _all_workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, spec in workflow["jobs"].items():
            key = (path.name, job_id)
            runs_on = spec.get("runs-on")
            if "codebuild-" in str(runs_on):
                routed.add(key)
            if key in _PERMANENT_EXCEPTIONS:
                exception_runs_on[key] = runs_on

    assert routed == _EXPECTED_ROUTED_JOBS, (
        f"missing routes: {_EXPECTED_ROUTED_JOBS - routed}; "
        f"unexpected routes: {routed - _EXPECTED_ROUTED_JOBS}"
    )
    assert (
        set(exception_runs_on) == _PERMANENT_EXCEPTIONS
    ), f"missing hosted exceptions: {_PERMANENT_EXCEPTIONS - set(exception_runs_on)}"
    wrong_runner = {
        key: value for key, value in exception_runs_on.items() if value != "ubuntu-latest"
    }
    assert not wrong_runner, f"hosted exception changed runner: {wrong_runner}"


def test_every_ci_yml_resolver_consumer_reads_the_resolver_not_a_literal() -> None:
    """Pin the complete Linux/Windows consumer set, tiers and empty-output fallback."""
    workflow = yaml.safe_load((_WORKFLOWS_DIR / "ci.yml").read_text(encoding="utf-8"))
    observed_consumers = {
        ("ci.yml", job_id): spec["runs-on"]
        for job_id, spec in workflow["jobs"].items()
        if any(
            f"needs.changes.outputs.{os_name}_runner" in str(spec.get("runs-on"))
            for os_name in ("linux", "windows")
        )
    }
    assert observed_consumers == _EXPECTED_RESOLVER_CONSUMER_JOBS


def test_all_fast_gates_remain_unconditional() -> None:
    workflow = yaml.safe_load((_WORKFLOWS_DIR / "fast-gate.yml").read_text(encoding="utf-8"))
    assert workflow["jobs"]
    for job_id, spec in workflow["jobs"].items():
        assert "needs" not in spec, job_id
        assert "if" not in spec, job_id
        assert spec["runs-on"] == _CANONICAL_ROUTING_EXPR, job_id


def test_ci_resolvers_share_actor_policy_and_emit_large_labels() -> None:
    workflow = yaml.safe_load((_WORKFLOWS_DIR / "ci.yml").read_text(encoding="utf-8"))
    changes = workflow["jobs"]["changes"]
    expected_eligibility = _CANONICAL_ROUTING_EXPR.split(" && format(", 1)[0] + " }}"
    steps = {step.get("id"): step for step in changes["steps"]}
    for step_id, output, os_name in (
        ("runner", "linux_runner_large", "linux"),
        ("windows-runner", "windows_runner", "windows"),
    ):
        step = steps[step_id]
        assert step["env"]["ELIGIBLE"] == expected_eligibility
        assert changes["outputs"][output] == f"${{{{ steps.{step_id}.outputs.{output} }}}}"
        assert step["env"]["LABEL"] == (
            f"codebuild-kirocrew-gha-{os_name}-${{{{ github.run_id }}}}-"
            "${{ github.run_attempt }}"
        )
        script = step["run"]
        assert 'if [ "$ELIGIBLE" = "true" ]; then' in script
        assert f'echo "{output}=$LABEL instance-size:large"' in script
        hosted = "ubuntu-latest" if os_name == "linux" else "windows-latest"
        assert f'echo "{output}={hosted}"' in script

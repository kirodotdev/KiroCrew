"""Pin every inline fleet route and resolver consumer to its event policy.

Actor admission here is an availability decision: an unlisted actor or missing
repository variable gets a hosted runner, not a queued job the webhook rejects.
AWS webhook filtering remains the security boundary. Fast Gate computes this
inline because its gates must not depend on another job, and the only condition
they carry is the merge-queue push skip pinned below.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

# The one `if:` a Fast Gate job carries: skip a push to main only while the
# repository variable MERGE_QUEUE_ENABLED is 'true', when the merge group
# already ran the gate on that exact tree.
_FAST_GATE_PUSH_SKIP_CLAUSE = "github.event_name != 'push' || vars.MERGE_QUEUE_ENABLED != 'true'"

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
# The workflows the merge queue runs. A merge_group run's actor is the person
# who queued the pull request, so the same actor predicate admits it; the event
# is named so a merge group's jobs reach the fleet instead of falling to hosted.
_CANONICAL_MERGE_GROUP_ROUTING_EXPR = (
    "${{ github.repository == 'kirodotdev/KiroCrew' && "
    + _ACTOR_PREDICATE
    + " && (github.event_name == 'push' || github.event_name == 'merge_group' || "
    "(github.event_name == 'pull_request' && "
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
# The sensitive-change gate runs only default-branch scripts, never PR code, so
# every pull_request and pull_request_review event may use the fleet.
_CANONICAL_BASE_ONLY_PR_EXPR = (
    "${{ github.repository == 'kirodotdev/KiroCrew' && "
    + _ACTOR_PREDICATE
    + " && github.event.pull_request.head.repo.full_name == github.repository && "
    "format('codebuild-kirocrew-gha-linux-{0}-{1}', github.run_id, github.run_attempt) "
    "|| 'ubuntu-latest' }}"
)
# The ratchet audit: push, manual dispatch, and the merge group, which is the
# integrated tree one step before it lands.
_CANONICAL_DISPATCH_MERGE_GROUP_EXPR = (
    "${{ github.repository == 'kirodotdev/KiroCrew' && "
    + _ACTOR_PREDICATE
    + " && (github.event_name == 'push' || github.event_name == 'workflow_dispatch' || "
    "github.event_name == 'merge_group') && "
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
    ("fast-gate.yml", "cwd-relative-repo-reads"),
    ("fast-gate.yml", "harness-parity"),
    ("fast-gate.yml", "memory-store-seam"),
    ("fast-gate.yml", "docs-lint"),
    ("fast-gate.yml", "static-ratchets"),
    ("build.yml", "build-wheel"),
    ("build.yml", "desktop-matrix"),
    ("main-ratchet-audit.yml", "ratchet-gates"),
    ("main-ratchet-audit.yml", "frontend-ceiling"),
    ("main-ratchet-audit.yml", "bundle-ceiling"),
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
    ("sensitive-change-review.yml", "sensitive-change-review"),
}
_PUSH_ONLY_WORKFLOWS = {
    "release.yml",
    "pr-merge-conflict-label.yml",
    "build-wheel.yml",
    "dependency-vulnerability.yml",
}
_DISPATCH_WORKFLOWS = {"pages.yml"}
_BASE_ONLY_PR_WORKFLOWS = {"sensitive-change-review.yml"}
_DISPATCH_MERGE_GROUP_WORKFLOWS = {"main-ratchet-audit.yml"}
# The fleet-routed workflows that declare a `merge_group` trigger.
_MERGE_GROUP_WORKFLOWS = {"ci.yml", "fast-gate.yml", "build.yml"}
# merge_group-triggered workflows with no fleet route: the queue's required
# check (a hosted poll), the OIDC content scan (a reusable-workflow call) and
# the hosted Python client suite.
_MERGE_GROUP_HOSTED_WORKFLOWS = {
    "merge-queue-readiness.yml",
    "internal-content-scan-gate.yml",
    "client-py.yml",
}

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
    ("ci.yml", "integration"): _CANONICAL_CONSUMER_EXPR_LARGE,
    ("ci.yml", "e2e-boot-matrix"): _CANONICAL_BOOT_MATRIX_EXPR,
    ("ci.yml", "real-adapter-contract"): _CANONICAL_CONSUMER_EXPR,
}


def _all_workflow_files() -> list[Path]:
    return sorted(_WORKFLOWS_DIR.glob("*.yml"))


def _expected_inline_expression(workflow_name: str) -> str:
    if workflow_name in _PUSH_ONLY_WORKFLOWS:
        return _CANONICAL_PUSH_EXPR
    if workflow_name in _BASE_ONLY_PR_WORKFLOWS:
        return _CANONICAL_BASE_ONLY_PR_EXPR
    if workflow_name in _DISPATCH_WORKFLOWS:
        return _CANONICAL_DISPATCH_EXPR
    if workflow_name in _DISPATCH_MERGE_GROUP_WORKFLOWS:
        return _CANONICAL_DISPATCH_MERGE_GROUP_EXPR
    if workflow_name in _MERGE_GROUP_WORKFLOWS:
        return _CANONICAL_MERGE_GROUP_ROUTING_EXPR
    return _CANONICAL_ROUTING_EXPR


def test_merge_group_trigger_parity() -> None:
    """A workflow names merge_group in its routing iff it is triggered by it.

    The routing expression admits `merge_group` only where the event can occur;
    a workflow triggered by merge_group but routed with the plain expression
    would send every merge group's jobs to hosted runners, and one routed for
    merge_group without the trigger claims an event it never receives.
    """
    triggered: set[str] = set()
    for path in _all_workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = workflow.get(True, workflow.get("on"))
        if isinstance(triggers, dict) and "merge_group" in triggers:
            triggered.add(path.name)
    assert triggered == (
        _MERGE_GROUP_WORKFLOWS | _DISPATCH_MERGE_GROUP_WORKFLOWS | _MERGE_GROUP_HOSTED_WORKFLOWS
    )


def test_merge_queue_readiness_polls_every_merge_group_workflow() -> None:
    """The queue's required check waits on exactly the workflows the queue runs.

    A workflow that gains the `merge_group` trigger without joining the poll
    would run on the group and still be ignored by its verdict; one polled
    without the trigger would never appear and fail every group at the appear
    window. The env is the one place the list lives in code, so it is pinned
    to the trigger set here rather than restated.
    """
    workflow = yaml.safe_load(
        (_WORKFLOWS_DIR / "merge-queue-readiness.yml").read_text(encoding="utf-8")
    )
    (job,) = workflow["jobs"].values()
    assert job["name"] == "PR Readiness", "the job name is the ruleset's required check"
    (step,) = job["steps"]
    polled = set(step["env"]["WORKFLOWS"].split())
    triggered = {
        path.name
        for path in _all_workflow_files()
        if "merge_group"
        in (
            (lambda w: w.get(True, w.get("on")) or {})(
                yaml.safe_load(path.read_text(encoding="utf-8"))
            )
        )
    }
    assert polled == triggered - {"merge-queue-readiness.yml"}


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


def test_all_fast_gates_skip_only_the_queued_push() -> None:
    """Every gate keeps the fleet route and carries exactly one condition.

    The one `if:` a gate may carry is the push/variable clause: with the merge
    queue on, a push to main already had every gate run on its merge group, so
    the push run skips whole and holds no fleet job an orphan could sit on. Pinned
    by equality so an extra term cannot dodge a gate on a PR or a merge group.
    """
    workflow = yaml.safe_load((_WORKFLOWS_DIR / "fast-gate.yml").read_text(encoding="utf-8"))
    assert workflow["jobs"]
    for job_id, spec in workflow["jobs"].items():
        assert "needs" not in spec, job_id
        assert spec.get("if") == _FAST_GATE_PUSH_SKIP_CLAUSE, job_id
        assert spec["runs-on"] == _CANONICAL_MERGE_GROUP_ROUTING_EXPR, job_id


def test_ci_resolvers_share_actor_policy_and_emit_large_labels() -> None:
    workflow = yaml.safe_load((_WORKFLOWS_DIR / "ci.yml").read_text(encoding="utf-8"))
    changes = workflow["jobs"]["changes"]
    expected_eligibility = _CANONICAL_MERGE_GROUP_ROUTING_EXPR.split(" && format(", 1)[0] + " }}"
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


def test_merge_queue_readiness_budget_covers_ci_critical_path() -> None:
    """The poll's TOTAL_BUDGET must outlast the slowest chain of ci.yml job caps.

    The budget is a ceiling on how long a green merge group may take before the
    queue's required check gives up on it. ci.yml's longest `needs` chain of
    `timeout-minutes` is that ceiling's floor: raise a shard cap without raising
    the budget and a slow-but-green group is dequeued. The job's own
    `timeout-minutes` must in turn exceed the budget, so the step's error -- not
    the job cap -- is what names the lane still pending.
    """
    import re

    readiness = yaml.safe_load(
        (_WORKFLOWS_DIR / "merge-queue-readiness.yml").read_text(encoding="utf-8")
    )
    (job,) = readiness["jobs"].values()
    (step,) = job["steps"]
    match = re.search(r"^\s*TOTAL_BUDGET=(\d+)\s*$", step["run"], re.MULTILINE)
    assert match, "TOTAL_BUDGET must be a literal integer assignment in the poll"
    budget_minutes = int(match.group(1)) / 60

    ci = yaml.safe_load((_WORKFLOWS_DIR / "ci.yml").read_text(encoding="utf-8"))
    jobs = ci["jobs"]

    def longest_path(job_id: str) -> int:
        spec = jobs[job_id]
        needs = spec.get("needs", [])
        needs = [needs] if isinstance(needs, str) else list(needs)
        upstream = max((longest_path(n) for n in needs), default=0)
        return upstream + int(spec["timeout-minutes"])

    critical_path = max(longest_path(job_id) for job_id in jobs)
    assert budget_minutes >= critical_path, (
        f"TOTAL_BUDGET is {budget_minutes:.0f} min but ci.yml's longest needs-chain "
        f"of timeout-minutes is {critical_path} min; raise the budget with the cap"
    )
    assert int(job["timeout-minutes"]) > budget_minutes


def _readiness_job() -> dict:
    workflow = yaml.safe_load(
        (_WORKFLOWS_DIR / "merge-queue-readiness.yml").read_text(encoding="utf-8")
    )
    (job,) = workflow["jobs"].values()
    return job


def test_merge_queue_readiness_rollout_values() -> None:
    """The poll's budget and job cap are the rollout's numbers.

    The ruleset's status-check timeout (180 minutes) is set from these, so a
    change here is a change to the documented rollout and must move with it.
    The poll has no rerun cap because it never reruns: a rerun inside the queue
    holds every group behind this one for the rerun's whole duration.
    """
    import re

    job = _readiness_job()
    (step,) = job["steps"]
    values = dict(re.findall(r"^\s*(TOTAL_BUDGET|MAX_RERUNS)=(\d+)\s*$", step["run"], re.MULTILINE))
    assert values == {"TOTAL_BUDGET": "7200"}
    assert job["timeout-minutes"] == 125


def test_merge_queue_readiness_reads_runs_and_never_reruns_them() -> None:
    """The poll holds read scope only and issues no rerun.

    A rerun's whole duration is time the groups queued behind this one spend
    waiting, so a failed lane is a verdict, not a retry. With no rerun to issue
    the job needs no write scope on Actions.
    """
    job = _readiness_job()
    assert job["permissions"] == {"actions": "read", "contents": "read"}
    (step,) = job["steps"]
    assert "gh run rerun" not in step["run"], "the poll must not rerun a failed run"


_FAKE_GH = r"""
import json, sys, time
from pathlib import Path

root = Path(sys.argv[1])
args = sys.argv[2:]
scenario = json.loads((root / "scenario.json").read_text())
options = scenario.pop("_options", {})
state_file = root / "state.json"
state = json.loads(state_file.read_text()) if state_file.exists() else {}
ids = {wf: 1000 + i for i, wf in enumerate(sorted(scenario))}
with (root / "calls.log").open("a") as log:
    log.write(" ".join(args) + "\n")

def save():
    state_file.write_text(json.dumps(state))

def wf_state(wf):
    # `running_ticks` lists the lane as in_progress for that many listings
    # before its first conclusion is shown.
    return state.setdefault(
        wf, {"attempt": 1, "running": scenario[wf].get("running_ticks", 0)}
    )

if args[:2] == ["api", "rate_limit"]:
    print(int(time.time()) + options.get("reset_in", 5))
    sys.exit(0)

if args[:2] == ["run", "rerun"]:
    # The poll must never issue one; the log is the evidence that it did not.
    with (root / "reruns.log").open("a") as log:
        log.write(" ".join(args) + "\n")
    sys.exit(0)

listed = state.get("_listings", 0)
state["_listings"] = listed + 1
if listed < options.get("rate_limited_listings", 0):
    save()
    print("gh: API rate limit exceeded for installation. (HTTP 403)", file=sys.stderr)
    sys.exit(1)
runs = []
for wf in scenario:
    st = wf_state(wf)
    status = "completed"
    if st["running"] > 0:
        status, st["running"] = "in_progress", st["running"] - 1
    runs.append({
        "id": ids[wf], "path": f".github/workflows/{wf}",
        "head_branch": "gh-readonly-queue/main/pr-1-abc-as-the-api-spells-it",
        "status": status, "run_attempt": st["attempt"],
        "conclusion": scenario[wf]["conclusion"] if status == "completed" else None,
        "html_url": f"https://example.invalid/runs/{ids[wf]}",
    })
save()
print(json.dumps({"workflow_runs": runs}))
"""


def _run_readiness(
    tmp_path: Path, scenario: dict[str, dict], options: dict | None = None
) -> tuple[int, str, list[str]]:
    """Run the real poll step against a fake `gh` that plays out `scenario`.

    Each workflow names the conclusion its one attempt reaches and how many
    listings it is shown in_progress first; `sleep` is a no-op so the ticks run
    back to back.
    """
    import json
    import subprocess
    import sys

    job = _readiness_job()
    (step,) = job["steps"]
    workflows = step["env"]["WORKFLOWS"].split()
    full: dict = {wf: scenario.get(wf, {"conclusion": "success"}) for wf in workflows}
    full["_options"] = options or {}
    (tmp_path / "scenario.json").write_text(json.dumps(full))
    (tmp_path / "fake_gh.py").write_text(_FAKE_GH)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path / "fake_gh.py"}" "{tmp_path}" "$@"\n'
    )
    (bin_dir / "sleep").write_text(f'#!/bin/sh\necho "$1" >> "{tmp_path / "sleeps.log"}"\n')
    for tool in ("gh", "sleep"):
        (bin_dir / tool).chmod(0o755)
    # cwd, TMPDIR and HOME inside tmp_path, so nothing the step writes (its
    # mktemp error file, a gh config, a relative path a later edit adds) lands
    # outside the test's own directory.
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "HOME": str(tmp_path),
        "REPO": "kirodotdev/KiroCrew",
        "SHA": "abc",
        "BRANCH": "gh-readonly-queue/main/pr-1-abc",
        "WORKFLOWS": step["env"]["WORKFLOWS"],
    }
    proc = subprocess.run(
        ["bash", "-c", step["run"]],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    log = tmp_path / "reruns.log"
    reruns = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, proc.stdout + proc.stderr, reruns


_needs_bash_and_jq = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("jq") is None,
    reason="executes the poll step; needs a POSIX bash and jq on PATH",
)


@_needs_bash_and_jq
def test_a_failed_lane_fails_the_group_on_the_tick_that_reads_it(tmp_path: Path) -> None:
    code, out, reruns = _run_readiness(tmp_path, {"ci.yml": {"conclusion": "failure"}})
    assert code == 1, out
    assert reruns == [], reruns
    assert "::error::ci.yml concluded 'failure'" in out and "not cleared to merge" in out
    assert "re-queue the pull request" in out
    # One listing: the verdict was final on the first tick, so nothing slept.
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert len([c for c in calls if "actions/runs" in c]) == 1, calls
    assert not (tmp_path / "sleeps.log").exists()


@_needs_bash_and_jq
def test_a_failed_lane_does_not_wait_for_the_lanes_still_running(tmp_path: Path) -> None:
    """A group with one red lane is ejected while its other lanes still run.

    Every tick the group stays queued is a tick every group behind it waits,
    so the first verdict is reported without awaiting the rest.
    """
    code, out, reruns = _run_readiness(
        tmp_path,
        {
            "build.yml": {"conclusion": "timed_out"},
            "ci.yml": {"conclusion": "success", "running_ticks": 5},
        },
    )
    assert code == 1, out
    assert reruns == [], reruns
    assert "::error::build.yml concluded 'timed_out'" in out
    assert "t+" not in out, "the poll ticked on after reading a verdict"


@_needs_bash_and_jq
def test_a_failed_fast_gate_is_its_own_verdict(tmp_path: Path) -> None:
    """A red Fast Gate fails the group at once; CI's dependent failure is not awaited."""
    code, out, reruns = _run_readiness(
        tmp_path,
        {
            "fast-gate.yml": {"conclusion": "failure"},
            "ci.yml": {"conclusion": "failure", "running_ticks": 3},
        },
    )
    assert code == 1, out
    assert reruns == [], reruns
    assert "::error::fast-gate.yml concluded 'failure'" in out
    assert "after-fast-gate" not in out


@_needs_bash_and_jq
def test_a_slow_green_group_is_cleared(tmp_path: Path) -> None:
    code, out, reruns = _run_readiness(
        tmp_path, {"ci.yml": {"conclusion": "success", "running_ticks": 2}}
    )
    assert code == 0, out
    assert reruns == [], reruns
    assert "Every polled workflow succeeded" in out


def test_merge_queue_readiness_reads_one_listing_per_tick() -> None:
    """One combined runs listing serves every polled workflow.

    The installation token is shared by every workflow in the repository, and
    40 queued groups each listing six workflows on their own would starve it.
    """
    import re

    job = _readiness_job()
    (step,) = job["steps"]
    run = step["run"]
    assert "actions/workflows/" not in run, "a per-workflow runs listing is back"
    assert len(re.findall(r'gh api --method GET "repos/\$REPO/actions/runs"', run)) == 1
    ticks = dict(
        re.findall(r"^\s*(APPEAR_TICK|SEEN_TICK|APPEAR_BUDGET)=(\d+)\s*$", run, re.MULTILINE)
    )
    assert ticks == {"APPEAR_TICK": "60", "SEEN_TICK": "180", "APPEAR_BUDGET": "300"}


@_needs_bash_and_jq
def test_a_green_group_costs_one_listing_per_tick(tmp_path: Path) -> None:
    code, out, _ = _run_readiness(
        tmp_path, {"ci.yml": {"conclusion": "success", "running_ticks": 1}}
    )
    assert code == 0, out
    calls = (tmp_path / "calls.log").read_text().splitlines()
    listings = [c for c in calls if "actions/runs" in c]
    ticks = (tmp_path / "sleeps.log").read_text().split()
    # Every tick but the last sleeps, and each tick read exactly one listing.
    assert len(listings) == len(ticks) + 1, (calls, ticks)
    assert set(ticks) == {"180"}, ticks


@_needs_bash_and_jq
def test_a_rate_limited_listing_backs_off_until_the_reset(tmp_path: Path) -> None:
    code, out, reruns = _run_readiness(tmp_path, {}, {"rate_limited_listings": 2, "reset_in": 400})
    assert code == 0, out
    assert reruns == []
    assert out.count("::warning::API rate limit hit; it resets at epoch") == 2, out
    sleeps = [int(x) for x in (tmp_path / "sleeps.log").read_text().split()]
    # Two backoffs to the reset plus at most 30 s of jitter; neither is a verdict.
    assert len(sleeps) == 2 and all(399 <= s <= 431 for s in sleeps), sleeps

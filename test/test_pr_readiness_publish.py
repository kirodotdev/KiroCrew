"""Behavioural tests for the publish step of .github/workflows/pr-readiness.yml.

`PR Readiness` is the only required status check on protected branches, so the
commit status this step POSTs is the verdict the repository actually gates on.
The labels beside it are advisory decoration.

Two runs of this workflow can evaluate the SAME revision concurrently: every
`pull_request_target` run gets its own concurrency group keyed on `run_id`
(deliberately, so a superseded run never shows as cancelled), which is correct
for two DIFFERENT revisions and leaves two runs on one revision racing. When
that race is lost on a label call, the step must still publish the verdict --
otherwise a cosmetic 404 decides whether the required check reports anything.

These tests extract the step's shell and execute it for real against a `gh`
stub, so the ordering and the tolerance are verified rather than assumed.
Mirrors the harness in test_pr_readiness_sweep.py.

Skipped where the POSIX toolchain the script needs (bash, jq) is unavailable,
which is the case on the Windows leg of the matrix.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pr-readiness.yml"
)

PUBLISH_STEP = "Publish status and label"
CLEANUP_STEP = "Remove readiness labels from closed pull request"

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None,
    reason="requires the workflow plus a POSIX bash and jq",
)

SHA = "4328fd0f941f09ff10f245fbdb4accf7c246febe"
READ_FAILURE_TOKEN = "[read-failed]"

# `gh` stub. Every call is RECORDED to $FIXTURES/calls.txt so ordering can be
# asserted, and any call whose recorded name appears in $FIXTURES/fail_* is made
# to fail the way the real API fails that race.
GH_STUB = r"""#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >> "$FIXTURES/calls.txt"

# gh pr view <PR> --repo <REPO> --json headRefOid --jq .headRefOid
if [ "$1 ${2:-}" = "pr view" ]; then
  cat "$FIXTURES/head_sha.txt"
  exit 0
fi

if [ "$1 ${2:-}" = "label list" ]; then
  cat "$FIXTURES/repo_labels.txt"
  exit 0
fi

if [ "$1 ${2:-}" = "label create" ]; then
  if [ -f "$FIXTURES/fail_label_create" ]; then
    echo 'HTTP 422: Validation Failed (https://api.github.com/repos/o/r/labels)' >&2
    echo 'Label already exists' >&2
    exit 1
  fi
  exit 0
fi

if [ "$1" = "api" ]; then
  # --method DELETE .../issues/N/labels/<encoded>
  if [ "${2:-}" = "--method" ] && [ "${3:-}" = "DELETE" ]; then
    if [ -f "$FIXTURES/fail_label_delete" ]; then
      echo 'gh: Label does not exist (HTTP 404)' >&2
      exit 1
    fi
    printf '%s\n' "$*" >> "$FIXTURES/deleted.txt"
    exit 0
  fi
  if [ "${2:-}" = "--method" ] && [ "${3:-}" = "POST" ]; then
    # The status POST passes --input <file>; the label POST still
    # pipes --input - via stdin.
    body=""
    prev=""
    for arg in "$@"; do
      if [ "$prev" = "--input" ] && [ "$arg" != "-" ]; then
        body="$(cat "$arg")"
      fi
      prev="$arg"
    done
    if [ -z "$body" ]; then
      body="$(cat)"
    fi
    case "${4:-}" in
      *"/statuses/"*)
        echo x >> "$FIXTURES/status_post_attempts.txt"
        if [ -f "$FIXTURES/fail_status" ]; then
          echo 'gh: Server Error (HTTP 500)' >&2
          exit 1
        fi
        printf '%s\n' "$body" > "$FIXTURES/published_status.json"
        exit 0
        ;;
      *"/labels")
        printf '%s\n' "$body" >> "$FIXTURES/added.txt"
        exit 0
        ;;
    esac
  fi
  case "$*" in
    *"/actions/runs?event=pull_request"*)
      [ -f "$FIXTURES/fail_runs" ] && { echo 'gh: Server Error (HTTP 500)' >&2; exit 1; }
      cat "$FIXTURES/runs.json"; exit 0 ;;
    *"/actions/runs?event=dynamic"*)
      [ -f "$FIXTURES/fail_codeql_runs" ] && { echo 'gh: Server Error (HTTP 500)' >&2; exit 1; }
      cat "$FIXTURES/codeql_runs.json"; exit 0 ;;
    *"/check-runs?check_name=CodeQL"*)
      [ -f "$FIXTURES/fail_codeql_checks" ] && { echo 'gh: Server Error (HTTP 500)' >&2; exit 1; }
      cat "$FIXTURES/codeql_checks.json"; exit 0 ;;
    *"/commits/"*"/check-runs?per_page="*)
      # The head SHA's whole check-run list, read by the fork re-check. Recorded
      # in calls.txt like every other request, so a same-repo publish can be
      # shown never to make it.
      [ -f "$FIXTURES/fail_fork_checks" ] && { echo 'gh: Server Error (HTTP 500)' >&2; exit 1; }
      cat "$FIXTURES/fork_checks.json"; exit 0 ;;
    *"/actions/runs/"[0-9]*)
      # The triggering lane's run, read by the hold step. `--jq .status` is
      # applied by gh itself, so the stub emits the final value.
      [ -f "$FIXTURES/fail_lane_run" ] && { echo 'gh: Server Error (HTTP 500)' >&2; exit 1; }
      cat "$FIXTURES/lane_run_status.txt"; exit 0 ;;
  esac
  case "${2:-}" in
    *"/issues/"*"/labels") cat "$FIXTURES/pr_labels.txt"; exit 0 ;;
  esac
fi
echo "gh stub: unhandled: $*" >&2
exit 90
"""


def _step(name: str) -> str:
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = spec["jobs"]["readiness"]["steps"]
    matches = [s["run"] for s in steps if s.get("name") == name and "run" in s]
    assert len(matches) == 1, f"expected exactly one {name!r} step, got {len(matches)}"
    return matches[0]


def _helper_script() -> str:
    """The retry-helper install step every other step sources at runtime."""
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = spec["jobs"]["readiness"]["steps"]
    matches = [s["run"] for s in steps if "run" in s and "cat > \"$RUNNER_TEMP/gh-retry.sh\"" in s["run"]]
    assert len(matches) == 1, "expected exactly one retry-helper install step"
    return matches[0]


@pytest.fixture(scope="module")
def publish_script() -> str:
    return _step(PUBLISH_STEP)


@pytest.fixture(scope="module")
def cleanup_script() -> str:
    return _step(CLEANUP_STEP)


class Result:
    def __init__(self, proc: subprocess.CompletedProcess[str], fixtures: Path) -> None:
        self.proc = proc
        self._fixtures = fixtures

    @property
    def ok(self) -> bool:
        return self.proc.returncode == 0

    @property
    def published(self) -> dict[str, str] | None:
        path = self._fixtures / "published_status.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    @property
    def deleted(self) -> list[str]:
        path = self._fixtures / "deleted.txt"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def added(self) -> str:
        """The label-add request bodies, as one blob (jq writes them multi-line)."""
        path = self._fixtures / "added.txt"
        return path.read_text() if path.exists() else ""

    @property
    def calls(self) -> list[str]:
        path = self._fixtures / "calls.txt"
        return path.read_text().splitlines() if path.exists() else []


class Runner:
    """Executes one readiness step against one fixture repository state."""

    def __init__(self, root: Path, script: str) -> None:
        self.script = script
        self.fixtures = root / "fixtures"
        self.work = root / "work"
        self.tmp = root / "runner-tmp"
        bindir = root / "bin"
        for d in (self.fixtures, self.work, self.tmp, bindir):
            d.mkdir(parents=True)
        stub = bindir / "gh"
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        (self.tmp / "pr-readiness-summary.md").write_text("## summary\n")
        self.summary = root / "step-summary.md"
        self.summary.write_text("")
        # The steps under test `source "$RUNNER_TEMP/gh-retry.sh"`; in CI the
        # first job step writes it there. Reproduce that provisioning here.
        subprocess.run(  # noqa: S603 - fixed argv, workflow-authored script
            ["bash", "-c", _helper_script()],
            env={**os.environ, "RUNNER_TEMP": str(self.tmp)},
            check=True,
            capture_output=True,
        )
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            "REPO": "kirodotdev/KiroCrew",
            "PR": "2064",
            "SHA": SHA,
            "URL": "https://github.com/kirodotdev/KiroCrew/actions/runs/1",
            "RUNNER_TEMP": str(self.tmp),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GH_TOKEN": "stub",
        }

    def run(
        self,
        *,
        target_label: str = "readiness: passed",
        status_state: str = "success",
        description: str = "all checks passed",
        pr_labels: tuple[str, ...] = ("readiness: checking",),
        repo_labels: tuple[str, ...] = (
            "readiness: checking",
            "readiness: action required",
            "readiness: passed",
        ),
        head_sha: str = SHA,
        fail_label_delete: bool = False,
        fail_label_create: bool = False,
        fail_status: bool = False,
    ) -> Result:
        (self.fixtures / "head_sha.txt").write_text(head_sha + "\n")
        (self.fixtures / "pr_labels.txt").write_text("\n".join(pr_labels) + "\n")
        (self.fixtures / "repo_labels.txt").write_text("\n".join(repo_labels) + "\n")
        for name, on in (
            ("fail_label_delete", fail_label_delete),
            ("fail_label_create", fail_label_create),
            ("fail_status", fail_status),
        ):
            flag = self.fixtures / name
            flag.unlink(missing_ok=True)
            if on:
                flag.write_text("1")
        (self.fixtures / "status_post_attempts.txt").unlink(missing_ok=True)
        for stale in ("calls.txt", "deleted.txt", "added.txt", "published_status.json"):
            (self.fixtures / stale).unlink(missing_ok=True)

        proc = subprocess.run(  # noqa: S603 - fixed argv, test-local stub
            ["bash", "-c", self.script],
            cwd=self.work,
            env={
                **self.env,
                "TARGET_LABEL": target_label,
                "STATUS_STATE": status_state,
                "DESCRIPTION": description,
            },
            text=True,
            capture_output=True,
        )
        return Result(proc, self.fixtures)


@pytest.fixture
def runner(tmp_path: Path, publish_script: str) -> Runner:
    # Seeded as a same-repo head whose every lane is green, the way the
    # context and verdict steps leave the environment for a real publish. The
    # publish step re-checks every success it is about to write, so a runner
    # with no lane fixtures behind it would read that as a failed read and
    # publish a stamped pending instead of the verdict a test handed it.
    return _recheck_runner(Runner(tmp_path, publish_script), "completed")


# ── The verdict lands ────────────────────────────────────────────────────────


def test_the_verdict_and_the_label_are_both_published(runner: Runner) -> None:
    result = runner.run(target_label="readiness: passed")
    assert result.ok, result.proc.stderr
    assert result.published == {
        "state": "success",
        "target_url": runner.env["URL"],
        "description": "all checks passed",
        "context": "PR Readiness",
    }
    assert len(result.deleted) == 1, result.deleted
    assert "readiness%3A%20checking" in result.deleted[0]
    assert "readiness: passed" in result.added


def test_a_stale_revision_publishes_nothing(runner: Runner) -> None:
    result = runner.run(head_sha="0000000000000000000000000000000000000000")
    assert result.ok, result.proc.stderr
    assert result.published is None
    assert result.deleted == []
    assert result.added == ""


# ── Losing a label race must not withhold the verdict ────────────────────────


def test_a_label_removed_by_a_concurrent_run_still_publishes(runner: Runner) -> None:
    """The 404 that froze 62 of this workflow's 100 most recent failures.

    A peer run on the same revision removes the label between this run's
    snapshot and its DELETE. The removal has already reached the state this run
    wanted, so it is not an error -- and it must not cost the verdict.
    """
    result = runner.run(fail_label_delete=True)
    assert result.ok, result.proc.stderr
    assert result.published is not None
    assert result.published["context"] == "PR Readiness"


def test_a_label_created_by_a_concurrent_run_still_publishes(runner: Runner) -> None:
    result = runner.run(
        repo_labels=("some-unrelated-label",), fail_label_create=True
    )
    assert result.ok, result.proc.stderr
    assert result.published is not None


def test_the_verdict_is_published_before_any_label_call(runner: Runner) -> None:
    """Ordering is the structural half of the fix.

    Tolerating the two known races removes the two failures we have seen;
    publishing first is what stops any FUTURE label-call failure from being able
    to withhold the verdict at all.
    """
    result = runner.run()
    assert result.ok, result.proc.stderr
    status_calls = [i for i, c in enumerate(result.calls) if "/statuses/" in c]
    label_calls = [i for i, c in enumerate(result.calls) if "label" in c]
    assert status_calls, result.calls
    assert label_calls, result.calls
    assert status_calls[0] < label_calls[0], result.calls


# ── But a real failure must still be a failure ───────────────────────────────


def test_a_deferred_evaluation_publishes_nothing(runner: Runner) -> None:
    """A truncated evaluation that deferred to an existing terminal verdict
    emits an empty status_state; the publish step must no-op green -- no
    status POST, no label churn."""
    result = runner.run(status_state="")
    assert result.ok, result.proc.stderr
    assert result.published is None
    attempts_file = runner.fixtures / "status_post_attempts.txt"
    assert not attempts_file.exists()


def test_a_failure_to_publish_the_verdict_fails_the_step(runner: Runner) -> None:
    """The tolerance is scoped to the advisory labels, not to the verdict.

    Without this the fix would be indistinguishable from swallowing errors,
    which would buy quiet at the cost of the signal.
    """
    result = runner.run(fail_status=True)
    assert not result.ok
    assert result.published is None


def test_a_failed_post_is_never_retried(runner: Runner) -> None:
    """Commit statuses are last-write-wins with no conditional write, so a
    retry after a failed POST races a concurrent run's newer verdict for the
    same revision -- between any guard read and the re-POST another run can
    publish, and the re-POST would overwrite it (a stale green over a fresh
    red, or a pending over a terminal). The step makes exactly ONE attempt
    and fails loud; a re-run republishes."""
    result = runner.run(fail_status=True)
    assert not result.ok
    attempts = (runner.fixtures / "status_post_attempts.txt").read_text()
    assert len(attempts.splitlines()) == 1


def test_an_unexpected_label_error_still_fails_the_step(
    tmp_path: Path, publish_script: str
) -> None:
    """Only the two documented races are tolerated; a 500 is still a failure."""
    runner = _recheck_runner(Runner(tmp_path, publish_script), "completed")
    stub = tmp_path / "bin" / "gh"
    stub.write_text(
        GH_STUB.replace(
            "echo 'gh: Label does not exist (HTTP 404)' >&2",
            "echo 'gh: Server Error (HTTP 500)' >&2",
        )
    )
    stub.chmod(0o755)
    result = runner.run(fail_label_delete=True)
    assert not result.ok
    # The verdict still landed, because it is published first.
    assert result.published is not None


# ── The closed-PR cleanup step shares the same race ──────────────────────────


def test_the_cleanup_step_tolerates_a_concurrent_removal(
    tmp_path: Path, cleanup_script: str
) -> None:
    """Same snapshot-then-remove shape, same 404, and it must not fail the run.

    Fixing only the publish step would leave this sibling behind.
    """
    runner = Runner(tmp_path, cleanup_script)
    result = runner.run(fail_label_delete=True)
    assert result.ok, result.proc.stderr


# ── A success is re-checked against a lane that started since ────────────────


def _recheck_runner(
    runner: Runner,
    lane_status: str,
    *,
    fail: bool = False,
    conclusion: str = "success",
    codeql_status: str = "completed",
    codeql_conclusion: str | None = "success",
    codeql_check_status: str = "completed",
    codeql_check_conclusion: str | None = "success",
    codeql_runs_present: bool = True,
    codeql_check_present: bool = True,
    fail_codeql_runs: bool = False,
    fail_codeql_checks: bool = False,
    fork: bool = False,
    fork_lane_status: str = "completed",
    fork_lane_conclusion: str | None = "success",
    fork_rows_attempt: int = 1,
    fast_gate_attempt: int = 1,
    ci_cancelled_twin: bool = False,
    fail_fork_checks: bool = False,
) -> Runner:
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    runner.env.update(
        {
            "FORK": "true" if fork else "false",
            "HEAD_REPO": "octo/KiroCrew" if fork else "kirodotdev/KiroCrew",
            "HEAD_REF": "feat/x",
            "MONITORED_LANES": spec["jobs"]["readiness"]["env"]["MONITORED_LANES"],
        }
    )
    head_repo = "octo/KiroCrew" if fork else "kirodotdev/KiroCrew"
    workflow_runs: list[dict[str, object]] = [
        {
            "id": 5,
            "name": "CI",
            "path": ".github/workflows/ci.yml",
            "status": lane_status,
            "conclusion": conclusion if lane_status == "completed" else None,
            "run_started_at": "2026-08-07T19:00:10Z",
            # A fork head's runs list the FORK as head_repository, which is
            # what lets the same filter answer for both.
            "head_repository": {"full_name": head_repo},
            "head_branch": "feat/x",
        },
        # Fast Gate is the Stage-2 fork lanes' trigger: their check-runs are
        # bound to its newest run id + attempt, so it sits on every head.
        {
            "id": 77,
            "name": "Fast Gate",
            "path": ".github/workflows/fast-gate.yml",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": fast_gate_attempt,
            "run_started_at": "2026-08-07T19:00:00Z",
            "head_repository": {"full_name": head_repo},
            "head_branch": "feat/x",
        },
    ]
    if ci_cancelled_twin:
        # Fork approval can start two runs of one lane on one head out of id
        # order, so the max-id twin is the one concurrency cancelled while the
        # lower-id twin, started later, is the one that ran.
        workflow_runs.append(
            {
                "id": 6,
                "name": "CI",
                "path": ".github/workflows/ci.yml",
                "status": "completed",
                "conclusion": "cancelled",
                "run_started_at": "2026-08-07T19:00:05Z",
                "head_repository": {"full_name": head_repo},
                "head_branch": "feat/x",
            }
        )
    (runner.fixtures / "runs.json").write_text(json.dumps({"workflow_runs": workflow_runs}))
    # CodeQL is a `dynamic` run and its security verdict is a separate exact-SHA
    # check-run, so both pages back the publish re-check. Green by default, so a
    # test that says nothing about CodeQL keeps its own subject.
    codeql_runs: list[dict[str, object]] = []
    if codeql_runs_present:
        codeql_runs.append(
            {
                "id": 9,
                "name": "CodeQL",
                "path": "dynamic/github-code-scanning/codeql",
                "status": codeql_status,
                "conclusion": codeql_conclusion if codeql_status == "completed" else None,
            }
        )
    (runner.fixtures / "codeql_runs.json").write_text(json.dumps({"workflow_runs": codeql_runs}))
    codeql_checks: list[dict[str, object]] = []
    if codeql_check_present:
        codeql_checks.append(
            {
                "id": 11,
                "name": "CodeQL",
                "app": {"slug": "github-advanced-security"},
                "status": codeql_check_status,
                "conclusion": (
                    codeql_check_conclusion if codeql_check_status == "completed" else None
                ),
            }
        )
    (runner.fixtures / "codeql_checks.json").write_text(json.dumps({"check_runs": codeql_checks}))
    # The seven Stage-2 fork review lanes report as check-runs on the head SHA,
    # each bound to the pull request AND to Fast Gate's run + attempt by its
    # external_id. All seven green on attempt `fork_rows_attempt` by default,
    # so a fork test that says nothing about them keeps its own subject;
    # `fork_lane_status` / `fork_lane_conclusion` reshape the UX Review row.
    fork_lanes = (
        ("Internal Content Scan", "internal-content-scan-pr-"),
        ("Opus 5 Review", "opus-pr-"),
        ("GPT 5.6 Review", "gpt-pr-"),
        ("Design Review", "design-pr-"),
        ("UX Review", "ux-pr-"),
        ("First Principles Review", "first-principles-pr-"),
        ("Security Scope Review", "scope-pr-"),
    )
    fork_checks: list[dict[str, object]] = []
    for offset, (name, prefix) in enumerate(fork_lanes):
        status, verdict = "completed", "success"
        if name == "UX Review":
            status = fork_lane_status
            verdict = fork_lane_conclusion if fork_lane_status == "completed" else None
        fork_checks.append(
            {
                "id": 40 + offset,
                "name": name,
                "external_id": f"{prefix}2064-77-{fork_rows_attempt}",
                "status": status,
                "conclusion": verdict,
            }
        )
    # A sibling pull request's row on the same head SHA: bound to another PR,
    # so it must never answer for this one.
    fork_checks.append(
        {
            "id": 60,
            "name": "Design Review",
            "external_id": f"design-pr-9-77-{fast_gate_attempt}",
            "status": "in_progress",
            "conclusion": None,
        }
    )
    (runner.fixtures / "fork_checks.json").write_text(json.dumps({"check_runs": fork_checks}))
    for name, on in (
        ("fail_runs", fail),
        ("fail_codeql_runs", fail_codeql_runs),
        ("fail_codeql_checks", fail_codeql_checks),
        ("fail_fork_checks", fail_fork_checks),
    ):
        flag = runner.fixtures / name
        flag.unlink(missing_ok=True)
        if on:
            flag.touch()
    return runner


def test_a_success_over_a_lane_that_restarted_publishes_pending(runner: Runner) -> None:
    """The evaluation read the runs page before the checkout and the
    disposition gate; a lane re-run that started since must not be published
    over as success. That success is what lets armed auto-merge land."""
    result = _recheck_runner(runner, "in_progress").run()
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert "CI" in result.published["description"]
    assert len(result.published["description"]) <= 140


def test_a_success_with_every_lane_done_still_publishes_success(runner: Runner) -> None:
    result = _recheck_runner(runner, "completed").run()
    assert result.published is not None
    assert result.published["state"] == "success"


# ── The fork re-check ────────────────────────────────────────────────────────


def _fork_checks_read(result: Result) -> list[str]:
    return [c for c in result.calls if "/check-runs?per_page=" in c]


def test_a_fork_success_is_re_checked_against_the_runs_page_too(runner: Runner) -> None:
    """A fork head's runs list the fork as head_repository, so the same filter
    answers for a fork's CI, Fast Gate, Build, Code Review and content-scan
    re-runs as it does for a same-repo PR. Gating this read on the repository
    left a fork re-run of any of those five free to publish over."""
    result = _recheck_runner(runner, "in_progress", fork=True).run()
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert "CI" in result.published["description"]


def test_a_fork_review_lane_still_running_holds_the_publish(runner: Runner) -> None:
    """The seven Stage-2 fork review lanes run from the default branch and never
    appear on a fork head's runs page; they report as check-runs on the head
    SHA, bound to the pull request by external_id. A row still running is a
    lane re-run the evaluation did not see -- the write a re-run's hold cannot
    defend against on its own, since the hold lands in seconds and this publish
    lands after it."""
    result = _recheck_runner(runner, "completed", fork=True, fork_lane_status="in_progress").run()
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert "UX Review" in result.published["description"]
    assert len(_fork_checks_read(result)) == 1


def test_a_fork_review_lane_that_turned_red_publishes_failure(runner: Runner) -> None:
    """Each fork lane fails its check ONLY on a real BLOCK (an errored or
    throttled run resolves neutral), so a failure-class conclusion is a
    judged-wrong verdict and publishes red, as the runs-page arm does."""
    result = _recheck_runner(
        runner, "completed", fork=True, fork_lane_status="completed", fork_lane_conclusion="failure"
    ).run()
    assert result.published is not None
    assert result.published["state"] == "failure"
    assert "UX Review" in result.published["description"]


def test_a_sibling_pull_requests_fork_row_never_answers_for_this_one(runner: Runner) -> None:
    """The fixture carries a Design Review row still running, bound to PR #9 on
    the same head SHA. It is not this pull request's lane and must not hold it:
    the external_id binding is the whole reason the row can be trusted."""
    result = _recheck_runner(runner, "completed", fork=True).run()
    assert result.published is not None
    assert result.published["state"] == "success"


def test_a_fast_gate_re_run_holds_the_publish_until_every_lane_reports_on_the_new_attempt(
    runner: Runner,
) -> None:
    """Binding by PR alone accepts the previous attempt's verdicts.

    A Fast Gate re-run keeps the run id and bumps the attempt; the Stage-2 lanes
    it fires stamp the NEW attempt into their rows, but until they post, the
    previous attempt's seven rows are the only ones on the SHA -- completed and
    green. Matched on PR alone they read as the verdict and a delayed publisher
    lands success while the re-run's lanes are still running. Bound to run +
    attempt, as the verdict step binds, none of them is current and the publish
    is held for every lane.
    """
    result = _recheck_runner(runner, "completed", fork=True, fork_rows_attempt=1, fast_gate_attempt=2).run()
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert "(not started)" in result.published["description"]


def test_a_cancelled_max_id_twin_on_a_fork_head_does_not_red_a_green_lane(runner: Runner) -> None:
    """Fork approval can start two runs of one lane on one head out of id order,
    so the twin with the higher id is the one concurrency cancelled and the one
    that actually ran has the lower id. The verdict step collapses that as
    "a cancelled max-id run yields to a later-started sibling" and scores the
    lane green; this re-check collapses it the same way, or it would publish
    failure over a verdict the evaluation scored green, on every recompute."""
    result = _recheck_runner(runner, "completed", fork=True, ci_cancelled_twin=True).run()
    assert result.published is not None
    assert result.published["state"] == "success"


def test_an_unreadable_fork_check_list_publishes_a_stamped_pending(runner: Runner) -> None:
    result = _recheck_runner(runner, "completed", fork=True, fail_fork_checks=True).run()
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert result.published["description"].startswith(READ_FAILURE_TOKEN)


def test_a_same_repo_success_never_reads_the_fork_check_list(runner: Runner) -> None:
    """The fork read costs one request and answers nothing for a same-repo head,
    whose review lanes appear on the runs page already read above it."""
    result = _recheck_runner(runner, "completed").run()
    assert result.published is not None
    assert result.published["state"] == "success"
    assert _fork_checks_read(result) == []


def test_a_fork_success_never_reads_codeql(runner: Runner) -> None:
    """A fork head cannot run the managed default-setup CodeQL; the verdict step
    lists it skipped, so re-checking it here would spend a request to hold on a
    page that is empty by construction."""
    result = _recheck_runner(runner, "completed", fork=True).run()
    assert result.published is not None
    assert not any("event=dynamic" in c for c in result.calls)


HOLD_STEP = "Hold the verdict while a monitored lane runs"


@pytest.fixture(scope="module")
def hold_script() -> str:
    return _step(HOLD_STEP)


def _hold(
    runner: Runner,
    hold_script: str,
    *,
    lane_status: str = "in_progress",
    fail_lane_read: bool = False,
    fail_status: bool = False,
) -> Result:
    """Run the hold step in the publish harness: same stub, same fixtures."""
    (runner.fixtures / "lane_run_status.txt").write_text(lane_status + "\n")
    for name, on in (("fail_lane_run", fail_lane_read), ("fail_status", fail_status)):
        flag = runner.fixtures / name
        flag.unlink(missing_ok=True)
        if on:
            flag.write_text("1")
    for stale in ("calls.txt", "published_status.json", "status_post_attempts.txt", "output.txt"):
        (runner.fixtures / stale).unlink(missing_ok=True)
    proc = subprocess.run(  # noqa: S603 - fixed argv, test-local stub
        ["bash", "-c", hold_script],
        cwd=runner.work,
        env={
            **runner.env,
            "LANE": "CI",
            "RUN_ID": "36290138303",
            "GITHUB_OUTPUT": str(runner.fixtures / "output.txt"),
        },
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return Result(proc, runner.fixtures)


def _held(runner: Runner) -> bool:
    path = runner.fixtures / "output.txt"
    return path.exists() and "held=true" in path.read_text()


def test_an_in_progress_trigger_holds_the_verdict_at_pending(
    runner: Runner, hold_script: str
) -> None:
    """An `in_progress` event MEANS a monitored lane is running, so nothing this
    status says is known to be true; `pending` is the one honest state. The hold
    step writes it and the run stops there -- no evaluation, no read of the
    status it replaces, since a read-then-decide on the STATUS is the
    stale-success window the publish step's own POST comment records three
    guards failing to close."""
    result = _hold(runner, hold_script)
    assert result.ok, result.proc.stderr
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert result.published["context"] == "PR Readiness"
    assert "CI" in result.published["description"]
    assert not result.published["description"].startswith(READ_FAILURE_TOKEN)
    assert _held(runner)
    # Two requests and no more: the lane's run, then the status POST.
    assert [c for c in result.calls if "/statuses/" in c or "/actions/runs/" in c] == result.calls
    assert sum("/statuses/" in c for c in result.calls) == 1


def test_a_hold_that_arrives_after_the_lane_completed_writes_nothing(
    runner: Runner, hold_script: str
) -> None:
    """The stranding case, and why the hold reads the RUN before it writes.

    Queue delay is the condition this workflow's dispatch count feeds, so a hold
    can execute long after its event. If the lane has completed by then, its
    completion is the sweep's evidence -- and a hold written now would stamp the
    status NEWER than that completion, pushing it below the evidence floor with
    no later event left to lift it. So a completed lane gets no hold at all.
    """
    result = _hold(runner, hold_script, lane_status="completed")
    assert result.ok, result.proc.stderr
    assert result.published is None
    assert not _held(runner)
    assert "already completed" in result.proc.stdout


def test_the_hold_reads_the_run_and_never_the_status(hold_script: str) -> None:
    """The one read the step makes is of the workflow run the event names --
    whether the fact being acted on still holds -- never of the commit status
    it is about to replace. A conditional hold on the STATUS is a read followed
    by a write, and a concurrent isolated publisher can land a stale success
    between the two."""
    assert 'repos/$REPO/actions/runs/$RUN_ID' in hold_script
    assert "commits/$SHA/status" not in hold_script
    assert "/statuses/" in hold_script
    assert '"pending"' in hold_script


def test_an_unreadable_lane_run_holds_anyway_and_stamps_the_read_failure(
    runner: Runner, hold_script: str
) -> None:
    """Unknown whether the lane still runs, so the hold is written -- pending
    only ever blocks -- and stamped with the token the sweep retries on age
    alone. That is what keeps a hold that DID land over the lane's completion
    from stranding the head: the sweep recomputes it regardless of evidence."""
    result = _hold(runner, hold_script, fail_lane_read=True)
    assert result.ok, result.proc.stderr
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert result.published["description"].startswith(READ_FAILURE_TOKEN + " ")
    assert _held(runner)


def test_a_failed_hold_fails_the_run_loud_and_does_not_retry(
    runner: Runner, hold_script: str
) -> None:
    """The POST is not retried, for the publish step's reason: a retry races a
    concurrent run's newer verdict. The run reds with an actionable error, and
    the lane's completion still reaches the sweep as evidence."""
    result = _hold(runner, hold_script, fail_status=True)
    assert not result.ok
    # `::error::` is an Actions annotation and goes to stdout.
    assert "Failed to hold the readiness status" in result.proc.stdout
    assert (runner.fixtures / "status_post_attempts.txt").read_text().count("x") == 1
    assert not _held(runner)


def test_an_in_progress_run_never_reaches_evaluation_or_publish() -> None:
    """The hold gates every step that could publish a verdict.

    The event is answered by the hold alone; the evaluate and publish steps
    carry `steps.hold.outputs.held != 'true'`, so an `in_progress` run costs the
    resolve, the hold, and nothing else.
    """
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = spec["jobs"]["readiness"]["steps"]
    by_name = {s.get("name"): s for s in steps}
    hold = by_name[HOLD_STEP]
    assert hold["id"] == "hold"
    assert "github.event.action == 'in_progress'" in hold["if"]
    names = [s.get("name") for s in steps]
    after_hold = names[names.index(HOLD_STEP) + 1 :]
    gated = [
        "Check out the disposition evaluator from the default branch",
        "Evaluate disposition records",
        "Evaluate current revision",
        PUBLISH_STEP,
    ]
    for name in gated:
        assert name in after_hold, name
        assert "steps.hold.outputs.held != 'true'" in by_name[name]["if"], name
    # And the publish step carries no event-shape downgrade of its own.
    assert "WR_ACTION" not in by_name[PUBLISH_STEP]["run"]
    assert "WR_ACTION" not in by_name[PUBLISH_STEP].get("env", {})


def test_an_unreadable_re_check_publishes_a_stamped_pending(runner: Runner) -> None:
    result = _recheck_runner(runner, "completed", fail=True).run()
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert result.published["description"].startswith("[read-failed]")


def test_a_codeql_rerun_in_flight_holds_the_success(runner: Runner) -> None:
    """CodeQL is a `dynamic` run, invisible to a re-check filtered to
    `event=pull_request` and MONITORED_LANES. A re-scan of this revision must
    still hold the publish: success on the sole required status is what lets
    armed auto-merge land while the code-scanning verdict is in flight."""
    result = _recheck_runner(runner, "completed", codeql_status="in_progress").run()
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert "CodeQL" in result.published["description"]


def test_a_codeql_result_reopened_after_a_green_workflow_holds_the_success(
    runner: Runner,
) -> None:
    """The managed workflow says the analyses ran, not that their results
    passed: that verdict is a separate exact-SHA check-run, and a re-scan
    re-opens it while the workflow stays green."""
    result = _recheck_runner(runner, "completed", codeql_check_status="in_progress").run()
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert "CodeQL" in result.published["description"]


def test_an_interim_neutral_codeql_result_is_not_a_clean_verdict(runner: Runner) -> None:
    """Default setup publishes neutral while a configured language has not
    reported. That is absence of a result, not a pass."""
    result = _recheck_runner(runner, "completed", codeql_check_conclusion="neutral").run()
    assert result.published is not None
    assert result.published["state"] == "pending"


def test_the_dynamic_codeql_conclusion_is_read_the_way_the_evaluator_reads_it(
    runner: Runner,
) -> None:
    """Arm for arm, or the re-check disagrees with the evaluation it re-checks.

    The verdict step's dynamic branch sends `success` on to the result
    check-run, scores `skipped` as PASSED (default setup did not scan this
    base), and everything else -- workflow-level `neutral` included -- into
    `failed`. Reading `neutral` as clean would restore a success that
    evaluation rejected; reading `skipped` as red would block a verdict it
    passed. Both are the same defect, in opposite directions.
    """
    passing = _recheck_runner(runner, "completed", codeql_conclusion="skipped").run()
    assert passing.published is not None
    assert passing.published["state"] == "success"

    blocking = _recheck_runner(runner, "completed", codeql_conclusion="neutral").run()
    assert blocking.published is not None
    assert blocking.published["state"] == "failure"
    assert "CodeQL" in blocking.published["description"]


def test_a_codeql_result_that_turned_red_publishes_failure(runner: Runner) -> None:
    result = _recheck_runner(runner, "completed", codeql_check_conclusion="failure").run()
    assert result.published is not None
    assert result.published["state"] == "failure"
    assert "CodeQL" in result.published["description"]


def test_no_dynamic_codeql_run_holds_nothing(runner: Runner) -> None:
    """An empty dynamic page means CodeQL does not apply to this base, or has
    not started -- and in the latter case the verdict step said pending. A hold
    here would stall every PR whose base is not the default branch."""
    result = _recheck_runner(runner, "completed", codeql_runs_present=False).run()
    assert result.published is not None
    assert result.published["state"] == "success"


def test_an_unreadable_codeql_page_publishes_a_stamped_pending(runner: Runner) -> None:
    result = _recheck_runner(runner, "completed", fail_codeql_runs=True).run()
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert result.published["description"].startswith("[read-failed]")


def test_an_unreadable_codeql_check_publishes_a_stamped_pending(runner: Runner) -> None:
    result = _recheck_runner(runner, "completed", fail_codeql_checks=True).run()
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert result.published["description"].startswith("[read-failed]")


def test_the_codeql_re_check_is_skipped_once_a_lane_already_downgraded(runner: Runner) -> None:
    """The CodeQL reads cost nothing once the publish is not a success:
    a lane red or open has already decided it."""
    result = _recheck_runner(runner, "in_progress", codeql_status="in_progress").run()
    assert result.published is not None
    assert result.published["state"] == "pending"
    assert "CI" in result.published["description"]
    assert not any("event=dynamic" in c for c in result.calls)


def test_a_non_success_verdict_is_not_re_checked(runner: Runner) -> None:
    result = _recheck_runner(runner, "in_progress").run(
        status_state="failure", target_label="readiness: action required"
    )
    assert result.published is not None
    assert result.published["state"] == "failure"
    assert not any("actions/runs" in c for c in result.calls)


def test_a_delayed_re_check_never_overwrites_a_red_rerun_with_pending(runner: Runner) -> None:
    """A lane re-run fails and publishes red; a delayed success evaluation
    then re-checks. It must publish failure, not a pending that erases the red."""
    result = _recheck_runner(runner, "completed", conclusion="failure").run()
    assert result.published is not None
    assert result.published["state"] == "failure"
    assert "CI" in result.published["description"]

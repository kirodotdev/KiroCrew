"""Behavioural tests for pr-readiness.yml's evaluate-step transport resilience.

Issue #2753: every read-only ``gh`` call in the readiness evaluation was
single-shot inside a fail-fast shell step, so one transient network/TLS error
aborted the job before the publish step could run -- the same commit was
observed evaluating green then red 39 seconds apart with nothing pushed.

These tests extract the real "Evaluate current revision" script (plus the
retry-helper install step it sources) and execute them with ``gh`` replaced by
a stub, verifying the three properties that matter:

1. A transient failure is retried and the evaluation still reaches its REAL
   verdict (the retry helper works and does not corrupt piped output).
2. A persistent transport failure produces the explicit NON-TERMINAL
   "could not be evaluated" verdict (pending / ``readiness: checking``) and
   exit 0 -- never a red check.
3. A genuine failing workflow conclusion still produces the terminal red
   ``action required`` verdict -- the fallback must not suppress real reds.

Skipped where the POSIX toolchain the script needs (bash, jq) is unavailable,
mirroring test_pr_readiness_sweep.py.
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
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "pr-readiness.yml"
)

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None,
    reason="requires the workflow plus a POSIX bash and jq",
)

# ``gh`` stub. Serves canned workflow-run / check-run JSON keyed on the URL,
# and can simulate a flaky or dead endpoint: any URL containing $FLAKY_SUBSTR
# fails with a TLS-style error until its per-run counter exceeds $FLAKY_FAILS.
GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
url=""
for arg in "$@"; do
  case "$arg" in repos/*|*/actions/*) url="$arg" ;; esac
done
if [ -n "${FLAKY_SUBSTR:-}" ] && [[ "$url" == *"$FLAKY_SUBSTR"* ]]; then
  count=0
  [ -f "$FIXTURES/flaky_count" ] && count="$(cat "$FIXTURES/flaky_count")"
  count=$(( count + 1 ))
  printf '%s' "$count" > "$FIXTURES/flaky_count"
  if [ "$count" -le "${FLAKY_FAILS:-0}" ]; then
    if [ -n "${HTTP_ERROR:-}" ]; then
      echo "gh: $HTTP_ERROR" >&2
    else
      echo "tls: failed to verify certificate: x509: certificate is not valid for any names, but wanted to match api.github.com" >&2
    fi
    # Emit a partial page on stdout so the test proves the retry helper
    # buffers per attempt and never leaks failed-attempt output into a pipe.
    printf '{"workflow_runs":[{"trunc'
    exit 1
  fi
fi
case "$url" in
  *"/commits/"*"/check-runs"*)             cat "$FIXTURES/check_runs.json"; exit 0 ;;
  *"/commits/"*"/status"*)
    # The truncated-fallback's defer guard: the CURRENT "PR Readiness"
    # commit-status state (gh applies --jq itself, so the stub emits the
    # final value). No fixture = no status exists. A __FAIL__ fixture
    # makes the read itself fail, exercising the unverifiable branch.
    if [ -f "$FIXTURES/existing_status_state.txt" ]; then
      state="$(cat "$FIXTURES/existing_status_state.txt")"
      if [ "$state" = "__FAIL__" ]; then
        echo 'gh: Server Error (HTTP 500)' >&2
        exit 1
      fi
      printf '%s\n' "$state"
    fi
    exit 0 ;;
  *"/actions/workflows/ci.yml/runs"*)      cat "$FIXTURES/ci_runs.json"; exit 0 ;;
  *"/actions/workflows/"*"/runs"*)         cat "$FIXTURES/green_runs.json"; exit 0 ;;
  *"/actions/runs?event=dynamic"*)         cat "$FIXTURES/codeql_runs.json"; exit 0 ;;
esac
echo "gh stub: unhandled: $*" >&2
exit 90
"""


def _steps() -> list[dict]:
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return spec["jobs"]["readiness"]["steps"]


def _helper_script() -> str:
    for step in _steps():
        if "run" in step and "cat > \"$RUNNER_TEMP/gh-retry.sh\"" in step["run"]:
            return step["run"]
    raise AssertionError("retry-helper install step not found")


def _evaluate_script() -> str:
    for step in _steps():
        if step.get("id") == "verdict":
            return step["run"]
    raise AssertionError("evaluate step not found")


def _run_json(name: str, *, status: str, conclusion: str) -> str:
    return json.dumps(
        {
            "workflow_runs": [
                {
                    "head_repository": {"full_name": "kirodotdev/KiroCrew"},
                    "head_branch": "feat/x",
                    "path": (
                        "dynamic/github-code-scanning/codeql"
                        if name == "codeql"
                        else f".github/workflows/{name}"
                    ),
                    "status": status,
                    "conclusion": conclusion,
                    "created_at": "2026-08-11T00:00:00Z",
                }
            ]
        }
    )


class Runner:
    """Executes the evaluate step against one stubbed repository state."""

    def __init__(self, root: Path) -> None:
        self.fixtures = root / "fixtures"
        bindir = root / "bin"
        self.temp = root / "runner_temp"
        for d in (self.fixtures, bindir, self.temp):
            d.mkdir(parents=True)
        stub = bindir / "gh"
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        self.output = root / "github_output"
        self.output.touch()
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_OUTPUT": str(self.output),
            "REPO": "kirodotdev/KiroCrew",
            "PR": "2650",
            "SHA": "a686d96a83859a73eb93b322de04b21bdea5f093",
            "HEAD_REPO": "kirodotdev/KiroCrew",
            "HEAD_REF": "feat/x",
            "DRAFT": "false",
            "FORK": "false",
            "BASE_REF": "main",
            "DEFAULT_BRANCH": "main",
            "TRIGGER_EVENT": "workflow_run",
            "TRIGGER_ACTION": "completed",
        }
        # Materialize the helper exactly as CI does: run the install step.
        subprocess.run(
            ["bash", "-c", _helper_script()],
            env=self.env,
            check=True,
            capture_output=True,
        )
        # Default fixtures: everything green and completed.
        green = _run_json("green.yml", status="completed", conclusion="success")
        (self.fixtures / "green_runs.json").write_text(green)
        (self.fixtures / "ci_runs.json").write_text(
            _run_json("ci.yml", status="completed", conclusion="success")
        )
        (self.fixtures / "codeql_runs.json").write_text(
            _run_json("codeql", status="completed", conclusion="success")
        )
        (self.fixtures / "check_runs.json").write_text(
            json.dumps(
                {
                    "check_runs": [
                        {"status": "completed", "conclusion": "success"}
                    ]
                }
            )
        )

    def evaluate(
        self,
        *,
        flaky_substr: str = "",
        flaky_fails: int = 0,
        fork: bool = False,
        http_error: str = "",
        existing_status_state: str = "",
    ):
        env = dict(self.env)
        if fork:
            env["FORK"] = "true"
        if http_error:
            env["HTTP_ERROR"] = http_error
        state_file = self.fixtures / "existing_status_state.txt"
        state_file.unlink(missing_ok=True)
        if existing_status_state:
            state_file.write_text(existing_status_state + "\n")
        if flaky_substr:
            env["FLAKY_SUBSTR"] = flaky_substr
            env["FLAKY_FAILS"] = str(flaky_fails)
        proc = subprocess.run(
            ["bash", "-c", _evaluate_script()],
            env=env,
            capture_output=True,
            text=True,
        )
        outputs = {}
        for line in self.output.read_text().splitlines():
            key, _, value = line.partition("=")
            outputs[key] = value
        return proc, outputs


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


class TestTransientFailureIsRetried:
    def test_one_flake_still_reaches_the_real_verdict(self, runner: Runner):
        # The observed #2753 failure site: the per-workflow runs read. One
        # transient failure, then success -- the retry must absorb it and the
        # evaluation must land on the REAL verdict, not the fallback.
        proc, outputs = runner.evaluate(
            flaky_substr="codex-review.yml/runs", flaky_fails=1
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"
        assert outputs["label"] == "readiness: passed"
        # The stub was actually retried (counter advanced past the failure).
        assert int((runner.fixtures / "flaky_count").read_text()) >= 2

    def test_failed_attempt_output_never_leaks(self, runner: Runner):
        # The stub prints a partial JSON page before failing; if the helper
        # streamed instead of buffering, the retry's good page would be
        # corrupted and jq would blow up. Reaching the real verdict proves
        # per-attempt buffering.
        proc, outputs = runner.evaluate(
            flaky_substr="build.yml/runs", flaky_fails=2
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"


class TestPersistentTransportFailureIsNonTerminal:
    def test_publishes_could_not_evaluate_and_exits_zero(self, runner: Runner):
        # Endpoint dead for all 3 attempts: the job must NOT go red. It exits
        # 0 with the explicit non-terminal verdict so the publish step runs.
        proc, outputs = runner.evaluate(
            flaky_substr="codex-review.yml/runs", flaky_fails=99
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert outputs["label"] == "readiness: checking"
        assert "could not be evaluated" in outputs["description"]
        # Exactly 3 attempts -- bounded, not infinite.
        assert int((runner.fixtures / "flaky_count").read_text()) == 3
        summary = (runner.temp / "pr-readiness-summary.md").read_text()
        assert "could not be evaluated" in summary

    def test_commit_status_description_fits_the_api_limit(self, runner: Runner):
        proc, outputs = runner.evaluate(
            flaky_substr="ci.yml/runs", flaky_fails=99
        )
        assert proc.returncode == 0, proc.stderr
        assert len(outputs["description"]) <= 140

    def test_fork_checkrun_lane_takes_the_same_fallback(self, runner: Runner):
        # A fork PR's AI-review lanes are read from the head SHA's check-runs
        # (checkrun: specs) -- a different branch of the evaluate loop than the
        # workflow-runs reads. A persistent transport failure there must take
        # the same non-terminal fallback, since fork PRs are the lane that
        # produced the documented frozen-verdict incidents.
        proc, outputs = runner.evaluate(
            flaky_substr="check-runs", flaky_fails=99, fork=True
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert outputs["label"] == "readiness: checking"
        assert "could not be evaluated" in outputs["description"]

    def test_a_truncated_run_defers_to_an_existing_blocking_verdict(
        self, runner: Runner
    ):
        # The revision already carries a blocking verdict (failure/error):
        # the merge is already held, and overwriting the red with pending
        # would only discard its diagnostics and set the sweep re-firing.
        # The truncated run defers: exit 0, nothing published.
        proc, outputs = runner.evaluate(
            flaky_substr="codex-review.yml/runs",
            flaky_fails=99,
            existing_status_state="failure",
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["state"] == "deferred"
        assert outputs.get("status_state", "") == ""
        summary = (runner.temp / "pr-readiness-summary.md").read_text()
        assert "deferred" in summary

    def test_a_truncated_run_re_pends_an_existing_success(
        self, runner: Runner
    ):
        # An existing SUCCESS must NOT be deferred to: a monitored rerun on
        # the same revision means validation state is unknown again, and
        # leaving the stale green in place would keep branch protection
        # mergeable while nothing has verified the revision. Pending can
        # only ever BLOCK a merge -- publishing it over success is the
        # fail-safe write, and re-evaluation restores the true verdict.
        proc, outputs = runner.evaluate(
            flaky_substr="codex-review.yml/runs",
            flaky_fails=99,
            existing_status_state="success",
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert outputs["label"] == "readiness: checking"

    def test_a_truncated_run_still_publishes_over_a_pending_status(
        self, runner: Runner
    ):
        # An existing PENDING status is not a completed verdict -- it is this
        # same fallback from an earlier run. Pending-over-pending loses
        # nothing, and the refreshed timestamp keeps the self-heal sweep's
        # staleness clock honest.
        proc, outputs = runner.evaluate(
            flaky_substr="codex-review.yml/runs",
            flaky_fails=99,
            existing_status_state="pending",
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert outputs["label"] == "readiness: checking"

    def test_an_unreadable_verdict_state_publishes_pending(
        self, runner: Runner
    ):
        # The defer-guard read failing leaves the verdict state unknown.
        # The worst a pending can do to an unseen verdict is block a merge
        # that re-evaluation will unblock, while deferring would leave a
        # possibly-stale green mergeable -- so unreadable publishes pending.
        proc, outputs = runner.evaluate(
            flaky_substr="codex-review.yml/runs",
            flaky_fails=99,
            existing_status_state="__FAIL__",
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert outputs["label"] == "readiness: checking"


class TestPermanentHttpErrorFailsLoud:
    def test_http_404_is_not_laundered_into_pending(self, runner: Runner):
        # A non-429 HTTP 4xx (renamed workflow file, missing scope) is a
        # permanent misconfiguration: retrying cannot fix it, and publishing
        # the pending fallback would hide it behind "transient network
        # failure" forever (the sweep re-fires pending statuses endlessly).
        # The helper must not retry it, and the job must fail loudly.
        proc, outputs = runner.evaluate(
            flaky_substr="codex-review.yml/runs",
            flaky_fails=99,
            http_error="HTTP 404: Not Found (repos/x/actions/workflows/codex-review.yml/runs)",
        )
        assert proc.returncode != 0
        assert outputs.get("status_state") != "pending"
        # No retries: the endpoint was hit exactly once.
        assert int((runner.fixtures / "flaky_count").read_text()) == 1

    def test_http_429_is_still_retried_as_transient(self, runner: Runner):
        # Rate limiting is the one HTTP error class that IS transient.
        proc, outputs = runner.evaluate(
            flaky_substr="build.yml/runs",
            flaky_fails=1,
            http_error="HTTP 429: rate limited",
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"
        assert int((runner.fixtures / "flaky_count").read_text()) >= 2

    def test_rate_limit_403_is_retried_as_transient(self, runner: Runner):
        # GitHub's primary and secondary rate limits surface as HTTP 403
        # (not 429) with rate-limit text in the body. They are transient:
        # classifying them permanent would turn readiness red on a busy
        # runner -- recreating the exact symptom this change fixes.
        proc, outputs = runner.evaluate(
            flaky_substr="build.yml/runs",
            flaky_fails=1,
            http_error=(
                "HTTP 403: You have exceeded a secondary rate limit. "
                "Please wait a few minutes before you try again."
            ),
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"
        # It WAS retried past the failure.
        assert int((runner.fixtures / "flaky_count").read_text()) >= 2

    def test_plain_403_is_still_permanent(self, runner: Runner):
        # A 403 WITHOUT rate-limit text (missing scope, SSO enforcement)
        # is a real misconfiguration: no retry, fail loud.
        proc, outputs = runner.evaluate(
            flaky_substr="codex-review.yml/runs",
            flaky_fails=99,
            http_error="HTTP 403: Resource not accessible by integration",
        )
        assert proc.returncode != 0
        assert outputs.get("status_state") != "pending"
        assert int((runner.fixtures / "flaky_count").read_text()) == 1


class TestGenuineFailureStaysRed:
    def test_real_failing_conclusion_is_still_action_required(
        self, runner: Runner
    ):
        # The obvious misreading of this change is "transport resilience
        # softened real failures". It must not: a completed/failure CI run
        # still yields the terminal red verdict.
        (runner.fixtures / "ci_runs.json").write_text(
            _run_json("ci.yml", status="completed", conclusion="failure")
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["label"] == "readiness: action required"

    def test_real_failure_plus_flake_on_another_lane_stays_red(
        self, runner: Runner
    ):
        # A transient blip elsewhere must not launder a genuine red into the
        # non-terminal pending fallback once the retry absorbs the blip.
        (runner.fixtures / "ci_runs.json").write_text(
            _run_json("ci.yml", status="completed", conclusion="failure")
        )
        proc, outputs = runner.evaluate(
            flaky_substr="claude-review.yml/runs", flaky_fails=1
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["label"] == "readiness: action required"

    def test_observed_red_dominates_persistent_transport_failure(
        self, runner: Runner
    ):
        # Precedence when BOTH happen: CI already recorded a genuine failure,
        # then a later lane's read dies for all 3 attempts. The fallback must
        # NOT mask the known red behind "could not be evaluated" -- an
        # already-observed blocker wins and the verdict stays terminal red.
        # (ci.yml is evaluated before the review lanes, so the failure is in
        # `failed[]` by the time the transport failure aborts the loop.)
        (runner.fixtures / "ci_runs.json").write_text(
            _run_json("ci.yml", status="completed", conclusion="failure")
        )
        proc, outputs = runner.evaluate(
            flaky_substr="claude-review.yml/runs", flaky_fails=99
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["label"] == "readiness: action required"
        assert "could not be evaluated" not in outputs["description"]
        # The red verdict was computed from a partial read -- the summary must
        # say so instead of presenting itself as a complete evaluation.
        summary = (runner.temp / "pr-readiness-summary.md").read_text()
        assert "truncated" in summary

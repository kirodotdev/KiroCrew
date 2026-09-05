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

# ``sleep`` stub, shadowed on the same PATH as the ``gh`` stub. The retry
# helper's backoff is a real shell ``sleep``, so a retry-exhausting call would
# cost 2+4s of wall clock, and no assertion depends on that time passing.
# Recording the requested seconds instead of waiting them is what makes the
# backoff schedule assertable at all.
SLEEP_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$1" >> "$FIXTURES/sleeps"
exit 0
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


def _runs_json(name: str, runs: list[dict]) -> str:
    """Multi-run fixture, in the order given (the Actions API returns
    newest-first). Each entry supplies id/status/conclusion; created_at
    defaults to one shared second, the tie the collapse must break on id."""
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
                    "created_at": "2026-08-11T00:00:00Z",
                    **run,
                }
                for run in runs
            ]
        }
    )


def _lane_log(proc: subprocess.CompletedProcess[str]) -> str:
    """The step's own diagnostic lane-state line (the only place the arrays are
    readable from a `gh run view --log`, per #3550)."""
    return next(
        line for line in proc.stdout.splitlines() if line.startswith("pr-readiness: lane state")
    )


class Runner:
    """Executes the evaluate step against one stubbed repository state."""
    def __init__(self, root: Path) -> None:
        self.fixtures = root / "fixtures"
        bindir = root / "bin"
        self.temp = root / "runner_temp"
        for d in (self.fixtures, bindir, self.temp):
            d.mkdir(parents=True)
        for name, body in (("gh", GH_STUB), ("sleep", SLEEP_STUB)):
            stub = bindir / name
            stub.write_text(body)
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
        # cwd pins the children under this runner's own temp dir so a
        # relative write in a future script revision cannot land in the
        # repository root (pytest's CWD).
        subprocess.run(
            ["bash", "-c", _helper_script()],
            env=self.env,
            check=True,
            capture_output=True,
            cwd=self.temp,
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
        disposition_ok: str = "",
        disposition_violations: str = "",
    ):
        env = dict(self.env)
        if disposition_ok:
            env["DISPOSITION_OK"] = disposition_ok
        if disposition_violations:
            env["DISPOSITION_VIOLATIONS"] = disposition_violations
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
            cwd=self.temp,
        )
        outputs = {}
        for line in self.output.read_text().splitlines():
            key, _, value = line.partition("=")
            outputs[key] = value
        return proc, outputs

    def backoff(self) -> list[int]:
        """Seconds the retry helper asked to sleep, in order."""
        log = self.fixtures / "sleeps"
        if not log.is_file():
            return []
        return [int(ln) for ln in log.read_text().split()]


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
        # Backed off between attempts, increasing, and never after the last one:
        # a retry loop that hammers the endpoint with no pause makes a secondary
        # rate limit worse rather than riding it out.
        assert runner.backoff() == [2, 4]
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


class TestLaneStateIsLoggedNotOnlySummarized:
    """#3550: a run that publishes a wrong verdict (e.g. a lane invisible
    under GITHUB_TOKEN but visible under a user token) could previously only
    be diagnosed by opening the $GITHUB_STEP_SUMMARY UI by hand --
    `gh run view --log` cannot query it. The evaluate step must also echo the
    lane arrays to the job's own stdout log."""

    def test_all_green_run_logs_every_lane_as_passed(self, runner: Runner):
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"
        log_line = next(
            line for line in proc.stdout.splitlines() if line.startswith("pr-readiness: lane state")
        )
        assert "pending=[]" in log_line
        assert "failed=[]" in log_line
        # Real lane labels, not just a non-empty bucket -- proves the log line
        # carries the SAME names the summary does, not a placeholder.
        assert "CI" in log_line
        assert "Opus 4.8 Review" in log_line

    def test_a_stuck_lane_is_named_in_the_log_line(self, runner: Runner):
        # The exact #3550 shape: one lane never completes (still queued),
        # everything else green. The diagnostic line must name it so a
        # stuck-pending PR is diagnosable from `gh run view --log` alone,
        # without opening the step-summary UI.
        (runner.fixtures / "codeql_runs.json").write_text(
            _run_json("codeql", status="queued", conclusion="")
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        log_line = next(
            line for line in proc.stdout.splitlines() if line.startswith("pr-readiness: lane state")
        )
        assert "CodeQL" in log_line
        assert "pending=[CodeQL" in log_line


class TestSameSecondRunCollapse:
    """The per-workflow collapse must be deterministic on the monotonic run
    id, not on second-granularity created_at: two runs of one workflow on one
    head routinely share a created_at second (synchronize + edited both fire),
    the API returns runs newest-first, and jq's sort_by is stable -- so a
    created_at sort picked the OLDEST run of a tied group. When that run was
    concurrency-cancelled, a lane whose newest run succeeded published
    "failure: N blocking readiness item(s)" on a fully-green PR."""

    def test_same_second_cancelled_twin_does_not_mask_a_success(
        self, runner: Runner
    ):
        # The observed shape: newest-first API order, the newer (higher-id)
        # run succeeded, its same-second concurrency-cancelled twin sits
        # after it. A created_at collapse selects the cancelled twin and
        # reddens the lane; the id collapse must reach the success.
        (runner.fixtures / "ci_runs.json").write_text(
            _runs_json(
                "ci.yml",
                [
                    {
                        "id": 33096637341,
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "id": 33096636697,
                        "status": "completed",
                        "conclusion": "cancelled",
                    },
                ],
            )
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"
        assert outputs["label"] == "readiness: passed"

    def test_a_cancelled_newest_run_stays_red(self, runner: Runner):
        # There is deliberately no cancelled-run filter: when the run with
        # the highest id was cancelled (e.g. a maintainer cancelled a rerun),
        # it IS the verdict and the lane must stay failure-class. Dropping it
        # would resurface the older success -- a stale green on a revision
        # whose latest validation never completed.
        (runner.fixtures / "ci_runs.json").write_text(
            _runs_json(
                "ci.yml",
                [
                    {
                        "id": 500,
                        "status": "completed",
                        "conclusion": "cancelled",
                    },
                    {
                        "id": 400,
                        "status": "completed",
                        "conclusion": "success",
                    },
                ],
            )
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["label"] == "readiness: action required"

    def test_all_runs_cancelled_still_reads_failure_class(
        self, runner: Runner
    ):
        # When every run of the workflow was cancelled there is no verdict,
        # and the lane must stay a blocking red -- not report "(not started)"
        # and pend forever, and never read as green.
        (runner.fixtures / "ci_runs.json").write_text(
            _runs_json(
                "ci.yml",
                [
                    {
                        "id": 200,
                        "status": "completed",
                        "conclusion": "cancelled",
                    },
                    {
                        "id": 100,
                        "status": "completed",
                        "conclusion": "cancelled",
                    },
                ],
            )
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["label"] == "readiness: action required"

    def test_a_real_failure_with_a_cancelled_twin_stays_red(
        self, runner: Runner
    ):
        # The cancelled-twin drop must never launder a genuine red: a
        # completed/failure run is a verdict, and it wins the collapse over
        # its cancelled sibling exactly like a success would.
        (runner.fixtures / "ci_runs.json").write_text(
            _runs_json(
                "ci.yml",
                [
                    {
                        "id": 700,
                        "status": "completed",
                        "conclusion": "failure",
                    },
                    {
                        "id": 600,
                        "status": "completed",
                        "conclusion": "cancelled",
                    },
                ],
            )
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["label"] == "readiness: action required"

    def test_codeql_collapse_breaks_the_same_tie_the_same_way(
        self, runner: Runner
    ):
        # The dynamic CodeQL resolution is a second, separately-written
        # collapse over the same API shape; it must break the same-second
        # tie identically or the defect just moves lanes.
        (runner.fixtures / "codeql_runs.json").write_text(
            _runs_json(
                "codeql",
                [
                    {
                        "id": 900,
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "id": 800,
                        "status": "completed",
                        "conclusion": "cancelled",
                    },
                ],
            )
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"
        assert outputs["label"] == "readiness: passed"

    def test_the_two_collapse_sites_carry_identical_logic(self):
        # The workflow resolves runs at two separately-written sites (the
        # monitored-workflow loop and the dynamic CodeQL read). Behavioral
        # tests exercise one shape each; this pins the collapse FRAGMENT
        # itself so an edit to one site cannot drift from the other for
        # shapes no fixture covers. The fragment starts after the
        # site-specific select() line and runs to the terminal collapse.
        script = _evaluate_script()
        fragment = "| max_by(.id) // empty"
        lines = [ln.strip() for ln in script.splitlines()]
        count = lines.count(fragment)
        assert count == 2, (
            "expected exactly the two run-collapse sites (monitored"
            f" workflows + dynamic CodeQL), found {count}"
        )
        # No site may re-grow a filter stage between the select() and the
        # collapse: the line preceding each collapse must be the end of the
        # site-specific select bracket.
        for i, line in enumerate(lines):
            if line == fragment:
                assert lines[i - 1].endswith("]"), (
                    "a collapse site carries an extra pipeline stage between"
                    f" select() and the collapse: {lines[i - 1]!r}"
                )


class TestAwaitingApprovalIsAttributedToTheMaintainer:
    @staticmethod
    def _all_monitored_workflows_await_approval(runner: Runner) -> None:
        awaiting = _run_json("x.yml", status="completed", conclusion="action_required")
        (runner.fixtures / "ci_runs.json").write_text(awaiting)
        (runner.fixtures / "green_runs.json").write_text(awaiting)
        (runner.fixtures / "check_runs.json").write_text(json.dumps({"check_runs": []}))

    @staticmethod
    def _lane_state(proc: subprocess.CompletedProcess[str]) -> str:
        return next(
            line for line in proc.stdout.splitlines() if line.startswith("pr-readiness: lane state")
        )

    def test_unapproved_fork_runs_still_block_without_claiming_failure(self, runner: Runner):
        self._all_monitored_workflows_await_approval(runner)

        proc, outputs = runner.evaluate(fork=True)

        assert proc.returncode == 0, proc.stderr
        assert outputs["state"] == "action_required"
        assert outputs["status_state"] == "failure"
        assert outputs["label"] == "readiness: action required"
        # Four, not three: Fast Gate joined CI, Build and Code Review as a
        # monitored lane when the cheap blocking gates were split out of ci.yml.
        # The stub routes every non-ci.yml workflow to green_runs.json, so it is
        # covered by the same `action_required` fixture as the other two.
        assert outputs["description"] == (
            "4 workflow(s) awaiting maintainer approval; none has run yet"
        )
        assert len(outputs["description"]) <= 140
        summary = (runner.temp / "pr-readiness-summary.md").read_text()
        assert "**Awaiting maintainer approval**" in summary
        assert "**Blocking**" not in summary
        assert "**Waiting**" in summary
        log_line = self._lane_state(proc)
        assert "failed=[]" in log_line
        # Fast Gate sits between CI and Build in the spec order, matching the
        # order pr-readiness.yml appends the lanes.
        assert "awaiting_approval=[CI Fast Gate Build Code Review]" in log_line

    def test_real_failure_and_approval_wait_are_reported_separately(self, runner: Runner):
        (runner.fixtures / "ci_runs.json").write_text(
            _run_json("ci.yml", status="completed", conclusion="failure")
        )
        (runner.fixtures / "green_runs.json").write_text(
            _run_json("x.yml", status="completed", conclusion="action_required")
        )

        proc, outputs = runner.evaluate(fork=True)

        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        # Three awaiting, not two: ci.yml is the one blocking item here, and
        # Build, Code Review and Fast Gate are the lanes left waiting.
        assert outputs["description"] == (
            "1 blocking readiness item(s); 3 awaiting maintainer approval"
        )
        summary = (runner.temp / "pr-readiness-summary.md").read_text()
        assert "**Blocking**" in summary
        assert "**Awaiting maintainer approval**" in summary

    def test_same_repo_action_required_remains_failure_class(self, runner: Runner):
        self._all_monitored_workflows_await_approval(runner)

        proc, outputs = runner.evaluate()

        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert "blocking readiness item" in outputs["description"]
        assert "awaiting maintainer approval" not in outputs["description"]
        log_line = self._lane_state(proc)
        assert "awaiting_approval=[]" in log_line
        assert "action_required" in log_line

    def test_approval_wait_dominates_a_later_transport_failure(self, runner: Runner):
        (runner.fixtures / "ci_runs.json").write_text(
            _run_json("ci.yml", status="completed", conclusion="action_required")
        )

        proc, outputs = runner.evaluate(
            fork=True,
            flaky_substr="build.yml/runs",
            flaky_fails=3,
        )

        assert proc.returncode == 0, proc.stderr
        assert outputs["state"] == "action_required"
        assert outputs["status_state"] == "failure"
        assert outputs["description"] == ("1 workflow(s) awaiting maintainer approval")
        summary = (runner.temp / "pr-readiness-summary.md").read_text()
        assert "**Awaiting maintainer approval**" in summary
        assert "Evaluation was truncated" in summary


class TestDispositionViolationsBlockTheVerdict:
    """Issue #6658: the disposition rule was mechanical only for a writer
    running the prepare-pr loop. Readiness publishes the repository's sole
    required status, so folding the violation list in here is what binds every
    writer -- including one who never runs that loop."""

    def test_a_violation_turns_an_otherwise_green_revision_red(self, runner: Runner):
        proc, outputs = runner.evaluate(
            disposition_ok="true",
            disposition_violations=(
                "disposition record claims no span= finding identity "
                "(comment 900 by alice; target=gpt) while that lane has findings"
            ),
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["label"] == "readiness: action required"
        assert outputs["description"] == "1 blocking readiness item(s)"
        assert "disposition rule:" in _lane_log(proc)

    def test_every_violation_is_counted_separately(self, runner: Runner):
        proc, outputs = runner.evaluate(
            disposition_ok="true",
            disposition_violations="first violation\nsecond violation",
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["description"] == "2 blocking readiness item(s)"

    def test_a_clean_evaluation_leaves_the_verdict_green(self, runner: Runner):
        # The ordinary case: the gate ran, found nothing, and must contribute
        # nothing -- not a pending, not a note.
        proc, outputs = runner.evaluate(disposition_ok="true", disposition_violations="")
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"
        assert outputs["label"] == "readiness: passed"

    def test_an_unreadable_record_set_waits_instead_of_going_red(self, runner: Runner):
        """A transient comments/permission API failure must never red the
        required status -- that is issue #2753's class of bug. UNKNOWN is
        pending, which a later event recomputes."""
        proc, outputs = runner.evaluate(disposition_ok="false")
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert outputs["label"] == "readiness: checking"
        assert "disposition records could not be read" in _lane_log(proc)

    def test_a_violation_outranks_an_unrelated_pending_lane(self, runner: Runner):
        """A violation is not something waiting can clear: only the author
        editing or deleting the comment can, so it must be reported as blocking
        even while other lanes are still running."""
        (runner.fixtures / "ci_runs.json").write_text(
            _run_json("ci.yml", status="in_progress", conclusion="")
        )
        proc, outputs = runner.evaluate(
            disposition_ok="true", disposition_violations="one violation"
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["description"] == "1 blocking readiness item(s)"

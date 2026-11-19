"""A job whose API calls sit inside a third-party action waits for the rate-limit reset.

CodeQL's results upload and the Dependency License Gate's review are made by
``github/codeql-action`` and ``actions/dependency-review-action``, from their own
processes, with the job's token. That token draws on the installation's hourly
REST pool; when the rest of the fleet has spent it the action fails, and the job
is red for a reason that says nothing about the diff.

No ``gh`` wrapper reaches those calls, so ``.github/actions/gh-rate-limit`` also
installs ``gh-rate-limit-wait``: it reads ``/rate_limit`` and sleeps until the
reset, out of the same job-wide budget as the wrapper. These tests run the
action's script for real with the runner of test_ship_report_rate_limit.py
(a fixed clock, stubs first on PATH), a ``sleep`` that records and a ``gh`` that
answers each ``/rate_limit`` filter the scripts use, then run the two
workflows' own step scripts through it. Nothing sleeps and nothing reaches the
network.

The ``gh`` stand-in answers in bash, so the tests need no ``jq``: none of the
scripts under test runs one.

Both jobs stay fail-closed: the wait can delay a job or fail it, and nothing in
it reports for the action it guards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from test_ship_report_rate_limit import NOW, Runner

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the stubs are POSIX scripts found on PATH by their shebang, under a POSIX bash",
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION_REF = "./.github/actions/gh-rate-limit"
WAIT = "gh-rate-limit-wait"

# Records the wait, then stands in for the reset: the pool the next
# `/rate_limit` read reports is the one staged for after the sleep.
SLEEP_THEN_RESET = r"""#!/usr/bin/env bash
echo "$1" >> "$FIXTURES/sleeps.log"
if [ -f "$FIXTURES/pool.after-sleep" ]; then
  mv -f "$FIXTURES/pool.after-sleep" "$FIXTURES/pool"
fi
"""

# `api rate_limit --jq FILTER` answers what each filter the scripts under test
# pass would select from a `/rate_limit` body whose REST bucket is the staged
# pool ("remaining reset") and whose other buckets are idle. No staged pool is
# an unreadable `/rate_limit`. Any other call is logged, fails the first $FAILS
# times with $FAIL_MESSAGE, then answers.
GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-} ${2:-}" = "api rate_limit" ]; then
  [ -f "$FIXTURES/pool" ] || exit 1
  read -r remaining reset < "$FIXTURES/pool"
  case "$4" in
    *'"\(.remaining) \(.reset)"'*) echo "$remaining $reset" ;;
    *'.remaining == 0'*) [ "$remaining" -ne 0 ] || echo "$reset" ;;
    .resources.core.remaining) echo "$remaining" ;;
    *) echo "gh stub: unhandled rate_limit filter: $4" >&2; exit 90 ;;
  esac
  exit 0
fi
echo "$*" >> "$FIXTURES/calls.log"
n="$(wc -l < "$FIXTURES/calls.log")"
if [ "$n" -le "${FAILS:-0}" ]; then
  echo "$FAIL_MESSAGE" >&2
  exit 1
fi
case "${1:-} ${2:-}" in
  "api repos/kirodotdev/KiroCrew") echo 'true' ;;
  *) echo "gh stub: unhandled: $*" >&2; exit 90 ;;
esac
"""

PERMISSION_DENIED = "gh: Resource not accessible by integration (HTTP 403)"
SECONDARY_429 = "gh: You have exceeded a secondary rate limit. Please wait. (HTTP 429)"


class WaitRunner(Runner):
    """The shared runner with the wait command installed and a pool to stage."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        for name, body in (("sleep", SLEEP_THEN_RESET), ("gh", GH_STUB)):
            stub = root / "bin" / name
            stub.write_text(body, encoding="utf-8")
            stub.chmod(0o755)

    def pool(self, remaining: int, reset_in: int = 3600) -> None:
        (self.fixtures / "pool").write_text(f"{remaining} {NOW + reset_in}\n", encoding="utf-8")

    def refills_after_the_sleep(self) -> None:
        (self.fixtures / "pool.after-sleep").write_text(f"15000 {NOW + 3600}\n", encoding="utf-8")

    def wait(self, floor: str = "") -> subprocess.CompletedProcess[str]:
        return self.run(f"{WAIT} {floor}".strip())


@pytest.fixture()
def runner(tmp_path: Path) -> WaitRunner:
    made = WaitRunner(tmp_path)
    made.install()
    return made


# --- the wait command ---------------------------------------------------------


def test_a_spent_pool_is_waited_out_once_until_its_reset(runner: WaitRunner) -> None:
    runner.pool(0, reset_in=100)
    runner.refills_after_the_sleep()
    done = runner.wait()
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "waited"
    assert runner.sleeps == [105]
    assert "::warning::" in done.stderr and "rate limit" in done.stderr


def test_a_pool_with_quota_left_is_not_waited_for(runner: WaitRunner) -> None:
    runner.pool(9000)
    done = runner.wait()
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ready"
    assert runner.sleeps == []


def test_a_pool_under_the_floor_is_waited_out(runner: WaitRunner) -> None:
    """The guarded action needs more than one request, so its caller names a floor."""
    runner.pool(40, reset_in=30)
    runner.refills_after_the_sleep()
    done = runner.wait("100")
    assert done.stdout.strip() == "waited"
    assert runner.sleeps == [35]


def test_a_pool_at_the_floor_is_not_waited_for(runner: WaitRunner) -> None:
    runner.pool(100)
    assert runner.wait("100").stdout.strip() == "ready"
    assert runner.sleeps == []


def test_a_reset_past_the_budget_fails_and_names_the_rate_limit(runner: WaitRunner) -> None:
    runner.pool(0, reset_in=3000)
    done = runner.wait()
    assert done.returncode != 0
    assert done.stdout.strip() == ""
    assert runner.sleeps == []
    assert "::error::" in done.stderr
    assert "GitHub API rate limit is spent" in done.stderr
    assert "CI infrastructure, not the code under test" in done.stderr


def test_a_pool_still_spent_after_its_reset_fails_instead_of_waiting_again(
    runner: WaitRunner,
) -> None:
    runner.pool(0, reset_in=100)
    done = runner.wait()
    assert done.returncode != 0
    assert runner.sleeps == [105]
    assert "rate limit" in done.stderr and "CI infrastructure" in done.stderr


def test_an_unreadable_pool_is_not_waited_for(runner: WaitRunner) -> None:
    """With no reading there is no reset to sleep until; the guarded action
    then runs and fails or passes by itself."""
    done = runner.wait()
    assert done.returncode == 0
    assert done.stdout.strip() == "ready"
    assert runner.sleeps == []
    assert "could not be read" in done.stderr


def test_the_wait_and_the_wrapper_share_one_budget(runner: WaitRunner) -> None:
    runner.pool(0, reset_in=700)
    runner.refills_after_the_sleep()
    assert runner.wait().stdout.strip() == "waited"
    runner.pool(0, reset_in=700)
    wrapped = runner.check_issues(fails=1)
    assert wrapped.returncode != 0
    assert "wait budget" in wrapped.stderr
    assert runner.sleeps == [705]


# --- the wrapper, for the cases the wait must not widen ------------------------


def test_a_permission_403_is_not_waited_for_even_with_the_pool_spent(runner: WaitRunner) -> None:
    """Only an error that names a rate limit waits. A refused permission fails
    on the spot, whatever the pool reads."""
    runner.pool(0, reset_in=100)
    step = runner.check_issues(fails=1, message=PERMISSION_DENIED)
    assert step.returncode != 0
    assert PERMISSION_DENIED in step.stderr
    assert len(runner.calls) == 1
    assert runner.sleeps == []


def test_a_secondary_limit_429_waits_and_is_retried_once(runner: WaitRunner) -> None:
    runner.pool(9000)
    step = runner.check_issues(fails=1, message=SECONDARY_429)
    assert step.returncode == 0, step.stderr
    assert len(runner.calls) == 2
    assert runner.sleeps == [60]


def test_a_reset_past_the_budget_says_it_is_infrastructure(runner: WaitRunner) -> None:
    runner.pool(0, reset_in=3000)
    step = runner.check_issues(fails=1)
    assert step.returncode != 0
    assert "API rate limit exceeded for installation" in step.stderr
    assert "CI infrastructure, not the code under test" in step.stderr
    assert runner.sleeps == []


# --- CodeQL -------------------------------------------------------------------


def _job(workflow: str, job_id: str) -> dict:
    return yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))["jobs"][job_id]


def _named(steps: list[dict], name: str) -> dict:
    (found,) = [s for s in steps if s.get("name") == name]
    return found


CODEQL = _job("codeql.yml", "analyze")["steps"]
WAIT_STEP = "Wait for API quota before the upload"
UPLOAD_STEP = "Upload the results"


def test_codeql_uploads_in_its_own_step_right_after_a_quota_wait() -> None:
    names = [s.get("name") or s.get("uses", "") for s in CODEQL]
    analyze = next(s for s in CODEQL if s.get("id") == "analyze")
    assert analyze["uses"].startswith("github/codeql-action/analyze@")
    assert analyze["with"]["upload"] == "failure-only"
    assert names.index(ACTION_REF) < names.index(analyze["uses"]) < names.index(WAIT_STEP)
    assert names.index(WAIT_STEP) + 1 == names.index(UPLOAD_STEP)
    assert _named(CODEQL, WAIT_STEP)["run"].split()[0] == WAIT


def test_codeql_uploads_the_scan_it_ran_under_the_same_category() -> None:
    analyze = next(s for s in CODEQL if s.get("id") == "analyze")
    upload = _named(CODEQL, UPLOAD_STEP)
    assert upload["uses"].startswith("github/codeql-action/upload-sarif@")
    assert upload["uses"].split("@")[1] == analyze["uses"].split("@")[1]
    assert upload["with"]["sarif_file"] == "${{ steps.analyze.outputs.sarif-output }}"
    assert upload["with"]["category"] == analyze["with"]["category"]


def test_codeql_is_green_only_when_the_upload_succeeded() -> None:
    """Neither the wait nor the upload may fail without failing the job, and
    neither is conditional."""
    for step in (_named(CODEQL, WAIT_STEP), _named(CODEQL, UPLOAD_STEP)):
        assert "continue-on-error" not in step, step
        assert "if" not in step, step


def test_codeql_uploads_each_category_once_per_job() -> None:
    """The action refuses a second upload of one category in the same job
    before it sends a request, so a retry step could only ever fail."""
    uploads = [s for s in CODEQL if "/upload-sarif@" in str(s.get("uses", ""))]
    assert uploads
    categories = [s["with"]["category"] for s in uploads]
    assert sorted(set(categories)) == sorted(categories)


def test_the_wait_before_the_upload_sleeps_until_the_reset(runner: WaitRunner) -> None:
    runner.pool(0, reset_in=100)
    runner.refills_after_the_sleep()
    done = runner.run("set -e\n" + _named(CODEQL, WAIT_STEP)["run"])
    assert done.returncode == 0, done.stderr
    assert runner.sleeps == [105]


def test_a_reset_past_the_budget_fails_the_job_before_the_upload(runner: WaitRunner) -> None:
    runner.pool(0, reset_in=3000)
    done = runner.run("set -e\n" + _named(CODEQL, WAIT_STEP)["run"])
    assert done.returncode != 0
    assert runner.sleeps == []
    assert "CI infrastructure, not the code under test" in done.stderr


# --- Dependency License Gate --------------------------------------------------

LICENSE = _job("dependency-review.yml", "license-gate")["steps"]


def test_the_license_gate_waits_for_quota_before_the_review_it_guards() -> None:
    before = _named(LICENSE, "Wait for API quota before the review")
    review = _named(LICENSE, "Review dependency licenses")
    names = [s.get("name") for s in LICENSE]
    assert names.index(before["name"]) + 1 == names.index(review["name"])
    assert before["if"] == review["if"]
    assert before["run"].split()[0] == WAIT


def test_the_pause_before_the_retry_is_the_reset_when_the_pool_is_spent(
    runner: WaitRunner,
) -> None:
    runner.pool(0, reset_in=400)
    runner.refills_after_the_sleep()
    done = runner.run("set -e\n" + _named(LICENSE, "Pause before retrying a failed review")["run"])
    assert done.returncode == 0, done.stderr
    assert runner.sleeps == [405]


def test_the_pause_before_the_retry_is_a_minute_when_quota_is_left(runner: WaitRunner) -> None:
    runner.pool(9000)
    done = runner.run("set -e\n" + _named(LICENSE, "Pause before retrying a failed review")["run"])
    assert done.returncode == 0, done.stderr
    assert runner.sleeps == [60]


def _verdict(
    runner: WaitRunner, license_finding: str = "false"
) -> subprocess.CompletedProcess[str]:
    return runner.run(
        "set -e\n" + _named(LICENSE, "Report the review verdict")["run"],
        REPO="kirodotdev/KiroCrew",
        BASE="a" * 40,
        HEAD="b" * 40,
        RETRY="failure",
        LICENSE_FINDING=license_finding,
    )


def test_two_failed_reviews_with_the_pool_spent_are_not_a_license_verdict(
    runner: WaitRunner,
) -> None:
    """A probe that waited for the reset would answer, and the job would call
    the rate-limited retry a license finding."""
    runner.pool(12, reset_in=3000)
    done = _verdict(runner)
    assert done.returncode == 1
    assert "could not run" in done.stdout
    assert "rate limit" in done.stdout and "CI infrastructure" in done.stdout
    assert "license verdict" not in done.stdout
    assert runner.calls == [], "the dependency-graph probe is not made"
    assert runner.sleeps == []


def test_a_license_the_retry_reported_is_a_license_verdict_whatever_the_pool_reads(
    runner: WaitRunner,
) -> None:
    """The retry read the dependency graph and named a license it does not
    accept. A pool that is low by now must not turn that into "re-run"."""
    runner.pool(12, reset_in=3000)
    done = _verdict(runner, license_finding="true")
    assert done.returncode == 1
    assert "license verdict" in done.stdout
    assert "could not run" not in done.stdout and "rate limit" not in done.stdout
    assert runner.calls == []


def test_the_license_finding_is_read_from_what_the_retry_reported() -> None:
    finding = _named(LICENSE, "Report the review verdict")["env"]["LICENSE_FINDING"]
    output = "steps.review_retry.outputs.invalid-license-changes"
    assert f"fromJSON({output} || '{{}}').forbidden[0] != null" in finding
    assert f"fromJSON({output} || '{{}}').unresolved[0] != null" in finding

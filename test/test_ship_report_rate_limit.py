"""Behavioural tests for the rate-limit-aware ``gh`` in .github/workflows/ship-report.yml.

The job's token shares the repository's installation rate limit with the rest
of the CI fleet. When the fleet has spent it, one ``gh`` read fails with
"API rate limit exceeded for installation" (HTTP 403) and the whole report is
withheld until the next day, although the limit resets within the hour. A
single REST read such as "Check if issues are enabled" is enough to fail the
run even when the GraphQL fetch before it succeeded.

The shared action .github/actions/gh-rate-limit installs a wrapper that every
later step's ``gh`` resolves to. These tests run the action's script for real
against a stub ``gh`` (and a stub
``sleep`` that only records how long it was asked to wait), then run the
workflow's own step scripts through it.

Skipped where the POSIX toolchain the steps need (bash, jq, GNU date) is
unavailable, which is the case on the Windows leg of the matrix.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ship-report.yml"
ACTION = ROOT / ".github" / "actions" / "gh-rate-limit" / "action.yml"
ACTION_REF = "./.github/actions/gh-rate-limit"
INSTALL_STEP = "Install rate-limit-aware gh"


def _has_gnu_date() -> bool:
    if shutil.which("date") is None:
        return False
    probe = subprocess.run(
        ["date", "-u", "-d", "@0", "+%s"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or not ACTION.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None
    or not _has_gnu_date(),
    reason="requires the workflow file plus a POSIX bash, jq and GNU date",
)

# The error gh prints for a spent installation limit, with a placeholder
# request ID.
RATE_LIMITED = (
    "gh: API rate limit exceeded for installation. If you reach out to GitHub "
    "Support for help, please include the request ID 0000:000000:000000:0000000:00000000 "
    "and timestamp 2026-11-02 10:07:23 UTC. (HTTP 403)"
)

# The stub fails the first $FAILS calls (other than `api rate_limit`) with
# $FAIL_MESSAGE, then answers. `api rate_limit --jq EXPR` applies the
# wrapper's own jq expression to fixtures/rate_limit.json, as gh does.
GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-} ${2:-}" = "api rate_limit" ]; then
  [ -f "$FIXTURES/rate_limit.json" ] || exit 1
  jq -r "$4" "$FIXTURES/rate_limit.json"
  exit 0
fi
echo "$*" >> "$FIXTURES/calls.log"
n="$(wc -l < "$FIXTURES/calls.log")"
if [ "$n" -le "${FAILS:-0}" ]; then
  echo "$FAIL_MESSAGE" >&2
  exit 1
fi
case "${1:-} ${2:-}" in
  "pr list") echo '[]' ;;
  "api repos/kirodotdev/KiroCrew") echo 'true' ;;
  *) echo "gh stub: unhandled: $*" >&2; exit 90 ;;
esac
"""

SLEEP_STUB = r"""#!/usr/bin/env bash
echo "$1" >> "$FIXTURES/sleeps.log"
"""

# A fixed clock, so a wait the wrapper computes does not depend on how long
# the stubs take to start. `date +%s` answers NOW; every other use (the
# fetch step's date arithmetic) goes to the real `date`.
NOW = 1_800_000_000
DATE_STUB = """#!/usr/bin/env bash
if [ "$#" -eq 1 ] && [ "$1" = "+%s" ]; then
  echo {now}
  exit 0
fi
exec {real} "$@"
"""


def _steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["report"]["steps"]


def _step(key: str) -> dict:
    return next(s for s in _steps() if key in (s.get("id"), s.get("name")))


def _action() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def _install_script() -> str:
    """The shell the shared action runs to put the wrapper on PATH."""
    (step,) = _action()["runs"]["steps"]
    return step["run"]


class Runner:
    def __init__(self, root: Path) -> None:
        self.fixtures = root / "fixtures"
        self.work = root / "work"
        self.temp = root / "runner_temp"
        stubs = root / "bin"
        for d in (self.fixtures, self.work, self.temp, stubs):
            d.mkdir(parents=True)
        date_stub = DATE_STUB.format(now=NOW, real=shutil.which("date"))
        for name, body in (("gh", GH_STUB), ("sleep", SLEEP_STUB), ("date", date_stub)):
            path = stubs / name
            path.write_text(body)
            path.chmod(0o755)
        (self.fixtures / "calls.log").write_text("")
        (self.fixtures / "sleeps.log").write_text("")
        self.outputs_file = self.work / "gh_output"
        self.outputs_file.write_text("")
        self.path_file = self.work / "gh_path"
        self.path_file.write_text("")
        self.base_path = f"{stubs}{os.pathsep}{os.environ['PATH']}"
        self.env = {
            **os.environ,
            "PATH": self.base_path,
            "FIXTURES": str(self.fixtures),
            "GH_TOKEN": "stub",
            "GITHUB_REPOSITORY": "kirodotdev/KiroCrew",
            "GITHUB_OUTPUT": str(self.outputs_file),
            "GITHUB_PATH": str(self.path_file),
            "RUNNER_TEMP": str(self.temp),
            "START": "2026-11-01T09:55:52Z",
            "END": "2026-11-02T09:57:09Z",
            "FAIL_MESSAGE": RATE_LIMITED,
        }

    def run(self, script: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv, test-local stubs
            ["bash", "-c", script],
            cwd=self.work,
            env={**self.env, **extra},
            text=True,
            encoding="utf-8",
            capture_output=True,
        )

    def install(self, budget: str | None = None) -> None:
        """Run the install step, then put what it added to GITHUB_PATH first
        on PATH, as the runner does for every later step."""
        # The action's `max-wait` input reaches the script as this variable;
        # resolve it from what ship-report passes, the way the runner does.
        passed = _step(INSTALL_STEP).get("with", {}).get("max-wait")
        default = _action()["inputs"]["max-wait"]["default"]
        wait = budget if budget is not None else str(passed or default)
        done = self.run(_install_script(), GH_RATE_LIMIT_MAX_WAIT=wait)
        assert done.returncode == 0, done.stderr
        added = [p for p in self.path_file.read_text().splitlines() if p]
        self.env["PATH"] = os.pathsep.join([*added, self.base_path])

    def spent(self, bucket: str, reset_in: int, **others: int) -> None:
        """Mark ``bucket`` (and any bucket named in ``others``) spent, each
        resetting the given number of seconds after the stub clock's NOW."""
        now = NOW
        resources = {
            name: {"limit": 1000, "remaining": 500, "reset": now + 3600}
            for name in ("core", "graphql", "search")
        }
        for name, seconds in {bucket: reset_in, **others}.items():
            resources[name] = {"limit": 1000, "remaining": 0, "reset": now + seconds}
        (self.fixtures / "rate_limit.json").write_text(json.dumps({"resources": resources}))

    def nothing_spent(self) -> None:
        now = NOW
        resources = {
            name: {"limit": 1000, "remaining": 500, "reset": now + 3600}
            for name in ("core", "graphql", "search")
        }
        (self.fixtures / "rate_limit.json").write_text(json.dumps({"resources": resources}))

    def check_issues(
        self, fails: int, message: str = RATE_LIMITED
    ) -> subprocess.CompletedProcess[str]:
        (self.fixtures / "calls.log").write_text("")
        return self.run(_step("check_issues")["run"], FAILS=str(fails), FAIL_MESSAGE=message)

    @property
    def outputs(self) -> dict[str, str]:
        lines = self.outputs_file.read_text().splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)

    @property
    def calls(self) -> list[str]:
        return (self.fixtures / "calls.log").read_text().splitlines()

    @property
    def sleeps(self) -> list[int]:
        return [int(s) for s in (self.fixtures / "sleeps.log").read_text().split()]


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


def test_the_failing_read_failed_the_run_without_the_wrapper(runner: Runner) -> None:
    """The failing input with no wrapper installed: one rate-limited read
    fails the step, and with it the report."""
    step = runner.check_issues(fails=1)
    assert step.returncode != 0
    assert "API rate limit exceeded for installation" in step.stderr


def test_a_rate_limited_read_waits_for_the_reset_and_succeeds(runner: Runner) -> None:
    runner.install()
    runner.spent("core", reset_in=100)
    step = runner.check_issues(fails=1)
    assert step.returncode == 0, step.stderr
    assert runner.outputs["has_issues"] == "true"
    assert len(runner.calls) == 2
    assert runner.sleeps == [105]
    assert "::warning::" in step.stderr


def test_the_wrapper_is_installed_before_every_other_step(runner: Runner) -> None:
    """Every gh call in the job comes after it, so every one is covered.
    Only the sparse checkout that fetches the action comes first."""
    checkout, install = _steps()[:2]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["sparse-checkout"] == ACTION_REF.removeprefix("./")
    assert install["name"] == INSTALL_STEP
    assert install["uses"] == ACTION_REF


def test_a_spent_graphql_bucket_is_waited_out_too(runner: Runner) -> None:
    """`gh pr list` and `gh issue list` are GraphQL; their bucket resets on
    its own clock."""
    runner.install()
    runner.spent("graphql", reset_in=300)
    step = runner.run(_step("fetch")["run"], FAILS="1")
    assert step.returncode == 0, step.stderr
    assert runner.outputs["count"] == "0"
    assert runner.sleeps == [305]


def test_a_later_reset_on_another_bucket_does_not_abort_the_read(runner: Runner) -> None:
    """Core resets soon and GraphQL long after the budget: a core read waits
    for the core reset instead of failing on the GraphQL one."""
    runner.install()
    runner.spent("core", reset_in=100, graphql=3000)
    step = runner.check_issues(fails=1)
    assert step.returncode == 0, step.stderr
    assert runner.sleeps == [105]


def test_a_secondary_limit_waits_a_fixed_minute(runner: Runner) -> None:
    """No bucket reads as spent, so there is no reset time to wait for."""
    runner.install()
    runner.nothing_spent()
    step = runner.check_issues(
        fails=1, message="gh: You have exceeded a secondary rate limit. (HTTP 403)"
    )
    assert step.returncode == 0, step.stderr
    assert runner.sleeps == [60]


def test_any_other_failure_is_not_retried(runner: Runner) -> None:
    runner.install()
    runner.spent("core", reset_in=100)
    step = runner.check_issues(fails=1, message="gh: Not Found (HTTP 404)")
    assert step.returncode != 0
    assert "Not Found (HTTP 404)" in step.stderr
    assert len(runner.calls) == 1
    assert runner.sleeps == []


def test_a_reset_past_the_budget_fails_without_waiting(runner: Runner) -> None:
    """Failing leaves the window unadvanced; the next run re-covers it."""
    runner.install()
    runner.spent("core", reset_in=3000)
    step = runner.check_issues(fails=1)
    assert step.returncode != 0
    assert "::error::" in step.stderr
    assert "API rate limit exceeded for installation" in step.stderr
    assert runner.sleeps == []
    assert len(runner.calls) == 1


def test_the_budget_is_shared_across_the_job(runner: Runner) -> None:
    runner.install(budget="1200")
    runner.spent("core", reset_in=700)
    assert runner.check_issues(fails=1).returncode == 0
    second = runner.check_issues(fails=1)
    assert second.returncode != 0
    assert len(runner.sleeps) == 1


def test_a_read_is_retried_at_most_twice(runner: Runner) -> None:
    runner.install()
    runner.spent("core", reset_in=10)
    step = runner.check_issues(fails=99)
    assert step.returncode != 0
    assert len(runner.calls) == 3
    assert len(runner.sleeps) == 2


def test_a_successful_read_passes_its_output_through(runner: Runner) -> None:
    runner.install()
    step = runner.check_issues(fails=0)
    assert step.returncode == 0, step.stderr
    assert runner.outputs["has_issues"] == "true"
    assert runner.sleeps == []

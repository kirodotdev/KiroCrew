"""Behavioural tests for .github/workflows/pr-readiness-sweep.yml.

The sweep's entire decision logic lives in one `run:` block of shell + jq that no
other test touches. These tests extract that script and execute it for real with
`gh` replaced by a stub, so the re-fire CONDITIONS are verified rather than
assumed -- and the condition is the whole point: too narrow and a frozen verdict
stays frozen, too broad and every genuinely-failing PR gets dispatched every 15
minutes forever.

Skipped where the POSIX toolchain the script needs (bash, jq, GNU `date -d`) is
unavailable, which is the case on the Windows leg of the matrix. Mirrors the
explicit nt guard in test_issue_triage_workflow.py.
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
    / "pr-readiness-sweep.yml"
)


def _gnu_date() -> bool:
    """GNU `date -d` is required; BSD date uses -j -f and would silently differ."""
    return (
        subprocess.run(
            ["date", "-u", "-d", "2026-01-01T00:00:00Z", "+%s"],
            capture_output=True,
        ).returncode
        == 0
    )


pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None
    or not _gnu_date(),
    reason="requires the workflow plus a POSIX bash, jq and GNU date",
)

# `gh` stub. Three shapes are served, keyed on the subcommand:
#   pr list                 -> the fixture PR list
#   api .../statuses        -> the fixture readiness status history
#   api .../check-runs      -> the fixture check runs
#   workflow run            -> RECORD the dispatch instead of firing it
GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
if [ "$1 ${2:-}" = "pr list" ]; then
  cat "$FIXTURES/prs.json"
  exit 0
fi
if [ "$1 ${2:-}" = "workflow run" ]; then
  # Record every -f key=value so the test can assert pr/sha were passed through.
  printf '%s\n' "$*" >> "$FIXTURES/dispatched.txt"
  exit 0
fi
if [ "$1" = "api" ]; then
  case "${2:-}" in
    *"/check-runs") cat "$FIXTURES/check_runs.json"; exit 0 ;;
    *"/statuses")   cat "$FIXTURES/statuses.json";   exit 0 ;;
  esac
fi
echo "gh stub: unhandled: $*" >&2
exit 90
"""


def _script() -> str:
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = spec["jobs"]["sweep"]["steps"]
    runs = [s["run"] for s in steps if "run" in s]
    assert len(runs) == 1, f"sweep step count changed: {len(runs)}"
    return runs[0]


@pytest.fixture(scope="module")
def script() -> str:
    return _script()


class Runner:
    """Executes the sweep's one step against one fixture repository state."""

    def __init__(self, root: Path, script: str) -> None:
        self.script = script
        self.fixtures = root / "fixtures"
        self.work = root / "work"
        bindir = root / "bin"
        for d in (self.fixtures, self.work, bindir):
            d.mkdir(parents=True)
        stub = bindir / "gh"
        stub.write_text(GH_STUB)
        stub.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FIXTURES": str(self.fixtures),
            "REPO": "kirodotdev/KiroCrew",
            "STATUS_CONTEXT": "PR Readiness",
            "STALE_MINUTES": "15",
            "MAX_DISPATCH": "10",
        }

    def sweep(
        self,
        *,
        state: str,
        status_at: str,
        check_completed_at: str | None = None,
        pr: int = 2064,
        sha: str = "4328fd0f941f09ff10f245fbdb4accf7c246febe",
        context: str = "PR Readiness",
        max_dispatch: str = "10",
    ) -> list[str]:
        """Run the sweep over ONE pull request; return the dispatches recorded."""
        (self.fixtures / "prs.json").write_text(
            json.dumps([{"number": pr, "headRefOid": sha}])
        )
        (self.fixtures / "statuses.json").write_text(
            json.dumps(
                [{"context": context, "state": state, "updated_at": status_at}]
            )
        )
        runs = (
            []
            if check_completed_at is None
            else [{"status": "completed", "completed_at": check_completed_at}]
        )
        (self.fixtures / "check_runs.json").write_text(json.dumps({"check_runs": runs}))
        applied = self.fixtures / "dispatched.txt"
        applied.unlink(missing_ok=True)

        proc = subprocess.run(  # noqa: S603 - fixed argv, test-local stub
            ["bash", "-c", self.script],
            cwd=self.work,
            env={**self.env, "MAX_DISPATCH": max_dispatch},
            text=True,
            capture_output=True,
        )
        # The sweep must never fail a run: a nudge it cannot make is not an error.
        assert proc.returncode == 0, proc.stderr
        self.last_stdout = proc.stdout
        if not applied.exists():
            return []
        return applied.read_text().splitlines()


@pytest.fixture
def runner(tmp_path: Path, script: str) -> Runner:
    return Runner(tmp_path, script)


# ── The pending freeze (the sweep's original purpose) ────────────────────────


def test_stale_pending_is_refired(runner: Runner) -> None:
    dispatched = runner.sweep(state="pending", status_at="2020-01-01T00:00:00Z")
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]
    assert "sha=4328fd0f941f09ff10f245fbdb4accf7c246febe" in dispatched[0]


def test_fresh_pending_is_left_alone(runner: Runner) -> None:
    """Inside STALE_MINUTES the fan-out may genuinely still be running."""
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert runner.sweep(state="pending", status_at=recent) == []


# ── The re-run freeze (the case this change adds) ────────────────────────────


def test_failure_with_later_check_evidence_is_refired(runner: Runner) -> None:
    """The PR #2064 incident, reduced.

    `gh run rerun --failed` creates a new run ATTEMPT whose completion emits no
    fresh `workflow_run: completed`, so the aggregator never re-evaluates. Here
    the verdict was published at 19:01:24Z and a check finished at 19:16:13Z --
    evidence that landed after the verdict, making it stale by construction.
    """
    dispatched = runner.sweep(
        state="failure",
        status_at="2026-08-07T19:01:24Z",
        check_completed_at="2026-08-07T19:16:13Z",
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]


def test_failure_with_no_later_evidence_is_left_alone(runner: Runner) -> None:
    """The anti-storm property, and the reason age is NOT the test here.

    A PR that is genuinely failing has an old terminal verdict and no newer check
    evidence. Dispatching on `state == failure` alone would nudge it every 15
    minutes forever; this asserts it is nudged zero times.
    """
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:16:13Z",
            check_completed_at="2026-08-07T19:01:24Z",
        )
        == []
    )


def test_failure_refire_is_self_terminating(runner: Runner) -> None:
    """Republishing must end the loop.

    After a nudge, readiness becomes the NEWEST timestamp for that SHA. The next
    sweep therefore sees no evidence newer than the verdict and stops -- which is
    what makes a scheduled re-fire safe rather than a dispatch loop.
    """
    # Same evidence, but the verdict has since been republished after it.
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:20:00Z",
            check_completed_at="2026-08-07T19:16:13Z",
        )
        == []
    )


def test_failure_with_no_completed_checks_is_left_alone(runner: Runner) -> None:
    """No check evidence at all means nothing proves the verdict stale."""
    assert (
        runner.sweep(state="failure", status_at="2026-08-07T19:01:24Z") == []
    )


def test_check_completing_in_the_same_second_is_not_new_evidence(runner: Runner) -> None:
    """The publish and the completion that triggered it race within a second.

    Without the margin, every ordinary terminal verdict would look stale on the
    very next sweep.
    """
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:01:24Z",
            check_completed_at="2026-08-07T19:01:26Z",
        )
        == []
    )


# ── States that must never be touched ───────────────────────────────────────


@pytest.mark.parametrize("state", ["success", "error"])
def test_other_states_are_never_refired(runner: Runner, state: str) -> None:
    """`success` needs no rescue; `error` is a publisher fault a real event fixes."""
    assert (
        runner.sweep(
            state=state,
            status_at="2020-01-01T00:00:00Z",
            check_completed_at="2026-08-07T19:16:13Z",
        )
        == []
    )


def test_a_different_status_context_is_ignored(runner: Runner) -> None:
    """Only the aggregate this sweep owns may be nudged."""
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:01:24Z",
            check_completed_at="2026-08-07T19:16:13Z",
            context="Coverage Gate",
        )
        == []
    )


def test_max_dispatch_caps_the_sweep(runner: Runner) -> None:
    """The runaway backstop still applies to the new path."""
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-08-07T19:01:24Z",
            check_completed_at="2026-08-07T19:16:13Z",
            max_dispatch="0",
        )
        == []
    )


def test_the_sweep_never_recomputes_a_verdict_itself(script: str) -> None:
    """It may only nudge the authoritative workflow.

    A sweep that published its own verdict would be a second source of truth for
    a required status -- and could mark a PR ready without the reviewers.
    """
    assert "gh workflow run pr-readiness.yml" in script
    for forbidden in ("/statuses -X POST", "--method POST", "-X POST"):
        assert forbidden not in script, f"sweep must not write statuses: {forbidden}"

"""PR Readiness attributes fork runs that wait for maintainer approval to the
maintainer, not to the code: they still block, without claiming a failure.
Split from test_pr_readiness_evaluate.py, whose Runner executes the real step."""

import json
import subprocess
from pathlib import Path

import pytest
from test_pr_readiness_evaluate import pytestmark  # noqa: F401 - same POSIX bash + jq skip
from test_pr_readiness_evaluate import Runner, _run_json


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


class TestAwaitingApprovalIsAttributedToTheMaintainer:
    @staticmethod
    def _all_monitored_workflows_await_approval(runner: Runner) -> None:
        awaiting = _run_json("x.yml", status="completed", conclusion="action_required")
        (runner.fixtures / "ci_runs.json").write_text(awaiting)
        (runner.fixtures / "green_runs.json").write_text(awaiting)
        (runner.fixtures / "check_runs.json").write_text(json.dumps({"check_runs": []}))
        # A `pull_request_target` run waits for no approval; it is still running.
        (runner.fixtures / "pt_runs.json").write_text(
            _run_json("sensitive-change-review.yml", status="in_progress", conclusion="")
        )

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
        # Six: CI, Fast Gate, Build, Code Review, PR Hygiene and Issue Gate are
        # the monitored workflow lanes. The stub routes every non-ci.yml
        # workflow to green_runs.json, so each is covered by the same
        # `action_required` fixture as the others.
        assert outputs["description"] == (
            "6 workflow(s) awaiting maintainer approval; none has run yet"
        )
        assert len(outputs["description"]) <= 140
        summary = (runner.temp / "pr-readiness-summary.md").read_text()
        assert "**Awaiting maintainer approval**" in summary
        assert "**Blocking**" not in summary
        assert "**Waiting**" in summary
        log_line = self._lane_state(proc)
        assert "failed=[]" in log_line
        # Fast Gate sits between CI and Build, and PR Hygiene then Issue Gate
        # follow Code Review in the spec order, matching the order
        # pr-readiness.yml appends the lanes.
        assert (
            "awaiting_approval=[CI Fast Gate Build Code Review PR Hygiene Issue Gate]" in log_line
        )

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
        # Five awaiting: ci.yml is the one blocking item here, and Build, Code
        # Review, PR Hygiene, Fast Gate and Issue Gate are the lanes left waiting.
        assert outputs["description"] == (
            "1 blocking readiness item(s); 5 awaiting maintainer approval"
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

        # A read that happens AFTER the consolidated runs read has observed
        # CI: on a fork that is the head SHA's check-runs.
        proc, outputs = runner.evaluate(
            fork=True,
            flaky_substr="check-runs",
            flaky_fails=3,
        )

        assert proc.returncode == 0, proc.stderr
        assert outputs["state"] == "action_required"
        assert outputs["status_state"] == "failure"
        assert outputs["description"] == ("1 workflow(s) awaiting maintainer approval")
        summary = (runner.temp / "pr-readiness-summary.md").read_text()
        assert "**Awaiting maintainer approval**" in summary
        assert "Evaluation was truncated" in summary

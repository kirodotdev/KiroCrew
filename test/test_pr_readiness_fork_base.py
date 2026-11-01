"""PR Readiness on a fork pull request whose base is not the default branch.

Fast Gate only runs on PRs to the default branch, and every Stage-2 fork lane
fires on Fast Gate completing, so a fork PR into a release branch gets a run
of none of them. Such a PR is HELD for a maintainer with a terminal, named,
non-passing verdict that carries no `[read-failed]` token, so the readiness
sweep does not re-fire a recompute for it on age. Every other PR shape keeps
its evaluation behaviour on main.
"""

import json
import os
from pathlib import Path

import pytest
from pr_readiness_sweep_helpers import Runner as SweepRunner
from pr_readiness_sweep_helpers import _gnu_date
from pr_readiness_sweep_helpers import _script as _sweep_script
from test_pr_readiness_evaluate import pytestmark  # noqa: F401 - same POSIX bash + jq skip
from test_pr_readiness_evaluate import (
    RUN_ID,
    Runner,
    _lane_log,
)

HOLD = "fork PR into a non-default base needs maintainer review"

# (check-run name, external_id prefix) of every Stage-2 fork lane.
FORK_LANES = (
    ("Internal Content Scan", "internal-content-scan-pr-"),
    ("Opus 5.5 Review", "opus-pr-"),
    ("GPT 6.1 Review", "gpt-pr-"),
    ("Design Review", "design-pr-"),
    ("UX Review", "ux-pr-"),
    ("First Principles Review", "first-principles-pr-"),
    ("Security Scope Review", "scope-pr-"),
)


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


def _into_release(runner: Runner, *, fast_gate_ran: bool) -> None:
    runner.env["BASE_REF"] = "release/0.8.0"
    if not fast_gate_ran:
        # The stub serves a run for every file in LANE_FILES; leaving Fast Gate
        # out is what a PR into a release branch looks like.
        runner.env["LANE_FILES"] = " ".join(
            f for f in runner.env["LANE_FILES"].split() if f != "fast-gate.yml"
        )


def _fork_check_runs(runner: Runner, conclusions: dict[str, str]) -> None:
    """One bound check-run per fork lane, for the current Fast Gate attempt."""
    rows = []
    for index, (name, prefix) in enumerate(FORK_LANES):
        rows.append(
            {
                "id": 91000 + index,
                "name": name,
                "status": "completed",
                "conclusion": conclusions.get(name, "success"),
                "external_id": f"{prefix}{runner.env['PR']}-{RUN_ID}-1",
            }
        )
    (runner.fixtures / "check_runs.json").write_text(json.dumps({"check_runs": rows}))


class TestAForkPullRequestIntoANonDefaultBaseIsHeld:
    def test_the_hold_is_terminal_named_and_not_a_read_failure(self, runner: Runner):
        # No lane ran, so the supersession gate has nothing to evaluate and
        # answers ok=false. The hold still wins: it is a blocker, and a
        # blocker publishes `failure` without the read-failure token.
        _into_release(runner, fast_gate_ran=False)
        runner.env["SUPERSESSION_OK"] = "false"
        proc, outputs = runner.evaluate(fork=True, disposition_ok="true")
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["label"] == "readiness: action required"
        assert outputs["description"] == HOLD
        assert "[read-failed]" not in outputs["description"]
        log = _lane_log(proc)
        for name, _ in FORK_LANES:
            assert f"{name} (only runs on PRs to main)" in log, (name, log)
            assert f"{name} (not started)" not in log, (name, log)
        assert "fork PR into release/0.8.0 needs maintainer review" in log

    def test_it_does_not_pass_even_with_every_lane_green(self, runner: Runner):
        # Fast Gate ran while the base was the default branch, and every lane
        # posted a clean verdict for this head before the base was changed.
        _into_release(runner, fast_gate_ran=True)
        _fork_check_runs(runner, {})
        runner.env["SUPERSESSION_OK"] = "true"
        proc, outputs = runner.evaluate(fork=True, disposition_ok="true")
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["description"] == HOLD
        log = _lane_log(proc)
        for name, _ in FORK_LANES:
            assert name in log.split("passed=[", 1)[1].split("]", 1)[0], (name, log)

    def test_a_red_lane_posted_before_the_base_changed_is_kept(self, runner: Runner):
        _into_release(runner, fast_gate_ran=True)
        _fork_check_runs(runner, {"Security Scope Review": "failure"})
        runner.env["SUPERSESSION_OK"] = "true"
        proc, outputs = runner.evaluate(fork=True, disposition_ok="true")
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["description"] == HOLD + "; 1 other blocking item(s)"
        assert "Security Scope Review (failure)" in _lane_log(proc)

    def test_a_dropped_block_is_kept(self, runner: Runner):
        _into_release(runner, fast_gate_ran=True)
        _fork_check_runs(runner, {})
        runner.env["SUPERSESSION_OK"] = "true"
        runner.env["SUPERSESSION_DROPPED"] = "GPT"
        proc, outputs = runner.evaluate(fork=True, disposition_ok="true")
        assert proc.returncode == 0, proc.stderr
        assert outputs["description"] == HOLD + "; 1 other blocking item(s)"
        assert "superseded verdict: GPT blocked this head" in _lane_log(proc)

    def test_a_real_supersession_read_failure_still_surfaces(self, runner: Runner):
        _into_release(runner, fast_gate_ran=True)
        _fork_check_runs(runner, {})
        runner.env["SUPERSESSION_OK"] = "false"
        proc, outputs = runner.evaluate(fork=True, disposition_ok="true")
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert "superseded verdicts could not be evaluated" in _lane_log(proc)


class TestOtherShapesAreUnchanged:
    def test_a_fork_pr_into_the_default_branch_reads_an_unevaluable_gate_as_unknown(
        self, runner: Runner
    ):
        runner.env["SUPERSESSION_OK"] = "false"
        proc, outputs = runner.evaluate(fork=True, disposition_ok="true")
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert outputs["description"].startswith("[read-failed]")
        assert HOLD not in outputs["description"]

    def test_a_same_repo_pr_into_a_release_branch_is_not_held(self, runner: Runner):
        runner.env["BASE_REF"] = "release/0.8.0"
        runner.env["SUPERSESSION_OK"] = "true"
        proc, outputs = runner.evaluate(disposition_ok="true")
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"
        assert "maintainer review" not in _lane_log(proc)

    def test_a_same_repo_pr_into_a_release_branch_reads_an_unevaluable_gate_as_unknown(
        self, runner: Runner
    ):
        runner.env["BASE_REF"] = "release/0.8.0"
        runner.env["SUPERSESSION_OK"] = "false"
        proc, outputs = runner.evaluate(disposition_ok="true")
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert outputs["description"].startswith("[read-failed]")
        assert "maintainer review" not in _lane_log(proc)


@pytest.mark.skipif(os.name == "nt" or not _gnu_date(), reason="the sweep script requires GNU date")
class TestTheSweepLeavesTheHoldAlone:
    """The hold is published as a terminal failure, which the sweep re-fires
    only on evidence newer than the verdict, never on age."""

    @pytest.fixture()
    def sweep(self, tmp_path: Path) -> SweepRunner:
        return SweepRunner(tmp_path / "sweep", _sweep_script())

    def test_the_hold_is_not_refired(self, sweep: SweepRunner):
        dispatched = sweep.sweep(
            state="failure",
            status_at="2026-10-09T17:00:00Z",
            check_completed_at="2026-10-09T10:21:00Z",
            description=HOLD,
        )
        assert dispatched == []

    def test_a_read_failure_pending_on_the_same_head_is_refired(self, sweep: SweepRunner):
        dispatched = sweep.sweep(
            state="pending",
            status_at="2026-10-09T17:00:00Z",
            check_completed_at="2026-10-09T10:21:00Z",
            description=(
                "[read-failed] 8 readiness check(s) still pending; "
                "waiting on Internal Content Scan (not started) (+7 more)"
            ),
        )
        assert len(dispatched) == 1

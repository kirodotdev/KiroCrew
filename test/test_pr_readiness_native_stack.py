"""PR Readiness counts the `branches:`-filtered lanes on a GitHub-native stack.

GitHub runs the stack trunk's `pull_request` workflows on every PR in a native
stack, so a mid-stack PR (own base a feature branch, stack trunk `main`) gets
CI / Fast Gate / Build / Internal Content Scan runs. The verdict must count
them, or a red CI reads `readiness: passed`. CodeQL default-setup does not
follow the stack, so it stays ineligible there.
"""

from pathlib import Path

import pytest
from test_pr_readiness_evaluate import pytestmark  # noqa: F401 - same POSIX bash + jq skip
from test_pr_readiness_evaluate import Runner, _bucket, _lane_log, _run_json

BRANCH_FILTERED = ("CI", "Fast Gate", "Build", "Internal Content Scan")


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


def _mid_stack(runner: Runner, trunk: str) -> None:
    runner.env["BASE_REF"] = "feat/lower-layer"
    runner.env["STACK_TRUNK"] = trunk


def _red_ci(runner: Runner) -> None:
    (runner.fixtures / "ci_runs.json").write_text(
        _run_json("ci.yml", status="completed", conclusion="failure")
    )


class TestNativeStackLanesCount:
    def test_red_ci_on_a_mid_stack_pr_blocks(self, runner: Runner):
        _mid_stack(runner, "main")
        _red_ci(runner)
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert outputs["label"] == "readiness: action required"

    def test_branch_filtered_lanes_are_evaluated_and_codeql_stays_ineligible(self, runner: Runner):
        _mid_stack(runner, "main")
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"
        log = _lane_log(proc)
        for lane in BRANCH_FILTERED:
            assert lane in _bucket(log, "passed")
            assert f"{lane} (only runs on PRs to main)" not in _bucket(log, "skipped")
        assert "CodeQL (only runs on PRs to main)" in _bucket(log, "skipped")


class TestOtherStacksKeepTheCarveOut:
    def test_a_hand_made_stack_never_reads_a_run_it_cannot_have(self, runner: Runner):
        # No `stack` object: the PR's own base still decides.
        _mid_stack(runner, "")
        _red_ci(runner)
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"
        skipped = _bucket(_lane_log(proc), "skipped")
        for lane in (*BRANCH_FILTERED, "CodeQL"):
            assert f"{lane} (only runs on PRs to main)" in skipped

    def test_a_native_stack_on_a_release_trunk_keeps_the_carve_out(self, runner: Runner):
        _mid_stack(runner, "release/1.2")
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        skipped = _bucket(_lane_log(proc), "skipped")
        for lane in BRANCH_FILTERED:
            assert f"{lane} (only runs on PRs to main)" in skipped

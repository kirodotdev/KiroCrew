"""PR Readiness reads CodeQL from either setup: default setup's managed
`dynamic` run, or the checked-in `.github/workflows/codeql.yml`."""

import json
from pathlib import Path

import pytest
from test_pr_readiness_evaluate import pytestmark  # noqa: F401 - same POSIX bash + jq skip
from test_pr_readiness_evaluate import Runner, _bucket, _lane_log, _run_json


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


def _codeql_pr_run(
    run_id: int,
    *,
    status: str = "completed",
    conclusion: str = "success",
    started: str = "2026-08-11T00:00:00Z",
    head_branch: str = "feat/x",
) -> dict:
    """One run of the checked-in .github/workflows/codeql.yml (advanced setup)."""
    return {
        "id": run_id,
        "run_attempt": 1,
        "head_repository": {"full_name": "kirodotdev/KiroCrew"},
        "head_branch": head_branch,
        "path": ".github/workflows/codeql.yml",
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "created_at": "2026-08-11T00:00:00Z",
        "run_started_at": started,
    }


class TestCodeQLFromEitherSetup:
    """CodeQL can come from default setup (a `dynamic` run) or
    from the checked-in `.github/workflows/codeql.yml` (a `pull_request` run).
    Under default setup the verdict must be exactly today's. With no dynamic
    run, the checked-in run answers, and it must never pass on its own: the
    exact-SHA `CodeQL` result check still decides, and a skipped checked-in run
    scanned nothing."""

    @staticmethod
    def _advanced(runner: Runner, runs: list[dict]) -> None:
        # Default setup off: no dynamic run on the head.
        (runner.fixtures / "codeql_runs.json").write_text(json.dumps({"workflow_runs": []}))
        (runner.fixtures / "codeql_pr_runs.json").write_text(json.dumps({"workflow_runs": runs}))

    @staticmethod
    def _result(runner: Runner, conclusion: str, status: str = "completed") -> None:
        (runner.fixtures / "check_runs.json").write_text(
            json.dumps(
                {
                    "check_runs": [
                        {
                            "id": 88002,
                            "status": status,
                            "conclusion": conclusion,
                            "app": {"slug": "github-advanced-security"},
                        }
                    ]
                }
            )
        )

    def test_advanced_success_with_a_clean_result_passes(self, runner: Runner):
        self._advanced(runner, [_codeql_pr_run(10)])
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"
        assert "CodeQL" in _bucket(_lane_log(proc), "passed")

    def test_advanced_success_with_a_failed_result_blocks(self, runner: Runner):
        self._advanced(runner, [_codeql_pr_run(10)])
        self._result(runner, "failure")
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert "CodeQL (failure)" in _lane_log(proc)

    @pytest.mark.parametrize(
        ("rows", "detail"),
        [([], "results not reported"), ("neutral", "results pending")],
    )
    def test_advanced_success_without_a_final_result_waits(self, runner: Runner, rows, detail: str):
        self._advanced(runner, [_codeql_pr_run(10)])
        if rows == "neutral":
            self._result(runner, "neutral")
        else:
            (runner.fixtures / "check_runs.json").write_text(json.dumps({"check_runs": []}))
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert f"CodeQL ({detail})" in _lane_log(proc)

    def test_advanced_failure_blocks(self, runner: Runner):
        self._advanced(runner, [_codeql_pr_run(10, conclusion="failure")])
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert "CodeQL (failure)" in _lane_log(proc)

    @pytest.mark.parametrize("status", ["queued", "in_progress"])
    def test_advanced_still_running_waits(self, runner: Runner, status: str):
        self._advanced(runner, [_codeql_pr_run(10, status=status)])
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert f"CodeQL ({status})" in _lane_log(proc)

    def test_advanced_skipped_never_passes_even_with_a_clean_result(self, runner: Runner):
        # Draft PR, variable unset, or the PR's own copy of the file skips the
        # job. A clean result row from elsewhere on the SHA must not turn that
        # into a pass: the skipped run scanned nothing.
        self._advanced(runner, [_codeql_pr_run(10, conclusion="skipped")])
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert "CodeQL (skipped)" in _lane_log(proc)
        assert "CodeQL" not in _bucket(_lane_log(proc), "passed")

    def test_advanced_cancelled_newest_run_with_no_later_run_blocks(self, runner: Runner):
        # A newer push cancels the OLD head's run; readiness only reads the
        # current head. A cancelled run ON the current head with nothing later
        # is a scan that never finished: failure-class, never green.
        self._advanced(
            runner,
            [
                _codeql_pr_run(20, conclusion="cancelled", started="2026-08-11T00:02:00Z"),
                _codeql_pr_run(10, started="2026-08-11T00:01:00Z"),
            ],
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"
        assert "CodeQL (cancelled)" in _lane_log(proc)

    def test_advanced_cancelled_twin_yields_to_the_later_started_run(self, runner: Runner):
        # Same-head re-trigger (e.g. ready_for_review) cancels the earlier run
        # in the PR's concurrency group; the later-started run is the verdict.
        self._advanced(
            runner,
            [
                _codeql_pr_run(20, conclusion="cancelled", started="2026-08-11T00:01:00Z"),
                _codeql_pr_run(10, started="2026-08-11T00:02:00Z"),
            ],
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"

    def test_neither_setup_ran_waits_as_not_started(self, runner: Runner):
        self._advanced(runner, [])
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert "CodeQL (not started)" in _lane_log(proc)

    def test_a_sibling_branchs_checked_in_run_cannot_answer(self, runner: Runner):
        # Two open PRs can share a head SHA; the run is bound by head branch.
        self._advanced(runner, [_codeql_pr_run(10, head_branch="feat/other")])
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert "CodeQL (not started)" in _lane_log(proc)

    @pytest.mark.parametrize("checked_in", ["skipped", "failure", "success"])
    def test_default_setup_still_answers_when_both_runs_exist(
        self, runner: Runner, checked_in: str
    ):
        # Before the switch the checked-in run is skipped (variable unset).
        # With the variable set but default setup still on, it fails its
        # upload. Either way the dynamic run is the source and the verdict is
        # today's.
        (runner.fixtures / "codeql_pr_runs.json").write_text(
            json.dumps({"workflow_runs": [_codeql_pr_run(10, conclusion=checked_in)]})
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"

    def test_a_running_default_setup_run_is_not_overridden_by_a_checked_in_success(
        self, runner: Runner
    ):
        (runner.fixtures / "codeql_runs.json").write_text(
            _run_json("codeql", status="in_progress", conclusion="")
        )
        (runner.fixtures / "codeql_pr_runs.json").write_text(
            json.dumps({"workflow_runs": [_codeql_pr_run(10)]})
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert "CodeQL (in_progress)" in _lane_log(proc)

    def test_both_runs_green_still_need_the_result_check(self, runner: Runner):
        (runner.fixtures / "codeql_pr_runs.json").write_text(
            json.dumps({"workflow_runs": [_codeql_pr_run(10)]})
        )
        self._result(runner, "failure")
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "failure"

    def test_a_default_setup_skip_still_passes_as_today(self, runner: Runner):
        (runner.fixtures / "codeql_runs.json").write_text(
            _run_json("codeql", status="completed", conclusion="skipped")
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"

    def test_a_fork_never_reads_either_codeql_source(self, runner: Runner):
        # Neither setup scans a fork head; it stays "not eligible" as today,
        # so a fork's checked-in run (it would be skipped) changes nothing.
        self._advanced(runner, [_codeql_pr_run(10, conclusion="failure")])
        proc, outputs = runner.evaluate(fork=True)
        assert proc.returncode == 0, proc.stderr
        assert "CodeQL (failure)" not in _lane_log(proc)
        assert "CodeQL (not started)" not in _lane_log(proc)

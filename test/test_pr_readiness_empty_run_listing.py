"""PR Readiness reads an empty `actions/runs?head_sha=` listing through the
head's check-runs before it calls any lane "(not started)"."""

import json
import os
import re
import shutil
from pathlib import Path

import pytest
from test_pr_readiness_evaluate import WORKFLOW, Runner, _lane_files, _lane_log

# The POSIX restriction only. A missing jq is a red, not a skip: the fallback
# under test is written in jq, so no stand-in could check it.
pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists() or os.name == "nt" or shutil.which("bash") is None,
    reason="requires the workflow plus a POSIX bash",
)


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    if shutil.which("jq") is None:
        pytest.fail("jq is required: the evaluate step's run-listing fallback is a jq program")
    return Runner(tmp_path)


class TestAnEmptyRunsListingIsNotNoRuns:
    """The `actions/runs?head_sha=` listing is a filtered search that has
    answered `total_count: 0` for a head whose own check-runs named its runs.
    An empty answer is read through the head's check-runs (an Actions job's
    `details_url` names its run id) before any lane is called "(not
    started)", and a run the check-runs name but that cannot be fetched makes
    the evaluation unreadable rather than absent."""

    _EMPTY = json.dumps({"workflow_runs": []})

    @staticmethod
    def _empty_listings(runner: Runner) -> None:
        for name in ("ci_runs.json", "green_runs.json", "codeql_pr_runs.json", "codeql_runs.json"):
            (runner.fixtures / name).write_text(TestAnEmptyRunsListingIsNotNoRuns._EMPTY)

    @staticmethod
    def _job_check_run(run_id: int, job_id: int) -> dict:
        return {
            "id": job_id,
            "status": "completed",
            "conclusion": "success",
            "app": {"slug": "github-actions"},
            "details_url": (
                f"https://github.com/kirodotdev/KiroCrew/actions/runs/{run_id}/job/{job_id}"
            ),
        }

    def _name_every_lane_in_check_runs(self, runner: Runner, *, fetchable: bool = True) -> None:
        """Every monitored lane plus the default-setup CodeQL run, reachable
        only through the head's check-runs."""
        paths = [f".github/workflows/{f}" for f in _lane_files()]
        check_runs = json.loads((runner.fixtures / "check_runs.json").read_text())["check_runs"]
        for n, path in enumerate([*paths, "dynamic/github-code-scanning/codeql"]):
            run_id = 91000 + n
            check_runs.append(self._job_check_run(run_id, 92000 + n))
            if not fetchable:
                continue
            run = {
                "id": run_id,
                "run_attempt": 1,
                "event": "dynamic" if path.startswith("dynamic/") else "pull_request",
                "head_sha": runner.env["SHA"],
                "head_repository": {"full_name": "kirodotdev/KiroCrew"},
                "head_branch": "feat/x",
                "path": path,
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-08-11T00:00:00Z",
                # The base a Code Review run records; other lanes ignore it.
                "display_title": f"[base {runner.env['BASE_REF']}]",
            }
            (runner.fixtures / f"run_{run_id}.json").write_text(json.dumps(run))
        (runner.fixtures / "check_runs.json").write_text(json.dumps({"check_runs": check_runs}))

    def test_every_check_run_is_read_not_only_the_latest_per_name(self, runner: Runner):
        # On a head shared with a sibling pull request, the default
        # filter=latest answers only the sibling's newer rows. The fallback
        # reads every row, so this PR's runs are still found.
        self._empty_listings(runner)
        codeql_result = json.loads((runner.fixtures / "check_runs.json").read_text())["check_runs"]
        self._name_every_lane_in_check_runs(runner)
        (runner.fixtures / "check_runs_latest.json").write_text(
            json.dumps({"check_runs": [*codeql_result, self._job_check_run(99999, 99998)]})
        )
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success", outputs
        assert "(not started)" not in _lane_log(proc)

    def test_a_named_run_that_cannot_be_read_is_unknown_not_absent(self, runner: Runner):
        self._empty_listings(runner)
        self._name_every_lane_in_check_runs(runner, fetchable=False)
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert "could not be evaluated" in outputs["description"]
        assert "(not started)" not in outputs["description"]

    def test_a_run_for_another_head_or_event_is_not_borrowed(self, runner: Runner):
        # The check-runs name each lane's run, but it ran for a different
        # commit or a different event: the lane is still not started here.
        self._empty_listings(runner)
        self._name_every_lane_in_check_runs(runner)
        for n, run_file in enumerate(sorted(runner.fixtures.glob("run_*.json"))):
            run = json.loads(run_file.read_text())
            run.update({"head_sha": "0" * 40} if n % 2 else {"event": "push"})
            run_file.write_text(json.dumps(run))
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert "CI (not started)" in _lane_log(proc)

    def test_check_runs_that_cannot_be_read_are_unknown_not_absent(self, runner: Runner):
        self._empty_listings(runner)
        proc, outputs = runner.evaluate(
            flaky_substr="/check-runs?per_page=100&filter=all", flaky_fails=99
        )
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert "could not be evaluated" in outputs["description"]

    def test_an_empty_listing_with_no_actions_check_runs_is_still_not_started(self, runner: Runner):
        self._empty_listings(runner)
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "pending"
        assert "CI (not started)" in _lane_log(proc)

    def test_a_non_empty_listing_reads_no_check_runs(self, runner: Runner):
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success"
        calls = (runner.fixtures / "calls").read_text().splitlines()
        assert not any("/check-runs?per_page=100&filter=all" in c for c in calls), calls
        assert not any(re.search(r"/actions/runs/[0-9]", c) for c in calls), calls

    def test_a_missing_dynamic_run_does_not_refetch_the_known_pull_request_runs(
        self, runner: Runner
    ):
        # No default-setup CodeQL run yet, the normal state early on a head.
        # The head's check-runs name its pull_request runs (already on the
        # runs page in hand) and one dynamic run; only the unknown one is read.
        (runner.fixtures / "codeql_runs.json").write_text(self._EMPTY)
        known = json.loads((runner.fixtures / "green_runs.json").read_text())["workflow_runs"][0]
        check_runs = json.loads((runner.fixtures / "check_runs.json").read_text())["check_runs"]
        check_runs += [self._job_check_run(known["id"], 1), self._job_check_run(91999, 2)]
        (runner.fixtures / "check_runs.json").write_text(json.dumps({"check_runs": check_runs}))
        dynamic = {
            "id": 91999,
            "event": "dynamic",
            "head_sha": runner.env["SHA"],
            "path": "dynamic/github-code-scanning/codeql",
            "status": "completed",
            "conclusion": "success",
        }
        (runner.fixtures / "run_91999.json").write_text(json.dumps(dynamic))
        proc, outputs = runner.evaluate()
        assert proc.returncode == 0, proc.stderr
        assert outputs["status_state"] == "success", outputs
        calls = (runner.fixtures / "calls").read_text().splitlines()
        fetched = [c for c in calls if re.search(r"/actions/runs/[0-9]", c)]
        assert fetched == ["repos/kirodotdev/KiroCrew/actions/runs/91999"], fetched

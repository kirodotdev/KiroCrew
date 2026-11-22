"""PR Readiness reads Sensitive Change Review from that workflow's own
`pull_request_target` run on the head, which executes the default branch's file.
A check of the same name, or a `pull_request` run of a PR's own copy of the
file, is not that run and does not pass the lane."""

import json
from pathlib import Path

import pytest
from test_pr_readiness_evaluate import pytestmark  # noqa: F401 - same POSIX bash + jq skip
from test_pr_readiness_evaluate import Runner, _lane_log, _run_json

PT_READ = "actions/runs?event=pull_request_target&"


def _pt_runs(runner: Runner, payload: str) -> None:
    (runner.fixtures / "pt_runs.json").write_text(payload)


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_the_pull_request_target_run_decides_the_lane(runner: Runner, conclusion: str):
    run = _run_json("sensitive-change-review.yml", status="completed", conclusion=conclusion)
    _pt_runs(runner, run)
    proc, outputs = runner.evaluate()
    assert proc.returncode == 0, proc.stderr
    assert outputs["status_state"] == conclusion
    log = _lane_log(proc)
    passed = log.split(" pending=")[0]
    assert ("Sensitive Change Review" in passed) == (conclusion == "success")
    assert ("Sensitive Change Review (failure)" in log) == (conclusion == "failure")


def test_a_same_named_check_from_another_run_does_not_pass_the_lane(runner: Runner):
    # No pull_request_target run on this head. The pull_request page still
    # carries a green run at the same path (the stub re-labels green_runs.json
    # for every lane file), and the head carries a green check-run of the same
    # name whose run is a `pull_request` run of the PR's own copy of the file.
    _pt_runs(runner, json.dumps({"workflow_runs": []}))
    checks = json.loads((runner.fixtures / "check_runs.json").read_text())
    checks["check_runs"].append(
        {
            "id": 88002,
            "name": "Sensitive Change Review",
            "status": "completed",
            "conclusion": "success",
            "details_url": "https://github.com/kirodotdev/KiroCrew/actions/runs/77002/job/1",
        }
    )
    (runner.fixtures / "check_runs.json").write_text(json.dumps(checks))
    own_copy = json.loads(
        _run_json("sensitive-change-review.yml", status="completed", conclusion="success")
    )["workflow_runs"][0]
    own_copy.update(id=77002, event="pull_request", head_sha=runner.env["SHA"])
    (runner.fixtures / "run_77002.json").write_text(json.dumps(own_copy))
    proc, outputs = runner.evaluate()
    assert proc.returncode == 0, proc.stderr
    assert outputs["status_state"] == "pending"
    assert "Sensitive Change Review (not started" in _lane_log(proc)


def test_an_unreadable_pull_request_target_read_is_not_a_pass(runner: Runner):
    # The lane's own read failing on every attempt is a transport failure:
    # the non-terminal verdict, never a success scored without the lane.
    proc, outputs = runner.evaluate(flaky_substr=PT_READ, flaky_fails=99)
    assert proc.returncode == 0, proc.stderr
    assert outputs["status_state"] == "pending"
    assert "could not be evaluated" in outputs["description"]


def test_a_pr_to_a_release_branch_is_read_too(runner: Runner):
    # GitHub runs the default branch's `pull_request_target` file whatever the
    # PR's base, so a release branch's older copy of the workflow (on
    # `pull_request`) does not decide this lane.
    runner.env["BASE_REF"] = "release/0.12.0"
    run = _run_json("sensitive-change-review.yml", status="completed", conclusion="failure")
    _pt_runs(runner, run)
    proc, outputs = runner.evaluate()
    assert proc.returncode == 0, proc.stderr
    assert outputs["status_state"] == "failure"
    assert "Sensitive Change Review (failure)" in _lane_log(proc)

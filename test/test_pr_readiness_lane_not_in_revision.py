"""A lane added to PR Readiness's monitored list after a head was pushed has no
run on that head, and only a new pull request event (a push, or closing and
reopening the PR) starts one. It stays pending, under a note that tells it apart
from a lane that is still starting."""

from pathlib import Path

import pytest
from test_pr_readiness_evaluate import pytestmark  # noqa: F401 - same POSIX bash + jq skip
from test_pr_readiness_evaluate import Runner, _lane_files, _lane_log, _run_json

NOTE = "not started on this head; push or reopen"


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


def _no_run_for_first_principles(runner: Runner) -> None:
    # Every other pull_request lane has a completed run on this head.
    runner.env["LANE_FILES"] = " ".join(
        f for f in _lane_files() if f not in ("ci.yml", "first-principles-review.yml")
    )


def _summary(runner: Runner) -> str:
    return (runner.temp / "pr-readiness-summary.md").read_text()


@pytest.mark.parametrize(
    "trigger", [("workflow_run", "completed"), ("pull_request_target", "ready_for_review")]
)
def test_a_lane_the_fan_out_left_out_is_named_and_stays_pending(
    runner: Runner, trigger: tuple[str, str]
):
    _no_run_for_first_principles(runner)
    runner.env["TRIGGER_EVENT"], runner.env["TRIGGER_ACTION"] = trigger
    proc, outputs = runner.evaluate()
    assert proc.returncode == 0, proc.stderr
    assert outputs["status_state"] == "pending"
    assert outputs["label"] == "readiness: checking"
    assert f"waiting on First Principles Review ({NOTE})" in outputs["description"]
    assert "push a commit, or close and reopen the pull request" in _summary(runner)


@pytest.mark.parametrize("case", ["runs_in_flight", "pull_request_target", "codeql"])
def test_otherwise_a_lane_with_no_run_reads_plain_not_started(runner: Runner, case: str):
    label = "First Principles Review"
    if case == "codeql":
        # Not a workflow file of this head: the default-setup run is managed.
        label = "CodeQL"
        (runner.fixtures / "codeql_runs.json").write_text('{"workflow_runs": []}')
    else:
        _no_run_for_first_principles(runner)
    if case == "runs_in_flight":
        for name in ("green_runs.json", "ci_runs.json"):
            running = _run_json(name, status="in_progress", conclusion="")
            (runner.fixtures / name).write_text(running)
    if case == "pull_request_target":
        # The event that starts this head's lanes: their runs may not exist yet.
        runner.env["TRIGGER_EVENT"] = "pull_request_target"
        runner.env["TRIGGER_ACTION"] = "synchronize"
    proc, outputs = runner.evaluate()
    assert proc.returncode == 0, proc.stderr
    assert outputs["status_state"] == "pending"
    assert f"{label} (not started)" in _lane_log(proc)
    assert "reopen" not in proc.stdout + outputs["description"] + _summary(runner)
    if case == "runs_in_flight":
        assert "CI (in_progress)" in _lane_log(proc)

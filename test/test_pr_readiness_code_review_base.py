"""PR Readiness counts a Code Review run only for the base it scanned.

Code Review does not hear `edited`, so readiness must hold its two hazards: a
title or body edit leaves the last scan's verdict standing, failure included,
and a base retarget never lets a scan of another base pass. Each run records
its base in its run-name, which the API returns as `display_title`.
"""

import os
import shutil
from pathlib import Path

import pytest
import yaml
from test_pr_readiness_evaluate import RUN_ID, WORKFLOW, Runner, _lane_log, _runs_json

# The POSIX restriction only. A missing jq is a red, not a skip: the base check
# under test is a jq program, so no stand-in could check it.
pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists() or os.name == "nt" or shutil.which("bash") is None,
    reason="requires the workflow plus a POSIX bash",
)


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    if shutil.which("jq") is None:
        pytest.fail("jq is required: the evaluate step's base check is a jq program")
    return Runner(tmp_path)


def _code_review(runner: Runner, **run) -> None:
    run = {"id": RUN_ID, "status": "completed", "conclusion": "success", **run}
    (runner.fixtures / "code_review_runs.json").write_text(_runs_json("code-review.yml", [run]))


def test_an_edit_keeps_a_failed_scan_red(runner: Runner):
    # The edit started no Code Review run, so the failed scan is still the
    # newest run and still the verdict.
    _code_review(runner, conclusion="failure")
    runner.env.update(
        TRIGGER_EVENT="pull_request_target",
        TRIGGER_ACTION="edited",
        PR_UPDATED_AT="2026-08-11T00:00:05Z",
    )
    proc, outputs = runner.evaluate()
    assert proc.returncode == 0, proc.stderr
    assert outputs["status_state"] == "failure"
    assert "Code Review (failure)" in _lane_log(proc)


@pytest.mark.parametrize(
    ("title", "note"),
    [
        ("[base release/0.13.0]", "scanned release/0.13.0; push or reopen"),
        ("Code Review", "scanned an unrecorded base; push or reopen"),
    ],
)
def test_a_scan_of_another_base_reads_pending(runner: Runner, title: str, note: str):
    # The PR was retargeted to main after a green scan of another base, or the
    # run predates the recorded base: neither answers for main.
    _code_review(runner, display_title=title)
    proc, outputs = runner.evaluate()
    assert proc.returncode == 0, proc.stderr
    assert outputs["status_state"] == "pending"
    assert f"Code Review ({note})" in _lane_log(proc)


def test_a_failed_scan_of_another_base_never_passes(runner: Runner):
    _code_review(runner, conclusion="failure", display_title="[base feat/stack]")
    proc, outputs = runner.evaluate()
    assert proc.returncode == 0, proc.stderr
    assert outputs["status_state"] != "success"


def test_a_scan_of_the_current_base_counts(runner: Runner):
    runner.env["BASE_REF"] = "release/0.13.0"
    _code_review(runner, display_title="[base release/0.13.0]")
    proc, outputs = runner.evaluate()
    assert proc.returncode == 0, proc.stderr
    assert outputs["status_state"] == "success"


def test_an_unrecorded_scan_of_an_older_release_branch_counts(runner: Runner):
    # A release branch cut before the run-name keeps a code-review.yml that
    # records no base and still rescans on a retarget, so its run answers.
    runner.env["BASE_REF"] = "release/0.12.0"
    _code_review(runner, display_title="Code Review")
    proc, outputs = runner.evaluate()
    assert proc.returncode == 0, proc.stderr
    assert outputs["status_state"] == "success"


def test_the_run_name_records_the_base_readiness_reads():
    spec = yaml.safe_load((WORKFLOW.parent / "code-review.yml").read_text(encoding="utf-8"))
    assert spec["run-name"] == "[base ${{ github.event.pull_request.base.ref }}]"
    on = spec.get("on", spec.get(True))
    assert "edited" not in on["pull_request"]["types"]

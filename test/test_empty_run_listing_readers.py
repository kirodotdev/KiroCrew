"""The PR Size approval handler and the Sensitive Change Review comment
workflow read an empty `head_sha` run listing through the head's check-runs
before they conclude that the run they re-run does not exist."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from ai_review_workflows_helpers import (
    _bash,
    _fork_child_env,
    _proc_log,
    _script_file,
    _step_script,
    _workflow,
    _write_stub,
)

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

HEAD = "1234567890abcdef1234567890abcdef12345678"
REPO = "example/repo"


def _run_step(tmp_path: Path, workflow: str, step: str, runs: dict, **env: str):
    """Run `step` of `workflow` against a stub gh whose run listings answer no
    runs while the head's check-runs name every run in `runs`. A run whose
    payload is None is named but cannot be read."""
    bash = _bash()
    if bash is None or os.name == "nt":
        pytest.skip("executes a workflow step; needs a POSIX bash")
    data, calls, stub_dir = tmp_path / "data", tmp_path / "calls", tmp_path / "bin"
    for d in (data, calls, stub_dir):
        d.mkdir()
    rows = [
        {"details_url": f"https://github.com/{REPO}/actions/runs/{run_id}/job/{run_id + 1}"}
        for run_id in runs
    ]
    (data / "checkruns.json").write_text(json.dumps({"check_runs": rows}))
    for run_id, payload in runs.items():
        if payload is not None:
            (data / f"run-{run_id}.json").write_text(json.dumps(payload))
    pull = {"head": {"sha": HEAD, "repo": {"full_name": REPO}}}
    (data / "pull.json").write_text(json.dumps(pull))
    _write_stub(
        stub_dir / "gh",
        'args="$*"\n'
        'case "$args" in\n'
        f'  *"/rerun"*) printf \'%s\\n\' "$args" >> "{calls}/rerun.txt"; exit 0 ;;\n'
        f'  *"/issues/"*"/comments"*) cat >> "{calls}/notice.txt"; exit 0 ;;\n'
        '  *"/actions/workflows/"*"/runs"*)\n'
        '    printf \'{"total_count": 0, "workflow_runs": []}\'; exit 0 ;;\n'
        f'  *"/check-runs?"*filter=all*) cat "{data}/checkruns.json"; exit 0 ;;\n'
        '  *"/actions/runs/"*)\n'
        '    id="${args##*actions/runs/}"; id="${id%% *}"\n'
        f'    if [ -f "{data}/run-$id.json" ]; then cat "{data}/run-$id.json"; exit 0; fi\n'
        '    echo "gh: Server Error (HTTP 502)" >&2; exit 1 ;;\n'
        f'  *"/pulls/"*) cat "{data}/pull.json"; exit 0 ;;\n'
        "esac\n"
        'echo "unexpected gh call: $args" >&2\n'
        "exit 9\n",
    )
    _write_stub(stub_dir / "sleep", "exit 0\n")
    result = subprocess.run(
        [bash, _script_file(tmp_path, _step_script(_workflow(workflow), step))],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env=_fork_child_env(tmp_path, stub_dir, REPO=REPO, **env),
        timeout=FALLBACK_TIMEOUT_SECS,
    )

    def read(name: str) -> str:
        f = calls / name
        return f.read_text(encoding="utf-8") if f.exists() else ""

    return result, read


def _run(run_id: int, workflow: str, event: str, head_sha: str = HEAD) -> dict:
    return {
        "id": run_id,
        "path": f".github/workflows/{workflow}",
        "event": event,
        "head_sha": head_sha,
        "status": "completed",
        "pull_requests": [{"number": 7}],
    }


class TestPrSizeOverride:
    def _rerun(self, tmp_path: Path, runs: dict):
        return _run_step(
            tmp_path, "pr-size-override.yml", "Re-run PR Size", runs, PR="7", HEAD=HEAD
        )

    def test_an_empty_listing_re_runs_the_pr_policy_run_the_check_runs_name(
        self, tmp_path: Path
    ) -> None:
        # The newest named run is another workflow's, and one PR Policy run
        # ran for another head: only this head's newest PR Policy run is re-run.
        runs = {
            300: _run(300, "pr-policy.yml", "pull_request"),
            310: _run(310, "pr-policy.yml", "pull_request"),
            320: _run(320, "pr-policy.yml", "pull_request", head_sha="f" * 40),
            330: _run(330, "ci.yml", "pull_request"),
        }
        result, read = self._rerun(tmp_path, runs)
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt").split() == [
            "api",
            "--method",
            "POST",
            f"repos/{REPO}/actions/runs/310/rerun",
        ], _proc_log(result)
        assert "has not run" not in result.stdout

    def test_a_named_run_that_cannot_be_read_is_a_read_failure(self, tmp_path: Path) -> None:
        result, read = self._rerun(tmp_path, {310: None})
        assert result.returncode == 0, _proc_log(result)
        assert read("rerun.txt") == ""
        assert "could not be read" in read("notice.txt"), _proc_log(result)
        assert "has not run" not in result.stdout


class TestSensitiveChangeReviewComment:
    def _rerun(self, tmp_path: Path, runs: dict):
        return _run_step(
            tmp_path,
            "sensitive-change-review-comment.yml",
            "Re-run the check for the current head",
            runs,
            PR="7",
        )

    def test_an_empty_listing_re_runs_the_run_the_check_runs_name(self, tmp_path: Path) -> None:
        # The newer named run is another pull_request_target workflow's on the
        # same head and PR: only the Sensitive Change Review run is re-run.
        runs = {
            400: _run(400, "sensitive-change-review.yml", "pull_request_target"),
            410: _run(410, "pr-readiness.yml", "pull_request_target"),
        }
        result, read = self._rerun(tmp_path, runs)
        assert result.returncode == 0, _proc_log(result)
        assert f"repos/{REPO}/actions/runs/400/rerun" in read("rerun.txt"), _proc_log(result)
        assert "No Sensitive Change Review run found" not in result.stdout

    def test_a_named_run_that_cannot_be_read_is_a_read_failure(self, tmp_path: Path) -> None:
        result, read = self._rerun(tmp_path, {400: None})
        assert result.returncode != 0, _proc_log(result)
        assert read("rerun.txt") == ""
        assert "HTTP 502" in result.stderr, _proc_log(result)
        assert "No Sensitive Change Review run found" not in result.stdout

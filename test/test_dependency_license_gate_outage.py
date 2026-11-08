"""The Dependency License Gate tells a dependency-graph outage from a license finding.

GitHub's dependency-graph API has outages in which ``dependency-review-action``
fails every PR with "Dependency review is not supported on this repository". The
gate retries a failed review once, and when the retry fails too it probes the API:
an API that answers means the failure is the action's verdict, one that does not
means the review could not run. Both fail closed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from kiro_crew.subprocess_utf8 import UTF8_TEXT

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "dependency-review.yml"
_ACTION = "actions/dependency-review-action@"


def _steps() -> list[dict]:
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    (job,) = doc["jobs"].values()
    return job["steps"]


def _step(name: str) -> dict:
    (found,) = [s for s in _steps() if s.get("name") == name]
    return found


def test_the_first_review_does_not_fail_the_job_by_itself() -> None:
    review = _step("Review dependency licenses")
    assert review["id"] == "review"
    assert review["continue-on-error"] is True
    assert str(review["uses"]).startswith(_ACTION)


def test_a_failed_review_is_retried_with_the_same_action_and_config() -> None:
    review, retry = _step("Review dependency licenses"), _step("Review dependency licenses (retry)")
    assert retry["if"] == "${{ steps.review.outcome == 'failure' }}"
    assert retry["uses"] == review["uses"]
    assert retry["with"] == review["with"]
    assert retry["continue-on-error"] is True
    pause = _step("Pause before retrying a failed review")
    assert pause["if"] == retry["if"]
    names = [s.get("name") for s in _steps()]
    assert names.index(pause["name"]) < names.index(retry["name"])


def test_the_verdict_step_runs_whenever_the_first_review_failed() -> None:
    verdict = _step("Report the review verdict")
    assert verdict["if"] == "${{ steps.review.outcome == 'failure' }}"
    assert verdict["env"]["RETRY"] == "${{ steps.review_retry.outcome }}"
    assert "continue-on-error" not in verdict


needs_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="runs the verdict step under a POSIX bash",
)


def _run_verdict(tmp_path: Path, retry: str, gh_exit: int) -> subprocess.CompletedProcess[str]:
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {tmp_path / 'calls'}\n"
        f"[ {gh_exit} -eq 0 ] || echo 'gh: HTTP 503: Service Unavailable' >&2\n"
        f"exit {gh_exit}\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{tmp_path}{os.pathsep}{env.get('PATH', '')}",
        GH_TOKEN="x",
        REPO="kirodotdev/KiroCrew",
        BASE="a" * 40,
        HEAD="b" * 40,
        RETRY=retry,
        RUNNER_TEMP=str(tmp_path),
    )
    return subprocess.run(
        ["bash", "-e", "-c", _step("Report the review verdict")["run"]],
        capture_output=True,
        env=env,
        cwd=tmp_path,
        **UTF8_TEXT,
    )


@needs_bash
def test_a_retry_that_passes_passes_the_gate(tmp_path: Path) -> None:
    proc = _run_verdict(tmp_path, retry="success", gh_exit=0)
    assert proc.returncode == 0, proc.stdout
    assert not (tmp_path / "calls").exists(), "a passed retry needs no probe"


@needs_bash
def test_two_failures_with_a_working_api_are_a_license_verdict(tmp_path: Path) -> None:
    proc = _run_verdict(tmp_path, retry="failure", gh_exit=0)
    assert proc.returncode == 1
    assert "license verdict" in proc.stdout
    assert "could not run" not in proc.stdout
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1 and "dependency-graph/compare/" in calls[0], calls


@needs_bash
def test_two_failures_with_the_api_down_are_reported_as_could_not_run(tmp_path: Path) -> None:
    proc = _run_verdict(tmp_path, retry="failure", gh_exit=1)
    assert proc.returncode == 1
    assert "could not run" in proc.stdout
    assert "not a license" in proc.stdout
    assert "HTTP 503" in proc.stdout
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 3, calls

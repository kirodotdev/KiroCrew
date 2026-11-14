"""The Dependency License Gate tells a dependency-graph outage from a license finding.

GitHub's dependency-graph API has outages in which ``dependency-review-action``
fails every PR with "Dependency review is not supported on this repository". The
gate retries a failed review once, and when the retry fails too it probes the API.
An API that answers means the failure is the action's verdict, and the job fails.
An API that does not answer gets one more wait (until a spent installation quota
resets, capped) and one last review. Only a review that runs and passes lets the
job pass; every other path fails closed.
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
#: The instant the stubbed `date` reports, so no step under test reads a live clock.
_NOW = 1_800_000_000


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


def test_the_probe_runs_only_when_both_reviews_failed_and_can_fail_the_job() -> None:
    probe = _step("Probe the dependency-graph API")
    assert probe["id"] == "probe"
    assert probe["if"] == (
        "${{ steps.review.outcome == 'failure' && steps.review_retry.outcome != 'success' }}"
    )
    # A working API makes the failure a license verdict, so the probe fails the job.
    assert "continue-on-error" not in probe


def test_the_last_review_follows_a_probe_that_found_the_api_down() -> None:
    review, last = _step("Review dependency licenses"), _step(
        "Review dependency licenses (last attempt)"
    )
    wait = _step("Wait for the API or the quota reset")
    assert last["if"] == wait["if"] == "${{ steps.probe.outcome == 'success' }}"
    assert last["uses"] == review["uses"]
    assert last["with"] == review["with"]
    assert last["continue-on-error"] is True
    names = [s.get("name") for s in _steps()]
    assert (
        names.index("Probe the dependency-graph API")
        < names.index(wait["name"])
        < names.index(last["name"])
        < names.index("Report the review verdict")
    )


def test_the_verdict_step_runs_whenever_the_first_review_failed() -> None:
    verdict = _step("Report the review verdict")
    assert verdict["if"] == "${{ steps.review.outcome == 'failure' }}"
    assert verdict["env"]["RETRY"] == "${{ steps.review_retry.outcome }}"
    assert verdict["env"]["PROBE"] == "${{ steps.probe.outcome }}"
    assert verdict["env"]["LAST"] == "${{ steps.review_last.outcome }}"
    assert "continue-on-error" not in verdict


def test_the_job_timeout_covers_the_longest_wait() -> None:
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    (job,) = doc["jobs"].values()
    # Up to 15 minutes of wait, plus three reviews and the probe.
    assert job["timeout-minutes"] >= 20


needs_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="runs the workflow steps under a POSIX bash",
)


def _fake_bin(tmp_path: Path, gh_body: str) -> None:
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n" f"printf '%s\\n' \"$*\" >> {tmp_path / 'calls'}\n" + gh_body,
        encoding="utf-8",
    )
    gh.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$1\" >> {tmp_path / 'slept'}\nexit 0\n",
        encoding="utf-8",
    )
    sleep.chmod(0o755)
    # One frozen instant for every `date +%s` the step reads, so a wait computed
    # from a reset time is exact however slow the runner is.
    date = tmp_path / "date"
    date.write_text(f"#!/usr/bin/env bash\necho {_NOW}\n", encoding="utf-8")
    date.chmod(0o755)


def _run_step(tmp_path: Path, name: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        PATH=f"{tmp_path}{os.pathsep}{env.get('PATH', '')}",
        GH_TOKEN="x",
        REPO="kirodotdev/KiroCrew",
        BASE="a" * 40,
        HEAD="b" * 40,
        RUNNER_TEMP=str(tmp_path),
        GITHUB_OUTPUT=str(tmp_path / "output"),
    )
    env.update(env_extra)
    return subprocess.run(
        ["bash", "-e", "-c", _step(name)["run"]],
        capture_output=True,
        env=env,
        cwd=tmp_path,
        **UTF8_TEXT,
    )


def _calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "calls"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _probe_gh(exit_code: int) -> str:
    return (
        f"[ {exit_code} -eq 0 ] || echo 'gh: HTTP 503: Service Unavailable' >&2\nexit {exit_code}\n"
    )


@needs_bash
def test_a_retry_that_passes_passes_the_gate(tmp_path: Path) -> None:
    proc = _run_step(
        tmp_path, "Report the review verdict", RETRY="success", PROBE="skipped", LAST="skipped"
    )
    assert proc.returncode == 0, proc.stdout


@needs_bash
def test_two_failures_with_a_working_api_are_a_license_verdict(tmp_path: Path) -> None:
    _fake_bin(tmp_path, _probe_gh(0))
    proc = _run_step(tmp_path, "Probe the dependency-graph API")
    assert proc.returncode == 1
    assert "license verdict" in proc.stdout
    calls = _calls(tmp_path)
    assert len(calls) == 1 and "dependency-graph/compare/" in calls[0], calls


@needs_bash
def test_a_probe_that_finds_the_api_down_records_its_answer(tmp_path: Path) -> None:
    _fake_bin(tmp_path, _probe_gh(1))
    proc = _run_step(tmp_path, "Probe the dependency-graph API")
    assert proc.returncode == 0, proc.stdout
    assert "HTTP 503" in (tmp_path / "output").read_text(encoding="utf-8")
    assert len(_calls(tmp_path)) == 3


@needs_bash
def test_a_last_review_that_passes_passes_the_gate(tmp_path: Path) -> None:
    proc = _run_step(
        tmp_path, "Report the review verdict", RETRY="failure", PROBE="success", LAST="success"
    )
    assert proc.returncode == 0, proc.stdout


@needs_bash
def test_a_last_review_that_fails_fails_closed_naming_the_api_answer(tmp_path: Path) -> None:
    proc = _run_step(
        tmp_path,
        "Report the review verdict",
        RETRY="failure",
        PROBE="success",
        LAST="failure",
        ANSWER="gh: HTTP 503: Service Unavailable",
    )
    assert proc.returncode == 1
    assert "could not run" in proc.stdout
    assert "HTTP 503" in proc.stdout


@pytest.mark.parametrize("probe", ["", "skipped", "failure"])
@needs_bash
def test_any_other_combination_fails_closed(tmp_path: Path, probe: str) -> None:
    proc = _run_step(tmp_path, "Report the review verdict", RETRY="failure", PROBE=probe, LAST="")
    assert proc.returncode == 1


def _rate_limit_gh(remaining: str, reset_in: int | None) -> str:
    reset = "''" if reset_in is None else str(_NOW + reset_in)
    return (
        'case "$*" in\n'
        f"  *remaining*) echo {remaining} ;;\n"
        f"  *reset*) echo {reset} ;;\n"
        "esac\n"
        "exit 0\n"
    )


def _slept(tmp_path: Path) -> int:
    (value,) = (tmp_path / "slept").read_text(encoding="utf-8").split()
    return int(value)


@needs_bash
def test_a_spent_quota_waits_until_it_resets(tmp_path: Path) -> None:
    _fake_bin(tmp_path, _rate_limit_gh("0", 400))
    proc = _run_step(tmp_path, "Wait for the API or the quota reset")
    assert proc.returncode == 0, proc.stdout
    # 400 s to the reset plus the step's 30 s margin.
    assert _slept(tmp_path) == 430


@needs_bash
def test_the_wait_is_capped_at_fifteen_minutes(tmp_path: Path) -> None:
    _fake_bin(tmp_path, _rate_limit_gh("0", 3000))
    _run_step(tmp_path, "Wait for the API or the quota reset")
    assert _slept(tmp_path) == 900


@needs_bash
def test_a_quota_that_is_not_spent_waits_a_fixed_five_minutes(tmp_path: Path) -> None:
    _fake_bin(tmp_path, _rate_limit_gh("4000", 3000))
    _run_step(tmp_path, "Wait for the API or the quota reset")
    assert _slept(tmp_path) == 300

"""Contract tests for .github/workflows/ci-red-census.yml.

The weekly red-cause census spends GitHub GraphQL points from the bucket every
workflow of the repository shares, holds a token, and writes text into a job
summary. Four things are pinned here instead of assumed:

* WHEN AND WHAT -- one weekend schedule and a manual dispatch, nothing a pull
  request can trigger; read-only permissions; one run at a time; pinned actions;
  a checkout that persists no credential and names no ref.
* BUDGET -- the flags the census is run with leave at least half of a 15,000
  point bucket alone, spend at most a third of it an hour, and the waits they
  allow fit the job's timeout together with the reading and the ``gh`` wrapper.
* EXIT CODES -- the census step, executed with ``python3`` stubbed: a whole
  read is green, a read the budget stopped (exit 3) is green with a warning,
  any other exit fails the job, and each writes the job summary.
* INPUTS -- a dispatch input that is not a date never reaches a command line
  and is never echoed.

The behavioural tests skip where there is no POSIX bash.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci-red-census.yml"
RATE_LIMIT_ACTION = "./.github/actions/gh-rate-limit"
CENSUS_STEP = "Take the census"

# What GitHub gives the Actions token for a repository of an Enterprise Cloud
# account, in GraphQL points an hour; the workflow's header cites the page.
BUCKET_POINTS = 15000
# One weekday of this repository took 1,279 seconds to read; seven of them.
READING_SECONDS = 7 * 1279
HOSTED_JOB_LIMIT_MINUTES = 360
# What the census allows one `gh` call that waits for nothing.
CALL_SECONDS = 300


def _document() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers() -> dict:
    document = _document()
    # PyYAML reads a bare ``on:`` key as the boolean True.
    return document.get("on") or document.get(True) or {}


def _job() -> dict:
    (job,) = _document()["jobs"].values()
    return job


def _step(name: str) -> dict:
    (step,) = [step for step in _job()["steps"] if step.get("name") == name]
    return step


def _flag(name: str) -> int:
    match = re.search(rf"{re.escape(name)} (\d+)\b", _step(CENSUS_STEP)["run"])
    assert match, f"the census step no longer passes {name}"
    return int(match.group(1))


# ── When it runs and what it may touch ───────────────────────────────────────


def test_it_runs_weekly_on_a_weekend_and_by_hand_only() -> None:
    triggers = _triggers()
    assert sorted(triggers) == ["schedule", "workflow_dispatch"]
    (tick,) = triggers["schedule"]
    minute, hour, day_of_month, month, day_of_week = tick["cron"].split()
    assert (day_of_month, month) == ("*", "*")
    assert day_of_week in ("0", "6", "7"), "the census runs on a Saturday or a Sunday"
    assert minute.isdigit() and hour.isdigit(), "one tick a week, not a range of them"
    assert sorted(triggers["workflow_dispatch"]["inputs"]) == ["since", "until"]


def test_every_permission_is_read() -> None:
    document = _document()
    assert document["permissions"] == {
        "contents": "read",
        "actions": "read",
        "checks": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    assert "permissions" not in _job(), "a job-level block would replace the read-only one"


def test_two_runs_never_overlap_and_a_running_one_is_not_cancelled() -> None:
    assert _document()["concurrency"] == {"group": "ci-red-census", "cancel-in-progress": False}


def test_every_action_is_pinned_and_the_checkout_keeps_no_credential() -> None:
    uses = [step["uses"] for step in _job()["steps"] if "uses" in step]
    remote = [ref for ref in uses if not ref.startswith("./")]
    assert remote and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref) for ref in remote), uses
    (checkout,) = [
        step
        for step in _job()["steps"]
        if step.get("uses", "").startswith("actions/") and "checkout" in step["uses"]
    ]
    options = checkout["with"]
    assert options["persist-credentials"] is False
    assert "ref" not in options and "repository" not in options, "the default branch only"
    fetched = options["sparse-checkout"].split()
    assert fetched == [".github/actions/gh-rate-limit", ".github/scripts", "scripts"]


def test_no_expression_is_placed_in_a_shell_script() -> None:
    for step in _job()["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")


def test_the_gh_wrapper_is_installed_before_anything_calls_the_api() -> None:
    steps = _job()["steps"]
    install = next(at for at, step in enumerate(steps) if step.get("uses") == RATE_LIMIT_ACTION)
    first_run = next(at for at, step in enumerate(steps) if "run" in step)
    assert install < first_run


def test_the_census_is_kept_as_an_artifact_for_ninety_days() -> None:
    (upload,) = [step for step in _job()["steps"] if "upload-artifact@" in step.get("uses", "")]
    assert upload["with"]["retention-days"] == 90
    assert upload["with"]["path"] == "census/"
    assert "--out-dir census " in _step(CENSUS_STEP)["run"]
    assert "cancelled()" in upload["if"], "a failed census still uploads what it wrote"


# ── The budget ───────────────────────────────────────────────────────────────


def test_the_census_leaves_half_the_bucket_and_spends_a_third_of_it_an_hour_at_most() -> None:
    reserve, hourly = _flag("--reserve-points"), _flag("--points-per-hour")
    assert reserve * 2 >= BUCKET_POINTS
    assert hourly * 3 <= BUCKET_POINTS
    assert reserve + hourly <= BUCKET_POINTS, "an untouched bucket must hold one hour's points"
    # The cap on one run: over the week this repository costs, under two buckets.
    assert 21000 < _flag("--max-points") <= 2 * BUCKET_POINTS
    assert "--logs never " in _step(CENSUS_STEP)["run"]


def test_a_bucket_too_small_for_the_census_fails_before_a_point_is_spent() -> None:
    steps = _job()["steps"]
    names = [step.get("name") for step in steps]
    probe = _step("Read the GraphQL bucket")
    assert names.index("Read the GraphQL bucket") < names.index(CENSUS_STEP)
    needed = int(probe["env"]["NEEDED_POINTS"])
    assert needed == _flag("--reserve-points") + _flag("--points-per-hour")
    assert "gh api rate_limit" in probe["run"], "the one read that costs nothing"
    assert "graphql" not in probe["run"].replace(".resources.graphql", "")


def test_the_waits_and_the_reading_fit_the_job_timeout() -> None:
    job = _job()
    (wrapper,) = [step for step in job["steps"] if step.get("uses") == RATE_LIMIT_ACTION]
    waits = _flag("--max-wait-seconds") + int(wrapper["with"]["max-wait"])
    assert READING_SECONDS + waits < job["timeout-minutes"] * 60
    assert job["timeout-minutes"] < HOSTED_JOB_LIMIT_MINUTES


def test_one_gh_call_outlasts_the_whole_wait_the_wrapper_may_spend_inside_it() -> None:
    (wrapper,) = [step for step in _job()["steps"] if step.get("uses") == RATE_LIMIT_ACTION]
    # The wrapper sleeps inside the `gh` process the census started, and charges
    # the sleep before it starts: a deadline inside the sleep loses both.
    assert _flag("--gh-timeout-seconds") >= int(wrapper["with"]["max-wait"]) + CALL_SECONDS


# ── The census step, executed ────────────────────────────────────────────────

posix_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="requires a POSIX bash to execute the step script",
)

SUMMARY = "### Red-cause census: example/repo, 2026-11-13 to 2026-11-19 UTC (7 day(s))\n"
COUNTS = (
    "GraphQL calls: 12000 (20500 points)\n"
    "GraphQL points left: 9100 (the bucket resets at 2026-11-22T13:00:00Z)\n"
    "Waited: 5400s in 4 wait(s) for the bucket's reset\n"
    "REST calls: 1\n"
    "Run time: 14000.0s\n"
)


def _run_step(
    tmp_path: Path, status: int, since: str = "", until: str = "", wrote: bool = True
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Run the census step with ``python3`` stubbed.

    The stub records its arguments, writes ``census/summary.md`` when ``wrote``,
    prints the summary and the count lines the way the census does, and exits
    ``status``. Returns the finished step, the stub's arguments (empty when it
    was never started) and the job summary.
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    argv_log = tmp_path / "python3.argv"
    (tmp_path / "summary.md").write_text(SUMMARY, encoding="utf-8", newline="\n")
    (tmp_path / "counts.txt").write_text(COUNTS, encoding="utf-8", newline="\n")
    stub = stubs / "python3"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\' "$*" > "{argv_log}"\n'
        + (f'cp "{tmp_path / "summary.md"}" census/summary.md\n' if wrote else "")
        + (f'cat "{tmp_path / "summary.md"}"\n' if wrote else "")
        + f'cat "{tmp_path / "counts.txt"}"\n'
        f"exit {status}\n",
        encoding="utf-8",
        newline="\n",
    )
    stub.chmod(0o755)
    job_summary = tmp_path / "job-summary.md"
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "GH_TOKEN": "stub",
            "REPO": "example/repo",
            "SINCE": since,
            "UNTIL": until,
            "GITHUB_STEP_SUMMARY": str(job_summary),
        }
    )
    done = subprocess.run(
        ["bash", "-c", _step(CENSUS_STEP)["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    argv = argv_log.read_text(encoding="utf-8") if argv_log.exists() else ""
    written = job_summary.read_text(encoding="utf-8") if job_summary.exists() else ""
    return done, argv, written


@posix_bash
def test_a_scheduled_run_reads_the_seven_days_before_today(tmp_path: Path) -> None:
    done, argv, written = _run_step(tmp_path, status=0)

    assert done.returncode == 0, done.stderr
    assert argv == (
        "scripts/ci_red_cause_census.py --last-days 7 --repo example/repo --out-dir census "
        "--logs never --workers 4 --reserve-points 7500 --points-per-hour 5000 "
        "--max-wait-seconds 9000 --max-points 30000 --gh-timeout-seconds 900"
    )
    assert written.startswith(SUMMARY)
    for line in COUNTS.splitlines():
        assert line in written
    assert "::warning::" not in done.stdout and "::error::" not in done.stdout
    assert (tmp_path / "census" / "run.txt").read_text(encoding="utf-8") == SUMMARY + COUNTS


@posix_bash
def test_a_dispatch_reads_the_days_it_was_given(tmp_path: Path) -> None:
    done, argv, _ = _run_step(tmp_path, status=0, since="2026-11-01", until="2026-11-03")

    assert done.returncode == 0, done.stderr
    assert argv.startswith(
        "scripts/ci_red_cause_census.py --since 2026-11-01 --until 2026-11-03 --repo "
    )
    assert "--last-days" not in argv


@posix_bash
@pytest.mark.parametrize(
    ("since", "until"),
    [
        ("2026-11-01", ""),
        ("", "2026-11-03"),
        ("2026-11-01T09:00Z", "2026-11-03"),
        ("2026-11-01", "2026-11-03 --max-points 999999"),
        ("$(touch pwned)", "2026-11-03"),
        ("2026-11-01\n::error::forged", "2026-11-03"),
    ],
)
def test_an_input_that_is_not_a_date_reaches_no_command_and_is_not_echoed(
    tmp_path: Path, since: str, until: str
) -> None:
    done, argv, written = _run_step(tmp_path, status=0, since=since, until=until)

    assert done.returncode == 1
    assert argv == "", "the census was started with an unvalidated input"
    assert done.stdout == (
        "::error::since and until must both be YYYY-MM-DD dates, or both be empty.\n"
    )
    assert written == ""
    assert not (tmp_path / "pwned").exists()


@posix_bash
def test_a_census_the_budget_stopped_is_green_with_a_warning(tmp_path: Path) -> None:
    done, _, written = _run_step(tmp_path, status=3)

    assert done.returncode == 0, done.stderr
    assert "::warning::The census stopped at its API budget" in done.stdout
    assert "::error::" not in done.stdout
    assert written.startswith(SUMMARY) and "GraphQL calls: 12000 (20500 points)" in written


@posix_bash
@pytest.mark.parametrize("status", [1, 2, 137])
def test_a_census_that_could_not_be_taken_fails_the_job_and_says_so(
    tmp_path: Path, status: int
) -> None:
    done, _, written = _run_step(tmp_path, status=status, wrote=False)

    assert done.returncode == 1
    assert f"::error::The census could not be taken (exit {status})" in done.stdout
    assert f"no summary was written (exit {status})" in written
    assert "REST calls: 1" in written


def _run_probe(tmp_path: Path, reply: str, gh_exit: int = 0) -> subprocess.CompletedProcess[str]:
    """Run the bucket step with a ``gh`` stub that prints ``reply`` and exits ``gh_exit``."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    stub = stubs / "gh"
    stub.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' '{reply}'\nexit {gh_exit}\n",
        encoding="utf-8",
        newline="\n",
    )
    stub.chmod(0o755)
    probe = _step("Read the GraphQL bucket")
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "GH_TOKEN": "stub",
            "NEEDED_POINTS": probe["env"]["NEEDED_POINTS"],
            "GITHUB_STEP_SUMMARY": str(tmp_path / "job-summary.md"),
        }
    )
    return subprocess.run(
        ["bash", "-c", probe["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


@posix_bash
def test_an_enterprise_bucket_is_large_enough(tmp_path: Path) -> None:
    done = _run_probe(tmp_path, "15000 14990")

    assert done.returncode == 0, done.stderr
    said = "GraphQL bucket of this token: 15000 points an hour, 14990 left.\n"
    assert done.stdout == said
    assert (tmp_path / "job-summary.md").read_text(encoding="utf-8") == said


@posix_bash
def test_a_thousand_point_bucket_fails_the_job_before_the_census(tmp_path: Path) -> None:
    done = _run_probe(tmp_path, "1000 1000")

    assert done.returncode == 1
    assert "::error::This token's GraphQL bucket is 1000 points an hour" in done.stdout
    assert "Nothing was read." in done.stdout


@posix_bash
@pytest.mark.parametrize(("reply", "gh_exit"), [("", 1), ("null null", 0), ("gh: HTTP 502", 1)])
def test_a_bucket_that_cannot_be_read_leaves_the_guard_to_the_census(
    tmp_path: Path, reply: str, gh_exit: int
) -> None:
    done = _run_probe(tmp_path, reply, gh_exit)

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("::warning::The GraphQL rate limit could not be read")

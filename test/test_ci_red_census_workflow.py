"""Contract tests for .github/workflows/ci-red-census.yml.

The red-cause census spends GitHub GraphQL points from the bucket every workflow
of the repository shares, holds a token, opens artifacts and writes text into a
job summary. What is pinned, for every job of the workflow unless said:

* WHEN AND WHAT -- a schedule and a manual dispatch, nothing a pull request can
  trigger; every permission `read`; one run at a time; pinned actions; a
  checkout that persists no credential and names no ref; no expression inside a
  shell script; the `gh` wrapper before the first script.
* WHEN IN THE DAY -- every tick is in the hours this repository runs least, off
  the top of the hour; the week is added up on exactly one of them, a weekend
  one, after that night's read whatever became of it.
* BUDGET -- one night's read takes at most half of a 15,000-point bucket and
  never starts a call under half of it; the bucket is read, for nothing, before
  any point is spent.
* DEADLINES -- each deadline is longer than the wait inside it: one `gh` call
  against the wrapper's wait, each job's timeout against its work and that wait.
* THE STEPS, executed with `python3` or `gh` stubbed -- what each is started
  with, what each exit code does to the job and to the job summary, and that an
  input that is not a date starts nothing and is not echoed.

The executed tests skip where there is no POSIX bash.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci-red-census.yml"
RATE_LIMIT_ACTION = "./.github/actions/gh-rate-limit"
DAY_STEP = "Take the census"
WEEK_STEP = "Add the week up"
PROBE_STEP = "Read the GraphQL bucket"

# What GitHub gives the Actions token for a repository of an Enterprise Cloud
# account, in GraphQL points an hour; the workflow's header cites the page.
BUCKET_POINTS = 15000
# One weekday of this repository: the points it cost and the seconds it took.
WEEKDAY_POINTS = 2786
WEEKDAY_SECONDS = 1279
# The UTC hours with the fewest runs created, on the weekday that was counted.
QUIET_HOURS = range(5, 9)
# What the census allows one `gh` call that waits for nothing.
CALL_SECONDS = 300


def _document() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def _triggers() -> dict[str, Any]:
    document = _document()
    # PyYAML reads a bare ``on:`` key as the boolean True.
    triggers: dict[str, Any] = document.get("on") or document.get(True) or {}
    return triggers


def _jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = _document()["jobs"]
    return jobs


JOB_IDS = sorted(_jobs())
every_job = pytest.mark.parametrize("job_id", JOB_IDS)


def _step(name: str) -> dict[str, Any]:
    (step,) = [
        step for job in _jobs().values() for step in job["steps"] if step.get("name") == name
    ]
    return step


def _flag(step: str, name: str) -> int:
    match = re.search(rf"{re.escape(name)} (\d+)\b", _step(step)["run"])
    assert match, f"{step!r} no longer passes {name}"
    return int(match.group(1))


def _wrapper_wait(job_id: str) -> int:
    (wrapper,) = [s for s in _jobs()[job_id]["steps"] if s.get("uses") == RATE_LIMIT_ACTION]
    return int(wrapper["with"]["max-wait"])


def _ticks() -> list[tuple[str, int, int, str]]:
    """``(cron, minute, hour, days of the week)`` of every schedule entry."""
    found = []
    for entry in _triggers()["schedule"]:
        minute, hour, day_of_month, month, days = entry["cron"].split()
        assert (day_of_month, month) == ("*", "*")
        found.append((entry["cron"], int(minute), int(hour), days))
    return found


def _days(spec: str) -> set[int]:
    """The days of the week a cron field names, Sunday as 0."""
    days: set[int] = set()
    for part in spec.split(","):
        first, _, last = part.partition("-")
        days |= {day % 7 for day in range(int(first), int(last or first) + 1)}
    return days


# ── When it runs and what it may touch ───────────────────────────────────────


def test_only_a_schedule_or_a_person_starts_it() -> None:
    triggers = _triggers()
    assert sorted(triggers) == ["schedule", "workflow_dispatch"]
    assert sorted(triggers["workflow_dispatch"]["inputs"]) == ["day", "week"]


def test_there_are_exactly_the_jobs_this_file_knows() -> None:
    assert JOB_IDS == ["day", "week"]


def test_every_permission_is_read() -> None:
    document = _document()
    assert document["permissions"]
    assert set(document["permissions"].values()) == {"read"}
    assert document["permissions"] == {
        "contents": "read",
        "actions": "read",
        "checks": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    for job in _jobs().values():
        assert "permissions" not in job, "a job-level block would replace the read-only one"


def test_two_runs_never_overlap_and_a_running_one_is_not_cancelled() -> None:
    assert _document()["concurrency"] == {"group": "ci-red-census", "cancel-in-progress": False}


@every_job
def test_every_action_is_pinned_and_the_checkout_keeps_no_credential(job_id: str) -> None:
    steps = _jobs()[job_id]["steps"]
    remote = [step["uses"] for step in steps if not step.get("uses", "./").startswith("./")]
    assert remote and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref) for ref in remote), remote
    (checkout,) = [step for step in steps if step.get("uses", "").startswith("actions/checkout@")]
    options = checkout["with"]
    assert options["persist-credentials"] is False
    assert "ref" not in options and "repository" not in options, "the default branch only"
    assert options["sparse-checkout"].split() == [
        ".github/actions/gh-rate-limit",
        ".github/scripts",
        "scripts",
    ]


@every_job
def test_no_expression_is_placed_in_a_shell_script(job_id: str) -> None:
    for step in _jobs()[job_id]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")


@every_job
def test_the_gh_wrapper_is_installed_before_the_first_script(job_id: str) -> None:
    steps = _jobs()[job_id]["steps"]
    install = next(at for at, step in enumerate(steps) if step.get("uses") == RATE_LIMIT_ACTION)
    assert install < next(at for at, step in enumerate(steps) if "run" in step)


@every_job
def test_what_a_job_wrote_is_kept_for_ninety_days_even_when_it_failed(job_id: str) -> None:
    steps = _jobs()[job_id]["steps"]
    (upload,) = [step for step in steps if "upload-artifact@" in step.get("uses", "")]
    assert upload["with"]["retention-days"] == 90
    assert "cancelled()" in upload["if"]
    folder = upload["with"]["path"].rstrip("/")
    scripts = " ".join(step.get("run", "") for step in steps)
    assert f"--out-dir {folder} " in scripts


def test_the_week_looks_for_the_artifact_the_night_uploads() -> None:
    spec = importlib.util.spec_from_file_location(
        "ci_red_census_week_for_workflow_test", ROOT / "scripts" / "ci_red_census_week.py"
    )
    assert spec is not None and spec.loader is not None
    weekly = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(weekly)
    (upload,) = [s for s in _jobs()["day"]["steps"] if "upload-artifact@" in s.get("uses", "")]
    assert upload["with"]["name"] == weekly.ARTIFACT
    assert f"--workflow {WORKFLOW.name} " in _step(WEEK_STEP)["run"]


# ── When in the day ──────────────────────────────────────────────────────────


def test_every_tick_is_in_the_quiet_hours_and_off_the_top_of_the_hour() -> None:
    ticks = _ticks()
    assert len({(minute, hour) for _cron, minute, hour, _days_ in ticks}) == 1, "one time a day"
    for _cron, minute, hour, _days_ in ticks:
        assert hour in QUIET_HOURS
        assert minute not in (0, 30), "scheduled runs are delayed most at the top of the hour"


def test_each_day_of_the_week_has_exactly_one_tick() -> None:
    named = [day for _cron, _minute, _hour, days in _ticks() for day in sorted(_days(days))]
    assert sorted(named) == list(range(7))


def test_the_week_is_added_up_on_one_weekend_tick_after_the_nights_read() -> None:
    week = _jobs()["week"]
    condition = week["if"]
    named = [cron for cron, _minute, _hour, _days_ in _ticks() if f"'{cron}'" in condition]
    assert len(named) == 1, "the week job names exactly one schedule entry"
    (days,) = [days for cron, _minute, _hour, days in _ticks() if cron == named[0]]
    assert _days(days) <= {0, 6} and len(_days(days)) == 1
    assert "inputs.week" in condition
    # It waits for the night's read, and runs whether or not that read failed.
    assert week["needs"] == "day"
    assert "!cancelled()" in condition and "success()" not in condition


# ── The budget ───────────────────────────────────────────────────────────────


def test_one_night_takes_half_the_bucket_at_most_and_leaves_half_alone() -> None:
    reserve, most = _flag(DAY_STEP, "--reserve-points"), _flag(DAY_STEP, "--max-points")
    assert reserve * 2 >= BUCKET_POINTS
    assert most * 2 <= BUCKET_POINTS
    assert most + reserve <= BUCKET_POINTS, "an untouched bucket holds the most a run may spend"
    assert most >= 2 * WEEKDAY_POINTS, "a heavy day still fits"
    assert "--logs never " in _step(DAY_STEP)["run"]


def test_the_census_never_waits_for_a_bucket() -> None:
    run = _step(DAY_STEP)["run"]
    assert "sleep" not in run and "--max-wait" not in run


def test_a_bucket_too_small_for_a_day_fails_before_a_point_is_spent() -> None:
    names = [step.get("name") for step in _jobs()["day"]["steps"]]
    assert names.index(PROBE_STEP) < names.index(DAY_STEP)
    probe = _step(PROBE_STEP)
    needed = int(probe["env"]["NEEDED_POINTS"])
    assert _flag(DAY_STEP, "--reserve-points") + WEEKDAY_POINTS <= needed <= BUCKET_POINTS
    assert "gh api rate_limit" in probe["run"], "the one read that costs nothing"
    assert "graphql" not in probe["run"].replace(".resources.graphql", "")


def test_the_week_spends_no_graphql_point() -> None:
    run = _step(WEEK_STEP)["run"]
    assert "ci_red_census_week.py" in run and "ci_red_cause_census.py" not in run


# ── Deadlines ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("job_id", "step"), [("day", DAY_STEP), ("week", WEEK_STEP)])
def test_one_gh_call_outlasts_the_wait_the_wrapper_may_spend_inside_it(
    job_id: str, step: str
) -> None:
    # The wrapper sleeps inside the `gh` process the script started, and charges
    # the sleep before it starts: a deadline inside the sleep loses both.
    assert _flag(step, "--gh-timeout-seconds") >= _wrapper_wait(job_id) + CALL_SECONDS


@every_job
def test_each_job_timeout_holds_its_work_and_the_wrappers_wait(job_id: str) -> None:
    job = _jobs()[job_id]
    work = 2 * WEEKDAY_SECONDS if job_id == "day" else CALL_SECONDS
    assert work + _wrapper_wait(job_id) < job["timeout-minutes"] * 60
    assert job["timeout-minutes"] <= 60, "no job of the census holds a runner for hours"


def test_no_job_asks_for_a_larger_runner() -> None:
    for job in _jobs().values():
        assert job["runs-on"] == "ubuntu-latest"


# ── The steps, executed ──────────────────────────────────────────────────────

posix_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="requires a POSIX bash to execute the step script",
)

SUMMARY = "### Red-cause census: example/repo, 2026-11-19 to 2026-11-19 UTC (1 day(s))\n"
COUNTS = "GraphQL calls: 1900 (2900 points)\nREST calls: 1\nRun time: 1300.0s\n"


def _run_script(
    tmp_path: Path, step: str, status: int, wrote: bool = True, **inputs: str
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Run one script step with ``python3`` stubbed.

    The stub records its arguments, writes ``summary.md`` into the directory it
    was given as ``--out-dir`` when ``wrote``, prints the summary and the count
    lines the way the scripts do, and exits ``status``. Returns the finished
    step, the stub's arguments (empty when it was never started) and the job
    summary.
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
        'out=""\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--out-dir" ]; then out="$2"; fi\n'
        "  shift\n"
        "done\n"
        + (f'cp "{tmp_path / "summary.md"}" "$out/summary.md"\n' if wrote else "")
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
            "BRANCH": "main",
            "DAY": inputs.get("day", ""),
            "GITHUB_STEP_SUMMARY": str(job_summary),
        }
    )
    done = subprocess.run(
        ["bash", "-c", _step(step)["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    argv = argv_log.read_text(encoding="utf-8") if argv_log.exists() else ""
    written = job_summary.read_text(encoding="utf-8") if job_summary.exists() else ""
    return done, argv, written


both_scripts = pytest.mark.parametrize("step", [DAY_STEP, WEEK_STEP])


@posix_bash
def test_a_night_reads_yesterday(tmp_path: Path) -> None:
    done, argv, written = _run_script(tmp_path, DAY_STEP, status=0)

    assert done.returncode == 0, done.stderr
    words = argv.split()
    assert words[:3] == ["scripts/ci_red_cause_census.py", "--last-days", "1"]
    assert "--since" not in words and "--until" not in words
    assert words[words.index("--repo") + 1] == "example/repo"
    assert written.startswith(SUMMARY)
    for line in COUNTS.splitlines():
        assert line in written
    assert "::warning::" not in done.stdout and "::error::" not in done.stdout


@posix_bash
def test_a_dispatch_reads_the_day_it_was_given(tmp_path: Path) -> None:
    done, argv, _ = _run_script(tmp_path, DAY_STEP, status=0, day="2026-11-03")

    assert done.returncode == 0, done.stderr
    words = argv.split()
    assert words[1:5] == ["--since", "2026-11-03", "--until", "2026-11-03"]
    assert "--last-days" not in words


@posix_bash
def test_the_week_ends_yesterday_or_at_the_day_it_was_given(tmp_path: Path) -> None:
    done, argv, written = _run_script(tmp_path, WEEK_STEP, status=0)
    assert done.returncode == 0, done.stderr
    words = argv.split()
    assert words[0] == "scripts/ci_red_census_week.py" and "--until" not in words
    assert words[words.index("--branch") + 1] == "main"
    assert written.startswith(SUMMARY) and "REST calls: 1" in written

    other = tmp_path / "given"
    other.mkdir()
    done, argv, _ = _run_script(other, WEEK_STEP, status=0, day="2026-11-03")
    assert done.returncode == 0, done.stderr
    assert argv.split()[1:3] == ["--until", "2026-11-03"]


@posix_bash
@both_scripts
@pytest.mark.parametrize(
    "day",
    [
        "2026-11-01T09:00Z",
        "2026-11-03 --max-points 999999",
        "$(touch pwned)",
        "2026-11-01\n::error::forged",
        "yesterday",
    ],
)
def test_an_input_that_is_not_a_date_starts_nothing_and_is_not_echoed(
    tmp_path: Path, step: str, day: str
) -> None:
    done, argv, written = _run_script(tmp_path, step, status=0, day=day)

    assert done.returncode == 1
    assert argv == "", "the script was started with an unvalidated input"
    assert done.stdout.startswith("::error::") and done.stdout.count("\n") == 1
    assert day not in done.stdout and "forged" not in done.stdout
    assert written == ""
    assert not (tmp_path / "pwned").exists()


@posix_bash
@both_scripts
def test_exit_3_is_green_with_a_warning_and_a_summary(tmp_path: Path, step: str) -> None:
    done, _, written = _run_script(tmp_path, step, status=3)

    assert done.returncode == 0, done.stderr
    assert "::warning::" in done.stdout and "::error::" not in done.stdout
    assert written.startswith(SUMMARY) and "REST calls: 1" in written


@posix_bash
@both_scripts
@pytest.mark.parametrize("status", [1, 2, 137])
def test_any_other_exit_fails_the_job_and_says_so(tmp_path: Path, step: str, status: int) -> None:
    done, _, written = _run_script(tmp_path, step, status=status, wrote=False)

    assert done.returncode == 1
    assert "::error::" in done.stdout and f"(exit {status})" in done.stdout
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
    probe = _step(PROBE_STEP)
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
    done = _run_probe(tmp_path, f"{BUCKET_POINTS} 14990")

    assert done.returncode == 0, done.stderr
    assert "::" not in done.stdout
    assert "15000" in done.stdout and "14990" in done.stdout
    assert (tmp_path / "job-summary.md").read_text(encoding="utf-8") == done.stdout


@posix_bash
def test_a_thousand_point_bucket_fails_the_job_before_the_census(tmp_path: Path) -> None:
    done = _run_probe(tmp_path, "1000 1000")

    assert done.returncode == 1
    assert "::error::" in done.stdout and "1000 points an hour" in done.stdout


@posix_bash
@pytest.mark.parametrize(("reply", "gh_exit"), [("", 1), ("null null", 0), ("gh: HTTP 502", 1)])
def test_a_bucket_that_cannot_be_read_leaves_the_guard_to_the_census(
    tmp_path: Path, reply: str, gh_exit: int
) -> None:
    done = _run_probe(tmp_path, reply, gh_exit)

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("::warning::")

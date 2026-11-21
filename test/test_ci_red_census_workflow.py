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
* BUDGET -- the census's reserve and its cap are not written in the workflow:
  the step before it sizes them to the bucket the token has, the census step
  takes them from that step and starts nothing without them. The sizing rule
  has its own tests (``test_ci_red_census_budget.py``).
* BOTH EVENTS -- every expression a step's ``env:`` holds has a value under a
  ``schedule`` payload (no inputs, no ``repository`` object) and under a
  dispatch, and the executed steps below get their environment from those
  expressions, not from a hand-written copy.
* DEADLINES -- each deadline is longer than the wait inside it: one `gh` call
  against the wrapper's wait, each job's timeout against its work and that wait.
* THE STEPS, executed with `python3` or `gh` stubbed -- what each is started
  with, what each exit code does to the job and to the job summary, and that an
  input that is not a date starts nothing and is not echoed.

The executed tests skip where there is no POSIX bash.
"""

from __future__ import annotations

import importlib.util
import json
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
BUDGET_STEP = "Size the budget to the bucket"

# What this repository's Actions token has, in GraphQL points an hour, and what
# was left of it when the first scheduled run read it.
BUCKET_POINTS = 10000
BUCKET_LEFT = 9353
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
    assert triggers and set(triggers) <= {
        "schedule",
        "workflow_dispatch",
    }, "an event a pull request can raise would run this with the repository's token"
    assert "schedule" in triggers and "workflow_dispatch" in triggers


def test_a_night_and_a_week_job_exist() -> None:
    assert {"day", "week"} <= set(JOB_IDS)


def test_every_permission_is_read() -> None:
    document = _document()
    assert document["permissions"], "no block at all would mean the token's defaults"
    assert set(document["permissions"].values()) == {"read"}
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
    # The wrapper, the census scripts and the watcher modules they load.
    assert {".github/actions/gh-rate-limit", ".github/scripts", "scripts"} <= set(
        options["sparse-checkout"].split()
    )


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


def test_the_week_takes_this_runs_day_without_listing_it() -> None:
    """An artifact of a run still in progress may not be listed yet; a later job of
    the same run can always download it."""
    week = _jobs()["week"]
    steps = week["steps"]
    (upload,) = [s for s in _jobs()["day"]["steps"] if "upload-artifact@" in s.get("uses", "")]
    (at,) = [i for i, s in enumerate(steps) if "download-artifact@" in s.get("uses", "")]
    fetch = steps[at]
    assert week["needs"] == "day"
    assert fetch["with"]["name"] == upload["with"]["name"]
    # No run id and no token: the same run's artifact, and no other run's.
    assert not {"run-id", "github-token", "repository"} & set(fetch["with"])
    # The night's read may have failed and uploaded nothing.
    assert fetch["continue-on-error"] is True
    names = [step.get("name") for step in steps]
    assert at < names.index(WEEK_STEP)
    assert f"--local {fetch['with']['path']} " in _step(WEEK_STEP)["run"]


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


def test_the_budget_is_sized_by_the_step_before_the_census_not_written_here() -> None:
    names = [step.get("name") for step in _jobs()["day"]["steps"]]
    assert names.index(BUDGET_STEP) < names.index(DAY_STEP)
    census = _step(DAY_STEP)
    run = census["run"]
    # No number of points is written in the census step: both come from the
    # step that read the bucket.
    assert not re.search(r"--(reserve|max)-points \d", run)
    step_id = _step(BUDGET_STEP)["id"]
    given = {
        flag: name
        for flag in ("--reserve-points", "--max-points")
        for name, value in census["env"].items()
        if f'{flag} "${name}"' in run and f"steps.{step_id}.outputs." in value
    }
    assert sorted(given) == ["--max-points", "--reserve-points"]
    assert given["--reserve-points"] != given["--max-points"]
    assert "--logs never " in run


def test_the_budget_is_sized_for_the_workers_the_census_runs() -> None:
    """The room left for calls in flight is one call a worker."""
    sized = re.search(r"--workers (\d+)\b", _step(BUDGET_STEP)["run"])
    assert sized and int(sized.group(1)) == _flag(DAY_STEP, "--workers")


def test_the_budget_step_expects_a_weekday() -> None:
    needs = _flag(BUDGET_STEP, "--needs-points")
    assert WEEKDAY_POINTS <= needs <= WEEKDAY_POINTS * 5 // 4


def test_the_census_never_waits_for_a_bucket() -> None:
    for name in (BUDGET_STEP, DAY_STEP):
        run = _step(name)["run"]
        assert "sleep" not in run and "--max-wait" not in run


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


# ── Both events ──────────────────────────────────────────────────────────────

SUNDAY_TICK = "17 7 * * 0"
# What the budget step hands the census step when the bucket holds a read.
BUDGET_OUTPUTS = {"reserve": "5120", "most": "3880"}


def _context(event: str, **inputs: Any) -> dict[str, Any]:
    """The contexts an expression can read, shaped as GitHub shapes them for ``event``.

    A ``schedule`` payload is ``{"schedule": <cron>}`` and nothing else: no
    inputs, no ``repository``, no ``sender``. On it ``github.ref_name`` is the
    default branch.
    """
    github = {"token": "stub", "repository": "example/repo", "ref_name": "main"}
    steps = {"budget": {"outputs": dict(BUDGET_OUTPUTS)}}
    if event == "schedule":
        schedule = {**github, "event": {"schedule": SUNDAY_TICK}}
        return {"github": schedule, "inputs": {}, "steps": steps}
    given = {"day": "", "week": False, **inputs}
    payload = {"inputs": given, "repository": {"default_branch": "main"}}
    return {"github": {**github, "event": payload}, "inputs": given, "steps": steps}


def _evaluate(value: str, context: dict[str, Any]) -> str:
    """``value`` with each ``${{ a.b || c.d }}`` replaced as GitHub would.

    Only property paths joined by ``||`` are understood; anything else fails
    here, so a new kind of expression has to be taught to this test.
    """

    def lookup(path: str) -> Any:
        assert re.fullmatch(r"[a-z_]+(\.[a-z_]+)+", path), f"unknown expression: {path!r}"
        found: Any = context
        for part in path.split("."):
            found = found.get(part) if isinstance(found, dict) else None
        return found

    def replace(match: re.Match[str]) -> str:
        chosen: Any = None
        for path in match.group(1).split("||"):
            chosen = lookup(path.strip())
            if chosen:
                break
        # A null property renders as the empty string.
        return "" if chosen is None else str(chosen)

    return re.sub(r"\$\{\{(.*?)\}\}", replace, str(value))


def _env(step: str, context: dict[str, Any]) -> dict[str, str]:
    return {name: _evaluate(value, context) for name, value in _step(step)["env"].items()}


ALL_ENV_STEPS = sorted(
    step["name"] for job in _jobs().values() for step in job["steps"] if "env" in step
)


EVENTS = {
    "a schedule tick": _context("schedule"),
    "a dispatch with nothing filled in": _context("workflow_dispatch"),
    "a dispatch for a day": _context("workflow_dispatch", day="2026-11-03"),
    "a dispatch for a week": _context("workflow_dispatch", day="2026-11-03", week=True),
}


@pytest.mark.parametrize("step", ALL_ENV_STEPS)
@pytest.mark.parametrize("event", EVENTS.values(), ids=EVENTS.keys())
def test_every_value_a_step_is_given_exists_under_every_event(
    step: str, event: dict[str, Any]
) -> None:
    env = _env(step, event)
    # `day` is the one value that may be empty: it then means yesterday.
    assert [name for name, value in env.items() if not value] in ([], ["DAY"])
    if "DAY" in env:
        assert env["DAY"] == event["inputs"].get("day", "")
    assert env.get("BRANCH", "main") == "main"
    assert env.get("REPO", "example/repo") == "example/repo"


def test_no_job_reads_a_part_of_the_event_a_schedule_lacks() -> None:
    """A schedule payload holds its cron and nothing else, so any other part of the
    event is read with a fallback behind it."""
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        for field in re.findall(r"github\.event\.([a-z_]+)", line):
            assert field == "schedule" or "||" in line, line


# ── The steps, executed ──────────────────────────────────────────────────────

posix_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="requires a POSIX bash to execute the step script",
)

SUMMARY = "### Red-cause census: example/repo, 2026-11-19 to 2026-11-19 UTC (1 day(s))\n"
COUNTS = "GraphQL calls: 1900 (2900 points)\nREST calls: 1\nRun time: 1300.0s\n"


def _run_script(
    tmp_path: Path,
    step: str,
    status: int,
    wrote: bool = True,
    context: dict[str, Any] | None = None,
    **inputs: str,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Run one script step with ``python3`` stubbed, under the environment its own
    ``env:`` gives it: a ``schedule`` event with no ``inputs``, a dispatch with.

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
    event = context or (_context("workflow_dispatch", **inputs) if inputs else _context("schedule"))
    env = dict(os.environ)
    env.update(_env(step, event))
    env.update(
        {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
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


def _load_budget() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ci_red_census_budget_for_workflow_test", ROOT / "scripts" / "ci_red_census_budget.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _size_in_process(
    tmp_path: Path, limit: Any, remaining: Any, gh_exit: int, capsys: pytest.CaptureFixture[str]
) -> tuple[list[str], int, str, str]:
    """What the sizing script does for a bucket of ``limit`` points with ``remaining``
    left, called in this process with the flags the workflow step passes it.

    Returns those flags, the exit status, what it printed and what it wrote to
    ``GITHUB_OUTPUT``.
    """
    flags = [
        "--needs-points",
        str(_flag(BUDGET_STEP, "--needs-points")),
        "--workers",
        str(_flag(BUDGET_STEP, "--workers")),
    ]
    reply = json.dumps({"resources": {"graphql": {"limit": limit, "remaining": remaining}}})
    outputs = tmp_path / "sized-outputs.txt"
    capsys.readouterr()
    status = _load_budget().main(
        flags, run=lambda argv: (gh_exit, reply), environ={"GITHUB_OUTPUT": str(outputs)}
    )
    printed = capsys.readouterr().out
    wrote = outputs.read_text(encoding="utf-8") if outputs.exists() else ""
    return flags, int(status), printed, wrote


def _run_budget_step(
    tmp_path: Path, status: int, printed: str, wrote: str
) -> tuple[subprocess.CompletedProcess[str], str, dict[str, str], str]:
    """Run the budget step with ``python3`` stubbed to do what the sizing script did:
    print ``printed``, add ``wrote`` to ``GITHUB_OUTPUT`` and exit ``status``.

    Returns the finished step, the stub's arguments, the step's outputs and the
    job summary.
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    argv_log = tmp_path / "python3.argv"
    (tmp_path / "printed.txt").write_text(printed, encoding="utf-8", newline="\n")
    (tmp_path / "wrote.txt").write_text(wrote, encoding="utf-8", newline="\n")
    stub = stubs / "python3"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\' "$*" > "{argv_log}"\n'
        f'cat "{tmp_path / "printed.txt"}"\n'
        f'cat "{tmp_path / "wrote.txt"}" >> "$GITHUB_OUTPUT"\n'
        f"exit {status}\n",
        encoding="utf-8",
        newline="\n",
    )
    stub.chmod(0o755)
    outputs = tmp_path / "outputs.txt"
    job_summary = tmp_path / "job-summary.md"
    env = dict(os.environ)
    env.update(_env(BUDGET_STEP, _context("schedule")))
    env.update(
        {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(outputs),
            "GITHUB_STEP_SUMMARY": str(job_summary),
        }
    )
    done = subprocess.run(
        ["bash", "-c", _step(BUDGET_STEP)["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    argv = argv_log.read_text(encoding="utf-8") if argv_log.exists() else ""
    lines = outputs.read_text(encoding="utf-8").splitlines() if outputs.exists() else []
    given = dict(line.split("=", 1) for line in lines)
    written = job_summary.read_text(encoding="utf-8") if job_summary.exists() else ""
    return done, argv, given, written


@posix_bash
def test_the_bucket_this_repository_has_runs_a_weekday(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first scheduled run read 10,000 points an hour with 9,353 left."""
    flags, status, printed, wrote = _size_in_process(
        tmp_path, BUCKET_POINTS, BUCKET_LEFT, 0, capsys
    )
    assert status == 0, printed

    done, argv, given, written = _run_budget_step(tmp_path, status, printed, wrote)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "::error::" not in done.stdout
    # The step starts the sizing script with the flags it was sized with here.
    assert argv.split() == ["scripts/ci_red_census_budget.py", *flags]
    assert written == printed
    assert f"{BUCKET_POINTS} points an hour, {BUCKET_LEFT} left" in written
    reserve, most = int(given["reserve"]), int(given["most"])
    assert most >= WEEKDAY_POINTS and BUCKET_LEFT - reserve >= WEEKDAY_POINTS
    assert reserve * 2 >= BUCKET_POINTS and most * 5 <= BUCKET_POINTS * 2

    # What the step set is what the census is started with.
    context = _context("schedule")
    context["steps"]["budget"]["outputs"] = given
    census = tmp_path / "census-step"
    census.mkdir()
    done, argv, _ = _run_script(census, DAY_STEP, status=0, context=context)
    assert done.returncode == 0, done.stderr
    words = argv.split()
    assert words[words.index("--reserve-points") + 1] == given["reserve"]
    assert words[words.index("--max-points") + 1] == given["most"]


@posix_bash
@pytest.mark.parametrize(
    ("limit", "remaining", "gh_exit"),
    [
        (1000, 1000, 0),  # a bucket no census fits
        (BUCKET_POINTS, 6000, 0),  # other jobs spent this hour's bucket
        (BUCKET_POINTS, BUCKET_LEFT, 1),  # the read failed
        (None, None, 0),  # the reply names no bucket
        ("many", "some", 0),
    ],
)
def test_a_bucket_that_does_not_hold_the_read_starts_no_census(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], limit: Any, remaining: Any, gh_exit: int
) -> None:
    _flags, status, printed, wrote = _size_in_process(tmp_path, limit, remaining, gh_exit, capsys)
    assert status == 1 and wrote == ""

    done, _argv, given, written = _run_budget_step(tmp_path, status, printed, wrote)

    assert done.returncode == 1
    assert given == {}, "a refused budget hands the census nothing"
    assert "::error::" in done.stdout
    assert "Refused:" in written and "Nothing was read." in written


def test_no_census_test_starts_an_interpreter_of_its_own() -> None:
    """A step test runs bash with ``python3`` and ``gh`` stubbed, and a script's own
    logic is called in process: several real interpreters cold-started side by
    side on the workers have stalled a shard."""
    launch = "sys." + "executable"
    for name in (
        "test_ci_red_census_workflow.py",
        "test_ci_red_census_budget.py",
        "test_ci_red_census_week.py",
    ):
        assert launch not in (ROOT / "test" / name).read_text(encoding="utf-8"), name


@posix_bash
@pytest.mark.parametrize(
    "outputs",
    [{}, {"reserve": "5120"}, {"reserve": "", "most": ""}, {"reserve": "5120", "most": "3880 --x"}],
)
def test_the_census_is_not_started_without_a_whole_budget(
    tmp_path: Path, outputs: dict[str, str]
) -> None:
    context = _context("schedule")
    context["steps"]["budget"]["outputs"] = outputs
    done, argv, written = _run_script(tmp_path, DAY_STEP, status=0, context=context)

    assert done.returncode == 1
    assert argv == "" and written == ""
    assert done.stdout.startswith("::error::") and done.stdout.count("\n") == 1

#!/usr/bin/env python3
"""Red-cause census: one cause for every red CI job in a date window.

A red check should mean the pull request's own change broke something. This
read-only report says how often it means something else:

    python3 scripts/ci_red_cause_census.py --since 2026-11-20T09:00Z \\
        --until 2026-11-20T12:00Z --repo kirodotdev/KiroCrew --out-dir census

It reads every workflow's runs created in the window and writes three files to
``--out-dir``. A bound is a UTC day (``2026-11-20``; as ``--until`` the whole day
is included) or a UTC minute (``2026-11-20T09:00Z``). ``--last-days 7`` is the
seven whole UTC days before today, in place of both bounds.

    red-jobs.csv   one row per red job: run id, job id, workflow, job name,
                   event, head SHA, PR number, cause, and the evidence that
                   decided the cause (80 printable ASCII characters at most); no cell
                   holds a control character
    summary.md     the three goal numbers with their n, and the cause counts
    summary.json   the same numbers as data

The summary, the GraphQL and REST call counts and the run time are printed too.

One cause per job. The rules are checked in this order and the first match wins:

1. ``rate_limit``      a failure annotation or the log names an API rate limit.
2. ``broken_main``     a failing test of the job is listed in a `main-red` issue
                       that was open when the job started.
3. ``infrastructure``  a runner or worker signature: the runner was lost or shut
                       down, an xdist worker crashed, the job ran out of memory
                       or disk, it timed out, or its checkout or setup step
                       failed.
4. ``flaky``           the same job of the same workflow on the same head SHA
                       passed later (a re-run).
5. ``diff``            none of the above: the change under test is the cause.

A red job is a check run that concluded `failure`, `timed_out` or
`startup_failure`. A `cancelled` one is red only when a failure annotation
carries an infrastructure signature (the runner ended it); a run a newer push
or a person cancelled is not a red check and is not counted. Cancelled jobs are
read only in a run that has five of them or fewer: more is a cancelled run.

Failing tests are read with the main-red watcher's own parser
(`.github/scripts/main_red_nodeids.py`) and the `@group` suffix is dropped with
`scripts/ci_main_red_skip.py`'s `without_group`, so a node id means the same
thing here as in the issue and in the skip.

API calls go through `gh api`, so a workflow that installs
`.github/actions/gh-rate-limit` gets its bounded wait on every call. Runs and
the main-red issues are read by GraphQL in pages of 100. Each page of runs
carries, per run, the count of its red jobs over every attempt, and only a run
that has one, or that ended cancelled, has its check runs read: ten check
suites to a call, with their annotations, steps and passes (fifty to a call
for a cancelled run with no red job, which is asked for less). REST is used for
the two things GraphQL lacks: the workflow list and a job's log.

A busy day here is some ninety thousand runs, a thousand pages, so ``--workers``
reads that many workflows, and that many batches of check suites, at a time (4
by default); the rows and the counts do not depend on it.

``--logs never`` (the default) reads no log. ``--logs undecided`` reads the log
of a job whose annotations name nothing: one REST call a job, refused where
the token cannot read logs, so ask for it only with a token that can. In a log
only an error the job
reported counts: a `##[error]` line, `gh`'s error output, a pytest `FAILED` or
`ERROR` summary line. The Actions token cannot read a job log in
this repository, so a job whose annotations name nothing and whose log was not
read is classified from what is left (its conclusion, its failed step, a later
pass) and counted in ``log_unread``.

What it costs, and the guard. The census spends GitHub GraphQL points, and
the bucket it spends is shared: a person's token has 5,000 an hour for every
session of that account, and a workflow's token shares its pool with the rest
of CI (#39367). One day of this repository (2026-11-19: 91,201 runs of 99
workflows) cost 1,828 calls and 2,786 points. So:

- every query reads ``rateLimit { cost remaining resetAt }``, and the census
  stops before a call that would start with fewer than ``--reserve-points``
  left (2,000 by default);
- it stops after spending ``--max-points`` (1,500 by default, about half a
  day of runs here and under a third of one hour's bucket).

A census stopped either way spends nothing more. A week of this repository is
some 20,000 points, more than any one hour should give up, so a census can
wait instead of stopping: with ``--max-wait-seconds`` it sleeps until the
bucket's reset when the reserve is reached, and with ``--points-per-hour`` also
after spending that many points since it last waited, so no hour of the bucket
gives up more than that (plus one call a worker already in flight). The waits
share the ``--max-wait-seconds`` budget; a reset past what is left of it stops
the census as above. Runs are listed newest first and GitHub has no date filter
for them, so a window read as separate days would page through every newer run
once a day; one paced census lists them once.

A check suite is counted whole
or not at all: one the stop left part read, and any other run of the same
workflow and head SHA, is left out and counted in ``suites_unread``, so no job
is reported with a cause a complete read could have changed. It still writes its files,
says ``Partial`` at the top of the summary with the reason and the span of the
runs it did read, sets ``partial`` in the JSON and exits 3 (1 is a failed
read): its counts cover only what was read. A whole day or more belongs in a
workflow, under the workflow's own token and after
``.github/actions/gh-rate-limit``, paced with ``--points-per-hour`` and with
``--max-points`` raised to fit (`ci-red-census.yml`); do not spend a person's
bucket on it.

Annotation and log text is untrusted, and so is a job's name. None of it is
placed in a shell command. Annotation and log text is cut to printable ASCII
before it reaches a file; a name reaches the CSV only, with its control
characters replaced, and never the summary or what is printed.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import json
import re
import subprocess  # noqa: S404 - fixed argv, no shell
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple, TypeVar

ROOT = Path(__file__).resolve().parents[1]
PAGE = 100
SUITE_BATCH = 10
# GitHub charges a query for the check runs it asks for, not the ones it gets, so
# a check suite is asked for the smallest of these that holds its red jobs.
RED_TIERS = (5, 25, 100)
# A run a newer push or a person cancelled has dozens of cancelled jobs; one the
# runner cut short has a few. Cancelled jobs are read up to this many a run.
CANCELLED_READ_MAX = 5
# A run that ended cancelled with no red job is asked for its few cancelled jobs
# only, which is light enough to read this many check suites in one call.
CANCELLED_SUITE_BATCH = 50
DEFAULT_WORKERS = 4
DEFAULT_RESERVE_POINTS = 2000
DEFAULT_MAX_POINTS = 1500
PARTIAL_EXIT = 3
# A paced census sleeps this long past the bucket's reset, and this long when
# GitHub's reset time cannot be read.
RESET_MARGIN_SECONDS = 5
UNKNOWN_RESET_WAIT_SECONDS = 60
_RESET_STAMP = "%Y-%m-%dT%H:%M:%SZ"
MAX_WORKERS = 8
EVIDENCE_CHARS = 80
ATTEMPTS = 3
BACKOFF_SECONDS = (2, 10)
GH_TIMEOUT_SECONDS = 300
CAUSES = ("rate_limit", "broken_main", "infrastructure", "flaky", "diff")
LOG_MODES = ("undecided", "never")
CI_WORKFLOW = "CI"
MAIN_RED_LABEL = "main-red"
CSV_COLUMNS = (
    "run_id",
    "job_id",
    "workflow",
    "job",
    "event",
    "head_sha",
    "pr",
    "cause",
    "evidence",
)

RED_CONCLUSIONS = ("FAILURE", "TIMED_OUT", "STARTUP_FAILURE")
_RED_FILTER = "[FAILURE, TIMED_OUT, STARTUP_FAILURE]"
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_DAY = re.compile(r"\d{4}-\d\d-\d\d")
_MINUTE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?Z")

RATE_LIMIT = re.compile(
    r"api rate limit"
    r"|(?:secondary|primary) rate limit"
    r"|rate limit (?:is |was )?(?:exceeded|spent)"
    r"|exceeded [a-z ]{0,20}rate limit",
    re.IGNORECASE,
)
INFRASTRUCTURE = re.compile(
    r"runner has received a shutdown signal"
    r"|lost communication with the server"
    r"|hosted runner encountered an error"
    r"|exceeded the maximum execution time"
    r"|worker '?gw\d+'? crashed"
    r"|node down: "
    r"|maximum crashed workers reached"
    r"|out of memory"
    r"|exit code 137\b"
    r"|no space left on device",
    re.IGNORECASE,
)
# The step names a job's checkout and toolchain setup carry: the runner's own
# `Set up job`, an unnamed `uses:` step (`Run actions/checkout@...`), and the
# `Checkout ...` / `Set up ...` / `Setup ...` names workflows give them.
SETUP_STEP = re.compile(
    r"(?:set up job|(?:run )?actions/(?:checkout|setup-[a-z0-9-]+)|checkout\b|set ?up\b)",
    re.IGNORECASE,
)
# In a log a signature counts only where the job reports an error: the runner's
# `##[error]` lines (its own, and every `::error::` a step wrote), `gh`'s error
# output, and pytest's `FAILED` / `ERROR` summary lines. A step's echoed script,
# a status line and a warning about a wait the job survived all name a rate
# limit or a runner without being why the job ended red.
_ERROR_MARK = "##[error]"
_GH_ERROR = "gh: "
# The Linux shards are `Backend Tests (<python>, <shard>)`, the Windows ones
# `Backend Tests (Windows) (<shard>)`.
BACKEND_SHARD = re.compile(r"Backend Tests (?:\([0-9.]+, \d+\)|\(Windows\) \(\d+\))")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_GENERIC = re.compile(r"Process completed with exit code \d+\.?|The operation was canceled\.?")


class CensusError(RuntimeError):
    """The census could not be taken; the message says which read failed."""


class BudgetStop(CensusError):
    """The next GraphQL call would spend points the census was told to leave."""


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise CensusError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WATCH = _load("main_red_nodeids_for_census", ROOT / ".github" / "scripts" / "main_red_nodeids.py")
SKIP = _load("ci_main_red_skip_for_census", ROOT / "scripts" / "ci_main_red_skip.py")


def evidence_text(text: str) -> str:
    return str(WATCH.clean(text, EVIDENCE_CHARS))


# -- the window ---------------------------------------------------------------


class Bound(NamedTuple):
    text: str
    at: dt.datetime
    whole_day: bool


def parse_bound(text: str) -> Bound:
    """A window bound: a UTC day (``2026-11-20``) or a UTC minute (``2026-11-20T09:00Z``)."""
    whole_day = _DAY.fullmatch(text) is not None
    if not whole_day and _MINUTE.fullmatch(text) is None:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a YYYY-MM-DD date or a YYYY-MM-DDTHH:MMZ time"
        )
    try:
        at = dt.datetime.fromisoformat(text if whole_day else text[:-1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a calendar date or time") from exc
    return Bound(text, at, whole_day)


def window(since: Bound, until: Bound) -> tuple[str, str, float]:
    """``(start, end, days)`` as UTC stamps that sort the way GitHub's do.

    A day given as ``--until`` is included whole; a time is where the window ends.
    """
    end = until.at + dt.timedelta(days=1) if until.whole_day else until.at
    if end <= since.at:
        raise CensusError("--until is not after --since")
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    return since.at.strftime(stamp), end.strftime(stamp), (end - since.at).total_seconds() / 86400


# -- what was read ------------------------------------------------------------


class Run(NamedTuple):
    run_id: int
    workflow: str
    event: str
    head_sha: str
    pr: int | None
    created_at: str
    suite_id: str
    red_jobs: int
    cancelled: bool


class Job(NamedTuple):
    run: Run
    job_id: int
    name: str
    conclusion: str
    started_at: str
    completed_at: str
    annotations: tuple[dict[str, Any], ...]
    failed_step: str


class MainRed(NamedTuple):
    number: int
    opened_at: str
    closed_at: str | None
    nodeids: frozenset[str]

    def open_at(self, stamp: str) -> bool:
        return self.opened_at <= stamp and (self.closed_at is None or stamp < self.closed_at)


class LogFacts(NamedTuple):
    rate_limit: str | None
    infrastructure: str | None
    failures: dict[str, str]


class Census:
    """What one census read: a row per red job, the runs behind them, the totals."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.runs: list[Run] = []
        self.partial: str | None = None
        self.workflows = 0
        self.log_unread = 0
        self.suites_unread = 0
        self.logs_read = 0


# -- reading one job ----------------------------------------------------------


def failure_messages(job: Job) -> list[str]:
    return [
        str(row.get("message") or "")
        for row in job.annotations
        if row.get("annotation_level") == "failure"
    ]


def first_match(pattern: re.Pattern[str], texts: Iterable[str]) -> str | None:
    """The first line of ``texts`` the pattern matches, or ``None``."""
    for text in texts:
        for line in text.splitlines():
            if pattern.search(line):
                return line
    return None


def error_text(line: str) -> str | None:
    """What one log line reports as an error, or ``None`` for any other line."""
    if line.startswith(_ERROR_MARK):
        return line[len(_ERROR_MARK) :]
    if line.startswith(_GH_ERROR) or WATCH.summary_entry(line) is not None:
        return line
    return None


def scan_log(text: str) -> LogFacts:
    """What one job log says: a rate limit, an infrastructure signature, its failing tests."""
    rate_limit = infrastructure = None
    failures: dict[str, str] = {}
    for raw in text.splitlines():
        line = WATCH.log_line(raw)
        entry = WATCH.summary_entry(line)
        if entry is not None:
            failures.setdefault(entry[0], entry[1])
        reported = error_text(line)
        if reported is None:
            continue
        if rate_limit is None and RATE_LIMIT.search(reported):
            rate_limit = reported
        if infrastructure is None and INFRASTRUCTURE.search(reported):
            infrastructure = reported
    return LogFacts(rate_limit, infrastructure, failures)


def annotations_name_nothing(job: Job) -> bool:
    """True when no annotation names a rate limit, a failing test or a runner signature."""
    messages = failure_messages(job)
    return not (
        first_match(RATE_LIMIT, messages)
        or first_match(INFRASTRUCTURE, messages)
        or WATCH.parse_annotations(list(job.annotations))
    )


def is_red(job: Job) -> bool:
    if job.conclusion in RED_CONCLUSIONS:
        return True
    return (
        job.conclusion == "CANCELLED"
        and first_match(INFRASTRUCTURE, failure_messages(job)) is not None
    )


def passed_key(run: Run, name: str) -> tuple[str, str, str]:
    return (run.workflow, name, run.head_sha)


def classify(
    job: Job,
    log: LogFacts | None,
    main_red: list[MainRed],
    passed: dict[tuple[str, str, str], list[tuple[str, int]]],
) -> tuple[str, str]:
    """``(cause, evidence)`` for one red job: the first rule that matches wins."""
    messages = failure_messages(job)
    hit = first_match(RATE_LIMIT, messages) or (log.rate_limit if log else None)
    if hit:
        return "rate_limit", evidence_text(hit)

    failures = WATCH.parse_annotations(list(job.annotations)) or (log.failures if log else {})
    stamp = job.started_at or job.completed_at or job.run.created_at
    for nodeid in failures:
        for issue in main_red:
            if issue.open_at(stamp) and SKIP.without_group(nodeid) in issue.nodeids:
                return "broken_main", evidence_text(f"#{issue.number} {nodeid}")

    hit = first_match(INFRASTRUCTURE, messages) or (log.infrastructure if log else None)
    if hit:
        return "infrastructure", evidence_text(hit)
    if job.failed_step and SETUP_STEP.match(job.failed_step):
        return "infrastructure", evidence_text(f"step: {job.failed_step}")
    if job.conclusion in ("TIMED_OUT", "STARTUP_FAILURE"):
        return "infrastructure", evidence_text(f"conclusion: {job.conclusion.lower()}")

    for completed_at, job_id in passed.get(passed_key(job.run, job.name), []):
        if completed_at > job.completed_at and job_id != job.job_id:
            return "flaky", evidence_text(f"passed on re-run: job {job_id}")

    if failures:
        return "diff", evidence_text(next(iter(failures)))
    for message in messages:
        if message.strip() and _GENERIC.fullmatch(message.strip()) is None:
            return "diff", evidence_text(message)
    return "diff", evidence_text(f"step: {job.failed_step}" if job.failed_step else "")


# -- the API ------------------------------------------------------------------

GhRunner = Callable[[list[str]], tuple[int, str, str]]


def run_gh(argv: list[str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["gh", *argv],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout, done.stderr


class Api:
    """`gh api` with a count of every call made, by kind. Safe to share between threads.

    It also keeps the GraphQL budget: ``reserve`` points are left in the
    bucket and at most ``max_points`` are spent. Once either would be crossed,
    every further GraphQL read raises ``BudgetStop`` and ``stopped`` says why.
    Reads already in flight on other threads finish, so the overshoot is at
    most one call a worker.

    With ``wait_left`` seconds to wait, reaching the reserve sleeps until the
    bucket's reset instead of stopping, and so does spending ``hourly_points``
    since the last such sleep. ``max_points`` still ends the census. One thread
    sleeps; the others queue behind it and then read the budget again.
    """

    def __init__(
        self,
        run: GhRunner = run_gh,
        sleep: Callable[[float], None] = time.sleep,
        reserve: int = DEFAULT_RESERVE_POINTS,
        max_points: int = DEFAULT_MAX_POINTS,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
    ) -> None:
        self._run = run
        self._sleep = sleep
        self.now = now
        self._lock = threading.Lock()
        self._pace = threading.Lock()
        self._paced_from = 0
        self.reserve = reserve
        self.max_points = max_points
        self.hourly_points: int | None = None
        self.wait_left = 0.0
        self.waited_seconds = 0.0
        self.waits = 0
        self.remaining: int | None = None
        self.reset_at = ""
        self.stopped: str | None = None
        self.graphql_calls = 0
        self.graphql_points = 0
        self.rest_calls = 0

    def _pause_reason(self) -> str | None:
        """Why the census may not spend now, or ``None``. The lock is held."""
        spent = self.graphql_points - self._paced_from
        if self.hourly_points is not None and spent >= self.hourly_points:
            return (
                f"{spent} GraphQL points spent since the last wait, "
                f"the --points-per-hour of {self.hourly_points}"
            )
        if self.remaining is not None and self.remaining < self.reserve:
            return (
                f"{self.remaining} GraphQL points left, under the --reserve-points "
                f"of {self.reserve} (the bucket resets at {self.reset_at or 'an unknown time'})"
            )
        return None

    def _reset_delay(self) -> float:
        """Seconds from now until just past the bucket's reset."""
        try:
            reset = dt.datetime.strptime(self.reset_at, _RESET_STAMP)
        except ValueError:
            return UNKNOWN_RESET_WAIT_SECONDS
        left = (reset.replace(tzinfo=dt.timezone.utc) - self.now()).total_seconds()
        return max(left, 0.0) + RESET_MARGIN_SECONDS

    def _guard(self) -> None:
        with self._pace:
            while True:
                with self._lock:
                    if self.stopped is None and self.graphql_points >= self.max_points:
                        self.stopped = (
                            f"{self.graphql_points} GraphQL points spent, "
                            f"the --max-points of {self.max_points}"
                        )
                    reason = None if self.stopped else self._pause_reason()
                    delay = 0.0
                    if reason is not None and self.wait_left <= 0:
                        self.stopped = reason
                    elif reason is not None:
                        delay = self._reset_delay()
                        if delay > self.wait_left:
                            self.stopped = (
                                f"{reason}; the reset is {delay:.0f}s away, past the "
                                f"{self.wait_left:.0f}s left of --max-wait-seconds"
                            )
                    if self.stopped is not None:
                        raise BudgetStop(self.stopped)
                    if reason is None:
                        return
                    self.wait_left -= delay
                    self.waited_seconds += delay
                    self.waits += 1
                _progress(f"{reason}; waiting {delay:.0f}s for the bucket's reset")
                self._sleep(delay)
                # A read that was in flight may have reported the spent bucket
                # during the sleep; the next read reports the new one.
                with self._lock:
                    self.remaining = None
                    self._paced_from = self.graphql_points

    def _read_budget(self, rate: Any) -> None:
        if not isinstance(rate, dict):
            return
        cost, remaining, reset_at = rate.get("cost"), rate.get("remaining"), rate.get("resetAt")
        with self._lock:
            self.graphql_points += cost if isinstance(cost, int) else 0
            if isinstance(remaining, int) and not isinstance(remaining, bool):
                self.remaining = remaining
            if isinstance(reset_at, str):
                self.reset_at = evidence_text(reset_at)

    def check_budget(self) -> None:
        """Read the bucket (one point) and stop here if it is already under the reserve."""
        self.graphql(BUDGET_QUERY, {})
        self._guard()

    def _count(self, graphql: int = 0, points: int = 0, rest: int = 0) -> None:
        with self._lock:
            self.graphql_calls += graphql
            self.graphql_points += points
            self.rest_calls += rest

    def _retry(self, attempt: int) -> bool:
        if attempt + 1 >= ATTEMPTS:
            return False
        self._sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
        return True

    def graphql(self, query: str, variables: dict[str, str | list[str]]) -> dict[str, Any]:
        """The ``data`` of one query. Every variable is a string or a list of strings.

        Values go over `-f` (raw string): `-F` would read a value that starts
        with `@` as a file name and a cursor of digits as a number.
        """
        argv = ["api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            if isinstance(value, list):
                for item in value:
                    argv += ["-f", f"{name}[]={item}"]
            else:
                argv += ["-f", f"{name}={value}"]
        reason = "unknown failure"
        for attempt in range(ATTEMPTS):
            self._guard()
            self._count(graphql=1)
            code, out, err = self._run(argv)
            try:
                body = json.loads(out) if out.strip() else None
            except ValueError:
                body = None
            data = body.get("data") if isinstance(body, dict) else None
            errors = body.get("errors") if isinstance(body, dict) else None
            if isinstance(data, dict):
                self._read_budget(data.get("rateLimit"))
            if code == 0 and isinstance(data, dict) and not errors:
                return data
            reason = evidence_text(json.dumps(errors) if errors else err or f"gh exited {code}")
            if not self._retry(attempt):
                break
        raise CensusError(f"GraphQL read failed: {reason}")

    def rest_json(self, path: str) -> Any:
        reason = "unknown failure"
        for attempt in range(ATTEMPTS):
            self._count(rest=1)
            code, out, err = self._run(["api", path])
            if code == 0:
                try:
                    return json.loads(out)
                except ValueError:
                    reason = "the response is not JSON"
            else:
                reason = evidence_text(err or f"gh exited {code}")
            if not self._retry(attempt):
                break
        raise CensusError(f"REST read of {path} failed: {reason}")

    def rest_text(self, path: str) -> str | None:
        """The body of one REST read, or ``None`` when it cannot be read. One try."""
        self._count(rest=1)
        code, out, _err = self._run(["api", path])
        return out if code == 0 else None


BUDGET_QUERY = "query { rateLimit { cost remaining resetAt } }"

_RED_JOB_FIELDS = """
        pageInfo { hasNextPage endCursor }
        nodes {
          databaseId name conclusion startedAt completedAt
          annotations(first: 50) { nodes { annotationLevel title message } }
          steps(first: 100) { nodes { number name conclusion } }
        }"""
# A cancelled job is red only by a runner signature in its annotations, so it
# is read without its steps.
_CANCELLED_JOB_FIELDS = """
        totalCount
        nodes {
          databaseId name conclusion startedAt completedAt
          annotations(first: 10) { nodes { annotationLevel title message } }
        }"""
_PASSED_JOB_FIELDS = """
        pageInfo { hasNextPage endCursor }
        nodes { databaseId name completedAt }"""

RUNS_QUERY = """
query($workflow: ID!, $after: String) {
  rateLimit { cost remaining resetAt }
  node(id: $workflow) {
    ... on Workflow {
      runs(first: 100, after: $after, orderBy: {field: CREATED_AT, direction: DESC}) {
        pageInfo { hasNextPage endCursor }
        nodes {
          databaseId createdAt event
          checkSuite {
            id
            conclusion
            commit { oid }
            red: checkRuns(first: 1, filterBy: {checkType: ALL, conclusions: %s}) { totalCount }
          }
        }
      }
    }
  }
}""" % (_RED_FILTER,)


def suites_query(first: int) -> str:
    """The query for a batch of check suites that each hold at most ``first`` red jobs."""
    return """
query($ids: [ID!]!) {
  rateLimit { cost remaining resetAt }
  nodes(ids: $ids) {
    ... on CheckSuite {
      id
      matchingPullRequests(first: 1) { nodes { number } }
      red: checkRuns(first: %d, filterBy: {checkType: ALL, conclusions: %s}) {%s
      }
      cancelled: checkRuns(first: %d, filterBy: {checkType: ALL, conclusions: [CANCELLED]}) {%s
      }
      passed: checkRuns(first: 100, filterBy: {checkType: ALL, conclusions: [SUCCESS]}) {%s
      }
    }
  }
}""" % (
        first,
        _RED_FILTER,
        _RED_JOB_FIELDS,
        CANCELLED_READ_MAX,
        _CANCELLED_JOB_FIELDS,
        _PASSED_JOB_FIELDS,
    )


CANCELLED_SUITES_QUERY = """
query($ids: [ID!]!) {
  rateLimit { cost remaining resetAt }
  nodes(ids: $ids) {
    ... on CheckSuite {
      id
      matchingPullRequests(first: 1) { nodes { number } }
      cancelled: checkRuns(first: %d, filterBy: {checkType: ALL, conclusions: [CANCELLED]}) {%s
      }
    }
  }
}""" % (
    CANCELLED_READ_MAX,
    _CANCELLED_JOB_FIELDS,
)

_SUITE_PAGE = """
query($suite: ID!, $after: String) {
  rateLimit { cost remaining resetAt }
  node(id: $suite) {
    ... on CheckSuite {
      page: checkRuns(first: 100, after: $after, filterBy: {checkType: ALL, conclusions: %s}) {%s
      }
    }
  }
}"""
SUITE_PAGE_QUERIES = {
    "red": _SUITE_PAGE % (_RED_FILTER, _RED_JOB_FIELDS),
    "passed": _SUITE_PAGE % ("[SUCCESS]", _PASSED_JOB_FIELDS),
}

ISSUES_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issues(first: 100, after: $after, labels: ["%s"], states: [OPEN, CLOSED],
           orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number title body createdAt closedAt author { __typename login } }
    }
  }
}""" % (MAIN_RED_LABEL,)


def _connection(value: Any, what: str) -> tuple[list[dict[str, Any]], str | None]:
    """``(nodes, cursor of the next page or None)`` of one GraphQL connection."""
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
        raise CensusError(f"GraphQL returned no {what}")
    info = value.get("pageInfo") or {}
    cursor = info.get("endCursor") if info.get("hasNextPage") else None
    nodes = [node for node in value["nodes"] if isinstance(node, dict)]
    return nodes, cursor if isinstance(cursor, str) and cursor else None


_Item = TypeVar("_Item")
_Result = TypeVar("_Result")


def _each(workers: int, read: Callable[[_Item], _Result], items: list[_Item]) -> list[_Result]:
    """``read`` of every item, in the items' order, at most ``workers`` at a time."""
    if workers <= 1 or len(items) <= 1:
        return [read(item) for item in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(read, items))


def _after(cursor: str | None) -> dict[str, str | list[str]]:
    return {"after": cursor} if cursor else {}


def list_workflows(api: Api, repo: str) -> list[tuple[str, str]]:
    """``(node id, name)`` of every workflow. REST: GraphQL has no workflow list."""
    found: list[tuple[str, str]] = []
    page = 1
    while True:
        body = api.rest_json(f"repos/{repo}/actions/workflows?per_page={PAGE}&page={page}")
        rows = body.get("workflows") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise CensusError("the workflows API did not return a list")
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("node_id"), str):
                found.append((row["node_id"], str(row.get("name") or "")))
        if len(rows) < PAGE:
            return found
        page += 1


def read_runs(api: Api, workflow_id: str, workflow: str, start: str, end: str) -> list[Run]:
    """One workflow's runs created in ``[start, end)``, newest first, a page of 100 at a time."""
    runs: list[Run] = []
    cursor: str | None = None
    while True:
        try:
            data = api.graphql(RUNS_QUERY, {"workflow": workflow_id, **_after(cursor)})
        except BudgetStop:
            return runs
        nodes, cursor = _connection((data.get("node") or {}).get("runs"), "workflow runs")
        older = False
        for node in nodes:
            created = str(node.get("createdAt") or "")
            if created < start:
                older = True
            suite = node.get("checkSuite")
            if not (start <= created < end) or not isinstance(suite, dict):
                continue
            runs.append(
                Run(
                    run_id=int(node.get("databaseId") or 0),
                    workflow=workflow,
                    event=str(node.get("event") or ""),
                    head_sha=str((suite.get("commit") or {}).get("oid") or ""),
                    pr=None,
                    created_at=created,
                    suite_id=str(suite.get("id") or ""),
                    red_jobs=int((suite.get("red") or {}).get("totalCount") or 0),
                    cancelled=suite.get("conclusion") == "CANCELLED",
                )
            )
        if older or cursor is None:
            return runs


def _annotation_rows(node: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """A check run's annotations in the REST shape the watcher's parser reads."""
    rows = (node.get("annotations") or {}).get("nodes") or []
    return tuple(
        {
            "annotation_level": str(row.get("annotationLevel") or "").lower(),
            "title": row.get("title"),
            "message": row.get("message"),
        }
        for row in rows
        if isinstance(row, dict)
    )


def _failed_step(node: dict[str, Any]) -> str:
    for step in (node.get("steps") or {}).get("nodes") or []:
        if isinstance(step, dict) and step.get("conclusion") in ("FAILURE", "TIMED_OUT"):
            return str(step.get("name") or "")
    return ""


def _job(run: Run, node: dict[str, Any]) -> Job:
    return Job(
        run=run,
        job_id=int(node.get("databaseId") or 0),
        name=str(node.get("name") or ""),
        conclusion=str(node.get("conclusion") or ""),
        started_at=str(node.get("startedAt") or ""),
        completed_at=str(node.get("completedAt") or ""),
        annotations=_annotation_rows(node),
        failed_step=_failed_step(node),
    )


def _all_pages(api: Api, suite_id: str, kind: str, first: Any) -> list[dict[str, Any]]:
    nodes, cursor = _connection(first, f"{kind} check runs")
    while cursor is not None:
        data = api.graphql(SUITE_PAGE_QUERIES[kind], {"suite": suite_id, "after": cursor})
        more, cursor = _connection((data.get("node") or {}).get("page"), f"{kind} check runs")
        nodes += more
    return nodes


Passed = dict[tuple[str, str, str], list[tuple[str, int]]]


def _merge(passed: Passed, more: Passed) -> None:
    for key, stamps in more.items():
        passed.setdefault(key, []).extend(stamps)


def _read_suites(
    api: Api, by_suite: dict[str, Run], ids: list[str], first: int
) -> tuple[list[Job], Passed, list[str]]:
    """``(jobs, passes, suites left unread)`` from one call for ``ids``.

    ``first`` is the red jobs to ask each suite for; 0 asks for its cancelled
    jobs only. A batch GitHub cannot answer in time is read as two halves.

    A suite is added whole or not at all: its red jobs, its cancelled jobs and
    its passes are gathered first and added together. The classifier reads
    them together (a red job is `flaky` only by a pass), so a suite the budget
    stopped part way through is left out and named unread rather than
    reported with half its facts. A suite whose first pages already hold
    everything needs no further call and is still added after a stop.
    """
    jobs: list[Job] = []
    passed: Passed = {}
    unread: list[str] = []
    try:
        data = api.graphql(suites_query(first) if first else CANCELLED_SUITES_QUERY, {"ids": ids})
    except BudgetStop:
        return jobs, passed, list(ids)
    except CensusError:
        if len(ids) < 2:
            raise
        for half in (ids[: len(ids) // 2], ids[len(ids) // 2 :]):
            found, passes, missed = _read_suites(api, by_suite, half, first)
            jobs += found
            _merge(passed, passes)
            unread += missed
        return jobs, passed, unread
    answered = set()
    for suite in data.get("nodes") or []:
        if not isinstance(suite, dict) or suite.get("id") not in by_suite:
            continue
        run = by_suite[suite["id"]]
        answered.add(run.suite_id)
        pulls = (suite.get("matchingPullRequests") or {}).get("nodes") or []
        number = pulls[0].get("number") if pulls and isinstance(pulls[0], dict) else None
        if run.event == "pull_request" and isinstance(number, int):
            run = run._replace(pr=number)
        try:
            red = _all_pages(api, run.suite_id, "red", suite.get("red")) if first else []
            green = _all_pages(api, run.suite_id, "passed", suite.get("passed")) if red else []
        except BudgetStop:
            unread.append(run.suite_id)
            continue
        jobs += [_job(run, node) for node in red]
        cancelled = suite.get("cancelled") or {}
        if int(cancelled.get("totalCount") or 0) <= CANCELLED_READ_MAX:
            jobs += [_job(run, node) for node in cancelled.get("nodes") or []]
        for node in green:
            stamp = (str(node.get("completedAt") or ""), int(node.get("databaseId") or 0))
            passed.setdefault(passed_key(run, str(node.get("name") or "")), []).append(stamp)
    return jobs, passed, unread + [suite for suite in ids if suite not in answered]


def read_jobs(api: Api, runs: list[Run], workers: int = 1) -> tuple[list[Job], Passed, int]:
    """``(jobs, passes, check suites left unread)`` for the runs that may have a red job.

    A run is read when a job of it, in any attempt, ended red, or when the run
    ended cancelled. The passes are keyed by workflow, job name and head SHA. A
    re-run keeps its run's check suite, so a job that failed and then passed
    shows both there. The pull request of a `pull_request` run is read here,
    for these runs only: asked of every run in the window it triples the time a
    page of runs takes.

    A pass can also sit in another run of the same workflow on the same head
    SHA. So when a suite with red jobs is left unread, every suite of that
    workflow and SHA is left out with it: none of their jobs is reported with a
    cause a complete read could have changed.
    """
    by_suite = {
        run.suite_id: run for run in runs if (run.red_jobs or run.cancelled) and run.suite_id
    }
    batches: list[tuple[int, list[str]]] = []
    below = -1
    for tier, size in ((0, CANCELLED_SUITE_BATCH), *((tier, SUITE_BATCH) for tier in RED_TIERS)):
        last = tier == RED_TIERS[-1]
        ids = [
            suite
            for suite, run in by_suite.items()
            if below < run.red_jobs and (last or run.red_jobs <= tier)
        ]
        batches += [(tier, ids[at : at + size]) for at in range(0, len(ids), size)]
        below = tier
    jobs: list[Job] = []
    passed: Passed = {}
    unread: set[str] = set()
    for found, passes, missed in _each(
        workers, lambda batch: _read_suites(api, by_suite, batch[1], batch[0]), batches
    ):
        jobs += found
        _merge(passed, passes)
        unread.update(missed)
    open_pushes = {
        (by_suite[suite].workflow, by_suite[suite].head_sha)
        for suite in unread
        if by_suite[suite].red_jobs
    }
    kept: list[Job] = []
    for job in jobs:
        if (job.run.workflow, job.run.head_sha) in open_pushes:
            unread.add(job.run.suite_id)
        else:
            kept.append(job)
    return kept, passed, len(unread)


def read_main_red(api: Api, repo: str, start: str, end: str) -> list[MainRed]:
    """The watcher's main-red issues that were open at some point in ``[start, end)``."""
    owner, _, name = repo.partition("/")
    found: list[MainRed] = []
    cursor: str | None = None
    while True:
        data = api.graphql(ISSUES_QUERY, {"owner": owner, "name": name, **_after(cursor)})
        nodes, cursor = _connection((data.get("repository") or {}).get("issues"), "issues")
        for node in nodes:
            author = node.get("author")
            author = author if isinstance(author, dict) else {}
            row = {"login": author.get("login"), "is_bot": author.get("__typename") == "Bot"}
            state = WATCH.parse_state(str(node.get("body") or ""))
            opened, closed = str(node.get("createdAt") or ""), node.get("closedAt")
            if (
                state is None
                or not WATCH.is_watcher_author(row)
                or not str(node.get("title") or "").startswith(WATCH.TITLE_PREFIXES)
                or not isinstance(node.get("number"), int)
                or opened >= end
                or (isinstance(closed, str) and closed < start)
            ):
                continue
            ids = frozenset(SKIP.without_group(nodeid) for nodeid in state["failures"])
            found.append(
                MainRed(node["number"], opened, closed if isinstance(closed, str) else None, ids)
            )
        if cursor is None:
            return found


def read_log(api: Api, repo: str, job: Job) -> LogFacts | None:
    """REST: GraphQL has no job log. ``None`` when the token cannot read it."""
    text = api.rest_text(f"repos/{repo}/actions/jobs/{job.job_id}/logs")
    return scan_log(text) if text else None


def _progress(text: str) -> None:
    print(f"ci_red_cause_census: {text}", file=sys.stderr, flush=True)


def take_census(api: Api, repo: str, start: str, end: str, logs: str, workers: int = 1) -> Census:
    """The census of ``[start, end)``, or as much of it as the GraphQL budget allowed.

    The budget is read before anything is spent, and the main-red issues before
    the runs, so a census the budget cuts short never files a broken-main job
    under ``diff`` for want of the issue.
    """
    census = Census()
    try:
        api.check_budget()
        main_red = read_main_red(api, repo, start, end)
    except BudgetStop as stop:
        census.partial = str(stop)
        return census
    workflows = list_workflows(api, repo)
    census.workflows = len(workflows)
    for runs in _each(workers, lambda row: read_runs(api, row[0], row[1], start, end), workflows):
        census.runs += runs
    _progress(f"{len(census.runs)} run(s) of {len(workflows)} workflow(s) read")
    jobs, passed, census.suites_unread = read_jobs(api, census.runs, workers)
    _progress(f"{len(jobs)} red or cancelled job(s) and {len(main_red)} main-red issue(s) read")
    census.partial = api.stopped
    for job in jobs:
        if not is_red(job):
            continue
        unnamed = annotations_name_nothing(job)
        log = None
        if logs == "undecided" and unnamed:
            log = read_log(api, repo, job)
            census.logs_read += log is not None
        if unnamed and log is None:
            census.log_unread += 1
        cause, evidence = classify(job, log, main_red, passed)
        census.rows.append(
            {
                "run_id": job.run.run_id,
                "job_id": job.job_id,
                "workflow": job.run.workflow,
                "job": job.name,
                "event": job.run.event,
                "head_sha": job.run.head_sha,
                "pr": job.run.pr if job.run.pr is not None else "",
                "cause": cause,
                "evidence": evidence,
            }
        )
    census.rows.sort(key=lambda row: (row["run_id"], row["job_id"]))
    return census


# -- the summary --------------------------------------------------------------


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def summarize(census: Census, days: float) -> dict[str, Any]:
    """The three goal numbers, each with its n, and the counts behind them."""
    rows = census.rows
    by_cause = {cause: sum(row["cause"] == cause for row in rows) for cause in CAUSES}

    pushes: dict[str, set[str]] = {}
    for row in rows:
        if row["workflow"] == CI_WORKFLOW and row["event"] == "pull_request":
            pushes.setdefault(row["head_sha"], set()).add(row["cause"])
    broken = sum("broken_main" in causes for causes in pushes.values())
    broken_only = sum(causes == {"broken_main"} for causes in pushes.values())

    shards = [
        row
        for row in rows
        if row["workflow"] == CI_WORKFLOW and BACKEND_SHARD.fullmatch(row["job"])
    ]
    flaky = sum(row["cause"] == "flaky" for row in shards)
    infra = sum(row["cause"] == "infrastructure" for row in shards)
    ci_pushes = {
        (run.event, run.head_sha)
        for run in census.runs
        if run.workflow == CI_WORKFLOW and run.event in ("pull_request", "push")
    }
    return {
        "days": round(days, 4),
        "partial": census.partial,
        "runs_read_from": min((run.created_at for run in census.runs), default=None),
        "runs_read_to": max((run.created_at for run in census.runs), default=None),
        "suites_unread": census.suites_unread,
        "workflows": census.workflows,
        "runs": len(census.runs),
        "red_jobs": len(rows),
        "by_cause": by_cause,
        "log_unread": census.log_unread,
        "logs_read": census.logs_read,
        "broken_main_share_of_red_pr_pushes": {
            "share": _share(broken, len(pushes)),
            "broken_main": broken,
            "broken_main_only": broken_only,
            "n": len(pushes),
        },
        "rate_limit_share_of_red_jobs": {
            "share": _share(by_cause["rate_limit"], len(rows)),
            "rate_limit": by_cause["rate_limit"],
            "n": len(rows),
        },
        "backend_shard_flaky_or_infrastructure_reds": {
            "per_day": round((flaky + infra) / days, 1),
            "flaky": flaky,
            "infrastructure": infra,
            "n": len(shards),
            "pushes_per_day": round(len(ci_pushes) / days, 1),
            "pushes": len(ci_pushes),
        },
    }


def _percent(share: float | None) -> str:
    return "n/a" if share is None else f"{share * 100:.1f}%"


def render_markdown(summary: dict[str, Any], repo: str, since: str, until: str) -> str:
    main = summary["broken_main_share_of_red_pr_pushes"]
    rate = summary["rate_limit_share_of_red_jobs"]
    shard = summary["backend_shard_flaky_or_infrastructure_reds"]
    lines = [
        f"### Red-cause census: {repo}, {since} to {until} UTC ({summary['days']:g} day(s))",
        "",
    ]
    if summary["partial"]:
        lines += [
            f"**Partial: stopped early ({summary['partial']}). Runs read: {summary['runs']}, "
            f"created {summary['runs_read_from'] or 'n/a'} to {summary['runs_read_to'] or 'n/a'}; "
            f"{summary['suites_unread']} of their check suites with a red or cancelled job "
            "were left unread and none of their jobs is counted. The counts below cover only "
            "what was read; they are not a census of the window.**",
            "",
        ]
    lines += [
        "| Goal number | Value | n |",
        "|---|---|---|",
        f"| Red PR pushes with a job red from a broken main | {_percent(main['share'])} "
        f"({main['broken_main']}) | {main['n']} red PR pushes |",
        f"| Red jobs caused by the API rate limit, all workflows | {_percent(rate['share'])} "
        f"({rate['rate_limit']}) | {rate['n']} red jobs |",
        f"| Backend shard reds from flaky tests or infrastructure, per day | {shard['per_day']} "
        f"({shard['flaky']} flaky + {shard['infrastructure']} infrastructure) | "
        f"{shard['n']} red backend shard jobs, {shard['pushes_per_day']} pushes a day |",
        "",
        "| Cause | Red jobs |",
        "|---|---|",
    ]
    lines += [f"| {cause} | {summary['by_cause'][cause]} |" for cause in CAUSES]
    lines += [
        "",
        f"Read {summary['runs']} run(s) of {summary['workflows']} workflow(s); "
        f"{summary['red_jobs']} red job(s). `log_unread`: {summary['log_unread']} job(s) "
        f"whose annotations name nothing and whose log was not read "
        f"({summary['logs_read']} log(s) read).",
        "",
    ]
    return "\n".join(lines)


def _cell(value: Any) -> str:
    """One CSV cell: no control character, and nothing a spreadsheet reads as a formula.

    A job name is whatever a pull request's workflow file calls the job.
    """
    text = _CONTROL.sub(" ", str(value))
    return "'" + text if text.lstrip()[:1] in ("=", "+", "-", "@") else text


def write_outputs(out_dir: Path, census: Census, summary: dict[str, Any], markdown: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "red-jobs.csv", "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for row in census.rows:
            writer.writerow([_cell(row[column]) for column in CSV_COLUMNS])
    (out_dir / "summary.md").write_text(markdown, encoding="utf-8", newline="\n")
    text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    (out_dir / "summary.json").write_text(text, encoding="utf-8", newline="\n")


def _not_negative(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = -1
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of 0 or more")
    return value


def _positive(text: str) -> int:
    value = _not_negative(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of 1 or more")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        epilog=(
            "Cost: one day of kirodotdev/KiroCrew (2026-11-19) was 1,828 GraphQL calls and "
            "2,786 points. The bucket is shared (5,000 points an hour for every session of a "
            "person's account), so the defaults stop early: read a few hours by hand, and run "
            "a day or more from a workflow, under its own token and after "
            ".github/actions/gh-rate-limit, paced with --points-per-hour and "
            "--max-wait-seconds. A census the budget stopped exits "
            f"{PARTIAL_EXIT} and marks its files partial."
        ),
    )
    parser.add_argument(
        "--since",
        type=parse_bound,
        help="UTC start: 2026-11-20 or 2026-11-20T09:00Z",
    )
    parser.add_argument(
        "--until",
        type=parse_bound,
        help="UTC end: a day (included whole) or a time such as 2026-11-20T12:00Z",
    )
    parser.add_argument(
        "--last-days",
        type=_positive,
        default=None,
        help="in place of --since and --until: this many whole UTC days, ending yesterday",
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument(
        "--logs",
        choices=LOG_MODES,
        default="never",
        help="undecided: read the log of a job whose annotations name nothing "
        "(one REST call a job; needs a token that can read logs)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        choices=range(1, MAX_WORKERS + 1),
        default=DEFAULT_WORKERS,
        help="API reads made at a time",
    )
    parser.add_argument(
        "--reserve-points",
        type=int,
        default=DEFAULT_RESERVE_POINTS,
        help="stop before a GraphQL call that would start with fewer points left in the "
        f"bucket (default {DEFAULT_RESERVE_POINTS})",
    )
    parser.add_argument(
        "--max-points",
        type=int,
        default=DEFAULT_MAX_POINTS,
        help=f"stop after spending this many GraphQL points (default {DEFAULT_MAX_POINTS}, "
        "about half a day of runs; a full day is about 2,800)",
    )
    parser.add_argument(
        "--points-per-hour",
        type=_positive,
        default=None,
        help="with --max-wait-seconds: after spending this many GraphQL points, wait for "
        "the bucket's reset before spending more (default: no hourly limit)",
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=_not_negative,
        default=0,
        help="wait for the bucket's reset, this many seconds in total, instead of stopping "
        "at the reserve or the hourly limit (default 0: stop)",
    )
    return parser


def main(
    argv: list[str] | None = None,
    api: Api | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    api = api or Api()
    if args.last_days is not None and args.since is None and args.until is None:
        today = api.now().date()
        args.since = parse_bound((today - dt.timedelta(days=args.last_days)).isoformat())
        args.until = parse_bound((today - dt.timedelta(days=1)).isoformat())
    elif args.last_days is not None or args.since is None or args.until is None:
        parser.error("give --since and --until, or --last-days alone")
    api.reserve, api.max_points = args.reserve_points, args.max_points
    api.hourly_points, api.wait_left = args.points_per_hour, float(args.max_wait_seconds)
    began = clock()
    try:
        if _REPO.fullmatch(args.repo) is None:
            raise CensusError("--repo is not owner/name")
        start, end, days = window(args.since, args.until)
        census = take_census(api, args.repo, start, end, args.logs, args.workers)
    except CensusError as exc:
        print(f"ci_red_cause_census: {exc}", file=sys.stderr)
        status = 1
    else:
        summary = summarize(census, days)
        markdown = render_markdown(summary, args.repo, args.since.text, args.until.text)
        write_outputs(args.out_dir, census, summary, markdown)
        print(markdown)
        status = PARTIAL_EXIT if census.partial else 0
    print(f"GraphQL calls: {api.graphql_calls} ({api.graphql_points} points)")
    if api.remaining is not None:
        print(f"GraphQL points left: {api.remaining} (the bucket resets at {api.reset_at})")
    if api.waits:
        print(f"Waited: {api.waited_seconds:.0f}s in {api.waits} wait(s) for the bucket's reset")
    print(f"REST calls: {api.rest_calls}")
    print(f"Run time: {clock() - began:.1f}s")
    return status


if __name__ == "__main__":
    sys.exit(main())

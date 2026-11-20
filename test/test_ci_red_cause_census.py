"""The red-cause census gives every red CI job exactly one cause, in a fixed order.

`scripts/ci_red_cause_census.py` reads runs, check runs and the main-red issues
through `gh api` and classifies each red job as rate limit, broken main,
infrastructure, flaky or diff. Every case below feeds it canned API payloads
through a stand-in for `gh`, so nothing here opens a socket or spawns a process.
The main-red issue body is built by the watcher's own renderer
(`.github/scripts/main_red_nodeids.py`), so a change to its record fails here.
"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = "kirodotdev/KiroCrew"
SHA = "a" * 40
DAY = "2026-11-20"
TEST_ID = "test/test_alpha.py::test_one"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CENSUS = _load("ci_red_cause_census_test_helper", ROOT / "scripts" / "ci_red_cause_census.py")
WATCH = CENSUS.WATCH

# -- canned payloads, in the shapes the GraphQL and REST APIs return ----------


def note(message: str, title: str | None = None, level: str = "FAILURE") -> dict[str, Any]:
    return {"annotationLevel": level, "title": title, "message": message}


def failing(nodeid: str = TEST_ID, reason: str = "AssertionError: boom") -> dict[str, Any]:
    return note(f"{nodeid} - {reason}", title=nodeid)


def check_run(
    job_id: int,
    name: str = "Backend Tests (3.12, 1)",
    conclusion: str = "FAILURE",
    notes: list[dict[str, Any]] | None = None,
    failed_step: str | None = "Run tests (shard 1/8)",
    started: str = f"{DAY}T10:00:00Z",
    completed: str = f"{DAY}T10:20:00Z",
) -> dict[str, Any]:
    steps = [{"number": 1, "name": "Set up job", "conclusion": "SUCCESS"}]
    if failed_step is not None:
        steps.append({"number": 2, "name": failed_step, "conclusion": "FAILURE"})
    return {
        "databaseId": job_id,
        "name": name,
        "conclusion": conclusion,
        "startedAt": started,
        "completedAt": completed,
        "annotations": {"nodes": notes or []},
        "steps": {"nodes": steps},
    }


def run_of(
    run_id: int = 1,
    workflow: str = "CI",
    event: str = "pull_request",
    sha: str = SHA,
    pr: int | None = 7,
) -> Any:
    return CENSUS.Run(
        run_id=run_id,
        workflow=workflow,
        event=event,
        head_sha=sha,
        pr=pr,
        created_at=f"{DAY}T09:59:00Z",
        suite_id=f"CS_{run_id}",
        red_jobs=1,
        cancelled=False,
    )


def job_of(node: dict[str, Any], run: Any = None) -> Any:
    return CENSUS._job(run or run_of(), node)


def main_red(
    nodeids: tuple[str, ...] = (TEST_ID,),
    opened: str = f"{DAY}T08:00:00Z",
    closed: str | None = None,
    number: int = 900,
) -> Any:
    return CENSUS.MainRed(number, opened, closed, frozenset(nodeids))


def later_pass(name: str = "Backend Tests (3.12, 1)", job_id: int = 55) -> dict[Any, Any]:
    return {("CI", name, SHA): [(f"{DAY}T11:00:00Z", job_id)]}


RATE_NOTE = note("gh: API rate limit exceeded for installation ID 1 (HTTP 403)")
SHUTDOWN_NOTE = note("The runner has received a shutdown signal.")
EXIT_NOTE = note("Process completed with exit code 1.")


# -- one case per cause -------------------------------------------------------


@pytest.mark.parametrize(
    ("node", "issues", "passed", "cause", "evidence"),
    [
        (check_run(1, notes=[RATE_NOTE]), [], {}, "rate_limit", "API rate limit exceeded"),
        (check_run(1, notes=[failing()]), [main_red()], {}, "broken_main", f"#900 {TEST_ID}"),
        (check_run(1, notes=[SHUTDOWN_NOTE]), [], {}, "infrastructure", "shutdown signal"),
        (check_run(1, notes=[failing()]), [], later_pass(), "flaky", "job 55"),
        (check_run(1, notes=[failing()]), [], {}, "diff", TEST_ID),
    ],
    ids=list(CENSUS.CAUSES),
)
def test_each_cause_is_decided_from_canned_payloads(
    node: dict[str, Any], issues: list[Any], passed: dict[Any, Any], cause: str, evidence: str
) -> None:
    got, why = CENSUS.classify(job_of(node), None, issues, passed)
    assert got == cause
    assert evidence in why


# -- the order: where two adjacent rules match, the earlier one wins ----------


def test_rate_limit_wins_over_broken_main() -> None:
    node = check_run(1, notes=[failing(reason="RuntimeError: API rate limit exceeded")])
    assert CENSUS.classify(job_of(node), None, [main_red()], {})[0] == "rate_limit"


def test_broken_main_wins_over_infrastructure() -> None:
    crashed = failing(reason="worker 'gw3' crashed while running 'test_one'")
    job = job_of(check_run(1, notes=[crashed, SHUTDOWN_NOTE]))
    assert CENSUS.classify(job, None, [main_red()], {})[0] == "broken_main"
    assert CENSUS.classify(job, None, [], {})[0] == "infrastructure"


def test_infrastructure_wins_over_flaky() -> None:
    job = job_of(check_run(1, notes=[SHUTDOWN_NOTE]))
    assert CENSUS.classify(job, None, [], later_pass())[0] == "infrastructure"


def test_flaky_wins_over_diff() -> None:
    job = job_of(check_run(1, notes=[failing(), EXIT_NOTE]))
    assert CENSUS.classify(job, None, [], later_pass())[0] == "flaky"
    assert CENSUS.classify(job, None, [], {})[0] == "diff"


# -- each rule's edge ---------------------------------------------------------


@pytest.mark.parametrize(
    ("opened", "closed", "cause"),
    [
        (f"{DAY}T08:00:00Z", None, "broken_main"),
        (f"{DAY}T08:00:00Z", f"{DAY}T12:00:00Z", "broken_main"),
        (f"{DAY}T08:00:00Z", f"{DAY}T09:00:00Z", "diff"),
        (f"{DAY}T10:30:00Z", None, "diff"),
    ],
)
def test_broken_main_needs_the_issue_open_when_the_job_started(
    opened: str, closed: str | None, cause: str
) -> None:
    job = job_of(check_run(1, notes=[failing()]))
    assert CENSUS.classify(job, None, [main_red(opened=opened, closed=closed)], {})[0] == cause


def test_broken_main_matches_a_node_id_with_an_xdist_group_suffix() -> None:
    job = job_of(check_run(1, notes=[failing(nodeid=TEST_ID + "@serial")]))
    assert CENSUS.classify(job, None, [main_red()], {})[0] == "broken_main"


@pytest.mark.parametrize(
    "message",
    [
        "The self-hosted runner lost communication with the server.",
        "The job running on runner r-1 has exceeded the maximum execution time of 60 minutes.",
        "[gw2] node down: Not properly terminated",
        "xdist: maximum crashed workers reached: 0",
        "Process completed with exit code 137.",
        "write error: No space left on device",
    ],
)
def test_runner_and_worker_signatures_are_infrastructure(message: str) -> None:
    job = job_of(check_run(1, notes=[note(message)]))
    assert CENSUS.classify(job, None, [], {})[0] == "infrastructure"


@pytest.mark.parametrize(
    ("step", "cause"),
    [
        ("Set up job", "infrastructure"),
        ("Run actions/checkout@v4", "infrastructure"),
        ("Checkout (full history)", "infrastructure"),
        ("Set up Python 3.12", "infrastructure"),
        ("Run tests (shard 1/8)", "diff"),
        ("Checkouts are counted", "diff"),
    ],
)
def test_a_failed_checkout_or_setup_step_is_infrastructure(step: str, cause: str) -> None:
    job = job_of(check_run(1, notes=[EXIT_NOTE], failed_step=step))
    got, why = CENSUS.classify(job, None, [], {})
    assert got == cause
    assert why == f"step: {step}"


def test_a_timed_out_job_is_infrastructure() -> None:
    job = job_of(check_run(1, conclusion="TIMED_OUT", failed_step=None))
    assert CENSUS.classify(job, None, [], {}) == ("infrastructure", "conclusion: timed_out")


def test_a_pass_before_the_failure_or_on_another_commit_is_not_a_rerun() -> None:
    job = job_of(check_run(1, notes=[failing()]))
    earlier = {("CI", job.name, SHA): [(f"{DAY}T09:00:00Z", 55)]}
    other = {("CI", job.name, "b" * 40): [(f"{DAY}T11:00:00Z", 55)]}
    assert CENSUS.classify(job, None, [], earlier)[0] == "diff"
    assert CENSUS.classify(job, None, [], other)[0] == "diff"


def test_a_cancelled_job_is_red_only_when_the_runner_ended_it() -> None:
    superseded = check_run(
        1,
        conclusion="CANCELLED",
        notes=[note("Canceling since a higher priority waiting request for CI-x exists")],
    )
    by_person = check_run(
        2, conclusion="CANCELLED", notes=[note("The run was canceled by @someone.")]
    )
    by_runner = check_run(3, conclusion="CANCELLED", notes=[SHUTDOWN_NOTE])
    assert [CENSUS.is_red(job_of(node)) for node in (superseded, by_person, by_runner)] == [
        False,
        False,
        True,
    ]


def test_a_warning_annotation_decides_nothing() -> None:
    waited = note("The installation API rate limit is spent; waiting 30s.", level="WARNING")
    job = job_of(check_run(1, notes=[waited, EXIT_NOTE]))
    assert CENSUS.classify(job, None, [], {})[0] == "diff"


# -- the log ------------------------------------------------------------------

LOG = "\n".join(
    [
        "2026-11-20T10:00:01.0000000Z ##[group]Run tests",
        "2026-11-20T10:00:01.1000000Z grep -iE 'out of memory|API rate limit exceeded' kernel.log",
        "2026-11-20T10:00:01.2000000Z ##[endgroup]",
        "2026-11-20T10:00:02.0000000Z GitHub API rate limit: 4500 REST requests remain; not waiting.",
        "2026-11-20T10:00:03.0000000Z ##[warning]The installation API rate limit is spent; waiting",
        "2026-11-20T10:05:00.0000000Z [gw1] node down: Not properly terminated",
        "2026-11-20T10:06:00.0000000Z FAILED test/test_alpha.py::test_one - AssertionError: boom",
        "2026-11-20T10:06:01.0000000Z ##[error]The runner has received a shutdown signal.",
        "2026-11-20T10:06:02.0000000Z ##[error]Process completed with exit code 1.",
    ]
)


def test_in_a_log_only_an_error_the_job_reported_names_a_cause() -> None:
    facts = CENSUS.scan_log(LOG)
    # The echoed script, the quota status line, the survived wait and the plain
    # xdist line all name a signature; none of them is an error the job reported.
    assert facts.rate_limit is None
    assert facts.infrastructure == "The runner has received a shutdown signal."
    assert facts.failures == {TEST_ID: "AssertionError: boom"}


@pytest.mark.parametrize(
    ("line", "rate_limit", "infrastructure"),
    [
        ("##[error]gh: You have exceeded a secondary rate limit.", True, False),
        ("gh: API rate limit exceeded for installation ID 1 (HTTP 403)", True, False),
        ("##[error]The GitHub API rate limit is spent and resets in 900s", True, False),
        ("FAILED test/test_a.py::test_x - RuntimeError: API rate limit exceeded", True, False),
        ("FAILED test/test_a.py::test_x - worker 'gw3' crashed while running", False, True),
        ("##[error]Process completed with exit code 137.", False, True),
        (
            "GitHub API rate limit: 4500 REST requests remain (floor 100); not waiting.",
            False,
            False,
        ),
        ("  if grep -qi 'API rate limit exceeded' err.txt; then", False, False),
        ("grep -iE 'out of memory|oom-kill' /var/log/kern.log", False, False),
        ("##[warning]The installation API rate limit is spent; waiting 30s", False, False),
        ("::warning::The GitHub API rate limit could not be read; not waiting.", False, False),
        ("test_runner_out_of_memory_is_reported PASSED", False, False),
        ("echo gh: API rate limit exceeded", False, False),
    ],
)
def test_a_signature_counts_in_a_log_only_on_an_error_line(
    line: str, rate_limit: bool, infrastructure: bool
) -> None:
    facts = CENSUS.scan_log(f"2026-11-20T10:00:00.0000000Z {line}")
    assert (facts.rate_limit is not None, facts.infrastructure is not None) == (
        rate_limit,
        infrastructure,
    )


def test_a_log_decides_a_job_whose_annotations_name_nothing() -> None:
    job = job_of(check_run(1, notes=[EXIT_NOTE]))
    assert CENSUS.annotations_name_nothing(job)
    assert CENSUS.classify(job, CENSUS.scan_log(LOG), [main_red()], {})[0] == "broken_main"
    assert CENSUS.classify(job, CENSUS.scan_log(LOG), [], {})[0] == "infrastructure"
    spent = CENSUS.scan_log("##[error]gh: You have exceeded a secondary rate limit.")
    assert CENSUS.classify(job, spent, [main_red()], {})[0] == "rate_limit"
    quiet = CENSUS.scan_log("GitHub API rate limit: 4500 REST requests remain; not waiting.")
    assert CENSUS.classify(job, quiet, [], {}) == ("diff", "step: Run tests (shard 1/8)")


# -- one parser, the watcher's ------------------------------------------------


def test_annotations_are_parsed_by_the_watchers_own_module() -> None:
    assert Path(WATCH.__file__) == ROOT / ".github" / "scripts" / "main_red_nodeids.py"
    assert Path(CENSUS.SKIP.__file__) == ROOT / "scripts" / "ci_main_red_skip.py"
    source = (ROOT / "scripts" / "ci_red_cause_census.py").read_text(encoding="utf-8")
    for name in ("parse_annotations", "annotation_entry", "parse_state", "without_group"):
        assert f"def {name}(" not in source

    rest_rows = [
        {"annotation_level": "failure", "title": TEST_ID, "message": f"{TEST_ID} - boom"},
        {"annotation_level": "failure", "title": None, "message": "Process completed"},
        {"annotation_level": "warning", "title": "test/test_b.py::test_w", "message": "x"},
    ]
    graphql = [
        note(f"{TEST_ID} - boom", title=TEST_ID),
        note("Process completed"),
        note("x", title="test/test_b.py::test_w", level="WARNING"),
    ]
    job = job_of(check_run(1, notes=graphql))
    assert WATCH.parse_annotations(list(job.annotations)) == WATCH.parse_annotations(rest_rows)
    assert WATCH.parse_annotations(rest_rows) == {TEST_ID: "boom"}


# -- a stand-in for `gh`: canned pages, every call recorded -------------------

Handler = Callable[[dict[str, Any]], Any]


class FakeGh:
    """Answers `gh api ...` argv from canned pages and keeps every argv it saw."""

    def __init__(self, **handlers: Handler) -> None:
        self.handlers = {
            "budget": lambda fields: {},
            "issues": lambda fields: issues_page(),
            **handlers,
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.argvs: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.argvs.append(argv)
        assert argv[0] == "api"
        if argv[1] != "graphql":
            kind = "log" if argv[1].endswith("/logs") else "workflows"
            self.calls.append((kind, {"path": argv[1]}))
            body = self.handlers[kind]({"path": argv[1]})
            if body is None:
                return 1, "", "gh: Must have admin rights to Repository. (HTTP 403)"
            return 0, body if isinstance(body, str) else json.dumps(body), ""
        assert all(flag == "-f" for flag in argv[2::2]), "every variable is a raw string"
        fields: dict[str, Any] = {}
        for pair in argv[3::2]:
            name, _, value = pair.partition("=")
            if name.endswith("[]"):
                fields.setdefault(name[:-2], []).append(value)
            else:
                fields[name] = value
        query = fields["query"]
        kind = next(
            kind
            for mark, kind in (
                ("... on Workflow", "runs"),
                ("issues(", "issues"),
                ("page: checkRuns", "suite_page"),
                ("... on CheckSuite", "suites"),
                ("rateLimit", "budget"),
            )
            if mark in query
        )
        self.calls.append((kind, fields))
        data = self.handlers[kind](fields)
        if data is None:
            return 1, json.dumps({"errors": [{"message": "Something went wrong"}]}), "gh: failed"
        return 0, json.dumps({"data": {"rateLimit": {"cost": 2}, **data}}), ""

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]


def api_for(gh: FakeGh) -> Any:
    return CENSUS.Api(run=gh, sleep=lambda seconds: None)


def workflows_page(*names: str) -> Handler:
    rows = [{"node_id": f"W_{name}", "name": name} for name in names]
    return lambda fields: {"total_count": len(rows), "workflows": rows}


def run_node(
    run_id: int,
    created: str,
    red: int = 0,
    event: str = "pull_request",
    sha: str = SHA,
    conclusion: str | None = None,
) -> dict[str, Any]:
    return {
        "databaseId": run_id,
        "createdAt": created,
        "event": event,
        "checkSuite": {
            "id": f"CS_{run_id}",
            "conclusion": conclusion or ("FAILURE" if red else "SUCCESS"),
            "commit": {"oid": sha},
            "red": {"totalCount": red},
        },
    }


def connection(nodes: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    return {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": nodes}


def runs_pages(pages: dict[str | None, dict[str, Any]]) -> Handler:
    return lambda fields: {"node": {"runs": pages[fields.get("after")]}}


def cancelled_jobs(*nodes: dict[str, Any], total: int | None = None) -> dict[str, Any]:
    return {"totalCount": len(nodes) if total is None else total, "nodes": list(nodes)}


def suites_of(
    by_id: dict[str, tuple[dict[str, Any], ...]], pulls: dict[str, int] | None = None
) -> Handler:
    """``by_id`` maps a suite to its (red, passed) or (red, passed, cancelled) check runs."""

    def answer(fields: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for suite in fields["ids"]:
            number = (pulls or {}).get(suite)
            rows.append(
                {
                    "id": suite,
                    "matchingPullRequests": {"nodes": [{"number": number}] if number else []},
                    "red": by_id[suite][0],
                    "passed": by_id[suite][1],
                    "cancelled": by_id[suite][2] if len(by_id[suite]) > 2 else cancelled_jobs(),
                }
            )
        return {"nodes": rows}

    return answer


def watcher_issue(nodeids: list[str], opened: str, closed: str | None) -> dict[str, Any]:
    failures = {nodeid: "AssertionError: boom" for nodeid in nodeids}
    url = "https://github.com/kirodotdev/KiroCrew/actions/runs/1"
    state = WATCH.next_state(None, failures, SHA, 1, url)
    return {
        "number": 900,
        "title": WATCH.title_for(failures),
        "body": WATCH.render_body(state, [], 900),
        "createdAt": opened,
        "closedAt": closed,
        "author": {"__typename": "Bot", "login": "github-actions"},
    }


def issues_page(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"repository": {"issues": connection(list(nodes))}}


def issues_of(*nodes: dict[str, Any]) -> Handler:
    return lambda fields: issues_page(*nodes)


NO_ISSUES = issues_of()


def census_args(out_dir: Path, *more: str) -> list[str]:
    window = ["--since", DAY, "--until", DAY, "--repo", REPO, "--out-dir", str(out_dir)]
    return [*window, "--logs", "undecided", *more]


def ticks(*values: float) -> Callable[[], float]:
    return iter(values).__next__


def read_csv(out_dir: Path) -> list[dict[str, str]]:
    with open(out_dir / "red-jobs.csv", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# -- reading runs: GraphQL, pages of 100, the cursor, the printed counts ------


def test_runs_are_read_by_graphql_in_pages_of_100_across_the_cursor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    newest = [run_node(1000 + at, f"{DAY}T23:{at % 60:02d}:00Z") for at in range(100)]
    older = [run_node(500 + at, f"{DAY}T05:{at % 60:02d}:00Z") for at in range(99)]
    red_run = run_node(400, f"{DAY}T04:00:00Z", red=1)
    before_window = run_node(300, "2026-11-19T23:59:59Z", red=1)
    gh = FakeGh(
        workflows=workflows_page("CI"),
        runs=runs_pages(
            {
                None: connection(newest, "CUR1"),
                "CUR1": connection(older + [red_run], "CUR2"),
                "CUR2": connection([before_window]),
            }
        ),
        suites=suites_of(
            {"CS_400": (connection([check_run(41, notes=[failing()])]), connection([]))}
        ),
        issues=NO_ISSUES,
        log=lambda fields: None,
    )

    status = CENSUS.main(census_args(tmp_path), api=api_for(gh), clock=ticks(0.0, 12.5))

    assert status == 0
    runs_calls = [fields for kind, fields in gh.calls if kind == "runs"]
    assert [fields.get("after") for fields in runs_calls] == [None, "CUR1", "CUR2"]
    assert all("runs(first: 100, after: $after" in fields["query"] for fields in runs_calls)
    assert all(fields["workflow"] == "W_CI" for fields in runs_calls)
    assert CENSUS.PAGE == 100
    # The red run before the window is paged past, never read as a check suite.
    assert [fields["ids"] for kind, fields in gh.calls if kind == "suites"] == [["CS_400"]]
    assert gh.kinds() == ["budget", "issues", "workflows", "runs", "runs", "runs", "suites"]
    printed = capsys.readouterr().out
    assert "GraphQL calls: 6 (12 points)\nREST calls: 1\nRun time: 12.5s\n" in printed
    assert "Read 200 run(s) of 1 workflow(s); 1 red job(s)." in printed
    assert [row["job_id"] for row in read_csv(tmp_path)] == ["41"]


def test_paging_stops_at_the_first_page_that_reaches_past_the_window() -> None:
    gh = FakeGh(
        runs=runs_pages(
            {
                None: connection(
                    [
                        run_node(3, "2026-11-21T00:00:00Z"),
                        run_node(2, f"{DAY}T12:00:00Z"),
                        run_node(1, "2026-11-19T23:00:00Z"),
                    ],
                    "CUR1",
                )
            }
        )
    )
    runs = CENSUS.read_runs(api_for(gh), "W_CI", "CI", f"{DAY}T00:00:00Z", "2026-11-21T00:00:00Z")
    assert [run.run_id for run in runs] == [2]
    assert gh.kinds() == ["runs"]


def test_the_pr_number_is_read_for_a_pull_request_run_and_never_for_a_push() -> None:
    red = (connection([check_run(1)]), connection([]))
    gh = FakeGh(suites=suites_of({"CS_1": red, "CS_2": red}, pulls={"CS_1": 7, "CS_2": 9}))
    runs = [run_of(run_id=1, pr=None), run_of(run_id=2, event="push", pr=None)]
    jobs, _passed, _unread = CENSUS.read_jobs(api_for(gh), runs)
    assert [(job.run.event, job.run.pr) for job in jobs] == [("pull_request", 7), ("push", None)]
    assert "matchingPullRequests" not in CENSUS.RUNS_QUERY


def test_check_runs_past_the_first_100_of_a_suite_are_read_by_cursor() -> None:
    first = [check_run(at, name=f"Job {at}") for at in range(100)]
    gh = FakeGh(
        suites=suites_of({"CS_1": (connection(first, "RED1"), connection([]))}),
        suite_page=lambda fields: {"node": {"page": connection([check_run(100, name="Job 100")])}},
    )
    jobs, _passed, _unread = CENSUS.read_jobs(api_for(gh), [run_of(run_id=1)])
    assert [job.job_id for job in jobs] == list(range(101))
    assert gh.calls[1][1]["suite"] == "CS_1"
    assert gh.calls[1][1]["after"] == "RED1"
    assert "checkRuns(first: 100, after: $after" in gh.calls[1][1]["query"]


def cancelled_run(run_id: int) -> Any:
    return run_of(run_id=run_id)._replace(red_jobs=0, cancelled=True)


def test_a_job_the_runner_cancelled_is_read_without_its_steps_and_counted() -> None:
    timed_out = check_run(
        7,
        conclusion="CANCELLED",
        notes=[note("The job has exceeded the maximum execution time of 45m0s")],
        failed_step=None,
    )
    superseded = check_run(8, conclusion="CANCELLED", notes=[note("The operation was canceled.")])
    suite = (connection([]), connection([], "PASSED1"), cancelled_jobs(timed_out, superseded))
    gh = FakeGh(suites=suites_of({"CS_1": suite}))

    jobs, passed, _unread = CENSUS.read_jobs(api_for(gh), [cancelled_run(1)])

    assert [(job.job_id, CENSUS.is_red(job)) for job in jobs] == [(7, True), (8, False)]
    assert CENSUS.classify(jobs[0], None, [], {})[0] == "infrastructure"
    # No red job, so no later pass is looked for: the passes are not paged.
    assert (passed, gh.kinds()) == ({}, ["suites"])
    query = gh.calls[0][1]["query"]
    assert "cancelled: checkRuns(first: 5," in query
    for unasked in ("steps(", "red: checkRuns", "passed: checkRuns"):
        assert unasked not in query
    # Beside red jobs the cancelled ones are read the same way: annotations, no steps.
    beside_red = CENSUS.suites_query(5)
    cancelled = beside_red[beside_red.index("cancelled: checkRuns") :]
    assert "cancelled: checkRuns(first: 5," in cancelled
    assert "steps(" not in cancelled
    assert "steps(first: 100)" in beside_red[: beside_red.index("cancelled: checkRuns")]


def test_a_run_with_more_cancelled_jobs_than_the_cap_is_a_cancelled_run() -> None:
    shown = [check_run(at, conclusion="CANCELLED", notes=[SHUTDOWN_NOTE]) for at in range(5)]
    suite = (connection([]), connection([]), cancelled_jobs(*shown, total=60))
    gh = FakeGh(suites=suites_of({"CS_1": suite}))
    jobs, _passed, _unread = CENSUS.read_jobs(api_for(gh), [cancelled_run(1)])
    assert jobs == []
    assert CENSUS.CANCELLED_READ_MAX == 5


def test_a_check_suite_is_asked_for_no_more_red_jobs_than_its_run_counted() -> None:
    red = (connection([check_run(1)]), connection([]))
    counts = {1: 1, 2: 5, 3: 6, 4: 25, 5: 26, 6: 100, 7: 250, 8: 0}
    runs = [
        run_of(run_id=run_id)._replace(red_jobs=count, cancelled=count == 0)
        for run_id, count in counts.items()
    ]
    gh = FakeGh(suites=suites_of({f"CS_{run_id}": red for run_id in counts}))

    CENSUS.read_jobs(api_for(gh), runs)

    asked = {}
    for _kind, fields in gh.calls:
        _, red, rest = fields["query"].partition("red: checkRuns(first: ")
        asked[int(rest.split(",")[0]) if red else 0] = fields["ids"]
    assert asked == {
        0: ["CS_8"],
        5: ["CS_1", "CS_2"],
        25: ["CS_3", "CS_4"],
        100: ["CS_5", "CS_6", "CS_7"],
    }


def test_cancelled_runs_with_no_red_job_are_read_fifty_to_a_call() -> None:
    nothing = (connection([]), connection([]))
    gh = FakeGh(suites=suites_of({f"CS_{at}": nothing for at in range(120)}))
    jobs, _passed, _unread = CENSUS.read_jobs(api_for(gh), [cancelled_run(at) for at in range(120)])
    assert jobs == []
    assert [len(fields["ids"]) for _kind, fields in gh.calls] == [50, 50, 20]


def test_a_batch_github_cannot_answer_is_read_as_two_halves() -> None:
    empty = (connection([check_run(1)]), connection([]))
    by_id = {f"CS_{at}": empty for at in range(4)}
    answer = suites_of(by_id)
    gh = FakeGh(suites=lambda fields: None if len(fields["ids"]) > 2 else answer(fields))
    jobs, _passed, _unread = CENSUS.read_jobs(api_for(gh), [run_of(run_id=at) for at in range(4)])
    assert [job.run.run_id for job in jobs] == [0, 1, 2, 3]
    sizes = [len(fields["ids"]) for _kind, fields in gh.calls]
    assert sizes == [4] * CENSUS.ATTEMPTS + [2, 2]


def test_a_single_suite_github_cannot_answer_ends_the_census() -> None:
    gh = FakeGh(suites=lambda fields: None)
    with pytest.raises(CENSUS.CensusError, match="GraphQL read failed"):
        CENSUS.read_jobs(api_for(gh), [run_of(run_id=1), run_of(run_id=2)])
    assert [len(fields["ids"]) for _kind, fields in gh.calls] == [2] * 3 + [1] * 3


def test_check_suites_are_read_ten_to_a_call_and_only_for_runs_with_a_red_job() -> None:
    empty = (connection([]), connection([]))
    gh = FakeGh(suites=suites_of({f"CS_{at}": empty for at in range(25)}))
    runs = [run_of(run_id=at) for at in range(25)]
    green = CENSUS.Run(99, "CI", "push", SHA, None, f"{DAY}T01:00:00Z", "CS_99", 0, False)
    CENSUS.read_jobs(api_for(gh), runs + [green])
    assert [len(fields["ids"]) for _kind, fields in gh.calls] == [10, 10, 5]


# -- the main-red issues ------------------------------------------------------


def test_main_red_issues_are_the_watchers_own_and_overlap_the_window() -> None:
    ours = watcher_issue([TEST_ID + "@serial"], f"{DAY}T08:00:00Z", None)
    by_person = {**ours, "number": 901, "author": {"__typename": "User", "login": "someone"}}
    no_record = {**ours, "number": 902, "body": "Main is red, trust me."}
    closed_before = {**ours, "number": 903, "closedAt": "2026-11-19T10:00:00Z"}
    opened_after = {**ours, "number": 904, "createdAt": "2026-11-21T00:00:01Z"}
    gh = FakeGh(issues=issues_of(ours, by_person, no_record, closed_before, opened_after))

    found = CENSUS.read_main_red(api_for(gh), REPO, f"{DAY}T00:00:00Z", "2026-11-21T00:00:00Z")

    assert [(issue.number, issue.nodeids) for issue in found] == [(900, frozenset({TEST_ID}))]
    assert [gh.calls[0][1]["owner"], gh.calls[0][1]["name"]] == REPO.split("/")
    assert 'labels: ["main-red"]' in gh.calls[0][1]["query"]


# -- the whole census ---------------------------------------------------------

HOSTILE = "$(touch pwned) `id` ☃ =cmd|' /C calc'!A0 " + "x" * 200


def full_census(tmp_path: Path, log: Handler, *more: str) -> tuple[FakeGh, Any, dict[str, Any]]:
    """Two workflows, one day: every cause once, a cancelled run and a job with no evidence."""
    other_sha = "b" * 40
    after_close = f"{DAY}T12:01:00Z"
    ci_runs = [
        run_node(10, f"{DAY}T10:00:00Z", red=5),
        run_node(11, f"{DAY}T11:00:00Z", red=1, sha=other_sha),
        run_node(12, f"{DAY}T12:00:00Z", red=3, event="push", sha="c" * 40),
        run_node(13, f"{DAY}T13:00:00Z", red=0, sha="d" * 40),
    ]
    review_runs = [run_node(20, f"{DAY}T10:00:00Z", red=1)]
    suites = {
        "CS_10": (
            connection(
                [
                    check_run(101, "Backend Tests (3.12, 1)", notes=[failing()]),
                    check_run(102, "Backend Tests (3.12, 2)", notes=[SHUTDOWN_NOTE]),
                    check_run(
                        103,
                        "Backend Tests (Windows) (3)",
                        notes=[failing("test/test_beta.py::test_two")],
                    ),
                    check_run(104, "Frontend Tests (1)", notes=[note(HOSTILE)]),
                ]
            ),
            connection(
                [
                    {
                        "databaseId": 199,
                        "name": "Backend Tests (Windows) (3)",
                        "completedAt": f"{DAY}T11:30:00Z",
                    }
                ]
            ),
            cancelled_jobs(
                check_run(
                    105,
                    "Backend Tests (3.12, 4)",
                    conclusion="CANCELLED",
                    notes=[note("The run was canceled by @someone.")],
                )
            ),
        ),
        "CS_11": (
            connection([check_run(111, "Coverage Gate", notes=[EXIT_NOTE], started=after_close)]),
            connection([]),
        ),
        "CS_12": (
            connection(
                [
                    check_run(
                        121, "Backend Tests (3.12, 1)", notes=[failing()], started=after_close
                    ),
                    check_run(
                        122, "Backend Tests (3.12, 5)", notes=[SHUTDOWN_NOTE], started=after_close
                    ),
                    check_run(
                        123, "Backend Tests (3.12, 6)", conclusion="TIMED_OUT", started=after_close
                    ),
                ]
            ),
            connection([]),
        ),
        "CS_20": (connection([check_run(201, "review", notes=[RATE_NOTE])]), connection([])),
    }
    gh = FakeGh(
        workflows=workflows_page("CI", "Code Review"),
        runs=lambda fields: {
            "node": {"runs": connection(ci_runs if fields["workflow"] == "W_CI" else review_runs)}
        },
        suites=suites_of(suites, pulls={"CS_10": 7, "CS_11": 8, "CS_12": 9}),
        issues=issues_of(watcher_issue([TEST_ID], f"{DAY}T08:00:00Z", f"{DAY}T10:30:00Z")),
        log=log,
    )
    api = api_for(gh)
    assert CENSUS.main(census_args(tmp_path, *more), api=api, clock=ticks(0.0, 1.0)) == 0
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    return gh, api, summary


def test_the_census_writes_one_row_per_red_job_and_the_three_goal_numbers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh, api, summary = full_census(tmp_path, log=lambda fields: None)

    rows = read_csv(tmp_path)
    assert list(rows[0]) == list(CENSUS.CSV_COLUMNS)
    assert [(row["job_id"], row["cause"]) for row in rows] == [
        ("101", "broken_main"),
        ("102", "infrastructure"),
        ("103", "flaky"),
        ("104", "diff"),
        ("111", "diff"),
        ("121", "diff"),
        ("122", "infrastructure"),
        ("123", "infrastructure"),
        ("201", "rate_limit"),
    ]
    assert rows[0] == {
        "run_id": "10",
        "job_id": "101",
        "workflow": "CI",
        "job": "Backend Tests (3.12, 1)",
        "event": "pull_request",
        "head_sha": SHA,
        "pr": "7",
        "cause": "broken_main",
        "evidence": f"#900 {TEST_ID}",
    }
    assert rows[5]["pr"] == ""

    assert summary["by_cause"] == {
        "rate_limit": 1,
        "broken_main": 1,
        "infrastructure": 3,
        "flaky": 1,
        "diff": 3,
    }
    assert summary["red_jobs"] == 9
    assert summary["runs"] == 5
    assert summary["broken_main_share_of_red_pr_pushes"] == {
        "share": 0.5,
        "broken_main": 1,
        "broken_main_only": 0,
        "n": 2,
    }
    assert summary["rate_limit_share_of_red_jobs"] == {"share": 0.1111, "rate_limit": 1, "n": 9}
    assert summary["backend_shard_flaky_or_infrastructure_reds"] == {
        "per_day": 4.0,
        "flaky": 1,
        "infrastructure": 3,
        "n": 6,
        "pushes_per_day": 4.0,
        "pushes": 4,
    }
    # Only the jobs whose annotations name nothing asked for their log.
    assert [fields["path"] for kind, fields in gh.calls if kind == "log"] == [
        f"repos/{REPO}/actions/jobs/{job_id}/logs" for job_id in (104, 111, 123)
    ]
    assert summary["log_unread"] == 3
    assert (api.graphql_calls, api.rest_calls) == (5, 4)

    printed = capsys.readouterr().out
    markdown = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert markdown in printed
    assert "| Red PR pushes with a job red from a broken main | 50.0% (1) | 2 red PR pushes |" in (
        markdown
    )
    assert "| Red jobs caused by the API rate limit, all workflows | 11.1% (1) | 9 red jobs |" in (
        markdown
    )
    assert "per day | 4.0 (1 flaky + 3 infrastructure) | 6 red backend shard jobs, 4.0 pushes" in (
        markdown
    )
    assert "GraphQL calls: 5 (10 points)\nREST calls: 4\nRun time: 1.0s\n" in printed


def test_a_job_with_no_readable_log_and_no_annotations_is_counted_never_dropped(
    tmp_path: Path,
) -> None:
    gh = FakeGh(
        workflows=workflows_page("CI"),
        runs=runs_pages({None: connection([run_node(10, f"{DAY}T10:00:00Z", red=1)])}),
        suites=suites_of(
            {"CS_10": (connection([check_run(101, failed_step=None)]), connection([]))}
        ),
        issues=NO_ISSUES,
        log=lambda fields: None,
    )
    assert CENSUS.main(census_args(tmp_path), api=api_for(gh), clock=ticks(0.0, 1.0)) == 0

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["log_unread"] == 1
    assert summary["logs_read"] == 0
    assert summary["red_jobs"] == 1
    assert [(row["job_id"], row["cause"], row["evidence"]) for row in read_csv(tmp_path)] == [
        ("101", "diff", "")
    ]
    assert "`log_unread`: 1 job(s)" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_a_readable_log_decides_the_job_and_leaves_log_unread_at_zero(tmp_path: Path) -> None:
    _gh, _api, summary = full_census(tmp_path, log=lambda fields: LOG)
    assert (summary["log_unread"], summary["logs_read"]) == (0, 3)
    row = next(row for row in read_csv(tmp_path) if row["job_id"] == "111")
    assert (row["cause"], row["evidence"]) == (
        "infrastructure",
        "The runner has received a shutdown signal.",
    )


def test_logs_never_reads_no_log_and_counts_every_job_it_left_undecided(tmp_path: Path) -> None:
    gh, _api, summary = full_census(tmp_path, lambda fields: LOG, "--logs", "never")
    assert gh.kinds().count("log") == 0
    assert (summary["log_unread"], summary["logs_read"]) == (3, 0)


def test_the_rows_and_the_call_counts_do_not_depend_on_the_worker_count(tmp_path: Path) -> None:
    results = []
    for workers in ("1", "8"):
        out_dir = tmp_path / workers
        _gh, api, summary = full_census(out_dir, lambda fields: None, "--workers", workers)
        rows = (out_dir / "red-jobs.csv").read_bytes()
        results.append((rows, summary, api.graphql_calls, api.graphql_points, api.rest_calls))
    assert results[0] == results[1]


def test_reads_made_side_by_side_come_back_in_the_order_asked() -> None:
    items = list(range(50))
    assert CENSUS._each(8, lambda item: item * 2, items) == [item * 2 for item in items]
    assert CENSUS._each(1, lambda item: item * 2, items) == [item * 2 for item in items]


def test_untrusted_text_is_cut_to_80_printable_ascii_and_never_reaches_a_command(
    tmp_path: Path,
) -> None:
    gh, _api, _summary = full_census(tmp_path, log=lambda fields: HOSTILE)

    evidence = next(row for row in read_csv(tmp_path) if row["job_id"] == "104")["evidence"]
    assert len(evidence) == CENSUS.EVIDENCE_CHARS
    assert all(" " <= ch <= "~" for ch in evidence)
    assert "`" not in evidence
    for argv in gh.argvs:
        assert not any("pwned" in word or "calc" in word for word in argv)
    raw = (tmp_path / "red-jobs.csv").read_bytes()
    assert b"\r" not in raw
    assert raw.isascii()


def test_a_cell_a_spreadsheet_would_run_is_written_as_text() -> None:
    assert CENSUS._cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert CENSUS._cell("-1+1") == "'-1+1"
    assert CENSUS._cell("@SUM(1)") == "'@SUM(1)"
    assert CENSUS._cell("step: Run tests") == "step: Run tests"
    assert CENSUS._cell(41) == "41"


# -- the summary's edges ------------------------------------------------------


def test_an_empty_window_gives_no_share_rather_than_a_division() -> None:
    summary = CENSUS.summarize(CENSUS.Census(), 1)
    assert summary["broken_main_share_of_red_pr_pushes"]["share"] is None
    assert summary["rate_limit_share_of_red_jobs"] == {"share": None, "rate_limit": 0, "n": 0}
    assert summary["backend_shard_flaky_or_infrastructure_reds"]["per_day"] == 0.0
    assert "| n/a (0) | 0 red jobs |" in CENSUS.render_markdown(summary, REPO, DAY, DAY)


@pytest.mark.parametrize(
    ("name", "shard"),
    [
        ("Backend Tests (3.12, 1)", True),
        ("Backend Tests (3.13, 12)", True),
        ("Backend Tests (Windows) (8)", True),
        ("Backend Tests", False),
        ("Backend Tests (Windows)", False),
        ("Backend Tests (namespace sandbox)", False),
        ("Backend Tests (IPv6, windows-latest)", False),
        ("Frontend Tests (1)", False),
    ],
)
def test_only_the_linux_and_windows_shard_jobs_count_as_backend_shards(
    name: str, shard: bool
) -> None:
    assert (CENSUS.BACKEND_SHARD.fullmatch(name) is not None) is shard


def test_a_day_given_as_until_is_included_whole() -> None:
    since, until = CENSUS.parse_bound("2026-11-20"), CENSUS.parse_bound("2026-12-03")
    assert CENSUS.window(since, until) == ("2026-11-20T00:00:00Z", "2026-12-04T00:00:00Z", 14)
    assert CENSUS.window(since, since)[2] == 1
    with pytest.raises(CENSUS.CensusError):
        CENSUS.window(until, since)


def test_a_window_can_be_a_few_hours() -> None:
    since, until = CENSUS.parse_bound("2026-11-20T09:00Z"), CENSUS.parse_bound("2026-11-20T12:00Z")
    assert CENSUS.window(since, until) == ("2026-11-20T09:00:00Z", "2026-11-20T12:00:00Z", 0.125)
    with_seconds = CENSUS.parse_bound("2026-11-20T12:00:30Z")
    assert CENSUS.window(since, with_seconds)[1] == "2026-11-20T12:00:30Z"
    with pytest.raises(CENSUS.CensusError, match="not after"):
        CENSUS.window(since, since)


def test_a_few_hours_are_scaled_to_a_day_in_the_per_day_number(tmp_path: Path) -> None:
    gh = FakeGh(
        workflows=workflows_page("CI"),
        runs=runs_pages({None: connection([run_node(10, f"{DAY}T10:00:00Z", red=1)])}),
        suites=suites_of(
            {
                "CS_10": (
                    connection([check_run(101, "Backend Tests (3.12, 2)", notes=[SHUTDOWN_NOTE])]),
                    connection([]),
                )
            }
        ),
    )
    args = ["--since", f"{DAY}T09:00Z", "--until", f"{DAY}T12:00Z", "--repo", REPO]
    assert CENSUS.main([*args, "--out-dir", str(tmp_path)], api=api_for(gh), clock=ticks(0, 1)) == 0
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["days"] == 0.125
    assert summary["backend_shard_flaky_or_infrastructure_reds"]["per_day"] == 8.0
    assert summary["backend_shard_flaky_or_infrastructure_reds"]["pushes_per_day"] == 8.0
    markdown = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert f"{DAY}T09:00Z to {DAY}T12:00Z UTC (0.125 day(s))" in markdown


@pytest.mark.parametrize("text", ["2026-11-20T09Z", "20261120", "2026-13-01", "2026-11-20T25:00Z"])
def test_a_window_bound_that_is_not_a_date_or_time_is_refused(
    text: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stop:
        CENSUS.main(["--since", text, "--until", DAY, "--repo", REPO, "--out-dir", str(tmp_path)])
    assert stop.value.code == 2
    assert "date" in capsys.readouterr().err


# -- the GraphQL budget -------------------------------------------------------


def bucket(remaining: int, cost: int = 1) -> dict[str, Any]:
    return {"rateLimit": {"cost": cost, "remaining": remaining, "resetAt": "2026-11-20T11:00:00Z"}}


def test_a_bucket_already_under_the_reserve_spends_one_point_and_stops(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh(budget=lambda fields: bucket(1999))

    status = CENSUS.main(census_args(tmp_path), api=api_for(gh), clock=ticks(0.0, 1.0))

    assert status == CENSUS.PARTIAL_EXIT == 3
    assert gh.kinds() == ["budget"]
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["partial"] == (
        "1999 GraphQL points left, under the --reserve-points of 2000 "
        "(the bucket resets at 2026-11-20T11:00:00Z)"
    )
    assert summary["red_jobs"] == 0
    printed = capsys.readouterr().out
    assert "**Partial: stopped early (1999 GraphQL points left" in printed
    assert "GraphQL calls: 1 (1 points)\n" in printed
    assert "GraphQL points left: 1999 (the bucket resets at 2026-11-20T11:00:00Z)" in printed
    assert read_csv(tmp_path) == []


def test_the_reserve_can_be_lowered_by_flag(tmp_path: Path) -> None:
    gh = FakeGh(
        budget=lambda fields: bucket(1999),
        workflows=workflows_page("CI"),
        runs=runs_pages({None: connection([])}),
    )
    args = census_args(tmp_path, "--reserve-points", "1000")
    assert CENSUS.main(args, api=api_for(gh), clock=ticks(0.0, 1.0)) == 0
    assert gh.kinds() == ["budget", "issues", "workflows", "runs"]
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["partial"] is None


def two_pages_then_a_red_run(first_page: dict[str, Any]) -> FakeGh:
    newest = [run_node(1000 + at, f"{DAY}T23:{at % 60:02d}:00Z") for at in range(100)]
    red = (connection([check_run(41, notes=[failing()])]), connection([]))
    return FakeGh(
        workflows=workflows_page("CI"),
        runs=lambda fields: {
            **(first_page if fields.get("after") is None else {}),
            "node": {
                "runs": (
                    connection(newest, "CUR1")
                    if fields.get("after") is None
                    else connection([run_node(400, f"{DAY}T04:00:00Z", red=1)])
                )
            },
        },
        suites=suites_of({"CS_400": red}),
    )


def test_a_bucket_that_falls_under_the_reserve_mid_census_keeps_what_was_read(
    tmp_path: Path,
) -> None:
    gh = two_pages_then_a_red_run(bucket(1990))

    status = CENSUS.main(census_args(tmp_path), api=api_for(gh), clock=ticks(0.0, 1.0))

    assert status == CENSUS.PARTIAL_EXIT
    # The second page of runs, and every check suite, is never asked for.
    assert gh.kinds() == ["budget", "issues", "workflows", "runs"]
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["runs"] == 100
    assert summary["partial"].startswith("1990 GraphQL points left")
    assert (summary["runs_read_from"], summary["runs_read_to"]) == (
        f"{DAY}T23:00:00Z",
        f"{DAY}T23:59:00Z",
    )
    assert f"Runs read: 100, created {DAY}T23:00:00Z to {DAY}T23:59:00Z" in (
        tmp_path / "summary.md"
    ).read_text(encoding="utf-8")
    assert "they are not a census of the window" in (tmp_path / "summary.md").read_text(
        encoding="utf-8"
    )


def test_max_points_stops_the_census_with_partial_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = two_pages_then_a_red_run({})
    args = census_args(tmp_path, "--max-points", "6")

    status = CENSUS.main(args, api=api_for(gh), clock=ticks(0.0, 1.0))

    # Two points a call: the bucket read, the issues and one page of runs spend the six.
    assert status == CENSUS.PARTIAL_EXIT
    assert gh.kinds() == ["budget", "issues", "workflows", "runs"]
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["partial"] == "6 GraphQL points spent, the --max-points of 6"
    assert "GraphQL calls: 3 (6 points)\n" in capsys.readouterr().out


# A partial census is consistent: a check suite is in the output whole or not at all.

SHARD = "Backend Tests (3.12, 1)"


def sha_of(run_id: int) -> str:
    return f"{run_id:040x}"


def own_push(run_id: int) -> Any:
    """A red run on a head SHA no other run shares."""
    return run_of(run_id=run_id, sha=sha_of(run_id))


def rerun_passed(job_id: int, cursor: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """A suite whose red job passed on a re-run; with ``cursor`` the pass is on a second page."""
    red = connection([check_run(job_id, SHARD, notes=[failing()])])
    later = {"databaseId": job_id + 5000, "name": SHARD, "completedAt": f"{DAY}T11:00:00Z"}
    if cursor is None:
        return red, connection([later])
    return red, connection([], cursor)


def passes_page(fields: dict[str, Any]) -> dict[str, Any]:
    job_id = int(fields["suite"].removeprefix("CS_"))
    later = {"databaseId": job_id + 5000, "name": SHARD, "completedAt": f"{DAY}T11:00:00Z"}
    return {"node": {"page": connection([later])}}


def causes(jobs: list[Any], passed: Any) -> dict[int, str]:
    return {job.job_id: CENSUS.classify(job, None, [], passed)[0] for job in jobs}


def test_a_stop_on_the_second_page_of_passes_leaves_the_suite_out_not_misfiled() -> None:
    suites = suites_of({"CS_1": rerun_passed(1, cursor="PASSED1")})
    whole = FakeGh(suites=suites, suite_page=passes_page)
    jobs, passed, unread = CENSUS.read_jobs(api_for(whole), [own_push(1)])
    assert (causes(jobs, passed), unread) == ({1: "flaky"}, 0)

    stopped = FakeGh(suites=suites, suite_page=passes_page)
    api = CENSUS.Api(run=stopped, sleep=lambda seconds: None, max_points=2)
    jobs, passed, unread = CENSUS.read_jobs(api, [own_push(1)])
    # Read to the end the job is `flaky`; its red half alone would say `diff`.
    assert (jobs, unread) == ([], 1)
    assert stopped.kinds() == ["suites"]


def test_a_stop_between_suites_keeps_the_ones_the_first_page_holds_whole() -> None:
    by_id = {
        "CS_1": rerun_passed(1),
        "CS_2": rerun_passed(2, cursor="PASSED2"),
        "CS_3": rerun_passed(3),
    }
    gh = FakeGh(suites=suites_of(by_id), suite_page=passes_page)
    api = CENSUS.Api(run=gh, sleep=lambda seconds: None, max_points=2)

    jobs, passed, unread = CENSUS.read_jobs(api, [own_push(at) for at in (1, 2, 3)])

    assert (causes(jobs, passed), unread) == ({1: "flaky", 3: "flaky"}, 1)
    assert gh.kinds() == ["suites"]


def test_an_unread_suite_takes_the_other_runs_of_its_push_out_with_it() -> None:
    # Runs 1 and 2 are the same workflow on the same head SHA: a pass of run 2,
    # left unread, could have made run 1's red job `flaky`.
    by_id = {"CS_1": rerun_passed(1), "CS_2": rerun_passed(2, cursor="PASSED2")}
    by_id["CS_3"] = rerun_passed(3)
    gh = FakeGh(suites=suites_of(by_id), suite_page=passes_page)
    api = CENSUS.Api(run=gh, sleep=lambda seconds: None, max_points=2)
    runs = [run_of(run_id=1), run_of(run_id=2), own_push(3)]

    jobs, passed, unread = CENSUS.read_jobs(api, runs)

    assert (causes(jobs, passed), unread) == ({3: "flaky"}, 2)


def test_a_stop_in_one_worker_while_others_are_mid_batch_misfiles_no_job() -> None:
    count = 80
    by_id = {f"CS_{at}": rerun_passed(at, cursor="MORE" if at % 2 else None) for at in range(count)}
    runs = [own_push(at) for at in range(count)]
    whole = FakeGh(suites=suites_of(by_id), suite_page=passes_page)
    jobs, passed, unread = CENSUS.read_jobs(api_for(whole), runs, workers=8)
    complete = causes(jobs, passed)
    assert (set(complete.values()), len(complete), unread) == ({"flaky"}, count, 0)

    gh = FakeGh(suites=suites_of(by_id), suite_page=passes_page)
    api = CENSUS.Api(run=gh, sleep=lambda seconds: None, max_points=6)
    jobs, passed, unread = CENSUS.read_jobs(api, runs, workers=8)

    partial = causes(jobs, passed)
    assert 0 < len(partial) < count
    assert len(partial) + unread == count
    # Every job that is reported has the cause the complete read gave it.
    assert all(complete[job_id] == cause for job_id, cause in partial.items())


def test_a_refused_batch_names_every_suite_in_it_unread() -> None:
    red = (connection([check_run(1)]), connection([]))
    gh = FakeGh(suites=suites_of({f"CS_{at}": red for at in range(25)}))
    api = CENSUS.Api(run=gh, sleep=lambda seconds: None, max_points=4)
    jobs, _passed, unread = CENSUS.read_jobs(api, [own_push(at) for at in range(25)])
    assert ([job.run.run_id for job in jobs], unread) == (list(range(20)), 5)
    assert api.stopped == "4 GraphQL points spent, the --max-points of 4"


def test_the_summary_counts_the_suites_a_stop_left_unread(tmp_path: Path) -> None:
    gh = FakeGh(
        workflows=workflows_page("CI"),
        runs=runs_pages(
            {
                None: connection(
                    [
                        run_node(1, f"{DAY}T10:00:00Z", red=1, sha=sha_of(1)),
                        run_node(2, f"{DAY}T09:00:00Z", red=1, sha=sha_of(2)),
                    ]
                )
            }
        ),
        suites=suites_of({"CS_1": rerun_passed(1), "CS_2": rerun_passed(2, cursor="PASSED2")}),
        suite_page=passes_page,
    )
    args = census_args(tmp_path, "--max-points", "8")

    assert CENSUS.main(args, api=api_for(gh), clock=ticks(0.0, 1.0)) == CENSUS.PARTIAL_EXIT

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert (summary["suites_unread"], summary["red_jobs"]) == (1, 1)
    assert summary["by_cause"]["flaky"] == 1
    assert summary["by_cause"]["diff"] == 0
    assert [row["job_id"] for row in read_csv(tmp_path)] == ["1"]
    assert "1 of their check suites with a red or cancelled job were left unread" in (
        tmp_path / "summary.md"
    ).read_text(encoding="utf-8")


def test_a_stop_while_reading_the_main_red_issues_reads_no_run(tmp_path: Path) -> None:
    gh = FakeGh(
        issues=lambda fields: {"repository": {"issues": connection([], "ISSUES2")}},
        workflows=workflows_page("CI"),
    )
    args = census_args(tmp_path, "--max-points", "4")
    assert CENSUS.main(args, api=api_for(gh), clock=ticks(0.0, 1.0)) == CENSUS.PARTIAL_EXIT
    # Without every issue a broken-main job would read as `diff`, so no run is read at all.
    assert gh.kinds() == ["budget", "issues"]
    assert read_csv(tmp_path) == []


def test_nothing_is_spent_after_the_stop() -> None:
    gh = FakeGh(runs=runs_pages({None: connection([], None)}))
    api = CENSUS.Api(run=gh, sleep=lambda seconds: None, max_points=2)
    api.graphql(CENSUS.RUNS_QUERY, {"workflow": "W_CI"})
    for _ in range(3):
        with pytest.raises(CENSUS.BudgetStop):
            api.graphql(CENSUS.RUNS_QUERY, {"workflow": "W_CI"})
    assert CENSUS.read_runs(api, "W_CI", "CI", f"{DAY}T00:00:00Z", "2026-11-21T00:00:00Z") == []
    assert (len(gh.argvs), api.graphql_calls, api.graphql_points) == (1, 1, 2)


def test_the_defaults_leave_most_of_a_shared_bucket_alone() -> None:
    assert (CENSUS.DEFAULT_RESERVE_POINTS, CENSUS.DEFAULT_MAX_POINTS) == (2000, 1500)
    api = CENSUS.Api(run=FakeGh())
    assert (api.reserve, api.max_points) == (2000, 1500)
    for query in (
        CENSUS.BUDGET_QUERY,
        CENSUS.RUNS_QUERY,
        CENSUS.suites_query(5),
        CENSUS.CANCELLED_SUITES_QUERY,
        CENSUS.ISSUES_QUERY,
        *CENSUS.SUITE_PAGE_QUERIES.values(),
    ):
        assert "rateLimit { cost remaining resetAt }" in query
    args = CENSUS.build_parser().parse_args(
        ["--since", DAY, "--until", DAY, "--repo", REPO, "--out-dir", "out"]
    )
    assert (args.logs, args.reserve_points, args.max_points) == ("never", 2000, 1500)
    text = CENSUS.build_parser().format_help()
    for said in ("--reserve-points", "--max-points", "2,786", "gh-rate-limit"):
        assert said in " ".join(text.split())


# -- the API wrapper ----------------------------------------------------------


def test_a_failed_read_is_retried_and_every_attempt_is_counted() -> None:
    answers = iter([None, None, {"node": {"runs": connection([])}}])
    waits: list[float] = []
    gh = FakeGh(runs=lambda fields: next(answers))
    api = CENSUS.Api(run=gh, sleep=waits.append)

    assert api.graphql(CENSUS.RUNS_QUERY, {"workflow": "W_CI"}) == {
        "rateLimit": {"cost": 2},
        "node": {"runs": connection([])},
    }
    assert api.graphql_calls == 3
    assert waits == [2, 10]


def test_a_read_that_keeps_failing_ends_the_census_with_the_counts_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh(workflows=workflows_page("CI"), runs=lambda fields: None)

    status = CENSUS.main(census_args(tmp_path), api=api_for(gh), clock=ticks(0.0, 3.0))

    assert status == 1
    captured = capsys.readouterr()
    assert "GraphQL read failed" in captured.err
    assert "GraphQL calls: 5 (4 points)\nREST calls: 1\nRun time: 3.0s\n" in captured.out
    assert not (tmp_path / "red-jobs.csv").exists()


def test_a_repository_that_is_not_owner_name_is_refused_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh()
    args = ["--since", DAY, "--until", DAY, "--repo", "a/b/../c", "--out-dir", str(tmp_path)]
    assert CENSUS.main(args, api=api_for(gh), clock=ticks(0.0, 0.0)) == 1
    assert gh.argvs == []
    assert "owner/name" in capsys.readouterr().err


def test_the_workflow_list_follows_rest_pages() -> None:
    full = [{"node_id": f"W_{at}", "name": f"wf {at}"} for at in range(100)]
    pages = {"1": full, "2": [{"node_id": "W_last", "name": "last"}]}
    gh = FakeGh(workflows=lambda fields: {"workflows": pages[fields["path"].rpartition("=")[2]]})
    api = api_for(gh)
    found = CENSUS.list_workflows(api, REPO)
    assert len(found) == 101
    assert found[-1] == ("W_last", "last")
    assert api.rest_calls == 2
    assert gh.calls[0][1]["path"] == f"repos/{REPO}/actions/workflows?per_page=100&page=1"

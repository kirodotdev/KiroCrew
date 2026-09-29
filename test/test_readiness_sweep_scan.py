"""Unit tests for .github/scripts/readiness_sweep_scan.py.

The scanner is the one thing that stands between the readiness sweep and the
shared REST rate-limit pool, so the properties here are the ones whose loss would
put the sweep back on that pool or make it mis-read a verdict: the page size that
was measured to stay under GraphQL's timeout, the readiness verdict coming from
the commit STATUS list and never the rollup, cursor paging of a rollup past 100
contexts (never a REST fallback), the failure-class spelling staying identical to
the workflow's, a failed page being retried at a smaller size from the same
cursor before the walk ends loudly with exit 0, and field errors beside usable
data marking what they nulled instead of discarding the page.

`gh` is replaced by monkeypatching `subprocess.run` inside the module, keyed on
which query and which variables the scanner sent.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "readiness_sweep_scan.py"
WORKFLOW = ROOT / ".github" / "workflows" / "pr-readiness-sweep.yml"


def _load():
    spec = importlib.util.spec_from_file_location("readiness_sweep_scan", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = _load()


def _check(conclusion: str, at: str, status: str = "COMPLETED") -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "status": status,
        "conclusion": conclusion,
        "completedAt": at,
    }


def _pr(
    number: int,
    *,
    sha: str = "a" * 40,
    updated: str = "2026-09-15T05:00:00Z",
    statuses: list[dict[str, str]] | None = None,
    checks: list[dict[str, Any]] | None = None,
    total: int | None = None,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, Any]:
    nodes = checks or []
    return {
        "number": number,
        "updatedAt": updated,
        "headRefOid": sha,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "status": {"contexts": statuses or []} if statuses is not None else None,
                        "statusCheckRollup": {
                            "contexts": {
                                "totalCount": len(nodes) if total is None else total,
                                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                                "nodes": nodes,
                            }
                        },
                    }
                }
            ]
        },
    }


def _page(prs: list[dict[str, Any]], *, has_next: bool = False, cursor: str | None = None):
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": prs,
                }
            }
        }
    }


def _contexts_page(checks: list[dict[str, Any]], *, has_next: bool = False, cursor=None):
    return {
        "data": {
            "repository": {
                "object": {
                    "statusCheckRollup": {
                        "contexts": {
                            "totalCount": 0,
                            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                            "nodes": checks,
                        }
                    }
                }
            }
        }
    }


class FakeGh:
    """Serve canned GraphQL documents and record every call the scanner made."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, str]] = []

    def __call__(self, argv, **_kwargs):
        assert argv[:3] == ["gh", "api", "graphql"]
        fields: dict[str, str] = {}
        for i, token in enumerate(argv):
            if token in ("-f", "-F"):
                key, _, value = argv[i + 1].partition("=")
                fields[key] = value
                # -F applies magic typing: right for the Int! page size, and
                # wrong for a cursor that happens to be all digits.
                assert (token == "-F") == (key == "first"), argv[i + 1]
        self.calls.append(fields)
        if not self.responses:
            raise AssertionError("scanner asked for more pages than the test served")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            return subprocess.CompletedProcess(argv, 1, "", str(response))
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan.time, "sleep", lambda _s: None)


def _run(monkeypatch, fake: FakeGh, capsys) -> tuple[list[dict[str, Any]], str, int]:
    monkeypatch.setattr(scan.subprocess, "run", fake)
    rc = scan.main(["--repo", "acme/widgets", "--status-context", "PR Readiness"])
    out, err = capsys.readouterr()
    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    return records, err, rc


def test_the_page_size_is_the_measured_one_and_orders_by_updated_at() -> None:
    # 50 and 100 answered HTTP 504 against the real repository; 25 did not.
    assert scan.PR_PAGE_SIZE == 25
    assert "$first: Int!" in scan.PR_PAGE_QUERY
    assert re.search(r"pullRequests\(states: OPEN, first: \$first,", scan.PR_PAGE_QUERY)
    assert "orderBy: {field: UPDATED_AT, direction: DESC}" in scan.PR_PAGE_QUERY
    assert scan.CONTEXTS_PAGE_SIZE == 100
    assert "contexts(first: 100)" in scan.PR_PAGE_QUERY
    assert "contexts(first: 100, after: $after)" in scan.CONTEXTS_PAGE_QUERY


def test_readiness_comes_from_the_commit_status_list_not_the_rollup(
    monkeypatch, capsys, fast
) -> None:
    # A check-run NAMED like the aggregate must not be able to decide the sweep,
    # so the rollup carries one and the status list carries the real thing.
    pr = _pr(
        7,
        statuses=[
            {"context": "AWS CodeBuild", "state": "SUCCESS", "createdAt": "2026-09-15T04:00:00Z"},
            {"context": "PR Readiness", "state": "PENDING", "createdAt": "2026-09-15T04:30:00Z"},
        ],
        checks=[
            {
                "__typename": "CheckRun",
                "name": "PR Readiness",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "completedAt": "2026-09-15T04:45:00Z",
            }
        ],
    )
    records, _, rc = _run(monkeypatch, FakeGh([_page([pr])]), capsys)
    assert rc == 0
    assert records[0]["readiness"] == {"state": "pending", "updated_at": "2026-09-15T04:30:00Z"}


def test_a_commit_with_no_statuses_reports_a_null_verdict(monkeypatch, capsys, fast) -> None:
    pr = _pr(8, statuses=None)
    records, _, _ = _run(monkeypatch, FakeGh([_page([pr])]), capsys)
    assert records[0]["readiness"] is None


def test_newest_completed_and_newest_failed_are_separate_and_ignore_greens(
    monkeypatch, capsys, fast
) -> None:
    pr = _pr(
        9,
        statuses=[],
        checks=[
            _check("SUCCESS", "2026-09-15T05:10:00Z"),
            _check("FAILURE", "2026-09-15T05:05:00Z"),
            _check("NEUTRAL", "2026-09-15T05:20:00Z"),
            _check("SKIPPED", "2026-09-15T05:21:00Z"),
            _check("FAILURE", "2026-09-15T05:30:00Z", status="IN_PROGRESS"),
        ],
    )
    records, _, _ = _run(monkeypatch, FakeGh([_page([pr])]), capsys)
    record = records[0]
    assert record["newest_completed_check_at"] == "2026-09-15T05:21:00Z"
    assert record["newest_failed_check_at"] == "2026-09-15T05:05:00Z"
    assert record["checks_complete"] is True


def test_the_failure_class_matches_the_workflows_spelling_exactly() -> None:
    # The workflow's REST reader listed the failure class in lower case; the
    # scanner spells the GraphQL enums in upper case. Compare the sets so neither
    # can gain or lose a member without the other noticing.
    text = WORKFLOW.read_text(encoding="utf-8")
    listed = set(
        re.findall(r"\b(failure|timed_out|cancelled|action_required|stale|startup_failure)\b", text)
    )
    assert listed == {c.lower() for c in scan.FAILURE_CONCLUSIONS}


def test_a_rollup_past_one_page_is_followed_by_cursor_never_rest(monkeypatch, capsys, fast) -> None:
    pr = _pr(
        10,
        sha="b" * 40,
        statuses=[],
        checks=[_check("SUCCESS", "2026-09-15T05:00:00Z")],
        total=150,
        has_next=True,
        cursor="Y3Vyc29y",
    )
    second = _contexts_page([_check("FAILURE", "2026-09-15T05:59:00Z")])
    fake = FakeGh([_page([pr]), second])
    records, err, rc = _run(monkeypatch, fake, capsys)
    assert rc == 0
    assert len(fake.calls) == 2
    follow_up = fake.calls[1]
    assert "object(oid: $oid)" in follow_up["query"]
    assert follow_up["oid"] == "b" * 40
    assert follow_up["after"] == "Y3Vyc29y"
    assert records[0]["newest_failed_check_at"] == "2026-09-15T05:59:00Z"
    assert records[0]["checks_complete"] is True
    assert "1 PRs with >100 contexts" in err
    # Nothing in the module can reach REST at all: the only `gh api` argv it
    # builds is the graphql one (the docstring may NAME the REST paths it
    # replaced, so the check is on the argv, not on prose).
    source = SCRIPT.read_text(encoding="utf-8")
    assert re.findall(r'"api",\s*"([a-z]+)', source) == ["graphql"]


def test_a_failed_rollup_page_yields_partial_evidence_not_a_dead_pr(
    monkeypatch, capsys, fast
) -> None:
    pr = _pr(
        11,
        statuses=[],
        checks=[_check("SUCCESS", "2026-09-15T05:00:00Z")],
        total=150,
        has_next=True,
        cursor="c",
    )
    boom = RuntimeError("HTTP 504")
    fake = FakeGh([_page([pr]), boom, boom, boom])
    records, err, rc = _run(monkeypatch, fake, capsys)
    assert rc == 0
    assert records[0]["checks_complete"] is False
    assert records[0]["newest_completed_check_at"] == "2026-09-15T05:00:00Z"
    assert "::warning::" in err and "PR #11" in err
    # Three attempts on the contexts page, plus the one PR page.
    assert len(fake.calls) == 4


def test_a_follow_up_page_that_resolves_to_nothing_is_partial_not_complete(
    monkeypatch, capsys, fast
) -> None:
    # The head moved between the two queries, so the commit the follow-up
    # addresses is gone: GraphQL answers 200 with `object: null` and no
    # `errors`. The pages past the first were never read, and the record must
    # say so rather than claim full evidence over a stale-green verdict.
    pr = _pr(
        12,
        statuses=[],
        checks=[_check("SUCCESS", "2026-09-15T05:00:00Z")],
        total=150,
        has_next=True,
        cursor="c",
    )
    vanished = {"data": {"repository": {"object": None}}}
    records, _, rc = _run(monkeypatch, FakeGh([_page([pr]), vanished]), capsys)
    assert rc == 0
    assert records[0]["checks_complete"] is False
    assert records[0]["newest_completed_check_at"] == "2026-09-15T05:00:00Z"


def test_a_pr_page_failing_down_to_one_ends_the_walk_loudly_with_exit_zero(
    monkeypatch, capsys, fast
) -> None:
    # Three attempts at 25 rule out a transient; then 12, 6, 3 and 1 are each
    # probed once at the SAME cursor, and only the failure at 1 ends the walk.
    first = _page([_pr(1, statuses=[])], has_next=True, cursor="p2")
    boom = RuntimeError("gh: HTTP 504")
    fake = FakeGh([first] + [boom] * 7)
    sleeps: list[float] = []
    monkeypatch.setattr(scan.time, "sleep", sleeps.append)
    records, err, rc = _run(monkeypatch, fake, capsys)
    assert rc == 0
    assert [r["number"] for r in records] == [1]
    assert "::warning::" in err and "the walk ends here" in err
    assert "a page of 1 pull request failed" in err
    assert "after 1 pull requests were emitted" in err
    assert "HTTP 504" in err
    assert len(fake.calls) == 8
    assert all(call["cursor"] == "p2" for call in fake.calls[1:])
    assert [call["first"] for call in fake.calls] == ["25"] + ["25"] * 3 + ["12", "6", "3", "1"]
    # Backoff only between the three attempts at 25; the probes do not wait.
    assert sleeps == [2.0, 4.0]
    assert "4 page-size reductions (ending at 1)" in err


def test_a_failed_page_is_retried_smaller_from_the_same_cursor_and_the_walk_goes_on(
    monkeypatch, capsys, fast
) -> None:
    # Page cost is content-dependent, so the page that fails at 25 can fail at
    # 25 on every sweep. Halving from the same cursor reaches the pull requests
    # behind it, and the walk then continues from the smaller page's cursor at
    # the smaller size (the size is not grown back within a sweep).
    boom = RuntimeError("gh: HTTP 504")
    fake = FakeGh(
        [
            _page([_pr(1, statuses=[])], has_next=True, cursor="p2"),
            boom,
            boom,
            boom,
            _page([_pr(2, statuses=[])], has_next=True, cursor="p3"),
            _page([_pr(3, statuses=[])]),
        ]
    )
    records, err, rc = _run(monkeypatch, fake, capsys)
    assert rc == 0
    assert [r["number"] for r in records] == [1, 2, 3]
    assert [call.get("cursor") for call in fake.calls] == [None, "p2", "p2", "p2", "p2", "p3"]
    assert [call["first"] for call in fake.calls] == ["25", "25", "25", "25", "12", "12"]
    assert "a page of 25 pull requests failed" in err and "retrying the same cursor at 12" in err
    assert "the walk ends here" not in err
    assert "1 page-size reductions (ending at 12)" in err


def test_field_errors_beside_usable_data_keep_the_page_and_mark_what_they_nulled(
    monkeypatch, capsys, fast
) -> None:
    # GitHub answers 200 with `data` AND `errors` when one field timed out: the
    # field is null, the rest of the page is intact. Node 0 lost its readiness
    # status, node 1 its rollup, node 2 nothing. The page must not be discarded
    # (that would drop every older pull request), node 0 must not be emitted
    # as "no verdict published", and node 1 must not read as "no checks".
    lost_status = _pr(20, statuses=[])
    lost_status["commits"]["nodes"][0]["commit"]["status"] = None
    lost_rollup = _pr(21, statuses=[])
    lost_rollup["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
    intact = _pr(22, statuses=[], checks=[_check("SUCCESS", "2026-09-15T05:00:00Z")])
    page = _page([lost_status, lost_rollup, intact], has_next=True, cursor="p2")
    page["errors"] = [
        {
            "message": "Something went wrong while executing your query.",
            "path": [
                "repository",
                "pullRequests",
                "nodes",
                0,
                "commits",
                "nodes",
                0,
                "commit",
                "status",
            ],
        },
        {
            "message": "timedout",
            "path": [
                "repository",
                "pullRequests",
                "nodes",
                1,
                "commits",
                "nodes",
                0,
                "commit",
                "statusCheckRollup",
                "contexts",
            ],
        },
    ]
    fake = FakeGh([page, _page([_pr(23, statuses=[])])])
    records, err, rc = _run(monkeypatch, fake, capsys)
    assert rc == 0
    assert [r["number"] for r in records] == [21, 22, 23]
    by_number = {r["number"]: r for r in records}
    assert by_number[21]["checks_complete"] is False
    assert by_number[22]["checks_complete"] is True
    assert by_number[23]["checks_complete"] is True
    assert len(fake.calls) == 2 and fake.calls[1]["cursor"] == "p2"
    assert "::warning::" in err and "field errors" in err and "#20" in err
    assert "1 PRs left out on field errors" in err


def test_an_error_with_no_path_touches_every_pull_request_on_the_page(
    monkeypatch, capsys, fast
) -> None:
    # An error that cannot be placed on a node may have nulled any field, so
    # every node's readiness is untrusted and the page emits nothing -- but its
    # cursor is still followed, so the older pages are read.
    page = _page([_pr(30, statuses=[]), _pr(31, statuses=[])], has_next=True, cursor="p2")
    page["errors"] = [{"message": "timedout"}]
    records, err, rc = _run(monkeypatch, FakeGh([page, _page([_pr(32, statuses=[])])]), capsys)
    assert rc == 0
    assert [r["number"] for r in records] == [32]
    assert "#30, #31" in err


def test_field_errors_on_a_contexts_follow_up_page_mark_the_evidence_partial(
    monkeypatch, capsys, fast
) -> None:
    pr = _pr(
        13,
        statuses=[],
        checks=[_check("SUCCESS", "2026-09-15T05:00:00Z")],
        total=150,
        has_next=True,
        cursor="c",
    )
    partial = _contexts_page([_check("FAILURE", "2026-09-15T05:59:00Z")], has_next=True, cursor="d")
    partial["errors"] = [{"message": "timedout", "path": ["repository", "object"]}]
    records, err, rc = _run(monkeypatch, FakeGh([_page([pr]), partial]), capsys)
    assert rc == 0
    assert records[0]["checks_complete"] is False
    # The nodes that did arrive still count.
    assert records[0]["newest_failed_check_at"] == "2026-09-15T05:59:00Z"
    assert "PR #13" in err and "field errors" in err


def test_graphql_errors_in_a_200_body_count_as_a_failed_attempt(monkeypatch, capsys, fast) -> None:
    # No usable `data` at all: a failed attempt, retried at the same size.
    errored = {"errors": [{"message": "Something went wrong while executing your query."}]}
    ok = _page([_pr(2, statuses=[])])
    fake = FakeGh([errored, ok])
    records, err, rc = _run(monkeypatch, fake, capsys)
    assert rc == 0
    assert [r["number"] for r in records] == [2]
    assert len(fake.calls) == 2
    assert [call["first"] for call in fake.calls] == ["25", "25"]


def test_a_null_connection_beside_errors_is_a_failed_attempt_not_an_empty_page(
    monkeypatch, capsys, fast
) -> None:
    nulled = {"data": {"repository": {"pullRequests": None}}, "errors": [{"message": "timedout"}]}
    ok = _page([_pr(3, statuses=[])])
    fake = FakeGh([nulled, ok])
    records, _, rc = _run(monkeypatch, fake, capsys)
    assert rc == 0
    assert [r["number"] for r in records] == [3]
    assert len(fake.calls) == 2


def test_every_page_is_walked_and_the_first_page_sends_no_cursor(monkeypatch, capsys, fast) -> None:
    fake = FakeGh(
        [
            _page([_pr(1, statuses=[])], has_next=True, cursor="c1"),
            _page([_pr(2, statuses=[])], has_next=True, cursor="c2"),
            _page([_pr(3, statuses=[])]),
        ]
    )
    records, err, rc = _run(monkeypatch, fake, capsys)
    assert [r["number"] for r in records] == [1, 2, 3]
    assert "cursor" not in fake.calls[0]
    assert fake.calls[1]["cursor"] == "c1" and fake.calls[2]["cursor"] == "c2"
    assert "3 pull requests, 3 GraphQL pages" in err


def test_the_workflow_runs_this_script_from_a_sparse_checkout() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["sweep"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["sparse-checkout"] == ".github/scripts/readiness_sweep_scan.py"
    assert checkout["with"]["persist-credentials"] is False
    run = "\n".join(str(s.get("run", "")) for s in steps)
    # Comments may name the old shape; the assertions are on code lines only.
    code = "\n".join(line for line in run.splitlines() if not line.lstrip().startswith("#"))
    assert "python3 .github/scripts/readiness_sweep_scan.py" in code
    # The per-PR REST reads are gone; only the comment read (mode 5) and the
    # dispatch remain as `gh` calls.
    assert "commits/$sha/statuses" not in code
    assert "commits/$sha/check-runs" not in code
    assert "gh pr list" not in code
    assert "issues/$pr_number/comments" in code
    assert "gh workflow run pr-readiness.yml" in code

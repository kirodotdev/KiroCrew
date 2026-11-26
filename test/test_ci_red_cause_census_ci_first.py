"""The red-cause census reads the `CI` workflow before every other one.

Two of the census's three goal numbers are counted from `CI` rows alone. When
the budget ends a census early, those rows are whole only if `CI` was read
first. The order is free: the same batches of check suites are asked for, so a
census costs the same calls and points whichever workflow GitHub lists first.
Every case feeds canned payloads through the stand-in for `gh`.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

import pytest
from test_ci_red_cause_census import (
    CENSUS,
    DAY,
    FakeGh,
    api_for,
    census_args,
    check_run,
    connection,
    failing,
    read_csv,
    run_node,
    suites_of,
    ticks,
    workflows_page,
)

OTHER = "PR Readiness"
CI = CENSUS.CI_WORKFLOW


def run_id_of(workflow: str, at: int) -> int:
    return (1000 if workflow == CI else 5000) + at


def red_runs(workflow: str, count: int, red: int = 1) -> list[dict[str, Any]]:
    return [
        run_node(
            run_id_of(workflow, at),
            f"{DAY}T10:{at % 60:02d}:00Z",
            red=red,
            sha=f"{run_id_of(workflow, at):040x}",
        )
        for at in range(count)
    ]


def two_workflows(
    first: str, second: str, ci_runs: int, other_runs: int, ci_red: int = 1
) -> FakeGh:
    """A repository whose workflow list names ``first`` before ``second``."""
    runs = {f"W_{CI}": red_runs(CI, ci_runs, ci_red), f"W_{OTHER}": red_runs(OTHER, other_runs)}
    suites = {
        node["checkSuite"]["id"]: (
            connection([check_run(node["databaseId"], notes=[failing()])]),
            connection([]),
        )
        for nodes in runs.values()
        for node in nodes
    }
    return FakeGh(
        workflows=workflows_page(first, second),
        runs=lambda fields: {"node": {"runs": connection(runs[fields["workflow"]])}},
        suites=suites_of(suites),
    )


def summary_of(out_dir: Path) -> dict[str, Any]:
    return json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))


def workflows_csv(out_dir: Path) -> dict[str, str]:
    with open(out_dir / "workflows.csv", encoding="utf-8", newline="") as handle:
        return {row["workflow"]: row["read_in_full"] for row in csv.DictReader(handle)}


def test_a_census_the_budget_cuts_short_has_every_ci_row_whichever_workflow_is_listed_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two points a call: the bucket, the issues, two listings and ONE batch of suites.
    gh = two_workflows(OTHER, CI, ci_runs=10, other_runs=10)
    args = census_args(tmp_path, "--max-points", "10")

    status = CENSUS.main(args, api=api_for(gh), clock=ticks(0.0, 1.0))

    assert status == CENSUS.PARTIAL_EXIT
    rows = read_csv(tmp_path)
    assert sorted(int(row["run_id"]) for row in rows) == [run_id_of(CI, at) for at in range(10)]
    assert {row["workflow"] for row in rows} == {CI}
    assert "GraphQL calls: 5 (10 points)\n" in capsys.readouterr().out
    summary = summary_of(tmp_path)
    assert summary["partial"] == "10 GraphQL points spent, the --max-points of 10"
    assert (summary["ci_read_in_full"], summary["workflows_read_in_full"]) == (True, 1)
    assert summary["suites_unread"] == 10
    assert workflows_csv(tmp_path) == {CI: "yes", OTHER: "no"}
    markdown = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "**Partial: stopped early" in markdown
    assert f"`{CI}` was read in full. Workflows read in full: 1 of 2" in markdown


def test_ci_is_listed_before_the_workflows_github_names_ahead_of_it() -> None:
    gh = two_workflows(OTHER, CI, ci_runs=1, other_runs=1)

    CENSUS.take_census(
        api_for(gh), "kirodotdev/KiroCrew", f"{DAY}T00:00:00Z", "2026-11-21T00:00:00Z", "never"
    )

    listed = [fields["workflow"] for kind, fields in gh.calls if kind == "runs"]
    assert listed == [f"W_{CI}", f"W_{OTHER}"]


def test_a_ci_run_with_many_red_jobs_is_read_before_another_workflows_small_one() -> None:
    # 30 red jobs put the `CI` suite in the last tier, which is asked for after the small ones.
    gh = two_workflows(OTHER, CI, ci_runs=1, other_runs=1, ci_red=30)
    api = api_for(gh)
    runs = [
        run
        for workflow in (OTHER, CI)
        for run in CENSUS.read_runs(
            api, f"W_{workflow}", workflow, f"{DAY}T00:00:00Z", "2026-11-21T00:00:00Z"
        )
    ]

    CENSUS.read_jobs(api, runs)

    asked = [fields["ids"] for kind, fields in gh.calls if kind == "suites"]
    assert asked == [[f"CS_{run_id_of(CI, 0)}"], [f"CS_{run_id_of(OTHER, 0)}"]]


@pytest.mark.parametrize(
    ("ci_runs", "other_runs"), [(0, 7), (7, 0), (10, 10), (11, 9), (3, 28), (25, 25), (1, 99)]
)
@pytest.mark.parametrize("first", [CI, OTHER])
def test_the_order_asks_for_no_more_batches_than_the_suites_need(
    first: str, ci_runs: int, other_runs: int, tmp_path: Path
) -> None:
    second = OTHER if first == CI else CI
    gh = two_workflows(first, second, ci_runs, other_runs)

    assert CENSUS.main(census_args(tmp_path), api=api_for(gh), clock=ticks(0.0, 1.0)) == 0

    batches = [len(fields["ids"]) for kind, fields in gh.calls if kind == "suites"]
    assert len(batches) == math.ceil((ci_runs + other_runs) / CENSUS.SUITE_BATCH)
    assert sum(batches) == ci_runs + other_runs
    ids = [suite for kind, fields in gh.calls if kind == "suites" for suite in fields["ids"]]
    ci_ids = [f"CS_{run_id_of(CI, at)}" for at in range(ci_runs)]
    assert ids[:ci_runs] == ci_ids
    assert len(read_csv(tmp_path)) == ci_runs + other_runs


def test_a_whole_census_says_every_workflow_was_read_in_full(tmp_path: Path) -> None:
    gh = two_workflows(OTHER, CI, ci_runs=2, other_runs=2)

    assert CENSUS.main(census_args(tmp_path), api=api_for(gh), clock=ticks(0.0, 1.0)) == 0

    summary = summary_of(tmp_path)
    assert summary["partial"] is None
    assert (summary["ci_read_in_full"], summary["workflows_read_in_full"]) == (True, 2)
    assert workflows_csv(tmp_path) == {CI: "yes", OTHER: "yes"}
    assert "read in full" not in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_a_stop_while_the_runs_are_listed_does_not_call_ci_read_in_full(tmp_path: Path) -> None:
    # The bucket, the issues and the `CI` listing spend the six: no suite is asked for.
    gh = two_workflows(OTHER, CI, ci_runs=3, other_runs=3)
    args = census_args(tmp_path, "--max-points", "6")

    assert CENSUS.main(args, api=api_for(gh), clock=ticks(0.0, 1.0)) == CENSUS.PARTIAL_EXIT

    assert gh.kinds() == ["budget", "issues", "workflows", "runs"]
    summary = summary_of(tmp_path)
    assert (summary["ci_read_in_full"], summary["workflows_read_in_full"]) == (False, 0)
    assert workflows_csv(tmp_path) == {CI: "no", OTHER: "no"}
    assert f"`{CI}` was not read in full. Workflows read in full: 0 of 2" in (
        tmp_path / "summary.md"
    ).read_text(encoding="utf-8")
    assert read_csv(tmp_path) == []

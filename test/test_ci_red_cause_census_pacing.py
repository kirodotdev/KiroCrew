"""The red-cause census paced inside the GraphQL budget.

A census run from a workflow sleeps until the bucket's reset instead of
stopping, takes its window as a count of days, and gives one `gh` call as long
as the `gh` wrapper may wait inside it. The canned API and its helpers are the
ones `test_ci_red_cause_census.py` builds; the clock and the sleep are
injected, so nothing here waits.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from test_ci_red_cause_census import (
    CENSUS,
    DAY,
    REPO,
    FakeGh,
    bucket,
    census_args,
    connection,
    read_csv,
    run_node,
    runs_pages,
    ticks,
    two_pages_then_a_red_run,
    workflows_page,
)

# -- a paced census waits for the bucket's reset instead of stopping ----------

TWENTY_TO_ELEVEN = dt.datetime(2026, 11, 20, 10, 40, tzinfo=dt.timezone.utc)


def paced_api(gh: FakeGh, slept: list[float]) -> Any:
    return CENSUS.Api(run=gh, sleep=slept.append, now=lambda: TWENTY_TO_ELEVEN)


def test_a_paced_census_waits_out_the_reserve_and_reads_the_whole_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = two_pages_then_a_red_run(bucket(1990))
    slept: list[float] = []
    args = census_args(tmp_path, "--max-wait-seconds", "3600")

    status = CENSUS.main(args, api=paced_api(gh, slept), clock=ticks(0.0, 1.0))

    # The bucket resets at 11:00:00Z, twenty minutes away, and the margin is added.
    assert slept == [20 * 60 + CENSUS.RESET_MARGIN_SECONDS]
    assert status == 0
    assert gh.kinds() == ["budget", "issues", "workflows", "runs", "runs", "suites"]
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["partial"] is None
    assert [row["job_id"] for row in read_csv(tmp_path)] == ["41"]
    assert "Waited: 1205s in 1 wait(s) for the bucket's reset\n" in capsys.readouterr().out


def test_a_paced_census_spends_no_more_than_its_hourly_points_between_waits(
    tmp_path: Path,
) -> None:
    gh = two_pages_then_a_red_run({})
    slept: list[float] = []
    args = census_args(tmp_path, "--points-per-hour", "6", "--max-wait-seconds", "3600")

    status = CENSUS.main(args, api=paced_api(gh, slept), clock=ticks(0.0, 1.0))

    # Two points a call: the bucket read, the issues and one page of runs spend
    # the six, and the wait comes before the fourth call. No reply here carries
    # a reset time, so the wait is the fixed one.
    assert slept == [CENSUS.UNKNOWN_RESET_WAIT_SECONDS]
    assert status == 0
    assert gh.kinds() == ["budget", "issues", "workflows", "runs", "runs", "suites"]


def test_a_reset_past_the_wait_budget_stops_the_census_without_sleeping(tmp_path: Path) -> None:
    gh = two_pages_then_a_red_run(bucket(1990))
    slept: list[float] = []
    args = census_args(tmp_path, "--max-wait-seconds", "600")

    status = CENSUS.main(args, api=paced_api(gh, slept), clock=ticks(0.0, 1.0))

    assert slept == []
    assert status == CENSUS.PARTIAL_EXIT
    assert gh.kinds() == ["budget", "issues", "workflows", "runs"]
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["partial"] == (
        "1990 GraphQL points left, under the --reserve-points of 2000 "
        "(the bucket resets at 2026-11-20T11:00:00Z); the reset is 1205s away, "
        "past the 600s left of --max-wait-seconds"
    )


def test_every_wait_is_charged_to_one_budget(tmp_path: Path) -> None:
    gh = two_pages_then_a_red_run({})
    slept: list[float] = []
    args = census_args(tmp_path, "--points-per-hour", "2", "--max-wait-seconds", "100")

    status = CENSUS.main(args, api=paced_api(gh, slept), clock=ticks(0.0, 1.0))

    # The first wait takes 60 of the 100 seconds; the second does not fit.
    assert slept == [60]
    assert status == CENSUS.PARTIAL_EXIT
    # No page of runs is asked for: the workflow list is REST, outside the budget.
    assert gh.kinds() == ["budget", "issues", "workflows"]
    partial = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["partial"]
    assert partial.endswith("the reset is 60s away, past the 40s left of --max-wait-seconds")


def test_max_points_ends_a_paced_census_too(tmp_path: Path) -> None:
    gh = two_pages_then_a_red_run({})
    slept: list[float] = []
    args = census_args(tmp_path, "--max-points", "6", "--max-wait-seconds", "3600")

    status = CENSUS.main(args, api=paced_api(gh, slept), clock=ticks(0.0, 1.0))

    assert (status, slept) == (CENSUS.PARTIAL_EXIT, [])
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["partial"] == (
        "6 GraphQL points spent, the --max-points of 6"
    )


def test_a_census_does_not_wait_unless_asked() -> None:
    args = CENSUS.build_parser().parse_args(
        ["--since", DAY, "--until", DAY, "--repo", REPO, "--out-dir", "out"]
    )
    assert (args.points_per_hour, args.max_wait_seconds) == (None, 0)
    api = CENSUS.Api(run=FakeGh())
    assert (api.hourly_points, api.wait_left) == (None, 0.0)


@pytest.mark.parametrize(
    "flags",
    [["--points-per-hour", "0"], ["--points-per-hour", "many"], ["--max-wait-seconds", "-1"]],
)
def test_a_pacing_flag_that_is_not_a_count_is_refused(flags: list[str]) -> None:
    window = ["--since", DAY, "--until", DAY, "--repo", REPO, "--out-dir", "out"]
    with pytest.raises(SystemExit):
        CENSUS.build_parser().parse_args([*window, *flags])


# -- the window as a count of days --------------------------------------------


def test_last_days_is_that_many_whole_utc_days_ending_yesterday(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh(
        workflows=workflows_page("CI"),
        runs=runs_pages(
            {
                None: connection(
                    [
                        run_node(4, "2026-11-20T00:00:00Z"),
                        run_node(3, "2026-11-19T23:59:59Z"),
                        run_node(2, "2026-11-13T00:00:00Z"),
                        run_node(1, "2026-11-12T23:59:59Z"),
                    ]
                )
            }
        ),
    )
    args = ["--last-days", "7", "--repo", REPO, "--out-dir", str(tmp_path)]

    status = CENSUS.main(args, api=paced_api(gh, []), clock=ticks(0.0, 1.0))

    # The clock reads the 20th: the window is the 13th to the 19th, both whole.
    assert status == 0
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert (summary["days"], summary["runs"]) == (7, 2)
    assert (summary["runs_read_from"], summary["runs_read_to"]) == (
        "2026-11-13T00:00:00Z",
        "2026-11-19T23:59:59Z",
    )
    assert "2026-11-13 to 2026-11-19 UTC (7 day(s))" in capsys.readouterr().out


@pytest.mark.parametrize(
    "window",
    [
        [],
        ["--since", DAY],
        ["--until", DAY],
        ["--last-days", "7", "--since", DAY, "--until", DAY],
        ["--last-days", "0"],
    ],
)
def test_the_window_is_two_bounds_or_a_count_of_days(window: list[str]) -> None:
    gh = FakeGh()
    with pytest.raises(SystemExit):
        CENSUS.main([*window, "--repo", REPO, "--out-dir", "out"], api=paced_api(gh, []))
    assert gh.calls == []


# -- a reply from the bucket that was slept out, and how long one call may take


def test_a_reply_from_the_bucket_that_was_slept_out_starts_no_second_wait() -> None:
    replies = iter(
        [
            bucket(1990),  # 11:00's bucket, under the reserve
            bucket(100, cost=3),  # sent before 11:00, read after the sleep
            {"rateLimit": {"cost": 5, "remaining": 14000, "resetAt": "2026-11-20T12:00:00Z"}},
            {"rateLimit": {"cost": 7, "remaining": 13990, "resetAt": "2026-11-20T12:00:00Z"}},
        ]
    )
    gh = FakeGh(budget=lambda fields: next(replies))
    slept: list[float] = []
    api = paced_api(gh, slept)
    api.hourly_points, api.wait_left = 14, 7200.0

    for _ in range(4):
        api.graphql(CENSUS.BUDGET_QUERY, {})

    # One sleep, for the reserve: the late reply's 100 points left are not believed.
    assert slept == [1205]
    assert (api.remaining, api.reset_at) == (13990, "2026-11-20T12:00:00Z")
    # Its three points do count against the new hour: with the 5 and the 7 the
    # hour's 14 is reached, and the next call sleeps to 12:00 first.
    with pytest.raises(StopIteration):
        api.graphql(CENSUS.BUDGET_QUERY, {})
    assert slept == [1205, 80 * 60 + CENSUS.RESET_MARGIN_SECONDS]


class _Finished:
    returncode, stdout, stderr = 0, "{}", ""


def test_one_gh_call_may_take_as_long_as_it_was_told(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], float]] = []

    def fake_run(argv: list[str], **options: Any) -> _Finished:
        seen.append((argv, options["timeout"]))
        return _Finished()

    monkeypatch.setattr(CENSUS.subprocess, "run", fake_run)

    assert CENSUS.run_gh(["api", "rate_limit"]) == (0, "{}", "")
    assert CENSUS.run_gh(["api", "rate_limit"], 900) == (0, "{}", "")
    assert seen == [(["gh", "api", "rate_limit"], 300), (["gh", "api", "rate_limit"], 900)]


def test_the_census_runs_gh_with_the_timeout_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Any] = []
    real_api = CENSUS.Api

    def api_with_fake_gh(run: Any) -> Any:
        built.append(run)
        return real_api(run=FakeGh(budget=lambda fields: bucket(1)), sleep=lambda seconds: None)

    monkeypatch.setattr(CENSUS, "Api", api_with_fake_gh)
    args = census_args(tmp_path, "--gh-timeout-seconds", "900")

    assert CENSUS.main(args, clock=ticks(0.0, 1.0)) == CENSUS.PARTIAL_EXIT
    (run,) = built
    assert (run.func, run.keywords) == (CENSUS.run_gh, {"timeout": 900})
    default = CENSUS.build_parser().parse_args(census_args(tmp_path))
    assert default.gh_timeout_seconds == CENSUS.GH_TIMEOUT_SECONDS == 300

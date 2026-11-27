"""The red-cause census's budget is sized to the bucket the token has, or refused.

`scripts/ci_red_census_budget.py` reads the token's GraphQL bucket and gives the
census a reserve and a cap. For any bucket, any amount left of it, any expected
cost and any worker count:

* a budget leaves half the bucket alone and spends two fifths of it at most,
  counting the calls in flight when the census stops;
* a budget is given whenever the read fits both, and refused whenever it does
  not, so a census that costs what was expected reaches neither line;
* the census's own guard, run with that budget, starts a call only while the
  last reply said the reserve was there, and alone never takes the bucket
  below half;
* a bucket that cannot be read is a refusal, never a guess.

The bucket the first scheduled run met, 10,000 points an hour with 9,353 left,
runs a weekday. `gh` is a stand-in throughout: no socket, no process.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[1]
WEEKDAY = 3000


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUDGET = _load("ci_red_census_budget_under_test", ROOT / "scripts" / "ci_red_census_budget.py")
CENSUS = _load("ci_red_cause_census_for_budget_test", ROOT / "scripts" / "ci_red_cause_census.py")

limits = st.integers(min_value=0, max_value=60000)
workers = st.integers(min_value=1, max_value=BUDGET.MAX_WORKERS)
needs = st.integers(min_value=1, max_value=20000)


@st.composite
def buckets(draw: st.DrawFn) -> tuple[int, int]:
    limit = draw(limits)
    return limit, draw(st.integers(min_value=0, max_value=limit))


@st.composite
def roomy_buckets(draw: st.DrawFn) -> tuple[int, int, int]:
    """``(limit, remaining, need)`` of a bucket that is mostly full, for a small read:
    the cases where a budget is usually given."""
    limit = draw(st.integers(min_value=1000, max_value=60000))
    remaining = draw(st.integers(min_value=limit * 3 // 4, max_value=limit))
    return limit, remaining, draw(st.integers(min_value=1, max_value=limit // 6))


# ── The rule ─────────────────────────────────────────────────────────────────


@settings(max_examples=300, deadline=None)
@given(roomy_buckets(), workers)
def test_a_budget_leaves_half_the_bucket_and_spends_two_fifths_at_most(
    roomy: tuple[int, int, int], count: int
) -> None:
    limit, remaining, need = roomy
    budget = BUDGET.size(limit, remaining, need, count)
    assume(isinstance(budget, BUDGET.Budget))
    in_flight = BUDGET.room_for(count)

    # With every worker's call landing past the line, half is still left ...
    assert (budget.reserve - in_flight) * 2 >= limit
    # ... and no more than two fifths is spent.
    assert (budget.most + in_flight) * 5 <= limit * 2
    assert budget.reserve + budget.most <= limit


@settings(max_examples=300, deadline=None)
@given(roomy_buckets(), workers)
def test_a_budget_is_given_only_when_the_expected_read_reaches_neither_line(
    roomy: tuple[int, int, int], count: int
) -> None:
    limit, remaining, need = roomy
    budget = BUDGET.size(limit, remaining, need, count)
    assume(isinstance(budget, BUDGET.Budget))

    assert budget.most >= need, "the read fits under the cap"
    assert remaining - need >= budget.reserve, "and ends above the reserve"


@settings(max_examples=300, deadline=None)
@given(buckets(), needs, workers)
def test_a_read_that_fits_is_never_refused_and_one_that_does_not_never_runs(
    bucket: tuple[int, int], need: int, count: int
) -> None:
    limit, remaining = bucket
    in_flight = BUDGET.room_for(count)
    budget = BUDGET.size(limit, remaining, need, count)

    # Stated in fifths and halves of the bucket, with a point of rounding to
    # spare, so this does not restate the script's own arithmetic.
    cap_holds_it = (need + in_flight + 1) * 5 <= limit * 2
    left_holds_it = (remaining - need - in_flight - 1) * 2 >= limit
    cap_too_small = (need + in_flight) * 5 > limit * 2
    left_too_small = (remaining - need - in_flight) * 2 < limit
    if cap_holds_it and left_holds_it:
        assert isinstance(budget, BUDGET.Budget), budget
    if cap_too_small or left_too_small:
        assert isinstance(budget, BUDGET.Refusal), budget


@settings(max_examples=200, deadline=None)
@given(roomy_buckets(), workers, st.integers(min_value=0, max_value=60000))
def test_more_left_in_the_bucket_never_refuses_what_less_allowed(
    roomy: tuple[int, int, int], count: int, more: int
) -> None:
    limit, remaining, need = roomy
    richer = min(limit, remaining + more)
    poorer = BUDGET.size(limit, remaining, need, count)
    assume(isinstance(poorer, BUDGET.Budget))

    # The limits depend on the bucket's size, not on what is left of it.
    assert BUDGET.size(limit, richer, need, count) == poorer


@settings(max_examples=150, deadline=None)
@given(
    roomy_buckets(),
    st.lists(st.integers(min_value=1, max_value=BUDGET.MOST_POINTS_A_CALL), max_size=400),
    st.integers(min_value=0, max_value=3000),
)
def test_the_census_guard_run_with_the_budget_never_takes_the_bucket_below_half(
    roomy: tuple[int, int, int], costs: list[int], others: int
) -> None:
    """The census's own `Api` guard, given the budget, against a bucket other jobs
    may also spend from between its calls."""
    limit, remaining, need = roomy
    budget = BUDGET.size(limit, remaining, need, 1)
    assume(isinstance(budget, BUDGET.Budget))
    left = remaining
    told: list[int] = []  # what each reply said was left
    queue = list(costs)

    def gh(argv: list[str]) -> tuple[int, str, str]:
        nonlocal left
        cost = queue.pop(0)
        left = max(left - cost, 0)
        told.append(left)
        rate = {"cost": cost, "remaining": left, "resetAt": "2026-11-21T08:00:00Z"}
        # Another workflow spends from the same bucket after this call.
        left = max(left - others, 0)
        return 0, json.dumps({"data": {"rateLimit": rate}}), ""

    api = CENSUS.Api(
        run=gh, sleep=lambda seconds: None, reserve=budget.reserve, max_points=budget.most
    )
    for _cost in costs:
        try:
            api.graphql(CENSUS.BUDGET_QUERY, {})
        except CENSUS.BudgetStop:
            break

    # A call is started only while the last reply said the reserve was still there,
    # so no reply but the last can report less.
    assert all(said >= budget.reserve for said in told[:-1])
    assert sum(costs[: len(told)]) <= budget.most + BUDGET.MOST_POINTS_A_CALL
    if others == 0:
        # With the bucket to itself, the census leaves half of it at every moment.
        assert all(said * 2 >= limit for said in told)


# ── The buckets that are known ───────────────────────────────────────────────


def test_the_bucket_the_first_scheduled_run_met_runs_a_weekday() -> None:
    assert BUDGET.size(10000, 9353, WEEKDAY, 4) == BUDGET.Budget(reserve=5120, most=3880)


@pytest.mark.parametrize(
    ("limit", "remaining", "runs"),
    [
        (15000, 15000, True),
        (10000, 8120, True),  # exactly the reserve and a weekday
        (10000, 8119, False),
        (7800, 7800, True),  # the smallest bucket that holds a weekday
        (7799, 7799, False),
        (5000, 5000, False),
        (1000, 1000, False),
        (0, 0, False),
    ],
)
def test_which_buckets_hold_a_weekday(limit: int, remaining: int, runs: bool) -> None:
    budget = BUDGET.size(limit, remaining, WEEKDAY, 4)
    assert isinstance(budget, BUDGET.Budget) is runs


def test_a_refusal_says_which_line_the_read_would_cross() -> None:
    small = BUDGET.size(1000, 1000, WEEKDAY, 4)
    spent = BUDGET.size(10000, 6000, WEEKDAY, 4)
    assert "1000 points an hour" in small.reason and "needs 3000" in small.reason
    assert "6000 of 10000" in spent.reason and "leaves 5120" in spent.reason


# ── Reading the bucket ───────────────────────────────────────────────────────

ARGS = ["--needs-points", str(WEEKDAY), "--workers", "4"]


def gh_reporting(body: Any, code: int = 0) -> Any:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return code, body if isinstance(body, str) else json.dumps(body)

    run.seen = seen  # type: ignore[attr-defined]
    return run


def bucket_of(limit: Any, remaining: Any) -> dict[str, Any]:
    return {
        "resources": {"core": {"limit": 15000}, "graphql": {"limit": limit, "remaining": remaining}}
    }


def test_a_budget_is_printed_and_handed_to_the_next_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outputs = tmp_path / "outputs.txt"
    run = gh_reporting(bucket_of(10000, 9353))

    status = BUDGET.main(ARGS, run=run, environ={"GITHUB_OUTPUT": str(outputs)})

    assert status == 0
    # One read, and the one that costs nothing.
    assert run.seen == [["api", "rate_limit"]]
    assert outputs.read_bytes() == b"reserve=5120\nmost=3880\n"
    printed = capsys.readouterr().out
    assert "10000 points an hour, 9353 left" in printed
    assert "3880" in printed and "5120" in printed and "Refused" not in printed


def test_a_budget_outside_a_workflow_is_only_printed(capsys: pytest.CaptureFixture[str]) -> None:
    assert BUDGET.main(ARGS, run=gh_reporting(bucket_of(10000, 9353)), environ={}) == 0
    assert "Budget:" in capsys.readouterr().out


@pytest.mark.parametrize(
    "run",
    [
        gh_reporting(bucket_of(1000, 1000)),
        gh_reporting(bucket_of(10000, 6000)),
        gh_reporting(bucket_of(10000, 9353), code=1),
        gh_reporting("not JSON"),
        gh_reporting([]),
        gh_reporting({"resources": {"core": {"limit": 15000, "remaining": 1}}}),
        gh_reporting(bucket_of(None, None)),
        gh_reporting(bucket_of("10000", "9353")),
        gh_reporting(bucket_of(True, True)),
        gh_reporting(bucket_of(-1, -1)),
        gh_reporting(bucket_of(10000, 10001)),
    ],
    ids=[
        "too small",
        "spent by others",
        "gh failed",
        "not JSON",
        "not an object",
        "no graphql bucket",
        "nulls",
        "strings",
        "booleans",
        "negative",
        "more left than the bucket holds",
    ],
)
def test_a_bucket_that_does_not_hold_the_read_or_cannot_be_read_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], run: Any
) -> None:
    outputs = tmp_path / "outputs.txt"

    assert BUDGET.main(ARGS, run=run, environ={"GITHUB_OUTPUT": str(outputs)}) == 1

    assert not outputs.exists(), "a refusal hands the census nothing"
    printed = capsys.readouterr().out
    assert "Refused:" in printed and printed.rstrip().endswith("Nothing was read.")
    assert "Budget:" not in printed


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--needs-points", "3000"],
        ["--workers", "4"],
        ["--needs-points", "0", "--workers", "4"],
        ["--needs-points", "lots", "--workers", "4"],
        ["--needs-points", "3000", "--workers", "0"],
        ["--needs-points", "3000", "--workers", "9"],
    ],
)
def test_arguments_that_are_not_counts_read_nothing(flags: list[str]) -> None:
    run = gh_reporting(bucket_of(10000, 9353))
    with pytest.raises(SystemExit):
        BUDGET.main(flags, run=run, environ={})
    assert run.seen == []


def test_the_room_is_what_the_census_can_have_in_flight() -> None:
    assert BUDGET.MAX_WORKERS == CENSUS.MAX_WORKERS
    assert BUDGET.room_for(4) == 4 * BUDGET.MOST_POINTS_A_CALL

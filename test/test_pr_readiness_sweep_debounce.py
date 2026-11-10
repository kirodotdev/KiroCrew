"""The sweep's debounce and rate-limit reserve (pr-readiness-sweep.yml).

A head's lanes finish one after another. The sweep holds a "check completed
later" re-fire while another check-run on the head is still running, and the
last completion re-fires it, so the readiness verdict is recomputed on the
final state, once. Before dispatching, it keeps a reserve of the shared REST
pool.

Uses the same harness as test_pr_readiness_sweep.py: the real run block, the
real scanner, and a `gh` stub.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pr_readiness_sweep_helpers import SCANNER, WORKFLOW, Runner, _gnu_date, _script

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or not SCANNER.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or not _gnu_date(),
    reason="requires the workflow plus a POSIX bash and GNU date",
)


def _ago(minutes: float) -> str:
    # The sweep compares these stamps with its own live `date -u +%s`, so each
    # one is taken from the clock at the moment the test builds it.
    now = datetime.now(timezone.utc)  # flake-ok: the sweep under test reads the real clock
    return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def script() -> str:
    return _script()


@pytest.fixture
def runner(tmp_path: Path, script: str) -> Runner:
    return Runner(tmp_path, script)


@pytest.mark.parametrize("state", ["pending", "failure"])
def test_a_head_whose_lanes_all_finish_gets_exactly_one_final_recompute(
    runner: Runner, state: str
) -> None:
    # Tick 1: a lane finished after the verdict, another is still running.
    assert (
        runner.sweep(state=state, status_at=_ago(10), check_completed_at=_ago(6), running_checks=1)
        == []
    )
    assert "still running" in runner.last_stdout

    # Tick 2: the last lane finished. One recompute, on the final state.
    dispatched = runner.sweep(state=state, status_at=_ago(10), check_completed_at=_ago(5))
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]

    # Tick 3: that recompute republished after the last completion. Nothing more.
    assert runner.sweep(state=state, status_at=_ago(1), check_completed_at=_ago(5)) == []


def test_a_lane_running_long_past_the_last_completion_does_not_hold_the_refire(
    runner: Runner,
) -> None:
    # A check-run still "in progress" 20 minutes after the newest completion is
    # treated as hung: the verdict is recomputed anyway.
    dispatched = runner.sweep(
        state="pending", status_at=_ago(30), check_completed_at=_ago(20), running_checks=1
    )
    assert len(dispatched) == 1


def test_a_stale_green_is_rescued_while_lanes_still_run(runner: Runner) -> None:
    # A red that landed after a green verdict permits a merge, so the debounce
    # never applies to the green arm.
    dispatched = runner.sweep(
        state="success",
        status_at=_ago(10),
        check_completed_at=_ago(5),
        check_conclusion="failure",
        running_checks=2,
    )
    assert len(dispatched) == 1


@pytest.mark.parametrize(
    ("remaining", "limit", "expected"),
    [
        (None, 15000, 1),  # /rate_limit unreadable: the MAX_DISPATCH cap alone, as before
        (15000, 15000, 1),  # plenty above the reserve
        (3024, 15000, 0),  # under one recompute above the 3,000 (20%) reserve
        (0, 15000, 0),  # the pool is spent
        (1000, 1000, 1),  # a standard 1,000 pool keeps a 200 reserve and still dispatches
    ],
)
def test_dispatch_keeps_the_rate_limit_reserve(
    runner: Runner, remaining: int | None, limit: int, expected: int
) -> None:
    dispatched = runner.sweep(
        state="pending",
        status_at=_ago(10),
        check_completed_at=_ago(5),
        rate_remaining=remaining,
        rate_limit=limit,
    )
    assert len(dispatched) == expected
    if expected == 0:
        assert "1 deferred by the rate-limit reserve" in runner.last_stdout


def test_a_stale_green_rescue_is_not_deferred_by_the_reserve(runner: Runner) -> None:
    # A green that a later failure contradicts permits a merge, so its rescue
    # is dispatched even when the pool is under the reserve.
    dispatched = runner.sweep(
        state="success",
        status_at=_ago(10),
        check_completed_at=_ago(5),
        check_conclusion="failure",
        rate_remaining=100,
    )
    assert len(dispatched) == 1

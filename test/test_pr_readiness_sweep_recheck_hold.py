"""The sweep's retry of a re-check hold on a settled head (pr-readiness-sweep.yml).

PR Readiness writes `readiness check(s) changed since evaluation; re-checking
<lane>` while a monitored lane runs, and counts on that lane's completion
arriving as check evidence. When the hold runs after the lane's check-runs
already finished, every completion on the head predates the hold's floor, and
the evidence test never fires. With no check-run left running the sweep
re-fires such a hold once it is STALE_MINUTES old.

Uses the same harness as test_pr_readiness_sweep.py: the real run block, the
real scanner, and a `gh` stub.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pr_readiness_sweep_helpers import REPO_ROOT, SCANNER, WORKFLOW, Runner, _gnu_date, _script

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or not SCANNER.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or not _gnu_date(),
    reason="requires the workflow plus a POSIX bash and GNU date",
)

PREFIX = "readiness check(s) changed since evaluation;"
HOLD = PREFIX + " re-checking Sensitive Change Review"
# The hold landed 186 s after the head's newest completion: below the
# 180 s publish-lag floor, so the evidence test sees nothing newer.
HOLD_AT = "2026-10-30T18:03:34Z"
LAST_CHECK_AT = "2026-10-30T18:00:28Z"


def _ago(minutes: float) -> str:
    # The sweep compares these stamps with its own live `date -u +%s`.
    now = datetime.now(timezone.utc)  # flake-ok: the sweep under test reads the real clock
    return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def script() -> str:
    return _script()


@pytest.fixture
def runner(tmp_path: Path, script: str) -> Runner:
    return Runner(tmp_path, script)


def test_a_hold_on_a_head_with_every_check_completed_is_refired(runner: Runner) -> None:
    dispatched = runner.sweep(
        state="pending",
        status_at=HOLD_AT,
        check_completed_at=LAST_CHECK_AT,
        description=HOLD,
    )
    assert len(dispatched) == 1
    assert "pr=2064" in dispatched[0]
    assert "sha=4328fd0f941f09ff10f245fbdb4accf7c246febe" in dispatched[0]
    assert "every check-run on the head completed" in runner.last_stdout


def test_a_hold_with_a_check_still_running_waits_for_its_evidence(runner: Runner) -> None:
    dispatched = runner.sweep(
        state="pending",
        status_at=HOLD_AT,
        check_completed_at=LAST_CHECK_AT,
        description=HOLD,
        running_checks=1,
    )
    assert dispatched == []
    assert "not re-firing" in runner.last_stdout


def test_a_hold_younger_than_stale_minutes_is_left_alone(runner: Runner) -> None:
    dispatched = runner.sweep(
        state="pending",
        status_at=_ago(10),
        check_completed_at=_ago(14),
        description=HOLD,
    )
    assert dispatched == []
    # Gated before the description read, like the other no-evidence recoveries.
    assert not runner.status_read.exists()


def test_an_ordinary_lane_pending_on_a_settled_head_is_still_not_refired(
    runner: Runner,
) -> None:
    dispatched = runner.sweep(
        state="pending",
        status_at=HOLD_AT,
        check_completed_at=LAST_CHECK_AT,
        description="1 readiness check(s) still pending; waiting on Issue Gate (not started)",
    )
    assert dispatched == []


def test_the_recompute_that_replaces_the_hold_ends_the_retry(runner: Runner) -> None:
    assert (
        len(
            runner.sweep(
                state="pending",
                status_at=HOLD_AT,
                check_completed_at=LAST_CHECK_AT,
                description=HOLD,
            )
        )
        == 1
    )
    # The recompute published a real verdict; the next tick has nothing to do.
    assert (
        runner.sweep(
            state="failure",
            status_at="2026-11-16T05:50:18Z",
            check_completed_at=LAST_CHECK_AT,
            description="1 blocking readiness item(s)",
        )
        == []
    )


def test_every_hold_the_publisher_writes_carries_the_sweep_prefix() -> None:
    sweep = WORKFLOW.read_text(encoding="utf-8")
    assert 'RECHECK_HOLD_PREFIX="%s"' % PREFIX in sweep
    assert '"$RECHECK_HOLD_PREFIX"*) return 0 ;;' in sweep
    publisher = (REPO_ROOT / ".github" / "workflows" / "pr-readiness.yml").read_text(
        encoding="utf-8"
    )
    sites = re.findall(r'(?:DESCRIPTION|hold_description)="([^"\n]*re-checking[^"\n]*)"', publisher)
    assert len(sites) >= 5, sites
    for site in sites:
        assert site.startswith(PREFIX), site

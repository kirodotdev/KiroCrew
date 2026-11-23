"""The sweep's two per-pull-request REST reads, answered by the scan instead.

pr-readiness-sweep.yml classifies a pending by its status description and
looks for a disposition record newer than a terminal verdict. Each was one REST
read per pull request per sweep. The scan carries the description and the
newest issue-comment time, and the sweep skips a read only on an ANSWER from
it. These tests run the real run block and the real scanner twice over the
same fixture state -- once with a GraphQL server that answers the two fields,
once with one that does not -- and pin three things:

- the dispatch decision is the same either way, on every case;
- a disposition record created or edited after the verdict is always read and
  always re-fires, whatever else sits on the pull request (the stale-green
  rescue);
- a read is skipped only when the scan's answer proves it can find nothing,
  and anything short of an answer makes the read.

Uses the same harness as test_pr_readiness_sweep.py.
"""

from __future__ import annotations

import itertools
import os
import re
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
    or shutil.which("jq") is None
    or not _gnu_date(),
    reason="requires the workflow plus a POSIX bash, jq and GNU date",
)

# A verdict far older than the publish lag, and offsets around it in seconds.
VERDICT = datetime(2026, 8, 30, 19, 1, 24, tzinfo=timezone.utc)
CHECK_AT = "2026-08-30T18:55:00Z"


def _at(seconds: int) -> str:
    return (VERDICT + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _token(script: str, name: str) -> str:
    """A literal the run block assigns, read from the block itself."""
    found = re.search(rf'^\s*{name}="([^"]+)"\s*$', script, re.MULTILINE)
    assert found, f"{name} is not assigned in the sweep's run block"
    return found.group(1)


# ── The disposition read ─────────────────────────────────────────────────────

# Where the newest disposition record and the newest ordinary comment sit
# relative to the verdict, in seconds. None: no such comment. A record's
# `updated_at` moves on an edit exactly as on creation, so one offset stands
# for both "created after" and "created before, edited after".
RECORD_OFFSETS = (None, -600, 0, 1, 3, 1200)
OTHER_OFFSETS = (None, -600, 0, 1, 1200)
COMMENT_CASES = list(itertools.product(("failure", "success"), RECORD_OFFSETS, OTHER_OFFSETS))


def _sweep_comments(runner: Runner, state: str, record: int | None, other: int | None, **kwargs):
    dispatched = runner.sweep(
        state=state,
        status_at=_at(0),
        check_completed_at=CHECK_AT,
        disposition_at=None if record is None else _at(record),
        other_comments=(
            None if other is None else [{"body": "bot summary", "updated_at": _at(other)}]
        ),
        # Something other than a comment touched the pull request after the
        # verdict (the label readiness writes after publishing), so the
        # `updatedAt` gate is open on every case and decides nothing here.
        pr_updated_at=_at(1800),
        **kwargs,
    )
    return dispatched, runner.comments_read.exists()


@pytest.mark.parametrize(("state", "record", "other"), COMMENT_CASES)
def test_the_comment_gate_never_changes_a_decision(
    runner: Runner, state: str, record: int | None, other: int | None
) -> None:
    without, read_without = _sweep_comments(runner, state, record, other)
    with_scan, read_with = _sweep_comments(runner, state, record, other, scan_reads=True)

    # Same inputs, same dispatches, whether or not the scan answered.
    assert with_scan == without
    # Without an answer the read is made, as the `updatedAt` gate alone decides.
    assert read_without
    # With one, the read is made exactly when some comment changed after the
    # verdict -- the only time it can find a record that did.
    newest = max((o for o in (record, other) if o is not None), default=None)
    assert read_with == (newest is not None and newest > 0)
    # The rescue: a record newer than the verdict is read and re-fires, in
    # both directions, whatever else is on the pull request.
    if record is not None and record > 0:
        assert read_with
        assert len(with_scan) == 1 and "pr=2064" in with_scan[0]
    else:
        assert with_scan == []


@pytest.mark.parametrize("state", ["failure", "success"])
@pytest.mark.parametrize("answer", ["__null__", "not-a-timestamp", ""])
def test_anything_short_of_an_answer_makes_the_comment_read(
    runner: Runner, state: str, answer: str
) -> None:
    """A nulled connection, a timestamp that does not parse, an empty one.

    None of them proves the comments are older than the verdict, so the read
    is made and the record after the verdict re-fires.
    """
    dispatched, read = _sweep_comments(
        runner, state, 1200, None, scan_reads=True, scan_newest_comment_at=answer
    )
    assert read
    assert len(dispatched) == 1


@pytest.mark.parametrize("state", ["failure", "success"])
def test_a_scan_older_than_a_record_delays_the_rescue_by_one_tick_only(
    runner: Runner, state: str
) -> None:
    """The scan is read minutes before the loop, so it can predate a record.

    A record posted after the scan read the comments is not seen on this tick:
    the scan's answer is older than the verdict and the read is skipped. The
    next tick's scan carries it, and the read is made.
    """
    dispatched, read = _sweep_comments(
        runner, state, 1200, None, scan_reads=True, scan_newest_comment_at=_at(-600)
    )
    assert not read and dispatched == []
    dispatched, read = _sweep_comments(runner, state, 1200, None, scan_reads=True)
    assert read and len(dispatched) == 1


def test_both_arms_ask_the_gate_before_the_comment_read(script: str) -> None:
    """Every call of the comment read sits behind both gates, in order."""
    lines = [line.strip() for line in script.splitlines()]
    calls = [i for i, line in enumerate(lines) if 'newest_disposition_epoch "$number"' in line]
    assert len(calls) >= 2
    for index in calls:
        before = [line for line in lines[:index] if line and not line.startswith("#")]
        assert before[-1] == 'comment_evidence_possible "$updated_epoch" || continue'
        assert before[-2] == '[ "$pr_updated_epoch" -gt "$updated_epoch" ] || continue'


# ── The description read ─────────────────────────────────────────────────────


def _descriptions(script: str) -> dict[str, str]:
    read_failure = _token(script, "READ_FAILURE_TOKEN")
    unpublished = _token(script, "VERDICT_UNPUBLISHED_TOKEN")
    hold = _token(script, "RECHECK_HOLD_PREFIX")
    return {
        "read-failure": f"{read_failure} Readiness could not be evaluated",
        "hold": f"{hold} re-checking Security Scope Review",
        "unpublished": f"{unpublished} 1 readiness check(s) still pending",
        "both": f"{read_failure} {unpublished} 1 readiness check(s) still pending",
        "lane": "11 readiness check(s) still pending; waiting on CI (not started)",
        "empty": "",
    }


KINDS = ("read-failure", "hold", "unpublished", "both", "lane", "empty")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("running", [0, 1])
def test_a_pending_is_classified_the_same_from_the_scan_as_from_rest(
    runner: Runner, script: str, kind: str, running: int
) -> None:
    """A settled pending with no later evidence, 20 minutes old.

    The description is the only thing that tells the recoveries apart. Read
    from the scan it costs no REST call and gives the same outcome.
    """
    description = _descriptions(script)[kind]

    def sweep(**kwargs):
        dispatched = runner.sweep(
            state="pending",
            status_at=_ago(20),
            check_completed_at=_ago(40),
            description=description,
            running_checks=running,
            **kwargs,
        )
        return dispatched, runner.status_read.exists(), runner.last_stdout

    without, read_without, _out = sweep()
    with_scan, read_with, out = sweep(scan_reads=True)
    assert with_scan == without
    assert read_without
    assert not read_with
    # The outcome each class must have, so "equal" cannot mean "both wrong".
    if kind in ("unpublished", "both"):
        assert "withheld advisory verdict" in out
    elif kind == "read-failure" or (kind == "hold" and running == 0):
        assert any("workflow run pr-readiness.yml" in line for line in with_scan)
    else:
        assert with_scan == []


def test_the_scan_description_does_not_depend_on_the_status_endpoint(
    runner: Runner, script: str
) -> None:
    """With the scan's answer in hand the REST status endpoint is not asked.

    Shown by making that endpoint fail: the read-failure pending is still
    recognised and re-fired.
    """
    dispatched = runner.sweep(
        state="pending",
        status_at=_ago(20),
        check_completed_at=_ago(40),
        description=_descriptions(script)["read-failure"],
        status_read_fails=True,
        scan_reads=True,
    )
    assert len(dispatched) == 1
    assert not runner.status_read.exists()


def test_a_young_pending_reads_nothing_either_way(runner: Runner, script: str) -> None:
    for scan_reads in (False, True):
        dispatched = runner.sweep(
            state="pending",
            status_at=_ago(5),
            check_completed_at=_ago(40),
            description=_descriptions(script)["read-failure"],
            scan_reads=scan_reads,
        )
        assert dispatched == []
        assert not runner.status_read.exists()

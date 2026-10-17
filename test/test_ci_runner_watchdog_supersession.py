"""A superseded orphan is not protected by the fleet hold, and a re-run is never cancelled.

When a newer run has taken the orphan's place, the hold that keeps
``scripts/ci/runner_watchdog.py`` from mistaking a slow fleet for a dead one
does not protect the superseded orphan. A run's own re-run attempt is never
cancelled, whichever route reaches the cancel.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ci_runner_watchdog_helpers import (
    REPO,
    FakeApi,
    _Clock,
    _job,
    _policy,
    _posts,
    _run,
    _sweep,
    _verdict_of,
    wd,
)

# ── a superseded orphan is not protected by the fleet hold ──────────────────


def test_a_superseded_orphan_is_cancelled_even_with_no_dispatch_evidence() -> None:
    """The hold protects a result someone wants; a superseded run has none.

    This is the shape of the 6-hour `fast-gate.yml` block: the tick held on evidence,
    and the run it held kept every later commit's Fast Gate out of the group. The heal
    still declines to RE-RUN it, so the cancel frees the slot without reviving a
    verdict the branch has moved past.
    """
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=90)]},
        {1: [_job(11, minutes_ago=60)]},
        evidence=False,
        newest_by_branch={"main": 2},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/1/cancel"]
    assert not _posts(api, "/rerun")
    assert outcomes[1] not in wd.FAILED_OUTCOMES


def test_without_supersession_the_same_run_is_still_held_on_missing_evidence() -> None:
    """Negative control: the evidence hold itself is not weakened.

    Same runs, same absent evidence, only the branch listing differs. Without this
    the change could have cleared every hold and read as passing.
    """
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=90)]},
        {1: [_job(11, minutes_ago=60)]},
        evidence=False,
        newest_by_branch={"main": 1},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert api.posts == []
    assert outcomes == {}


def test_the_supersession_question_is_not_asked_when_no_hold_applies() -> None:
    """The ordinary orphan pays no extra branch listing: the heal path already asks.

    Asserted on the CALL, not on a listing count, because the heal path legitimately
    lists the branch more than once and an absolute count would pin that instead.
    """
    import unittest.mock as _mock

    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    with _mock.patch.object(wd, "supersession_clears_hold") as spy:
        _sweep(api)
    assert spy.call_count == 0


def test_the_supersession_question_is_asked_when_a_hold_applies() -> None:
    """Positive control for the test above: the spy would be vacuous without it."""
    import unittest.mock as _mock

    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=90)]},
        {1: [_job(11, minutes_ago=60)]},
        evidence=False,
    )
    with _mock.patch.object(wd, "supersession_clears_hold", return_value=False) as spy:
        _sweep(api)
    assert spy.call_count >= 1


def test_the_cleared_hold_is_logged_as_a_decision_not_as_a_cancel() -> None:
    """The line is emitted from the hold loop, before anything has decided to cancel.

    A line asserting the run WAS cancelled would be false whether or not the tick
    acts: the hold loop runs before any cancel decision, and a manual dispatch can
    still be a dry run. A reader debugging from such a log would look for a cancel
    that never happened.
    """
    verdict = wd.RunVerdict(
        run_id=1,
        run_attempt=1,
        head_branch="main",
        head_repo=REPO,
        event="push",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
    )
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, newest_by_branch={"main": 2})
    lines: list[str] = []
    assert wd.supersession_clears_hold(api, _policy(), verdict, lines.append)
    line = " ".join(lines)
    assert "is not applied" in line
    assert "it is cancelled" not in line


def test_a_pull_request_orphan_is_judged_by_head_sha_not_by_branch_listing() -> None:
    """Two open pull requests can share one head branch, so a newer run of that
    branch NAME does not establish that THIS run was superseded. At an open head the
    hold stands (the run is current); once the head moves, it is released."""
    verdict = wd.RunVerdict(
        run_id=1,
        run_attempt=1,
        head_branch="feature",
        head_repo=REPO,
        event="pull_request",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
        head_sha="sha-1",
    )
    listed = {
        "in_progress": [
            _run(2, minutes_ago=30, event="pull_request", branch="feature", head_sha="sha-2"),
            _run(1, minutes_ago=60, event="pull_request", branch="feature", head_sha="sha-1"),
        ]
    }
    current = FakeApi(
        listed, {}, newest_by_branch={"feature": 2}, open_pull_heads={"feature": ["sha-1"]}
    )
    assert not wd.supersession_clears_hold(current, _policy(), verdict, lambda _l: None)
    # Both reads happen: the pulls API for the head, the listing for a same-SHA successor.
    assert any(path.startswith(f"repos/{REPO}/pulls?") for path in current.gets)
    assert any("branch=feature" in path for path in current.gets)
    moved = FakeApi(
        listed, {}, newest_by_branch={"feature": 2}, open_pull_heads={"feature": ["sha-2"]}
    )
    assert wd.supersession_clears_hold(moved, _policy(), verdict, lambda _l: None)
    closed = FakeApi(listed, {}, newest_by_branch={"feature": 2}, open_pull_heads={})
    assert wd.supersession_clears_hold(closed, _policy(), verdict, lambda _l: None)


def test_a_fork_orphan_is_never_read_as_superseded() -> None:
    verdict = wd.RunVerdict(
        run_id=1,
        run_attempt=1,
        head_branch="main",
        head_repo="someone/example-repo",
        event="push",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
    )
    api = FakeApi({}, {}, newest_by_branch={"main": 2})
    assert not wd.supersession_clears_hold(api, _policy(), verdict, lambda _l: None)


def test_an_unanswerable_supersession_lookup_leaves_the_hold_standing() -> None:
    """Cancelling needs supersession ESTABLISHED, never assumed from a failed read."""
    verdict = wd.RunVerdict(
        run_id=1,
        run_attempt=1,
        head_branch="main",
        head_repo=REPO,
        event="push",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
    )

    def explode(*_a: Any, **_k: Any) -> bool:
        raise wd.LookupInconclusive("the branch listing could not be read")

    import unittest.mock as _mock

    api = FakeApi({}, {})
    with _mock.patch.object(wd, "is_current_run", explode):
        assert not wd.supersession_clears_hold(api, _policy(), verdict, lambda _l: None)


# ── somebody's own re-run is never cancelled, whichever route reaches the cancel ──


def _orphan_at_attempt(attempt: int) -> Any:
    return wd.RunVerdict(
        run_id=1,
        run_attempt=attempt,
        head_branch="main",
        head_repo=REPO,
        event="push",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
    )


def test_a_rerun_attempt_released_from_the_hold_is_still_not_cancelled() -> None:
    """The hold route ends the same way as the no-hold route: red, run named, no cancel.

    A later attempt may BE the operator response this script's logs ask for. The
    supersession check releases the hold (the hold protects a result the branch has
    moved past), so the run stays an orphan and reaches the heal path -- where the
    attempt guard refuses the cancel and records the FAILED outcome. Holding it green
    instead would recreate the 6-hour block with nobody told; cancelling it would
    discard a person's work with nothing left to show they did it.
    """
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=90, attempt=2)]},
        {1: [_job(11, minutes_ago=60, attempt=2)]},
        evidence=False,
        newest_by_branch={"main": 2},
    )
    logged: list[str] = []
    clock = _Clock()
    verdicts, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED  # the hold was released
    assert outcomes == {1: wd.OUTCOME_RERUN_ATTEMPT_LEFT}
    assert outcomes[1] in wd.FAILED_OUTCOMES
    assert api.posts == []
    said = " ".join(logged)
    assert "::error::" in said and "attempt 2" in said and "gh run cancel 1" in said


def test_attempt_one_is_still_released_by_supersession() -> None:
    """Positive control: the 6-hour block this escape exists for was a stuck
    attempt-1 run, so narrowing must not take the escape away from it."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, newest_by_branch={"main": 2})
    assert wd.supersession_clears_hold(api, _policy(), _orphan_at_attempt(1), lambda _l: None)


# ── ...and the no-hold route ends the same way ──────────────────────────────────


def test_a_superseded_rerun_attempt_is_not_cancelled_when_no_hold_applies() -> None:
    """Healthy dispatch evidence means no hold, so the hold guard is never consulted;
    the heal path must ask the same question itself before its cancel. Without this
    the run is cancelled, `_rerun` declines it as superseded, and the tick goes green
    over somebody's discarded `gh run rerun`."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr", attempt=2)]},
        {1: [_job(11, attempt=2)]},
        newest_by_branch={"pr": 2},
    )
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_RERUN_ATTEMPT_LEFT}
    assert _posts(api, "/cancel") == []
    assert _posts(api, "/rerun") == []
    # A failed outcome: the run keeps its concurrency group's running slot and the
    # watchdog will never free it, so the tick must go red and name the command.
    assert wd.OUTCOME_RERUN_ATTEMPT_LEFT in wd.FAILED_OUTCOMES
    said = " ".join(logged)
    assert "attempt 2" in said and "left untouched" in said and "discard" in said
    assert "::error::" in said and "gh run cancel 1" in said


def test_an_undecidable_supersession_leaves_a_rerun_attempt_uncancelled() -> None:
    """A lookup that cannot answer is treated like supersession: nothing is
    cancelled on a guess, since the cancel is the irreversible half."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr", attempt=2)]},
        {1: [_job(11, attempt=2)]},
        newest_by_branch={"pr": []},
    )
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_RERUN_ATTEMPT_LEFT}
    assert _posts(api, "/cancel") == []
    said = " ".join(logged)
    assert "::error::" in said and "cannot be told" in said
    assert "gh run cancel 1" in said and "gh run rerun 1" in said


def test_a_rerun_attempt_that_is_still_newest_is_healed_like_any_orphan() -> None:
    """Positive control: the guard is about supersession, not about attempt 2 as
    such. A stuck hand re-run that IS its branch's head loses nothing by a cancel
    that its re-run follows."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr", attempt=2)]},
        {1: [_job(11, attempt=2)]},
        newest_by_branch={"pr": 1},
        cancel_lands_after=1,
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/1/cancel"]
    assert len(_posts(api, "/rerun")) == 1


def test_attempt_one_superseded_with_no_hold_is_still_cancelled() -> None:
    """Positive control for the heal path: attempt 1 is nobody's re-run, so the
    pre-existing cancel-then-decline outcome for a superseded attempt-1 orphan is
    kept as it was (see the superseded-between-cancel-and-rerun test above)."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]}, {1: [_job(11)]}, newest_by_branch={"pr": 2}
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/1/cancel"]

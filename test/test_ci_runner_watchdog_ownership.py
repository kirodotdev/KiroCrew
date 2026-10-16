"""Once ``scripts/ci/runner_watchdog.py`` cancels a run, it owns that run until it is re-run.

The heal re-reads the run and its jobs right before the cancel, so a run a
human re-ran, a run that completed and a run whose job got a runner are left
alone. After an accepted cancel the watchdog sees the run through to a re-run:
it escalates a slow cancel to force-cancel, shares one wait budget across
runs, keeps the time the re-run needs in reserve, restores a successor its
cancel took down, and names every run it could not settle. An API failure in
the middle of a heal never drops that ownership.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from ci_runner_watchdog_helpers import (
    NOW,
    REPO,
    WORKFLOW,
    FakeApi,
    _cancelled_orphan_job,
    _Clock,
    _job,
    _policy,
    _posts,
    _run,
    _saturation_band_api,
    _sweep,
    _tick,
    _verdict_of,
    wd,
)

# ── the heal re-verifies before it cancels ──────────────────────────────────


def test_a_run_a_human_rerun_between_listing_and_heal_is_not_cancelled() -> None:
    """The verdict was computed for attempt 1; the live run is attempt 2. Hands off."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    api.run_overrides[1] = _run(1, attempt=2)
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}
    assert api.posts == []


def test_a_run_that_completed_before_the_heal_is_not_cancelled() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    api.run_overrides[1] = _run(1, status="completed", conclusion="success")
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}
    assert api.posts == []


def test_a_run_whose_job_got_a_runner_before_the_heal_is_not_cancelled() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    # The job flips to running after the listing read and stays running, so the
    # pre-cancel read (the last of the reads) sees a job that has a runner.
    reads = {"n": 0}
    original_get = api.get

    def flipping_get(path: str) -> Any:
        payload = original_get(path)
        if "/runs/1/jobs" in path:
            reads["n"] += 1
            if reads["n"] >= 2:
                payload = {"total_count": 1, "jobs": [_job(11, status="in_progress")]}
        return payload

    api.get = flipping_get  # type: ignore[method-assign]
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}
    assert api.posts == []


# ── once cancelled, the watchdog owns the run until it is re-run ────────────


def test_a_refused_cancel_is_logged_and_the_run_is_left_for_the_next_tick() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, refuse={"/cancel": 409})
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_CANCEL_FAILED}
    assert _posts(api, "/rerun") == []


def test_the_rerun_waits_for_the_cancel_to_land() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, cancel_lands_after=3)
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}
    # One read before the cancel, then three not-yet-completed reads, then the completed one.
    polls = [p for p in api.gets if p.endswith("/actions/runs/1")]
    assert len(polls) == 5
    assert _posts(api, "/force-cancel") == []


def test_a_slow_cancel_is_escalated_to_force_cancel_and_then_rerun() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, cancel_lands_after=10_000)
    _, outcomes = _sweep(api, force_cancel_after=timedelta(seconds=30))
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert len(_posts(api, "/force-cancel")) == 1
    cancel_i = api.posts.index(f"repos/{REPO}/actions/runs/1/cancel")
    force_i = api.posts.index(f"repos/{REPO}/actions/runs/1/force-cancel")
    rerun_i = api.posts.index(f"repos/{REPO}/actions/runs/1/rerun")
    assert cancel_i < force_i < rerun_i


def test_a_cancel_that_never_lands_exhausts_the_budget_without_rerunning() -> None:
    api = FakeApi(
        {"in_progress": [_run(1)]},
        {1: [_job(11)]},
        cancel_lands_after=10_000,
        force_cancel_works=False,
    )
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api,
        _policy(heal_budget=timedelta(seconds=60), force_cancel_after=timedelta(seconds=20)),
        clock=clock.now,
        sleep=clock.sleep,
        log=logged.append,
    )
    assert outcomes == {1: wd.OUTCOME_CANCEL_TIMED_OUT}
    assert _posts(api, "/rerun") == []
    assert len(_posts(api, "/force-cancel")) == 1
    assert any(line.startswith("::error::") and "gh run rerun 1" in line for line in logged)


def test_the_wait_budget_is_shared_across_runs_not_multiplied() -> None:
    """Five slow cancels must cost one budget, not five -- the job has a fixed ceiling."""
    api = FakeApi(
        {"in_progress": [_run(i, minutes_ago=100 - i) for i in range(1, 6)]},
        {i: [_job(i * 10, run_id=i)] for i in range(1, 6)},
        cancel_lands_after=10_000,
        force_cancel_works=False,
    )
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api,
        _policy(heal_budget=timedelta(seconds=120)),
        clock=clock.now,
        sleep=clock.sleep,
        log=lambda _l: None,
    )
    assert set(outcomes.values()) == {wd.OUTCOME_CANCEL_TIMED_OUT}
    assert clock.t <= 130


def test_the_cancel_wait_stops_early_enough_to_leave_the_rerun_reserve() -> None:
    """A cancel that lands with too little of the tick left to verify its re-run would
    be a mutation the job ceiling can interrupt; the wait ends before that point."""
    api = FakeApi(
        {"in_progress": [_run(1)]},
        {1: [_job(11)]},
        cancel_lands_after=10_000,
        force_cancel_works=False,
    )
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api,
        _policy(heal_budget=timedelta(seconds=300), tick_budget=timedelta(seconds=300)),
        clock=clock.now,
        sleep=clock.sleep,
        log=lambda _l: None,
    )
    assert outcomes == {1: wd.OUTCOME_CANCEL_TIMED_OUT}
    # 300 s budget minus the 240 s reserve: the wait gave up around 60 s, not 300.
    assert clock.t <= 300 - wd.RERUN_RESERVE_SECONDS + 10, clock.t


def test_a_rerun_late_in_the_tick_is_deferred_not_started() -> None:
    """Two orphans are cancelled while the reserve still fits; the first one's re-run then
    eats into it (slow branch listings), so the second is not begun: started, a worst-case
    restoration would outlive the job. Deferred, the run is named and the next tick's
    recovery pass re-runs it."""
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=50, branch="other")]},
        {1: [_job(11)], 2: [_job(21, run_id=2)]},
    )
    clock = _Clock()
    # Run 1's three lookups (one before its POST, two after) cost 60 s; with 30 s
    # of slack over the reserve, its own POST still fits (checked at 20 s) and
    # run 2's, checked at 65 s, does not.
    _slow_reads(api, clock, r"[?&]branch=main", 20.0)
    logged: list[str] = []
    _, outcomes = wd.run_watchdog(
        api,
        _policy(tick_budget=timedelta(seconds=wd.RERUN_RESERVE_SECONDS + 30)),
        clock=clock.now,
        sleep=clock.sleep,
        log=logged.append,
    )
    assert outcomes == {1: wd.OUTCOME_HEALED, 2: wd.OUTCOME_RERUN_DEFERRED}
    assert _posts(api, "/cancel") == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/2/cancel",
    ]
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert any(line.startswith("::error::") and "gh run rerun 2" in line for line in logged)
    assert wd.OUTCOME_RERUN_DEFERRED in wd.FAILED_OUTCOMES


def test_the_rerun_reserve_is_checked_again_right_before_the_post_not_only_before_the_lookup() -> (
    None
):
    """The branch lookup between the reserve check and the POST is itself slow enough to
    eat the reserve: the POST must not go out on a check that is already stale."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    clock = _Clock()
    _slow_reads(api, clock, r"[?&]branch=main", 20.0)
    logged: list[str] = []
    _, outcomes = wd.run_watchdog(
        api,
        _policy(tick_budget=timedelta(seconds=wd.RERUN_RESERVE_SECONDS + 10)),
        clock=clock.now,
        sleep=clock.sleep,
        log=logged.append,
    )
    assert outcomes == {1: wd.OUTCOME_RERUN_DEFERRED}
    assert _posts(api, "/rerun") == []
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/1/cancel"]
    assert any(line.startswith("::error::") and "gh run rerun 1" in line for line in logged)


def test_a_cancel_is_not_posted_when_its_rerun_could_not_be_verified_in_time() -> None:
    """With less than the reserve left, cancelling would discard the run's finished jobs
    and leave it cancelled until the next tick; untouched, it loses nothing by waiting."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api,
        _policy(tick_budget=timedelta(seconds=wd.RERUN_RESERVE_SECONDS - 1)),
        clock=clock.now,
        sleep=clock.sleep,
        log=logged.append,
    )
    assert outcomes == {1: wd.OUTCOME_NOT_ATTEMPTED}
    assert api.posts == []
    assert any("left untouched for the next tick" in line for line in logged)


def test_pre_cancel_evidence_is_judged_against_the_current_wall_time_not_the_ticks_start() -> None:
    """A sibling started promptly 25 min before the tick began -- inside the 30-min lookback,
    so the orphan is healable. The tick is slow: 400 s pass before the cancel. Re-read at
    that moment the start is 31.7 min old, outside the lookback; with nothing newer the
    fleet's state is unknown and the cancel is held, exactly as a fresh tick would."""
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=25.3,
        started_minutes_ago=25,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [prompt]},
        evidence=False,
    )
    clock = _Clock()
    _slow_reads(api, clock, r"/actions/runs/1$", 400.0)  # the pre-cancel re-read is slow
    verdicts, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=lambda _l: None
    )
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert api.posts == []
    assert outcomes == {}


def test_the_rerun_reserve_covers_the_longest_restoration_chain_under_the_job_ceiling() -> None:
    """The reserve is derived from the chain's own constants, and the workflow's
    `timeout-minutes` sits above the tick budget with room for checkout and the
    summary, so the ceiling is never what ends a restoration."""
    chain = (wd.MAX_RESTORE_DEPTH + 1) * (
        wd.POST_RERUN_SETTLE_SECONDS + 2 * wd.SUCCESSOR_SETTLE_SECONDS
    )
    assert wd.RESTORE_CHAIN_SECONDS == chain
    assert wd.RERUN_RESERVE_SECONDS >= chain + 30
    tick_budget = wd.Policy(repo=REPO, now=NOW).tick_budget.total_seconds()
    assert (
        tick_budget
        >= wd.RERUN_RESERVE_SECONDS + wd.Policy(repo=REPO, now=NOW).heal_budget.total_seconds()
    )
    timeout_lines = [
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("timeout-minutes:")
    ]
    assert len(timeout_lines) == 1, timeout_lines
    timeout_minutes = int(timeout_lines[0].split(":", 1)[1].strip())
    assert timeout_minutes * 60 >= tick_budget + 120


def test_runs_are_rerun_as_each_cancel_lands_not_after_the_slowest() -> None:
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=50, branch="other")]},
        {1: [_job(11)], 2: [_job(21, run_id=2)]},
        cancel_lands_after=2,
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED, 2: wd.OUTCOME_HEALED}
    assert len(_posts(api, "/rerun")) == 2


def test_a_run_superseded_between_cancel_and_rerun_is_not_rerun_into_its_successor() -> None:
    """A new push landed after the cancel: re-running the old run would cancel the new one."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]}, {1: [_job(11)]}, newest_by_branch={"pr": 2}
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/1/cancel"]
    assert _posts(api, "/rerun") == []
    assert any("branch=pr" in p and "event=push" in p for p in api.gets)


def test_a_per_sha_audit_run_is_heal_exempt_and_never_cancelled() -> None:
    api = FakeApi(
        {"in_progress": [_run(1, workflow="main-ratchet-audit.yml")]},
        {1: [_job(11)]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEAL_EXEMPT
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert api.posts == []


def test_a_run_that_finished_on_its_own_before_the_cancel_landed_is_not_rerun() -> None:
    api = FakeApi(
        {"in_progress": [_run(1)]}, {1: [_job(11)]}, cancel_lands_after=2, completes_as="success"
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_COMPLETED_ON_ITS_OWN}
    assert _posts(api, "/rerun") == []


def test_a_cancel_that_hit_a_hand_rerun_attempt_is_followed_by_a_restoring_rerun() -> None:
    """The cancel was verified against attempt 1 but landed on attempt 2: re-run, and say so."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, attempt_after_cancel=2)
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert any("::warning::" in line and "re-run by hand" in line for line in logged)


def test_a_successor_that_appears_during_the_rerun_is_restored_and_the_rerun_cancelled() -> None:
    """A push landed between the newest check and the re-run: yield to it."""
    successor = _run(2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr")
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
    )
    api.run_overrides[2] = successor
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_RESTORED}
    assert api.posts == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/2/rerun",
    ]


def test_a_successor_still_running_when_the_window_closes_is_unsettled_not_settled() -> None:
    """A group cancel can land late and a run mid-cancellation reports in_progress until it
    does, so a successor that has not reached a terminal state by the end of the window is
    never called settled: the tick fails, names it, and says what to type if it ends cancelled."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {
            1: [_job(11)],
            2: [_job(21, status="in_progress", minutes_ago=1, codebuild=False, run_id=2)],
        },
        newest_after_rerun={"pr": 2},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    clock = _Clock()
    logged: list[str] = []
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_UNSETTLED}
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/1/cancel"] * 2
    assert any(line.startswith("::error::") and "gh run rerun 2" in line for line in logged)
    # Watched for the whole settle window, not given up on after the first read.
    assert len([p for p in api.gets if p.endswith("/actions/runs/2")]) >= 1 + int(
        wd.SUCCESSOR_SETTLE_SECONDS // wd.POST_RERUN_SETTLE_SECONDS
    )


def test_a_successor_that_finished_on_its_own_is_left_alone() -> None:
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
    )
    api.run_overrides[2] = _run(
        2, minutes_ago=1, status="completed", conclusion="success", branch="pr"
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_FINISHED}
    assert not any(p.endswith("/runs/2/rerun") for p in api.posts)


def test_a_successor_that_turns_out_to_be_cancelling_mid_window_is_not_called_live() -> None:
    """Live jobs at first, then one concludes cancelled: the group was cancelling it all along."""
    live_jobs = [_job(21, status="in_progress", minutes_ago=1, codebuild=False, run_id=2)]
    cancelled = _run(2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr")
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: live_jobs},
        newest_after_rerun={"pr": 2},
        flip_after_reads={2: (3, cancelled)},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_RESTORED}
    assert api.posts[-1] == f"repos/{REPO}/actions/runs/2/rerun"


def test_the_successor_rerun_is_bracketed_too_and_yields_to_a_run_landing_in_its_window() -> None:
    """Run 1's re-run yielded to run 2; run 2's re-run in turn sees run 3 appear and yields to it."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {
            1: [_job(11)],
            2: [],
            3: [_job(31, status="in_progress", minutes_ago=1, codebuild=False, run_id=3)],
        },
        newest_after_rerun_sequence={"pr": [2, 2, 3]},
    )
    api.run_overrides[2] = _run(
        2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr"
    )
    api.run_overrides[3] = _run(3, minutes_ago=0.5, status="in_progress", branch="pr")
    _, outcomes = _sweep(api)
    # Run 3 is still running when its window closes: unsettled, not settled.
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_UNSETTLED}
    assert api.posts == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/2/rerun",
        f"repos/{REPO}/actions/runs/2/cancel",
    ]


def test_a_chain_of_superseding_pushes_is_not_chased_past_the_depth_cap() -> None:
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: [], 3: [], 4: []},
        newest_after_rerun_sequence={"pr": [2, 2, 3, 3, 4]},
    )
    for run_id in (2, 3, 4):
        api.run_overrides[run_id] = _run(
            run_id, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr"
        )
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_LOST}
    assert not any(p.endswith("/runs/4/rerun") for p in api.posts)
    assert any("not chasing further" in line and "gh run rerun 4" in line for line in logged)


def test_a_successor_we_cancelled_and_will_not_restore_is_lost_not_a_clean_handoff() -> None:
    """Declining at depth 0 is healthy; declining AFTER our own re-run cancelled it is not.

    Run 1 is heal-safe and healed, its re-run's concurrency group cancels the successor
    run 2 that landed in the settle window, and run 2's OWN revision then reads
    heal-unsafe, so the watchdog will not restore it. At depth 0 that verdict means
    "nothing was touched" and the tick stays green; here it means the watchdog
    destroyed a run and walked away, so it must carry the same red as a refused
    re-run or the run vanishes behind a green tick with no fingerprint for recovery.
    """
    successor = _run(
        2,
        minutes_ago=1,
        status="completed",
        conclusion="cancelled",
        branch="pr",
        head_sha="successor-publishes",
    )
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
        workflow_contents={
            ("successor-publishes", "ci.yml"): (
                "concurrency:\n  group: ${{ github.ref }}\njobs:\n  ship:\n"
                "    runs-on: ubuntu-latest\n    steps:\n      - run: npm publish\n"
            )
        },
    )
    api.run_overrides[2] = successor
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_LOST}
    assert wd.OUTCOME_SUCCESSOR_LOST in wd.FAILED_OUTCOMES
    assert not any(path.endswith("/runs/2/rerun") for path in api.posts)
    assert any("::error::" in line and "gh run rerun 2" in line for line in logged)


def test_a_narrow_bound_still_reads_the_slow_start_because_the_reserve_is_age_aware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound too small to read everything must still be able to see saturation.

    The reserve takes the newest run at least ``saturation_wait`` old, so under a
    one-slot reserve it takes the 20-minute middle run holding the slow start and
    holds. The reverted rule -- reserve the newest run outright -- would take the
    3-minute young run, which is too young to contain a wait past the threshold and
    so can only report the fleet dispatching. Both bounds reach the same verdict
    here, which is the point: the hold now rests on evidence that was read.
    """
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 2)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    narrow = _saturation_band_api()
    verdicts, outcomes = _sweep(narrow)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_SATURATED
    assert narrow.posts == []

    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 50)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 10)
    wide = _saturation_band_api()
    wide_verdicts, _ = _sweep(wide)
    assert _verdict_of(wide_verdicts, 1).verdict == wd.SKIPPED_SATURATED
    assert wide.posts == []
    assert outcomes is not None


def test_the_evidence_reserve_prefers_runs_old_enough_to_show_a_slow_start() -> None:
    """The reserve slice, and the negative control that the reverted rule fails.

    A run younger than ``saturation_wait`` cannot hold a start that waited that
    long, so reserving the newest runs outright is blind to the one signal that
    holds. Measured on this repository the newest ten live runs spanned 0.0 to 0.6
    minutes while 352 runs could carry one, so this is the normal shape, not an
    edge case.

    The straddling pair is what pins the line at ``saturation_wait`` and not at
    ``orphan_after``: 8 minutes is in, 4 minutes is out, so swapping the constant
    reds this test.
    """
    policy = _policy()
    assert policy.saturation_wait == timedelta(minutes=5)
    runs = [_run(i, minutes_ago=age) for i, age in enumerate([25, 20, 8, 6, 4, 1], start=1)]
    reserve = [run["id"] for run in wd._evidence_reserve(runs, policy)]
    # Every run at or past 5 minutes, newest last; the 4- and 1-minute runs are out.
    assert reserve == [1, 2, 3, 4]
    assert wd._can_carry_a_slow_start(runs[2], policy) is True  # 8 min, over the line
    assert wd._can_carry_a_slow_start(runs[4], policy) is False  # 4 min, under it
    # The reverted rule takes the tail outright, so its newest slot is the 1-minute
    # run, which cannot carry a wait that crossed the line: that reserve can only ever
    # answer "dispatching" and is blind to the signal that holds.
    assert runs[-wd.LIVE_EVIDENCE_RESERVE :][-1]["id"] == 6
    young_only = [_run(7, minutes_ago=3), _run(8, minutes_ago=1)]
    assert [run["id"] for run in wd._evidence_reserve(young_only, policy)] == [7, 8]


def test_the_successor_is_not_judged_while_our_own_rerun_is_still_cancelling() -> None:
    """Our re-run's cancel never lands: force-cancel is tried, then the tick fails rather than guesses.

    Exercised on _restore_successor directly: through the full sweep the heal's own
    cancel would time out first and never reach a re-run."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {
            1: [_job(11)],
            2: [_job(21, status="in_progress", minutes_ago=1, codebuild=False, run_id=2)],
        },
        cancel_lands_after=10_000,
        force_cancel_works=False,
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    verdict = wd._base_verdict(_run(1, branch="pr"), NOW)
    logged: list[str] = []
    clock = _Clock()
    outcome = wd._restore_successor(
        api, f"repos/{REPO}/actions/runs/1", verdict, 2, _policy(), logged.append, tick=_tick(clock)
    )
    assert outcome == wd.OUTCOME_OWN_RERUN_UNCANCELLED
    assert wd.OUTCOME_OWN_RERUN_UNCANCELLED in wd.FAILED_OUTCOMES
    assert _posts(api, "/force-cancel") == [f"repos/{REPO}/actions/runs/1/force-cancel"]
    assert _posts(api, "/rerun") == []
    assert not any("/runs/2/jobs" in p for p in api.gets)  # never judged
    assert any("::error::" in line and "run 2" in line for line in logged)


def test_a_slow_own_rerun_cancel_is_force_cancelled_and_then_the_successor_is_judged() -> None:
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {
            1: [_job(11)],
            2: [_job(21, status="in_progress", minutes_ago=1, codebuild=False, run_id=2)],
        },
        cancel_lands_after=10_000,  # a plain cancel never lands; force-cancel does
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    verdict = wd._base_verdict(_run(1, branch="pr"), NOW)
    clock = _Clock()
    outcome = wd._restore_successor(
        api,
        f"repos/{REPO}/actions/runs/1",
        verdict,
        2,
        _policy(),
        lambda _l: None,
        tick=_tick(clock),
    )
    # The successor was judged (force-cancel got our re-run out of the way) and, still
    # running at the end of its window, is unsettled rather than called settled.
    assert outcome == wd.OUTCOME_SUCCESSOR_UNSETTLED
    assert _posts(api, "/force-cancel") == [f"repos/{REPO}/actions/runs/1/force-cancel"]
    assert any(p.endswith("/actions/runs/2") for p in api.gets)  # it WAS judged
    assert len(_posts(api, "/force-cancel")) == 1


def test_a_cancelled_successor_that_was_itself_superseded_is_not_rerun() -> None:
    """A yet-newer run took the branch over: it carries the verdict, nothing is re-run."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun_sequence={"pr": [2, 3]},
    )
    api.run_overrides[2] = _run(
        2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr"
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_SUPERSEDED}
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert wd.OUTCOME_SUCCESSOR_SUPERSEDED not in wd.FAILED_OUTCOMES


def _slow_reads(api: FakeApi, clock: _Clock, pattern: str, seconds: float) -> None:
    """Every GET matching ``pattern`` costs ``seconds`` of wall clock, as a slow API would."""
    original_get = api.get

    def get(path: str) -> Any:
        if re.search(pattern, path):
            clock.t += seconds
        return original_get(path)

    api.get = get  # type: ignore[method-assign]


def test_the_settle_windows_are_wall_clock_so_slow_reads_cannot_stretch_them() -> None:
    """Each successor read takes 20 s. Counting sleeps alone, seven reads would stretch the
    30 s window to nearly three minutes of real time; measured on the clock, the window
    closes on schedule and the verdict is the same."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    clock = _Clock()
    _slow_reads(api, clock, r"/actions/runs/2$", 20.0)
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=lambda _l: None
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_UNSETTLED}
    # 5 s settle + a 30 s window + at most one read that overran it.
    assert clock.t <= 5 + wd.SUCCESSOR_SETTLE_SECONDS + 20 + wd.POST_RERUN_SETTLE_SECONDS, clock.t


def test_a_nested_rerun_without_time_left_names_the_successor_instead_of_starting() -> None:
    """Slow branch listings eat the budget before the successor ends cancelled; re-running it
    now could not be verified inside the tick, so it is reported lost by name rather than
    started and cut off by the job ceiling."""
    live_jobs = [_job(21, status="in_progress", minutes_ago=1, codebuild=False, run_id=2)]
    cancelled = _run(2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr")
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: live_jobs},
        newest_after_rerun={"pr": 2},
        flip_after_reads={2: (3, cancelled)},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    clock = _Clock()
    _slow_reads(api, clock, r"[?&]branch=", 100.0)
    # The budget carries the root re-run's own slow pre-POST lookup on top of
    # the reserve, so the root is re-run; the successor's lookup then leaves
    # less than the nested reserve.
    budget = wd.RERUN_RESERVE_SECONDS + 100.0
    logged: list[str] = []
    _, outcomes = wd.run_watchdog(
        api,
        _policy(tick_budget=timedelta(seconds=budget)),
        clock=clock.now,
        sleep=clock.sleep,
        log=logged.append,
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_LOST}
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert any(line.startswith("::error::") and "gh run rerun 2" in line for line in logged)
    assert clock.t <= budget, clock.t  # never past the tick deadline


def test_a_successor_that_never_settles_is_a_failed_outcome_not_a_guess() -> None:
    """Status in_progress, no job live, none cancelled yet: cannot tell, so fail and keep ownership."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_UNSETTLED}
    assert any("::error::" in line and "gh run rerun 2" in line for line in logged)
    assert wd.OUTCOME_SUCCESSOR_UNSETTLED in wd.FAILED_OUTCOMES


def test_a_successor_cancelled_asynchronously_is_still_restored() -> None:
    """The first reads after yielding still show the successor in_progress with a cancelled job;
    the run-level cancellation lands later."""
    live = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    cancelled = _run(2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr")
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {
            1: [_job(11)],
            2: [
                _job(21, status="completed", conclusion="cancelled", minutes_ago=1, codebuild=False)
            ],
        },
        newest_after_rerun={"pr": 2},
        flip_after_reads={2: (2, cancelled)},
    )
    api.run_overrides[2] = live
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_RESTORED}
    assert api.posts[-1] == f"repos/{REPO}/actions/runs/2/rerun"


def test_a_successor_whose_rerun_is_refused_reds_the_tick() -> None:
    successor = _run(2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr")
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
        refuse={"/runs/2/rerun": 403},
    )
    api.run_overrides[2] = successor
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_LOST}


def test_a_same_named_fork_branch_does_not_masquerade_as_the_newest_run() -> None:
    """The branch listing matches names; a fork's `main` is listed first but is not ours."""
    listing = [
        {"id": 2, "head_repository": {"full_name": "someone/example-repo"}},
        {"id": 1, "head_repository": {"full_name": REPO}},
    ]
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, newest_by_branch={"main": listing})
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_a_cancelled_orphan_is_still_recovered_past_a_same_named_fork_branch() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    listing = [
        {"id": 2, "head_repository": {"full_name": "someone/example-repo"}},
        {"id": 1, "head_repository": {"full_name": REPO}},
    ]
    api = FakeApi(
        {"cancelled": [run]}, {1: [_cancelled_orphan_job(11)]}, newest_by_branch={"main": listing}
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_RECOVERED}


def test_recovery_read_budget_serves_the_run_nearest_updated_at_expiry_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshed_old_run = _run(
        1,
        minutes_ago=80,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
    )
    expiring_newer_run = _run(
        2,
        minutes_ago=100,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=80,
    )
    api = FakeApi(
        {"cancelled": [expiring_newer_run, refreshed_old_run]},
        {1: [], 2: []},
        evidence=False,
    )
    monkeypatch.setattr(wd, "RECOVERY_CLASSIFY_READS", 1)
    clock = _Clock()
    wd.recover_cancelled_runs(api, _policy(), budget=0, tick=_tick(clock), log=lambda _l: None)
    job_reads = [path for path in api.gets if "/jobs?" in path]
    assert job_reads == [f"repos/{REPO}/actions/runs/2/jobs?per_page=100&page=1&filter=latest"]


def test_the_post_rerun_check_settles_before_declaring_the_heal_done() -> None:
    api = FakeApi({"in_progress": [_run(1, branch="pr")]}, {1: [_job(11)]})
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=lambda _l: None
    )
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert len([p for p in api.gets if "branch=pr" in p]) == 3  # before, after, after settle
    assert clock.t >= wd.POST_RERUN_SETTLE_SECONDS


def test_a_refused_rerun_explained_by_someone_elses_rerun_is_superseded_not_failed() -> None:
    """Refused because the run is in flight again: somebody re-ran it between the cancel and our POST."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, refuse={"/runs/1/rerun": 403})
    reads = {"n": 0}
    original_get = api.get

    def in_flight_after_refusal(path: str) -> Any:
        payload = original_get(path)
        if path.endswith("/actions/runs/1") and payload.get("status") == "completed":
            reads["n"] += 1
            if reads["n"] >= 2:  # the read AFTER the refused POST sees a fresh attempt running
                payload = {**payload, "status": "in_progress", "run_attempt": 2, "conclusion": None}
        return payload

    api.get = in_flight_after_refusal  # type: ignore[method-assign]
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}


def test_a_refused_rerun_after_cancelling_a_hand_rerun_attempt_is_still_a_failure() -> None:
    """The cancel landed on attempt 2 (a human's re-run); the refused re-run must compare against 2, not 1."""
    api = FakeApi(
        {"in_progress": [_run(1)]},
        {1: [_job(11)]},
        refuse={"/runs/1/rerun": 403},
        attempt_after_cancel=2,
    )
    # After the cancel the run reports attempt 2 and stays completed/cancelled at attempt 2.
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_RERUN_REFUSED}
    assert any("::error::" in line and "gh run rerun 1" in line for line in logged)


def test_a_refused_rerun_whose_read_back_also_fails_is_a_failure_not_superseded() -> None:
    """During an API incident the re-run POST is refused AND the confirming read fails.
    With nothing to explain the refusal, the outcome must fail closed and name the run,
    never guess that somebody else re-ran it."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, refuse={"/runs/1/rerun": 403})
    _failing_reads(api, r"/actions/runs/1$", times=None, after_post="/runs/1/rerun")
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_RERUN_REFUSED}
    assert any(line.startswith("::error::") and "gh run rerun 1" in line for line in logged)
    assert not any("already re-run by someone else" in line for line in logged)


def test_an_ambiguous_cancel_is_owned_and_reconciled_not_re_posted() -> None:
    """The cancel landed server-side but its response was lost. Re-posting would hit a
    conflict on the cancelling run and read as a refusal, stranding it cancelled; instead
    the run is owned and polled like any accepted cancel, and re-run when it completes."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, ambiguous={"/runs/1/cancel": 1})
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/1/cancel"]  # posted ONCE
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert any("ambiguous result" in line for line in logged)


def test_an_ambiguous_rerun_that_landed_is_verified_like_a_clean_one() -> None:
    """The re-run POST's response was lost, but the run is in flight again: it landed, and
    it gets the same post-re-run verification as a clean POST rather than 'superseded'."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, ambiguous={"/runs/1/rerun": 1})
    original_get = api.get

    def in_flight_once_rerun_was_posted(path: str) -> Any:
        payload = original_get(path)
        if path.endswith("/actions/runs/1") and any(p.endswith("/runs/1/rerun") for p in api.posts):
            payload = {**payload, "status": "in_progress", "run_attempt": 2, "conclusion": None}
        return payload

    api.get = in_flight_once_rerun_was_posted  # type: ignore[method-assign]
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]  # posted ONCE
    assert any("it landed" in line for line in logged)
    assert not any("already re-run by someone else" in line for line in logged)


def test_an_ambiguous_rerun_that_did_not_land_is_a_failure_that_names_the_run() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    original_post = api.post

    def lost_without_landing(path: str) -> None:
        if path.endswith("/runs/1/rerun"):
            api.posts.append(path)
            raise wd.ApiError(0, "TimeoutError: response lost", ambiguous=True)
        original_post(path)

    api.post = lost_without_landing  # type: ignore[method-assign]
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_RERUN_REFUSED}
    assert any(line.startswith("::error::") and "gh run rerun 1" in line for line in logged)


def test_a_refused_rerun_nobody_else_explains_is_a_failed_outcome() -> None:
    """403 with the run still cancelled at our attempt: nobody re-ran it, the verdict is lost -- say so."""
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=50, branch="other")]},
        {1: [_job(11)], 2: [_job(21, run_id=2)]},
        refuse={"/runs/1/rerun": 403},
    )
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_RERUN_REFUSED, 2: wd.OUTCOME_HEALED}
    assert wd.OUTCOME_RERUN_REFUSED in wd.FAILED_OUTCOMES
    assert any("::error::" in line and "gh run rerun 1" in line for line in logged)


def test_a_branch_listing_that_never_reaches_our_run_fails_closed() -> None:
    """An empty (or fork-only) listing is 'cannot tell', never 'still newest'."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, newest_by_branch={"main": []})
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert _posts(api, "/rerun") == []
    assert wd.OUTCOME_LOOKUP_FAILED in wd.FAILED_OUTCOMES


def test_a_listing_that_shows_an_older_run_but_not_ours_is_inconclusive_not_superseded() -> None:
    """An older same-repo run first with the judged run absent contradicts newest-first ordering."""
    older_only = [{"id": 0, "head_repository": {"full_name": REPO}}]
    api = FakeApi(
        {"in_progress": [_run(1)]}, {1: [_job(11)]}, newest_by_branch={"main": older_only}
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert _posts(api, "/rerun") == []


def test_the_hold_is_judged_per_orphan_relative_to_its_own_queue_time() -> None:
    """One prompt start 25 min ago: evidence for an orphan queued 60 min ago, none for one queued 20 min ago."""
    older = _run(1, minutes_ago=90, branch="a")
    younger = _run(2, minutes_ago=40, branch="b")
    healthy = _run(
        3,
        minutes_ago=30,
        status="completed",
        conclusion="success",
        updated_minutes_ago=24,
        branch="c",
    )
    started = _job(
        31,
        status="completed",
        conclusion="success",
        minutes_ago=25.5,
        started_minutes_ago=25.2,
        runner_name="r",
        run_id=3,
    )
    api = FakeApi(
        {"in_progress": [older, younger], "completed": [healthy]},
        {1: [_job(11, minutes_ago=60)], 2: [_job(21, minutes_ago=20, run_id=2)], 3: [started]},
        evidence=False,
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert _verdict_of(verdicts, 2).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_a_run_with_several_orphans_is_judged_from_its_newest_orphans_queue_time() -> None:
    """A matrix run queued one job 60 min ago and another 20 min ago. A start 25 min ago says
    the fleet was alive between them -- nothing about the fleet the younger one is waiting
    on. The hold must be judged from the newest orphan, not the oldest."""
    healthy = _run(
        3,
        minutes_ago=30,
        status="completed",
        conclusion="success",
        updated_minutes_ago=24,
        branch="c",
    )
    started_between = _job(
        31,
        status="completed",
        conclusion="success",
        minutes_ago=25.5,
        started_minutes_ago=25.2,
        runner_name="r",
        run_id=3,
    )
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=90)], "completed": [healthy]},
        {1: [_job(11, minutes_ago=60), _job(12, minutes_ago=20)], 3: [started_between]},
        evidence=False,
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert outcomes == {}
    assert api.posts == []


def test_a_start_after_the_newest_orphan_queued_clears_the_hold_for_the_whole_run() -> None:
    healthy = _run(
        3,
        minutes_ago=30,
        status="completed",
        conclusion="success",
        updated_minutes_ago=9,
        branch="c",
    )
    started_after = _job(
        31,
        status="completed",
        conclusion="success",
        minutes_ago=10.5,
        started_minutes_ago=10.2,
        runner_name="r",
        run_id=3,
    )
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=90)], "completed": [healthy]},
        {1: [_job(11, minutes_ago=60), _job(12, minutes_ago=20)], 3: [started_after]},
        evidence=False,
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_the_branch_listing_is_paged_past_a_wall_of_same_named_fork_runs() -> None:
    fork = {"id": 900, "head_repository": {"full_name": "someone/example-repo"}}
    page1 = [dict(fork, id=900 + i) for i in range(wd.BRANCH_LISTING_DEPTH)]
    page2 = [{"id": 1, "head_repository": {"full_name": REPO}}]
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, newest_by_branch={"main": page1})
    original_get = api.get

    def paged_get(path: str) -> Any:
        if "branch=main" in path and "page=2" in path:
            return {"workflow_runs": page2}
        return original_get(path)

    api.get = paged_get  # type: ignore[method-assign]
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_main_exits_nonzero_when_a_heal_ran_out_of_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    summary = tmp_path / "summary.md"
    argv = ["--repo", REPO, "--summary", str(summary)]
    monkeypatch.setattr(
        wd, "run_watchdog", lambda *_a, **_k: ([], {1: wd.OUTCOME_CANCEL_TIMED_OUT})
    )
    assert wd.main(argv) == 1
    for outcome in sorted(wd.FAILED_OUTCOMES):
        monkeypatch.setattr(wd, "run_watchdog", lambda *_a, _o=outcome, **_k: ([], {1: _o}))
        assert wd.main(argv) == 1, outcome
    monkeypatch.setattr(wd, "run_watchdog", lambda *_a, **_k: ([], {1: wd.OUTCOME_HEALED}))
    assert wd.main(argv) == 0
    assert summary.read_text(encoding="utf-8").count("## CI runner watchdog") == 2 + len(
        wd.FAILED_OUTCOMES
    )


# ── the API can fail mid-heal; ownership is never dropped on an exception ────


def _failing_reads(
    api: FakeApi, pattern: str, *, times: int | None, after_post: str | None = None
) -> None:
    """Make GETs of paths matching ``pattern`` raise ``ApiError`` -- ``times`` times, or
    forever when ``None`` -- optionally only once a POST ending in ``after_post`` was seen."""
    original_get = api.get
    state = {"left": times}

    def get(path: str) -> Any:
        armed = after_post is None or any(p.endswith(after_post) for p in api.posts)
        if armed and re.search(pattern, path) and (state["left"] is None or state["left"] > 0):
            if state["left"] is not None:
                state["left"] -= 1
            raise wd.ApiError(502, "bad gateway")
        return original_get(path)

    api.get = get  # type: ignore[method-assign]


def test_a_failed_read_while_waiting_for_a_cancel_keeps_the_run_owned() -> None:
    """Two 502s on the status poll: the run stays pending, is polled again, and is re-run."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    _failing_reads(api, r"/actions/runs/1$", times=2, after_post="/runs/1/cancel")
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert sum("could not read" in line for line in logged) == 2


def test_reads_that_never_recover_time_the_cancel_out_instead_of_raising() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    _failing_reads(api, r"/actions/runs/1$", times=None, after_post="/runs/1/cancel")
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_CANCEL_TIMED_OUT}
    assert _posts(api, "/rerun") == []
    assert any(line.startswith("::error::") and "gh run rerun 1" in line for line in logged)


def test_a_read_failure_before_the_cancel_leaves_the_run_untouched() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    _failing_reads(api, r"/actions/runs/1$", times=None)
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_NOT_ATTEMPTED}
    assert api.posts == []


def test_a_branch_listing_that_cannot_be_read_is_an_inconclusive_lookup_not_a_crash() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    _failing_reads(api, r"[?&]branch=", times=None, after_post="/runs/1/cancel")
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert _posts(api, "/rerun") == []
    assert any(line.startswith("::error::") and "gh run rerun 1" in line for line in logged)


def test_a_successor_whose_reads_keep_failing_is_named_unsettled_not_abandoned() -> None:
    """The successor cannot be read for the whole window: ownership ends in a failed
    outcome that names it, never in an exception that leaves it cancelled and unnamed."""
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: []},
        newest_after_rerun={"pr": 2},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    _failing_reads(api, r"/actions/runs/2$", times=None)
    logged: list[str] = []
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_UNSETTLED}
    assert any(line.startswith("::error::") and "gh run rerun 2" in line for line in logged)


def test_a_transient_successor_read_failure_does_not_change_the_verdict() -> None:
    """Same shape as the mid-window cancellation case, with the first successor read failing."""
    live_jobs = [_job(21, status="in_progress", minutes_ago=1, codebuild=False, run_id=2)]
    cancelled = _run(2, minutes_ago=1, status="completed", conclusion="cancelled", branch="pr")
    api = FakeApi(
        {"in_progress": [_run(1, branch="pr")]},
        {1: [_job(11)], 2: live_jobs},
        newest_after_rerun={"pr": 2},
        flip_after_reads={2: (3, cancelled)},
    )
    api.run_overrides[2] = _run(2, minutes_ago=1, status="in_progress", branch="pr")
    _failing_reads(api, r"/actions/runs/2$", times=1)
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUCCESSOR_RESTORED}
    assert api.posts[-1] == f"repos/{REPO}/actions/runs/2/rerun"

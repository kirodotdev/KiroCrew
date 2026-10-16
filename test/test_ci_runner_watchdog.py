"""The CI runner watchdog must heal exactly the orphaned-runner shape and nothing else.

``scripts/ci/runner_watchdog.py`` cancels and re-runs a ``ci.yml`` run whose
CodeBuild-routed job has sat *queued* with no runner past a threshold. Every
guard rail exists because the alternative is a watchdog that cancels a
healthy run: a job that is merely slow to dispatch, a run that is young, a run
held by its concurrency group, a fork the token cannot re-run, a run the
watchdog already re-ran twice, or a run a human re-ran between the listing
and the heal.

The second family of tests pins ownership: once a cancel is accepted the
watchdog must see it through to a re-run -- escalating to force-cancel, sharing
one wait budget across runs, and on the next tick recovering a run whose cancel
landed after the budget ran out -- because a cancelled run that nobody re-runs
has lost its verdict for good.

The GitHub API is a fake that records every write, so each test pins both the
verdict and the exact calls made. Nothing here touches the network.

This file holds the orphaned shape, the holds that keep a slow fleet from
looking dead, the runs that look stuck but are not, and the guard rails on
which runs a tick may touch and how it lists them. The rest of the family
lives beside it: ``test_ci_runner_watchdog_ownership.py`` (the re-check before
the cancel, and the ownership family above),
``test_ci_runner_watchdog_recovery.py`` (the next tick's recovery and
pull-request runs), ``test_ci_runner_watchdog_cli.py`` (the entry point, its
output and rate limits), ``test_ci_runner_watchdog_workflows.py`` (the watched
set and the read bound) and ``test_ci_runner_watchdog_supersession.py`` (a
superseded orphan, and a re-run attempt). The shared fake and builders are in
``ci_runner_watchdog_helpers.py``.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
from datetime import timedelta
from typing import Any

import pytest
from ci_runner_watchdog_helpers import (
    CODEBUILD,
    NOW,
    REPO,
    FakeApi,
    _cancelled_orphan_job,
    _Clock,
    _job,
    _policy,
    _posts,
    _run,
    _sweep,
    _verdict_of,
    wd,
)

# ── the orphaned shape ──────────────────────────────────────────────────────


def test_an_orphaned_codebuild_job_gets_the_run_cancelled_and_rerun() -> None:
    api = FakeApi(
        {"in_progress": [_run(1)]},
        {1: [_job(10, status="completed", codebuild=False), _job(11, large=True)]},
    )
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.ORPHANED
    assert [o.job_id for o in verdict.orphans] == [11]
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert api.posts == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
    ]


def test_the_label_prefix_is_matched_even_with_an_override_suffix() -> None:
    assert wd.is_codebuild_job({"labels": ["codebuild-p-1-1 instance-size:large"]})
    assert wd.is_codebuild_job({"labels": ["ubuntu-latest", "codebuild-p-1-1"]})
    assert not wd.is_codebuild_job({"labels": ["ubuntu-latest"]})
    assert not wd.is_codebuild_job({"labels": []})
    assert not wd.is_codebuild_job({})


def test_the_rerun_is_the_whole_run_so_the_runner_label_is_recomputed() -> None:
    """A failed-jobs re-run would reuse the first attempt's `changes` outputs and its stale label."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}
    assert api.posts[-1] == f"repos/{REPO}/actions/runs/1/rerun"
    assert not any(p.endswith("/rerun-failed-jobs") for p in api.posts)


def test_dry_run_reads_everything_and_writes_nothing() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api, dry_run=True)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_DRY_RUN}
    assert api.posts == []
    assert api.gets  # the detection still happened


# ── slow is not dead: CodeBuild saturation holds the watchdog back ──────────


def test_a_recent_slow_codebuild_start_means_saturation_and_nothing_is_healed() -> None:
    """Another routed job waited 6 min for a runner and started 2 min ago: CodeBuild is queueing."""
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {
            1: [_job(11)],
            2: [
                _job(
                    21,
                    status="in_progress",
                    minutes_ago=8,
                    started_minutes_ago=2,
                    runner_name="r",
                    run_id=2,
                )
            ],
        },
    )
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.SKIPPED_SATURATED
    assert "job-21" in verdict.detail and "6 min" in verdict.detail
    assert outcomes == {}
    assert api.posts == []


def test_saturation_evidence_inside_a_young_run_still_counts() -> None:
    """A 6-minute wait on a job that started 2 minutes ago, in a run created 9 minutes ago.

    The run is younger than the orphan threshold, so nothing will be done to it, but
    it is older than the saturation line, so it CAN hold a served start that crossed
    it -- and that start holds the watchdog back from run 1.
    """
    young = _run(2, minutes_ago=9, branch="other")
    slow = _job(
        21, status="in_progress", minutes_ago=8, started_minutes_ago=2, runner_name="r", run_id=2
    )
    api = FakeApi({"in_progress": [_run(1), young]}, {1: [_job(11)], 2: [slow]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_SATURATED
    assert _verdict_of(verdicts, 2).verdict == wd.SKIPPED_YOUNG
    assert outcomes == {}
    assert api.posts == []


# ── the orphan's own queue decides before the fleet does ────────────────────


def test_dispatch_queue_strips_the_per_run_suffix_and_keeps_the_fleet_override() -> None:
    """Two runs' jobs on the same project and fleet compare equal; a fleet override does not."""
    linux_a = wd.dispatch_queue([CODEBUILD.format(run=1, attempt=1)])
    linux_b = wd.dispatch_queue([CODEBUILD.format(run=98765, attempt=3)])
    large = wd.dispatch_queue([CODEBUILD.format(run=1, attempt=1) + " instance-size:large"])
    windows = wd.dispatch_queue(["codebuild-example-gha-windows-1-1"])
    assert linux_a == linux_b == frozenset({"codebuild-example-gha-linux"})
    assert large == frozenset({"codebuild-example-gha-linux instance-size:large"})
    assert windows != linux_a
    # Hosted labels are kept verbatim; the suffix rule is for routed labels only.
    assert wd.dispatch_queue(["ubuntu-latest"]) == frozenset({"ubuntu-latest"})
    assert wd.dispatch_queue(["windows-2022-1-1"]) == frozenset({"windows-2022-1-1"})


def _incident_siblings(*, queued_minutes_ago: float = 94) -> list[dict[str, Any]]:
    """The measured shape: fourteen fast-gate jobs queued together, thirteen started in 23 s."""
    started = queued_minutes_ago - 23 / 60
    return [
        _job(
            100 + i,
            status="completed",
            conclusion="success",
            minutes_ago=queued_minutes_ago,
            started_minutes_ago=started,
            completed_minutes_ago=started - 1,
            runner_name=f"r{i}",
        )
        for i in range(13)
    ]


def test_prompt_sibling_starts_on_the_orphans_own_queue_clear_a_slow_start_elsewhere() -> None:
    """The measured incident: thirteen siblings on the orphan's label started in 23 s,
    the fourteenth sat for an hour and a half, and a 7-minute start on ANOTHER fleet
    held it unhealed for six hours.

    The orphan's own queue served thirteen jobs promptly after it queued, so the
    reading for that queue is DISPATCHING and the run is healed. The slow start on the
    large fleet is a fact about that fleet's capacity, not this queue's.

    Negative control: judged without the queue, the fleet-wide reading sees the slow
    start and holds; that is the hold that kept the run stuck.
    """
    orphan = _job(11, minutes_ago=94)
    slow_elsewhere = _job(
        21,
        status="in_progress",
        minutes_ago=10,
        started_minutes_ago=3,
        runner_name="r",
        run_id=2,
        large=True,
    )
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=94), _run(2, minutes_ago=12, branch="other")]},
        {1: [orphan] + _incident_siblings(), 2: [slow_elsewhere]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}

    # The control: the same evidence, read label-blind, is the old hold.
    evidence = wd.DispatchEvidence()
    evidence.absorb([orphan] + _incident_siblings() + [slow_elsewhere], _policy())
    since = _verdict_of(verdicts, 1).orphans[0].queued_at
    blind = wd.resolve_hold(FakeApi({}, {}), _policy(), evidence, since)
    assert blind is not None and blind[0] == wd.SKIPPED_SATURATED
    scoped = wd.resolve_hold(
        FakeApi({}, {}), _policy(), evidence, since, queue=wd.dispatch_queue(orphan["labels"])
    )
    assert scoped is None


def test_a_slow_start_on_the_orphans_own_queue_holds_though_the_rest_of_the_fleet_is_prompt() -> (
    None
):
    """Scoping cuts both ways: a saturated own queue holds even when other fleets are quick.

    Run 2 shares the orphan's queue and served a job after a 6-minute wait; run 3 on
    the large fleet served one in seconds. Label-blind, the slow start would hold
    anyway, so the test also pins that the detail NAMES the own-queue reading -- the
    difference a reader of the tick log needs.
    """
    slow_same_queue = _job(
        21, status="in_progress", minutes_ago=8, started_minutes_ago=2, runner_name="r", run_id=2
    )
    prompt_elsewhere = _job(
        31,
        status="in_progress",
        minutes_ago=3,
        started_minutes_ago=2.8,
        runner_name="r",
        run_id=3,
        large=True,
    )
    api = FakeApi(
        {
            "in_progress": [
                _run(1),
                _run(2, minutes_ago=40, branch="other"),
                _run(3, minutes_ago=5, branch="third"),
            ]
        },
        {1: [_job(11)], 2: [slow_same_queue], 3: [prompt_elsewhere]},
    )
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.SKIPPED_SATURATED
    assert "own CodeBuild queue" in verdict.detail and "job-21" in verdict.detail
    assert outcomes == {}
    assert api.posts == []


def test_a_same_queue_prompt_start_does_not_lift_the_partial_evidence_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unread-run hold guards against a slow start nobody read, and a same-queue
    prompt start does not rule one out.

    Same listing as the partial-hold pin, except the prompt start is on the orphan's
    own queue. The own-queue reading is derived from the starts this sweep read; a
    slow start on that same queue would turn it into SATURATED, and an unread
    30-minute run can hold exactly that start. The retained candidates carry no queue
    attribution that could rule it out, so the sweep holds either way and posts
    nothing. Negative control: the same listing with the unread run gone heals, so the
    hold is the unread run's, not the start's.
    """
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 2)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    # The unread run is affordable to read, and a tick with an orphan reads it
    # (`top_up_unread_evidence`); this pins the hold itself, so the top-up is priced out.
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_TOPUP_READS", 0)
    older = _run(500, minutes_ago=30, status="queued", branch="older")

    same_queue = _job(
        21, status="in_progress", minutes_ago=20, started_minutes_ago=19, runner_name="r", run_id=2
    )
    held = FakeApi(
        {"in_progress": [_run(1), older, _run(2, minutes_ago=20, branch="other")]},
        {1: [_job(11)], 2: [same_queue], 500: []},
    )
    verdicts, outcomes = _sweep(held)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_PARTIAL_EVIDENCE
    assert outcomes == {} and held.posts == []

    other_fleet = dict(same_queue, labels=[same_queue["labels"][0] + " instance-size:large"])
    held_elsewhere = FakeApi(
        {"in_progress": [_run(1), older, _run(2, minutes_ago=20, branch="other")]},
        {1: [_job(11)], 2: [other_fleet], 500: []},
    )
    elsewhere_verdicts, elsewhere_outcomes = _sweep(held_elsewhere)
    assert _verdict_of(elsewhere_verdicts, 1).verdict == wd.SKIPPED_PARTIAL_EVIDENCE
    assert elsewhere_outcomes == {} and held_elsewhere.posts == []

    read_in_full = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=20, branch="other")]},
        {1: [_job(11)], 2: [same_queue]},
    )
    full_verdicts, full_outcomes = _sweep(read_in_full)
    assert _verdict_of(full_verdicts, 1).verdict == wd.ORPHANED
    assert full_outcomes == {1: wd.OUTCOME_HEALED}


def test_a_prompt_own_queue_does_not_clear_the_outage_hold() -> None:
    """The own-queue reading settles the orphan's place in line, not whether the fleet is up.

    Thirteen siblings started 94 minutes ago and nothing on CodeBuild has started in
    the half hour since: the orphan was never in line, but re-running it now would
    queue finished work into what may be a total outage. The outage hold still holds,
    and the tick says so.
    """
    orphan = _job(11, minutes_ago=94)
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=94)]},
        {1: [orphan] + _incident_siblings()},
        evidence=False,
    )
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert outcomes == {} and api.posts == []


def test_the_own_queue_reading_prefers_recent_starts_and_reads_old_ones_one_way() -> None:
    """Recent starts are read like the fleet-wide rule; old ones only say "dispatching".

    An old slow start with nothing recent is SILENT: it does not say the queue is slow
    now, and it does not say the orphan was never in line either, so the label-blind
    rules keep the decision. Old prompt starts alone say DISPATCHING; a recent slow
    start says SATURATED whatever the old ones said.
    """
    policy = _policy()
    since = NOW - timedelta(minutes=94)
    queue = wd.dispatch_queue(_job(11)["labels"])

    old_slow = _job(
        21, status="completed", minutes_ago=90, started_minutes_ago=80, runner_name="r", run_id=2
    )
    evidence = wd.DispatchEvidence()
    evidence.absorb([old_slow], policy)
    assert evidence.own_queue(since, policy, queue) == (wd.OWN_QUEUE_SILENT, None)

    old_prompt = wd.DispatchEvidence()
    old_prompt.absorb(_incident_siblings(), policy)
    assert old_prompt.own_queue(since, policy, queue) == (wd.OWN_QUEUE_DISPATCHING, None)

    recent_slow = _job(
        31, status="in_progress", minutes_ago=9, started_minutes_ago=3, runner_name="r", run_id=3
    )
    old_prompt.absorb([recent_slow], policy)
    reading, slow = old_prompt.own_queue(since, policy, queue)
    assert reading == wd.OWN_QUEUE_SATURATED and slow is not None and slow.job_id == 31

    # Another fleet's starts are not this queue's, however recent or prompt.
    elsewhere = wd.DispatchEvidence()
    elsewhere.absorb(
        [
            _job(
                41,
                status="in_progress",
                minutes_ago=2,
                started_minutes_ago=1.9,
                runner_name="r",
                run_id=4,
                large=True,
            )
        ],
        policy,
    )
    assert elsewhere.own_queue(since, policy, queue) == (wd.OWN_QUEUE_SILENT, None)


def test_orphans_spanning_two_queues_get_the_label_blind_reading() -> None:
    """A run with a Linux orphan and a Windows orphan has no one own queue.

    A prompt start on the Linux queue says nothing about the Windows one, and the hold
    is judged for the run as a whole, so a slow start elsewhere holds it as before.
    """
    windows_orphan = dict(_job(12), labels=["codebuild-example-gha-windows-1-1"])
    slow_elsewhere = _job(
        21,
        status="in_progress",
        minutes_ago=10,
        started_minutes_ago=3,
        runner_name="r",
        run_id=2,
        large=True,
    )
    api = FakeApi(
        {"in_progress": [_run(1, minutes_ago=94), _run(2, minutes_ago=12, branch="other")]},
        {1: [_job(11, minutes_ago=94), windows_orphan] + _incident_siblings(), 2: [slow_elsewhere]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_SATURATED
    assert outcomes == {} and api.posts == []


def test_a_young_runs_start_still_counts_as_evidence_though_the_run_is_not_actionable() -> None:
    """A run the classifier refuses to act on still feeds the evidence set.

    Run 2 is 3 minutes old, so it is SKIPPED_YOUNG and nothing will be done to it,
    but its served start is what the hold on run 1 is judged against. It is also
    younger than the saturation line, so it cannot hold a wait that crossed it and can
    only ever report the fleet dispatching -- which is why the evidence reserve
    prefers runs old enough to report the other way.
    """
    young = _run(2, minutes_ago=3, branch="other")
    prompt = _job(
        21, status="in_progress", minutes_ago=3, started_minutes_ago=2.8, runner_name="r", run_id=2
    )
    api = FakeApi({"in_progress": [_run(1), young]}, {1: [_job(11)], 2: [prompt]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert _verdict_of(verdicts, 2).verdict == wd.SKIPPED_YOUNG
    assert outcomes == {1: wd.OUTCOME_HEALED}


def _jobs_change_on_later_reads(api: FakeApi, run_id: int, later: list[dict[str, Any]]) -> None:
    """The first jobs read of ``run_id`` answers as configured; every later one answers ``later``."""
    original_get = api.get
    seen = {"n": 0}

    def get(path: str) -> Any:
        if f"/actions/runs/{run_id}/jobs" in path:
            seen["n"] += 1
            if seen["n"] > 1:
                api.gets.append(path)
                return {"total_count": len(later), "jobs": later}
        return original_get(path)

    api.get = get  # type: ignore[method-assign]


def test_dispatch_evidence_is_re_read_before_the_cancel_and_a_fresh_saturation_holds() -> None:
    """At the top of the tick the sibling had started within 20 s (dispatching); by the time
    the cancel is about to go out it shows a 6-minute wait. The stale evidence must not
    cancel a run whose queued job the fleet may still pick up."""
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=2.5,
        started_minutes_ago=2.2,
        runner_name="r",
        run_id=2,
    )
    slow = _job(
        21, status="in_progress", minutes_ago=8, started_minutes_ago=2, runner_name="r", run_id=2
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [prompt]},
    )
    _jobs_change_on_later_reads(api, 2, [slow])
    logged: list[str] = []
    clock = _Clock()
    verdicts, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_SATURATED
    assert api.posts == []
    assert outcomes == {}
    assert any("re-checked before the cancel" in line for line in logged)


def test_the_pre_cancel_hold_is_judged_from_the_freshly_read_orphans_not_the_stale_set() -> None:
    """At the top of the tick the run had one orphan (queued 60 min ago) and a sibling start
    25 min ago cleared the hold. By the re-read a second matrix job has crossed the
    threshold (queued 20 min ago); judged from IT, that start proves nothing, and the run
    is held instead of cancelled."""
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
        {1: [_job(11, minutes_ago=60)], 3: [started_between]},
        evidence=False,
    )
    _jobs_change_on_later_reads(api, 1, [_job(11, minutes_ago=60), _job(12, minutes_ago=20)])
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert [o.job_id for o in verdict.orphans] == [11, 12]  # the summary shows what was read
    assert api.posts == []
    assert outcomes == {}


def test_the_evidence_sweep_runs_before_the_runs_re_read_so_a_hand_rerun_during_it_is_seen() -> (
    None
):
    """A human re-runs the target while the fleet is being re-read. The target's own re-read
    comes AFTER that sweep, immediately before the cancel, so it sees the new attempt and
    leaves the run alone instead of cancelling on a verdict that went stale during the sweep."""
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=2.5,
        started_minutes_ago=2.2,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [prompt]},
    )
    original_get = api.get
    reads = {"n": 0}

    def hand_rerun_during_the_sweep(path: str) -> Any:
        if "/actions/runs/2/jobs" in path:
            reads["n"] += 1
            if reads["n"] == 2:  # the pre-cancel evidence sweep, not the top-of-tick scan
                api.run_overrides[1] = _run(1, attempt=2)
        return original_get(path)

    api.get = hand_rerun_during_the_sweep  # type: ignore[method-assign]
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_SUPERSEDED}
    assert api.posts == []


def test_evidence_that_cannot_be_re_read_before_the_cancel_fails_closed() -> None:
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=2.5,
        started_minutes_ago=2.2,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [prompt]},
    )
    original_get = api.get
    seen = {"n": 0}

    def get(path: str) -> Any:
        if "/actions/runs/2/jobs" in path:
            seen["n"] += 1
            if seen["n"] > 1:
                raise wd.ApiError(502, "bad gateway")
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert "could not be re-read" in verdict.detail
    assert api.posts == []
    assert outcomes == {1: wd.OUTCOME_EVIDENCE_REREAD_DEFERRED}


def test_a_rate_limit_on_the_fresh_evidence_read_has_an_accurate_outcome() -> None:
    """A rate limit is a CONDITION, not a one-off, so the deferral it causes reds the tick.

    The 502 case above stays green and deferrable: nothing was touched and the next
    tick re-reads. A rate limit is what killed the tick in the incident, and while it
    persists every tick would defer and every tick would look healthy, with the orphan
    still parking every later push behind it.
    """
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]})
    original_get = api.get
    reads = {"in_progress": 0}

    def get(path: str) -> Any:
        # Counted per FIRST page, so the limit lands on the fresh-evidence
        # RE-READ rather than on a later page of the initial gather: the gather
        # pages until an empty page, because a short page is not this endpoint's
        # tail, so "the second listing call" is not the same thing as "the second
        # listing". Matched with a boundary -- a plain `page=1` substring also
        # matches `per_page=100`, which is on every one of these calls.
        if re.search(r"/actions/runs\?.*status=in_progress", path) and re.search(
            r"[?&]page=1(?![0-9])", path
        ):
            reads["in_progress"] += 1
            if reads["in_progress"] > 1:
                raise wd.ApiError(
                    403,
                    "API rate limit exceeded for installation ID 12345",
                    remaining="0",
                )
        return original_get(path)

    api.get = get  # type: ignore[method-assign]
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert outcomes == {1: wd.OUTCOME_ABORTED_RATE_LIMITED}
    assert wd.OUTCOME_ABORTED_RATE_LIMITED in wd.FAILED_OUTCOMES
    # The plain deferral stays green: that is the 502 path, pinned above.
    assert wd.OUTCOME_EVIDENCE_REREAD_DEFERRED not in wd.FAILED_OUTCOMES
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert wd.OUTCOME_ABORTED_RATE_LIMITED in summary
    assert wd.OUTCOME_NOT_ATTEMPTED not in summary


def test_evidence_still_fresh_at_cancel_time_heals_as_before() -> None:
    prompt = _job(
        21,
        status="in_progress",
        minutes_ago=2.5,
        started_minutes_ago=2.2,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [prompt]},
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}
    # The fleet was re-read exactly once for the cancel, not once per run.
    assert sum("/actions/runs/2/jobs" in path for path in api.gets) == 2


def test_a_prompt_recent_codebuild_start_is_not_saturation() -> None:
    """The sibling waited 20 s: CodeBuild is dispatching normally, so the queued job is dead."""
    started = _job(
        21,
        status="in_progress",
        minutes_ago=2.5,
        started_minutes_ago=2.2,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=40, branch="other")]},
        {1: [_job(11)], 2: [started]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_an_old_slow_start_is_outside_the_saturation_lookback() -> None:
    """A slow start 45 min ago says nothing about CodeBuild now."""
    old_slow = _job(
        21, status="completed", minutes_ago=55, started_minutes_ago=45, runner_name="r", run_id=2
    )
    api = FakeApi(
        {"in_progress": [_run(1), _run(2, minutes_ago=60, branch="other")]},
        {1: [_job(11)], 2: [old_slow]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_dispatch_evidence_ignores_hosted_never_started_and_carried_over_jobs() -> None:
    since = NOW - timedelta(minutes=60)
    hosted_slow = _job(
        31,
        status="in_progress",
        minutes_ago=20,
        started_minutes_ago=2,
        runner_name="r",
        codebuild=False,
    )
    # Carried over from an earlier attempt: created (at the re-run) AFTER it started.
    carried = _job(32, status="completed", minutes_ago=2, started_minutes_ago=30, runner_name="r")
    never_started = _job(33, minutes_ago=20)
    evidence = wd.DispatchEvidence()
    evidence.absorb([hosted_slow, carried, never_started], _policy())
    assert evidence.inconclusive(since, _policy()) and not evidence.saturated(since, _policy())
    prompt = _job(34, status="in_progress", minutes_ago=3, started_minutes_ago=2.8, runner_name="r")
    evidence.absorb([prompt], _policy())
    assert not evidence.inconclusive(since, _policy()) and not evidence.saturated(since, _policy())
    slow = _job(35, status="in_progress", minutes_ago=9, started_minutes_ago=3, runner_name="r")
    evidence.absorb([slow], _policy())
    slowest = evidence.slowest(since, _policy())
    assert (
        slowest is not None and slowest.job_id == 35 and slowest.queued_for == timedelta(minutes=6)
    )
    # Relative to an orphan that queued AFTER both starts, neither counts.
    assert evidence.inconclusive(NOW - timedelta(minutes=1), _policy())
    # Judged later in a slow tick, a start that has aged past the lookback drops out.
    later = _policy(now=NOW + timedelta(minutes=28))
    assert evidence.inconclusive(since, later)


def test_a_prompt_start_from_before_the_orphan_queued_is_not_evidence_the_fleet_is_alive() -> None:
    """The onset of an outage: the fleet was fine 25 min ago, the orphan queued 20 min ago, nothing since."""
    orphan_run = _run(1, minutes_ago=60)
    healthy_before = _run(
        2,
        minutes_ago=30,
        status="completed",
        conclusion="success",
        updated_minutes_ago=24,
        branch="other",
    )
    started_before = _job(
        21,
        status="completed",
        conclusion="success",
        minutes_ago=25.5,
        started_minutes_ago=25.2,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [orphan_run], "completed": [healthy_before]},
        {1: [_job(11, minutes_ago=20)], 2: [started_before]},
        evidence=False,
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert outcomes == {}
    assert api.posts == []


def test_a_prompt_start_after_the_orphan_queued_clears_the_hold() -> None:
    orphan_run = _run(1, minutes_ago=60)
    healthy_after = _run(
        2,
        minutes_ago=12,
        status="completed",
        conclusion="success",
        updated_minutes_ago=8,
        branch="other",
    )
    started_after = _job(
        21,
        status="completed",
        conclusion="success",
        minutes_ago=11,
        started_minutes_ago=10.7,
        runner_name="r",
        run_id=2,
    )
    api = FakeApi(
        {"in_progress": [orphan_run], "completed": [healthy_after]},
        {1: [_job(11, minutes_ago=20)], 2: [started_after]},
        evidence=False,
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_no_recent_codebuild_start_anywhere_is_inconclusive_and_heals_nothing() -> None:
    """A total fleet outage looks exactly like an orphan from the queued side: hold and say so."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, evidence=False)
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    assert "rollback" in verdict.detail
    assert outcomes == {}
    assert api.posts == []
    assert any("status=completed" in p for p in api.gets)  # the completed sample was consulted


def test_a_prompt_start_in_a_recently_completed_run_is_enough_evidence() -> None:
    """No live run shows a start, but the newest completed run finished a CodeBuild job promptly."""
    api = FakeApi(
        {"in_progress": [_run(1)]}, {1: [_job(11)]}
    )  # default evidence comes from a completed run
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEALED}


def test_stale_completed_runs_do_not_hide_recent_fleet_evidence() -> None:
    stale = [
        _run(
            100 + i,
            minutes_ago=50 + i,
            status="completed",
            conclusion="success",
            updated_minutes_ago=40 + i,
            branch=f"stale-{i}",
            workflow="ci.yml",
        )
        for i in range(wd.COMPLETED_SAMPLE)
    ]
    recent = _run(
        200,
        minutes_ago=12,
        status="completed",
        conclusion="success",
        updated_minutes_ago=3,
        branch="recent",
        workflow="fast-gate.yml",
    )
    api = FakeApi(
        {"in_progress": [_run(1, workflow="build.yml")], "completed": stale + [recent]},
        {
            1: [_job(11)],
            **{
                int(run["id"]): [_job(1000 + i, run_id=int(run["id"]))]
                for i, run in enumerate(stale)
            },
            200: [
                _job(
                    2001,
                    status="completed",
                    conclusion="success",
                    minutes_ago=11,
                    started_minutes_ago=10.7,
                    runner_name="recent-runner",
                    run_id=200,
                )
            ],
        },
        evidence=False,
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}
    completed_listings = [path for path in api.gets if "status=completed" in path]
    assert "/workflows/ci.yml/runs?" in completed_listings[0]
    assert any("/workflows/fast-gate.yml/runs?" in path for path in completed_listings)
    assert not any(f"/runs/{run['id']}/jobs" in path for run in stale for path in api.gets)


def test_completed_run_reads_stay_within_the_sample_bound() -> None:
    completed = [
        _run(
            300 + i,
            minutes_ago=10 - i / 10,
            status="completed",
            conclusion="success",
            updated_minutes_ago=2,
            branch=f"recent-{i}",
        )
        for i in range(wd.COMPLETED_SAMPLE + 5)
    ]
    api = FakeApi(
        {"completed": completed},
        {
            int(run["id"]): [_job(3000 + i, run_id=int(run["id"]))]
            for i, run in enumerate(completed)
        },
        evidence=False,
    )
    evidence = wd.DispatchEvidence()
    wd.sample_completed_runs(api, _policy(), evidence)
    completed_ids = {int(run["id"]) for run in completed}
    reads = [
        path
        for path in api.gets
        if "/jobs" in path and int(path.split("/runs/", 1)[1].split("/", 1)[0]) in completed_ids
    ]
    assert len(reads) == wd.COMPLETED_SAMPLE


def test_the_completed_sample_is_not_read_when_nothing_is_orphaned() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11, status="completed", codebuild=False)]})
    _sweep(api)
    assert not any("status=completed" in p for p in api.gets)


# ── things that look stuck but are not ──────────────────────────────────────


def test_a_queued_hosted_job_is_not_an_orphan() -> None:
    """Only CodeBuild labels are run-attempt specific; a hosted queue is just slow."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11, codebuild=False)]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEALTHY
    assert outcomes == {}
    assert api.posts == []


def test_a_codebuild_job_queued_under_the_threshold_is_left_alone() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11, minutes_ago=14)]})
    verdicts, _ = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEALTHY
    assert api.posts == []


def test_a_running_codebuild_job_is_not_an_orphan() -> None:
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11, status="in_progress")]})
    verdicts, _ = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEALTHY
    assert api.posts == []


def test_a_young_run_is_never_actionable() -> None:
    """Its jobs are still read (they may carry saturation evidence), but nothing is done to it."""
    api = FakeApi({"in_progress": [_run(1, minutes_ago=5)]}, {1: [_job(11, minutes_ago=5)]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_YOUNG
    assert outcomes == {}
    assert api.posts == []


def test_a_run_with_no_jobs_is_waiting_on_its_group_not_on_a_runner() -> None:
    api = FakeApi({"pending": [_run(1, minutes_ago=45, status="pending")]}, {1: []})
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.WAITING_ON_GROUP
    assert "concurrency group" in verdict.detail
    assert outcomes == {}
    assert api.posts == []


def test_a_jobless_run_under_the_group_threshold_is_just_healthy() -> None:
    api = FakeApi({"pending": [_run(1, minutes_ago=20, status="pending")]}, {1: []})
    verdicts, _ = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEALTHY


# ── guard rails ─────────────────────────────────────────────────────────────


def test_fork_runs_are_reported_but_never_touched() -> None:
    api = FakeApi({"in_progress": [_run(1, fork=True)]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_FORK
    assert outcomes == {}
    assert api.posts == []


def test_a_head_repository_with_another_name_counts_as_a_fork() -> None:
    run = _run(1)
    run["head_repository"] = {"fork": False, "full_name": "Other-Org/example-repo"}
    assert wd.is_fork_run(run, REPO)
    run["head_repository"] = {"fork": False, "full_name": REPO.upper()}
    assert not wd.is_fork_run(run, REPO)
    run["head_repository"] = None
    assert not wd.is_fork_run(run, REPO)


def test_the_attempt_cap_stops_a_run_that_keeps_orphaning() -> None:
    """Each intervention bumps the attempt, so the cap bounds interventions per run."""
    api = FakeApi(
        {"in_progress": [_run(1, attempt=3), _run(2, attempt=2, branch="other")]},
        {1: [_job(11, attempt=3)], 2: [_job(21, attempt=2)]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_ATTEMPT_CAP
    assert _verdict_of(verdicts, 2).verdict == wd.ORPHANED
    assert outcomes == {2: wd.OUTCOME_HEALED}
    assert all("/runs/2/" in path for path in api.posts)


def test_the_per_invocation_cap_leaves_the_rest_for_the_next_tick_oldest_first() -> None:
    runs = [_run(i, minutes_ago=100 - i, branch=f"pr-{i}") for i in range(1, 8)]  # 1 is the oldest
    api = FakeApi(
        {"in_progress": list(reversed(runs))},  # the API lists newest first
        {i: [_job(i * 10, run_id=i)] for i in range(1, 8)},
    )
    _, outcomes = _sweep(api, max_runs=5)
    healed = sorted(run_id for run_id, outcome in outcomes.items() if outcome == wd.OUTCOME_HEALED)
    assert healed == [1, 2, 3, 4, 5]
    assert outcomes[6] == outcomes[7] == wd.OUTCOME_NOT_ATTEMPTED
    assert len(_posts(api, "/cancel")) == 5


def test_a_run_listed_under_two_statuses_is_inspected_once() -> None:
    run = _run(1)
    api = FakeApi({"in_progress": [run], "queued": [run]}, {1: [_job(11)]})
    verdicts, _ = _sweep(api)
    assert [v.run_id for v in verdicts] == [1]
    assert len(_posts(api, "/cancel")) == 1


def test_the_candidate_listing_is_repo_wide_and_paginated() -> None:
    """One paginated repo-wide listing per status covers every workflow; no newest-N
    truncation, because an orphan is an OLD run at the tail of the newest-first pages.
    Negative control: reverted per-workflow code issues `/workflows/<wf>/runs` calls
    and never the repo-wide `/actions/runs?status=` call this asserts."""
    runs = [_run(i, minutes_ago=200 - i) for i in range(1, 131)]
    api = FakeApi({"in_progress": list(reversed(runs))}, {})
    listed = wd.list_all_candidate_runs(api, REPO)
    # Every watched run is examined -- all 130, not the newest 50 -- so the oldest
    # (the orphans) are never truncated away.
    assert sorted(int(r["id"]) for r in listed) == list(range(1, 131))
    in_progress_pages = [
        p
        for p in api.gets
        if p.startswith(f"repos/{REPO}/actions/runs?") and "status=in_progress" in p
    ]
    # 100, then 30, then the empty page that ends it: a page below `per_page` is
    # not the tail of this endpoint (see the short-page test below), so only an
    # empty one stops paging.
    assert len(in_progress_pages) == 3
    assert "per_page=100" in in_progress_pages[0] and "page=1" in in_progress_pages[0]
    # The second page keeps the page size: `page` is an offset in units of
    # `per_page`, so a 20-run second page would re-read runs 21-40 instead.
    assert "per_page=100" in in_progress_pages[1] and "page=2" in in_progress_pages[1]
    assert "per_page=100" in in_progress_pages[2] and "page=3" in in_progress_pages[2]
    # Every listing is the repo-wide endpoint, never a per-workflow one.
    assert not any("/actions/workflows/" in p and "/runs?status=" in p for p in api.gets)


def test_a_short_page_does_not_end_the_repo_wide_listing() -> None:
    """The endpoint returns pages below `per_page` mid-listing, so a short page is
    not the tail.

    Measured against `status=queued` on this repository: 98, then 100, then 100,
    then 99, while `total_count` stands at 927 and a six-hour-old `fast-gate.yml`
    orphan holding `main`'s concurrency slot sits on page seven.

    The negative control below is the reverted rule -- stop at the first page
    shorter than `PAGE_SIZE` -- run over the same pages: it reads page one only and
    never sees the deep orphan.
    """
    pages = [
        [_run(1000 + i, minutes_ago=5) for i in range(98)],
        [_run(2000 + i, minutes_ago=30) for i in range(100)],
        [_run(3000 + i, minutes_ago=200) for i in range(99)] + [_run(7, minutes_ago=370)],
    ]

    def serve(path: str) -> Any:
        page = int(dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))["page"])
        return {"workflow_runs": pages[page - 1] if page <= len(pages) else []}

    api = FakeApi({}, {})
    api.get = serve  # type: ignore[method-assign]
    collected = list(
        wd._iter_repo_runs(api, REPO, status="queued", max_pages=wd.REPO_LISTING_MAX_PAGES)
    )
    assert len(collected) == 298
    assert 7 in {int(run["id"]) for run in collected}

    control: list[dict[str, Any]] = []
    for index, batch in enumerate(pages, start=1):
        control.extend(batch)
        if len(batch) < wd.PAGE_SIZE:  # the reverted rule
            break
        assert index  # the loop is exercised, not short-circuited by an empty first page
    assert len(control) == 98
    assert 7 not in {int(run["id"]) for run in control}


def test_the_workflow_scoped_listing_does_not_end_on_a_short_page_either() -> None:
    """The same rule, at the second site that reads this index.

    ``list_runs`` is workflow-scoped, but it queries the SAME status-filtered runs
    index, which returns pages below ``per_page`` mid-listing. A short page there is
    no more the tail than it is repo-wide, so the cap or an empty page ends it.

    Negative control: the reverted rule -- break at the first page shorter than
    ``PAGE_SIZE`` -- over the same pages keeps 3 of the 5 runs, so a sample of 5
    would be drawn from a third of its index.
    """
    short_then_more = [
        [_run(1, minutes_ago=1), _run(2, minutes_ago=2), _run(3, minutes_ago=3)],
        [_run(4, minutes_ago=4), _run(5, minutes_ago=5)],
    ]

    def serve(path: str) -> Any:
        page = int(dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))["page"])
        return {"workflow_runs": short_then_more[page - 1] if page <= 2 else []}

    api = FakeApi({}, {})
    api.get = serve  # type: ignore[method-assign]
    listed = wd.list_runs(api, REPO, "ci.yml", status="completed", cap=5)
    assert [int(run["id"]) for run in listed] == [5, 4, 3, 2, 1]

    control: list[dict[str, Any]] = []
    for batch in short_then_more:
        control.extend(batch)
        if len(batch) < wd.PAGE_SIZE:  # the reverted rule
            break
    assert len(control) == 3


def test_the_jobs_listing_does_not_end_on_a_short_page_and_bounds_by_total_count() -> None:
    """Third site reading a paginated listing, with a cheaper tail than the runs index.

    ``filter=latest`` is the same shape of post-paging filter as ``status=``, so a
    re-run run's jobs page can come back short mid-listing. A job dropped for that
    reason is a queued fleet job the tick never sees, and queued fleet jobs ARE the
    orphan evidence. Unlike the runs index this payload carries ``total_count``, so
    the tail is known exactly: reading stops there, with no extra empty-page call on
    top of the one page a normal run needs.

    Negative control: the reverted rule -- stop at the first page below ``PAGE_SIZE``
    -- over the same pages keeps 2 of the 3 jobs and loses the queued fleet job.
    """
    fleet_job = _job(999, status="queued", codebuild=True)
    pages = [
        [_job(1), _job(2)],  # short, yet not the tail
        [fleet_job],
    ]

    calls: list[str] = []

    def serve(path: str) -> Any:
        calls.append(path)
        page = int(dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))["page"])
        return {"total_count": 3, "jobs": pages[page - 1] if page <= len(pages) else []}

    api = FakeApi({}, {})
    api.get = serve  # type: ignore[method-assign]
    listed = wd.list_jobs(api, REPO, 1)
    assert [int(job["id"]) for job in listed] == [1, 2, 999]
    # Exactly the pages that held jobs: ``total_count`` ends it, so no empty-page probe.
    assert len(calls) == 2

    control: list[dict[str, Any]] = []
    for batch in pages:
        control.extend(batch)
        if len(batch) < wd.PAGE_SIZE:  # the reverted rule
            break
    assert len(control) == 2
    assert 999 not in {int(job["id"]) for job in control}


def test_a_short_page_does_not_end_the_branch_listing_either() -> None:
    """Fourth site, and the one where a short page silently refuses a safe re-run.

    ``newest_run_id_for_branch`` pages until it has SEEN the run being judged; that
    sighting is the proof the listing reached far enough to trust its answer. Reading
    a short page as the tail raises ``LookupInconclusive`` while the judged run sits
    on the very next page, which declines a re-run the evidence allows and leaves a
    cancelled run behind a green tick.

    Negative control: the reverted rule over the same pages ends at page one and the
    lookup raises.
    """
    judged = wd.RunVerdict(
        run_id=42,
        run_attempt=1,
        head_branch="main",
        head_repo=REPO,
        event="push",
        status="in_progress",
        url="https://example.invalid/runs/42",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
    )
    pages = [
        [_run(90, branch="main", event="push")],  # short page, newest first
        [_run(42, branch="main", event="push")],  # the judged run
    ]

    def serve(path: str) -> Any:
        page = int(dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))["page"])
        return {"workflow_runs": pages[page - 1] if page <= len(pages) else []}

    api = FakeApi({}, {})
    api.get = serve  # type: ignore[method-assign]
    assert wd.newest_run_id_for_branch(api, REPO, judged) == 90

    control_pages = []
    for batch in pages:
        control_pages.append(batch)
        if len(batch) < wd.BRANCH_LISTING_DEPTH:  # the reverted rule
            break
    assert len(control_pages) == 1
    assert 42 not in {int(run["id"]) for batch in control_pages for run in batch}


def test_a_truncated_live_listing_fails_the_tick_instead_of_only_warning() -> None:
    """A tick that could not look as deep as an orphan sits is not a clean tick.

    The depth needed to reach an orphan is the run arrival rate times the orphan's
    age, so a cap set past the whole measured set today re-truncates short of an
    orphan under load growth. The paging logs a ``::warning::``, which nobody is
    required to read, so truncation also carries a tick-level verdict and an outcome
    inside ``FAILED_OUTCOMES`` -- the same device the rate-limit abort uses.

    Negative control: the ``::warning::`` alone, which is what shipped before, leaves
    the outcome set empty and the tick exits 0.
    """
    full_page = [_run(1000 + i, minutes_ago=5) for i in range(wd.PAGE_SIZE)]

    def serve(path: str) -> Any:
        # More runs exist than the cap admits, and the listing says so.
        return {"total_count": 50 * wd.PAGE_SIZE, "workflow_runs": list(full_page)}

    api = FakeApi({}, {})
    api.get = serve  # type: ignore[method-assign]
    logged: list[str] = []
    _runs, aborted, truncated = wd.gather_all_candidate_runs(api, REPO, log=logged.append)
    assert aborted is None
    # Two entries, not three: `pending` shares the walk but not the sink.
    assert [m.split(" run listing")[0] for m in truncated] == ["in_progress", "queued"]
    assert all("of 5000 run(s)" in m for m in truncated)
    assert any(line.startswith("::warning::") for line in logged)

    # The reported form: a marker verdict plus an outcome that fails the tick.
    outcomes = {wd.LISTING_TRUNCATED_MARKER_ID: wd.OUTCOME_LISTING_TRUNCATED}
    assert wd.FAILED_OUTCOMES & set(outcomes.values())

    # Negative control: warning only, which is what shipped before this change.
    control_outcomes: dict[int, str] = {}
    assert not (wd.FAILED_OUTCOMES & set(control_outcomes.values()))


def _ceilinged_listing(queued: list[dict[str, Any]]) -> tuple[FakeApi, list[str]]:
    """A runs index that serves ``queued`` the way GitHub does past its ceiling.

    Newest first, filtered by ``created=<=…`` when given, ``total_count`` for the
    filtered set, and an EMPTY page past result ``REPO_LISTING_RESULT_CEILING``.
    """
    api = FakeApi({}, {})
    queries: list[str] = []

    def serve(path: str) -> Any:
        queries.append(path)
        params = dict(urllib.parse.parse_qsl(path.partition("?")[2]))
        if params.get("status") != "queued":
            return {"total_count": 0, "workflow_runs": []}
        rows = sorted(queued, key=lambda run: run["created_at"], reverse=True)
        created = params.get("created")
        if created is not None:
            assert created.startswith("<="), created
            rows = [run for run in rows if run["created_at"] <= created[2:]]
        page, size = int(params["page"]), int(params["per_page"])
        start = (page - 1) * size
        if start >= wd.REPO_LISTING_RESULT_CEILING:
            return {"total_count": 0, "workflow_runs": []}
        return {"total_count": len(rows), "workflow_runs": rows[start : start + size]}

    api.get = serve  # type: ignore[method-assign]
    return api, queries


def _queued_runs(count: int, *, same_second: bool = False) -> list[dict[str, Any]]:
    return [
        _run(10_000 + i, status="queued", minutes_ago=30 if same_second else 30 + i / 60)
        for i in range(count)
    ]


def test_a_queued_listing_past_the_ceiling_is_narrowed_by_created_window() -> None:
    """1,500 queued runs: one listing reads 1,000, so the oldest -- where an orphan
    sits -- were never classified and every tick went red (36640617372: "queued run
    listing reached 910 of 921"). The second window reads past the ceiling."""
    queued = _queued_runs(1500)
    api, queries = _ceilinged_listing(queued)
    logged: list[str] = []
    runs, aborted, truncated = wd.gather_all_candidate_runs(api, REPO, log=logged.append)
    assert aborted is None
    assert truncated == []
    assert {int(run["id"]) for run in runs} == {int(run["id"]) for run in queued}
    oldest = min(queued, key=lambda run: run["created_at"])
    assert runs[0]["id"] == oldest["id"]
    assert any("created=%3C%3D" in query for query in queries)


def test_a_queued_listing_under_the_ceiling_reads_one_window() -> None:
    api, queries = _ceilinged_listing(_queued_runs(300))
    runs, _aborted, truncated = wd.gather_all_candidate_runs(api, REPO, log=lambda _l: None)
    assert len(runs) == 300 and truncated == []
    assert not any("created=" in query for query in queries)


def test_a_window_that_cannot_narrow_still_fails_the_tick() -> None:
    # 1,200 runs in one second: the second window starts at that same second and
    # reads the same 1,000 again, so narrowing cannot reach the rest.
    api, _queries = _ceilinged_listing(_queued_runs(1200, same_second=True))
    _runs, _aborted, truncated = wd.gather_all_candidate_runs(api, REPO, log=lambda _l: None)
    assert len(truncated) == 1
    assert truncated[0].startswith("queued run listing")
    assert truncated[0].endswith("after 2 created window(s)")


def test_the_window_cap_bounds_the_quota_and_reports_what_it_could_not_read() -> None:
    windows = wd.REPO_LISTING_MAX_WINDOWS
    api, queries = _ceilinged_listing(_queued_runs(wd.REPO_LISTING_RESULT_CEILING * windows + 500))
    _runs, _aborted, truncated = wd.gather_all_candidate_runs(api, REPO, log=lambda _l: None)
    assert len(truncated) == 1
    assert truncated[0].endswith(f"after {windows} created window(s)")
    queued_reads = [query for query in queries if "status=queued" in query]
    assert len(queued_reads) <= windows * (wd.REPO_LISTING_MAX_PAGES + 1)


def test_the_repo_wide_paging_respects_its_page_cap() -> None:
    """A listing that always returns a full page would page forever; the cap stops it.
    Negative control: reverted code has no `_iter_repo_runs` and no repo-wide page cap,
    so this exercises a path that does not exist there."""
    api = FakeApi({}, {})
    full_page = [{"id": i, "path": ".github/workflows/ci.yml"} for i in range(1, wd.PAGE_SIZE + 1)]
    api.get = lambda _path: {"workflow_runs": full_page}  # type: ignore[method-assign]
    collected = list(
        wd._iter_repo_runs(api, REPO, status="in_progress", max_pages=wd.REPO_LISTING_MAX_PAGES)
    )
    assert len(collected) == wd.PAGE_SIZE * wd.REPO_LISTING_MAX_PAGES


def test_cancelled_recovery_reaches_a_deep_orphan_inside_the_reachable_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recently cancelled, old-created run sits on page nine and is recovered.

    The recovery cap equals the live one, because ``REPO_LISTING_RESULT_CEILING``
    bounds both at the API's reachable window: a deeper cap names pages this
    endpoint never serves. Two controls: a 2-page cap cannot reach page nine, and a
    run past the ceiling cannot be reached at ANY cap -- which is why truncation is
    reported
    rather than silently tolerated.
    """

    def api_with_deep_orphan() -> FakeApi:
        newer_created = [
            _run(
                1000 + i,
                minutes_ago=1 + i,
                status="completed",
                conclusion="cancelled",
                updated_minutes_ago=200,
                branch=f"filler-{i}",
            )
            for i in range(wd.PAGE_SIZE * (wd.REPO_LISTING_MAX_PAGES - 2))
        ]
        orphan = _run(
            1,
            minutes_ago=2_000,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=5,
            branch="deep-orphan",
        )
        return FakeApi(
            {"cancelled": newer_created + [orphan]},
            {1: [_cancelled_orphan_job(11, run_id=1)]},
            newest_by_branch={"deep-orphan": 1},
            evidence=False,
        )

    recovery_cap = wd.RECOVERY_LISTING_MAX_PAGES
    # This cap equals the live-listing one: REPO_LISTING_RESULT_CEILING bounds BOTH
    # at the API's reachable window, so a deeper recovery cap would name pages this
    # endpoint never serves. What this test pins is that a recently cancelled run
    # whose CREATION is old sits deep in the index and is recovered from inside that
    # window.
    assert (
        recovery_cap == wd.REPO_LISTING_MAX_PAGES <= wd.REPO_LISTING_RESULT_CEILING // wd.PAGE_SIZE
    )
    monkeypatch.setattr(wd, "RECOVERY_LISTING_MAX_PAGES", 2)
    control = api_with_deep_orphan()
    _, control_outcomes = _sweep(control)
    assert 1 not in control_outcomes
    assert not _posts(control, "/rerun")
    monkeypatch.setattr(wd, "RECOVERY_LISTING_MAX_PAGES", recovery_cap)
    api = api_with_deep_orphan()
    _, outcomes = _sweep(api)
    assert outcomes[1] == wd.OUTCOME_RECOVERED
    assert _posts(api, "/rerun") == [f"repos/{REPO}/actions/runs/1/rerun"]
    assert any("status=cancelled" in path and "page=9" in path for path in api.gets)

    # Second control, and the honest bound: a run past REPO_LISTING_RESULT_CEILING
    # cannot be reached at any cap, because the endpoint serves an empty page there
    # rather than the next result. Raising the cap does nothing, which is why the
    # operator-facing remedy says to narrow the listing.
    reachable = wd.REPO_LISTING_RESULT_CEILING // wd.PAGE_SIZE
    assert wd.REPO_LISTING_MAX_PAGES == reachable
    assert wd.RECOVERY_LISTING_MAX_PAGES == reachable


def test_repo_listing_warns_only_when_its_own_total_count_is_not_reached() -> None:
    """Reach is judged against ``total_count``, not against the page cap.

    The cap cannot be the signal, because at ``REPO_LISTING_RESULT_CEILING`` the
    endpoint serves an EMPTY page: measured on this repository, page 11 of
    `status=queued&per_page=100` returns no runs and `total_count: 0`, not a 422. A
    walk that ended on that page looks exactly like one that reached the tail, so
    only the count says which happened.
    """
    full_page = [{"id": i, "path": ".github/workflows/ci.yml"} for i in range(wd.PAGE_SIZE)]
    saturated = FakeApi({}, {})
    # 500 exist, the cap admits 200: a shortfall the cap alone would also have caught.
    saturated.get = lambda _path: {  # type: ignore[method-assign]
        "total_count": 5 * wd.PAGE_SIZE,
        "workflow_runs": full_page,
    }
    saturated_log: list[str] = []
    saturated_runs = list(
        wd._iter_repo_runs(
            saturated,
            REPO,
            status="in_progress",
            max_pages=2,
            log=saturated_log.append,
        )
    )
    assert len(saturated_runs) == 2 * wd.PAGE_SIZE
    assert any("200 of 500" in line and "in_progress" in line for line in saturated_log)

    # The ceiling case the page cap CANNOT catch: the walk ends on an empty page
    # well inside its cap, and only `total_count` shows the tail went unread.
    def at_ceiling(path: str) -> Any:
        page = int(dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))["page"])
        if page <= 2:
            return {"total_count": 9 * wd.PAGE_SIZE, "workflow_runs": full_page}
        return {"total_count": 0, "workflow_runs": []}

    ceiling = FakeApi({}, {})
    ceiling.get = at_ceiling  # type: ignore[method-assign]
    ceiling_log: list[str] = []
    ceiling_runs = list(
        wd._iter_repo_runs(
            ceiling,
            REPO,
            status="queued",
            max_pages=wd.REPO_LISTING_MAX_PAGES,
            log=ceiling_log.append,
        )
    )
    assert len(ceiling_runs) == 2 * wd.PAGE_SIZE
    assert any("200 of 900" in line for line in ceiling_log)
    # Negative control: the reverted rule fired on `page == max_pages`, which this
    # walk never reaches, so it said nothing at all about an unread tail.
    assert len(ceiling_runs) // wd.PAGE_SIZE < wd.REPO_LISTING_MAX_PAGES

    short = FakeApi({"in_progress": [_run(1)]}, {})
    short_log: list[str] = []
    short_runs = list(
        wd._iter_repo_runs(
            short,
            REPO,
            status="in_progress",
            max_pages=2,
            log=short_log.append,
        )
    )
    assert [run["id"] for run in short_runs] == [1]
    assert not short_log


def test_a_shortfall_from_churn_warns_but_does_not_fail_the_tick() -> None:
    """``total_count`` is a page-one snapshot of a set that MUTATES mid-walk.

    A run that leaves ``queued`` between two page reads lands as ``yielded < total``
    with every page delivered, so treating any shortfall as an unread tail fails the
    tick on churn -- which on a busy repository is most ticks, and a signal that
    fires for a non-reason is the one nobody reads. The tick fails only where the
    shortfall has a nameable cause: the page cap was spent, or an empty page came
    back at the result ceiling.

    Negative control: the rule that recorded EVERY shortfall records this one, so
    the sink is non-empty and the tick goes red for a set that merely moved.
    """
    ceiling_pages = wd.REPO_LISTING_RESULT_CEILING // wd.PAGE_SIZE

    def churned(path: str) -> Any:
        page = int(dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))["page"])
        # Page one says 250 exist; by page three the set has drained to 150.
        if page == 1:
            return {"total_count": 250, "workflow_runs": [_run(i) for i in range(wd.PAGE_SIZE)]}
        if page == 2:
            return {"total_count": 150, "workflow_runs": [_run(500 + i) for i in range(50)]}
        return {"total_count": 150, "workflow_runs": []}

    api = FakeApi({}, {})
    api.get = churned  # type: ignore[method-assign]
    log: list[str] = []
    sink: list[str] = []
    runs = list(
        wd._iter_repo_runs(
            api,
            REPO,
            status="queued",
            max_pages=ceiling_pages,
            truncated=sink,
            log=log.append,
        )
    )
    assert len(runs) == wd.PAGE_SIZE + 50
    # Warned, so the reading is still visible, and NOT recorded, so no failed outcome.
    assert any("the set moved during the walk" in line for line in log)
    assert sink == []
    # The walk neither spent its cap nor reached the ceiling, which is what makes
    # this churn rather than an unread tail.
    assert len(runs) < wd.REPO_LISTING_RESULT_CEILING

    # Negative control: the reverted rule recorded every shortfall.
    control: list[str] = []
    if len(runs) < 250:
        control.append("recorded")
    assert control == ["recorded"]


def test_a_pathless_run_is_not_classified_or_changed() -> None:
    run = _run(1)
    run.pop("path")
    api = FakeApi({"in_progress": [run]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api)
    assert all(verdict.run_id != 1 for verdict in verdicts)
    assert 1 not in outcomes
    assert not any("/runs/1/jobs" in path for path in api.gets)
    assert not any("/runs/1/" in path for path in api.posts)


def test_the_completed_sample_listing_stays_workflow_scoped_and_paginated() -> None:
    """The completed-run evidence sample keeps its per-workflow, globally-capped read."""
    runs = [_run(i, minutes_ago=200 - i, status="completed") for i in range(1, 131)]
    api = FakeApi({"completed": list(reversed(runs))}, {})
    listed = wd.list_runs(api, REPO, "ci.yml", status="completed", cap=120)
    assert len(listed) == 120
    assert sorted(int(r["id"]) for r in listed) == list(range(11, 131))  # the 120 newest
    pages = [p for p in api.gets if "/actions/workflows/ci.yml/runs?" in p]
    assert len(pages) == 2
    assert "per_page=100" in pages[0] and "page=1" in pages[0]
    assert "per_page=100" in pages[1] and "page=2" in pages[1]


def test_jobs_are_paginated_past_one_page() -> None:
    jobs = [_job(i, status="completed", codebuild=False) for i in range(1, 151)]
    jobs.append(_job(999))
    api = FakeApi({"in_progress": [_run(1)]}, {1: jobs})
    verdicts, _ = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.ORPHANED
    assert [o.job_id for o in verdict.orphans] == [999]
    # Listed once for the verdict, once for the dispatch evidence re-read that
    # precedes the heal, and once again immediately before the cancel.
    assert len([p for p in api.gets if "/runs/1/jobs" in p]) == 6

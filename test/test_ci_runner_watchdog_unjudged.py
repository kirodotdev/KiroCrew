"""A stuck run only a person can recover is judged, or the tick says it was not.

The classify bound serves heal-eligible runs first. These tests cover what keeps a run
of a heal-exempt workflow from going unread behind them: reads held for it inside the
bound, a verdict for every run the top-up reads, a per-tick line naming the queued runs
left without one, and the rule that saturation turns such an orphan into a warning only
until a job of it has queued ``Policy.exempt_saturation_cap``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from ci_runner_watchdog_helpers import (
    REPO,
    FakeApi,
    _Clock,
    _job,
    _policy,
    _run,
    _ts,
    _verdict_of,
    wd,
)

# Derived from the script's own sets, so a workflow moving between them moves with it.
EXEMPT = sorted(wd.heal_exempt_workflows())[0]
HEAL_SAFE = sorted(wd.HEAL_SAFE_WORKFLOWS)[0]
ORPHAN_MINUTES = _policy().orphan_after.total_seconds() / 60


def _sweep_logged(api: FakeApi, **overrides: Any) -> tuple[list[Any], dict[int, str], list[str]]:
    clock = _Clock()
    logged: list[str] = []
    verdicts, outcomes = wd.run_watchdog(
        api, _policy(**overrides), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    return verdicts, outcomes, logged


def _served(run_id: int, *, minutes_ago: float, waited: float = 0.2) -> dict[str, Any]:
    """A CodeBuild job of ``run_id`` that got a runner after ``waited`` minutes."""
    return _job(
        run_id * 10,
        status="in_progress",
        minutes_ago=minutes_ago,
        started_minutes_ago=minutes_ago - waited,
        runner_name="runner",
        run_id=run_id,
    )


def _healthy_heal_eligible(count: int) -> tuple[list[dict[str, Any]], dict[int, list[Any]]]:
    """``count`` heal-eligible runs past the orphan threshold whose job was served promptly."""
    runs = [
        _run(
            1000 + i,
            minutes_ago=ORPHAN_MINUTES + 5 + i,
            workflow=HEAL_SAFE,
            branch=f"branch-{i}",
        )
        for i in range(count)
    ]
    jobs = {
        int(run["id"]): [_served(int(run["id"]), minutes_ago=ORPHAN_MINUTES + 5 + i)]
        for i, run in enumerate(runs)
    }
    return runs, jobs


def _exempt_orphan(run_id: int, *, queued_minutes: float, status: str = "queued") -> Any:
    run = _run(run_id, minutes_ago=queued_minutes, status=status, workflow=EXEMPT)
    return run, [_job(run_id * 10 + 1, minutes_ago=queued_minutes, run_id=run_id)]


# ── the bound holds reads for runs only a person can recover ─────────────────


@pytest.mark.parametrize("status", ["queued", "in_progress"])
@pytest.mark.parametrize("beyond_the_bound", [0, 7])
def test_a_heal_exempt_orphan_is_judged_however_many_heal_eligible_runs_are_past_the_threshold(
    status: str, beyond_the_bound: int
) -> None:
    """The bound full of heal-eligible runs past the threshold, and one exempt orphan.

    A bound spent whole on runs the watchdog could heal leaves the exempt orphan unread
    and the tick at exit 0 with it still queued. The held reads judge it: it is named
    and it fails the tick.
    """
    eligible, jobs = _healthy_heal_eligible(wd.LIVE_CLASSIFY_READS + beyond_the_bound)
    orphan, orphan_jobs = _exempt_orphan(1, queued_minutes=6 * 60, status=status)
    by_status: dict[str, list[dict[str, Any]]] = {"in_progress": list(eligible)}
    by_status.setdefault(status, []).append(orphan)
    api = FakeApi(by_status, {**jobs, 1: orphan_jobs})

    verdicts, outcomes, logged = _sweep_logged(api)

    assert {v.run_id: v.verdict for v in verdicts}.get(
        1
    ) == wd.HEAL_EXEMPT, "the orphan got no verdict"
    assert outcomes.get(1) == wd.OUTCOME_HUMAN_REQUIRED
    assert wd.FAILED_OUTCOMES & set(outcomes.values())
    assert any("needs a human" in line and " 1 " in line for line in logged)
    job_reads = [path for path in api.gets if "/jobs" in path]
    assert len(job_reads) == wd.LIVE_CLASSIFY_READS, "the held read displaces one, it adds none"


def test_without_the_held_reads_the_same_orphan_goes_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the held reads are what judges it, not the shape of the fixture."""
    monkeypatch.setattr(wd, "LIVE_EXEMPT_CLASSIFY_READS", 0)
    eligible, jobs = _healthy_heal_eligible(wd.LIVE_CLASSIFY_READS)
    orphan, orphan_jobs = _exempt_orphan(1, queued_minutes=6 * 60)
    api = FakeApi({"in_progress": eligible, "queued": [orphan]}, {**jobs, 1: orphan_jobs})

    verdicts, outcomes, logged = _sweep_logged(api)

    assert 1 not in {v.run_id for v in verdicts}
    assert not wd.FAILED_OUTCOMES & set(outcomes.values())
    # Still said: the run is in the line of queued runs the tick did not judge.
    assert wd.unjudged_line([orphan], _policy()) in logged


def test_the_held_reads_go_to_queued_runs_first_then_to_the_oldest_attempt() -> None:
    """More candidates than held reads: `queued` before `in_progress`, oldest attempt first.

    A re-run that started under the threshold and a fork's run take none of them.
    """
    held = wd.LIVE_EXEMPT_CLASSIFY_READS
    eligible, _ = _healthy_heal_eligible(wd.LIVE_CLASSIFY_READS)
    # Older than every heal-eligible run, so none of them rides in on the evidence
    # reserve (the newest runs). Every in-progress candidate is OLDER than every queued
    # one, so age alone would pick them; the queued ones are chosen first all the same.
    oldest_eligible = min(wd.parse_timestamp(run["created_at"]) for run in eligible)
    base = (_policy().now - oldest_eligible).total_seconds() / 60 + 100
    queued = [
        _run(10 + i, minutes_ago=base + i, status="queued", workflow=EXEMPT)
        for i in range(held - 1)
    ]
    running = [_run(30 + i, minutes_ago=base + 100 + i, workflow=EXEMPT) for i in range(3)]
    rerun = _run(50, minutes_ago=24 * 60, status="queued", workflow=EXEMPT, attempt=2)
    rerun["run_started_at"] = _ts(ORPHAN_MINUTES - 1)
    fork = _run(51, minutes_ago=24 * 60, status="queued", workflow=EXEMPT, fork=True)
    candidates = sorted(
        eligible + queued + running + [rerun, fork], key=lambda run: run["created_at"]
    )

    bounded, unread = wd.live_runs_within_read_bound(candidates, _policy(), lambda _line: None)

    read = {int(run["id"]) for run in bounded}
    assert {int(run["id"]) for run in queued} <= read
    # One held read is left after the queued runs: the in-progress attempt started longest ago.
    longest = min(running, key=lambda run: run["created_at"])
    assert {int(run["id"]) for run in running} & read == {int(longest["id"])}
    assert not {50, 51} & read
    assert len(bounded) == wd.LIVE_CLASSIFY_READS
    assert [run["created_at"] for run in bounded] == sorted(run["created_at"] for run in bounded)
    assert {50, 51} <= set(unread), "an unread run stays retained for the hold"


def test_human_only_shape_is_the_exemption_the_verdict_applies() -> None:
    """Priority only, and the same condition ``_mark_heal_exempt`` uses on the verdict."""
    assert wd._human_only_shape(_run(1, workflow=EXEMPT), REPO)
    assert wd._human_only_shape(_run(2, workflow=EXEMPT, status="queued"), REPO)
    assert not wd._human_only_shape(_run(3, workflow=HEAL_SAFE), REPO)
    assert not wd._human_only_shape(_run(4, workflow=EXEMPT, fork=True), REPO)
    # Held by its concurrency group with no jobs: nothing to read.
    assert not wd._human_only_shape(_run(5, workflow=EXEMPT, status="pending"), REPO)
    for workflow in sorted(wd.HEAL_SAFE_PULL_REQUEST_WORKFLOWS - wd.HEAL_SAFE_WORKFLOWS):
        # Declared safe for pull-request runs only, so its push run is a person's to recover.
        assert wd._human_only_shape(_run(6, workflow=workflow, event="push"), REPO)
        assert not wd._human_only_shape(_run(7, workflow=workflow, event="pull_request"), REPO)
    for run in (_run(8, workflow=EXEMPT), _run(9, workflow=HEAL_SAFE)):
        orphaned = wd.classify_run(run, [_job(81, run_id=int(run["id"]))], _policy())
        marked = wd._mark_heal_exempt(orphaned, wd.heal_exempt_workflows())
        assert (marked.verdict == wd.HEAL_EXEMPT) == wd._human_only_shape(run, REPO)


# ── a run the top-up reads gets a verdict, for the same calls ────────────────


def _top_up_tick() -> FakeApi:
    """A heal-eligible orphan inside the bound and an exempt orphan the bound leaves out."""
    healable = _run(1, minutes_ago=60, status="queued", workflow=HEAL_SAFE, branch="healable")
    exempt, exempt_jobs = _exempt_orphan(2, queued_minutes=45)
    recent = _run(3, minutes_ago=ORPHAN_MINUTES - 5, workflow=HEAL_SAFE, branch="recent")
    return FakeApi(
        {"queued": [healable, exempt], "in_progress": [recent]},
        {1: [_job(11, minutes_ago=60, run_id=1)], 2: exempt_jobs, 3: [_served(3, minutes_ago=4)]},
    )


def test_a_run_the_top_up_reads_gets_a_verdict_and_costs_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The top-up reads run 2's jobs for evidence; the same read gives run 2 its verdict.

    The same tick is driven twice on identical fakes, once with the top-up's ``on_read``
    dropped. The requests are the same, one for one; only the verdict differs.
    """
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 2)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    monkeypatch.setattr(wd, "LIVE_EXEMPT_CLASSIFY_READS", 0)
    judging = _top_up_tick()
    verdicts, outcomes, _ = _sweep_logged(judging, dry_run=True)

    assert _verdict_of(verdicts, 2).verdict == wd.HEAL_EXEMPT
    assert outcomes[2] == wd.OUTCOME_HUMAN_REQUIRED

    real_top_up = wd.top_up_unread_evidence

    def evidence_only(*args: Any, on_read: Any = None, **kwargs: Any) -> tuple[int, int]:
        return real_top_up(*args, **kwargs)

    monkeypatch.setattr(wd, "top_up_unread_evidence", evidence_only)
    silent = _top_up_tick()
    silent_verdicts, silent_outcomes, _ = _sweep_logged(silent, dry_run=True)

    assert 2 not in {v.run_id for v in silent_verdicts}
    assert 2 not in silent_outcomes
    assert judging.gets == silent.gets
    assert judging.posts == silent.posts == []


def test_the_top_up_hands_each_read_to_on_read_once_in_read_order() -> None:
    policy = _policy()
    candidates = {900 + i: policy.now - timedelta(minutes=30 + i) for i in range(4)}
    jobs_of = {run_id: [_served(run_id, minutes_ago=20)] for run_id in candidates}
    plain_reads: list[int] = []
    hooked_reads: list[int] = []
    handed: list[tuple[int, list[dict[str, Any]]]] = []

    def reader(into: list[int]) -> Any:
        def read(run_id: int) -> list[dict[str, Any]]:
            into.append(run_id)
            return jobs_of[run_id]

        return read

    wd.top_up_unread_evidence(
        None,  # type: ignore[arg-type]
        policy,
        wd.DispatchEvidence(unread_candidates=dict(candidates)),
        lambda _line: None,
        read_jobs=reader(plain_reads),
    )
    hooked = wd.DispatchEvidence(unread_candidates=dict(candidates))
    wd.top_up_unread_evidence(
        None,  # type: ignore[arg-type]
        policy,
        hooked,
        lambda _line: None,
        read_jobs=reader(hooked_reads),
        on_read=lambda run_id, jobs: handed.append((run_id, jobs)),
    )

    assert hooked_reads == plain_reads
    assert sorted(plain_reads) == sorted(candidates)
    assert handed == [(run_id, jobs_of[run_id]) for run_id in plain_reads]
    assert hooked.unread_candidates == {}


def _orphan_run(run_id: int, *, age: float, event: str = "push") -> tuple[Any, list[Any]]:
    """A heal-eligible run whose one CodeBuild job has queued since the run was created."""
    run = _run(
        run_id,
        minutes_ago=age,
        status="queued",
        workflow=HEAL_SAFE,
        event=event,
        branch=f"{event}-{run_id}",
    )
    return run, [_job(run_id * 10 + 1, minutes_ago=age, run_id=run_id)]


def _capped_tick(
    monkeypatch: pytest.MonkeyPatch, inside: list[Any], topped_up: list[Any]
) -> tuple[dict[int, str], list[int]]:
    """Drive a dry tick whose bound reads ``inside`` and whose top-up reads ``topped_up``.

    Returns the outcomes and the run ids the tick chose to heal, in the order it chose them.
    """
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", len(inside))
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", len(inside))
    monkeypatch.setattr(wd, "LIVE_EXEMPT_CLASSIFY_READS", 0)
    runs = [run for run, _ in inside + topped_up]
    api = FakeApi({"queued": runs}, {int(run["id"]): jobs for run, jobs in inside + topped_up})
    verdicts, outcomes, _ = _sweep_logged(api, dry_run=True)
    assert {v.run_id for v in verdicts if v.verdict == wd.ORPHANED} == {int(r["id"]) for r in runs}
    chosen = [run_id for run_id, outcome in outcomes.items() if outcome == wd.OUTCOME_DRY_RUN]
    return outcomes, chosen


def test_the_heal_cap_goes_to_the_oldest_push_orphans_wherever_they_were_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten push orphans read inside the bound, six OLDER ones read by the top-up.

    The top-up's verdicts arrive after the bound's, so a cap spent in arrival order would
    heal the younger runs and leave the older ones for the next tick.
    """
    cap = _policy().max_runs
    younger = [_orphan_run(100 + i, age=ORPHAN_MINUTES + 5 + i) for i in range(10)]
    older = [_orphan_run(200 + i, age=ORPHAN_MINUTES + 25 + i) for i in range(cap + 1)]

    outcomes, chosen = _capped_tick(monkeypatch, younger, older)

    oldest_first = sorted(older, key=lambda pair: pair[0]["created_at"])
    assert chosen == [int(run["id"]) for run, _ in oldest_first[:cap]]
    left = {run_id for run_id, outcome in outcomes.items() if outcome == wd.OUTCOME_NOT_ATTEMPTED}
    assert left == {int(run["id"]) for run, _ in younger + oldest_first[cap:]}


def test_the_slot_kept_for_other_events_goes_to_the_oldest_of_them_wherever_it_was_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Push orphans fill the cap but one slot; that slot is the OLDEST non-push orphan's."""
    cap = _policy().max_runs
    pushes = [_orphan_run(100 + i, age=ORPHAN_MINUTES + 5 + i) for i in range(cap + 2)]
    young_pull = _orphan_run(300, age=ORPHAN_MINUTES + 2, event="pull_request")
    old_pull = _orphan_run(301, age=ORPHAN_MINUTES + 60, event="pull_request")

    _, chosen = _capped_tick(monkeypatch, pushes + [young_pull], [old_pull])

    oldest_pushes = sorted(pushes, key=lambda pair: pair[0]["created_at"])[: cap - 1]
    assert chosen == [int(run["id"]) for run, _ in oldest_pushes] + [301]


# ── saturation makes an exempt orphan a warning, up to the cap ───────────────


def _saturated_tick(queued_minutes: float, *, waited: float) -> FakeApi:
    """An exempt orphan, and a job of its queue served ``waited`` minutes late after it queued."""
    orphan, orphan_jobs = _exempt_orphan(1, queued_minutes=queued_minutes, status="in_progress")
    other = _run(2, minutes_ago=ORPHAN_MINUTES, workflow=HEAL_SAFE, branch="other")
    return FakeApi(
        {"in_progress": [orphan, other]},
        {1: orphan_jobs, 2: [_served(2, minutes_ago=waited + 1, waited=waited)]},
    )


SLOW = _policy().saturation_wait.total_seconds() / 60 + 3
PROMPT = 0.2


@pytest.mark.parametrize(
    ("from_cap", "minutes", "waited", "held"),
    [
        (False, ORPHAN_MINUTES + 15, SLOW, True),
        (True, -1, SLOW, True),
        (True, 0, SLOW, False),
        (True, 300, SLOW, False),
        (False, ORPHAN_MINUTES + 15, PROMPT, False),
    ],
    ids=["saturated", "saturated-just-under-cap", "at-cap", "hours-past-cap", "dispatching"],
)
def test_saturation_holds_an_exempt_orphan_as_a_warning_only_under_the_cap(
    from_cap: bool, minutes: float, waited: float, held: bool
) -> None:
    """``minutes`` is how long the orphaned job has queued, counted from the cap when ``from_cap``."""
    cap = _policy().exempt_saturation_cap.total_seconds() / 60
    api = _saturated_tick(cap + minutes if from_cap else minutes, waited=waited)

    verdicts, outcomes, logged = _sweep_logged(api)

    assert _verdict_of(verdicts, 1).verdict == wd.HEAL_EXEMPT
    failed = bool(wd.FAILED_OUTCOMES & set(outcomes.values()))
    warned = [line for line in logged if line.startswith("::warning::") and "run 1 " in line]
    errored = [line for line in logged if line.startswith("::error::") and "run 1 " in line]
    if held:
        assert outcomes[1] == wd.OUTCOME_HUMAN_HELD_SATURATED
        assert not failed
        assert warned and not errored
    else:
        assert outcomes[1] == wd.OUTCOME_HUMAN_REQUIRED
        assert failed
        assert errored and not warned
    assert wd.exempt_held_line(int(held), _policy()) in logged
    assert api.posts == [], "nothing is cancelled or re-run for an exempt workflow either way"


def test_a_slow_start_on_another_queue_does_not_hold_an_orphan_whose_own_queue_is_prompt() -> None:
    """The orphan's own queue answers first: served promptly there, the job is lost, not slow.

    A slow start on the large fleet says nothing about the queue this job waits in, so
    the run needs a human at once, exactly as the heal's own hold reads it.
    """
    orphan, orphan_jobs = _exempt_orphan(
        1, queued_minutes=ORPHAN_MINUTES + 15, status="in_progress"
    )
    same_queue = _run(2, minutes_ago=ORPHAN_MINUTES, workflow=HEAL_SAFE, branch="same")
    other_queue = _run(3, minutes_ago=ORPHAN_MINUTES, workflow=HEAL_SAFE, branch="other")
    slow_elsewhere = _job(
        31,
        status="in_progress",
        minutes_ago=SLOW + 1,
        started_minutes_ago=1,
        runner_name="runner",
        run_id=3,
        large=True,
    )
    api = FakeApi(
        {"in_progress": [orphan, same_queue, other_queue]},
        {1: orphan_jobs, 2: [_served(2, minutes_ago=3, waited=PROMPT)], 3: [slow_elsewhere]},
    )

    _, outcomes, logged = _sweep_logged(api)

    assert outcomes[1] == wd.OUTCOME_HUMAN_REQUIRED
    assert wd.exempt_held_line(0, _policy()) in logged

    # The control: with no start on its own queue the fleet-wide reading applies and holds it.
    fleet_only = FakeApi(
        {"in_progress": [orphan, other_queue]}, {1: orphan_jobs, 3: [slow_elsewhere]}
    )
    _, fleet_outcomes, _ = _sweep_logged(fleet_only)
    assert fleet_outcomes[1] == wd.OUTCOME_HUMAN_HELD_SATURATED


def test_the_held_count_is_printed_on_a_tick_with_nothing_held() -> None:
    eligible, jobs = _healthy_heal_eligible(3)
    _, outcomes, logged = _sweep_logged(FakeApi({"in_progress": eligible}, jobs))
    assert outcomes == {}
    assert wd.exempt_held_line(0, _policy()) in logged
    seven, eight = (wd.exempt_held_line(count, _policy()).split() for count in (7, 8))
    assert "7" in seven and "7" not in eight and "8" in eight, "the line carries the count"


def test_the_held_outcome_is_not_a_failed_outcome_and_the_required_one_is() -> None:
    assert wd.OUTCOME_HUMAN_HELD_SATURATED not in wd.FAILED_OUTCOMES
    assert wd.OUTCOME_HUMAN_REQUIRED in wd.FAILED_OUTCOMES


# ── the tick names the queued runs it did not judge ──────────────────────────


def test_unjudged_queued_runs_are_the_stuck_looking_rows_without_a_verdict() -> None:
    policy = _policy()
    past = ORPHAN_MINUTES + 1
    stuck_old = _run(1, minutes_ago=past + 50, status="queued", workflow=EXEMPT)
    stuck_new = _run(2, minutes_ago=past, status="queued", workflow=HEAL_SAFE)
    judged = _run(3, minutes_ago=past, status="queued", workflow=EXEMPT)
    running = _run(4, minutes_ago=past, status="in_progress", workflow=EXEMPT)
    young = _run(5, minutes_ago=ORPHAN_MINUTES - 1, status="queued", workflow=EXEMPT)
    rerun = _run(6, minutes_ago=24 * 60, status="queued", workflow=EXEMPT, attempt=2)
    rerun["run_started_at"] = _ts(ORPHAN_MINUTES - 1)
    ghost = _run(7, minutes_ago=wd.GHOST_AFTER.total_seconds() / 60 + 1, status="queued")
    pending = _run(8, minutes_ago=past, status="pending", workflow=EXEMPT)
    rows = [ghost, rerun, stuck_new, stuck_old, judged, running, young, pending]

    unjudged = wd.unjudged_queued_runs(rows, {3}, policy)

    assert [int(run["id"]) for run in unjudged] == [1, 2], "oldest attempt first"
    assert wd.unjudged_queued_runs(rows, {1, 2, 3}, policy) == []


def test_the_unjudged_line_carries_the_count_and_names_a_bounded_number_of_runs() -> None:
    policy = _policy()
    extra = 3
    rows = [
        _run(100 + i, minutes_ago=ORPHAN_MINUTES + 100 - i, status="queued")
        for i in range(wd.UNJUDGED_IDS_NAMED + extra)
    ]
    line = wd.unjudged_line(rows, policy)
    words = line.replace(",", " ").replace(":", " ").split()
    assert str(len(rows)) in words
    assert [word for word in words if word in {str(run["id"]) for run in rows}] == [
        str(run["id"]) for run in rows[: wd.UNJUDGED_IDS_NAMED]
    ]
    assert f"{extra} more" in line
    none = wd.unjudged_line([], policy)
    assert "0" in none.split() and none != line
    assert "::" not in line and "::" not in none, "a plain line: no annotation, no outcome"


def test_a_tick_names_the_queued_runs_it_left_unjudged_and_stays_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No orphan among the runs read, so no top-up: the rest are named, the tick passes."""
    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", 3)
    monkeypatch.setattr(wd, "LIVE_EVIDENCE_RESERVE", 1)
    monkeypatch.setattr(wd, "LIVE_EXEMPT_CLASSIFY_READS", 1)
    eligible = [
        _run(10 + i, minutes_ago=ORPHAN_MINUTES + 30 + i, status="queued", workflow=HEAL_SAFE)
        for i in range(3)
    ]
    exempt = [
        _run(20 + i, minutes_ago=ORPHAN_MINUTES + 10 + i, status="queued", workflow=EXEMPT)
        for i in range(3)
    ]
    # Every job waits on a hosted runner, so no run read is an orphan.
    jobs = {
        int(run["id"]): [_job(int(run["id"]) * 10, codebuild=False, run_id=int(run["id"]))]
        for run in eligible + exempt
    }
    api = FakeApi({"queued": eligible + exempt}, jobs)

    verdicts, outcomes, logged = _sweep_logged(api)

    judged = {v.run_id for v in verdicts}
    left = sorted(
        (run for run in eligible + exempt if int(run["id"]) not in judged),
        key=lambda run: run["created_at"],
    )
    assert len(judged) == wd.LIVE_CLASSIFY_READS and left
    assert wd.unjudged_line(left, _policy()) in logged
    assert outcomes == {}, "the count is a log line: it gives the tick no outcome"

    monkeypatch.setattr(wd, "LIVE_CLASSIFY_READS", len(eligible + exempt))
    _, _, all_read = _sweep_logged(FakeApi({"queued": eligible + exempt}, jobs))
    assert wd.unjudged_line([], _policy()) in all_read

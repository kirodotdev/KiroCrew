"""The watchdog frees a stuck ``main`` push run's concurrency group.

A push run of ``ci.yml`` keeps its group's running slot while it is queued
(``cancel-in-progress`` is true only for ``pull_request``), so every later push to
``main`` is evicted behind it. The fixture is a real ``ci.yml`` run's job list at
the instant its ``snapshot_at`` names: CodeBuild-routed jobs queued past the orphan
threshold with no runner, two Windows shards running after a slow wait for theirs
(so the fleet reads as saturated), and a newer ``main`` push pending behind it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ci_runner_watchdog_helpers import (
    NOW,
    REPO,
    FakeApi,
    _Clock,
    _job,
    _policy,
    _posts,
    _run,
    wd,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ci_runner_watchdog" / "incident_main_run_jobs.json"
INCIDENT_RUN = 44239973267
NEWER_MAIN_RUN = 44243000097


def _shift(value: str | None, offset: timedelta) -> str | None:
    if value is None:
        return None
    moved = datetime.fromisoformat(value.replace("Z", "+00:00")) + offset
    return moved.strftime("%Y-%m-%dT%H:%M:%SZ")


def _incident() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The incident run and its jobs, moved so the snapshot instant is ``NOW``."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot = datetime.fromisoformat(data["snapshot_at"].replace("Z", "+00:00"))
    offset = NOW - snapshot
    jobs = [
        {
            **job,
            "created_at": _shift(job["created_at"], offset),
            "started_at": _shift(job["started_at"], offset),
            "completed_at": _shift(job["completed_at"], offset),
        }
        for job in data["jobs"]
    ]
    created = datetime.fromisoformat(data["run_created_at"].replace("Z", "+00:00"))
    run = _run(INCIDENT_RUN, minutes_ago=(snapshot - created).total_seconds() / 60)
    return run, jobs


def _incident_api(**extra: Any) -> FakeApi:
    run, jobs = _incident()
    newer = _run(NEWER_MAIN_RUN, minutes_ago=30, status="pending")
    return FakeApi({"in_progress": [run], "pending": [newer]}, {INCIDENT_RUN: jobs}, **extra)


def test_the_incident_fixture_is_an_orphaned_main_push_run() -> None:
    run, jobs = _incident()
    queued = [job for job in jobs if job["status"] == "queued"]
    assert queued, "the fixture must hold the jobs that never got a runner"
    assert all(job["runner_name"] == "" for job in queued)
    assert all(any(label.startswith("codebuild-") for label in job["labels"]) for job in queued)
    verdict = wd.classify_run(run, jobs, _policy())
    assert verdict.verdict == wd.ORPHANED
    assert {orphan.name for orphan in verdict.orphans} == {job["name"] for job in queued}


def test_a_superseded_main_run_is_cancelled_when_the_tick_is_short_of_the_rerun_reserve() -> None:
    """No re-run follows a superseded run's cancel, so the re-run reserve does not
    apply to it: the cancel and an immediate force-cancel are posted, the run is
    watched until it completes, and nothing is re-run."""
    api = _incident_api()
    clock = _Clock()
    logged: list[str] = []
    _, outcomes = wd.run_watchdog(
        api,
        _policy(tick_budget=timedelta(seconds=wd.RERUN_RESERVE_SECONDS - 1)),
        clock=clock.now,
        sleep=clock.sleep,
        log=logged.append,
    )
    assert outcomes[INCIDENT_RUN] == wd.OUTCOME_SUPERSEDED
    assert outcomes[INCIDENT_RUN] not in wd.FAILED_OUTCOMES
    assert _posts(api, "/cancel") == [f"repos/{REPO}/actions/runs/{INCIDENT_RUN}/cancel"]
    assert _posts(api, "/rerun") == []
    assert _posts(api, "/force-cancel") == [
        f"repos/{REPO}/actions/runs/{INCIDENT_RUN}/force-cancel"
    ]
    assert any("concurrency group is free" in line for line in logged)


def test_a_superseded_cancel_that_never_completes_reds_the_tick_and_names_the_command() -> None:
    api = _incident_api(cancel_lands_after=10**6, force_cancel_works=False)
    clock = _Clock()
    logged: list[str] = []
    _, outcomes = wd.run_watchdog(
        api,
        _policy(tick_budget=timedelta(seconds=wd.RERUN_RESERVE_SECONDS - 1)),
        clock=clock.now,
        sleep=clock.sleep,
        log=logged.append,
    )
    assert outcomes[INCIDENT_RUN] == wd.OUTCOME_CANCEL_TIMED_OUT
    assert outcomes[INCIDENT_RUN] in wd.FAILED_OUTCOMES
    assert _posts(api, "/rerun") == []
    assert clock.t <= wd.RERUN_RESERVE_SECONDS
    assert any(
        line.startswith("::error::") and f"gh run cancel {INCIDENT_RUN} --force" in line
        for line in logged
    )


def test_a_superseded_run_is_left_untouched_once_the_tick_budget_is_spent() -> None:
    api = _incident_api()
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api,
        _policy(tick_budget=timedelta(seconds=0)),
        clock=clock.now,
        sleep=clock.sleep,
        log=lambda _line: None,
    )
    assert outcomes[INCIDENT_RUN] == wd.OUTCOME_NOT_ATTEMPTED
    assert api.posts == []


def test_a_short_tick_with_an_unanswerable_branch_lookup_cancels_nothing() -> None:
    """A lookup that cannot say whether a newer run exists never licenses the
    cancel-only path: the run may be current, and its re-run would not fit."""
    api = FakeApi({"in_progress": [_run(1)]}, {1: [_job(11)]}, newest_by_branch={"main": []})
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api,
        _policy(tick_budget=timedelta(seconds=wd.RERUN_RESERVE_SECONDS - 1)),
        clock=clock.now,
        sleep=clock.sleep,
        log=lambda _line: None,
    )
    assert outcomes[1] == wd.OUTCOME_NOT_ATTEMPTED
    assert api.posts == []


def test_a_main_push_orphan_takes_the_cap_before_older_pull_request_orphans() -> None:
    """More orphans than the cap: the ``main`` push run is healed even though every
    pull-request orphan is older, and the last slot still goes to the oldest
    pull-request orphan so push orphans cannot starve it."""
    cap = 2
    pulls = [
        _run(
            index,
            minutes_ago=90 - index,
            branch=f"feature-{index}",
            event="pull_request",
            head_sha=f"sha-{index}",
        )
        for index in range(1, cap + 2)
    ]
    main = _run(50, minutes_ago=40)
    jobs: dict[int, list[dict[str, Any]]] = {
        int(run["id"]): [_job(int(run["id"]) * 10, run_id=int(run["id"]), minutes_ago=30)]
        for run in pulls
    }
    jobs[50] = [_job(500, run_id=50, minutes_ago=30)]
    api = FakeApi(
        {"in_progress": [*pulls, main]},
        jobs,
        open_pull_heads={str(run["head_branch"]): [str(run["head_sha"])] for run in pulls},
    )
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(max_runs=cap), clock=clock.now, sleep=clock.sleep, log=lambda _line: None
    )
    assert f"repos/{REPO}/actions/runs/50/cancel" in _posts(api, "/cancel")
    assert outcomes[50] == wd.OUTCOME_HEALED
    assert outcomes[1] == wd.OUTCOME_HEALED
    deferred = [
        run_id for run_id, outcome in outcomes.items() if outcome == wd.OUTCOME_NOT_ATTEMPTED
    ]
    assert deferred and all(run_id not in (1, 50) for run_id in deferred)


def test_push_orphans_filling_the_cap_still_leave_a_slot_for_a_pull_request_orphan() -> None:
    cap = 3
    pushes = [_run(10 + index, minutes_ago=60, branch=f"release/{index}") for index in range(cap)]
    pull = _run(1, minutes_ago=90, branch="feature", event="pull_request", head_sha="sha-1")
    runs = [pull, *pushes]
    jobs = {
        int(run["id"]): [_job(int(run["id"]) * 10, run_id=int(run["id"]), minutes_ago=30)]
        for run in runs
    }
    api = FakeApi({"in_progress": runs}, jobs, open_pull_heads={"feature": ["sha-1"]})
    clock = _Clock()
    _, outcomes = wd.run_watchdog(
        api, _policy(max_runs=cap), clock=clock.now, sleep=clock.sleep, log=lambda _line: None
    )
    assert outcomes[1] == wd.OUTCOME_HEALED
    assert [outcomes[int(run["id"])] for run in pushes].count(wd.OUTCOME_NOT_ATTEMPTED) == 1

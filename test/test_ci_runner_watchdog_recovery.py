"""The next tick recovers a cancel that landed after the budget ran out.

``scripts/ci/runner_watchdog.py`` finds a cancelled orphan that nobody re-ran
again by its fingerprint and re-runs it at its own revision, under the same
guard rails as a live heal: the attempt cap, the fork guard, the heal-exempt
workflows and the per-tick cap. Heal safety is read from each run's own
revision, and a pull-request run is judged by its head SHA against the open
pull requests.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from ci_runner_watchdog_helpers import (
    REPO,
    FakeApi,
    _cancelled_orphan_job,
    _Clock,
    _job,
    _policy,
    _posts,
    _run,
    _sweep,
    _ts,
    _verdict_of,
    wd,
)

# ── the next tick recovers a cancel that landed after the budget ────────────


def test_a_cancelled_orphan_nobody_rerun_is_rerun_on_the_next_tick() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    api = FakeApi(
        {"cancelled": [run]},
        {
            1: [
                _job(10, status="completed", conclusion="success", codebuild=False),
                _cancelled_orphan_job(11),
            ]
        },
        newest_by_branch={"main": 1},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.CANCELLED_ORPHAN
    assert outcomes == {1: wd.OUTCOME_RECOVERED}
    assert api.posts == [f"repos/{REPO}/actions/runs/1/rerun"]


def test_a_runs_own_revision_that_adds_publish_is_exempt() -> None:
    run = _run(1, head_sha="publish-sha")
    own_revision = (
        "concurrency:\n  group: ${{ github.ref }}\njobs:\n  ship:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: npm publish\n"
    )
    api = FakeApi(
        {"in_progress": [run]},
        {1: [_job(11)]},
        workflow_contents={("publish-sha", "ci.yml"): own_revision},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEAL_EXEMPT
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert api.posts == []


def test_a_runs_own_clean_revision_is_healed_and_read_once() -> None:
    run = _run(1, head_sha="clean-sha")
    own_revision = (
        "concurrency:\n  group: ${{ github.ref }}\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    )
    api = FakeApi(
        {"in_progress": [run]},
        {1: [_job(11)]},
        workflow_contents={("clean-sha", "ci.yml"): own_revision},
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HEALED}
    revision_reads = [path for path in api.gets if "/contents/.github/workflows/ci.yml?" in path]
    assert len(revision_reads) == 1
    assert api.posts[:2] == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
    ]


@pytest.mark.parametrize(
    "workflow_content",
    [
        wd.ApiError(500, "contents unavailable"),
        {"encoding": "base64", "content": "%%%not-base64%%%"},
    ],
)
def test_an_unreadable_run_revision_is_a_failed_outcome_not_a_silent_exemption(
    workflow_content: Any,
) -> None:
    run = _run(1, head_sha="unreadable-sha")
    api = FakeApi(
        {"in_progress": [run]},
        {1: [_job(11)]},
        workflow_contents={("unreadable-sha", "ci.yml"): workflow_content},
    )
    verdicts, outcomes = _sweep(api)
    # An unreadable revision is UNKNOWN, not unsafe: nothing is cancelled, and
    # the outcome is a failure so a run whose safety nobody could establish
    # cannot age out of its recovery window behind a green tick.
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEAL_SAFETY_UNKNOWN}
    assert wd.OUTCOME_HEAL_SAFETY_UNKNOWN in wd.FAILED_OUTCOMES
    assert api.posts == []


def test_a_run_without_a_head_sha_cannot_be_judged_and_is_a_failed_outcome() -> None:
    run = _run(1)
    run.pop("head_sha")
    api = FakeApi({"in_progress": [run]}, {1: [_job(11)]})
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_HEAL_SAFETY_UNKNOWN}
    assert api.posts == []


def test_a_publish_or_deploy_orphan_is_observed_but_requires_a_human() -> None:
    api = FakeApi(
        {"in_progress": [_run(1, workflow="pages.yml")]},
        {1: [_job(11)]},
    )
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.HEAL_EXEMPT
    assert "human" in verdict.detail
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert api.posts == []
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert wd.OUTCOME_HUMAN_REQUIRED in summary


def test_a_heal_exempt_orphan_names_itself_in_an_error_line() -> None:
    # FAILED_OUTCOMES: a red tick's log names the run and the command to type.
    api = FakeApi(
        {"in_progress": [_run(1, workflow="pages.yml")]},
        {1: [_job(11)]},
    )
    lines: list[str] = []
    clock = _Clock()
    _verdicts, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=lines.append
    )
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    errors = [line for line in lines if line.startswith("::error::run 1 ")]
    assert len(errors) == 1
    assert "gh run cancel 1" in errors[0]
    assert api.posts == []


def test_recovery_uses_the_cancelled_runs_own_revision() -> None:
    run = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        head_sha="publish-sha",
    )
    own_revision = (
        "concurrency:\n  group: ${{ github.ref }}\njobs:\n  ship:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: npm publish\n"
    )
    api = FakeApi(
        {"cancelled": [run]},
        {1: [_cancelled_orphan_job(11)]},
        newest_by_branch={"main": 1},
        workflow_contents={("publish-sha", "ci.yml"): own_revision},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEAL_EXEMPT
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert _posts(api, "/rerun") == []
    revision_reads = [path for path in api.gets if "/contents/.github/workflows/ci.yml?" in path]
    assert len(revision_reads) == 1


def test_recovery_never_reruns_a_publish_or_deploy_workflow() -> None:
    run = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        workflow="release.yml",
    )
    api = FakeApi(
        {"cancelled": [run]},
        {1: [_cancelled_orphan_job(11)]},
        newest_by_branch={"main": 1},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.HEAL_EXEMPT
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert api.posts == []
    assert any("branch=main" in path for path in api.gets)


def test_recovery_reaches_an_orphan_behind_newer_cancelled_runs() -> None:
    orphan = _run(
        1,
        minutes_ago=80,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        branch="orphan",
    )
    superseded = [
        _run(
            i,
            minutes_ago=70 - i,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=4,
            branch=f"superseded-{i}",
        )
        for i in range(2, 53)
    ]
    api = FakeApi(
        {"cancelled": list(reversed(superseded)) + [orphan]},
        {
            1: [_cancelled_orphan_job(11, run_id=1)],
            **{
                run["id"]: [_cancelled_orphan_job(run["id"] * 10, run_id=run["id"])]
                for run in superseded
            },
        },
        newest_by_branch={
            "orphan": 1,
            **{run["head_branch"]: 100 + run["id"] for run in superseded},
        },
    )
    _, outcomes = _sweep(api, max_runs=5)
    assert outcomes == {1: wd.OUTCOME_RECOVERED}
    assert len(_posts(api, "/rerun")) == 1


def test_recovery_classifies_the_oldest_in_window_runs_within_its_read_cap() -> None:
    """The read cap bounds job reads per tick AND spends them oldest-first.

    Every `main` push cancels the run it supersedes, so a recovery window holds far
    more cancelled runs than a tick should read. The orphan here is the OLDEST
    in-window run, sitting behind more newer ones than the cap allows, so a cap
    applied to a newest-first walk would never reach it.
    """
    orphan = _run(
        1,
        minutes_ago=88,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=70,
        branch="orphan",
    )
    # A fixed count, independent of the cap, so the cap is the only thing the
    # read assertion below measures.
    newer = [
        _run(
            i,
            minutes_ago=80 - i / 10,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=10,
            branch=f"newer-{i}",
        )
        for i in range(2, 72)
    ]
    api = FakeApi(
        {"cancelled": list(reversed(newer)) + [orphan]},
        {
            1: [_cancelled_orphan_job(11, run_id=1)],
            **{
                run["id"]: [_cancelled_orphan_job(run["id"] * 10, run_id=run["id"])]
                for run in newer
            },
        },
        newest_by_branch={
            "orphan": 1,
            **{run["head_branch"]: 100 + run["id"] for run in newer},
        },
    )
    _, outcomes = _sweep(api, max_runs=5)
    assert outcomes[1] == wd.OUTCOME_RECOVERED
    cancelled_ids = {1} | {int(run["id"]) for run in newer}
    job_reads = [
        path
        for path in api.gets
        if "/jobs" in path and int(path.split("/runs/", 1)[1].split("/", 1)[0]) in cancelled_ids
    ]
    assert len(job_reads) <= wd.RECOVERY_CLASSIFY_READS
    # Independent of the cap's value: a tick must not read every in-window run.
    assert len(job_reads) < 1 + len(newer)


def test_a_cancelled_run_too_short_lived_to_hold_an_orphan_is_excluded_for_free() -> None:
    """Pins the recovery pass's zero-read exclusion to the CLASSIFIER, not to a number.

    The exclusion skips a cancelled run whose lifetime is under ``orphan_after``
    without spending a job read. That is only sound if such a run can never be
    classified as an orphan, so the classifier is driven here with the most
    favourable job the lifetime permits -- created with the run, still queueing
    when the run was cancelled -- and must find nothing. The paired case lifts
    the lifetime to the threshold and the same job does fingerprint, proving the
    exclusion tracks the classifier rather than a hardcoded bound.
    """
    policy = _policy()

    def verdict_for(lifetime_minutes: float) -> Any:
        created, cancelled = 80.0, 80.0 - lifetime_minutes
        run = _run(
            1,
            minutes_ago=created,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=cancelled,
        )
        job = _job(
            11,
            status="completed",
            conclusion="cancelled",
            minutes_ago=created,
            completed_minutes_ago=cancelled,
        )
        return wd.classify_cancelled_run(run, [job], policy, newest_check=lambda: True)

    threshold = policy.orphan_after.total_seconds() / 60
    assert verdict_for(threshold - 1).orphans == []
    assert verdict_for(threshold - 1).verdict != wd.CANCELLED_ORPHAN
    assert verdict_for(threshold).verdict == wd.CANCELLED_ORPHAN


def test_the_recovery_pass_spends_no_read_on_a_run_too_short_lived_to_hold_an_orphan() -> None:
    """What keeps the classify bound off runs that were never candidates.

    Every `main` push cancels the run it supersedes, and those live seconds. Were
    each one read, the bound would be spent on them and a real candidate behind
    them could age out of the window unread.
    """
    supersession = [
        _run(
            i,
            minutes_ago=30,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=30 - 0.1,
            branch=f"superseded-{i}",
        )
        for i in range(2, 9)
    ]
    candidate = _run(
        1,
        minutes_ago=80,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=20,
        branch="orphan",
    )
    api = FakeApi(
        {"cancelled": supersession + [candidate]},
        {1: [_cancelled_orphan_job(11, run_id=1)], **{run["id"]: [] for run in supersession}},
        newest_by_branch={"orphan": 1},
    )
    _, outcomes = _sweep(api, max_runs=5)
    assert outcomes[1] == wd.OUTCOME_RECOVERED
    job_reads = [path for path in api.gets if "/jobs?" in path]
    assert job_reads == [f"repos/{REPO}/actions/runs/1/jobs?per_page=100&page=1&filter=latest"]


def test_a_run_too_short_lived_to_hold_an_orphan_does_not_red_the_tick_from_the_tail() -> None:
    """The near-expiry flag asks a human to look. It must not ask about a non-candidate."""
    tail = [
        _run(
            i,
            minutes_ago=89,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=89 - 0.1,
            branch=f"superseded-{i}",
        )
        for i in range(2, 6)
    ]
    verdicts: list[Any] = []
    outcomes: dict[int, str] = {}
    wd._flag_unclassified_near_expiry(tail, _policy(), verdicts, outcomes, lambda _l: None)
    assert verdicts == []
    assert outcomes == {}


def test_a_stuck_run_in_a_heal_exempt_workflow_reds_the_tick() -> None:
    """Most of the watched set is heal-exempt, `main-ratchet-audit.yml` among them, and it
    was in the incident this script exists for. A warning inside a passing scheduled run
    nobody watches would leave that incident's shape intact."""
    api = FakeApi(
        {"in_progress": [_run(1, workflow="main-ratchet-audit.yml")]},
        {1: [_job(11)]},
    )
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_HUMAN_REQUIRED}
    assert wd.OUTCOME_HUMAN_REQUIRED in wd.FAILED_OUTCOMES


def test_the_completed_sample_order_covers_exactly_the_watched_set() -> None:
    """The sample order is a permutation of the watched set, so priority cannot
    introduce an unwatched workflow or drop a watched one from sampling."""
    assert sorted(wd.COMPLETED_SAMPLE_WORKFLOWS) == sorted(wd.WATCHED_WORKFLOWS)
    assert len(set(wd.COMPLETED_SAMPLE_WORKFLOWS)) == len(wd.COMPLETED_SAMPLE_WORKFLOWS)


def test_a_cancelled_run_superseded_by_a_newer_run_is_not_rerun() -> None:
    """Re-running an old pull-request run would cancel its successor through the group."""
    run = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        branch="pr",
    )
    api = FakeApi(
        {"cancelled": [run]}, {1: [_cancelled_orphan_job(11)]}, newest_by_branch={"pr": 2}
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_SUPERSEDED
    assert outcomes == {}
    assert api.posts == []


def test_a_cancelled_run_whose_jobs_had_runners_is_not_an_orphan() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    job = _cancelled_orphan_job(11)
    job["runner_name"] = "some-runner"
    api = FakeApi({"cancelled": [run]}, {1: [job]}, newest_by_branch={"main": 1})
    verdicts, outcomes = _sweep(api)
    assert not any(v.run_id == 1 for v in verdicts)
    assert outcomes == {}
    assert not any("branch=" in p for p in api.gets)  # the newest-run lookup was never needed


def test_a_cancelled_run_with_a_short_queue_wait_is_not_an_orphan() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    api = FakeApi(
        {"cancelled": [run]},
        {1: [_cancelled_orphan_job(11, queued_minutes=3)]},
        newest_by_branch={"main": 1},
    )
    _, outcomes = _sweep(api)
    assert outcomes == {}
    assert api.posts == []


def test_recovery_only_looks_inside_its_window() -> None:
    run = _run(
        1, minutes_ago=300, status="completed", conclusion="cancelled", updated_minutes_ago=200
    )
    api = FakeApi(
        {"cancelled": [run]}, {1: [_cancelled_orphan_job(11)]}, newest_by_branch={"main": 1}
    )
    _, outcomes = _sweep(api)
    assert outcomes == {}
    assert not any("/runs/1/jobs" in p for p in api.gets)


def test_recovery_respects_the_attempt_cap_and_the_fork_guard() -> None:
    capped = _run(
        1,
        minutes_ago=70,
        attempt=3,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        branch="feat",
    )
    fork = _run(
        2,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        fork=True,
    )
    api = FakeApi(
        {"cancelled": [capped, fork]},
        {1: [_cancelled_orphan_job(11)], 2: [_cancelled_orphan_job(21, run_id=2)]},
        newest_by_branch={"feat": 1, "main": 2},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_ATTEMPT_CAP
    assert _verdict_of(verdicts, 2).verdict == wd.SKIPPED_FORK
    assert outcomes == {}
    assert api.posts == []


def test_recovery_takes_the_per_tick_cap_before_live_heals_so_a_backlog_cannot_starve_it() -> None:
    """Five live orphans every tick must not keep a cancelled orphan waiting until its
    recovery window expires: the cancelled run has already lost its verdict, the live
    ones lose nothing by waiting one more tick."""
    live = [_run(i, minutes_ago=100 - i, branch=f"pr-{i}") for i in range(1, 6)]
    cancelled = _run(
        9, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5
    )
    api = FakeApi(
        {"in_progress": live, "cancelled": [cancelled]},
        {
            **{i: [_job(i * 10, run_id=i)] for i in range(1, 6)},
            9: [_cancelled_orphan_job(91, run_id=9)],
        },
        newest_by_branch={"main": 9},
    )
    _, outcomes = _sweep(api, max_runs=5)
    assert outcomes[9] == wd.OUTCOME_RECOVERED
    assert api.posts[0] == f"repos/{REPO}/actions/runs/9/rerun"  # recovery went first
    healed = [run_id for run_id, outcome in outcomes.items() if outcome == wd.OUTCOME_HEALED]
    assert len(healed) == 4  # five slots, one spent on recovery
    assert outcomes[5] == wd.OUTCOME_NOT_ATTEMPTED  # the youngest live orphan waits a tick


def test_recovery_itself_is_bounded_by_the_per_tick_cap() -> None:
    cancelled = [
        _run(
            i,
            minutes_ago=100 - i,
            status="completed",
            conclusion="cancelled",
            updated_minutes_ago=5,
            branch=f"pr-{i}",
        )
        for i in range(1, 8)
    ]
    api = FakeApi(
        {"cancelled": cancelled},
        {i: [_cancelled_orphan_job(i * 10, run_id=i)] for i in range(1, 8)},
        newest_by_branch={f"pr-{i}": i for i in range(1, 8)},
    )
    _, outcomes = _sweep(api, max_runs=5)
    assert sum(1 for o in outcomes.values() if o == wd.OUTCOME_RECOVERED) == 5
    assert sum(1 for o in outcomes.values() if o == wd.OUTCOME_NOT_ATTEMPTED) == 2


def test_a_pull_request_run_at_its_open_head_is_healed() -> None:
    """A pull-request run is judged by HEAD SHA against the open pull requests on
    its branch, not by the branch-name listing (which two pull requests sharing one
    head branch defeat). At an open head it is current, so a cancelled orphan is
    re-run and a live orphan is cancelled and re-run, exactly like a `main` push.
    """
    cancelled = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        event="pull_request",
        branch="pr",
    )
    api = FakeApi(
        {"cancelled": [cancelled]},
        {1: [_cancelled_orphan_job(11)]},
        newest_by_branch={"pr": 1},
        open_pull_heads={"pr": ["sha-1"]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.CANCELLED_ORPHAN
    assert outcomes == {1: wd.OUTCOME_RECOVERED}
    assert api.posts == [f"repos/{REPO}/actions/runs/1/rerun"]
    # The judgement read the pulls API, not `pull_requests[]` on the run payload.
    assert any(path.startswith(f"repos/{REPO}/pulls?") for path in api.gets)

    live = FakeApi(
        {"in_progress": [_run(2, event="pull_request", branch="pr")]},
        {2: [_job(21)]},
        newest_by_branch={"pr": 2},
        open_pull_heads={"pr": ["sha-2"]},
    )
    live_verdicts, live_outcomes = _sweep(live)
    assert _verdict_of(live_verdicts, 2).verdict == wd.ORPHANED
    assert live_outcomes == {2: wd.OUTCOME_HEALED}
    assert live.posts == [
        f"repos/{REPO}/actions/runs/2/cancel",
        f"repos/{REPO}/actions/runs/2/rerun",
    ]


def test_a_pr_keyed_workflow_orphan_is_healed_on_its_pull_request_run() -> None:
    """`code-review.yml` is PR-keyed and declared safe for pull-request runs only.

    Both orphan shapes are healed on a pull-request run, and the revision gate --
    read from the real workflow file, whose group is
    `code-review-${{ github.event.pull_request.number }}` -- admits it because the
    run's EVENT reaches that read. A push-shaped orphan of the same workflow is
    exempt: there the PR number is empty and the group a constant.
    """
    cancelled = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        event="pull_request",
        branch="pr",
        workflow="code-review.yml",
    )
    live = _run(2, event="pull_request", branch="pr2", workflow="code-review.yml")
    api = FakeApi(
        {"cancelled": [cancelled], "in_progress": [live]},
        {1: [_cancelled_orphan_job(11)], 2: [_job(21)]},
        newest_by_branch={"pr": 1, "pr2": 2},
        open_pull_heads={"pr": ["sha-1"], "pr2": ["sha-2"]},
    )
    verdicts, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_RECOVERED, 2: wd.OUTCOME_HEALED}
    assert _verdict_of(verdicts, 1).verdict == wd.CANCELLED_ORPHAN
    assert _verdict_of(verdicts, 2).verdict == wd.ORPHANED

    pushed = FakeApi(
        {"in_progress": [_run(3, event="push", branch="main", workflow="code-review.yml")]},
        {3: [_job(31)]},
        newest_by_branch={"main": 3},
    )
    push_verdicts, push_outcomes = _sweep(pushed)
    assert _verdict_of(push_verdicts, 3).verdict == wd.HEAL_EXEMPT
    assert push_outcomes == {3: wd.OUTCOME_HUMAN_REQUIRED}
    assert pushed.posts == []


def test_two_pull_requests_sharing_a_head_branch_do_not_supersede_each_other() -> None:
    """The case a branch-name successor check cannot handle.

    Two pull requests open on `shared` (different bases) both have head SHA `sha-1`.
    Each carries its own run at that SHA, and the branch listing shows the OTHER
    request's run as newer. By branch name run 1 would read superseded and its
    cancelled orphan left behind a green `skipped-superseded`; by head SHA it is
    current -- the SHA is an open head -- and, the two payloads naming disjoint
    pull requests, the newer run is a sibling's and not a successor: run 1 is re-run.
    """
    mine = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        event="pull_request",
        branch="shared",
        head_sha="sha-1",
        pr_numbers=(101,),
    )
    theirs = _run(
        2,
        minutes_ago=69,
        event="pull_request",
        branch="shared",
        head_sha="sha-1",
        pr_numbers=(102,),
    )
    api = FakeApi(
        {"cancelled": [mine], "in_progress": [theirs]},
        {1: [_cancelled_orphan_job(11)], 2: []},
        newest_by_branch={"shared": 2},
        open_pull_heads={"shared": ["sha-1", "sha-1"]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.CANCELLED_ORPHAN
    assert outcomes[1] == wd.OUTCOME_RECOVERED
    assert f"repos/{REPO}/actions/runs/1/rerun" in api.posts


def test_a_newer_run_at_the_same_sha_from_the_same_pull_request_supersedes() -> None:
    """`labeled`, `unlabeled`, `edited` and `reopened` start a new run at the SAME head
    SHA, and the group cancels the older one. Re-running the older run would cancel
    the newer through the same group and stand in its place. When both payloads name
    the same pull request, the newer run IS the successor: the older is superseded,
    not re-run, and the successor is what a post-re-run check would restore."""
    older = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        event="pull_request",
        branch="pr",
        head_sha="sha-1",
        pr_numbers=(101,),
    )
    labeled = _run(
        2, minutes_ago=10, event="pull_request", branch="pr", head_sha="sha-1", pr_numbers=(101,)
    )
    api = FakeApi(
        {"cancelled": [older], "in_progress": [labeled]},
        {1: [_cancelled_orphan_job(11)], 2: []},
        newest_by_branch={"pr": 2},
        open_pull_heads={"pr": ["sha-1"]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_SUPERSEDED
    assert outcomes == {}
    assert api.posts == []
    judged = wd._base_verdict(older, _policy().now)
    assert wd.current_or_successor_id(api, REPO, judged) == 2


def test_a_newer_run_at_the_same_sha_of_unknown_pull_request_fails_closed() -> None:
    """Same SHA, newer run, and at least one payload names no pull request (most
    same-repository runs name none): successor or sibling cannot be told, and
    "current" would license a cancel through the other run's group. Fail closed:
    `lookup-inconclusive`, a failed outcome that names both runs, nothing posted.
    Not resolved by watching the group's cancel either -- during the fleet outage
    every orphan lives in, the run to watch stays queued and nothing settles."""
    older = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        event="pull_request",
        branch="pr",
        head_sha="sha-1",
    )
    newer = _run(2, minutes_ago=10, event="pull_request", branch="pr", head_sha="sha-1")
    api = FakeApi(
        {"cancelled": [older], "in_progress": [newer]},
        {1: [_cancelled_orphan_job(11)], 2: []},
        newest_by_branch={"pr": 2},
        open_pull_heads={"pr": ["sha-1"]},
    )
    verdicts, outcomes = _sweep(api)
    verdict = _verdict_of(verdicts, 1)
    assert verdict.verdict == wd.LOOKUP_INCONCLUSIVE
    assert "run(s) 2" in verdict.detail and "same head SHA" in verdict.detail
    assert outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert wd.OUTCOME_LOOKUP_FAILED in wd.FAILED_OUTCOMES
    assert api.posts == []
    # The newer run itself is the newest at the SHA, so IT is current and healable
    # (aged past the orphan threshold here; above it was young only to be newer).
    live = FakeApi(
        {"in_progress": [{**newer, "created_at": _ts(60)}]},
        {2: [_job(21)]},
        newest_by_branch={"pr": 2},
        open_pull_heads={"pr": ["sha-1"]},
    )
    _, live_outcomes = _sweep(live)
    assert live_outcomes == {2: wd.OUTCOME_HEALED}


def test_a_same_sha_run_that_appears_after_the_re_run_withdraws_it_and_fails() -> None:
    """The pre-check found the run current; a same-SHA run of unknown pull request
    appears in the settle window. The re-run may have displaced it through the group,
    and nothing can restore it on a guess: the re-run is cancelled and the outcome is
    a failed one naming the situation, never `cancelled-and-rerun`."""
    run = _run(1, event="pull_request", branch="pr", head_sha="sha-1")
    late = _run(2, minutes_ago=1, event="pull_request", branch="pr", head_sha="sha-1")
    api = FakeApi(
        {"in_progress": [run]},
        {1: [_job(11)]},
        newest_by_branch={"pr": 1},
        open_pull_heads={"pr": ["sha-1"]},
    )
    original_get = api.get

    def appearing_get(path: str) -> Any:
        if api._rerun_seen and "branch=pr" in path:
            api.gets.append(path)
            return {"workflow_runs": [late, run]}
        return original_get(path)

    api.get = appearing_get  # type: ignore[method-assign]
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert api.posts == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
        f"repos/{REPO}/actions/runs/1/cancel",
    ]


def test_a_live_pull_request_orphan_with_an_ambiguous_same_sha_run_is_not_cancelled() -> None:
    """The cancel comes before the re-run's successor check. A live pull-request
    orphan whose same-SHA newer run cannot be told from a sibling's is therefore
    judged BEFORE the cancel: an inconclusive answer leaves it untouched (no POST at
    all) under the failed outcome that names it, instead of cancelling it and then
    discovering nobody can re-run it. A failed pulls read is held the same way."""
    older = _run(1, minutes_ago=70, event="pull_request", branch="pr", head_sha="sha-1")
    newer = _run(2, minutes_ago=10, event="pull_request", branch="pr", head_sha="sha-1")
    api = FakeApi(
        {"in_progress": [older, newer]},
        {1: [_job(11)], 2: []},
        newest_by_branch={"pr": 2},
        open_pull_heads={"pr": ["sha-1"]},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.ORPHANED
    assert outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert api.posts == []

    unreadable = FakeApi(
        {"in_progress": [older]},
        {1: [_job(11)]},
        newest_by_branch={"pr": 1},
        open_pull_heads={"pr": wd.ApiError(500, "boom")},
    )
    _, held_outcomes = _sweep(unreadable)
    assert held_outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert unreadable.posts == []
    # A push orphan keeps its one-listing budget: no pre-cancel lookup, cancel then re-run.
    pushed = FakeApi(
        {"in_progress": [_run(3, minutes_ago=70)]}, {3: [_job(31)]}, newest_by_branch={"main": 3}
    )
    _, push_outcomes = _sweep(pushed)
    assert push_outcomes == {3: wd.OUTCOME_HEALED}
    assert pushed.posts == [
        f"repos/{REPO}/actions/runs/3/cancel",
        f"repos/{REPO}/actions/runs/3/rerun",
    ]


def test_a_pull_request_run_whose_head_moved_is_superseded_and_its_successor_named() -> None:
    """A newer push moved the head: the old run is superseded. Its successor is named
    only when the payloads identify a newer run at the open head as the SAME pull
    request's; an unidentified one is `SUPERSEDED_SUCCESSOR_UNIDENTIFIED` -- superseded
    for every judgement before a mutation, never a run to restore."""
    old = wd.RunVerdict(
        run_id=1,
        run_attempt=1,
        head_branch="pr",
        head_repo=REPO,
        event="pull_request",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
        head_sha="sha-1",
        pull_request_numbers=(101,),
    )
    stale_newer = _run(3, minutes_ago=30, event="pull_request", branch="pr", head_sha="sha-old")
    successor = _run(
        2, minutes_ago=40, event="pull_request", branch="pr", head_sha="sha-2", pr_numbers=(101,)
    )
    judged = _run(
        1, minutes_ago=60, event="pull_request", branch="pr", head_sha="sha-1", pr_numbers=(101,)
    )
    api = FakeApi(
        {"in_progress": [stale_newer, successor, judged]},
        {},
        newest_by_branch={"pr": 3},
        open_pull_heads={"pr": ["sha-2"]},
    )
    assert wd.current_or_successor_id(api, REPO, old) == 2
    assert not wd.is_current_run(api, REPO, old)

    unidentified = _run(2, minutes_ago=40, event="pull_request", branch="pr", head_sha="sha-2")
    anonymous = FakeApi(
        {"in_progress": [stale_newer, unidentified, judged]},
        {},
        newest_by_branch={"pr": 3},
        open_pull_heads={"pr": ["sha-2"]},
    )
    assert wd.current_or_successor_id(anonymous, REPO, old) == wd.SUPERSEDED_SUCCESSOR_UNIDENTIFIED
    assert not wd.is_current_run(anonymous, REPO, old)
    # Head moved and the new head's run is not listed yet: the same answer, since an
    # unlisted run may exist and be the one a re-run would displace.
    unlisted = FakeApi(
        {"in_progress": [stale_newer, judged]},
        {},
        newest_by_branch={"pr": 3},
        open_pull_heads={"pr": ["sha-2"]},
    )
    assert wd.current_or_successor_id(unlisted, REPO, old) == wd.SUPERSEDED_SUCCESSOR_UNIDENTIFIED


def test_a_pull_request_run_whose_pull_request_closed_has_no_successor() -> None:
    """No open pull request has the branch as head: superseded, nothing to restore.

    A cancelled orphan of a closed pull request is not re-run (the cancel already
    happened; nobody wants the result), and a live one is cancelled only -- freeing
    the queue -- with the `superseded-before-cancel` outcome, which is not a failure.
    """
    cancelled = _run(
        1,
        minutes_ago=70,
        status="completed",
        conclusion="cancelled",
        updated_minutes_ago=5,
        event="pull_request",
        branch="closed-pr",
    )
    api = FakeApi(
        {"cancelled": [cancelled]},
        {1: [_cancelled_orphan_job(11)]},
        newest_by_branch={"closed-pr": 1},
        open_pull_heads={},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.SKIPPED_SUPERSEDED
    assert outcomes == {}
    assert api.posts == []


def test_a_pull_request_re_run_that_lands_on_a_closed_pull_request_is_withdrawn() -> None:
    """The pull request closes between the pre-check and the post-check: the re-run
    is cancelled again and nothing is restored, under a non-failed outcome."""
    run = _run(1, event="pull_request", branch="pr", head_sha="sha-1")
    api = FakeApi(
        {"in_progress": [run]},
        {1: [_job(11)]},
        newest_by_branch={"pr": 1},
        open_pull_heads={"pr": ["sha-1"]},
    )
    original_get = api.get

    def closing_get(path: str) -> Any:
        if path.startswith(f"repos/{REPO}/pulls?") and api._rerun_seen:
            api.gets.append(path)
            return []
        return original_get(path)

    api.get = closing_get  # type: ignore[method-assign]
    verdicts, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_RERUN_WITHDRAWN}
    assert wd.OUTCOME_RERUN_WITHDRAWN not in wd.FAILED_OUTCOMES
    assert api.posts == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
        f"repos/{REPO}/actions/runs/1/cancel",
    ]


def test_a_re_run_overtaken_by_an_unidentified_head_move_is_withdrawn_and_reported() -> None:
    """The head moves during the settle window and the newer run at it names no pull
    request: the re-run may have displaced this pull request's run through the group,
    and restoring a sibling's would hide that. The re-run is cancelled and the outcome
    is a failed one that names the situation; nothing is restored on a guess."""
    run = _run(1, event="pull_request", branch="pr", head_sha="sha-1")
    moved = _run(2, minutes_ago=1, event="pull_request", branch="pr", head_sha="sha-2")
    api = FakeApi(
        {"in_progress": [run]},
        {1: [_job(11)]},
        newest_by_branch={"pr": 1},
        open_pull_heads={"pr": ["sha-1"]},
    )
    original_get = api.get

    def moving_get(path: str) -> Any:
        if api._rerun_seen and path.startswith(f"repos/{REPO}/pulls?"):
            api.gets.append(path)
            return [{"number": 101, "head": {"sha": "sha-2", "ref": "pr"}}]
        if api._rerun_seen and "branch=pr" in path:
            api.gets.append(path)
            return {"workflow_runs": [moved, run]}
        return original_get(path)

    api.get = moving_get  # type: ignore[method-assign]
    _, outcomes = _sweep(api)
    assert outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert api.posts == [
        f"repos/{REPO}/actions/runs/1/cancel",
        f"repos/{REPO}/actions/runs/1/rerun",
        f"repos/{REPO}/actions/runs/1/cancel",
    ]


def test_an_unreadable_pulls_listing_is_inconclusive_never_current() -> None:
    """The pulls read is the whole judgement for a pull-request run, so a failed read
    is `LookupInconclusive`, and so is a full page (it may have a tail)."""
    verdict = wd.RunVerdict(
        run_id=1,
        run_attempt=1,
        head_branch="pr",
        head_repo=REPO,
        event="pull_request",
        status="in_progress",
        url="https://example.invalid/runs/1",
        age=timedelta(minutes=60),
        verdict=wd.ORPHANED,
        workflow="ci.yml",
        head_sha="sha-1",
    )
    failing = FakeApi({}, {}, open_pull_heads={"pr": wd.ApiError(500, "boom")})
    with pytest.raises(wd.LookupInconclusive):
        wd.current_or_successor_id(failing, REPO, verdict)
    full_page = FakeApi({}, {}, open_pull_heads={"pr": ["sha-1"] * wd.PULL_REQUEST_LISTING_DEPTH})
    with pytest.raises(wd.LookupInconclusive):
        wd.current_or_successor_id(full_page, REPO, verdict)


def test_a_human_cancel_of_a_healthy_run_never_matches_the_recovery_fingerprint() -> None:
    """Jobs that ran on a runner, or were cancelled within seconds of queueing, are not orphans."""
    run = _run(1, minutes_ago=30, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    ran = _job(
        11,
        status="completed",
        conclusion="cancelled",
        minutes_ago=25,
        completed_minutes_ago=5,
        runner_name="r",
    )
    quick = _job(
        12, status="completed", conclusion="cancelled", minutes_ago=6, completed_minutes_ago=5
    )
    api = FakeApi({"cancelled": [run]}, {1: [ran, quick]}, newest_by_branch={"main": 1})
    verdicts, outcomes = _sweep(api)
    assert not any(v.run_id == 1 for v in verdicts)
    assert outcomes == {}
    assert api.posts == []


def test_recovery_reruns_a_cancelled_orphan_even_without_dispatch_evidence() -> None:
    """A cancelled orphan has no finished work left to protect: holding it under an
    outage would only let the recovery window expire and abandon it. Re-running it
    leaves it queued until the fleet returns, which is what the live pass wants."""
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    live = _run(2, minutes_ago=60, branch="other")
    api = FakeApi(
        {"cancelled": [run], "in_progress": [live]},
        {1: [_cancelled_orphan_job(11)], 2: [_job(21, run_id=2)]},
        newest_by_branch={"main": 1, "other": 2},
        evidence=False,
    )
    verdicts, outcomes = _sweep(api)
    # The live orphan is held (nothing to prove the fleet is alive) ...
    assert _verdict_of(verdicts, 2).verdict == wd.SKIPPED_NO_DISPATCH_EVIDENCE
    # ... while the already-cancelled one is re-run regardless.
    assert _verdict_of(verdicts, 1).verdict == wd.CANCELLED_ORPHAN
    assert outcomes == {1: wd.OUTCOME_RECOVERED}
    assert api.posts == [f"repos/{REPO}/actions/runs/1/rerun"]


def test_recovery_reruns_a_cancelled_orphan_even_when_codebuild_is_saturated() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    slow = _run(2, minutes_ago=40, branch="other")
    slow_job = _job(
        21, status="in_progress", minutes_ago=8, started_minutes_ago=2, runner_name="r", run_id=2
    )
    api = FakeApi(
        {"cancelled": [run], "in_progress": [slow]},
        {1: [_cancelled_orphan_job(11)], 2: [slow_job]},
        newest_by_branch={"main": 1},
    )
    verdicts, outcomes = _sweep(api)
    assert _verdict_of(verdicts, 1).verdict == wd.CANCELLED_ORPHAN
    assert outcomes == {1: wd.OUTCOME_RECOVERED}
    assert api.posts == [f"repos/{REPO}/actions/runs/1/rerun"]


def test_a_cancelled_orphan_the_listing_never_reaches_is_a_failed_outcome_not_healthy() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    api = FakeApi(
        {"cancelled": [run]}, {1: [_cancelled_orphan_job(11)]}, newest_by_branch={"main": []}
    )
    logged: list[str] = []
    clock = _Clock()
    verdicts, outcomes = wd.run_watchdog(
        api, _policy(), clock=clock.now, sleep=clock.sleep, log=logged.append
    )
    assert _verdict_of(verdicts, 1).verdict == wd.LOOKUP_INCONCLUSIVE
    assert outcomes == {1: wd.OUTCOME_LOOKUP_FAILED}
    assert api.posts == []
    assert any("::error::" in line and "gh run rerun 1" in line for line in logged)
    summary = wd.render_summary(verdicts, outcomes, _policy())
    assert "[1](https://example.invalid/runs/1)" in summary and wd.OUTCOME_LOOKUP_FAILED in summary


def test_recovery_in_dry_run_reruns_nothing() -> None:
    run = _run(1, minutes_ago=70, status="completed", conclusion="cancelled", updated_minutes_ago=5)
    api = FakeApi(
        {"cancelled": [run]}, {1: [_cancelled_orphan_job(11)]}, newest_by_branch={"main": 1}
    )
    _, outcomes = _sweep(api, dry_run=True)
    assert outcomes == {1: wd.OUTCOME_DRY_RUN}
    assert api.posts == []

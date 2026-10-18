"""A later tick's pass over cancelled orphans that nobody re-ran.

``recover_cancelled_runs`` classifies the recent cancelled runs, at most
``RECOVERY_CLASSIFY_READS`` of them, and re-runs each orphan through the same ``_rerun``
bracket a live heal uses; ``_flag_unclassified_near_expiry`` reds a tick that leaves a
cancelled run unclassified with less of its window left than one schedule interval. The
knob is read as ``runner_watchdog.RECOVERY_CLASSIFY_READS`` at call time.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

import runner_watchdog
from runner_watchdog import (
    CANCELLED_ORPHAN,
    HEAL_EXEMPT,
    HEALTHY,
    LOOKUP_INCONCLUSIVE,
    OUTCOME_DRY_RUN,
    OUTCOME_HEALED,
    OUTCOME_HUMAN_REQUIRED,
    OUTCOME_LOOKUP_FAILED,
    OUTCOME_NOT_ATTEMPTED,
    OUTCOME_RECOVERED,
    Api,
    ApiError,
)
from runner_watchdog_parts.classify import (
    Policy,
    RunVerdict,
    _base_verdict,
    _label,
    classify_cancelled_run,
    parse_timestamp,
)
from runner_watchdog_parts.heal import Tick, _rerun
from runner_watchdog_parts.heal_safety import _mark_heal_exempt, heal_exempt_workflows
from runner_watchdog_parts.listing import list_jobs, list_recent_cancelled_runs


def _flag_unclassified_near_expiry(
    runs: list[dict[str, Any]],
    policy: Policy,
    verdicts: list[RunVerdict],
    outcomes: dict[int, str],
    log: Callable[[str], None],
) -> None:
    """Refuse a green tick for a cancelled run the classify cap will never reach.

    The cap is a fair trade for a run that will still be listed next tick: it waits
    one interval and costs nothing. It is not a fair trade for a run with less than
    one interval of recovery window left, because there is no next tick for it --
    the window check at the top of the pass drops it before the cap is ever
    consulted, and its verdict is gone. A burst of more cancels than the cap, all
    within one window, is exactly that case: the oldest fill the cap every tick
    while the tail ages out having never been looked at.

    So the tail is walked and the unreachable ones are recorded, which costs NO API
    read -- the decision is the run's own ``updated_at`` against the window and the
    interval. They are reported as ``LOOKUP_INCONCLUSIVE`` / ``OUTCOME_LOOKUP_FAILED``
    rather than a new outcome of their own, because that pair already means the one
    thing being claimed here: the run's state was never determined, so the tick may
    have lost a verdict and must exit nonzero. What the detail adds is WHY nobody
    looked, so an operator reading the summary is not sent hunting for an API error
    that never happened.

    Deliberately NOT a re-run: this pass never established that these runs are
    orphans, and re-running a run a human cancelled on purpose is the harm the
    fingerprint exists to prevent. The outcome asks for a human, it does not act.
    """
    for run in runs:
        created = parse_timestamp(str(run["created_at"]))
        updated = parse_timestamp(str(run.get("updated_at") or run["created_at"]))
        if updated - created < policy.orphan_after:
            # Same zero-read exclusion the pass itself applies: a run that did not
            # live as long as the orphan threshold cannot hold a job that queued
            # past it, so nobody needs to look at it and it must not red the tick.
            continue
        left = policy.recovery_window - (policy.now - updated)
        if left <= timedelta(0) or left > policy.schedule_interval:
            continue
        verdict = _base_verdict(run, policy.now)
        verdict.verdict = LOOKUP_INCONCLUSIVE
        minutes = int(left.total_seconds() // 60)
        verdict.detail = (
            f"the per-tick cap of {runner_watchdog.RECOVERY_CLASSIFY_READS} classify reads was reached before "
            f"this run, and only {minutes}m of its {int(policy.recovery_window.total_seconds() // 60)}m "
            "recovery window remain, so no later tick will list it"
        )
        outcomes[verdict.run_id] = OUTCOME_LOOKUP_FAILED
        verdicts.append(verdict)
        log(
            f"::error::{_label(verdict)} is a cancelled run nobody classified: {verdict.detail}. "
            f"Check it and run `gh run rerun {verdict.run_id}` if it was an orphan."
        )


def recover_cancelled_runs(
    api: Api,
    policy: Policy,
    *,
    budget: int,
    tick: Tick,
    log: Callable[[str], None] = print,
    exempt_workflows: frozenset[str] | None = None,
    verdicts: list[RunVerdict] | None = None,
    outcomes: dict[int, str] | None = None,
) -> tuple[list[RunVerdict], dict[int, str]]:
    """Re-run cancelled orphans that an earlier tick cancelled but nobody re-ran.

    Pull-request runs are covered as well as pushes: a heal whose cancel
    outlives the budget leaves the run ``cancelled`` with nobody to re-run it,
    and that is exactly the case this pass exists for. The fingerprint is what
    keeps it from resurrecting a human's deliberate cancel: it requires a
    cancelled ``codebuild-`` job that never had a runner and had queued past the
    orphan threshold, which a healthy run a human stopped never shows. A human
    who cancels an orphaned run and walks away gets that run re-run once
    (twice at most, by the attempt cap), which is the same thing a heal would
    have done.

    The dispatch hold the live pass obeys does NOT apply here, deliberately.
    That hold protects finished work: cancelling a half-done run into an
    outage discards it. A cancelled orphan has nothing left to discard -- its
    verdict is already lost, and only a re-run can bring it back. Re-running
    into saturation merely queues it; re-running into an outage leaves it
    queued until the fleet returns, which is strictly better than cancelled,
    and if it orphans again the live pass (attempt cap included) takes over.
    Holding it instead would let the recovery window expire under a long
    outage and abandon the run silently, which is the one unrecoverable path.
    """
    # The caller may own these containers. A rate limit raised out of this pass
    # never lands the tuple return, so results the pass ALREADY produced -- a
    # recovery that re-ran a run, consuming one of the tick's five heal slots --
    # would be invisible to the caller's slot accounting and five more live runs
    # would be healed on top of them. Filling the caller's containers as we go
    # means an abort leaves it holding exactly what was done.
    verdicts = [] if verdicts is None else verdicts
    outcomes = {} if outcomes is None else outcomes
    if exempt_workflows is None:
        exempt_workflows = heal_exempt_workflows()
    cancelled_runs = list_recent_cancelled_runs(api, policy.repo, log=log)
    reads_left = runner_watchdog.RECOVERY_CLASSIFY_READS
    for index, run in enumerate(cancelled_runs):
        created = parse_timestamp(str(run["created_at"]))
        updated = parse_timestamp(str(run.get("updated_at") or run["created_at"]))
        if policy.now - updated > policy.recovery_window:
            continue
        if updated - created < policy.orphan_after:
            # Costs no read and excludes no orphan. A job is created no earlier
            # than its run and stops queueing no later than the run's terminal
            # transition, so its queue wait -- the very quantity the fingerprint
            # tests against `orphan_after` -- cannot exceed the run's lifetime.
            # A run that did not live that long therefore cannot hold an orphan.
            # This is what keeps the read bound below from being spent on runs
            # that were never candidates: every `main` push cancels the run it
            # supersedes, and of 1000 consecutive cancelled runs measured over
            # 6.8 days the median lived 4 seconds and only 2 reached
            # `orphan_after`, so the busiest 90-minute window holds 400 cancelled
            # runs but 2 candidates against a bound of 50.
            continue
        if reads_left <= 0:
            log(
                f"reached the per-tick cap of {runner_watchdog.RECOVERY_CLASSIFY_READS} cancelled runs to "
                "classify; the rest are left for the next tick"
            )
            _flag_unclassified_near_expiry(cancelled_runs[index:], policy, verdicts, outcomes, log)
            break
        reads_left -= 1
        try:
            jobs = list_jobs(api, policy.repo, int(run["id"]))
        except ApiError as exc:
            if exc.rate_limited:
                # A rate limit means this run's jobs could not be READ, not that the
                # run is healthy. Swallowing it here hides it from the caller's
                # handler -- the one that records ``aborted-rate-limited`` -- and
                # that outcome exists precisely because "the next tick re-reads" is
                # no answer for a run in the final interval of its window. Let it
                # out; the caller decides whether the reset is close enough to wait.
                raise
            # Nothing has been touched; the next tick's pass reads it again.
            log(f"could not read the jobs of cancelled run {int(run['id'])}: {exc}")
            continue

        def is_newest(run: dict[str, Any] = run) -> bool:
            # Only asked once the orphan fingerprint matched, so a tick that finds
            # nothing costs one listing per cancelled run, not two.
            return runner_watchdog.is_current_run(api, policy.repo, _base_verdict(run, policy.now))

        verdict = _mark_heal_exempt(
            classify_cancelled_run(run, jobs, policy, newest_check=is_newest),
            exempt_workflows,
        )
        if verdict.verdict == HEALTHY:
            continue
        if verdict.verdict == LOOKUP_INCONCLUSIVE:
            outcomes[verdict.run_id] = OUTCOME_LOOKUP_FAILED
            log(
                f"::error::{_label(verdict)} is a cancelled orphan that was NOT re-run: {verdict.detail}. "
                f"Run `gh run rerun {verdict.run_id}` by hand."
            )
        log(f"{_label(verdict)}: {verdict.verdict} -- {verdict.detail}")
        verdicts.append(verdict)
        if verdict.verdict == HEAL_EXEMPT:
            outcomes[verdict.run_id] = OUTCOME_HUMAN_REQUIRED
            continue
        if verdict.verdict != CANCELLED_ORPHAN:
            continue
        if budget <= 0:
            outcomes[verdict.run_id] = OUTCOME_NOT_ATTEMPTED
            continue
        budget -= 1
        if policy.dry_run:
            log(f"[dry-run] would re-run {_label(verdict)}")
            outcomes[verdict.run_id] = OUTCOME_DRY_RUN
            continue
        outcome = _rerun(
            api,
            f"repos/{policy.repo}/actions/runs/{verdict.run_id}",
            verdict,
            policy,
            log,
            tick=tick,
        )
        outcomes[verdict.run_id] = OUTCOME_RECOVERED if outcome == OUTCOME_HEALED else outcome
    return verdicts, outcomes

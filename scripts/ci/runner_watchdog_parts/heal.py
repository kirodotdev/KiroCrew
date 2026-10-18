"""Acting on orphaned runs: the hold decision, the cancel, and the verified re-run.

``heal_runs`` cancels each orphaned run and re-runs it once the cancel lands, inside the
tick's budget (``Tick``); ``resolve_hold`` reads the dispatch evidence to tell a slow fleet
from a lost job; ``_rerun`` brackets each re-run; and ``_restore_successor`` with
``_rerun_cancelled_successor`` yields to a newer run that appeared while one was re-run.
``runner_watchdog`` imports this module and re-exports every name in it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Callable

import runner_watchdog
from runner_watchdog import (
    COMPLETED_SAMPLE,
    COMPLETED_SAMPLE_WORKFLOWS,
    HEAL_EXEMPT,
    MAX_RESTORE_DEPTH,
    NESTED_RERUN_RESERVE_SECONDS,
    ORPHANED,
    OUTCOME_CANCEL_FAILED,
    OUTCOME_CANCEL_TIMED_OUT,
    OUTCOME_COMPLETED_ON_ITS_OWN,
    OUTCOME_DRY_RUN,
    OUTCOME_HEAL_SAFETY_UNKNOWN,
    OUTCOME_HEALED,
    OUTCOME_HUMAN_REQUIRED,
    OUTCOME_LOOKUP_FAILED,
    OUTCOME_NOT_ATTEMPTED,
    OUTCOME_OWN_RERUN_UNCANCELLED,
    OUTCOME_RERUN_ATTEMPT_LEFT,
    OUTCOME_RERUN_DEFERRED,
    OUTCOME_RERUN_REFUSED,
    OUTCOME_RERUN_WITHDRAWN,
    OUTCOME_SUCCESSOR_FINISHED,
    OUTCOME_SUCCESSOR_LOST,
    OUTCOME_SUCCESSOR_RESTORED,
    OUTCOME_SUCCESSOR_SUPERSEDED,
    OUTCOME_SUCCESSOR_UNSETTLED,
    OUTCOME_SUPERSEDED,
    OWN_QUEUE_DISPATCHING,
    OWN_QUEUE_SATURATED,
    POST_RERUN_SETTLE_SECONDS,
    RERUN_ENDPOINT,
    RERUN_RESERVE_SECONDS,
    SKIPPED_NO_DISPATCH_EVIDENCE,
    SKIPPED_PARTIAL_EVIDENCE,
    SKIPPED_SATURATED,
    SUCCESSOR_SETTLE_SECONDS,
    SUPERSEDED_SUCCESSOR_UNIDENTIFIED,
    SUPERSEDED_WITHOUT_SUCCESSOR,
    Api,
    ApiError,
    LookupInconclusive,
    _safe_text,
)
from runner_watchdog_parts.classify import (
    DispatchEvidence,
    Policy,
    RunVerdict,
    _base_verdict,
    _fmt_delta,
    _label,
    classify_run,
    dispatch_queue,
    parse_timestamp,
)
from runner_watchdog_parts.heal_safety import _mark_heal_exempt, workflow_is_heal_safe_at_revision
from runner_watchdog_parts.listing import list_jobs, list_runs
from runner_watchdog_parts.supersession import (
    _rerun_attempt_may_be_cancelled,
    _supersession_is_answerable,
    current_or_successor_id,
)


@dataclass
class _Pending:
    verdict: RunVerdict
    cancelled_at: float
    forced: bool = False


@dataclass
class Tick:
    """This invocation's wall clock: injectable time, and the deadline it must respect."""

    clock: Callable[[], float]
    sleep: Callable[[float], None]
    deadline: float
    started_at: datetime
    started_clock: float

    def remaining(self) -> float:
        return self.deadline - self.clock()

    def now(self) -> datetime:
        """The wall-clock time of THIS moment, not of the tick's start.

        Anything judged for freshness late in a slow tick -- the dispatch
        evidence re-read before a cancel, above all -- must be judged against
        the time it was read, or a start that has aged past the lookback would
        still count as recent and an outage would read as dispatching.
        """
        return self.started_at + timedelta(seconds=self.clock() - self.started_clock)

    def window(self, seconds: float) -> float:
        """The wall-clock instant a wait of ``seconds`` ends, never past the deadline."""
        return min(self.clock() + seconds, self.deadline)

    def pause(self, until: float) -> None:
        """Sleep one poll interval, or less if ``until`` is closer; never a busy loop."""
        self.sleep(min(POST_RERUN_SETTLE_SECONDS, max(0.5, until - self.clock())))


HoldCheck = Callable[[RunVerdict], "tuple[str, str, str | None] | None"]


def heal_runs(
    api: Api,
    verdicts: list[RunVerdict],
    policy: Policy,
    *,
    tick: Tick,
    log: Callable[[str], None] = print,
    hold_check: HoldCheck | None = None,
    prime: Callable[[], None] | None = None,
) -> dict[int, str]:
    """Cancel every orphaned run, then re-run each one as its cancellation lands.

    Two phases so that the wait is shared: five cancellations in flight cost one
    budget, not five. Before each cancel the run and its jobs are re-read and the
    verdict re-derived; a run whose attempt moved on, or that is no longer
    orphaned, is left alone. ``hold_check`` is asked again too, on evidence
    re-read for the occasion: the fleet's state at the top of the tick is not
    its state when the cancel is sent, and a hold that appeared in between
    means the queued job is presumed alive after all. ``prime`` gathers that
    evidence BEFORE the first run is re-read, so nothing but a pure judgement
    and the reserve check stands between a run's re-read and its cancel: a
    runner that picks the job up, or a human who re-runs it, during the sweep
    is seen by the re-read rather than cancelled on a stale verdict.
    """
    outcomes: dict[int, str] = {}
    pending: list[_Pending] = []
    if prime is not None and verdicts:
        prime()
    for verdict in verdicts:
        if verdict.verdict == HEAL_EXEMPT:
            log(f"::warning::{_label(verdict)}: {verdict.detail}")
            outcomes[verdict.run_id] = OUTCOME_HUMAN_REQUIRED
            continue
        run_path = f"repos/{policy.repo}/actions/runs/{verdict.run_id}"
        try:
            fresh_run = api.get(run_path) or {}
            fresh_jobs = list_jobs(api, policy.repo, verdict.run_id)
        except ApiError as exc:
            # Nothing has been touched yet; the next tick re-inspects it.
            log(f"{_label(verdict)} could not be re-read before the heal ({exc}); leaving it")
            outcomes[verdict.run_id] = OUTCOME_NOT_ATTEMPTED
            continue
        fresh = classify_run(fresh_run, fresh_jobs, replace(policy, now=tick.now()))
        if fresh.run_attempt != verdict.run_attempt or fresh.verdict != ORPHANED:
            log(
                f"{_label(verdict)} changed before the heal ({fresh.verdict}: {fresh.detail}); leaving it"
            )
            outcomes[verdict.run_id] = OUTCOME_SUPERSEDED
            continue
        # From here on the run is judged from what was just read, not from the
        # top of the tick: a matrix job that crossed the orphan threshold in
        # between is in the fresh orphan set, and the hold must be judged from
        # the NEWEST orphan the run has now.
        verdict.orphans = fresh.orphans
        verdict.detail = fresh.detail
        verdict.head_sha = fresh.head_sha
        hold = hold_check(verdict) if hold_check is not None else None
        if hold is not None:
            verdict.verdict, verdict.detail, hold_outcome = hold
            if hold_outcome is not None:
                outcomes[verdict.run_id] = hold_outcome
            log(f"::warning::{_label(verdict)}: {verdict.detail} (re-checked before the cancel)")
            continue
        if verdict.run_attempt != 1 and not _rerun_attempt_may_be_cancelled(
            api, policy, verdict, log
        ):
            outcomes[verdict.run_id] = OUTCOME_RERUN_ATTEMPT_LEFT
            continue
        if (
            verdict.run_attempt == 1
            and verdict.event == "pull_request"
            and not _supersession_is_answerable(api, policy, verdict, log)
        ):
            outcomes[verdict.run_id] = OUTCOME_LOOKUP_FAILED
            continue
        if tick.remaining() < RERUN_RESERVE_SECONDS:
            # A cancel is only worth posting if its re-run can still be started
            # and verified inside this tick; otherwise it would discard the
            # run's finished jobs and leave it cancelled until the next tick's
            # recovery pass. Untouched, the run loses nothing by waiting.
            log(
                f"{_label(verdict)}: only {int(tick.remaining())} s of this tick's budget remain, not "
                f"enough to cancel and then verify a re-run; left untouched for the next tick"
            )
            outcomes[verdict.run_id] = OUTCOME_NOT_ATTEMPTED
            continue
        revision_safe = workflow_is_heal_safe_at_revision(
            api, policy.repo, verdict.workflow, verdict.head_sha, event=verdict.event
        )
        if revision_safe is None:
            log(
                f"::error::{_label(verdict)}: its heal safety cannot be established at its own "
                f"revision, so nothing is cancelled; re-run it by hand if it stays stuck"
            )
            outcomes[verdict.run_id] = OUTCOME_HEAL_SAFETY_UNKNOWN
            continue
        if not revision_safe:
            _mark_heal_exempt(verdict, frozenset({verdict.workflow}))
            log(f"::warning::{_label(verdict)}: {verdict.detail} (checked at the run revision)")
            outcomes[verdict.run_id] = OUTCOME_HUMAN_REQUIRED
            continue
        verdict.revision_heal_safe = True
        if policy.dry_run:
            log(f"[dry-run] would cancel and re-run {_label(verdict)}")
            outcomes[verdict.run_id] = OUTCOME_DRY_RUN
            continue
        try:
            api.post(f"{run_path}/cancel")
        except ApiError as exc:
            if not exc.ambiguous:
                log(f"cancel refused for {_label(verdict)}: {exc}")
                outcomes[verdict.run_id] = OUTCOME_CANCEL_FAILED
                continue
            # The cancel may have landed. It is not re-posted (a repeat on a
            # cancelling run is a conflict that would read as a refusal and
            # strand the run cancelled); the run is owned and polled like any
            # accepted cancel, and the force-cancel escalation settles the
            # question either way inside the wait.
            log(
                f"cancel of {_label(verdict)} had an ambiguous result ({exc}); treating it as "
                f"accepted and reconciling from the run's own state"
            )
        else:
            log(f"cancel accepted for {_label(verdict)}")
        pending.append(_Pending(verdict=verdict, cancelled_at=tick.clock()))

    # The cancel wait stops early enough that every re-run it leads to can still
    # be verified inside the tick: the heal budget, or the reserve, whichever
    # comes first.
    deadline = min(
        tick.clock() + policy.heal_budget.total_seconds(),
        tick.deadline - RERUN_RESERVE_SECONDS,
    )
    while pending:
        still_pending: list[_Pending] = []
        for item in pending:
            run_path = f"repos/{policy.repo}/actions/runs/{item.verdict.run_id}"
            try:
                run = api.get(run_path) or {}
            except ApiError as exc:
                # Ownership is kept: the run stays pending and is polled again;
                # if the API never answers, the deadline names it as timed out.
                log(f"could not read {_label(item.verdict)} while waiting for its cancel: {exc}")
                still_pending.append(item)
                continue
            if str(run.get("status") or "") == "completed":
                # The cancel was verified against one attempt and posted a moment
                # later; read back what it actually hit before re-running.
                conclusion = _safe_text(run.get("conclusion"))
                attempt = int(run.get("run_attempt") or item.verdict.run_attempt)
                if conclusion != "cancelled":
                    log(
                        f"{_label(item.verdict)} finished on its own ({conclusion}) before the "
                        f"cancel landed; leaving its verdict alone"
                    )
                    outcomes[item.verdict.run_id] = OUTCOME_COMPLETED_ON_ITS_OWN
                    continue
                if attempt != item.verdict.run_attempt:
                    log(
                        f"::warning::{_label(item.verdict)} had been re-run by hand (now attempt "
                        f"{attempt}) in the moment before the cancel; re-running to restore it"
                    )
                    # The re-run below judges "did somebody else already re-run this" by
                    # comparing attempts, so the verdict must name the attempt the cancel
                    # actually landed on, not the one it was computed for.
                    item.verdict.run_attempt = attempt
                outcomes[item.verdict.run_id] = _rerun(
                    api, run_path, item.verdict, policy, log, tick=tick
                )
                continue
            if (
                not item.forced
                and tick.clock() - item.cancelled_at >= policy.force_cancel_after.total_seconds()
            ):
                # A plain cancel waits for running steps to wind down; force-cancel
                # bypasses `always()` steps and the like so the run completes now.
                try:
                    api.post(f"{run_path}/force-cancel")
                    log(f"force-cancel sent for {_label(item.verdict)}")
                except ApiError as exc:
                    log(f"force-cancel refused for {_label(item.verdict)}: {exc}")
                item.forced = True
            still_pending.append(item)
        pending = still_pending
        if not pending:
            break
        remaining = deadline - tick.clock()
        if remaining <= 0:
            for item in pending:
                outcomes[item.verdict.run_id] = OUTCOME_CANCEL_TIMED_OUT
                log(
                    f"::error::{_label(item.verdict)} was cancelled but did not complete within "
                    f"this tick's wait budget; it has NOT been re-run. The next tick's recovery pass "
                    f"re-runs it if it is still the newest run of its branch; otherwise run "
                    f"`gh run rerun {item.verdict.run_id}` by hand."
                )
            break
        tick.sleep(min(10.0, max(1.0, remaining)))
    return outcomes


def sample_completed_runs(api: Api, policy: Policy, evidence: DispatchEvidence) -> None:
    """Absorb recent completed runs' CodeBuild starts, at most once per evidence set.

    The fleet is shared across every watched workflow, so a prompt start in any
    of them is evidence it is dispatching. To bound the cost, at most
    ``COMPLETED_SAMPLE`` completed runs are read in total across the watched
    workflows, high-traffic workflows first.
    """
    evidence.completed_sampled = True
    remaining = COMPLETED_SAMPLE
    for workflow in COMPLETED_SAMPLE_WORKFLOWS:
        if remaining <= 0:
            break
        for run in list_runs(api, policy.repo, workflow, status="completed", cap=remaining):
            if policy.now - parse_timestamp(str(run.get("updated_at") or run["created_at"])) > (
                policy.saturation_lookback
            ):
                continue
            remaining -= 1
            evidence.absorb(list_jobs(api, policy.repo, int(run["id"])), policy)


def resolve_hold(
    api: Api,
    policy: Policy,
    evidence: DispatchEvidence,
    since: datetime,
    *,
    queue: frozenset[str] | None = None,
) -> tuple[str, str] | None:
    """Whether the fleet's state forbids acting on an orphan that queued at ``since``.

    Called only when there is something to act on, by the live pass and the
    recovery pass alike, so both obey the same hold: re-running into saturation
    or an outage is as wrong for a cancelled orphan as for a live one. Only
    starts after ``since`` count -- a fleet that was dispatching before the
    orphan queued says nothing about the fleet it is waiting on.

    ``queue`` is the orphaned job's own CodeBuild queue (``dispatch_queue`` of its
    labels), and ``DispatchEvidence.own_queue`` is asked first. SATURATED holds:
    that queue is slow now, whatever the rest of the fleet did. DISPATCHING rules
    the label-blind saturation hold out for this queue, and only that: it does NOT
    settle whether the fleet is up now, so the outage question below is still asked
    of the fleet-wide starts, and a fleet that has served nothing lately still holds;
    and it does not lift the partial-evidence hold, because it is read off the starts
    this sweep read while a slow start on this same queue may sit in a run that went
    unread. SILENT (the queue served nothing usable after the orphan queued) leaves
    every label-blind rule in force, because a slow start on ANOTHER label is a fact
    about that label's capacity, not this queue's. The completed-run sample is taken
    at most once per tick, and only when no recent start was seen at all: it cannot
    settle an unread-listing case, which the ``unread_saturation_capable`` branch
    below holds on instead.
    """
    own_queue_prompt = False
    if queue is not None:
        reading, slow = evidence.own_queue(since, policy, queue)
        if reading == OWN_QUEUE_SATURATED and slow is not None:
            return (
                SKIPPED_SATURATED,
                f"the orphaned job's own CodeBuild queue is dispatching slowly ({slow.name} "
                f"started after waiting {_fmt_delta(slow.queued_for)}); queued jobs are presumed "
                f"alive, nothing healed",
            )
        own_queue_prompt = reading == OWN_QUEUE_DISPATCHING
    if evidence.inconclusive(since, policy) and not evidence.completed_sampled:
        # Nothing live has started on CodeBuild lately. Before treating that as
        # an outage, read the newest completed runs: a fleet that finished jobs
        # promptly in the last half hour is dispatching, even if quietly.
        sample_completed_runs(api, policy, evidence)
    slowest = None if own_queue_prompt else evidence.slowest(since, policy)
    if slowest is not None:
        return (
            SKIPPED_SATURATED,
            f"CodeBuild is dispatching slowly ({slowest.name} started after waiting "
            f"{_fmt_delta(slowest.queued_for)}); queued jobs are presumed alive, nothing healed",
        )
    if evidence.inconclusive(since, policy):
        return (
            SKIPPED_NO_DISPATCH_EVIDENCE,
            f"no CodeBuild-routed job has started since the orphaned job queued "
            f"({_fmt_delta(policy.now - since)} ago), so an orphan cannot be told from a fleet outage; "
            f"nothing healed. If this persists, the documented rollback (route the Linux jobs back to "
            f"ubuntu-latest) is the response",
        )
    unread = evidence.unread_saturation_capable(policy)
    if unread:
        # Reached only when the evidence read as DISPATCHING: prompt starts, nothing
        # slow. That verdict authorizes cancelling finished work, and it is not
        # established while a run old enough to hold a slow start went unread. The
        # completed-run sample does not settle it either: it reads the newest
        # completions, so a fleet serving some jobs promptly and queueing others past
        # the threshold can show a prompt start there while the slow one sits in a
        # live run this sweep never read. Hold rather than guess. The callers that
        # judge an orphan first spend ``top_up_unread_evidence`` on exactly these runs,
        # so what remains unread here is a capable set past the top-up bound, or a run
        # that crossed the age line after the top-up.
        #
        # An own-queue DISPATCHING reading does not lift this hold. It is derived from
        # the starts this sweep READ, and a slow start on the orphan's own queue would
        # turn it into SATURATED; an unread run can hold exactly that start, and the
        # retained candidates carry no queue attribution that could rule it out. So
        # the reading rules out the label-blind saturation hold above (a slow start on
        # another label is not about this queue) and nothing more.
        #
        # Counted at THIS call's clock, so a retained unread run that was under the
        # line when the listing was read and has since crossed it turns the hold on
        # from that moment. The cancel phase re-reads the listing once and then judges
        # several cancels against it, so a count frozen at the read would let the
        # last cancel of the phase act on a premise minutes out of date.
        return (
            SKIPPED_PARTIAL_EVIDENCE,
            f"{unread} live run(s) that could hold a slow CodeBuild "
            f"start went unread (per-tick job-read bound {runner_watchdog.LIVE_CLASSIFY_READS}, topped up by at "
            f"most {runner_watchdog.LIVE_EVIDENCE_TOPUP_READS} more reads on a tick with an orphan to judge, all or "
            f"nothing), so saturation cannot be ruled out; nothing healed. Raising the top-up bound "
            f"or narrowing the listing is the response -- it is a job-read budget against the "
            f"shared installation quota, not the API's reachable window",
        )
    return None


def _latest_queue(verdict: RunVerdict) -> datetime:
    """When the run's NEWEST orphaned job queued: the moment the hold is judged from.

    A run with several orphaned jobs (a matrix) queued them at different times.
    Evidence that the fleet was dispatching must postdate ALL of them: a start
    between the oldest and the newest says nothing about the fleet the newest
    is waiting on, and clearing the hold on it would cancel queued work that
    may be about to be picked up.
    """
    return max(orphan.queued_at for orphan in verdict.orphans)


def _orphan_queue(verdict: RunVerdict) -> frozenset[str] | None:
    """The one CodeBuild queue every orphaned job of the run waits in, or None.

    Same-queue evidence speaks for one queue. A matrix run whose orphans span two
    (a Linux shard and a Windows shard, say) gets no same-queue reading: a served
    start on one of them says nothing about the other, and the hold is judged for
    the run as a whole, so it falls back to the label-blind rules.
    """
    queues = {dispatch_queue(orphan.labels) for orphan in verdict.orphans}
    if len(queues) != 1:
        return None
    return next(iter(queues))


def _out_of_time(
    verdict: RunVerdict, depth: int, tick: Tick, log: Callable[[str], None]
) -> str | None:
    """The failed outcome for a re-run the tick can no longer verify, or None if it can.

    A first-level re-run needs the whole restoration chain's reserve; a nested
    one (a successor cancelled by this script's own re-run) needs one more
    level. Either way the run is named: a deferred first-level run is what the
    next tick's recovery pass exists for, a successor is not (a group cancel
    leaves no orphan fingerprint), so it is reported lost and needs a hand.
    """
    remaining = tick.remaining()
    if depth == 0:
        if remaining >= RERUN_RESERVE_SECONDS:
            return None
        log(
            f"::error::{_label(verdict)} is cancelled and NOT re-run: only {int(remaining)} s of "
            f"this tick's budget remain and a re-run needs up to {int(RERUN_RESERVE_SECONDS)} s to be "
            f"verified. The next tick's recovery pass re-runs it if it is still the newest run of its "
            f"branch; otherwise run `gh run rerun {verdict.run_id}` by hand."
        )
        return OUTCOME_RERUN_DEFERRED
    if remaining >= NESTED_RERUN_RESERVE_SECONDS:
        return None
    log(
        f"::error::successor run {verdict.run_id} ended cancelled, but only {int(remaining)} s of this "
        f"tick's budget remain and its re-run needs up to {int(NESTED_RERUN_RESERVE_SECONDS)} s to be "
        f"verified; it was NOT re-run. Run `gh run rerun {verdict.run_id}` by hand (a group cancel "
        f"leaves no orphan fingerprint, so the recovery pass will not find it)."
    )
    return OUTCOME_SUCCESSOR_LOST


def _rerun(
    api: Api,
    run_path: str,
    verdict: RunVerdict,
    policy: Policy,
    log: Callable[[str], None],
    *,
    tick: Tick,
    depth: int = 0,
) -> str:
    """Re-run the run, then make sure the re-run did not land on a newer run's toes.

    The GitHub API has no conditional mutation, so "still newest" is checked
    immediately before the POST and again after it, once the concurrency group
    has had a moment to act. If a newer run of the branch appeared in that
    window, the re-run is cancelled straight away (it is this script's own) and
    the newer run is restored -- through this same function, so its re-run is
    bracketed the same way, down to ``MAX_RESTORE_DEPTH``. A lookup that cannot
    reach a verdict, or a re-run refusal the run's own state does not explain,
    is a failed outcome: the run is still cancelled and nobody has re-run it.

    A re-run is begun only while the tick has ``RERUN_RESERVE_SECONDS`` left
    (one more level, for a nested re-run), checked immediately before the POST:
    that is the chain above at its longest, so once begun it always completes
    inside the budget, and the job's ceiling never cuts it off with a successor
    cancelled and nobody left to notice. A re-run that cannot start is a failed
    outcome that names the run (see ``_out_of_time``).
    """
    if not verdict.revision_heal_safe:
        revision_safe = workflow_is_heal_safe_at_revision(
            api, policy.repo, verdict.workflow, verdict.head_sha, event=verdict.event
        )
        if revision_safe is None:
            log(
                f"::error::{_label(verdict)}: its heal safety cannot be established at its own "
                f"revision, so it is NOT re-run; `gh run rerun {verdict.run_id}` by hand"
            )
            return OUTCOME_HEAL_SAFETY_UNKNOWN
        if not revision_safe:
            _mark_heal_exempt(verdict, frozenset({verdict.workflow}))
            log(f"::warning::{_label(verdict)}: {verdict.detail}")
            return OUTCOME_HUMAN_REQUIRED
        verdict.revision_heal_safe = True
    try:
        if not runner_watchdog.is_current_run(api, policy.repo, verdict):
            log(
                f"{_label(verdict)} was superseded (a newer run of its branch, or its pull request "
                f"closed); not re-running it"
            )
            return OUTCOME_SUPERSEDED
    except LookupInconclusive as exc:
        log(
            f"::error::{_label(verdict)} is cancelled and NOT re-run: {exc}. Run `gh run rerun "
            f"{verdict.run_id}` by hand."
        )
        return OUTCOME_LOOKUP_FAILED
    # Checked HERE, after the lookup and immediately before the POST: the lookup
    # itself can be slow enough to eat the reserve, and a re-run started on a
    # stale check is the one that gets cut off mid-verification.
    out_of_time = _out_of_time(verdict, depth, tick, log)
    if out_of_time is not None:
        return out_of_time
    try:
        api.post(f"{run_path}/{RERUN_ENDPOINT}")
    except ApiError as exc:
        # A refusal is explained only if the run is no longer waiting for one:
        # somebody re-ran it (the attempt moved past the one this verdict names,
        # which is the attempt the cancel landed on) or it is in flight again.
        try:
            current = api.get(run_path) or {}
        except ApiError as read_exc:
            # Unexplained is unexplained: with no read to explain the refusal,
            # fail closed and name the run rather than guess it was re-run.
            log(
                f"::error::re-run refused for {_label(verdict)}: {exc}, and the run could not be read "
                f"back ({read_exc}). It is cancelled and nobody is known to have re-run it; run "
                f"`gh run rerun {verdict.run_id}` by hand."
            )
            return OUTCOME_RERUN_REFUSED
        current_attempt = int(current.get("run_attempt") or 0)
        landed = current_attempt > verdict.run_attempt or current.get("status") != "completed"
        if not landed:
            log(
                f"::error::re-run refused for {_label(verdict)}: {exc}. The run is cancelled and nobody "
                f"has re-run it; run `gh run rerun {verdict.run_id}` by hand."
            )
            return OUTCOME_RERUN_REFUSED
        if not exc.ambiguous:
            log(
                f"re-run of {_label(verdict)} refused ({exc}) because it was already re-run by someone "
                f"else (now attempt {current_attempt}, {current.get('status')})"
            )
            return OUTCOME_SUPERSEDED
        # The POST's result was ambiguous but the run has moved on: it landed.
        # Fall through to the same post-re-run verification as a clean POST.
        log(
            f"the re-run POST for {_label(verdict)} had an ambiguous result ({exc}), but the run is "
            f"now attempt {current_attempt} ({current.get('status')}): it landed"
        )
    else:
        log(f"re-ran {_label(verdict)}")
    for attempt in range(2):
        try:
            newest = current_or_successor_id(api, policy.repo, verdict)
        except LookupInconclusive as exc:
            if verdict.event == "pull_request":
                # A same-SHA run whose pull request cannot be identified appeared in the
                # settle window. If it is this pull request's, the re-run has displaced
                # it through the group and nothing can restore it on a guess; if it is
                # a sibling's, the re-run is harmless but its heal cannot be called done.
                # Withdraw the re-run so nothing runs on the uncertainty, and fail loudly.
                log(
                    f"::error::{_label(verdict)} was re-run, but {exc}. The re-run is cancelled; "
                    f"if a run at this head ends cancelled, `gh run rerun` it by hand -- a group "
                    f"cancel leaves no orphan fingerprint, so the recovery pass will not find it."
                )
                _withdraw(api, run_path, verdict, log)
                return OUTCOME_LOOKUP_FAILED
            log(
                f"::error::{_label(verdict)} was re-run, but whether a newer run superseded it cannot be told: {exc}"
            )
            return OUTCOME_LOOKUP_FAILED
        if newest == verdict.run_id:
            if attempt == 0:
                tick.sleep(POST_RERUN_SETTLE_SECONDS)
                continue
            return OUTCOME_HEALED
        if newest == SUPERSEDED_SUCCESSOR_UNIDENTIFIED:
            # The head moved during the settle window while the pull request stays
            # open. A run at the new head -- listed and unidentified, or not listed
            # yet -- may have been cancelled by this re-run through the group, and
            # restoring a sibling's would hide that. Withdraw the re-run and fail loudly.
            log(
                f"::error::{_label(verdict)} was re-run, but its pull request's head has since "
                f"moved and the run at the new head cannot be identified as this pull request's; "
                f"the re-run is cancelled, and if that newer run ends cancelled it was displaced "
                f"by this one: `gh run rerun` it by hand."
            )
            _withdraw(api, run_path, verdict, log)
            return OUTCOME_LOOKUP_FAILED
        if newest == SUPERSEDED_WITHOUT_SUCCESSOR:
            # The pull request closed between the pre-check and here. The re-run
            # serves a head nobody wants, so it is withdrawn; no open pull request has
            # a run it could have displaced, so nothing is restored.
            log(
                f"{_label(verdict)} was re-run, but its pull request has since closed; cancelling "
                f"the re-run, nothing to restore"
            )
            _withdraw(api, run_path, verdict, log)
            return OUTCOME_RERUN_WITHDRAWN
        return _restore_successor(
            api, run_path, verdict, newest, policy, log, tick=tick, depth=depth
        )
    return OUTCOME_HEALED


def _withdraw(api: Api, run_path: str, verdict: RunVerdict, log: Callable[[str], None]) -> None:
    """Cancel this script's own re-run; a refused cancel is logged, never fatal."""
    try:
        api.post(f"{run_path}/cancel")
    except ApiError as exc:
        log(f"could not cancel the withdrawn re-run of {_label(verdict)}: {exc}")


def _status_or_unknown(api: Api, run_path: str, log: Callable[[str], None]) -> str:
    """A run's status, or ``""`` when the read failed: unknown is never ``completed``."""
    try:
        return str((api.get(run_path) or {}).get("status") or "")
    except ApiError as exc:
        log(f"could not read {run_path.rsplit('/', 1)[1]} this round: {exc}")
        return ""


def _restore_successor(
    api: Api,
    run_path: str,
    verdict: RunVerdict,
    successor_id: int,
    policy: Policy,
    log: Callable[[str], None],
    *,
    tick: Tick,
    depth: int = 0,
) -> str:
    """A newer run appeared while this one was being re-run: yield to it.

    Two things are asynchronous here and both are waited out rather than
    sampled. First this script's own re-run: its cancel is posted and then
    polled to ``completed`` (force-cancel halfway through the window), so that
    the re-run is out of the concurrency group for good before the successor is
    judged -- a re-run still winding down could cancel the successor AFTER it
    was judged. If the re-run has not completed by the end of the window the
    outcome is a failure that names the successor. Then the successor, read
    until it reaches a TERMINAL state or the window closes: one that completes
    ``cancelled`` is re-run, unless a yet-newer run of the branch has taken over
    (then that run carries the verdict and nothing is re-run); one that
    completes any other way finished on its own. A successor still running when
    the window closes is never called settled -- a group cancel can land late,
    and a run the group is cancelling reports ``in_progress`` until it does --
    so it is the unsettled failure that keeps ownership honest: the tick goes
    red and names the run and the command to type if it does end cancelled.
    """
    log(
        f"::warning::{_label(verdict)} was superseded by run {successor_id} while being re-run; "
        f"cancelling the re-run and restoring the successor"
    )
    try:
        api.post(f"{run_path}/cancel")
    except ApiError as exc:
        log(f"could not cancel the re-run of {_label(verdict)}: {exc}")
    # The successor is judged only once this script's own re-run is out of the
    # group for good: a re-run still winding down could cancel the successor
    # AFTER it was judged live. Escalate to force-cancel halfway through the
    # window, and if the re-run still has not completed, fail rather than judge.
    started = tick.clock()
    window_end = tick.window(SUCCESSOR_SETTLE_SECONDS)
    force_at = started + SUCCESSOR_SETTLE_SECONDS / 2
    forced = False
    while _status_or_unknown(api, run_path, log) != "completed":
        now = tick.clock()
        if now >= window_end:
            log(
                f"::error::the re-run of {_label(verdict)} is still not cancelled after "
                f"{int(now - started)} s, so successor run {successor_id} cannot be judged "
                f"safely; it was NOT restored. Check run {successor_id} and `gh run rerun` it if it ends "
                f"cancelled."
            )
            return OUTCOME_OWN_RERUN_UNCANCELLED
        if not forced and now >= force_at:
            try:
                api.post(f"{run_path}/force-cancel")
                log(f"force-cancel sent for the re-run of {_label(verdict)}")
            except ApiError as exc:
                log(f"force-cancel refused for the re-run of {_label(verdict)}: {exc}")
            forced = True
        tick.pause(window_end)

    # Every read below may fail; a failed read is an unknown round, not a
    # verdict. Only a TERMINAL state settles the successor: a window that closes
    # on a still-running successor is the unsettled failure, which names it
    # (the group's cancel leaves no orphan fingerprint for recovery to find).
    successor_path = f"repos/{policy.repo}/actions/runs/{successor_id}"
    started = tick.clock()
    window_end = tick.window(SUCCESSOR_SETTLE_SECONDS)
    while True:
        try:
            successor = api.get(successor_path) or {}
            if str(successor.get("status") or "") == "completed":
                if successor.get("conclusion") != "cancelled":
                    log(f"successor run {successor_id} finished on its own; nothing to restore")
                    return OUTCOME_SUCCESSOR_FINISHED
                return _rerun_cancelled_successor(
                    api, successor, successor_path, verdict, policy, log, tick=tick, depth=depth
                )
        except ApiError as exc:
            log(f"could not read successor run {successor_id} this round: {exc}")
        now = tick.clock()
        if now >= window_end:
            log(
                f"::error::successor run {successor_id} was still running when the "
                f"{int(now - started)} s window closed, so whether the concurrency group cancelled it "
                f"cannot be told yet; it was NOT judged settled. If it ends cancelled, run "
                f"`gh run rerun {successor_id}` by hand: a group cancel leaves no orphan fingerprint, so "
                f"the recovery pass will not find it."
            )
            return OUTCOME_SUCCESSOR_UNSETTLED
        tick.pause(window_end)


def _rerun_cancelled_successor(
    api: Api,
    successor: dict[str, Any],
    successor_path: str,
    verdict: RunVerdict,
    policy: Policy,
    log: Callable[[str], None],
    *,
    tick: Tick,
    depth: int = 0,
) -> str:
    """The successor ended cancelled: re-run it with the same bracket as any re-run.

    The successor's re-run goes through ``_rerun``, so it too is preceded by a
    newest check and followed by a settled second one, and a run that lands in
    ITS window is restored in turn -- until ``MAX_RESTORE_DEPTH``, past which a
    still-newer run is reported as lost rather than chased.
    """
    successor_id = int(successor.get("id") or successor_path.rsplit("/", 1)[1])
    if depth + 1 > MAX_RESTORE_DEPTH:
        log(
            f"::error::successor run {successor_id} ended cancelled while restoring a chain of "
            f"{depth} superseding run(s); not chasing further. Run `gh run rerun {successor_id}` by hand."
        )
        return OUTCOME_SUCCESSOR_LOST
    successor_verdict = _base_verdict({**successor, "id": successor_id}, policy.now)
    out_of_time = _out_of_time(successor_verdict, depth + 1, tick, log)
    if out_of_time is not None:
        return out_of_time
    outcome = _rerun(
        api, successor_path, successor_verdict, policy, log, tick=tick, depth=depth + 1
    )
    if outcome == OUTCOME_HEALED:
        log(f"re-ran successor run {successor_id}")
        return OUTCOME_SUCCESSOR_RESTORED
    if outcome == OUTCOME_SUPERSEDED:
        log(
            f"successor run {successor_id} was itself superseded; the newer run carries the verdict"
        )
        return OUTCOME_SUCCESSOR_SUPERSEDED
    if outcome == OUTCOME_RERUN_REFUSED:
        log(
            f"::error::successor run {successor_id} was cancelled by the re-run of {_label(verdict)} "
            f"and could not be re-run. Run `gh run rerun {successor_id}` by hand."
        )
        return OUTCOME_SUCCESSOR_LOST
    if outcome == OUTCOME_HUMAN_REQUIRED:
        # At depth 0 this outcome means "we declined to touch anything", a healthy
        # tick. Here it means the opposite: OUR re-run's concurrency group cancelled
        # this successor, and its own revision then read heal-unsafe, so we will not
        # restore what we destroyed. That is the same loss as a refused re-run and
        # must carry the same red -- otherwise the run disappears behind a green tick
        # with no orphan fingerprint for recovery to find.
        log(
            f"::error::successor run {successor_id} was cancelled by the re-run of {_label(verdict)} "
            f"and its own revision is not heal-safe, so it was NOT restored. Run "
            f"`gh run rerun {successor_id}` by hand."
        )
        return OUTCOME_SUCCESSOR_LOST
    return outcome

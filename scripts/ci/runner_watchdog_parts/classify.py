"""What one run is, judged from its own payload and jobs.

The run model (``OrphanedJob``, ``RunVerdict``, ``Policy``), the routing-label reading
(``is_codebuild_job``, ``dispatch_queue``), the verdict of a live run (``classify_run``)
and of a cancelled one (``classify_cancelled_run``), and ``DispatchEvidence``: what the
CodeBuild jobs that did get a runner say about the fleet. ``runner_watchdog`` imports
this module and re-exports every name in it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from runner_watchdog import (
    CANCELLED_ORPHAN,
    CODEBUILD_LABEL_PREFIX,
    HEAL_EXEMPT,
    HEALTHY,
    LOOKUP_INCONCLUSIVE,
    ORPHANED,
    OWN_QUEUE_DISPATCHING,
    OWN_QUEUE_SATURATED,
    OWN_QUEUE_SILENT,
    SATURATION_FRACTION,
    SKIPPED_ATTEMPT_CAP,
    SKIPPED_FORK,
    SKIPPED_SUPERSEDED,
    SKIPPED_YOUNG,
    WAITING_ON_GROUP,
    LookupInconclusive,
    _safe_text,
)


@dataclass(frozen=True)
class OrphanedJob:
    name: str
    job_id: int
    labels: tuple[str, ...]
    queued_for: timedelta
    queued_at: datetime


@dataclass
class RunVerdict:
    run_id: int
    run_attempt: int
    head_branch: str
    head_repo: str
    event: str
    status: str
    url: str
    age: timedelta
    verdict: str
    workflow: str
    head_sha: str = ""
    # The pull requests the run payload names, when it names any. Carried only so a
    # newer run at the SAME head SHA can be told to be the same pull request's (its
    # successor) or a sibling's on a shared head branch; absent on most same-repo
    # runs, in which case that question fails closed.
    pull_request_numbers: tuple[int, ...] = ()
    revision_heal_safe: bool = False
    orphans: list[OrphanedJob] = field(default_factory=list)
    detail: str = ""

    @property
    def actionable(self) -> bool:
        return self.verdict in (
            ORPHANED,
            CANCELLED_ORPHAN,
            LOOKUP_INCONCLUSIVE,
            HEAL_EXEMPT,
        )


@dataclass
class Policy:
    """Everything the classifier and the healer need to agree on."""

    repo: str
    now: datetime
    dry_run: bool = False
    orphan_after: timedelta = timedelta(minutes=15)
    group_wait_after: timedelta = timedelta(minutes=30)
    max_attempt: int = 3
    max_runs: int = 5
    heal_budget: timedelta = timedelta(seconds=300)
    # Wall clock for the whole invocation, from the first listing to the last
    # re-run. Mutations that start an ownership chain (a re-run and its
    # verification) are begun only while RERUN_RESERVE_SECONDS remain, so the
    # workflow's `timeout-minutes` -- which must sit comfortably above this --
    # never interrupts one.
    tick_budget: timedelta = timedelta(seconds=540)
    force_cancel_after: timedelta = timedelta(seconds=90)
    recovery_window: timedelta = timedelta(minutes=90)
    # How often the schedule fires (`ci-runner-watchdog.yml`: `*/10`). Only the
    # recovery pass reads it, and only to answer one question: will a cancelled run
    # the per-tick classify cap did not reach still be inside its recovery window
    # when the next tick lists? Below one interval of window the answer is no, and
    # "the rest are left for the next tick" stops being true for that run.
    schedule_interval: timedelta = timedelta(minutes=10)

    @property
    def saturation_wait(self) -> timedelta:
        """How long a served start must have waited to count as saturation evidence.

        A third of the orphan threshold. Raising it to the threshold itself is
        tempting -- measured over 404 CodeBuild starts here, queue wait is 27s
        median and 47s at p90 but 429s at p99 and 709s at the slowest, so a third
        of the threshold reads as saturated on 10 of those starts, and the hold it
        takes is what keeps a stuck run stuck -- but the inference behind a raise
        is unsound for the fleet-wide reading, which is label-blind. A start served
        in seven minutes on ANOTHER label says nothing about the queue the orphan is
        in, so raising the line there widens the window in which a queued-but-alive
        job is cancelled. The same-queue reading (``DispatchEvidence.same_queue``)
        is the comparison a raise would be safe against; the line is shared by both
        readings and is left where the label-blind one needs it, and the raise for
        the same-queue reading is tracked at #13644 rather than made here.
        """
        return self.orphan_after / SATURATION_FRACTION

    @property
    def saturation_lookback(self) -> timedelta:
        return self.orphan_after * 2


def parse_timestamp(value: str) -> datetime:
    """GitHub timestamps are ISO-8601 with a trailing ``Z``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def is_codebuild_job(job: dict[str, Any]) -> bool:
    """A job is CodeBuild-routed when any of its labels starts with ``codebuild-``.

    The label can carry override suffixes after a space (``instance-size:large``),
    so match on the prefix of each label rather than on equality.
    """
    return any(str(label).startswith(CODEBUILD_LABEL_PREFIX) for label in job.get("labels") or [])


_PER_RUN_LABEL_SUFFIX = re.compile(r"-\d+-\d+$")


def dispatch_queue(labels: Iterable[str]) -> frozenset[str]:
    """The CodeBuild queue a job's labels route it to, with the per-run suffix removed.

    A routed job's label is ``codebuild-<project>-<run id>-<run attempt>``, optionally
    followed by an override such as ``instance-size:large``. The project selects the
    CodeBuild project and the override selects its fleet; the run id and attempt
    only exist because CodeBuild requires them in the label and say nothing about
    which queue serves the job. Stripping them makes two jobs in different runs
    that wait on the same project and fleet compare equal, which is the comparison
    the same-queue evidence below needs. Labels that are not CodeBuild-routed are
    kept verbatim.
    """
    queue: set[str] = set()
    for label in labels:
        text = str(label)
        if not text.startswith(CODEBUILD_LABEL_PREFIX):
            queue.add(text)
            continue
        head, _, overrides = text.partition(" ")
        head = _PER_RUN_LABEL_SUFFIX.sub("", head)
        queue.add(f"{head} {overrides}".strip())
    return frozenset(queue)


def is_fork_run(run: dict[str, Any], repo: str) -> bool:
    head = run.get("head_repository") or {}
    if head.get("fork") is True:
        return True
    full_name = str(head.get("full_name") or "")
    return bool(full_name) and full_name.lower() != repo.lower()


def _workflow_of(run: dict[str, Any]) -> str | None:
    """The workflow file a run belongs to, from its ``path`` (``.github/workflows/X.yml``)."""
    name = str(run.get("path") or "").rsplit("/", 1)[-1]
    return name or None


def _base_verdict(run: dict[str, Any], now: datetime) -> RunVerdict:
    created = parse_timestamp(run["created_at"])
    workflow = _workflow_of(run)
    if workflow is None:
        raise ValueError("run payload has no usable workflow path")
    return RunVerdict(
        run_id=int(run["id"]),
        run_attempt=int(run.get("run_attempt") or 1),
        head_branch=_safe_text(run.get("head_branch")),
        head_repo=_safe_text((run.get("head_repository") or {}).get("full_name")),
        event=_safe_text(run.get("event")),
        status=_safe_text(run.get("status")),
        url=_safe_text(run.get("html_url")),
        age=now - created,
        verdict=HEALTHY,
        workflow=workflow,
        head_sha=_safe_text(run.get("head_sha")),
        pull_request_numbers=_pull_request_numbers(run),
    )


def _pull_request_numbers(run: dict[str, Any]) -> tuple[int, ...]:
    """The pull-request numbers a run payload names; empty when it names none."""
    numbers: list[int] = []
    for pull in run.get("pull_requests") or []:
        number = (pull or {}).get("number") if isinstance(pull, dict) else None
        if isinstance(number, int):
            numbers.append(number)
    return tuple(sorted(set(numbers)))


def _guard(
    verdict: RunVerdict, run: dict[str, Any], policy: Policy, actionable_verdict: str
) -> RunVerdict:
    """The guard rails shared by the live and the cancelled shape."""
    if is_fork_run(run, policy.repo):
        verdict.verdict = SKIPPED_FORK
        verdict.detail = "head repository is a fork; the workflow token cannot re-run it"
        return verdict
    # A pull-request run passes here like a push run. Its successor check is by
    # head SHA against the open pull requests on its branch, then by the listing for
    # a newer run at that SHA (`current_or_successor_id`), asked immediately before
    # the cancel (`_supersession_is_answerable`) and again before the re-run.
    if verdict.run_attempt >= policy.max_attempt:
        verdict.verdict = SKIPPED_ATTEMPT_CAP
        verdict.detail = (
            f"already at attempt {verdict.run_attempt}; the watchdog stops at "
            f"{policy.max_attempt} so a run that keeps orphaning is escalated, not looped"
        )
        return verdict
    verdict.verdict = actionable_verdict
    return verdict


def classify_run(run: dict[str, Any], jobs: list[dict[str, Any]], policy: Policy) -> RunVerdict:
    """Decide what, if anything, is wrong with one live (not completed) run."""
    verdict = _base_verdict(run, policy.now)
    if verdict.age < policy.orphan_after:
        verdict.verdict = SKIPPED_YOUNG
        verdict.detail = "younger than the orphan threshold"
        return verdict
    if verdict.status == "completed":
        verdict.verdict = SKIPPED_SUPERSEDED
        verdict.detail = "completed between the listing and the heal"
        return verdict
    if not jobs:
        if verdict.age >= policy.group_wait_after:
            verdict.verdict = WAITING_ON_GROUP
            verdict.detail = (
                "no jobs were created: the run is waiting on its concurrency group, "
                "not on a runner"
            )
        else:
            verdict.detail = "no jobs yet"
        return verdict
    for job in jobs:
        if job.get("status") != "queued" or not is_codebuild_job(job):
            continue
        queued_for = policy.now - parse_timestamp(job["created_at"])
        if queued_for < policy.orphan_after:
            continue
        verdict.orphans.append(_orphan(job, queued_for))
    if not verdict.orphans:
        verdict.detail = "no CodeBuild-routed job has waited past the threshold"
        return verdict
    verdict = _guard(verdict, run, policy, ORPHANED)
    if verdict.verdict == ORPHANED:
        verdict.detail = f"{len(verdict.orphans)} CodeBuild-routed job(s) queued with no runner"
    return verdict


def classify_cancelled_run(
    run: dict[str, Any],
    jobs: list[dict[str, Any]],
    policy: Policy,
    *,
    newest_check: Callable[[], bool],
) -> RunVerdict:
    """Decide whether a *cancelled* run is an orphan whose cancel was never followed by a re-run.

    A cancelled job that never had a runner keeps the orphan's fingerprint: a
    ``codebuild-`` label, an empty runner name, and a queue wait (creation to
    completion) past the threshold. Only the run that still carries its branch's
    verdict is worth re-running (``current_or_successor_id``): anything superseded
    would, re-run, only cancel its successor through the concurrency group, and a
    closed pull request's run has nobody left to want its result.
    """
    verdict = _base_verdict(run, policy.now)
    updated = parse_timestamp(str(run.get("updated_at") or run["created_at"]))
    if policy.now - updated > policy.recovery_window:
        verdict.detail = "cancelled outside the recovery window"
        return verdict
    for job in jobs:
        if job.get("conclusion") != "cancelled" or not is_codebuild_job(job):
            continue
        if job.get("runner_name"):
            continue
        completed_at = job.get("completed_at")
        if not completed_at:
            continue
        queued_for = parse_timestamp(str(completed_at)) - parse_timestamp(job["created_at"])
        if queued_for < policy.orphan_after:
            continue
        verdict.orphans.append(_orphan(job, queued_for))
    if not verdict.orphans:
        verdict.detail = "cancelled, but no job carries the orphan fingerprint"
        return verdict
    if is_fork_run(run, policy.repo):
        # Never re-run, so never worth a branch lookup either.
        return _guard(verdict, run, policy, CANCELLED_ORPHAN)
    try:
        newest = newest_check()
    except LookupInconclusive as exc:
        # A cancelled orphan nobody can safely re-run is a lost verdict, not a
        # healthy run: it is reported as a failed outcome by the caller.
        verdict.verdict = LOOKUP_INCONCLUSIVE
        verdict.detail = f"cancelled orphan, but whether a newer run exists cannot be told: {exc}"
        return verdict
    if not newest:
        verdict.verdict = SKIPPED_SUPERSEDED
        verdict.detail = (
            "a newer run exists for this branch, or its pull request has closed; re-running "
            "this one would only cancel a successor or serve a head nobody wants"
        )
        return verdict
    verdict = _guard(verdict, run, policy, CANCELLED_ORPHAN)
    if verdict.verdict == CANCELLED_ORPHAN:
        verdict.detail = f"cancelled with {len(verdict.orphans)} orphaned CodeBuild-routed job(s) and never re-run"
    return verdict


@dataclass
class DispatchEvidence:
    """What the CodeBuild jobs that DID get a runner say about the fleet right now.

    Every CodeBuild start inside the lookback is kept with its start time and
    its queue wait, and judged RELATIVE TO THE ORPHAN being acted on: only a
    start that happened after the orphaned job queued says anything about the
    fleet the orphan is waiting on. A prompt start from before the orphan
    queued is exactly what the onset of an outage looks like -- the fleet was
    fine, then it was not -- and must not clear the hold. Three states follow
    from the starts that qualify: a slow one means the fleet is SATURATED
    (queued jobs are alive, do not touch them); prompt starts and nothing slow
    mean the fleet is DISPATCHING (a job that has waited past the threshold is
    in no queue at all); none at all is INCONCLUSIVE -- a total outage looks
    exactly like an orphan from the queued side, and cancelling and re-running
    into an outage only discards finished work.
    """

    starts: list[tuple[datetime, timedelta, OrphanedJob]] = field(default_factory=list)
    completed_sampled: bool = False
    # The runs that COULD carry a qualifying slow start and went unread this sweep,
    # kept as run id -> creation time rather than as a count. A run at least
    # ``saturation_wait`` old can hold a served start that waited that long; a younger
    # one cannot. While any such run is unread, "no slow start was seen" does not mean
    # none exists, and the completed-run sample cannot close the gap: it reads the
    # newest completions, so a mixed fleet that serves some jobs promptly and queues
    # others past the threshold can put a prompt start in the sample while the slow one
    # sits in a live run nobody read. Cancelling on that re-queues finished work into
    # the saturation it failed to see.
    #
    # The TIMES are retained, not the verdict they imply, because age is the half that
    # grows while the sweep runs: a run 10 s under the line when the listing was read
    # is over it by the time a later cancel is judged, and a frozen count calls it
    # incapable for the whole phase. ``_since`` retains the starts for the same reason.
    unread_candidates: dict[int, datetime] = field(default_factory=dict)

    def unread_saturation_capable(self, policy: Policy) -> int:
        """How many retained unread runs are old enough NOW to hold a slow start.

        Judged against ``policy.now`` at every call, so a run that crossed
        ``saturation_wait`` after the listing was read is counted from that moment on.
        """
        return sum(
            1
            for created in self.unread_candidates.values()
            if policy.now - created >= policy.saturation_wait
        )

    def absorb(self, jobs: list[dict[str, Any]], policy: Policy) -> None:
        # Every served start is kept, whatever its age. The fleet-wide readings apply
        # the lookback when they are asked (``_since``, ``slowest_served_wait``),
        # because for them a start is evidence about the fleet NOW and ages out; the
        # same-queue reading does not, because for it a start is evidence about
        # whether the orphan ever stood in that line, and that does not age.
        for job in jobs:
            if not is_codebuild_job(job) or not job.get("runner_name") or not job.get("started_at"):
                continue
            started = parse_timestamp(str(job["started_at"]))
            waited = started - parse_timestamp(job["created_at"])
            if waited < timedelta(0):
                # Carried over from an earlier attempt: (re-)created after it started.
                continue
            self.starts.append((started, waited, _orphan(job, waited)))

    def _since(
        self, since: datetime, policy: Policy
    ) -> list[tuple[datetime, timedelta, OrphanedJob]]:
        # The lookback is applied again HERE, against the policy's (current)
        # clock, not only when the start was absorbed: evidence gathered early
        # in a slow tick is judged later, and a start that has aged past the
        # lookback in between no longer says anything about the fleet now.
        floor = max(since, policy.now - policy.saturation_lookback)
        return [entry for entry in self.starts if entry[0] >= floor]

    def recent_starts(self, since: datetime, policy: Policy) -> int:
        return len(self._since(since, policy))

    def slowest(self, since: datetime, policy: Policy) -> OrphanedJob | None:
        slow = [entry for entry in self._since(since, policy) if entry[1] >= policy.saturation_wait]
        if not slow:
            return None
        return max(slow, key=lambda entry: entry[1])[2]

    def same_queue(
        self, since: datetime, policy: Policy, queue: frozenset[str], *, recent: bool
    ) -> list[tuple[datetime, timedelta, OrphanedJob]]:
        """The starts served by the orphaned job's OWN queue after it queued.

        The label-blind evidence above answers "is the fleet dispatching"; this
        answers the narrower question the hold actually needs, "was this job ever in
        the line it is waiting on". A start on the same CodeBuild project and fleet
        (``dispatch_queue``) after the orphan queued is a job that stood in the same
        line and got out of it, so what it says about that line is not diluted by a
        slow start on another label with its own capacity.

        ``recent`` applies the lookback, as the fleet-wide readings do; without it every
        start after ``since`` counts. ``own_queue`` says which is asked when.
        """
        floor = max(since, policy.now - policy.saturation_lookback) if recent else since
        return [
            entry
            for entry in self.starts
            if entry[0] >= floor and dispatch_queue(entry[2].labels) == queue
        ]

    def own_queue(
        self, since: datetime, policy: Policy, queue: frozenset[str]
    ) -> tuple[str, OrphanedJob | None]:
        """What the orphan's own queue says: SATURATED, DISPATCHING, or nothing.

        Recent starts (inside the lookback) are read first and read as the fleet-wide
        rule reads them: one that waited ``saturation_wait`` or more means that queue is
        saturated NOW and is returned with the verdict; prompt ones and nothing slow
        mean it is dispatching. Only when the queue served nothing recently are the
        older starts consulted, and they carry ONE conclusion: if every one of them was
        prompt, the queue served jobs that stood in line with the orphan and the orphan
        was never in that line -- a fact that does not age, unlike the state of the
        fleet. An old slow start with nothing recent is not read as saturation (it says
        nothing about the queue now) and not read as dispatching either; the label-blind
        rules take over. Whether the fleet is up now is the outage hold's question, and
        ``resolve_hold`` still asks it after a DISPATCHING reading.
        """
        recent = self.same_queue(since, policy, queue, recent=True)
        if recent:
            slow = [entry for entry in recent if entry[1] >= policy.saturation_wait]
            if slow:
                return OWN_QUEUE_SATURATED, max(slow, key=lambda entry: entry[1])[2]
            return OWN_QUEUE_DISPATCHING, None
        older = self.same_queue(since, policy, queue, recent=False)
        if older and all(entry[1] < policy.saturation_wait for entry in older):
            return OWN_QUEUE_DISPATCHING, None
        return OWN_QUEUE_SILENT, None

    def saturated(self, since: datetime, policy: Policy) -> bool:
        return self.slowest(since, policy) is not None

    def inconclusive(self, since: datetime, policy: Policy) -> bool:
        return self.recent_starts(since, policy) == 0

    def slowest_served_wait(self, policy: Policy) -> tuple[timedelta, str] | None:
        """The longest queue wait among the starts inside the lookback, slow or not.

        Reported whether or not it crosses ``saturation_wait``, because the margin
        between the two is what says whether the line still discriminates. Measured
        here the slowest served wait was 709s against a 300s line; a reading that
        climbs toward the line means the hold is about to fire on ordinary traffic,
        and one that sits far below it means the line could be raised. Either way it
        is drift a reader should see in the tick's own log rather than have to
        re-measure by hand. Bounded to the lookback like the fleet-wide readings it
        calibrates: a start served hours ago is not the queue the line judges now.
        """
        recent = [
            entry for entry in self.starts if policy.now - entry[0] <= policy.saturation_lookback
        ]
        if not recent:
            return None
        started, waited, job = max(recent, key=lambda entry: entry[1])
        return waited, job.name


def _orphan(job: dict[str, Any], queued_for: timedelta) -> OrphanedJob:
    return OrphanedJob(
        name=_safe_text(job.get("name")),
        job_id=int(job["id"]),
        labels=tuple(_safe_text(label) for label in job.get("labels") or []),
        queued_for=queued_for,
        queued_at=parse_timestamp(job["created_at"]),
    )


def _fmt_delta(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes} min"


def _label(verdict: RunVerdict) -> str:
    return f"run {verdict.run_id} attempt {verdict.run_attempt} ({verdict.head_branch})"

#!/usr/bin/env python3
"""runner_watchdog.py -- unstick CI runs whose CodeBuild-routed jobs lost their runner.

Driven by ``.github/workflows/ci-runner-watchdog.yml``; stdlib only.

The failure mode
----------------
``ci.yml`` routes some Linux jobs to an AWS CodeBuild-hosted runner through a
per-run label, ``codebuild-<project>-<run_id>-<run_attempt>``. CodeBuild starts
one ephemeral just-in-time runner per ``workflow_job.queued`` webhook, and that
runner registers a broker session before it can take the job. The session
handshake is not reliable: when the broker fails the first ``CreateSession`` call
after having half-created the session server-side, every retry is refused with
"a session for this runner already exists", and the runner gives up after a
hard-coded few minutes -- shorter than the ghost session's server-side expiry.
The runner process exits cleanly, CodeBuild records the build as SUCCEEDED, and
nothing ever retries: the label is specific to this run attempt, so no other
runner can pick the job up, and ``timeout-minutes`` does not apply to a job that
was never started. The job sits *queued* until GitHub's own multi-hour pending
limit, the run stays *in_progress*, and on ``main`` -- whose concurrency group
does not cancel in-progress runs -- every later push is held pending behind it
and then evicted by the next push. One orphaned shard blocked verdicts on
``main`` for ten hours.

What this script does
---------------------
Lists ``ci.yml`` runs that are ``queued``, ``in_progress`` or ``pending``, reads
each run's jobs, and calls a run ORPHANED when at least one job is still
``queued``, carries a ``codebuild-`` label, and has waited longer than
``Policy.orphan_after`` (15 minutes). CodeBuild's measured queue-to-start on this
repository is under a minute, so a quarter of an hour is far outside any
legitimate wait. For each orphaned run, oldest first and at most
``Policy.max_runs`` (5) per invocation, it cancels the run, waits for the cancellation to land, then
re-runs it. The re-run creates a new run attempt, which produces fresh
``workflow_job.queued`` webhooks and a fresh runner label.

The re-run is a FULL re-run, not ``rerun-failed-jobs``, because of how the
label is computed: ``ci.yml`` computes it ONCE, in its ``changes`` job, and the
routed jobs read it from that job's outputs. A failed-jobs re-run does not run
``changes`` again, so the re-queued jobs would carry a label whose attempt
suffix is stale, and CodeBuild's documentation does not say whether it honours
that. A full re-run recomputes the label. The cost is one CI round; the
alternative was a ten-hour hold.

Slow is not dead
----------------
A ``codebuild-`` job queued past the threshold is also what CodeBuild account
concurrency saturation looks like, and cancelling and re-running saturated runs
would only add to the contention. The two are told apart by what the OTHER
routed jobs are doing -- counting only starts AFTER the orphaned job queued,
because a fleet that was dispatching before the orphan queued says nothing
about the fleet it is waiting on (the onset of an outage looks exactly like
that). When a CodeBuild job that did get a runner started in that window after
waiting a long time (a third of the orphan threshold or more), CodeBuild is
dispatching slowly and every queued job is presumed alive; the tick reports the
runs as ``saturated`` and heals nothing. When such starts were prompt -- the
normal case, measured in seconds on this repository -- a job that has waited a
quarter of an hour is not in any queue, and is healed. When NOTHING has started
on CodeBuild since the orphan queued (live runs first, then the newest
completed runs), the evidence is inconclusive: from the queued side a
total fleet outage looks exactly like an orphan, and cancelling and re-running
into an outage only discards finished work. The tick holds, says so, and points
at the documented rollback.

Mutations are verified after the fact
-------------------------------------
The API offers no conditional cancel or re-run, so each mutation is bracketed:
the cancel is verified against one attempt beforehand and, once it lands, the
run's attempt and conclusion are read back (a run that finished on its own is
left with its verdict; a run somebody re-ran in the gap is re-run again so
nothing is lost). The re-run is preceded by a newest-of-branch check and
followed by another after a short settle, and if a newer run of the branch
(same head repository -- the listing matches branch NAMES, and forks share
them) appeared in that window the re-run is cancelled, its cancellation is waited
out, and the newer run is read until it reaches a terminal state or the settle
window closes: a cancelled successor is re-run in its place, through the same
bracketed re-run, so a run landing in ITS window is restored in turn (two
levels deep at most) -- unless a yet-newer run has taken the branch over, in
which case that run carries the verdict; one that finished any other way is
left alone. One still running when the window closes is never called settled
(a group cancel can land late); it is a failed outcome (the tick goes red and
names it) rather than a guess. The
branch lookup itself is paged until the run being judged is found and fails
closed if it is not, and a re-run refusal the run's own state does not explain
(nobody else re-ran it) is a failed outcome too.

Healing keeps ownership of what it cancelled
--------------------------------------------
The verdict is re-derived from a fresh read of the run and its jobs immediately
before the cancel, and the cancel is only sent if the SAME attempt is still
orphaned. A human who re-ran the run by hand between the listing and the heal
has moved it to a new attempt, and that attempt is left alone.

Once a cancel is accepted the script owns that run until it has been re-run:
it polls every cancelled run until it reports ``completed``, escalates to
``force-cancel`` when a plain cancel has not landed after
``Policy.force_cancel_after`` (90 seconds; a job still executing an ``always()``
step can hold a cancel open for minutes), and re-runs each run the moment it
completes. If the shared ``Policy.heal_budget`` (5 minutes) is exhausted first,
the run is reported as an error and left for the NEXT tick's recovery pass. The
whole invocation runs inside ``Policy.tick_budget`` (9 minutes): a re-run, whose
verification can take up to ``RERUN_RESERVE_SECONDS`` when a newer run lands in
its window and has to be restored, is begun only while that much remains, so
the workflow's ``timeout-minutes`` (set above the budget) never interrupts one
half-way; a re-run that cannot start in time is reported as an error and left
for the recovery pass,
which looks at recently *cancelled* runs (within ``Policy.recovery_window``,
90 minutes), obeys the same saturation/outage hold as the live pass, recognises
the orphan shape on their cancelled jobs (a ``codebuild-`` label, no runner
name, queued past the threshold when cancelled), and re-runs those that are
still the newest run of their branch. That last test is what makes the pass
safe for runs cancelled by something other than this script: a pull-request
run superseded by a newer push has nothing left to say, and re-running it
would cancel the newer run through the workflow's own concurrency group.

Guard rails
-----------
* A run younger than ``Policy.orphan_after`` is never actionable; its jobs are still
  read, because a slow start inside it is saturation evidence (above).
* Immediately before every re-run the run must still be the newest run of its
  branch and event; otherwise re-running it would cancel its successor through
  the workflow's own concurrency group.
* A run whose head repository is a fork is reported but never touched: forks
  are never routed to CodeBuild, and the workflow token could not re-run them.
* A run at ``Policy.max_attempt`` (3) or beyond is reported but never touched. Every
  intervention bumps the attempt, so this bounds the number of automatic
  interventions per run and stops a run that keeps orphaning from being
  cancelled and re-run forever.
* A cancel the API refuses, or a re-run it refuses (typically 403 when someone
  re-ran it by hand first), is logged and skipped.
* When CodeBuild is observed dispatching slowly (above), nothing is healed.
* A run in the group with ZERO jobs is *waiting on its concurrency group*, not
  orphaned: cancelling it would only lose that push's verdict. It is reported so
  the summary explains what is holding it, and the blocking run is what gets
  healed.

``--dry-run`` performs every read and no write. The exit status is 1 when any
heal ran out of budget or a superseding run could not be restored, so the
workflow run goes red and names the run.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

CODEBUILD_LABEL_PREFIX = "codebuild-"
WORKFLOW_FILE = "ci.yml"
# `pending` runs are held by their concurrency group and have no jobs; they are
# listed so the summary can say WHY they wait, never acted on.
CANDIDATE_STATUSES = ("in_progress", "queued", "pending")
PAGE_SIZE = 100
# A run is re-run whole so `changes` recomputes the per-attempt runner label.
RERUN_ENDPOINT = "rerun"
# A recent CodeBuild start that waited at least this fraction of the orphan
# threshold means CodeBuild is saturated, not that a label is dead.
SATURATION_FRACTION = 3
# When no live run shows a recent CodeBuild start, this many newest completed
# runs are read for one before anything is healed.
COMPLETED_SAMPLE = 10
# How long to wait after a re-run before re-checking that no newer run of the
# branch appeared in the window: GitHub applies the concurrency group's
# cancellation asynchronously.
POST_RERUN_SETTLE_SECONDS = 5.0
# How long to keep reading a successor run after yielding to it, waiting for the
# group's asynchronous cancellation to either land (then re-run it) or not.
SUCCESSOR_SETTLE_SECONDS = 30.0
# A successor's own re-run is bracketed exactly like the first one, and its
# successor's in turn; this bounds the chain, since each level needs another
# push to land inside a few seconds' window.
MAX_RESTORE_DEPTH = 2
# The longest a single re-run can take to verify, counting sleeps: the settle
# read, then -- for the first re-run and each of the MAX_RESTORE_DEPTH nested
# ones -- the wait for our own re-run's cancel and the successor window. A
# re-run is started only while at least this much of the tick's budget remains
# (plus a margin for the API reads in between), so the job's ceiling is never
# what ends a restoration: a chain cut off half-way leaves a successor
# cancelled that the recovery fingerprint cannot see (it never queued long).
RESTORE_LEVEL_SECONDS = POST_RERUN_SETTLE_SECONDS + 2 * SUCCESSOR_SETTLE_SECONDS
RESTORE_CHAIN_SECONDS = (MAX_RESTORE_DEPTH + 1) * RESTORE_LEVEL_SECONDS
RERUN_RESERVE_SECONDS = RESTORE_CHAIN_SECONDS + 45.0
# Every wait inside a restoration is a wall-clock window (API latency counts,
# not just the sleeps between reads) capped by the tick's deadline, and a
# NESTED re-run -- of a successor this script's own re-run got cancelled -- is
# begun only while one more level fits. A successor that cannot be re-run in
# time is reported as lost, by name, rather than started and then cut off.
NESTED_RERUN_RESERVE_SECONDS = RESTORE_LEVEL_SECONDS + 15.0
# The branch listing is a name match; same-named branches on forks share it,
# so it is paged (this many per page, at most this many pages) until a run from
# the same head repository is found -- and the run being judged must itself
# appear, or the answer is "cannot tell", never "still newest".
BRANCH_LISTING_DEPTH = 50
BRANCH_LISTING_MAX_PAGES = 4

# Verdicts for one run. The summary groups by these.
ORPHANED = "orphaned"
CANCELLED_ORPHAN = "cancelled-orphan"
HEALTHY = "healthy"
SKIPPED_YOUNG = "skipped-young"
SKIPPED_FORK = "skipped-fork"
SKIPPED_ATTEMPT_CAP = "skipped-attempt-cap"
SKIPPED_SUPERSEDED = "skipped-superseded"
SKIPPED_SATURATED = "skipped-saturated"
SKIPPED_NO_DISPATCH_EVIDENCE = "skipped-no-dispatch-evidence"
LOOKUP_INCONCLUSIVE = "lookup-inconclusive"
WAITING_ON_GROUP = "waiting-on-group"

# Outcomes of acting on a run.
OUTCOME_DRY_RUN = "dry-run"
OUTCOME_HEALED = "cancelled-and-rerun"
OUTCOME_RECOVERED = "rerun-after-earlier-cancel"
OUTCOME_SUPERSEDED = "superseded-before-cancel"
OUTCOME_COMPLETED_ON_ITS_OWN = "completed-on-its-own"
OUTCOME_SUCCESSOR_RESTORED = "superseded-after-rerun-successor-restored"
OUTCOME_SUCCESSOR_FINISHED = "superseded-after-rerun-successor-finished"
OUTCOME_SUCCESSOR_LOST = "superseded-after-rerun-successor-lost"
OUTCOME_SUCCESSOR_UNSETTLED = "superseded-after-rerun-successor-unsettled"
OUTCOME_OWN_RERUN_UNCANCELLED = "superseded-after-rerun-own-rerun-still-running"
OUTCOME_SUCCESSOR_SUPERSEDED = "superseded-after-rerun-successor-superseded-too"
OUTCOME_LOOKUP_FAILED = "branch-lookup-inconclusive"

OUTCOME_CANCEL_FAILED = "cancel-failed"
OUTCOME_CANCEL_TIMED_OUT = "cancel-timed-out"
OUTCOME_RERUN_DEFERRED = "rerun-deferred-out-of-time"
OUTCOME_RERUN_REFUSED = "rerun-refused"
OUTCOME_NOT_ATTEMPTED = "not-attempted-cap-reached"

# Outcomes that mean a verdict may have been lost: the tick exits 1 on any of them
# so the workflow run goes red and its log names the run and the command to type.
FAILED_OUTCOMES = frozenset(
    {
        OUTCOME_CANCEL_TIMED_OUT,
        OUTCOME_RERUN_DEFERRED,
        OUTCOME_RERUN_REFUSED,
        OUTCOME_SUCCESSOR_LOST,
        OUTCOME_SUCCESSOR_UNSETTLED,
        OUTCOME_OWN_RERUN_UNCANCELLED,
        OUTCOME_LOOKUP_FAILED,
    }
)


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _safe_text(value: Any) -> str:
    """Escape control characters in text that came from the API.

    Job names, labels, branch names and error bodies are copied into stdout and
    the step summary, and stdout of a privileged job is parsed for workflow
    commands: a job name carrying a newline followed by ``::warning::`` or
    ``::add-mask::`` would otherwise be honoured as one. Every API-sourced
    string is passed through here at ingestion, so no log line has to remember
    to escape on its own. Newlines and other controls come out as their
    ``\\xNN`` / ``\\uNNNN`` spelling, which keeps the value on one line and
    readable, and prefix checks such as the CodeBuild label test are unaffected.
    A branch name escaped here would not match its own API query, but git
    refuses control characters in ref names, so the case cannot arise -- and
    if it somehow did the mismatch fails closed (the judged run is not seen).
    """
    text = str(value) if value is not None else ""
    return _CONTROL_CHARS.sub(lambda m: m.group().encode("unicode_escape").decode("ascii"), text)


class ApiError(Exception):
    """An HTTP error from the GitHub API, with its status code attached.

    ``ambiguous`` marks a MUTATION whose result is unknown: the request was
    sent, and then the response could not be read (or was a server error), so
    the server may or may not have applied it. Such a POST is never retried by
    the client -- a second cancel or re-run on top of one that landed is a
    conflict, and a conflict would be misread as a refusal. The caller
    reconciles from the run's own state instead.
    """

    def __init__(self, status: int, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(f"HTTP {status}: {_safe_text(message)}")
        self.status = status
        self.ambiguous = ambiguous


class Api(Protocol):
    """The two calls this script needs. Faked in tests, HTTP in production."""

    def get(self, path: str) -> Any: ...

    def post(self, path: str) -> None: ...


class GitHubApi:
    """Minimal authenticated GitHub REST client over urllib.

    Retries once on a 5xx or a transport error, because a scheduled watchdog that
    dies on one transient failure heals nothing that tick.
    """

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        *,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._open = opener

    def _request(self, method: str, path: str) -> Any:
        url = path if path.startswith("https://") else f"{self._base_url}/{path.lstrip('/')}"
        request = urllib.request.Request(url, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "kirocrew-ci-runner-watchdog")
        # A read is retried once on any transient failure. A mutation is NOT,
        # whatever the failure: once the request may be on the wire its effect
        # is unknown, and a repeat can only turn "it landed" into a conflict;
        # the caller reconciles from the run's own state.
        mutation = method != "GET"
        attempts = 0
        while True:
            attempts += 1
            try:
                with self._open(request, timeout=30) as response:
                    body = response.read()
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except (OSError, http.client.HTTPException) as body_exc:
                    # The error body is a courtesy; failing to read it must not
                    # turn a classified HTTP error into an unhandled crash.
                    detail = f"(error body unreadable: {type(body_exc).__name__})"
                if exc.code >= 500:
                    if not mutation and attempts == 1:
                        self._sleep(2)
                        continue
                    raise ApiError(exc.code, detail, ambiguous=mutation) from exc
                raise ApiError(exc.code, detail) from exc
            except urllib.error.URLError as exc:
                # Usually the connection itself failed, but a timeout while
                # sending or awaiting the response surfaces here too, and then
                # the request may already have been delivered. A read is
                # retried; a mutation is ambiguous and left to the caller.
                if not mutation and attempts == 1:
                    self._sleep(2)
                    continue
                raise ApiError(0, str(exc.reason), ambiguous=mutation) from exc
            except (OSError, http.client.HTTPException, ValueError) as exc:
                # A timeout or reset while READING the response (the connect
                # succeeded, so it is not a URLError), a partial read
                # (``IncompleteRead`` is an HTTPException, not an OSError), or a
                # truncated body that is not JSON: the same transient class,
                # retried once for a read and surfaced as an ApiError otherwise.
                # For a mutation the request WAS sent, so the error is ambiguous.
                if not mutation and attempts == 1:
                    self._sleep(2)
                    continue
                raise ApiError(0, f"{type(exc).__name__}: {exc}", ambiguous=mutation) from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str) -> None:
        self._request("POST", path)


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
    orphans: list[OrphanedJob] = field(default_factory=list)
    detail: str = ""

    @property
    def actionable(self) -> bool:
        return self.verdict in (ORPHANED, CANCELLED_ORPHAN, LOOKUP_INCONCLUSIVE)


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
    list_cap: int = 50
    heal_budget: timedelta = timedelta(seconds=300)
    # Wall clock for the whole invocation, from the first listing to the last
    # re-run. Mutations that start an ownership chain (a re-run and its
    # verification) are begun only while RERUN_RESERVE_SECONDS remain, so the
    # workflow's `timeout-minutes` -- which must sit comfortably above this --
    # never interrupts one.
    tick_budget: timedelta = timedelta(seconds=540)
    force_cancel_after: timedelta = timedelta(seconds=90)
    recovery_window: timedelta = timedelta(minutes=90)

    @property
    def saturation_wait(self) -> timedelta:
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


def is_fork_run(run: dict[str, Any], repo: str) -> bool:
    head = run.get("head_repository") or {}
    if head.get("fork") is True:
        return True
    full_name = str(head.get("full_name") or "")
    return bool(full_name) and full_name.lower() != repo.lower()


def _base_verdict(run: dict[str, Any], now: datetime) -> RunVerdict:
    created = parse_timestamp(run["created_at"])
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
    )


def _guard(
    verdict: RunVerdict, run: dict[str, Any], policy: Policy, actionable_verdict: str
) -> RunVerdict:
    """The guard rails shared by the live and the cancelled shape."""
    if is_fork_run(run, policy.repo):
        verdict.verdict = SKIPPED_FORK
        verdict.detail = "head repository is a fork; the workflow token cannot re-run it"
        return verdict
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
    completion) past the threshold. Only the newest run of its branch is worth
    re-running: anything older has been superseded and, on a pull request,
    re-running it would cancel its successor through the concurrency group.
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
            "a newer run exists for this branch; re-running this one would only cancel it"
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

    def absorb(self, jobs: list[dict[str, Any]], policy: Policy) -> None:
        for job in jobs:
            if not is_codebuild_job(job) or not job.get("runner_name") or not job.get("started_at"):
                continue
            started = parse_timestamp(str(job["started_at"]))
            if policy.now - started > policy.saturation_lookback:
                continue
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

    def saturated(self, since: datetime, policy: Policy) -> bool:
        return self.slowest(since, policy) is not None

    def inconclusive(self, since: datetime, policy: Policy) -> bool:
        return self.recent_starts(since, policy) == 0


def _orphan(job: dict[str, Any], queued_for: timedelta) -> OrphanedJob:
    return OrphanedJob(
        name=_safe_text(job.get("name")),
        job_id=int(job["id"]),
        labels=tuple(_safe_text(label) for label in job.get("labels") or []),
        queued_for=queued_for,
        queued_at=parse_timestamp(job["created_at"]),
    )


def _runs_path(repo: str) -> str:
    return f"repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs"


def list_runs(api: Api, repo: str, *, status: str, cap: int) -> list[dict[str, Any]]:
    """Newest ``ci.yml`` runs with the given status filter, at most ``cap``, oldest first."""
    runs: list[dict[str, Any]] = []
    page = 1
    while len(runs) < cap:
        # The page size never changes between pages: ``page`` is an offset in
        # units of ``per_page``, so a smaller final page would re-read the head
        # of the listing and never reach the runs it was meant to fetch.
        query = urllib.parse.urlencode({"status": status, "per_page": PAGE_SIZE, "page": page})
        payload = api.get(f"{_runs_path(repo)}?{query}")
        batch = (payload or {}).get("workflow_runs") or []
        if not batch:
            break
        runs.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    del runs[cap:]
    return sorted(runs, key=lambda run: run["created_at"])


def list_candidate_runs(api: Api, repo: str, *, cap: int) -> list[dict[str, Any]]:
    """Live runs across every candidate status, deduplicated, oldest first."""
    seen: dict[int, dict[str, Any]] = {}
    for status in CANDIDATE_STATUSES:
        for run in list_runs(api, repo, status=status, cap=cap):
            seen.setdefault(int(run["id"]), run)
    return sorted(seen.values(), key=lambda run: run["created_at"])


def list_jobs(api: Api, repo: str, run_id: int) -> list[dict[str, Any]]:
    """Every job of the run's latest attempt."""
    jobs: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": PAGE_SIZE, "page": page, "filter": "latest"})
        payload = api.get(f"repos/{repo}/actions/runs/{run_id}/jobs?{query}")
        batch = (payload or {}).get("jobs") or []
        jobs.extend(batch)
        if len(batch) < PAGE_SIZE:
            return jobs
        page += 1


class LookupInconclusive(Exception):
    """The branch listing did not reach the run being judged, so "newest" is unknown."""


def newest_run_id_for_branch(api: Api, repo: str, verdict: RunVerdict) -> int:
    """The newest run of this branch, event AND head repository.

    The listing's ``branch`` filter matches the head branch by NAME, and a fork
    pull request whose branch happens to share the name (``main``, ``patch-1``)
    is listed alongside, newest first. Pages are read until BOTH the newest
    same-head-repository run and the run being judged have been seen: the first
    is the answer, the second is the proof that the listing reached far enough
    to be trusted. A listing that shows an older same-repository run but not the
    run being judged is inconsistent (the judged run exists and is newer), and
    answering "superseded" from it would leave a cancelled run behind with a
    green tick, so the lookup raises instead. So does a listing that runs out of
    pages: "still newest" is the answer that lets a re-run cancel a newer run
    through the concurrency group, and it is never the default.
    """
    newest: int | None = None
    page = 1
    while page <= BRANCH_LISTING_MAX_PAGES:
        query = urllib.parse.urlencode(
            {
                "branch": verdict.head_branch,
                "event": verdict.event,
                "per_page": BRANCH_LISTING_DEPTH,
                "page": page,
            }
        )
        try:
            payload = api.get(f"{_runs_path(repo)}?{query}")
        except ApiError as exc:
            # The listing is the only thing standing between a re-run and a
            # newer run's concurrency group; without it no verdict is safe.
            raise LookupInconclusive(
                f"the branch listing for {verdict.head_repo}:{verdict.head_branch} could not be read: {exc}"
            ) from exc
        runs = (payload or {}).get("workflow_runs") or []
        for run in runs:
            run_id = int(run["id"])
            head_repo = str((run.get("head_repository") or {}).get("full_name") or "")
            if newest is None and head_repo.lower() == verdict.head_repo.lower():
                newest = run_id
            if run_id == verdict.run_id:
                # The judged run is in view, so the newest-first prefix above it
                # is complete: whatever same-repository run came first is the answer.
                return newest if newest is not None else run_id
        if len(runs) < BRANCH_LISTING_DEPTH:
            break
        page += 1
    raise LookupInconclusive(
        f"run {verdict.run_id} of {verdict.head_repo}:{verdict.head_branch} ({verdict.event}) is not "
        f"within the {BRANCH_LISTING_MAX_PAGES * BRANCH_LISTING_DEPTH} newest listed runs, so whether a "
        f"newer run exists cannot be told"
    )


def is_newest_for_branch(api: Api, repo: str, verdict: RunVerdict) -> bool:
    """Re-running a run that a newer push has superseded would cancel the newer run
    through the workflow's own concurrency group, so every re-run checks this first."""
    return newest_run_id_for_branch(api, repo, verdict) == verdict.run_id


def _fmt_delta(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes} min"


def _label(verdict: RunVerdict) -> str:
    return f"run {verdict.run_id} attempt {verdict.run_attempt} ({verdict.head_branch})"


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


HoldCheck = Callable[[RunVerdict], "tuple[str, str] | None"]


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
        hold = hold_check(verdict) if hold_check is not None else None
        if hold is not None:
            verdict.verdict, verdict.detail = hold
            log(f"::warning::{_label(verdict)}: {verdict.detail} (re-checked before the cancel)")
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
    """Absorb the newest completed runs' CodeBuild starts, at most once per evidence set."""
    evidence.completed_sampled = True
    for run in list_runs(api, policy.repo, status="completed", cap=COMPLETED_SAMPLE):
        if policy.now - parse_timestamp(str(run.get("updated_at") or run["created_at"])) > (
            policy.saturation_lookback
        ):
            continue
        evidence.absorb(list_jobs(api, policy.repo, int(run["id"])), policy)


def resolve_hold(
    api: Api, policy: Policy, evidence: DispatchEvidence, since: datetime
) -> tuple[str, str] | None:
    """Whether the fleet's state forbids acting on an orphan that queued at ``since``.

    Called only when there is something to act on, by the live pass and the
    recovery pass alike, so both obey the same hold: re-running into saturation
    or an outage is as wrong for a cancelled orphan as for a live one. Only
    starts after ``since`` count -- a fleet that was dispatching before the
    orphan queued says nothing about the fleet it is waiting on. The
    completed-run sample is taken at most once per tick.
    """
    if evidence.inconclusive(since, policy) and not evidence.completed_sampled:
        # Nothing live has started on CodeBuild lately. Before treating that as
        # an outage, read the newest completed runs: a fleet that finished jobs
        # promptly in the last half hour is dispatching, even if quietly.
        sample_completed_runs(api, policy, evidence)
    slowest = evidence.slowest(since, policy)
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
    try:
        if not is_newest_for_branch(api, policy.repo, verdict):
            log(f"{_label(verdict)} was superseded by a newer run of its branch; not re-running it")
            return OUTCOME_SUPERSEDED
    except LookupInconclusive as exc:
        log(
            f"::error::{_label(verdict)} is cancelled and NOT re-run: {exc}. Run `gh run rerun {verdict.run_id}` by hand."
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
            newest = newest_run_id_for_branch(api, policy.repo, verdict)
        except LookupInconclusive as exc:
            log(
                f"::error::{_label(verdict)} was re-run, but whether a newer run superseded it cannot be told: {exc}"
            )
            return OUTCOME_LOOKUP_FAILED
        if newest == verdict.run_id:
            if attempt == 0:
                tick.sleep(POST_RERUN_SETTLE_SECONDS)
                continue
            return OUTCOME_HEALED
        return _restore_successor(
            api, run_path, verdict, newest, policy, log, tick=tick, depth=depth
        )
    return OUTCOME_HEALED


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
    return outcome


def recover_cancelled_runs(
    api: Api,
    policy: Policy,
    *,
    budget: int,
    tick: Tick,
    log: Callable[[str], None] = print,
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
    verdicts: list[RunVerdict] = []
    outcomes: dict[int, str] = {}
    for run in list_runs(api, policy.repo, status="cancelled", cap=policy.list_cap):
        updated = parse_timestamp(str(run.get("updated_at") or run["created_at"]))
        if policy.now - updated > policy.recovery_window:
            continue
        try:
            jobs = list_jobs(api, policy.repo, int(run["id"]))
        except ApiError as exc:
            # Nothing has been touched; the next tick's pass reads it again.
            log(f"could not read the jobs of cancelled run {int(run['id'])}: {exc}")
            continue

        def is_newest(run: dict[str, Any] = run) -> bool:
            # Only asked once the orphan fingerprint matched, so a tick that finds
            # nothing costs one listing per cancelled run, not two.
            return is_newest_for_branch(api, policy.repo, _base_verdict(run, policy.now))

        verdict = classify_cancelled_run(run, jobs, policy, newest_check=is_newest)
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


_MARKDOWN_SPECIALS = re.compile(r"([\\`*_{}\[\]()<>#+!|~])")


def _md(value: str) -> str:
    """Escape text for interpolation into the step summary's Markdown.

    Branch names, job names and error bodies come from the API, and a fork's
    branch name is chosen by whoever opened the fork. Every Markdown-significant
    character is backslash-escaped so such a value renders as the literal text
    it is and cannot close a table cell, open a link or add a heading. Values
    are never put in code spans, because a backtick inside one cannot be
    escaped. Control characters were already escaped at ingestion.
    """
    return _MARKDOWN_SPECIALS.sub(r"\\\1", value)


def _md_link(verdict: RunVerdict) -> str:
    """``[run_id](url)`` with the URL percent-encoded so it cannot close the link."""
    url = urllib.parse.quote(verdict.url, safe=":/?#@!$&'*+,;=%-._~")
    return f"[{verdict.run_id}]({url})"


def render_summary(verdicts: list[RunVerdict], outcomes: dict[int, str], policy: Policy) -> str:
    lines = ["## CI runner watchdog", ""]
    lines.append(f"Mode: {'dry run' if policy.dry_run else 'live'}.")
    lines.append(f"Inspected {len(verdicts)} run(s).")
    lines.append("")
    acted = [v for v in verdicts if v.actionable]
    reported = [
        v
        for v in verdicts
        if v.verdict
        in (
            SKIPPED_FORK,
            SKIPPED_ATTEMPT_CAP,
            SKIPPED_SUPERSEDED,
            SKIPPED_SATURATED,
            SKIPPED_NO_DISPATCH_EVIDENCE,
            WAITING_ON_GROUP,
        )
    ]
    if not acted and not reported:
        lines.append("Nothing stuck.")
        return "\n".join(lines) + "\n"
    if acted:
        lines.append("| Run | Attempt | Branch | Age | Orphaned jobs | Outcome |")
        lines.append("|---|---|---|---|---|---|")
        for v in acted:
            jobs = "<br>".join(f"{_md(o.name)} ({_fmt_delta(o.queued_for)})" for o in v.orphans)
            outcome = outcomes.get(v.run_id, OUTCOME_NOT_ATTEMPTED)
            lines.append(
                f"| {_md_link(v)} | {v.run_attempt} | {_md(v.head_branch)} | "
                f"{_fmt_delta(v.age)} | {jobs} | {outcome} |"
            )
        lines.append("")
    if reported:
        lines.append("Reported, not acted on:")
        lines.append("")
        for v in reported:
            lines.append(f"- {_md_link(v)} {_md(v.head_branch)} -- {v.verdict}: {_md(v.detail)}")
        lines.append("")
    return "\n".join(lines) + "\n"


def run_watchdog(
    api: Api,
    policy: Policy,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> tuple[list[RunVerdict], dict[int, str]]:
    verdicts: list[RunVerdict] = []
    evidence = DispatchEvidence()
    start = clock()
    tick = Tick(
        clock=clock,
        sleep=sleep,
        deadline=start + policy.tick_budget.total_seconds(),
        started_at=policy.now,
        started_clock=start,
    )
    for run in list_candidate_runs(api, policy.repo, cap=policy.list_cap):
        # Jobs are read for EVERY run, young ones included: a young run is never
        # actionable, but a recent slow CodeBuild start inside it is exactly the
        # saturation evidence that must hold the watchdog back from older runs.
        jobs = list_jobs(api, policy.repo, int(run["id"]))
        verdict = classify_run(run, jobs, policy)
        log(f"{_label(verdict)}: {verdict.verdict} -- {verdict.detail}")
        for orphan in verdict.orphans:
            log(
                f"  queued {_fmt_delta(orphan.queued_for)} with no runner: {orphan.name} {list(orphan.labels)}"
            )
        verdicts.append(verdict)
        evidence.absorb(jobs, policy)

    # The hold is judged per run, relative to when its NEWEST orphaned job
    # queued: a start that postdates an older orphan may still predate a
    # younger one, in another run or in the same one.
    for verdict in verdicts:
        if verdict.verdict != ORPHANED:
            continue
        hold = resolve_hold(api, policy, evidence, _latest_queue(verdict))
        if hold is not None:
            verdict.verdict, verdict.detail = hold
            log(f"::warning::{_label(verdict)}: {verdict.detail}")

    # Recovery goes FIRST and takes the per-tick cap before live heals do. A
    # cancelled orphan has already lost its verdict and only a re-run brings it
    # back, whereas a live orphan loses nothing by waiting one more tick; were
    # live heals served first, a sustained backlog of five live orphans per
    # tick would starve recovery until the cancelled run aged out of the
    # recovery window and its verdict was gone for good.
    recovered, recovered_outcomes = recover_cancelled_runs(
        api, policy, budget=policy.max_runs, tick=tick, log=log
    )
    slots_used = sum(
        1
        for v in recovered
        if v.verdict == CANCELLED_ORPHAN
        and recovered_outcomes.get(v.run_id) != OUTCOME_NOT_ATTEMPTED
    )
    slots_left = max(0, policy.max_runs - slots_used)

    orphaned = [v for v in verdicts if v.verdict == ORPHANED]
    to_heal, deferred = orphaned[:slots_left], orphaned[slots_left:]

    fresh: dict[str, Any] = {}

    def prime_fresh_evidence() -> None:
        # The evidence above is minutes old by the time the cancels start. It is
        # re-read once, BEFORE the first run is re-read for its cancel, and the
        # completed-run sample is taken here too, so judging a run later needs
        # no read at all -- the run's own re-read then sits immediately before
        # its cancel. Read against the wall clock of the sweep; a fleet that
        # cannot be re-read fails closed (nothing cancelled on stale evidence).
        if "evidence" in fresh or "error" in fresh:
            return
        read_at = replace(policy, now=tick.now())
        try:
            latest = DispatchEvidence()
            for run in list_candidate_runs(api, policy.repo, cap=policy.list_cap):
                latest.absorb(list_jobs(api, policy.repo, int(run["id"])), read_at)
            sample_completed_runs(api, read_at, latest)
            fresh["evidence"] = latest
        except ApiError as exc:
            fresh["error"] = exc

    def fresh_hold(verdict: RunVerdict) -> tuple[str, str] | None:
        prime_fresh_evidence()
        if "error" in fresh:
            return (
                SKIPPED_NO_DISPATCH_EVIDENCE,
                f"dispatch evidence could not be re-read before the cancel ({fresh['error']}); "
                f"nothing healed on evidence that may be stale",
            )
        # Judged against the wall clock of THIS moment: a start that has aged
        # past the lookback since the sweep no longer counts. The sample is
        # already taken, so this is a pure judgement -- no read, no delay
        # between the run's own re-read and its cancel.
        return resolve_hold(
            api, replace(policy, now=tick.now()), fresh["evidence"], _latest_queue(verdict)
        )

    outcomes = heal_runs(
        api, to_heal, policy, tick=tick, log=log, hold_check=fresh_hold, prime=prime_fresh_evidence
    )
    for verdict in deferred:
        outcomes[verdict.run_id] = OUTCOME_NOT_ATTEMPTED
        log(
            f"{_label(verdict)}: per-invocation cap of {policy.max_runs} reached; left for the next tick"
        )

    verdicts.extend(recovered)
    outcomes.update(recovered_outcomes)
    return verdicts, outcomes


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def build_parser() -> argparse.ArgumentParser:
    """Every threshold is a ``Policy`` default; the CLI carries only what the workflow varies."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name"
    )
    parser.add_argument("--dry-run", action="store_true", default=_env_flag("DRY_RUN"))
    parser.add_argument(
        "--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""), help="markdown summary path"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.repo:
        print("--repo (or GITHUB_REPOSITORY) is required", file=sys.stderr)
        return 2
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        print("GH_TOKEN or GITHUB_TOKEN is required", file=sys.stderr)
        return 2
    api = GitHubApi(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    policy = Policy(repo=args.repo, now=datetime.now(timezone.utc), dry_run=args.dry_run)
    verdicts, outcomes = run_watchdog(api, policy)
    summary = render_summary(verdicts, outcomes, policy)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(summary)
    else:
        print(summary)
    return 1 if FAILED_OUTCOMES & set(outcomes.values()) else 0


if __name__ == "__main__":
    sys.exit(main())

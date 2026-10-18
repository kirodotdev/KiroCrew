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
Lists ``queued``, ``in_progress`` and ``pending`` runs REPO-WIDE -- one paginated
``GET /repos/{repo}/actions/runs?status=…`` per status returns runs of every
workflow at once -- and keeps only those whose ``path`` names a workflow that
routes jobs to the CodeBuild fleet (``WATCHED_WORKFLOWS`` -- the label
``codebuild-kirocrew-gha`` reaches ``ci.yml``, ``fast-gate.yml``,
``main-ratchet-audit.yml``, ``build.yml`` and eleven others). One listing per
status covers the whole watched set, and covers strictly more than a
per-workflow loop would: a fleet-routed workflow nobody registered is still
returned, and dropped only because it is not watched. It then reads each kept
run's jobs and calls a run ORPHANED when at least one job is still ``queued``,
carries a ``codebuild-`` label, and has waited longer than
``Policy.orphan_after`` (15 minutes). CodeBuild's measured queue-to-start on this
repository is under a minute, so a quarter of an hour is far outside any
legitimate wait. For each orphaned run, oldest first across every watched
workflow and at most ``Policy.max_runs`` (5) per invocation as one global cap,
it cancels the run, waits for the cancellation to land, then
re-runs it. The re-run creates a new run attempt, which produces fresh
``workflow_job.queued`` webhooks and a fresh runner label.

The watched set is a fixed tuple, not read from disk, and it is a client-side
FILTER on the repo-wide listing rather than a set of endpoints to poll: a
workflow that gains a fleet route without being added here goes unhealed even
though the listing returns it, and a workflow that loses one is still filtered in
harmlessly. A test pins the tuple to the set of workflows whose ``runs-on``
actually routes to the fleet, and pins ``ci-runner-watchdog.yml`` out of it --
its own comment names the label, and a watchdog that cancelled and re-ran itself
would never finish a tick.

A GitHub rate limit is survivable, not fatal. A 403 or 429 whose body names a
rate limit is honoured against its ``Retry-After`` / ``X-RateLimit-Reset`` with
one cheap in-budget retry; when the reset is too far off, the tick stops
gathering, acts on the runs it has already classified, and records an
``aborted-rate-limited`` outcome that the summary names. That outcome is a
FAILURE, so ANY aborted tick exits nonzero, however much it classified first:
the same abort skips the cancelled-orphan recovery pass, and a cancelled run in
the last tick-interval of its window ages out before a later tick reaches it.
Every other
status -- 401, 404, 5xx -- and every malformed payload still raises exactly as
before.

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
that).

The orphaned job's OWN queue is asked first. A routed label is
``codebuild-<project>-<run>-<attempt>``, optionally with an ``instance-size``
override; the project and override pick the CodeBuild project and fleet, the run
and attempt are only there because CodeBuild requires them (``dispatch_queue``
strips them). A start served by that same project and fleet after the orphan
queued is a job that stood in the same line and got out of it, so it decides the
hold on its own: one that waited a third of the orphan threshold or more means
that queue is saturated and the tick holds; prompt ones and nothing slow mean the
queue is being served and a job that has waited a quarter of an hour in it is not
in it at all, whatever another label's queue is doing. The typical carrier is the
run's own sibling jobs: a fast-gate run queues fourteen jobs on one label within
a second, and thirteen of them starting in under a minute while the fourteenth
sits for an hour and a half is the dropped-dispatch shape exactly (a slow start on
another label that afternoon held that orphan unhealed for six hours before this
reading existed).

Only when the orphan's own queue served nothing that qualifies -- a single-job
run, or a run whose orphans span two queues -- is the fleet-wide, label-blind
reading used. When a CodeBuild job that did get a runner started in that window
after waiting a long time (a third of the orphan threshold or more), CodeBuild is
dispatching slowly and every queued job is presumed alive; the tick reports the
runs as ``saturated`` and heals nothing. That line is deliberately low BECAUSE
this reading is label-blind: a start served quickly on another label says nothing
about the queue the orphan is in. When such starts were prompt -- the normal case,
27s median and 47s at p90 over 404 measured starts here -- a job that has waited a
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
nothing is lost). Every re-run is preceded by a newest-of-branch check and followed by
another after a short settle. If a newer run of the branch
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
name, queued past the threshold when cancelled), and re-runs them only when
they still carry their branch's verdict: a push run when it is the newest run of
its branch, a pull-request run when its head SHA is the head of an open pull
request on its branch. A run superseded by a newer push, or of a closed pull
request, is left cancelled.

Guard rails
-----------
* A run younger than ``Policy.orphan_after`` is never actionable, and two age bands
  decide what reads it. A run at least ``saturation_wait`` (= ``orphan_after`` / 3) old
  is what the evidence reserve is spent on, since only it can hold a wait that crossed
  the line. A younger one is not reserved a read while any run qualifies, and is read
  only if a classify slot remains after the actionable-shaped and older runs, then only
  because a prompt start inside it is dispatch evidence (above). The one exception is
  the reserve's fallback: when NO run can carry a slow start the reserve takes the
  newest runs regardless of age, because an empty reserve reads as an outage.
* Immediately before every re-run, the run must still be the newest run of
  its branch and event; otherwise it is reported for a human.
* The saturation/outage hold does NOT apply to a ``push`` run a newer push has
  already superseded. The hold protects a queued job whose result someone still
  wants, and that run's result is discarded by the branch moving on, while holding
  it keeps it alive in a ``cancel-in-progress: false`` concurrency group where it
  evicts every later commit's run (``supersession_clears_hold`` carries the measured
  incident). At attempt 1 such a run is cancelled, and the rule above still declines
  to re-run it. Past attempt 1 it is NOT cancelled either -- a later attempt may be
  somebody's own ``gh run rerun``, and cancelling a superseded run is never followed
  by a re-run, so the cancel would discard their work -- and the refusal is a FAILED
  outcome (``rerun-attempt-superseded-left-untouched``) that names the run and the
  ``gh run cancel`` a human must type to free the group
  (``_rerun_attempt_may_be_cancelled``).
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
import base64
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

CODEBUILD_LABEL_PREFIX = "codebuild-"
# Every workflow whose `runs-on` routes at least one job to the CodeBuild
# fleet at this repository. The watchdog lists and heals runs of each. The
# fleet label (`codebuild-kirocrew-gha-…`) is the membership signal; a test
# pins this tuple to the set of workflows whose `runs-on` actually carries it.
# `ci-runner-watchdog.yml` is deliberately absent: the label appears only in
# its own explanatory comment, and a watchdog must never cancel or re-run
# itself out from under a tick.
WATCHED_WORKFLOWS: tuple[str, ...] = (
    "build-wheel.yml",
    "build.yml",
    "ci.yml",
    "code-review.yml",
    "cross-platform.yml",
    "dependency-review.yml",
    "dependency-vulnerability.yml",
    "fast-gate.yml",
    "macos-on-demand.yml",
    "main-ratchet-audit.yml",
    "pages.yml",
    "pr-merge-conflict-label.yml",
    "pr-readiness.yml",
    "pr-scope.yml",
    "release.yml",
    "screenshot-evidence.yml",
    "sensitive-change-review.yml",
)
# Put the workflows that carry routine pull-request traffic ahead of release
# and audit workflows when looking for recent fleet starts.
COMPLETED_SAMPLE_WORKFLOWS: tuple[str, ...] = (
    "ci.yml",
    "fast-gate.yml",
    "code-review.yml",
) + tuple(
    workflow
    for workflow in WATCHED_WORKFLOWS
    if workflow not in {"ci.yml", "fast-gate.yml", "code-review.yml"}
)
# The membership test for a run listed repo-wide: a run's `path` places it in a
# workflow, and only the watched ones are kept. A frozenset because it is
# consulted once per listed run across the whole repo, not once per watched
# workflow.
_WATCHED_SET = frozenset(WATCHED_WORKFLOWS)
# Automatic healing is unsafe unless the workflow's run-level concurrency group
# is keyed on a ref or pull request and a full re-run has no durable side effect.
# Two gates decide it, and a workflow clears BOTH to be healed.
# HEAL_SAFE_WORKFLOWS is the DECLARED gate and the LOAD-BEARING one: a written
# judgement that a full re-run is safe, so a workflow joining WATCHED_WORKFLOWS
# stays exempt until a person adds it here. The derived gate below is
# BEST-EFFORT. It reads each workflow's non-comment YAML, requires the branch or
# pull-request key -- a structural fact it reads reliably -- and rejects the
# publish, deploy and signing spellings it KNOWS. A publish step spelled a way
# the patterns miss -- a new marketplace action, a language toolchain nobody here
# uses yet -- passes it, which is why the declaration is the judgement and this is
# a backstop
# for a listed workflow that later grows a recognized signal. Pinning each
# declared workflow's content instead would expire the declaration on every edit
# to ci.yml or fast-gate.yml, the most-edited files here.
#
# Every entry here is REF-KEYED, and that is a requirement rather than a
# coincidence. Healing is cancel plus a full re-run, whose only protection against
# cancelling a live successor is the successor check. For a PUSH run that check is
# the newest-of-branch listing, which filters by head branch, event and head
# repository -- never by pull-request number. Two pull requests can share a head
# branch (different base branches), so for a PR-KEYED concurrency group another
# PR's newer run would be read as a push run's successor. A push run of a PR-keyed
# workflow has no group at all (`github.event.pull_request.number` is empty on a
# push), so such a workflow can only ever be healed on its pull-request runs, and
# it is declared in HEAL_SAFE_PULL_REQUEST_WORKFLOWS below instead.
#
# `macos-on-demand.yml` is ref-keyed and triggered only by `pull_request`. Its
# runs are pull-request runs, which are healed (see `current_or_successor_id`), so
# the declaration is reachable; a test pins that every declared workflow has a
# trigger a heal can act on.
HEAL_SAFE_WORKFLOWS: frozenset[str] = frozenset(
    {
        "build.yml",
        "ci.yml",
        "fast-gate.yml",
        "macos-on-demand.yml",
    }
)
# Declared heal-safe for PULL-REQUEST runs only. Every entry is PR-KEYED
# (`<name>-${{ github.event.pull_request.number }}`, `cancel-in-progress: true`)
# and triggered only by `pull_request`, so no push run of these exists to judge by
# branch name. A pull-request run's successor is judged by HEAD SHA against the
# open pull requests on its head branch, then by the listing for a newer run AT
# that SHA (`current_or_successor_id`): a check indifferent to how the group is
# keyed, and one that two pull requests sharing a branch cannot confuse when the
# payloads name their pull requests, and that fails closed when they do not.
# The derived gate admits the PR-number key for a pull-request run and only then.
# None of these publish, deploy or sign; a test pins each one PR-keyed and
# pull-request-only so a workflow that grows a `push` trigger falls back to exempt
# until somebody moves it.
HEAL_SAFE_PULL_REQUEST_WORKFLOWS: frozenset[str] = frozenset(
    {
        "code-review.yml",
        "cross-platform.yml",
        "dependency-review.yml",
        "pr-scope.yml",
        "screenshot-evidence.yml",
    }
)
_PUBLISH_OR_DEPLOY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bnpm\s+publish\b",
        r"\bgh\s+release\b",
        r"\bactions/(?:deploy-pages|upload-pages-artifact)@",
        r"\buses:\s*\./\.github/workflows/publish-[\w-]+\.yml\b",
        r"\bdocker\s+push\b",
        r"\bdocker\s+buildx\s+build\b[^\n]*\s--push\b",
        r"\baws\s+s3\s+cp\b",
        r"\baws\s+codeartifact\s+publish-package-version\b",
        r"\btwine\s+upload\b",
        r"\b(?:sign-and-notarize\.yml|notarytool\b|codesign\b|signtool\b|cosign\s+sign\b|aws\s+signer\b)",
    )
)
# A REF-keyed run-level concurrency group. This is what a PUSH run needs: its
# successor check filters the runs listing by branch NAME, so a PR-keyed group
# cannot tell one pull request's run from another's on a shared head branch.
_BRANCH_GROUP_SIGNAL = re.compile(r"\bgithub\.(?:ref(?:_name)?|head_ref)\b")
# A PR-keyed group. Admitted for a PULL-REQUEST run and only then: its successor is
# judged by head SHA against the open pull requests on the branch, not by the
# listing, so how the group is keyed does not enter that judgement. On a push the
# PR number is empty and the group degenerates to a constant, which is why a push
# run never gets this signal.
_PULL_REQUEST_GROUP_SIGNAL = re.compile(r"\bgithub\.event\.(?:pull_request\.)?number\b")
# The open pull requests on a head branch are read one page deep. A branch with
# more open pull requests than this is not a shape this repository has, and a
# full page is treated as "cannot tell" rather than trusted as complete.
PULL_REQUEST_LISTING_DEPTH = 30
# What `current_or_successor_id` answers for a pull-request run whose pull request
# is CLOSED or merged: no open pull request has its branch as head. Negative, so it
# never collides with a run id. A re-run that lands on this is cancelled again and
# nothing is restored, because no run anyone wants exists for it to have displaced.
SUPERSEDED_WITHOUT_SUCCESSOR = -2
# A pull-request run whose head MOVED while its pull request stays open: either the
# newer runs at the open head cannot be identified as the same pull request's (the
# payloads name no pull request), or none is listed yet. Superseded for every
# judgement made BEFORE a mutation -- nothing at the old head is worth re-running --
# but never a successor anyone may restore: restoring a sibling's run would leave
# the same pull request's run, cancelled through the group, unrestored and
# unreported, and an unlisted run may exist and have been cancelled the same way.
# A re-run that finds this AFTER it started is withdrawn and reported FAILED, naming
# the head, since what its group displaced cannot be told.
SUPERSEDED_SUCCESSOR_UNIDENTIFIED = -3
# The watchdog's own workflow, never watched: its comment names the fleet label.
WATCHDOG_WORKFLOW = "ci-runner-watchdog.yml"

# The event that KICKS a tick between scheduled ones: `fast-gate.yml` completing
# (`workflow_run`, declared in `ci-runner-watchdog.yml`). GitHub's `schedule` is
# best-effort and was measured at ~100 minutes between `*/10` ticks on a quiet
# Sunday, so an orphan that aged past the threshold just after one tick waited
# ninety minutes for the next while `main` sat red behind it. A completed Fast
# Gate is the cheapest heartbeat the repository already emits -- one per head,
# PR and `main` alike -- and a kicked tick is identical to a scheduled one except
# for the debounce in `kick_is_redundant`, which keeps the schedule's quota shape.
KICK_EVENT = "workflow_run"
# A rate-limited call whose window resets within this many seconds, and inside
# the tick's remaining budget, is waited out and retried once; a further-off
# reset aborts the gather instead of burning budget on a sleep.
RATE_LIMIT_RETRY_SECONDS = 30.0
# The synthetic run id under which a rate-limited abort records its outcome, so
# the summary can name it. No real workflow run carries id 0.
RATE_LIMIT_MARKER_ID = 0
# The same device for a LIVE candidate listing that hit its page cap: the tick
# could not see the tail, which is exactly where an orphan sits, so it is not a
# clean tick. Negative, so it can never collide with a run id or with the marker
# above.
LISTING_TRUNCATED_MARKER_ID = -1
# `pending` runs are held by their concurrency group and have no jobs; they are
# listed so the summary can say WHY they wait, never acted on.
CANDIDATE_STATUSES = ("in_progress", "queued", "pending")
PAGE_SIZE = 100
# The repo-wide run listing (`GET /repos/{repo}/actions/runs?status=…`) returns
# runs of EVERY workflow, so one paginated call per status covers the watched set
# instead of one call per watched workflow. An orphan is an OLD run and rides at
# the tail of the newest-first listing, so paging must reach it while still
# bounding a runaway that would otherwise page forever. Measured on this
# repository: `status=queued` alone reports `total_count` 927 over ten pages, and
# a six-hour-old `fast-gate.yml` orphan holding `main`'s concurrency slot sits on
# page SEVEN. A premise of ~172 concurrent runs (about two pages) does not
# describe this load, so the cap is set past the whole measured set rather than
# just past that orphan.
#
# The cap is the API's own reachable window, not a number of our choosing: this
# endpoint serves at most REPO_LISTING_RESULT_CEILING results when `status` is
# supplied, so a higher cap is unreachable and its pages would never be served.
REPO_LISTING_MAX_PAGES = 10
# `GET /repos/{repo}/actions/runs?status=…` stops at 1000 results. Measured on
# this repository: page 11 of `status=queued&per_page=100` returns an EMPTY
# `workflow_runs` and `total_count: 0` -- not a 422, and not an error of any kind.
# That is why an empty page cannot be read as the tail on its own: at the ceiling
# it is indistinguishable from one, so a walk that trusted it would end silently
# with an orphan past result 1000 and report a clean sweep. `_iter_repo_runs`
# therefore compares what it yielded against the first page's `total_count`.
REPO_LISTING_RESULT_CEILING = 1000
# Past that ceiling an actionable listing is NARROWED, not paged deeper: the next
# window lists the same status with `created=<=<oldest created_at read so far>`,
# so it starts where the previous one stopped. Measured: `status=queued` held up to
# 1024 runs while the ceiling serves 1000, so two windows reach the whole set and
# three leave room for growth. Each window costs up to REPO_LISTING_MAX_PAGES reads
# against the shared installation quota, and only a window that hit the ceiling
# opens another, so a tick under the ceiling pays nothing extra.
REPO_LISTING_MAX_WINDOWS = 3
# The candidate statuses a heal can ever act on. `pending` is excluded: those runs
# are held by their concurrency group, have no jobs, and `classify_run` maps a
# jobless run to WAITING_ON_GROUP, never ORPHANED. So a `pending` listing that
# could not be read in full has hidden nothing actionable, and failing a tick over
# it would go red exactly during the saturation this script exists to survive.
ACTIONABLE_CANDIDATE_STATUSES = ("in_progress", "queued")

# The jobs of ONE run, so the page count is bounded by the run's own matrix rather
# than by fleet load: the widest watched workflow expands to a few hundred jobs, so
# six pages of 100 clear it several times over. Kept as a cap anyway, because the
# listing no longer stops on a short page and an unbounded reader would page
# forever on a misbehaving endpoint.
JOBS_LISTING_MAX_PAGES = 6
# Cancelled runs are indexed newest-first by CREATION time, but recovery selects
# by cancellation update time, so a long-running orphan sits deep in this index
# even when its cancellation is inside the recovery window. Ten pages hold 1000
# cancellations, which is about five hours at the 200/hour this repo was measured
# at (300 inside one 90-minute window). That is a reach, not a promise: the
# 2026-09-20 orphans were 21 hours old, and a run created that long before a burst
# cancelled it sits past the cap. The index retains about 90 days, so this cap is
# normally reached and that alone is not a problem; what fails a tick is reaching
# it BEFORE the start of the recovery window, since then runs both created and
# cancelled inside the window went unlisted.
#
# Ten rather than sixteen because REPO_LISTING_RESULT_CEILING is the real bound:
# pages 11 and up are never served for a status-filtered listing, so the extra six
# pages described a reach this endpoint cannot give.
RECOVERY_LISTING_MAX_PAGES = 10
# A run is re-run whole so `changes` recomputes the per-attempt runner label.
RERUN_ENDPOINT = "rerun"
# A recent CodeBuild start that waited at least this fraction of the orphan
# threshold means CodeBuild is saturated, not that a label is dead. Judged first
# on starts from the orphaned job's own queue, then fleet-wide; see
# ``Policy.saturation_wait`` for why the fleet-wide reading pins the line low.
SATURATION_FRACTION = 3
# What the orphaned job's own queue says (``DispatchEvidence.own_queue``).
OWN_QUEUE_SATURATED = "saturated"
OWN_QUEUE_DISPATCHING = "dispatching"
OWN_QUEUE_SILENT = "silent"

# When no live run shows a recent CodeBuild start, this many newest completed
# runs are read for one before anything is healed.
COMPLETED_SAMPLE = 10
# How many live runs the classification pass reads the jobs of per sweep, against
# the shared installation quota the incident exhausted. Runs arrive oldest first,
# so the head of the bound serves those nearest the orphan threshold.
LIVE_CLASSIFY_READS = 50
# Reserved out of that bound for dispatch evidence: the served CodeBuild starts a
# saturation hold is judged by. Drawn from the NEWEST runs that could carry one (see
# ``_can_carry_a_slow_start``), because a reserve taken from the newest runs outright
# can only ever yield prompt starts and is blind to the one signal that holds.
# Measured here with the line at five minutes: the newest ten live runs spanned 0.0
# to 0.6 minutes, none of them able to carry a wait that crossed it, while 352 listed
# runs could -- young enough to speak about the fleet now, old enough for a slow
# start to show.
LIVE_EVIDENCE_RESERVE = 10
# The bound above is spent on EVERY tick, orphan or not, so it is sized for the
# common tick. It leaves the runs past it unread, and an unread run that is old
# enough to hold a slow start holds every heal (``SKIPPED_PARTIAL_EVIDENCE``). On a
# tick that HAS an orphan to judge, the reads that would settle the hold are worth
# spending: ``top_up_unread_evidence`` reads the jobs of every unread run that could
# hold such a start, up to this many more, and only then. Measured on 2026-09-29: 133
# live runs against the 50-read bound left 83 capable runs unread on a tick with four
# orphans, none healed, and the next tick held the same four the same way. All or
# nothing: a capable set larger than this bound is not read at all, because reading
# part of it cannot lift the hold (any unread capable run still holds) and the reads
# would be spent for no heal. A tick with no orphan spends nothing here.
LIVE_EVIDENCE_TOPUP_READS = 200
# A listed live run older than this is a GHOST: a record the runs index still
# returns as queued, in progress or pending but that GitHub itself does not hold
# as live. GitHub cancels any job that has not started within 24 hours and caps a
# CodeBuild build at 8, so a run in a live status two days on has no job that will
# ever run and nothing a heal could act on; the ones measured here (created five
# weeks earlier, no job ever created) answer a cancel with 409 "completed" and a
# direct read with 404. They are dropped before the read bound is drawn, for two
# reasons. Read, they spend the classify budget: 16 of the 50 reads on the tick
# measured, every tick, re-reading the same jobless records. Unread, they would be
# worse: a queued run past the saturation line counts as a run that COULD hold a
# slow start, so an unread ghost would hold the heal back on every tick, for ever.
GHOST_AFTER = timedelta(days=2)
# How many in-window cancelled runs the recovery pass reads the jobs of per tick.
# Every `main` push cancels the run it supersedes, so this repo holds hundreds of
# cancelled runs inside one recovery window (300 measured in a 90-minute window),
# and classifying each costs one job read. The pass walks the oldest cancellation
# updates first, so the bound spends its reads on the runs closest to ageing out
# of the window and a newer update waits for the next tick rather than displacing
# an older orphan.
RECOVERY_CLASSIFY_READS = 50
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
# Pull-request runs are healed like push runs. Their successor is judged by head
# SHA against the open pull requests on the head branch, then by the listing for a
# newer run at that same SHA (`current_or_successor_id`). The head question is
# answered from the pulls API by branch, never from `pull_requests[].number` on the
# run payload (of 20 sampled same-repository runs only 9 carried it); that field is
# consulted only to tell a same-SHA newer run's pull request from the judged run's,
# and its absence fails that one question closed.
SKIPPED_ATTEMPT_CAP = "skipped-attempt-cap"
SKIPPED_SUPERSEDED = "skipped-superseded"
SKIPPED_SATURATED = "skipped-saturated"
SKIPPED_NO_DISPATCH_EVIDENCE = "skipped-no-dispatch-evidence"
# Runs old enough to carry a served start past the threshold went unread this sweep,
# so saturation cannot be ruled out. Holding leaves the queued work alone; acting
# cancels finished work and re-queues it into a fleet the sweep could not see. The
# premise is the unread SATURATION-CAPABLE runs, not merely that the read bound was
# reached: a bound spent entirely on runs that could not carry such a start leaves
# nothing unseen. On a tick with an orphan to judge, ``top_up_unread_evidence`` reads
# those runs before the hold is judged, so this verdict is reached only when the
# capable set exceeded ``LIVE_EVIDENCE_TOPUP_READS`` or a run crossed the age line
# after the top-up. Without the top-up the premise is met on most ticks here (about
# 302 unread capable runs against a 50-read bound), tracked at #13644.
SKIPPED_PARTIAL_EVIDENCE = "skipped-partial-dispatch-evidence"
LOOKUP_INCONCLUSIVE = "lookup-inconclusive"
WAITING_ON_GROUP = "waiting-on-group"
HEAL_EXEMPT = "heal-exempt"
# Not a run verdict but a tick-level one: the marker RunVerdict carries it so the
# summary can report that gathering stopped on a rate limit.
TICK_ABORTED_RATE_LIMITED = "tick-aborted-rate-limited"
# Tick-level too: a live candidate listing hit ``REPO_LISTING_MAX_PAGES`` and the
# oldest live runs were never read. The depth needed to reach an orphan is the run
# arrival rate times the orphan's age, so a cap that reaches past the whole
# measured set today re-truncates short of the orphan under modest load growth --
# reproducing the very blindness this script exists to end. The paging already
# logs a `::warning::`, which nobody is required to read, so the truncation also
# carries a verdict and a failed outcome.
TICK_LISTING_TRUNCATED = "tick-listing-truncated"

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
# A pull-request run re-run by this script and then found to belong to a CLOSED
# pull request. The re-run is cancelled again (it serves a head nobody wants) and
# nothing is restored: no open pull request has a run it could have displaced. Not
# a failed outcome: no verdict is lost. A head that moved while the pull request
# stays open is NOT this case; that is `OUTCOME_LOOKUP_FAILED`, since the re-run may
# have displaced the run at the new head.
OUTCOME_RERUN_WITHDRAWN = "superseded-after-rerun-pull-request-closed"
OUTCOME_LOOKUP_FAILED = "branch-lookup-inconclusive"

OUTCOME_CANCEL_FAILED = "cancel-failed"
OUTCOME_CANCEL_TIMED_OUT = "cancel-timed-out"
OUTCOME_RERUN_DEFERRED = "rerun-deferred-out-of-time"
OUTCOME_RERUN_REFUSED = "rerun-refused"
OUTCOME_NOT_ATTEMPTED = "not-attempted-cap-reached"
OUTCOME_EVIDENCE_REREAD_DEFERRED = "deferred-evidence-reread-failed"
# An orphan past attempt 1 that a newer push supersedes (or whose supersession
# cannot be told), left uncancelled: a later attempt may be somebody's own `gh run
# rerun`, and cancelling a superseded run is never followed by a re-run, so the
# cancel would discard their work with nothing left to show they did it. A FAILED
# outcome: the run keeps its concurrency group's running slot, and on `main`
# (`cancel-in-progress: false`) that evicts every later push's run from the
# pending slot -- the measured 6-hour incident in `supersession_clears_hold`'s
# docstring. The watchdog has decided it will never free that slot itself, so a
# human must, and a `::warning::` inside a green scheduled run tells nobody; the
# tick goes red and names the `gh run cancel` to type, for the same reason
# `OUTCOME_HUMAN_REQUIRED` is a failed outcome.
OUTCOME_RERUN_ATTEMPT_LEFT = "rerun-attempt-superseded-left-untouched"
# A stuck run in a workflow this script will not heal, so only a human can move
# it. A failed outcome: most of the watched set is heal-exempt,
# `main-ratchet-audit.yml` among them, and it was one of the three workflows in
# the incident this script exists for. Reporting those as a warning inside a
# passing scheduled run nobody watches would leave that incident's own shape --
# a stuck run nobody is told about -- intact for the majority of the repo.
OUTCOME_HUMAN_REQUIRED = "human-required-heal-exempt-workflow"
# The run's own revision could not be read, so its heal safety is UNKNOWN rather
# than unsafe. A failed outcome, because a cancelled run near the end of its
# recovery window would otherwise age out behind a green tick on the strength of
# a read nobody completed.
OUTCOME_HEAL_SAFETY_UNKNOWN = "heal-safety-unreadable-at-run-revision"
# A tick a rate limit cut short. A FAILED outcome, because the gathering abort
# also skips the recovery pass, and recovery is the only thing between a
# cancelled orphan and the end of its 90-minute window: "the next tick re-lists"
# is no answer for a run in the final tick-interval of that window while the
# limit persists. The runs classified ahead of the abort are still acted on; what
# the outcome refuses is calling a tick healthy when it could not look.
OUTCOME_ABORTED_RATE_LIMITED = "aborted-rate-limited"

# A live candidate listing stopped on its page cap, so the OLDEST live runs were
# never read -- and the tail is exactly where an orphan sits. The depth needed to
# reach one is the run arrival rate times the orphan's age, so a cap that clears
# the whole measured set today re-truncates short of an orphan under modest load
# growth, reproducing the blindness this script exists to end. A tick that could
# not look that far is not a clean tick, so this is a FAILURE like the abort
# above, rather than only the `::warning::` the paging logs and nobody must read.
OUTCOME_LISTING_TRUNCATED = "listing-truncated"

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
        OUTCOME_ABORTED_RATE_LIMITED,
        OUTCOME_LISTING_TRUNCATED,
        OUTCOME_HEAL_SAFETY_UNKNOWN,
        OUTCOME_HUMAN_REQUIRED,
        OUTCOME_RERUN_ATTEMPT_LEFT,
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


class LookupInconclusive(Exception):
    """The successor question could not be answered, so no mutation may rest on it.

    For a push run: the branch listing did not reach the run being judged, so
    "newest" is unknown. For a pull-request run also: a newer run at the judged
    run's own head SHA whose pull request neither payload identifies, so successor
    (the same pull request's; a re-run of the judged run would cancel it through the
    group) and sibling (another pull request on a shared head branch) cannot be told
    apart. Both fail closed: the run is left as it is, the tick reds and names it.
    """


class ApiError(Exception):
    """An HTTP error from the GitHub API, with its status code attached.

    ``ambiguous`` marks a MUTATION whose result is unknown: the request was
    sent, and then the response could not be read (or was a server error), so
    the server may or may not have applied it. Such a POST is never retried by
    the client -- a second cancel or re-run on top of one that landed is a
    conflict, and a conflict would be misread as a refusal. The caller
    reconciles from the run's own state instead.

    ``retry_after`` and ``remaining`` carry the rate-limit hints from the
    response headers (seconds until the window resets, and the remaining quota),
    so the caller can wait out a cheap reset or abort a far-off one. ``rate_limited``
    is true only for a 403/429 the body or headers identify as a quota exhaustion,
    which the gather loop treats as a stop-and-report signal rather than a crash.
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        ambiguous: bool = False,
        retry_after: float | None = None,
        remaining: str | None = None,
    ) -> None:
        self._message = _safe_text(message)
        super().__init__(f"HTTP {status}: {self._message}")
        self.status = status
        self.ambiguous = ambiguous
        self.retry_after = retry_after
        self.remaining = remaining

    @property
    def rate_limited(self) -> bool:
        if self.status not in (403, 429):
            return False
        if "rate limit" in self._message.lower():
            return True
        return self.remaining == "0"


class Api(Protocol):
    """The two calls this script needs. Faked in tests, HTTP in production."""

    def get(self, path: str) -> Any: ...

    def post(self, path: str) -> None: ...


# The GitHub client and the listing, classification, heal-safety, supersession, heal
# and recovery stages live in the ``runner_watchdog_parts`` package beside this file
# (its ``__init__`` names each owner). This module keeps the knobs and the
# vocabulary above and, below, the tick driver, the kicked-tick debounce, the step
# summary and the entry point. It re-exports every owner name, so
# ``runner_watchdog.<name>`` resolves as before, and a name the tests patch here is
# read by the owners as ``runner_watchdog.<name>`` at call time, so the patch
# reaches them.
#
# Run as ``python3 runner_watchdog.py`` this module is ``__main__``: the alias makes
# the owners' ``import runner_watchdog`` find it rather than load a second copy.
# Loaded by path, as the tests load it, its directory is not on ``sys.path``, so the
# directory is appended (never put first) for the owner imports.
sys.modules.setdefault("runner_watchdog", sys.modules[__name__])
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)
from runner_watchdog_parts.classify import (  # noqa: E402,F401
    _PER_RUN_LABEL_SUFFIX,
    DispatchEvidence,
    OrphanedJob,
    Policy,
    RunVerdict,
    _base_verdict,
    _fmt_delta,
    _guard,
    _label,
    _orphan,
    _pull_request_numbers,
    _workflow_of,
    classify_cancelled_run,
    classify_run,
    dispatch_queue,
    is_codebuild_job,
    is_fork_run,
    parse_timestamp,
)
from runner_watchdog_parts.client import (  # noqa: E402,F401
    GitHubApi,
    _rate_limit_hints,
    _ReadsThrough,
)
from runner_watchdog_parts.heal import (  # noqa: E402,F401
    HoldCheck,
    Tick,
    _latest_queue,
    _orphan_queue,
    _out_of_time,
    _Pending,
    _rerun,
    _rerun_cancelled_successor,
    _restore_successor,
    _status_or_unknown,
    _withdraw,
    heal_runs,
    resolve_hold,
    sample_completed_runs,
)
from runner_watchdog_parts.heal_safety import (  # noqa: E402,F401
    _mark_heal_exempt,
    _workflow_without_full_line_comments,
    _yaml_mapping_entries,
    heal_exempt_workflows,
    heal_safe_declared,
    workflow_has_ref_or_pr_concurrency,
    workflow_is_heal_safe_at_revision,
    workflow_text_has_publish_or_deploy_step,
    workflow_text_is_heal_safe,
)
from runner_watchdog_parts.listing import (  # noqa: E402,F401
    _can_carry_a_slow_start,
    _evidence_reserve,
    _heal_eligible_shape,
    _is_watched,
    _iter_actionable_runs,
    _iter_repo_runs,
    _may_hold_jobs,
    _runs_path,
    drop_ghost_runs,
    gather_all_candidate_runs,
    list_all_candidate_runs,
    list_jobs,
    list_recent_cancelled_runs,
    list_runs,
    live_runs_within_read_bound,
    top_up_unread_evidence,
)
from runner_watchdog_parts.recovery import (  # noqa: E402,F401
    _flag_unclassified_near_expiry,
    recover_cancelled_runs,
)
from runner_watchdog_parts.supersession import (  # noqa: E402,F401
    _current_or_successor_for_pull_request,
    _open_pull_request_heads,
    _rerun_attempt_may_be_cancelled,
    _supersession_is_answerable,
    current_or_successor_id,
    is_current_run,
    newest_run_id_for_branch,
    supersession_clears_hold,
)

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
    aborted_markers = [v for v in verdicts if v.verdict == TICK_ABORTED_RATE_LIMITED]
    truncated_markers = [v for v in verdicts if v.verdict == TICK_LISTING_TRUNCATED]
    real = [
        v for v in verdicts if v.verdict not in (TICK_ABORTED_RATE_LIMITED, TICK_LISTING_TRUNCATED)
    ]
    lines.append(f"Inspected {len(real)} run(s).")
    if aborted_markers:
        lines.append("")
        lines.append(
            f"Aborted (rate limited): {_md(aborted_markers[0].detail)} -- acted on the runs already "
            f"classified; the next tick re-lists."
        )
    if truncated_markers:
        lines.append("")
        lines.append(
            f"Reach incomplete: {_md(truncated_markers[0].detail)} -- the oldest live runs were "
            f"never classified, so an orphan among them was not seen. The cap is already the API's "
            f"reachable window; narrow the listing instead."
        )
    lines.append("")
    acted = [v for v in real if v.actionable]
    reported = [
        v
        for v in real
        if v.verdict
        in (
            SKIPPED_FORK,
            SKIPPED_ATTEMPT_CAP,
            SKIPPED_SUPERSEDED,
            SKIPPED_SATURATED,
            SKIPPED_NO_DISPATCH_EVIDENCE,
            SKIPPED_PARTIAL_EVIDENCE,
            WAITING_ON_GROUP,
        )
    ]
    if not acted and not reported:
        lines.append(
            "No run was classified before the tick aborted."
            if aborted_markers
            else "Nothing stuck."
        )
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
            reported_outcome = outcomes.get(v.run_id)
            outcome_text = f"; outcome: {reported_outcome}" if reported_outcome is not None else ""
            lines.append(
                f"- {_md_link(v)} {_md(v.head_branch)} -- {v.verdict}: {_md(v.detail)}{outcome_text}"
            )
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
    exempt_workflows = heal_exempt_workflows()
    start = clock()
    tick = Tick(
        clock=clock,
        sleep=sleep,
        deadline=start + policy.tick_budget.total_seconds(),
        started_at=policy.now,
        started_clock=start,
    )
    aborted: str | None = None

    def cheap_retry(thunk: Callable[[], Any]) -> Any:
        """Run ``thunk``; on a rate limit whose reset is cheap and in budget, wait once and retry.

        A rate-limit whose reset is unknown or too far off, or a second one on
        the retry, propagates -- the gather catches it and ends the tick. Every
        other ApiError passes straight through, so 401/404/5xx keep raising.
        """
        try:
            return thunk()
        except ApiError as exc:
            if not exc.rate_limited:
                raise
            wait = exc.retry_after
            if (
                wait is not None
                and 0 <= wait <= RATE_LIMIT_RETRY_SECONDS
                and tick.remaining() - wait >= RERUN_RESERVE_SECONDS
            ):
                log(
                    f"rate limited ({exc}); the window resets in {int(wait)} s, within budget -- "
                    f"waiting once and retrying"
                )
                sleep(wait)
                return thunk()
            raise

    listing_truncated: list[str] = []
    try:
        candidate_runs, listing_abort, listing_truncated = gather_all_candidate_runs(
            api,
            policy.repo,
            get=lambda path: cheap_retry(lambda: api.get(path)),
            log=log,
        )
        bounded_runs, unread_candidates = live_runs_within_read_bound(candidate_runs, policy, log)
        # Merged by run id rather than taking the larger count: the union is what the
        # later re-judgement needs, and two listings of the same run agree on its
        # creation time, so an update cannot lose an unread run either listing saw.
        evidence.unread_candidates.update(unread_candidates)
        for run in bounded_runs:
            # Jobs are read for every run the bound admits, young ones included: a
            # young run is never actionable, but a prompt CodeBuild start inside it is
            # dispatch evidence. A run younger than the threshold cannot hold a start
            # that waited that long, which is why the bound's reserved reads go to the
            # newest runs that ARE at least that old.
            def read_jobs(run: dict[str, Any] = run) -> list[dict[str, Any]]:
                return list_jobs(api, policy.repo, int(run["id"]))

            jobs = cheap_retry(read_jobs)
            verdict = _mark_heal_exempt(classify_run(run, jobs, policy), exempt_workflows)
            log(f"{_label(verdict)}: {verdict.verdict} -- {verdict.detail}")
            for orphan in verdict.orphans:
                log(
                    f"  queued {_fmt_delta(orphan.queued_for)} with no runner: {orphan.name} {list(orphan.labels)}"
                )
            verdicts.append(verdict)
            evidence.absorb(jobs, policy)

        if listing_abort is not None:
            raise listing_abort

        # Only now, and only when there is an orphan to judge: the reads that settle
        # the partial-evidence hold are spent on a tick that can heal something, and
        # on no other. A tick whose verdicts hold no orphan reads nothing here.
        if any(verdict.verdict == ORPHANED for verdict in verdicts):

            def read_unread_jobs(run_id: int) -> list[dict[str, Any]]:
                return cheap_retry(lambda: list_jobs(api, policy.repo, run_id))

            top_up_unread_evidence(api, policy, evidence, log, read_jobs=read_unread_jobs)

        # The hold is judged per run, relative to when its NEWEST orphaned job
        # queued: a start that postdates an older orphan may still predate a
        # younger one, in another run or in the same one.
        for verdict in verdicts:
            if verdict.verdict != ORPHANED:
                continue
            hold = resolve_hold(
                api, policy, evidence, _latest_queue(verdict), queue=_orphan_queue(verdict)
            )
            if hold is not None and supersession_clears_hold(api, policy, verdict, log):
                hold = None
            if hold is not None:
                verdict.verdict, verdict.detail = hold
                log(f"::warning::{_label(verdict)}: {verdict.detail}")

        # Logged AFTER the holds are resolved, because the completed-run sample is
        # taken while resolving and a sweep whose live runs were all quiet has nothing
        # to measure before it. A tick that observed no served start at all -- no live
        # one, and no orphan to trigger the sample -- reports nothing rather than a
        # zero, since absent evidence is not a fast queue. The margin between this and
        # `saturation_wait` is what says whether the line still separates a slow queue
        # from a dead label, so it belongs in the tick's own log rather than in a hand
        # measurement taken after the hold misbehaves.
        slowest_seen = evidence.slowest_served_wait(policy)
        if slowest_seen is not None:
            waited, name = slowest_seen
            # Seconds, not `_fmt_delta`: it floors to whole minutes, and the waits this
            # reading exists to watch are 27s median and 47s at p90 here, so every normal
            # one would print as "0 min" -- indistinguishable from the absent reading the
            # block above refuses to invent, and blind to drift across the whole
            # sub-minute range.
            # Two ways this is a SAMPLE rather than the tick's true maximum, both
            # deliberate and both named in the label. It spans every start this sweep
            # read, not the subset any one verdict judged (a hold counts only starts
            # after its own orphan queued), so it can exceed the line on a tick that
            # healed -- the reading calibrates what waits this fleet produces (#13644),
            # a question about the fleet and not about one orphan. And it is taken from
            # the bounded classify sweep only: the pre-cancel re-read below absorbs
            # further starts after this point, and they are not in this number.
            log(
                "slowest served CodeBuild wait observed this tick (initial bounded "
                "sample: every start this sweep read, not only those a hold counted, "
                f"and not the later pre-cancel re-read): {int(waited.total_seconds())}s "
                f"({name}), against a saturation line of "
                f"{int(policy.saturation_wait.total_seconds())}s"
            )
    except ApiError as exc:
        # A rate limit stops gathering rather than losing the whole tick: the
        # runs classified ahead of it are acted on below (heal_runs re-reads the
        # fleet's state and holds anything it cannot verify), and the next tick
        # re-lists. Any other error is the caller's to raise, untouched here.
        if not exc.rate_limited:
            raise
        aborted = str(exc)
        log(
            f"::warning::the tick hit a GitHub rate limit while gathering ({exc}); it stops listing, "
            f"acts on the {len(verdicts)} run(s) already classified, and the next tick re-lists"
        )

    # Recovery goes FIRST and takes the per-tick cap before live heals do. A
    # cancelled orphan has already lost its verdict and only a re-run brings it
    # back, whereas a live orphan loses nothing by waiting one more tick; were
    # live heals served first, a sustained backlog of five live orphans per
    # tick would starve recovery until the cancelled run aged out of the
    # recovery window and its verdict was gone for good. A rate limit here is
    # non-fatal too: the recovery pass is left for the next tick.
    recovered: list[RunVerdict] = []
    recovered_outcomes: dict[int, str] = {}
    if aborted is None:
        try:
            recover_cancelled_runs(
                _ReadsThrough(api, lambda path: cheap_retry(lambda: api.get(path))),
                policy,
                budget=policy.max_runs,
                tick=tick,
                log=log,
                exempt_workflows=exempt_workflows,
                verdicts=recovered,
                outcomes=recovered_outcomes,
            )
        except ApiError as exc:
            if not exc.rate_limited:
                raise
            aborted = str(exc)
            log(
                f"::warning::the recovery pass hit a GitHub rate limit ({exc}); it is left for the "
                f"next tick"
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
            fresh_runs = list_all_candidate_runs(api, policy.repo, log=log)
            bounded_fresh, latest.unread_candidates = live_runs_within_read_bound(
                fresh_runs, read_at, log
            )
            for run in bounded_fresh:
                latest.absorb(list_jobs(api, policy.repo, int(run["id"])), read_at)
            # This read exists only because a cancel is about to be judged, so the
            # top-up's premise -- an orphan waiting on the question -- holds by
            # construction; the same bound and the same all-or-nothing rule apply.
            top_up_unread_evidence(api, read_at, latest, log)
            sample_completed_runs(api, read_at, latest)
            fresh["evidence"] = latest
        except ApiError as exc:
            fresh["error"] = exc

    def fresh_hold(verdict: RunVerdict) -> tuple[str, str, str | None] | None:
        prime_fresh_evidence()
        if "error" in fresh:
            # A one-off error (a 502) is genuinely deferrable: nothing was touched,
            # the run stays orphaned and the next tick re-reads it. A RATE LIMIT is
            # not one-off but a condition, and it is the condition that killed the
            # tick in the incident: every tick would defer and every tick would be
            # green while the orphan keeps parking every later push behind it. So a
            # rate-limited re-read carries the tick's existing rate-limit outcome,
            # which is FAILED, rather than the plain deferral.
            failed = getattr(fresh["error"], "rate_limited", False)
            return (
                SKIPPED_NO_DISPATCH_EVIDENCE,
                f"dispatch evidence could not be re-read before the cancel ({fresh['error']}); "
                f"nothing healed on evidence that may be stale",
                OUTCOME_ABORTED_RATE_LIMITED if failed else OUTCOME_EVIDENCE_REREAD_DEFERRED,
            )
        # Judged against the wall clock of THIS moment: a start that has aged
        # past the lookback since the sweep no longer counts. The sample is
        # already taken, so this is a pure judgement -- no read, no delay
        # between the run's own re-read and its cancel.
        hold = resolve_hold(
            api,
            replace(policy, now=tick.now()),
            fresh["evidence"],
            _latest_queue(verdict),
            queue=_orphan_queue(verdict),
        )
        if hold is not None and supersession_clears_hold(
            api, replace(policy, now=tick.now()), verdict, log
        ):
            # Re-asked here as well as in the sweep: a run that was its branch's
            # newest when the sweep read it can be superseded by the time the cancel
            # is sent, and that is precisely when holding it starts costing every
            # later commit its gate.
            return None
        return None if hold is None else (*hold, None)

    outcomes: dict[int, str] = {}
    for verdict in verdicts:
        if verdict.verdict != HEAL_EXEMPT:
            continue
        outcomes[verdict.run_id] = OUTCOME_HUMAN_REQUIRED
        # A failed outcome, so the tick goes red: FAILED_OUTCOMES promises the log
        # then names the run and the command to type. Without this line a red tick
        # carries only the plain verdict line, and every tick stays red with no
        # `::error::` saying which run a human has to recover.
        log(
            f"::error::{_label(verdict)} needs a human: {verdict.detail} Recover it by hand "
            f"(`gh run cancel {verdict.run_id}`, then `gh run rerun {verdict.run_id}` if its "
            f"verdict is still needed)."
        )
    outcomes.update(
        heal_runs(
            api,
            to_heal,
            policy,
            tick=tick,
            log=log,
            hold_check=fresh_hold,
            prime=prime_fresh_evidence,
        )
    )
    for verdict in deferred:
        outcomes[verdict.run_id] = OUTCOME_NOT_ATTEMPTED
        log(
            f"{_label(verdict)}: per-invocation cap of {policy.max_runs} reached; left for the next tick"
        )

    verdicts.extend(recovered)
    outcomes.update(recovered_outcomes)
    if aborted is not None:
        # A synthetic verdict carries the abort so the summary names it. It is
        # not a run, so it never lands in the acted/reported tables. Its outcome
        # IS a failure, because the abort also skipped the recovery pass.
        verdicts.append(
            RunVerdict(
                run_id=RATE_LIMIT_MARKER_ID,
                run_attempt=0,
                head_branch="",
                head_repo="",
                event="",
                status="",
                url="",
                age=timedelta(0),
                verdict=TICK_ABORTED_RATE_LIMITED,
                workflow="",
                detail=aborted,
            )
        )
        outcomes[RATE_LIMIT_MARKER_ID] = OUTCOME_ABORTED_RATE_LIMITED
    if listing_truncated:
        # Same device, for the reach rather than the quota: the listing stopped on
        # its page cap, so the oldest live runs were never classified and an orphan
        # among them is invisible. Recorded as a failure so the tick goes red
        # instead of reporting the runs it DID see as a clean sweep.
        verdicts.append(
            RunVerdict(
                run_id=LISTING_TRUNCATED_MARKER_ID,
                run_attempt=0,
                head_branch="",
                head_repo="",
                event="",
                status="",
                url="",
                age=timedelta(0),
                verdict=TICK_LISTING_TRUNCATED,
                workflow="",
                detail="; ".join(listing_truncated),
            )
        )
        outcomes[LISTING_TRUNCATED_MARKER_ID] = OUTCOME_LISTING_TRUNCATED
    return verdicts, outcomes


def kick_is_redundant(
    api: Api,
    repo: str,
    *,
    now: datetime,
    self_run_id: int,
    interval: timedelta,
    log: Callable[[str], None] = print,
) -> str | None:
    """Whether a KICKED tick should stand down because a tick already covers this moment.

    One read of this workflow's own recent runs. A tick other than this one that
    STARTED (`in_progress` or `completed`, any conclusion) within ``interval`` of
    now means the schedule, or an earlier kick, has classified the fleet within the
    cadence the schedule promises; this kick adds nothing but its full read cost.
    Only started ticks count: a `queued`/`pending`/`waiting` sibling is the kick
    that arrived behind this one and is held by the concurrency group -- counting
    it would make the two stand down for each other and nobody run.

    Returns the reason to skip, or None to run the full tick. A failed read runs
    the tick (None): the schedule runs it unconditionally, and starving on a
    transient error would re-create the gap the kick exists to fill. The caller
    decides what a rate limit here means for the tick.
    """
    page = api.get(f"{_runs_path(repo, WATCHDOG_WORKFLOW)}?per_page=10")
    newest: datetime | None = None
    for run in page.get("workflow_runs", []) if isinstance(page, dict) else []:
        try:
            if int(run.get("id", 0)) == self_run_id:
                continue
            status = run.get("status")
            if status not in ("in_progress", "completed"):
                continue
            # A `cancelled` row never ran a tick: it is a kick the concurrency
            # group evicted while queued, or a kick that stood down and cancelled
            # itself (`stand_down`). Counting either as a tick would let a chain of
            # kicks less than one interval apart stand down for each other, each
            # seeing the one before, while no full tick ran since the last cron.
            if status == "completed" and run.get("conclusion") in ("cancelled", "skipped"):
                continue
            started = parse_timestamp(run.get("run_started_at") or run["created_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if newest is None or started > newest:
            newest = started
    if newest is None:
        return None
    age = now - newest
    if timedelta(0) <= age < interval:
        return (
            f"a tick started {_fmt_delta(age)} ago, inside the {_fmt_delta(interval)} "
            f"schedule interval; this kick adds nothing and stands down"
        )
    log(f"the newest other tick started {_fmt_delta(age)} ago; the schedule is late, running")
    return None


# How long a stood-down kick waits for its own cancel to land before giving up
# and exiting on its own. The cancel is what marks the row `cancelled` so later
# kicks do not count it as a tick; a row that completes `success` first is
# indistinguishable from a full tick in the listing.
STAND_DOWN_CANCEL_WAIT_SECONDS = 60.0


def stand_down(
    api: Api,
    repo: str,
    *,
    self_run_id: int,
    reason: str,
    summary_path: str = "",
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> None:
    """Record why a kicked tick is not running, then cancel this run so the row reads `cancelled`.

    The listing `kick_is_redundant` reads cannot tell a kick that stood down from a
    kick that ran: both are `completed`. The one field this run can set on its own
    row from inside is its conclusion, by cancelling itself (one POST). The runner
    then stops this step; the wait below is for that stop, bounded so a cancel the
    API accepted but never delivered cannot hang the job. A cancel that is refused
    is logged and the run completes `success` -- it then counts as one tick for one
    interval, which is the degraded case, not a wrong heal.
    """
    log(f"kicked tick: {reason}")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(f"## CI runner watchdog\n\nKicked tick stood down: {reason}.\n")
    if self_run_id <= 0:
        log(
            "kicked tick: no GITHUB_RUN_ID, so this row cannot be cancelled and will count as a tick"
        )
        return
    try:
        api.post(f"repos/{repo}/actions/runs/{self_run_id}/cancel")
    except ApiError as exc:
        log(
            f"kicked tick: self-cancel refused ({exc}); this row will count as a tick for one interval"
        )
        return
    deadline = clock() + STAND_DOWN_CANCEL_WAIT_SECONDS
    while clock() < deadline:
        sleep(5.0)
    log("kicked tick: the self-cancel was accepted but has not stopped this step; exiting")


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
    if os.environ.get("GITHUB_EVENT_NAME", "") == KICK_EVENT:
        try:
            self_run_id = int(os.environ.get("GITHUB_RUN_ID", "0") or 0)
        except ValueError:
            self_run_id = 0
        try:
            reason = kick_is_redundant(
                api,
                args.repo,
                now=policy.now,
                self_run_id=self_run_id,
                interval=policy.schedule_interval,
            )
        except ApiError as exc:
            # The one read that decides the debounce failed. A rate limit means the
            # full tick would abort red on its first listing for nothing; anything
            # else is the schedule's own posture, which runs unconditionally.
            if exc.rate_limited:
                reason = f"the debounce read was rate limited ({exc})"
            else:
                print(f"kicked tick: the debounce read failed ({exc}); running the full tick")
                reason = None
        if reason is not None:
            stand_down(
                api, args.repo, self_run_id=self_run_id, reason=reason, summary_path=args.summary
            )
            return 0
    verdicts, outcomes = run_watchdog(api, policy)
    summary = render_summary(verdicts, outcomes, policy)
    # Its own footprint against the quota whose exhaustion started all this. Logged
    # rather than enforced: a cap here would silently stop healing, which is the
    # failure this script exists to end. Read these across a few scheduled ticks to
    # judge whether the bounds above are sustainable per hour.
    remaining = api.rate_limit_remaining
    print(
        f"this tick made {api.calls} GitHub API call(s); the installation quota "
        f"reported {remaining if remaining is not None else 'no'} request(s) remaining"
    )
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(summary)
    else:
        print(summary)
    if outcomes.get(RATE_LIMIT_MARKER_ID) == OUTCOME_ABORTED_RATE_LIMITED:
        print(
            "::error::a GitHub rate limit cut this tick short, so the cancelled-orphan recovery "
            "pass did not run; a cancelled run near the end of its recovery window can age out "
            "before a later tick reaches it"
        )
    if outcomes.get(LISTING_TRUNCATED_MARKER_ID) == OUTCOME_LISTING_TRUNCATED:
        print(
            "::error::a live run listing did not reach its own total_count, so the oldest live "
            "runs were never classified and an orphan among them was not seen. The page cap is "
            "already the API's reachable window and the listing already narrows by created "
            "window up to REPO_LISTING_MAX_WINDOWS, so the remedy is more windows or a narrower "
            "split (branch or event) rather than paging deeper"
        )
    return 1 if FAILED_OUTCOMES & set(outcomes.values()) else 0


if __name__ == "__main__":
    sys.exit(main())

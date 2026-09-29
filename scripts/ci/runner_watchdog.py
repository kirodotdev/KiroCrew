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
    "pr-scope.yml",
    "release.yml",
    "screenshot-evidence.yml",
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
# nothing unseen. At this repository's listing size the premise is still met on most
# ticks (about 302 unread capable runs against a 50-read bound), so this is a more
# honest hold rather than a rarer one; raising the bound or narrowing the listing is
# what lowers it, tracked at #13644.
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


def _rate_limit_hints(headers: Any) -> tuple[float | None, str | None]:
    """Seconds until the rate-limit window resets, and the remaining quota, from headers.

    ``Retry-After`` (seconds) is honoured first -- it is what a secondary rate
    limit sends. Otherwise ``X-RateLimit-Reset`` (an epoch second) gives the
    wait when the quota is spent (``X-RateLimit-Remaining`` is ``0``). A missing
    or non-numeric header yields ``None``, so a caller with no usable hint aborts
    rather than guessing a wait.
    """
    if headers is None:
        return None, None
    remaining = headers.get("X-RateLimit-Remaining")
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after)), remaining
        except (TypeError, ValueError):
            pass
    reset = headers.get("X-RateLimit-Reset")
    if reset and remaining == "0":
        try:
            return max(0.0, float(reset) - time.time()), remaining
        except (TypeError, ValueError):
            pass
    return None, remaining


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
        # This watchdog exists because a shared installation quota ran out, and it
        # is itself a heavy consumer of that quota: the cancelled index alone is
        # sixteen pages every tick. Counting its own calls and remembering the last
        # `X-RateLimit-Remaining` the API reported turns "is the schedule
        # sustainable?" from a guess into a number in every tick's summary.
        self.calls = 0
        self.rate_limit_remaining: str | None = None

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
            self.calls += 1
            try:
                with self._open(request, timeout=30) as response:
                    # Tolerated rather than required: the quota reading is
                    # observability, and a response object without headers must
                    # not turn a successful read into a crash.
                    headers = getattr(response, "headers", None)
                    remaining = headers.get("X-RateLimit-Remaining") if headers else None
                    if remaining is not None:
                        self.rate_limit_remaining = remaining
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
                retry_after, remaining = _rate_limit_hints(exc.headers)
                if remaining is not None:
                    self.rate_limit_remaining = remaining
                raise ApiError(
                    exc.code, detail, retry_after=retry_after, remaining=remaining
                ) from exc
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


class _ReadsThrough:
    """An ``Api`` whose READS go through a wrapper and whose mutations do not.

    The recovery pass reads the cancelled index, each run's jobs and each branch
    lookup through the plain client, so a rate limit whose window resets in a few
    seconds -- one the gather phase would simply wait out -- aborted the pass
    instead. For a cancelled run with less window left than one schedule interval
    there is no next tick, so a cheap reset was costing a verdict. Wrapping the
    reads hands recovery the same wait-and-retry the gather already has.

    Mutations pass straight through, deliberately: a cancel or re-run that may be
    on the wire is never repeated, whatever the failure.
    """

    def __init__(self, api: Api, read: Callable[[str], Any]) -> None:
        self._api = api
        self._read = read

    def get(self, path: str) -> Any:
        return self._read(path)

    def post(self, path: str) -> None:
        self._api.post(path)


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


def _yaml_mapping_entries(text: str) -> Iterator[tuple[tuple[str, ...], str]]:
    """Yield simple block-style YAML keys with their mapping path and raw value."""
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_-]+):(?P<value>.*)$", line)
        if match is None:
            continue
        indent = len(match.group("indent"))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = match.group("key")
        yield tuple(item[1] for item in stack) + (key,), match.group("value").strip()
        stack.append((indent, key))


def _workflow_without_full_line_comments(raw: str) -> str:
    return "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))


def workflow_has_ref_or_pr_concurrency(text: str, *, event: str = "push") -> bool:
    """Whether the run-level concurrency group names a ref, or -- for a pull-request
    run -- a pull-request number.

    The event decides which key is enough. A push run's successor is judged by the
    branch listing, so its group must be ref-keyed. A pull-request run's successor is
    judged by head SHA against the open pull requests on its branch, which does not
    depend on the group at all, so the PR-number key is admitted for it; on a push
    that key is empty and the group a constant, so it is never admitted there.
    """
    groups = [
        value.partition("#")[0]
        for keys, value in _yaml_mapping_entries(text)
        if keys == ("concurrency", "group") or (keys == ("concurrency",) and value)
    ]
    signals: tuple[re.Pattern[str], ...] = (_BRANCH_GROUP_SIGNAL,)
    if event == "pull_request":
        signals = (_BRANCH_GROUP_SIGNAL, _PULL_REQUEST_GROUP_SIGNAL)
    return any(signal.search(group) for group in groups for signal in signals)


def workflow_text_has_publish_or_deploy_step(raw: str) -> bool:
    """Whether workflow text can publish, deploy, or sign durable output."""
    text = _workflow_without_full_line_comments(raw)
    if any(pattern.search(text) for pattern in _PUBLISH_OR_DEPLOY_PATTERNS):
        return True
    for keys, value in _yaml_mapping_entries(text):
        if len(keys) == 3 and keys[0] == "jobs" and keys[2] == "environment":
            return True
        permission = value.partition("#")[0].strip().strip("\"'")
        if (
            len(keys) >= 2
            and keys[-2] == "permissions"
            and keys[-1] in {"pages", "packages", "deployments"}
            and permission == "write"
        ):
            return True
    return bool(
        re.search(r"\buses:\s*docker/build-push-action@", text, re.IGNORECASE)
        and re.search(r"^\s*push:\s*true\s*(?:#.*)?$", text, re.IGNORECASE | re.MULTILINE)
    )


def workflow_text_is_heal_safe(raw: str, *, event: str = "push") -> bool:
    """Whether workflow text is branch-scoped (for this event) and free of durable side effects."""
    text = _workflow_without_full_line_comments(raw)
    return workflow_has_ref_or_pr_concurrency(
        text, event=event
    ) and not workflow_text_has_publish_or_deploy_step(text)


def heal_safe_declared(workflow: str, event: str) -> bool:
    """The DECLARED gate for one run: is this workflow declared heal-safe for this event.

    A ref-keyed declaration covers every event, pull-request runs included. A
    PR-keyed declaration covers pull-request runs only: on any other event the PR
    number is empty, so the workflow has no concurrency group and nothing protects
    a re-run of it from a live successor; such a run stays exempt.
    """
    if workflow in HEAL_SAFE_WORKFLOWS:
        return True
    return workflow in HEAL_SAFE_PULL_REQUEST_WORKFLOWS and event == "pull_request"


def heal_exempt_workflows(
    workflows: tuple[str, ...] = WATCHED_WORKFLOWS,
    heal_safe: frozenset[str] = HEAL_SAFE_WORKFLOWS | HEAL_SAFE_PULL_REQUEST_WORKFLOWS,
) -> frozenset[str]:
    """Watched workflows that require human recovery: everything declared safe for NO event.

    Classification reads the declaration alone. The YAML itself is read at the
    RUN'S OWN revision, by ``workflow_is_heal_safe_at_revision``, immediately
    before every mutation. Reading the tick's checkout here as well would judge a
    weaker subject: a branch can change a group or add publishing, and the
    checkout cannot see that. Anything the checkout read would have stopped, the
    revision read stops too, before a run is touched.

    A workflow declared safe for pull-request runs only is NOT in this set; its
    push runs are marked exempt per run by ``_mark_heal_exempt`` through
    ``heal_safe_declared``, which sees the event.
    """
    return frozenset(workflow for workflow in workflows if workflow not in heal_safe)


def workflow_is_heal_safe_at_revision(
    api: Api, repo: str, workflow: str, head_sha: str, *, event: str = "push"
) -> bool | None:
    """Whether a workflow is heal-safe AT ONE RUN'S OWN revision, for that run's event.

    Three answers, because a declared-unsafe workflow and one whose safety cannot
    be established are not the same fact and must not share an outcome. ``False``
    means read and judged unsafe, or declared unsafe with no read needed: a human
    recovers it, and the tick is healthy. ``None`` means undeterminable -- no
    ``head_sha``, or the contents read or its decoding failed -- which is a FAILED
    outcome, so a run whose safety nobody could establish cannot age out of its
    recovery window behind a green tick.

    Either way nothing is cancelled or re-run: only ``True`` admits a mutation.
    """
    if not heal_safe_declared(workflow, event):
        return False
    if not head_sha:
        return None
    query = urllib.parse.urlencode({"ref": head_sha})
    encoded_workflow = urllib.parse.quote(workflow, safe="")
    path = f"repos/{repo}/contents/.github/workflows/{encoded_workflow}?{query}"
    try:
        payload = api.get(path)
    except ApiError:
        return None
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return None
    content = payload.get("content")
    if not isinstance(content, str):
        return None
    try:
        encoded = "".join(content.split())
        raw = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (UnicodeError, ValueError):
        return None
    return workflow_text_is_heal_safe(raw, event=event)


def _mark_heal_exempt(verdict: RunVerdict, exempt_workflows: frozenset[str]) -> RunVerdict:
    # The attempt cap is included deliberately. That cap exists to stop US looping
    # re-runs, and an exempt workflow is never re-run by us at all, so the cap is
    # not the operative reason nobody touched the run -- and reporting it instead
    # would leave a stuck orphan behind a GREEN tick, since the attempt cap carries
    # no outcome. The label the summary shows becomes `heal-exempt`; the attempt is
    # kept in the detail so the escalation reason is not lost. Forks are left alone:
    # nobody's token can re-run one, which is a more specific answer than exemption.
    # A workflow declared safe for pull-request runs only is exempt on its push runs
    # (`heal_safe_declared`), so the event is consulted alongside the set.
    exempt = verdict.workflow in exempt_workflows or not heal_safe_declared(
        verdict.workflow, verdict.event
    )
    if exempt and verdict.verdict in (
        ORPHANED,
        CANCELLED_ORPHAN,
        SKIPPED_ATTEMPT_CAP,
    ):
        if verdict.verdict == SKIPPED_ATTEMPT_CAP and not verdict.orphans:
            return verdict
        at_cap = (
            f" It is also at attempt {verdict.run_attempt}, the watchdog's cap."
            if verdict.verdict == SKIPPED_ATTEMPT_CAP
            else ""
        )
        verdict.verdict = HEAL_EXEMPT
        verdict.detail = (
            "the workflow is not heal-safe: its run concurrency is not keyed to a ref or pull "
            "request, or it publishes, deploys, or signs durable output; automatic cancel and "
            f"full re-run are disabled, so a human must recover this orphan.{at_cap}"
        )
    return verdict


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


def _runs_path(repo: str, workflow: str) -> str:
    return f"repos/{repo}/actions/workflows/{workflow}/runs"


def _iter_repo_runs(
    api: Api,
    repo: str,
    *,
    status: str,
    max_pages: int,
    truncated: list[str] | None = None,
    get: Callable[[str], Any] | None = None,
    log: Callable[[str], None] = print,
) -> Iterator[dict[str, Any]]:
    """Yield runs of EVERY workflow with the given status, newest first, across at
    most ``max_pages`` pages of ``PAGE_SIZE``.

    ``GET /repos/{repo}/actions/runs?status=…`` is repo-wide: one paginated call
    returns runs of every workflow, so a single call chain per status covers the
    watched set. The caller filters to the watched set from each run's ``path``.

    Paging stops at the first EMPTY page, or at ``max_pages`` -- the page cap that
    bounds a runaway. A SHORT page does not end this listing: measured against
    `status=queued` on this repository, the endpoint returns 98, then 100, then
    100, then 99 while `total_count` stands at 927, so a page below ``PAGE_SIZE``
    is a property of the index rather than the tail. Treating one as the end stops
    a tick after page one, which collapses the reach to the newest ~2 minutes of
    runs while the orphan threshold is 15 minutes, and hides a six-hour outage.

    An empty page is not proof of the tail either, because the endpoint serves one
    at ``REPO_LISTING_RESULT_CEILING`` too. So reach is judged by comparing what was
    yielded against the first page's ``total_count`` -- but only a shortfall with a
    NAMEABLE cause appends to ``truncated``: the walk spent ``max_pages``, or it
    stopped on an empty page at that ceiling. A shortfall below both is CHURN, since
    ``total_count`` is a page-one snapshot of a set that mutates while the walk runs,
    and it is logged rather than recorded. Opt-in per call site on purpose:
    truncating the recovery pass is normal and its docstring says so, and a
    ``pending`` listing hides nothing a heal could act on, while an unread tail on an
    ACTIONABLE status means the tick could not look where orphans are.
    """
    fetch = get or api.get
    page = 1
    yielded = 0
    total: int | None = None
    hit_cap = False
    while page <= max_pages:
        # The page size never changes between pages: ``page`` is an offset in
        # units of ``per_page``, so a smaller page would re-read the head of the
        # listing and never reach the runs it was meant to fetch.
        query = urllib.parse.urlencode({"status": status, "per_page": PAGE_SIZE, "page": page})
        payload = fetch(f"repos/{repo}/actions/runs?{query}")
        batch = (payload or {}).get("workflow_runs") or []
        if total is None:
            reported = (payload or {}).get("total_count")
            total = reported if isinstance(reported, int) and reported > 0 else None
        if not batch:
            break
        yield from batch
        yielded += len(batch)
        hit_cap = page == max_pages
        page += 1
    if total is None or yielded >= total:
        return
    # A shortfall alone does not mean the tail went unread. ``total_count`` is a
    # snapshot of page one, and a live status set MUTATES during a ten-page walk:
    # a run that leaves `queued` between two page reads lands as `yielded < total`
    # with every page delivered. Failing a tick on that would fail on CHURN, which
    # on a busy repository is most ticks -- and a signal that fires for a non-reason
    # is the one nobody reads.
    #
    # So the tick only fails where the shortfall has a NAMEABLE cause: the walk
    # spent its page cap, or it stopped on an empty page at the result ceiling,
    # where the endpoint serves one instead of the next run. Everything else is
    # reported as the warning it was before.
    at_ceiling = yielded >= REPO_LISTING_RESULT_CEILING
    message = (
        f"{status} run listing reached {yielded} of {total} run(s) "
        f"({page - 1} page(s) read, cap {max_pages}, API ceiling "
        f"{REPO_LISTING_RESULT_CEILING})"
    )
    if hit_cap or at_ceiling:
        log(f"::warning::{message}; the oldest were not read")
        if truncated is not None:
            truncated.append(message)
        return
    log(f"::warning::{message}; the set moved during the walk, and every page was delivered")


def _is_watched(run: dict[str, Any]) -> bool:
    """Whether a repo-wide-listed run belongs to a watched workflow, by its ``path``."""
    return _workflow_of(run) in _WATCHED_SET


def list_runs(api: Api, repo: str, workflow: str, *, status: str, cap: int) -> list[dict[str, Any]]:
    """Newest runs of one watched workflow with the given status filter, at most
    ``cap``, oldest first.

    Workflow-scoped on purpose, and not replaceable by a page of the repo-wide
    listing: that index is ordered by CREATION time, while this sample wants a
    recent COMPLETION. Watched workflows differ by an order of magnitude in
    duration (`fast-gate.yml` runs in about 1.5 minutes, `ci.yml` in about 20),
    so a repo-wide page mixes them and a long run that finished seconds ago sits
    below every short run created since it started. Scoped to one workflow, whose
    runs share a duration, creation order tracks completion order and the newest
    page holds the newest finishes. The total stays capped at ``COMPLETED_SAMPLE``
    either way.

    A SHORT page is not the tail here either: this is the same status-filtered runs
    index as the repo-wide listing, only scoped to one workflow, and that index
    returns pages below ``per_page`` mid-listing. So paging stops on the cap or an
    EMPTY page, never on a short one. In practice a watched workflow's first page
    already exceeds ``cap``, so the empty-page read costs nothing for the
    workflows this samples."""
    runs: list[dict[str, Any]] = []
    page = 1
    while len(runs) < cap:
        # The page size never changes between pages: ``page`` is an offset in
        # units of ``per_page``, so a smaller final page would re-read the head
        # of the listing and never reach the runs it was meant to fetch.
        query = urllib.parse.urlencode({"status": status, "per_page": PAGE_SIZE, "page": page})
        payload = api.get(f"{_runs_path(repo, workflow)}?{query}")
        batch = (payload or {}).get("workflow_runs") or []
        if not batch:
            break
        runs.extend(batch)
        page += 1
    del runs[cap:]
    return sorted(runs, key=lambda run: run["created_at"])


def gather_all_candidate_runs(
    api: Api,
    repo: str,
    *,
    get: Callable[[str], Any] | None = None,
    log: Callable[[str], None] = print,
) -> tuple[list[dict[str, Any]], ApiError | None, list[str]]:
    """Live runs of every watched workflow, any rate-limit abort, and any listing
    that hit its page cap; runs oldest first.

    One paginated repo-wide listing per candidate status covers every workflow at
    once, and each run is kept only if its ``path`` names a watched workflow. This
    is one listing per status instead of one per watched workflow, and it reaches a
    fleet-routed workflow nobody registered (returned, then dropped for not being
    in the watched set) -- breadth a per-workflow chain cannot have. It is NOT
    strictly more: breadth and depth are separate axes, and this form buys the
    first only while ``_iter_repo_runs`` pages deep enough for the second. No
    newest-N truncation is applied here -- an orphan is an OLD run at the tail of
    the newest-first listing, so keeping only the newest N would discard exactly
    the runs being hunted; the page cap bounds the listing cost, and the global
    per-tick heal cap still bounds how many runs are acted on.

    The third element names each ACTIONABLE status whose listing did not reach its
    own ``total_count``. It is a tick-level failure rather than a note, because the
    unread tail is where an orphan sits: see ``OUTCOME_LISTING_TRUNCATED``. A
    ``pending`` shortfall is logged and not recorded, because nothing in that status
    is ever healed -- and `pending` is exactly what grows during the saturation this
    script has to survive, so failing a tick over it would go red for a non-reason
    at the worst moment.
    """
    truncated: list[str] = []
    seen: dict[int, dict[str, Any]] = {}
    try:
        for status in CANDIDATE_STATUSES:
            for run in _iter_repo_runs(
                api,
                repo,
                status=status,
                max_pages=REPO_LISTING_MAX_PAGES,
                truncated=(truncated if status in ACTIONABLE_CANDIDATE_STATUSES else None),
                get=get,
                log=log,
            ):
                if _is_watched(run):
                    seen.setdefault(int(run["id"]), run)
    except ApiError as exc:
        if not exc.rate_limited:
            raise
        return sorted(seen.values(), key=lambda run: run["created_at"]), exc, truncated
    return sorted(seen.values(), key=lambda run: run["created_at"]), None, truncated


def list_all_candidate_runs(
    api: Api, repo: str, *, log: Callable[[str], None] = print
) -> list[dict[str, Any]]:
    """Live runs of every watched workflow, deduplicated, oldest first."""
    runs, aborted, _truncated = gather_all_candidate_runs(api, repo, log=log)
    if aborted is not None:
        raise aborted
    return runs


def _heal_eligible_shape(run: dict[str, Any], repo: str) -> bool:
    """Whether a listed run has a SHAPE a heal could act on, from the listing alone.

    Priority only, never authorization. Every real gate still runs afterwards and
    can still refuse: the workflow's concurrency group read at the run's own
    revision, the successor check, the saturation hold. What this reads is what
    those gates cannot reverse -- an event a heal can act on (``push`` or
    ``pull_request``), a head repository that is this one (no token can re-run a
    fork's run), and a workflow declared heal-safe for that event. All three are on
    the listing row, so the ranking costs no read.
    """
    event = _safe_text(run.get("event"))
    workflow = _workflow_of(run)
    return (
        event in {"push", "pull_request"}
        and not is_fork_run(run, repo)
        and workflow is not None
        and heal_safe_declared(workflow, event)
    )


def _can_carry_a_slow_start(run: dict[str, Any], policy: Policy) -> bool:
    """Whether a run could hold a served start that waited past ``saturation_wait``.

    Two conditions. The run must be in a status that has jobs at all -- a ``pending``
    run is held by its concurrency group with none created, so it can carry no start
    at any age. And it must be at least ``saturation_wait`` old, since a younger one
    cannot contain a wait that long.

    Deliberately NOT a third condition on ``updated_at``. A run untouched for longer
    than ``saturation_lookback`` looks unable to hold a countable start, and excluding
    those would cut the count materially: measured, 352 listed runs pass the two
    conditions above and only 193 were touched inside the lookback. But a run is quiet
    for exactly two reasons, and the API cannot tell them apart from the listing: it
    is a zombie nothing will ever happen to, or ITS JOB HAS BEEN QUEUED THAT WHOLE
    TIME -- the saturation this hold exists for. When such a job is finally served,
    the listing that called its run incapable was a snapshot taken a moment before,
    and the pre-cancel re-read narrows that window without closing it. So the price of
    the two conditions is an over-hold on zombie runs, which costs a heal, against an
    under-hold that cancels work the fleet was about to serve.
    """
    if not _may_hold_jobs(run):
        return False
    return policy.now - parse_timestamp(str(run["created_at"])) >= policy.saturation_wait


def _may_hold_jobs(run: dict[str, Any]) -> bool:
    """The age-free half of ``_can_carry_a_slow_start``: does this run have jobs at all.

    Split out because the two halves age differently. A run's STATUS is a reading of
    the listing and goes stale only when the listing is re-read; its AGE grows with
    the wall clock, so a run just under the line at the sweep is over it minutes
    later. The unread set is therefore retained by this half and re-judged on age at
    each use, rather than collapsed to one integer at the sweep's clock.
    """
    # ACTIONABLE_CANDIDATE_STATUSES answers "may this run be healed"; the question
    # here is "can this run carry a job at all". The two coincide because every status
    # in which jobs exist is also one a heal can act on, and `pending` -- held by its
    # concurrency group with no jobs created -- is in neither. A status added here for
    # heal purposes must be checked against BOTH readings before it is added.
    return _safe_text(run.get("status")) in ACTIONABLE_CANDIDATE_STATUSES


def _evidence_reserve(runs: list[dict[str, Any]], policy: Policy) -> list[dict[str, Any]]:
    """The reserve slice: newest runs that could hold a slow start.

    ``runs`` arrives oldest first. A run that cannot carry a served start past the
    line -- too young, or ``pending`` and so jobless -- can only ever show the fleet
    dispatching, so reserving a read for it leaves the reserve unable to answer the
    other way. Preferring the newest runs that CAN keeps the reserve current while
    letting it answer both.
    """
    capable = [run for run in runs if _can_carry_a_slow_start(run, policy)]
    # Falls back to the newest runs when none is capable: they still show the
    # fleet dispatching, and an empty reserve would read as an outage.
    source = capable or runs
    return source[-LIVE_EVIDENCE_RESERVE:]


def drop_ghost_runs(
    runs: list[dict[str, Any]], policy: Policy, log: Callable[[str], None] = print
) -> list[dict[str, Any]]:
    """The listed live runs minus the ghosts: records past ``GHOST_AFTER`` in a live status.

    Applied before the read bound is drawn, so a ghost is neither read nor counted as
    an unread run able to hold a slow start (see ``GHOST_AFTER`` for why both matter).
    Dropping is by AGE alone, from the listing: the reads that could tell more -- jobs,
    or the run itself -- are the cost being saved. The age is taken from the NEWER of
    ``created_at`` and ``run_started_at``. A re-run keeps the ``created_at`` of the run
    it re-runs and moves ``run_started_at`` to the attempt, so an attempt an operator
    (or this watchdog) started on a run two days old is as young as that attempt; a
    ghost never started, and its two stamps agree. One line names how many were
    dropped, so a tick that acted on a shorter set than it listed says so.
    """
    live: list[dict[str, Any]] = []
    ghosts = 0
    oldest: datetime | None = None
    for run in runs:
        created = parse_timestamp(str(run["created_at"]))
        started_raw = run.get("run_started_at")
        started = parse_timestamp(str(started_raw)) if started_raw else created
        newest = max(created, started)
        if policy.now - newest >= GHOST_AFTER:
            ghosts += 1
            oldest = newest if oldest is None or newest < oldest else oldest
            continue
        live.append(run)
    if ghosts:
        log(
            f"::notice::{ghosts} listed run(s) older than {_fmt_delta(GHOST_AFTER)} dropped as "
            f"ghosts (oldest last started {oldest.isoformat() if oldest else '?'}): GitHub "
            "cancels a job that has not started within a day, so a run still listed live "
            "past that has nothing a heal could act on, and reading it would only spend the bound"
        )
    return live


def live_runs_within_read_bound(
    runs: list[dict[str, Any]], policy: Policy, log: Callable[[str], None] = print
) -> tuple[list[dict[str, Any]], dict[int, datetime]]:
    """The live runs whose jobs this sweep may read: oldest first, evidence reserved.

    The reads serve two purposes that pull opposite ways. Classifying an orphan
    wants the OLDEST runs, the only ones that can be actionable. Judging whether
    the fleet still dispatches wants a run's served CodeBuild starts, which is the
    evidence ``resolve_hold`` weighs. A bound spent purely oldest first starves
    the second at exactly backlog scale, and a sweep with no dispatch evidence
    heals nothing, which is safe but useless precisely when the watchdog is
    needed. So the head of the bound goes to the oldest runs and its tail is
    reserved for evidence.

    The reserve takes the newest runs THAT ARE THEMSELVES at least
    ``policy.saturation_wait`` old. A younger run cannot contain a start that
    waited that long, so reserving the newest runs outright yields prompt starts
    only and can never see the slow start that holds -- the reserve would answer
    one of the hold's two questions and be structurally blind to the other. When
    no run is old enough, the newest are taken anyway: they can still show the
    fleet dispatching, and a sweep with nothing at all falls to the
    completed-run sample instead.

    Within the head, runs that could actually be acted on go first: the
    ``_heal_eligible_shape`` pair AND an age past the orphan threshold. Oldest-first
    alone
    ranks by age, and the oldest live runs at this repository are runs no heal can
    ever act on: measured on this repository, 220 watched live runs sit past the
    orphan threshold and 18 past a day, the oldest 36 days, runs that stay listed
    and therefore re-read the same slots on every tick -- a fork's, which no token
    can re-run, or a closed pull request's, which is cancelled and not re-read. A
    same-repository run of a workflow declared heal-safe for its event -- the only
    kind that can be cleared, and the kind that holds a branch's concurrency slot
    while it is stuck -- ranked 30th of 40 slots at six hours old. That margin shrinks as
    zombies accumulate, so age is the ordering WITHIN each class rather than
    across them.

    The age condition is what keeps the priority honest now that the reserve no
    longer absorbs the youngest runs: every ``push`` run of a heal-safe workflow
    carries the eligible shape, so shape alone would let this minute's pushes
    outrank an orphan stuck for hours. They are still read, just not ahead of it.
    """
    runs = drop_ghost_runs(runs, policy, log)
    if len(runs) <= LIVE_CLASSIFY_READS:
        return runs, {}
    # Disjoint by construction, so a run cannot be read twice: the reserve is cut
    # out of the listing before the classify pool is drawn from what remains.
    reserved = _evidence_reserve(runs, policy)
    reserved_ids = {run["id"] for run in reserved}
    pool = [run for run in runs if run["id"] not in reserved_ids]
    # Sized off the reserve ACTUALLY taken, not off the constant. A short listing hands
    # back fewer than LIVE_EVIDENCE_RESERVE slots, and subtracting the constant would
    # leave that difference unspent -- reading fewer runs than the bound allows, which
    # classifies fewer orphans and leaves more unread capable runs holding the heal.
    oldest = LIVE_CLASSIFY_READS - len(reserved)
    banded = sum(1 for run in reserved if _can_carry_a_slow_start(run, policy))
    # One line, emitted here rather than before the reserve is computed: the split it
    # names has to be the split this tick actually took. Interpolating the constants
    # earlier printed 40/10 on a tick that then spent 47/3, so the read-budget
    # diagnostic described a division no sweep performed.
    log(
        f"::notice::{len(runs)} live runs reached the per-tick live job-read cap of "
        f"{LIVE_CLASSIFY_READS}; the {oldest} classified are drawn actionable-shaped "
        "first (heal-eligible and past the orphan threshold) then oldest first, and "
        f"{len(reserved)} reads are reserved for the newest runs able to hold a start "
        f"that waited {_fmt_delta(policy.saturation_wait)} ({banded} of them can), whose "
        "served CodeBuild starts are the dispatch evidence; the rest wait for the next "
        "tick, and any unread run old enough to hold a slow start holds the heal back"
        + (
            ""
            if banded == len(reserved)
            else " -- the shortfall is the newest-runs fallback, which reports the fleet "
            "dispatching but cannot show a slow start"
        )
    )

    def actionable_shape(run: dict[str, Any]) -> bool:
        # Shape AND age: a run younger than the orphan threshold is never actionable,
        # and every run of a heal-safe workflow carries the heal-eligible shape, so
        # shape alone would let the youngest pushes outrank an orphan that has been
        # stuck for hours. They are still read, just not ahead of it.
        return (
            _heal_eligible_shape(run, policy.repo)
            and policy.now - parse_timestamp(str(run["created_at"])) >= policy.orphan_after
        )

    eligible = [run for run in pool if actionable_shape(run)]
    rest = [run for run in pool if not actionable_shape(run)]
    classified = (eligible + rest)[:oldest]
    # The caller reads jobs in the order given and the sweep's log is read
    # chronologically, so the whole selection is sorted by age. Sorting the classify
    # slice alone was enough only while the reserve was the newest runs outright; the
    # reserve is now an age band in the middle, so it has to be sorted in with them.
    selected = sorted(classified + reserved, key=lambda run: run["created_at"])
    selected_ids = {run["id"] for run in selected}
    # The second value is the premise the partial hold is judged on: the runs this
    # sweep did not read that could hold a served start past the threshold, kept as
    # id -> creation time so the judgement is re-made on the clock of each later use
    # instead of frozen here. Retaining the runs that COULD have carried a start rather
    # than reporting "the bound was reached" is what makes the hold's premise
    # checkable: a sweep whose unread band could not have held one may act. It does not
    # make the hold rare here -- about 302 unread capable runs against a 50-read bound
    # -- it makes it exact: a bound spent entirely on runs too young, or on jobless
    # `pending` runs, leaves nothing unseen and releases the heal, where keying on the
    # bound alone never did.
    unread_candidates = {
        int(run["id"]): parse_timestamp(str(run["created_at"]))
        for run in runs
        if run["id"] not in selected_ids and _may_hold_jobs(run)
    }
    return selected, unread_candidates


def list_recent_cancelled_runs(
    api: Api, repo: str, *, log: Callable[[str], None] = print
) -> list[dict[str, Any]]:
    """Cancelled runs of every watched workflow, oldest cancellation update first.

    The reach is BEST-EFFORT and deliberately carries no coverage claim. GitHub orders
    this index by CREATION time while recovery selects by cancellation time, and offers
    no ordering by the latter, so nothing readable from the listing can establish that
    every run cancelled inside the window was seen: a long-lived run cancelled during a
    burst sits past any page cap, which is exactly what the 2026-09-20 orphans looked
    like. Three rounds of review went into inventing a signal for that gap; each spelling
    claimed more than the API can answer, so the honest form is the truncation WARNING
    the paging already logs, plus this docstring. A cancelled run that never got listed
    needs `gh run rerun` by hand, and the reach only changes how often that is true.
    """
    watched: dict[int, dict[str, Any]] = {}
    for run in _iter_repo_runs(
        api,
        repo,
        status="cancelled",
        max_pages=RECOVERY_LISTING_MAX_PAGES,
        log=log,
    ):
        if _is_watched(run):
            watched.setdefault(int(run["id"]), run)
    return sorted(
        watched.values(),
        key=lambda run: parse_timestamp(str(run.get("updated_at") or run["created_at"])),
    )


def list_jobs(api: Api, repo: str, run_id: int) -> list[dict[str, Any]]:
    """Every job of the run's latest attempt.

    A SHORT page does not end this listing either, but the reason it is safe to keep
    reading differs from the runs index. The payload carries ``total_count``, so the
    tail is known exactly and a page below ``per_page`` costs nothing: the loop stops
    when that many jobs are in hand. The measurement behind the short-page rule was
    taken on the status-filtered runs index, not here, and ``filter=latest`` is the
    same shape of post-paging filter -- so a re-run run's jobs page can be short
    mid-listing, and a job dropped for that reason is a queued fleet job the tick
    never sees, which is the orphan evidence the whole classification rests on.
    Without ``total_count`` the listing falls back to reading until an EMPTY page,
    which costs one extra call rather than risking a dropped job.
    """
    jobs: list[dict[str, Any]] = []
    total: int | None = None
    page = 1
    while page <= JOBS_LISTING_MAX_PAGES:
        query = urllib.parse.urlencode({"per_page": PAGE_SIZE, "page": page, "filter": "latest"})
        payload = api.get(f"repos/{repo}/actions/runs/{run_id}/jobs?{query}")
        batch = (payload or {}).get("jobs") or []
        if not batch:
            return jobs
        jobs.extend(batch)
        if total is None:
            reported = (payload or {}).get("total_count")
            total = reported if isinstance(reported, int) and reported >= 0 else None
        if total is not None and len(jobs) >= total:
            return jobs
        page += 1
    return jobs


class LookupInconclusive(Exception):
    """The successor question could not be answered, so no mutation may rest on it.

    For a push run: the branch listing did not reach the run being judged, so
    "newest" is unknown. For a pull-request run also: a newer run at the judged
    run's own head SHA whose pull request neither payload identifies, so successor
    (the same pull request's; a re-run of the judged run would cancel it through the
    group) and sibling (another pull request on a shared head branch) cannot be told
    apart. Both fail closed: the run is left as it is, the tick reds and names it.
    """


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
            payload = api.get(f"{_runs_path(repo, verdict.workflow)}?{query}")
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
        if not runs:
            # An EMPTY page is the tail; a SHORT one is not. This is the same
            # status-ordered runs index that returns pages below ``per_page``
            # mid-listing, only filtered by branch and event, and stopping on a
            # short page here answers "not within the newest listed runs" while
            # the judged run sits on the very next page -- refusing a re-run the
            # evidence allows, and leaving a cancelled run behind a green tick.
            break
        page += 1
    raise LookupInconclusive(
        f"run {verdict.run_id} of {verdict.head_repo}:{verdict.head_branch} ({verdict.event}) is not "
        f"within the {BRANCH_LISTING_MAX_PAGES * BRANCH_LISTING_DEPTH} newest listed runs, so whether a "
        f"newer run exists cannot be told"
    )


def _open_pull_request_heads(api: Api, repo: str, verdict: RunVerdict) -> set[str]:
    """The head SHAs of every OPEN pull request whose head is this run's branch.

    Read from the pulls API by ``head=<owner>:<branch>``, which is exact on the
    branch name within one head repository -- a fork's same-named branch is a
    different owner and is not listed. Two pull requests open on the same head
    branch return the same SHA twice; the set is what matters. One page, and a full
    page is refused: it may have a tail, and "current" must never be answered from
    a listing that might not contain the run's pull request.
    """
    owner = verdict.head_repo.partition("/")[0] or repo.partition("/")[0]
    query = urllib.parse.urlencode(
        {
            "head": f"{owner}:{verdict.head_branch}",
            "state": "open",
            "per_page": PULL_REQUEST_LISTING_DEPTH,
        }
    )
    try:
        pulls = api.get(f"repos/{repo}/pulls?{query}")
    except ApiError as exc:
        raise LookupInconclusive(
            f"the open pull requests on {verdict.head_repo}:{verdict.head_branch} could not be read: {exc}"
        ) from exc
    if not isinstance(pulls, list):
        raise LookupInconclusive(
            f"the open pull requests on {verdict.head_repo}:{verdict.head_branch} came back malformed"
        )
    if len(pulls) >= PULL_REQUEST_LISTING_DEPTH:
        raise LookupInconclusive(
            f"{verdict.head_repo}:{verdict.head_branch} has {len(pulls)} or more open pull requests, "
            f"more than one page; whether run {verdict.run_id} is current cannot be told"
        )
    return {
        str(((pull or {}).get("head") or {}).get("sha") or "").lower()
        for pull in pulls
        if isinstance(pull, dict)
    } - {""}


def _current_or_successor_for_pull_request(api: Api, repo: str, verdict: RunVerdict) -> int:
    """The pull-request analogue of ``newest_run_id_for_branch``: judged by HEAD SHA,
    then by the listing for a newer run AT that SHA.

    Two reads. The pulls API says which head SHAs the open pull requests on the
    branch have; the runs listing (this branch, this event, same head repository,
    paged newest-first until the judged run is seen, as the push path does) says
    which newer runs exist. Neither alone is enough.

    The SHA answers supersession by PUSH, and answers it without the ambiguity a
    branch-name listing has: two pull requests sharing a head branch carry the same
    head SHA, so a run at that SHA is current for both, and nothing here needs
    ``pull_requests[].number`` for that. No open pull request has the branch: the
    request closed or merged, ``SUPERSEDED_WITHOUT_SUCCESSOR``. The head moved: the
    successor is the newest listed run at an open head that the payloads identify as
    the same pull request's.

    The listing answers supersession WITHOUT a push. `labeled`, `unlabeled`,
    `edited` and `reopened` each start a new run at the SAME head SHA, and every
    declared pull-request workflow cancels the run in progress when one arrives. A
    re-run of the older run would cancel that newer one through the group and
    stand in its place -- with no successor named, nothing would restore it. So a
    newer same-repository run at the judged run's own SHA is read as this run's
    successor when the two payloads name a common pull request, ignored as a sibling
    pull request's run (a shared head branch) when they name disjoint ones, and
    otherwise -- most same-repository runs carry no ``pull_requests[]`` at all --
    the answer is ``LookupInconclusive``, naming the runs, because "current" is what
    licenses a cancel through the other run's group and is never assumed. That is
    deliberately not resolved by observing the group's own cancel: every orphan this
    script meets exists during a fleet outage, where the run to observe stays queued
    and observation cannot settle, and a green outcome resting on an unobserved
    provider behaviour is the false green this script must never produce.

    A successor is named only when identified as the same pull request's. When the
    head moved while the pull request stays open, the answer is
    ``SUPERSEDED_SUCCESSOR_UNIDENTIFIED`` whether a newer run at the new head is
    listed and unidentified or not listed yet: superseded for every judgement made
    before a mutation, never a run to restore -- restoring a sibling's run would
    leave the same pull request's run, cancelled through the group, unreported.
    """
    open_heads = _open_pull_request_heads(api, repo, verdict)
    if not open_heads:
        return SUPERSEDED_WITHOUT_SUCCESSOR
    own_sha = verdict.head_sha.lower()
    at_open_head = own_sha in open_heads
    successor: int | None = None
    ambiguous: list[int] = []
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
            payload = api.get(f"{_runs_path(repo, verdict.workflow)}?{query}")
        except ApiError as exc:
            raise LookupInconclusive(
                f"the branch listing for {verdict.head_repo}:{verdict.head_branch} could not be read: {exc}"
            ) from exc
        runs = (payload or {}).get("workflow_runs") or []
        for run in runs:
            run_id = int(run["id"])
            if run_id == verdict.run_id:
                # The judged run is in view, so every newer run of the branch has
                # been seen and the answer is settled. An identified same-pull-request
                # successor decides it whatever else was seen.
                if successor is not None:
                    return successor
                if ambiguous:
                    raise LookupInconclusive(
                        f"run(s) {', '.join(str(r) for r in ambiguous)} are newer {verdict.event} "
                        f"runs of {verdict.head_repo}:{verdict.head_branch} at the same head SHA "
                        f"{own_sha[:12]} as run {verdict.run_id}, and whether each belongs to the "
                        f"same pull request (its successor, from a label or edit) or to another pull "
                        f"request sharing the branch cannot be told from the payloads; re-running "
                        f"{verdict.run_id} could cancel a successor, so neither is assumed"
                    )
                if at_open_head:
                    return verdict.run_id
                # The head moved past this run while its pull request stays open. Whether
                # a newer run at the new head is listed and unidentified, or not listed
                # yet, the answer is the same: superseded, with no run to restore.
                return SUPERSEDED_SUCCESSOR_UNIDENTIFIED
            head_repo = str((run.get("head_repository") or {}).get("full_name") or "")
            if head_repo.lower() != verdict.head_repo.lower():
                continue
            head_sha = str(run.get("head_sha") or "").lower()
            if head_sha not in open_heads:
                continue
            theirs = _pull_request_numbers(run)
            same_pull_request = bool(set(theirs) & set(verdict.pull_request_numbers))
            if same_pull_request:
                if successor is None:
                    successor = run_id
                continue
            if theirs and verdict.pull_request_numbers:
                # Disjoint, both known: a sibling pull request's run on a shared head
                # branch, in its own concurrency group. Neither successor nor threat.
                continue
            if head_sha == own_sha:
                ambiguous.append(run_id)
        if not runs:
            break
        page += 1
    raise LookupInconclusive(
        f"run {verdict.run_id} of {verdict.head_repo}:{verdict.head_branch} ({verdict.event}) is not "
        f"within the {BRANCH_LISTING_MAX_PAGES * BRANCH_LISTING_DEPTH} newest listed runs, so which "
        f"run succeeded it cannot be told"
    )


def current_or_successor_id(api: Api, repo: str, verdict: RunVerdict) -> int:
    """The run that carries this branch's verdict now: this run, its successor, or
    ``SUPERSEDED_WITHOUT_SUCCESSOR``.

    Dispatches on the event. A push run is judged by the branch listing
    (``newest_run_id_for_branch``); a pull-request run by head SHA against the open
    pull requests on the branch and by the listing for a newer run at that SHA
    (``_current_or_successor_for_pull_request``). Every
    caller asks this, never one shape's function directly, so the two shapes cannot
    drift apart at one call site and not another.
    """
    if verdict.event == "pull_request":
        return _current_or_successor_for_pull_request(api, repo, verdict)
    return newest_run_id_for_branch(api, repo, verdict)


def is_current_run(api: Api, repo: str, verdict: RunVerdict) -> bool:
    """Re-running a run that a newer push has superseded would cancel the newer run
    through the workflow's own concurrency group, so every re-run checks this first."""
    return current_or_successor_id(api, repo, verdict) == verdict.run_id


def supersession_clears_hold(
    api: Api, policy: Policy, verdict: RunVerdict, log: Callable[[str], None]
) -> bool:
    """Whether a fleet hold has nothing left to protect on this run. One branch listing.

    The dispatch-evidence hold exists so a queued job that a runner may still pick
    up is not cancelled out from under the run that wants its result. A run a newer
    push has superseded has no result anyone wants: the branch moved on, and the heal
    path's own pre-re-run check will decline to re-run it for exactly that reason. So
    whether the fleet is dispatching, saturated or out decides nothing about it.

    What holding it DOES cost: a workflow whose group keeps superseded runs alive
    (``cancel-in-progress: false``, which ``ci.yml`` and ``fast-gate.yml`` both set on
    ``main``) admits one running plus one pending run, so a superseded run that never
    terminates holds the running slot and GitHub evicts every later commit's run from
    the pending slot. Measured: ``fast-gate.yml`` run 35893226216 sat ``queued`` on one
    orphaned job for 6 hours behind a partial-evidence hold; every later ``main`` Fast
    Gate was evicted without running, and ``ci.yml``'s ``await-fast-gate`` failed
    closed at its 720-second budget on each one, so 25 pushes produced 11 failures and
    zero verdicts.

    Asked ONLY when a hold would otherwise apply, so the ordinary orphan pays no extra
    listing. Push and pull-request runs alike, through ``current_or_successor_id``: a
    pull-request run is judged by head SHA against its branch's open pull requests
    and by the listing for a newer run at that SHA.
    The head repository must be this repository; a fork cannot push to its branches,
    so that test is belt-and-braces rather than the fork boundary itself.

    A lookup that cannot answer leaves the hold standing: cancelling needs
    supersession ESTABLISHED, never assumed from a failed read.

    Releasing the hold does not by itself cancel anything. The run stays an orphan
    and reaches ``heal_runs``, which for any attempt past the first asks
    ``_rerun_attempt_may_be_cancelled`` immediately before its cancel: a
    superseded later attempt may be somebody's own ``gh run rerun``, so it is left
    untouched under a FAILED outcome that names the run. That guard sits at the
    cancel rather than here so that a held run and an unheld one end the same
    way -- reported red, not held green with no outcome -- and so the answer is
    given once, where the irreversible step is.
    """
    if verdict.event not in {"push", "pull_request"}:
        return False
    if verdict.head_repo.lower() != policy.repo.lower():
        return False
    try:
        if is_current_run(api, policy.repo, verdict):
            return False
    except LookupInconclusive as exc:
        log(
            f"{_label(verdict)}: the fleet hold stands, because whether a newer run "
            f"supersedes this one cannot be told ({exc})"
        )
        return False
    log(
        f"{_label(verdict)}: a newer push supersedes it, so the fleet hold has no result "
        f"to protect and is not applied; freeing its concurrency group is what a cancel "
        f"would then buy, the re-run check still declines to re-run it, and past attempt 1 "
        f"the heal path declines even the cancel"
    )
    return True


def _rerun_attempt_may_be_cancelled(
    api: Api, policy: Policy, verdict: RunVerdict, log: Callable[[str], None]
) -> bool:
    """Whether cancelling an orphan past attempt 1 can still end in a re-run.

    Cancelling a superseded orphan is never followed by a re-run: ``_rerun``
    declines a superseded run, and the ``superseded-before-cancel`` outcome is not
    a failed one. For attempt 1 that is the intended trade -- nobody re-ran it, so
    nothing anyone did is lost, and the cancel frees its concurrency group. A later
    attempt may BE somebody's ``gh run rerun`` of the stuck run, the operator
    response this script's own logs ask for, and cancelling it ends in silent loss:
    the recovery pass will not restore it either, because ``classify_cancelled_run``
    classifies a superseded cancelled run out of ``CANCELLED_ORPHAN``. So the
    question is asked HERE, immediately before the cancel and on every route to it
    (a fleet hold released by ``supersession_clears_hold`` lands here too): a run
    that is still its branch's newest is cancelled and re-run like any orphan; a
    superseded one, or one whose supersession cannot be told, is left untouched --
    and reported as a FAILED outcome, because the run then holds its concurrency
    group until a human frees it, and nothing else will tell them.
    """
    try:
        if is_current_run(api, policy.repo, verdict):
            return True
    except LookupInconclusive as exc:
        log(
            f"::error::{_label(verdict)}: left untouched, because this is attempt "
            f"{verdict.run_attempt} and whether a newer push supersedes it cannot be told "
            f"({exc}); cancelling a re-run attempt that turns out superseded would discard "
            f"somebody's work irrecoverably. It holds its concurrency group until a human "
            f"decides: `gh run cancel {verdict.run_id}` if the branch has moved on, "
            f"`gh run rerun {verdict.run_id}` if its result is still wanted."
        )
        return False
    log(
        f"::error::{_label(verdict)}: left untouched, because this is attempt "
        f"{verdict.run_attempt} and a newer push supersedes it: the cancel would not be "
        f"followed by a re-run, and a re-run attempt may be somebody's own, so cancelling "
        f"it could discard their work irrecoverably. It holds its concurrency group, and "
        f"every later push's run is evicted behind it, until a human frees it: "
        f"`gh run cancel {verdict.run_id}`."
    )
    return False


def _supersession_is_answerable(
    api: Api, policy: Policy, verdict: RunVerdict, log: Callable[[str], None]
) -> bool:
    """Whether the successor question for a first-attempt PULL-REQUEST orphan can be
    answered, asked immediately before its cancel.

    The cancel comes first and the re-run second, and only the re-run asks whether a
    newer run supersedes this one. For a first attempt a superseded answer is the
    intended trade (the cancel frees the group; nothing anyone did is lost), so the
    answer itself is not needed here. An UNANSWERABLE lookup is another matter: after
    the cancel it leaves the run cancelled with nobody to re-run it, the lost verdict
    this script exists to prevent. On a push run that takes a failed listing read,
    rare enough that the push path keeps its one-listing budget and reports it after
    the fact. On a pull-request run it is an ordinary shape: a newer run at the same
    head SHA whose pull request neither payload names, so successor or sibling cannot
    be told, and cancelling THIS run on that uncertainty would destroy a verdict that
    may be the current one. So for a pull-request run the lookup is made here, before
    anything irreversible, and an inconclusive answer leaves the run untouched under
    the same failed outcome the re-run would have reported, naming the run and the
    command. The answer is not cached: the re-run asks again on the state after the
    cancel, which is the state that matters for it, and a same-SHA run that appears in
    between is met there the same way (withdrawn, failed, named).
    """
    try:
        current_or_successor_id(api, policy.repo, verdict)
    except LookupInconclusive as exc:
        log(
            f"::error::{_label(verdict)}: left untouched, because whether a newer run supersedes "
            f"it cannot be told ({exc}); cancelling it first would leave it cancelled with nobody "
            f"able to re-run it. Decide by hand: `gh run cancel {verdict.run_id}` if the branch "
            f"or pull request has moved on, `gh run rerun {verdict.run_id}` if its result is still "
            f"wanted."
        )
        return False
    return True


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
        # live run this sweep never read. Hold rather than guess.
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
            f"start went unread against this tick's job-read bound of {LIVE_CLASSIFY_READS}, so "
            f"saturation cannot be ruled out; nothing healed. Raising that bound or narrowing the "
            f"listing is the response -- it is a job-read budget against the shared installation "
            f"quota, not the API's reachable window",
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
        if not is_current_run(api, policy.repo, verdict):
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
            f"the per-tick cap of {RECOVERY_CLASSIFY_READS} classify reads was reached before "
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
    reads_left = RECOVERY_CLASSIFY_READS
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
                f"reached the per-tick cap of {RECOVERY_CLASSIFY_READS} cancelled runs to "
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
            return is_current_run(api, policy.repo, _base_verdict(run, policy.now))

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

    outcomes = {
        verdict.run_id: OUTCOME_HUMAN_REQUIRED
        for verdict in verdicts
        if verdict.verdict == HEAL_EXEMPT
    }
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
            "already the API's reachable window, so the remedy is to NARROW the listing (by "
            "created window, branch or event) rather than to page deeper"
        )
    return 1 if FAILED_OUTCOMES & set(outcomes.values()) else 0


if __name__ == "__main__":
    sys.exit(main())

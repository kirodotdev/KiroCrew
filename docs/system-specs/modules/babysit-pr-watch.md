# Babysit PR watch

## Purpose

The agent-facing babysit flow prefers `monitor_watch`. It creates a durable,
typed structured monitor through a session-bound directive. The controller probes
the provider before invoking the model, persists canonical observations and
budgets, and wakes the owning session only for a new actionable fingerprint.
Provider-fact-only GitHub review readiness therefore spends no agent turn while
the pull request is unchanged.

How strongly that preference reads is an installation's choice:
`monitoring.prefer_structured_arming` (default off) decides whether the tool
descriptions offer the structured path only once the objective is judged fully
typed-decidable, or name it the default for a supported pull request with the
prompt loop as the exception. It refuses neither tool, and in both positions
evidence the typed provider cannot observe stays on the prompt loop. See
`monitor-architecture.md` for the two costs of defaulting to the structured path.

`monitor_start` creates a finite same-session AutoNudge loop for objectives or
evidence the structured provider cannot decide, including generic comments and
advisory review text. Its stateless directive is validated by
`mcp_tools.control.monitor_start`, then applied by
`dashboard.session_directive_apply._monitor_start` through
`autonudge_authz.authorize_and_add_nudge`. `AutoNudgeService` persists and
schedules the loop. Those two `monitor_start` surfaces are the only callers that
ask for the gate; the chokepoint defaults every other caller UNGATED, the generic
REST route included. Gating is the state that can silently stop work, so a caller
that names no value resolves toward spending a turn per interval rather than toward
a watch that deactivates itself. A loop whose instruction names exactly one public
GitHub pull request attaches `PrWatchProbe`, which FETCHES that pull request every
tick and hands the reading to the wake judge.

There is no script-cron driver. A babysit request uses `monitor_watch` or a finite
`monitor_start` loop owned by the session that can inspect and act on a wake, both
of which run in the gateway -- which is what lets the gated path reach the judge at
all. A cron script runs as a sandboxed subprocess with no gateway credential and no
decisions provider, so a reading made there has nothing to decide with.

A registered script job holding its own copy of the removed driver can still reach
the probe through the installed package, and the probe refuses that context: the
watch identity raises, and because the raise is not a `ValueError` the kernel does
not convert it to `Done`. It propagates instead, so the scheduler counts a failed
run: the message naming `monitor_start` lands in `last_error` and the job is
AUTO-PAUSED once the consecutive-failure threshold is reached. It stays listed,
paused, saying what to arm instead -- the job record is the only durable trace that
the watch was ever armed, so deleting it would take the evidence with the watch.
The alternative to refusing at all is a job that polls on schedule, decides
nothing, and reports nothing, which reads to its owner as a watch still running.
The in-gateway driver marks its own context, so the refusal reaches only the
subprocess path.

### What a gated loop changes about the numbers

`max_cycles` counts DELIVERED cycles, so for a gated loop it bounds delivered
TURNS rather than intervals elapsed -- one field bounding two different quantities
depending on whether inference fired, which any budget UI or operator reasoning
has to know. Not wakes: a wake is only one of the four things that consume the
budget, alongside a streak-floor delivery, a gate fallback and a post-wake
follow-up, so reading the cap as a wake count under-states what it spends.
A gated loop is never starved: after `_MAX_QUIET_STREAK` consecutive quiet
observations it is delivered anyway, counted apart from wakes in `floor_ticks` so
a periodic delivery is never read as a real signal. Every uncertain path -- no
probe, no inferable target, a probe defect, a kernel that reached no verdict --
fires as before, because a wrongly-quiet tick is silence with half-finished work
behind it while a wrongly-spent tick costs what every tick costs today.

## Automatic goal pursuit

An actionable human request can start the existing `/goal` loop without a slash
command or a mode choice. The agent decides from the request and conversation,
calls the session-bound `goal` tool, and continues working in the same turn.
Factual questions and discussion do not require a goal. Design-only requests
retain design as their finish line. Requests served by existing monitor/watch
tools use those tools directly, preserving the session's single-loop contract.

Goal pursuit uses the same tool authorization, approvals and credential
protections as an ordinary session. Starting a goal grants no additional tool
permission. The shared continuation does not impose a separate blanket ban on
`git push` or credential reads; each operation remains subject to the existing
session permissions and user authorization.

`NudgeLoop.goal` carries the objective, completion criteria, progress, status and
evidence in `autonudge.json`. A goal is an ungated prompt loop, not a structured
monitor or a second task store. Ready work resumes after one second of idle time;
an explicitly waiting goal uses a 60-second cadence. Ordinary watches keep their
existing deadlines. New goals have a 50-cycle, four-hour backstop. Reaching a
backstop pauses work and is not completion.

`GOAL_MAX_OBJECTIVE_CHARS` bounds each objective to 16,000 characters after
redaction at `GoalState` retention, including revisions and store reloads; the
goal tool schema advertises and validates the same input limit. Overflow is
explicitly rejected before mutation, never truncated. This deliberately breaks
the previously unbounded manual `/goal` contract above that limit while
preserving full accepted objectives, including the existing 10–12k-character
cases, through revisions and persistence. Criteria and evidence remain limited
to eight items of 240 characters, and progress to 600 characters. Both shared
authorizers accept a continuation over the generic 8,000-character message cap
only when its raw text exactly matches `continuation_message(goal)` for the
supplied typed goal, before normalization or redaction. A mismatched typed
message is refused. All admission, redaction, generation and critical-audit
checks still apply. Generic prompt loops keep their existing message cap.
The goal tool's directive envelope has its own transport limit; progress and
status revisions omit the objective rather than resending or truncating it.

`/goal --max N` keeps its 1–50 cycle range and default of 50. `/goal clear`
removes only the identified typed goal. If the session instead holds a watch,
monitor, or other goal-less loop, it reports that no goal is armed and preserves
that record. Those loops remain manageable through their existing controls.
`off` is ordinary objective text, not a subcommand.
Starting `/goal <objective>` preserves an existing watch or unfinished goal;
revise the goal through `goal(action="update")`, or clear it before starting
another. A completed or ended typed goal can be replaced by a new goal.
New pursuit goals use Stop/Pause/Resume instead of creating sentinel files.
Stored goal-less loops retain their instruction, sentinel and budget contracts.
Generic REST edits of a typed goal's instruction or resumption of a finished
goal return an HTTP 409 with code `autonudge_update_refused`.
The stored cycle and runtime limits of a typed goal cannot be changed by an
update; a refused mixed patch changes no fields. Owner-authenticated REST Resume
and human `goal(action="resume")` keep the same objective, progress and remaining
budget. Both refuse spent budgets, while a manual pause or approval stall can
be resumed with budget remaining.
REST Resume supplies the revision observed by the user as `expected_generation`;
the existing store mutation lock checks it before changing any state. Missing or
stale revisions on a typed-goal Resume return HTTP 409; malformed revisions return
HTTP 400, and zero is valid. A later Stop wins over an earlier Resume request,
including a Stop received while the goal is already paused. The native goal tool
retains its existing generation and human-provenance admission checks. Goal-less
Resume remains compatible.

`monitor_update` refuses typed goals and directs the caller to the goal tool.
Both store creation paths preserve an unfinished typed goal, including one
paused by a cycle or runtime limit; `monitor_start` and `monitor_watch` cannot
replace its objective or progress. These checks run before mutation under the
existing store lock, so an outstanding wake cannot renew or replace a goal
after the scheduler pauses it. Completed and ended goals retain the existing
human-started goal replacement path. Goal-less loops keep their existing
budget-edit, bound-revival and stopped-replacement contracts.

The host admits automatic creation only from an authenticated human turn.
That goal's own continuation can update progress or finish with evidence, but
cannot invent a new user goal or resume a paused one. Updates name the current
goal id and configuration generation; the commit rechecks identity and the
producing turn's Stop state under the existing mutation lock. A failed disk write
rolls back the metadata along with the loop. Completed and ended goals remain
inspectable until the next human goal replaces them. Existing watches and paused
goals are preserved; explicit abandonment uses `goal(action="end")`.
Manual `/goal` carries the runner's live slot and session Stop predicate through
authorization to that same admission check. Replacement checks admission again
after awaited provider-credential cleanup, before removing the old row or
publishing its successor. Refusal retains the old row and restores any provider
credential grant revoked for the uncommitted replacement.
During a Discord or Webex goal wake, a human correction gains goal authority
only after the backend consumption echo matches the registered human steer.
The same goal can then be explicitly ended; automation steers and write
acknowledgements cannot authorize abandonment. A later Stop still wins.
Revisions use the existing update authorizer's critical audit before mutation;
an unavailable audit store refuses the change.

Dashboard Stop pauses future goal turns and cancels the current response.
If storage fails during Stop, pursuit stays paused in the running process and
the save failure is logged. The retained `goal_pause_unsaved` reason, Stop
response, channel reply, and goal panel disclose that a restart may lose the pause.
The panel offers retrying the save rather than resuming work. Only a successful
write acknowledges a saved pause.
Repeated Stop invalidates earlier Resume requests without reviving work or
resetting progress and limits. Completed and budget-limited goals keep their
status and stop reasons. Retrying an unsaved pause clears its unsaved marker only
after the write succeeds. If saving fails, the running process retains the
refusal of those earlier requests.
Human steering consumed during an automatic turn can redirect that goal.
Queue-preserving handovers keep pursuit active; explicit Stop pauses separately.
An empty or unmatched consumption echo, or an automation-origin steer, cannot
grant that authority.
The chat's automation control shows the goal objective and status. Its popup
shows criteria, progress, evidence, and pause/resume; users steer in the existing
chat. The REST and WebSocket projections carry the same goal metadata.
In a dashboard session linked to a channel, Pause targets the visible chat slot
and inspection reads the loop's channel binding.
Goal admission, slash commands, Stop and pause-save warnings share
the exact-binding collection in `goal_actions`, consulting existing slot-bound loops
through the live slot's explicit `linked_session_key`. This prevents a new goal
from arming beside a legacy loop in that same conversation. The singular
`goal_loop_for_session` resolver refuses multiple matching records for inspection,
creation or revision. Stop pauses every typed goal on those explicit bindings,
leaves goal-less watches untouched, and reports an unsaved pause if any owned goal
has one. No inverse mapping is guessed from a normalized slot name: every
candidate, including the primary dashboard binding, must match the stored key
exactly. A legacy row without a live explicit link remains inspectable and
removable by its existing REST id.
Dashboard and channel Stop callers supply their existing state through the Stop
seams, including session lifecycle. A trusted channel reconciliation therefore
does not strand a goal retained under the earlier exact dashboard slot key.
Stop also follows that explicit link when the channel cannot arm new goals;
this does not widen goal creation eligibility. Legacy bare Slack timestamps use
the session layer's existing `canonical_key` shim.
Multiple owned goals do not prevent cancellation of the current response.
An approval stall requires an explicit human retry; Resume uses the existing
retry behavior without granting additional tool permissions. Spent cycle or
runtime budgets remain a backstop.
`needs_input` pauses while a required human answer is missing, `waiting` keeps
checking an existing external operation, and `blocked` records why work cannot
continue. A final assistant response alone never marks the goal complete.
Slack goal continuations use the directive-capable turn driver with the existing
tool gate and approval decider, so their progress and completion reach the same
consumer as an ordinary human turn.

## Same-session monitor contract

`monitor_start`, `monitor_update`, and `autonudge_stop` are session directives,
not direct AutoNudge mutations. `mcp_tools.control` validates the tool payload,
uses strict session-key resolution only as a context guard, and returns an
encoded directive. `dashboard.session_directive_apply.apply_session_directive`
applies that directive on the user-facing session. The split prevents a cron,
hook, or subagent from using inherited process identity to arm, rewrite, or
stop another session's unattended loop; `test_autonudge_stop_auth.py` pins the
binding-key-only targeting and the non-nudgeable-session refusals.

A directive reaches the consumer two ways. On kiro-cli the marker inside the
tool's own RESULT TEXT is decoded under the verified `_meta.kiro` identity. On any
backend that emits no such identity, the MCP stub has already parked the validated
payload on the gateway keyed by `session_directive.call_input_digest` of the raw
`tools/call` arguments, and the consumer claims it by the same digest computed
from the `tool_call` frame's `rawInput` — nothing is read out of the result body,
so a backend that re-serialises, duplicates, offloads or caps that body cannot lose
the directive. `test_session_directive_input_digest.py` drives the real consumer
with every KAS result shape observed so far, and
[agent-host-contract.md](agent-host-contract.md) §9 states what a provider must
declare about its `rawInput`. The consumer-side failure paths log at `warning`
(`session-directive NOT APPLIED`, `NO CALL INPUT`, `CLAIM MISS`, `DENIED`), which
is what makes this class of drop visible in `gateway.log` instead of silent.

`monitor_start` binds one loop to the calling session and is create-only. It
refuses when either automation kind already occupies the binding, preserving the
existing record and its evidence. `monitor_update` is the only way to revise or
re-arm the bound legacy loop. The binding-key and collision tests in
`test_autonudge_stop_auth.py` pin that behavior.

A retained stop is refused at the turn boundary, and the three tools say so
before the turn ends **whenever the retained record is readable**.
`mcp_tools.control._retained_stop_refusal` reads the same
`/api/autonudge/session-monitor` endpoint `monitor_inspect` reads and, when the
binding holds an inactive record whose outcome is retained evidence, returns a
refusal naming the retained outcome, its target, and the owner-only clear —
instead of an ack. This closes a false acknowledgement rather than adding a
capability: the tool answers the model over its own pipe DURING the turn while
`apply_session_directive` runs after the turn's result is processed, so the
authorizer's refusal and the `ARM_REFUSAL_NOTICE_PREFIX` transcript notice both
arrive after the model has ended its turn believing a monitor exists. The
preflight is read-only and fails OPEN — an unreachable gateway arms as before,
because a preflight that failed closed would let one bad read block all arming —
and the turn-boundary refusal remains the enforcement point. So the preflight is
an ADVISORY early answer, not a second gate: on an unreadable read, and in the
TOCTOU window where the record changes after the read, the in-turn answer and the
enforced outcome can still differ, and the turn boundary is what settles it. It
never clears or overwrites a record: clearing retained evidence stays the
owner-only dashboard action. It also runs **only in the MCP server**: a directive
tool's handler is re-run a second time inside the GATEWAY by
`mcp_core.derive_directive`, which discards the returned text, and that replay is
called synchronously on the gateway's own event loop — so the preflight's
blocking loopback read would ask the gateway for an answer only the loop already
waiting on it could give, stalling every co-hosted session until the timeout.
`mcp_core.directive_capture_active` is the seam the guard reads, and the skip
costs nothing: the preflight exists to reach the MODEL in the arming turn, which
only the MCP-side run can do.
`monitoring.models.retained_outcome_blocks_rearm` is
the single predicate shared with `autonudge._stopped_row_is_replaceable`, so what
cannot drift is the RULE itself — one outcome classification serves both sites,
rather than two copies diverging. The replaceable/retained split is pinned as
explicit data in `test_monitor_retained_stop_false_ack.py`, because a test that
merely compares the two callers of one predicate is tautological. That file also
covers all three tools, the fail-open paths, the system-imposed outcomes that
must still arm, and the endpoint wire contract the refusal depends on — dropping
`outcome` from `MONITOR_PUBLIC_FIELDS` would make the preflight fail open
silently.

`NudgeLoop.next_due_ts`, `notify_user_input`, and `notify_turn_complete` make
dashboard-loop cadence deadline-preserving: user activity cancels a pending
timer but does not move its deadline, and a delivered nudge begins its next
full interval when that nudge turn ends. This prevents active conversation
from postponing monitoring forever while avoiding a nudge racing a user turn;
`test_autonudge_deadline.py::test_user_turn_resumes_remaining_time_not_full_interval`
and `test_delivered_fire_clears_deadline_then_turn_end_starts_fresh` pin both
sides of the contract. Channel-bound loops re-arm after their unattended turn
in `AutoNudgeService._run_fire_cycle` because they do not use the dashboard
turn-lifecycle hooks.

The schemas in `validation.MONITOR_START_SCHEMA` and
`validation.MONITOR_UPDATE_SCHEMA` bound the message, interval, cycle cap, and
wall-clock budget. `mcp_tools.control.monitor_start` supplies bounded positive
defaults from `mcp_tools._limits`; zero and negative cycle or runtime limits are
rejected. The cap is a runaway backstop, not evidence that the watched work
completed: `AutoNudgeService._timer` deactivates a capped loop and emits
`expired`.

`AutoNudgeService.runtime_budget_exceeded` measures a configured wall-clock
budget from the persisted creation time. `_timer` checks it before a fire and
`_run_fire_cycle` checks it after a delivered turn, so a running turn is not
cancelled but an expired loop is not re-armed. `test_autonudge.py` pins budget
persistence across restart and the post-delivery check.

`monitor_update` resolves the loop only by the calling session's binding and
patches its message or limits through `authorize_and_update_nudge`. It does
not accept a loop identifier. `_monitor_update` refuses a new cap or budget
that cannot yield another fire and never revives a manual pause as a side
effect. It may re-arm a loop stopped by its own cycle cap or runtime budget
only when the relevant bound is raised; the paused-loop and bound-revival
tests in `test_autonudge_stop_auth.py` pin those distinctions.

`autonudge_stop` is deliberately non-confirming at tool-call time because the
consumer applies it after the turn result is processed. The applier removes an
ordinary monitor loop on the calling binding and reports an idempotent local
miss. It never exposes a cross-session target; `test_autonudge_stop_auth.py`
pins both the request wording and the local-binding behavior.

## PR fetcher

`probes.gh_pr` reads one pull request and makes no wake decision. Whether a tick
is worth the owning session's turn is the wake judge's answer, read against the
loop's own criteria; the one deterministic mapping -- a merged or closed pull
request ends the watch -- belongs to the auto-nudge core, which is the layer that
can act on it. `PrWatchProbe.observe` therefore returns NO observations. The
reading is published on the probe instance and the driver reads it there.

`_parse_config` accepts a JSON message naming one repository, one pull request,
and optionally the one pinnable host. A message that can never be valid raises
`ValueError`, which the driver converts to a removed watch rather than a retried
tick. Keys this build does not read are ignored, so a watch armed by an earlier
build keeps working. `test_gh_pr_fetch.py` pins both rules.

Every call goes through `_Transport`, one object per tick, which owns:

* `github_runner.resolve_gh` and `github_runner.run_gh` -- the repo's single gh
  spawn chokepoint: the validated absolute path, the restricted GitHub
  environment, an SEL audit record per spawn, and the pinned host that stops an
  ambient `GH_HOST` re-pointing a bare `owner/name` slug at another server.
* A per-call timeout under a whole-tick budget, so a paginated read cannot spend
  the product of the two.
* Bounded retry with exponential backoff and full jitter. Jitter matters because
  several loops on one host tick on the same cadence. A refusal that names itself
  and is not transient is answered once rather than retried.
* Rate limits read off the response headers (`gh api --include`), so a call backs
  off on a nearly-spent window instead of discovering the floor by being refused.
  `retry-after` wins over the reset epoch, and every wait is capped.

The reading itself:

* Check runs are paginated against the API's own `total_count`. That count is why
  this reads the check-runs endpoint rather than the rollup served beside the pull
  request: the rollup is a bare array, so a truncated read of it is undetectable.
  Commit statuses are a separate sequence read the same way and by the same code,
  because a required gate can be published as one and appears on no check-runs page
  -- and a first-page-only read of them omits exactly the rows most likely to be
  gating, with nothing else on the reading saying so. The two share one function on
  purpose: two copies of a counted read is how one of them ends up without the count
  check, and that one is a failing gate missing from a board reporting itself whole.
* `_collapse` folds duplicate rows to one per identity, newest by start time, and
  reports how many raw rows it folded. An unknown conclusion is reported as
  `unknown`; a cancelled or stale row as `superseded`.
* The identity a row folds under is its WORKFLOW, not the app that posted it. Every
  GitHub Actions row carries one app slug, so the slug cannot separate two workflow
  files that each define a job of the same name, and folding those lets one
  workflow's green stand in for the other's failure on a board still reporting
  itself whole. So an Actions row is qualified by its workflow and by nothing when
  that is unknown: the slug would be a false qualifier, reading as "one lane" on
  exactly the rows it cannot tell apart.
* The workflow is resolved only where a check name is SHARED between two runs, which
  is the only case that needs it -- either two workflow files that must stay two
  rows, or two runs of one workflow that must fold, and nothing else on the row says
  which. An unshared name needs no qualifier, so the ordinary board resolves nothing
  and spends no call: measured at 40 rows across 19 runs with no name shared, where
  resolving every run would cost 19 calls a tick to separate nothing. Where a shared
  name cannot be resolved, recency stops meaning supersession for that identity: the
  conservative bucket is kept and the reading reports itself `partial` with a note
  naming the unresolved identity. A redundant look costs a turn, a dropped gate
  costs the merge.
* A duplicate that cannot be ordered by time keeps the more conservative bucket.
  Saying so is the READING's job rather than the row's: `partial` and its note are
  what a consumer reads, and a per-row flag none of them opens would be a field
  the reading cannot back.
* Comments and reviews are carried WITH their bodies, clipped per item and in
  total, newest first, inside a fetch horizon. The bot's own comments are skipped,
  because otherwise the watch is a feedback loop. A remark whose timestamp cannot
  be read is left out: an age of unknown freshness would be carried every tick.
* One `status` per reading: `ok` when every page was read, `partial` when something
  was read and something was not, `unavailable` when the subject was not reached.
  A refusal becomes a status, never an exception. A consumer treats `partial` as a
  target nobody read whole, which fires.
* `as_facts` is the durable half -- typed facts plus who said something and when.
  `bodies` is a separate call, so keeping the first cannot accidentally keep the
  second: remark prose stays in the process that fetched it. Each remark does carry a
  short digest of its body, because the prose is gone after the tick and a record
  saying only that a remark existed leaves a WRONG quiet unexaminable -- the digest
  identifies what was screened without storing it. Every key in the durable half has
  a reader; the transport's own counters are not there, since they describe the fetch
  rather than the subject and this record is written every tick into the budget the
  judge's evidence must fit inside.
* A check run and a commit status are separate sequences in the forge's own model, so
  the fold identity carries which family a row came from and the two never merge. The
  pair that would otherwise merge is ordinary output: a status with no target URL
  carries no qualifier, and neither does a check run whose name needed no resolution.

The kernel is still in the path for what a stateless reading cannot hold: the
epoch, so its dedupe memory resets on a new head, and the consecutive-failure
backstop, which turns a run of unreadable ticks into one report that the watch is
blind.

## Watch kernel invariants

`irq.state_path` includes the subject identity and cron job identifier. Two
jobs watching one PR therefore do not suppress each other's alerts. `load_state`
treats missing or malformed state as fresh and `save_state` uses `atomic_write`;
the degradation is a possible duplicate wake, not a crash-loop. If persistence
fails while a coalescing window is open, `irq.run` reports immediately with a
warning rather than delaying an observation into state it cannot recover.

A `Tick.epoch` changes when the PR head changes. `irq.run` clears
`REVISION` dedupe and coalescing state on that change, so failures on the new
head can wake again. Conversation observations set `resets_on=ResetsOn.NEVER`, so
a force-push does not replay an already-seen comment or review. These distinct
key spaces are load-bearing: treating every signal as head-scoped loses
conversation dedupe, while treating every signal as sticky hides failures on a
new head.

`irq.run` coalesces ordinary wake observations until the configured floor has
elapsed and the check rollup settles, or until its hard wall elapses. The hard
wall ensures a permanently pending check delays a wake instead of losing it.
Sticky conversation observations can fire once the floor elapses even while
checks remain pending; they do not become more informative by waiting for CI.
`Severity.IMMEDIATE` and `Severity.TERMINAL` bypass the ordinary window. The
coalescing and sticky-observation tests in `test_irq.py` pin these cases.

Dedupe is time-bounded. The kernel re-alerts a persistent condition after its
window because a script cannot observe whether gateway delivery succeeded;
permanent acknowledgement could turn one lost delivery into permanent silence.
The fetcher emits no observations, so nothing of its own is deduped here; the
kernel's dedupe serves the reports it raises itself. `test_irq.py` pins those.

`Tick(fetch_ok=False)` increments the kernel-owned error streak. A persistent
failure reports that the watch is blind; a successful fetch clears the streak
and its blind marker. If state cannot be written, the kernel reports on the
first failed tick because a counted threshold would otherwise be unreachable.
The watch-health tests in `test_irq.py` pin recovery, re-alerting, and the
unwritable-state path; `test_gh_pr_fetch.py` pins that an unreadable reading is
what reaches the kernel as a failed tick.

## Delivery and lifecycle

Script cron execution maps `Skip` to no delivery, `Report` to a result while
keeping the job, and `Done` to a result whose successful delivery removes the
job. The script-cron branch in `slack.gateway._init_cron` delivers a result to
the originating dashboard slot, queues it if that slot is busy, and rehydrates
a closed slot from history when possible. If no slot is available, it sends a
notification instead. This makes the arming session the normal wake target
without claiming that headless delivery can start a session.

The bundled script is a source asset, not a gateway import. Existing jobs must
still resolve a registered copy under the configured cron directory through
`cron_script.resolve_script_path`. The cron gateway revalidates and scans that
current script body at fire time, then executes it through the script sandbox.
The babysit skill no longer registers new script jobs.

## Non-goals

The structured GitHub monitor digests PR-level comment bodies to detect that
one changed, so an in-place edit whose `created_at` never moves still wakes the
owner. It never interprets what a comment says or decides whether an advisory
finding is valid, and it deliberately does not read inline review-thread bodies
at all -- it reports only the count of unresolved, non-outdated threads there. It
reports typed provider facts and leaves judgment, source inspection, and any
reply to the reactivated babysit session.
`monitor_start` remains appropriate when each delivered cycle requires the agent
to make progress, the objective requires untyped evidence, or the watched subject
is unsupported by a structured provider.

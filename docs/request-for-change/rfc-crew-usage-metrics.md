---
title: Crew usage metrics, wave 2 -- conductor use, session-tree shape, session control, monitors, the work ledger, event sources and crew log health
status: partial
author: chenmingwei23
created: 2026-10-19
last-audited: 2026-10-21
audited-at: ba5a6fc614
doc-pr: null
implementation-prs: [21342, 21308, 22217, 22219, 22220, 22223, 22226, 22227]
tracking-issues: [21270]
supersedes: []
superseded-by: []
---

# RFC: crew usage metrics, wave 2

- Status: partial, not yet accepted. Code facts are measured at
  `ba5a6fc614`. On main: section 5 (#21342 `4f16fd2ffc`, #21308
  `024ff158fb`), 6.1 (#22220 `3f63700f54`), 6.2 (#22217 `9800507e19`), 6.3
  (#22219 `beacdbcba9`), the three 6.4 metrics (#22223 `31880e6938`), the 6.5
  first batch (#22226 `2d2a08a8c9`) and four of the seven 6.5 second-batch
  instruments (#22227 `e283043821`). Still open: the 6.4 `actor` change
  (`ACTOR_VALUES` has no `webhook` or `heartbeat`) and `append.bytes`,
  `append.batch` and `fold.lag` from the 6.5 second batch, which #22227 left
  out because each needs work on the append path.
- Author: chenmingwei23
- Tracking issue: [#21270](https://github.com/kirodotdev/KiroCrew/issues/21270)
- Builds on: [rfc-crew-log-telemetry-export.md](https://github.com/kirodotdev/KiroCrew/pull/21288)
  (the counting-fold export model and its first metrics, PRs
  [#21342](https://github.com/kirodotdev/KiroCrew/pull/21342) and
  [#21308](https://github.com/kirodotdev/KiroCrew/pull/21308)).
- Governing RFC for consent and egress: #21288, sections 4.6, 8 and 9.
  `telemetry.enabled` stays off by default, and nothing leaves the machine
  unless the operator points `otlp_endpoint` at a collector. This RFC adds no
  outbound path and changes neither rule.
- Not a dependency: [#22181](https://github.com/kirodotdev/KiroCrew/pull/22181)
  is a separate draft proposal, under review, about sending product metrics to
  a Kiro endpoint. Nothing here assumes it is accepted.

## 1. Summary

This RFC adds about twenty counters and a few gauges so that we can answer how
people use the crew: how much the conductor is used, how wide and deep session
trees get, how agents drive each other through session control, what monitor
loops actually do, how work ledger goals and items end, which event sources wake
sessions, and whether the crew log itself is healthy.

Every new metric is a counter or a gauge. Durations and sizes are recorded as a
counter with a small closed `bucket` attribute instead of a histogram, so the
distribution survives backends that do not keep explicit histogram buckets.
Every attribute value comes from a closed enum. No metric carries a session id,
an item id, a name, a path, a URL or any user text.

## 2. Motivation

The first export round (#21342, #21308) counts sessions and turns. It cannot yet
answer the questions we are asked most:

| Question | Before wave 2 |
|---|---|
| How many turns run inside conductor sessions vs worker sessions? | Not answerable: `crew.turn.started` is split by who started the turn, not by the kind of session |
| How many children does a parent session open? | Not answerable |
| How deep do trees get? | Answered by `kirocrew.crew.session.tree.depth`, already a counter with a closed `depth` bucket in #21308 (#21288 section 5); no histogram is involved |
| How often do agents use session control, and which operations? | Not answerable: `tool.call.duration` groups by tool kind, and session control calls fall into `other` |
| Do monitor loops do useful work, or tick quietly? | Not answerable: only the live loop count exists |
| How do work ledger items end, and how long do they take? | Not answerable: no ledger metric exists |
| Which event sources wake sessions? | Partly: webhook wakes land in `other` |
| Does the crew log drop data, fold slowly, or rebuild from scratch? | Not answerable: no crew log metric exists |

## 3. Goals and non-goals

Goals:

- Answer the questions in section 2 with counters and gauges only.
- No effect on the person using Crew: recording a metric is an in-memory add,
  never a disk write, a network call or a log line on the turn path.
- Keep the append path of the crew log untouched in the first batch.
- Keep every attribute a closed enum with a stated value list.

Non-goals:

- Egress: where the numbers are sent and which ones leave the machine. This
  RFC adds no outbound path. Egress stays governed by #21288 sections 4.6, 8
  and 9:
  opt-in through `telemetry.enabled`, and no default endpoint.
- Counting machines or people. No metric here carries a machine, install,
  account or person id.
- "Stale" work (a worker that has not reported for hours). Deferred to a later
  round.
- Exact latencies per event. Those need spans and are out of scope here.

## 4. Rules every metric in this RFC follows

1. Counter or gauge only. No new histogram.
2. A duration or size is a counter with a `bucket` attribute. Each metric states
   its bucket edges.
3. Attribute values are closed enums. Unknown values map to `other`.
4. Counters are exported with DELTA temporality, as every counter is today
   (`src/kiro_crew/metrics/temporality.py`).
5. "How many right now" is a gauge read from live state, never a counter that is
   incremented and decremented. A crash loses the decrement; a gauge does not
   drift.
6. Sources, in order of preference: a small counting fold over crew log entries
   (the model of the base RFC, numbers-only state, never over-count except for
   the one bounded over-count on a backward clock step across a restart that
   #21288 section 4.7 accepts); otherwise the single commit point of the
   feature, after the write succeeds.
7. Everything stays behind `telemetry.enabled` and the existing attribute
   sanitiser.
8. Failure isolation. Many wave-2 sources are commit points in request handlers
   and service code (the session control handlers, `WebhookRunStore.record`,
   the work ledger record handler, autonudge firing and its stop commit), not
   bus subscribers, so the bus's own subscriber-error isolation does not cover
   them. Every record call at a commit point is wrapped so that any exception
   it raises is caught and dropped, with at most a debug log on that failure
   path. A failure while recording never changes the outcome of the call it
   measures: the same return value, HTTP status, raised error and written
   state as with telemetry off. Recording runs after the write commits, so it
   cannot undo or block that write. Each area's tests pin this by making the
   recorder raise and asserting the measured call's result is unchanged.

## 5. Changes to the first-round metrics

These landed in #21342 (merged, `4f16fd2ffc`) and #21308 (merged,
`024ff158fb`).

| Metric | Change | Why |
|---|---|---|
| `kirocrew.crew.turn.started` | Attributes become `{actor, agent_kind}` | Count turns inside conductor, worker, team-lead and crewmate sessions. At most 8 x 16 = 128 series, and 10 x 16 = 160 after the `actor` change in 6.4 |
| `kirocrew.crew.session.tree.depth` | No change: already a counter with attribute `depth`: `0` / `1` / `2` / `3+`, as in #21288 section 5 and #21308 | Keep the depth distribution on any backend |
| `kirocrew.crew.session.tree.child` | New counter, attributes `parent_kind` (agent_kind enum) and `nth`: `1` / `2` / `3` / `4-7` / `8+` | Tree width, see below |

Tree width: each child session adds one to `tree.child` with `nth` set to its
ordinal among its creator's children. Then:

- each child adds one at its own ordinal's label, so a parent with three
  children adds one each at `nth=1`, `2` and `3`
- parents with at least k children = the count at `nth=k`, for k = 1, 2 or 3
- the grouped `4-7` and `8+` labels count children, not parents, so they give
  no exact "at least k" parent count for k above 3

The source of `nth` is the one #21288 section 5 defines: a counting fold per
creator over the `session/spawned` entries on the creator's own log, one add
per child. It keeps a count of children, never a list of parent or child ids.

This gives the width distribution without waiting for a parent to end. A crash
cannot over-count it. The one exception is the bounded over-count on a
backward clock step across a restart that #21288 section 4.7 accepts, which
also covers these child adds. `parent_kind` lets us read "children per
conductor" on its own.

The conductor-to-worker ratio needs no new metric: it is
`session.started{agent_kind=conductor}` against
`session.started{agent_kind=worker}`.

## 6. New metrics

### 6.1 Session control

Session control is a set of MCP tools agents use to drive other sessions
(`session_create`, `session_send`, `session_fork`, `session_stop`, ...). People
use the dashboard instead, so every caller is an agent.

| Metric | Type | Attributes | Source |
|---|---|---|---|
| `kirocrew.crew.session_control.calls` | counter | `op`: the 18 tool verbs of `SESSION_CONTROL_OPS` (`create`, `send`, `fork`, `stop`, `close`, `continue`, `broadcast`, `status`, `read_message`, `summary`, `retry`, `reload`, `rename`, `set_model`, `end_wait`, `adopt`, `release`, `generate_title`) plus `other`, 19 values; `caller`: agent_kind enum; `outcome`: `ok` / `error` | Counting fold over the caller's tool-call entries, or the `/api/session-control/*` handlers after the call completes |

`other` is reachable: the `/revive` and `/history` routes have no verb in the
list, so their calls report `other`.

At most 19 x 16 x 2 = 608 series (`caller` is the 16-value agent_kind enum in
6.6). A backend that should not carry all of them can drop `outcome` from its
dimensions, which leaves 304.

### 6.2 Monitor loops (autonudge)

Before #22217 (merged, `9800507e19`) only
`kirocrew.inventory.monitor_loops.active` existed. The four counters below
now ship on main.

| Metric | Type | Attributes | Source |
|---|---|---|---|
| `kirocrew.autonudge.loops.started` | counter | `kind`: `loop` / `monitor` | The loop-creation mutation in `autonudge_service/` (no crew log entry exists for it) |
| `kirocrew.autonudge.ticks` | counter | `kind`; `outcome`: `delivered` / `quiet` / `refused` / `failed` / `other` | `autonudge_service/firing.py`, one point per tick decision |
| `kirocrew.autonudge.loops.stopped` | counter | `kind`; `reason`: 28 closed stop reasons plus `other`, mapped from the stored text as listed in 6.6; `ran`: `lt_1h` / `1h_24h` / `gt_24h` / `other` | The single commit point in `autonudge_stop_log.py` |
| `kirocrew.monitor.decisions` | counter | `decision`: `no_change` / `record_only` / `wake_actionable` / `stop_success` / `stop_blocked` / `retry_provider` / `stop_budget` / `other`; `kind`: the registered monitor kinds plus `other`, listed in 6.6 | `monitoring/decision.py::decide_monitor` |

The ratio of `quiet` to `delivered` ticks is the first number to read: it tells
whether loops idle or work.

### 6.3 Work ledger

Before #22219 (merged, `beacdbcba9`) there was no ledger metric. The four
counters below now ship on main. Every change is already a `work/recorded`,
`crew/dispatch` or `crew/report` entry.

| Metric | Type | Attributes | Source |
|---|---|---|---|
| `kirocrew.work.created` | counter | `kind`: `goal` / `item`; `depth`: `0` / `1` / `2+` | Ledger record handler, after the write commits |
| `kirocrew.work.item_closed` | counter | `state`: `accepted` / `rejected` / `abandoned`; `fails`: `0` / `1` / `2` / `3+`; `duration`: `le_10m` / `le_1h` / `le_4h` / `gt_4h` | Same handler, on `action=close` |
| `kirocrew.work.dispatches` | counter | `target_kind`: `session` / `crew` | After the `crew/dispatch` write |
| `kirocrew.work.reports` | counter | `status`: `progress` / `done` / `blocked` / `question` / `failed`; `reason`: `approval` / `needs_human` / `none` | After the `crew/report` write |

Why the commit point and not a counting fold, which rule 6 prefers. The
existing `work` fold's limit on closed items is not the reason: a new counting
fold would keep no item records, so that limit would not apply to it. The
reason is the labels. `depth` on `work.created`, and `fails` and `duration` on
`work.item_closed`, need facts about one item (its parent, its failure count,
its creation time). A numbers-only fold could only supply them by holding a
record per item id until the item closes, which rule 6 and #21288 section 4.4
forbid. The record handler has the item record in hand when its write commits,
so it reads those facts there and keeps nothing. `dispatches` and `reports`
need no per-item fact and could come from a counting fold; they stay at the
same handler so all four ledger counters share one source and one restart
behaviour (a commit-point counter never re-counts on restart, so it needs no
restart baseline).

### 6.4 Event sources

| Metric | Type | Attributes | Source |
|---|---|---|---|
| `kirocrew.webhook.requests` | counter | `outcome`, the 11 values of `webhooks.METRIC_OUTCOMES`: the recorded-run outcomes `completed` / `degraded` / `timeout` / `error` / `rejected_capacity` / `unauthorized` / `disabled`; the refusals no run records `throttled` / `unavailable` / `rejected_body`; and `other` | `WebhookRunStore.record` in `webhooks.py` for recorded runs, and the refusing branches of the hooks handler for requests turned away before a run exists |
| `kirocrew.subagent.completed` | counter | `outcome`: `completed` / `failed` / `stopped` / `unknown` | The sub-agent terminal path; matches crew log `subagent/completed` and `subagent/failed` |
| `kirocrew.inventory.webhooks.registered` | gauge | none | Entry count of the registered hooks file, read like the other inventory gauges |

Plus one vocabulary change: the closed `actor` enum of `turn/started` gains
`webhook` and `heartbeat`. Today those wakes fall into `other`, so
`crew.turn.started{actor}` cannot separate them.

### 6.5 Crew log health

Before #22226 (merged, `2d2a08a8c9`) and #22227 (merged, `e283043821`) main
had no crew log metric, only the in-process `WriterStats` and warning logs.
Both batches below now ship on main except `append.bytes`, `append.batch` and
`fold.lag`.

First batch (none of these touch the append thread):

| Metric | Type | Attributes | Where | Cost |
|---|---|---|---|---|
| `kirocrew.crew_log.write.lost` | counter (merged as an observable gauge, see below) | `cause`: `dropped` / `overflowed` / `wake_dropped` / `other` | Writer drop and overflow paths, eager queue drop | Failure path only |
| `kirocrew.crew_log.fold.apply` | counter | `scope`: `slot` / `session`; `fold`: the closed fold catalogue (`FOLD_NAMES`, checked at import); `bucket`: `lt_10ms` / `lt_100ms` / `lt_1s` / `ge_1s` | Around `_advance` / `_advance_session` in the eager worker | Once per fold per batch, worker thread |
| `kirocrew.crew_log.fold.start` | counter | `scope`; `origin`: `warm` / `savepoint` / `cold` / `other` | The three start branches of a fold | Once per fold start |

Recorded deviation: #22226 (merged, `2d2a08a8c9`) ships
`kirocrew.crew_log.write.lost` as an observable gauge, not the counter in the
table above. Its reading is a process-lifetime total, kept in memory and reset
on restart, and the loss rate is the difference between two readings. The
reason is the project rule that no instrument exports a CUMULATIVE series,
pinned by `test_no_instrument_exports_cumulative`: the instrument reads totals
that other modules own, so it must be asynchronous, and an observable counter
would export CUMULATIVE. Which type stands is left to the maintainer ruling on
this RFC.

Second batch:

| Metric | Type | Attributes | Where | Cost |
|---|---|---|---|---|
| `kirocrew.crew_log.savepoint.write` | counter | `kind`: `session` / `slot`; `outcome`: `written` / `failed`; `bucket` | `checkpoint.save` / `save_slot` | About once per 256 entries |
| `kirocrew.crew_log.append.bytes` | counter | none | After a batch write | Once per batch |
| `kirocrew.crew_log.append.batch` | counter | `bucket` (write + fsync time) | Batch write | Once per batch |
| `kirocrew.crew_log.eager.queue_depth` | gauge | none | Observable callback reads the queue size | Reader thread only |
| `kirocrew.crew_log.writer.buffered` | gauge | none | Observable callback reads `WriterStats` | Reader thread only |
| `kirocrew.crew_log.bus.subscriber_error` | counter | `kind`: `fold_advanced` / `tree_advanced` | Bus exception branch | Failure path only |
| `kirocrew.crew_log.fold.lag` | counter | `bucket` | Enqueue time on the wake, oldest wake per batch | One `time.monotonic()` on the append thread |

`fold.lag` is the only proposal that adds work to the append thread, which is
why it waits for the second batch and a measurement. The bus has no queue (an
event with no subscriber is dropped by design), so there is no backlog to
measure, only subscriber errors. Per-session file size is not a metric because
session ids are unbounded.

### 6.6 Closed value lists

Every enum this RFC uses that is not spelled out in its own table is listed
here, with how raw text becomes a closed value. The code builds each set from
the constants that produce its values (#22217 `9800507e19`, #22220
`3f63700f54`, #22223 `31880e6938`, #22226 `2d2a08a8c9` and #21342
`4f16fd2ffc`, all merged), and a test pins each list.

**agent_kind** (`caller` in 6.1, `agent_kind` and `parent_kind` in 5), 16
values from #21342: the 13 agent-file stems `conductor`, `dashboard_author`,
`dashboard_manager`, `guest`, `heartbeat`, `knowledge`, `ledger_conductor`,
`lite`, `pipeline_conductor`, `research`, `security_conductor`, `team_lead`,
`worker`, plus `default`, `crewmate` and `other`. An operator-written agent name
is mapped to this set when the session opens and is never kept.

**actor** on `crew.turn.started`, 8 values today: `app`, `autonudge`, `crew`,
`cron`, `gateway`, `subagent`, `user`, `other`. Section 6.4 adds `webhook` and
`heartbeat`, for 10.

**reason** on `autonudge.loops.stopped`. On main the stored reason is free
text: the stop record clips it to 200 characters (`autonudge_stop_log.py`
`safe_text`), and a structured monitor's stop reason may be up to 500 characters
(`monitoring/models.py`, `MAX_MONITOR_STOP_REASON_CHARS`). The counter never
reports that text. It strips surrounding whitespace and looks the string up in
a fixed table. An exact match reports the table's value. A reason with no
table entry, including composed reasons such as `campaign_<status>` and any
free text, falls back to the row's terminal `MonitorOutcome` value when the
monitor row still records one (`metrics/monitor_loops.py`), so a monitor that
stopped on `pull_request_merged` reports `success`. Only a reason with neither
reports `other`.

| Stored reason | Reported as |
|---|---|
| `cycle_cap`, `stop_sentinel`, `monitor_terminal`, `autonudge_stop`, `session_start_failures`, `consecutive_failures`, `structural_terminal`, `model_refused`, `sentinel_dropped`, `manual` (legacy loop reasons, `autonudge_service/model.py`) | itself |
| `agent_turn_budget`, `token_budget`, `provider_error_budget`, `verdict_stall`, `invalid_monitor_record`, `unsupported_monitor_version`, `completion_evidence_unavailable`, `session_unavailable`, `user_stop`, `session_close` (structured monitor stop reasons, `monitoring/models.py`) | itself |
| `success`, `blocked`, `budget`, `target_unavailable` (the terminal `MonitorOutcome` values; also what any unmatched reason reports when the monitor row records one of them; `user_stop` and `session_close` are listed above) | itself |
| `removed`, `unspecified` (defaults `autonudge_stop_log.py` supplies for a row that vanished or stopped with no reason) | itself |
| `runtime_budget` (both the legacy and the monitor spelling) | itself |
| `approval_stalled` (legacy), `approval_stall` (monitor) | `approval_stall` |
| anything else | `other` |

That is 28 reported values plus `other`, 29 in all.

**kind** on `monitor.decisions`: the kinds registered in
`monitoring/registry.py`, which are `gh-pr`, `github_pull_request`,
`github_workflow_run`, `gitlab_merge_request`, `bitbucket_pull_request` and
`azure_devops_pull_request`. A kind with no registry entry (for example a store
row written by a newer version) reports `other`, so 7 values. `decision` is the
7 members of `MonitorDecision` plus `other`.

**kind** on the three `autonudge.*` counters: `loop`, `monitor`, plus `other`
for a stop record whose kind is missing.

**fold** on `crew_log.fold.apply`: the 18 names of `FOLD_NAMES` in
`crew_log/projection_catalogue.py`, which are `status`, `usage`, `timeline`,
`tools`, `approvals`, `subagents`, `class`, `outline`, `otel_usage`,
`otel_session_tree`, `ledger`, `radar`,
`work`, `panel`, `agentic`, `mistakes`, `workstreams` and `worktree`. The label
is read from that catalogue at import, so there is no second copy to drift; a
name outside it reports `other`, which only a caller bug can reach. `scope` is
`slot` / `session` plus `other`.

### 6.7 Series bounds

The upper bound for each attributed counter is the product of its value counts,
`other` included.

| Metric | Bound |
|---|---|
| `crew.turn.started` | 8 x 16 = 128; 10 x 16 = 160 after the `actor` change |
| `crew.session.tree.child` | 16 x 5 = 80 |
| `crew.session_control.calls` | 19 x 16 x 2 = 608 (304 without `outcome`) |
| `webhook.requests` | 11 |
| `subagent.completed` | 4 |
| `autonudge.loops.stopped` | 3 x 29 x 4 = 348 |
| `monitor.decisions` | 8 x 7 = 56 |
| `crew_log.fold.apply` | 3 x 19 x 4 = 228. Each fold has one scope, so a correct install reaches at most 18 x 4 = 72 |

## 7. Phases

1. Section 5 changes, in #21342 (merged, `4f16fd2ffc`) and #21308 (merged,
   `024ff158fb`).
2. Session control (6.1) and crew log health first batch (6.5).
3. Monitors (6.2), work ledger (6.3), event sources (6.4) and the `actor`
   vocabulary change.
4. Crew log health second batch, after measuring `fold.lag` cost on a busy
   install.

Each phase is one PR per area with tests that pin the attribute value lists.

Two things wave 2 takes from #21288. First, it relies on question 1 of #21288
section 11 (approval for a data category or for a named list of metrics). Wave 2
reads the answer as a category; if the answer is a named list, each phase above
returns for review, and section 6.6 is the list it brings. Second, section 6.3
pulls #21288's S4 phase (work ledger: nesting, item states and how long items
take) into this round and scopes it here.

## 8. Backward compatibility

- `crew.session.tree.depth` keeps the counter type #21288 and #21308 already
  give it; nothing here changes its type.
- Adding `agent_kind` to `crew.turn.started` adds series; it removes none.
- Adding `webhook` and `heartbeat` to the `actor` enum moves some turns out of
  `other`. Dashboards that read `other` will see it shrink.

## 9. Security and privacy considerations

- No attribute carries an id, a name, a path, a URL or user text. Each enum is
  listed in this document and pinned by a test.
- Monitor stop reasons and webhook outcomes contain free text in local logs;
  only the mapped closed value is used, never the text.
- All metrics stay behind `telemetry.enabled`, which stays off by default. This
  RFC adds no outbound path: the numbers reach only the OTLP collector an
  operator configures, under #21288 sections 4.6, 8 and 9, and none by
  default.
- A failure while recording never changes the outcome of the measured call
  (section 4, rule 8).

## 10. Alternatives considered

- Histograms for durations and depth. Rejected: their buckets do not survive
  every backend, and the bucket sets we need are small.
- An up/down counter for "loops running" or "trees open". Rejected: a crash
  loses the decrement and the number drifts upward. Gauges read live state.
- Counting ledger outcomes in a counting fold. Rejected for the reason in 6.3:
  the `depth`, `fails` and `duration` labels need facts about one item, which a
  numbers-only fold could supply only by holding a record per item id, and
  #21288 section 4.4 forbids that. The record handler reads them from the item
  record when its write commits.
- One generic "feature used" counter with a `feature` attribute. Rejected: each
  area needs its own outcome enum, and one shared counter would mix them.

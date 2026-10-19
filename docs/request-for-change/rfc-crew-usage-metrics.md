---
title: Crew usage metrics, wave 2 -- conductor use, session-tree shape, session control, monitors, the work ledger, event sources and crew log health
status: draft
author: chenmingwei23
created: 2026-10-19
last-audited: 2026-10-19
audited-at: afed43d2c0
doc-pr: null
implementation-prs: []
tracking-issues: [21270]
supersedes: []
superseded-by: []
---

# RFC: crew usage metrics, wave 2

- Status: draft. Nothing in this document is built. Code facts are measured at
  `afed43d2c0`.
- Author: chenmingwei23
- Tracking issue: [#21270](https://github.com/kirodotdev/KiroCrew/issues/21270)
- Builds on: [rfc-crew-log-telemetry-export.md](https://github.com/kirodotdev/KiroCrew/pull/21288)
  (the counting-fold export model and its first metrics, PRs
  [#21342](https://github.com/kirodotdev/KiroCrew/pull/21342) and
  [#21308](https://github.com/kirodotdev/KiroCrew/pull/21308)).
- Related: [rfc-telemetry-export-kiro-endpoint.md](https://github.com/kirodotdev/KiroCrew/blob/docs/21270-telemetry-export-endpoint-rfc/docs/request-for-change/rfc-telemetry-export-kiro-endpoint.md)
  (where these numbers are sent, and which of them leave the machine).

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

| Question | Today |
|---|---|
| How many turns run inside conductor sessions vs worker sessions? | Not answerable: `crew.turn.started` is split by who started the turn, not by the kind of session |
| How many children does a parent session open? | Not answerable |
| How deep do trees get? | A histogram whose buckets may not survive the backend |
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

- Where the numbers are sent and which ones leave the machine. That is
  [rfc-telemetry-export-kiro-endpoint.md](https://github.com/kirodotdev/KiroCrew/blob/docs/21270-telemetry-export-endpoint-rfc/docs/request-for-change/rfc-telemetry-export-kiro-endpoint.md).
- Counting machines. No metric here carries a machine or install id.
- Per-person counts. People are counted by the endpoint from the Kiro sign-in
  (see the export RFC), never by a metric attribute.
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
   (the model of the base RFC, numbers-only state, never over-count); otherwise
   the single commit point of the feature, after the write succeeds.
7. Everything stays behind `telemetry.enabled` and the existing attribute
   sanitiser.

## 5. Changes to the first-round metrics

These land in the open PRs #21342 and #21308.

| Metric | Change | Why |
|---|---|---|
| `kirocrew.crew.turn.started` | Attributes become `{actor, agent_kind}` | Count turns inside conductor, worker, team-lead and crewmate sessions. About 8 x 6 = 48 series at most |
| `kirocrew.crew.session.tree.depth` | Histogram -> counter, attribute `depth`: `0` / `1` / `2` / `3+` | Keep the depth distribution on any backend |
| `kirocrew.crew.session.tree.child` | New counter, attributes `parent_kind` (agent_kind enum) and `nth`: `1` / `2` / `3` / `4-7` / `8+` | Tree width, see below |

Tree width: each child session adds one to `tree.child` with `nth` set to its
ordinal among its creator's children. Then:

- parents with at least one child = the count at `nth=1`
- parents with at least k children = the sum over `nth >= k`

This gives the width distribution without waiting for a parent to end, and a
crash cannot over-count it. `parent_kind` lets us read "children per conductor"
on its own.

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
| `kirocrew.crew.session_control.calls` | counter | `op`: the closed tool list (`create`, `send`, `fork`, `stop`, `close`, `continue`, `broadcast`, `status`, `read_message`, `summary`, `retry`, `reload`, `rename`, `set_model`, `end_wait`, `adopt`, `release`, `generate_title`); `caller`: agent_kind enum; `outcome`: `ok` / `error` | Counting fold over the caller's tool-call entries, or the `/api/session-control/*` handlers after the call completes |

At most 18 x 6 x 2 = 216 series. A backend that should not carry all of them can
drop `outcome` from its dimensions.

### 6.2 Monitor loops (autonudge)

Today only `kirocrew.inventory.monitor_loops.active` exists.

| Metric | Type | Attributes | Source |
|---|---|---|---|
| `kirocrew.autonudge.loops.started` | counter | `kind`: `loop` / `monitor` | The loop-creation mutation in `autonudge_service/` (no crew log entry exists for it) |
| `kirocrew.autonudge.ticks` | counter | `kind`; `outcome`: `delivered` / `quiet` / `refused` / `failed` | `autonudge_service/firing.py`, one point per tick decision |
| `kirocrew.autonudge.loops.stopped` | counter | `kind`; `reason`: the fixed stop reasons plus `cycle_cap` / `runtime_cap` / `other`; `ran`: `lt_1h` / `1h_24h` / `gt_24h` | The single commit point in `autonudge_stop_log.py` |
| `kirocrew.monitor.decisions` | counter | `decision`: `no_change` / `record_only` / `wake_actionable` / `stop_success` / `stop_blocked` / `retry_provider` / `stop_budget`; `kind`: the closed monitor kinds | `monitoring/decision.py::decide_monitor` |

The ratio of `quiet` to `delivered` ticks is the first number to read: it tells
whether loops idle or work.

### 6.3 Work ledger

There is no ledger metric today. Every change is already a `work/recorded`,
`crew/dispatch` or `crew/report` entry.

| Metric | Type | Attributes | Source |
|---|---|---|---|
| `kirocrew.work.created` | counter | `kind`: `goal` / `item`; `depth`: `0` / `1` / `2+` | Ledger record handler, after the write commits |
| `kirocrew.work.item_closed` | counter | `state`: `accepted` / `rejected` / `abandoned`; `fails`: `0` / `1` / `2` / `3+`; `duration`: `le_10m` / `le_1h` / `le_4h` / `gt_4h` | Same handler, on `action=close` |
| `kirocrew.work.dispatches` | counter | `target_kind`: `session` / `crew` | After the `crew/dispatch` write |
| `kirocrew.work.reports` | counter | `status`: `progress` / `done` / `blocked` / `question` / `failed`; `reason`: `approval` / `needs_human` / `none` | After the `crew/report` write |

These count at the commit point rather than in a fold because the `work` fold
keeps only the last 32 closed items; older items survive only as a total.

### 6.4 Event sources

| Metric | Type | Attributes | Source |
|---|---|---|---|
| `kirocrew.webhook.requests` | counter | `outcome`: `completed` / `timeout` / `error` / `rejected_capacity` / `unauthorized` / `disabled` | `WebhookRunStore.record` in `webhooks.py` |
| `kirocrew.subagent.completed` | counter | `outcome`: `completed` / `failed` / `stopped` / `unknown` | The sub-agent terminal path; matches crew log `subagent/completed` and `subagent/failed` |
| `kirocrew.inventory.webhooks.registered` | gauge | none | Entry count of the registered hooks file, read like the other inventory gauges |

Plus one vocabulary change: the closed `actor` enum of `turn/started` gains
`webhook` and `heartbeat`. Today those wakes fall into `other`, so
`crew.turn.started{actor}` cannot separate them.

### 6.5 Crew log health

Main has no crew log metric. Only the in-process `WriterStats` and warning logs
exist; folds, savepoints and the bus are not timed.

First batch (none of these touch the append thread):

| Metric | Type | Attributes | Where | Cost |
|---|---|---|---|---|
| `kirocrew.crew_log.write.lost` | counter | `cause`: `dropped` / `overflowed` / `wake_dropped` | Writer drop and overflow paths, eager queue drop | Failure path only |
| `kirocrew.crew_log.fold.apply` | counter | `scope`: `slot` / `session`; `fold`: the closed fold catalogue (`FOLD_NAMES`, checked at import); `bucket`: `lt_10ms` / `lt_100ms` / `lt_1s` / `ge_1s` | Around `_advance` / `_advance_session` in the eager worker | Once per fold per batch, worker thread |
| `kirocrew.crew_log.fold.start` | counter | `scope`; `origin`: `warm` / `savepoint` / `cold` | The three start branches of a fold | Once per fold start |

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

## 7. Phases

1. Section 5 changes, in #21342 and #21308.
2. Session control (6.1) and crew log health first batch (6.5).
3. Monitors (6.2), work ledger (6.3), event sources (6.4) and the `actor`
   vocabulary change.
4. Crew log health second batch, after measuring `fold.lag` cost on a busy
   install.

Each phase is one PR per area with tests that pin the attribute value lists.

## 8. Backward compatibility

- `crew.session.tree.depth` changes type before it ships, so no consumer has
  seen the histogram.
- Adding `agent_kind` to `crew.turn.started` adds series; it removes none.
- Adding `webhook` and `heartbeat` to the `actor` enum moves some turns out of
  `other`. Dashboards that read `other` will see it shrink.

## 9. Security and privacy considerations

- No attribute carries an id, a name, a path, a URL or user text. Each enum is
  listed in this document and pinned by a test.
- Monitor stop reasons and webhook outcomes contain free text in local logs;
  only the mapped closed value is used, never the text.
- All metrics stay behind `telemetry.enabled`. Which ones leave the machine is
  decided in the export RFC, not here.

## 10. Alternatives considered

- Histograms for durations and depth. Rejected: their buckets do not survive
  every backend, and the bucket sets we need are small.
- An up/down counter for "loops running" or "trees open". Rejected: a crash
  loses the decrement and the number drifts upward. Gauges read live state.
- Counting ledger outcomes in the `work` fold. Rejected: the fold forgets closed
  items after 32.
- One generic "feature used" counter with a `feature` attribute. Rejected: each
  area needs its own outcome enum, and one shared counter would mix them.

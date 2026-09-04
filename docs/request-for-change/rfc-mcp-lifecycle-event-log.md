---
title: MCP Session Trace as the Lifecycle Event Log's First Emitter
status: draft
author: Ray Xu (buluoray)
created: 2026-09-01
last-audited: 2026-09-01
audited-at: 1ee69f225
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: MCP Session Trace as the Lifecycle Event Log's First Emitter

## Summary

The lifecycle event log (`src/kiro_crew/events/`, landed by
[#3808](https://github.com/kirodotdev/KiroCrew/pull/3808), simplified by
[#7386](https://github.com/kirodotdev/KiroCrew/pull/7386)) is a validated schema with no live
writer. Its own contract defers three decisions to whoever emits first: the ordering model, the
writer, and — implicitly — the failure posture. This RFC settles those three as **package-level
precedents** that every later domain inherits, and lands them exercised by one concrete
emitter/consumer pair: a per-session MCP registration trace, motivated by the residual gaps of
[#7366](https://github.com/kirodotdev/KiroCrew/pull/7366) (the MCP session report).

The three precedents, in one line each:

1. **Ordering** is a per-`key` monotonic `seq` assigned by the single writer; global order is
   deliberately not promised.
2. **The writer** is one gateway-owned append path to `<data_home>/events/lifecycle.jsonl`,
   serialized behind one lock, rotated by the existing `jsonl_util.rotate_jsonl_at`.
3. **Failure posture is fail-open**: this log is observability, not audit. A dropped event logs a
   warning; it never fails the operation that emitted it. SEL remains the fail-closed,
   HMAC-chained audit trail, and nothing here touches it.

## Motivation

### Current state

- The events package ships a typed envelope, a kind registry, tolerant parsing, an additive-only
  evolution contract, and a read-only backfill validator proven against real stores. Nothing
  writes. Its docstring states that ordering "arrives, specified and exercised, with the first
  emitter", and the backfill module states the write path "lands with the first consumer that
  folds the log". Both debts are called in here.
- #7366 gives each session an in-memory report of what its MCP servers actually reported
  (ready / failed with reason / awaiting OAuth), stamped with the session identity it describes
  and dropped by the serializer on identity mismatch.

### Problems

- **History dies with the session.** After `reset-conversation` the panel is honestly blank until
  the next turn creates a session; what the previous generation had mounted is unrecoverable. ACP
  offers no way to re-ask a live session either — registration is push-once at init.
- **Invalidation is a projector-side identity check plus seven courtesy `clear_mcp_report()`
  call sites.** Review found call sites missing twice during #7366; the single-projector guard
  closes correctness, but the shape invites the same finding forever.
- **Nothing is traceable after the fact.** Which server failed, when, across which session
  generations — unanswerable today, and exactly the question an operator asks after an OAuth
  expiry or a broken spawn.
- **Every future surface pays again.** The task-manager and context-panel consumers named in
  #3808 will need the same ordering/writer/failure decisions; made ad hoc by whichever domain
  emits first, they become accidental precedent. This RFC makes them deliberate.

## Goals

- Define the ordering, writer, and failure-posture contracts once, at package level.
- Emit MCP registration lifecycle facts from the capture points #7366 already built, inheriting
  its ownership predicate (`_owns_mcp_frame`) and redaction unchanged.
- Fold the log into a per-slot MCP trace: current generation as today, prior generations as
  labeled history.
- Leave every existing store — SEL, transcripts, usage shards, the in-memory report — exactly as
  authoritative as it is now.

## Non-goals

- Per-tool attribution (ACP cannot attribute tool lists per session today; server granularity is
  the finest honest answer).
- Replacing or extending SEL, or giving this log tamper evidence.
- Querying a live session mid-flight (no such ACP request exists; that is an upstream protocol
  ask, tracked separately).
- Migrating other domains (cron, subagent, autonudge) onto the log. They adopt the precedents
  set here in their own changes.

## Design

### Ordering: per-key `seq`, single writer, no global promise

Every emit site in scope runs inside the gateway process, so one writer can serialize appends
behind an asyncio lock and assign `seq` per `key` (seeded at startup from the live segment's
tail). `seq` is a new **optional** envelope field — additive under the schema contract; parsers
that predate it ignore it, and `RawEvent` tolerance already covers unknown kinds.

Scope choice, and why: consumers fold **per key** (a slot's trace, a cron's history, a subagent's
lifecycle) — that is the join axis the envelope already defines. A global sequence would promise
cross-key ordering nobody consumes, and would become a lie the moment a second writer process
ever exists. Cross-key readers keep `ts_ms`, which is already documented as the event's own time.

### Writer: `events/log.py`

- Path: `<data_home>/events/lifecycle.jsonl`; closed segments rotate into
  `<data_home>/events/lifecycle.d/` via `jsonl_util.rotate_jsonl_at` (1 MiB segments, keep 4 —
  the `slow_commands.jsonl` precedent, bounded at ~5 MiB total).
- One module-level async-locked append function; emit sites construct a typed kind and call it.
  `serialize(event, src=...)` from `events/base.py` is the only line format.
- Startup seeds per-key counters by scanning the live segment only (closed segments are history;
  a key resuming after rotation restarts its counter — acceptable because `seq` orders within a
  fold, and a fold that spans segments already orders segments by rotation order).

### Failure posture: fail-open, stated once

A write failure warns and drops. Rationale: the log's consumers render dashboards; its absence
degrades a panel, not a security boundary. SEL is the fail-closed trail and keeps that role. This
contrast is stated here precisely so later reviews do not re-litigate it per emitter: **an
emitter that needs fail-closed semantics belongs in SEL, not here.**

### The `mcp/` domain (new kinds in `events/kinds.py`)

| Kind | Fields (beyond `key`, `ts_ms`) | Emitted when |
|---|---|---|
| `mcp/session-began` | `session_id`, `roster: tuple[str, ...]` | init drain starts for a new or loaded session |
| `mcp/server-initialized` | `session_id`, `server` | an owned registration frame or live event is accepted |
| `mcp/server-init-failure` | `session_id`, `server`, `error` | same, failure case; `error` is redacted **before** emit, then capped |
| `mcp/oauth-requested` | `session_id`, `server` | an owned OAuth request is accepted |
| `mcp/session-ended` | `session_id`, `cause` (`reset`\|`reload`\|`discard`\|`terminated`) | the teardown seam releases the session |

`key` is the slot's session key (the existing convention for session-shaped keys). `session_id`
is the generation discriminator — the same identity #7366 stamps on the report. Emits sit
**downstream** of the ownership predicate and redaction, so the log inherits #7366's attribution
guarantees without new logic; a frame the report refuses is a frame the log never sees.

### Consumer: per-slot MCP trace

`GET /api/chat/slots/{slot}/mcp-trace` folds the log for the slot's key: the current
`session_id`'s events render as the live report does today (the in-memory report stays, as the
zero-latency cache); prior generations render as collapsed, timestamped "previous session" rows.
The serializer's identity check is untouched — the log adds history; it does not answer
liveness. Once `mcp/session-ended` exists, the seven courtesy `clear_mcp_report()` calls become
provably redundant and can be retired in a follow-up.

## Migration plan

### Phase 1: precedents + writer + `mcp/` emitters (backend only, invisible)

`events/log.py`, the `seq` envelope field, the five kinds, emit sites at the #7366 capture
points, and a backfill-style validation run against a real data home. Nothing reads the log yet;
the change is purely additive.

### Phase 2: trace endpoint + panel history

The fold, the endpoint, and the panel's "previous session" rows.

### Phase 3: retire the courtesy clears

Delete the seven `clear_mcp_report()` call sites once `session-ended` events are observed
covering every teardown path; the identity check remains as the backstop.

## Backward compatibility

Additive throughout: no existing store is read, written, or reshaped; new envelope field is
optional; unknown kinds parse as `RawEvent` by contract. Rollback at any phase is deletion of
the new code plus the log directory.

## Security considerations

- Failure reasons pass the existing exfiltration-URL and credential redaction **before** emit
  (redact-before-truncate, per the SEL lesson).
- Events carry server names and session identifiers only — never tool arguments, payloads, or
  environment. Tool-invocation auditing stays in SEL.
- The log lives under the data home but is not keystone material; the agent may read it. It must
  never be added to the sensitive-path deny lists, or the panel's own backend could not serve it.

## Open questions

1. Retention: 4 × 1 MiB segments (proposed) vs age-based pruning.
2. Should gateway shutdown emit `mcp/session-ended` for every live session, or is absence of
   further events the honest record there? (Proposed: absence — a crash cannot emit either, so
   consumers must tolerate missing ends regardless.)

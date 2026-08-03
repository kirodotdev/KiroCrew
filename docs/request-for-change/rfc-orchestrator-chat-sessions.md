# RFC: Orchestrator Chat Sessions — an engineered pipeline with a decision-only agent

Status: v5 — post-second-council revision; proposed as design of record
Councils: round 1 (v1 draft, 4-member cross-vendor, REVISE — every structural
BLOCKER traced to simulating engineering in a prompt) → v4 architecture rewrite
(heavy engineering, light agent) → round 2 (v4, same roster, unanimous REVISE:
architecture endorsed by all four reviewers including the defender; findings
are specification gaps, all folded in below)
Author: kirocrew agent session, directed by zezhexu
Date: 2026-08-03

## North star

**The experience must feel like chatting with a capable person.** The user
fires off thoughts as they come — three unrelated asks in a row, a follow-up on
something from ten minutes ago, a "wait, actually…" correction — and the
conversation absorbs them. Instant, casual acknowledgment; results attributed
the way people do it in messaging apps; clarifying questions rare; the
machinery invisible.

Model: **ChatGPT full-duplex voice mode.** The realtime layer does turn-taking,
barge-in, and short acknowledgments at millisecond latency, and delegates all
complex reasoning to a text LLM. Here: the pipeline (queue + executor +
delivery) plus templated acknowledgments is the realtime layer; topic
sub-sessions are the text LLM; the orchestrator agent is a **decision
function**.

*Acknowledged trade-off (council round 2, dissent on record):* removing agent
re-narration forfeits some conversational continuity (tone, callbacks,
"by the way" weaving). Adjudication: keep no-renarration as the default — it is
the architecture's core cost/correctness win — and carry the north-star burden
with (a) completion-burst coalescing, (b) the `note` escape valve, (c) a hard
usability-review gate in Phase 2 that re-evaluates this decision against real
transcripts. If transcripts read like a ticketing system, the fallback design
is a narration-on-top option, not a return to prompt-held state.

## Architecture principle: heavy engineering, light agent

| Concern | Owner | Why |
|---|---|---|
| Message durability & ordering | ingress queue (engineering) | prompt-held queues lose data (council r1) |
| run→topic attribution | topic store (engineering) | prompt-held maps misattribute (council r1) |
| Result delivery | forward + attribution (engineering) | LLM re-narration distorts, doubles tokens |
| Instant acknowledgment | templates + tiny model | must be sub-second |
| Routing & queue-handling judgment | decision agent (LLM) | genuinely requires judgment |
| Sub-task execution | topic sub-sessions (user's chosen agent) | the actual work |

```
user msg ──> ingress queue (durable, per-session, msg ids, entry state machine)
                   │ enqueue always succeeds; instant templated ack
                   ▼
        OrchestratorManager (the control plane — see "Homes")
          serializes decision invocations (single-flight per session)
                   ▼
        decision agent (cheap model, structured I/O, stateless)
          input:  snapshot{generation, queue, topics, event}
          output: actions[] + optional 1-line note
                   ▼
        action executor (validates + CAS on snapshot generation)
          spawn_run(keep=true) / spawn_continue / spawn_steer /
          release / hold / ask — with idempotency keys
          topic store: topic_id, active_run_id, status, digest, queue refs
                   ▼
        delivery: forward the sub-session's summary verbatim,
          attributed to the originating user message
```

## Homes (council r2: the three unspecified locations, now pinned)

- **Control plane**: a new `OrchestratorManager` (sibling of `SubagentManager`),
  owning the ingress queue, topic store, and decision-invocation loop. It
  subscribes to the existing subagent completion bus (the same events that
  drive `[Subagent completion event]` injection) and to an enqueue hook at the
  HTTP handler layer. It is NOT a branch inside `_run_chat()` — orchestrator
  sessions bypass the normal turn loop for ingress (see product shape).
- **Storage**: per-session JSON files under the session's meta directory
  (`queue.json`, `topics.json`), atomic-write on every transition — the same
  durability pattern as subagent `state.json` and the #1114 registry rebuild.
  Explicitly: this durability is NEW Phase-1 work; the existing slot queue
  (`state.py` `_ChatSlot._queue`) is in-memory and is not reused.
- **Product shape**: an **orchestrator session type**, not a mode flag on
  `_ChatSlot`. The HTTP message handler routes messages for orchestrator
  sessions to `OrchestratorManager.enqueue()` before (instead of) turn
  creation — the hold-users gate is explicitly not on this path (council r2:
  the earlier "steer opt-out" framing is superseded; the bypass is at the
  handler layer, by session type). The synthesis nudge (`_pending_synthesis`)
  never arms for this session type.

## Contracts (council r2: the underspecified mechanics, now specified)

### Queue entry state machine + idempotent dispatch

`pending → claimed(decision) → accepted(run_id) → running → terminal(done|failed)`

- The executor marks `accepted` only after spawn/continue returns a run id;
  every dispatch carries an idempotency key `(msg_id, topic_id, attempt)` so a
  crash between accept and persist cannot double-execute after restart.
- Restart reconciliation: on boot, `OrchestratorManager` replays `queue.json` /
  `topics.json` against live run state (`spawn_list` + state.json), re-claims
  `claimed` entries, re-attaches `running` ones, and re-delivers unforwarded
  terminals — the #1114 rebuild pattern extended to this store.

### Decision-invocation concurrency

Single-flight per session: at most one decision call in flight; events arriving
meanwhile are folded into the next snapshot. Every snapshot carries a
`generation`; the executor applies an action set only if the store's generation
still matches (CAS) — a mismatch discards the stale decision and re-invokes on
the fresh snapshot. Two near-simultaneous events therefore cannot produce
duplicate `route`/`spawn` against the same pending message.

### Result summary contract

There is no structured summary field today — `SubagentInfo.result` is arbitrary
prose truncated by `completion_keep` (council r2). Phase 1 defines one:

- Every dispatched task appends a fixed instruction to end with a delimited
  block (`<<<SUMMARY … >>>`, ≤150 words). Delivery extracts the block; if
  absent/malformed, it falls back to the truncated result and marks the
  delivery `summary_fallback` (a probe metric).
- Follow-up: a first-class `summary` field on `SubagentInfo`, written by the
  completion path — tracked as a separate small core PR.

### Delivery & attribution

- Phase 1 (no UI change): each forward opens with a textual attribution line —
  `↩ re: "<originating message, truncated>"` — because no quote-reply
  primitive exists in the dashboard chat message model (council r2; `reply_to`
  exists only in channels and artifact comments). Free: msg_id→text is in the
  queue entry.
- Phase 2: real quote-reply rendering + topic chips + queue/topic visibility.
- **Burst coalescing** (north-star, council r2): terminals arriving within a
  short window (~2s) are delivered as one grouped message (each with its own
  attribution line) instead of a machine-gun sequence.

### Decision agent I/O

Verb set: `route / spawn / hold / steer / forward / ask / release`, plus
optional `note` (1 line, default empty). Executor validates every action
against live state; illegal actions are rejected and the agent re-invoked with
the error. Steer remains advisory-only (fire-and-forget; effect verified at
next completion, re-queued if lost). Topics spawn `keep=true` (retention
promotes at spawn; dedicated-process resume path). Concurrency ceiling = the
resolved subagent cap; overflow waits visibly in the queue.

## Existing primitives this sits on (#1023 + #1246, field-verified)

`spawn_continue` (session/load restore) · `spawn_steer` (15s startup grace) ·
typed `conversation_busy` / `conversation_gone` / `resume_failed` ·
retain-by-default + TTL + restart registry rebuild · result.txt/`spawn_status`.

## Non-goals (v1)

Cross-topic context sharing; nested spawning in sub-sessions; multiple agents
per orchestrator session.

## Phased plan (council r2: Phase 1 split into PR-sized slices)

- **Phase 0 — routing-quality probe (now, zero core).** Feed the decision agent
  engineered-shaped snapshots (hand-assembled queue+topic JSON per the schema
  above) over scripted interleaved traffic; measure routing accuracy and `ask`
  cadence in isolation. NOT the prompt-heavy orchestrator agent — that would
  conflate routing error with bookkeeping error (council r2).
- **Phase 1 — pipeline MVP, as five PRs:**
  1. `OrchestratorManager` skeleton + durable queue/topic stores + restart
     reconciliation (backend only, feature-flagged)
  2. Orchestrator session type + HTTP ingress hook + templated acks
  3. Decision-invocation protocol (single-flight, generation CAS) + executor
     with idempotent dispatch
  4. Delivery: summary-contract extraction, textual attribution, burst
     coalescing; synthesis-nudge disable for the session type
  5. `SubagentInfo.summary` structured field (small core PR, parallel)
- **Phase 2 — surface:** quote-reply rendering, topic chips, queue/topic panel;
  **usability-review gate** on the north star (does the transcript read like a
  person?) with authority to add a narration-on-top option.
- **Phase 3 — convergence:** fold into the sessions-as-uniform-compute-units
  direction; the pipeline becomes the first production consumer of the
  symmetric session API. Queue/store schemas carry a `schema_version` field so
  Phase-3 migration is explicit rather than breaking (council r2).

## Known risks

1. **Misrouting** (residual LLM risk): `ask` on low confidence + Phase-2 chips;
   blast radius one topic; recovery = release + respawn with digest + queued
   payload replay.
2. **Canned-feeling acks / fragmented transcript** (north-star risk, dissent on
   record): coalescing + note valve now; Phase-2 gate decides if narration
   returns as an option.
3. **Decision latency** (~1-2s per event): hidden behind instant enqueue ack.
4. **Scope**: Phase 1 is honest about being five PRs; Phase 0 gates the whole
   investment on the routing question.

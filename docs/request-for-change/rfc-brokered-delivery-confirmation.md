---
title: Commit a brokered inline-collected result on the parent turn's outcome, not on a relay acknowledgement
status: draft
author: kirocrew agent session, directed by jeeshofone
created: 2026-10-28
last-audited: 2026-10-28
audited-at: ea38bdd5e5
doc-pr: 25214
implementation-prs: []
tracking-issues: [18743]
supersedes: []
superseded-by: []
---

# RFC: Commit a brokered inline-collected result on the parent turn's outcome, not on a relay acknowledgement

Status: draft. Acceptance requested from a maintainer of the MCP broker and the sub-agent delivery path. Part of [#18743](https://github.com/kirodotdev/KiroCrew/issues/18743). Main-side references were read at `ea38bdd5e5`. References to the inline-collection mechanism were read on [#17684](https://github.com/kirodotdev/KiroCrew/pull/17684) at head `0592d79154`, which is open and not on main.

## Summary

A blocking `spawn_sub_agents` call returns its children's results inline. [#17684](https://github.com/kirodotdev/KiroCrew/pull/17684) stops those results from also arriving as `[Subagent completion event]` turns, and it decides when a returned result counts as delivered. On the unbrokered path it records a mark when `respond()` wrote the whole frame into kiro-cli's pipe, holds that mark under the parent attempt that spawned the child, and confirms it only when that attempt lands. On the brokered path (a `kirocrew-core` stubbed through `mcp_gateway.stub_servers`) a reply marks nothing today, so every child's completion is also delivered: a duplicate turn, never a loss.

Issue #18743 asks how a brokered reply should earn the same suppression. Two answers were considered. One is a brokered acknowledgement: the stub confirms each relayed frame back through the broker to the backend, and the commit waits for it. The other is to commit on the parent turn's own outcome, which #17684 already does for the unbrokered path.

This document recommends the second. A brokered reply records a pending mark under its spawning attempt exactly as an unbrokered one does, with `written` replaced by a new `relayed` reason that means "handed to the broker". The mark is confirmed when the attempt lands and kiro-cli's own ACP stream reported that tool call `completed`; anything else revokes it. No frame on the MCP relay path changes, no `kirocrew.*` key travels anywhere, and the only new state is a `call_id` on each held mark and a capped per-attempt set of completed call ids, both dropped when the attempt settles.

## Motivation

### The gap

Behind the broker, the `kirocrew-core` backend writes its `tools/call` response to its own stdout. The broker relays it to the session's stub, and the stub writes it into kiro-cli's stdin. Two hops follow the backend's write: broker to stub, and stub to kiro-cli. If either loses the frame (kiro-cli stops reading mid-relay, the stub dies after the backend wrote), a result that was already marked delivered reaches the parent turn by neither path.

#17684 closes this for itself by marking nothing on a brokered reply (`on_answered(written)` is True only when `respond()` in `mcp_shared_wire.py` wrote the whole frame into the client's pipe, never for a pooled backend). So the gap is not a loss today. It is a cost: a stubbed `kirocrew-core` pays one redundant model turn per child, which is the cost #17684 exists to remove. Stubbing `kirocrew-core` is off by default (`stub_servers` defaults to empty in `config/integration_sections.py`) and is blocked on this issue.

### Evidence

A live run on an earlier #17684 head (real kiro-cli 2.29.0, brokered `kirocrew-core`, two dashboard turns and one cron turn, recorded in a comment on #17684) relayed 29 brokered responses. Every one confirmed `delivered:true`, none came back false or timed out, and the write-to-empty-pipe drain measured p50 0.02 ms, p95 1.09 ms, max 1.11 ms.

No frame has been observed lost on these hops, and no user has reported such a loss. The case for this work rests on the failure mode and on the duplicate-turn cost, not on an incident.

### Why the earlier design was cut

#17684 carried a brokered acknowledgement for several rounds: a backend capability, a broker offer on each forwarded `tools/call`, a stub marker and answer line, per-connection confirmation tracking, a stub-side pipe read wait, and a bounded backend waiter pool. Review found it outside that PR's goal and sized well past it, with new permanent wire surface on the core MCP relay path. It was removed. This document starts from that review.

## Goals

- A brokered `spawn_sub_agents` reply suppresses its children's completion turns under the same rule as an unbrokered one.
- A result is acknowledged only after kiro-cli reported the call `completed` and the parent turn that received it landed. Anything short of that is a revoke, and the completion is delivered: a duplicate, never a loss.
- Each review point #18743 lists under "Clears when" is answered (section 4).

## Non-goals

- Changing any frame the broker, the stub or a backend sends or reads.
- Confirming delivery for MCP tools other than the inline-collecting `spawn_sub_agents` call. No other call has an outcome hook today.
- Making a pooled backend's write count as `written`. `written` keeps its #17684 meaning.
- Turning `stub_servers` on for `kirocrew-core` by default. That stays an operator choice; this document only removes the reason it is blocked.
- `spawn_run` delivery, deferring completions while a parent turn is busy ([#17898](https://github.com/kirodotdev/KiroCrew/issues/17898)), and the dashboard-born tab's own window ([#23495](https://github.com/kirodotdev/KiroCrew/issues/23495)).

## 1. Design

### 1.1 What #17684 already does

On #17684 at `0592d79154`:

1. `_commit_settled` (`mcp_shared_dispatch.py`) answers the call, then runs `DeferredTool.on_answered(written)` (`mcp_shared_calls.py`).
2. `wave.py` sends a `mark-collected` for the members whose block survived the `MAX_RESPONSE_LEN` cut. `api_spawn_mark_collected` (`dashboard/messaging_api/run_control.py`) records it only with `written: true`.
3. For every parent but a dashboard-born one (`parent_turns.settles_at_release`), the mark is pending under the attempt that spawned the child (`SessionManager.turn_token`: key, task, epoch), and only when that attempt also read the child done (`_seen_done_under`). A dashboard-born parent has no attempt to settle under: its `written` mark is acknowledged when it arrives (`_settle_collected_on_slot`).
4. Engines call `note_turn_landed` where they decide a turn landed. `settle_turn` ends an attempt: a noted attempt confirms its pending marks, anything else revokes them.
5. `_inline_verdict` (`subagent_injection.py`) answers each delivery route: confirmed is acknowledged, pending is parked, anything else is delivered.

Step 4 is already the parent turn's own outcome. Only step 2's admission keeps a brokered reply out of it: `_commit_settled` passes `written and not is_pooled_backend()`.

The issue's body and comments describe an earlier #17684 head, which committed at the backend's write and had a `COLLECTION_PHASES` set (`claim`, `commit`, `drop`). Neither is on `0592d79154`. This document builds on the current head, so the phase-skew rule those comments cite is replaced by the mixed-version rule in section 5.

### 1.2 The change

A brokered reply sends its mark with a reason that says how far the frame got:

| `on_answered` sees | Mark sent | Gateway records |
|---|---|---|
| `respond()` wrote the whole frame to kiro-cli's pipe | `written: true` (as today) | pending under the spawning attempt |
| the reply went to the broker connection (pooled backend) and the write returned without error | `relayed: true` (new) | pending under the spawning attempt |
| the write failed or was cut | nothing | nothing; completions are delivered |

A `relayed` mark is held under the spawning attempt like a `written` one, with one more condition on its confirm. The gateway already reads kiro-cli's ACP stream for the turn, including each `tool_call_update` and its `rawOutput` (`_extract_tool_call_update` in `acp/client_tool_events.py`, `acp/_dispatch_parts/tool_result_events.py`). A `relayed` mark is confirmed only when both hold:

- the attempt landed (`note_turn_landed`, then `settle_turn`), and
- in that attempt's stream, the `spawn_sub_agents` tool call that carried the mark reached `status: completed`, and its output carries the call's `call_id` (the 12-hex-character id #17684 already sends in the `POST /api/spawn` body; the implementation adds it to the result text `wave.py` returns). The `call_id` leads the result text, so neither the backend's `MAX_RESPONSE_LEN` cut nor the gateway's display cut removes it, and the implementation PR tests that it survives the gateway's redaction. The match is limited to the ACP `toolCallId` of that `spawn_sub_agents` call: the same string in any other tool's output does not count.

A deadline, a stop, a reap, a cancellation or a failure before landing revokes the mark, and so does a `failed` status for that call or no matching `completed` update at all. The parked completion then takes its route.

For a dashboard-born parent (`settles_at_release` is False), which has no attempt, a `relayed` mark is acknowledged only once the ACP stream reported that call `completed` with its `call_id`. Until then the completion is delivered.

### 1.3 Why this is enough evidence

If the frame is lost after the backend's write, kiro-cli never receives that result. Either the turn stays parked on the call until the engine's deadline, the user's stop or the reap ends it, none of which is a landed stop reason, or the stub answers the call itself. The stub does that when its bridge ends with requests outstanding: `_emit_error_frames` in `mcp_gateway/stub_bridge.py` answers each abandoned id with an error frame. The model may then carry on, so the turn can land, and landing alone would confirm a lost result. Whatever status kiro-cli reports for that call, the stub's error frame cannot carry the call's `call_id`, so no `completed` match exists and the mark is revoked. The same holds for a broker restart (`stub_link` answers abandoned ids the same way), kiro-cli no longer reading, and kiro-cli's own call timeout.

If the frame arrives, kiro-cli reports the call `completed` with the result in `rawOutput`, the model reads it, and the turn continues. That is stronger evidence than a relay acknowledgement: an acknowledgement proves the stub wrote bytes into a pipe, and this proves kiro-cli parsed the result and the turn went on past it.

One case is left: kiro-cli reports the call `completed` but the model's context does not include the result. A relay acknowledgement is sent before that point too, so neither design covers it.

## 2. Alternatives weighed

### 2.1 A brokered acknowledgement (the cut design)

The stub confirms each relayed `tools/call` response back through the broker to the backend, and the backend's `on_answered` waits for that confirmation before marking.

| | Brokered acknowledgement | Parent turn's outcome (recommended) |
|---|---|---|
| What it proves | the stub wrote the frame into kiro-cli's pipe | kiro-cli reported the call `completed` with its `call_id`, and the turn landed |
| New wire surface | a capability, an offer field on forwarded `tools/call`, a stub answer line, a backend reply | none on any MCP frame; two optional fields on Kiro Crew's own `mark-collected` body |
| New state | per-connection confirmation table in the broker; waiter pool in the backend | a `call_id` on each held mark and a capped per-attempt set of completed call ids |
| Third-party servers | must never see the offer field, so the broker must strip it per backend | untouched |
| Event loop | the stub's pipe-drain wait must not block its loop; the backend waiter must be bounded and timed out | no new wait |
| Mixed versions | old stub or old broker answers nothing; silence must be read as a drop, with a timeout per call | old backend sends no `relayed`; old gateway ignores the unknown field (section 5) |
| Latency to commit | one relay round trip | the rest of the parent turn |
| A stub error frame after a lost frame | the acknowledgement never comes; revoke | no matching `completed` update; revoke |
| Covers a result kiro-cli reports but drops | no | no |

The acknowledgement commits sooner. Nothing in the delivery path benefits from the sooner commit, because #17684 already parks every non-dashboard-born completion until the attempt settles. A commit at the acknowledgement would still wait for the settle to take effect.

### 2.2 Keep marking nothing on a brokered reply

This is #17684's position and costs one redundant turn per child on a stubbed `kirocrew-core`. It stays the fallback: a backend or gateway that does not speak `relayed` behaves this way (section 5).

## 3. Bounds

`relayed` adds one field to each held mark (its `call_id`) and one per-attempt set of the `spawn_sub_agents` call ids the ACP stream reported `completed`. The set is needed because the ACP update can arrive before the mark's POST. It is capped at the same 200 entries as a mark body, each a 12-character id, and dropped when the attempt settles; for a dashboard-born parent it is dropped when the slot's turn ends. Everything else reuses #17684's bounds: one `mark-collected` body carries at most the first 200 child ids, each at most `_COLLECTED_ID_MAX_LEN` (128), and a pending mark lives only as long as its attempt. The implementation PR tests both new bounds.

## 4. The "Clears when" points

| Point from #18743 | Answer |
|---|---|
| Confirmation is asked only for calls that need it (a call with an outcome hook) | Only a call whose `DeferredTool` carries `on_answered` sends a mark. Today that is the inline-collecting `spawn_sub_agents` call alone. Every other call is relayed exactly as now. |
| A third-party MCP server never sees a `kirocrew.*` key | Nothing is added to any MCP frame. `relayed` travels only on the `mark-collected` HTTP body from `kirocrew-core` to its own gateway. |
| Every new table and waiter is bounded in bytes and count | One field per held mark and one per-attempt set, both capped and dropped at settle (section 3). No waiter is added. |
| No blocking probe runs on an event loop | There is no probe. `on_answered` reads the return of a write it already made, and the `completed` check reads ACP events the gateway already parses. |
| Mixed old and new installs read silence as a drop | Each version mix ends in "deliver the completion" (section 5). |
| Weigh the parent turn's outcome against a brokered acknowledgement | Section 2.1; the outcome is recommended. |

## 5. Rollout and compatibility

One implementation PR, after #17684 merges, since it extends #17684's `mark-collected` route and `on_answered` contract.

| Backend (`kirocrew-core`) | Gateway | Result |
|---|---|---|
| old | old or new | no mark on a brokered reply; completions delivered |
| new | old | the old gateway does not read `relayed` and records nothing, since it records only `written: true`; completions delivered |
| new | new | `relayed` mark held under the spawning attempt; confirmed on landing, revoked otherwise |

The new-backend, old-gateway row depends on #17684's rule that a mark without `written: true` changes nothing. The implementation PR keeps that rule and pins that a body without `written: true` flags nothing under the predecessor contract. A gateway and its `kirocrew-core` normally share a version, so the mixed rows only occur during a restart.

The wire contracts #17684's spec marks as changeable until this issue (the `/api/spawn` inline fields, the commit body, `DeferredTool.on_answered`) change by an optional `relayed` field and a `call_id` field on the commit body, the `call_id` in the returned result text, and `on_answered` taking a reason instead of a bool. The states table in `docs/system-specs/modules/subagent.md` keeps its rows. Its commit trigger on the brokered path moves from "nothing" to "the spawning attempt's landing", and the implementation PR names that row.

Phases:

1. **Phase 1: `relayed` marks.** `on_answered` reports `relayed` for a pooled write that returned without error; `api_spawn_mark_collected` records it under the spawning attempt; `settle_turn` confirms it only with the matching `completed` update. Every engine that calls `note_turn_landed` also forwards the completed `spawn_sub_agents` call ids it saw: `turn_bracket.py`, `cron_turn.py`, the Telegram `turn_engine.py` and `task_executor.py`. Exit criteria: a brokered `spawn_sub_agents` call whose parent turn lands injects no completion turn; a stub killed after the backend's write, whose error frames let the turn land, injects every completion; one whose turn is cancelled, stopped or reaped after the backend's write injects every completion; a gateway without the field ignores it.
2. **Phase 2: unblock stubbing `kirocrew-core`.** Remove the #18743 reference in `docs/system-specs/modules/subagent.md` (the pooled-backend line under the inline-collection section, added by #17684). Exit criteria: a live run with `kirocrew-core` stubbed, with a cron and a channel parent, matches #17684's unbrokered manual-verification table.

Either phase can be abandoned alone. Without Phase 1, the system keeps #17684's behaviour.

## 6. Security considerations

`relayed` is accepted only on the existing `mark-collected` route, from the caller that route already admits, under the attempt `turn_token` already checks. It grants nothing a `written` mark does not. A forged `relayed` mark could at worst suppress a completion turn whose result the turn did not land with, and the landing check revokes that mark.

## 7. Open questions

1. Should `relayed` and `written` collapse into one reason now that both are settled by landing? Keeping both preserves the record of how far a frame got, which helps when diagnosing a revoke. This document keeps both.
2. The case in section 1.3 where kiro-cli reports a result `completed` but it never reaches the model is covered by neither design. It is out of scope here; a fix would belong in kiro-cli.
3. Matching the `completed` update by `call_id` in the output text assumes kiro-cli passes `kirocrew-core`'s text through `rawOutput` unchanged, which `tool_result_events.py` reads today. If a backend other than kiro-cli summarizes tool output, the match fails and the mark is revoked: a duplicate, never a loss. The implementation PR records which harnesses pass the match.

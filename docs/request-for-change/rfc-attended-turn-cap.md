---
title: Cap how many attended chat turns run at once, at the one dispatch every send and Resume reaches
status: draft
author: kirocrew agent session, directed by jeeshofone
created: 2026-11-02
last-audited: 2026-11-02
audited-at: d6d40e23a2
doc-pr: 28633
implementation-prs: []
tracking-issues: [25055]
supersedes: []
superseded-by: []
---

# RFC: Cap how many attended chat turns run at once, at the one dispatch every send and Resume reaches

Status: draft. Acceptance requested from a maintainer of dashboard turn dispatch. Part of [#25055](https://github.com/kirodotdev/KiroCrew/issues/25055). Main-side references were read at `d6d40e23a2`. References to the bulk Resume were read on [#27512](https://github.com/kirodotdev/KiroCrew/pull/27512) at head `0dbab773cb`, which is open and not on main.

## Summary

With Shared Crew fair admission off (the default), nothing limits how many dashboard chat turns a person starts can run together. The cold-start semaphore paces boots, and the background-turn cap counts only unattended app-owned turns. Resuming many interrupted sessions, one tab at a time or with the bulk Resume in #27512, leaves every one of them running at once after its start ends.

This document recommends one attended-turn semaphore, held by `DashboardState` beside `_bg_turn_sema`, taken in the exit guard of `_run_chat` beside `gate_turn`, the one seam every turn producer passes, and released when the turn ends. Turns queue at the cap, with a bounded wait that ends in a visible message. A turn a person typed goes ahead of queued Resumes. The default is `auto`, sized from the host, and `0` turns the cap off.

## Motivation

### Current state on main

- `SessionManager._start_sem` holds a permit from runtime spawn until the session's MCP servers have reported (`src/kiro_crew/cold_start_sizing.py`). It paces boots, not turns.
- Many dashboard turns are started by `spawn_guarded_turn` (`src/kiro_crew/dashboard/turn_dispatch.py`), which sets only the wall-clock ceiling. Its callers on main are `chat_api/chat_send.py`, `chat_turn/queue_drain.py`, `chat_turn/queue_cycle.py`, `mate_welcome.py` and `slack/gateway_runtime/nudge_dashboard_fire.py`.
- A second path starts a turn without `spawn_guarded_turn`: `_ChatSlot.enqueue_or_run_prompt` (`src/kiro_crew/dashboard/state_parts/chat_slot.py`) runs a prompt on an idle slot with a bare `asyncio.create_task(run_chat_coro(...))`. Its callers on main are `session_deliver.py`, `slack/gateway_runtime/result_egress.py`, `server_runtime/workflow_startup.py` and the issue radar's `crew_runtime_session.py`.
- Other producers call `_run_chat` directly with neither helper: `chat_regenerate.py`, `chat_rewind.py`, `chat_turn/slash_commands.py`, `handlers/taskrunner.py`, `openai_compat.py`, the spec builder's `runtime.py` and Slack inbound. Every one of them passes the exit guard of `_run_chat` in `dashboard/chat_runner.py`, which already calls `gate_turn`.
- `shared_crew.turn_gate.gate_turn` applies Shared Crew fair admission (per-principal and Crew-wide active limits from `EffectiveLimits`) when `shared_crew.admission_enabled` is true. It is off by default, so on a default install it is a pass-through.
- `run_background_turn` (`state_parts/dashboard_services.py`) waits on `_bg_turn_sema` only when `slot.unattended`, and passes an attended slot straight through. Only some send paths wrap a turn in it; the queue drain does not.
- `agent.admission_gate` defers cron firings, TaskRunner steps and workflow agent calls under memory pressure, and never gates a direct chat turn.

### The problem

#25055 measured it with a real `SessionManager` at `agent.cold_start_concurrency` 4, the floor of its `auto` sizing: 40 resumed sessions boot 5 at a time and then hold 40 turns at once. A session whose agent is still running needs no start, so its turn begins at once. Each of those turns holds a model stream, its MCP servers and its tool processes for the whole turn, so host memory and model rate limits take the load that the cold-start queue was meant to spread.

#27512 makes this one click. Its `_resume_candidates` runs one `gather` over every candidate and relies on `_start_sem` for pacing, which ends when each start ends.

## Goals

- At most N attended turns run at once, where N is a setting with a host-sized default.
- Every path that starts an attended dashboard turn passes the same check, so no attended turn can bypass it. Whether a turn is attended is read from `_ChatSlot.unattended`, so the cap is only as complete as that flag.
- A turn over the cap waits and then runs. If the wait passes its bound, it ends with a message that names the cap; it is never dropped silently.
- A message a person typed is not stuck behind a bulk Resume.
- The waiting count is visible, as `background_turn_stats` makes the unattended queue visible.

## Non-goals

- Changing the unattended cap, `dashboard.max_background_turns`, or which turns count as unattended.
- Changing cold-start pacing or the person reserve on `_start_sem`.
- Gating cron, TaskRunner or workflow agent calls, which `agent.admission_gate` already covers.
- Steering into a turn that is already running. A steer is not a new turn and takes no permit.

## 1. Design

### 1.1 The semaphore

`DashboardState` gains an `_attended_turn_sema` field, created lazily by an accessor and resized only while idle, the same way the `_bg_turn_sema` field and its `_background_turn_sema()` accessor work today, with `_attended_turns_running` and `_attended_turns_waiting` counters. Two semaphores rather than one shared pool, so a crew of unattended turns cannot starve people and people cannot starve the crew.

### 1.2 Where it is taken

The permit is taken inside the `guarded` wrapper of `_run_chat` (`dashboard/chat_runner.py`), around the same call that runs `gate_turn`, and released in that wrapper's `finally`. Every turn producer listed above reaches this wrapper, so no call site changes and none can bypass it. Unattended slots skip the permit and stay under `run_background_turn` as today. Unattended turns started on paths that do not call `run_background_turn` (the queue drain, for one) stay uncapped; that gap is out of scope here. A guard test asserts that the wrapper takes the permit for an attended slot and that no module under `src/kiro_crew/` runs a turn's body without going through `_run_chat`.

Nesting with fair admission: the attended permit is taken first, then `gate_turn`. When admission is off, `gate_turn` passes through and only the permit applies. When admission is on and queues the turn, the turn holds its attended permit while it waits there. This keeps one ordering rule (the attended queue's, §1.3) and cannot invert it, at the cost of a permit sitting idle while admission holds the turn. Open question 3 asks whether admission-on installs should skip the attended cap instead.

The wait is bounded, matching `_BACKGROUND_QUEUE_WAIT_SECS = 1800` for the unattended cap: a turn that cannot take a permit within 1800 seconds ends with a message saying the attended cap was full and naming the setting, and the person's message is kept for resending (the guard records the turn as failed before it started and keeps its message; the Phase 1 exit criteria test this). For turns started through `spawn_guarded_turn`, wait time counts against the turn's wall-clock ceiling, because `_bounded_turn` arms its deadline before `_run_chat` is entered and this design changes no call site. A slot whose turn limit is shorter than 1800 seconds therefore ends at its own ceiling first, with the existing timeout card; the cap message appears only when the 1800-second bound is the shorter one. On the `enqueue_or_run_prompt` and direct `_run_chat` paths there is no outer ceiling, so the 1800-second permit bound is the only limit on the wait.

While a turn waits for a permit, the slot carries a distinct `awaiting_attended_permit` flag. Status reads it as queued, not running. A steer to that slot is held and delivered once the turn starts, because there is no running turn to steer yet; a new send to it queues behind the waiting turn exactly as a send to a running slot does today.

A re-entrant turn (`_run_chat` called with `_prompt_depth > 0`, as a nested `/prompts get` is) reuses the permit its outer turn holds and takes no new one. Otherwise an outer turn holding a permit while it awaits its nested turn would deadlock against it, every time at cap 1. A turn that holds a permit and then waits on `SessionManager._start_sem` (the cold-start limit) keeps its permit, because that wait is on the system, not on a person.

### 1.2a Waiting on a person

A turn paused on a tool-approval prompt or a question card keeps its permit. Releasing it would let more than N turns keep model streams, MCP servers and tool processes alive at once, which is what the cap exists to bound, and a re-acquire after the answer could stall the turn while the agent's permission request is still outstanding. The cost is that N turns parked on prompts block a new send until one is answered, the bounded wait ends, or a parked turn is stopped. The waiting line names how many running turns are waiting on a person, so the person can answer or stop one.

### 1.3 Order

Waiters are served by priority, then arrival. A send a person typed, or a Resume pressed on one tab, is priority 0. A Resume from the bulk button (`via == RESUME_ALL_VIA` in #27512) is priority 1. This needs a small priority waiter queue in place of a bare `asyncio.Semaphore`; it sits behind the same acquire and release calls.

### 1.4 What the person sees

A waiting slot shows its existing queued state, with a line saying the turn starts when one of the running turns ends. The status payload gains `attended_turns: {cap, running, waiting}`.

### 1.5 Setting

`dashboard.max_attended_turns`: `auto` (default), `0` for off, or an integer from 1 to 64. `auto` is twice the effective `agent.cold_start_concurrency`, clamped to 2..64: 8 to 32 when cold-start concurrency is itself `auto` (its 4..16 range), and 2 to 64 for an explicit cold-start value (1..32). An explicit cap is clamped to 1..64. This is an open question for the maintainers (§7).

## 2. Alternatives considered

| Option | Result |
| --- | --- |
| Do nothing | A bulk Resume of tens of sessions runs every turn at once once starts end, as #25055 measured. |
| Pace inside #27512's `_resume_candidates` only | Fixes the button but not tab-by-tab Resumes or ordinary sends, and leaves a second limit in a second place. |
| Count attended turns in `_bg_turn_sema` | One pool, but unattended crews and people then compete for the same permits, and `max_background_turns` changes meaning. |
| Gate attended turns in `agent.admission_gate` | That gate defers by memory pressure, not by count, and its contract says chat turns are never gated. |
| Reuse Shared Crew fair admission (`gate_turn`) | It already counts active turns, but per principal and per Crew, and it is off by default. Turning it on to get a host cap changes Shared Crew behaviour for everyone; §1.2 nests the two instead. |
| Reject a turn over the cap | Loses the person's message or Resume. Queueing only starts it late. |

## 3. Interaction with #27512

#27512 adds the bulk Resume and does not change dispatch. This design does not depend on any code in #27512 and can land before or after it on main. The only link is the priority in §1.3, which reads #27512's `RESUME_ALL_VIA`. If this lands first, every Resume is priority 0 until #27512 adds the marker. #27512's module docstring, which says starts are paced by `_start_sem`, gets one sentence naming this cap when both are on main.

## 4. Migration plan

| Phase | PR | Exit criteria |
| --- | --- | --- |
| 1. Cap | one PR on main | With the cap at N, N+K attended turns started together run at most N at once and all K finish; turns started through `enqueue_or_run_prompt` are counted too; a turn waiting on a person keeps its permit; a wait past 1800 s ends with the cap message and keeps the person's message for resending; a nested turn reuses its outer turn's permit; a turn from a direct `_run_chat` producer (for example regenerate) is counted; an unattended slot takes no attended permit; `attended_turns` is in the status payload; `0` turns the cap off |
| 2. Priority | same PR, or a follow-up once #27512 merges | A typed send queued after bulk Resumes starts before them |

## 5. Test plan

One regression test in `test/test_turn_dispatch.py`: set the cap to 2, run `_run_chat` for 5 attended slots (3 through `spawn_guarded_turn`, 2 through `chat_regenerate`) whose coroutines each wait on an event, and assert that only 2 are inside the coroutine and 3 are counted as waiting. Then release the events and assert all 5 finish. On main all 5 enter at once, so the test fails. The same test repeats the count with turns started through `enqueue_or_run_prompt`, checks that a turn parked on an approval wait keeps its permit and that a waiting send starts only after the parked turn is answered or stopped, and checks that a nested `/prompts get` at cap 1 completes without waiting. Phase 2 adds one ordering assertion.

## 6. Backward compatibility

The cap only delays turns. Setting `dashboard.max_attended_turns: 0` restores today's behaviour exactly. No stored format changes.

## 7. Open questions

1. Is `2 × cold_start_concurrency` the right `auto` default, or should it read memory headroom, as `agent.admission_gate` does?
2. Should a crewmate's own turn that a person opened count as attended, as `_ChatSlot.unattended` says today?
3. With Shared Crew fair admission on, should the attended cap step aside so only one queue applies?

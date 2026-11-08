---
title: Provider throttle queue - queue and retry turns while the model provider throttles, instead of failing them
status: accepted
author: pearcekieser
created: 2026-11-03
last-audited: 2026-11-05
audited-at: 16ff876b5a
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Provider throttle queue

- Status: accepted. This is the product owner's decision, given 2026-11-03 by
  Pearce Kieser (@Pearcekieser). Nothing of it is on main. This document lands
  on its own first, because the implementation changes what a chat turn does
  when the provider throttles, and the First Principles lane reads that decision
  off the base.
- Narrowed on 2026-11-05 at review. bolichen97 asked for a v1 that gates
  dashboard chat turns only, ships off by default, and lists the call sites that
  move. Pearce confirmed the narrowing the same day. Every other prompt start
  keeps today's path (§4.4).
- Extends [rfc-overload-resilience.md](rfc-overload-resilience.md) §14.4 (the
  dependency coordinator) to dashboard chat turns. It does not edit that
  document.
- Claims about the code were checked at `16ff876b5a` (main).

## 1. Problem

When the model provider throttles, a chat turn ends on an error card
approximately 70 s after the first throttle. A throttle window of several minutes
outlasts that. A search of one host's dashboard chat transcripts for the `error`
rows with the two card texts below, over the 14 days to 2026-11-03, found 107
such failures:

- 99 turns ended on the throttle card ("The model provider is throttling
  requests" or "Bedrock is throttling requests"). 41 of them were on 2026-11-03.
- 8 turns ended on "The selected model is unavailable ... (capacity throttle or
  region rollout)".

Between 20:56Z and 21:09Z on 2026-11-03, 25 failures hit 18 sessions. Most of
those turns started together. A fleet orchestrator session sent them with
`session_send`, 5 to 8 in one minute.

A tool that counts these failures today must match the card text, because the
error row carries no structured kind for a throttle.

Today's behaviour, at `16ff876b5a`:

1. KAS retries a throttled request for approximately 6 s inside one attempt.
2. The chat ladder then re-sends the turn `_TRANSIENT_RETRIES = 3` times
   (`llm_helpers_parts/backend_errors.py`) on `throttle_retry_delay`. That curve
   starts at `THROTTLE_RETRY_BASE_DELAY = 6.0` s and doubles, with up to 25 %
   jitter. The waits add up to approximately 45 s. With the four KAS attempts,
   the turn ends approximately 70 s after the first throttle, on the throttle
   card with Resume.
3. Each session retries on its own clock. `_shared_dependency_delay` in
   `dashboard/chat_turn/recovery.py` floors the delay by the error's own stated
   reset and by the dependency coordinator's `retry_at` for the scope. A chat turn
   has no task row, so it never joins that schedule. A schedule exists only when
   a sub-agent, a TaskRunner step or a workflow agent opened one.
4. A cron turn's prompt goes through `stream_and_collect`
   (`llm_helpers_parts/turn_stream.py`). That loop re-sends a throttled prompt
   `_TRANSIENT_RETRIES = 3` times on `transient_retry_delay`, the 2 s curve, then
   tries the `agent.fallback_model` chain, then fails. The cron's own
   `_CRON_TRANSIENT_RETRIES = 2` (`slack/gateway_runtime/cron_dispatch.py`)
   covers only the steps before the prompt is sent.
5. A sub-agent with a task row parks on the `DependencyCoordinator` in
   `taskq/dependency.py`. It has one schedule per scope, one probe, then a ramp,
   a 3600 s deadline and 20 attempts. TaskRunner steps (`task_executor.py`) and
   workflow agents (`workflows/agent_pool.py`) park on it too.
6. Every caller classifies a throttle with no scope hint, and a throttle error
   names no model. So `scope_for` in `taskq/adapters/acp_provider.py` puts every
   throttle under `provider:acp`, and `record_provider_throttle` gets the same
   key.

So 18 sessions that the same provider throttles keep sending at the same time,
and each one fails on its own.

## 2. Goals

1. A throttle on a scope that is not already backing off opens a cool-off for
   it, whichever prompt start saw the throttle. In v1 the cool-off holds
   dashboard chat turns only. Every other prompt start keeps today's path (§4.4).
2. A dashboard chat turn that the provider throttles waits in a queue and is
   re-sent from it. It does not fail when its normal re-sends are spent. §4.3
   step 6 and §5 items 6 and 7 name the exceptions. While a
   cool-off is open, a new chat turn on that scope also waits. No turn is
   dropped, and no message runs twice.
3. The scope backs off exponentially with full jitter, up to a cap. A
   provider-stated Retry-After or reset time wins. A turn that waits past a
   give-up window ends on today's card with Resume.
4. When the provider recovers, the queue releases turns gradually by AIMD
   (additive increase, multiplicative decrease), in arrival order.
5. The chat shows that the turn is queued and when it retries next, with
   "Retry now" and "Cancel".
6. One config key turns all of this on. It is off by default, and off keeps
   today's retry paths and cards. The row fields of goal 7 are written either
   way (§8).
7. A throttle card row carries a structured error kind, the provider and the
   model, so a tool can read it without matching the card text.
8. While a scope recovers from a throttle, the chat turns that §4.5 counts as
   injected, such as `session_send` deliveries, start no faster than a set rate
   on that scope.

## 3. Non-goals

- Gating any prompt start other than the dashboard chat runner's in v1. Cron
  turns and the other `stream_and_collect` callers, `TurnDriver` channel turns,
  sub-agents, TaskRunner steps, workflow agents, compaction, `run_bg_oneliner`
  and direct callers keep today's retries, timeouts and errors (§4.4). Moving
  any of them is a later change with evidence of its own (open question 4).
- Changing the coordinator's park, its pacing or its `provider:acp` key.
- Turning the queue on by default in v1. The default is revisited once the §9
  evidence exists (open question 3).
- Changing provider credentials or regions.
- Choosing a fallback model on its own. The opt-in `agent.fallback_model` chain
  keeps its place (§5.3). An automatic fallback is a possible later step.
- The "selected model is unavailable" error. It keeps today's path. Adding it to
  the queue is open question 2.
- A usage limit or a spent allowance (#21679). It stays terminal and never
  queues.
- The open sub-agent bugs #25212 and #17439 in the coordinator's wake and its
  one-continue rule. The gate does not depend on those paths.
- Surviving a gateway restart. The gate is in memory (§5.5).

## 4. Design

### 4.1 One gate, one schedule per scope

A new in-memory `ProviderThrottleGate` in `src/kiro_crew/adaptive/` holds one
record per scope. A scope is one ACP backend and one model, written
`provider:<backend>:<model>`. If throttles are account-wide, every model on the
account opens its own scope, which costs one extra probe per model. If they are
per model, a wider key would hold back models that are not throttled. So the
narrow key is the safe default.

The scope comes from the ACP client at admission, before any error exists. The
backend is the client's ACP backend. The model is the model that the client
requests, so a session on `auto` uses `provider:<backend>:auto` for every start.
A fallback swap changes the requested model, and with it the scope.

Only the gate uses the narrow key in v1. No caller passes a scope hint to
`classify_exception`, gate on or off. So the coordinator schedule and
`record_provider_throttle` keep the `provider:acp` key of §1 point 6.

Admission sits where a turn's `session/prompt` request goes out, once for each
turn. There are two such places, one for each transport:
`AcpSessionHandle.prompt` (`acp/session_handle_turn.py`) for the shared runtime,
and `AcpClient._send_prompt` (`acp/client_turn.py`) for a direct client. Both
admit only a send that the chat runner marked. The chat runner marks the prompt
sends of its own turn, with the turn's typed bit (§4.5) and its give-up clock.
The mark covers only those sends. A helper call that the turn makes carries no
mark, whether it runs in a task of its own, such as a title or summary call, or
is awaited inline, such as the poison canary of `_on_acp_error_card`
(`dashboard/chat_turn/turn_failures.py`). The chat runner marks a turn only when
Shared Crew did not admit it and its slot is attended (§5 items 6 and 7). Every
other send at these two places is report-only (§4.4). It goes out at once, and
with the key on a throttle on it is reported to the gate.

The gate's states (cooling, probing and ramping), its window and its backoff
step `k` are defined in §4.2. Each admitted chat turn takes one in-flight unit,
and the window is the most units that a recovering scope lets out at once.

Time that a chat turn waits for admission does not count toward its prompt
timeout or toward any stall watchdog. The whole-turn ceiling,
`agent.chat_turn_timeout_secs`, still bounds the turn through its give-up
deadline (§4.3 step 5).

A start counts as accepted at the first model-output event of its own session:
text, a thought, a tool call, or the end of the turn with a stop reason that the
provider gave. A host-made end, such as a cancel, the turn ceiling or an `error:`
stop reason, counts as no output. A co-tenant event that the shared runtime
forwards (`runtime_global=True`) does not count, and neither does an MCP-server
or steer event. Acceptance is what grows the window (§4.2). An admitted chat turn
holds its unit until its turn ends, is throttled, fails or is cancelled, so a
long turn that streams output still counts against the window. A throttle is an
error that `acp_error_is_throttle` accepts. The ACP adapter maps it to
`rate_limited` or `concurrency_exceeded`.

The 429 rule from [dynamic-subagent-sizing](../../src/kiro_crew/docs/dynamic-subagent-sizing.md)
and `docs/system-specs/modules/adaptive-concurrency.md` stays. A chat throttle is
still reported to `record_provider_throttle`, and no host cap moves for it. The
gate is a per-provider channel only.

### 4.2 State machine

| State | Admits chat turns | Moves on |
|---|---|---|
| open (no record) | every one | a throttle: cooling, `k = 1` |
| cooling | none | `retry_at` passes, or Retry now on a waiter: probing |
| probing | one probe: the head of the gate queue, or the next chat turn when the queue is empty | the probe accepted: ramping, window 2, so one more turn can go while the probe's turn runs. The probe throttled: cooling, `k + 1`, or `k` unchanged when a Retry now started the probe. The probe ends with no output and no throttle (Stop, Cancel, a timeout or another error): the next head of the gate queue is the probe, `k` unchanged |
| ramping | while admitted chat turns in flight < window | each accepted turn: window + 1. A throttle on an admitted turn: window halved, rounded down. Window 0: cooling, `k + 1`. Window >= ramp ceiling and queue empty: open |

- A cool-off is full jitter. Its length is uniform in
  `[0, min(max_secs, base_secs * 2^(k-1))]`. A stated Retry-After or reset time
  replaces it when it is later.
- No event moves `retry_at` earlier while a scope cools. A later stated reset
  moves it later. For every gate scope, the coordinator's `retry_at` for
  `provider:acp`, the key of all its schedules in v1, is one more floor on the
  cool-off. Today's chat ladder floors its re-sends the same way (§1 point 3).
  A new chat turn on an open scope starts at once, as it does today.
- `k` goes up only when a turn that the gate released is throttled. It never
  goes up for each reporter, so 18 sessions that report one burst spend one step.
  This is the coordinator's rule (`DependencyCoordinator.report`).
- A throttle that a turn reports while the scope is cooling joins the cool-off.
- A report-only throttle (§4.4) opens a cool-off on an open scope, with `k = 1`,
  and joins a cooling one. During a probe or a ramp it changes nothing, because
  the window moves only on the outcomes of turns that the gate admitted. A
  report-only start's acceptance is not reported.
- Waiters are released in arrival order. Each waiter has a `(priority, seq)` sort
  key with priority 0. When the per-chat priority of #15543 lands, it can fill in
  the priority without a rewrite.
- A replay (§4.3) joins the gate's queue as a new arrival and keeps its turn's
  give-up clock. The one exception is a Retry now probe that the provider
  throttles, whose replay keeps the head (§6).
- A probing or ramping scope opens, and its record is dropped, when no admitted
  turn is in flight, its queue is empty, and nothing has throttled on it for
  `agent.throttle_queue_max_secs`. So a quiet host does not pace a later burst
  with the window of an old throttle.
- The give-up window is per waiter. It is measured from that turn's first
  throttle or from its first wait, whichever came first.

### 4.3 Chat turns

This section describes a chat turn that the chat runner marks (§4.1). An
unmarked chat turn keeps today's ladder, with its sleeps and its error card.

A marked chat turn has one wait point. That point is admission at dispatch,
before the prompt goes out.

1. The send that sees a throttle on a chat turn reports it to the gate. The
   ladder counts the
   re-send as it does today. The rewind of the throttled KAS attempt (#21876)
   runs, and then the throttled attempt ends and puts its replay at the head of
   the slot queue with no sleep.
2. The drain starts the replay, and its prompt waits for admission before it is
   sent. This is the same wait that a new turn on a cooling scope gets. So a
   re-send and a new send wait in the same gate queue. A slash command that the
   chat runner sends as prompt text (`stream_command`) takes the same path,
   because it goes through the same send. That includes a typed `/compact` that
   `AcpSessionHandle.prompt` sends as `_kiro/session/compact` on a KAS engine. A
   command that goes out on `_kiro.dev/commands/execute`, as most do on the kiro
   backend (`command_is_native`), sends no `session/prompt` and keeps today's
   path (the last row of §4.4).
3. The slot shows busy while the turn waits. A message sent to that session goes
   to the slot queue as it does today and runs once, after the waiting turn. A
   steer that the waiting turn does not consume becomes today's head card when
   that turn ends.
4. Stop and Cancel take today's Stop path to the provider's `cancel`
   (`AcpSessionProvider.cancel` in `acp/session_provider.py` and
   `AcpProvider.cancel` in `providers/acp_turns.py`). A waiting turn has no
   prompt out, so `session/cancel` has nothing to cancel. So `cancel` first ends
   the turn's admission wait, if it has one, and returns a new outcome,
   `dequeued`. Every caller that cancels a turn treats `dequeued` as a clean
   cancel that sent nothing. `stop_turn` (`session_lifecycle_parts/turn_stop.py`)
   answers it as a soft stop, as it answers `acked`. `cancel_owned_turn` in the
   same module, which the turn ceiling (`_owned_native_timeout_cancel` in
   `dashboard/turn_dispatch.py`) and a responsive crewmate chat's preempt
   (`dashboard/responsive_chat.py`) call, ends a wait and returns `dequeued`
   before it arms anything. Neither sets `prev_turn_cancelled`. No prompt went
   out, so the next turn in that session carries no cancelled-turn restore. The
   waiter leaves the gate and nothing is sent. After Stop or Cancel the turn
   ends as a stopped turn. A responsive crewmate chat answers `dequeued` by
   putting the waiting turn's message back in the slot queue behind the owner's
   message, so that text still runs once. Only Stop and Cancel drop a waiting
   turn. Nothing reads Stop only after a timer.
5. After `TRANSIENT_RETRIES` re-sends, the turn keeps re-sending through the
   gate queue and is not given up. Its give-up deadline is the earliest of three
   times. The first is the give-up window after the turn's first throttle or
   first wait. The second is the turn's carried ceiling deadline. The third is
   the ceiling deadline of the attempt that waits now. Each ceiling deadline is
   less `APPROVAL_TURN_MARGIN_SECS`, the margin that `tool_approval_timeout_secs`
   keeps for an approval wait. The chat runner takes an attempt's ceiling
   deadline from one of three places:
   - the deadline that `_bounded_turn` publishes in `_TURN_DEADLINE`
     (`dashboard/turn_dispatch.py`), from `agent.chat_turn_timeout_secs` or the
     session's own turn limit. `spawn_guarded_turn` and `bounded_chat_turn`
     start their turns through it.
   - the deadline of the `asyncio.wait_for` with which the OpenAI-compatible shim
     (`dashboard/openai_compat.py`) bounds `_run_chat`. The shim publishes
     nothing, so it hands that deadline to `_run_chat`, which uses it only for
     the gate.
   - for any other attempt, which runs with no turn ceiling today,
     `resolve_turn_limit(slot)` counted from the attempt's start. That is the
     ceiling that `spawn_guarded_turn` would give the attempt's replays anyway. A `session_send` delivery into an idle slot
     (`enqueue_or_run_prompt`), a Slack-linked inbound turn
     (`slack/handler_runtime/inbound.py`), a regenerate
     (`dashboard/chat_regenerate.py`) and a TaskRunner result turn
     (`dashboard/handlers/taskrunner.py`) start this way. The value adds no
     ceiling to the first attempt. It bounds that attempt's gate wait, and it
     becomes the carried ceiling of the turn's replays.

   The carried deadline is the first attempt's. Each replay carries that
   deadline, and `spawn_guarded_turn` (`dashboard/turn_dispatch.py`) gives a
   throttle replay only the ceiling time that its turn has left. So a replay
   never restarts the ceiling, and the gate gives up while the waiting attempt
   still has time to write its card. At the deadline the turn shows today's
   throttle card with Resume and the §6 fields.
6. A nested turn that `/prompts get` starts inside a chat turn, at
   `_prompt_depth > 0`, waits for admission like any chat turn. A throttle on it
   still ends it on today's transient notice, because a nested turn has no
   replay of its own. It is the one chat turn that does not re-send through the
   queue.

A turn that waits in the gate holds no attended-turn permit. It holds no Shared
Crew admission or background-turn permit either, because §5 items 6 and 7 keep
such turns out of the gate. The open RFC PR
[#28633](https://github.com/kirodotdev/KiroCrew/pull/28633), at status `draft`,
proposes such a permit. Whichever of the two lands second follows that rule.

### 4.4 Call sites in v1

Only the dashboard chat runner waits in the gate. Every other prompt start keeps
today's path. With the key on, when the provider throttles one of them, the
transport reports the throttle to the gate. That start never waits in the gate,
is never refused by it, and keeps its own re-sends, timeouts and errors. With the
key off, nothing is reported (§8).

| Code path | In v1 | What changes with the key on |
|---|---|---|
| Chat runner admission at dispatch: a turn of `_run_chat` (`dashboard/chat_runner.py`), sent by `AcpSessionHandle.prompt` or `AcpClient._send_prompt` | moves: waits in the gate | The prompt waits for admission before it goes out (§4.3 step 2), for a turn that Shared Crew did not admit and that runs on an attended slot (§5 items 6 and 7). A slash command sent as prompt text, a Resume, a Retry now and the continue of §5.2 are chat runner turns too |
| Every turn that `_run_chat` runs, whatever sent it: `session_send` and the other `session_deliver.deliver` origins, the dashboard nudge, workflow, TaskRunner and sub-agent results delivered into a chat, app-token and script-cron sends to `/api/chat` that reach an attended slot, and crewmate chats | moves: waits in the gate | Waits for admission like any chat turn. §4.5 says which of these count as injected |
| Chat runner throttle arm: `_on_acp_transient_retry` (`dashboard/chat_turn/acp_recovery.py`), reached from the error dispatch in `_run_chat` | moves: queues the replay | For a marked turn only, puts the replay at the head of the slot queue with no sleep, after the send has reported the throttle (§4.3 step 1). Once the fixed re-sends and any fallback swap are spent, a new branch in that dispatch routes a throttle that the gate recorded to this arm, ahead of the `_on_acp_error_card` fallthrough that ends the turn today (§4.3 step 5) |
| Provider cancel: `AcpSessionProvider.cancel` (`acp/session_provider.py`) and `AcpProvider.cancel` (`providers/acp_turns.py`) | moves: ends a wait | Ends the turn's admission wait before it looks for a turn in flight, and returns `dequeued` (§4.3 step 4) |
| Stop and owner-fenced cancel: `stop_turn` and `cancel_owned_turn` (`session_lifecycle_parts/turn_stop.py`), and the callers of the second, the turn ceiling `_owned_native_timeout_cancel` (`dashboard/turn_dispatch.py`) and the responsive crewmate preempt (`dashboard/responsive_chat.py`) | moves: new outcome | Treat `dequeued` as a clean cancel that sent nothing and leave `prev_turn_cancelled` unset. The responsive preempt puts the waiting turn's message back in the slot queue behind the owner's message (§4.3 step 4) |
| OpenAI-compatible shim: `dashboard/openai_compat.py` | moves: hands over its deadline | Passes the deadline of its `asyncio.wait_for` around `_run_chat` to the chat runner, for the gate's ceiling term only (§4.3 step 5) |
| Shared Crew turn admission: `gate_turn` (`shared_crew/turn_gate.py`) | moves: tells the body | Passes the turn body whether it admitted the turn, so the chat runner marks only an unadmitted turn (§5 item 6) |
| Unattended-turn cap: `run_background_turn` (`dashboard/state_parts/dashboard_services.py`) | stays | An unattended slot's turns carry no mark and are report-only (§5 item 7) |
| Replay dispatch: `spawn_guarded_turn` (`dashboard/turn_dispatch.py`) | moves: shorter ceiling | Gives a throttle replay only the ceiling time that its turn has left (§4.3 step 5) |
| Retry now: a new route, `POST /api/chat/slots/{slot}/throttle-retry-now` | moves: new | Moves one chat waiter to the head of the gate queue (§6). The owner only: it uses the owner check of the dashboard routes, `require_owner_dashboard_request` (`dashboard/handlers/_shared_access.py`), so it refuses an app token and any caller that is not the owner. With the key off no turn waits, so it finds no waiter and changes nothing |
| Chat runner fallback swap: `_on_acp_transient_exhausted` (`dashboard/chat_turn/acp_recovery.py`) | stays | No gate call of its own. It runs where it does today (§5.3), and the swapped turn's prompt waits at dispatch like any chat turn |
| `stream_and_collect` (`llm_helpers_parts/turn_stream.py`): cron turns, history consolidation, heartbeat turns, app and side callers, and the `retry_transient=False` callers (`nudge_channel_fire.py`, `runtime_charge.py`, the cron continuation in `cron_dispatch.py`) | stays: report-only | A throttle is reported to the gate. Its re-sends, its fallback chain and its failure stay as today |
| `TurnDriver.run` (`messaging/driver.py`): Slack, Telegram and the other channel turns | stays: report-only | A throttle is reported to the gate. Nothing else changes |
| Sub-agents (`subagent_manager/run_turn.py`), TaskRunner steps (`task_executor.py`) and workflow agents (`workflows/agent_pool.py`) | stays: report-only | A throttle is reported to the gate. Each still parks on the coordinator under `provider:acp` |
| Compaction, `run_bg_oneliner` (`llm_helpers_parts/background_turns.py`) callers such as titles and summaries, a helper task that a chat turn starts, and direct callers such as the auto-improvement runner and the code-review pool | stays: report-only | A throttle is reported to the gate. Nothing else changes |
| A steer, sent as a second `session/prompt` into a turn in flight (`_steer_via_concurrent_prompt`) or on `session/steering` | stays | Nothing. It goes out on a request of its own, not at the two admission places |
| A kiro-cli command on `_kiro.dev/commands/execute`, a typed `/compact` on a direct kiro client included, and a model switch or other session-config call | stays | Nothing. No `session/prompt` goes out |

A caller that the table does not name is report-only, because only the chat
runner sets the mark (§4.1). A test pins that an unmarked send never waits, with
the key on and its scope cooling.

A report-only row adds one report and no wait. Without it, a chat turn would
learn of a throttle only from its own send, so chat turns would keep starting into
a scope that a cron burst or a sub-agent fan-out has just found throttled.

### 4.5 Injected turns

A chat turn is typed when the chat runner gets `_directive_user_origin=True`.
`chat_send` sets it for the owner's own send, and the Resume route
(`dashboard/chat_api/continue_turn.py`) sets it for the owner's Resume press. A
Retry now, which only the owner can press (§4.4), also makes its turn typed.
Every other chat turn is injected. That covers `session_send` and every other
`session_deliver.deliver` origin, the dashboard nudge, workflow and TaskRunner
results, sub-agent completions, app-token sends to `/api/chat`, and a Resume
that an app token, `session_retry` or `session_continue` sends. A script cron that posts to `/api/chat` with no app claim counts as typed, because
`chat_send` treats it as the owner's send. The window still paces it. Because the
default is injected, a new path into the chat runner needs no entry in a list.

The gate's typed bit and `_directive_user_origin` are separate values, and the
typed bit grants nothing. When the drain merges a batch of queued entries, the
typed bit is set when any entry is typed. `_directive_user_origin` keeps its own
rule there and is set only when every entry has it. So a peer's text merged with
a typed message gains none of the owner's authority.

The chat runner passes the typed bit down with its mark (§4.1). The replay at
the head of the slot queue (§4.3), each re-send and each fallback attempt keep
the bit of the turn they repeat.

An injected turn waits in the same queue as every other chat turn. While its
scope cools, probes, or ramps with a window below the ramp ceiling, the gate also
admits at most `agent.throttle_queue_injected_starts_per_min` injected turns per
scope in any 60 s. The count is a sliding 60 s of admission times. A re-send of
an injected turn counts against the limit too.

- The limit check, the record of the admission time and the unit for that turn
  are one step under the scope's lock, so two turns cannot take the same place.
- An injected waiter that only the limit holds back does not block the waiters
  behind it. The next waiter that the window and the limit allow goes first, so a
  typed turn is not held behind a wave of injected ones.
- The gate sets one timer per scope for the earliest moment that frees a place:
  the oldest admission time leaving the 60 s, or the earliest give-up deadline.
  So a held-back waiter is never left without a wake.
- The limit ends when a ramping window reaches the ramp ceiling, and it applies
  again whenever a throttle halves the window below the ceiling. The scope's
  record stays, and every waiter, a held-back one included, is then released
  through the window in arrival order. The window keeps its +1 for each accepted
  turn, so a backlog leaves no faster than the window allows, and it cannot keep
  a healthy scope from opening. The scope opens, and its record is dropped, when
  the window is at or above the ceiling and the queue is empty (§4.2).
- The limit does not apply to an open scope, so a healthy provider sees no
  change. The first burst on an open scope goes out unpaced, as today. The first
  throttle from it opens a cool-off, and the limit then spreads the injected
  turns in that scope until its window reaches the ramp ceiling again.

When the limit holds an injected waiter back, the queued notice in that chat
names the limit as the reason.

## 5. Interaction with existing behaviour

1. `_shared_dependency_delay` stays for the gate-off path. With the gate on, the
   gate decides every chat turn's wait, and the coordinator's `retry_at` is a
   floor (§4.2).
2. A throttle after the turn streamed output keeps today's single continue
   (`_on_acp_posttoken_transient`). The send reports that throttle to the gate
   like any other, so it can halve a ramping window or open a cool-off. The
   continue is a chat turn of its own, so it waits for admission, and a throttle
   before its own output takes §4.3. It is not a throttle replay, so it gets a
   ceiling of its own, as it does today, and carries no deadline from the turn
   it continues.
3. `agent.fallback_model` keeps its place. When it is set, the chat ladder swaps
   after the normal re-sends, as it does today. The fallback model has its own
   scope. The queue applies when no swap applies or the chain is spent. The
   chain in `stream_and_collect` is unchanged (§4.4).
4. When a waiter ends (Stop, Cancel, a timeout or give-up), it leaves the queue
   and gives up any unit.
5. On restart the gate is empty. A chat turn that waited in it ends with today's
   restart card and Resume, like every other in-flight turn.
6. Shared Crew fair admission, `dashboard.shared_crew.admission_enabled`, is off
   by default. When it is on, `gate_turn` (`shared_crew/turn_gate.py`) admits
   each chat turn around the whole of `_run_chat`. A turn that waited in the
   gate would hold that admission for up to the give-up window, and could keep
   the owner's turns on a healthy scope out. So in v1 the chat runner sets its
   mark only on a turn that `gate_turn` ran unadmitted, through
   `_running_unadmitted`. `gate_turn` passes that answer to the turn body,
   because it clears the admitted-work mark before the body runs. A nested turn
   takes its parent's answer. The answer comes from this turn's own admission,
   so a turn admitted just before the key went off is still not marked. With fair admission on, a chat turn is
   report-only and keeps today's ladder (open question 5).
7. An unattended slot, one that an app owns such as an Issue Radar crew, runs
   each turn under a background-turn permit (`run_background_turn` in
   `dashboard/state_parts/dashboard_services.py`, at most
   `dashboard.max_background_turns` at once, 4 by default). A turn that waited
   in the gate would hold its permit, and the other unattended turns would wait
   up to 1800 s for one and then end without starting. So the chat runner sets
   no mark on an unattended slot in v1. Its turns are report-only and keep
   today's ladder (open question 5). A `session_send` delivery into a person's
   chat runs on an attended slot and is marked as usual.

## 6. What the user sees

- From a turn's first wait, its transcript row is the queued notice. It says that
  the model provider throttles requests, that the turn is queued, and the local
  time of the next try. A countdown counts down to that time. When the gate moves
  `retry_at`, the row updates in place.
- The row is a new `throttle_queued` token with a family of its own in the
  gateway-notice table (`website/src/pages/chat/gatewayNotice.ts`), in the
  `notice` role. The existing NoticeCard draws it, with two additions: the
  countdown and an action row. There is no new error card.
- A crewmate chat does not draw the queued row. The maintainer decision
  `docs/decisions/2026-10-09-crewmate-chat-draws-messages-only.md` keeps runtime
  notices out of that chat, and `isCrewmateChatRow` drops every `notice` row.
  The crewmate's Sessions tab shows the row, and the give-up card still draws in
  the crewmate chat.
- The row has two buttons. "Retry now" moves this turn to the head of the queue.
  While the scope cools, it also ends the cool-off for one probe, and this turn
  is that probe. While the scope probes or ramps, the turn goes when the next
  unit is free. If a Retry now probe is accepted, the scope starts to ramp for
  every waiter. If it is throttled, the turn stays at the head and `k` does not
  change. `retry_at` moves only for a later stated reset, as for any throttle
  (§4.2). So a click adds no backoff step for other sessions. "Cancel" is the
  existing Stop for this turn.
- At the give-up window the row becomes today's throttle card with Resume.
- The buttons are in the dashboard only. A linked channel gets the notice text.

The rows below carry the same structured fields in their `meta`: the queued row,
today's throttle card, and the "selected model is unavailable" card. The fields
are on each of these rows, whether a give-up wrote it or the gate-off ladder did.

| Key | Value |
|---|---|
| `error_kind` | `classify_provider_error(...).kind` for the error, so `throttle` or `model_unavailable` |
| `provider` | the ACP backend, for example `kas` |
| `model` | the requested model id |
| `scope` | the gate scope, `provider:<backend>:<model>` |
| `held_by` | on a give-up only: `injected_limit` when the limit of §4.5 held the turn last, else absent |

- `error_kind` is a new key. It does not replace `meta.kind`, which still selects
  the card and its fix. So no card changes because of it.
- The `error_kind` values are the provider error classifier's own names. The
  `model_unavailable` gateway notice keeps its own `notice` token, which is a
  different key on a different row.
- `error_kind` joins `RESERVED_ROW_META_KEYS` in `dashboard/chat_handlers.py`, so a
  caller of `/api/chat` cannot write it on a user row. A reader still checks that
  the row role is `error` or `notice`.

## 7. Config

All keys are read on each decision, so no restart is needed. Each key is
documented in `src/kiro_crew/docs/configuration.md`.

| Key | Default | Meaning |
|---|---|---|
| `agent.throttle_queue_enabled` | `false` | Turns the gate on. `false` keeps today's retry paths and cards (§8). |
| `agent.throttle_queue_base_secs` | `30` | Cool-off ceiling for the first step. It doubles for each failed release. |
| `agent.throttle_queue_max_secs` | `600` | Highest cool-off ceiling. A stated Retry-After can be longer. |
| `agent.throttle_queue_give_up_secs` | `2700` | Longest wait for one turn. Then it ends on today's card with Resume. A lower whole-turn ceiling ends it sooner (§4.3 step 5). |
| `agent.throttle_queue_ramp_ceiling` | `8` | Window at which the injected limit ends and a ramping scope with an empty queue opens again. |
| `agent.throttle_queue_injected_starts_per_min` | `2` | Most injected chat turns per scope in any 60 s while the scope cools, probes or ramps below the ramp ceiling (§4.5). `0` turns the limit off. |

v1 ships `agent.throttle_queue_enabled` off. Its default is revisited once the
§9 evidence is on the implementation PR (open question 3).

The AIMD steps are +1 for each accepted turn and x0.5 for each throttle. They
are part of the algorithm and are not config keys (open question 1).

## 8. When it is off

`agent.throttle_queue_enabled` is `false` by default. With it off, no start
waits in the gate, no throttle is reported to it, and it keeps no records. Every retry
path and card is the one on main before this change:

- A chat turn re-sends 3 times on the 6 s throttle curve, floored by
  `_shared_dependency_delay`, and then ends on the throttle card with Resume.
- No injected-start limit applies (§4.5).
- Every caller in a report-only row of §4.4 is on today's path. With the key
  on, the only difference is its report to the gate.

If the key turns off while turns wait, the gate admits every waiter at its next
decision and drops its records. A decision comes with the next prompt start at
either admission place, or with the scope's own timer, which fires at the
latest when its cool-off or a waiter's give-up deadline ends. Each waiter then runs on today's path. A parity test pins the
chat path with the key off, and one more test pins the switch while turns wait.
For each report-only row, a test pins that its sends, re-sends and errors are the
same with the key on as with it off.

The structured `meta` fields of §6 are written with the key on or off. They are
data on the row, and they change no card and no retry path. With the key off,
`scope` names the scope that the gate would use.

## 9. Verification

An isolated gateway runs a stub ACP backend that answers `ThrottlingException`
for a set time and then recovers. A Dev Fleet pod or the offline harness gateway
(`kiro_crew.testing.harness`) can host it. The runs that test the queue set
`agent.throttle_queue_enabled = true`, because the default is off. With N
concurrent dashboard chat sessions, the evidence must show these results:

1. The turns queue and do not fail.
2. The retries spread out.
3. The release is gradual.
4. No turn or message is lost or duplicated.
5. The key off, the default, gives today's failures.
6. A burst of `session_send` deliveries into N idle sessions during a cool-off
   starts no more than the injected limit allows in each minute, and a typed
   turn in another session is not held behind them. When the window reaches the
   ramp ceiling, the held-back turns leave through the window, not all at once.
7. Each throttle row carries `error_kind`, `provider`, `model` and `scope`.
8. A cron turn on the same backend and requested model as the chat sessions,
   which the stub throttles during the run, sends, re-sends and fails exactly as
   with the key off. Its throttle opens a cool-off that the chat turns then wait
   out.
9. Stop, and Cancel on the queued row, end a turn that waits for admission at
   once. Nothing is sent for it, and the next turn in that session carries no
   cancelled-turn restore. The same holds when the turn ceiling ends the wait.
   A responsive crewmate preempt puts the waiting turn's message back in the
   slot queue, and it runs once after the owner's message.
10. With Shared Crew fair admission on, a throttled chat turn takes today's path
    and never waits in the gate.
11. With a whole-turn ceiling shorter than the throttle, a queued turn and its
    replays end before the first attempt's ceiling deadline, on the throttle
    card with the §6 fields, and no turn-limit card follows.
12. A turn on an unattended slot takes today's path and never waits in the
    gate.

The evidence goes on the implementation PR.

## 10. Alternatives considered

1. Make the chat ladder longer only, for example 10 re-sends. Each session would
   still retry on its own clock, so the burst stays a burst. Rejected.
2. Put chat turns on the `DependencyCoordinator`. The coordinator writes task-row
   events for each wait, and a chat turn has no row. A row for each chat turn is a
   much larger change to the task store. The coordinator also releases in
   capacity-sized batches, not by AIMD. Rejected for now. The gate reads the
   coordinator's `retry_at` as a floor on a cool-off (§4.2), as today's chat
   ladder does for its re-sends.
3. Sleep inside the throttle arm, as today, for the whole wait. A timer that
   reads Stop only when it returns holds a stopped turn for up to the give-up
   window, and the arm would be a second wait point beside dispatch. Rejected.
4. End the turn and park the message in the slot queue with a hold, as the
   sign-in hold does. An idle slot with held entries needs its own drain timer,
   and a new send to an idle slot would pass the held entry. Rejected.
5. Gate every prompt start in v1. The first draft of this document did that, with
   a park mode for the callers that the coordinator parks. Review narrowed v1 to
   the dashboard chat runner, so that the change and its risk sit on one path,
   behind a key that is off by default. Rejected for v1. Open question 4 keeps
   the other callers in view.

## 11. Open questions

1. Must the AIMD steps (+1, x0.5) be config keys too? This document keeps them
   fixed.
2. Must "the selected model is unavailable (capacity throttle or region
   rollout)" queue too? It was 8 of the 107 failures in §1. It keeps today's path
   until a decision.
3. When must `agent.throttle_queue_enabled` default to `true`? v1 ships it off.
   The question stays open until the §9 evidence exists and a maintainer has
   read it.
4. Which callers in the report-only rows of §4.4 must wait in the gate after v1,
   and in what order? Each keeps today's path until a change of its own moves it,
   with its own evidence.
5. Must a chat turn wait in the gate while Shared Crew fair admission is on, or
   on an unattended slot? Both need the wait to give back what the turn holds,
   the Shared Crew admission or the background-turn permit, and take it again
   when the gate admits the turn. v1 keeps such turns on today's path (§5 items
   6 and 7).

---
title: Agent-started turns — show a turn the agent starts between user turns as its own reply
status: accepted
author: iamwhatever
created: 2026-10-08
last-audited: 2026-10-08
audited-at: 1ad2d6d05f
doc-pr: 18051
implementation-prs: []
tracking-issues: [17566]
supersedes: []
superseded-by: []
---

# RFC: Agent-started turns — show a turn the agent starts between user turns as its own reply

- Status: accepted. The product owner chose option A on all four [decisions](#decisions-needed) on 2026-10-08: an agent-started turn is its own assistant message with a notification, claude only, a user message queues behind it, and every other backend keeps today's behaviour. Phases 0 to 2 are unlocked; Phase 3 stays blocked. Phase 0's verdicts are recorded under [Phase 0](#phase-0--probe-no-product-code). Nothing is implemented on main yet.
- Author: iamwhatever
- Design credit: the reader hand-off, the prompt-less turn through `_run_chat` and the end-of-turn signal are rinbn's [design note on #17566](https://github.com/kirodotdev/KiroCrew/issues/17566). This document writes that note up and adds what review of [#17884](https://github.com/kirodotdev/KiroCrew/pull/17884) found a new path must keep.
- Related: `docs/system-specs/modules/acp-client.md` (stdout, the replay buffer, cancellation), `docs/system-specs/modules/app-notifications.md` (unread badge, turn sound)

Code facts are measured at `1ad2d6d05f`.

## Summary

On the `claude` backend, Claude Code can start a turn by itself after the dashboard turn ends: a `run_in_background` command exits, the model gets a task notification, and it answers. claude-agent-acp writes that turn to stdout. Kiro Crew has no turn open, so nobody reads it, and the next user message reads it as its own: the old reply and tool rows show under the new message, stamped late, with no notification.

This RFC adds an **agent-started turn**: a reader that watches stdout between turns, and a turn with no prompt that runs through the same `_run_chat` and the same `_dispatch_events` read loop as a user turn. The agent's reply becomes its own assistant message, saved and redacted like any other, with the normal end-of-turn notification.

## Motivation

### Repro

1. Set `agent.acp_backend = claude`.
2. Send "run `sleep 60; echo done` in the background and tell me when it finishes". The turn ends.
3. Wait two minutes. Nothing appears.
4. Send "status?". The reply to step 2's background task ("it finished, output: done") and its tool rows render under "status?", stamped now, followed by the answer to "status?".

### Current frame flow

All readers below are `AcpClient` methods in `src/kiro_crew/acp/client.py`.

| When | Who reads stdout | What happens to a frame |
|---|---|---|
| Turn in flight | `_prompt_loop`, under `_turn_lock` | Classified by `_process_message`, dispatched by `_dispatch_events` |
| Command or init waits | `_wait_for_response`, `_drain_notifications` | Read outside `_turn_lock`; foreign frames go back to `_buffer` |
| Compaction | `wait_for_compaction`, `_drain_post_compaction_metadata` | Read outside `_turn_lock` |
| Between turns | nobody | Frames sit in the pipe; the next `_prompt_loop` takes `_buffer` first (`_read_message`), then reads them as this turn's |

Those five sites are every caller of `_read_message` in `client.py`. `_process_message` classifies any `session/update` as `"update"` with no turn attribution, and nothing on main reads `_meta["_claude/origin"]`.

`AcpSessionHandle` backends (kiro, KAS, codex; `ACP_BACKENDS_ACP_RUNTIME`, `src/kiro_crew/agent_sdk/backends.py`) do not misplace these frames: the pre-turn drain in `AcpSessionHandle.prompt()` (`src/kiro_crew/acp/session_handle.py`) discards them, counted in one warning, never shown. `AcpClient` backends (`ACP_BACKENDS_ACP_CLIENT_SPAWNABLE`, `backends.py`) have no drain.

### Why dropping is not enough

[#17884](https://github.com/kirodotdev/KiroCrew/pull/17884) gave `AcpClient` a claude-only drain. Its review rounds showed that every frame type other than text carries a side effect the read loop runs: the SEL tool-invocation audit (`_maybe_audit_tool_call`, `client.py`), PreToolUse / PostToolUse hooks (`_maybe_fire_pre_tool_hooks`, `_maybe_fire_post_tool_hooks`), the per-harness tripwires, permission answers, MCP OAuth and init notices, config-option updates, usage tracking (`_track_usage_update`, `client.py`), background-launch tracking (`_note_background_launch`, `client.py`), the harness parity gate, and the 100-entry replay buffer bound (`AcpClient._buffer`). Its final form dropped only text chunks and left every other frame to the next turn. The [self-review](https://github.com/kirodotdev/KiroCrew/pull/17884#issuecomment-6052833639) rejected that: the model's reply is lost while its tool rows still render, and Claude's own session holds a reply the user never saw. #17884 is closed in favour of this RFC.

The lesson that shapes this design: a second reader that interprets frames has to re-implement the read loop. The reader proposed here interprets nothing. It only notices that a frame arrived and hands stdout to a real turn.

## Goals

1. An agent-started turn is shown and saved like any turn: its own assistant message, timestamped when produced, persisted, redacted, and replayed on resume. This is the first acceptance criterion.
2. Every side effect a user turn's frames get, an agent-started turn's frames get, because they go through the same `_dispatch_events`.
3. The next user message shows only its own answer.
4. The slot counts as busy while the agent-started turn runs, so RSS recycle and the idle sweep leave it alone.
5. The user is told: the normal end-of-turn notification fires.

## Non-goals

- Changing `AcpSessionHandle`'s pre-turn drain. It stays as is for kiro, KAS and codex (see decision 4).
- Letting the agent start a turn on an idle session that has no live process. A recycled or swept session has no stdout to watch.
- Any new frontend component. The turn renders with the existing assistant and tool rows; a small "started by the agent" label is the only visible addition.

## Design

### 1. Idle reader in `AcpClient`

When a read turn ends (`_prompt_loop`'s `finally`), and the backend is in the agent-started set (claude, see decision 2), `AcpClient` starts one background task, the idle reader. It acquires `_turn_lock` and awaits one line from stdout.

Every other stdout reader stops it first. A new `_claim_stdout()` cancels the idle reader and awaits its exit before the caller reads. All five read sites call it: `_prompt_loop`, `_wait_for_response`, `_drain_notifications`, `wait_for_compaction` and `_drain_post_compaction_metadata`. A test enumerates `_read_message` callers so a sixth site cannot skip it.

Cancelling the reader inside `readline()` loses no bytes: `asyncio.StreamReader.readuntil` removes data from its buffer only once the separator is found, so a cancelled wait leaves a partial line in place for the next reader. Phase 1 pins this with a test through a real `StreamReader`.

When the reader gets a line, it parses it and puts the frame back at the front of `_buffer`. Then:

- A frame that shows the agent acting is the start of an agent-started turn: a `session/update` of kind `agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update` or `plan`, or a server request (`session/request_permission`, or any request with an id). The reader calls `on_agent_turn(client)`, which the dashboard registers, and exits. It never dispatches the frame itself.
- Any other frame stays in `_buffer` for the next reader, as on main, and the reader reads again while `_buffer` has room. That covers the notifications (metadata, MCP init, config option) and the `session/update` kinds that report state, not activity. Phase 0 saw two such frames between turns: a `session_info_update` (the session title) about a second after a session's first turn, and a lone origin-marked `usage_update` right after a turn that waited for a subagent. A turn started on either would be an empty reply with a notification. When `_buffer` is full the reader exits and leaves the rest in the pipe in order.

The reader holds `_turn_lock` until the callback has marked the slot busy (step 2), so no user turn can start reading in between.

### 2. The agent-started turn in the dashboard

`on_agent_turn` runs synchronously on the event loop. It claims the slot the way a dispatched user turn does (compare `_start_next_queued_turn`, `src/kiro_crew/dashboard/chat_runner.py`): it sets `slot.running` and schedules `_run_chat` with a new `agent_started=True` flag and no message. `_run_chat` then holds the per-session semaphore for the turn, as every turn does. Only after `slot.running` is set does the reader release `_turn_lock`.

If `slot.running` is already set, the callback starts nothing and the frame stays in `_buffer`. Two kinds of turn can hold the slot at that moment, and the rule has to serve both:

- A user turn that has claimed the slot but not yet called `_claim_stdout()`, because it still waits on the semaphore. It reads the frame from `_buffer` when it starts, as on main. §6 lists this window.
- The previous turn, which has stopped reading stdout but has not released the slot yet, because its turn-end bookkeeping still runs. Nothing reads the frame until the next user message, which is the bug this RFC fixes.

So the callback also registers a retry for when the task that holds the slot ends. `AcpClient` marks a hand-off as pending, and the next `_prompt_loop` clears the mark when it takes the frames from `_buffer`. The retry checks the mark: if a user turn took the frames, it does nothing; otherwise it starts the agent-started turn.

With `agent_started=True`, `_run_chat`:

- appends no user row and sends nothing to the agent;
- takes its events from `client.stream_unsolicited()` instead of `client.stream(...)`;
- writes `turn/started` to the crew log with an `agent` actor, so the turn is not counted as a user prompt;
- runs every other step a user turn runs: tool rows, permission cards, redaction, persistence, crew-log steps, the turn-end bookkeeping and the queue hand-off.

`AcpClient.stream_unsolicited()` calls `_dispatch_events` with a request id no response can match. `_dispatch_events` acquires `_turn_lock` through `_prompt_loop`, so the reader hand-off and the turn's read follow the same lock discipline as a user turn. The turn ends on the first of:

- a `usage_update` whose `_meta["_claude/origin"].kind` is in the adapter's autonomous set: `task-notification`, `peer`, `coordinator`, `observer` and `observer-activity`. A user turn's closing `usage_update` carries an origin too, `{"kind": "human"}`, so the test is membership in that set, never the presence of `_claude/origin`. Only the agent-started turn ends on such a frame; a user turn that reads one, such as the lone one after a held turn (§1), treats it as a usage frame. Phase 0 saw it end every autonomous cycle, a cancelled one included. The adapter's source skips it when no assistant message set the cycle's usage, and such a cycle ends on the gates below;
- process death (the normal `AcpProcessDied` path);
- the stale-turn and timeout gates a user turn already has (`acp-client.md`, "Stale-turn gate");
- a user Stop (see Cancel).

A turn that ends on a timeout gets the same synthetic completion a user turn gets, so the slot never stays busy.

### 3. Persistence and redaction

The assistant row and its tool rows are written by the same code a user turn uses, so `_redact_tool_field` (`src/kiro_crew/dashboard/chat_utils.py`) and assistant-text redaction apply unchanged. The assistant row carries `meta.origin = {"kind": "agent", "reason": <origin kind>}`; the reason is drawn from a closed set and any other value is stored as `other`, so adapter text never reaches the row. Only `kind` is read from the origin object: a `peer` origin carries a subagent's report as free text in `body` (Phase 0). The history replay on resume includes the row as an ordinary assistant turn, so the transcript and Claude's own session agree.

### 4. Notifications

The turn ends through the normal turn-end path, so the unread badge and the turn sound fire the way they do for a finished user turn (`app-notifications.md`, "Unread badge" and "Sound events"). `_notify_autonudge_turn_end` (`chat_runner.py`) is skipped for an agent-started turn: a monitor or autonudge loop did not send its prompt, so the turn must not count as one of its cycles.

### 5. Slot busy accounting

The turn holds the session semaphore for its whole run, like any turn. RSS recycle (`_rss_threshold_check` in `src/kiro_crew/session_cleanup.py`) and the idle sweep (`_expire_idle`, same file) both skip a session whose semaphore is held, so neither can kill it mid-turn. The turn also refreshes `last_used`, so the idle timeout restarts after it.

The gap before the agent-started turn begins is already covered on claude: the harness background hold in `_rss_threshold_check` (`HARNESS_BACKGROUND_WORK_HOLD_SECS`, one hour) keeps a session whose harness launched background work recently.

### 6. A user message during the agent-started turn

Per decision 3 (recommended: queue), a message sent while the agent-started turn runs takes the existing queued path, because `slot.running` is set. It runs when the agent-started turn ends. A message whose prompt was already written before the reader saw the first frame is the one case this design does not attribute. So is a user turn that claimed the slot before the frame arrived (§2). In both, that turn reads the agent-started frames as its own. Phase 0 found the adapter does not interleave the two: it queues the prompt behind the running cycle, so the cycle's frames arrive first and end with its origin `usage_update`, and the prompt's own reply follows.

### 7. Cancel and interrupt

Stop on an agent-started turn sends `session/cancel` for the session, as Stop on a user turn does, then runs the normal cancel grace window (`acp-client.md`, "Cancel Grace Window"). Phase 0 found that the adapter honours that cancel: the cycle stops and still ends with its origin `usage_update`, so the turn ends on its normal end condition. If a cancel is ignored, the grace window ends the Kiro Crew turn locally, and frames that arrive afterwards start a new agent-started turn instead of leaking into the next user turn.

### 8. What stays

The `AcpSessionHandle` drain is untouched. On `AcpClient` backends outside the agent-started set, between-turn frames keep reaching the next turn's read loop exactly as on main.

## Migration plan

### Phase 0 — probe (no product code)

A script drives claude-agent-acp directly over stdio with the repro and records every frame. Exit criteria, written into this document:

- the exact `_meta["_claude/origin"]` shape on the closing `usage_update`, and the closed set of `kind` values;
- whether a `session/cancel` stops an agent-started turn;
- what happens when a `session/prompt` is written while an agent-started turn streams (queued by the adapter, interleaved, or rejected).

**Verdicts, 2026-10-08.** Measured on claude-agent-acp 0.87.0 (claude-agent-sdk 0.3.287), with Claude Code on Bedrock and the `initialize` capabilities `AcpClient` sends. Four runs: a background command, a Stop during the agent-started turn, a prompt during it, and a background subagent. Only frame structure is recorded here.

- **Origin shape.** The closing `usage_update` carries `_meta["_claude/origin"]`, an object whose `kind` names the origin, plus fields that depend on the kind:
  - after a background command: `{"kind": "task-notification", "producer": "session-task", "runId": ...}`;
  - when a subagent hands back: `{"kind": "peer", "from": ..., "senderTaskId": ..., "name": ..., "body": ..., "handback": true}`, with the subagent's report as text in `body`;
  - on every user turn: `{"kind": "human"}`.

  The adapter's autonomous set is `task-notification`, `peer`, `coordinator`, `observer` and `observer-activity` (`AUTONOMOUS_RESULT_ORIGINS` in `dist/acp-agent.js`). Its source sends that `usage_update` only when an assistant message set the cycle's usage; every cycle in the four runs had one.
- **Cancel.** A `session/cancel` sent at the first chunk of an agent-started turn stopped it. The last chunk and the origin `usage_update` arrived within about 10 ms, and no response followed, since there is no request to answer. The next prompt ran normally.
- **Prompt during the turn.** Queued by the adapter, which advertises `agentCapabilities._meta.claudeCode.promptQueueing: true` in `initialize`. The agent-started cycle streamed to its end and its origin `usage_update`; then came the prompt's reply, its `{"kind": "human"}` `usage_update` and its response. Nothing interleaved and nothing was rejected.
- **Background subagent.** A turn that starts a background subagent does not end when its text ends: the adapter holds the `session/prompt` response until the subagent hands back (`settleOrDefer` in `dist/acp-agent.js`). The subagent's tool calls and the model's follow-up streamed inside that open turn, so none of them arrived between turns. A permission request from such a subagent was not exercised: the probe session ran in Claude Code's `auto` mode, which sent none.
- **Frames between turns that start no turn.** A `session_info_update` with the session title arrived about a second after the first turn of every run, and a lone `usage_update` with a `task-notification` origin arrived right after the held turn's response. §1 leaves both in `_buffer`.

To rerun: send `initialize` and `session/new`, then a prompt asking for `sleep 15; echo probe-done` with `run_in_background`; answer each `session/request_permission` with its first allow option; read stdout for two minutes. For the Stop and prompt runs, ask for a long reply when the command finishes and send `session/cancel` or a second `session/prompt` at its first `agent_message_chunk`.

### Phase 1 — transport (`AcpClient` only, no visible change)

Idle reader, `_claim_stdout()` at all five read sites, `stream_unsolicited()`. With no `on_agent_turn` registered, the reader puts the frame back and exits, so behaviour matches main. Exit criteria:

- a cancel inside `readline()` loses no bytes (real `StreamReader`, partial line);
- each of the five read sites stops the reader before it reads; a caller-enumeration test fails if a new `_read_message` caller skips `_claim_stdout()`;
- `stream_unsolicited()` yields the frames of a fake agent-started turn and ends on the origin `usage_update`, on process death and on the timeout gate;
- an origin of kind `human` ends no agent-started turn, and a `session_info_update` or a lone origin `usage_update` between turns starts none;
- `check_harness_parity.py` passes: the reader adds no enforcement role, because it dispatches nothing.

### Phase 2 — dashboard wiring (claude)

`on_agent_turn`, `_run_chat(agent_started=True)`, the origin label, the notification skip for autonudge. Exit criteria:

- the repro: the "it finished" reply appears on its own within seconds of the background command ending, timestamped then, with the unread badge; the next "status?" bubble holds only its own answer;
- the agent-started turn's tool call writes one SEL record, fires PreToolUse / PostToolUse hooks, and a permission request in it shows a normal permission card;
- tool rows and assistant text are redacted (a credential-shaped string in a fake frame is stored redacted);
- the assistant row is in the resume history replay;
- RSS recycle and the idle sweep skip the session while the turn runs;
- a user message sent during the turn queues and runs after it;
- the two interleavings in §2: a frame that arrives while a user turn has claimed the slot but waits on the semaphore is read by that turn and starts no second turn; a frame that arrives while the previous turn is still tearing down starts the agent-started turn when that turn's task ends;
- Stop ends the turn within the cancel grace window;
- the turn does not count as an autonudge or monitor cycle.

### Phase 3 — other backends (blocked on decision 4)

Each further backend joins the agent-started set only once its adapter marks the end of a turn it started, with its own Phase 0 probe.

## Test plan

Unit tests live beside the existing suites: `test/test_acp_client.py` for Phase 1 (fake stdout through a real `asyncio.StreamReader`), and a chat-runner suite for Phase 2 that drives `_run_chat(agent_started=True)` with a fake client. Phase 2 also adds one browser-harness frame showing the agent-started reply with its label. Manual verification is the repro above on a live claude backend, recorded in the Phase 2 PR.

## Backward compatibility

No stored-data migration. A row without `meta.origin` is a user-prompted turn, as every row on main is. Phase 2 adds one config key, `agent.agent_started_turns` (default `true`), as a kill switch: with it off, the reader is never started and the claude backend behaves as main does.

## Security considerations

- No new endpoint and no new way to run a tool. Every tool call in an agent-started turn goes through the same `session/request_permission` flow, approval floor and deny sets as a user turn, and is SEL-audited the same way.
- The reader parses one frame and branches on its method name only. It never logs frame content.
- `meta.origin.reason` is from a closed set, so adapter text cannot reach a stored field through it.
- The agent-started turn runs under the session's own trust and identity; it opens no session and borrows no other session's grant.

## Alternatives considered

- **Drop between-turn frames (#17884).** Loses the model's reply while its tool rows still render, and every non-text frame needs its side effect re-implemented. Rejected in self-review.
- **A separate renderer for between-turn frames.** Duplicates redaction, persistence, audits and hooks. Rejected in rinbn's note for the same reason.
- **Attach the frames to the previous turn.** That turn has closed, its notification has fired and its row is persisted; reopening it breaks the turn-end bookkeeping and still tells the user nothing.

## Decisions needed

All four are decided (product owner, 2026-10-08). Each table is kept as the record of what was weighed.

### 1. How an agent-started turn is shown

| Option | What the user sees |
|---|---|
| A. Own assistant message + notification | The reply appears when produced, badge and sound fire |
| B. Hidden (drop and count) | Nothing; Claude remembers a reply the user never saw |

Recommendation: **A**. B is what #17884 shipped and review rejected; it leaves the transcript and the model disagreeing.

**Decided: A.** An agent-started turn shows as its own assistant message, and the normal end-of-turn notification fires.

### 2. Scope

| Option | Cost |
|---|---|
| A. claude only | One backend, one end signal (`_claude/origin`) |
| B. Backend-neutral now | Needs an end signal no other adapter sends; a quiet-timeout guess would end real turns early |

Recommendation: **A**, built so a backend joins by naming its end signal (Phase 3).

**Decided: A.** The claude backend only, because its adapter is the one that marks the end of such a turn.

### 3. A user message during the agent-started turn

| Option | Effect |
|---|---|
| A. Queue behind it | Same as a message sent during any turn; the agent finishes its reply first |
| B. Interrupt it | Sends `session/cancel`, then the user's prompt; the background reply may be cut |

Recommendation: **A**. It is how the dashboard treats every running turn, and the user can still press Stop. Claude Code's own behaviour is unchecked; Phase 0 records it, and this decision is revisited if Claude itself interrupts.

**Decided: A.** A user message sent while the agent-started turn streams queues behind it; nothing interrupts it.

### 4. kiro, KAS, codex and the other `AcpClient` backends

| Option | Effect |
|---|---|
| A. Keep today's behaviour, adopt per backend later | Runtime backends keep dropping (counted); other `AcpClient` backends keep passing frames to the next turn |
| B. Same treatment now | Blocked on decision 2 B |

Recommendation: **A**. No other adapter is known to start turns today, and none marks the end of one.

**Decided: A.** Other backends keep today's behaviour; Phase 3 stays blocked.

## Acceptance

Accepted 2026-10-08: the product owner chose A on each of the four decisions above, recorded by iamwhatever. Phases 0 to 2 follow for the claude backend; Phase 3 waits for another adapter to mark the end of a turn it starts.

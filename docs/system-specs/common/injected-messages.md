# Injected messages

Some messages in a session were not typed by a human. Automation injects them:
a cron job reporting, a sub-agent finishing, the runner recovering a broken turn,
a nudge loop poking an idle slot. They arrive on the same queue as user input, so
they need a marker the model and the frontend can both recognise.

**The user may not be present.** Process the envelope and act; do not answer it as
though someone is waiting for a conversational reply.

Every prefix is defined once, in `src/kiro_crew/dashboard/state.py`, so the
frontend has one list to mirror and no second copy can drift. Classification is by
`str.startswith` on the resolved prefix, never by a loose regex.

## Cron notification

A cron job called `send_message(session="origin")` and the origin dashboard slot
was reachable. `dashboard/handlers/messaging.py` wraps the text:

```
[Cron notification from "<job name>"]
<content from the cron agent>
[End of cron notification]
```

- Prefix `CRON_NOTIFY_PREFIX = '[Cron notification from '`, terminator
  `CRON_NOTIFY_END = '[End of cron notification]'`. The job label sits between a
  literal `"` pair and the closing `]`; `CRON_NOTIFY_RE` extracts it, falling back
  to `"cron"` when the label is unparseable.
- The label, the text and the title are all redacted (exfiltration URLs, then
  credentials) before the wrapper is built.
- The runner appends it to the slot with role `inject` and a `cronLabel` meta
  entry, so the dashboard renders a compact clock chip instead of echoing the
  wrapper. The text wrapper stays in `content` because that is what the model
  reads.
- If the slot is mid-turn the message is queued as `queued` and drained later; a
  queue at capacity evicts its oldest entry rather than growing without bound. An
  idle slot instead gets an immediate guarded turn.
- When the origin slot is not in memory it is rehydrated from history. A session
  that is genuinely gone (never persisted, deleted, or closed) resolves to nothing
  and delivery falls back to a dashboard notification (plus a Slack DM when the
  caller asked for one), with `(session closed)` appended to the text. No phantom
  empty tab is ever created.

**How to treat it:** do the work it implies. If a cron reports a build failure,
fix the build. There is nobody to ask.

## Sub-agent completion

A background sub-agent finished. `slack/gateway.py` builds the envelope on the
single completion path that serves every terminal outcome:

```
[Subagent completion event]
Agent `<id>` (<agent name>) <status> <emoji>
Task: <first 100 chars of the task>

<result detail>
```

- Prefix `SUBAGENT_COMPLETION_PREFIX = '[Subagent completion event]'`.
- `<status> <emoji>` is one of `completed ✅`, `failed ❌`, or `stopped by user ⏹`.
  The agent-name parenthetical is present only when the sub-agent ran under a named
  agent.
- The detail is the trimmed result when it fits. When the completion copy dropped
  content, or in orchestrator mode, it is a summary plus a `result_path` pointer, so
  the parent reads the full transcript on demand (`read`, `grep`, `spawn_status`)
  instead of re-running the sub-agent.
- A user-stopped agent says so explicitly and instructs the parent not to treat the
  partial output as a finished result or retry it unprompted.
- The runner appends it with role `subagent`, so it renders as its own message kind
  rather than a user bubble.
- Orchestration guards append to the same envelope when a stage has burned its
  spawn-round budget, telling the parent to stop spawning and ask the user.

**How to treat it:** wait for it rather than polling, then synthesize. After
`spawn_run` the turn is over: continuing to work in the same turn duplicates and
races the sub-agents. Your reply is what the user sees, so fold the results into it
rather than pasting them.

When every agent in a fan-out has completed and each result has been processed, one
further synthesis turn is fired, prefixed `SUBAGENT_SYNTHESIS_PREFIX = '[SYSTEM]
Sub-agent synthesis:'`. Its visible reply is the consolidated, user-facing summary,
so treat it as the deliverable: restate the goal, synthesize across the agents
rather than repeating each in turn, and give concrete next actions.

## Sub-agent delivery failure

The sub-agent completed but injecting its result into the parent session timed out.
`subagent.py` builds:

```
[Subagent completion event]
Agent `<id>` ❌ <reason>
Task: <first 100 chars of the task>
The agent finished but result delivery timed out.
Result saved at: <path> (<n> bytes)
Use the read tool to retrieve it if needed.
```

The result-path lines are present only when a result file exists. **The result is
on disk**, so use the `read` tool to retrieve it rather than re-running the work.

Two adjacent variants exist for a gateway restart, same prefix:

- `⚠️ orphaned by gateway restart` plus `Result saved at: <path>` and
  `Use the read tool to retrieve it.`
- `❌ lost to gateway restart` plus `No result was captured before the restart.`

All three are redacted before any delivery path. When the parent has no open
dashboard surface, undelivered notices are batched into a single digest DM rather
than N pings.

## Automatic recovery continuations

The runner injects a synthetic continuation when a turn ended for a system reason
rather than because the model was done. Each has its own prefix in
`dashboard/state.py`, each renders as an `inject` message (not a user bubble), and
none is mirrored to a linked Slack or Telegram thread as though the user typed it:

| Prefix | Fired when |
|---|---|
| `[Tool refusal — automatic recovery]` | A tool call was refused for a recoverable system reason (a host-gate policy deny, or the read-only bash gate) and the turn ended early. Carries the reason back so the model can adapt instead of stalling for the user. |
| `[Stalled turn — automatic recovery]` | A genuinely wedged turn was detected and reset. Tells the model the interruption was a system stall, NOT the user, and to resume from its last committed step rather than restart. |
| `[Tool stall — automatic recovery]` | The per-session watchdog judged an in-flight tool dead and cancelled the session. Hands over the stall context so the model can check partial results and continue. |
| `[Interrupted turn — automatic recovery]` | A transient backend 5xx cut a turn short after tokens or tool calls had already streamed. |
| `[Empty response — automatic recovery]` | The model returned no output twice. Continue the pending request; do not restart from scratch or re-run steps that already succeeded. |

The recovery classification for the last two is **structural**: the queue entry
carries `kind == "synthetic_recovery"` (`SYNTHETIC_RECOVERY_KIND`), set at insert
time. Metadata survives every queue transformation (merge, prefixing, truncation)
and cannot collide with a user pasting the transcript-visible recovery text back
in, which must classify as a plain user message.

There is deliberately no retry cap on refusal recovery: the model decides when to
stop, and the user's Stop button remains the hard breaker.

## Auto-nudge cycle

The auto-nudge service runs each bound slot's loop against a persistent deadline
(`next_due_ts`, one full interval after the loop's last cycle). A user message
cancels the pending fire — a nudge never races a human turn — but does not push
the deadline back: when the slot's turn completes (`HOOK_EVENT_STOP`) the timer
resumes toward the same deadline, firing shortly after the turn if it already
passed. Only the loop's own delivered cycles start a fresh interval (measured
from the nudge turn's end). When the timer elapses it injects the nudge as the
next turn into the same slot:

```
[auto-nudge cycle <N>]
<nudge message>
```

- `N` is `cycle_count + 1`. Only DELIVERED nudges count toward `max_cycles`.
- `{{STOP_FILE}}` in the configured message is substituted with the resolved stop
  sentinel path before the tag is prepended.
- The slot entry uses role `nudge` with a structured `nudge` meta block (`cycle`,
  `loop_id`), so the dashboard shows a compact cycle chip. The tag stays in
  `content` because that is what the model reads, and the body is deliberately not
  duplicated into meta: a multi-KB payload is stored and broadcast once.
- A nudge arriving while the slot is already running is DROPPED, not queued.
  Queueing would stack identical multi-KB payloads and blow the context window; the
  next idle tick schedules again.
- An unattended nudge turn refuses to run without a hook manager, so it can never
  bypass the PreToolUse governance gate. Same fail-closed posture as cron.
- Loops persist to `autonudge.json` under the data home and are re-armed on gateway
  restart. A slot that is unreachable (no history, deleted, or closed) has its loop
  removed.

**How to treat it:** it is a self-prompt. Continue the work; the operator asked for
the loop, but is not waiting on this specific message.

## Widget actions

A widget rendered inline via `<mcwidget title="Title">HTML</mcwidget>` can hand
text back toward the session, but it **cannot inject a turn**. The path is:

1. Inside the sandboxed iframe, a click on a `[data-action]` element collects
   `data-action`, `data-payload`, and any form-field values, then
   `parent.postMessage({type: 'mc-widget-action', action, payload}, '*')`.
2. The parent (`WidgetFrame.tsx`) validates the shape: the action must be a string
   (truncated to 64 chars), the payload must be a plain object, and the composed
   text is capped. It formats `[UI] <action>: <JSON payload>` (or `[UI] <action>`
   with no payload) and dispatches an internal `mc-widget-send` event.
3. `ChatPage.tsx` **pre-fills the composer** with that text and records it. It
   never auto-submits.

The iframe's own `isTrusted` click check is NOT the trust boundary and must not be
treated as authoritative: LLM-emitted `<script>` in the same document can
`postMessage` directly and skip that handler entirely. The real protection is that
the parent requires an explicit human gesture, so a widget action can never become
a user-role turn on its own.

When the user does send the pre-filled text, the turn is tagged
`meta.origin = 'widget'`. The backend then refuses the one chat-text-reachable
privilege escalation for such turns: orchestrator `go` / `go all` auto-run is
denied (audited as `auto_run_denied`) and the text falls through to a normal, fully
gated turn. Mode changes and tool approvals live on separate endpoints an iframe
cannot reach.

So there is no `[Widget action event]` envelope. What reaches the session is an
ordinary user message beginning `[UI] `, sent by a human, carrying an origin tag.

## Adding a new envelope

- Define the prefix in `dashboard/state.py` next to the others.
- Classify with `startswith` on that constant, and if the entry must survive queue
  transformations, tag the queue entry's `kind` instead of matching content.
- Decide the slot role (`inject`, `subagent`, `nudge`) so the frontend renders it
  as machine-originated, not as a user bubble.
- Redact before every delivery path, not just the one you are adding.
- Make sure it is not mirrored to a linked messaging surface as user input.
- If it triggers an unattended turn, keep the fail-closed hook-manager requirement:
  an automation-driven turn must run under the PreToolUse governance gate.

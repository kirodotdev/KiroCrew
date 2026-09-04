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

## Structured monitor wake

The probe controller injects an action turn only for a newly actionable
fingerprint. The controller constructs one envelope, and dashboard, Slack, and
Discord pass those exact bytes through:

```
[Monitor wake]
Monitor <id>: GitHub pull request <target>; objective: review_ready.
Fingerprint: <fingerprint>. Classification: <reason code>.
Head: <revision>. Changed: <allowlisted canonical facts>.
Next action: <bounded instructions>.
```

`MONITOR_WAKE_PREFIX = '[Monitor wake]'` is defined in `dashboard/state.py`.
The complete envelope is redacted before its 4,096-character cap and contains
only canonical provider facts, never raw responses, logs, comments, diffs,
stdout, or stderr. Dashboard stores it as role `nudge` with compact monitor
identity metadata; messaging surfaces receive the same content. It is
automation, not user speech. A raw provider completion event is the only signal
that the resulting agent action finished.

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

The prompt itself is appended to the slot as an `inject` row carrying
`meta.injectKind = "synthesis"`, and the turn is dispatched with
`_synthetic_payload=True`. Both matter: the row is what stops the prompt reaching
the conversation log unattributed (it previously replayed as though the user had
typed it), and the flag is what keeps a synthetic turn out of the
time-to-first-token distribution.

## Sub-agent delivery failure

A sub-agent reached a terminal state but injecting its report into the parent
session failed (most commonly a delivery timeout). `subagent.py` builds:

```
[Subagent completion event]
Agent `<id>` ❌ <reason>
Task: <first 100 chars of the task>
<outcome line>
Result saved at: <path> (<n> bytes)
Use the read tool to retrieve it if needed.
```

The outcome line reflects the run's actual terminal state instead of asserting
completion — this path fires for every terminal state, including runs that
never executed (the never-ran reading comes from the record's execution marker,
never from its error wording):

- completed: `The agent finished but result delivery timed out.`
- failed after execution began: `The agent failed before a result could be delivered.`
- failed before execution (approval or queued rejection, no output exists):
  `The run failed before it started, so there is no result to deliver.`
- stopped before execution began (no output exists):
  `The run was stopped before it started, so there is no result to deliver.`
- stopped mid-run: `The run was stopped before it completed.`

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

## How an `inject` row is rendered

Role `inject` covers several unrelated things, so the render side does not guess
from the text. Every `inject` row carries `meta.injectKind`, stamped at the append
site, and `meta` (unlike an `inject` row's `cls`) survives the persistence
boundary:

| `injectKind` | Row is | Renders as |
|---|---|---|
| `synthesis` | The post-fan-out consolidation prompt | Collapsed one-line note |
| `recovery` | A runner-authored continuation | Its own recovery card, or a generic note if the marker is unrecognised |
| `cron` | A scheduled job's output — the user's own | Labelled bubble (also carries `cronLabel`) |
| `user_replay` | The user's original message, replayed because the turn emitted nothing | Ordinary bubble; it is speech |

`resolveInjectCard` in `website/src/pages/chat/RecoveryCard.tsx` is the single
decision point, shared by `ChatPage` and the `transcriptRenderers` registry so the
surfaces cannot disagree. It prefers a recognised content marker (durable, and
carrying per-kind copy no tag reproduces), then applies a POSITIVE allowlist:
only `recovery` and `synthesis` become a note. Everything else — including a row
with no stamp, written by a gateway older than the field — keeps whatever the
surface drew before, so no history changes rendering underneath the user.

## Turn-recovery continuations

The runner injects a synthetic continuation when a turn ended for a system reason
rather than because the model was done. Each has its own prefix in
`dashboard/state.py`, each renders as an `inject` message (not a user bubble), and
none is mirrored to a linked Slack or Telegram thread as though the user typed it:

| Prefix | Fired when |
|---|---|
| `[Tool refusal — automatic recovery]` | A tool call was refused for a recoverable system reason (a host-gate policy deny, the read-only bash gate, or a PreToolUse hook block) and the in-band notice below could not carry the reason. **Fallback only** — see the in-band note under the table. |
| `[Stalled turn — automatic recovery]` | A genuinely wedged turn was detected and reset. Tells the model the interruption was a system stall, NOT the user, and to resume from its last committed step rather than restart. |
| `[Tool stall — automatic recovery]` | The per-session watchdog judged an in-flight tool dead and cancelled the session. Hands over the stall context so the model can check partial results and continue. |
| `[Interrupted turn — automatic recovery]` | A transient backend 5xx cut a turn short after tokens or tool calls had already streamed. |
| `[Empty response — automatic recovery]` | The model returned no output twice. Continue the pending request; do not restart from scratch or re-run steps that already succeeded. |
| `[Unfinished action — automatic recovery]` | The turn ended right after announcing an immediate action ("I'll do that now") without making the tool call, so nothing actually happened yet a billed turn was recorded. Instructs the model to carry out the announced action now — unless it was actually deferred pending the user's approval or an unmet condition, in which case it is told to hold and say what it is waiting for (a semantic consent backstop, since the terminal-promise detector's approval-gate deny-list cannot enumerate every conditional phrasing). Bounded to one attempt per turn; a second consecutive promise-only ending falls through and lands normally with a give-up notice. |

**A tool deny is explained IN-BAND first, and the injection above is the
fallback.** ACP's permission response carries only `outcome`/`optionId`, so the
host cannot attach a reason to a rejection — kiro-cli hands the model the fixed
tool result `"User denied tool execution"`, which reads as the person having
clicked No. `chat_runner._steer_policy_notice` therefore steers
`state.build_refusal_steer_notice`'s body into the turn **before** answering the
permission request. Holding the unanswered request is what makes that race-free:
the turn is provably in flight, so the notice is queued and folded in at the next
model-inference boundary — the one right after the rejected tool resolves — and
the model adapts inside the SAME turn. It is opt-in by positive capability
(`supports_steer`, i.e. `ACP_BACKENDS_STEER`), so a harness without mid-turn
steer is unchanged.

`should_queue_refusal_recovery` then suppresses the extra turn only when every
refusal got a notice AND a `steering_consumed` echo accounted for all of them. An
unconfirmed notice counts as undelivered: skipping wrongly leaves the model with
kiro-cli's wrong attribution and no correction, while queueing wrongly costs one
turn the model is told twice — which is what this path cost before in-band
delivery existed.

The recovery classification for the last two is **structural**: the queue entry
carries `kind == "synthetic_recovery"` (`SYNTHETIC_RECOVERY_KIND`), set at insert
time. Metadata survives every queue transformation (merge, prefixing, truncation)
and cannot collide with a user pasting the transcript-visible recovery text back
in, which must classify as a plain user message.

There is deliberately no retry cap on refusal recovery: the model decides when to
stop, and the user's Stop button remains the hard breaker.

## Stop-hook continuation

A Stop hook that exits 0 and prints a block decision on stdout asks the harness to
keep the session going instead of ending the turn
([contract](https://kiro.dev/docs/hooks/types#agent-stop)):

```json
{"decision": "block", "reason": "<the instruction to continue with>"}
```

`reason` IS the message. The runner parses the hook outputs collected for the Stop
event — exit-0 stdout, plus the `BLOCKED:` marker `_fire` synthesises for any
exit-2 hook — and each well-formed decision is queued as the next turn behind
`HOOK_CONTINUATION_RECOVERY_PREFIX = '[Hook continuation — automatic]'`. This
lets a hook judge a finished turn and push it further — a gate that checks tests
pass, or one that auto-continues a trivial read — with no round-trip to the user.

- **Nothing failed.** Unlike the recovery continuations above, the turn ran to
  completion; a hook simply asked for another. The card's copy names the hook as
  the cause rather than reporting an interruption. The constant is named into the
  `*_RECOVERY_PREFIX` family only so `test_recovery_card_prefixes.py` covers it —
  a marker outside that family renders as a full-width bubble instead of a card.
- **Only a block decision with a non-blank `reason` continues.** Plain logging
  output, non-JSON, a non-block decision, a block with no reason, and the
  `BLOCKED:` markers an exit-2 hook contributes are all ignored, so an ordinary
  Stop hook stops the turn as before.
- **A continuation is not queued once a stop is pending.** Injection is suppressed
  when a stop is already in progress as the hook output is processed, when a
  session reset is already re-queuing, and when the turn was cancelled by the
  user. A soft stop arriving AFTER the entry is queued does not remove it: the
  first Stop press deliberately preserves the queue, so the entry still drains on
  the next dispatch (behind the session-reset notice). The second press clears the
  queue outright, which is the hard breaker.
- Several hooks' instructions keep firing order: each is inserted at the queue
  front in reverse.
- Entries carry `kind == "synthetic_recovery"` as well as the prefix, so the
  dequeue path classifies them structurally and the flattened message still
  classifies once the metadata is gone — which is what keeps the continuation out
  of a linked Slack thread's user-message mirror.
- **A backstop cap bounds a runaway loop.** `agent.max_stop_hook_nudges` (default 100)
  limits how many consecutive hook continuations a run may take. When the depth reaches the
  cap, the next block decision is refused: no turn is dispatched, and a halt card
  (`[Stop-hook nudge cap reached] #N`) is surfaced instead so the transcript shows the loop
  was force-stopped at depth N. `0` disables the cap — the opt-in for a genuinely unbounded
  feedback loop, where terminating is the hook's own responsibility and Stop stays the
  breaker. The cap exists because the model cannot end a hook loop (even a "nothing left to
  do" turn re-fires the hook); only the hook or this backstop can.
- **The Stop stdin payload carries `hook_continuation_count`**, the depth of the current
  unbroken continuation run (`0` on a normal turn, one deeper per consecutive hook
  continuation), plus `stop_hook_active` as its boolean shorthand (`count > 0`). The Kiro
  contract defines no cap and neither field, so these are additive: a hook may self-limit
  (`if not stop_hook_active: block` continues at most once), threshold on the count, or
  surface it to the model, while a real gate hook checks its own condition and ignores them.
  The depth is tracked on the slot and reset by any non-continuation turn; both keys ride
  beside `assistant_text`, stamped on every Stop fire.
- **Fail-closed by construction**, so no separate guard is needed: with no hook
  store the Stop event produces no stdout, which parses to no instructions and
  queues nothing — and that same branch returns a `BLOCKED:` marker for every
  `PreToolUse` call, so no tool runs at all. A session that cannot govern its tool
  calls cannot produce a continuation either.
- The `reason` is external process output, and it is redacted (exfiltration URLs,
  then credentials) on the dequeue path shared by every queued turn, before the
  continuation is classified or dispatched.

**How to treat it:** it is an instruction from an automation the operator
configured, not a question from a person. Do the work it asks for and continue.

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
- Structured monitor action accounting is a separate internal completion
  callback, not a new injected-message envelope. Until the probe dispatcher is
  attached, structured records remain fail-closed. The dormant adapters do not
  change the legacy `[auto-nudge cycle N]` body, delivered-cycle count, `fired`
  event, or rearm timing. When attached, dashboard and Discord report only a raw
  provider completion; Slack likewise requires the raw provider completion and
  shares its one consumed usage result with telemetry. A dispatched monitor wake
  that reaches stream exhaustion remains in flight only until its durable,
  bounded completion-evidence deadline; no synthetic completion is injected.

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

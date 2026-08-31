# Session Control Module

## Overview

Session control lets one of the user's chat sessions observe and interrupt
another: open a new session, stop an in-flight turn, close (archive) a session,
and read a transcript tail.
It exists because a session cannot see what its peers are doing. A session that
has spent an hour on a PR cannot tell whether the session watching the build has
finished, and today the only way to find out is for the human to switch tabs and
look. Session control lets the session ask directly.

Five MCP tools on `kirocrew-dashboard`, five strict-internal routes, one config
switch. Every route is on `_STRICT_INTERNAL_API_PATHS`; an unlisted one is
unreachable in production because the caller's `X-Internal-Secret` is ignored.

| Tool | Route | What it does |
|------|-------|--------------|
| `session_create` | `POST /api/session-control/create` | Open a new, empty session in the caller's workspace |
| `session_stop` | `POST /api/session-control/stop` | Stop another session's in-flight turn |
| `session_close` | `POST /api/session-control/close` | Close (archive) another session, as the tab ✕ does — heavier than stop, and recoverable rather than a delete |
| `session_send` | `POST /api/session-control/send` | Deliver a message that another session runs as its next turn |
| `session_read_message` | `GET /api/session-control/read` | Read another session's transcript tail + liveness |

**One verb here writes into another session's conversation: `session_send`.**
Reading returns a transcript tail, stopping cancels a turn the way the Stop button
does, creating opens an empty session, and sending delivers a message that the
target runs as its next turn. Delivery is the sharpest verb and is bounded
accordingly: the body is redacted through `sanitize_outbound` before it is
persisted, it is prefixed with a `[sent by session <caller> via session_send]`
envelope so the target's transcript can never render it as something the person
typed, and channel agents are blocked from it outright.

**Delivery has two authorization moments, and only the first is enforced today.**
An idle target runs the prompt immediately, under the authorization that admitted
it. A busy target QUEUES it, and the generic drain re-runs no check — so a target
that gains a channel mirror between enqueue and drain broadcasts the delivered
text. That window is accepted, not overlooked: it is not specific to this module
(a human-typed message into a busy session drains through the same ungated path),
so it is fixed once at the drain rather than per caller. Tracked as issue #5911.

`session_create` earns its place on its own, not as the front half of a delivery
design: an agent that has just worked out that a job needs its own session can
open it pre-named and bound to the right agent, in the caller's workspace, and
hand the person a key they can read and stop. Without it the person does that by
hand -- new tab, retype the title, pick the agent -- and the two observation verbs
have nothing to point at that the agent itself put there. It deliberately does
NOT seed a first message: that would be delivery.

`kirocrew-dashboard` rather than `kirocrew-core`, because these tools are not a
capability every session should carry. That server is an **assignable set**: it
is absent from the default agent's spec and loads only for an agent whose own
spec references it, so an ordinary session spends no context on tools it will
never call. The set already holds the chat-folder tools, and the two classes are
granted together on purpose — an agent given the job of organizing sessions is
the same agent that should be able to see what they are doing. A test pins that
bundling so neither half can leave the set unnoticed.

Discovery is not new: `list_sessions` already enumerates the caller's sessions,
and its keys are what `target` accepts.

## Authorization

Deny-by-default, and checked in **one** place — `authorize_target` — for every
verb that takes a target (`stop`, `send`, `close`, `read`), so a guard cannot be
present on one and missing on another. (`session_create` has no target to
authorize; it checks the caller's own eligibility with the same refusals.) Every refusal is recorded in the SEL as
`session_control.<op>` with `outcome=denied`, so an attempt to reach a session
that is out of bounds is visible after the fact even though nothing happened.

| Refusal | Status | Why |
|---------|--------|-----|
| Config switch off (`agent.session_control`) | 403 | Operator opted out |
| Caller session cannot be identified | 403 | An unidentifiable caller makes the self-target guard blind |
| Caller is an unattended session (`cron-*`, `workflow-*`) | 403 | A scheduled job acting on live conversations is not a handoff |
| Caller is itself incognito, temporary, or app-scoped | 403 | Caller-side isolation — the direction the target-side checks cannot see |
| Caller is channel-linked (`linked_session_key` set) | 403 | The exfiltration direction: a linked caller's conversation IS a channel thread, so a read would hand a private dashboard transcript to that channel's readers. `CHANNEL_AGENT_BLOCKED_TOOLS` keys on the agent identity; a linked slot is a second route to the same surface |
| Caller's own session is no longer open | 403 | Nothing to attribute the operation to |
| Caller changed workspace while a creation was in flight | 403 | Creation resolves the workspace's project directory off-loop, so it suspends between authorizing the caller and allocating the slot. Both decisions that read the caller's workspace -- the memory boundary the child inherits, and whether the answering agent is bound to that workspace -- are invalidated by a move, and re-deciding the binding here is not available: it needs a config load, which must not run on the event loop |
| Named agent does not resolve to a configured one | 403 | The resolver falls back to the default agent, which passes the workspace check because it is the caller's own default -- so no boundary is crossed, but the created session would store and advertise a name that is not what answers. `ResolvedBindings.requested_resolved` states that contract for callers that store the requested name. Refused rather than rewritten to the effective agent: nothing exists yet, so a corrected name costs one retry, whereas an existing slot keeps its stored name verbatim so a momentarily stale resolution cannot permanently rebind it |
| Target is the caller | 403 | A session controlling itself has no exit |
| Target is unattended (`cron-*`, `workflow-*`) | 403 | A `workflow-<run_id>` slot is display-only and a cron's turns are driven by a schedule |
| Target is incognito or temporary | 403 | Never addressable, matching `list_sessions` |
| Target is app-scoped | 403 | App sessions are the app's, not a peer's |
| Target is channel-linked (`linked_session_key` set) | 403 | Its conversation is mirrored to Slack/Telegram, so reaching it crosses a surface boundary both ways — and its stop cannot be honoured, because the stop path addresses `dashboard:<slot>` while a linked slot's turns run under its linked key |
| Target or caller has an outbound channel mirror (`get_mirror_link`) | 403 | The same boundary reached by the other mechanism. `linked_session_key` marks a channel-BORN slot; a dashboard-born slot given a mirror link republishes its turns to a channel just as surely, and the link lives in the session store rather than on the slot, so the slot-side check reads empty on exactly the session that mirrors |
| Target is a crew-mode session (`mode == "crew"`) | 403 | A crew session's turn lifecycle is not the dashboard's: `/api/chat` routes its input to `state.crew.ingest`, which makes a durable queue entry and fans it out to topic sub-sessions. Refused rather than emulated — a target whose lifecycle differs needs its own handling, not a second copy of the orchestrator's rules |
| Target is in another workspace | 403 | Workspaces are the memory boundary |
| Target names no open session | 404 | A mistake, not an authorization failure |
| Title matches more than one session | 409 | Guessing means acting on the wrong conversation |

Two notes on scope:

- **Only sessions the dashboard currently holds are addressable.** A closed tab
  is out of reach on purpose — waking one would resurrect a conversation the
  user put away. This is narrower than `list_sessions`, which also lists history.
- **Every target-taking tool is on `CHANNEL_AGENT_BLOCKED_TOOLS`, including the
  read.** A channel agent is contained to channel posts, and session control
  crosses that boundary in both directions: a stop or close reaches the user
  through one of their dashboard transcripts, and `session_read_message` pulls a
  private dashboard conversation into a channel other humans can see. Containment
  is about what crosses the boundary, not about who writes, so the read is
  blocked alongside the rest. `session_create` earns its place for a different
  reason: it writes nothing into an existing conversation, but it puts a
  persistent, sidebar-visible session outside that containment.

All these tools additionally require a **signed** caller identity
(`_resolve_session_key_strict`), not the lenient `/proc` ancestor walk. A
subagent spawned by `spawn_run` lives under its parent slot's process tree, so
the walk resolves it to the parent — and since authorization here is entirely
"what may this session reach", that would let a subagent read or stop the
parent's sibling sessions. A caller the gateway issued no key to is refused with
an explanation rather than silently borrowing one.

The routes are **strict-internal** (`_STRICT_INTERNAL_API_PATHS`): loopback plus
`X-Internal-Secret`, with no cookie fall-through. No browser calls them, and they
are the entry point to opening, stopping, and reading another live conversation —
a cookie path there would be a new authorization surface rather than a
convenience. The MCP process holds the secret; an agent's own sandbox does not
(`KIROCREW_INTERNAL_SECRET` is stripped from agent env), which is why these are
tools rather than something an agent can curl.

Each handler **re-asserts** `request["internal_auth"] is True` rather than
trusting the path classification. Strict is not self-enforcing at the handler:
with the header absent the middleware falls through to cookie auth, and a
`local_only=False` deployment reclassifies strict paths as mixed. Because these
routes authorize on the `X-Session-Key` the caller supplies, a same-origin page
holding only a dashboard cookie could otherwise act **as** any of the user's
sessions. `internal_auth` is set only after a constant-time secret match, so one
check closes the cookie path, the app-token path, and the non-loopback
reclassification together. The same reasoning is why
`/api/computer-use/frame` re-asserts it.

The config read fails **closed**: `KiroCrewConfig.load()` raising resolves to
disabled, which is also the field's own default, so neither a malformed unrelated
section nor a missing setting can produce cross-session reach.

## The wait → read poll loop

`session_read_message` is the observation half, and polling is the supported
shape:

1. `session_read_message(target)` — record `next_since`.
2. `wait(seconds=…)`.
3. `session_read_message(target, since=<previous next_since>)` — returns only what
   arrived since, so a loop does not re-read the same messages.

`total` is an **absolute position** in the session, not the length of the live
window. A slot retains only its most recent messages in memory and credits each
trimmed row to a frozen-prefix counter, so a length-derived cursor would freeze
at the retention cap — and a poller on a long session would silently stop seeing
replies, on exactly the sessions that need it most. Positions are based on the
**durable-only** frozen-prefix counter (`_disk_older_durable_count`), which
counts only trimmed rows a durable read returns — never the all-rows
`_disk_older_count`, which also counts transient rows and would shift every
position as soon as one was trimmed. A trimmed session therefore keeps an exact
cursor: `next_since` is returned as usual. The one trim-related refusal left is
a `since` **below** the trimmed prefix (409 `cursor_unavailable`): those rows
exist only on disk now, and starting the read at the window instead would
silently skip everything in between. The caller falls back to a tail read.

`running` is what makes the loop terminable: `running: false` with an empty
window means the target finished and went idle, which is different from "nothing
new yet". `queue_depth` reports how much the target still owes.

The cursor deliberately stops **before the streaming tail**. `chat_runner`
appends a `chunk` row per token burst and `_flush_segment` then deletes that
trailing run, replacing it with one durable assistant message — so chunk rows are
always a suffix, never interleaved. Counting them would inflate `total`, the
flush would shrink the list back under it, and the next `since=next_since` read would
skip the finished reply permanently. A read taken mid-reply therefore reports
`streaming: true`, so an empty window while the target is composing is
distinguishable from an empty window because nothing is happening.

A stale cursor is refused, not clamped. A compacted or rewound transcript shrinks,
so a `since` past the end answers 409 `cursor_unavailable` and the caller falls
back to a tail read. Clamping it to the end would look friendlier and lose data:
the rows below the clamp are what replaced the old tail, a cursor never moves
backwards, so they would be skipped permanently while the response read as
"nothing new". A cursor exactly AT the end is not stale and still returns an empty
window.

## Stopping is safe to re-send

The Stop button escalates: a second press while the first cancel is still pending
hard-kills the turn, and the hard-kill path clears the slot's queue and its pending
steers. That is right for a button, where the second press means a person watched
the cooperative stop fail to take. It is wrong for an RPC, where a client that got
no response inside its 30s request timeout re-sends the same request — so on the
button's semantics a timeout retry would silently get the destructive variant of a
verb the caller asked for once, and the queued work would be gone with nothing
saying a retry rather than a decision caused it (issue #5074).

`session_stop` therefore withholds the escalation for a call it cannot tell apart
from a retry. `stop_retry.allow_escalation` records the first stop a caller makes
against a target and answers `False` for any repeat inside `WINDOW_SECS` (120s);
`stop_slot_turn` takes that as `escalate=False` and lets the repeat fall through to
its existing "stop already in progress" no-op.

Three properties are worth stating because each one is a way this could have gone
wrong:

- **Only the escalation is withheld, never the stop.** A repeat that finds the
  target running again soft-stops it exactly as a first call would. The window
  suppresses a kill, not a cancel.
- **The window is anchored at the first stop and is not extended by the repeats it
  absorbs.** So escalation is suppressed for at most one window: a client that
  retries forever is absorbed, and after 120s a stop that STILL finds the target
  winding down escalates — which is the case where escalating is the right answer.
  A sliding window would put a hard kill out of reach of any caller polling faster
  than the window.
- **The key is (caller, target), not the target alone.** A retry comes from the
  caller that made the original request; two different callers stopping one target
  are two independent decisions, and keying on the target would suppress the second
  caller's FIRST call — removing escalation from the RPC rather than making a retry
  safe.

The window is sized against what it has to outlast rather than picked: below the
30s request timeout it would expire before the retry it exists to absorb. Nothing
durable backs it, for `create_rate_limit`'s reason — a restart buys a caller one
window, not a capability.

The caller is told which of the two no-op facts it hit. `already_stopping`
separates "was never running" from "its cancel is still in flight", because a
de-duplicated retry reaches that reply routinely and rendering both as "nothing to
stop" would tell the second caller the opposite of what happened.

## Closing archives, and re-checks at the point of no return

`session_close` is the tool-side equivalent of the tab ✕. It is **non-destructive**:
the conversation is saved to history (`closed=True`) and can be reopened later, so
closing dismisses the LIVE tab, it does not delete the transcript. It is a
strictly heavier act than `session_stop` — an in-flight turn is cancelled first
and its work discarded — so the tool description tells the caller to read the
session before closing it. It reuses the dashboard's own close path
(`close_slot`), the same sequence the ✕ button runs: a synchronous tombstone,
auto-nudge-loop retirement BEFORE the awaits so no nudge resurrects the tab, the
owning app's close hook with rollback, persist-as-closed, and per-tab session
teardown. Its three failure modes surface as their own codes at HTTP 500
(`nudge_retire_failed`, `app_close_hook_failed`, `history_save_failed`), which is
why the routes now forward a 500 rather than degrading it to 400.

**Authorization is re-asserted at the point of no return.** `authorize_target`
runs before `close_slot`, but `close_slot` then awaits — auto-nudge retirement
takes the AutoNudge lock, and the app hook awaits external work — and a target
that was unmirrored and unlinked at admission can gain a channel mirror or link
in that window. Archiving a now-channel-backed session it was never allowed to
reach is exactly the boundary the `mirrored_target` / `linked_session_target`
guards hold, so `close_target` passes a SYNCHRONOUS `pre_pop_check` that runs
immediately before the slot is popped, after every await (the nudge retirements
and the app hook). It re-runs `authorize_target` with `skip_enabled_check=True` —
omitting the one part of that gate that can read config on the loop, since the
feature was already confirmed enabled at admission and disabling it mid-close is
not a containment boundary — and compares the re-resolved slot to the one being
closed **by identity**: a concurrent close-and-reopen can re-mint the same key
onto a different session, and popping that would tear down the replacement while
saving the stale slot (409 `target_replaced`). Being synchronous is the whole
point — there is no suspension between the last retirement, this re-check, and
the pop, so nothing (a channel mirror/link landing, a re-mint, or a racing
`monitor_start` arming a loop) can change between the final authorization and the
archival; an awaited re-check, by contrast, reopens exactly those windows. Any
refusal aborts the close, rolls back the retired nudge loop, and surfaces as the
guard's own status. This is the same "re-gate adjacent to the mutation, comparing
identity not presence" discipline `create_session` uses for its slot allocation,
and the same theme as the queued-drain re-check (#5911). The human ✕ path passes
no check — the person owns the tab and closes it unconditionally.

## Configuration

`agent.session_control` (bool, default **false**). Off makes every tool refuse
with a message naming the switch, so an agent that has not been granted it
reports why rather than failing silently.

Default-off is the deliberate part. The tools ride on the existing
assignable `kirocrew-dashboard` server rather than a new one, so an operator who
had already assigned that server to an agent for folder organization would
otherwise find that agent able to read peer transcripts and stop peer turns purely
by upgrading. Every target is still one of the user's own sessions on their own
machine, reached over loopback with an audited internal secret -- the objection is
not that the capability is dangerous but that it would arrive without anyone
granting it. Making it an explicit switch costs one setting and buys a grant that
matches what the operator actually chose.

Both absent and malformed values resolve to disabled. `_safe_bool(..., False)`
handles the malformed case -- `bool("false")` is `True`, so a user who wrote the
value in an editor that quotes it would otherwise get the opposite of what they
read -- and the lookup now supplies `False` for the absent case, so nothing has to
infer a grant from silence.

## What is deliberately not here

- **No delivery to a target outside the addressable set.** `session_send` writes
  into another session's conversation, but only one the same `authorize_target`
  guard admits: a channel-linked, channel-mirrored, crew-mode, incognito,
  app-scoped, unattended or cross-workspace target is refused, so the verb cannot
  reach a conversation other people are party to. The residual is the queued arm's
  second authorization moment, recorded above and tracked as #5911.
- **No cross-workspace or cross-machine reach.** The boundary is one gateway's
  live sessions in one workspace.
- **No waking closed sessions.** See above.
- **No writes on the read path.** `session_read_message` never changes the
  target's state, so a poll loop cannot perturb what it is measuring.

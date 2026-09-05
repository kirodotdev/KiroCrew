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
| `session_create` | `POST /api/session-control/create` | Open a new, empty session in the caller's workspace, optionally filed into a sidebar folder at creation |
| `session_stop` | `POST /api/session-control/stop` | Stop another session's in-flight turn |
| `session_close` | `POST /api/session-control/close` | Close (archive) another session, as the tab ✕ does — heavier than stop, and recoverable rather than a delete |
| `session_send` | `POST /api/session-control/send` | Deliver a message that another session runs as its next turn; `target="user"` escalates to the human instead (see below) |
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

`session_create` also takes an optional `folder` — a folder id or `/`-separated
human path, resolved with `chat_folder_create`'s `parent` semantics (missing
segments created, behind the same tree-shaping gate) — and files the slot as
part of creation (#6118). Filing used to be a second call
(`chat_folder_move_session`), and the window between the two was a real defect
path: a folder deleted in between left the session unfiled with the create
already done. The handler assigns `folder_id` inside the same synchronous window
that configures the slot, holds `suspend_slots_push` across the whole
allocation-to-persist span (so the slot's first broadcast frame already shows it
filed, and a slot whose birth write fails is never broadcast at all), and
carries the placement in the persist-at-birth metadata, so no caller or client
ever observes an unfiled session and the placement survives a restart.
An unresolvable folder refuses the whole create — nothing exists yet, so refusal
loses nothing — existence is confirmed read-only under the folder-store lock
(`read_folders`) before the allocation, and the move path's Model-B un-hide runs
only after the filing has landed, so a refused create leaves no folder-tree
mutation behind.

### What a created child inherits

Creation copies two different kinds of state, and the split is deliberate.

**Identity** — the child is created in the caller's workspace (the memory
boundary; a child left in `default` would be both a boundary crossing and
unaddressable by its own creator), inherits the caller's agent when none is
named, takes that workspace's project directory as its cwd, and is attributed to
the caller via `created_by` so the per-creator slot ceiling is countable.

**Approval posture** — the caller's `_trust` and `_trust_reads` transfer, so a
trusted operator's dispatched worker does not stall on a prompt nobody is
watching. This is the posture `parent_trusted` already gives a `spawn_run`
subagent, which reads the parent's stored `"auto"` policy; a `session_create`
child previously started from `_ChatSlot.__init__`'s empty defaults, so the same
delegation behaved differently depending only on whether it got a sidebar tab.
No session-store write happens at creation: the child has no ACP session yet
(`set_approval_policy` no-ops on a missing session), and `chat_runner` already
derives the persistable policy from `_trust` on every session create/resume, so
the subagent spawn gate sees it from the child's first turn.

Two grants are excluded, and the exclusions are load-bearing:

| Not inherited | Why |
|---|---|
| `_trusted_patterns` | Per-command grants ("`npm test` is fine"), not a posture. A pattern is judged against the session the operator was LOOKING at, while a dispatched worker runs model-authored work they have not seen, so the same glob can admit a command the grant was never asked about. Inheriting them also pays for itself in neither direction: with `_trust` set the child already auto-approves via `_slot_is_trusted`, so the list is dead weight, and because `chat_runner` matches patterns independently of `_trust` it changes an outcome only when the operator withheld session trust and approved single commands instead — the case that must keep asking |
| `_trust_scope` | A TTL-bounded, SEL-audited `SafetyOverride` scope, re-checked on every approval. Forking the key would hand a second session a credential whose revocation this path cannot observe, so the child would keep auto-approving after the scope that justified it is gone. An unattended worker that needs one gets its own, from whatever owns its lifecycle |

The posture that transfers is the one held at **allocation**, read off the
re-resolved caller in the synchronous window after the last gate — not the one
read on entry. Creation suspends three times before the slot exists (project
directory, config load, folder confirmation), and an operator selecting `normal`
in any of those windows would otherwise have a revoked posture resurrected by a
create already in flight. Revoking mid-call yields an untrusted child.

Nothing about trust is persisted at birth. The birth metadata carries
`tab_id`, `origin`, `created_at`, `workspace`, `agent`, `project`, `title`,
`memory_mode`, and `folder_id` / `created_by` when set — no trust field — so a
restart returns the child to interactive along with its creator.

The create's SEL record carries `agent`, `folder_id`, and what the child was
born with: `inherited_trust` and `inherited_trust_reads`, present on both
outcomes so `"false"` is positive evidence the posture did not transfer. That is
what makes an auto-approved tool call in a dispatched session traceable to the
creator's posture rather than unexplained.

**Known gap.** Inheritance is transitive — a trusted child is itself an eligible
creator — and revoking the creator's trust afterwards does not cascade to
already-born workers, so one click covers a dispatch tree that outlives the
click's scope. Bounded by the slot caps, by trust being in-memory only, and by
the global Trust picker (no slot selected), which sets `normal` across every live
slot. A per-slot revoke that walks `created_by` is tracked in issue #8589.

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
| Config switch off (`agent.session_control` explicitly `false`) | 403 | Operator withdrew the capability from every agent at once. Defaults to true — the agent's `kirocrew-dashboard` mount is the grant. **Exception:** a crew-member DM slot (`member-*` caller key) bypasses this switch — see "Member callers" below |
| Caller session cannot be identified | 403 | An unidentifiable caller makes the self-target guard blind |
| Caller is an unattended session (`workflow-*`) | 403 | A `workflow-<run_id>` slot exists only once its originating tab is gone, so there is no owning session to fence it to. **Exception:** a cron slot (`cron-*` caller key) is admitted and fenced by creator ownership instead — see "Cron callers" below |
| Caller is itself incognito, temporary, or app-scoped | 403 | Caller-side isolation — the direction the target-side checks cannot see |
| Caller is an APP-owned cron (`created_by` starts `app:`), or a cron whose job cannot be found | 403 | `app_owned_cron_caller` / `cron_owner_unverifiable`. A cron tab is minted without `app=`, so the `_app` check above cannot see an app's own scheduled job; ownership is read from the JOB instead, and an unverifiable owner fails closed — see "Cron callers" below |
| Caller is channel-linked (`linked_session_key` set) | 403 | The exfiltration direction: a linked caller's conversation IS a channel thread, so a read would hand a private dashboard transcript to that channel's readers. `CHANNEL_AGENT_BLOCKED_TOOLS` keys on the agent identity; a linked slot is a second route to the same surface. **Exception:** a `cron:<job_id>` link, which names the job's own run transcript and republishes to nobody |
| Caller's own session is no longer open | 403 | Nothing to attribute the operation to |
| Caller changed workspace while a creation was in flight | 403 | Creation resolves the workspace's project directory off-loop, so it suspends between authorizing the caller and allocating the slot. Both decisions that read the caller's workspace -- the memory boundary the child inherits, and whether the answering agent is bound to that workspace -- are invalidated by a move, and re-deciding the binding here is not available: it needs a config load, which must not run on the event loop |
| Named agent does not resolve to a configured one | 403 | The resolver falls back to the default agent, which passes the workspace check because it is the caller's own default -- so no boundary is crossed, but the created session would store and advertise a name that is not what answers. `ResolvedBindings.requested_resolved` states that contract for callers that store the requested name. Refused rather than rewritten to the effective agent: nothing exists yet, so a corrected name costs one retry, whereas an existing slot keeps its stored name verbatim so a momentarily stale resolution cannot permanently rebind it |
| Target is the caller | 403 | A session controlling itself has no exit |
| Target is unattended (`cron-*`, `workflow-*`) | 403 | A `workflow-<run_id>` slot is display-only and a cron's turns are driven by a schedule. Not exempted for a cron CALLER: a cron may create and drive its own children, never another job's tab |
| Target is incognito or temporary | 403 | Never addressable, matching `list_sessions` |
| Target is app-scoped | 403 | App sessions are the app's, not a peer's |
| Target is channel-linked (`linked_session_key` set) | 403 | Its conversation is mirrored to Slack/Telegram, so reaching it crosses a surface boundary both ways — and its stop cannot be honoured, because the stop path addresses `dashboard:<slot>` while a linked slot's turns run under its linked key |
| Target or caller has an outbound channel mirror (`get_mirror_link`) | 403 | The same boundary reached by the other mechanism. `linked_session_key` marks a channel-BORN slot; a dashboard-born slot given a mirror link republishes its turns to a channel just as surely, and the link lives in the session store rather than on the slot, so the slot-side check reads empty on exactly the session that mirrors |
| Target is a crew-mode session (`mode == "crew"`) | 403 | A crew session's turn lifecycle is not the dashboard's: `/api/chat` routes its input to `state.crew.ingest`, which makes a durable queue entry and fans it out to topic sub-sessions. Refused rather than emulated — a target whose lifecycle differs needs its own handling, not a second copy of the orchestrator's rules |
| Target is in another workspace | 403 | Workspaces are the memory boundary |
| Target names no open session | 404 | A mistake, not an authorization failure |
| Title matches more than one session | 409 | Guessing means acting on the wrong conversation |

### Member callers: switch bypass, bounded by creator ownership

A crew member's pinned DM slot (caller key prefixed `member-`, created only by
`POST /api/members/{slug}/thread`) is a **conductor by design**: it dispatches
work into worker sessions it creates, patrols them, and reports back, with no
operator configuration. Two rules give it that shape:

- **The `agent.session_control` switch does not gate a member caller.** Members
  work out of the box — this is the zero-configuration contract, and it is a
  deliberate trade-off: an operator who turned session control off has NOT
  thereby disabled member dispatch. There is currently no separate switch for
  it; disabling a member disables its dispatch.
- **A member caller may only act on sessions it created.** Slot creation records
  `created_by` (the creator's caller key) in the slot's birth metadata; it is
  persisted with the session and rehydrated on restart (both restore paths).
  `authorize_target` refuses a member caller whose key does not match the
  target's `created_by` (`not_creator`, 403) — and this ownership boundary binds
  **even when the global switch is enabled**, so a member never widens to the
  ordinary caller's reach. Every other refusal in the table above still applies
  to member callers unchanged.

Ordinary (non-member) callers are untouched: they still require the switch.

### The fence propagates to what a fenced caller creates

`_caller_is_ownership_fenced` covers three populations, not two: a member DM slot,
a cron slot, and **anything either of them created**. The third is the one a key
prefix cannot see, and without it the fence buys nothing. A created child is minted
with a plain `chat-` key and INHERITS its creator's agent, so a fenced caller
running a session-control agent would otherwise get an unfenced deputy for free:
create a child, seed it, and the child — an ordinary caller by key — reads any
same-workspace session and reports back through the transcript its creator is
allowed to read.

`_created_by` is the marker, and it needs no lineage walk: `create_session` is its
ONLY writer, so a non-empty value means "an agent made this session" at any depth.
A grandchild carries its parent's key there and is fenced by the same test, and a
chain whose middle slot has been closed cannot fail open because no chain is
walked. A person's own tab and a fork reach `get_or_create_slot` directly and stay
unattributed, so ordinary human use is unaffected.

There is deliberately NO attendance exemption. `_ChatSlot._human_seen` looks like
the right hatch and is not: it records that a human has EVER driven the slot, is
monotonic and persisted, and says nothing about who authored the turn running now.
Releasing the fence on it would hand the creator its deputy back for the price of
the user glancing at the tab once — cron creates the child, the user types into it,
and from then on every cron-authored turn in that child runs unfenced. The question
the predicate can answer is "whose authority is this session", not "is a person at
the keyboard", so a person working in an agent-created session keeps that session's
reach rather than their own.
The member-facing tool surface is the ordinary `kirocrew-dashboard` `session_*`
tool set, mounted **per session** rather than through the on-disk agent
template: a member DM session's ACP `session/new` **and `session/load`** carry
the dashboard server as a session-level `mcpServers` entry (built by
`members.member_dispatch_session_server`, identity via `KIROCREW_SESSION_KEY`
in the entry's env) — both establishment paths, because `session/load`
re-initializes the session's MCP servers, so a resume that skipped the
injection would strip a member thread of its tools mid-conversation. On the
KAS backend the wire agent projection additionally grants the server in
`tools` plus the member's approval-free dashboard verbs in `allowedTools`
(ceiling-filtered like every other grant): `_MEMBER_DASHBOARD_GRANTS`, the
conductor's read/create set plus `session_send` and `session_stop` — the
write verbs are safe to auto-approve for a member *specifically* because the
`created_by` ownership fence above bounds them to worker sessions the member
itself opened. Member sessions also bypass the provider warm pool
(`bypass_member`): a pooled child was spawned with no session key on the
default backend, so a warm hit would skip both the member backend route and
the mount. The member backend is `agent.member_acp_backend` (default `kas`),
and requires a wire-capable backend (`ACP_BACKENDS_MEMBER_DISPATCH`: the
claude seam and KAS); kiro-cli v2 reads its template from disk and exposes no
per-session channel, so a member session on it runs as plain chat — the tools
are simply not mounted, never mounted-and-refused. Because the mount is
session-scoped, no other session on the same agent template gains the tools,
preserving the two-part grant for ordinary agents (the switch AND the
per-agent server assignment).

### Cron callers: unattended admission, bounded by the same fence

A cron job's own slot (`cron-<job_id>`, minted by
`inject_cron_result_to_dashboard`) is admitted to the surface even though nobody
is watching it, so a scheduled run can enumerate work and dispatch a session per
item. Three refusals had to move for that, and one deliberately did not:

- **The unattended caller refusal now covers `workflow-*` only.** What must not
  happen is a scheduled job reaching the user's OWN conversations, which is a
  question about scope, not attendance — an unattended job already starts a turn
  in the session that owns it every time it delivers with
  `send_message(session="origin")`. A cron can be held to that scope; a workflow
  result slot cannot, because it is minted only once its originating tab is gone
  and so has no owner to fence it to. Membership of `UNATTENDED_SLOT_PREFIXES` is
  the fail direction for any prefix added later: a new unattended surface is
  refused as a source until it is given a fence of its own.
- **A `cron:<job_id>` link is exempt from the caller-side channel-link
  refusals.** Those exist for channel links, where a read lands in front of a
  Slack or Telegram audience. A cron tab's link names the job's own run
  transcript and republishes to nobody. The TARGET-side refusal is not exempted.
- **The `created_by` fence binds a cron caller exactly as it binds a member**
  (`_caller_is_ownership_fenced` is the single predicate both admissions and the
  fence read, so they cannot drift). A cron reaches the sessions it created and
  nothing else, fail-closed on an unowned slot. `unattended_target` still stands,
  so a cron cannot reach another job's tab.
- **The global switch still gates a cron.** Unlike a member, a cron gets no
  bypass: the switch is the user's statement that agents may open and drive
  sessions at all, and a job running while they are asleep is the last caller
  that should be exempt from it.

**An APP-owned cron is refused, and ownership is read from the job.** This is the
one place admitting a cron would otherwise open something. `_app` is how every
other isolation decision recognises an app, but `inject_cron_result_to_dashboard`
mints the cron tab WITHOUT `app=`, so an app's own scheduled job arrives with
`_app == ""` and would pass the check beside it. An app could then create a
persistent, sidebar-visible session that is not app-scoped, which is exactly the
confinement escape the `_app` refusal exists to prevent, reached through the app's
cron instead of its session. `_app_owned_cron_refusal` therefore reads ownership off the job, which has **two
spellings** because two writers record it differently: the app cron SDK tags
`created_by = "app:{app_name}"`, while `mcp_cron`'s own `cron_add` records the
calling session in `session_key` and never writes `created_by` at all — so an
app-scoped session's job carries its authority only in the second. Both are
checked, and the second delegates to `_app` on the owning slot (resolved through
`caller_slot_key`, not a naive `removeprefix`) rather than re-deriving app-ness, so
there is one definition of "is this an app". **A new job field that can name a
principal is a hole until it is added to that function.** The refusal code is
`app_owned_cron_caller`, distinct from `app_scoped_caller` because callers render
that one with app-session wording that would misdescribe a cron. A job the registry
cannot produce, or a registry that cannot answer, refuses with
`cron_owner_unverifiable`: "could not verify the owner" must not read as "has no
owner", and nothing legitimate is refused by it because a cron whose job is gone is
not running.

One residual is accepted rather than closed. When `session_key` names a session that
is no longer open its `_app` cannot be read, and the refusal returns nothing for it.
Refusing instead would disable dispatch for the ordinary case — a user-created job
whose authoring tab has since been closed, which is most of them — so the
fail-closed direction is wrong here in a way it is not for a missing job. What
bounds the exposure is that the slot has to be gone: while an app's session is live,
its jobs are refused.

Applied at both
caller-side sites so the two halves stay mirrors, and scoped to cron callers so no
other caller pays for the lookup.

In `authorize_target` this refusal sits **before** `_resolve_slot`, unlike the other
caller-side refusals. A caller refused for its own identity must learn nothing from
the attempt, and resolving first makes the refusal an existence oracle: a guessed
target answers `target_not_found` (404) when it does not exist and the refusal (403)
when it does, so a caller allowed to touch nothing could enumerate the user's
session keys and titles by the shape of the error. The unattended prefix gate is
already on that side of the resolution for the same reason. The pre-existing
caller-side block below the resolution (`app_scoped_caller`, `ephemeral_caller`,
`linked_session_caller`, `mirrored_caller`) has the same shape and is left as it is
here: moving those changes refusal precedence for callers that exist today.

A session a cron creates is tagged `SlotOrigin.CRON`, not `USER`. A cron's own
slot carries that tag so its output stays outside the `slots:user` WS scope, and
a USER-labelled child would hand it that exposure by the route of creating a
session and writing there instead. The tag follows the caller's AUTHORITY rather
than its key prefix, for the same reason the fence does: a child inherits its
creator's agent, so a cron's child can itself call `create_session`, and a
prefix-only test mints THAT grandchild `USER` because its caller key is a plain
`chat-`. `create_session` therefore reads the caller slot's own `_origin` as well,
which carries the tag transitively to any depth. Only app tokens are filtered by
origin (`_serialize_for_client` returns the unfiltered payload to a dashboard
user), so a CRON-origin descendant stays in the sidebar exactly as a cron tab does.

Capability remains bounded per agent, which the slot-key prefix could not see:
`@kirocrew-dashboard` is an opt-in per-agent server, absent from the default
agent's spec, so a job whose agent does not mount it never has the verbs at all.
A cron whose fan-out must run without an approval prompt needs the write verbs in
its own agent's `allowedTools`; `_CONDUCTOR_DASHBOARD_GRANTS` deliberately
withholds them, because a conductor agent also runs in dashboard sessions where
no ownership fence applies.

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

## Escalating to the human (`session_send target="user"`)

The human is addressable with the same verb a peer session is. `session_send`
with the literal target `user` does not start a turn anywhere; it lands one
`escalation` row in the DM thread of the crew member that owns the caller, and
returns. Where the card lands is fixed by who is calling:

| Caller | Lands in | Member index touched |
|--------|----------|----------------------|
| A member DM slot (`member-<slug>`) | its own thread | yes |
| A worker session a member created (`_created_by` is a member slot) | the **creating member's** thread | yes |
| Any other session | its own transcript | no |

The literal is reserved, but a session may itself be titled `user` (title resolution is a documented way to address a session); that collision is refused as `ambiguous_target` (409) rather than silently taking the reserved meaning — the caller addresses the session by key instead. The collision is checked only after the caller is authorized, so an ineligible caller cannot learn from the refusal that such a session exists.

The human is not a slot, so none of the *target* gates apply — but every
*caller* gate of `authorize_target` does, in the same order and with the same
codes (`caller_unidentified`, `session_control_disabled` with the member
bypass, `unattended_caller`, `caller_gone`, `app_scoped_caller`,
`ephemeral_caller`, `linked_session_caller`, `mirrored_caller`): an escalation
is still a session reaching outside its own transcript. For a worker the
result says `reply_in_caller_thread: false` — the human answers in the
member's thread, and the worker learns of it only if the member relays it
(the member is the actor that owns the worker, not the human).

The row is `role: escalation`, `cls: msg msg-escalation`, content the caller's
markdown (one line of background, what was tried, what is needed — the tool
description asks for exactly that shape), and `meta`:
`{kind: "escalation", escalation_id, from_session, deadline, default_action,
options, goal, state: "pending", created_ts, mid}`. `deadline` is normalised to
an absolute ISO timestamp from a duration (`30m`, `2h`, `1d`) or an ISO input
and must fall 1 minute to 7 days out; `options` is at most six de-duplicated
strings of at most 120 characters; `default_action` and `goal` are at most 500.
Every field is sanitised with the same `sanitize_outbound` the peer path uses,
because the text crosses from one session into a transcript the human reads.
An invalid field is refused (`400`, `deadline_invalid` / `options_too_many` /
…) before anything is appended.

A member's escalation is also recorded on that member's **conversation index**
(see [crew-conversation.md](crew-conversation.md)): a pending record pointing
at `(session_key, mid)`, written *before* the card is surfaced under a
pre-minted row id so a reply can never race the record. `needs_you` —
projected on the member slot's `slots` frame and on the `GET /api/members`
roster row — is *derived* from the pending records, never stored on the slot.

Which reply answers which record is one rule, applied identically by the
index (`mark_answered`) and by the chat projection that draws the card:

- a live `user` row carrying `meta.escalation_id` (an option chip) answers
  exactly that record — the id rides the busy paths too (a queue entry's meta,
  a steer row's meta), because a member that just escalated is usually
  mid-turn when the human answers; when the queue drain merges several
  entries into one row, only an entry that is *itself* the human's
  (`human_reply`) contributes its id (`escalation_ids`), and the merged row is
  the human's only if **every** merged entry was — one automated or non-owner
  entry in the batch and the row answers nothing;
- a live `user` row without one (typed text) answers the pending record only
  when **exactly one** was pending *at the moment the row was appended* — the
  hook snapshots that set from the index's in-memory view on the event loop,
  so a record landing on the executor a beat later is neither counted nor
  answered, and the index agrees with transcript order; with none or several
  pending it answers nothing, so an unrelated message cannot silently retire N
  open decisions;
- a record whose deadline has passed is swept to `defaulted` (a
  `default_action` was declared) or `expired` first and is never answered
  late — judged against the **reply row's own timestamp**, not the moment of
  the index write, so a timely reply whose durable save crosses the deadline
  still answers (and `answered_ts` is the row's); a replayed row (fork,
  rotation, transfer) answers nothing; only a row
  the **authenticated composer** produced answers at all — the chat handler
  stamps `meta.human_reply` server-side only on the auth layer's positive
  `is_dashboard_user` signal (a validated dashboard credential; app tokens,
  derived internal callers and the internal secret never qualify — a falsy
  `app` claim is not trust) **and** the owner identity
  (`is_owner_dashboard_request`: the member's DM thread is the owner's
  conversation, so a non-owner dashboard user who can post there is not the
  human the escalation was raised to), never trusted from the client, and carries it
  through the queue, steer and requeue paths, so a peer's `session_send` row,
  a heartbeat, a cron `prompt:` or an internal caller posting into the member
  slot, which all land as `user` rows, never answer.

The index record is written *before* the card under a pre-minted row id, and
the write is load-bearing: if it fails the escalation is refused
(`escalation_index_unavailable`, 500) rather than delivered without a
lifecycle. The card row is then appended and surfaced like any other row and
persisted by the slot's ordinary flush — there is no forced save and no
rollback on this path. If the thread closes, the caller loses eligibility, or
the worker changes workspace across the index await, the record is retracted
and the call refused (`target_gone`, 409); if the append itself fails, the
record is retracted. A worker whose creating member's thread is in another
workspace is refused up front (`workspace_mismatch`), the same boundary the
peer path holds.

**Recovery: reconcile with the transcript on restore.** Consistency between
the index and the transcript runs in one direction — the transcript is the
truth, the index a thin projection of it — so on restore the projection is
re-derived from the transcript, both ways. Before a member's pending count is
trusted, each roster read in a gateway process hands the transcript in order
(the persisted rows — including the rotated archive, since the restored live
window is only a tail — followed by live rows not yet flushed) to
`crew_conversation.reconcile_with_transcript`: a pending record older than
`ORPHAN_GRACE_SECS` (120 s) whose card row is absent (the gateway exited
between the index write and the slot's flush) moves to `retracted`
(`retracted_reason: orphan`); a pending record whose card row is present and
that a later `user` row with `meta.human_reply` answers under the live rule
(named `escalation_id`/`escalation_ids`, or free text with exactly one pending
at that point of the transcript) moves to `answered` (the gateway exited
between the reply's transcript save and the live hook's index write). Replies
are replayed *before* deadlines are swept, each judged against the record's
deadline as of the reply's own row timestamp, so a timely reply is never
recorded as `defaulted` because recovery ran after the deadline. A record
still inside the grace is *deferred* — neither retracted nor trusted — and the
member is marked reconciled only when nothing was deferred, so it is looked at
again on the next read. The mark is keyed to the transcript's generation
(persisted mtime plus live-window length), not to the process: a rewind or any
rewrite that removes a card row changes the generation and the index is
re-derived on the next read while the member has anything pending (with
nothing pending there is nothing a re-derivation could change). The live hook keeps the same
direction: the human's reply row is persisted first and the record is marked
`answered` only when that save committed; an unprimed memory view is not an
empty one — the hook falls back to the file rather than treating "nothing
cached" as "nothing pending".

The reply that answers also pushes a slots update, so the badge clears with
the reply. One SEL `session_escalate` line is written per delivery, naming the
escalation id, the target thread, and whether a deadline, default and options
were carried; caller-side refusals are audited as `session_control.escalate`
denials with the same codes the peer path uses.

Three constraints are the contract, and each has a corresponding refusal or
absence in the code rather than a note in the tool description:

1. **Non-blocking, with a veto window.** Delivery *is* success; the caller's turn
   continues and nothing awaits the human. A caller that can proceed on a
   sensible default states `deadline` + `default_action` — "unless you stop me
   by *t*, I do *X*" — and the window closing is recorded as `defaulted`, never
   as a failure. The human's reply is an ordinary user turn in the member
   thread, with the option chips on the card sending their text as that turn.
   There is no `session_wait_for_reply`; a caller that wants the answer polls
   its own thread with `session_read_message` like any other input.
2. **Attention budget.** The card's home is the member's DM thread, where the
   chat projection folds escalations by `goal`. Anything that mirrors it
   elsewhere goes through the notification bus — `system.agent`, `kind:
   escalation`, `group_key: escalation:<slug>:<goal>` — so N escalations on one
   goal stack as one entry in the bell today and route as one item once the
   per-channel "deliver to additional channels" bridge (RFC notification
   bridge) exists. There is deliberately **no transport-specific switch** on
   this path: a `slack_escalate`-style boolean would be a persisted contract
   the bridge would have to migrate on day one.
3. **Escalation is not approval.** This verb carries a decision the member is
   allowed to make on its own if unanswered. Anything the human must
   *actively grant* — objective and metric definitions, budgets, the member's
   own continuation or scope — must not travel here, and this PR implements no
   approval flow. The distinction is not blocking-vs-non-blocking
   (`ask_question` is itself non-blocking) but *who may act if nobody answers*:
   an escalation lets the member proceed on its stated default; an approval
   never does, and stays on the approval surfaces that wait for the grant.

## Configuration

`agent.session_control` (bool, default **true**). The grant that decides who may
reach a peer session is the **agent config**, not this switch: the five tools come
from the `kirocrew-dashboard` MCP server, so an agent whose spec does not mount it
never has them — the same rule as every other MCP server. A second default-off
gate on top of that only made the capability unreachable for an agent that had
already been given it deliberately, and `_install_conductor_agent()` shipping that
mount is what an explicit grant looks like.

What the switch is still for is a single withdrawal: an operator who wants the
capability gone from every agent at once, without editing each spec. So the
direction that must keep working is an explicit `false`, and `_safe_bool` is what
keeps a quoted `"false"` from loading as enabled — `bool("false")` is `True`, so a
plain coercion would give a user who wrote it in an editor that quotes values the
opposite of what they read.

A config read that RAISES still resolves to disabled rather than to the default.
That is deliberately not symmetric with the absent case: an unreadable config is a
transient fault the operator can diagnose from the log line, and refusing during it
costs a retry, while assuming the default during it would let unrelated corruption
decide an authorization question.

One consequence worth stating, because it is what the default-off gate was
protecting: the same server carries the `chat_folder_*` tools, so an agent assigned
it for folder organization has the session verbs too. Whether they prompt depends on
that agent's `allowedTools` — naming individual tools leaves the session verbs to
`hooks.on_tool_call`, while naming the whole server auto-approves them, because
`_mcp_pattern` maps a bare `@server` entry to a one-level glob and
`is_tool_in_allowlist` checks `@server` before `@server/<tool>`. The shipped
conductor is in the second class for `session_create` and `session_read_message`
(`_CONDUCTOR_DASHBOARD_GRANTS`), which is its stated operating model: its patrol
loop runs with nobody at the keyboard and must not block on an approval no one is
there to give. An operator who wants folder tools without session control names the
folder tools individually.

## What is deliberately not here

- **No delivery to a target outside the addressable set.** `session_send` writes
  into another session's conversation, but only one the same `authorize_target`
  guard admits: a channel-linked, channel-mirrored, crew-mode, incognito,
  app-scoped, unattended or cross-workspace target is refused, so the verb cannot
  reach a conversation other people are party to. The one non-session target,
  `user`, reaches only the caller's own thread or its creating member's — never
  a third session. The residual is the queued arm's
  second authorization moment, recorded above and tracked as #5911.
- **No cross-workspace or cross-machine reach.** The boundary is one gateway's
  live sessions in one workspace.
- **No deadline wakeup.** Nothing fires when an escalation's veto window
  closes: the index reads the record as `defaulted`/`expired` on the next
  read, and the member that declared the default is the one that acts on it
  — a member whose turn ends before its own deadline must arrange its own
  wake (a monitor loop or a schedule). A timer here would make the gateway,
  not the member, the actor of record for the default.
- **No waking closed sessions.** See above.
- **No writes on the read path.** `session_read_message` never changes the
  target's state, so a poll loop cannot perturb what it is measuring.

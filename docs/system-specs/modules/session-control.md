# Session Control Module

## Overview

Session control lets one of the user's chat sessions observe and interrupt
another: open a new session, fork a session so the new one carries a
transcript, stop an in-flight turn, close (archive) a session, and read a
transcript tail.
It exists because a session cannot see what its peers are doing. A session that
has spent an hour on a PR cannot tell whether the session watching the build has
finished, and today the only way to find out is for the human to switch tabs and
look. Session control lets the session ask directly.

Six MCP tools on `kirocrew-dashboard`, six strict-internal routes, and two
config switches: `agent.session_control` plus the member-dispatch bypass ceiling.
Every route is on `_STRICT_INTERNAL_API_PATHS`; an unlisted one is
unreachable in production because the caller's `X-Internal-Secret` is ignored.

| Tool | Route | What it does |
|------|-------|--------------|
| `session_create` | `POST /api/session-control/create` | Open a new, empty session in the caller's workspace, optionally filed into a sidebar folder at creation |
| `session_fork` | `POST /api/session-control/fork` | Open a new session that CARRIES a copy of a source session's transcript — the caller's own by default — the way the dashboard's Fork button does; optionally titled, filed, and cut at a fork point |
| `session_stop` | `POST /api/session-control/stop` | Stop another session's in-flight turn |
| `session_close` | `POST /api/session-control/close` | Close (archive) another session, as the tab ✕ does — heavier than stop, and recoverable rather than a delete |
| `session_send` | `POST /api/session-control/send` | Deliver a message that another session runs as its next turn, or cut it into the turn already running (`steer`) |
| `session_read_message` | `GET /api/session-control/read` | Read another session's transcript tail + liveness |

**One verb here writes into another session's conversation: `session_send`.**
Reading returns a transcript tail, stopping cancels a turn the way the Stop button
does, creating opens an empty session, and sending delivers a message that the
target runs as its next turn. Delivery is the sharpest verb and is bounded
accordingly: the body is redacted through `sanitize_outbound` before it is
persisted, it is prefixed with a `[sent by session <caller> via session_send]`
envelope so the target's transcript can never render it as something the person
typed, and channel agents are blocked from it outright.

**Delivery has two authorization moments, and both are enforced.** An idle target
runs the prompt immediately, under the authorization that admitted it. A busy
target QUEUES it, and the queue entry carries the containment that held at
admission (`containment_meta`); the drain recomputes those constraints and drops
any entry for which one is newly held (`newly_held_constraints`), so a target that
gains a channel mirror between enqueue and drain never broadcasts the delivered
text. The re-check is not specific to this module — a human-typed message into a
busy session drains through the same path — which is why it lives at the drain
rather than in each caller (#5911).

**A dropped queued delivery is reported to its SENDER, not only its target.**
`session_send` answers `started: false` when a busy target queues the message,
which on its own says the message will run later; every signal of a later drop —
the retracted queue card, the visible notice, the broadcast — lands on the
target's transcript, which the sender does not read. So the queue entry also
carries the sending slot (`send_origin_meta`, stamped at admission beside the
containment snapshot), and the drop appends a notice to the sender's own
transcript naming the target, the constraint that changed and an excerpt of the
dropped text (`notify_send_origin_dropped`); the SEL row names the sender as the
drop's `origin`. The stamp rides `meta` rather than a consumption callback
because `meta` is one of the keys a queued prompt is persisted with while a
callback-carrying entry is excluded from that write, so a callback would trade
the relay's survival across a restart for a notice that cannot survive one
either. A requeued steer keeps the stamp because the requeue copies the
admission dict onto the new entry's meta. Four cases deliberately produce no
notice: a human-typed entry carries no sender; a session that queued onto itself
already reads the target's own notice; a sender closed while the message waited
has no transcript left, and the SEL row is what keeps that outcome recoverable;
and a structurally exempt entry is never dropped at all. The report is
best-effort and does not gate the drop — withholding the message is the
authorization decision, and it must not depend on the notice landing.

**The stamp names a session, not a key, and does not survive a restart.** It
carries the sender's `_tab_id` beside its slot key and is omitted unless both are
present, because a slot key does not identify a session: a plain
`get_or_create_slot(name)` mints a fresh slot object on a free key and the
explicitly-named keys are deterministic (`cron-{job.id}`, `workflow-{run_id}`, a
channel's own), so a closed sender's key is handed to the next occupant, whose
link scope and audience are declared per creation. The notice therefore requires
the live slot's tab to equal the stamped one and otherwise treats the sender as
gone, which it is. `sanitize_restored_queue` strips the whole stamp, joining the
containment snapshot and the turn actor: those are read, but this one names a
WRITE TARGET, so a stamp carried back off the metadata line would append the
entry's own text to a session the editor does not own, with nothing that retracts
it. A delivery that outlives a restart and is then dropped reports to nobody while
the delivery itself still survives, which is the price of the stamp living in
`meta`.

**`steer: true` asks for a third outcome on a busy target.** Instead of waiting
for the running turn, the message cuts into it (`steer_into_running_turn`, the
same path the dashboard composer's mid-turn steer uses), so a caller watching a
worker go the wrong way can say so while the work is still in flight rather than
after it lands. The result names the outcome: `steered` for an injection,
`started` for a turn begun, and neither for a queued message.

Two properties keep the steer arm from being a hole in the queued arm's checks.
The delivery happens NOW rather than later, which removes the queue's waiting
window and replaces it with a narrower one: the steer RPC suspends on
`stdin.drain()`, and containment can move under that suspension.

- **Containment.** The drain re-check exists because a queued prompt runs later
  than the moment it was authorized. Nothing suspends between `authorize_target`
  and the steer call, so the text is committed against the containment the gate
  cleared. Each of the three outcomes then closes the RPC's own window in the only
  way still available to it. A steer that could not be injected re-runs the gate
  before falling back to the queue, and a target that resolves to a different slot
  OBJECT is refused (`target_moved`) — by identity, not by key string, because a
  session closed and resumed under the same name compares equal while the delivery
  would land on the replaced object. A requeued steer becomes an ordinary queued
  entry and faces the drain's re-check, unexempted (see Provenance), and its
  admission stamp comes from the SEND's gate rather than from reading the slot at
  teardown. That distinction is the whole protection: the teardown runs past the
  RPC's suspension, so a slot read there folds a mirror linked during the
  suspension into the baseline, and the drain then reads the widened audience as
  one the authorization saw. A SUCCESSFUL
  steer cannot be recalled — kiro-cli has acknowledged consumption — so instead the
  containment holding at admission is compared against the containment holding
  after the RPC, and if a constraint newly holds, the turn is stopped. That is a
  cooperative cancel with `escalate=False`: one automatic decision, not a person
  who watched a stop fail to take. It matters because
  `_deliver_cross_surface_reply` resolves the mirror LIVE at reply-delivery time,
  so a mirror linked mid-turn is a real audience and not one fixed at turn start.
  The delivery is still reported as successful — it happened, and an `ok: false`
  would invite a retry that double-sends — with the constraint names recorded in
  the audit trail under `steer_stopped_on`.

  The stop alone would not be enough, because it reacts and the turn can finish
  first: `_deliver_cross_surface_reply` runs from the turn's own completion path and
  resolves the mirror LIVE, so a turn that completes inside the RPC's suspension
  publishes before any post-RPC check resumes, and a sent reply cannot be recalled.
  So the send also RECORDS the containment it was admitted under on the slot, before
  the RPC and synchronously with the authorization, and that record stays for the
  whole turn. The publisher then asks `cross_surface_withheld` at delivery: it
  compares the containment holding THEN against each recorded admission and withholds
  the cross-surface leg when a constraint newly holds.

  The decision lives with the publisher because that is the only moment it cannot go
  stale. A check the sender runs when its RPC returns says nothing about a mirror
  bound between then and the reply, and the reply is what reaches the channel. So the
  sender records and, on what it can see, stops the turn; whether the reply may be
  published is the publisher's question, answered in the same synchronous moment it
  publishes.

  Two consequences worth stating. An ordinary steer costs the channel audience
  nothing: the comparison is exact rather than precautionary, so a turn nobody
  interfered with publishes normally. And the record is TURN-SCOPED -- the teardown
  empties it unconditionally -- so one turn's withheld reply never judges the next by
  an authorization that was never about it.
- **Provenance.** Both arms hand over the same text: redacted through
  `sanitize_outbound` and prefixed with the
  `[sent by session <caller> via session_send]` envelope. So an injected steer
  can no more pose as human typing than a queued delivery can. This is the
  property the composer's own gate protects by keeping app-authenticated sends
  off the steer path; here an explicit envelope answers it.

  It reaches one level further down. `directive_user_origin` exempts a queue entry
  from the drain's LINKED drop because "the author typed into the session's own
  surface", and the requeue used to derive that from the slot — sound while the
  composer was the only caller of `steer_into_running_turn`, wrong as soon as
  `session_send` became the second. Provenance is now REPORTED by the caller
  (`user_origin`) and recorded per in-flight steer, defaulting to false so a future
  caller cannot acquire the human's exemption by saying nothing. The composer keeps
  it; a peer's steer does not.

A steer is a delivery MODE, not a permission: `authorize_target` decides who may
send, unchanged, and a crew-bound (`executor == "remote"`) target stays refused
before either arm is reached. `steer` is strictly typed at both entry points (the
tool schema and the HTTP handler) and defaults false, so a caller that omits it
keeps the queue-or-run behaviour.

**A target mid-plan is busy even when `running` says otherwise.** Both arms read
`slot.running or slot._in_stage_execution`, the predicate every producer that must
not start a concurrent turn reads — the composer, the cron injection, the nudge arm
and the transfer gate. Between a multi-stage plan's stages each stage's `_run_chat`
closes its own turn, so `running` reads False while the plan is still live, and
`enqueue_or_run_prompt` gates on `running` alone: handing it a prompt there starts a
second turn racing the plan, with no recovery once two turns own the same slot. So
an inter-stage send is queued with the same admission stamp that method applies and
held until the plan ends. There is no steer client in that window either, so a steer
falls through its own re-gate to the same queue branch.

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
part of creation (#6118). The caller's OWN slot is filed the same way with
`chat_folder_file_self` (folder tools, same server): it takes no `session`
argument, resolves the target from the verified caller key, and so can be
granted where `chat_folder_move_session` is withheld — a conductor files itself
in the goal's folder and then creates its workers under `<goal>/<agent>`. Filing used to be a second call
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

### `session_fork`: a created child that carries a transcript

`session_create` opens an empty session, and the case it cannot serve is the
one that produced this verb: an agent that has spent a long investigation in one
session and now wants to split the follow-up into several, each of which should
START with what was found. Writing the findings to disk and seeding each child
with a pointer is the workaround; the dashboard's own Fork button already does
the right thing for a person, and `session_fork` is that button reachable from
the MCP surface.

**One fork core, two entry points.** `chat_fork.api_chat_slot_fork` (the human
route, `POST /api/chat/slots/{slot}/fork`) is a thin wrapper — slot lookup, slot
cap, App Kit ownership, body parsing — around two shared coroutines:
`resolve_fork_source`, which freezes the parent's memory identity and refuses a
parent whose persisted mode is unrecognised, and `fork_slot`, which does
everything from the transcript snapshot (the pending-rewrite flush, the
consistent read under `_fork_lock`, the rotated-archive rebuild) through the
child's mint, message copy, save and the deleted-source rollback. The
session-control verb calls the same two, so what an agent's fork copies and what
it inherits — agent, model, memory store and mode bound at birth, project,
folder, tags, `forked_from` — is by construction what a person's fork copies
and inherits. The human route's behaviour is unchanged by the split; the core
takes the request-derived facts as arguments (`request_app`, `origin`,
`count_user_session`, `jev_route_allowed`, the SEL caller and operation) and the
two routes answer them differently:

| Fact | Human route | `session_fork` |
|---|---|---|
| `origin` | `request_slot_origin(request_app)` | The caller's authority, as `create_session` derives it: `CRON` for a cron caller or a cron's descendant, else `USER` |
| `count_user_session` | `True` — a human request-layer session for the pulse survey | `False` — an agent-made session is not one (`test_session_pulse_session_count` sweeps for the literal, so the core takes it as a parameter and the only literal `True` stays in `chat_fork.py`) |
| `jev_route_allowed` | `is_owner_dashboard_request` | `False` — arming a second routed session is the owner's own click, which no agent caller made; the child keeps the parent's pinned `model` |
| SEL operation | `chat.slot_fork` | `session_control.fork`, on the core's rows and on this module's own `_audit` row (`session_fork`), so the two entry points stay distinguishable |

The core's refusals are `web.json_response` objects — that is the module's
refusal shape and its coded sites are what the error-code ratchet
(`test_chat_fork_error_codes`) pins, so the split did not re-type them.
`fork_session` reads the `{error, code}` body back into a `SessionControlError`
(`_fork_refusal`), so a caller sees `value_out_of_range`,
`no_messages_to_fork`, `fork_snapshot_unstable` and the rest under the same
codes the human route reports.

**Authorization is the union of two existing rules, not a third.** A fork
manufactures a session the caller then owns, so the caller must be an eligible
CREATOR: `_refuse_ineligible_creator` runs on entry and again adjacent to the
allocation, exactly as in `create_session`, plus the same switch, unattended
and identification gates in the same order. Copying a source's transcript is a
READ of it, so a `source` that is not the caller goes through `authorize_target`
with operation `fork` — the same verb-level requirement, fence and refusal
codes `read_messages` has (`ephemeral_target`, `workspace_mismatch`,
`linked_session_target`, `not_creator`, and so on; the fork tests assert the
codes match read's, case for case). Those decisions are re-asserted at the act,
not only at admission: `fork_session` hands `fork_slot` a synchronous `recheck`
that it runs immediately before minting the child and again after the memory
bind, before any row is copied. The recheck re-runs the caller's eligibility,
the source's liveness, `authorize_target` for a peer source (so a source that
gained a channel mirror or moved workspace mid-fork refuses with read's own
code), the folder's existence against the committed folder list (so a folder
deleted mid-fork refuses `folder_not_found` rather than filing the child under a
dangling id), and both slot ceilings (so two forks in flight cannot each pass the
entry check and both land over the cap). The caller's trust posture is read
inside the stamp, in the same synchronous window, so a grant revoked while the
transcript was being read is not reapplied to the child. Behind that, the source's workspace is part of the identity
`resolve_fork_source` freezes and `fork_slot` re-compares, which is what catches
a workspace move on the caller's OWN transcript, where no target check runs
(`store_unavailable`, retryable). The caller's own session is the one target
`authorize_target` cannot admit (`self_target`), and it is the DEFAULT source
here: a session copying its own transcript crosses no boundary, so an omitted
`source`, and a `source` that resolves to the caller, both take the self path
and skip the target guard while keeping the creator gates.

**What the child gets on top of the human fork**, applied by a `stamp` callback
`fork_slot` runs on the child before its birth save, so it lands in the same
metadata line as the transcript and is on disk before the slot is broadcast (no
second persistence window; a failed save withdraws the whole child): `title` (else the fork's `↳ Fork of <parent>`),
`folder_id` (else the parent's folder, which the human fork inherits; an
unknown folder refuses the whole fork before any copy, confirmed read-only under
the folder-store lock like `create_session`, and the Model-B un-hide runs only
after the filing has landed), creator attribution (`created_by` = the caller,
plus `_created_by_sid` / `_lineage_minted` as at a create, so the other verbs
reach the child and the per-creator ceiling counts it), and the caller's
session posture — `_trust` and `_trust_reads`, with `_trusted_patterns` and
`_trust_scope` excluded for the reasons "What a created child inherits" gives.
A fork spends the `session_create` rate budget and is bounded by the same live
and per-creator slot ceilings; it is a session the caller manufactured, whatever
it starts with.

**Deliberately not offered.** No `agent`, `model` or `mode` override: a
transcript belongs to the memory boundary it was written in, and `chat_fork`
binds the child's execution identity from the parent's for exactly that reason.
No tail fork (`direction` is fixed to `head`); the human route keeps it behind
`dashboard.tail_fork_enabled`. No `prompt`: the child starts idle, and seeding
it is `session_send`'s job — the same split that keeps `session_create` from
being the front half of a delivery.

### What a created child inherits

Creation copies two different kinds of state, and the split is deliberate.

**Identity** — the child is created in the caller's workspace (the memory
boundary; a child left in `default` would be both a boundary crossing and
unaddressable by its own creator), inherits the caller's agent when none is
named, takes that workspace's project directory as its cwd, and is attributed to
the caller via `created_by` so the per-creator slot ceiling is countable. The
caller's ACP session id is also frozen onto the child at this mint
(`_created_by_sid`), from the live caller handle, so the child's `session/opened`
lineage cites the creator that was live when it was made rather than a
replacement that may take the creator's slot before the child's first turn. The
id is backend-authored, so it is bounded where it is retained: one past
`MAX_ACP_SESSION_ID_LEN` (`kiro_crew/validation.py`, the constant every store of a
backend session id shares) is dropped at mint, never truncated -- the sid
is optional and absent is a legal record. The mint also sets `_lineage_minted`, the
in-memory witness that THIS process stamped both fields; neither the witness nor
the sid is written to the transcript, which an agent's file tools can edit, so a
metadata edit cannot forge the gateway-authored lineage the fenced crew log
records (see `crew-log-emitter.md`).

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
read on entry. Creation awaits project and agent resolution, execution identity
validation and optional folder confirmation before the slot exists.
An operator selecting `normal` in any of those windows would otherwise have a revoked posture resurrected by a
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
verb that takes a target (`stop`, `send`, `close`, `read`, and `fork` when its
`source` is another session), so a guard cannot be
present on one and missing on another. (`session_create` has no target to
authorize; it checks the caller's own eligibility with the same refusals, and
`session_fork` applies both: the creator eligibility for the caller, and the
target guard for a source that is not the caller itself.) Every refusal is recorded in the SEL as
`session_control.<op>` with `outcome=denied`, so an attempt to reach a session
that is out of bounds is visible after the fact even though nothing happened.

| Refusal | Status | Why |
|---------|--------|-----|
| Config switch off (`agent.session_control` explicitly `false`) | 403 | Operator withdrew the capability from every agent at once. Defaults to true — the agent's `kirocrew-dashboard` mount is the grant. **Exception:** a crew member bypasses this switch while `agent.member_dispatch` is true (its default) — recognised either as a `member-*` DM caller key OR as a chat slot bound to that member's private V2 store; see "Member callers" below |
| Caller session cannot be identified | 403 | An unidentifiable caller makes the self-target guard blind |
| Caller is an unattended session (`workflow-*`) | 403 | A `workflow-<run_id>` slot exists only once its originating tab is gone, so there is no owning session to fence it to. **Exception:** a cron slot (`cron-*` caller key) is admitted and fenced by creator ownership instead — see "Cron callers" below |
| Caller is itself incognito, temporary, or app-scoped | 403 | Caller-side isolation — the direction the target-side checks cannot see |
| Caller is an APP-owned cron (`created_by` starts `app:`), or a cron whose job cannot be found | 403 | `app_owned_cron_caller` / `cron_owner_unverifiable`. A cron tab is minted without `app=`, so the `_app` check above cannot see an app's own scheduled job; ownership is read from the JOB instead, and an unverifiable owner fails closed — see "Cron callers" below |
| Caller is channel-linked (`linked_session_key` set) | 403 | The exfiltration direction: a linked caller's conversation IS a channel thread, so a read would hand a private dashboard transcript to that channel's readers. `CHANNEL_AGENT_BLOCKED_TOOLS` keys on the agent identity; a linked slot is a second route to the same surface. **Two exceptions:** a `cron:<job_id>` link, which names the job's own run transcript and republishes to nobody; and a 1:1 DM whose only human is the configured owner (`audience_is_owner`) — see "Owner-DM channel callers" below |
| Caller's own session is no longer open | 403 | Nothing to attribute the operation to |
| Caller changed workspace while a creation was in flight | 403 | Creation resolves the workspace's project directory off-loop, so it suspends between authorizing the caller and allocating the slot. Both decisions that read the caller's workspace -- the memory boundary the child inherits, and whether the answering agent is bound to that workspace -- are invalidated by a move, and re-deciding the binding here is not available: it needs a config load, which must not run on the event loop |
| Named agent does not resolve to a configured one | 403 | The resolver falls back to the default agent, which passes the workspace check because it is the caller's own default -- so no boundary is crossed, but the created session would store and advertise a name that is not what answers. `ResolvedBindings.requested_resolved` states that contract for callers that store the requested name. Refused rather than rewritten to the effective agent: nothing exists yet, so a corrected name costs one retry, whereas an existing slot keeps its stored name verbatim so a momentarily stale resolution cannot permanently rebind it |
| Caller may not bind a child to the selected member's private store | 403 | `memory_delegation_denied`; one check, on the route every branch of agent resolution has already produced, before slot allocation. Two admissions: the store is the caller's OWN, which requires this process's vouched identity and the caller's durable record to agree, or the caller is not ownership-fenced. Same-store workers remain allowed; unfenced global callers retain member assignment. See "A created worker receives one execution identity" |
| Caller changes history key, agent or memory store during creation | 400 | `caller_memory_changed`; the live caller must still match the identity checked before awaited preparation |
| Target is the caller | 403 | A session controlling itself has no exit |
| Target is unattended (`cron-*`, `workflow-*`) | 403 | A `workflow-<run_id>` slot is display-only and a cron's turns are driven by a schedule. Not exempted for a cron CALLER: a cron may create and drive its own children, never another job's tab |
| Target is incognito or temporary | 403 | Never addressable, matching `list_sessions` |
| Target is app-scoped | 403 | App sessions are the app's, not a peer's |
| Target is channel-linked (`linked_session_key` set) | 403 | Its conversation is mirrored to Slack/Telegram, so reaching it crosses a surface boundary both ways — and its stop cannot be honoured, because the stop path addresses `dashboard:<slot>` while a linked slot's turns run under its linked key |
| Target or caller has an outbound channel mirror (`get_mirror_link`) | 403 | The same boundary reached by the other mechanism. `linked_session_key` marks a channel-BORN slot; a dashboard-born slot given a mirror link republishes its turns to a channel just as surely, and the link lives in the session store rather than on the slot, so the slot-side check reads empty on exactly the session that mirrors. **Caller-side exception:** an owner DM whose mirror IS its own conversation — the same audience, established by `audience_is_owner` before either caller-side channel refusal runs |
| Target is in another workspace | 403 | Workspaces are the memory boundary |
| Target names no open session | 404 | A mistake, not an authorization failure |
| Title matches more than one session | 409 | Guessing means acting on the wrong conversation |

### Member callers: switch bypass, bounded by creator ownership

A crew member is a **conductor by design**: it dispatches work into worker
sessions it creates, patrols them, and reports back, with no operator
configuration. A member runs in TWO kinds of slot, and both are that operating
model rather than an optional capability, so both are recognised as a member
caller (`_member_caller`, the single predicate the switch bypass and the
ownership fence both read):

- **(a) its pinned DM slot** (caller key prefixed `member-`, created only by
  `POST /api/members/{slug}/thread`); and
- **(b) an ORDINARY dashboard chat slot** (`chat-<n>-<ts>`) whose bound memory
  store is that member's private V2 store — the store a DM slot would be bound
  to. A member agent (e.g. `kirocrew-conductor`) also runs in an ordinary chat
  slot bound to its V2 store, and its whole operating model (`session_create` /
  `session_send` / `session_read_message` / `session_stop` / `session_close`)
  runs from there, so refusing it in a chat slot would leave the member
  chat-only in the surface it exists to drive. The identity here is the STORE,
  not the key: a store counts iff its config record carries a non-empty
  `owner_member` AND `memory_version == 2` AND that owner is still an active
  agent bound to exactly this store (`_store_is_member_owned`, read from the
  loaded config record — never the on-disk manifest, which would be blocking IO
  at the synchronous fence — and **failing closed** on an unreadable or degraded
  `memory_stores` section). Admission never widens to a private V2 caller whose
  store is not a crew member's. The gate carries the admission it made into the
  inner fence rather than letting the fence re-read the record later — see "A
  member-created worker stays inside its own private memory" below.

Two rules give a member caller its shape:

- **The `agent.session_control` switch does not gate a member caller.** Members
  work out of the box — this is the zero-configuration contract, and it is a
  deliberate trade-off: an operator who turned session control off has NOT
  thereby disabled member dispatch. The operator ceiling on that bypass is
  `agent.member_dispatch` (bool, default **true**). Left at its default it
  reproduces this exactly — the member bypasses the switch. Set to `false`, a
  member caller stops bypassing and falls back under `agent.session_control`
  like any ordinary caller, so an operator who withdrew session control can
  keep member DM threads chat-only without disabling the member itself. The
  ceiling is read at the switch gate (`member_dispatch_enabled()`) and, like
  the switch, **fails closed** — an unreadable config withdraws the bypass
  rather than granting it.
- **A member caller may only act on sessions it created.** Slot creation records
  `created_by` (the creator's caller key) in the slot's birth metadata; it is
  persisted with the session and rehydrated on restart (both restore paths).
  `authorize_target` refuses a member caller whose key does not match the
  target's `created_by` (`not_creator`, 403) — and this ownership boundary binds
  **even when the global switch is enabled**, so a member never widens to the
  ordinary caller's reach. It binds identically for case (b): the chat-slot
  member is fenced to the workers it created, never the user's own sessions.
  Every other refusal in the table above still applies to member callers
  unchanged.

Ordinary (non-member) callers are untouched: they still require the switch.

#### The strict-internal surface admits a member DM slot, not every scoped caller

The five routes sit behind `_require_internal`, which first refuses anything
without a valid `X-Internal-Secret`. On the authenticated branch,
`_private_caller_refusal` captures the caller's canonical execution scope once
off-loop through `internal_memory_scope`, without opening learned memory:

- an **owner / Global-V1 caller** (no store scope) falls through to the handler,
  exactly as the surface behaved before member dispatch existed;
- a **crew-member DM slot** (a `member-*` session key) is ADMITTED while the
  surface is reachable for it — `agent.member_dispatch` OR the global
  `agent.session_control` switch — so its request reaches `session_control.py`
  where the creator-ownership fence above does the real gating;
- **every other scoped caller**, including a member while BOTH switches are
  off, keeps the `member_scope_denied` 403;
- an **unavailable or mismatched execution identity** returns
  `member_identity_unavailable` 409.

The gate reads the SAME two switches the switch gate does — `member_dispatch` is
a bypass ON TOP of `session_control`, not a replacement, so a member with
`member_dispatch` off falls back UNDER the global switch rather than out of a
surface the operator left open to everyone. All reads fail closed on an
unreadable config, so the surface can never open wider than the two switches
behind it. The member DM admission does not widen other scoped callers' access
or replace the creator-ownership checks in `session_control.py`.

#### A created worker receives one execution identity

`create_session` captures the caller's canonical execution before asynchronous
project or template resolution. An omitted member inherits the caller; an explicit
member uses its existing stable member/store identity under the ordinary
delegation rules. Template and project choices do not select memory. An explicit
TEMPLATE named by a caller that carries an execution keeps that caller's store and
member identity and takes the template's selection namespace — `selection_kind`
`"template"`, the requested name as `selection_name`, the resolved provider
template as `template_id` — through `ExecutionContext.with_template`, the same
rewrite the subagent admission gate makes for `spawn_run(agent=…)`. The record
therefore says whose memory the child runs on and, separately, what was picked to
run it: a member's child that selected a template is that member's delegate, and
`ContextBuilder` reads the namespace to withhold the member operating protocol from
it (see [memory-skills-hooks](memory-skills-hooks.md)). A caller whose member has
no persisted `member_id` is the one exception: its member is named by the selection
alone, so the arm keeps that selection and changes only the template with its own
`replace` — `with_template` would leave the child attributed to no member, which is
the shape the spawn gate mints for that caller. The child's
execution record is published before slot metadata, broadcast or provider startup.
Publication failure retracts an idle empty child and reports the actual failure.

Member scope is not itself the thing that bounds cross-member delegation — the
authorization below is. The existing session-control switches, creator ownership
fence, application scope and approval policy remain independent. Memory identity
failure is explicit and never substitutes Global.
Incognito and temporary children inherit the stricter retention mode.

#### Who may bind a child to a private store

Selecting an agent is a caller-supplied string, so it cannot itself authorize the
private V2 store that agent names. `create_session` therefore authorizes the
child's private binding ONCE, at the single point where every branch of agent
resolution has produced its final route, and before the slot is allocated. Placing
it per branch is what left the surface open: the explicit member selection, the
inherited caller execution and the caller-agent fallback all reach a member store,
so a check on one of them leaves the others.

A private member store is reachable on two authorities and no others:

- the store is the caller's OWN, which needs TWO sources to AGREE: this process's
  own vouched identity for that session, and the session's durable execution
  record. Neither alone is admissible. The record is metadata on the caller's own
  transcript, so by itself it answers a question about the caller with the
  caller's own claim. The vouched identity is written only by
  `bind_session_execution`, which no session can reach, and only when the store it
  publishes was established independently of that record — a publication that
  carries the owner over FROM the record, as a provider template switch or a
  dashboard fork does, does not vouch for it, because vouching a value the session
  chose would make the two sources one. Claiming it is OPT-IN: the default is not to
  vouch, so a binder that says nothing about provenance publishes the record and
  claims no authority, and a caller that should have claimed it fails loudly at a
  refused dispatch rather than quietly widening access. A member-LESS identity is never
  vouched even when its caller asks: no member means the Global store, and the admission
  identifies its caller by member, so the entry could never be admitted.
  A record published elsewhere can also leave a vouched entry behind.
  Agreement therefore fails closed against a forged
  record and against a stale vouched entry alike. `slot.agent` and
  `slot.memory_store` remain inadmissible, and not only because a later write can
  change them: both are rehydrated from that same record on restore;
- the caller is not ownership-fenced, which is the owner's own dashboard session.
  This keeps the shipped capability: an owner reopening member conversations and
  dispatching member workers.

The vouched half is held in this process only, so a restart drops it while the durable
records survive, and the own-store admission is refused until the owner re-selects the
agent — which binds afresh through the durable path and vouches again. That deferral is
deliberate rather than an oversight: nothing reachable on the rehydrate path can
re-establish the authority safely, because every candidate resolves through something the
session itself can influence. The record is written by the session; `slot.memory_store` is
rehydrated from that record; the execution the selection path carries is built from it on
the provider-switch path; and a config lookup there is keyed by that record's own
`member_id`, so re-reading config agrees with a forged record by construction instead of
checking it. Refusing is the fail-closed direction, an owner's own dispatch is unaffected,
and the refusal is pinned by a regression test alongside the re-bind that clears it. The
authenticated identity that would let a rehydrated session self-heal without an owner
action is tracked separately as #12528.

For an operator, the recovery is one owner action and nothing at restart time: a member
session whose worker dispatch answers `memory_delegation_denied` after a gateway restart
regains it as soon as its owner re-selects that member's agent on the slot, which binds
afresh through the durable path and vouches again. The same action clears a refusal
caused by cap eviction, since both reach the admission as an absent entry.
Re-selecting the agent the slot already names is enough: the owner-facing switch
records the selection with `replace`, so it re-binds rather than short-circuiting on an
unchanged choice. Closing a tab is NOT such a trigger — a non-destructive close and an
idle archive both retain a persistent session's vouch, because the conversation is
recreated from the warm pool on resume and the turn-start rebind publishes nothing when
the selection has not changed, so withdrawing there would charge a restart's refusal to
closing a tab. The refusal
the caller sees names neither store nor member, so it is the server-side cause line that
tells an operator a dropped entry apart from a record that disagrees with what this
process committed; a member cannot diagnose which it hit from the refusal alone.

The vouched half is also BOUNDED, by one named count cap. The population is not the
set of live sessions: every persistent `bind_session_execution` that establishes its
own store vouches, and several
of its callers mint a key per REQUEST rather than per session — a webhook that sends
no `sessionKey` gets a per-second one, a task runner refine run gets one per run — so
uptime alone would grow the map until the process restarted. Each such producer
releases its own entry at teardown where it has one, and the cap is the backstop for
the producers that do not. Passing the cap evicts the LEAST RECENTLY USED entry — a
successful own-store admission refreshes its entry, so recency follows USE rather than
birth and churn from the teardown-less producers falls on idle keys instead of on the
member session still dispatching through its own. Eviction refuses that session's
own-store admission until it binds again: the same deferral a restart
carries, in the same fail-closed direction, and it never touches a durable record.
Overflow is counted and reported, so an evicted entry is distinguishable from one
never vouched — both read as absent. A refusal also records its CAUSE server-side —
authority this process does not hold, against a record that disagrees with what it
committed — so an operator can tell a restart-dropped or evicted entry from the forgery
the agreement exists to refuse. The caller's own refusal still distinguishes neither,
and the log names neither store nor member, so it is not a second disclosure channel
for what the refusal withholds. Two bounds are declared, one per dimension the map
adds: the entry COUNT, and the length of each retained STRING, since 4096 rows of an
unbounded field is unbounded. The string bound is the repository's shared bound for
names and ids, and an execution carrying an oversized field is DROPPED rather than
truncated — a truncated identity would compare equal to the session that owns the
shortened form. The retained key needs no bound of its own: the vouch runs strictly
after the durable write, so the map cannot hold a key the record cannot carry.

Everything `_caller_is_ownership_fenced` already treats as untrusted is refused
with `memory_delegation_denied` (403): a cron slot, a member DM slot naming a PEER
member's agent, and anything either of them created — the fenced caller's unfenced
deputy. An app-token caller never reaches the route (`internal_secret_required`)
and an app-scoped one cannot create at all. The refusal names neither the store nor
the member, so it cannot confirm a guessed agent name.

The verdict is the one the HTTP gate settled on the caller's verified scope,
carried in as `caller_fenced` exactly as the other routes carry
`precomputed_ownership_fenced`; absent, it is evaluated inline. It is only ever
read as a REFUSAL, so a config record that stops saying "member" between admission
and this check can turn a refusal into an admission the owner already holds, never
the reverse.

A session with native provider context cannot change members in place. An unused
chat may select a member only with selection revision checks covering prewarming,
resume pointers and late provider completion. Interactive creation and
`session_create` use the same execution selection rules.

### The fence propagates to what a fenced caller creates

`_caller_is_ownership_fenced` covers three populations, not two: a member DM slot,
a cron slot, and **anything either of them created**. The third is the one a key
prefix cannot see, and without it the fence buys nothing. A created child is minted
with a plain `chat-` key and INHERITS its creator's agent, so a fenced caller
running a session-control agent would otherwise get an unfenced deputy for free:
create a child, seed it, and the child — an ordinary caller by key — reads any
same-workspace session and reports back through the transcript its creator is
allowed to read.

The case-(b) member is kept FENCED by the decision the HTTP gate already made,
not by re-reading its member status at the fence. The gate admits a member on the
caller's VERIFIED private scope; the inner fence, left to itself, would re-derive
member-ownership from the MUTABLE config record via `_caller_is_ownership_fenced`
→ `_member_caller` → `_store_is_member_owned` — and that read happens after the
body read and the prewarms have suspended. An operator's own config writer can
change the record in that window in ways that are all legitimate writes, not
corruption: un-assign the member (`owner_member` cleared, `memory_version` still
2), drop `memory_version` (the loader coerces a missing key to `1`), or drop the
entry outright (a malformed entry is silently discarded). After any of them the
record no longer says "member", `_member_caller` goes False, and the fence would
collapse to `bool(_created_by)` — so a chat slot with no `_created_by`, with the
global switch on, would reach a foreign same-workspace session it did not create
(a silent cross-session transcript read). Guarding that by classifying the store
more finely does not hold: whatever property of the record the fence keys on, a
config write can remove it.

So the verified admission travels with the request instead. `_private_caller_refusal`
marks the request when it admits a crew member (either spelling), every route reads
the mark back (`_carried_fence`) and hands it to `stop_target` / `close_target` /
`send_to_target` / `read_messages` as `caller_fenced`, which forward it to
`authorize_target` as `precomputed_ownership_fenced=True`. A member admitted as one
is creator-fenced for the whole request, whatever the record says a beat later. An
owner / Global-V1 caller carries nothing (`None`) and its fence is evaluated inline
exactly as before member dispatch existed — so a plain chat tab bound to a legacy V1
NAMED store keeps the owner reach the gate admitted it with; nothing about a
non-`default` store narrows it to creator-only.

The switch BYPASS is the one thing that still reads the record live, at every gate
(`_member_bypass` → `_member_caller`): a member the operator un-assigns loses its
bypass at once and falls back under the global switch. That direction can only
tighten, so re-reading there is correct where re-reading at the fence was not.
`_store_is_member_owned` itself is a single fail-closed boolean — `True` only for a
record with `memory_version == 2`, a non-empty `owner_member`, and that owner still
an ACTIVE agent bound to exactly this store (a deleted crew leaves its store record
behind with `owner_member` set but no agent, and must not keep the bypass); every
other answer, including an unreadable config or a degraded `memory_stores`
section, is `False`, which withdraws admission and the bypass and can never open
the surface wider than it is.

`_created_by` is the marker, and it needs no lineage walk: `create_session` is its
ONLY writer, so a non-empty value means "an agent made this session" at any depth.
A grandchild carries its parent's key there and is fenced by the same test, and a
chain whose middle slot has been closed cannot fail open because no chain is
walked. A person's own tab and a fork reach `get_or_create_slot` directly and stay
unattributed, so ordinary human use is unaffected.

The same attribution is the one lineage fact the child's append-only crew log
records: its `session/opened` carries `parent {slot, sid?}` -- `_created_by` as the
slot, and `_created_by_sid`, the creator's ACP session id frozen at mint from the
live caller handle -- but only while the slot carries the in-memory mint witness
(`_lineage_minted`); the `created_by` restored from transcript metadata after a
restart serves this fence and never the crew log (see `crew-log-emitter.md`). The
fence above reads the slot; a fold that builds the tree of sessions reads the crew log.

#### A member-created worker can itself dispatch — the nested-conductor design

Case (b) keys member identity on the STORE, and `create_session` binds a member's
child to that member's own V2 store at birth (the execution record published in
`_persist_birth`, admitted by the own-store authority above). So a worker the member
spawned is ALSO on a member store, which means `_member_caller` case (b) is true for
it too: with
`agent.member_dispatch` on, a member-created worker passes the HTTP gate and
`_member_bypass` and can `session_create` its own children — grandchildren of the
original member — without the operator's global `session_control` switch. This is
INTENDED, not an accidental widening: the conductor model is recursive by design (a
`kirocrew-conductor` dispatches a `kirocrew-conductor` child for a sub-goal that
itself decomposes), and depth is bounded elsewhere — the conductor agent caps
nesting (children may conduct, grandchildren may not), not this fence. Every node
in that tree stays bounded by the SAME ownership fence: a worker reaches only the
grandchildren it created itself, never the user's own sessions and never a sibling
worker's, because `_created_by` is checked at each hop. The population the
store-as-identity predicate admits is therefore "a crew member and its own dispatch
tree", each member-store node fenced to what it created — the recursion carries the
operating model down without carrying reach across it.

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
in the entry's env, plus `KIROCREW_BOUND_PORT` — the entry's env is built from
scratch rather than inherited, and a child left to rediscover the port falls
through to the run-marker check, which needs an `lsof` view the sandbox's user
namespace does not have, so a gateway on any non-default port would be dialled
at the default one) — both establishment paths, because `session/load`
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
claude seam, KAS, codex and opencode); kiro-cli v2 reads its template from disk and
exposes no per-session channel, so a member session on it runs as plain chat —
the tools are simply not mounted, never mounted-and-refused. Codex qualifies
because `providers/mirrors/codex.py` already gives it a per-session array and
its routing is `SESSION_CONFIG`, a member of `ENFORCED_ROUTINGS`, so
`_apply_session_permission_routing` refuses the session outright when
`mode=read-only` cannot be armed — what was missing was the decision, not a
mechanism. `tool_gate.is_enforced` is true for opencode as well, on
`VERIFIED_SEEDED_SETTINGS`: the value is seeded into the child's environment and READ
BACK from the harness's own config resolution before the first prompt, so a session
that cannot establish the asking posture is refused there too, and
`providers/mirrors/opencode.py` documents `permission_surface_owned` as
accepted-and-ignored for exactly that reason.

Which code appends the entry depends on who composes the array.

### The crew panel rides the same vehicle

A crew member's DM session mounts a SECOND session-level server, `kirocrew-panel`,
by which the member publishes its own webview: `panel_publish` sends a JSON object
and names a template, and the Members page Dashboard tab renders it. Same vehicle,
same reason. The server is `opt_in` in `agent._MANAGED_MCP_SERVERS`, so no spec
emits it, and the Capabilities editor's "Add configured MCP connection" list is
built from CONFIGURED connections rather than from host-managed opt-in servers, so
it never appears there either. Without this mount the only remaining grant surface
is a hand-typed custom connection colliding with the managed name, and no crew can
publish a panel at all.

The element is `members.member_panel_session_server`, composed by the one writer
`members._member_session_element` that also builds the dispatch entry, so both
carry `KIROCREW_SESSION_KEY`, `KIROCREW_BOUND_PORT` and the managed `KIROCREW_HOME`
override by construction. On the KAS backend the wire projection grants
`@kirocrew-panel` in `tools` plus `_MEMBER_PANEL_GRANTS` in `allowedTools`,
ceiling-filtered like every other grant.

Both panel verbs are approval-free, and the reason is worth stating because
`mcp_panel`'s own module doc forbids an `autoApprove` key on that server. The two
paths differ in exactly the thing that rule is about: an `autoApprove` key is
resolved inside kiro-cli, emits no permission request, and so skips
`hooks.on_tool_call` and the governance ceiling with it, while a grant in
`allowedTools` is filtered by `kas_agents._ceiling_permitted` through
`may_skip_gate_now`, which fails closed. `panel_templates` is a read.
`panel_publish` is a write, and it passes the invariant the dashboard grant sets
are judged by -- a granted verb may create or read, never mutate something that
already exists and is not the agent's own -- because the panel it writes is the
calling crew's own: the server takes no crew or session argument, resolves the
publishing crew strictly from the calling session, and refuses a subagent rather
than walking `/proc` ancestors to its parent's panel.

Two operator switches, asked per server rather than shared. `agent.crew_panel`
(bool, default **true**) is the ceiling, read through `members.crew_panel_enabled`,
which fails closed on a raising read AND on a config that loaded having discarded
the `agent` section -- `load()` coerces a malformed section away and falls back to
the permissive default, so trusting that default would be a fail-open. A
whole-server `disabled` on `kirocrew-panel` withholds the mount too, for the reason
it withholds the dashboard one. Neither switch is inherited from the other server's
answer: an operator who withdrew session control keeps the drawer, and one who
switched the panel off loses only the panel. The grant follows the mount in the
same call (`AcpRuntime._mount_member_panel` returns both), so a switched-off server
is never both named in `tools` and pre-approved on the session that is not mounting
it.


`AcpClient._append_member_dispatch_server` serves the backends whose array the
CLIENT builds — claude's and opencode's — and honours the permission-surface
precondition there for an UNENFORCED routing only: claude's is `SEEDED_SETTINGS`,
declared and not enforced, so owning `settings.local.json`
(`_claude_settings_authored`) stands in for the read-back this core does not have,
while a harness whose routing is enforced must not be held to a file it never writes.
A runtime-served harness never reaches that helper: codex's array comes from
`AcpRuntime._mirrored_session_mcp`, and `create_session` / `load_session` append the
member entry themselves keyed on a non-empty `member_session_key`, which
`AcpProvider._member_session_key` returns only for a member key on a backend in the
set. The agent spec cannot supply the server instead:
`mirrors.identity.identity_bound_crew_servers` withholds the spec-described spelling
of it, because such an element carries no session identity and would answer
`identity_unattested` to every verb.

Two operator switch-offs bind the mount, and both are asked wherever the array is
composed. Switching the dashboard server off WHOLE (`disabled`) withholds it with no
backend condition: the form has no per-call spelling, so no harness can refuse a call
to a server it was handed, and the `tools` allowlist that keeps a disabled server out
of the spec-described half of the array does not reach an element a composer appends
itself. `AcpClient` reads the projection's `disabled_servers`; `AcpRuntime` asks
`session_mcp.session_mcp_server_is_disabled` on its create and resume paths, through
that reader rather than a projection field because KAS has no mirror to carry one, and
from the spec scope its host actually resolves the agent from
(`overlay_project_scope`). On KAS the member GRANT follows the same answer, since the
widening is approval-free. The resume half matters on its own: `session/load`
re-initializes the session's servers and would otherwise re-mount what `session/new`
withheld. Switching off one TOOL of that server is narrower and is weighed against the
backend: where withholding the server is the whole of its per-tool deny channel
(`mirrors.registry.PerToolDeny.WHOLE_SERVER`, opencode today) the mount is withheld
too, while codex refuses the call at permission time and claude's deny rules refuse it
inside the adapter, so both keep their mounts. Because the mount is
session-scoped, no other session on the same agent template gains the tools,
preserving the two-part grant for ordinary agents (the switch AND the
per-agent server assignment).

### Cron callers: unattended admission, bounded by the same fence

A cron job's own slot (`cron-<job_id>`, minted at run start by
`ensure_cron_slot` so identity exists while the turn runs — with
`inject_cron_result_to_dashboard` as the idempotent delivery-time fallback
creator, #8336) is admitted to the surface even though nobody
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
disabled even though the field's declared default is enabled, so neither a
malformed unrelated section nor an unreadable setting can produce cross-session
reach.

### Owner-DM channel callers: the audience predicate

The caller-side channel refusals (`linked_session_caller`, `mirrored_caller`) and
the work ledger's `channel_session` refusal exist for one threat: a channel
session acts on words from a thread other people are in, and what it reads lands
in front of them. `session_read_message` would hand a private dashboard transcript
to a Slack or Discord audience that was never party to it; a work brief would
publish a private dispatch's acceptance bar there. That reasoning assumes an
audience distinct from the operator. A **1:1 DM whose only human is the configured
owner** has none — the "audience" the containment protects is the operator
themself — and refusing it made every Discord and Telegram conversation a session
that could dispatch nothing.

Three gates decide this, and three different facts are available to them — the
live `linked_session_key`, the key prefix (`is_channel_session_key`), the mirror
store. A key prefix can never be cleared while a link can, so gates keying on
different facts would disagree about one slot. They therefore consult **one
predicate**, `session_control.audience_is_owner(state, slot)`, and the ledger
reaches it through `session_audience_is_owner(state, session_key)`,
which resolves the slot with the same `caller_slot_key` every session-control verb
uses — so "the ledger gate and session control agree on the same slot" holds by
construction. The clause walk itself is `owner_dm_refusal`, which returns the
first fact that FAILED (`""` when none did) and of which `audience_is_owner` is the
boolean face; every gate renders that reason into its refusal, so the three tell a
caller the same thing about the same slot and none of them can name a clause the
predicate did not actually evaluate. The refusal CODES are unchanged
(`linked_session_caller`, `mirrored_caller`, `channel_session`).

The predicate is a conjunction of positive facts, and any it cannot establish
answers **false**:

1. The slot is channel-born — its `linked_session_key` is a channel key (a
   `cron:<job_id>` link is not). A dashboard-born slot that mirrors to a DM is
   not the subject: its own conversation is the dashboard, and the mirror refusal
   keeps judging it as before.
2. The key parses under the canonical grammar (`messaging.link.parse_session_key`)
   as a **direct** conversation with exactly one peer, on a surface in
   `OWNER_DM_CONDUCTOR_SURFACES`. A `group`/`forum` key names a wider audience; a
   `unified` bucket names no peer; the legacy two-segment Slack shape does not
   parse; a surface outside the set fails closed by construction.
3. The channel's **live** transport names exactly one owner and it is that peer:
   `messaging.transport.sole_direct_target(transport.configured_targets())`, the
   same one-identity rule `/sessions` and the proactive owner DM apply, shared with
   `_owner_dm_target` so the two surfaces name the same human. An allow-list is a
   list of people permitted to talk to the agent, not a claim that any of them is
   the operator, so two entries name nobody. Read off the transport (the roster in
   force now, reloaded live, an in-memory read that stays callable from
   `close_target`'s no-suspension re-check) rather than the config record. An
   absent transport means the channel is not running and refuses.
4. The outbound mirror, if any, **is** the conversation the session lives in. The
   dispatcher binds the DM as its own mirror on every turn, and that is the same
   audience — but the dashboard can retarget a mirror at any thread or channel,
   and a retargeted DM republishes what it reads to people who are not the owner.
   So the mirror must equal the **origin** conversation the dispatcher recorded
   (`SessionManager.get_origin_link`, written on every inbound turn beside the
   mirror bind — Discord always did; Telegram now records it too). It is compared
   as a whole `ChannelLink` because Discord's DM channel id is not the peer id,
   so no derivation from the key could stand in for the recorded truth. An
   unknown origin, an unreadable store, or a mirror aimed anywhere else refuses.
   A **paused** mirror (the dashboard's Disconnect row) keeps its binding and
   reads exactly like a live one — the audience did not change.

   **The origin does not survive a gateway restart, and the exemption goes with
   it.** `set_origin_link` holds the record in memory by design (`session.py`),
   while the slot and its mirror are persisted and the slot is re-surfaced at boot.
   So between a restart and the owner's next channel message this clause is the one
   that fails while the other three hold: a monitor-loop cycle or a turn taken in
   the surfaced tab is refused, and the conductor loop resumes on the next inbound
   DM message, which records the origin again. This is a deliberate fail-closed
   cost, not an edge case — a mirror with no recorded origin cannot be told apart
   from one the dashboard retargeted, and admitting it on the strength of a
   persisted binding alone is exactly the retarget the clause exists to catch. It
   is also the one refusal a correctly configured owner DM can still meet, so all
   three gates name it (`ORIGIN_NOT_ON_RECORD`) and say what clears it, rather than
   reporting a channel link the caller cannot do anything about. Making the
   exemption survive a restart means giving the origin (or an equivalent durable
   record of the conversation a session was born in) persistence in `session.py` —
   out of scope here, since that store serves the auto-compact notice, whose own
   reason for being in memory is that a restart takes the live session with it.

**What is relaxed.** An admitted owner DM may `session_create`, and may `send`,
`read`, `stop` and `close` **the sessions it created**, and may hold a work ledger
— the whole conductor loop. **What is kept.** It is creator-fenced:
`_caller_is_ownership_fenced` treats every non-cron channel link as fenced (the
only linked caller that gets past the refusals is an owner DM), so it inherits a
crew member's reach, not the owner's own tab's. A wrong audience inference
therefore costs the sessions the DM created and never the person's other
conversations — `session_read_message` on an arbitrary private tab, the disclosure
the containment was written for, is still refused (`not_creator`, worded "an
owner-DM channel session can only control sessions it created itself"). Group and
thread sessions on every channel stay refused by all three gates; every channel
outside the set stays refused; `channel.CHANNEL_AGENT_BLOCKED_TOOLS` (the
multi-agent Channel feature's permission-request block) is untouched; the
target-side refusals are untouched, so a channel session is still never a
`session_send` target. The reach is not new to the trust model: the same DM
already resumes any dashboard session into itself through `!sessions` /
`/sessions` under the same single-owner rule.

**Which channels got which path.** Discord and Telegram: the predicate, because
both facts membership asserts were verified against their transports — the DM key
is `{surface}:{agent}:direct:{peer}` and `configured_targets()` advertises that peer
as `user:{peer}` from configured state alone (Weixin and WeCom fold learned
identities in, which is why `constants.CHANNEL_OWNER_DM_NAMESPACES` excludes them
and this set is a subset of it). Slack, Webex, Teams, WhatsApp, iMessage, Feishu,
WeCom, Weixin and `unified`-scope DMs: no relaxation — they read as contained
exactly as before. A channel graduates by verifying the two facts for it and adding
its name to `OWNER_DM_CONDUCTOR_SURFACES`; nothing else changes. No "detach from
channel" dashboard action was built: the dashboard's Disconnect row pauses outbound
delivery and retains the binding by design (see [session](session.md)), and with
the predicate in place an owner needs neither it nor an unlink to conduct.

Related fold: the ledger's bind ownership check compares `_created_by` (the
creator's **slot** key, as `session_create` stamps it) against the conductor
**session** key's resolved slot (`caller_slot_key`), because a channel-born
conductor is keyed `discord:…:genN` while its slot is that key folded to the
filename charset — a string comparison refused every worker such a conductor
created as `worker_not_owned`. A dashboard conductor compares as before.

Pinned by `test/test_session_control_owner_dm.py`: a Discord thread and a
Telegram forum topic refused by all three gates; an owner DM on Discord and on
Telegram conducting end to end; the fence; the paused mirror; every fail-closed
edge (two identities, a stranger's DM, an absent or unavailable transport, a
retargeted mirror, an unknown origin, an unreadable store, unparseable and
non-direct keys, a dashboard-born mirrored caller); gate agreement over one slot;
the post-read re-check; the bind fold; and that mirror-unlink clears only the
mirror.

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
teardown. Its four failure modes surface as their own codes at HTTP 500
(`history_write_running`, `nudge_retire_failed`, `app_close_hook_failed`,
`history_save_failed`), which is why the routes now forward a 500 rather than
degrading it to 400.

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
saving the stale slot (409 `target_replaced`).

There is a SECOND config read on that gate to close, not only the switch: the
ownership fence (`_caller_is_ownership_fenced` → `_member_caller` →
`_store_is_member_owned`) loads config on a cache miss to classify the caller's
store, and running that inside the no-suspension window is the same blocking-IO
hazard. `close_target` therefore never computes the fence inside the window: a
verdict the HTTP gate carried (a caller admitted as a crew member, see "Member
callers") is honoured as-is, and for any other caller it is resolved ONCE up front
— while the cache is still warm from `prewarm_enabled_check` and before
`close_slot`'s awaits — and passed to BOTH the initial gate and the re-check as
`precomputed_ownership_fenced`, so the callback consults a carried boolean instead
of re-deriving member-ownership from config on the loop. Being synchronous is the whole point —
there is no suspension between the last retirement, this re-check, and the pop, so
nothing (a channel mirror/link landing, a re-mint, or a racing `monitor_start`
arming a loop) can change between the final authorization and the archival; an
awaited re-check, by contrast, reopens exactly those windows. Any refusal aborts
the close, rolls back the retired nudge loop, and surfaces as the guard's own
status. This is the same "re-gate adjacent to the mutation, comparing identity not
presence" discipline `create_session` uses for its slot allocation, and the same
theme as the queued-drain re-check (#5911). The human ✕ path passes no check — the
person owns the tab and closes it unconditionally.

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

`agent.member_dispatch` (bool, default **true**). The operator ceiling on the
member switch bypass described under "Member callers". At its default a member DM
caller bypasses `agent.session_control` — the zero-configuration contract, and
today's behaviour, so installing this key changes nothing until it is set. Set it
`false` and a member caller stops bypassing: it falls back under
`agent.session_control` like any ordinary caller, which lets an operator who
withdrew session control keep member DM threads chat-only without disabling the
member. Read at the switch gate via `member_dispatch_enabled()`, and **fails
closed** everywhere it can go wrong. (1) An unreadable config withdraws the member
bypass (`member_dispatch_enabled()` returns false on a raising read). (2) A config
that loads but *discarded the `agent` section* (or the whole file) also withdraws
it: `load()` does not raise on a malformed section — it drops the section and
falls back to the permissive `member_dispatch=True` default, recording the loss in
`degraded_sections`, so `member_dispatch_enabled()` returns false when
`degraded_sections` names `agent` or the whole-config marker `*`, the same
"could not read it" vs "was never set" distinction `tailnet_identity_unknown` and
the publish gate draw. (3) At load time a *missing* key defaults to true (today's
behaviour) while any *present but malformed* value — including a quoted `"false"`,
a routine operator quoting mistake — coerces to false BEFORE schema validation, so
a botched opt-out withdraws the bypass rather than silently leaving it on. This is
a config-level ceiling, not an enterprise `SCOPE_CATALOG` scope: `session_control`
itself is a plain `agent.*` bool with no catalog entry, and a scope would need a
new governance enforcement seam rather than a data-only append, so it is left to a
follow-up.

## What is deliberately not here

- **No delivery to a target outside the addressable set.** `session_send` writes
  into another session's conversation, but only one the same `authorize_target`
  guard admits: a channel-linked, channel-mirrored, incognito,
  app-scoped, unattended or cross-workspace target is refused, so the verb cannot
  reach a conversation other people are party to. That holds for a steer too: the
  mode changes when the message runs, never who may send it.
- **No cross-workspace or cross-machine reach.** The boundary is one gateway's
  live sessions in one workspace.
- **No waking closed sessions.** See above.
- **No writes on the read path.** `session_read_message` never changes the
  target's state, so a poll loop cannot perturb what it is measuring.

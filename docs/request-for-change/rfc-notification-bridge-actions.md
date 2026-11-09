---
title: Interactive actions for the Notification Bridge (B4) — act on a bridged approval, question or mute from the chat transport
status: accepted
author: rohnkum
created: 2026-11-08
last-audited: 2026-11-09
audited-at: 6264d9ef0f
doc-pr: 32917
implementation-prs: []
tracking-issues: [32902]
supersedes: []
superseded-by: []
---
# RFC: Interactive actions for the Notification Bridge (B4)

> **Nothing here is built.** This document designs Phase B4 of the accepted
> Notification Bridge ([rfc-notification-bridge.md](rfc-notification-bridge.md)),
> which that RFC lists as "future, separate proposal" (its §Migration plan,
> Phase B4) and as a non-goal of its own scope. The bridge egress half that B4
> builds on IS on main — `src/kiro_crew/notifications/bridge.py`,
> `bridge_egress.py` and `bridge_slack.py`, landed by B1a
> ([#32384](https://github.com/kirodotdev/KiroCrew/pull/32384)) and B1b
> ([#32537](https://github.com/kirodotdev/KiroCrew/pull/32537)). B4 adds the
> return path: a reply the bridge turns back into an action.
>
> **Direction accepted (§Status and decision); the B4h flag flip still needs its
> own security sign-off before it turns on, and each phase still goes through its
> normal review.** B4 hands approval authority to an outside chat app, so the
> acceptance it waits on is a security-boundary decision, not a product one — see
> §Security considerations and §Open design questions.

- Status: accepted (direction). Written so the First Principles, Design and
  Security review lanes have a design to check the B4 implementation PRs against,
  the way the bridge RFC landed before B1a/B1b. The parent bridge RFC is accepted
  and B1a/B1b are merged; B4's direction is now signed, and each phase still goes
  through its normal review with the preview flag off until B4h.
- Author of the parent Notification Bridge RFC and this feature's originator:
  **zezhexu** (bridge RFC author, [#670](https://github.com/kirodotdev/KiroCrew/pull/670)).
- Related: [rfc-notification-bridge.md](rfc-notification-bridge.md) (the egress
  half B4 extends), [messaging](../system-specs/modules/messaging.md) (the owner
  identity rules B4 reuses), issue
  [#589](https://github.com/kirodotdev/KiroCrew/issues/589) (the bridge's
  original tracking issue, closed), PRs
  [#670](https://github.com/kirodotdev/KiroCrew/pull/670) (bridge RFC),
  [#32384](https://github.com/kirodotdev/KiroCrew/pull/32384) (B1a egress seam)
  and [#32537](https://github.com/kirodotdev/KiroCrew/pull/32537) (B1b owner
  Slack DM delivery).

## Status and decision

- **Direction signed.** A maintainer (bolichen97) has signed the direction of
  B4: a chat approval from the verified owner DM is accepted, with single-use,
  expiring tokens — the in-chat approve and deny, under the deny-rule limit below
  ([review](https://github.com/kirodotdev/KiroCrew/pull/32917#pullrequestreview-5816740346)).
- **The condition, folded exactly.** A command the deny rules refuse cannot be
  allowed from chat; chat can only answer prompts the dashboard would also show;
  the override of a refused command stays dashboard-only. This is the condition
  the RFC already proposes (§Security considerations, §Open design questions Q2),
  and it matches the shipped `_override_offer` gate in `permission_prompt.py`.
- **Q7 answered (the B4p note).** The maintainer answered §Open design questions
  Q7: B4p's `system.approval` note is **bridge-only** — it reaches the bridge sink
  without ringing the dashboard bell, which keeps today's behaviour of ringing only
  on expiry (the dashboard already shows a pending approval inline). Recorded in Q7
  and the B4p row, with a test that the flag adds no bell entry.
- **Scope of the sign-off.** This signs the **direction only**. Each B4 phase
  still goes through its normal First Principles / Design / Security review, and
  the default-off preview flag stays off until the B4h change. The sign-off is a
  review comment on direction, not an acceptance of this document as final.

## Summary

The Notification Bridge fans a notification out to a chat transport, one
direction only: the bridge posts and the chat app shows. B4 adds the **return
path** — the owner replies in the chat app and the reply becomes the action the
notification was about. Three actions, no more: **approve/deny** a pending tool
call, **answer an `ask_question` card**, and **mute** the source channel's
further bridged notes. Every inbound action is admitted only from the one
verified owner DM the channel already names, is single-use and time-bounded, and
is written to the crew log the same way the dashboard's own answer is. A
transport that cannot carry buttons falls back to reply keywords; a transport
that can carry neither is read-only and B4 changes nothing for it.

## Motivation

### Current state

A tool-approval prompt is already answerable outside the dashboard today, on one
path: when a turn is linked to a Slack thread, `permission_prompt.py` mirrors the
dashboard prompt into that thread (`post_linked_approval`) and a Slack click
resolves the same future the dashboard button would. For the non-interactive
runtimes (subagent, cron, task runner, AutoNudge) `slack/gateway_runtime/
turn_approvals.py` builds an approval callback that races the dashboard against
the owner's Slack DM, and `messaging/approval.py` holds the per-channel deciders
(`PendingApprovals`, `SessionApprovalDecider`, `TextReplyApprovalDecider`) that
let a channel reply answer a pending approval. So "answer an approval from a chat
app" is a shipped seam, not a new one.

What is **not** reachable today is acting on a note the Notification Bridge
posted. B1a/B1b made a bridged note reach the owner's Slack DM
(`bridge_slack.py`, `bridge_egress.py`), but a bridged note is a dead end: it
carries no affordance and its reader has no path back into a decision. And no
channel answers an `ask_question` / elicitation card at all — that one
(`dashboard/chat_api/slot_approval.py`,
`dashboard/chat_turn/elicitation_prompt.py`) is dashboard-only on main.

There is also nothing to bridge yet: on main nothing puts a *pending* tool
approval on the notification bus. `permission_prompt.py` shows the card in-session
and, when a thread is linked, in that Slack thread; its only bus note
(`state.notify`) fires when an approval *expires*, not when it opens. So B4 needs a
producer before it can carry a pending approval anywhere — see §Migration plan B4p.

### What B4 actually adds

Three things, and only these are new trust boundaries:

1. **Acting on a bridged note.** The shipped approval-over-Slack paths above are
   wired to a *live turn* that happened to link a thread. A bridged note travels
   through the bridge's own egress; B4 is the first path that turns a reply to a
   *bridged* message back into the action the note was about.
2. **Answering an `ask_question` card from chat.** No channel does this on main;
   B4 adds it.
3. **Muting the source channel from chat.**

B4 does not invent "approve from chat" — it extends the existing deciders and
tokens to the bridge's egress path. That is why it is its own phase and gated on
a security sign-off: it widens *which* messages carry approval authority (a
bridged note, not just a live-turn thread) and it adds `ask_question` as a thing
chat can answer. The authority a chat reply carries is not new; the set of
messages that carry it, and the elicitation case, are.

## Goals

- Let the verified channel owner approve/deny a bridged tool-approval prompt from
  the chat app.
- Let the verified channel owner answer a bridged `ask_question` card from the
  chat app.
- Let the verified channel owner mute the source channel's further bridged notes
  from the chat app, without opening the dashboard.
- Make every inbound action single-use, time-bounded, owner-only, and audited in
  the crew log identically to the dashboard answer it stands in for.
- Degrade cleanly per transport: buttons where the transport has them, reply
  keywords where it does not, read-only where it has neither.

## Non-goals

- **Free-form agent chat from the transport.** B4 answers a *specific pending
  decision the bridge posted*; it is not a second way to talk to the agent. The
  channel conversation feature already owns that.
- **New actions beyond the three named.** No "retry", no "edit", no "run this
  other command". The set is approve/deny, answer `ask_question`, mute. A fourth
  action is a later proposal.
- **Acting on a note that carries no pending decision.** An informational note
  (a cron digest, a completion event) has nothing to act on; its bridged message
  carries no controls.
- **Non-owner actors.** A peer who merely messaged the bot, a room, a thread, an
  allow-list member who is not the sole owner DM — none can act. See §Identity
  binding.
- **New cloud cost or a new outbound service.** B4 rides the transports already
  connected; it adds no rendezvous service and no new credential.
- **Changing the dashboard answer path.** The dashboard stays the authority; B4
  is a second, narrower door into the same decision, never a replacement.

## Design

### Where it attaches

The bridge's rendering path (`bridge_slack.py` and the per-transport sinks B2
adds) is where an outbound note is turned into a transport message. B4 attaches
here: when the note being bridged carries a **pending decision** (a tool-approval
prompt, an `ask_question` card) or is a **channel note** (mutable source), the
sink renders the note AND the action affordances for that transport. The inbound
half is a new reader on each transport's existing event stream that recognises
only the affordances B4 itself emitted.

### Reusing the shipped approval seams

B4 builds on what already resolves an approval; it introduces no second token
scheme and no new approval store. But there is **not one shared record** across
the surfaces — each surface keeps its own, and a B4 design that pretended
otherwise would bind its tests to a store that does not hold the other surfaces'
decisions. The real seams on main, per surface:

- **Dashboard:** its own approval futures, keyed by approval id, in
  `dashboard/interaction_coordinator.py` (`_pending_approvals`).
- **Slack:** its own `_pending_approvals` dict in `slack/handler.py`, keyed by
  channel and message timestamp (not by request id), with a separate native-click
  path.
- **Per-channel reply deciders:** `messaging/approval.py`'s `PendingApprovals`
  ("one instance per channel"), whose only production instance today is
  `PendingApprovals("webex")` in `webex/transport_dispatch_interactions.py`, with
  `SessionApprovalDecider` / `TextReplyApprovalDecider` beside it.
- **What joins them:** `slack/gateway_runtime/turn_approvals.py` does not share a
  record — it races the dashboard against the Slack DM by cross-resolving two
  *separate* futures, so whichever answers first resolves the underlying decision
  and the other side observes it resolved.

So the single-use and cross-transport idempotency B4 needs rests on **the
underlying decision future**, the thing `turn_approvals.py` already cross-resolves
— not on a shared approval record, which does not exist. A bridged approve/deny
resolves that decision future; the first resolution wins and every surface
(dashboard, Slack click, a second transport) then sees it resolved. Which store a
bridged action posts *through* on each transport, and where the link from a posted
card to its decision lives, is the seam B4a must pin — see §Open design questions
Q5, because it is not settled by a shipped shared record.

For the Slack button, the token is the shipped `slack/approval_tokens.py`:
`build_approval_token(session_key, request_id, nonce)` mints a nonce-bound,
single-use token the gateway re-derives from its own registry
(`_approval_registry_key`); the chat app supplies nothing but the token. B4's
Slack buttons carry *this* token, not a new one.

**A bridged note must not post a second card for a decision already shown.** When
a turn already linked a Slack thread, `permission_prompt.py` (`post_linked_approval`,
via `_interactive_approval`) has already posted one approval card into the owner
DM. B4's bridge sink must recognise that case and render affordances on the
existing message rather than posting another. On main the Slack store is keyed by
message timestamp, not request id, so the link from a card to its decision is part
of the B4a seam above, not something that already exists to key on.

The `ask_question` case does not go through an approval store at all. On main it
opens an **agent-ask card keyed by `ask_id`** (`mcp_tools/control.py`,
`dashboard/handlers/ask_question.py`, over `/api/agent-ask/open`), not the MCP
elicitation resolver. B4e resolves *that* card by its `ask_id`; the RFC does not
claim an elicitation-resolver path that is not the one in use.

Where a transport has no shipped token (a reply-keyword transport with no
components), the keyword reader admits a keyword only as a reply to the bridged
message bound to the live decision, exactly as a button press is. No separate
token store is introduced.

### Identity binding

The actor must be the channel's **verified owner DM**, inferred by the single
rule the messaging layer already owns, reused verbatim — this RFC adds no new
owner-identity path:

- `messaging.transport.sole_direct_target()` — a channel names its owner only
  when it advertises exactly one available, non-`learned` `user:` target. An
  ambiguous roster names no owner, so no one can act.
- **Slack binds its owner directly**, not through the inference rule above:
  `slack.handler_runtime.access.is_owner()` checks the configured `owner_id`.
  Slack has its own client and streaming path and is deliberately **absent** from
  `constants.CHANNEL_OWNER_DM_NAMESPACES` (that tuple derives from
  `CHANNEL_SEND_NAMESPACES`, which already removes `slack` and `unified`), so Slack
  is owner-bindable through `is_owner` even though it is not in the tuple. B4 must
  not gate Slack on tuple membership.
- `constants.CHANNEL_OWNER_DM_NAMESPACES` — the per-channel capability list for the
  **inference-based** transports (the ones that reach their owner through
  `sole_direct_target`, not a configured `owner_id`). A transport that binds its
  owner by inference and is absent from this tuple (WeCom and weixin today, because
  they cannot yet tell a configured recipient from a learned peer) is **read-only
  for B4**: it may receive bridged notes but may not act on them, until it
  graduates by marking learned peers (`learned=True`) the way `sole_direct_target`
  already reads. This mirrors the fix in
  [#32792](https://github.com/kirodotdev/KiroCrew/pull/32792), which kept a peer who
  only messaged the bot from being inferred as the owner. The rule, then: **Slack
  binds through `is_owner`; every other transport must be in
  `CHANNEL_OWNER_DM_NAMESPACES`.**

**B4 keeps the shipped owner-only rule; it does not loosen it.** On Slack the
shipped `is_allowed_user(user_id)` already returns `is_owner(user_id)`
(`slack/handler_runtime/access.py`), so the live-turn click is already owner-only.
B4's inbound door admits an action from the same verified owner DM (`is_owner` on
Slack, `sole_direct_target` elsewhere) — parity with the live-turn path, applied
now to a bridged note. An inbound action whose sender is not the bound owner is
dropped and audited as a refusal; it never reaches the decision.

### Replay and expiry

- **Single-use:** the first admitted action consumes the pending record. A second
  press, an edited reply, or a replayed webhook finds no record and is a no-op
  with a one-line "already answered / expired" acknowledgement, never a second
  resolution of the decision.
- **Expiry:** the decision an action resolves is an in-process future, so **every
  outstanding action expires on a gateway restart** — the decision it named no
  longer exists, and an inbound action carrying it resolves nothing and is treated
  as expired. Within a process, the deadline is **per surface** and there is no one
  module constant that governs it: the Slack race waits with
  `asyncio.wait_for(pending.future, timeout=...)` on
  `DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS` or `._APPROVAL_TIMEOUT`
  (`slack/gateway_runtime/turn_approvals.py`), and the reply-keyword path has its
  own `TEXT_APPROVAL_TIMEOUT_S` (`messaging/approval.py`). So B4a does not patch a
  single `APPROVAL_TIMEOUT_S` — it **adds one clock seam that every bridged-expiry
  path reads**, and the expiry tests drive that seam (no sleeps). When the decision
  resolves by any other path — answered in the dashboard, the turn stopped — the
  bridged affordance, when the transport allows an edit, is updated to "answered
  elsewhere".
- **Idempotency:** because resolution runs through the one underlying decision
  future (the thing `turn_approvals.py` already cross-resolves), two actors or two
  transports racing the same decision resolve it at most once; the loser gets the
  "already answered" acknowledgement.

### Audit trail in the crew log

An action admitted from a transport is written to the crew log through the same
emitters the dashboard answer uses — not a new side log — so the record of "who
approved this and from where" is one timeline. Each admitted or refused action
emits a crew-log entry naming the request id, the source channel, the transport,
the bound owner identity, and the outcome (approved / denied / answered / muted /
refused / expired). The SEL audit (`sel.py` `log_tool_invocation` /
`log_api_access`) records the inbound action as an owner-authenticated API access
from the transport, so a bridged approval is as auditable as a dashboard one. A
refusal (wrong actor, expired token, replayed) is audited too, because a refused
attempt to act on an approval is exactly what a security reviewer needs to see.

### Per-transport capability

| Transport | Owner bindable | Action affordance | B4 behaviour |
|---|---|---|---|
| Slack | yes (`owner_id`) | interactive buttons + reply keywords | full |
| Discord | yes (`sole_direct_target`) | buttons (components) + reply keywords | full |
| Telegram | yes (`sole_direct_target`) | inline keyboard + reply keywords | full |
| Webex | yes (`sole_direct_target`) | card actions or reply keywords | full where the SDK carries card actions; reply keywords otherwise |
| WeCom / weixin | no (excluded from `CHANNEL_OWNER_DM_NAMESPACES`) | — | read-only; receives notes, cannot act, until it graduates |

**Buttons vs reply keywords:** a transport that carries interactive components
renders Approve / Deny (and, for a question, one button per offered answer plus a
"answer in dashboard" link for free-form). A transport without components falls
back to reply keywords the message spells out ("reply `approve` / `deny`", or
"reply `1`/`2`/… to pick"). The reply-keyword reader admits a keyword only as a
reply to the bridged message that carries the live token, so a bare `approve`
typed into the channel with no pending action is ignored. Capability is detected
from the transport, never assumed; a transport that advertises neither is
read-only.

### Failure modes

- **Token expired or decision already resolved:** no-op, one-line "already
  answered or expired" acknowledgement, audited.
- **Wrong actor (non-owner, learned peer, room/thread):** dropped, audited as a
  refusal, no acknowledgement to the actor beyond what the transport posts by
  default (so a stranger learns nothing about the decision).
- **Ambiguous owner (roster names no sole DM):** the note bridges read-only; no
  affordance is rendered, because no actor can be verified.
- **Transport delivery of the acknowledgement fails:** the decision has already
  resolved (resolution precedes acknowledgement); a failed ack is logged, not
  retried into a second resolution.
- **Gateway restart between bridge and action:** the decision futures are
  in-process, so a restart drops every outstanding decision; an action whose
  decision no longer exists resolves nothing and is treated as expired. No state
  is reconstructed or persisted across the restart.
- **Chat app forges a token:** it names a decision the gateway did not create, so
  it resolves nothing and is refused and audited.
- **Two transports carry the same decision:** idempotent on the one underlying
  decision future; one wins, the other gets "already answered".

## Migration plan

Each phase is one PR, under the file-size and review budgets, and ships behind a
default-off preview flag until B4 as a whole is accepted. Phases are additive:
until the inbound reader lands, the affordances render but do nothing, and until a
transport's sink is extended it stays read-only.

- **B4a — bridge→decision binding:** the glue that lets a bridged note's reply
  resolve the underlying decision future (the one `turn_approvals.py`
  cross-resolves), reusing `slack/approval_tokens.py` for the Slack token, plus
  the link from a posted card to its decision that §Open design questions Q5
  settles, with no transport wired yet. Backend only, no new approval store. B4a
  adds **one clock seam that every bridged-expiry path reads**, because the
  deadline is per surface on main (the Slack race on
  `DashboardState._APPROVAL_TIMEOUT` / `_BACKGROUND_APPROVAL_TIMEOUT_SECS`, the
  reply path on `TEXT_APPROVAL_TIMEOUT_S`), so there is no single
  `APPROVAL_TIMEOUT_S` to patch. Tests pin single-use (the first resolution wins; a
  second resolves nothing) and expiry by driving that seam, no sleeps.
- **B4b — crew-log + SEL audit emitters:** the admitted/refused action entries,
  reusing the dashboard answer emitters. Tests pin that a bridged approval and a
  dashboard approval write the same shape, and that a refusal is audited.
- **B4p — a pending-approval producer (before B4c, because without it B4c has
  nothing to bridge):** on main nothing puts a *pending* tool approval on the
  notification bus. `permission_prompt.py` shows the card in-session and, when a
  thread is linked, mirrors it into that Slack thread (`post_linked_approval`); its
  only bus note (`state.notify`) fires on *expiry* (`APPROVAL_EXPIRED_NOTE_TITLE`),
  not at open. The only `system.approval` producer is `config_pending.py`, which
  §Open design questions Q6 says must never carry buttons. So B4p adds the missing
  producer: when the approval card opens, `permission_prompt.py` publishes a
  `system.approval` note carrying a meta key that names the request (producer =
  the tool-approval path, the Q6 identifier), **behind the default-off preview
  flag**. Per the maintainer's answer to Q7, this note is **bridge-only**: it
  reaches the bridge sink **without ringing the dashboard bell**, which keeps
  today's behaviour of ringing only when an approval expires (the dashboard already
  shows a pending approval inline, so a bell per approval would be noise). When the
  card was already posted to a linked Slack DM, the Slack sink reuses that card (its
  `link_message` recorded in the bridge's own state, per Q5) instead of posting a
  second. Tests pin: flag off → no new note; flag on → exactly one `system.approval`
  note per pending approval, carrying the Q6 meta key, and **no new dashboard bell
  entry**; a note for an already-linked card reuses it rather than posting twice.
- **B4c — approve/deny, Slack buttons:** the first live action on the first
  transport that has owner binding and components, consuming the B4p note,
  carrying the shipped `slack/approval_tokens.py` token and resolving the
  underlying decision future; the bridge sink reuses the already-posted card for an
  approval `permission_prompt.py` linked, rather than posting a second. Tests pin
  owner-only admission as parity with the live-turn path (which is already
  owner-only via `is_allowed_user` → `is_owner`) and that a non-owner press is
  refused.
- **B4d — approve/deny, reply-keyword fallback:** the keyword reader for a
  transport without components, admitted only as a reply to the bridged message
  carrying the live `request_id`. A bridged affordance offers **Approve and Deny
  only** — the signed direction covers those two. `messaging/approval.py`'s
  `TextReplyApprovalDecider` also maps `trust`/`always`/`3` to a `TRUST` verdict
  that auto-approves the rest of the session; a bridged reply must **not** reach
  that verdict. So B4d reuses only the Approve/Deny branch of the decider (or
  refuses a `trust` reply outright), and a test pins that a `trust` reply to a
  bridged note is refused, not honoured. (If Trust from chat is wanted, it is its
  own action for the maintainer to decide, not folded in here.)
- **B4e — answer `ask_question`:** the one-button-per-answer rendering and the
  inbound mapping back to the agent-ask card keyed by `ask_id`
  (`mcp_tools/control.py`, `dashboard/handlers/ask_question.py`, over
  `/api/agent-ask/open` — not the MCP elicitation resolver), with the "answer in
  dashboard" link for free-form questions.
- **B4f — mute the source channel:** the mute action, reusing `ChannelSettings`
  the bridge already writes, so a mute from chat and a mute from the dashboard are
  the same write.
- **B4g — remaining transports:** Discord, Telegram, Webex sinks extended to the
  affordances their SDKs carry; WeCom/weixin stay read-only.
- **B4h — preview flag off → on (separate PR, gated on sign-off):** flip the
  default only after the security sign-off in §Open design questions is recorded.

(Phase letters are a plan, not a promise of count; a phase that proves larger
than one PR splits, and a phase not needed is dropped with a one-line note, per
the smallest-complete-change rule.)

## Backward compatibility

Compatible and additive. With the preview flag off, the bridge behaves exactly as
B1–B3 left it: notes bridge out, nothing comes back. A transport not in
`CHANNEL_OWNER_DM_NAMESPACES` is unaffected in every phase. No existing setting is
renamed or removed; the mute action writes the same `ChannelSettings` field the
dashboard already writes. The dashboard answer path is untouched.

## Security considerations

B4 is the bridge's first inbound action path, so these are the crux, and the
sign-off in §Open design questions turns on them. Note that approving a tool call
from a chat app is not itself a new authority on main — `permission_prompt.py`'s
linked Slack thread and `turn_approvals.py`'s dashboard-vs-DM race already do it
for a live turn; the deny-rule *override*, though, already ships dashboard-only
(`permission_prompt.py` gates it on `_override_offer`, with "an override is
answered on the dashboard alone"), and B4 keeps it there.

- **Authority already leaves the dashboard; B4 extends which messages carry it.**
  An approve from a chat app lets a tool run, and a chat app can do that today for
  a live-turn thread. B4 adds the bridged-note case under the same owner-only
  admission the shipped click already applies (`is_allowed_user` is `is_owner` on
  Slack, `sole_direct_target` elsewhere — never a new path), resolving the one
  underlying decision future, carrying the shipped `slack/approval_tokens.py`
  token, with a full crew-log + SEL audit of every admitted and refused action.
- **The chat app is not the authenticator.** The token names a decision only the
  gateway created; the gateway re-derives the decision from its own in-process
  state (the decision future / the Slack approval registry) and trusts nothing the
  chat app sends beyond the
  token. A forged or replayed token matches no live record.
- **The deny-rule override stays dashboard-only.** A command the deny rules
  refuse cannot be allowed from chat; chat answers only prompts the dashboard
  would also show. This matches both bolichen97's signed condition and the
  shipped `_override_offer` gate.
- **Least surface.** Only three actions, only on notes that already carry a
  pending decision, only from the one verified owner DM, only while the decision
  is live. An informational note carries no affordance.
- **No widening of `CHANNEL_OWNER_DM_NAMESPACES`.** B4 does not add a transport to
  the owner-bindable set to make a feature work; a transport graduates only by
  earning the distinction `sole_direct_target` reads, as a separate change with
  its own reasoning.
- **Reviewer's veto.** Because this is a security-boundary change, the Security
  review lane and a maintainer sign-off decide whether the authority transfer is
  acceptable at all, and may bound it further (e.g. approve/deny allowed from
  chat but not a tool call above a risk tier). That decision is theirs, recorded
  in §Open design questions, not assumed here.

## Alternatives considered

- **A read-only bridge (no B4).** The owner reads the question in Slack and walks
  to the dashboard to answer. Simplest and safest, but it leaves the away-from-
  dashboard owner — the bridge's whole audience — unable to act. B4 exists to
  close that gap under a tight boundary.
- **A deep link back to the dashboard instead of an in-chat action.** The bridged
  note carries a one-tap link that opens the dashboard decision. Keeps all
  authority in the dashboard and needs no inbound trust boundary; it is the right
  answer if the sign-off refuses the authority transfer. B4 proposes the in-chat
  action as the richer option and the deep link as the fallback, not the reverse.
- **A generic inbound command channel.** Rejected as a non-goal: it reopens
  free-form agent control from the transport, a much wider boundary than three
  bounded actions on a live decision.

## Open design questions

1. **The sign-off itself.** Approving a tool call over Slack from a live turn
   already ships; what B4 genuinely adds is (a) acting on a note the bridge
   posted, (b) answering an `ask_question` card from chat, and (c) muting the
   source channel from chat. Does a maintainer accept these three, under the
   owner-only admission and the reuse of the shipped decision-future and
   `slack/approval_tokens.py` seams? If (a) is refused, B4 ships as the deep-link
   alternative for bridged approvals and keeps only (b) and (c). (Owner decision,
   security boundary.) **bolichen97 has signed the direction** — see §Status and
   decision.
2. **Risk-tier ceiling.** Is there a tool-risk tier above which approval must
   still happen in the dashboard? The deny-rule override already ships
   dashboard-only (`permission_prompt.py` gates it on `_override_offer`), and B4
   does not touch that. Default proposed, matching bolichen97's condition: an
   in-chat approve may not override a command the deny list refuses; that override
   stays dashboard-only.
3. **Mute scope.** Does the chat mute mute only bridged delivery to that transport,
   or the channel's notes everywhere? Default proposed: it writes the same
   per-channel `ChannelSettings` the dashboard mute writes, so the two agree.
4. **Acknowledgement verbosity to a non-owner.** How much does a refused actor
   see? Default proposed: nothing beyond the transport's own default, so a
   stranger in a shared space learns nothing about the decision.
5. **Which store a bridged action resolves through, per surface (the B4a seam).**
   There is no single shared approval record on main: the dashboard holds its own
   futures (`dashboard/interaction_coordinator.py`), Slack its own
   timestamp-keyed `_pending_approvals` (`slack/handler.py`), and
   `messaging/approval.py`'s per-channel `PendingApprovals` has one production
   instance (Webex). What is shared is the underlying decision future, which
   `turn_approvals.py` cross-resolves. Default proposed: a bridged action resolves
   that decision future, and B4a adds a lookup from a posted card to its decision
   (the Slack store is keyed by message timestamp today, so this link is new).
   **Resolved:** that lookup lives in the bridge's own state, as a `link_message`
   record from the posted card to its decision, not beside the Slack store — so
   the Slack sink can reuse an already-linked card instead of posting a second.
   (Design decision; it does not widen the boundary.)
6. **Identifying a tool-approval note.** On main the only producer of a
   `system.approval` note is the sealed-file config-pending review
   (`dashboard/handlers/config_pending.py`), whose apply path is already owner-only
   and behind CSRF. Default proposed: a config-pending `system.approval` note never
   carries a B4 affordance, and B4 identifies an actionable tool-approval note by
   its producer and a meta key, not by the channel name.
7. **A bell note per pending approval (B4p). Resolved by the maintainer.** B4p
   publishes a `system.approval` note when an approval card opens, so the bridge has
   something to carry. The maintainer's answer: the note is **bridge-only** — it
   reaches the bridge sink **without ringing the dashboard bell**. The dashboard
   bell keeps today's behaviour and rings only when an approval expires, because the
   dashboard already shows a pending approval inline and a bell per approval would
   be noise. B4p and §Tests pin that, with the flag on, a pending approval adds no
   dashboard bell entry.

## Tests

- B4a: single-use (the first resolution of the underlying decision future wins; a
  second resolves nothing), expiry by driving the one clock seam B4a adds that every
  bridged-expiry path reads (not a single `APPROVAL_TIMEOUT_S`; the real deadlines
  are `DashboardState._APPROVAL_TIMEOUT` / `_BACKGROUND_APPROVAL_TIMEOUT_SECS` and
  `TEXT_APPROVAL_TIMEOUT_S`), no sleeps; a forged token resolves nothing, and a
  restart (fresh in-process state) leaves no decision to resolve.
- B4b: a bridged action and a dashboard action write the same crew-log shape; a
  refusal (wrong actor, expired, replayed) is audited.
- B4p: with the preview flag off, an opening approval card publishes no new note;
  with it on, exactly one `system.approval` note per pending approval, carrying the
  Q6 meta key, and **no new dashboard bell entry** (the note is bridge-only); an
  approval already posted to a linked Slack card reuses that card (its
  `link_message`) rather than posting a second.
- B4c/B4d: owner-only admission (Slack through `is_owner`, other transports through
  `sole_direct_target`); a non-owner / learned-peer / room sender is refused; a
  button and a reply keyword resolve the same approval; a second press is a no-op;
  and a `trust`/`always`/`3` reply to a bridged note is **refused**, never mapped to
  the session-wide TRUST verdict.
- B4e: an `ask_question` answer from chat maps to the agent-ask outcome the
  dashboard would record for that `ask_id`; a free-form question offers the
  dashboard link.
- B4f: a chat mute and a dashboard mute write the same `ChannelSettings`.
- B4g: a transport without components renders reply-keyword affordances; an
  inference-based transport outside `CHANNEL_OWNER_DM_NAMESPACES` (WeCom/weixin)
  renders none and stays read-only, while Slack (bound through `is_owner`, not the
  tuple) stays full.
- Determinism: no inbound action resolves a decision twice across two transports
  or two presses; idempotency pinned on the one underlying decision future.

## Out of scope (explicit)

- Free-form agent conversation from the transport (owned by channel
  conversations).
- Any action other than approve/deny, answer `ask_question`, mute.
- Acting on an informational note that carries no pending decision.
- Non-owner actors, rooms, threads, learned peers.
- Adding a transport to `CHANNEL_OWNER_DM_NAMESPACES` to enable a feature.
- New cloud services, new credentials, or any outbound rendezvous.
- Changing the dashboard answer path or the bridge's outbound B1–B3 behaviour.

---
title: Feishu two-way dashboard link and numbered-text tool approval -- steer and approve a running dashboard session from a Feishu DM
status: draft
author: jhua-labs
created: 2026-11-06
last-audited: 2026-11-07
audited-at: e30915e712
doc-pr: null
implementation-prs: []
tracking-issues: [31448]
supersedes: []
superseded-by: []
---

# RFC: Feishu two-way dashboard link and numbered-text tool approval

- Status: draft. Nothing built. Tracking issue [#31448](https://github.com/kirodotdev/KiroCrew/issues/31448). A maintainer asked on that issue for this RFC as its own docs-only pull request; whether the design is accepted, including Q1, is decided in that pull request's review, and each implementation pull request needs a maintainer design sign-off before it merges.
- Related: #31283 (Feishu proactive send, merged; Phase 3 depends on it), #31122 (images in Feishu-path answers, merged), #31807 (Feishu pictures visible in the dashboard, merged), [#32130](https://github.com/kirodotdev/KiroCrew/issues/32130) (text heads-up on the linked channel when a dashboard approval is waiting, `tier:T2`; the smaller step before Phase 3, see 5.3), #32152 (dashboard → channel mirror sends attachments as raw paths), #7548 (Feishu interactive cards; a later, nicer approval UI, not required here), #2779 (first-party mobile approval app), [rfc-session-address-model.md](rfc-session-address-model.md) (the longer-term surface model this should not contradict), [rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md) (what an unanswered approval does to a loop).
- Measured at `e30915e712` (main, 2026-11-07). Code is cited by file and symbol, read at that commit.

## 1. Summary

A user who runs a long dashboard session (a goal loop, a multi-hour development task) and is reachable only on Feishu cannot steer or approve it from the phone. The dashboard's **Connect to Feishu DM** link is outbound-only, and Feishu has no approval decider. This RFC proposes three independently shippable phases:

1. **Two-way link.** Make the existing **Connect to Feishu DM** menu action two-way, the way it already is for Telegram and Discord, with a Feishu session-resume adapter on the shared `messaging/session_resume.py` machinery. No new UI.
2. **Numbered-text approval on the Feishu path.** Reuse `TextReplyApprovalDecider` exactly as WhatsApp does, so a turn running on the Feishu path asks instead of refusing.
3. **Linked approval mirror for dashboard-run turns.** Generalise the Slack-only "mirror the dashboard approval prompt to the linked thread" path into a channel-neutral seam, with Feishu as its first text-reply consumer.

Phase 3 is what the scenario in 2.1 needs, and it touches the most code. It rests on one finding (2.3): today no channel other than Slack receives a dashboard-run turn's approval prompt.

## 2. Motivation

### 2.1 The scenario, as it happened

On 2026-11-07 the author ran a dashboard turn, walked away from the desk, and watched the Feishu DM on the phone the whole time. The turn reached a tool that needed interactive approval. Nothing arrived on Feishu. The prompt waited its 600 s window in the dashboard (`agent.tool_approval_timeout_secs`) and was auto-declined, and the turn stalled. The same happened twice that day. The agent can already message the user first on Feishu (#31283), so the user was reachable; the session had no way to ask, and no way to take an answer back.

### 2.2 Current state on Feishu

- **Outbound-only link.** The session context menu offers **Connect to Feishu DM** (direct message · the owner's `open_id`) next to **Connect to Weixin DM**, and on main it produces a binding labelled `One-way · this session → Feishu`. `FEISHU_CAPABILITIES` (`src/kiro_crew/feishu/transport.py`) declares `files_outbound=True` (since #31122), `max_buttons=0` and `supports_proactive_send=True`, and does not declare `supports_session_resume`, so it takes the default `False` (`ChannelCapabilities` in `src/kiro_crew/messaging/transport.py`). The flag's docstring in `messaging/transport.py` says only a transport whose inbound path resolves the mirror binding may declare it; declaring it without that would "promise a two-way link whose replies silently start a separate session". A reply in the Feishu DM therefore runs in `feishu:<app>:direct:<open_id>`, not in the connected dashboard session. `src/kiro_crew/docs/channel-capabilities.md` row "Dashboard link is two-way" is ❌ for Feishu.
- **The label is already data-driven.** The `One-way`/`Two-way` tag in the menu is rendered from the slot row's `drives_session` (`website/src/components/LinkedSurfacesSection.tsx`), which `dashboard/state_parts/slots_frame.py` derives from `mirror_accepts_inbound`, which `dashboard/chat_mirror.py` (`_resumes_inbound`) computes from `supports_session_resume` plus the transport's `may_resume_from`. So Phase 1 changes no frontend code: the same click starts reading `Two-way · Feishu ↔ this session`.
- **No approval decider.** `_resolve_approval_mode` in `feishu/gateway.py` resolves the transport approval mode to `auto` or `interactive`; its docstring notes Feishu has no interactive decider, so `interactive` means deny-by-default. `src/kiro_crew/docs/channel-capabilities.md` ("WeCom, Weixin, iMessage and Feishu never prompt") confirms a tool needing permission is refused with no prompt and no window.
- **The workaround removes the gate.** The only way to run tools from Feishu is approval mode `auto`/`trust`, which approves everything.

### 2.3 Current state on the dashboard side: the Slack-only mirror

A turn that runs in a **dashboard slot** asks for approval through `dashboard/chat_turn/permission_prompt.py`. Its Slack branch mirrors the prompt to the linked Slack thread (`post_linked_approval` in `slack/handler_runtime/approvals.py`) when the slot is Slack-linked, and cleans up with `resolve_linked_approval` in the same module. On expiry it posts `APPROVAL_EXPIRED_NOTE_TITLE` to the dashboard bell only (the `state.notify` call in the same function). A grep of `permission_prompt.py` for `telegram|discord|whatsapp|feishu|channel_type` returns nothing: **no other channel receives a dashboard-run turn's approval prompt.**

The same holds for Telegram and Discord. A Telegram or Discord two-way link hands an inbound message to the dashboard slot (`telegram/dispatch/midturn.py` → `hand_to_resumed_slot` in `dashboard/channel_handoff.py`), so the turn runs in the dashboard, and its approval prompt reaches only the dashboard (and Slack, if Slack-linked). Two-way linking alone therefore does not give any non-Slack channel remote approval of a dashboard session. This is a code-reading result, not a reproduced one, and should be confirmed by a maintainer (Q2).

The other cross-surface legs are text-only too: `dashboard/chat_turn/recipient.py` echoes the user's dashboard message (`_deliver_cross_surface_user_message`) and the turn's reply (`_deliver_cross_surface_reply`) through plain `send_message`; #32152 tracks their attachments, and the open [#32232](https://github.com/kirodotdev/KiroCrew/pull/32232) teaches the reply leg to upload images. That is relevant here only as the place a mirrored approval prompt would also be sent from.

### 2.4 What already exists to build on

- `messaging/session_resume.py` (1313 lines): the channel-neutral half of session resume: eligible-session listing and search, picker registry with nonce/TTL/owner scoping, binding-conflict rules, and the inbound routing + settlement state machine. Channels supply a `ResumeSurface` (`messaging/session_resume.py`). Per-channel adapters are `telegram/session_resume.py` (380 lines: `TelegramSessionResume`, with `leave_resumed_session`) and `discord/session_resume.py` (418 lines). Telegram's ownership rule is `may_resume_from` in `telegram/transport.py`: only one unambiguous owner DM may drive a dashboard session inbound.
- `messaging/approval.py`: `TextReplyApprovalDecider`, `parse_approval_reply` (only a reply that is entirely an answer counts), `pending_for`, `deliver_verdict`, `TEXT_APPROVAL_TIMEOUT_S = 300.0`, and `APPROVAL_CHOICE_LABELS = ("Approve once", "Deny", "Trust this session")`. WhatsApp wires it in `whatsapp/transport_dispatch.py`: `_consume_approval_reply` takes a reply as a verdict, and the dispatcher constructs the decider only for an operator sender; a non-operator gets `decider=None`, i.e. deny.
- `messaging/spawn_approval_delivery.py`: a precedent for a channel-namespace-keyed delivery registry ("one seam, every channel") for host approval prompts, with `None` meaning "fall through".
- `resolve_approval` in `dashboard/state_parts/dashboard_services.py`: what a Slack click resolves for a dashboard-run approval.

## 3. Goals

- G1. **Connect to Feishu DM** produces a two-way binding: while bound, every message in that DM continues the connected dashboard session, visibly in its tab, and the user can leave the binding from the DM or from the dashboard menu.
- G2. A turn on the Feishu path in `interactive` mode asks for approval as a numbered message instead of refusing; reply `1`/`2`/`3`; silence denies.
- G3. A dashboard-run turn in a session two-way-linked to a Feishu DM delivers its approval prompt to that DM, and a numbered reply resolves the same pending approval the dashboard shows. First answer wins on either surface.
- G4. Only the allow-listed owner can bind or approve. Every other sender keeps today's behaviour.
- G5. The capability matrix and `src/kiro_crew/docs/feishu-integration.md` say what is true after each phase.

## 4. Non-goals

- Feishu group chats (binding or approving from a group). DM only, matching Telegram's owner-DM rule.
- Interactive cards / buttons (#7548). The text protocol is designed so a card can replace the rendering later without changing the decision path.
- Changing the approval ladder, the deny floor, or what needs approval.
- Changing the dashboard approval timeout (`agent.tool_approval_timeout_secs`).
- The heads-up-only notification (#32130). It is complementary and smaller; this RFC does not block or replace it.
- Adopting the Phase 3 seam for Telegram/Discord/WhatsApp. The seam should make that a small follow-up, but it is not in scope.

## 5. Design

### 5.1 Phase 1: make "Connect to Feishu DM" two-way

Phase 1 adds no new user-facing surface. The menu action already exists; Phase 1 makes Feishu honour the `supports_session_resume` contract so the same click yields a `direction: both` binding (2.2).

- Add `feishu/session_resume.py`, modelled on `telegram/session_resume.py`, implementing `ResumeSurface` with Chinese copy consistent with `feishu/commands.py`.
- **Primary path is the dashboard menu.** Binding from the dashboard needs no Feishu-side UI; the work is the inbound half and the leave path.
- **Routing.** Inbound DM messages from the owner consult the binding through the shared routing state machine and, when bound, hand off with `hand_to_resumed_slot` as Telegram does. Unbound DMs keep their own session.
- **Ownership.** Declare `supports_session_resume=True` and implement `may_resume_from` with Telegram's rule: a DM, and exactly one allow-listed owner (`feishu.allowed_open_ids` of length 1). Groups never qualify.
- **One binding per DM.** A Feishu DM has no threads, so the binding key is the DM. On bind the DM is told which session it now drives; `/help` shows the binding.
- **Leave.** Add a leave command to `feishu/commands.py` (today: `/new`, `/compact`, `/agent`, `/stop`, `/help`). Disconnecting from the dashboard menu also ends the binding.
- **Optional, may follow later.** A Feishu-side picker to bind from the phone. Feishu has `max_buttons=0`, so it would be a numbered list; the reply `N` is a choice only while a picker for that DM is open (the shared registry's nonce/TTL already scopes this).

### 5.2 Phase 2: numbered-text approval for turns on the Feishu path

- Mirror WhatsApp: in `feishu/transport_dispatch.py`, construct `TextReplyApprovalDecider(session_key, sessions=...)` only for an operator sender; pass `approval_session_key`; otherwise keep `decider=None` (deny-by-default).
- The renderer opens each prompt with `open_approval(..., conversation_id=<the DM's chat_id>)`, as WhatsApp's renderer does. Before a message becomes a prompt, dispatch checks `pending_for(session_key, chat_id)`; if the text parses with `parse_approval_reply`, it calls `deliver_verdict(session_key, verdict, chat_id)` and sends a short receipt instead of starting a turn. Passing the conversation id on all three calls matters: under `messaging.dm_scope=unified` one session key can have a pending approval opened from another channel, and a Feishu `1` must never answer a prompt Feishu was not shown.
- The renderer sends the numbered prompt via the existing reply path (text). Timeout: `TEXT_APPROVAL_TIMEOUT_S` (300 s), matching WhatsApp; an unanswered prompt is denied and announced.
- `feishu/gateway.py:_resolve_approval_mode` keeps its shape; `interactive` now means "ask" for the operator.

### 5.3 Phase 3: channel-neutral linked approval mirror

Replace the Slack-specific branch in `permission_prompt.py` with a small registry, in the style of `spawn_approval_delivery.py`:

- A channel registers a `LinkedApprovalDelivery` keyed by channel namespace with two calls: `post(session_key, link, prompt) -> handle | None` and `resolve(handle, verdict)`.
- `permission_prompt.py` looks up the slot's linked channel and, if a delivery is registered and the link is two-way, posts the prompt there. Slack becomes the first registered implementation, with behaviour unchanged (same pause/mute rules, same failure handling).
- The Feishu implementation posts the numbered prompt to the bound DM (proactive send, #31283) and records `(session_key, request_id, chat_id)`; a reply is matched only against prompts posted to that `chat_id`.
- **Two choices, not three.** A linked prompt offers `1` approve once and `2` deny. `resolve_approval` in `dashboard/state_parts/dashboard_services.py` takes only an approve/reject boolean, so it cannot grant "Trust this session"; that needs the slot's trust state, the pending card's `trust_grantable` proof and the effective session's approval policy, as the dashboard's own trust action handles them (compare the `trust` card action in `dashboard/handlers/crew_workers.py`). Offering `3` from Feishu is Q6.
- When the owner replies in that DM, Feishu dispatch checks for an outstanding linked approval for the bound session **before** `hand_to_resumed_slot`; a parsed verdict calls `resolve_approval` on the dashboard state with the same request id. A reply that does not parse is handed off as a normal message (it may be the user's explanation).
- First answer wins: if the dashboard resolves first, the Feishu side gets `resolve(handle, verdict)` and sends a one-line "already answered in the dashboard" notice; a late `1` in the DM then gets "no pending approval" rather than becoming a prompt.
- Timeout stays the dashboard's (`agent.tool_approval_timeout_secs`, default 600 s), because the future being resolved is the dashboard's. Interaction with the goal-loop approval hold is unchanged: an unanswered prompt still holds the loop.
- **Relation to #32130.** #32130 sends a text heads-up ("an approval is waiting… open the dashboard") to any outbound-capable linked channel. If #32130 lands first, its send can become the `post` of the registry here, and 3b upgrades the heads-up into an answerable prompt for two-way links. Either can land without the other.

## 6. Migration plan

| Phase | PR-sized change | Exit criteria |
|---|---|---|
| 1 | Feishu resume adapter, leave command, capability flag, docs row | **Connect to Feishu DM** on a dashboard session yields a `direction: both` binding (menu reads `Two-way · Feishu ↔ this session`), and a DM reply continues that session (row visible in the dashboard tab); leaving returns the DM to its own session; a non-owner cannot bind; a group message cannot bind; with two allow-listed `open_id`s the link stays one-way; tests mirror the Telegram resume suite. |
| 2 | Feishu text decider + reply consumption | In `interactive` mode a Feishu-path tool call produces a numbered prompt; `1` runs it, `2` denies, `3` trusts the session; silence denies at 300 s with a notice; a non-owner's `1` is a normal message and approves nothing. Capability matrix "Approval prompt waits" for Feishu reads 300s. |
| 3a | Refactor Slack linked approval into the registry, no behaviour change | Existing Slack linked-approval tests pass unchanged; `permission_prompt.py` no longer imports `kiro_crew.slack.handler` directly. |
| 3b | Feishu linked-approval delivery | A dashboard-run tool approval in a Feishu-bound session appears in the DM with choices `1` and `2` only; `1` in the DM resolves the dashboard prompt; a `1` sent while the only pending approval for that session key was opened from another conversation is not consumed; answering in the dashboard first produces the "already answered" notice; a late reply is not consumed as a prompt. Blocked on Q1. |

Each phase is independently abandonable: 1 is useful alone (questions answered as text), 2 is useful alone (Feishu-native sessions), 3a is a refactor that stands without 3b.

**3a and 3b may ship as one PR.** The split is a recommendation, not a requirement. Kept apart, 3a is a pure refactor whose only review question is "did Slack's behaviour change", a regression on the security-sensitive approval path is attributable to one change, and maintainers can accept the seam while still debating the Feishu consumer (or reject the seam per Q1 without wasting the Feishu work, which then falls back to the Feishu-only alternative in section 9). Combined, the reviewer sees the seam shaped by a real consumer and the project skips a PR with no user-visible value; the Slack branch being replaced is about 100 lines of `permission_prompt.py`. A combined PR is acceptable on one condition: 3a's exit criterion still holds inside it, that is, the existing Slack linked-approval tests pass unchanged, with no edits to their assertions. Maintainers choose.

## 7. Backward compatibility

- No config keys change. A user who never connects a Feishu DM sees no behaviour change from Phase 1. An existing one-way Feishu binding may need to be reconnected once to pick up `accepts_inbound`, which is computed when the link is made (to be verified in Phase 1).
- Phase 2 changes `interactive` on Feishu from "refuse silently" to "ask" for the owner only. Users who set `auto`/`trust` are unaffected. This is a user-visible default change for `interactive` users and should be called out in the changelog.
- Phase 3a must be behaviour-identical for Slack.

## 8. Security considerations

- **Who can approve.** Only the allow-listed owner, as WhatsApp's dispatcher in `whatsapp/transport_dispatch.py`. A peer on the allow-list may chat but gets `decider=None`. Linked approvals (3b) re-check the sender against the binding's owner, not just the allow-list.
- **Who can bind.** Telegram's rule (`may_resume_from` in `telegram/transport.py`): exactly one allow-listed owner, DM only. A second allow-listed `open_id` makes the link one-way again rather than letting either person drive the session.
- **Exact-answer parsing.** `parse_approval_reply` counts only a reply that is entirely an answer, so prose is never consumed as a verdict.
- **Scope of a verdict.** A verdict resolves exactly one `(session_key, request_id)` that was shown in the replying conversation (`conversation_id` on `open_approval`, `pending_for` and `deliver_verdict`). "Trust this session" is offered only on the Feishu path (Phase 2), where `TextReplyApprovalDecider` already handles it; a linked prompt (Phase 3) offers approve once or deny.
- **Deny on silence.** Unchanged: the timeout never approves.
- **Restricted sessions.** Incognito/temporary sessions follow the same ceiling the Slack mirror applies; if the Slack path withholds a prompt for a muted/paused mirror (`slack_mirror_is_paused`), the registry applies the same check for every channel.
- **Audit.** Bind, unbind and every remote verdict are recorded in the SEL audit log with channel, sender id and request id.
- **No new outbound reach.** Prompts go only to the bound owner DM via the existing allow-listed proactive send.
- **Per-surface governance** (rfc-session-address-model §10.2) is left as is: the session's existing approval policy applies whether the answer comes from the dashboard or the DM.

## 9. Alternatives considered

- **Wait for interactive cards (#7548).** Better UX, but parked; the text protocol works now and a card can later render the same prompt and resolve the same request id.
- **Feishu-only hack in `permission_prompt.py`** (a second `if slot._feishu_linked` branch beside Slack). Smaller, but repeats the Slack-specific coupling and leaves Telegram/Discord without a path. Rejected in favour of 3a/3b.
- **Heads-up only (#32130).** Tells the user to go to the dashboard; does not let them answer. Kept as a complementary, smaller step.
- **Answer by forwarding text only (Phase 1 alone).** Covers questions the agent asks in prose, not tool approvals. Kept as Phase 1's standalone value, not as the full answer.
- **Set `auto` approval for unattended loops.** Works today, removes the human gate. Not a substitute.
- **Wait for rfc-session-address-model's surface work.** Its Phase 3 (durable `channels[]`) and Phase 4 (opaque identity, blocked on a ruling since 2026-09-11) would give Phase 3 here a cleaner reachability source, but neither is started. The registry in 5.3 reads the existing mirror binding today and can switch to `channels[]` later without changing callers.

## 10. Open questions

- Q1. Do maintainers want the linked-approval registry (3a) at all, or is cross-channel approval of dashboard turns meant to wait for rfc-session-address-model's surface work? Phase 3 is blocked on this answer.
- Q2. Is the 2.3 finding (no non-Slack channel receives a dashboard-run approval, Telegram/Discord two-way links included) correct and intended?
- Q3. Linked-approval timeout: the dashboard's 600 s, or the text channels' 300 s? This draft picks the dashboard's because that is the future being resolved.
- Q4. Command names for leaving (and the optional picker) on Feishu: reuse the names Telegram and Discord expose for session resume, or Feishu-specific ones?
- Q5. While a DM is bound, should a Feishu-native session's own pending approval (Phase 2) and a linked approval (Phase 3) ever coexist? This draft says no: binding suspends the DM's own session, so only linked approvals are answerable while bound.
- Q6. Should a linked prompt offer `3` (trust this session)? It needs a shared dashboard helper that applies the slot trust state, `trust_grantable` and the session's approval policy, instead of the boolean `resolve_approval`. This draft leaves it out.

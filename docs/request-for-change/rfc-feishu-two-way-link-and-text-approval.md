---
title: Feishu two-way dashboard link, numbered-text tool approval and owner-only group resume -- steer a dashboard session or a Bot from Feishu
status: draft
author: jhua-labs
created: 2026-11-06
last-audited: 2026-11-15
audited-at: dc4692c96f
doc-pr: null
implementation-prs: []
tracking-issues: [31448]
supersedes: []
superseded-by: []
---

# RFC: Feishu two-way dashboard link, numbered-text tool approval and owner-only group resume

- Status: draft. Nothing built. Tracking issue [#31448](https://github.com/kirodotdev/KiroCrew/issues/31448). A maintainer asked on that issue for this RFC as its own docs-only pull request; whether the design is accepted is decided in that pull request's review, and each implementation pull request needs a maintainer design sign-off before it merges.
- Related: #31283 (Feishu proactive send, merged), [rfc-notification-bridge-actions.md](rfc-notification-bridge-actions.md) and its tracking issue [#32902](https://github.com/kirodotdev/KiroCrew/issues/32902) (B4: answering a bridged approval by reply; Phase 3 here defers to it), #36602 (Feishu heads-up when a dashboard approval is waiting, merged; closed #32130), #7548 (Feishu interactive cards; a later, nicer approval UI, not required here), [rfc-session-address-model.md](rfc-session-address-model.md) (the longer-term surface model this should not contradict), [rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md) (what an unanswered approval does to a loop).
- Measured at `dc4692c96f` (main, 2026-11-15). Code is cited by file and symbol, read at that commit.
- Revision 2026-11-15: the first draft (audited at `e30915e712`) proposed its own channel-neutral linked-approval registry as Phase 3. B4 has since shipped that seam for Telegram, Discord and Webex, so Phase 3 now asks only for Feishu to join B4. Phase 4 (owner-only group resume, one group per Bot) is new.

## 1. Summary

A user who is reachable only on Feishu cannot steer a dashboard session or a Bot chat from the phone. **Connect to a channel** to Feishu is outbound-only, Feishu has no approval decider, and a Feishu group is never more than a place a Bot posts to. This RFC proposes four phases that can each ship or be dropped on their own:

1. **Two-way owner DM.** Make **Connect to Feishu DM** two-way for the single allow-listed owner, the way Telegram does today.
2. **Numbered-text approval on the Feishu path.** Reuse `TextReplyApprovalDecider` as WhatsApp does, so a turn running on the Feishu path asks instead of refusing.
3. **Dashboard approvals answered from Feishu, through B4.** Add Feishu to B4's reply transports. No new seam.
4. **Owner-only group resume.** Let an allow-listed Feishu group linked to a session or a Bot carry the owner's replies back into it, so each Bot can have its own two-way Feishu group. Depends on Phase 1 and on Q3.

## 2. Motivation

### 2.1 The scenarios, as they happened

- **Approval from the phone (2026-11-07).** The author ran a dashboard turn, walked away and watched the Feishu DM. The turn reached a tool that needed interactive approval. Nothing arrived on Feishu; the prompt waited its 600 s window in the dashboard (`agent.tool_approval_timeout_secs`) and was auto-declined. Since then #36602 sends a heads-up to Feishu, but the user still cannot answer it there.
- **A Bot reply taken by the wrong session (2026-11-14).** A daily-report Bot chat was linked to the owner's Feishu DM through **Connect to a channel**, while the main dashboard session already owned that DM. The Bot's report arrived in Feishu. The owner replied `1` to it. The reply did not reach the Bot chat: it went to the main session that owns the DM key, which took it as an answer to its own pending options. Nothing on the Feishu side shows which session a reply will reach.
- **One group per Bot.** To keep several Bots out of one DM, the natural layout is one Feishu group per Bot (`chat_id` in `allowed_group_ids`, picked in that Bot's **Connect to a channel** menu). Today each group only receives the Bot's posts; a reply in it never reaches the Bot.

### 2.2 Current state on Feishu

- **Outbound-only link.** `FEISHU_CAPABILITIES` (`feishu/transport.py`) does not declare `supports_session_resume`, so it takes the default `False` (`ChannelCapabilities` in `messaging/transport.py`), and Feishu does not override `may_resume_from`. `dashboard/chat_mirror.py` (`_resumes_inbound`) therefore writes an outbound-only binding, and a reply in the DM runs in `feishu:<app>:direct:<open_id>`, not in the linked session. `src/kiro_crew/docs/channel-capabilities.md` row "Dashboard link is two-way" is ❌ for Feishu.
- **The label is already data-driven.** The `One-way`/`Two-way` tag is rendered from the slot row's `drives_session` (`website/src/components/LinkedSurfacesSection.tsx`), derived from `mirror_accepts_inbound`, which `_resumes_inbound` computes from `supports_session_resume` plus `may_resume_from`. Phase 1 changes no frontend code.
- **No approval decider.** `_resolve_approval_mode` in `feishu/gateway.py` resolves to `auto` or `interactive`; Feishu has no interactive decider, so `interactive` means deny-by-default. `src/kiro_crew/docs/channel-capabilities.md`: "WeCom, Weixin, iMessage and Feishu never prompt".
- **Read-only in B4.** `TRANSPORT_SINKS` in `notifications/bridge_transport.py` includes `feishu`, so a bridged approval note reaches Feishu. `REPLY_TRANSPORTS` lists only `discord`, `telegram` and `webex`; its comment says each needs "an inbound reader that knows which message a reply quotes". Feishu's inbound parser (`feishu/client.py`) does not read `parent_id` or `root_id` today.
- **Groups are gated by sender already.** `FeishuTransport.receive` serves a group message only when the group is in `allowed_group_ids` with `allow_group` on, and the message @-mentions the bot; it then runs `authorize()` on the sender `open_id` against `allowed_open_ids`, the same check a DM gets. A group member who is not allow-listed is refused and SEL-audited today. `_route` in `feishu/transport_dispatch.py` keys a group on its `chat_id` (one session for all members), and a group turn always runs with `minimal_context`, because a reply in a group is read by every member.

### 2.3 How far inbound resume reaches on other channels

- **Telegram** overrides `may_resume_from` to `thread_id is None and len(self._allowed) == 1 and conversation_id in self._allowed`: one unambiguous owner DM only. Groups and topics stay outbound-only.
- **Discord** keeps the default `may_resume_from` (`messaging/transport.py`), which follows the capability, so an allow-listed thread (`Discord thread · <id>`) also carries replies back into the linked session.
- **Slack** resumes through the thread `slack-link` opens in a channel.

Phase 1 copies Telegram. Phase 4 is the Discord-like extension, with an owner-sender rule Discord does not need.

### 2.4 What already exists to build on

- `messaging/session_resume.py`: the channel-neutral half of session resume (eligible sessions, picker registry with nonce/TTL/owner scoping, binding-conflict rules, inbound routing). Channel adapters are `telegram/session_resume.py` and `discord/session_resume.py`.
- `messaging/approval.py`: `TextReplyApprovalDecider`, `parse_approval_reply`, `pending_for`, `deliver_verdict`, `TEXT_APPROVAL_TIMEOUT_S = 300.0`. WhatsApp wires it in `whatsapp/transport_dispatch.py`, constructing the decider only for an operator sender.
- B4: `notifications/bridge_reply.py` (`answer_bridged_reply`) and the per-channel readers `telegram/dispatch/bridge_reply.py`, `discord/bridge_reply.py`, `webex/bridge_reply.py`.
- `dashboard/chat_mirror.py`: refuses a second inbound-capable binding on one conversation ("another session is already linked to this conversation").

## 3. Goals

- G1. **Connect to Feishu DM** produces a two-way binding for the single allow-listed owner: while bound, every owner message in that DM continues the linked session or Bot chat, visibly in its tab; the owner can leave the binding from the DM or the dashboard menu.
- G2. A turn on the Feishu path in `interactive` mode asks for approval as a numbered message instead of refusing; silence denies.
- G3. A bridged approval note on Feishu can be answered by replying to it, through B4, with B4's owner-only admission.
- G4. An allow-listed Feishu group linked to a session or a Bot carries the owner's @-mentioned replies back into it; nobody else's message drives it.
- G5. Only the allow-listed owner can bind, approve or resume. Every other sender keeps today's behaviour.
- G6. `src/kiro_crew/docs/channel-capabilities.md` and `src/kiro_crew/docs/feishu-integration.md` say what is true after each phase.

## 4. Non-goals

- Interactive cards / buttons (#7548). The text protocol is designed so a card can replace the rendering later without changing the decision path.
- Changing the approval ladder, the deny floor, what needs approval, or the dashboard approval timeout.
- Multiple Feishu apps per gateway (one Feishu bot per Kiro Crew Bot). Out of scope; one app, one bot identity.
- Group resume for any sender other than the owner, or for a group with more than one allow-listed person driving it.

## 5. Design

### 5.1 Phase 1: two-way owner DM

- Declare `supports_session_resume=True` and override `may_resume_from` with Telegram's rule: a DM, and exactly one allow-listed owner (`feishu.allowed_open_ids` of length 1). Groups do not qualify in this phase.
- Add `feishu/session_resume.py`, modelled on `telegram/session_resume.py`, implementing `ResumeSurface`, with Chinese copy consistent with `feishu/commands.py`.
- **Routing.** An owner DM message consults the binding through the shared routing state machine and, when bound, hands off with `hand_to_resumed_slot` as Telegram does. An unbound DM keeps its own session.
- **One binding per DM.** The existing conflict rule in `chat_mirror.py` already allows one inbound-capable binding per conversation. On bind the DM is told which session or Bot it now drives; `/help` shows the binding. This also fixes the 2026-11-14 case: the owner sees where a reply goes, and linking a Bot to the DM moves the inbound side to the Bot instead of leaving it with the previous owner session.
- **Leave.** Add a leave command to `feishu/commands.py`. Disconnecting from the dashboard menu also ends the binding.
- **Optional later.** A Feishu-side picker to bind from the phone, as a numbered list (`max_buttons=0`), scoped by the shared registry's nonce/TTL.

### 5.2 Phase 2: numbered-text approval for turns on the Feishu path

- Mirror WhatsApp: in `feishu/transport_dispatch.py`, construct `TextReplyApprovalDecider(session_key, sessions=...)` only for the owner; pass `approval_session_key`; otherwise keep `decider=None`.
- The renderer opens each prompt with `open_approval(..., conversation_id=<chat_id>)`. Before a message becomes a prompt, dispatch checks `pending_for(session_key, chat_id)`; a reply that parses with `parse_approval_reply` calls `deliver_verdict(session_key, verdict, chat_id)` and gets a short receipt instead of starting a turn. Passing the conversation id matters under `messaging.dm_scope=unified`: a Feishu `1` must never answer a prompt Feishu was not shown.
- Timeout `TEXT_APPROVAL_TIMEOUT_S` (300 s), as WhatsApp; an unanswered prompt is denied and announced.

### 5.3 Phase 3: dashboard approvals answered from Feishu, through B4

The first draft proposed a linked-approval registry in `permission_prompt.py`. B4 now delivers bridged approval notes to Feishu and accepts replies on Telegram, Discord and Webex, so this RFC no longer proposes its own seam. Phase 3 is:

- Read the quoted message on Feishu inbound (`parent_id`, and `root_id` where Feishu sets it) and record the message id of each bridged note Feishu sends, so a reply can be matched to the note it answers.
- Add a Feishu reader alongside `telegram/dispatch/bridge_reply.py` that hands a matched reply to `answer_bridged_reply`, and add `feishu` to `REPLY_TRANSPORTS`.
- Owner-only admission, verdict scope and timeout are B4's, unchanged.

The design and sequencing belong to B4 and are tracked on #32902. This section records only that Feishu's two-way link (Phase 1) and B4's reply path share the quoted-message reader, so whichever lands first should build it for both.

### 5.4 Phase 4: owner-only group resume (one group per Bot)

Depends on Phase 1. Blocked on Q3.

- **Admission.** Extend `may_resume_from` to admit an allow-listed group (`allow_group` on, `chat_id` in `allowed_group_ids`), still requiring exactly one allow-listed owner. Inbound resume additionally requires that the message's sender is that owner. The group gates in `FeishuTransport.receive` (allow-listed group, @-mention, `authorize()` on the sender) stay in front, so the change is one more check, not a new gate.
- **Other members.** A message from anyone else in the group never resumes the session. It keeps today's behaviour: refused if the sender is not allow-listed; if the owner rule rejects it, it is answered with a one-line refusal and does not start a turn in the linked session.
- **Binding.** One group, one linked session or Bot. A group binding and the owner DM binding are separate conversations and do not conflict.
- **Context.** Today a group turn is always `minimal_context`, and `_route` keeps group traffic out of the DM session so private content is not read back to the group. Resuming a linked session from a group would bring that session's transcript, memory and lessons into replies that every group member reads. Q3 asks how to resolve this; the options are in section 10.
- **Visibility.** The group is told which session or Bot it drives when bound, as in Phase 1.

## 6. Migration plan

| Phase | PR-sized change | Exit criteria |
|---|---|---|
| 1 | Feishu resume adapter, `may_resume_from`, leave command, capability flag, docs row | **Connect to Feishu DM** yields a `direction: both` binding (menu reads `Two-way`); an owner DM reply continues that session or Bot (row visible in its tab); leaving returns the DM to its own session; a non-owner cannot bind; a group cannot bind; with two allow-listed `open_id`s the link stays one-way; tests mirror the Telegram resume suite. |
| 2 | Feishu text decider + reply consumption | In `interactive` mode a Feishu-path tool call produces a numbered prompt; `1` runs it, `2` denies, `3` trusts the session; silence denies at 300 s with a notice; a non-owner's `1` is a normal message. |
| 3 | Feishu quoted-message reader + B4 reply reader | A reply to a bridged approval note on Feishu resolves it as on Telegram; a reply from a non-owner, or to a different message, resolves nothing. Sequenced by #32902. |
| 4 | Group admission in `may_resume_from` + owner-sender check | An owner @-mention in an allow-listed, linked group continues the linked session or Bot; another member's message never does; with two allow-listed owners the group link stays one-way; behaviour under Q3's answer is tested. |

Each phase is independently abandonable: 1 is useful alone, 2 is useful for Feishu-native sessions, 3 follows B4's plan, and 4 needs 1.

## 7. Backward compatibility

- No config keys change. A user who never links Feishu sees no change.
- An existing one-way Feishu binding may need to be reconnected once to pick up `accepts_inbound`, which is computed when the link is made (to be verified in Phase 1).
- Phase 2 changes `interactive` on Feishu from "refuse silently" to "ask" for the owner only. Call it out in the changelog.
- Phase 4 changes nothing for a group that is not linked to a session.

## 8. Security considerations

- **Who can bind, approve or resume.** Only the single allow-listed owner, following Telegram's `may_resume_from` and WhatsApp's operator-only decider. A second allow-listed `open_id` makes every Feishu link one-way again.
- **Group senders (Phase 4).** Admission moves from the conversation to the sender: the group must be allow-listed and linked, and the sender must be the owner. A group member cannot drive the session even if they are allow-listed for their own DM.
- **Private context in a group (Phase 4).** See Q3. Until it is answered, Phase 4 does not ship.
- **Exact-answer parsing.** `parse_approval_reply` counts only a reply that is entirely an answer.
- **Scope of a verdict.** A verdict resolves exactly one approval shown in the replying conversation (`conversation_id` on `open_approval`, `pending_for`, `deliver_verdict`; B4's quoted-message match for Phase 3).
- **Deny on silence** is unchanged.
- **Audit.** Bind, unbind, every remote verdict and every refused group resume are recorded in the SEL audit log with channel, sender id and request id.
- **No new outbound reach.** Messages go only to allow-listed DMs and groups.

## 9. Alternatives considered

- **A Feishu-only linked-approval registry** (the first draft's Phase 3). Superseded by B4, which already owns this seam for three transports.
- **Wait for interactive cards (#7548).** Better UX, but parked; the text protocol works now.
- **Group resume for every allow-listed sender (Discord's default).** Simpler, but a Feishu group's members are not all the owner, and one group session would be driven by several people. Rejected in favour of the owner-sender rule.
- **One Feishu app per Bot.** Gives each Bot its own name and avatar, but needs several long connections per gateway and routing by app. Out of scope.
- **Set `auto` approval for unattended loops.** Works today and removes the human gate. Not a substitute.

## 10. Open questions

- Q1. Command names for leaving a binding (and the optional picker) on Feishu: reuse the Telegram/Discord names, or Feishu-specific ones?
- Q2. While a DM is bound, should a Feishu-native session's own pending approval (Phase 2) and a bridged approval (Phase 3) coexist? This draft says no: binding suspends the DM's own session.
- Q3. **Private context in a resumed group (Phase 4).** Which of these should apply?
  - (a) Admit only a group whose members are exactly the owner and the bot, checked at bind time and on each turn through the Feishu chat-members API, and run the turn with full context.
  - (b) Admit any allow-listed group, but run a group-resumed turn with `minimal_context`, so the linked transcript continues while memory, lessons and skills are withheld.
  - (c) Leave groups outbound-only and rely on the DM.
  This draft leans to (a) for the one-group-per-Bot case, with (b) as the fallback when membership cannot be read.
- Q4. Should a refused group message from another member get a visible refusal, or be dropped silently as today's non-mentioned chatter is?

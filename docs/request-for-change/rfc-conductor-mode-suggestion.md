---
title: "Conductor mode suggestion: the agent offers the mid-chat switch once, and the person presses it"
status: draft
author: neilieva
created: 2026-11-19
last-audited: 2026-11-19
audited-at: b3aa5fbb24
doc-pr: 39471
implementation-prs: []
tracking-issues: [33753]
supersedes: []
superseded-by: []
---

# RFC: Conductor mode suggestion

**Status:** `draft`. Nothing here is built. Measured against main `b3aa5fbb24`.

## 1. Summary

A plain chat can already switch into conductor mode mid-conversation (PR #28901), but only if the person notices the chip. The dashboard labels it "Conductor mode". This RFC adds one way for the agent to point at it: a new session directive, `suggest_crew_mode`, that shows a small card with one button. The button calls the same `turnOnCrewMode` the chip calls. The card is sent at most once per chat, shows only where the chip itself is offered, and can only be raised on a turn a person sent. The agent never switches the chat.

## 2. Motivation

### Current state (at `b3aa5fbb24`)

- A plain root chat that already has a message is offered the mid-chat switch: `crewModeMidChat = crewModeEligible && !crewModeChosen && hasUserMessage && isRootSession(currentSlot)` (`website/src/pages/chat/page/crewModeSwitch.ts`). The composer shows it as a chip when `crewModeMidChatOffered` is true (`website/src/pages/ChatPage.tsx`, `website/src/pages/chat/page/ComposerDockView.tsx`).
- The client is the only place that decides eligibility. Memory mode, app origin and the default agent are read in `plainChat` and `crewModeEligible` (`crewModeSwitch.ts`). The server has no matching rule for turning conductor mode on.
- The switch is one way. After the first message the server refuses to move a conductor mode root onto another agent (`CREW_MODE_LOCKED` and `_crew_mode_lock_refusal` in `dashboard/chat_api/agent_switch.py`, and the module docstring of `src/kiro_crew/crew_mode.py`). The chip's hint doesn't say so (`components.crewMode.chip_hint_off`).
- The agent already has a card path for next steps: the `suggest_followup` directive (`dashboard/session_directive_apply.py`). Its applier, `_suggest_followup` in `dashboard/session_directive_apply_slot.py`, delivers a `followup_card` to every owner socket through `deliver_ws_owners`, and `website/src/hooks/useWebSocket.ts` handles it. Each client stores the card per slot (`setFollowupCard`, `website/src/store/chat/composerCards.ts`) and dismisses its own copy (`dismissFollowupItem`).

### Problems

Work often starts as one thread and turns into several independent tasks, each with its own done condition. By then the person is deep in the conversation and the chip is easy to miss. They keep running parallel work by hand in one thread, or notice late.

## 3. Goals

- The agent can suggest conductor mode when the work splits, and the person decides.
- Pressing the card does what pressing the chip does.
- No change to who is eligible for conductor mode, and no change to the one-way lock after the first message.

## 4. Non-goals

- The agent switching a chat on its own. The switch is one way, so the person always confirms.
- Conductor mode for custom-template chats (#33369). This follows whatever `crewModeMidChat` allows.
- Starting chats in conductor mode by default (`dashboard.new_chat_crew_mode`, #30904).
- A server copy of the client's eligibility rule.
- A tuned trigger. The first rule comes from #33753, and later work can tune it.

## 5. Design

### 5.1 The directive

A new session directive kind, `suggest_crew_mode`, with no arguments. The MCP tool emits it the way `suggest_followup` in `mcp_tools/control_parts/directive.py` does, and the applier handles it next to that kind in `session_directive_apply.py`. It answers with one sentence the model can act on, like `suggest_followup` does:

1. "Already in conductor mode" when `crew_mode.slot_is_crew_mode_root(slot)` is true (`src/kiro_crew/crew_mode.py`). It sends nothing and leaves the flag as it is. This comes first, so a chat that took the offer reports where it is now.
2. "Already offered" when the slot's `crew_mode_offered` flag is set (§5.3). It sends nothing.
3. "Not saved" when the slot save in the next paragraph fails. The applier clears the flag and sends nothing.
4. "Not delivered" when `deliver_ws_owners` reports zero completed sends. The applier clears the flag, saves the slot again, and the model says it in text instead.
5. "Sent" when at least one client got the `crew_mode_suggestion` event. The reply says the dashboard shows the card only where the chat can switch now, so the model must not assume the person saw it.

The applier checks and sets `crew_mode_offered` with no await in between. Then it force-saves the slot, the way the pin route saves `pinned` (`save_slot_off_loop(..., force=True)`, `dashboard/chat_folders.py`), and only then delivers. So two calls in the same chat can't both send, and a restart can't offer the card twice. A crash after the save and before delivery spends the offer with no card shown, which fails closed. Every case writes an audit row through `_audit`, like the other kinds (`session_directive_apply.py`).

The kind joins `_DASHBOARD_ONLY_DIRECTIVES` beside `suggest_followup` (`session_directive_apply_outcome.py`). That gate runs in `apply_session_directive_outcome` before the applier, so a refused call sets no flag. It refuses a turn with no slot, or a session that `has_dashboard_surface` rejects (`session_directive_apply.py`). A dashboard-born chat passes it even with every tab closed, and gets case 4.

Case 1 is the only eligibility check on the server. It reads the slot without the slot lock, so a switch that commits in the same tick costs one spent offer in a chat that can't show the card. All other eligibility stays on the client (§5.2).

### 5.2 The card

Each client stores the event per slot, the way `setFollowupCard` does. It renders the card only while `crewModeMidChatOffered` is true for the slot on screen, the same value that shows the chip, and hides it as soon as the value goes false. It never judges eligibility when the event arrives, because a window showing another chat can't compute it. The copy is fixed and carries no agent text:

- One line, a new catalog key under `components.crewMode`, saying the work looks like it splits into separate tasks and that conductor mode stays on for this chat once turned on.
- The chip itself (`CrewModeChip`) as the button, with its label (`components.crewMode.chip_off`, "Conductor mode") and the same busy inputs the mid-chat chip gets (`crewModeBusy`, `memorySwitching`, `crewModeSwitching`, `ComposerDockView.tsx`). The card can arrive while the suggesting turn still runs, and the button stays unavailable until the turn ends. It calls `turnOnCrewMode` (`crewModeSwitch.ts`).
- A dismiss, also a new catalog key, that only hides the card.

Every attached owner client gets the event and may render its own copy, as with `followup_card`. Dismissing is per client. Pressing on one client switches the chat, and `switch_slot_agent` pushes the new slot list to every client at the end of every successful switch (`state.push_slots_update()` in `dashboard/chat_api/agent_switch.py`). `crewModeMidChatOffered` then goes false everywhere and the card leaves every client.

If no client can show the card (a non-root chat, a crew member's chat, a chat on an agent other than the default, a non-persistent memory mode, an app chat), the offer is still spent. A later change, such as a memory-mode switch or a switch back to the default agent, doesn't restore it. The model was told the card might not show.

### 5.3 Once per chat

`crew_mode_offered` is a boolean persisted with the slot, registered everywhere `pinned` is:
- A `Field` row like the one for `pinned` (`dashboard/slot_persistence/metadata_codec.py`), so `_flag` writes it only when it's true.
- A reader like `_read_pinned` (`metadata_codec.py`).
- Entries in `LINE_ORDER` and `MERGE_ORDER` (beside `pinned` in `metadata_codec.py`).
- An entry in `SLOT_OWNED_META_KEYS` (beside `pinned` in `history_schema.py`), so the save in case 4 that omits a false value also clears a true one already on disk.

A slot saved before this change reads as not offered. Once set, the directive sends nothing for the life of the chat.

### 5.4 Who may raise it

`suggest_crew_mode` joins `USER_ORIGIN_DIRECTIVES` (`directive_origin.py`). So a cron, a loop wake, a sub-agent or a peer send riding a user's slot is refused, as `suggest_followup` is (`session_directive_apply.py`, and the comment at `context_assembly/turn.py`). The per-turn guidance rides the same `user_origin_gate_admits` call `suggest_followup` uses (`context_assembly/turn.py`), so a turn the user-origin gate refuses never sees the tool. A slot-less channel turn can still see the guidance, and the dashboard-only gate refuses its call (§5.1).

### 5.5 When the agent suggests it

One short line in the interactive guidance, appended after the `suggest_followup` line (`context_assembly/turn.py`) and behind the same gate. The rule is the one #33753 proposes, "three or more independent items with separate done conditions", with "items" read as tasks in the agent's plan:

> (The suggest_crew_mode tool offers this chat a switch to conductor mode. Use it once, only when your plan has three or more independent tasks with separate done conditions. Never switch the chat yourself.)

Cost: this line is 205 characters, about 50 tokens. The `suggest_followup` line is 330 characters (`turn.py`), about 80 tokens. The schema sits beside `suggest_followup`'s (`mcp_tools/control_parts/schemas.py`), which is about 4 KB. This one has no properties, so it is smaller.

## 6. Migration plan

The implementation ships in one phase, small enough for one PR.

**Phase 1: directive, card, flag, guidance line.** The implementation PR also adds the two catalog keys and updates `docs/system-specs/modules/crew-mode.md` and `docs/architecture/mcp.md`. Exit criteria:
- On a person's turn in a plain root chat with a message, `suggest_crew_mode` answers "sent", the card shows, and pressing it puts the chat in conductor mode as the chip does.
- The card shows during the suggesting turn with its button unavailable, and the button works once the turn ends.
- The card says conductor mode stays on for this chat.
- A call whose slot save fails answers "not saved", sends nothing and leaves `crew_mode_offered` unset.
- After a "sent", the saved metadata holds `crew_mode_offered` before the event is delivered.
- A second call in the same chat answers "already offered" and sends nothing. This holds after a dismiss. It also holds after a gateway restart.
- In a chat already in conductor mode, the call answers "already in conductor mode", sends nothing, and leaves `crew_mode_offered` as it was.
- In a dashboard-born chat with every tab closed, the call answers "not delivered" and leaves `crew_mode_offered` unset on disk.
- The same call from a cron turn, a loop wake and a sub-agent on that slot is refused with the user-origin refusal.
- A call on a turn with no slot is refused with the dashboard-only refusal.
- Two calls in one turn send one event. The second answers "already offered".
- In a chat with a non-persistent memory mode, the call answers "sent", no card shows, and a second call answers "already offered".
- The guidance line is absent from a cron turn's prompt and present on a person's turn in a plain root chat.
- With two clients showing the chat, both show the card. Pressing it on one removes it from the other once that client processes the slots update.
- A client showing another chat when the event arrives shows the card after it opens this chat.

## 7. Backward compatibility

The change is additive. If the agent never calls the tool, nothing changes. A slot saved before this change reads as not offered (§5.3).

## 8. Security considerations

- **Nothing switches without the person.** The directive carries no switch. Only the card's button calls `turnOnCrewMode`, which is the chip's own path, and the switch's one-way lock is unchanged. The press is the switch route's own request (`POST /api/chat/slots/{slot}/agent`), so that route's own authorization applies to it.
- **The person sees that it's one way.** The card comes with the agent's endorsement, so it states what the chip's hint doesn't: conductor mode stays on for this chat.
- **No new origin can raise it.** Joining `USER_ORIGIN_DIRECTIVES` admits only a turn a person sent on a user-facing conversation, and the dashboard-only gate still applies (§5.1). Untrusted content an agent reads (a web page, a mail) could still steer it into calling the tool. The worst outcome is one card the person can dismiss, at most once per chat.
- **Fixed copy.** The directive takes no arguments, so nothing the agent read can reach the card's text.
- **No new entry point.** The card renders only where the chip is already offered. A sub-agent, a crew member or a conductor chat never shows it.

## 9. Alternatives considered

- **Leave it to the person.** This is how it works today, and the chip gets missed mid-task.
- **Start every chat in conductor mode** (`dashboard.new_chat_crew_mode`). This gives a conductor to chats that never needed one.
- **Let the agent switch the chat.** The agent would flip a one-way switch without the person's confirmation.
- **A new Crew chat with a handoff instead of a mid-chat switch.** #28901 already switches the ongoing chat, so a handoff would be a second way in.
- **Fold it into `suggest_followup`.** Follow-ups come after finished work, and this comes mid-task. A flag on that card would mix a mid-task switch with a finished-work suggestion.
- **Copy the client's full eligibility rule to the server.** Then the server could answer "not offered here" in every case. No server rule exists today, and a copy of `plainChat` and `crewModeEligible` would drift from the client's.
- **Check a few more slot fields on the server (root, crew member, memory mode).** The root check `card_lifecycle.is_root_session` answers false until the session tree is seeded after boot (`card_lifecycle.py`), so it would refuse real root chats in that window. The other fields are part of the client's rule, which brings back the drift problem. A wrong "sent" costs one spent offer, in a chat that usually can't show the card.
- **Set the flag only when a client renders the card.** That needs a new client-to-server call for one bit, and it still can't tell a dismiss from a missed render.

## 10. Open questions

None blocking. The wording in §5.5 can be tuned later without changing the design.

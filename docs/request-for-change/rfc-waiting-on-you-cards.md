---
title: Waiting-on-you cards — an agent pins what it needs from the person above the composer, and the card stays until it is answered, cleared or replaced
status: draft
author: unstablebrainiac (drafted with an agent session)
created: 2026-11-18
last-audited: 2026-11-18
audited-at: 640338acb1
doc-pr: null
implementation-prs: []
tracking-issues: [20961]
supersedes: []
superseded-by: []
---

# RFC: Waiting-on-you cards

Status: draft. Nothing is built and this document changes no code. It answers [#20961](https://github.com/kirodotdev/KiroCrew/issues/20961). Acceptance is requested from a maintainer, who records it on this Status line. It also asks for a ruling that amends one line of the [2026-09-13 `[OPTIONS:]` decision](../decisions/2026-09-13-options-are-read-from-the-newest-reply-only.md) (section "Relation to the 2026-09-13 decision"). Every code reference below names a symbol and was read at `640338acb1`.

## Summary

When an agent is blocked on the person, the only place that says so is the turn that first said it. In a watch loop, every later cycle writes another "still waiting on you" reply on top of it, and the person scrolls back to find what was actually asked.

This RFC proposes a **waiting-on-you card**. The agent declares, with a tool call, what it needs from the person: an action, or a decision with options. The dashboard pins the card above the composer and keeps it there across turns, loop cycles and gateway restarts. The card goes away when the person answers it or writes in the chat, when the agent clears it, or when it expires. A session can hold a short stack of them, one per open ask.

It adds a public agent tool contract and a persisted gateway store, and it changes a product-shape decision about the composer band, so per `CONTRIBUTING.md` it goes RFC-first, as its own pull request.

## Motivation

### Current state

Four mechanisms already say "the person is needed". None fits a wait that outlives one turn.

| Mechanism | What it does | Why it does not cover this |
|---|---|---|
| `[OPTIONS: …]` chips | Buttons under the composer, read from the newest assistant reply only. The same ending (`has_options`) puts an idle armed loop's sidebar row on "Waiting on you" (`loop_waiting`), and any session in the board's Waiting lane | The next reply without the tag removes them, so the 2026-09-13 decision tells a blocked loop to restate them every cycle. That restating is the noise this RFC removes. |
| `ask_question` card | A blocking card; the answer is the tool result | It lives for a fixed 30-minute window (`_AGENT_ASK_WINDOW_DEFAULT`, used by `api_agent_ask_open` in `src/kiro_crew/dashboard/handlers/ask_question.py`) and holds the turn open the whole time. A non-blocking mode was removed on purpose (`ask_question` in `src/kiro_crew/mcp_tools/control.py`, [rfc-question-card-pager.md §3.6](rfc-question-card-pager.md)). |
| Stateless question card (kiro-cli's native `AskUserQuestion`) | A non-blocking card that a user row retires | One per slot, in gateway memory only (`slot._question_pending`, `mark_pending` in `src/kiro_crew/dashboard/interaction_coordinator.py`), so a restart drops it. Its record blocks idle compaction (`idle_compaction_blocker` in `src/kiro_crew/dashboard/chat_utils_completion.py`) and counts as an open interaction in `pending_interaction` (`src/kiro_crew/dashboard/responsive_chat.py`), which the queue (`_finish_queue_cycle` in `src/kiro_crew/dashboard/chat_turn/queue_cycle.py`) and dashboard card sends (`has_open_decision` in `src/kiro_crew/dashboard/card_send.py`) read. Its answer quotes the agent's question in a user row. |
| Work-ledger ask (`work_ask_record` in `src/kiro_crew/mcp_work.py`; `reason=needs_human`, [rfc-work-ledger-person-wait-hold.md](rfc-work-ledger-person-wait-hold.md)) | A conductor records what an item needs from the person, why, the options and its pick. `awaits_person` in `src/kiro_crew/dashboard/handlers/work_ledger_board.py` shows it on every needs-you reader: the conductor mode Board's "Needs your input", the sidebar's crew count, the goal board and the welcome. It stays current until a report, a decide or a close. | Conductor work items only. A plain chat or a loop with no ledger has nothing like it. |

Near misses:

- **Automatic Dynamic Dashboard cards** have a "you" field (`src/kiro_crew/dashboard/card_lifecycle.py`). It is opt-in, written by a background model from recent rows, and shown in the side panel.
- **Per-session message pins** (`src/kiro_crew/dashboard/chat_pins.py`) are the person's own bookmarks, with no options, answer or retirement.
- **The pinned-prompt banner** (`pages.chat.pinnedPrompt`) keeps the person's own last prompt in view. It holds no agent text.
- **The sidebar needs-me card** (`website/src/pages/chat-sidebar/needsMeCard.ts`) lists a row's open approvals, questions, forms and the newest reply's options, and answers them from the sidebar. Its `reply` kind is built from the newest reply only, so it shares the `[OPTIONS:]` lifetime.
- **A legacy loop's declared wait** ([rfc-legacy-loop-reads-stops-waits.md §2.4](rfc-legacy-loop-reads-stops-waits.md), open in [#21622](https://github.com/kirodotdev/KiroCrew/pull/21622)) would let a loop's agent hold the loop while it waits on a person, for at most 24 hours. It holds the loop and says nothing about what is needed. A pinned ask holds nothing and says what is needed, so the two compose: a loop that cannot proceed may declare the wait and pin the ask, and the person's message releases one and retires the other.
- **The auto-nudge quiet judge** (`DEFAULT_QUIET_WHEN` in `src/kiro_crew/autonudge_judge.py`) skips a tick that has nothing new. After a streak of quiet ticks it fires anyway (`_JUDGE_QUIET_STREAK_FLOOR_DEFAULT` in `src/kiro_crew/autonudge_service/gate.py`), and those forced ticks are where a blocked loop restates its ask. The judge decides whether a tick wakes the agent. This RFC covers what the agent does on a tick that does wake it.

### Problem

1. **The ask scrolls away.** In a loop, the turn that explained the ask is buried under N status replies.
2. **The loop restates to stay visible.** Every restatement is a visible turn that tells the person nothing new.
3. **Non-loop chats lose it too.** A side question the person asks while an ask is open pushes the ask out of view.
4. **The sidebar cannot say "waiting on you" for a plain-text ask** ([#20961](https://github.com/kirodotdev/KiroCrew/issues/20961)). Unread clears on open, and only a question card sets the waiting marker.

## Benefits

What the person gets, as outcomes a Phase 1 test or a look at the screen can check. Nothing is built, so the figures below come from the constants cited, not from measurement. The code references in this section, Use cases and Downsides were read at `25c11702dd`.

- **The ask stays on screen without scrolling.** Today it is in the turn that first made it, and every later row pushes it up. With a card it sits above the composer however many rows come after it, until it is answered, cleared or expires. Check: pin an ask, append 50 rows, and the card is still in the composer band.
- **A blocked loop stops writing "still waiting" replies.** An auto-nudge loop whose ticks read quiet is still delivered a turn every 10 quiet ticks (`_MAX_QUIET_STREAK` and `_JUDGE_QUIET_STREAK_FLOOR_DEFAULT` in `src/kiro_crew/autonudge_service/gate.py`), and under the 2026-09-13 decision that turn restates its options to keep the chips on screen. At the 600 s interval the pull-request loop uses (`references/rationale.md` of the `kirocrew-prepare-pr` skill), a ten-hour overnight wait in which every tick reads quiet produces six such replies; at 300 s it produces twelve. With the ask pinned, each of those turns ends with `nothing_to_do`, whose quiet-end token is dropped so nobody sees a reply (`nothing_to_do` in `src/kiro_crew/mcp_tools/control_parts/directive.py`). The person comes back to the first explanation and the card, with no restatements under it. The forced wakes still run: this removes the visible replies, not the turns.
- **Other chats show that this one needs the person.** Today a plain chat whose agent asks in prose shows nothing on its sidebar row: `needs_input` comes only from a live card, and `loop_waiting` only from an armed loop whose newest reply ends in options (`website/src/pages/chat-sidebar/SessionRow.tsx`). With a pinned ask the row, the tab strip and the board view read "Action for you · <title>" from any other chat, and the sidebar needs-me card answers it without opening the chat.
- **A restart or a lazy restore keeps the ask.** The stateless question card lives in `slot._question_pending`, in gateway memory, so a restart drops it, and a lazily restored tab reads only a 100-row tail (`LAZY_TAIL_ROWS` in `src/kiro_crew/dashboard/chat_persistence_parts/restore_drivers.py`). The store survives both, so a days-old ask comes back with its status.
- **No turn is held open.** `ask_question` holds the turn for its fixed 30-minute window (`_AGENT_ASK_WINDOW_DEFAULT = 1800` in `src/kiro_crew/dashboard/state_parts/dashboard_state.py`) and then withdraws the card. A pinned ask holds nothing: a loop keeps polling whatever else it waits on, and the ask waits for the person for up to 7 days.
- **One click answers, in the person's own words.** An option sends exactly its label as the person's message. The agent learns which ask it answered from the per-turn block (section 6), so no user row quotes the agent's question.

## Use cases

Each flow walks one ask from declaration to retirement. The titles and options are examples.

### 1. An agent needs a decision while the person works in another chat

The person has two chats open. In chat A an agent is drafting release notes. The person is typing in chat B.

1. Chat A's agent reaches a choice it cannot make: whether to list two reverted pull requests. It calls `waiting_on_you` with key `reverted-prs`, title "List the two reverted PRs in the notes?" and options "List them" and "Leave them out", and ends its turn with a short reply on what it has done so far.
2. In chat B the person sees chat A's sidebar row change to "Action for you · List the two reverted PRs in the notes?", in the card's own colour. Nothing rings: pinning does not notify (Non-goals).
3. The person opens the needs-me card on chat A's row, reads the title, detail and both options, picks "Leave them out" and sends. Opening chat A instead shows the same card above the composer, with "Pinned 4 min ago".
4. Chat A receives one user message, "Leave them out", from the person. The ask is retired, and the card and the sidebar status go away in every open window.
5. The agent's next turn carries `The person's message below answers your ask "reverted-prs".` in its per-turn block, and it finishes the notes.

A Crew Mode worker cannot take step 1. Its call gets the answer `ask_question` gives a worker (`CREW_WORKER_ASK_ERROR` in `src/kiro_crew/dashboard/handlers/ask_question.py`), and it reports `question` to its conductor instead.

### 2. An overnight watch raises an ask and stays quiet until morning

Before leaving, the person armed a pull-request watch in a chat: an auto-nudge loop, or a cron job that wakes the same chat.

1. At 02:10 a cycle finds Security Scope Review blocking the merge until a writer posts an override. The agent pins `scope-override`, titled "Post the Security Scope override on #38289", with the ready override text in the detail and options "I posted it" and "Hold the override". It writes one reply that explains the block.
2. The cycles after that arrive as `[auto-nudge cycle N]` or `[Cron notification from "…"]` rows (`CRON_NOTIFY_PREFIX` in `src/kiro_crew/dashboard/state_parts/injected_turns.py`). They carry no `meta.human`, which only send paths a person reaches set (`HUMAN_TURN_META_KEY` in `src/kiro_crew/history_schema.py`), so none of them retires the ask. Each woken cycle sees the ask in its per-turn block, keeps polling CI and reviews, finds nothing new, and ends with `nothing_to_do`. The chat gains no visible reply overnight.
3. At 08:30 the person opens the dashboard. The chat's sidebar row reads "Action for you · Post the Security Scope override on #38289", and the card shows "Pinned 6 h ago" with the override text.
4. The person copies the text, posts it on the pull request, and clicks "I posted it" on the card.
5. That message retires the ask. The next turn names the answered ask, and the agent re-checks the lane and carries on watching.

If the chat is a conductor and the ask belongs to one of its work-ledger items, the agent records it with `work_ask_record` instead, and it shows as "Needs your input" (section 6). It never pins a card for it.

### 3. An ask is answered elsewhere or becomes moot, and the card retires

Four endings, each starting from a pinned ask:

- **The blocker resolves on its own.** In use case 2 a maintainer posts the override at 06:00. The next woken cycle sees the lane pass and calls `waiting_on_you_clear` with `scope-override`. An open window turns the card into its closed notice, and the sidebar status goes. At 08:30 the person sees no card, and one reply saying the lane passed.
- **The person writes something else.** With `reverted-prs` pinned, the person types "Also fix the typo in the heading". That is a live row from the owner with `meta.human`, so it retires every ask pinned when it was sent, and the card goes. The next turn's block lists `reverted-prs` once as retired by a message, with its full title, detail and options. The agent either reads the message as covering the ask or re-declares it, and the card comes back with a new `ask_id`.
- **The person dismisses it.** The person clicks ✕ on the card or in the needs-me card. The ask goes, and the next agent turn gets its record with reason `dismissed`.
- **Nobody acts.** The loop was stopped and the agent never runs again. Seven days after the ask's last change the sweep removes it, broadcasts the removal so an open window drops the card, and appends its one-line display row to the history.

## Goals

- An agent can declare an ask that stays pinned above the composer until it is answered, cleared or expires, with no restating.
- An ask can be an action ("post the scope override on #38289") or a decision with options.
- A session can hold several asks at once, each answered or cleared on its own.
- Asks survive a reload, a reconnect and a gateway restart, including for a tab restored lazily.
- A loop cycle that changes nothing can end silently while the ask stays on screen.
- The sidebar row, tab strip and chat board view show a distinct "Action for you" status, and the sidebar needs-me card can answer or dismiss the ask, as #20961 asks.

## Non-goals

- Slack, Discord, Webex and other channel surfaces. The tool tells a non-dashboard session to use `[OPTIONS:]`.
- Any backward scan for `[OPTIONS:]`. Chips stay newest-reply-only.
- Changing `ask_question`, native cards, or what sets `needs_input`.
- Holding or slowing an auto-nudge loop because an ask is pinned. A loop that also waits on something external, such as a maintainer's review, must keep polling it.
- Notifications. Pinning an ask does not ring the bell.

## Design

### 1. The tools

Two MCP tools on `kirocrew-core`.

`waiting_on_you` declares or replaces one ask:

```json
{
  "key": "scope-override",
  "title": "Post the Security Scope override on #38289",
  "detail": "Security Scope Review blocks merge until a writer posts the override. The text is ready: `/ai-review override scope cf69b736d8 false-positive: …`",
  "options": [
    {"label": "I posted it", "description": "I re-check the lane and continue"},
    {"label": "Hold the override", "description": "I keep the PR parked and stop asking"}
  ]
}
```

| Field | Rule |
|---|---|
| `key` | Required. `^[a-z0-9][a-z0-9-]{0,39}$`. The agent's handle for the ask. A call with a pinned key replaces that ask and keeps its place in the stack. |
| `title` | Required. What is needed, in one line. At most 120 characters. |
| `detail` | Optional. Why, and what the person needs to act. Markdown, at most 1,000 characters. |
| `options` | Optional, 0 to 6. Label at most 200 characters, description at most 500, no duplicate labels. A label is sent as the owner's message, so one a click would run as more than prose is refused at declaration: a leading `/` or `@`, a `$name` skill token, or a `[ Paste #` token, the shapes `dispatchIsCommandShaped` (`website/src/app-sdk/protocol/dispatchShape.ts`) keeps an `[OPTIONS:]` chip from sending. Zero options makes an action card. |

`waiting_on_you_clear` takes `key`, or `all: true`.

- Over-length text and malformed options are **refused**, not truncated, so the agent knows what the person sees. `validate_ask_user_question` truncates, and it skips a question with no options (`src/kiro_crew/validation_parts/ask_question_card.py`), so it is not reused as is.
- At most 4 asks per session. A fifth new key is refused (`too_many_asks`) with the current keys listed.
- Every declaration that changes anything gets a new server-minted `ask_id`, and its 7-day expiry restarts. A call identical to the pinned ask is a no-op: same `ask_id`, nothing written, nothing broadcast.
- Text is passed through `redact_credentials` and `redact_exfiltration_urls` before it is stored.
- **Who may call.** Both tools need an attested dashboard-born session, the gate `ask_question` uses (`ask_question` in `src/kiro_crew/mcp_tools/control.py`). A Crew Mode worker gets the "ask your conductor" answer `ask_question` gives it (`CREW_WORKER_ASK_ERROR` in `src/kiro_crew/dashboard/handlers/ask_question.py`).
- **Gateway routes.** The tools call `POST /api/agent-waiting/set` and `/clear`. Like `/api/agent-ask/*` ([rfc-question-card-pager.md §5](rfc-question-card-pager.md)), they require the MCP server's internal secret and an attested session key, and they address only the caller's own slot.

### 2. Storage: a gateway store, with a display-only row

Live asks live in a gateway-owned store, `waiting_on_you.json` under the data home, keyed by session key. It follows the change-card store (`CardStore` and the module docstring of `src/kiro_crew/dashboard/change_cards.py`):

- **Memory first, disk before success.** Each check-and-mutate (set, clear, answer, dismiss, expiry) runs under one lock. The record is written to disk, off the event loop and behind a file lock, before the tool or route reports success and before anything is broadcast. A write that fails changes nothing and reports the error.
- **Tamper-evident.** The file is agent-writable, so each record carries a keyed MAC under a vault subkey. The MAC covers the whole record: its session key, and every ask's key, `ask_id`, text and timestamps. A record the gateway did not write, one that was edited, or one moved under another session's key, is dropped on load with a warning. An oversized or foreign file (not a regular file, or not UTF-8) loads as empty, with a warning, and is not overwritten until the next successful write. A read or key error that may pass loads nothing: the store stays unloaded and refuses every change, as `change_cards.py` does, so no write replaces the records still on disk, and the next call reads again.
- **Bounded.** At most 4 live asks per session (section 1), 4 retired records per session (below), 2,000 sessions with any record, and a 4 MB file cap, in the manner of `MAX_STORED_CARDS` and `STORE_FILE_MAX`. Past the session cap the oldest session's records are dropped.
- **Restore reads the store directly**, so a tab restored lazily from its 100-row tail (`LAZY_TAIL_ROWS` in `src/kiro_crew/dashboard/chat_persistence_parts/restore_drivers.py`) gets its sidebar status and card back however long ago the ask was made.
- **Expiry.** An ask expires 7 days after its last change. A sweep that runs every 10 minutes removes it, persists that, and broadcasts the removal so an open window drops the card. The 7 days is fixed. It is not `monitoring.max_runtime_secs`, because asks are not only for loops.
- **Incognito and temporary sessions** (`is_incognito_transcript` in `src/kiro_crew/history_derivation.py`) keep their asks in memory only. A restart drops them.

**Retired records.** When an ask is retired by anything other than the agent itself (the person's message, a dismissal, expiry), the store keeps its full record, the reason and its `ask_id` in a short per-session list. The next agent turn consumes it once (section 6), and the list is cleared when that turn starts. A record unconsumed after 7 days is dropped.

**The display-only row.** Each set, clear, answer, dismissal and expiry also appends one row to the transcript: the existing `card` role (`ROLE_CARD` in `src/kiro_crew/dashboard/chat_cards.py`) with a new surface, `waiting`. `card` is in `DISPLAY_ONLY_ROLES` (`src/kiro_crew/history_projection_contract.py`), so no model-bound reader sees it. It renders as one line, "Pinned: Post the Security Scope override on #38289" or "Dismissed: …", so the history shows when an ask came and went. As the `chat_cards.py` module docstring says, nothing reads state back from the row.

**Session lifecycle.**

- **Fork and transfer:** the new session starts with no asks.
- **Rewind and regenerate:** the asks stay pinned. The agent still sees them every turn (section 6) and clears what no longer stands. Matching each ask to the turn that made it would mean reading authority back from rows. An owner's edit-and-resend is the owner writing, so it retires the asks pinned when it was sent, as a typed message does.
- **Session deleted:** its records are removed.

### 3. What retires an ask

| Event | Effect |
|---|---|
| The owner answers a card (section 4) | That ask is retired. Other asks stay. |
| A live row from the owner with `meta.human` (`HUMAN_TURN_META_KEY` in `src/kiro_crew/history_schema.py`) that is not a card answer: a typed message, an `[OPTIONS:]` chip, an edit-and-resend | Every ask pinned when it was sent is retired, except one whose answer is already on its way |
| A replayed row (`broadcast=False`: transcript rotation recovery, fork, transfer) | Nothing, as stateless cards are gated today (the `broadcast` check beside `_QUESTION_RETIRING_ROLES` in `src/kiro_crew/dashboard/state_parts/slot_transcript.py`) |
| A Shared Crew teammate's message or card answer, a teammate's edit of the owner's queued message, or a channel row stamped human with an unknown principal (`project_channel_turn_live` in `src/kiro_crew/dashboard/channel_slots.py`) | Nothing. Only the owner answers the owner's asks. |
| Any machine row: auto-nudge, cron inject, webhook, sub-agent delivery, a peer `session_send`, an app send, an Issue Radar wake, a task-runner summary | Nothing. A machine-written `user` row is not the person. |
| The agent calls `waiting_on_you_clear` | The named asks are removed |
| The agent re-declares a pinned key | That ask is replaced in place, with a new `ask_id` |
| The owner clicks ✕, on the card or in the needs-me card | That ask is removed |
| 7 days pass | The ask expires |

The `meta.human` rule follows `history_schema.py`'s own guidance: a reader that wants human activity requires the marker rather than excluding the machine callers it knows about. `_QUESTION_RETIRING_ROLES` keys on role and is not reused.

A typed message retires every pinned ask because the server cannot tell which ask it answers. Keeping asks across it would let a card outlive the moment it was answered. The cost is that an unrelated message hides an ask until the agent's next turn, which gets the retired records (section 6) and re-declares what still stands.

**Queued sends.** A message sent while the agent is busy waits in the queue, and its row is written when the queue drains, after any ask the agent pinned meanwhile. So an owner's queued message carries the ids of the asks pinned when it was sent (`waitingOnYouShown`, a reserved meta key), and its row retires only those. The drain can merge queued entries into one row (`_start_next_queued_turn` in `src/kiro_crew/dashboard/chat_turn/queue_drain.py`). Its meta merge keeps the last writer's value for every key except the send and delivery ids it collects, so `waitingOnYouShown` gets a merge of its own: a merged row retires the union of what each entry saw. An owner's edit of a queued message re-stamps it with the asks pinned at the edit and marks the row as the owner's, a teammate's entry included. Anyone else's edit stamps an empty set, whatever the entry's original author. An entry with no stamp (sent while the store was still loading, or restored from an older queue) retires the asks pinned when its row is written. A card answer is queued as its own entry that is never merged with another, so each answer reaches its own row and retires only its own ask.

**A draft in progress is kept.** As with stateless cards (`src/kiro_crew/docs/agent-questions.md`, "Rendering and answers"), a window where the owner is typing into an action card's text field keeps that card on screen after a retirement, marked "This ask was closed. Your note was not sent." The note stays on screen, unsent, until the owner discards it. It is never sent as an answer.

### 4. The card and its answer

**Placement.** Pinned in the composer band where `PendingQuestionCard` renders (`website/src/pages/chat/page/ComposerDockView.tsx`, `website/src/components/ChatPane.tsx`), in the main view and in grid panes. A blocking `ask_question` card or a native card sits above the stack and takes keyboard focus first.

**Look and label.** "Waiting on you" already means a live question or approval in the sidebar (`pages.chatSidebar.code_channel_crew_waiting` in `website/src/i18n/en.context.json`: "Same meaning as the other 'Waiting on you' states"). A standing ask must not look like one. So on every surface, card header, sidebar row, tab strip and chat board view, it is labelled **"Action for you"**, in its own colour, separate from the question and approval state. The card shows the agent's name, the title, the detail collapsed to two lines, the options, and "Pinned 3 h ago". The stack shows the newest card in full and "2 more" above it, which expands in place.

**Who sees it.** The owner sees and answers the cards. A Shared Crew collaborator who can see the session sees them read-only, with no buttons.

**Answering.** An option and then Send, or "Done" with an optional note on an action card, is the ordinary chat send (`POST /api/chat`), the same send the composer makes. Its text is exactly the chosen label, the typed note, or `Done` for an empty action answer. Its meta carries a claim, `waitingOnYouAnswer: {key, ask_id}`. A separate answer route would need its own reservation, retry and restart states for what the chat send already settles: whether the message was sent, queued or refused.

- **Owner-only.** Only the machine owner's claim counts. A claim from an app token or a Shared Crew teammate is dropped before it can change delivery, and the send goes through as that sender's ordinary message.
- **Checked before it is sent.** An answer to an ask no longer pinned, or to one that already has an answer queued or on its way from another window, is refused with 409 `waiting_on_you_stale`, and the card shows the refusal. Of two concurrent answers to one ask, only the first proceeds.
- **Checked again when its row is written.** A claim that still names a pinned ask stamps the reserved `meta.waitingOnYou: {key, ask_id}` on the row and retires that ask alone. The key is in `RESERVED_ROW_META_KEYS` (`src/kiro_crew/dashboard/chat_handlers.py`), so a caller cannot forge it. A claim whose ask the agent replaced, cleared or closed while the answer waited in the queue retires nothing. Its row is stamped as answering an ask that is gone, so the turn tells the agent the message answers its earlier version.
- **Never steered, never merged.** A card answer never steers into a running turn and never takes the responsive path. A busy agent queues it as its own entry, and the card says "Your answer is queued" with its controls off until the row is written. Cancelling the queued answer or stopping the agent leaves the ask pinned. Editing a queued answer's words turns it into an ordinary message.
- **Refused sends keep the card.** A refused send, such as a locked composer or a pause that lands while the send is being admitted (409 `crew_paused`), leaves the ask pinned and keeps any note, with the usual send error. Otherwise a paused crew queues the answer like any person's send.
- **Restart while queued.** A queue restored after a gateway restart drops each entry's person provenance by design (`sanitize_restored_queue` in `src/kiro_crew/dashboard/slot_queue_repository.py`). The claim itself survives the restore, as the rest of the entry's meta does, but a claim is honoured only on a row the owner wrote (`meta.human`), which a restored entry never is. A restored answer therefore reaches the agent as an ordinary message without its link to the ask, and the ask stays pinned. That fails in the safe direction: nothing is retired that the owner did not answer, and the owner or the agent can still close it.
- **What the session receives.** One user turn whose text is only what the owner chose or typed, with `meta.human`. The agent learns which ask was answered from the per-turn block (section 6), not from the text.
- **Not a card-origin turn.** The owner sees exactly the text that is sent, as with an `[OPTIONS:]` chip. So the turn carries the owner's ordinary authority and does not get `CARD_TURN_PREFIX` (`src/kiro_crew/dashboard/card_send.py`).

**A stale card.** An ask the agent replaces, clears or lets expire while its card is on screen turns into its closed notice in place, keeping any unsent note to discard (section 3). An answer refused as stale shows the refusal on the card, and nothing is sent.

**✕** calls `.../dismiss` with `{ask_id}`. It is owner-only and refuses app tokens. It removes the ask and adds a retired record with reason `dismissed`.

### 5. Slot payload and status readers

The slot payload gains `waiting_on_you: [{key, ask_id, title, options, ts}]`. `needs_input` is unchanged and stays false for a pinned ask.

| Reader | Change |
|---|---|
| Sidebar row, tab strip, chat board view, grid pane header (`website/src/components/SessionGridView.tsx`) | Show "Action for you · <newest title>" in its own colour. A live question or approval still wins over it. |
| Sidebar needs-me card (`website/src/pages/chat-sidebar/needsMeCard.ts`) | A new `waiting` kind beside `approval`, `question`, `form` and `reply`, with the same answer and ✕ as the card: the chat send and the dismiss route |
| Dynamic Dashboard Questions tab (`website/src/pages/chat/command-center/CommandCenterPanel.tsx`) | Lists the asks beside question cards and approvals |
| Sidebar loop row "Waiting on you" (`loop_waiting` in `website/src/pages/chat-sidebar/SessionRow.tsx`) and the board's Waiting lane (`inferLane` in `website/src/pages/chat/sessionLane.ts`) | Unchanged: both still read `has_options`, the newest reply's `[OPTIONS:]` ending. In the row's status order "Action for you" sits above `loop_waiting`, so a loop with a pinned ask and an options ending reads "Action for you". A pinned ask does not move a session into the Waiting lane. |
| Crew pause `_waits_on_person` (`src/kiro_crew/dashboard/handlers/crew_pause.py`), crew worker `STATUS_ASKING` (`src/kiro_crew/dashboard/handlers/crew_workers.py`), peer instances (`_PEER_SLOT_BOOL_FIELDS` in `src/kiro_crew/dashboard/handlers_instances_peer.py`), headless CLI (`asks_now` in `src/kiro_crew/cli_chat_headless.py`), the Ops Mission Control board (`_slot_state` in `src/kiro_crew/apps/builtins/ops_mission_control/backend/http_routes/board.py`), `website/src/apps/crew-companion`, `chat_done` | Unchanged in this RFC. They keep reading `needs_input`. |

### 6. What the agent sees

- **Tool result.** The tool names the pinned set: `Pinned "scope-override". Pinned asks: scope-override. It stays until the person answers or writes, you clear it, or 7 days pass.`
- **Per-turn block.** While any ask is pinned, or any retired record is unconsumed, each turn's context carries one block. It is gateway-written reference context, placed with the session context and outside the person's message, and it quotes the agent's own earlier text as "your earlier ask". It lists:
  - each pinned ask's key, title and age;
  - each retired record, once, with its reason and its full title, detail and options, so the agent can re-declare one that still stands even after compaction;
  - for a turn started by a card answer, which ask it answered: `The person's message below answers your ask "scope-override".`
- **Size.** The block is at most 6,000 characters. Pinned asks show key, title and age only. A retired record shows its title, its option labels without descriptions, and a detail cut to 500 characters. That is at most 120 + 6 × 200 + 500 = 1,820 characters a record before framing. With 4 pinned asks and 4 retired records, any record that still does not fit is listed by key and title only, with a marker.
- **Guidance** in `src/kiro_crew/config/prompt.md`, the `babysit` skill and the goal-conductor skill:
  - declare an ask once with `waiting_on_you`;
  - clear it when the blocker resolves on its own;
  - on a loop cycle where nothing changed, end with `nothing_to_do` instead of restating the ask;
  - a conductor keeps a ledger item's ask on `work_ask_record` and never pins a card for it. The two records have different lifetimes (the ledger ask lasts until a report, decide or close; a card retires on any owner message or after 7 days), so one ask on both would let "Action for you" and "Needs your input" disagree. Every needs-you reader changes together ([rfc-delayed-task-brief.md](rfc-delayed-task-brief.md)), so a ledger ask stays on the ledger.

## Relation to the 2026-09-13 decision

The decision rejected [#10173](https://github.com/kirodotdev/KiroCrew/pull/10173)'s card because it **re-surfaced a stale options turn** found by scanning back. Users could not tell it from a live question. The revert, [#10615](https://github.com/kirodotdev/KiroCrew/pull/10615), removed that card, a sidebar status and a Waiting lane.

This RFC brings back a card above the composer and a sidebar status. It differs on each point the decision names:

- **Declared, not inferred.** Nothing scans the transcript. An ask exists only because the agent called a tool for it. It ends when the owner answers or writes, the agent clears it, or it expires.
- **Told apart from a live question.** The card and every status say "Action for you" in their own colour, never "Waiting on you". A live question card keeps its own shape, label and colour, and sits above the stack.
- **Kept honest.** The agent sees its pinned asks every turn, and the 7-day expiry bounds an ask it forgets.

The decision's remedy line, "A loop that is still blocked on the user re-states its options in its newest turn", is what produces the per-cycle noise. This RFC asks the decision's maintainer for a ruling, recorded as a new decision entry, that a blocked loop may instead pin an ask and stay quiet. `[OPTIONS:]` chips themselves are untouched.

The same ruling covers one swap of status readers. A loop that pins its ask and ends its cycles with `nothing_to_do` has no options ending, so `has_options` is false. Its sidebar row then reads "Action for you" in place of "Waiting on you", and the session leaves the board's Waiting lane (section 5). The amendment names this swap, so the maintainer rules on it too.

## Migration plan

Two PRs after this document lands, each independently revertible.

**Phase 1: tools, store, card, per-turn block.** The tools and their gateway routes, the store, the display-only row, answers through the chat send, the dismiss route, the slot payload field, the pinned stack, the status on each surface in section 5, the needs-me `waiting` kind, and the per-turn block. Phase 1 includes the block because retirement ships here, and without it the agent cannot learn that an ask was retired. Every new string gets a key in `website/src/i18n/locales/en.json`, in every shipped locale catalog under `website/src/i18n/locales/`, and a context entry in `website/src/i18n/en.context.json`, passing the catalog parity and placeholder tests.

Exit criteria, each pinned by a test:

- **Survival.** An ask survives a gateway restart, and a tab restored lazily shows it and its status.
- **Integrity.** A store record edited on disk is dropped on load. An oversized file loads as empty and is not overwritten by the load. A first load whose read or key fails refuses writes and leaves the file as it was. A failed write reports an error and broadcasts nothing.
- **Retirement.** A live owner row with `meta.human` retires every ask pinned when it was sent. Each of these retires none: a `broadcast=False` re-append of such a row, an auto-nudge row, a cron inject, a peer `session_send` relay, an app send, an Issue Radar wake, and a Shared Crew teammate's message.
- **Answers.** Answering one card of two retires only that one.
- **Text sent.** An answer's user row text is exactly the chosen label, the typed text, or `Done`, with `meta.waitingOnYou`. The key, title and detail appear in no user row.
- **Stale and wrong answers.**
  - An answer to an ask no longer pinned, or with an old `ask_id` after a re-declare, gets 409 `waiting_on_you_stale` and sends nothing.
  - A second answer to an ask whose answer is queued or in flight, from another window or during admission, is refused; two concurrent answers deliver one.
  - A claim from a teammate or an app retires nothing.
  - A queued answer whose ask was replaced or cleared retires nothing, and its turn names the earlier version.
- **Paused crew.** An answer sent while Pause all holds the slot is queued and retires the ask when its row is written.
- **Refused send.** A pause that lands during admission (409 `crew_paused`) leaves the ask pinned.
- **Queue.** A queued answer and a queued typed message produce separate rows: the answer retires its ask, and the typed message then retires the rest. A queued typed message leaves an ask pinned after it was sent, and a teammate's edit of the owner's queued message retires none.
- **Limits.** A fifth key is refused with the current keys listed. Over-length text is refused. An identical re-declare writes no row and broadcasts nothing.
- **No busy side effects.** A pinned ask leaves `needs_input` false, and `idle_compaction_blocker` and `pending_interaction` do not report the slot busy.
- **Lifecycle.** A fork starts with no asks. Rewind and regenerate keep them. An ask expires 7 days after its last change, and the open window drops it.
- **Incognito.** An incognito session's ask is not written to disk.
- **Per-turn block.** It appears exactly when an ask is pinned or a retired record is unconsumed, names a dismissed or retired ask once with its full record, and stays under 6,000 characters with 4 maximum-length asks.
- **Refusals.** A non-dashboard session, an unattested caller, a call without the internal secret, and an app token or an admitted teammate on the dismiss route are each refused.

**Phase 2: agent guidance.** The guidance bullets in section 6. Exit criteria: skill and prompt tests assert the `babysit`, goal-conductor and `prompt.md` text.

## Backward compatibility

- The display-only row reuses the `card` role. An older build without the `waiting` surface sees an unknown card surface; Phase 1 checks that it is skipped, not an error.
- An older build ignores `waiting_on_you.json` and the `waiting_on_you` slot field.
- Agents that never call the tools see no change.

## Security considerations

- **Agent text never enters a user row.** [§3.6](rfc-question-card-pager.md) says agent-written text must not arrive with the person's authority. Here the title, detail and key are shown or kept in meta, never sent as text. The user row holds only what the owner chose or typed. A chosen label is agent-written text that the owner picked and saw, which is the authority an `[OPTIONS:]` chip label has today, and a label shaped like a command is refused when the ask is declared (section 1).
- **The agent's own text comes back as reference.** The per-turn block quotes the agent's earlier ask as gateway context, outside the person's message, never as something the person said.
- **Retirement needs the owner.** Only the owner's live `meta.human` rows, answers and dismissals retire asks. Another session's `session_send`, an app, a teammate or a channel member cannot clear them.
- **Answers are checked.** An answer must name an ask still pinned, by its current `ask_id`, and only the owner's claim counts. Its text is the owner's own send, so it carries no more authority than a message the owner typed.
- **Own slot only.** The tool routes need the internal secret and an attested session key, and they address only the caller's slot.
- **Tamper-evident store.** Records are MAC'd as change cards are. Nothing is read back from transcript rows.
- **Rendering.** Title and detail render through the chat's markdown renderer, after redaction, as assistant text does.
- **No new authority over the loop.** Pinning holds nothing, stops nothing and wakes nothing.

## Downsides

What the person and the codebase pay for this design, and how the design bounds each cost.

- **Screen space above the composer.** Up to four cards stack where `PendingQuestionCard` renders (`website/src/pages/chat/page/ComposerDockView.tsx`), which shortens the visible transcript, most of all in a small window or a grid pane. *Bound:* the stack shows only the newest card in full, with its detail collapsed to two lines and "N more" above it; a session holds at most 4 asks; a blocking question card sits above the stack and takes focus first.
- **Stale cards.** A card can outlive its ask when the blocker resolved and the agent has not run since, or has stopped. *Bound:* the agent sees every pinned ask on every turn (section 6) and its guidance says to clear one that resolved; any message the owner types retires them all; ✕ removes one; 7 days after its last change an ask expires. What remains: an agent that is not woken again leaves its card up until the person acts or the 7 days pass.
- **Over-retirement.** A typed message retires every pinned ask, an unrelated one included, because the server cannot tell which ask it answers (section 3). The card is gone until the agent's next turn re-declares it, which costs that turn one more tool call. *Bound:* the retired record reaches that turn once with its full text, so nothing is lost, and only the owner's own live rows retire anything.
- **One more status to tell apart.** "Action for you" joins "Waiting on you" and "Needs your input", and a loop that pins its ask and ends its cycles quietly leaves the board's Waiting lane, because `inferLane` (`website/src/pages/chat/sessionLane.ts`) reads `has_options`. *Bound:* the new status has its own label and colour on every surface, a live question or approval outranks it (section 5), and the lane swap is part of the ruling this RFC asks for.
- **More gateway state.** A new store file with a lock, a MAC subkey, a 10-minute expiry sweep, three routes, a slot field and up to 4 retired records per session, all of which need tests and upkeep. *Bound:* the store copies the change-card store's shape (`CardStore` in `src/kiro_crew/dashboard/change_cards.py`) rather than inventing one, and it is capped at 2,000 sessions and 4 MB (section 2).
- **If the store is lost or refused.** If the file is deleted, a record fails its MAC, or the key cannot be read, the asks it held are gone, or the store refuses every change until a read succeeds. The person sees the cards and the "Action for you" status disappear, and an agent that calls the tool gets an error. The agent's per-turn block then lists no pinned ask and no retired record, so nothing tells it the ask went away: it can only notice that its ask is missing from the block on its next woken turn and re-declare it. *Bound:* nothing else reads the store (`needs_input`, idle compaction and the queue stay as they are, section 5), so a lost store fails toward "no card", never toward a wrong answer, because an answer is checked against the store and refused as stale; the display-only rows keep the history; and the MAC keeps a forged ask from ever appearing.
- **Context on every turn.** While any ask is pinned or any retired record is unconsumed, every turn carries the per-turn block, up to 6,000 characters. *Bound:* a pinned ask costs only its key, title and age; the block is absent when nothing is pinned or retired; retired records are consumed once.
- **Agents may pin what is not an ask.** An agent could pin status or an FYI as an "action". *Bound:* pinning rings no bell, a session holds at most 4, the guidance declares an ask only for what needs the person, and the owner can ✕ any card.
- **Two places an ask can live.** A conductor's ledger ask stays on `work_ask_record` while a plain chat's ask is a card, so a reader of the code meets two mechanisms. *Bound:* the guidance forbids a card for a ledger ask (section 6), so one ask never has two records with different lifetimes.
- **Coverage gaps.** Channel sessions (Slack, Discord, Webex) get nothing and are told to use `[OPTIONS:]`, and an incognito or temporary session's asks are lost on a restart (section 2).
- **Translation work.** Every new string needs a key in every catalog under `website/src/i18n/locales/` and a context entry in `website/src/i18n/en.context.json`, held by the catalog parity and placeholder tests (Migration plan).

## Alternatives considered

- **Restore the backward `[OPTIONS:]` scan** ([#10173](https://github.com/kirodotdev/KiroCrew/pull/10173)). Rejected by the 2026-09-13 decision: it would re-surface stale asks.
- **Pin a card for a conductor's ledger ask.** `work_ask_record` already records it and every needs-you reader shows it as "Needs your input". A second record with a different lifetime could disagree with the first, so ledger asks stay on the ledger (section 6).
- **A non-blocking `ask_question`.** Rejected in [§3.6](rfc-question-card-pager.md) because the answer quoted the agent's question in a user row. This design sends only what the owner chose or typed.
- **Blocking `ask_question` from the loop.** It holds the loop's turn for up to 30 minutes, then withdraws the card, and the loop cannot poll anything else meanwhile.
- **An end-of-turn `[WAITING: …]` tag**, as #20961 proposes. It has the lifetime of the newest reply, as `[OPTIONS:]` does, so a loop still restates every cycle.
- **Reuse the stateless card record** (`_question_pending`). It is one per slot, held in memory only, and makes the slot busy. Its answer also quotes the question.
- **Fold live state from transcript rows.** Lazy restore keeps a 100-row tail and only six meta scalars (`_display_tail` in `src/kiro_crew/dashboard/chat_persistence_parts/restore_drivers.py`), so a days-old ask falls out of it. The transcript is also agent-writable.
- **Answer as a hidden `[User action]` turn**, as [rfc-bot-hears-user-card-answers.md](rfc-bot-hears-user-card-answers.md) does for Bot threads. That queue (`mate_thread_wakes`) serves Bot threads only. A visible user turn also shows the owner their own answer in the chat.
- **Keep asks across unrelated messages.** The server cannot tell which ask a message answers, so a card would outlive its answer.
- **Let the agent pin its own message** with `chat_pins.py`. Those are the person's bookmarks, with no options, answer or retirement.
- **Promote the Dynamic Dashboard "you" field.** It is model-summarised, opt-in, and shown only in the side panel.

## Open questions

1. **Names.** `waiting_on_you` / `waiting_on_you_clear` for the tools and "Action for you" for the label. `pin_ask` would collide with session pinning (`chat_session_pin`). The tool name keeps the issue's wording; the label differs on purpose, as explained in the decision section.
2. **At most 4 asks per session.** This is a guess at what fits the composer band without crowding it. Open to change.
3. **Expiry of 7 days.** A shorter bound would catch forgotten asks sooner, but would end real long waits.

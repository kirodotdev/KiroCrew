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
| `[OPTIONS: …]` chips | Buttons under the composer, read from the newest assistant reply only | The next reply without the tag removes them, so the 2026-09-13 decision tells a blocked loop to restate them every cycle. That restating is the noise this RFC removes. |
| `ask_question` card | A blocking card; the answer is the tool result | It lives for a fixed 30-minute window (`_AGENT_ASK_WINDOW_DEFAULT`, used by `api_agent_ask_open` in `src/kiro_crew/dashboard/handlers/ask_question.py`) and holds the turn open the whole time. A non-blocking mode was removed on purpose (`ask_question` in `src/kiro_crew/mcp_tools/control.py`, [rfc-question-card-pager.md §3.6](rfc-question-card-pager.md)). |
| Stateless question card (kiro-cli's native `AskUserQuestion`) | A non-blocking card that a user row retires | One per slot, in gateway memory only (`slot._question_pending`, `mark_pending` in `src/kiro_crew/dashboard/interaction_coordinator.py`), so a restart drops it. Its record blocks idle compaction (`idle_compaction_blocker` in `src/kiro_crew/dashboard/chat_utils_completion.py`) and counts as an open interaction in `pending_interaction` (`src/kiro_crew/dashboard/responsive_chat.py`), which the queue (`_finish_queue_cycle` in `src/kiro_crew/dashboard/chat_turn/queue_cycle.py`) and dashboard card sends (`has_open_decision` in `src/kiro_crew/dashboard/card_send.py`) read. Its answer quotes the agent's question in a user row. |
| Work-ledger `reason=needs_human` ([rfc-work-ledger-person-wait-hold.md](rfc-work-ledger-person-wait-hold.md)) | Holds a conductor's patrol; the goal popover shows "Waiting on you" | Conductor work items only, and it says nothing about *what* is needed. |

Near misses:

- **Automatic Dynamic Dashboard cards** have a "you" field (`src/kiro_crew/dashboard/card_lifecycle.py`). It is opt-in, written by a background model from recent rows, and shown in the side panel.
- **Per-session message pins** (`src/kiro_crew/dashboard/chat_pins.py`) are the person's own bookmarks, with no options, answer or retirement.
- **The pinned-prompt banner** (`pages.chat.pinnedPrompt`) keeps the person's own last prompt in view. It holds no agent text.
- **The sidebar needs-me card** (`website/src/pages/chat-sidebar/needsMeCard.ts`) lists a row's open approvals, questions, forms and the newest reply's options, and answers them from the sidebar. Its `reply` kind is built from the newest reply only, so it shares the `[OPTIONS:]` lifetime.
- **The auto-nudge quiet judge** (`DEFAULT_QUIET_WHEN` in `src/kiro_crew/autonudge_judge.py`) skips a tick that has nothing new. After a streak of quiet ticks it fires anyway (`_JUDGE_QUIET_STREAK_FLOOR_DEFAULT` in `src/kiro_crew/autonudge_service/gate.py`), and those forced ticks are where a blocked loop restates its ask. The judge decides whether a tick wakes the agent. This RFC covers what the agent does on a tick that does wake it.

### Problem

1. **The ask scrolls away.** In a loop, the turn that explained the ask is buried under N status replies.
2. **The loop restates to stay visible.** Every restatement is a visible turn that tells the person nothing new.
3. **Non-loop chats lose it too.** A side question the person asks while an ask is open pushes the ask out of view.
4. **The sidebar cannot say "waiting on you" for a plain-text ask** ([#20961](https://github.com/kirodotdev/KiroCrew/issues/20961)). Unread clears on open, and only a question card sets the waiting marker.

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
| `options` | Optional, 0 to 6. Label at most 200 characters, description at most 500, no duplicate labels. Zero options makes an action card. |

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
- **Tamper-evident.** The file is agent-writable, so each record carries a keyed MAC under a vault subkey. A record the gateway did not write, or one that was edited, is dropped on load with a warning. An unreadable or oversized file loads as empty, with a warning, and is not overwritten until the next successful write.
- **Bounded.** At most 4 live asks per session (section 1), 4 retired records per session (below), 2,000 sessions with any record, and a 4 MB file cap, in the manner of `MAX_STORED_CARDS` and `STORE_FILE_MAX`. Past the session cap the oldest session's records are dropped.
- **Restore reads the store directly**, so a tab restored lazily from its 100-row tail (`LAZY_TAIL_ROWS` in `src/kiro_crew/dashboard/chat_persistence_parts/restore_drivers.py`) gets its sidebar status and card back however long ago the ask was made.
- **Expiry.** An ask expires 7 days after its last change. A sweep that runs every 10 minutes removes it, persists that, and broadcasts the removal so an open window drops the card. The 7 days is fixed. It is not `monitoring.max_runtime_secs`, because asks are not only for loops.
- **Incognito and temporary sessions** (`is_incognito_transcript` in `src/kiro_crew/history_derivation.py`) keep their asks in memory only. A restart drops them.

**Retired records.** When an ask is retired by anything other than the agent itself (the person's message, a dismissal, expiry), the store keeps its full record, the reason and its `ask_id` in a short per-session list. The next agent turn consumes it once (section 6), and the list is cleared when that turn starts. A record unconsumed after 7 days is dropped.

**The display-only row.** Each set, clear, answer, dismissal and expiry also appends one row to the transcript: the existing `card` role (`ROLE_CARD` in `src/kiro_crew/dashboard/chat_cards.py`) with a new surface, `waiting`. `card` is in `DISPLAY_ONLY_ROLES` (`src/kiro_crew/history_projection_contract.py`), so no model-bound reader sees it. It renders as one line, "Pinned: Post the Security Scope override on #38289" or "Dismissed: …", so the history shows when an ask came and went. As the `chat_cards.py` module docstring says, nothing reads state back from the row.

**Session lifecycle.**

- **Fork and transfer:** the new session starts with no asks.
- **Rewind and regenerate:** every ask in the slot is cleared, and the agent re-declares from where the conversation now stands. Matching each ask to the turn that made it would mean reading authority back from rows.
- **Session deleted:** its records are removed.

### 3. What retires an ask

| Event | Effect |
|---|---|
| The owner answers a card (section 4) | That ask is retired. Other asks stay. |
| A live row from the owner with `meta.human` (`HUMAN_TURN_META_KEY` in `src/kiro_crew/history_schema.py`) that is not a card answer: a typed message, an `[OPTIONS:]` chip | Every ask is retired |
| A replayed row (`broadcast=False`: transcript rotation recovery, fork, transfer) | Nothing, as stateless cards are gated today (the `broadcast` check beside `_QUESTION_RETIRING_ROLES` in `src/kiro_crew/dashboard/state_parts/slot_transcript.py`) |
| A Shared Crew teammate's message, or a channel row stamped human with an unknown principal (`project_channel_turn_live` in `src/kiro_crew/dashboard/channel_slots.py`) | Nothing. Only the owner answers the owner's asks. |
| Any machine row: auto-nudge, cron inject, webhook, sub-agent delivery, a peer `session_send`, an app send, an Issue Radar wake, a task-runner summary | Nothing. A machine-written `user` row is not the person. |
| The agent calls `waiting_on_you_clear` | The named asks are removed |
| The agent re-declares a pinned key | That ask is replaced in place, with a new `ask_id` |
| The owner clicks ✕, on the card or in the needs-me card | That ask is removed |
| 7 days pass | The ask expires |

The `meta.human` rule follows `history_schema.py`'s own guidance: a reader that wants human activity requires the marker rather than excluding the machine callers it knows about. `_QUESTION_RETIRING_ROLES` keys on role and is not reused.

A typed message retires every ask because the server cannot tell which ask it answers. Keeping asks across it would let a card outlive the moment it was answered. The cost is that an unrelated message hides an ask until the agent's next turn, which gets the retired records (section 6) and re-declares what still stands.

**Queued sends.** The queue drain merges queued entries into one row and unions their meta (`_start_next_queued_turn` in `src/kiro_crew/dashboard/chat_turn/queue_drain.py`). A card answer is queued as its own entry that is never merged with another, so each answer reaches its own row and retires only its own ask. If Phase 1 finds that cannot be done, the fallback is that a merged row counts as a typed message and retires every ask, whether it merged an answer with a typed message or two answers.

**A draft in progress is kept.** As with stateless cards (`src/kiro_crew/docs/agent-questions.md`, "Rendering and answers"), a window where the owner is typing into an action card's text field keeps that card on screen after a retirement, marked "This ask was closed". The draft can be copied into the composer or discarded. It is not sent as an answer.

### 4. The card and its answer

**Placement.** Pinned in the composer band where `PendingQuestionCard` renders (`website/src/pages/chat/page/ComposerDockView.tsx`, `website/src/components/ChatPane.tsx`), in the main view and in grid panes. A blocking `ask_question` card or a native card sits above the stack and takes keyboard focus first.

**Look and label.** "Waiting on you" already means a live question or approval in the sidebar (`pages.chatSidebar.code_channel_crew_waiting` in `website/src/i18n/en.context.json`: "Same meaning as the other 'Waiting on you' states"). A standing ask must not look like one. So on every surface, card header, sidebar row, tab strip and chat board view, it is labelled **"Action for you"**, in its own colour, separate from the question and approval state. The card shows the agent's name, the title, the detail collapsed to two lines, the options, and "Pinned 3 h ago". The stack shows the newest card in full and "2 more" above it, which expands in place.

**Who sees it.** The owner sees and answers the cards. A Shared Crew collaborator who can see the session sees them read-only, with no buttons.

**Answering.** An option button, or "Done" with an optional text field on an action card, calls `POST /api/chat/slots/{slot}/waiting-on-you/{key}/answer` with `{ask_id, answer_id, label}` for a decision card or `{ask_id, answer_id, text}` for an action card. `answer_id` is chosen by the client, so a retry is recognised.

- **Owner-only.** The route refuses app tokens, as the `/api/ask-question/*` routes do (`_deny_app_token`).
- **Checked against the server's copy.** The `key` must be pinned with the same `ask_id`, or the route answers 409 `superseded`. A decision card's `label` must exactly match one of the stored options, and it may not carry `text`; otherwise 400. An action card's `text` may be empty, may not come with `label`, and is bounded and sanitised like composer text.
- **Retire only on acceptance.** The route admits the turn through the normal chat send, now or queued, and retires the ask only once the send is accepted. A paused crew does not refuse it: `_api_chat_send` (`src/kiro_crew/dashboard/chat_api/chat_send.py`) queues a person's send while Pause all holds the slot, so the answer is accepted as queued and the ask is retired. A refused send, such as a pause that lands while the send is being admitted (the 409 `crew_paused` after the admission awaits), leaves the ask pinned and returns the refusal, so neither the card nor the answer is lost.
- **Idempotent.** Under the store lock, the route first persists a reservation, `answer_id` → `pending`, on the ask, so a concurrent request with any `answer_id` gets 409 `answered` instead of a second send. Once the send is accepted, the reservation becomes a terminal tombstone holding the outcome. On refusal it is removed. Tombstones are kept in their own per-session list, at most 16 and for 24 hours, apart from the retired records that a turn consumes. A retry with the same `answer_id` then gets the first result back and sends nothing, however soon the next turn starts.
- **Restart during an answer.** A gateway that stops after persisting `pending` but before persisting the outcome cannot know whether the turn was sent, and it does not read that back from the transcript. On load, every `pending` reservation becomes `uncertain`. The ask stays pinned and the card reads "Your answer may already have been sent. Check the chat above." A retry with that `answer_id` gets 409 `uncertain` and sends nothing. The person either answers again with a fresh click, which takes a new `answer_id`, or dismisses the card. The worst case is one duplicate answer that the person chose to send, never a lost one or a silent resend.
- **What the session receives.** One user turn whose text is only what the owner chose or typed: the label, the typed text, or `Done` for an empty action answer. It carries `meta.human` and a new reserved meta key, `waitingOnYou: {key, ask_id, answer_id}`. That key is added to `RESERVED_ROW_META_KEYS` (`src/kiro_crew/dashboard/chat_handlers.py`), so a caller cannot forge it. The agent learns which ask was answered from the per-turn block (section 6), not from the text.
- **Not a card-origin turn.** The owner sees exactly the text that is sent, as with an `[OPTIONS:]` chip. So the turn carries the owner's ordinary authority and does not get `CARD_TURN_PREFIX` (`src/kiro_crew/dashboard/card_send.py`).

**A stale card.** A 409 tells the window why:

- `answered` (another window or the needs-me card answered first): the card shows "Answered elsewhere" and the label is dropped.
- `superseded` or `cleared` (the agent replaced or withdrew it): the card shows that, and the label is put in the composer, as the `ask_question` 404 fallback does (`onFallbackSend`). Nothing is sent unless the owner sends it.

**✕** calls `.../dismiss` with `{ask_id}`. It is owner-only and refuses app tokens. It removes the ask and adds a retired record with reason `dismissed`.

### 5. Slot payload and status readers

The slot payload gains `waiting_on_you: [{key, ask_id, title, options, ts}]`. `needs_input` is unchanged and stays false for a pinned ask.

| Reader | Change |
|---|---|
| Sidebar row, tab strip, chat board view, grid pane header (`website/src/components/SessionGridView.tsx`) | Show "Action for you · <newest title>" in its own colour. A live question or approval still wins over it. |
| Sidebar needs-me card (`website/src/pages/chat-sidebar/needsMeCard.ts`) | A new `waiting` kind beside `approval`, `question`, `form` and `reply`, with the same answer and ✕ as the card, through the same routes |
| Dynamic Dashboard Questions tab (`website/src/pages/chat/command-center/CommandCenterPanel.tsx`) | Lists the asks beside question cards and approvals |
| Crew pause `_waits_on_person` (`src/kiro_crew/dashboard/handlers/crew_pause.py`), crew worker `STATUS_ASKING` (`src/kiro_crew/dashboard/handlers/crew_workers.py`), peer instances (`_PEER_SLOT_BOOL_FIELDS` in `src/kiro_crew/dashboard/handlers_instances_peer.py`), headless CLI (`asks_now` in `src/kiro_crew/cli_chat_headless.py`), the Ops Mission Control board (`_slot_state` in `src/kiro_crew/apps/builtins/ops_mission_control/backend/http_routes/board.py`), `website/src/apps/crew-companion`, `chat_done` | Unchanged in this RFC. They keep reading `needs_input`. |

### 6. What the agent sees

- **Tool result.** The tool names the pinned set: `Pinned "scope-override". Pinned asks: scope-override. It stays until the person answers or writes, you clear it, or 7 days pass.`
- **Per-turn block.** While any ask is pinned, or any retired record is unconsumed, each turn's context carries one block. It is gateway-written reference context, placed with the session context and outside the person's message, and it quotes the agent's own earlier text as "your earlier ask". It lists:
  - each pinned ask's key, title and age;
  - each retired record, once, with its reason and its full title, detail and options, so the agent can re-declare one that still stands even after compaction;
  - for a turn started by a card answer, which ask it answered: `The person's message below answers your ask "scope-override".`
- **Size.** The block is at most 6,000 characters. Pinned asks show key, title and age only. A retired record shows its title, its option labels without descriptions, and a detail cut to 500 characters. That is at most 120 + 6 × 200 + 500 = 1,820 characters a record before framing. With 4 pinned asks and 4 retired records, any record that still does not fit is listed by key and title only, with a marker.
- **Guidance** in `config/prompt.md`, the `babysit` skill and the goal-conductor skill:
  - declare an ask once with `waiting_on_you`;
  - clear it when the blocker resolves on its own;
  - on a loop cycle where nothing changed, end with `nothing_to_do` instead of restating the ask;
  - a conductor whose ledger item is `needs_human` pins one ask for it.

## Relation to the 2026-09-13 decision

The decision rejected [#10173](https://github.com/kirodotdev/KiroCrew/pull/10173)'s card because it **re-surfaced a stale options turn** found by scanning back. Users could not tell it from a live question. The revert, [#10615](https://github.com/kirodotdev/KiroCrew/pull/10615), removed that card, a sidebar status and a Waiting lane.

This RFC brings back a card above the composer and a sidebar status. It differs on each point the decision names:

- **Declared, not inferred.** Nothing scans the transcript. An ask exists only because the agent called a tool for it. It ends when the owner answers or writes, the agent clears it, or it expires.
- **Told apart from a live question.** The card and every status say "Action for you" in their own colour, never "Waiting on you". A live question card keeps its own shape, label and colour, and sits above the stack.
- **Kept honest.** The agent sees its pinned asks every turn, and the 7-day expiry bounds an ask it forgets.

The decision's remedy line, "A loop that is still blocked on the user re-states its options in its newest turn", is what produces the per-cycle noise. This RFC asks the decision's maintainer for a ruling, recorded as a new decision entry, that a blocked loop may instead pin an ask and stay quiet. `[OPTIONS:]` chips themselves are untouched.

## Migration plan

Two PRs after this document lands, each independently revertible.

**Phase 1: tools, store, card, per-turn block.** The tools and their gateway routes, the store, the display-only row, the answer and dismiss routes, the slot payload field, the pinned stack, the status on each surface in section 5, the needs-me `waiting` kind, and the per-turn block. Phase 1 includes the block because retirement ships here, and without it the agent cannot learn that an ask was retired. Every new string gets a key in `website/src/i18n/locales/en.json`, in every shipped locale catalog under `website/src/i18n/locales/`, and a context entry in `website/src/i18n/en.context.json`, passing the catalog parity and placeholder tests.

Exit criteria, each pinned by a test:

- **Survival.** An ask survives a gateway restart, and a tab restored lazily shows it and its status.
- **Integrity.** A store record edited on disk is dropped on load. An oversized file loads as empty and is not overwritten by the load. A failed write reports an error and broadcasts nothing.
- **Retirement.** A live owner row with `meta.human` retires every ask. Each of these retires none: a `broadcast=False` re-append of such a row, an auto-nudge row, a cron inject, a peer `session_send` relay, an app send, an Issue Radar wake, and a Shared Crew teammate's message.
- **Answers.** Answering one card of two retires only that one.
- **Text sent.** An answer's user row text is exactly the chosen label, the typed text, or `Done`, with `meta.waitingOnYou`. The key, title and detail appear in no user row.
- **Stale and wrong answers.**
  - An answer with an old `ask_id` after a re-declare gets 409 `superseded`.
  - A label not among the stored options gets 400.
  - A second window gets 409 `answered` and drops the label.
  - A retry with the same `answer_id` sends one turn, also after the next turn has started.
  - Two concurrent answers to one ask send one turn; the other gets 409 `answered`.
  - A restart after `pending` was persisted, both before and after the send was accepted, leaves the ask pinned as `uncertain`, and a retry with that `answer_id` sends nothing.
- **Paused crew.** An answer sent while Pause all holds the slot is queued and retires the ask.
- **Refused send.** A pause that lands during admission (409 `crew_paused`) leaves the ask pinned.
- **Queue.** A queued answer and a queued typed message produce separate rows: the answer retires its ask, and the typed message then retires the rest.
- **Limits.** A fifth key is refused with the current keys listed. Over-length text is refused. An identical re-declare writes no row and broadcasts nothing.
- **No busy side effects.** A pinned ask leaves `needs_input` false, and `idle_compaction_blocker` and `pending_interaction` do not report the slot busy.
- **Lifecycle.** A fork starts with no asks. A rewind clears them. An ask expires 7 days after its last change, and the open window drops it.
- **Incognito.** An incognito session's ask is not written to disk.
- **Per-turn block.** It appears exactly when an ask is pinned or a retired record is unconsumed, names a dismissed or retired ask once with its full record, and stays under 6,000 characters with 4 maximum-length asks.
- **Refusals.** A non-dashboard session, an unattested caller, a call without the internal secret, and an app token on the answer and dismiss routes are each refused.

**Phase 2: agent guidance.** The guidance bullets in section 6. Exit criteria: skill and prompt tests assert the `babysit`, goal-conductor and `prompt.md` text.

## Backward compatibility

- The display-only row reuses the `card` role. An older build without the `waiting` surface sees an unknown card surface; Phase 1 checks that it is skipped, not an error.
- An older build ignores `waiting_on_you.json` and the `waiting_on_you` slot field.
- Agents that never call the tools see no change.

## Security considerations

- **Agent text never enters a user row.** [§3.6](rfc-question-card-pager.md) says agent-written text must not arrive with the person's authority. Here the title, detail and key are shown or kept in meta, never sent as text. The user row holds only what the owner chose or typed. A chosen label is agent-written text that the owner picked and saw, which is the authority an `[OPTIONS:]` chip label has today.
- **The agent's own text comes back as reference.** The per-turn block quotes the agent's earlier ask as gateway context, outside the person's message, never as something the person said.
- **Retirement needs the owner.** Only the owner's live `meta.human` rows, answers and dismissals retire asks. Another session's `session_send`, an app, a teammate or a channel member cannot clear them.
- **Answers are checked.** A label must match the server's stored options, and an answer must name the current `ask_id`.
- **Own slot only.** The tool routes need the internal secret and an attested session key, and they address only the caller's slot.
- **Tamper-evident store.** Records are MAC'd as change cards are. Nothing is read back from transcript rows.
- **Rendering.** Title and detail render through the chat's markdown renderer, after redaction, as assistant text does.
- **No new authority over the loop.** Pinning holds nothing, stops nothing and wakes nothing.

## Alternatives considered

- **Restore the backward `[OPTIONS:]` scan** ([#10173](https://github.com/kirodotdev/KiroCrew/pull/10173)). Rejected by the 2026-09-13 decision: it would re-surface stale asks.
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

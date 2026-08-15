# Agent questions (`ask_question`)

Lets an agent pause mid-turn, ask the dashboard user a multiple-choice question,
and receive the answer as a tool result — no extra turn, no text parsing.

## Why this exists

Kiro Crew already had two ways to offer the user a choice, and neither could
return a value to a running turn:

| Mechanism | Where it renders | Can block a turn? |
|---|---|---|
| `[OPTIONS: a \| b \| c]` text tag | every surface (dashboard chips, Slack/Discord/Telegram buttons) | No — it is an end-of-turn gate |
| `AskUserQuestion` tool → `question_card` | dashboard only | No — and the trigger tool does not exist in kiro-cli 2.14.0 |
| `ask_question` (this feature) | dashboard only | **Yes** |

The `QuestionCard` component and the `question_card` websocket event already
existed, keyed off an ACP `tool_call` titled `AskUserQuestion`. That tool is not
present in kiro-cli 2.14.0 (the string appears nowhere in the binary), so the
whole pipeline was unreachable. `ask_question` supplies the missing trigger from
Kiro Crew's own MCP server rather than waiting on the agent CLI.

### Why not ACP elicitation

kiro-cli 2.14.0 compiles the ACP `elicitation/create` schema (`form` and `url`
modes, `requestedSchema` with `enum` / `oneOf` single-select and array
multi-select) and gates it on `clientCapabilities.elicitation`. That would be the
ideal wire — its schema maps almost exactly onto `QuestionCard`'s data model.

It is not usable yet: an MCP server issuing `elicitation/create` gets back
`-32601 method not found`, i.e. the MCP→ACP forwarding path is unimplemented.
`ask_question` therefore provides the capability in-process. (Advertising
`clientCapabilities.elicitation` so the native prompt lights up when upstream
ships the bridge is a separate change — PR #512 — with no functional coupling to
this one.)

## Flow

```
agent calls ask_question (MCP)
  └─ mcp_core: strict session resolution → POST /api/ask-question   [blocks]
       └─ handlers/ask_question.api_ask_question
            ├─ validate_ask_user_question (payload normalization)
            ├─ reject unknown slot with 404 (never block on a card nobody renders)
            └─ DashboardState.request_question
                 ├─ redact question/header/option text
                 ├─ broadcast_ws("question_card", {ask_id, slot, questions})
                 └─ await future (bounded)
                                          … user clicks / types, hits Submit …
  frontend POST /api/ask-question/{ask_id}/answer
    └─ DashboardState.resolve_question → future resolves
         └─ blocked POST returns {status: "answered", answers}
              └─ agent receives "The user answered: …"
  finally: broadcast_ws("question_card_resolved", {ask_id})
```

This mirrors `DashboardState.request_approval` (the tool-approval round-trip).
The two differences: the resolution value is the user's answer map rather than an
allow/deny boolean, and the card is addressed to a single slot rather than to
the whole gateway.

## Design decisions

**Dashboard-only, strict session resolution.** `_resolve_session_key_strict`
(env var or HMAC-verified pid sidecar, never the `/proc` ancestor walk) — a
subagent living under a parent slot's process tree must not be able to post a
question card into the parent's conversation. Non-dashboard sessions get a
refusal pointing at `[OPTIONS:]`, which works on every surface.

**Bounded wait, default 300s, ceiling 540s.** The ceiling is set by the ACP
tool-stall watchdog, not by the `wait` tool. `acp/client.py::_TOOL_STALL_TIMEOUT`
is 600s and is armed once a tool call is dispatched; a blocked `ask_question`
emits no progress frames, so a window at or beyond 600s lets the watchdog declare
the turn dead and kill it — and an answer arriving after that has no turn left to
return to. 540s keeps a 60s margin. (`wait` can afford 1800s because it is a
different mechanism; copying that number here was a bug, caught in review.) The
HTTP socket timeout is deliberately `timeout_secs + 30` so the socket cannot trip
first and strand a question the user is still answering.

**Timeout and dismissal are indistinguishable to the agent.** Both yield no
answer. The tool's own output instructs the agent not to re-ask automatically —
an auto-retry loop would spam the user's chat. This is a prompt-level guard, not
an enforced rate limit.

**`question_card_resolved` carries the `ask_id`.** It fires in the `finally`
block, so a timed-out or cancelled question is always retracted from the UI
instead of staying clickable and 404-ing on submit. The frontend reducer matches
on `ask_id` so a late resolution from an earlier question cannot wipe a newer
card the user is mid-way through.

**Answer values are coerced to `str`.** Keys and values are echoed into the
agent's transcript, so a nested object cannot smuggle structure into it.

**`cancel_questions_for_slot`** unblocks every question pending on a slot. It is
called from `chat_handlers._unblock_pending_waits`, the single chokepoint that
the force-stop, soft-stop, interrupt, and slot-delete paths all use (alongside
`_reject_pending_approvals`) — so a blocked ask cannot outlive the turn that
issued it. The two unblocks are combined in one helper on purpose: a pending
question holds an MCP worker on a blocked HTTP request exactly as an unresolved
approval future holds the runner, and three separate call sites each needing
their own second line is how one of them gets missed.

**Session resets release it too.** The agent, model, bulk-model, reasoning-effort
and workspace switches all reset the slot's session, tearing down the agent
process — but a pending question lives in dashboard state, not in the session, so
it would otherwise survive the reset and wait out its full window with no agent
left to receive the answer. `chat_handlers._reset_slot_session` is the chokepoint
for that family and is the only place a switch handler may call
`sessions.reset`.

**Legacy path untouched.** The `event.title == "AskUserQuestion"` sniff in
`chat_runner.py` remains. It cannot double-render: MCP tool calls arrive titled
`Running: @<server>/<tool>`, so the equality check never matches, and cards from
that path carry no `ask_id` (the frontend keeps its send-as-message behavior for
those).

## Payload limits

Enforced by `validate_ask_user_question`, which is the single source of truth;
`ASK_QUESTION_SCHEMA` only shape-checks the agent's arguments.

| Limit | Value |
|---|---|
| questions per card | 4 |
| options per question | 6 |
| question text | 500 chars |
| header badge | 50 chars |
| option label | 200 chars |
| option description | 500 chars |

Malformed individual questions/options are skipped defensively; a payload with
no valid questions left is rejected with 400.

## API

`POST /api/ask-question` — blocks until answered.
Body: `{session_key, questions, timeout_secs?}`.
Returns `{status: "answered", ask_id, answers}` or `{status: "timeout", ask_id}`.
404 when the slot does not exist, 400 on an invalid payload.

`POST /api/ask-question/{ask_id}/answer` — resolves a pending question.
Body: `{answers: {question: answer}}`, or `{dismissed: true}` to unblock with no
answer. 404 when no pending question owns that id.

`GET /api/ask-question/pending` — question cards still awaiting an answer.
`question_card` is a one-shot broadcast, so a reload or websocket reconnect after
it fired would otherwise leave the agent waiting with nothing on screen. The
frontend re-syncs this on websocket open, the same way it re-syncs
`GET /api/approvals`. Both kinds are listed, distinguished by their identity:

| Shape | Source | Meaning |
|---|---|---|
| `{ask_id, slot, questions, ts}` | the blocked wait registry | a parked round-trip |
| `{card_id, slot, questions, ts}` | the slot's needs-input record | a stateless card |

The stateless half exists because the status is durable while the card was not:
without it a reloaded tab shows "needs your answer" with no card to answer and no
way to dismiss it (the client no longer knows the `card_id`), and only sending a
message could clear it. Rehydration is add-only and never overwrites a card
already on screen — that card is either the same ask carrying the user's
half-entered answer or a newer one, and both outrank a snapshot. A record with no
stored `questions` is a status-only marker and is not listed.

`POST /api/ask-question/dismiss` — retire a STATELESS card's session status.
Body: `{slot, card_id}` — the slot key and the card identity the `question_card`
payload carries (not a session key: a channel-born conversation's two keys differ
and the client holds only the slot). `card_id` is required because the dismissal
is a round-trip: a newer ask can replace the card before the request lands, and a
slot-only clear would retire the NEW card's status. 400 without it; 404 when the
slot holds no stateless record with that id. A blocking ask is deliberately NOT
dismissible here — it owns its lifecycle through the answer endpoint, and clearing
its status from this route would report a session as unblocked while its tool call
is still parked on the wait.

**All four endpoints are owner-only.** Refusing app tokens is not enough: a
dashboard session token is also minted for every allowed Slack user
(`!dashboard`), and it carries an empty app claim, so it clears the app gate
while belonging to someone who is not the owner. That caller could address a card
at any slot — phishing the owner with crafted options and reading the typed
answer out of its own blocked response — or resolve a card the owner is still
looking at, feeding the agent an answer the owner never gave.
`is_owner_dashboard_request` is reused rather than re-derived so "owner" has one
definition: an exact `owner_id` match, or a signed `local-app` / `local-startup`
bootstrap subject when no owner is configured. That is also the identity the
`ask_question` tool itself carries, since its token is minted as
`generate_token(owner_id or "local-app")`.

**The card is broadcast to owner sockets only.** `broadcast_ws_owners` sends both
`question_card` and `question_card_resolved` to `_owner_ws_clients`, never the
all-clients channel. Owner-gating the HTTP endpoints would buy nothing otherwise:
an allowed Slack user's `!dashboard` session registers as an ordinary WS client,
so a plain `broadcast_ws` would hand them the owner's question text, options, and
`ask_id` over the socket even though they cannot call the endpoints.

## Session status: `needs_input`

A card is a websocket broadcast with no transcript row, so an ask was visible only
in the tab that happened to receive it — and a BLOCKING ask parks the turn, so the
slot went on reporting `running` with nothing able to advance it. Every slot
payload therefore carries one derived field:

| Field | Meaning |
|---|---|
| `needs_input` | an unanswered question card is on screen |

Its scope is exactly that: it corrects a status that would otherwise be **wrong**.
A turn that merely ENDED is not an ask, including one ending in an `[OPTIONS:]`
tag — every finished turn is waiting on the user, so a status raised there carries
no information (the same reason `waiting_for_input` cannot carry a badge) and the
row already says so with its last message and its unread dot. Raising it there
also cost the row its live turn status, since surfaces rank this above every
"working" signal. It is separate from `pending_approval`, whose answer is
allow/deny on a tool rather than input, and which keeps its own precedence and
label everywhere the two are rendered.

It is **not** gated on `running`: a blocking ask parks the turn mid-flight, so the
session is running AND waiting on the user. Surfaces rank it directly below the
approval treatments and above every "working" signal for that reason — otherwise
a blocked session reads as "Thinking…".

The record is a map on the slot keyed by the ask's identity,
`{card_id: {ts, blocking, questions?}}`, written by `post_question_card` (with a
minted `card_id`, which also rides the broadcast so a client can name it later)
and by `request_question` (`card_id` = the `ask_id`). A map rather than one field
because parked asks overlap — with a single record the second ask overwrote the
first, and whichever resolved first cleared the only entry while the other was
still waiting. Entries are recorded BEFORE the delivery await, because a
backpressured socket would otherwise leave a window where a user row finds no
record to retire and the mark lands after it, stranding an answered session in
`needs_input`. There is deliberately **no capacity eviction**: dropping a blocking
entry would clear the status of an `ask_question` call still parked on its future
and report a stuck session as idle. The map is bounded by construction instead —
at most one stateless entry per slot, and one blocking entry per in-flight
`ask_question` request, each holding a live HTTP request bounded by
`_QUESTION_TIMEOUT_MAX`.

A stateless entry also stores its redacted `questions`, which is what makes the
durable status actionable: the card itself is a broadcast with no transcript row,
so the pending endpoint above serves the payload back to a reloaded tab rather
than leaving a status with no card behind it.

A **stateless card supersedes** any earlier stateless entry, because the frontend
holds one card per slot: keeping both would leave the replaced card unreachable —
nothing to answer or dismiss — while its entry held the status up. Blocking
entries are never collapsed; each parked round-trip is separately answerable.

Retirement, and only these paths:

| Path | Retires |
|---|---|
| a LIVE turn-consuming message (`user` or `nudge` — see `_QUESTION_RETIRING_ROLES`) | the stateless entry only |
| the blocking round-trip's exit (answered / dismissed / timed out / cancelled) | its own entry, by `card_id` |
| `POST /api/ask-question/dismiss` | the stateless entry, by `card_id` |

Every retirement is also **announced** as a `question_card_resolved` event carrying
the `card_id`, so a second window — and a `/pending` response already in flight —
drops the card instead of re-rendering it. The reverse direction is covered too: a
cancelled queued answer never lands, so the client re-syncs `/pending` on
`queue_cancel` and the card comes back from the record rather than leaving the
status with nothing on screen.

Both filters (`blocking`, `card_id`) leave a non-matching entry in place rather
than clearing it. Three things deliberately do NOT retire anything: a user row
against a blocking entry (nothing it does resolves a parked wait, so clearing
would report the agent as working while its tool call is stuck), a REPLAYED user
row (`broadcast=False` — transcript-rotation recovery, forks and session transfers
re-append historical rows, and an old message says nothing about the question
asked a moment ago), and the agent's own further output (a card posted mid-turn
outlives the lines that follow it).

## Frontend behaviour

The card renders through one component, `PendingQuestionCard`, used by both the
single-chat view and every session-grid pane. Panes need it because in split
mode the agent that asked may not be the pane in focus, and a pane that rendered
the card without the `ask_id` branch would start a second turn and strand the
blocked tool call.

**Submit requires an answer to every question.** The answer map is keyed by
question text, so a partial submit resumes the agent with a map missing entries
it asked for — it cannot distinguish "unanswered" from "never asked". A
multi-question card is one atomic ask.

**One submission at a time.** Submit and Dismiss both lock while a request is in
flight. Without the guard a double-click fires two calls: the first resolves the
wait, the second 404s, and the 404 handler then sends the answer *again* as a
chat message — a duplicate turn from a single user intent.

**Dismiss unblocks with no answer**, posting `{dismissed: true}` so the caller
gets a timeout-equivalent result instead of waiting out its window. The control
is offered only on `ask_id` cards; a legacy card blocks nothing, so it has
nothing to dismiss.

**Cards clear by `ask_id`, never by slot.** A slow response for ask A must not
erase a newer ask B that already replaced it in the same slot, which would leave
B blocked with no card until its own timeout. `resolveQuestionCard` exists for
exactly this; `clearQuestionCard({slot})` is only for legacy cards, which have no
id to match on.

**Only a 404 falls back to sending a message.** That is the sole proof the wait
is gone (already answered, dismissed, timed out, or its slot was reset). Any
other failure — offline, 5xx, tunnel throttle — is retryable and the agent is
almost certainly still blocked, so the card stays up for a retry; clearing it
would strand the tool call *and* start a second turn it could never join.

**Reconnect reconciles in both directions.** Both WS events are one-shot, so a
reload can miss either one — a card that should be showing is absent, or one
resolved while disconnected is still on screen. `reconcileQuestions` decides what
to drop and re-add from three inputs: the pending map captured **before** the HTTP
snapshot was requested, the map once the response arrives, and the response
itself. That ordering is the correctness argument, because the response describes
the server as it was when the request was served and races live events both ways:
a card that arrives during the fetch is missing from the response and must not be
dropped (it would leave the agent blocked until timeout), and a card resolved
during the fetch is still in the response and must not be re-added (its submit
could only 404). Legacy cards are never dropped this way: the server has no record
of them, so their absence says nothing.

**The welcome hero stands down while a card is pending.** It is centred in the
empty transcript, which is the space the card occupies above the composer, so
both mounted overlap — and an agent asking before it has produced any output is a
real first-turn case.

## When the agent should use it

Use `ask_question` to pause **mid-turn** on a decision that blocks progress.
Prefer the `[OPTIONS:]` tag when the turn is ending anyway — it is cheaper and
renders on every surface. The tool description states this so the tool does not
displace the tag everywhere.

## Not covered

- Non-dashboard surfaces. Slack/Discord have button support via `[OPTIONS:]` but
  no blocking question card.
- Rate limiting. Nothing prevents an agent from asking repeatedly within a turn
  beyond the prompt-level instruction.
- In an empty session the card visually overlaps the centered welcome
  suggestions; with any chat history it sits normally above the composer.

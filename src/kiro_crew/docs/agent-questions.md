# Agent questions (`ask_question`)

`ask_question` posts a dashboard question card for a decision that needs the user's input. It is a stateless, non-blocking tool: the agent ends its turn after requesting the card, and the answer returns as the next ordinary user message.

## When to use it

Use `ask_question` when a dashboard user needs to choose or supply an answer before the work can continue. Prefer `[OPTIONS: a | b | c]` when the turn is ending and the answer should work on every chat surface.

`ask_question` is available only to sessions with a dashboard surface. On other surfaces, use `[OPTIONS:]` instead.

## Tool input

```json
{
  "questions": [
    {
      "header": "SCOPE",
      "question": "Which deployment should I investigate?",
      "options": [
        {"label": "Production", "description": "Current production deployment"},
        {"label": "Staging", "description": "Pre-production deployment"}
      ],
      "multiSelect": false
    }
  ],
  "timeout_secs": 300
}
```

| Field | Requirement |
|---|---|
| `questions` | Required non-empty array; at most 4 questions. |
| `question` | Required text; truncated to 500 characters. |
| `header` | Optional badge text; truncated to 50 characters. |
| `options` | Required array; at most 6 valid options per question. |
| `options[].label` | Required text; truncated to 200 characters. |
| `options[].description` | Optional text; truncated to 500 characters. |
| `multiSelect` | Optional boolean; false by default. |
| `timeout_secs` | Optional integer validated from 15 through 540. The current stateless directive does not carry this value to the card, so it does not create a wait or timeout result. |

Malformed nested questions and options are skipped; the request fails when no valid question remains. Duplicate normalized question text or option labels are rejected. The frontend limits a typed custom answer to 2,000 characters.

## Flow

```
agent calls ask_question
  └─ validates and encodes a session directive with the normalized questions
       └─ dashboard session directive posts question_card without ask_id
            └─ PendingQuestionCard renders the card for that slot
                 └─ user submits answers
                      └─ answers are sent as the next ordinary chat message
                           └─ agent continues in a new turn with full context
```

The tool result tells the agent to end its turn. The server records the card as `needs_input` so a reconnect can rehydrate it from `GET /api/ask-question/pending`.

## Rendering and answers

`PendingQuestionCard` is shared by the main chat view and session panes. `QuestionCard` renders an optional uppercase header badge, the question text, labeled options with optional descriptions, and a custom-answer field.

For a single-select question, selecting a different option replaces the previous selection. For `multiSelect: true`, multiple option labels can be selected. Typing a custom answer clears option selections for that question.

Every question must have an answer before Submit becomes available. The card emits answers keyed by question text; the stateless wrapper sends the answer values as newline-separated message text. Dismiss removes the stateless card and its `needs_input` status without sending an answer.

Only one stateless card is retained per slot; a later card replaces the earlier one. A live user or nudge message retires an unanswered stateless card. Reloads and websocket reconnects reconcile pending cards with `GET /api/ask-question/pending`.

## Blocking HTTP API

The MCP `ask_question` tool does not call this API: it returns a stateless, non-blocking session directive. `POST /api/ask-question` remains a separate blocking round trip for owner callers and returns only after the card is answered, dismissed, cancelled, or timed out.

All four endpoints call `_deny_app_token` and `_deny_non_owner` before reading their bodies. App tokens receive `403 {"error": "app token not permitted for this endpoint", "code": "app_token_forbidden"}`; non-owners receive a `403` owner-only denial.

### `POST /api/ask-question`

Request body: `{session_key, questions: [...], timeout_secs?}`. `session_key` must resolve to an existing slot; `questions` uses the same validator as the tool; `timeout_secs`, when supplied, must be an integer and is bounded by the blocking wait.

Success responses are `200 {"status": "answered", "ask_id", "answers"}` or `200 {"status": "timeout", "ask_id"}`. Invalid JSON, a non-object body, a missing `session_key`, invalid questions, a non-integer timeout, or duplicate keys after redaction return `400`; an unknown or unrenderable slot returns `404`.

### `GET /api/ask-question/pending`

Returns `200` with an array of cards that can be rehydrated after a reload or websocket reconnect. A blocking card has `{ask_id, slot, questions, ts}`; a stateless card has `{card_id, slot, questions, ts}`. Empty or status-only records are omitted.

`ask_id` identifies a parked blocking wait and is answered through the endpoint below. A stateless `card_id` has no blocked caller: its answer is the next ordinary user message, and its status is retired through the dismiss endpoint or that message.

### `POST /api/ask-question/dismiss`

Request body: `{slot, card_id}`. This endpoint retires only a stateless card's `needs_input` record and returns `200 {"ok": true}`.

Invalid JSON, a non-object body, or missing `slot` or `card_id` returns `400`; an unknown, stale, or blocking card record returns `404`. It cannot dismiss a blocking `ask_id` card.

### `POST /api/ask-question/{ask_id}/answer`

Request body: `{answers: {question: answer}}`, or `{dismissed: true}` to resolve the blocking wait without an answer. Successful resolution returns `200 {"ok": true}`.

Invalid JSON, a non-object body, missing or empty answers, more than four answers, or overlong question keys or answer values return `400`; an already answered, expired, or unknown `ask_id` returns `404`.

## Blocking lifecycle

A blocking card is registered under its `ask_id` until its wait exits. Answering, dismissing, timing out, or cancellation retires that record and broadcasts its resolution.

Stopping, interrupting, or deleting a slot unblocks its pending blocking questions. Session reset uses the same unblock path, so a blocking wait cannot outlive the session that issued it.

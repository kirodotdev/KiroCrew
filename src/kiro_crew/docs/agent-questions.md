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
  ]
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
| `timeout_secs` | Optional integer validated from 15 through 540, accepted for compatibility but never read, so the tool's inputSchema does not advertise it. The current stateless directive does not carry this value to the card, so it does not create a wait or timeout result. |

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

Only one stateless card is retained per slot; a later card replaces the earlier one. A live user message retires an unanswered stateless card; an auto-nudge cycle does not, because it wakes the same agent in the same conversation and the answer still reaches it. Anything else needs the card's own Dismiss control. Reloads and websocket reconnects reconcile pending cards with `GET /api/ask-question/pending`.

## Two delivery paths for a card answer

A no-`ask_id` question card can be answered on two paths, chosen by whether the slot's turn is live when Submit is pressed:

- **Non-blocking `ask_question` card.** This tool ends the agent's turn before the card appears, so the turn is idle when the user answers. The answer starts an ordinary next turn, carrying full context. This is the path the flow above describes.
- **Native `AskUserQuestion` card.** kiro-cli raises this card while its own turn is still running and waiting on the answer. Submitting it therefore *steers* the answer into that live turn (the same `steer: true` delivery the mid-turn split send uses), so the waiting turn consumes it instead of the answer queuing behind the very turn that asked for it. If the turn has already ended by the time the user answers (the card outlived it), there is nothing to steer into and the answer falls back to starting an ordinary next turn, exactly like the non-blocking card.

Both the main chat and the split panes key this decision on the shared `selectComposerBusy` slot-turn-live rule, so the two surfaces cannot drift. Because the card clears on Submit and a steer into a busy slot shows no optimistic bubble, a steer whose delivery is not confirmed (a transport failure or a late receipt) hands the answer back to the composer with a delivery-unconfirmed notice rather than dropping it.

The blocking `POST /api/ask-question` round trip is a separate owner-only HTTP
path that no agent tool uses; its endpoint contract is a contributor reference
rather than part of using the feature.

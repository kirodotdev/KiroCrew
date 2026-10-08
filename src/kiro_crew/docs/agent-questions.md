# Agent questions (`ask_question`)

`ask_question` posts a dashboard question card for a decision that needs the user's input. In a dashboard session the tool call **blocks** until the user responds, and the answers are the tool's **result**: the agent continues in the same turn and never receives them as a user message. That matters because question text is agent-authored; echoing it back inside a user-role turn would give it the user's authority.

When the call has no attested session identity, or no dashboard client is attached to show the card, no card is shown: the tool result says why, and the agent asks in plain text instead.

## When to use it

Use `ask_question` when a dashboard user needs to choose or supply an answer before the work can continue. Prefer `[OPTIONS: a | b | c]` when the turn is ending and the answer should work on every chat surface.

`ask_question` is available only to sessions with a dashboard surface. On other surfaces (Slack, Discord, Webex and the rest), use `[OPTIONS:]` instead; the user's answer there still arrives as their next chat message, not as a tool result.

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
| `timeout_secs` | Optional integer validated from 15 through 540, accepted for compatibility but never read, so the tool's inputSchema does not advertise it. The gateway sets the card's wait window. |

Malformed nested questions and options are skipped; the request fails when no valid question remains. Duplicate normalized question text or option labels are rejected. The frontend limits a typed custom answer to the server's per-answer bound (1,485 characters, sized so four maximum-length questions and answers always fit one tool result), and the server still refuses a set of answers whose combined tool result, after credential redaction, would exceed 8,000 characters (`answers_too_long`); the card then tells you to shorten them rather than to retry.

## Flow

Blocking (a dashboard session with an attested identity and an attached client):

```
agent calls ask_question
  └─ POST /api/agent-ask/open  (X-Internal-Secret, attested X-Session-Key)
       └─ question_card with ask_id on THAT session's own slot
            └─ tool loops: /api/session-keepalive, then /api/agent-ask/<id>/wait (20 s slices)
                 └─ user submits → POST /api/ask-question/<id>/answer
                      └─ the next wait returns the outcome
                           └─ tool result → agent continues in the same turn
```

The keepalive between wait slices resets the ACP tool-stall watchdog, so the card waits for a person (for a fixed 30 minutes; callers cannot change it) rather than for a transport. A tool call that stops polling for two minutes is presumed dead and its card is withdrawn. A cancelled call withdraws its card too.

The result is one of:

| Outcome | Tool result |
|---|---|
| Answered | `User has answered your questions:` then one `"<question>" -> "<answer>"` line per question, each side a JSON string (quotes and newlines escaped), quoting the server's stored copy of each question |
| Dismissed | `The user dismissed the question card without answering.` |
| Replied in the composer | `The user replied in chat instead of answering the question card. …` — the typed text follows as the next user message |
| Replied in the composer while the slot was busy | `… Their message is queued and arrives once this turn ends …` — the typed text pops after the current turn |
| No answer in time | `The user did not answer the question card in time, so it was withdrawn. …` |
| Withdrawn (restart, tab reset, lost ask) | `The question card was withdrawn before the user answered. …` |

The transcript records an answered card under the `ask_question` tool row as a folded **N questions answered** chip that opens to each question and answer. It is built from the persisted tool output, so it survives a reload, and it is keyed on the backend-recorded tool name so no other tool's output can render as one.

If the user submits after the wait has already ended, the answer endpoint returns 404 and retires the stale card. The 404 carries the recorded terminal reason while it remains available: an answer that already reached the agent (`answered`, `composer`, or `queued`) is discarded, while an expired or reasonless legacy ending places the answer text in the composer rather than sending it. Restored text quotes the agent's questions, and only the user may choose to send that as their own message.

## Rendering and answers

`PendingQuestionCard` is shared by the main chat view and session panes. `QuestionCard` renders an optional uppercase header badge, the question text, labeled options with optional descriptions, and a custom-answer field. A card carrying more than one question shows one question at a time, with its position and back/forward arrows in the corner; a single-question card shows neither.

For a single-select question, selecting a different option replaces the previous selection. For `multiSelect: true`, multiple option labels can be selected, and each option carries a checkbox so the mode is visible before the second pick. Typing a custom answer clears option selections for that question.

Every question must have an answer before Submit becomes available, and on a multi-question card Submit is reached by walking to the last question. The card emits answers keyed by question text. The stateless wrapper sends one `Q. <question>` / `A. <answer>` pair per question, pairs separated by a blank line, so an answer cannot be read without the question it belongs to; a single-question card sends the bare answer, which needs no label. The `Q.` / `A.` initials come from the active locale. Dismiss removes the stateless card and its `needs_input` status without sending an answer.

Only one stateless card is retained per slot; a later card replaces the earlier one. The server owns the card's lifecycle: a live user message (or, for a native card, a consumed steer) retires the record and broadcasts `question_card_resolved` with the `card_id`, and every open window clears the card from that one broadcast, except a window where the stateless card holds a typed answer in progress: that card stays so the draft is not lost, and it can still be dismissed or sent as a plain message; a blocking card's corresponding frame carries its `ask_id` and terminal reason, so another window discards an answer that already reached the agent and restores only an unanswered draft. An auto-nudge cycle does not retire a card, because it wakes the same agent in the same conversation and the answer still reaches it. Anything else needs the card's own Dismiss control. Reloads and websocket reconnects reconcile pending cards and recent blocking-card outcomes with `GET /api/ask-question/pending`.

## The no-`ask_id` card: kiro-cli's native `AskUserQuestion`

Only one kind of card has no `ask_id` now: the one kiro-cli raises itself through its native `AskUserQuestion` tool while its own turn is still running and waiting on the answer. The server posts it through the stateless owner (a `card_id`, a `needs_input` record, the same retirement) and marks it `native` on the `question_card` frame and the `/pending` row, which is how the dashboard tells it apart from a blocking MCP card. Submitting it *steers* the answer into that live turn (the same `steer: true` delivery the mid-turn split send uses), so the waiting turn consumes it instead of the answer queuing behind the very turn that asked for it. If the turn has already ended by the time the user answers (the card outlived it), there is nothing to steer into and the answer falls back to starting an ordinary next turn.

Both the main chat and the split panes key this decision on the shared `selectComposerBusy` slot-turn-live rule, so the two surfaces cannot drift. Because the card clears on Submit and a steer into a busy slot shows no optimistic bubble, a steer whose delivery is not confirmed (a transport failure or a late receipt) hands the answer back to the composer with a delivery-unconfirmed notice rather than dropping it.

A blocking MCP card answered from the composer instead of the card is retired through the answer endpoint with a reason: `composer` when the message is sent now, `queued` when the slot was busy and the message pops at turn end. The tool result tells the agent which, so it reads the next message rather than treating the question as declined.

The older blocking `POST /api/ask-question` round trip is a separate owner-only
HTTP path, held open as one request and therefore capped at 540 seconds under
the tool-stall watchdog; its endpoint contract is a contributor reference
rather than part of using the feature.

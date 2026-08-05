# Inline Action Buttons

## Problem

Agents can send Block Kit messages with arbitrary buttons via `send_message`, but every button click currently goes through the OPTIONS handler which:

1. Deletes the original message
2. Posts the button's `value` as a **visible user message** in the thread
3. Starts a **new session** with that text

This means structured payloads (JSON, metadata) pollute the chat, and the originating session loses context. Adding new button behaviors requires KiroCrew code changes.

## Goal

Allow agents to define arbitrary Block Kit buttons whose clicks route back to the **originating session** as invisible context, with no KiroCrew code changes per button type.

## Design

### Button value protocol

Buttons use a prefix convention in their `value` field to control routing:

| Prefix | Behavior |
|---|---|
| `action::{payload}` | Route payload to originating session as context injection |
| _(no prefix)_ | Current behavior — visible message, new session |

The `action::` prefix is chosen because it's unlikely to collide with natural language button values, and the `::` delimiter is unambiguous.

### Example button

```json
{
  "type": "button",
  "text": {"type": "plain_text", "text": "📝 Draft reply"},
  "action_id": "options_choice_0",
  "value": "action::{\"intent\": \"draft_reply\", \"email_id\": \"AAMk...\", \"from\": \"jbarr\"}"
}
```

### Clicked-button visual state

When a button is clicked, it must remain visible as a non-interactive "done" indicator. Slack Block Kit doesn't support disabled buttons, so we use this approach:

- The `actions` block containing the clicked button is split into:
  1. A `context` block with mrkdwn text `✓ {button label}` (renders as small gray text)
  2. A new `actions` block with the remaining (unclicked) buttons
- If all buttons in an actions block have been clicked, only context blocks remain

This preserves full visual context — with 5 emails × 4 buttons each, the user always sees which actions they've taken and which remain.

### Routing flow

1. User clicks button → Slack sends interaction payload to Socket Mode handler
2. `interactions.py::_handle_options` checks if `value.startswith("action::")`
3. **If action button:**
   - Extract payload: `value[len("action::"):]`
   - Extract display text from `action["text"]["text"]`
   - Replace the clicked button with a `✓ {label}` context block (see above)
   - Update the Slack message via `update_message` with the modified blocks
   - Post the display text as a visible user message for conversational continuity
   - Inject the structured payload as a context entry:
     ```
     --- CONTEXT ENTRY BEGIN ---
     [Action button clicked: {"intent": "draft_reply", "email_id": "AAMk...", "from": "jbarr"}]
     --- CONTEXT ENTRY END ---
     ```
   - Route to the **existing session** (same `thread_ts`) via `handle_message`
4. **If no prefix:** current behavior unchanged (delete message, post value, new session)

### What the agent sees

A normal message turn with:
- **Visible text:** The button's display text (e.g., "📝 Draft reply")
- **Context entry:** The full structured payload

The agent parses the JSON from the context entry and dispatches on `intent` or any other field. No KiroCrew code changes needed per button type — the agent's prompt defines the behavior.

### What the user sees

- The button they clicked becomes a static "✓ Draft reply" label (small gray text)
- All other buttons on the same message remain clickable
- A short message appears in the thread: "📝 Draft reply"
- The agent responds in the same thread

No JSON or structured payload visible in chat.

## Files changed

### `src/kiro_crew/slack/interactions.py`

Modify `_handle_options`:
- Check `value` for `action::` prefix (buttons), then `action_id` (extended elements)
- For buttons: extract payload from `value` (existing behavior)
- For extended elements: extract base payload from `action_id`, extract user selection via `_extract_selected_value(action)`, merge as `selected_value`
- Derive display label: buttons use `text.text`, extended elements use `placeholder.text` + selected display text
- Rest of flow shared: transform blocks, update message, post display text, inject context, route to session
- If no prefix on either field: current behavior (unchanged)

Add helper `_extract_selected_value(action) -> tuple[str, str]`:
- Returns `(raw_value, display_text)` by checking fields in order:
  `selected_option` → `selected_date` → `selected_time` → `selected_date_time`
- For `selected_option`: raw = `.value`, display = `.text.text`
- For date/time: raw = display = the date/time string

Add helper `_mark_button_clicked(blocks, clicked_action_id, label) -> list`:
- Walk blocks looking for `actions` blocks
- When the clicked `action_id` is found in an actions block's elements:
  - Remove the clicked element from elements
  - Insert a `context` block (with `✓ {label}`) immediately before the actions block
  - If no elements remain, drop the now-empty actions block
- Return the updated block list (works for any element type, not just buttons)

### `src/kiro_crew/slack/handler.py`

Add optional `action_context` parameter to `handle_message`:
- When provided, prepend as a context entry in the message sent to the agent

### `src/kiro_crew/context.py`

Add `action_context` parameter to `ContextBuilder.build_message`:
- When provided, append as a `--- CONTEXT ENTRY ---` block

## Extended interactive elements

### Supported elements

Beyond buttons, the `action::` protocol extends to other Block Kit interactive elements that fire `block_actions` events:

| Element | Slack payload field | Example use case |
|---|---|---|
| Static select menu | `selected_option.value` | Priority picker, category selector |
| Date picker | `selected_date` (YYYY-MM-DD) | Snooze until, deadline |
| Time picker | `selected_time` (HH:mm) | Reminder time |
| Datetime picker | `selected_date_time` (unix epoch) | Schedule send |
| Overflow menu | `selected_option.value` | Compact action menu |
| Radio buttons | `selected_option.value` | Single-choice selection |

Not supported (different interaction model):
- Multi-select menus / checkboxes (array payloads — add later if needed)
- Plain text input (requires `dispatch_action` or modals)
- Modals / views

### Routing protocol

The `action_id` carries the routing prefix and base payload. The user's selection is extracted from the element-specific field and merged in:

```
action_id: "action::{\"intent\": \"snooze\", \"email_id\": \"AAMk...\"}"
```

When the user picks a date, the handler:
1. Parses the base payload from `action_id[len("action::"):]`
2. Extracts the user's selection from the element-specific field
3. Merges them: `{"intent": "snooze", "email_id": "AAMk...", "selected_value": "2026-04-15"}`
4. Injects the merged payload as the context entry

The `selected_value` key is normalized — regardless of whether Slack sends `selected_date`, `selected_time`, `selected_option.value`, etc., the context entry always uses `selected_value`.

### Example: date picker

Agent sends:
```json
{
  "type": "datepicker",
  "action_id": "action::{\"intent\": \"snooze\", \"email_id\": \"AAMk...\"}",
  "placeholder": {"type": "plain_text", "text": "Snooze until..."}
}
```

User picks 2026-04-15. The agent sees:
```
--- CONTEXT ENTRY BEGIN ---
[Action element selected: {"intent": "snooze", "email_id": "AAMk...", "selected_value": "2026-04-15"}]
--- CONTEXT ENTRY END ---
```

### Example: static select

Agent sends:
```json
{
  "type": "static_select",
  "action_id": "action::{\"intent\": \"set_priority\", \"task_id\": \"T-1234\"}",
  "placeholder": {"type": "plain_text", "text": "Set priority"},
  "options": [
    {"text": {"type": "plain_text", "text": "🔴 High"}, "value": "high"},
    {"text": {"type": "plain_text", "text": "🟡 Medium"}, "value": "medium"},
    {"text": {"type": "plain_text", "text": "🟢 Low"}, "value": "low"}
  ]
}
```

User picks "🔴 High". The agent sees:
```
--- CONTEXT ENTRY BEGIN ---
[Action element selected: {"intent": "set_priority", "task_id": "T-1234", "selected_value": "high"}]
--- CONTEXT ENTRY END ---
```

### Visual feedback for non-button elements

Non-button elements use the same `_mark_button_clicked` approach, adapted per element type:

| Element | ✓ label format |
|---|---|
| Date picker | `✓ Snooze until: 2026-04-15` |
| Time picker | `✓ Remind at: 14:30` |
| Static select | `✓ Priority: 🔴 High` |
| Overflow / radio | `✓ {selected option text}` |

The label is composed from the element's `placeholder.text` (or a fallback) + the selected display text.

### Key difference from buttons

Buttons carry the full payload in `value`. Extended elements carry the base payload in `action_id` and the user's selection in an element-specific field. This is because:
- `action_id` is the only field common to all interactive elements
- `value` is button-specific; other elements use `selected_date`, `selected_option`, etc.
- The `action_id` has a 255-char limit in Slack, which is sufficient for typical JSON payloads

### `action_id` length validation

`send_message` validates that every `action_id` in the outgoing blocks is ≤ 255 characters. If any `action_id` exceeds the limit, the request is rejected immediately with a clear error (HTTP 400) listing the offending element. This catches oversized payloads before they reach Slack, giving the agent actionable feedback to shorten or use short-ID indirection.

### Implementation

Modify `_handle_options` to:
1. Check `action_id` (not just `value`) for `action::` prefix
2. Extract base payload from `action_id`
3. Extract user selection via priority chain: `selected_option.value` → `selected_date` → `selected_time` → `selected_date_time`
4. Merge `{"selected_value": extracted}` into base payload
5. Derive display label from element type + selection text
6. Rest of flow identical: update message, post display text, inject context, route to session

Buttons use `value` exclusively for the `action::` prefix — no `action_id` fallback. Extended elements use `action_id` exclusively. This keeps routing unambiguous: each element type has exactly one field to check.

## Not in scope

- Custom `action_id` routing beyond the `action::` prefix convention (buttons use `options_choice_` prefix; extended elements use `action::` in `action_id`)
- Multi-click tracking / state management
- Dashboard button support (Slack-only for now)
- Button-specific agent selection (uses thread's current agent)
- Multi-select menus / checkboxes (array payloads)
- Plain text input / modals

## Testing

- Unit test: `_mark_button_clicked` correctly transforms blocks for buttons and non-button elements
- Unit test: `_extract_selected_value` returns correct value/display for each element type
- Unit test: `_handle_options` with `action::` prefix in `value` (button) routes to existing session
- Unit test: `_handle_options` with `action::` prefix in `action_id` (extended element) merges `selected_value` and routes
- Unit test: `_handle_options` without prefix on either field preserves current behavior
- Manual: send Block Kit message with action buttons, click several, verify ✓ labels persist
- Manual: send message with date picker, select date, verify merged payload in context

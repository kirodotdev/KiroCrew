---
name: mochi-remind
description: "Handle due reminders — notify the user with natural language and mark them done."
always: false
---

# @mochi-remind — Handle Due Reminders

Handle reminders that have reached their trigger time.

## Steps

### 1. Verify Meetings (if applicable)

For items with `kind=meeting`, only if the user has connected a calendar tool. **Do not assume a
server or tool name** — look at the tools you actually have and find one that lists events in a
time range.

1. List events in a window around the meeting's `triggerAt` (±2 hours) to find it.
2. Match by subject/title (fuzzy — the label may be abbreviated).
3. Check the event status:
   - **Cancelled**: Tell the user the meeting was cancelled. Mark the item `done`.
   - **Rescheduled** (start time differs from `triggerAt` by >5 min): Tell the user the new time. Call `update_watchlist` to update `triggerAt` to the new start time minus the original lead time. Do NOT mark done.
   - **Still on schedule**: Proceed to notify normally (step 2).
4. If the lookup fails or returns no match, proceed with the reminder anyway — don't block notification on a calendar lookup failure.

Skip this step for `kind=reminder` items or when you have no calendar tool.

### 2. Notify the User

**Before notifying, check activity log for duplicates:**
Call `read_mochi_file({ which: "activity" })` and scan the last 10 entries.
If you find a `notification` entry within the last 15 minutes that already communicated
the same reminder (same label/subject), skip the notification — it was already delivered
by a concurrent agent. Just mark the item done and finish.

Use `perform_pet_action({ action: "notify", pushToChat: true })` with natural language.

**Single reminder:**
"Hey! You asked me to remind you to review the design doc"

**Meeting reminder:**
"Heads up — Team sync starts in 5 minutes!"

**Cancelled meeting:**
"Good news — Team sync was cancelled, you've got a free slot 🎉"

**Rescheduled meeting:**
"Heads up — Team sync got moved to 3:30 PM (was 2:00 PM)"

**Multiple reminders:**
"A few things! You wanted to: review the design doc, check the deployment status,
and prep for the 1:1."

**Late reminder (idle resume):**
"Welcome back! You asked me to remind you about the design doc at 2:00 PM —
sorry I'm late, you were away"

Use the pet's personality — warm, concise, with occasional emoji.

### 3. Mark Done

Call `update_watchlist` to mark each reminder as done:

```
update_watchlist({ update: [
  { id: '<reminder-id>', status: 'done' }
]})
```

For rescheduled meetings, update `triggerAt` (do NOT set `nextCheckAfter` — meetings use `triggerAt` only):
```
update_watchlist({ update: [
  { id: '<meeting-id>', triggerAt: '<new-iso-time>' }
]})
```

### 4. Finish

After marking reminders done (or rescheduling), you're done. No further action needed.

## Tool Restrictions

Allowed tools:
- `update_watchlist` — mark reminders as done or reschedule
- `perform_pet_action` — notify the user
- `read_mochi_file` — check the activity log for duplicate notifications
- A calendar tool, if the user has connected one — used only to verify a `kind=meeting` item. Whatever it is called; do not guess at a name that isn't in your tool list.

## Dedup Rule

Before notifying, check if this reminder was already handled. If the item's `status` is already `done`, skip it — another agent or the system already processed it. Never push the same reminder content to chat twice.

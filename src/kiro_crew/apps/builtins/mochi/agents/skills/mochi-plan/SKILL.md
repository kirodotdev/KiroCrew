---
name: mochi-plan
description: "INTERNAL pet queue — generates the pet's 30-minute behavior schedule (moves, moods, notifications). NOT for user task or calendar planning."
always: false
---

# @mochi-plan — Full Queue Planning

You are creating a complete plan for the upcoming period. This is the heavyweight path — only invoked when the queue is missing, expired, or all tasks are done.

**Plan window**: You decide how long to plan for based on context:
- Normal daytime: 60 minutes
- Late night (after 22:00) with user inactive: 2–4 hours (let the pet sleep)
- During a long meeting block: until the last meeting ends + 5 min
- Weekend / quiet day: 1–2 hours
Set `planned_until` accordingly. The poller won't re-plan until your window expires.

**IMPORTANT: get_plan, update_plan, get_watchlist, update_watchlist, perform_pet_action, read_mochi_file, etc. are Mochi's own MCP tools, available to you directly. Call them as regular tool calls — do NOT try to use any workaround.**

## Step 1: Gather Context

Read ALL of these (in order):
1. `get_plan()` — the previous queue: scan expired tasks and planner_notes
2. `get_watchlist()` — active watch items, plus ones that finished recently (reminders, meetings, watched pages). Items the user CANCELLED are in here too, and must not be resurrected.
3. `read_mochi_file({ which: "activity" })` — today's activity log (`"activity-yesterday"` for the previous day)
4. `perform_pet_action({ action: "query" })` — current pet position and display info. This is a read; it does not move or speak.
5. Today's calendar (optional) — only if the user has connected a calendar tool:
   - **Do not assume a server or tool name.** Look at the tools you actually have and find one that lists events in a time range, and (if present) one that returns a single event's details.
   - **If no calendar tool is available**: SKIP the calendar step and plan without calendar awareness. Do not block on it and do not attempt any workaround.

**IMPORTANT: Calendar times are in UTC (ISO format). Convert to the user's local timezone
(from the ## Current Time section in your prompt) before comparing with "now" or presenting
to the user. Never show UTC times to the user.**

### Step 1.5b: Update User Rhythm

If `planner_notes.user_rhythm` is missing or older than 24 hours, scan the activity log
(today's plus `activity-yesterday`) to extract the user's work patterns:

```json
{
  "typical_start": "09:30",
  "typical_end": "18:00",
  "focus_hours": ["14:00-16:00"],
  "quiet_days": ["Saturday", "Sunday"],
  "avg_response_mins": 5,
  "updated_at": "<ISO timestamp>"
}
```

How to compute:
- `typical_start`: average time of first activity entry each day
- `typical_end`: average time of last activity entry each day
- `focus_hours`: time ranges where message density is lowest (gaps > 30 min between activities)
- `quiet_days`: days with < 5 activity entries
- `avg_response_mins`: average gap between consecutive chat messages

If not enough data (only today's log, or a nearly empty one), use defaults: start 09:00, end 18:00, no focus hours, weekends quiet.
Store in `planner_notes.user_rhythm` — it persists across plans.

### Calendar → Watchlist Sync (Step 1.5)

After fetching the calendar, sync meetings into the watchlist as `kind: 'meeting'` items.
The planning agent manages meeting lifecycle — the chat agent does not create meetings.

**IMPORTANT: Always call `get_watchlist()` first** to check existing items — it returns items that already finished, which is how you avoid resurrecting a meeting.

For each meeting today:
1. Check if a watch item with matching label already exists (any kind: 'meeting' or 'reminder')
2. If exists with `status: 'cancelled'` → **SKIP. User explicitly dismissed this meeting. Do NOT re-add.**
3. If exists with `status: 'watching'` → check if the calendar start time changed:
   - Compare the calendar start time against the item's `notes.meetingStart` field.
   - If the start time differs by >1 min, update the item — preserve the user's lead time (`triggerAt` offset from `meetingStart`): `update_watchlist({ update: [{ id, triggerAt: '<same lead offset before new start ISO>', notes: { meetingStart: '<new start ISO>' } }] })`
   - Otherwise **SKIP. Do NOT modify.** The user or chat agent may have customized triggerAt or notes (e.g. "remind me 30 min early to prep slides"). Never override their changes.
4. If exists with `status: 'done'` → skip (already notified)
5. If not found at all → `update_watchlist({ add: [{ label: '<title>', kind: 'meeting', target: '<title>', triggerAt: '<5 min before start ISO>', autoComplete: true, priority: 'high', notes: { meetingStart: '<start ISO>' } }] })`
6. If meeting was removed from calendar but still `watching` in watchlist → `update_watchlist({ cancel: ['<id>'] })`

Meeting items use `triggerAt` only — they do NOT need periodic checking via `nextCheckAfter`. Do NOT set `nextCheckAfter` when creating or updating meeting items.
The WatchlistService reminderTimer (1-min precision) fires when `triggerAt` arrives.

Use calendar data for planning awareness too:
- Avoid scheduling moves/notifications during meetings
- Include meeting count and times in the daily briefing
- Schedule breaks between back-to-back meetings

## Step 2: Assess Situation

Before building the queue, run through these checks. They determine what kind of plan to create.

### Meeting Time Classification
For each calendar event, compare its start/end times against the current time:
- **Upcoming**: starts in the future → plan a pre-meeting notification
- **Ongoing**: started in the past, ends in the future → user is in the meeting now, minimize interruptions
- **Completed**: ended in the past → do NOT reference it as upcoming or ongoing in the narrative. Mention it only in past tense if relevant ("Offsite ended at 4 PM")

This classification is critical. Never say a meeting is "about to start" if it has already ended.

### Activity Mode (from user settings)
The owner has configured their preferred activity level. This was injected into your session context. Follow it when deciding how many moves, notifications, and interactions to schedule. If the mode says "minimal interruption", respect that — fewer moves, fewer notifications, only important alerts.

### Sleep Detection
If the last user activity (message, drag, screenshot) was > 2 hours ago AND it's between 23:00–08:00 → owner is likely asleep. Create a minimal plan: 1-2 slow moves only, no notifications. Set mood to "sleepy".

### First Plan of the Day — Daily Briefing
If the previous queue is missing or its `planned_at` is yesterday → this is the first plan today.

1. **Greeting**: Time-appropriate greeting as the first notify task:
   - Before 12:00 → "Good morning!"
   - 12:00–18:00 → "Good afternoon!"
   - After 18:00 → "Good evening!"

2. **Overnight changes**: Call `get_watchlist()` — items that finished recently are still in the list, which is what makes overnight change detection possible.
   Scan for items that changed since the last plan:
   - Items that changed status (a watched page moved, a build finished) → mention specifically
   - Items that expired or were auto-completed → mention briefly
   - Items with failCount > 0 (check failures) → flag as needing attention
   - If nothing changed → "All quiet overnight ✅"

3. **Pending attention**: From the active watchlist, highlight:
   - High priority items with no change in > 24h → "the release page hasn't moved in 3 days"
   - Reminders due today → "You have a reminder at 3pm: review doc"

4. **Today's schedule**: Calendar summary from Step 1 (meetings count, first meeting time, attendees).
   Include whatever detail the calendar tool you have actually returns — location and organizer if
   present, otherwise just times and titles. If it can report free/busy, use that to name focus blocks.

5. **Slack mentions**: Only if the user has Slack tools:
   - List channels with unread / mention counts, skip every DM, take the few with mentions
   - Read recent messages in those and pick out the ones addressed to the user
   - Example: "3 people pinged you on Slack: Alice in #oncall about deployment, Bob in #team about sprint"
   - If you have no Slack tools, skip this step silently. Do not guess at tool names.

6. **Slack channel digests**: Check watchlist for `kind: 'slack-channel'` items.
   If any exist and their last check was > 20h ago, their summaries will be available
   from the watch check results. Include the latest `lastResult` in the briefing.
   If no slack-channel items in watchlist, skip this step.

7. **Compose briefing**: Combine steps 1–6 into ONE natural-language notify with `pushToChat: true`:
   "Good morning! Two of your watched pages changed overnight, one build is still running.
   3 meetings today, first at 10:00."

   Rules:
   - Normal things in one line, abnormal things get detail
   - If everything is normal, keep it to one sentence
   - If something needs user action, say it explicitly
   - Keep it concise — user reads this in a small bubble

8. **Carry yesterday forward**: put the day's key events in `planner_notes` so the next plan has them. Do NOT call `learn_add` — lessons are the chat agent's job and that tool is not yours.

### Owner Returned After Absence
If the activity log shows a gap > 30 minutes followed by recent activity → owner just came back. Schedule a welcome-back notify early in the plan.

### Late Night / Weekend Work
If it's after 22:00 on a weekday or anytime on a weekend AND the owner is active → comment naturally (surprise, concern, encouragement). Don't nag.

## Step 3: Scan Expired Tasks

If the previous queue had unexecuted tasks (done: false, execute_after in the past):
- **Meeting reminders** (category: "meeting") expired < 15 min ago → create a catch-up notification: "You had {title} — how did it go?"
- **Expired moves / tips** → discard (no longer relevant)
- **All other expired tasks** → discard

## Step 4: Build the Queue

Create a complete queue for the next plan window (typically 60 minutes daytime).

### Milestones
Check the activity log for recent `[memory]` entries. If you see a round-number milestone (e.g., "Companion hour #50", "#100", "#200"), schedule a celebratory notify task with `pushToChat: true`.

### Meetings
- Meeting watch items are synced from calendar in Step 1.5 — respecting user cancellations.
- Meetings use `triggerAt` only (no periodic checking). WatchlistService notifies at trigger time.
- During active meetings: no moves, no notifications (except urgent ones)
- After a meeting ends: the watch item auto-completes. Optionally schedule a welcome-back notify: "How was {title}?"

### Battery
Skip battery entirely. There is no battery tool in this build, so do not schedule
low-battery warnings and do not claim to be watching the battery.

### Break Reminders
- If the owner has been active for a long stretch without a break (check activity log timestamps) → schedule a break suggestion notify with mood "sleepy"
- Don't repeat if you already suggested a break recently (check planner_notes or done tasks)

### Feature Tips (First Week)
If the `[memory]` entries show companion hours < 168 (one week), read the tips catalog
(`mochi-tips` — its path is in the ## Your Skills table in your prompt) and pick one tip
that isn't in `planner_notes.skipped_tips`. Schedule it as a notify task. Add the tip id to
`skipped_tips` in the plan's planner_notes.

### Task Types
- **move**: Wandering movements to feel alive. Mix positions, peek from edges, visit other displays. Move frequently — a pet that stays still feels dead.
- **notify**: Speech bubbles for meaningful moments. Don't spam, but don't be silent either.
- **mood**: Set expression based on context (playful when idle, curious when something changed, calm during meetings).
- **freestyle**: Open-ended agent task for creative behavior. Use sparingly.

Note: `check_watching` is no longer a queue task type. Watch checks are driven by the Electron
WatchlistService timer based on each item's `nextCheckAfter`. Reminders are also handled by
WatchlistService's reminder timer.

Use your judgment on quantity — it depends on context. A lazy Sunday afternoon might have lots of wandering. A busy meeting block might have zero moves and just reminders.

### Watch Items

Read the watchlist from Step 1 for situational awareness only. **Do NOT schedule check_watching tasks.**

Watch item checks are driven by the Electron WatchlistService timer (1-min precision, based on `nextCheckAfter`).
The planning agent's role is limited to:
- Reading watchlist status for the narrative and daily briefing
- Syncing calendar meetings into the watchlist (Step 1.5) — respecting user cancellations
- Noting watch item status in `planner_notes.watching_last_status` for context

**Do NOT:**
- Schedule `check_watching` tasks in the queue
- Try to control watch check frequency or timing
- Set `nextCheckAfter` or `checkIntervalMins` on watch items
- Re-add meetings the user has cancelled (`status: 'cancelled'`)

### Agent Task Scheduling
- freestyle is low priority — only schedule if the plan has room
- Set `requires_agent: true` on freestyle tasks

### Timing
- Space tasks apart for natural rhythm — don't cluster everything at the start
- Meeting reminders: 5 minutes before meeting start
- Don't schedule anything during active meetings

### Rhythm-Aware Scheduling
If `planner_notes.user_rhythm` exists, use it to adjust the plan:
- Current time < `typical_start` → minimal plan (1-2 slow moves, no greeting yet — user hasn't arrived)
- Current time is in `focus_hours` → reduce notifications to high priority only, fewer moves
- Today is in `quiet_days` → lighter plan overall (fewer moves, fewer tips, only important notifications)
- Space notifications at least `avg_response_mins` apart (don't send faster than the user reads)
- If no rhythm data yet (first few days), use defaults and don't apply these rules
- If current time > `typical_end - 30min` (approaching end of workday) → skip low/normal priority watch checks, only schedule high priority
- If current time > `typical_end` (past end of workday) → skip all watch checks unless high priority. User is likely wrapping up or gone.

## Step 5: Submit the Plan

Call `update_plan` with `full_replace`:

```
update_plan({
  full_replace: {
    planned_at: "<now ISO>",
    planned_until: "<now + chosen window — see Plan Window rules above>",
    needs_replan: false,
    mood: "<overall mood for this cycle>",
    narrative: "<one sentence describing the current situation>",
    planner_notes: {
      skipped_tips: ["<tips already given — accumulate across plans>"],
      watching_last_status: { "<item>": "<last known status>" },
      user_pattern: "<observed user behavior pattern>"
    },
    tasks: [ ... ]
  }
})
```

## Important Rules
- Do NOT *execute* pet actions — every move, bubble, and mood goes into the queue via `update_plan`. The one exception is `perform_pet_action({ action: "query" })` in Step 1, which only reads position.
- Keep the narrative concise (one sentence)
- Set `requires_agent: true` on freestyle tasks
- Preserve `planner_notes.skipped_tips` across plans — append, don't reset
- **No duplicate notifications**: Before adding a notify task with `pushToChat: true`, check the activity log and done tasks from the previous queue. If the same message (or substantially similar content) was already pushed to chat recently, do NOT include it again. The user sees chat history — repeating the same notification is annoying.

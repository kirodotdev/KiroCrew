---
name: mochi-replan
description: "INTERNAL pet queue — partial adjustment of the pet's behavior schedule. NOT for user task or calendar planning."
always: false
---

# @mochi-replan — Partial Queue Adjustment

Adjust the current plan without starting from scratch. Faster and cheaper than @mochi-plan.

## Context to Read

1. `get_plan()` — current queue (pending tasks only, done tasks filtered out)
2. `get_watchlist()` — active watch items (check for overdue items that need scheduling)
3. `read_mochi_file({ which: "activity" })` — recent activity (last 5-10 entries)
4. The `planner_notes` from the queue — contains memory from the last full plan

Do NOT re-read the calendar. That is only for `mochi-plan`.

## Two Modes

### Automatic Mode (triggered by poller when needs_replan is true)

Detect what changed and adjust:

- **User dragged the pet** (activity shows drag/interrupted_by_user):
  → Remove the next scheduled move task
  → Add a playful reaction: `{ type: "notify", action: { summary: "Hey! I was going somewhere!" }, category: "reaction" }`
  → Optionally add a new move back to a natural position

- **User sent a message referencing the plan** (activity shows recent chat):
  → Check if any queued tasks conflict with what the user said
  → Adjust accordingly

- **Watched item status changed**:
  → If a watch result notification came in (activity log), update planner_notes.watching_last_status
  → Note: watch checks are driven by WatchlistService, not the queue. Do NOT schedule check_watching tasks.

- **General drift** (time passed, some tasks executed):
  → Rebalance remaining tasks if timing feels off

### Explicit Mode (triggered by chat agent with user request)

The chat agent will provide context about what the user wants. Common requests:

- **"Remind me at 5pm"** → Chat agent handles this directly via `update_watchlist` + `update_plan` (see prompt.md Watch List section). Replan is only needed if the chat agent sets `needs_replan: true`.
- **"Watch this page"** → Chat agent handles this directly via `update_watchlist`. WatchlistService timer will handle periodic checks automatically based on `nextCheckAfter`.
- **"Be quiet for a while"** → Remove upcoming notify tasks, keep only moves
- **"Come here"** → This should use `perform_pet_action` instead (instant), but if it comes through replan, add an immediate move
- **"Stop moving"** → Remove upcoming move tasks

## How to Apply Changes

Use `update_plan` with incremental changes:

```
update_plan({
  tasks_to_add: [{ id: "r1", type: "notify", ... }],
  tasks_to_remove: ["t3"],  // remove by id
  tasks_to_modify: [{ id: "t4", execute_after: "<new time>" }],
  needs_replan: false,  // ALWAYS set this to false when replan completes
  narrative: "<updated situation description>"
})
```

## Important Rules
- ALWAYS set `needs_replan: false` when done — otherwise the next poll cycle will replan again
- Do NOT call perform_pet_action() or any execution tools — all actions go through update_plan queue changes
- Keep changes minimal — only adjust what needs adjusting
- Preserve planner_notes structure when updating
- **No duplicate notifications**: Before adding a notify task with `pushToChat: true`, check the activity log and existing done tasks. If the same message (or substantially similar content) was already pushed to chat, do NOT add it again. The user sees chat history — repeating the same notification is annoying.

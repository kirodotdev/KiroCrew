---
name: mochi-watch
description: "Check one or more watch items, compare with last known status, suggest actions, and update the watchlist."
always: false
---

# @mochi-watch — Check Watch Items

Check watch items from the persistent watchlist, compare with last known status, and notify on changes.

> **The watchlist is its own scheduler.** Each item's `checkIntervalMins` is consumed by the queue poller to decide when to re-check. NEVER create a `cron_add` / `@kirocrew-cron` job for a watch item — the cron would run the check a second time and burn tokens twice. When creating a watch item, ALWAYS set `checkIntervalMins` explicitly from the user's stated frequency (daily = 1440, hourly = 60, etc.); omitting it silently applies the 10-minute default, which is almost never what the user asked for. That value is remembered as `baseIntervalMins` and is the floor for every later adjustment.

## Steps

### 1. Check Status

For each item, use the appropriate tool:

| Kind | How to check |
|------|------|
| url | `web_fetch({ url: "<target>" })` |
| custom | Decide based on the target description — `web_fetch`, or whatever available tool fits best |
| slack-channel | Read the channel's recent messages since `lastChecked`, then summarize key topics. Use whichever Slack tool you actually have; do not guess at a name. |
| slack-topic | Search Slack for `<target>`, newest first, and keep results newer than `lastChecked`. Same rule about tool names. |

If a check fails, try an alternative tool before incrementing failCount.
Only increment failCount if ALL alternatives also fail.
Continue checking other items — don't abort the batch on one failure.
If a `slack-*` item's kind needs a Slack tool you do not have, record that as the failure reason
rather than improvising a tool call.

**⚠️ CRITICAL: Even if a check fails (JS-rendered page, 403, network error, no data), you MUST still call `update_watchlist` with a `historyEntry` for that item. This advances `nextCheckAfter` so the item is not immediately re-checked on the next poll. Failing to write `historyEntry` causes the item to stay "due" forever, triggering a spawn storm (100+ agents per hour). Use `changed: false` and record the failure reason in `result`.**

### 2. Compare Results

For each item, compare the new result with `lastResult`:
- **Changed** → record `changed: true`
- **Unchanged** → record `changed: false`

**Deduplication rule — avoid false positives:**
Compare the semantic meaning, not the exact wording. If the status, counts,
and latest activity are all the same as `lastResult`, mark `changed: false`
even if your phrasing differs.

Examples of FALSE positives to avoid:
- lastResult: "Page shows 2 open items. Latest update: config section edited."
- newResult: "Still 2 open items. Config section was edited (already noted)."
→ Same state. The edit was already recorded. Mark `changed: false`.

- lastResult: "3 new messages in the channel, latest from the release thread."
- newResult: "3 new messages, latest from the release thread, plus 1 already-seen reply."
→ If that reply was already present last time, this is unchanged.

Rule of thumb: if you can't point to a specific NEW event (new message from someone else,
status transition, new result appearing) that wasn't in `lastResult`, it's unchanged.

**Slack channel special rule:**
For `kind: slack-channel`, `changed` should ALWAYS be `true` (channels are always active).
But this does NOT mean you should always notify. The `changed` flag just means "update lastResult".
The notify decision is separate — see Step 5's UNIVERSAL DEDUP RULE.

### 3. Determine Completion (autoComplete)

If `autoComplete` is true, check terminal conditions:

| Kind | Terminal condition (→ done) |
|------|---------------------------|
| url / custom | Agent judges based on triggerCondition |
| slack-channel | Never auto-complete (long-term subscription) |
| slack-topic | Agent judges — if 3+ consecutive checks return no results, suggest stopping to user |

- If unsure, leave as `watching` and ask the user via options

### 4. Batch Update

Call `update_watchlist` ONCE with all updates batched.

**Dynamic frequency adjustment:** Adjust `checkIntervalMins` based on change patterns, but **never faster than the cadence the user asked for**. That cadence is on the item as `baseIntervalMins` (older items may not have it — then treat the item's current `checkIntervalMins` as the base and do not speed it up at all).

- **Something changed** → reset to `baseIntervalMins`. NOT to some fixed few minutes: "watch this flight daily" means daily, and the moment the price moves is exactly when a hardcoded reset would start polling it every few minutes for the rest of the day.
- **No change** → back off from the current interval using Fibonacci-style steps, never below `baseIntervalMins` and never above 180 minutes:
  - current ≤ 5 → 10; ≤ 10 → 15; ≤ 15 → 25; ≤ 25 → 40; ≤ 40 → 65; ≤ 65 → 105; ≤ 105 → 170; ≤ 170 → 180
  - current > 180 → keep as-is (the user set a longer cadence deliberately; do not override)
  - If a step would land below `baseIntervalMins`, use `baseIntervalMins`.

**Exception**: `slack-channel` and `slack-topic` kinds — do NOT adjust checkIntervalMins.
These are user-configured subscription frequencies, not adaptive polling. Keep the interval as-is.

```
update_watchlist({ update: [
  {
    id: '<item-id>',
    lastResult: '<new status summary>',
    checkCount: <previous + 1>,
    failCount: <0 if success, previous + 1 if failed>,
    status: '<done if terminal, watching otherwise>',
    historyEntry: { result: '<summary>', changed: <true/false> },
    checkIntervalMins: <baseIntervalMins if changed, otherwise next back-off step (never below baseIntervalMins, cap 180)>,
    notified: <true if you will call perform_pet_action to notify, false/omit otherwise>
  },
]})
```

### 5. Notify (only if something changed)

**No change → do NOT notify. Just update_watchlist and move to step 6.**

**⚠️ UNIVERSAL DEDUP RULE (applies to ALL item kinds — url, slack, custom):**

Before pushing ANY notification to the user, you MUST check whether the user has already seen this information:

1. Read `read_mochi_file({ which: "activity" })` — scan the last 20 entries
2. Find any `notification` entry within the last 60 minutes that mentions the same item (by label or target)
3. Compare what you're about to say vs what was already communicated:
   - If the core facts are the same (same status, same counts, same alerts) → **DO NOT notify**
   - Only notify if there is genuinely NEW information the user hasn't seen yet
4. When you do notify, include ONLY the new delta — not a full re-summary of everything

Examples:
- Last notification said "docs page unchanged, 2 open items" → still unchanged, 2 open → **SKIP**
- Last notification said "page had 2 open items" → now 3 → notify "1 new item added"
- Last notification said "channel quiet" → still quiet → **SKIP**
- Last notification said "site returned 503" → now returns 200 OK → notify "site recovered ✅"
- Last notification said "3 matches for the topic" → still 3 → **SKIP**
- Last notification said "3 matches for the topic" → now 1 dropped off, 1 new → notify "1 new result: <title>"

**If in doubt, don't notify.** The user can always check the watchlist panel. Silence is better than spam.

**Something changed or terminal state reached:**
Use `perform_pet_action({ action: "notify", summary: "...", pushToChat: true, watchItemId: "<item-id>" })`.

⚠️ **CRITICAL: You MUST pass `notified: true` in the update_watchlist call (step 4) AND `watchItemId` in perform_pet_action. This prevents the system from sending duplicate notifications.**

| Situation | Action |
|-----------|--------|
| High priority + changed | `perform_pet_action({ ..., sticky: true, pushToChat: true, watchItemId: "<id>", mood: "curious" })` |
| Normal priority + changed | `perform_pet_action({ ..., pushToChat: true, watchItemId: "<id>" })` |
| Low priority + changed | No notification (user sees in daily briefing). Do NOT set `notified: true` in update_watchlist. |
| Terminal (done/failed) | MANDATORY: `perform_pet_action({ action: "notify", summary: "<label> is done/failed: <reason>", pushToChat: true, watchItemId: "<id>", mood: "happy" })` |
| Nothing changed | **No notification. Proceed to step 6 with summary.** |

⚠️ **CRITICAL: When marking an item as done or failed, you MUST call perform_pet_action with pushToChat: true and watchItemId AFTER update_watchlist (with notified: true). The user needs to know WHY it was marked done. Never silently mark done without notifying.**

**Proactive suggestions** (only when meaningful, via perform_pet_action notify with watchItemId):
- A watched URL has returned an error for 3+ consecutive checks → `perform_pet_action({ action: "notify", summary: "<label> has been failing to load — the target may be down", pushToChat: true, watchItemId: "<id>", mood: "scared" })` — **only once**. After notifying, add `"nudged": true` to the item's notes via update_watchlist. Do NOT nudge again if notes already has `"nudged": true`.
- 3+ checks no change → Do NOT notify. The user already knows it's being watched. Silence is fine.

**⚠️ DEDUP RULE: Never push the same or substantially similar notification to chat twice. Before calling perform_pet_action with pushToChat, ask yourself: "Did I already tell the user this exact thing?" If the status hasn't changed since the last notification (check `lastResult` vs new result), do NOT notify again. Only notify on NEW information the user hasn't seen.**

### 6. Finish

After all tool calls, you're done. No further action needed.

## Tool Restrictions

Allowed tools:
- `update_watchlist` — update check results
- `perform_pet_action` — notify the user (only when something changed)
- `read_mochi_file` — the activity log, for the dedup rule above
- `web_fetch` — fetch any public URL to check its status
- Slack READ tools, if the user has connected a Slack server. Names vary between servers: find the one that reads a channel, the one that reads a thread, and the one that searches, from the tools you actually have.

⛔ Slack write tools are forbidden — Mochi must NEVER send messages or modify anything on Slack.
⛔ Do NOT read DM channel content — skip DM channels entirely.

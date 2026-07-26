---
name: babysit
description: Same-session monitoring loop for PRs, CI runs, tickets, and deployments using the monitor_start / monitor_update / autonudge_stop MCP tools. The loop re-injects your check instructions into THIS session on an idle interval — same context, same tools — and works from dashboard chat, Slack threads, and Discord DMs. Use when the user says "babysit", "monitor", "keep checking", "keep an eye on", "loop on this PR", "let me know when", or wants polling that outlives a wait+poll window. NOT for fresh-session work (use cron_add) or external-system callbacks (use register_hook).
tags: [skill, kirocrew, monitor, babysit, autonudge, loop]
---

# Babysit (same-session monitoring loop)

## Overview

`monitor_start(message, interval_secs?, max_cycles?)` binds a monitoring loop
to **your current session**. After each of your turns completes and the
session sits idle for `interval_secs`, the message is re-injected as your
next turn. You keep the full conversation context, memory, and tools on every
cycle. Loops persist to `~/.kiro/crew/autonudge.json` and survive gateway
restarts.

Works from:

| Surface | Binding | Cadence |
|---|---|---|
| Dashboard chat | bare slot key | idle timer (re-armed after every turn) |
| Slack thread | `slack:<thread_ts>` | fixed interval after each unattended turn |
| Discord DM | `discord:{agent}:direct:{user}` | fixed interval after each unattended turn |

`autonudge_stop(reason?)` stops the loop bound to the current session from
any of those surfaces. `monitor_update(message?, interval_secs?, max_cycles?)`
revises the loop already bound to this session in place, keeping its cycle
count — use it when the instruction you armed has gone stale, or to raise the
cap on a loop that is still doing useful work.

### `interval_secs` is an idle gap, not a period

The timer arms when your turn **ends**, so the real cadence is
`interval_secs` + however long each cycle's work takes. A 300s interval with
5-minute checks wakes you roughly every 10 minutes. Size it for the gap you
want *between* cycles.

### You must stop the loop yourself

`max_cycles` (default 24) is a **runaway backstop, not a finish line**. A loop
that coasts into its cap did not complete — it ran out of rope, and whatever
it was watching is still unresolved. Real loop stores show this is the common
failure: two live babysit loops ended at exactly 24/24 and 20/20 delivered
cycles, neither having called `autonudge_stop`. Evaluate the exit condition
every single cycle and stop deliberately.

### Context grows every cycle

Each cycle appends a full turn — tool calls, CI output, diffs — to the **same**
session. That shared context is the point of a same-session loop, but nothing
bounds it: long babysits walk into compaction, which can summarise away the
very instructions the loop keeps re-injecting. Keep per-cycle output minimal.

### An arm failure is not a flaky connection

If `monitor_start` reports it could not arm a loop, no monitoring is running.
That message is distinct from the transient MCP reconnects you retry through —
do not write it off as flakiness. Fall back to an in-turn `wait`+poll loop and
tell the user monitoring is not active.

## Decision table

- User is waiting and total time < 30 min → `wait` + poll, no loop.
- "Babysit / monitor / keep checking" in THIS conversation → `monitor_start`.
- Work belongs in a fresh isolated session each cycle → `cron_add`.
- External system will call back → `register_hook`.
- Non-session context (cron/webhook) needs monitoring → HEARTBEAT.md task.

## Workflow

1. **Write the message as instructions to your future self.** Include:
   - what to check (PR URL, job id, ticket),
   - what to do with findings (fix + push, summarize, escalate),
   - the exit condition, ending with: "when met, tell the user and call
     `autonudge_stop`".
2. **Call `monitor_start`.** `interval_secs` default 300 suits CI/review
   polling. `max_cycles` defaults to 24 (≈2h of idle gaps at 300s); raise it
   for longer work, and pass `0` for unlimited only when the user explicitly
   asks for an unbounded loop.
3. **Tell the user monitoring is active and END YOUR TURN.** The loop wakes
   you — do not wait+poll on top of it.
4. **Each cycle:** do the check, act, and report only real signals. Don't
   post "nothing new" every cycle. If the instruction no longer matches
   reality, `monitor_update` it rather than working around it.
5. **On the exit condition** (or the user saying stop): report, then call
   `autonudge_stop` with a reason. Do not let the cap do this for you.

## Example

User: "babysit PR #247 until it's review-ready"

```
monitor_start(
  message="Check PR #247: CI checks and review-bot comments. Fix legitimate
           High/Medium findings and push (single commit, force-with-lease).
           Rebut false positives. When all checks are green and every thread
           is resolved, tell the user the PR is review-ready and call
           autonudge_stop.",
  interval_secs=300,
  max_cycles=20,
)
```

## Rules & gotchas

- **One loop per session** — a new `monitor_start` replaces the existing loop.
- **Busy sessions skip a cycle** (never queue) — a long-running turn delays
  the next check to the following interval; skipped cycles don't count
  toward `max_cycles`.
- **Unattended turns are bounded to 30 min** on Slack/Discord; keep each
  cycle's work small and incremental.
- **Slack/Discord loops auto-approve tools** on the unattended turn
  (Slack always; Discord follows the gateway approval mode — under
  interactive approval a Discord cycle cannot use tools, so prefer
  dashboard/Slack for tool-heavy babysitting or run the gateway with
  `--approval yolo`/`auto`).
- **Kill switches:** `autonudge_stop` (preferred), the dashboard 🎯 popover
  (dashboard loops), `max_cycles`, or the per-loop STOP sentinel file.
- Loops fire `[auto-nudge cycle N]`-tagged messages — treat them as your own
  scheduled wake-ups, not user input.

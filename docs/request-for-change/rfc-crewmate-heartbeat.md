---
title: Crewmate heartbeat -- an opt-in wake that keeps the crewmate working on its own, quietly by default
status: accepted
author: buluoray
created: 2026-10-15
last-audited: 2026-10-15
audited-at: 8da5b41552
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crewmate heartbeat

- Status: accepted. This is the product owner's decision, given 2026-10-15 by
  rayrayxu. Claims about today's code were checked at `8da5b41552` (main).
- Author: buluoray
- Related: [rfc-crewmate-goals.md](rfc-crewmate-goals.md) § 3.5 (check-ins, and
  which turns may raise a card), [rfc-crewmate-dynamic-dashboard.md](rfc-crewmate-dynamic-dashboard.md)
  (the crewmate's Dashboard tab), [rfc-crewmate-dashboard-upkeep-skill.md](rfc-crewmate-dashboard-upkeep-skill.md)
  (how the crewmate keeps that page current).

## 1. Summary

A crewmate gets a **Heartbeat** switch, off by default. When it is on, the
gateway wakes the crewmate in its own thread every chosen interval (30 minutes
by default). On each wake the crewmate looks for whatever it can move forward
toward its goals, one thing or many, and does it.

Doing work and telling the user are separate. A heartbeat wake is quiet by
default: the crewmate records what it did where the user already looks, its
Dashboard tab and its work ledger, and ends without a message. It writes in the
chat only when the user would lose something by not knowing now.

The switch sits in the Goals tab's check-ins section, beside the Daily Goal
Reminder, Weekly report and Monthly review.

## 2. Problem

A crewmate today works only when something wakes it: a user message, a
sub-agent result, a cron job, or one of the three check-ins. The check-ins run
at most once a day, so between them a crewmate with a goal and open work sits
idle until the user comes back and asks.

The user then has to drive every step. A crewmate that already knows its goals
and its tracked work should be able to keep going on its own, the way an
employee does, and report the way an employee does: rarely, and only what
matters.

## 3. Proposal

### 3.1 The switch

| | |
|---|---|
| Where | Goals tab, check-ins section: a full-width **Heartbeat** row under the three tiles (Daily, Weekly, Monthly) |
| Default | off |
| Interval | 15 min, 30 min (default), 1 h, 2 h, 4 h |
| Needs | at least one goal, the same rule as the other check-ins |
| Who turns it on | the user, from the row; or the crewmate proposes it with a `set_check_ins` card and the user presses Apply. The crewmate cannot turn it on itself |

The heartbeat is a fourth cadence in the goal record's `schedules`, next to
`daily`, `weekly` and `monthly` (`member_goals.CADENCES`). It keeps one
gateway-owned cron job through the same installer key scheme,
`goal-heartbeat:<slug>` (`dashboard/goal_reminder.py` `MANAGED_PREFIXES`), so it
gets the cron store's persistence and timezone handling, is hidden from the
Schedule page like the other check-ins, and is removed with the crewmate
(`crew_removal._remove_goal_check_ins`). Its cron expression comes from the
interval: `*/15 * * * *`, `*/30 * * * *`, `0 * * * *`, `0 */2 * * *`,
`0 */4 * * *`.

### 3.2 A wake

The wake goes through the check-ins' existing `fire` path and dispatches one
hidden turn in the crewmate's pinned thread (`mate_welcome.dispatch_hidden_turn`,
`human_seen=False`). It inherits that path's skips, so a heartbeat does nothing
that time when the thread is not open, a turn is running or queued, the switch
is off, or no goal is left. A wake never interrupts the user or the crewmate.

The prompt carries the goal block (`member_goals.progress_block`) and tells the
crewmate to:

1. Look at its goals, milestones and the open items in its work ledger.
2. Do whatever it can move forward without the user: one thing or many, as the
   work needs. Long or heavy work goes to a worker session (`session_create`,
   then `session_send`), never a sub-agent, so the thread stays free and the
   user can see the work in the sidebar and take it over. A later wake reads the
   worker's progress (`session_read_message`) rather than starting the same
   work again.

   Every crewmate carries the conductor's session tools and its guidance for
   running work across sessions, whatever template it was made from. Today only
   conductor agents have them (`agent.py`). A crewmate whose template lacks them
   gets them through its private agent copy, the same way a missing
   `kirocrew-mate` server is added at creation
   ([#20611](https://github.com/kirodotdev/KiroCrew/pull/20611)), and the
   heartbeat relies on them. A crewmate whose harness cannot load a private
   copy keeps heavy work for its next wake and does it in small steps in its
   own thread.
3. Record the result where the user looks (§ 3.3).
4. Decide whether the user needs a message (§ 3.4). If not, end the turn with
   `nothing_to_do`.

When there is nothing to move forward, the wake ends with `nothing_to_do` and
posts nothing.

### 3.3 Quiet work shows on the Dashboard tab

The user should learn to read the crewmate's **Dashboard tab** to see what it is
doing, not the chat and not the Goals tab. Quiet work lands in the places that
page already draws from:

| What the crewmate did | Where it is recorded | Shown on |
|---|---|---|
| moved a tracked item forward, finished it, or got stuck | work ledger | Dashboard tab (fold-backed fields update themselves) |
| fields the page asks the crewmate to fill | `dashboard_write` | Dashboard tab |
| took a reading of a milestone | `key_result_progress` | Goals tab milestone |

The Goals tab stays about goals: it shows no activity list and no record of
what each heartbeat did.

### 3.4 When the crewmate writes in the chat

Only in three cases:

1. **Blocked.** It cannot go on without the user's decision.
2. **Done.** Something the user asked for is finished.
3. **Surprise or risk.** Something the user would want to hear about now, such as
   a broken build or a security problem.

Everything else waits for the Weekly report. Before writing, the crewmate asks
itself one question: would the user lose anything by not knowing this now? If
not, it stays quiet.

### 3.5 Asking the user

The heartbeat prompt states the rule, and nothing else enforces it:

- Ask about one thing once. Never ask again, and never ask the same thing in
  other words.
- Ask with an `[OPTIONS:]` line, never with an `ask_question` card.
- When the user does not answer, treat it as "not now". If the step can be
  undone, go ahead with the safe default and note it on the Dashboard tab; if it
  cannot, leave it and record it on the Dashboard tab as waiting on the user.

The crewmate keeps to this from its own thread's history. A record of asked
questions kept by the gateway is out of scope (§ 5).

### 3.6 Cost

Each wake that runs is a model turn: at 30 minutes, up to 48 a day for one
crewmate. The switch is off by default, a wake that finds nothing ends at once
with `nothing_to_do`, and a wake is skipped while the thread is busy. The tile
says the interval and that each wake uses credits.

## 4. UI

```
Check-ins
 ┌───────────┐ ┌───────────┐ ┌───────────┐
 │ Daily     │ │ Weekly    │ │ Monthly   │
 │ 09:00     │ │ Mon 09:00 │ │ day 1     │
 │ On        │ │ Off       │ │ Off       │
 └───────────┘ └───────────┘ └───────────┘
 ┌───────────────────────────────────────┐
 │ Heartbeat  every 30 min, finds work   │
 │            on its own            Off  │
 └───────────────────────────────────────┘
```

The three tiles keep their current width. The heartbeat gets its own row under
them, because it is a different kind of thing: the three tiles report on the
work, and the heartbeat does the work. The row carries one line saying what it
does, so the user does not need to learn the word first.

The first page shows state only. Clicking a tile or the row opens the check-ins
second page, where each switch, the times and the heartbeat interval are set.

## 5. Non-goals

- A gateway record of questions the crewmate asked and the user did not answer.
  The rule lives in the prompt (§ 3.5). If repeated asking shows up after a
  compaction or in a new session, that record is the next step.
- An activity list on the Goals tab.
- A heartbeat for sessions that are not crewmates.
- Proactive messages on messaging channels. The heartbeat runs in the
  crewmate's dashboard thread only.

## 6. Existing crewmates

The switch is off for every crewmate, new and existing. A goal record written
before this change reads with `heartbeat` off and the default interval.

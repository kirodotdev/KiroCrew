---
title: Crewmate heartbeat -- an opt-in wake that keeps the crewmate working on its own, quietly by default
status: accepted
author: buluoray
created: 2026-10-15
last-audited: 2026-10-21
audited-at: 85a081707c
doc-pr: 20934
implementation-prs: [21172]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crewmate heartbeat

- Status: accepted. See the history below for what was accepted when.
- Audit: every claim about code was checked on 2026-10-21. Claims about main
  were checked at `85a081707c`. Claims about the implementation were checked
  against [#21172](https://github.com/kirodotdev/KiroCrew/pull/21172), first at
  `d433ae0f39` and then in the commit that carries this line with the code it
  describes. § 7 lists each claim under the one it holds for.
- History:

  | Date | What happened |
  |---|---|
  | 2026-10-15 | The product owner (rayrayxu) accepted the first text (`6249176ee1`): intervals 15, 30 (default), 60, 120 and 240 minutes, and every crewmate given the conductor's session tools through its private agent copy. |
  | 2026-10-20 | The text was revised (`e9afde0fa9`) and merged in [#20934](https://github.com/kirodotdev/KiroCrew/pull/20934) on 2026-10-21. The revision moved the intervals to 30, 60 (default), 120 and 240 minutes (§ 3.1, § 3.6). It dropped the promise that every crewmate gets the session tools: a wake uses them only when the thread already holds them (§ 3.7, new). It rewrote § 3.2 (a stopping skip, folding pre-tool text, a gateway line once every milestone is met) and § 3.6 (cost as a ceiling on wakes a day). No review or comment on #20934 recorded acceptance of this revision before the merge. |
  | 2026-10-21 | After bolichen97's post-merge review ([comment](https://github.com/kirodotdev/KiroCrew/pull/20934#issuecomment-6308582968)), the product owner (rayrayxu) kept the status `accepted` for the revised text, with this change. This change also matches § 3.2 to #21172 as it stands: the gateway folds no text and adds no milestones line, and a wake skips while a monitor loop drives the thread. It defines what follows a declined or expired milestones card (§ 3.2), splits the facts (§ 7) and moves every audit to the two commits named above. |
  | 2026-10-21 | With #21172's review fixes, § 3.2 adds that a wake proposes no new milestones while a milestones card waits on the user, checks a stopping gateway once (after its reads), and reads the monitor-loop skip from `ledger_wake.worker_looping`. Acceptance of this addition is not recorded yet. |
- Author: buluoray
- Related: [rfc-crewmate-goals.md](rfc-crewmate-goals.md) § 3.5 (check-ins, and
  which turns may raise a card), [rfc-crewmate-dynamic-dashboard.md](rfc-crewmate-dynamic-dashboard.md)
  (the crewmate's Dashboard tab), [rfc-crewmate-dashboard-upkeep-skill.md](rfc-crewmate-dashboard-upkeep-skill.md)
  (how the crewmate keeps that page current).

## 1. Summary

A crewmate gets a **Heartbeat** switch, off by default. When it is on, the
gateway wakes the crewmate in its own thread every chosen interval (an hour by
default). On each wake the crewmate looks for whatever it can move forward
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
| Interval | 30 min, 1 h (default), 2 h, 4 h (§ 3.6 says why there is no shorter one) |
| Needs | at least one goal, the same rule as the other check-ins |
| Who turns it on | the user, from the row; or the crewmate proposes it with a `set_check_ins` card and the user presses Apply. The crewmate cannot turn it on itself |

The heartbeat is a fourth cadence in the goal record's `schedules`, next to
`daily`, `weekly` and `monthly` (`member_goals.CADENCES`). It keeps one
gateway-owned cron job through the same installer key scheme,
`goal-heartbeat:<slug>` (`dashboard/goal_reminder.py` `MANAGED_PREFIXES`), so it
gets the cron store's persistence and timezone handling, is hidden from the
Schedule page like the other check-ins, and is removed with the crewmate
(`crew_removal._remove_goal_check_ins`). The job is named `Heartbeat: <name>`.
Its cron expression comes from the interval: `*/30 * * * *`, `0 * * * *`,
`0 */2 * * *`, `0 */4 * * *`.

### 3.2 A wake

The wake goes through the check-ins' existing `fire` path and dispatches one
hidden turn in the crewmate's pinned thread (`mate_welcome.dispatch_hidden_turn`,
`human_seen=False`). It inherits that path's skips, so a heartbeat does nothing
that time when the thread is not open, a turn is running or queued, the switch
is off, or no goal is left. It adds two skips of its own. A wake that finds the
gateway stopping after its own reads (`kiro_crew.shutdown_event` set) starts no
turn, since the stop would cancel a turn that had already been paid for; the
cron timer itself stops firing once the event is set. A wake that fires while a
monitor loop that will fire again is bound to the thread starts no turn either:
between two loop cycles the thread is idle, and the loop already drives that
work. That answer is the work ledger's own (`ledger_wake.worker_looping`). A wake never interrupts
the user or the crewmate.

The prompt carries the goal block (`member_goals.progress_block`). Right under
it, the prompt asks for `nothing_to_do` at once, with no text before it, when
nothing can move forward. It then tells the crewmate to:

1. Look at its goals, milestones and the open items in its work ledger.
2. Do whatever it can move forward without the user: one thing or many, as the
   work needs (§ 3.7 says who does it). Met milestones, or work that waits on
   the user, still leave a step that needs no one: the next improvement, new
   milestones proposed with `set_key_results`, a draft the user can review and
   undo. Only a step that cannot be undone or that reaches outside (a push, a
   merge, a release, a message to someone else) waits for the user, recorded
   on the Dashboard tab.
3. Record the result where the user looks (§ 3.3).
4. Decide whether the user needs a message (§ 3.4). If not, end the turn with
   `nothing_to_do`.

When there is nothing to move forward, the wake ends with `nothing_to_do` and
posts nothing. The gateway does not fold or hide what a wake writes: text the
crewmate writes before a tool call or before `nothing_to_do` shows in the chat
like any reply. That is why the prompt asks for the silent end first. Hiding a
wake's narration is left for later, if wakes turn out to be noisy. It would
hide rows when the chat draws them, the way the chat already hides tool rows,
not rewrite stored rows.

Met milestones are one case of step 2: the next milestones on a
`set_key_results` card are a step that needs no one. The gateway does not judge
whether a milestone is met. It changes the prompt in one case only: while a
milestones card the crewmate proposed is still pending or applying in the
thread, step 2 drops new milestones and the prompt says that card already
waits on the user. So a wake does not propose a second card the user has not
answered, and does not spend credits re-asking. A card store that cannot be
read counts as no card.

#### After a milestones card

A `set_key_results` card ends like any change card. The heartbeat adds no rule
of its own to either ending:

| The card | What happens |
|---|---|
| Declined (the user presses Cancel) | The card becomes `cancelled`. The milestones stay as they were. |
| Not pressed for 24 hours (`CARD_TTL_SECONDS`) | The card becomes `expired` the next time the card store reads or sweeps it. The milestones stay as they were. |

In both cases the crewmate hears the ending once, at the start of its next turn
in that thread (a heartbeat wake counts): the `[CHANGE CARD RESULTS]` block
names the card and its status (`take_unreported`, `render_change_card_results`).
The next wake's prompt is built from the unchanged record, so it may propose
milestones again. Nothing in the gateway delays or blocks that, and the prompt
says nothing about a declined card. The "ask once" rule in § 3.5 is about
`[OPTIONS:]` questions, not cards.

The next wake is itself the patrol over the work the heartbeat started, so a
heartbeat turn arms no monitor loop.

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

Each wake that runs is a model turn and uses credits. How many credits a turn
costs is not something the heartbeat can see or control, so the heartbeat does
not promise a cost. What it does say is how often a crewmate can wake: each
interval sets a ceiling on wakes a day, and the user picks the ceiling.

| Interval | At most, per crewmate |
|---|---|
| 30 minutes (the floor) | 48 wakes a day |
| 1 hour (the default) | 24 wakes a day |
| 2 hours | 12 wakes a day |
| 4 hours | 6 wakes a day |

| | |
|---|---|
| Default | off, for every crewmate, new and existing |
| Fewer wakes than the ceiling | a wake is skipped when the thread is busy or closed, the switch is off, there is no goal, an active monitor loop is bound to the thread, or the gateway is stopping |
| Cheapest wake | one that finds nothing ends at once with `nothing_to_do` |
| Where it is said | the check-ins page under the switch ("Each wake uses credits."), the `crewmate.check_ins` card's row ("Heartbeat (each wake uses credits)"), the `set_check_ins` tool description, and the offer after the milestones are confirmed |

Quiet wakes keep the thread short too. A quiet end with no text before it
writes one tool row and no chat message, so a day of quiet wakes adds little to the history the
next wake reads.

### 3.7 Who does the work

The heartbeat ships on its own, on today's crewmate: its chat does its own
work. There is no separate conductor and no dedicated worker behind it; the
heartbeat relies only on what a crewmate's thread already holds.

| The crewmate's thread | How a wake moves long or heavy work |
|---|---|
| holds the `session_*` tools (its member backend mounts session control per session and the user has not switched `kirocrew-dashboard` off) | the same way the crewmate's operating-mode block already asks: a worker session (`session_create`, then `session_send` with a self-contained brief), never a sub-agent, so the thread stays free and the user can see the work in the sidebar and take it over. A later wake reads the worker's progress (`session_read_message`) rather than starting the same work again |
| does not hold them | in small steps, one piece per wake, with a note in the work ledger saying where it stopped, so the next wake picks it up there |

Which row applies is the context builder's own answer for a member turn with no
live session evidence (`goal_reminder.member_has_session_tools`); any failure
reads as the second row, which works with or without the tools.

A dedicated per-crewmate worker, a worker config the crewmate can grow, and
moving all of a crewmate's work out of its own chat are a separate change. When
that lands, only the first row's wording changes; the switch, the schedule, the
quiet end and the cost rules above stay as they are.

## 4. UI

```
Check-ins
 ┌───────────┐ ┌───────────┐ ┌───────────┐
 │ Daily     │ │ Weekly    │ │ Monthly   │
 │ 09:00     │ │ Mon 09:00 │ │ day 1     │
 │ On        │ │ Off       │ │ Off       │
 └───────────┘ └───────────┘ └───────────┘
 ┌───────────────────────────────────────┐
 │ Heartbeat  every 1 h, finds work on   │
 │            its own               Off  │
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

## 7. Facts and where they hold

### On main at `85a081707c`

| Fact | Where |
|---|---|
| Three check-in cadences: daily, weekly, monthly. No heartbeat cadence | `member_goals.CADENCES` |
| One gateway-owned cron job per cadence, keyed `goal-<cadence>:<slug>` | `dashboard/goal_reminder.py` `MANAGED_PREFIXES` |
| `GET /api/crons` (the Schedule page) hides these jobs | `dashboard/goal_reminder.py` |
| A check-in fires through `fire`, and skips when the thread is not open or a turn is running or queued | `goal_reminder.fire`, `_idle_thread` |
| A check-in is one hidden gateway turn in the crewmate's pinned thread | `mate_welcome.dispatch_hidden_turn` |
| The goal block in the prompt | `member_goals.progress_block` |
| `nothing_to_do` is refused only on a turn a person or a channel opened | `session_directive_apply_slot._nothing_to_do` |
| A crewmate's own goal, milestones and check-ins cards are allowed on any turn in its own thread | `handlers.guide.REQ_OWN_GOAL_TURN` |
| The crewmate tools the wake uses: `set_check_ins`, `set_key_results`, `key_result_progress`, `dashboard_write` | `mcp_mate.py`, `mcp_panel.py` |
| Whether a member turn may be taught the session tools | `context_assembly.member_gates._member_dispatch_held` |
| A card not pressed for 24 hours expires; a cancelled or expired card is reported once on the thread's next turn | `change_cards_records.CARD_TTL_SECONDS`, `REPORTED_STATUSES`; `change_cards.CardStore.take_unreported`; `chat_turn/turn_dispatch_gates.py` |
| A monitor loop can be looked up by its thread | `autonudge.AutoNudgeService.get_by_slot` |
| Crew removal removes a crewmate's check-in jobs | `agent_admin/crew_removal._remove_goal_check_ins` |
| The gateway's stop signal | `kiro_crew.shutdown_event` |

### Added by #21172 only (not on main)

| Fact | Where |
|---|---|
| The `heartbeat` cadence and its `interval`: 30, 60 (default), 120 or 240 minutes | `member_goals.CADENCES`, `HEARTBEAT_INTERVALS`, `DEFAULT_HEARTBEAT_INTERVAL` |
| The job `goal-heartbeat:<slug>`, named `Heartbeat: <name>`, on `*/30 * * * *`, `0 * * * *`, `0 */2 * * *` or `0 */4 * * *` | `goal_reminder.cron_expr_for` |
| The wake prompt, with the silent-end line under the goal block | `goal_reminder.heartbeat_prompt` |
| No new milestones while a milestones card waits on the user | `goal_reminder.milestones_card_waiting` |
| The stopping skip, once, after the wake's reads | `goal_reminder.gateway_stopping` |
| The monitor-loop skip | `goal_reminder._monitor_loop_drives`, through `ledger_wake.worker_looping` |
| Worker session or small steps | `goal_reminder.member_has_session_tools`, through `agent_sdk.agent_specs.session_mcp_server_is_disabled` |
| The credits wording: "Each run uses credits." on the check-ins page; "Heartbeat (each run uses credits)" on the card | `en.json` `goals_heartbeat_credits`; `change_card_catalog.py` |
| The Heartbeat in the offer after milestones are confirmed | `goal_setup.py` |
| The Goals-tab Heartbeat row and interval picker | `website/src/pages/members/CrewGoals.tsx` |

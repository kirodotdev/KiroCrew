---
title: Crewmate goals — a long-term goal, short-term tracking, and a daily reminder
status: accepted
author: buluoray
created: 2026-10-12
last-audited: 2026-10-12
audited-at: d866dd2607
doc-pr:
implementation-prs: []
tracking-issues: [19240]
supersedes: []
superseded-by: []
---

# RFC: Crewmate goals

- Status: accepted. This is the product owner's decision, given 2026-10-12 by
  rayrayxu. Claims about today's code were checked at `d866dd2607` (main).
- Author: buluoray
- Related: [#19240](https://github.com/kirodotdev/KiroCrew/issues/19240) (the
  crewmate's own MCP server, `set_goal`), [#19258](https://github.com/kirodotdev/KiroCrew/pull/19258)
  (`update_self`), [rfc-crewmates-launch.md](rfc-crewmates-launch.md) § 02.

## 1. Summary

A crewmate gets a **goal** that is separate from its **About**:

- **About** says who the crewmate is. The crewmate manages it itself.
- **Goal** says what it is working toward: one **long-term goal**, and the
  **short-term goals** it is doing now to get there, each tracked as open or done,
  and up to three **key results** that say how far it is from the goal. The user changes both directly. The crewmate manages its
  short-term goals itself, with no sign-off, and proposes a new long-term goal
  through a change card the user confirms.
- Three switches on the Goals tab: a **Daily Goal Reminder** that keeps the
  crewmate on its goal, a **Weekly report** and a **Monthly review** for the
  user. Reports are shown on the crewmate's Dashboard side panel and saved as
  artifacts.

The Goals tab on the profile, which today is a "coming soon" card, shows the
long-term goal and its short-term goals.

## 2. Problem

Today one field does two jobs, and a third place that sounds like a goal stores
nothing.

| Where | What it does today |
|---|---|
| New crewmate card, Advanced: "What it looks after" | writes the crew config's `description` |
| A `crewmate.create` change card (a crewmate made from chat) | posts its `goal` as the crew config's `description`, and its optional `schedule` sends that goal as the job's message |
| Profile, About | shows the same `description` |
| Profile, Goals tab | a placeholder card, `profile_goals_soon` |
| The crewmate's first message | asks the user for its goal; the answer stays in the chat and is written nowhere |

So a person tells a new crewmate its goal, and the product loses it. Nothing on
the profile shows it, and nothing brings it back to the crewmate later. A goal
also has a shape a description does not: it changes over time, and it has parts
that get done.

## 3. Proposal

### 3.1 Two fields, two owners

| Field | Holds | Who changes it | How |
|---|---|---|---|
| About (`description`) | who the crewmate is, in a sentence or two | the crewmate; the user from the crew editor | `update_self` from #19258 |
| Long-term goal | what the crewmate works toward | the user directly; the crewmate by proposal | the Goals tab, or `set_goal` (a change card) |
| Key results | how the crewmate will know the goal is reached | the crewmate proposes them, the user confirms; the crewmate updates their progress directly | `set_key_results` (a change card), `key_result_progress` |
| Short-term goals | the steps toward it | the user and the crewmate, directly | the Goals tab, or the crewmate's short-term goal tools |

The card's Advanced field writes the **goal**, not the description, and is labelled
**Goal**. A create with no goal leaves it empty, and the crewmate's first message
asks for it as it does today. When the user answers, the crewmate proposes it with
`set_goal`, and the card saves it once the user confirms. A goal the user typed on
the card is saved as given.

A `crewmate.create` change card (`change_card_catalog.py`, the crewmate made from
chat) is the same: its `goal` writes the long-term goal into the goal record, not
`description`. Its optional `schedule` stays what it is today, the crewmate's own
work schedule, created as before.

### 3.2 What a goal holds

```text
goal
├─ long_term      one paragraph: what the crewmate is working toward
├─ key_results[]  how it will know it got there, at most 3
│  ├─ text      e.g. "PRs in a release have a description"
│  ├─ target    e.g. 27, or a word target such as "yes"
│  ├─ current   the crewmate's latest reading
│  ├─ updated_at
│  └─ readings[] every reading with its time, kept 90 days, for reports
└─ short_term[]   the steps it is taking toward it now, at most 20; at the cap a
                  new one evicts the oldest done or dropped item, and is refused
                  only when all 20 are open
   ├─ text
   ├─ status   open | done | dropped
   ├─ updated_at
   └─ updated_by   user | crewmate
```

It is stored as `trust/member-goals/<slug>.json` under the data home, a
gateway-only directory like the one the rules file lives in, and keyed like the
other member files (name-scoped, so a colliding slug never inherits another
member's goal). It is not beside `briefing.md`: the member's directory is
writable by the crewmate's own file tools, which would let it change its
long-term goal or key results without the user's button. Every write goes through
the gateway's routes and tools.

The record has the same protection as `member-rules/`: the tool gate keeps the
crewmate's file tools out of `trust/`, but the OS sandbox leaves `trust/`
writable, so a shell the crewmate spawns could still rewrite it. Sealing `trust/`
in the sandbox is an operator decision outside this RFC. Both the long-term goal and the short-term goals live in
this record, and neither is written to the protected `member-rules/` file: rules
are the user's safety boundary and stay out of the crewmate's reach, even through
a card. This replaces #19240's plan to write the goal into the rules file when a
`set_goal` card is applied; `set_goal` writes this record instead.

### 3.3 How the crewmate changes its goals

> **Amended 2026-10-13, Ray:** short-term goals are removed. The crewmate's work
> ledger (its tasks on the Crew board and the greeting card) already says what it
> is doing now, so a second list of current steps on the Goals tab only repeated
> it. `short_goal_add` and `short_goal_update` are gone, with the short-term
> undo row and the 20-item cap; a record that still holds a `short_term` list is
> read past and the list is dropped on its next write. A key result's reading is
> free text, a number or a short qualitative note, of up to 120 characters; a
> target stays at 40. The Goals tab labels key results **Milestones**; the
> code, the API and the tool names keep `key_results`.

On the `kirocrew-mate` server from #19258:

| Tool | Does | Sign-off |
|---|---|---|
| `goal_read` | returns the long-term goal and its key results | none |
| `set_goal(long_term)` | proposes a new long-term goal | a change card the user confirms (#19240) |
| `set_key_results(items)` | proposes the key results, replacing the current set | a change card the user confirms |
| `key_result_progress(id, current)` | records a new reading for one key result | none |
| `save_goal_report(kind, title, html \| markdown)` | files a weekly report or monthly review, as one self-contained page or as Markdown | none |

The long-term goal is what the user asked the crewmate to work toward, so the
crewmate cannot change it without the user's button. A reading is the crewmate's
own measurement and is recorded at once. The user edits the goal and the key
results on the Goals tab directly.

### 3.3a Key results come from the crewmate

The user says what the crewmate should work toward; the crewmate works out how to
tell when it got there. Once a long-term goal is set, the crewmate looks into the
work (the repo, the queue, the past releases it can read), then proposes one to
three key results with `set_key_results`, each something it can check itself and
report as a number or a plain yes/no. The card shows them; the user confirms,
edits or declines. A goal that cannot be measured gets no key results, and the
crewmate says so instead of inventing a number.

After that the crewmate keeps the readings current with `key_result_progress`
whenever it checks, with no sign-off. When the long-term goal changes, the
crewmate proposes a new set. The user can edit or remove a key result on the
Goals tab at any time.

### 3.4 Goals and work-ledger tasks

> **Amended 2026-10-13, Ray:** with short-term goals removed (§ 3.3), the work
> ledger is the one record of what the crewmate is doing now. The Goals tab does
> not link to it or list it. The vocabulary change below stands.

This section amends [rfc-crewmate-greeting.md](rfc-crewmate-greeting.md)'s
vocabulary: the "goal in flight" and "goals it left open" that the greeting reads
are work-ledger **tasks**. The greeting's behaviour is unchanged.

They stay separate. A work-ledger entry (what the Crew board and the greeting
card in [rfc-crewmate-greeting.md](rfc-crewmate-greeting.md) read) is one concrete
task the crewmate is running: fix a bug, watch a PR. It ends when the task ends.
The goal is what those tasks add up to.

- The greeting keeps reading the work ledger, and its user-facing copy says
  **task**, not goal, so the two never share a word. Today the only greeting
  string that says goal is `pages.membersPage.resume_failed` ("Could not read
  where {{name}}'s goal stands."); it becomes "…where {{name}}'s task stands."
- The weekly report and the monthly review read the tasks the crewmate finished
  in their period from its work ledger (§ 3.5).

### 3.5 Check-ins and reports

> **Amended 2026-10-13, Ray:** the Daily Goal Reminder takes a fresh reading of
> each key result and then carries on with the crewmate's next task; it no
> longer edits short-term goals. The Weekly report and Monthly review read the
> tasks the crewmate finished in the period from its work ledger (the items the
> Crew board shows); when none are listed there, the prompt asks the crewmate to
> summarise what it finished from its own work log and to say in the report that
> it did. The Dashboard side panel no longer shows the live goal picture: the
> goal, the key results and the reports are on the Goals tab only, and the
> Dashboard tab is the crewmate's own page.

Three cadences, each with one job, so the user is not told the same thing three
times. Each has its own switch on the Goals tab, off by default, and none can be
turned on while the crewmate has no long-term goal. Each switch writes one
gateway-owned schedule for that crewmate and turning it off removes it. One time
of day (default 09:00 in the user's timezone) serves all three; the weekly report
picks a weekday and the monthly review a day of the month.

| Cadence | For | What happens | Where the user sees it |
|---|---|---|---|
| **Daily Goal Reminder** | the crewmate | a hidden turn: take a fresh reading of each key result, then continue with the next task, ask only if blocked | one line in the thread, and the Goals tab updates |
| **Weekly report** | the user | how each key result moved this week (from its readings), the tasks finished this week (from the work ledger), what is stuck, the plan for next week | a report artifact, linked from one line in the thread and a notification |
| **Monthly review** | the user | whether the long-term goal and key results still fit the work; if not, the crewmate proposes new key results or a new goal through a change card, raised from the review turn itself | a report artifact like the weekly one, with any card in the thread |

**Check-ins are not ordinary schedules.** The three schedules live in the
gateway's cron store, so they keep its persistence, timezone handling and
missed-run behaviour, but they are internal: they are not listed on the
dashboard's Schedule page or on the crewmate's Schedules tab, and they are not
counted there. The Goals tab switches are their only control.

**How this relates to the schedules a crewmate already has.** The Daily Goal
Reminder is the only goal check-in. A `crewmate.create` card's `schedule` is the
crewmate's work schedule (when it runs its job), not a reminder, and an existing
one is left untouched; the check-ins never read, replace or remove it. A monitor
loop (`monitor_start`, auto-nudge) belongs to one session and one task and ends
with it, so it does not overlap a standing goal. A crewmate that already has a
work schedule and turns the Daily Goal Reminder on gets both: the job, and the
check-in on how the job is going.

**Which turns may raise a card.**

> **Amended 2026-10-13, Ray:** the Monthly-review-only exception is replaced by
> the rule below, so that the goal-setup flow (§ 3.8) can propose milestones
> from the turn its research comes back in.

A card normally needs a turn the user sent. A crewmate's own goal cards are the
exception: any turn in the crewmate's own pinned thread (a check-in, a
sub-agent's result coming back, the goal-setup nudge) may propose that
crewmate's `set_goal`, `set_key_results` and `set_check_ins` cards. A turn that
came from a messaging channel still cannot, every other card kind stays refused,
and `propose_change`, which names its crewmate in the request, stays user-turn
only. As always, nothing changes until the user presses it.

> **Accepted 2026-10-20 (product owner, rayrayxu):** a `capabilities` card whose
> additions are only servers this gateway already runs joins the three kinds above,
> so a worker report that names a missing capability can raise its card from a
> turn the user did not send; a registry install, a skill or a raw draft stays
> user-turn only.

This amends [rfc-crewmate-guides-and-mate.md](rfc-crewmate-guides-and-mate.md)'s
rule that a turn the user did not send gets `not_user_turn`, for these four card
kinds in the crewmate's own thread. It is acceptable because a card is a
proposal, not a change: the turn may have read untrusted content, but whatever it
proposes is shown to the user as a before-and-after card and does nothing until
they press Apply, and every other kind stays refused. The three goal kinds can
only name the crewmate's own goal, milestones and check-ins. The `capabilities`
case widens what the crewmate's worker can call, so it is held tighter: its
additions are limited to servers this gateway already runs (nothing is installed
or fetched), the grant stays bounded by `POLICY ∩ PROFILE` like any other, the
card shows the worker's grants before and after, an approval it would widen
keeps its acknowledgement, and a turn from a messaging channel is still
refused.

The weekly report says what moved; the monthly review asks whether the direction
is still right. Without the monthly step, key results set once would never be
revisited.

### 3.5a Where reports are shown

> **Amended 2026-10-13, Ray:** the Dashboard side panel shows no goal summary;
> reports are listed on the Goals tab only (§ 3.5).

- **Goals tab.** The **Reports** section lists the newest weekly reports and
  monthly reviews, each linking its artifact.
- **Report artifacts.** Each weekly report and monthly review is saved as its own
  artifact, filed under `Crewmates/<name>/Reports`, so past reports stay
  browsable and shareable. The thread and the notification link to it; the chat
  does not carry the report's full text.

> **Revision pending acceptance (2026-10-21, #22962):** a report is sent as
> `html` (one self-contained page) or `markdown`, one of the two. The printable
> page is the default the check-in prompts ask for: A4 sheets with a header band,
> numbered sections and a footer, the conclusion first, charts drawn from the
> readings, static and readable on paper. Markdown stays available for a plain
> report. A report opens in the same sandbox as the crewmate's Dashboard page,
> and its own scripts never run there: the page is a static snapshot.

### 3.6 The goal in the crewmate's context

> **Amended 2026-10-13, Ray:** the block carries the long-term goal and its key
> results; there are no short-term goals (§ 3.3).
>
> **Amended 2026-10-13, Ray:** the block carries each key result's text and
> target but not its readings, so the goal is not repeated on every turn. The
> member section is resent only when it changes, and a reading in it would
> change it on every progress update. The crewmate reads current values with
> `goal_read`, which each check-in already does first.

The long-term goal and its key results are injected after the rules and before
the briefing, so the crewmate always knows what it is working toward. The
section is sent again only after a compaction, on a new session, or when the
goal or its key results change.

### 3.7 The Goals tab

> **Amended 2026-10-13, Ray:** the tab holds the long-term goal, the key results
> (labelled **Milestones**), the three check-ins and the reports, and nothing
> else. It has two pages: the first only shows state, and every edit and detail
> is a page pushed over it, the way the Profile tab's About and Notes open.

- **First page**, one card each, its icon after its label:
  - the long-term goal; the card opens its editor;
  - the milestones, each row its latest reading in the crewmate's own words, what
    it is aiming for and when it was last read; a row opens that milestone's page
    (the latest reading, every earlier update, Edit and Remove). Empty until the
    crewmate's proposal is confirmed;
  - the **Daily Goal Reminder**, **Weekly report** and **Monthly review** as three
    tiles saying whether each is on and when it runs; the card opens the switches,
    the shared time, the weekday and the day of the month;
  - the **Reports**: the newest weekly reports and monthly reviews.
- A removed milestone comes back to the first page, which offers its Undo.

### 3.8 When the user sets a goal

> **Added 2026-10-13, Ray.** A user should not have to learn what milestones and
> check-ins are, or where to switch them on.

Saving a new long-term goal (on the Goals tab, or by applying a `crewmate.goal`
card) sends the crewmate one hidden turn in its own thread. An Undo putting an
older one back, and a small edit (a typo) send nothing. Removing a goal sends a
short turn of its own (_Amended 2026-10-14, Ray_): the crewmate acknowledges it,
stops work that only served it, and proposes dropping or relinking milestones
left serving no goal. In the setup
turn the crewmate:

1. says back what it understands the goal to mean, in a sentence or two;
2. starts one background sub-agent to research it (what the work looks like
   today, what could be measured, what is already in place), then ends its turn,
   so the chat stays free for anything else the user wants;
3. proposes milestones on a card when the research comes back.

Applying the crewmate's milestones card always sends it a second hidden turn
(2026-10-13 Ray revision), whether or not a check-in is on, so the conversation
carries on without the user having to write. When no check-in is on yet, the
crewmate explains in plain words what each check-in would do and proposes the
ones that fit, on a `crewmate.check_ins` card; otherwise it says what it will
work on first. A milestone edit on the Goals tab sends nothing.

It asks about one thing at a time. When the crewmate is busy, the turn waits for
its thread to go idle (for a bounded time); a newer save replaces a waiting one.

### 3.9 A list of goals, linked milestones, and tracking in chat

> **2026-10-13 Ray revision.** This supersedes "one long-term goal" and "up to
> three key results" wherever the sections above still say so.

- **A list of goals.** A crewmate holds any number of goals, each with a stable
  id (`g` and 8 hex digits), its text (the old long-term goal's 2000-character
  cap), and when and by whom it was created and last changed. The Goals tab
  numbers them by their place in the list and shows each number on a coloured
  circle (five theme tile colours, by position, cycling past five; the number
  always goes with its colour). A record written before this revision holds
  one `long_term` string; it reads as a one-goal list and is written in the new
  shape on its next write.
- **Milestones stay one shared set**, not nested under goals. Each gains
  `goal_ids`, the goals it serves (none, one or several), which the crewmate
  chooses when it proposes the set with `set_key_results`. An unknown id is
  refused, removing a goal strips its id from every milestone, and the card and
  the Goals tab show each milestone's goals as their numbered circles.
- **The caps are removed, at Ray's request.** There is no limit on how many
  goals or milestones a crewmate holds, and the old three-milestone cap is gone
  too: the user decides what they want tracked, and a hardcoded count would
  refuse a reasonable choice without explaining why. Instead the crewmate is
  guided: its tool descriptions, its goal-setup nudge and its own context say
  that a few goals and a few milestones are easier to track, so it proposes a
  small set and suggests merging as the list grows, and the user decides. Each
  text keeps its length cap, and the whole record keeps its 64 KiB storage
  bound (`record_too_large`), which is a safety bound rather than a count.
- **Cards by goal id.** `set_goal` adds a goal, or with a `goal_id` replaces that
  goal's text; Undo of an added goal removes exactly that goal, and Undo of a
  replacement restores the raw earlier text from the card's private snapshot.
  Own-thread admission (`REQ_OWN_GOAL_TURN`) is unchanged.
- **Check-ins and reports** stay one shared set; their prompts list every goal
  by number and each milestone's goals. The per-turn context block lists every
  goal numbered and each milestone with its goal numbers, with no readings.
- **Per-goal nudge.** Saving a new goal, or a real change to one, on the Goals
  tab sends the existing hidden goal-setup nudge with the same rules (a real
  change only, waiting for an idle thread up to ten minutes, a newer save
  replacing a waiting one). It names the goal that changed and lists them all.
  The `[Milestones confirmed]` nudge stays.
- **Add something {name} should track.** Below the check-ins, a full-width row
  opens a text box; Send posts what the user typed as their own visible message
  ("Please track this for me: <text>") into
  the crewmate's thread through the ordinary send path, and closes the profile
  card when it floats over the chat. What the crewmate then tracks is an item in
  its work ledger, the items the Crew board shows; the Goals tab shows none of
  them. The user never sees the word "ledger": the crewmate's context teaches
  it to record what it is asked to track in its work ledger, keep it updated,
  and call it tracking when it talks to the user.

## 4. UI mock

Static mocks, not screenshots of shipped UI. The images are attached to this RFC's PR.

**Goals tab.** The long-term goal, the key results the crewmate proposed, the
short-term goals toward it, and the three check-in and report switches.

![Goals tab, desktop](https://github.com/user-attachments/assets/806f6412-9d53-4dfd-86f3-05e1abee5701)
![Goals tab, phone](https://github.com/user-attachments/assets/d029b3da-c553-4a70-92b2-26f0d020013c)

**No goal yet.** The check-in and report switches are off and cannot be turned on.

![Goals tab with no goal](https://github.com/user-attachments/assets/46befb44-2587-4b0e-9303-f314dfe5c972)

**The crewmate proposes its key results.** After researching the work, as one
change card the user confirms.

![Key results proposal, desktop](https://github.com/user-attachments/assets/0c274fd7-d1e2-4692-9030-353a9f0e19de)
![Key results proposal, phone](https://github.com/user-attachments/assets/383598db-af29-4d87-9763-b7fec9691915)

**A new long-term goal.** Only this needs sign-off: `set_goal` shows a change
card, and nothing changes until the user presses Apply.

![set_goal change card, desktop](https://github.com/user-attachments/assets/376f53ef-3679-495a-830f-1e95a90d340a)

**The daily reminder.** A hidden turn, so the thread shows only the crewmate's
update. Short-term goals it checked off or added appear as a note with Undo.

![Daily reminder, desktop](https://github.com/user-attachments/assets/303487f9-2389-4667-93ca-e1fc18d7f317)
![Daily reminder, phone](https://github.com/user-attachments/assets/98396bd9-1472-4b90-bba0-f55f04ca194b)

**Dashboard side panel.** The live goal picture read from the goal record, and
the latest reports.

![Dashboard side panel, desktop](https://github.com/user-attachments/assets/402adb27-540a-4a93-94df-0b2bd81e7dc9)
![Dashboard side panel, phone](https://github.com/user-attachments/assets/1af9d730-ed22-4d15-8bd4-df50238b62ae)

**Weekly report.** Saved as an artifact; the thread only links to it.

![Weekly report artifact](https://github.com/user-attachments/assets/9019ab69-b496-4e68-8330-47f268a25a9c)

**New crewmate card.** The Advanced field is **Goal** and writes the long-term
goal, not About.

![New crewmate card with the Goal field](https://github.com/user-attachments/assets/59697c71-d6e1-4341-9e44-9844b446883b)

## 5. Non-goals

- Goals shared across a team of crewmates.
- Deadlines, priorities or progress percentages on short-term goals.
- Reporting goals anywhere outside the crewmate's own thread and profile.
- Changing what the rules file holds or who can write it.

## 6. Decided: where the goal is stored

The product owner decided on 2026-10-12 that the long-term goal goes into the new
per-member goal record with the short-term goals (§ 3.2), not into
`member-rules/<slug>.md`. The rules file keeps holding rules only. The record
lives in `trust/member-goals/`, out of the crewmate's file tools' reach.

## 7. Existing crewmates

Crewmates created before this change are left as they are. Their `description`
is not moved, and they start with no goal and the reminder off.

## History

| Date | Status | Change |
|---|---|---|
| 2026-10-12 | accepted | The goal, key results, check-ins and reports as described above. |
| 2026-10-21 | revision pending acceptance | `save_goal_report` takes `html` or `markdown`; reports default to a printable page (§ 3.3, § 3.5a; #22962). |

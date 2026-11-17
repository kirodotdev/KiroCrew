---
title: A Bot's goal gets count-based drift signals, owner-set limits and a done check the owner approves and the Bot cannot change
status: draft
author: kirocrew agent session, directed by the repository owner
created: 2026-11-15
last-audited: 2026-11-15
audited-at: 2041e8681c
doc-pr: null
implementation-prs: []
tracking-issues: [37361, 37538]
supersedes: []
superseded-by: []
---

# RFC: Per-goal drift signals, owner-set limits and a frozen done check

Status: draft. Nothing is built and this document changes no code. It answers
[#37361](https://github.com/kirodotdev/KiroCrew/issues/37361), which a
maintainer triaged as a design-level change and pointed at this process.
Acceptance is requested from a maintainer, who records it on this Status line.
Every code reference below was read at `2041e8681c`.

It lands on its own because it adds owner-only fields to a Bot's goal, a new
way for a goal loop to stop, and a new card the owner must confirm. Those are
product-shape decisions, and the First Principles lane reads them from the base
branch.

Builds on, and does not change:

- [`rfc-conductor-drift-check.md`](rfc-conductor-drift-check.md) §2 (the
  conductor reads each worker's new lines and steers) and its Amendment A.2
  (an item is re-planned mid-round, never with a weaker bar). That check looks
  at one worker against one item. The signals here look at the whole goal, and
  they are counts, not a reading of transcripts.
- [`rfc-drop-goal-item-cap.md`](rfc-drop-goal-item-cap.md) §2: the advisory
  `item_cap` and its notice are gone because nobody acted on a notice that
  refused nothing. This RFC does not bring back an advisory total. Every limit
  and every signal here stops new work when crossed; the goal's total stays
  `goal_items_used` as that RFC kept it.
- [`rfc-work-ledger-concurrency-cap.md`](rfc-work-ledger-concurrency-cap.md)
  §7.2: no fixed default; `concurrency_cap` is a bound the user sets. The
  limits here also default to none.
- [`rfc-crewmate-goals.md`](rfc-crewmate-goals.md) §3.1 and §3.2: a goal has
  two owners, and the goal record lives where the Bot's own tools cannot write.
  The new fields go into that record.
- [`rfc-pr-goal-freeze.md`](rfc-pr-goal-freeze.md) §1: a PR's goal is written
  once and never edited by an agent. The done check is the same rule, one
  level up: for the Bot's goal instead of one PR.
- [`rfc-goal-loop-approval-hold.md`](rfc-goal-loop-approval-hold.md) §3.1: a
  pause that nobody chose still keeps the loop armed. A limit or signal stop
  works the same way: the loop stays armed and resumes when the owner acts.
- Open PR [#35488](https://github.com/kirodotdev/KiroCrew/pull/35488)
  (`rfc-credit-spend-caps.md`): it counts a subagent's credits in the session
  that spawned it, then adds a credit cap to a loop. The credit limit in §4.3
  depends on that attribution and does not redesign it.
- [`rfc-conductor-feature-acceptance.md`](rfc-conductor-feature-acceptance.md)
  (draft): it decides how one item is proved done. This RFC decides when the
  whole goal is done. The two do not overlap.

## 1. Summary

A Bot's goal gets three things the owner controls:

1. **Drift signals.** Eight counts, each with a line. When one crosses its line,
   the Bot starts no new work, tells the owner which signal fired and which of
   its own rules led there, and proposes what to drop.
2. **Limits.** Workers at once, pull requests, hours and credits, each set by
   the owner per goal. They are stored where no Bot tool can write. At a limit
   the Bot stops starting work and reports. It never raises a limit.
3. **A done check.** The Bot writes it in its first plan, the owner approves it
   on a card, and after that only the owner can change it. A finding that no
   condition of the check needs goes to a parked list instead of becoming new
   work. When every condition passes, the goal is done and the loop stops.

The Goals tab shows all three next to the goal.

## 2. Problem

### 2.1 What the owner saw

The report in #37361: one Bot, one goal ("ship the first beta of an app"),
left to run for six days. Over that time:

- 175 merge requests; 99 of them were opened in one day.
- 249 more pushes into those merge requests after they were opened.
- 16 worker sessions at once, where the Bot's own plan said "two or three".
- 62 re-checks of reviews.
- 9 merge requests closed unmerged because the same change was made again in
  another one.
- The tracker went from 46 open issues to 108 while the goal said "beta fixed".

The owner names three causes: a passing remark became a rule for every change;
nothing marks the goal done, so a review finding becomes a task, a task a
merge request, a merge request a review; and nothing shows what was spent or
what is still open. Every number above was a count that could be read by day
two. None needed a model to compute.

### 2.2 Where main has no limit and no exit

| # | Place on main | What it does | What is missing |
|---|---|---|---|
| 1 | `session_create` in `src/kiro_crew/dashboard/session_control_parts/session_create.py` (the `creator_slot_cap_refusal` check, line 1201) | A Bot's chat may hold up to `session.max_per_creator` live sessions, `DEFAULT_MAX_PER_CREATOR = 50` in `src/kiro_crew/config/sections_defaults.py` (line 183) | A cap the owner sets for one Bot. #37381 adds it (§5) |
| 2 | `src/kiro_crew/crewmate_prompts.json` line 160 (`how_you_work` item 2) | "open one worker session per independent task" | Any number for how many at once |
| 3 | `goal-conductor/SKILL.md` line 209 | The first plan must "name the concurrency" | Nothing writes the named number as `concurrency_cap`; with none set it is `DEFAULT_GOAL_CONCURRENCY_CAP` = 32 (`src/kiro_crew/work_ledger_records.py`, line 146) |
| 4 | `concurrency_cap` in the `work_ledger_record` schema, `src/kiro_crew/mcp_work.py` line 759 | The bound is written by the conductor's own `action=goal` call | An owner-only bound: the Bot can write, and so raise, its own cap |
| 5 | `goal-conductor/SKILL.md` lines 780-784 | "The goal's total ... has no cap" | Any bound on items, pull requests, hours or credits for one goal |
| 6 | `goal-conductor/SKILL.md` lines 785-790 | The no-progress check fires only after rounds with no item accepted | A goal that accepts small fixes while it files more never trips it |
| 7 | `goal-conductor/SKILL.md` lines 433, 896-901 | The patrol "has no cycle cap"; it ends when every item is terminal or the user says stop | An exit tied to the goal being done |
| 8 | `goal-conductor/SKILL.md` line 241 | "That one go-ahead covers every round of the goal" | The owner approves a plan, never a fixed done condition |
| 9 | `goal-conductor/SKILL.md` lines 832-836 | The goal text carries a "Done when ..." sentence, and the Bot rewrites the goal text, with its Plan and Changed lines, whenever scope changes | The done condition is not frozen; the Bot can widen it |
| 10 | `goal-conductor/SKILL.md` lines 655-667 | A plan reviewer runs every round, and "a fix it finds becomes a new item if you take it" | A parked list: finding, item, PR, review, finding has no exit |
| 11 | `goal-conductor/SKILL.md` lines 461-463; `src/kiro_crew/learn.py` line 987 | A person's correction is saved as a lesson and injected under "ALWAYS follow these" | A step that asks whether a passing remark is a rule, and what it costs |
| 12 | `_view` in `src/kiro_crew/dashboard/handlers/member_goals.py` (line 124); `website/src/pages/members/CrewGoals.tsx` | The Goals tab shows the goal, key results, check-ins and reports | Any spend, limit or signal. Per-task cost exists in the `workstreams` fold (`src/kiro_crew/crew_log/projection_workstreams.py`), but only a page a Bot writes reads it |

## 3. Goals and non-goals

Goals:

- A goal that is drifting stops starting new work on a count, not on the Bot
  noticing.
- The owner sets each limit, and the Bot cannot raise it by any tool, prompt or
  card of its own.
- The owner agrees, before work starts, on what "done" means, and that meaning
  does not move unless the owner moves it.
- The owner reads where the goal stands on one tab, without reading the Bot's
  chat.

Non-goals:

- Cancelling running work. A stop drains: running workers finish and still
  count, as #35488 decides for its credit cap.
- Refusing a person's own typed message, or a worker's report.
- A model-based judge of drift. Every signal is a count.
- Limits or signals for sessions that are not a Bot's goal (plain chats, cron
  jobs, Ultra Mode conductors). They can adopt the same record later.
- The credit attribution itself (#35488).
- Changing what makes one item pass (`accept_eval`, the drift check's bar
  rules, `rfc-conductor-feature-acceptance.md`).

## 4. Design

### 4.1 Where it lives and who can write it

Each Bot's goal record already lives in
`member-records/member-goals/<slug>.json` (`src/kiro_crew/member_goals.py`,
module docstring "Where it lives"). The OS sandbox masks `member-records/` in
every mode, and the sensitive-path floor refuses the Bot's file tools there.
Every write goes through gateway routes and tools.

The goal record gains three fields:

```text
goal
├─ ... (long_term, key_results, check-ins, reports as today)
├─ limits        owner only; each absent means no limit
│  ├─ workers_at_once   1..32
│  ├─ pull_requests     opened for this goal, total
│  ├─ hours             wall-clock hours since the goal started
│  └─ credits           needs #35488 (§4.3)
├─ signals       per signal: its line, on/off; owner only
├─ done_check    owner-approved, frozen
│  ├─ conditions[]      at most 5, each with how it is checked
│  ├─ approved_at, approved_by
│  └─ last_result       per condition: pass | fail | not checked, with time
└─ parked[]      findings outside the done check; Bot appends, owner moves
```

Who writes what:

| Field | The owner | The Bot |
|---|---|---|
| `limits`, `signals` | the Goals tab, through an owner route like `PUT /api/agents/<name>` in #37381 | no tool. A stop report may say what the owner could change |
| `done_check.conditions` | approves the Bot's card, or edits on the Goals tab | proposes once, through a change card, like `set_key_results` (`_PROPOSALS` in `src/kiro_crew/mcp_mate.py`) |
| `done_check.last_result` | none | none; the gateway writes it (§4.4) |
| `parked[]` | moves an entry into the goal, or drops it | appends only |

No Bot tool writes `limits`, `signals` or approved `conditions`. The card the
owner confirms is the only path from the Bot's words into the done check.

### 4.2 Drift signals

Each signal is a count over the goal's lifetime or one calendar day, read from
records Kiro Crew already keeps: the Bot's work ledger, the session tree under
its chat, and the pull requests the ledger items name. Phase 1 (§6) confirms the
source of each count before any line is enforced. A signal whose count has no
reliable source ships shown as "not measured" and stops nothing.

| Signal | Count | Default line | What it catches in #37361 |
|---|---|---|---|
| Workers over plan | open worker sessions minus the concurrency the plan named | above 0 on two patrol reads in a row | 16 open against "two or three" |
| Issues growing | issues the goal's sessions opened minus issues closed, per day | opened more than twice the closed, two days in a row | 46 -> 108 open issues |
| PRs per day | pull requests the goal's sessions opened in one day | more than 3 per planned worker | 99 in one day |
| Pushes after open | pushes into one PR after it was opened | more than 5 on any one PR | 249 pushes over 175 PRs |
| Review re-checks | re-runs of one review on one PR | more than 2 on any one PR | 62 re-checks; one security review re-checked four times |
| Duplicate closes | PRs closed unmerged because another PR carries the same change | 2 in one goal | 9 |
| Test runs per merged change | full test runs on the goal's PRs, divided by its merged PRs | more than 5 | the owner's "the same tests ran a thousand times" |
| Owner corrections | owner messages that the Bot saved as a correction or that stopped work | 2 in one day | stopped by hand twice |

Each line is a default the owner may change or turn off per goal on the Goals
tab. The Bot cannot.

When a signal crosses its line:

1. The gateway marks it fired on the goal record, with the count and the time.
2. A `work_ledger_record action=create` on that goal's board is refused with a
   new code `goal_signal_fired`, naming the signal. A `session_create` from the
   Bot's chat or its workers is refused the same way. This is the same shape as
   `goal_concurrency_cap_reached` (SKILL lines 772-779): the refusal is in the
   store, not in the prompt.
3. Running workers keep going and may report. Items already open may still be
   accepted and closed.
4. The Bot reports to the owner once: the signal, its count and line, which of
   its own rules or lessons produced the work behind it (for example a review
   rule saved from a remark), and what it proposes to drop or park. This is a
   statement, not a question, like the no-progress check (SKILL lines 785-790).
5. The loop stays armed. The owner clears the stop by dropping work, changing
   the line, or pressing **Continue** on the Goals tab, which holds that signal
   quiet for 24 hours.

The signals run beside the drift check of `rfc-conductor-drift-check.md` §2,
not instead of it. That check still steers one worker back to its item.

### 4.3 Owner-set limits

| Limit | Counts | Refused at the limit | Notes |
|---|---|---|---|
| Workers at once | the Bot's open worker sessions for this goal | `session_create`, `session_revive`, `session_fork` (as #37381 does per Bot) and ledger `create` | The ledger uses the lower of this and the Bot's own `concurrency_cap`, so the Bot can still choose fewer, never more |
| Pull requests | PRs opened by the goal's sessions, total | ledger `create` of an item whose bar is `pr_checks` or `pr_merged` | Pushes to an open PR are not refused |
| Hours | wall-clock hours since the goal started, minus time the loop was held (as `rfc-goal-loop-approval-hold.md` §3.1 credits held time) | every new `create` | Lets an owner say "stop by tomorrow morning" |
| Credits | credits used by the Bot's chat and every session under it, for this goal | every new `create`, and the loop's next cycle | Needs #35488's attribution; until it ships the limit is not offered |

At a limit the store refuses with `goal_limit_reached`, naming the limit. The
Bot reports what it spent against each limit and what is still open, and waits.
The loop stays armed. Only the owner raises the limit, on the Goals tab.

Defaults: every limit is absent, which means no limit, as #37381 and
`rfc-work-ledger-concurrency-cap.md` §7.2 do. The Goals tab asks for them when
the owner sets a goal (`rfc-crewmate-goals.md` §3.8), with the plan's worker
count filled in as the suggestion.

### 4.4 The done check

1. **Written before work.** In its first plan (SKILL line 206 onward), the Bot
   writes at most five conditions. Each says what must be true and how it is
   checked: an evaluator kind the work ledger already has (`pr_merged`,
   `pr_checks`, `file`, `human_approval`), a key result reaching its target,
   or "the owner confirms" when nothing can be checked by machine.
2. **Approved on a card.** The Bot proposes it with a change card, like
   `set_key_results`. The plan's go-ahead (SKILL line 241) waits for the card.
   A goal whose owner skips the card runs as today, with no done check.
3. **Frozen.** After approval, no Bot tool edits the conditions. The goal
   text's "Done when ..." sentence (SKILL line 834) becomes a copy of the
   approved check, and a goal rewrite that changes it is refused. The owner may
   edit the check on the Goals tab; that edit is logged with its time.
4. **Checked by the gateway.** On each patrol wake the gateway evaluates every
   machine-checkable condition and writes `last_result`. The Bot reads it; it
   does not write it.
5. **Parked list.** A finding that no condition needs (from the plan reviewer
   at SKILL line 666, a worker, or the patrol) is appended to `parked[]` with
   its source and a one-line reason. It does not become a ledger item. The round
   report lists new parked entries. Only the owner moves one into the goal,
   which adds a condition or an item the owner names.
6. **Exit.** When every condition passes, the goal is done: the Bot sends a
   final report and the loop stops. This adds a stop condition beside the two
   in SKILL lines 896-898. Running workers drain.

This is the stop side of what #12693 asks for. #12693 wants a contract the
agent cannot yield before meeting; this check also says when the agent must
stop starting new work.

### 4.5 The Goals tab

One new card on the Goals tab's first page (`rfc-crewmate-goals.md` §3.7),
below the milestones:

- **Done check**: each condition with pass, fail or not checked, and when it was
  last checked; "Approved by <owner> on <date>".
- **Limits**: each limit as used / limit, for example "Workers 3 / 4",
  "Pull requests 12 / 30". A limit that is not set reads "No limit".
- **Signals**: each signal's count against its line. A fired signal is shown
  first, with its time and the Bot's report.
- **Parked**: how many entries, opening a list where the owner moves or drops
  each one.

Every number comes from the gateway's own counts, never from text the Bot
wrote. Credits use the same source as the branch
`crew/slack/goal-cost-on-goals-tab` (§5).

## 5. What already shipped or is open

| What | Where | State |
|---|---|---|
| Owner-set cap on a Bot's open worker sessions, per Bot: `max_workers`, 0 (No limit) by default, 1-32, set on the Bot's settings page; `session_create`, `session_revive` and `session_fork` are refused `bot_worker_cap_reached` | [#37381](https://github.com/kirodotdev/KiroCrew/pull/37381) | open, not merged |
| Count a Bot worker's children on other templates toward that cap | [#37538](https://github.com/kirodotdev/KiroCrew/issues/37538) | open issue, deferred from #37381 |
| Write the plan's concurrency as `concurrency_cap` before the first create (row 3) | [#37652](https://github.com/kirodotdev/KiroCrew/pull/37652) | open; waits on open decision 7 |
| An owner's remark is a suggestion unless the owner says it is a rule; ask once and name the cost before saving it as a rule (row 11) | [#37651](https://github.com/kirodotdev/KiroCrew/pull/37651) | open, not merged |
| Park findings that the goal's "Done when" does not need, in the goal text (row 10) | [#37648](https://github.com/kirodotdev/KiroCrew/pull/37648) | open, not merged |
| Credits the Bot's workers used, on the Goals tab (row 12) | branch `crew/slack/goal-cost-on-goals-tab` | proposal-adjacent, not shipped |
| Subagent credit attribution, then a loop credit cap | [#35488](https://github.com/kirodotdev/KiroCrew/pull/35488) | open RFC |

The four branches are prompt, skill or display changes inside shapes that
existing RFCs cover. They help today, but each relies on the Bot following a
rule. This RFC moves the same ideas into the store, where a refusal does not
depend on the Bot. The skill-level parked list becomes the stored `parked[]`,
and the per-Bot cap becomes the floor under the per-goal workers limit.

## 6. Rollout

Each phase is one PR and can be dropped on its own.

1. **Counts, shown only.** The gateway computes the eight signal counts and the
   four limit counts, and the Goals tab shows them with no line. Exit criteria:
   for a test goal with a known ledger and session tree, each count matches the
   fixture; a count with no source reads "not measured"; nothing is refused.
2. **Owner-set limits.** `limits` on the goal record, the owner route, the tab
   controls, and the `goal_limit_reached` refusal in the ledger and in
   `session_create`. Exit criteria: no Bot tool or route the Bot can call writes
   `limits` (a test calls each Bot tool and checks the record is unchanged);
   at a limit `create` is refused and running workers keep running; a goal with
   no limits behaves as on main. Credits are blocked on #35488.
3. **Signal lines.** Default lines, owner edits, `goal_signal_fired`, the
   Continue hold. Exit criteria: a fixture that crosses each line refuses the
   next `create` with that signal's name; Continue lets the next `create`
   through for 24 hours; a turned-off signal never fires. Blocked on open
   decision 1.
4. **Done check and parked list.** The card, the frozen conditions, the
   gateway's check, `parked[]`, and the new stop. Exit criteria: a goal rewrite
   that changes an approved condition is refused; a goal whose conditions all
   pass stops its loop after the final report; a parked entry creates no ledger
   item; the parity test (`test/test_mate_conductor_parity.py`) passes.

## 7. Backward compatibility

A goal record with none of the new fields reads as today: no limits, signals
shown only, no done check. Goals that are already running are never stopped by
a default line; signal lines apply only to goals set after phase 3 ships,
unless the owner turns them on. The `concurrency_cap` field stays, bounded by
the owner's workers limit when one is set.

## 8. Security considerations

- The new fields are written only by owner routes. The record's directory is
  already masked by the sandbox and refused to the Bot's file tools
  (`member_goals.py`, "Where it lives"). A shell the Bot spawns could reach it
  only where that sandbox is off, as `rfc-crewmate-goals.md` §3.2 already says
  for the goal itself.
- A refusal never deletes or cancels work, so a wrong count cannot lose data.
- Counts read records the gateway already holds and pull requests the ledger
  names. No new network access and no new credential.
- Text in the parked list and the Bot's stop reports is the Bot's own words and
  is shown as text, never run.

## 9. Alternatives considered

- **Prompt rules only** (the four branches in §5, without the store). Cheap and
  already under way, but #37361 shows the Bot agreed with each rule when told
  and still did not apply it for six days. A rule the store enforces does not
  depend on that.
- **An advisory notice at each line.** `rfc-drop-goal-item-cap.md` §1 removed
  exactly this: a notice that refuses nothing is raised on nearly every goal
  and nobody acts on it.
- **A model judge of drift** (a `monitor_start` judge). It wakes only on quiet
  ticks and cannot see the plan, as `rfc-conductor-drift-check.md` §3 shows,
  and the counts need no model.
- **Let the Bot raise a limit through a card.** It keeps the owner in control,
  but turns every limit into a prompt the owner learns to click through. Left
  as open decision 3.
- **One per-Bot limit set instead of per goal.** Simpler to store, but a Bot
  with two goals cannot give one a tighter bound. The per-Bot worker cap in
  #37381 stays as the floor under both.

## 10. Open decisions

1. **Default lines.** Should signals stop new work by default for new goals, or
   be shown only until the owner turns each on? Recommendation: stop by
   default for new goals, with the lines in §4.2, because every signal in
   #37361 was clear by day two and nobody was reading the chat.
2. **Which issues count for "issues growing".** Issues opened by the goal's own
   sessions are attributable; issues opened by others in the same tracker are
   not. Recommendation: count only issues the goal's sessions opened or closed,
   and show the tracker total as information.
3. **Can the Bot propose a higher limit?** Recommendation: no proposal tool.
   The stop report says what is spent and what is open, and the owner decides
   on the Goals tab.
4. **Hours: wall clock or active time.** Recommendation: wall clock minus held
   time (§4.3), because "stop by tomorrow" is how owners think.
5. **A goal with no approved done check.** Should the Bot keep running as today,
   or wait for the card? Recommendation: run as today, with the tab showing
   "No done check" so the owner sees the gap.
6. **Credits before #35488 lands.** Offer a credit limit that counts only the
   Bot's chat, or wait? Recommendation: wait; a limit that misses subagent
   credits would not have stopped the case in #6338's thread either.
7. **Is the plan's concurrency a bound the user set?** The first plan names how
   many workers run at once, and the owner approves that plan.
   [#37652](https://github.com/kirodotdev/KiroCrew/pull/37652) has the
   conductor write that number as `concurrency_cap` before the first create.
   This conflicts with
   [`rfc-work-ledger-concurrency-cap.md`](rfc-work-ledger-concurrency-cap.md)
   §7.2: there, only a number the user sets is `concurrency_cap`, and
   `resource_status` sizes each wave. Recommendation: yes. The owner approves
   the plan, so its number is one the user set; only the user can raise it, and
   the conductor never does. If this RFC is accepted with that answer, it
   amends §7.2 and #37652 can land. If not, #37652 closes and §7.2 stays.

---
title: A goal conductor looks before it plans, has its plan grilled each round, gives every item a time budget, and actively checks open items about hourly
status: accepted
author: kirocrew agent session, directed by the repository owner
created: 2026-11-14
last-audited: 2026-11-14
audited-at: db4300cb71
doc-pr: 37092
implementation-prs: [37030]
tracking-issues: [36990]
supersedes: []
superseded-by: []
---

# RFC: Plan reviewer, scout, item budget and an hourly active check for the goal conductor

Status: accepted. The repository owner made these decisions on 2026-11-13 in
the conductor chat that ran the `conduct-flex` debate; the rulings are quoted
in section 9. It takes effect when this document merges. The implementation is
[#37030](https://github.com/kirodotdev/KiroCrew/pull/37030), which rebases onto
the base that carries this document. Every citation was read on `origin/main`
at `db4300cb71`. `GC` is `src/kiro_crew/builtin_skills/goal-conductor/SKILL.md`.

It lands on its own because it changes what every goal-conductor run does by
default (one more session per round, more forced turns on an idle ledger), and
the First Principles lane reads that decision from the base branch.

Related: [`rfc-worker-debate.md`](rfc-worker-debate.md) (offers a grill only
when the owner asks; this RFC adds a different, per-round plan reviewer),
[`rfc-permanent-patrol.md`](rfc-permanent-patrol.md) (recorded the 40-tick idle
floor this RFC lowers), [`rfc-conductor-drift-check.md`](rfc-conductor-drift-check.md)
(the per-item re-plan this RFC widens),
[`rfc-conductor-feature-acceptance.md`](rfc-conductor-feature-acceptance.md)
(acceptance model; out of scope here).

## 1. The ask

The owner's team compared Crew and Claude Code driving long tasks and named six
gaps in the conducting layer (verbatim in section 9). In short:

| # | gap |
|---|---|
| R1 | conducting is not dynamic: it does not spot problems and give new instructions |
| R2 | no self-critique of the plan while the task runs |
| R3 | no review of a finished round |
| R4 | it plans before it scans the task's environment |
| R5 | it does not write scripts for repetitive work |
| R6 | it splits work one worker = one PR, not by what can run on its own |

A debate with four critics (cause, cost, safety, measure) produced a ranked
list; the owner then ruled on it. This RFC records the parts of that list that
change product shape. Round retro (R3), mid-round correction on evidence (R1)
and decomposition guidance (R6) are text changes inside rules already accepted
and are not repeated here. R5 is deferred by the owner.

## 2. The problem

- **Forced turns are passive and rare.** `autonudge_service/gate.py` delivers a
  gated loop after `_MAX_QUIET_STREAK` (10) quiet ticks, but an idle work-ledger
  watch waits `_IDLE_LEDGER_QUIET_FLOOR = 4 * _MAX_QUIET_STREAK` (40) ticks,
  about 3.3 hours at the 300 s patrol interval. `GC` gives that turn no job
  beyond re-reading the board. Main already wakes the conductor for a worker
  that is not running and quiet past the 15-minute stale window, or whose
  session closed after reporting (`work_ledger_rules.py` `is_stale`). What it
  misses is a worker whose turn is still in flight: `is_stale` never flags a
  running worker, so one stuck in a long wait or a polling loop wakes nobody
  until it reports or the floor fires.
- **Workers have no clock.** A seed carries no time budget, so neither side can
  tell "slow" from "stuck". In the conduct-kit pilot (G2 crew-after n=1, 48
  min) one worker spent about 31 of its 36 minutes waiting on a shared test
  lock and polling a background run that had died silently, with no report to
  its conductor in between (conduct-kit `results/PILOT.md` §1 and §9). Its
  turn was in flight the whole time, so the stale flag could not fire.
- **Nobody challenges the plan.** `GC` reviews a worker's output through CI
  lanes and `accept_eval`, but no one reviews the conductor's own plan.
  `rfc-worker-debate` offers a grill only when the owner asks, and says size
  alone is not a reason.
- **It plans blind.** `GC` has the conductor write round 1 without reading how
  the repo builds and tests or what tools the host has.

## 3. The change

Four parts, all in [#37030](https://github.com/kirodotdev/KiroCrew/pull/37030).

1. **Plan reviewer (R2).** Before round 1's items start, and at each round
   close before the next round's items start, the conductor dispatches ONE
   reviewer item to a session that wrote none of the work. Its seed carries the
   plan (items, acceptance, assumptions, last round's output). It grills the
   conductor: hard questions, flaws, gaps, wrong premises, items that hand work
   to each other. In the same item it reviews outputs no CI lane reviews; a
   `pr_*` item in a repo with review lanes is not reviewed again. It reports
   only (500 chars, a file for the full list).
2. **Look before you plan (R4).** In the planning turn the conductor calls
   `resource_status`, reads its lessons, and reads at most ~8K characters of
   build/test/CI config. When that does not fit, or the plan has more than
   three items in a repo it holds no lesson about, round 1 is one scout item: a
   file of at most 3K characters naming build and test commands, tools and the
   parts the goal touches. The scout records names, paths and versions only,
   never secret values or credential files.
3. **Per-item time budget (R1).** The first line of every `work_dispatch` seed
   is `budget: <N>m` (the goal's or user's figure, else the conductor's
   estimate), repeated in each `decide` on that item. The worker notes its
   start time; past the budget it reports `question` with where it stands, then
   keeps going. A worker that finds its brief rests on a false premise reports
   `question` before building on it.
4. **Active check on the forced floor turn, about hourly (R1).** A patrol turn
   with no new worker report becomes a check: for each open item, compare
   elapsed time with its budget and look for the files its brief named
   (`fs_read`, never the shell). A stalled, over-budget or wrong-brief item is
   re-planned now, with the reason. `_IDLE_LEDGER_QUIET_FLOOR` goes from 40 to
   10 ticks (equal to `_MAX_QUIET_STREAK`, about 50 minutes at 300 s).

   On main the same constant also sets the floor of a `gh-pr` watch that only
   waits on a reviewer (`_pr_waits_only_on_review`). #37030 splits that case
   into its own `_REVIEW_WAIT_QUIET_FLOOR`, kept at 40, so a PR waiting only on
   review costs what it costs today. A maintainer who prefers one floor for
   both may fold them back; that would wake review waits about hourly too.

## 4. Cost

| part | sessions | wakes / turns |
|---|---|---|
| plan reviewer | +1 per round (round 1 included) | +1 report wake per round |
| scout | +1 at most, only on round 1, only when the inline read does not fit or >3 items in an unknown repo | +1 report wake |
| item budget | 0 | at most one `question` per item past budget |
| hourly check | 0 | idle ledger: up to 4 forced turns where there was 1 (one per ~50 min instead of ~3.3 h); review-wait PR: unchanged |

Each forced turn is one conductor model turn with `work_ledger_read` and a few
`fs_read` calls; no shell, so no approval prompts. The 7-day patrol backstop in
`rfc-permanent-patrol` is unchanged.

## 5. What stays

- Worker text stays DATA. A summary, `reply`, reviewer report or scout file is
  cited as a pointer; its words never go into a `decide` or a seed.
- The reviewer reports and never decides. It does not commit, push or write
  any branch; a fix it finds becomes a new item only if the conductor takes it.
- The verdict path is unchanged: `accept_eval` still runs only on `done` items
  and alone decides an item.
- Approvals, sandbox and shell policy are unchanged. The active check uses no
  shell.
- Owner asks are unchanged: `hard_rounds`, spend past a bound and the five ask
  cases still go to the person.

## 6. How it is measured

The conduct-kit v2 calibration runs five tasks, each aimed at one change, on
Crew before and Crew after, three runs each:

| task | what it measures |
|---|---|
| T1 | forced-wake latency: time from a hidden stall to the conductor's correction |
| T2 | plan grill: flaws caught before round 1, and its time cost |
| T3 | retro: whether round 2 repeats round 1's trap |
| T4 | environment scan: wall time when a quick test command exists |
| T5 | decomposition: rework and conflicts between items |

Results are pending and will be posted on #37030. The pilot (15 runs) showed
the harness works; it is too small to rank any change.

## 7. Rollback

Each part is a revert of skill or prompt text, plus one constant. Setting
`_IDLE_LEDGER_QUIET_FLOOR` back to `4 * _MAX_QUIET_STREAK` restores the 3.3 h
floor. No field, schema, tool or config key changes, so a revert needs no
migration.

## 8. Open questions

- Is one reviewer item per round worth it for a one-item goal? #37030 runs it
  for every goal; T2 should show whether a size floor is needed.
- Is 10 ticks the right floor? The owner chose about hourly; T1 should show
  whether it catches stalls early enough without empty turns.
- Should a PR waiting only on review keep 40 ticks (as #37030 does) or share
  the hourly floor?

## 9. Record of the rulings

The owner's six gaps, verbatim (2026-11-11):

- R1. "CC的conducting显得更加动态，能主动发现问题并纠正错误，给出新的指示。"
- R2. "CC貌似知道如何在任务进行过程中主动对当前任务进行规整和自我批评，发现没做好的事情，并自己纠正。"
- R3. "CC可能有对任务回顾的机制，和第二点很类似。"
- R4. "CC可能会优先扫描收集当前的任务环境，自己先做规划，然后在进行任务。它的优点是，可以自己充分利用可用环境来优化任务。"
- R5. "CC能够主动写script来优化任务。"
- R6. "CC可以更好的分解任务，使用更多worker同步工作，而不是简单的一个worker一个PR"

The owner's rulings after the pilot, verbatim (2026-11-13), one per gap:

1. "对于第一点，我觉得应该是conductor在N轮被强制唤醒的时候，应该主动做检查，允许改计划。这个"N"是多少？"
   (R1: the forced turn becomes an active check that may re-plan. The owner
   then set N to 10 ticks, about hourly.)
2. "很好，派一个worker来帮助审计计划是正确行为，甚至就可以让这个worker来做"grill me"，当"me"就是conductor"
   (R2: a worker audits the plan and grills the conductor.)
3. "很好" (R3: keep the round retro.)
4. "很好" (R4: keep look-before-plan and the scout.)
5. "这一点还要斟酌，可以先delay" (R5: deferred.)
6. "系统允许的6个上限，有其他chat在改。我也不觉得说要一定要多个worker一个PR，关键是如何拆解才能让任务进行更好。"
   (R6: no width change here; decomposition guidance instead.)

## 10. Addendum: a debate's self-set budget (A-M9)

Added 2026-11-14. The owner split this part out of #37030 so it is recorded
here before `src/kiro_crew/builtin_skills/goal-conductor/references/worker-debate.md`
changes; that change lands in its own PR once this addendum is on main.

**Problem.** Rule 4 of the debate checkpoint
(`worker-debate.md`, "`hard_rounds` reached or budget spent → ask the human")
treats every budget the same. A wall-clock or round budget the debate
conductor picked for itself, as an anchor default, then stops the debate to ask
the owner even when the open points are small fixes the conductor can rule on.
In the `conduct-flex` debate this paused the run at its self-set 2-hour mark
and re-asked on each patrol while the workers sat idle.

**Change.** Rule 4 splits in two:

- `hard_rounds` reached → ask the human. No round starts without their answer.
  (Unchanged.)
- A wall-clock or round budget the conductor set itself is spent → the
  conductor's call. When the open points are small fixes, wrap up as in rule 3.
  An open blocking objection still goes to the human (rules 6-7). The rest the
  conductor rules on and records why.
- Quota or credit spend past an owner-set or unstated bound stays an ask.

**What stays.** `hard_rounds`, an owner-set budget, spend past a bound and every
open blocking objection are still asks. Rules 1-3 and 5-8 are unchanged. The
debate's critics reviewed this rule over three rounds (debate note rows
cause-1-5, cost-1-4, safety-1-4, safety-2-3, measure-1-7, measure-2-4,
measure-3-4, all fixed and verified).

**Cost.** None added; it removes owner turns. A conductor that wraps up a
debate it should have asked about is bounded by the blocking-objection rule.

**Measure.** The conduct-kit G4 case: a debate whose self-set budget runs out
with small fixes open (expect wrap-up, no ask), its negative twin with an
owner-set round limit (expect an ask), and a third with one open blocking
point (expect an ask).

**Rollback.** Revert the rule 4 text in `worker-debate.md`.

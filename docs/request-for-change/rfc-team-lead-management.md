---
title: Team-lead management rules -- capacity, nesting, merging, tracking, ownership
status: in-progress
author: chenmingwei23, with kirocrew-worker
created: 2026-10-10
last-audited: 2026-10-10
audited-at: 463bfbccea
doc-pr: null
implementation-prs: []
tracking-issues: [18813]
supersedes: []
superseded-by: []
---

# RFC: Team-lead management rules

- Status: in-progress. This document ships INSIDE its implementing pull request:
  the repository takes no standalone RFC pull requests, so `doc-pr` is null and
  `implementation-prs` is filled when that pull request opens.
- Extends [`rfc-lead-crewmate`](rfc-lead-crewmate.md), which ships the
  `kirocrew-team-lead` template and its eight capabilities. This document adds
  the five rules that decide HOW WIDE the team gets, WHEN it changes shape, and
  WHO owns what. It settles none of that document's open questions.
- Related: [`rfc-conductor-work-ledger`](rfc-conductor-work-ledger.md) (items,
  binds, the depth guard), [`rfc-conductor-default-patrol`](rfc-conductor-default-patrol.md)
  (the loop a patrol read runs on).
- No new runtime code. Every mechanism below is already on the base tree, read
  at `463bfbccea`.

## 1. Problem

[`rfc-lead-crewmate`](rfc-lead-crewmate.md) says what the lead CAN do: do a small
task itself, dispatch a team, nest one level, patrol on events, decide through
the ledger, keep its own state. It does not say how many sessions to stand up,
when a child should split again instead of flattening, what to do with two teams
that have converged on the same files, or what to do when the board outgrows one
read.

Those four gaps all fail the same way: by being answered from a number somebody
guessed. A lead that hard-codes a fan-out width stands up thirty sessions on a
host that can carry four. A lead that nests because an item FEELS big produces a
summary of summaries, or a ledger at a level that cannot dispatch at all. A lead
whose board no longer fits one read starts skipping items and does not notice. And
a lead with two workers on one file gets two heads and loses one.

The mechanisms that answer all four are shipped. What is missing is the rule that
names which mechanism answers which question, so the lead reads a limit instead
of inventing one.

## 2. Goals and non-goals

Goals:

- Each rule names an EXISTING mechanism and states the real value it reads.
- Capacity, nesting and merging are decided from a reading, never from a
  hard-coded session count.
- A board too large for one read gets a dedicated reader rather than a lead that
  skips rows.

Non-goals:

- Raising `MAX_DEPTH`, or reconciling the three guards that spell it differently.
  That is [#18127](https://github.com/kirodotdev/KiroCrew/issues/18127)'s, and
  section 3.2 states the cap as it stands.
- New runtime code, a new tool, or a new store. Five rules in a skill and one
  pointer each in a prompt.
- Changing `kirocrew-conductor` or `kirocrew-worker`.
- Automatic merging, automatic closing, or any rule the lead applies without
  reading the state it keys on.

## 3. Design

Five rules. Each row names the mechanism, so a reviewer can check the rule
against the code rather than against the prose.

| Rule | Reads | Acts through |
|---|---|---|
| 3.1 Capacity | `resource_status` posture; the server's own slot limits | queue instead of dispatch; `session_close` |
| 3.2 Another level | the item's parts; `MAX_DEPTH` | dispatch a conductor, or flatten to workers |
| 3.3 Merge teams | shared files, a one-item line, bouncing handoffs | `work_ledger_record action=close`, then reseed |
| 3.4 Add a tracker | the compact ledger read; the stored item bound | a dispatched tracker worker writing one summary item |
| 3.5 Ownership | the file and output map the lead itself wrote | one owner per file, one integrator per output |

### 3.1 Capacity: read it, never assume it

Call `resource_status` before every wave, and again before a RESEED wave -- a
reseed stands up sessions exactly like a first dispatch, and is the wave a lead is
most likely to treat as free because the work is not new.

The posture is one of four words (`POSTURE_*` in
`src/kiro_crew/resource_status.py`): `ample`, `tight`, `critical`, `unknown`. On
`tight` or `critical`, dispatch what fits and QUEUE the rest as registered items
rather than standing them up. An item that exists and is not yet bound is visible
on the board; a wave the host could not carry is a set of sessions that thrash.

Close a child once its item is terminal AND its result has been read, with
`session_close`. Both halves matter: closing before the read loses the result,
and leaving it open after the read holds a slot for nothing.

The prompt hard-codes no session count. The ceiling is the server's own, in
`src/kiro_crew/dashboard/state.py`: `MAX_SLOTS_PER_CREATOR` (50) bounds what one
creator may hold and `MAX_LIVE_SLOTS` (500) bounds the host. The posture is the
tighter of the two readings in practice, because memory binds long before the
slot count does.

### 3.2 Another level: only for several independent long parts

A child conductor splits again only when its item has SEVERAL INDEPENDENT parts
that each run long. Independent, because parts that must be done in order are one
worker's sequence and splitting them buys handoffs instead of parallelism. Long,
because a level costs a session, a ledger and a summary; a level whose parts each
finish in one pass pays that for nothing. Otherwise the child flattens: it
dispatches workers directly.

The cap is real and it is low. `MAX_DEPTH = 2` in
`src/kiro_crew/work_ledger.py`, and three guards spell it differently:

| Guard | Refuses | So |
|---|---|---|
| `child_depth` | `depth + 1 > MAX_DEPTH` | a session at depth 1 MAY mint a child conductor at depth 2 |
| `ensure_conductor` | `checked_depth > MAX_DEPTH` | a ledger AT depth 2 is admitted |
| item creation | `record.depth >= MAX_DEPTH` | a conductor at depth 2 may never create an item |

Read together: a depth-2 conductor can be created and can then do nothing with
its ledger. So the safe tree today is **lead -> conductor -> workers**, and a
lead that dispatches a conductor from a conductor has built a level that refuses
its first dispatch. This document does not propose raising the cap or aligning
the guards; the mismatch is tracked in
[#18127](https://github.com/kirodotdev/KiroCrew/issues/18127). The rule is to
dispatch within the shape the guards already permit.

### 3.3 Merge teams: three triggers, one procedure

Merge two lines of work when any of these holds:

- They edit the same files. Two owners on one file is the failure 3.5 exists to
  prevent, and merging is the fix once it has already happened.
- One line is down to a single item. A line with one item is a worker with a
  conductor's overhead.
- Handoffs between them keep bouncing -- the same artifact crossing back and
  forth means the split was drawn in the wrong place.

The procedure: close one line's items with `work_ledger_record action=close`
(`close` is in both action sets in `src/kiro_crew/work_ledger.py`), then reseed
the surviving line with the remaining work AND the closed line's artifacts. The
artifacts are the point -- a reseed without them restarts work that was done.

`session_adopt` exists (`src/kiro_crew/mcp_dashboard.py`) and moves a session's
whole subtree under a new parent, which is the sidebar-shaped version of this
merge. It is deliberately WITHHELD from the auto-approve grants
(`src/kiro_crew/agent.py`), so every call raises an approval prompt. That makes
it right for a person consolidating conductors and wrong for an unattended
cycle, which is why the rule above goes through close-and-reseed instead. A lead
that wants the subtree moved asks the owner.

### 3.4 Add a tracker when one read stops fitting

Dispatch a dedicated tracker worker when any of these is true:

- The compact ledger read is no longer something the lead can act on in one pass.
  Compact is already the narrow form: `compact=true` returns `_COMPACT_ROW_FIELDS`
  in `src/kiro_crew/dashboard/handlers/work_ledger.py` -- the status columns and
  the derived flags, no acceptance, no artifacts, no events. When even that is too
  much, the board has outgrown the reader, not the format. A board may hold up to
  `WORK_STORED_ITEM_LIMIT` (256) items, well past the `DEFAULT_GOAL_ITEM_CAP` (20)
  a goal starts with.
- The work spans several ledgers or several machines, so no one read covers it.
- The lead has started skipping items -- the symptom that it already does not fit.

The tracker reads every ledger and the pull requests, and writes ONE summary item.
Where the lead is a crewmate it also fills the agentic dashboard fields
(`dashboard_fields` / `dashboard_write`, served from
`src/kiro_crew/dashboard/handlers/agent_panel.py`). The lead then reads only that
summary item, and reaches for a full read only when the summary says to.

**The tracker reports, never decides.** It is dispatched as an ordinary worker, so
its ledger verbs are `work_brief` and `work_report` (`WORKER_TOOLS` in
`src/kiro_crew/mcp_work.py`) and it holds no decision or verdict verb at all. The
separation is not a convention it is asked to respect; it is what a worker's
toolset already is.

### 3.5 Ownership: one owner, one integrator, a separate grader

- **One owner per shared file.** Two workers writing one file is the failure the
  split exists to avoid: the second push either loses the first or reverts it.
  Where two items need the same file, one owns it and the other says what it
  needs.
- **One integrator per output.** One merge queue, one deployer. A second writer
  to a shared output produces the same race one level up.
- **Testing or review is a separate worker.** An author does not grade its own
  work. The lead reads the grader's report, and an acceptance is still the
  evaluator's verdict against the item's own bar, as
  [`rfc-lead-crewmate`](rfc-lead-crewmate.md) capability 6 already requires.

## 4. Risks

- **A posture read that is stale by the time the wave lands.** The reading is a
  moment, and a wave takes seconds. It is still strictly better than a guessed
  count, and the queue-the-rest half means a wrong reading costs a delayed
  dispatch rather than a thrashing host.
- **A merge trigger that fires too early.** A one-item line is sometimes a line
  about to grow. The trigger is a prompt for the lead to decide, not an automatic
  close, and closing an item records the reason.
- **A tracker that becomes a second decider.** Its toolset prevents the verdict,
  but a summary that editorialises can still steer. The rule says what the summary
  carries; a tracker that recommends is a tracker to re-seed.
- **A lead that reads the depth cap as a target.** The shape in 3.2 is a ceiling,
  not a plan. Most goals are one level.

## 5. Security

No new capability class and no new reachable surface. Every mechanism named is
already granted to this role or already gated:

- `resource_status`, the compact ledger read, `work_brief` and `work_report` are
  reads or writes to the caller's own record, which is the property that made them
  auto-approvable.
- `work_ledger_record action=close` writes only the caller's own board.
- `session_adopt` stays WITHHELD. This document relies on that rather than asking
  for it, and section 3.3 routes the merge around it precisely so an unattended
  cycle never needs the prompt.
- `dashboard_write` lands only from a member thread, so the panel a lead writes is
  its own one. That scoping is unchanged.

## 6. Alternatives considered

- **A fan-out number in the prompt.** Rejected: a number right for one host is
  wrong for the next, and the reading that would correct it is already available.
- **Raise `MAX_DEPTH` so a lead can nest twice.** Out of scope and separately
  tracked ([#18127](https://github.com/kirodotdev/KiroCrew/issues/18127)). A
  deeper tree also multiplies sessions and summarises summaries, which is the cost
  the cap was chosen against.
- **Make the tracker a conductor so it can also unblock items.** Rejected: that is
  the same role doing both reading and deciding, which is the failure 3.4's last
  paragraph exists to prevent.
- **Enforce the rules in code rather than in a skill.** Rejected for now: four of
  the five key on judgements (independent, long, bouncing, no longer fits) that no
  guard can read. The limits that ARE mechanical -- the slot caps and the depth
  guard -- are already enforced in code, and these rules read them.

## 7. Open questions

1. Should the tracker's summary item live on the lead's own board or on a board of
   its own? Proposal: the lead's board, so one compact read still reaches it.
2. Is `session_adopt` worth an explicit operator-facing path for the merge case,
   given it already carries the approval prompt a consolidating person is present
   for? Left open; nothing here depends on the answer.

## 8. Rollout

One pull request, the one that carries this document. Exit criteria, each pinned
by a test there:

- The skill carries all five rules, and the prompt carries one pointer per rule
  rather than a copy.
- Each rule's named mechanism is pinned by a contract test with a unique phrase
  and a positive control, so a rule that loses its mechanism reddens.
- The depth rule states the cap as the guard's refusal, so a moved cap needs no
  prompt edit.
- No session count, fan-out width, or posture threshold is hard-coded in either
  the skill or the prompt.

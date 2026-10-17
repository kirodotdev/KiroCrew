---
title: Work ledger caps a goal's open items, and its item total becomes an advisory line
status: accepted
author: kirocrew agent session, directed by iamwhatever
created: 2026-10-12
last-audited: 2026-10-12
audited-at: 0a47021090
doc-pr: null
implementation-prs: [19338]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Work ledger caps a goal's open items, and its item total becomes an advisory line

Status: accepted. The maintainer (iamwhatever) accepted this design in the
conductor chat on 2026-10-12 at about 06:12 UTC: a hard cap on open items, the
total cap advisory only. The implementation is open as
[#19338](https://github.com/kirodotdev/KiroCrew/pull/19338) and rebases onto the
base that carries this document. Every code reference below was read at
`0a47021090`.

This document lands on its own because it changes what the goal conductor does by
default: which `create` the work ledger refuses, and what the conductor asks the
user when refused. The First Principles lane reads that decision from the base
branch. It amends [`rfc-conductor-work-ledger.md`](rfc-conductor-work-ledger.md)
in its own document, so that one is not edited here.

## 1. Problem

`_create_item` in `src/kiro_crew/work_ledger_writes.py` refuses a goal board's `create`
with `goal_item_cap_reached` once `goal_items_used >= item_cap`. `item_cap`
defaults to 20 (`DEFAULT_GOAL_ITEM_CAP`) and `goal_items_used` counts every item
the current goal has created: closed ones, and every round's re-plans. Only a
`goal` write with `new_goal=true` starts the count again.

So a goal that dispatches items which land and close quickly is refused with
nothing running. The conductor prompt (`src/kiro_crew/agent.py`,
`_CONDUCTOR_SYSTEM_PROMPT`) and the goal-conductor skill
(`src/kiro_crew/builtin_skills/goal-conductor/SKILL.md`) then tell it to stop
dispatching and ask the user whether to "spend more". The number the user is asked
about is the goal's history, not its load.

The user's ruling, given on the dispatch for #19338: cap parallel work, not lifetime
totals. Keep the total visible.

## 2. What bounds spend

Each open work item is a live worker session: a model turn budget, a sandbox, host
memory. A closed item costs nothing further. The thing a bound protects is how many
of those run at once. Today's code already knows this for the store:
`MAX_ITEMS_PER_CONDUCTOR = 32` counts only open items and frees a seat on close.
But 32 is the store's safety bound for every board, far past what one goal should
run; nothing bounds a single goal's fan-out below it except the prompt.

The lifetime total is still worth seeing. A goal that has created 40 items against
an expected 20 is information the user wants in a round report. It is not a reason
to stop a goal whose items are landing.

## 3. Decision

Two numbers on the goal's board header, each doing one job:

| Field | Default | Range | Job |
|---|---|---|---|
| `concurrency_cap` (new) | 6 | 1..32 | hard: a `create` on a board that has recorded a goal is refused `goal_concurrency_cap_reached` (409) when the board's OPEN items are at or past it; writes nothing |
| `item_cap` (existing) | 20 | 1..256 | advisory: a `create` past it is admitted; `work_ledger_read` returns `cap_exceeded: true` beside `goal_items_used` on the `conductor` block, compact reads included |

- Both are set with `work_ledger_record action=goal`. Closing an item frees a seat
  with no write.
- The ceiling on `concurrency_cap` is the store's open bound (`MAX_ITEMS_PER_CONDUCTOR`,
  32), for the reason `MAX_GOAL_ITEM_CAP` is pinned to the stored bound: a cap the
  store refuses first would only hide that refusal behind a different code.
- `cap_exceeded` is `goal_items_used > item_cap` on a board with a recorded goal:
  true from the first create past the cap, where the refusal fired before; false on a
  queue board (pipeline, security conductors), which never writes a goal and whose
  count is its whole history.
- The `goal_item_cap_reached` code is removed, not kept dormant.
- The store's checks run in order: stored total, then the store's open bound, then
  the goal's concurrency cap. A cap at its ceiling therefore yields the store's own
  `item_cap_exceeded`.
- A rebuild from the crew log keeps both caps from a clean cache in step with the
  log, as it keeps `item_cap` today; a dirty cache falls back to both defaults.
- A header from before the field reads `concurrency_cap` as 6, so an upgraded board
  is bounded like a new one on its next create. Items already open stay open.

The conductor prompt and the goal-conductor skill say: a concurrency refusal is
about parallel work; wait for an item to land, or ask the user to raise
`concurrency_cap`. The total is a line in every round report
(`items used: N of <item_cap>`), and when `cap_exceeded` is true the conductor says
so in that report and carries on. The no-progress check (two rounds, nothing
accepted) is unchanged.

## 4. Not decided here

- `MAX_ITEMS_PER_CONDUCTOR` (32) and the stored bound (256) keep their values.
  [`rfc-work-fold-forget-closed.md`](rfc-work-fold-forget-closed.md) decides the
  stored bound separately.
- Queue boards that never record a goal keep only the store's bounds, as they did
  under `item_cap`.
- Neither cap rides the crew-log fold.
- The 6 and the 20 are defaults a user may want to tune; nothing here makes them
  configuration.

## 5. Alternatives considered

- **Keep the lifetime refusal and raise its default.** Any total stops a healthy
  goal eventually, and the conductor still asks about history rather than load.
- **Drop `item_cap` entirely and keep only `goal_items_used`.** Loses the user's
  own expected total; the round report could say "items used: 40" but not "of 20",
  and the conductor would have no signal to surface. The user asked for the line and
  the flag.
- **Bound concurrency by lowering `MAX_ITEMS_PER_CONDUCTOR`.** That bound protects
  the store and the fold for every board, queue boards included; a goal's fan-out is
  a per-goal choice.

## 6. Acceptance

Accepted by the maintainer (iamwhatever) in the conductor chat, 2026-10-12 ~06:12
UTC: cap parallel work (open items) in code; keep the lifetime total as an advisory
line, not a refusal. Recorded here as `accepted` on landing, so #19338 rebases onto
the base that carries it.

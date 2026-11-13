---
title: Work ledger caps a goal's open items, and its item total becomes an advisory line
status: partial
author: kirocrew agent session, directed by iamwhatever
created: 2026-10-12
last-audited: 2026-11-13
audited-at: 9b32941be6
doc-pr: null
implementation-prs: [19338]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Work ledger caps a goal's open items, and its item total becomes an advisory line

Amended 2026-11-09 by
[`rfc-drop-goal-item-cap.md`](rfc-drop-goal-item-cap.md): the advisory
`item_cap` and `cap_exceeded` of §3 are removed; `concurrency_cap` and
`goal_items_used` stand.

Amended 2026-11-13 by §7 below, by the repository owner's decision: the
default `concurrency_cap` becomes `MAX_ITEMS_PER_CONDUCTOR` (32), the field
stays for a bound the user sets, and `resource_status` decides each wave. §3 to
§6 are kept as the record of the 2026-10-12 decision. §3 shipped in #19338;
§7 is not built. §7 relies on
[`rfc-work-ledger-resource-pressure.md`](rfc-work-ledger-resource-pressure.md)
for the host signal that replaces the fixed 6.

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

## 7. Amendment (2026-11-13): no fixed default; resources decide the wave

Decided 2026-11-13 by the repository owner in the conductor chat. The owner's
words: "有人也提到这个DEFAULT_GOAL_CONCURRENCY_CAP对于ultra mode没有用，是不是卡Bot
worker的？我觉得我们不应该微操，依赖资源探取。" (Someone said the default cap does
nothing for Ultra Mode; is it holding back Bot workers? We should not
micro-manage; rely on probing resources.)

This reverses an earlier owner ruling. While `item_cap` was being removed
([`rfc-drop-goal-item-cap.md`](rfc-drop-goal-item-cap.md), 2026-11-09), the
owner kept `concurrency_cap` at 6 to stop an agent opening too many workers by
itself. The owner has now made the opposite call: the bound on parallel work
comes from the host's resources, not from a constant. Every code reference in
this section was read at `9b32941be6`.

### 7.1 What the 6 does today

- It is not Bot-only and not dead for Ultra Mode. The check is
  `live.goal_version > 0 and len(open_items) >= live.concurrency_cap` in
  `_admit_create_locked` in `src/kiro_crew/work_ledger_writes.py`, which both `_create_item` and `create_bound_item` call. It binds any
  board that has recorded a goal.
- Ultra Mode's team lead always records a goal, so it is always bound. A Bot
  (crewmate) conductor records a goal only for multi-item work, so it is bound
  then. Queue boards and a remote peer's `hub:` board never record a goal and
  get only the store's bound of 32. Native subagents are not ledger items.
- The cap is per board, not per goal tree: a child conductor gets its own 6,
  so one tree can hold 6 x 6 = 36 sessions. That, and subagents escaping it, is
  the likely source of "useless for Ultra Mode".
- The 6 was never measured. §2 names spend and host memory as the reason, and
  §4 already called it a default a user may want to tune.
- The team-lead skill already says the opposite of a fixed number: "Carry no
  number of your own" (`src/kiro_crew/builtin_skills/team-lead/SKILL.md`, section 5).

### 7.2 Decision

| Change | Where |
|---|---|
| `DEFAULT_GOAL_CONCURRENCY_CAP = MAX_ITEMS_PER_CONDUCTOR` (32). With §3's check order, a board at 32 open items is refused by the store's `item_cap_exceeded` first, so the goal check never fires by default. | `DEFAULT_GOAL_CONCURRENCY_CAP` in `src/kiro_crew/work_ledger_records.py` |
| The field stays (1..32, set with `work_ledger_record action=goal concurrency_cap=<N>`). It is the user's way to set a bound, and a user-set value still refuses `goal_concurrency_cap_reached`. | unchanged |
| The refusal text says wait for a seat: dispatch nothing new until an item lands. It no longer says "ask the user and raise it", which already contradicts the conductor prompt's "never ask to raise it". | the `goal_concurrency_cap_reached` message in `_admit_create_locked` (`src/kiro_crew/work_ledger_writes.py`) and its copy in `src/kiro_crew/mcp_work.py` |
| The conductor rule drops "at most 6 open" and says: before each wave, `resource_status` decides how many to start; a bound the user set is `concurrency_cap`; on a `host_pressure` record, follow the pressure RFC. | `spend_bounded` in `src/kiro_crew/agent_parts/spec_prompts.py`, the "Rounds are not gated, but spend is bounded" section of `goal-conductor/SKILL.md` |
| The Bot conductor gets the same `resource_status` wave rule. Its prompt has none today (`crewmate_prompts.json`: no hits). The shared rule keeps Bot and Ultra Mode in parity (`test/test_mate_conductor_parity.py`). | `src/kiro_crew/crewmate_prompts.json` or the shared conductor rules in `spec_prompts.py` |
| Tests that pin 6 move to the new default. | `test/test_work_ledger_caps.py` and the other ledger tests that reference the 6 |

Compatibility: every header write stores `concurrency_cap`
(`ConductorRecord.to_dict` in `src/kiro_crew/work_ledger_records.py`), and a `goal` write
that names no cap keeps the stored one (the `goal` branch of `src/kiro_crew/work_ledger_writes.py`). So a
header with no key reads as the new default, and a board that already stored
6 keeps 6: it cannot be told apart from a user who chose 6. That board runs
under 6 until a `goal` write names another value, or until it is replaced by a
new board. New boards start at 32. No migration rewrites stored values.

### 7.3 Linked to the pressure signal

Dropping the fixed 6 relies on
[`rfc-work-ledger-resource-pressure.md`](rfc-work-ledger-resource-pressure.md).
A local worker session from `session_create` has no host-memory refusal of its
own; only the cold-start queues and the counts (32 per board, 50 per creator,
500 live) apply. The pressure RFC puts the host's memory posture on the
ledger, wakes the conductor when it turns tight or critical, and refuses a
`create` at critical. Without it, a conductor that skips `resource_status` can
fan out to 32 worker chats per board on a host that cannot hold them. The two
code changes should land in the order pressure first, default second, or in
one PR.

### 7.4 Not decided here

- Deleting the field or its code path. It is the user's only goal bound, and
  removing it touches the schema, the fold and validation for no gain.
- A memory floor on `session_create` itself. If Bot fan-out ever overloads a
  host even with the pressure signal, reusing `check_memory_available` there
  (as remote dispatch does) is the upgrade.
- `MAX_ITEMS_PER_CONDUCTOR`, `MAX_SLOTS_PER_CREATOR` and `MAX_LIVE_SLOTS` keep
  their values.

### 7.5 Alternatives considered

- **Keep 6.** The owner's earlier ruling. Rejected by the owner: a constant
  nobody measured blocks a healthy host and does nothing for a starved one.
- **Delete `concurrency_cap`.** Removes the user's bound. See 7.4.
- **Raise the default to another constant (say 12).** Still a guess; it moves
  the problem without tying it to the host.

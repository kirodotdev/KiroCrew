---
title: The work ledger drops the advisory goal item cap and its one-time notice
status: accepted
author: kirocrew agent session, directed by the repository owner
created: 2026-11-09
last-audited: 2026-11-09
audited-at: 1e3b894d87
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: [rfc-conductor-item-total-notice.md]
superseded-by: []
---

# RFC: The work ledger drops the advisory goal item cap and its one-time notice

Status: accepted. The repository owner made this decision on 2026-11-09 in the
conductor chat. It takes effect when this document merges; the code change
rebases onto the base that carries it. Every code reference below was read at
`1e3b894d87`.

This document lands on its own because it removes a field from a first-class
agent's tool (`work_ledger_record action=goal item_cap`) and a rule from the
goal conductor's prompt, and the First Principles lane reads that decision from
the base branch. It supersedes
[`rfc-conductor-item-total-notice.md`](rfc-conductor-item-total-notice.md) and
amends §3 and §5 of
[`rfc-work-ledger-concurrency-cap.md`](rfc-work-ledger-concurrency-cap.md).

## 1. Problem

Since [#19338](https://github.com/kirodotdev/KiroCrew/pull/19338) the goal's
`item_cap` refuses nothing. `ConductorRecord.cap_exceeded` in
`src/kiro_crew/work_ledger_records.py` turns true once `goal_items_used >
item_cap` (default `DEFAULT_GOAL_ITEM_CAP` = 20), and the conductor prompt
(`spend_bounded` in `src/kiro_crew/agent_parts/spec_prompts.py`) and the
goal-conductor skill tell the conductor to say so once and to keep an
`item_cap_notice` artifact in its session ledger at every round close.

The owner reports that nobody acts on the notice: it is raised on nearly every
goal, and it costs a prompt rule, a session-ledger artifact rewritten every
round, a tool field and a header field. The number it guards (how many items a
goal has created) is still worth seeing; the line it is measured against is not.

## 2. Decision

- `item_cap` is removed from the board header, from `work_ledger_record
  action=goal`, from its JSON schema and from `work_ledger_read`.
  `DEFAULT_GOAL_ITEM_CAP`, `MAX_GOAL_ITEM_CAP` and `cap_exceeded` go with it.
- The conductor prompt and the goal-conductor skill drop the item-cap notice
  and the `item_cap_notice` session-ledger artifact. The rest of the
  `spend_bounded` rule (the concurrency cap and the no-progress check) stays.
- `goal_items_used` and `created_total` stay on the `conductor` block of
  `work_ledger_read`, so a round report can still state the goal's total.
  `new_goal=true` still restarts the count.
- `concurrency_cap` (default 6, 1..32) and the store's
  `MAX_ITEMS_PER_CONDUCTOR` (32, wire code `item_cap_exceeded`) are unchanged.

Compatibility:

- A header on disk that still carries `item_cap` reads without error; the key
  is ignored and dropped on the next header write.
- A `work_ledger_record` call that still passes `item_cap` is refused as an
  unknown field (400), and nothing is written. Refusing is the schema's
  existing behaviour for any unknown key, so it needs no new code, and it tells
  a stale caller the field is gone instead of letting it believe a cap was set.

## 3. Amends

- `rfc-work-ledger-concurrency-cap.md` §3: the `item_cap` row of the table and
  the advisory-total paragraph no longer hold. §5's rejected alternative ("Drop
  `item_cap` entirely and keep only `goal_items_used`") is the path taken now:
  the total stays visible without a line to measure it against.
- `rfc-conductor-item-total-notice.md`: superseded in full.

## 4. Not decided here

- Whether round reports must state the total. The count is readable; the
  prompt does not require it.
- Any change to `concurrency_cap`, `MAX_ITEMS_PER_CONDUCTOR` or the stored
  bound (256).

## 5. Alternatives considered

- **Keep `item_cap` but drop only the notice.** Leaves a field that no code
  path and no prompt reads.
- **Ignore an `item_cap` argument instead of refusing it.** Needs a kept schema
  entry and a drop path for a field with no effect, and a stale caller would
  believe a cap was set.

---
title: The Ultra Mode conductor takes stated defaults instead of asking
status: accepted
author: kirocrew agent session, directed by iamwhatever
created: 2026-11-06
last-audited: 2026-11-06
audited-at: 0984ba735f
doc-pr: 31878
implementation-prs: [31822]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: The Ultra Mode conductor takes stated defaults instead of asking

Status: accepted. The repository owner (iamwhatever) made this decision on
2026-11-06 in the Ultra Mode conductor chat, after reading the stop-point
research summarised in section 1. It takes effect when this document merges.
The implementation is
[#31822](https://github.com/kirodotdev/KiroCrew/pull/31822), which rebases onto
the base that carries this document. Every code reference below was read at
`0984ba735f`.

This document lands on its own because it changes what the goal conductor does
by default, and the First Principles lane reads that decision from the base
branch. It amends two rules of
[`rfc-work-ledger-concurrency-cap.md`](rfc-work-ledger-concurrency-cap.md) §3
(asking to raise `concurrency_cap`, and the two-round no-progress check kept
"unchanged") in its own document, so that one is not edited here.
[`rfc-conductor-item-total-notice.md`](rfc-conductor-item-total-notice.md)
lists the two-round check as unchanged too; its own rule (the item-total
notice) is untouched.

## 1. Problem

Ultra Mode runs a chat on `kirocrew-team-lead` (`DEFAULT_CREW_MODE_AGENT` in
`src/kiro_crew/crew_mode.py`), which follows the `goal-conductor` skill
unchanged. An audit of every place that procedure stops for the person found
five `ask_question` steps where a clear default exists, plus a milestone rule
that invites `[OPTIONS: ...]` choices in every report. An unattended goal then
sits for hours on a question whose answer the conductor already knows.

| # | where on `0984ba735f` | trigger | what it does today |
|---|---|---|---|
| 6 | `goal-conductor/SKILL.md`, "A `pending` acceptance has a budget" | an item reads `pending` for 24 h (`pending_overdue`) | asks: keep waiting or drop |
| 9 | SKILL Patrol; `patrol_budget` rule in `agent_parts/spec_prompts.py` | `patrol_budget.py check` exit 21 (runtime under 3000 s) | asks for a longer runtime |
| 10 | SKILL Patrol renewal table | `patrol_budget.py renew` exit 30 (renewal cap spent) | asks for another budget |
| 13 | SKILL "No progress"; `spend_bounded` in `spec_prompts.py` | two rounds in a row, nothing accepted | asks; dispatches nothing until answered |
| 14 | SKILL "Concurrency cap"; `spend_bounded` | wants more than `concurrency_cap` open | asks to raise the cap |
| 16 | `talking_to_the_person` in `spec_prompts.py` | every milestone | answers go in `[OPTIONS: ...]`, so milestone turns end offering choices |

A rule that lives only in session context cannot fix this: session context is
rendered under `[SESSION CONTEXT — background reference only` and ranks below
the agent prompt and the skill steps that ask.

## 2. Decision

The conductor takes a stated default and says what it did. It asks the person
only when a default cannot settle the choice.

- **#6 `pending_overdue`.** The first time, re-arm the bar with `action=accept`
  (a fresh 24 h), record `action=decide` with a reason starting
  `pending overdue: re-armed once`, and say so in the report with the evidence.
  The second time, close the item `abandoned` and report it.
- **#9 exit 21.** Re-run `check` with a 3000-second runtime, the shortest a
  patrol can hold, arm with that, and tell the person. The script itself still
  never raises a runtime.
- **#10 exit 30.** Below the 7-day ceiling, carry on:
  `_extend_for_open_ledger` in `src/kiro_crew/autonudge_service/firing.py`
  already raises a spent `max_cycles` or `max_runtime_secs` on a
  `watch="work-ledger"` loop while ledger items are open, up to
  `monitoring.max_runtime_secs` (604,800 s as shipped). At that ceiling the
  gateway stops extending, so the conductor still asks for another budget,
  naming the open items.
- **#13 no progress.** Two empty rounds in a row is a statement to the person,
  not a gate: the conductor names what failed, narrows to the failing items and
  re-plans once. Only a third empty round in a row stops new dispatches until
  the person answers. This amends the concurrency-cap RFC's "unchanged" check.
- **#14 concurrency cap.** Never ask to raise it; wait for a seat. A bound the
  person sets is still applied with `action=goal` `concurrency_cap=<N>`. This
  amends the concurrency-cap RFC's "or ask the user to raise
  `concurrency_cap`".
- **#16 and an agent-prompt rule.** One short section, carried by both the
  `kirocrew-conductor` and the `kirocrew-team-lead` agent prompts: act on a
  clear recommendation and report; ask only for credentials or a permission
  only the person can grant, spend, an irreversible step or deleting or
  overwriting someone else's work, a costly preference that cannot be inferred,
  or a choice the person said they want to make. A milestone report is a
  statement; `[OPTIONS: ...]` only when it carries one of those asks.

## 3. What does not change

- A `human_approval` acceptance is still asked.
- The needs-human checklist step 1 (credentials, spend, deleting or
  overwriting someone's work, irreversible) still never takes a default.
- The `/ai-review override` rules: no agent posts an override.
- The work ledger's code: the cap is still enforced by `_create_item`
  (`goal_concurrency_cap_reached`), and no schema, tool or record changes.
- The Round-0 go-ahead.

## 4. Alternatives considered

- **Session-context rule only.** Rejected: it ranks below the skill steps that
  ask (section 1).
- **Keep the asks and auto-answer them.** Rejected: it keeps the blocking card
  and adds a mechanism; a stated default is text only.
- **Ask on the second overdue instead of abandoning.** Rejected by the owner:
  the item has then waited 48 hours on a check that does not report, and the
  report names it with its evidence, so the person can reopen it.

## 5. Risk and rollback

Prompt and skill text only; a revert of the implementation PR restores every
ask. The risky choices (credentials, spend, delete, irreversible) keep their
ask, so a wrong default here costs a re-plan or a re-opened item, not data.

## 6. Source

The stop-point audit that this decision rests on read `goal-conductor/SKILL.md`,
`agent_parts/spec_prompts.py`, `autonudge_service/firing.py` and
`monitoring/limits.py`; its findings are restated in section 1 so this document
stands on its own.

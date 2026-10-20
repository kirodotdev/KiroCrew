---
title: The goal conductor keeps its item total out of round reports and says it once past the cap
status: accepted
author: kirocrew agent session, directed by adlio
created: 2026-10-20
last-audited: 2026-10-20
audited-at: 6432cad9ea
doc-pr: null
implementation-prs: [22417]
tracking-issues: [22415]
supersedes: []
superseded-by: []
---

# RFC: The goal conductor keeps its item total out of round reports and says it once past the cap

Status: accepted once this document merges. adlio (repository admin) made the
decision in the conductor chat on 2026-10-20 and asked for it to land as its own
RFC first; the approving maintainer's merge records acceptance. The
implementation is open as
[#22417](https://github.com/kirodotdev/KiroCrew/pull/22417) and rebases onto the
base that carries this document. Every code reference below was read at
`6432cad9ea`.

This document lands on its own because it changes what the goal conductor says
to the user by default, and the First Principles lane reads that decision from
the base branch. It amends one sentence of
[`rfc-work-ledger-concurrency-cap.md`](rfc-work-ledger-concurrency-cap.md) §3
in its own document, so that one is not edited here.

## 1. Problem

`rfc-work-ledger-concurrency-cap.md` §3 made the goal's item total advisory and
specified how the conductor surfaces it: "The total is a line in every round
report (`items used: N of <item_cap>`)". The conductor prompt
(`_CONDUCTOR_SYSTEM_PROMPT` in `src/kiro_crew/agent.py`, its "Rounds are not
gated, but spend is bounded" paragraph) and the goal-conductor skill
(`src/kiro_crew/builtin_skills/goal-conductor/SKILL.md`, its "Item cap
(advisory)" bullet) carry that instruction, and `src/kiro_crew/docs/work-ledger.md`
documents it.

In use, the line works against the user:

- **It reads as the user's quota.** Conductors render it as "Tasks used: 5 of 6"
  at the foot of every report. Users take it as a budget they are spending and
  must manage, and ask what it means
  ([#22415](https://github.com/kirodotdev/KiroCrew/issues/22415)). The
  conductor plans and dispatches the items; the user does neither.
- **It asks nothing of the user until the cap is passed.** Nothing is refused
  past `item_cap`, so below it the number carries no decision. The one report
  that matters, the first past the cap, looks like every report before it.
- **The "of M" is sometimes wrong.** Conductors have put the concurrency cap (6)
  or a count of finished items in that slot, so the line can misinform as well
  as distract.

## 2. Decision

The item total is the conductor's to manage. It stays out of routine round
reports. The first report after `cap_exceeded` turns true tells the user once,
in plain words, how many work items the goal has needed against the cap it
planned for, for example: "This goal has needed 23 work items so far, more than
the 20 I planned for. I'm carrying on; say stop if that's more than you want."
Then the conductor carries on.

Two constraints from the conductor's existing round-close rule shape how:

- **The notice is a statement, not a question.** The conductor resets its chat
  at every round close, and never while a question to the person is unanswered
  (the prompt's "Drop your chat at a round close" paragraph and the skill's
  "Then drop your chat" step). A "keep going?" question would hold
  every reset until the user replied.
- **"Once" has to survive that reset.** The conductor's memory of having said it
  goes with the chat, so the state lives in its session ledger as an
  `item_cap_notice` artifact. Every round-close `session_ledger_record` writes it:
  `told the user at N` while `cap_exceeded` is true, `none` while it is false.
  A `goal` write that sets `new_goal=true` or changes `item_cap` also writes
  `none`, in the same turn and before any dispatch. The notice fires only on a
  round where `cap_exceeded` is true and the stored value does not start with
  `told`.

Rewriting the key every round matters for two reasons:

- **The artifacts map is bounded.** It keeps at most 32 keys
  (`_MAX_ARTIFACTS` in `src/kiro_crew/session_ledger_layout.py`) and the ledger
  fold in `src/kiro_crew/crew_log/projection_ledger.py` drops the oldest first
  once it holds more (`LEDGER_ARTIFACT_LIMIT`). A
  re-recorded key moves to the newest position, so a key written every round
  ages out only if 32 other keys are written within one round, which no
  conductor flow comes near. The event tail (20 entries) has the same problem
  with no such remedy, which is why the marker is not an event.
- **The goal write clears it at once.** Waiting for the next round close is not
  enough: a replaced goal with a small `item_cap` can pass it before that write,
  and a raised `item_cap` below the count so far leaves `cap_exceeded` true. In
  both cases the old `told` would suppress the notice the new goal or new cap
  deserves. Clearing on the `goal` write itself means the next report past the
  cap names the cap the user now has.

Unchanged: `item_cap`, `concurrency_cap` and their defaults (20 and 6);
`goal_items_used` and `cap_exceeded` on `work_ledger_read`'s `conductor` block,
compact reads included; the concurrency refusal; the two-rounds no-progress
check; and the loop's own cycle budget, both the `[patrol budget: cycle N/M]`
line a wake carries and the dashboard's "Goal active (cycle N/M)" chip. That
count measures the loop the user armed, not work the conductor plans, and
it lives outside the chat.

## 3. Amendment to `rfc-work-ledger-concurrency-cap.md`

§3's sentence "The total is a line in every round report
(`items used: N of <item_cap>`), and when `cap_exceeded` is true the conductor
says so in that report and carries on" is replaced by §2 above. The rest of
that RFC stands, including its reason for keeping `item_cap`: the user's
expected total is what gives the conductor a signal to surface.

## 4. Change

One PR, [#22417](https://github.com/kirodotdev/KiroCrew/pull/22417):

- the conductor prompt in `agent.py` and the goal-conductor skill carry §2's
  rule, in the same words;
- `src/kiro_crew/docs/work-ledger.md`, the `cap_exceeded` docstring in
  `src/kiro_crew/work_ledger_records.py` and the read comment in
  `src/kiro_crew/dashboard/handlers/work_ledger.py` describe it;
- `test/test_conductor_agent.py::test_back_to_back_rounds_keep_a_spend_bound`
  pins both copies: the marker, its `none` mirror, and the absence of the
  per-report line and of a "keep going?" question.

**Exit criteria:** neither the prompt nor the skill contains
`items used: N of` or "in every round report"; both name the `item_cap_notice`
marker, its `none` mirror at round close, and its clearing on a `goal` write
that sets `new_goal=true` or changes `item_cap`; the test above passes.

## 5. Backward compatibility

No API, field or stored state changes. A conductor already running keeps its
loaded prompt until its session restarts; the next wake after an upgrade reads
no `item_cap_notice` and, if its goal is already past the cap, says so once.

## 6. Security considerations

None. The change is instruction text and one session-ledger key holding a
short string the conductor writes about its own goal.

## 7. Alternatives considered

- **Keep the line, fix the label.** Correct wording ("work items used") still
  puts a meter in front of the user every round and still buries the one report
  that matters.
- **Show the count only in the dashboard.** It is already readable from the
  ledger; a new surface is more than the problem needs.
- **Track "already told" in code, on the work ledger's `conductor` header.** A
  flag the ledger flips once when `cap_exceeded` first turns true, and resets on
  `new_goal=true` or a raised cap, would replace an instruction with state the
  code keeps. It adds a stored field, a write path and its tests for one
  sentence of chat. Worth revisiting if conductors are seen to repeat or skip
  the notice in practice.
- **Ask "keep going?" at the cap.** Rejected for the reset reason in §2.

## 8. Open questions

None.

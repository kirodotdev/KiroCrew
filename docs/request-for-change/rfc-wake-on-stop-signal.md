---
title: Wake on a stop signal -- only a worker's explicit report wakes its conductor, and a turn end or a close wakes nobody
status: in-progress
author: Raymond Chen, with kirocrew-worker
created: 2026-10-10
last-audited: 2026-10-10
audited-at: d32c084921
doc-pr: null
implementation-prs: [18854]
tracking-issues: [18836]
supersedes: []
superseded-by: []
---

# RFC: Wake on a stop signal

- Status: in-progress. The decision was made by the product owner on 2026-10-10,
  as the ruling on [#18836](https://github.com/kirodotdev/KiroCrew/issues/18836):
  a worker never wakes its conductor implicitly, and before it stops it states
  why through `work_report`. This document lands on its own first, because the
  First Principles lane reads an RFC's status off the base branch and the
  implementation changes what a `work-ledger` watch does by default.
- Amends [rfc-crew-log-wake.md](rfc-crew-log-wake.md) in three named places:
  its Goal 2, its section 3.2 (trigger two, a worker session closes) and its
  section 3.2b (trigger three, a worker's turn ends). Section 4 below says what
  survives of each. That document is `implemented` and is not edited; this one
  carries the amendment, the way
  [rfc-crewmate-guides-and-mate.md](rfc-crewmate-guides-and-mate.md) and
  [rfc-question-card-auto-submit.md](rfc-question-card-auto-submit.md) carry
  theirs.
- Related: [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md) (the
  ledger, `work_report` and the Phase 3 wake gate),
  [rfc-work-ledger-person-wait-hold.md](rfc-work-ledger-person-wait-hold.md)
  (the other reason a patrol spends no turn).
- Measured at `d32c084921`.

## 1. Problem

A bound worker's conductor is woken implicitly. Three triggers pull its armed
`work-ledger` loop forward through `AutoNudgeService.fire_now`, and two of them
observe something the worker did not say:

| trigger | entry point | what it observes |
|---|---|---|
| one | `conductor_wake._observe_event` | the `work` fold advanced: any item whose `last_report_at` moved, `progress` included |
| two | `slot_lifecycle._wake_conductor_for_closed_worker` | the worker's dashboard session is gone |
| three | `timers._wake_bound_conductor` | a worker's turn ended, with any outcome |

All three rest on one premise, which `rfc-crew-log-wake` section 3.2b states
outright: a turn ending is the ending "a conductor most wants to hear about".
The premise holds only while a worker's turn ends when its work does.

It stops holding as soon as `wait` becomes a stop-and-recover mode. A worker
that parks itself ends its turn and is resumed later, so every wait fires
trigger three, and the conductor is woken to be told nothing happened.

A false wake is not free, and `rfc-crew-log-wake` itself records why. Its
section 3.2b accepts a `progress`-then-turn-end push on the grounds that "the
probe answers quiet ... and no turn is spent". That is true of the probe and not
of the loop: the pushed tick still runs the probe, still counts against
`max_cycles`, and still advances the quiet streak whose floor delivers a turn
anyway. `conductor_wake.ITEM_PULLS_PER_HOUR` exists because of exactly that --
its own comment says a worker writing `progress` in a loop would otherwise buy
its conductor a floor turn every `_MAX_QUIET_STREAK` writes. A cap on a wake
nobody wanted is a symptom, not a fix.

## 2. Goals and non-goals

Goals:

1. A worker wakes its conductor only by saying something the conductor must act
   on: a `work_report` whose status is `blocked`, `question` or `done`.
2. A turn that ends having reported nothing wakes nobody, and nothing is
   inferred from the ending.
3. A worker's session closing wakes nobody.
4. One set of waking statuses, read by the trigger and by the gate, so a trigger
   can never push for news the gate refuses.
5. Liveness for a crashed or silent worker is unchanged: the item's own `stale`
   and `orphaned` flags, read on the conductor's own patrol tick.

Non-goals:

- Changing the staleness conjunction. `work_ledger.is_stale`,
  `STALE_ELIGIBLE_STATUSES` and `_worker_owns_next_move` stay exactly as they
  are, including the `worker_closed` arm `rfc-crew-log-wake` section 3.2 added.
- A new status or a new report field. `work_report`'s four statuses and its
  `reason` vocabulary are untouched: a worker that parks itself reports
  `progress`, which is already the status that wakes nobody.
- Making `wait` a stop-and-recover mode. This removes an obstacle in front of
  that change; it is not that change.
- A configuration switch. Phase 3 decided the gate has none and recorded why,
  and a switch here would mean two wake contracts to reason about.
- Lowering the patrol interval band. The `goal-conductor` skill's 300..900
  seconds stays, and it is what bounds how late an orphan is noticed.

## 3. Design

### 3.1 One trigger, gated on the signal

Trigger one survives, narrowed. `conductor_wake` keeps its keyed crew-log bus
subscription per watched board, diffs each item's report stamp as before, and
fires only for an item whose status is in a new shared set:

```python
# kiro_crew/work_vocab.py
WORK_WAKE_STATUSES: tuple[str, ...] = ("done", "blocked", "question")
```

The set lives in `work_vocab` because both readers need it and neither may
import the other: `ledger_wake` is the watch's pure gate and must not grow
imports, and `conductor_wake` is reachable from the gateway's boot path.
`ledger_wake.WAKE_STATUSES`, which already held these three values as its own
literal, is derived from it. Two spellings would let a trigger push for news the
gate refuses, which is the one wasted wake nobody can see.

The rendered board the bus event carries already holds each item's status, so
the gate costs no store read and no second lookup.

### 3.2 Trigger two retires: a close is not the worker's word

A close is the dashboard's event. A conductor woken by one cannot tell it apart
from the worker having spoken, which is the implicit wake this design removes.

What `rfc-crew-log-wake` section 3.2 bought is already covered without a window,
by the probe input that same section added. `work_ledger.is_stale`'s
`worker_closed` arm flags an open item whose worker reported and then vanished
at once, no staleness window required, and the conductor's patrol tick is its
delivery. That arm stays. So does everything section 3.2 says about reading
existence through `slot_exists` rather than `get_slot`, and about keeping the
window for an item that has never reported.

That arm also covers a case the trigger never did: a worker whose process dies
with its slot still open. The trigger observed a close; the flag observes an
absence of reports, which is the larger set.

The cost is latency on an orphan: one patrol interval rather than seconds. The
conductor prompt and the `goal-conductor` skill are changed to say so, and to
tell a conductor to read the `stale` and `orphaned` flags on every cycle.

Retiring the trigger is a deletion, not a second gate: the helper
(`_wake_conductor_for_closed_worker`), its two call sites in `close_slot` and
the facade re-export all go. A gated close trigger would be a second status
check that has to stay in step with section 3.1's, for the latency of one
interval on a case the flags already carry.

### 3.2b Trigger three retires: a turn end says nothing about the turn

`notify_turn_complete` is called once per turn end however the turn ended, which
section 3.2b chose deliberately so that no outcome vocabulary had to be kept in
step with the runner's. The same property is what makes the trigger unusable
once a worker can stop without finishing: a turn that raised, a turn that
produced nothing, a turn that reported `progress` and a turn that parked itself
in a wait all reach it identically, and the ending carries no evidence of which
one it was.

So the contract inverts. Rather than the gateway inferring a result from an
ending, the worker states where it is before it stops:

| the worker's last word before it stops | wakes the conductor |
|---|---|
| `done` | yes -- it must verify the claim |
| `blocked` | yes -- it must clear or re-plan around the dependency |
| `question` | yes -- the decision is its own |
| `progress` (including a worker parking itself in a wait) | no |
| nothing at all | no |

The three waking values are `WORK_WAKE_STATUSES`; the two non-waking rows are
everything else, so the sets are closed by construction rather than by a list
that can drift.

A worker that ends its turn having reported nothing is the case section 3.2b
existed for, and it is now left to the staleness window on purpose. Section
3.2b's own account of that case says the push buys "one window earlier than a
scheduled tick would have found it" -- one interval of latency, against a wake
on every ordinary turn end of every bound worker.

### 3.3 The worker-facing contract

A worker is told the rule it has to act on, because the rule is only honest if
the worker knows it: the report is the only thing that wakes its conductor, so
before it stops it says where it is. A pause it expects to be resumed from is a
`progress` report. The `kirocrew-worker` prompt, the conductor charter's patrol
section, the `goal-conductor` skill, `work_report`'s own tool description, the
`monitor_start` and `monitor_update` `watch` field descriptions and
`docs/work-ledger.md` all state it the same way.

Nothing in that text promises a worker that saying it is waiting exempts it from
the staleness clock, because it does not: an item quiet past the window is
flagged whatever its last report said, and that flag is the backstop for a
worker that died inside a wait.

### 3.4 What the push still is

Unchanged from `rfc-crew-log-wake` sections 3.2c to 3.5: the push is
`fire_now(..., defer_if_firing=True)` and nothing else, the scheduled tick stays
as the liveness fallback, reports arriving together share one batched tick, and
the per-item pull-forward budget still applies. Dropping the two loop-side
triggers removes `conductor_wake`'s only store read (`read_binding`), so the
module comes off `work_ledger`'s permitted-importer allowlist; the event's own
key is the conductor's board, so the surviving trigger needs no binding.

## 4. What survives of rfc-crew-log-wake

| in that document | after this one |
|---|---|
| Goal 1 (an actionable report arrives in seconds) | unchanged, and now the only wake |
| Goal 2 (a closed worker's session wakes its conductor without a window) | retired. The `worker_closed` arm of `is_stale` carries it at the patrol cadence |
| Goal 3 (no new delivery or decision path) | unchanged |
| Goal 4 (loss is harmless) | unchanged |
| section 3.1 (trigger one) | kept, gated on `WORK_WAKE_STATUSES` |
| section 3.2 (trigger two, the close) | the trigger is retired; the `worker_closed` probe input, the `slot_exists` reading and the never-reported window it defines all stay |
| section 3.2b (trigger three, the turn end) | retired whole |
| section 3.2c to 3.5 (refusals, fallback, coalescing, budget) | unchanged |
| section 5 (security: the worker gains no handle on its conductor) | unchanged, and strictly narrower -- a worker now moves its conductor's deadline only by writing a report it is already authorised to write |

## 5. Risks

- **An orphaned worker is noticed one patrol interval later.** The flags that
  find it are unchanged, so the loss is latency, bounded by the interval band
  the skill enforces (300..900 seconds). Mitigated by telling the conductor to
  read `stale` and `orphaned` every cycle rather than wait to be woken.
- **A conductor that armed no patrol hears nothing.** True before this change
  too: a wake moves a deadline and cannot create a loop. `monitor_start` with
  `watch="work-ledger"` is already mandatory in the skill, and
  `rfc-conductor-default-patrol` is the design for arming one at bind.
- **A worker that never reports and never dies is invisible until the window.**
  Unchanged: no trigger ever covered it, because there is nothing to observe.

## 6. Security

Strictly narrower than the base design. The surviving trigger fires on a
`work/recorded` entry the worker is already authorised to write, through a
subscription keyed to the conductor's own board; the two retired triggers fired
on gateway-observed events the worker could cause without writing anything. No
payload crosses, the upward `session_send` refusal is untouched, and the push
still only moves a deadline the conductor's own gate then judges.

## 7. Alternatives considered

- **Keep all three triggers and filter at the gate.** Rejected: the gate already
  filters, and the waste is upstream of it. The tick is what costs a cycle
  against `max_cycles` and a step of the quiet streak, and only the trigger can
  decline to arm one.
- **Keep the close trigger, gated the same way as section 3.1.** Rejected as the
  smaller-looking change that is not: it buys one interval of latency on a case
  the `stale` flag already covers, and costs a second status gate that must stay
  in step with the first forever.
- **A new `waiting` status, or a `waiting` value on `work_report`'s `reason`.**
  Rejected. A new status would have to be placed in or out of
  `STALE_ELIGIBLE_STATUSES`, `_worker_owns_next_move` and `accept_batch`'s
  filters, which this design's own non-goals forbid touching. A new `reason`
  value was built and removed before merge: no reader distinguished it from a
  bare `progress`, since `is_stale` reads no reason and `progress` wakes nobody
  either way, so it was a word with no consequence. The behaviour the ruling
  asked for -- a worker parked in a wait costing its conductor nothing -- is
  delivered by section 3.1's gate.
- **Raise `ITEM_PULLS_PER_HOUR` or lower the patrol interval.** Rejected: both
  treat the symptom. The cap exists because the triggers over-fire.

## 8. Open questions

1. Should `is_stale` eventually read a stop reason, so a worker parked in a
   long `wait` is measured from its recover deadline rather than from its last
   report? Out of scope here by non-goal, and only answerable once `wait` is
   actually a stop-and-recover mode. It would be a change to the liveness
   conjunction and wants its own document.
2. `conductor_wake.ITEM_PULLS_PER_HOUR` now bounds a population that cannot
   occur: only waking reports arm a tick, and
   `ledger_wake.MAX_WAKES_PER_ITEM_PER_HOUR` already caps those at the same
   number one layer down. The two measure different things (ticks armed versus
   turns spent), so collapsing them is a judgement call rather than a cleanup.
   Left in place.

## 9. Rollout

One pull request,
[#18854](https://github.com/kirodotdev/KiroCrew/pull/18854), carrying the gate,
the two deletions, the prompt and documentation text, and the tests. This
document lands first, on its own pull request, so the implementation's First
Principles lane reads the decision from the base branch.

No migration and no flag. The change is to what a wake fires on, and a ledger
already on disk is read identically; a conductor whose loop is mid-cycle when
the gateway restarts arms at delay zero on boot exactly as before, and reads the
same board.

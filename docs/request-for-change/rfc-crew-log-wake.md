---
title: Crew log wake -- a worker's write pulls the conductor's tick forward
status: in-progress
author: Raymond Chen, with kirocrew-lead
created: 2026-10-01
last-audited: 2026-10-01
audited-at: 321fd996a2
doc-pr: null
implementation-prs: [15691]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crew log wake -- a worker's write pulls the conductor's tick forward

- Status: in-progress. This document ships INSIDE its implementing pull request:
  the repository takes no standalone RFC pull requests, so `doc-pr` is null and
  the implementation is the one named in `implementation-prs`.
- Builds on [`rfc-conductor-work-ledger`](rfc-conductor-work-ledger.md) Phase 3
  (the `work-ledger` probe, in flight on
  [#12781](https://github.com/kirodotdev/KiroCrew/pull/12781)) and on
  [`rfc-append-only-ledger`](rfc-append-only-ledger.md) (the crew log). It adds
  no delivery path and no second loop: it moves one deadline.
- Terminology. An earlier draft of this design (2026-09-22, never in this tree)
  said "the ledger is the channel" and hooked `session_ledger_record`. The word
  has since moved. The per-unit append-only file is the **crew log**
  (`kiro_crew.crew_log`, recorded by default since
  [#14295](https://github.com/kirodotdev/KiroCrew/pull/14295)). The **session
  ledger** (`session_ledger.py`, fold `ledger`) and the **work ledger**
  (`work_ledger.py`, entry type `WORK_ENTRY_TYPE`, rebuilt by
  `rebuild_from_crew_log`) are two folds of that log. So the channel the draft
  wanted already exists: every report a worker makes lands in the crew log
  first. This document hooks the log, not a fold.

## 1. Problem

A conductor learns that a worker finished, got stuck, asked a question or died
by ticking. Phase 3 makes a tick that finds nothing cost no turn, which fixed
the price; it did not fix the delay. The gate runs on the loop's cadence
(`idle_secs`, 300 s by default), so a `question` written one second after a
tick waits 299 s, and a worker whose session was closed with its item open is
noticed only when the staleness window has also elapsed on a later tick.

The eager fold already sits on the crew-log append path and names this gap:
`crew_log/eager.py` folds a slot's board the moment its entry lands, and its
header says "WHAT IT DOES NOT DO: push ... there is no consumer yet to need it."
The conductor's armed gate is that consumer.

The team discussion of 2026-09-30 reached the same place from the product side:
the conductor "does not know when a session it opened ended", because "the
ledger does not trigger a wake". Two routes were weighed. Direct worker to
conductor messaging over `session_send` has the infrastructure but is withheld
for the reason `rfc-conductor-work-ledger` gives (nothing bounds WHAT is sent).
The crew log route needs one more piece, and this RFC is that piece.

## 2. Goals and non-goals

Goals:

1. A worker's actionable report reaches its conductor within seconds of the
   write, not within one cadence.
2. A worker session that is closed with its item still open wakes its conductor
   without waiting for a staleness window.
3. No new delivery path, no new decision path. The push moves a deadline; the
   existing gate and budget decide whether a turn is spent.
4. Loss is harmless. A dropped push is caught by the next scheduled tick, so no
   delivered-map, replay sweep or durable subscription is needed.

Non-goals:

- Lifting the upward `session_send` refusal. The worker still gains no handle on
  its conductor.
- A general event bus, a UI push, or a per-(slot, fold) revision; `eager.py`
  records why that is a separate contract.
- Replacing the periodic tick. It stays as the liveness fallback at whatever
  cadence the conductor already set.
- A configuration switch. Phase 3 decided the gate has none and recorded why.

## 3. Design

```
(1) worker turn --work_report--> work_ledger.apply_worker_report
                                  |  durable write + crew-log append
                                  v
                      crew_log eager queue (already on the append path)
                                  |  worker thread, not the writer
                                  |
(2) worker session closed --> chat_handlers.close_slot, after the slot is gone
                                  |
(3) worker turn ended (any outcome) --> timers.notify_turn_complete
                                  |
                                  v
            read_binding(worker slot) -> conductor slot -> its armed work-ledger loop
                                  |      ONE lookup, shared by all three
                                  v
                      AutoNudgeService.fire_now(loop_id)      <-- the whole change
                                  |
                                  v
                  ordinary _timer body: stop sentinel, caps, probe gate
                     quiet (progress) -> re-arm, no turn
                     wake  (done / blocked / question / stale) -> one turn
```

The three triggers differ only in what they observe; the lookup, the push and
everything after it are one path. That is why the lookup is factored into its own
module (`conductor_wake`) rather than written three times: a fourth trigger is
plausible, and the thing to bound is the copy count.

### 3.1 Trigger one: a worker's write

`apply_worker_report` already appends a `WORK_ENTRY_TYPE` entry to the worker's
crew log through the emitter, and `eager.py` already receives every append on a
queue it drains off the writer's thread. The hook is a second consumer on that
drain: when the entry is a `work/*` entry written from a worker unit, resolve
the binding (`read_binding(worker_slot_key)` gives the conductor slot and item),
find the conductor slot's armed `work-ledger` loop, and call `fire_now`.

`fire_now` is the right seam and the only one touched. Its own docstring states
the contract this design relies on: "it does not deliver the nudge itself. It
re-arms through `_arm_timer`, so the cycle runs inside the ordinary `_timer`
body -- the stop sentinel, the cycle cap, the wall-clock budget, the
approval-stall stop and the probe gate all apply exactly as they do on a
scheduled tick." So a `progress` report pulls the tick forward and the probe
answers quiet, which re-arms and spends nothing; a `done`, `blocked` or
`question` pulls it forward and the probe answers wake. The actionable set,
the fingerprint and `MAX_WAKES_PER_ITEM_PER_HOUR` are Phase 3's and are not
restated here.

The session-ledger fold (`ledger/recorded` entries) is deliberately not a
trigger. A worker's `session_ledger_record` is its own working memory and has
no binding to a conductor; the work ledger is the reporting channel, and Phase
2 made that the only one a worker can write toward its conductor.

### 3.2 Trigger two: a worker session closes

`chat_handlers.close_slot` is the one path a dashboard session is closed
through. Once the close is COMMITTED, the same lookup runs: binding, conductor
slot, armed loop, `fire_now`. Committed means after the archival save succeeded,
or after the hand-over exit's tail drain landed -- never between the slot's pop
and that save. A tick fired in that window would read the popped slot as closed
and persist a `worker_closed` stall, and the save's failure arm puts the slot
back without being able to retract that observation.

Phase 3's liveness rule is a conjunction: quiet past the window, AND the worker
not running, AND the next move still the worker's. "Not running" is true of an
idle worker between turns as well as of a closed one, which is why the window
exists at all: it separates a worker that is thinking from a worker that is
gone. A close is not ambiguous. So the probe gains one input beside
`worker_running`: `worker_closed`. An open item whose worker is closed and whose
next move is the worker's is stale at once, window or not. An item whose move
belongs to the conductor (a `done` awaiting verification) is not woken by the
close, for the reason Phase 3 gives: silence there is the expected end of the
work.

One qualification, and it is the difference between a close and the EVIDENCE of a
close. The trigger above observes a real close; the probe's `worker_closed` input
does not -- it is recomputed per tick by failing to find the worker's session in
the dashboard's in-memory slot table, and an absence there has two causes. A
session that existed and ended is one. A session not registered where that read
can see it is the other: the bind-to-first-report gap this very window was written
for, a slot table still rehydrating after a restart, a worker bound under a key
that table does not carry. The two are indistinguishable from the absence alone,
and this design makes ticks land in exactly those moments far more often -- it
arms every work-ledger loop at delay zero on boot, and pulls a tick forward on any
sibling worker's write.

Existence is read through `DashboardState.slot_exists`, not `get_slot`.
`get_slot` hides a slot still under construction so nobody acquires a
half-finished session, and the import path retracts its slot from the table
across its async tail while keeping the construction mark. Both are an open
session, so a worker that is rehydrating or resuming never reads as closed.
Two states with no slot object read as open too: a key the boot restore could
not read (`unrestored_slot_keys`, kept because the read failed, not because
the session is gone), and every key while a restore is still in flight
(`restoring_open_slots`), when a tab not reached yet has no slot.

So the window is skipped only for an item that has REPORTED at least once
(`last_report_at` set). A report proves the first cause: the worker was there, it
spoke, and now it is not. An item with no report keeps the window, which is the
behaviour that gap has always had, and the staleness window is what covers it.

### 3.2b Trigger three: a worker's turn ends

Triggers one and two both need the worker to have DONE something: written a
report, or had its session closed. A third ending has neither property and is
the one a conductor most wants to hear about -- a turn that ended without
reporting at all.

`autonudge_service/timers.py:notify_turn_complete` is where a slot's turn end
reaches the service, called by the gateway after `HOOK_EVENT_STOP`. The same
lookup runs there: binding, conductor slot, armed loop, `fire_now`.

**The outcome kinds this keys on: all of them, by construction.** That is the
point of choosing this seam rather than an error hook. `notify_turn_complete` is
called once per turn end regardless of how the turn ended, so the set it covers
is every ending:

| ending | writes `work/recorded`? | reached by |
|---|---|---|
| reported, then ended | yes | triggers one and three |
| ended without reporting | no | trigger three only |
| ended in a provider or tool error | no | trigger three only |
| ended empty (no reply, no tool call) | no | trigger three only |
| session closed | no | trigger two |

So there is no outcome vocabulary to enumerate and none to keep in step with the
runner's. A list of error kinds would have been a second copy of the runner's own
taxonomy, and the kind missing from it would be the one that mattered.

What makes this affordable is Phase 3's gate, not restraint at the trigger. A
worker that reported `progress` and then ended its turn pulls the tick forward
and the probe answers quiet: the epoch moved, nothing in it is actionable, the
loop re-arms and no turn is spent. A worker that ended without reporting leaves
an item whose last word still owes the conductor a report, and §3.2's
`worker_closed` input does not apply (the session is still open) -- so that one is
caught by the staleness window as before, one window earlier than a scheduled
tick would have found it. The trigger moves the deadline; the gate still decides.

### 3.2c Two refusals that would otherwise lose the push

The three triggers share one lookup and one refusal policy: a refused `fire_now`
is logged at debug and dropped, because the scheduled tick reads the same ledger a
cadence later. Two cases make that fallback too slow to be the answer, and both
are consequences of this design's own goal -- a conductor that can now set an
hours-long cadence has an hours-long fallback.

**Mid-fire.** `fire_now` refuses when the loop is inside `_run_fire_cycle`, and
the re-arm at the end of that cycle goes through `_arm_from_deadline` -- the
loop's own next deadline, not now. The cycle in flight read the ledger BEFORE the
write that caused the push, so dropping the intent holds a `question` for a whole
cadence. `fire_now` gains `defer_if_firing`: the refusal still stands as the
call's answer, and the loop is recorded so the tail of the in-flight cycle arms at
delay zero instead. Opt-in, because the operator's own Fire-now button reports its
refusal to a person who can press again, while this trigger's caller is a drain
thread with nobody to tell.

**A restart.** The push rides an in-process queue, so every push in flight when
the gateway dies is gone: the crew-log entry is durable, the notification was not.
`AutoNudgeService.start` therefore resumes an active `work-ledger` loop at delay
zero rather than toward its persisted deadline, and only that kind -- for a
pull-request watch the next poll reads the same pull request, so there is nothing
to replay. One tick per such loop at boot replays every push lost in the window,
because the probe reads the ledger itself: whatever landed while the process was
down is in the fold. That is also why there is no replay log and no delivered map
-- the store is the record, and re-reading it is the replay.

Both zero-delay arms carry the version refusal `_arm_from_deadline` already makes,
and leaving it out of either was a reachable hole rather than a theoretical one. A
monitor record whose `version` this gateway does not implement belongs to a newer
one, so arming its loop delivers an unattended turn under a policy nothing here can
interpret -- and a work-ledger watch is a `gate=True` prompt loop, which `_load`
leaves ACTIVE through its unsupported-version branch. So both the startup resume
and the push refuse such a record and leave its stored `active` intent untouched,
for the reason that branch gives: the intent belongs to the gateway that wrote it
and must survive the downgrade. Inertness is the local consequence.

### 3.3 The tick stays, as the fallback

A push can be dropped. The eager queue drops under pressure by design (`eager.py`:
"a slow consumer must cost currency, never turn latency"), the process can
restart between the append and the drain, and `fire_now` refuses when the loop
is mid-fire. None of that needs recovery machinery, because the scheduled tick
runs the identical gate over the identical store a cadence later and sees the
same fingerprint move. The 2026-09-22 draft's `delivered` map, replay sweep and
one-shot scheduler deadline were there to make push the only path; with the
tick kept, they are not needed and are not built.

So the three ways a push can miss are not equal, and only one of them falls
back to the tick:

| how the push misses | what catches it |
|---|---|
| dropped (eager queue under pressure, binding unreadable) | the slow tick, one cadence later |
| refused mid-fire | not lost: `defer_if_firing` arms the tail of that cycle at delay zero (§3.2c) |
| in flight across a restart | not lost: `start()` ticks every work-ledger loop once at delay zero (§3.2c) |

Both "not lost" rows are pinned against the real service: a push landing inside
the fire window leaves the loop re-armed at delay zero once the cycle ends, and a
loop restored with a stale fingerprint wakes once when the ledger moved while the
gateway was down and stays quiet, spending no turn, when it did not.

What changes for the conductor is only how long it waits for the fallback, so
the `goal-conductor` skill can lengthen the patrol cadence once this lands:
the tick is for silence, and silence is measured in hours.

### 3.4 Coalescing

Several workers reporting inside one cadence produce several `fire_now` calls
on the same loop. `_arm_timer` cancels the previous timer and arms a new one at
delay zero, so the loop ticks once and the probe reads every item's newest
event in that one tick; this is the coalescing the draft asked for, obtained
from the existing timer rather than from a queue of envelopes. A `fire_now`
that arrives while the loop is in `_run_fire_cycle` is refused, and the
re-arm at the end of that cycle covers the report that caused it.

A pulled-forward tick is extra, and two rules keep it from costing more than
the news it carries. It never spends the post-wake follow-up tick: that free
second turn belongs to the loop's own cadence, so a push landing right after a
wake (a worker's close after its `done` report, or the reports that arrived
while the woken turn ran) goes through the probe and wakes only for an
observation the delivered turn did not already carry. And a pulled-forward tick
the gate answers quiet keeps the loop's existing deadline when that is earlier
than a fresh interval, so a quiet push never delays the scheduled check.

The kernel's own coalescing window is NOT applied to these observations. A
`WAKE` normally waits out `irq.DEFAULT_COALESCE_SECS` (240 s) per entry, and the
tick that finds it still young answers quiet and re-arms at the loop's cadence
-- so a pulled-forward tick would find the `question` and then hold it a whole
cadence. That floor exists for a subject whose sub-observations may not exist
yet; a report is complete when written and a stall is already decided. The probe
therefore sets the window to zero through its `tuning()` override and emits both
as `WAKE`: with a zero window the kernel delivers every fresh `WAKE` of the tick
in one report and masks all of their keys together. `IMMEDIATE` would skip the
delay too, but the kernel delivers an `IMMEDIATE` on its own and masks only that
one key, so a tick that found two workers' reports would wake for the first and
wake again later for the second -- news the first turn had already read off the
board.

### 3.5 Budget

Turns per item are already bounded by the probe's
`MAX_WAKES_PER_ITEM_PER_HOUR`. Pull-forwards are bounded separately, because a
quiet tick spends no wake but still runs the probe and still advances the quiet
streak whose floor delivers a turn anyway. One item may pull its conductor's
loop forward at most 12 times in a sliding hour (`ITEM_PULLS_PER_HOUR` in
`conductor_wake`); a push that coalesces into a tick already armed, or into a
cycle already holding a deferred pull-forward, is not counted. Past the cap the
write still lands and the scheduled tick still reads it; only the push is
dropped, and one INFO line says so per item per window. The number is a first
guess matched to the QA bar, not derived; the count lives in the service beside
the loop and resets on restart.

## 4. Cost

| | Phase 3 alone | with this RFC |
|---|---|---|
| conductor turns per worker report | gate decides | gate decides (unchanged) |
| delay from `question` to conductor turn | up to `idle_secs` | seconds |
| delay from worker close to conductor turn | `idle_secs` + staleness window | seconds |
| delay from a worker turn that never reported | `idle_secs` + staleness window | staleness window |
| patrol cadence the skill can set | minutes | hours |
| new timers, stores, maps | none | no timer and no store; five in-memory structures on the service, all per loop, released with the loop and lost on restart: three loop-id sets (`_pulled_forward`, a deferred pull-forward for an in-flight cycle; `_pushed_ticks`, an armed tick a push set; `_pushed_running`, a running tick a push set) and the pull-forward cap's `_pull_forward_counts` (item -> times in the last hour) and `_pull_forward_capped` (pairs already logged) (3.2c, 3.5) |

## 5. Security

- The worker gains no handle on its conductor. The push carries no payload: it
  moves a deadline on a loop the conductor armed, and the probe then reads the
  store under the conductor's own identity, exactly as on a scheduled tick.
- A worker cannot spend the conductor's budget faster than Phase 3 allows.
  Every pull-forward runs the same `MAX_WAKES_PER_ITEM_PER_HOUR` cap, cycle cap
  and wall-clock budget, and an item's pull-forwards are capped on their own
  (3.5); a report storm collapses into one tick per cycle.
- The hook runs on the eager drain thread, never on the writer's thread, so a
  slow or failing lookup cannot delay a worker's append; the drop counter
  `eager_dropped` already measures back-pressure.
- `read_binding` is the Phase 2 resolver and is read with `strict=False`: an
  unreadable binding means no push, and the tick covers it.

## 6. Alternatives considered

- **Direct upward `session_send`.** Infrastructure exists and the refusal is a
  prompt-level and authz-level choice. Rejected again for the reason the work
  ledger RFC records: nothing bounds what is sent, and the receiving conductor
  cannot tell a report from an instruction.
- **A new delivery path for a `[ledger wake]` envelope** (the 2026-09-22 draft).
  Rejected: the gate already delivers a turn with the ledger snapshot, and a
  second path would need its own caps, stop sentinel and approval-stall rule.
- **Hook `session_ledger_record` instead of the crew log.** Rejected: that fold
  has no conductor binding, and it would miss `work_report`.
- **Push from the eager fold to the UI as well.** Deferred; `eager.py` states the
  revision contract it needs, and this RFC adds no reader.

## 7. Open questions

1. Should a worker's `blocked` close pull the tick forward at all, or only
   closes with an open item? Proposal: only open items; a closed item has
   nothing left for the conductor to do about the worker.
2. Does a `progress` push that finds the loop mid-fire need any record?
   **Decided during implementation: yes, a transient one.** The original proposal
   ("no; the end-of-cycle re-arm and the fingerprint make it visible next tick")
   was wrong about the re-arm: that re-arm goes through `_arm_from_deadline`, so
   the news waits a full cadence rather than one tick, and this design's whole
   point is that the cadence is now allowed to be hours. §3.2c records the answer
   -- `defer_if_firing` on `fire_now`, released at the one site that applies it.

## 8. Rollout

1. This document, inside the pull request that implements it. The repository
   takes no standalone RFC pull requests, so the design and the code land
   together and the document is reviewed against the diff beside it.
2. The implementation, in that same pull request, based on
   [#12781](https://github.com/kirodotdev/KiroCrew/pull/12781) until it merges and
   retargeted to `main` after: the one-call arm (`watch` on `monitor_start` and
   `monitor_update`, the payload, the applier, the authz forward), the eager-drain
   consumer, the `close_slot` hook, the turn-end hook, the `worker_closed` probe
   input, and the two refusal hardenings of §3.2c. Tests pin, each in its own
   case: a `work/recorded` append from a bound worker fires `fire_now` on the
   bound conductor's loop and nothing on an unbound slot; a `progress` report
   fires and the probe answers quiet; a `question` report fires and the probe
   wakes; a close with an open worker-move item wakes inside the window; a close
   with a `done` item does not; a turn end from a bound worker fires; a refused
   `fire_now` neither raises nor blocks the eager drain; and `monitor_start` with
   `watch: "work-ledger"` arms a loop whose subject is the caller's own slot.
3. `goal-conductor` skill: lengthen the patrol cadence and name the tick as the
   liveness fallback.

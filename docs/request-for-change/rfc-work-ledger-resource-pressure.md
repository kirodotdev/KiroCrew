---
title: Host resource pressure goes on the work ledger and wakes the conductor, which pauses dispatch
status: in-progress
author: kirocrew agent session, directed by the repository owner
created: 2026-11-13
last-audited: 2026-11-14
audited-at: 16e11a1870
doc-pr: 36304
implementation-prs: [36481, 37164]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Host resource pressure goes on the work ledger and wakes the conductor

Status: in-progress, narrowed. **Withdrawn 2026-11-14 by the repository owner:**
the `host_pressure` record (§3.1, §3.2), the conductor wake (§3.3), the
conductor pause rule (§3.4, the tight advice in §3.5) and the §12 amendment.
The owner's words: "我的想法是缩小，其实只要给个简单指示，说明派活要先查资源，然后给AI工具查资源不就好了？为什么改那么多代码"
and "开精简 PR，只留 critical 拒绝。" So the conductor checks `resource_status`
before it dispatches (the existing tool), and only one piece of code stays: a
work-ledger `create` is refused 409 `host_memory_critical` while
`admission_check()` refuses (critical memory). All of it shipped in
[#36481](https://github.com/kirodotdev/KiroCrew/pull/36481). The trim
[#37164](https://github.com/kirodotdev/KiroCrew/pull/37164) removes the
withdrawn parts and renames the refusal code from `host_pressure_critical` to
`host_memory_critical`, since nothing named `host_pressure` is left. This
withdrawal lands first in its own PR, so that trim rebases onto it.

What stays, as shipped in #36481: `_refuse_if_host_critical` in
`src/kiro_crew/work_ledger_writes.py`, called from `_create_item` only (a
remote peer's `create_bound_item` keeps its own `crew_busy` gate). It calls the
live `admission_check()`, not `cached_admission_check()` as §3.5 says: that
cache refreshes only when asked, so the first `create` after recovery could be
refused on a stale verdict.

The text below is the original design, kept as the record. Read it with the
withdrawal above: only the critical refusal of §3.5 and §5 test 6 are in force.

This document lands on its own because it changes what the goal conductor does
by default (it pauses dispatch on host pressure) and which `create` the work
ledger refuses (a new 409 at critical memory), and the First Principles lane
reads that decision from the base branch.

Linked: [`rfc-work-ledger-concurrency-cap.md`](rfc-work-ledger-concurrency-cap.md)
§7 drops the fixed default of 6 open items per goal and lets resources decide
each wave. That change relies on this one: the pressure record, wake and
critical refusal below are what bound a goal's fan-out once the constant is
gone.

Related: [`rfc-conductor-work-ledger.md`](rfc-conductor-work-ledger.md) (the
ledger), [`rfc-work-ledger-person-wait-hold.md`](rfc-work-ledger-person-wait-hold.md)
(the hold that runs before the probe).

## 1. The ask

The owner, verbatim: "然后conductor/worker协调的时候，如果注意到资源非常紧张，应该记录到ledger，然后wake up conductor，让它能够根据当前资源消耗，主动暂停工作"

In English: while a conductor and its workers run, if resources get very
tight, record that on the ledger and wake the conductor, so it can pause work
itself based on current use.

## 2. What exists, and the gap

| Part | Where | What it does today | Gap |
|---|---|---|---|
| Posture | `_classify` in `src/kiro_crew/resource_status.py` | `ample` / `tight` (at or below `agent.resource_pressure_gb`, 4 GB) / `critical` (at or below `agent.resource_critical_gb`, 2 GB) / `unknown`. Memory only, cgroup-clamped. Load is reported, never classified. | none for memory |
| `[RESOURCES]` line | `ContextManager.build_message` | Added to any turn while posture is tight or critical. | Only on a turn. A conductor with no turn never sees it. |
| Admission gate | `admission_check`, `cached_admission_check` (both in `resource_status.py`) | Refuses background work (cron, task-runner steps, helper turns) at critical; admits on `unknown`. | Work-ledger `create` does not consult it. |
| Episode model | `ResourcePressureNotifier` in `src/kiro_crew/notifications/resource_pressure.py` | Samples every 30 s (`SAMPLE_INTERVAL_SECS`). An episode runs from the first non-ample probe to the first ample one. Alerts critical at once (re-alert every 30 min, `CRITICAL_REALERT_SECS`), tight after 600 s sustained (`SUSTAINED_TIGHT_SECS`), one recovery note. `unknown` is not a signal. | Talks to the notification bus (the person), not to conductors. |
| Push to conductor | `_fire(svc, conductor_slot_key, item_id)` in `src/kiro_crew/conductor_wake.py`; boards from `_watched_boards` | `fire_now` on the conductor's work-ledger loop, batched, hourly budget per item. | Only a worker report in `WORK_WAKE_STATUSES` triggers it. |
| Wake gate | `WorkLedgerProbe.observe` in `src/kiro_crew/probes/work_ledger.py` | Per-conductor dedupe by token and a 12/h budget per entry. A synthetic entry (`_ALL_CLOSED_ENTRY`) already carries the "all closed" wake. | No pressure observation. |
| Conductor rules | goal-conductor skill, "Rounds are not gated, but spend is bounded" | Concurrency cap and no-progress check. | Nothing on pressure. |

The signal exists and already has hysteresis. Three wires are missing: signal
to a record the conductor can read, signal to a conductor wake, and a rule for
what the conductor does.

## 3. Decision

The gateway's `ResourcePressureNotifier` is the one noticer. No agent has to
look. It gains one sink beside the bus.

### 3.1 The `host_pressure` record

While an episode is open, on each 30 s sample, the notifier writes one host
file atomically, `<crew state>/work_ledger/host_pressure.json`:

```json
{"episode": "<id>", "level": "tight", "since": "<iso>", "updated_at": "<iso>",
 "reading": "<memory_reading()>", "load_per_cpu": 0.8}
```

- `level` is the episode's alerted level, not the raw sample: `tight` only
  after 600 s sustained, `critical` at once. On recovery to `ample` it writes
  `level: "clear"` once.
- `load_per_cpu` may be null. It is carried for the conductor to read; it wakes
  no one (see §7).
- The record is host-wide, as pressure is. Each conductor stays the only writer
  of its own board header.

### 3.2 Read surface and expiry

`work_ledger_read` adds a `host_pressure` block to the `conductor` section,
full and compact, computed at read time: `{level, since, age_secs}`.

A record whose `updated_at` is older than 3 samples (90 s) reads as absent. A
torn or missing file reads as absent and never raises. So a gateway that died
or restarted mid-episode cannot leave a stale `critical` behind. Absent means
proceed, the same as `admission_check` on `unknown`.

A `clear` record is written once and never refreshed, so it reads for 900 s
(three patrol ticks) instead. A conductor that was mid-turn when memory
recovered, or whose push was refused, still reads `clear` on its next tick and
gets its resume wake. A `clear` that outlives the episode is harmless: it means
proceed, the same as absent.

The conductor writes its response on its own ledger with the existing actions
(`decide` on each held item), so the board shows the cause and the pause.

### 3.3 Wake, once per conductor per level change

On a level change (none to tight, none or tight to critical, tight or critical
to clear) the notifier calls one new helper,
`conductor_wake.wake_all_for_pressure(svc)`. It runs the existing
`_fire(svc, board, item_id="_pressure")` for each board in `_watched_boards`.

On that tick the probe reads the record and emits one WAKE observation with
token `pressure:<episode>:<level>`, charged to a new synthetic entry
`_PRESSURE_ENTRY`, on the `_ALL_CLOSED_ENTRY` pattern. Result:

- One wake per conductor per level change.
- Workers are not the source, so N workers cannot cause N wakes.
- The 30-min critical re-alert and each 30 s refresh do not wake (same token).
- The plain 300 s patrol tick still reads the record, so a refused `fire_now`
  costs latency, not the news.

### 3.4 What the conductor does

A short section in the goal-conductor skill after "Rounds are not gated, but
spend is bounded", and the same rule in the Bot conductor text (parity with
`test/test_mate_conductor_parity.py`):

- `tight`: start nothing new (`create`, `bind`, `session_create`); let
  in-flight items run. Say it in the round report:
  `paused: host memory tight (<reading>)`.
- `critical`: the same, plus `decide` on each open item whose worker is about
  to run a heavy step: hold heavy steps (full suite, build, subagent waves)
  until `host_pressure` clears; light work is fine. Delivered with the existing
  decision and `session_send`.
- `clear`, or `host_pressure` absent: resume dispatch; send "resume" to held
  items.
- Never kill a worker. Never ask the person: this is not one of the five ask
  cases.

### 3.5 Advise at tight, refuse at critical

`tight` is advice. A 4 GB reading is often fine, so stopping there stays the
conductor's judgement.

`critical` refuses. `_create_item` returns 409 `host_pressure_critical` when
`cached_admission_check()` refuses, placed after the concurrency-cap check.
It reuses the existing admission contract: critical refuses, `unknown` admits,
and `agent.admission_gate: false` admits. The refusal writes nothing. So a
conductor that misses the rule still cannot dispatch into a host about to
freeze.

## 4. Non-goals

- A load, CI-queue or rate-limit trigger. See §7.
- Stopping a worker mid-step. The adaptive controller's spawn gate and the
  subagent memory floor stay the net for that.
- A new tool, work status, report `reason`, event kind or config key.
- Pressure on a remote peer's host.

## 5. Tests the code PR carries

Unit:

1. Notifier sink: a tight blip under 600 s writes nothing; sustained tight
   writes `tight`; critical writes `critical` at once; tight to critical
   rewrites; recovery writes `clear` once; `unknown` changes nothing. Injected
   `probe_fn` and `clock`, as the notifier's existing tests do.
2. The notifier calls the wake hook exactly once per level change, not on the
   30-min re-alert and not on each 30 s refresh.
3. Read surface: `host_pressure` present with `age_secs`; `updated_at` older
   than 90 s reads absent; a torn or missing file reads absent and never raises.
4. Probe: one WAKE per `pressure:<episode>:<level>` per conductor; a re-read is
   quiet; a new level is news; the entry never spends a real item's 12/h budget.
5. `wake_all_for_pressure` fires each watched board once; a 409 or 404 from
   `fire_now` is dropped at DEBUG.
6. `_create_item` refuses `host_pressure_critical` when the cached verdict
   refuses; admits on tight, on `unknown`, and with `agent.admission_gate:
   false`; the refusal leaves the files byte-identical, as the cap refusal
   tests check.
7. Person-wait hold: a board whose open items all wait on a person is released
   once by a level change (tight to clear), then holds again. Amended by §12;
   this item first pinned "stays held".

End to end (pod-e2e, one run): fake the probe to critical; one conductor turn
arrives; `work_ledger_read` shows `host_pressure.level=critical`; a `create` is
refused; fake ample; one resume turn arrives.

## 6. Cost

- Per episode: one atomic write under 300 bytes per 30 s sample while it lasts;
  one `fire_now` per watched board per level change; one conductor turn per
  conductor per level change, typically two (enter and clear).
- The conductor turn is the real cost, since it re-sends the conversation. The
  600 s tight filter keeps episodes rare.
- One new 409 code. No new tool, status, event kind or config key.
- Estimated size: about 90 lines of code and 20 lines of skill text.

## 7. Known limits

- **Memory only.** Posture ignores CPU load, CI queue depth and provider rate
  limits, which this owner has gated on in practice. The record carries
  `load_per_cpu`, but no load level wakes anyone. A load trigger changes
  posture and `admission_check` semantics, so it is a follow-up with its own
  decision. The code carries
  `shortcut: memory posture only; upgrade when a load threshold is agreed`.
- **One host.** Remote-dispatched items run elsewhere; the hub's pressure says
  nothing about them. The conductor rule applies to local items only.
- **Nested conductors** each get their own wake. A child and its parent both
  pausing is correct, and costs one turn each.
- **Held boards wake once per level.** Amended by §12. This bullet first said
  "held boards stay held"; that left queued work stuck.
- **The reaction is still a prompt at tight.** Only critical has a code
  backstop (the create refusal). A worker already in a heavy step is not
  stopped.
- **Not a lease.** Two conductors that both read `clear` can both dispatch at
  once, the trade-off `resource_status` already documents.

## 8. Security

No new authority. The record is written by the gateway only, under the crew
state directory, and read through the existing `work_ledger_read` binding. The
wake reuses `_fire` and its budgets. The refusal only removes a `create`; it
grants nothing.

## 9. Alternatives considered

- **Workers notice and report** (a new `reason="resources"` or status). Needs a
  worker to look before each heavy step, gives up to N wakes for N workers,
  never expires while a worker sits idle, and a non-person `reason` would have
  to stay out of the person-wait hold. Rejected.
- **The patrol probe polls posture each tick.** No push, so up to 300 s late,
  and no hysteresis: a reading flapping around 4 GB mints a token per flip
  unless the probe copies the notifier's episode logic. Rejected.
- **Refuse at tight too.** Stops healthy work on a reading that is often fine.
  Rejected; tight stays advice.

## 10. Rollout

One code PR after this document lands, with the §5 tests. It lands before, or
together with, the default change in
[`rfc-work-ledger-concurrency-cap.md`](rfc-work-ledger-concurrency-cap.md) §7.
Exit criteria:

- A sustained tight episode puts `host_pressure.level=tight` on
  `work_ledger_read` and wakes each watched conductor once.
- At critical, `create` is refused `host_pressure_critical` and writes nothing.
- On recovery each conductor gets one wake, and the record reads `clear`, then
  absent after 900 s.

## 11. Open questions

None open.

## 12. Amendment (2026-11-13): a level change releases a person-wait hold once

**Withdrawn 2026-11-14 by the repository owner**, with the record and the
wake it relied on (see the status above). Its code PR
[#36657](https://github.com/kirodotdev/KiroCrew/pull/36657) closed unmerged.

This section reverses the pin in §5 item 7 and the "held boards stay held"
limit in §7. It builds on this document (on main) and on its code PR
[#36481](https://github.com/kirodotdev/KiroCrew/pull/36481) (open), whose
First Principles review asked for the reversal to be decided here first
([comment](https://github.com/kirodotdev/KiroCrew/pull/36481#issuecomment-6847130133)).

**Why.** Queued work is not on the ledger. A conductor that paused dispatch at
`tight` keeps the rest of its round in its own plan. If every open item then
waits on a person (`reason` `approval` or `needs_human`), the person-wait hold
([rfc-work-ledger-person-wait-hold.md](rfc-work-ledger-person-wait-hold.md)
§3.2) holds the patrol. It runs before the probe, so the `clear` wake of §3.3
never reaches the conductor, and the queued work never resumes. So §7's
"nothing is running there to pause" was true, but something was waiting there
to resume.

**Decision.** A pressure level change is a new fact the patrol must see.
`person_wait_fingerprint` also carries the `host_pressure` record's episode and
level. A change of either releases a held board for one turn. That turn
records the new fingerprint, so the next tick holds again if every open item
still waits on a person. So a held board gets one turn per episode and level,
typically two per episode (enter and clear).

**What stays the same.** Every other part of the hold. The fingerprint still
leaves out the conductor's own writes. The 30 s refresh and the 30-min
critical re-alert keep the same episode and level, so they release nothing. An
absent record adds nothing to the fingerprint. A `clear` record that expires to
absent moves the fingerprint once more, which costs a held board one more turn.
Held boards stay held for every other reason.

**Cost.** At most one extra conductor turn per held board per level change,
plus one when `clear` expires. No new tool, field, status, config key or event
kind.

**Safety.** No new authority. The record is still written by the gateway
only. A worker cannot forge a level change, so it cannot use this to break a
hold.

**Test.** The code PR replaces §5 item 7's "stays held" test with one test: a
held board under `tight` gets one turn when the level goes to `clear`, then
holds again.

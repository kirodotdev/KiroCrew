---
title: Host resource pressure goes on the work ledger and wakes the conductor, which pauses dispatch
status: accepted
author: kirocrew agent session, directed by the repository owner
created: 2026-11-13
last-audited: 2026-11-13
audited-at: 9b32941be6
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Host resource pressure goes on the work ledger and wakes the conductor

Status: accepted. The repository owner asked for this on 2026-11-13 in the
conductor chat and chose the design below (option C of the design study). It
takes effect when this document merges; the code change follows in its own PR
and rebases onto the base that carries this document. Nothing is built. Every
code reference was read at `9b32941be6`.

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
7. Person-wait hold: a board whose open items all wait on a person stays held
   under a pressure wake. Pinned so a later change is deliberate.

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
- **Held boards stay held.** The person-wait hold runs before the probe, so a
  board whose open items all wait on a person does not wake on pressure.
  Nothing is running there to pause.
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
  absent after 90 s with no refresh.

## 11. Open questions

None open.

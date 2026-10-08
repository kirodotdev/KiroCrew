---
title: Work-ledger person-wait hold -- a conductor patrol rests while every open item waits on a person, and a worker can say why it is blocked
status: in-progress
author: iamwhatever, with kirocrew-worker
created: 2026-10-08
last-audited: 2026-10-08
audited-at: 230b9141c8
doc-pr: null
implementation-prs: []
tracking-issues: [18054]
supersedes: []
superseded-by: []
---

# RFC: Work-ledger person-wait hold

- Status: in-progress. The decision was made by the operator who owns the conductor patrol (2026-10-08). The implementation PR follows this document and links back to it; its First Principles lane reads an RFC's status off the base branch, so this document lands on its own first, as GOVERNANCE.md asks of an RFC.
- Amends [rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md) in one place, named in section 3.3: a `work-ledger` watch whose open items all wait on a person is no longer extended.
- Related: [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md) (the ledger and `work_report`), [rfc-crew-log-wake.md](rfc-crew-log-wake.md) (how a worker's report wakes the conductor).
- Measured at `230b9141c8`.

## 1. Problem

A conductor's `watch: "work-ledger"` loop is kept alive while any item is open. When the cycle cap or runtime budget runs out, `_extend_for_open_ledger` in `src/kiro_crew/autonudge_service/firing.py` raises it, up to the seven-day backstop. That is the right call while a worker is building or testing.

It is the wrong call when every open item is waiting on a person. A worker whose tool approval timed out, or whose question only the user can answer, will not move until that person acts. The loop still takes turns on its timer and on the quiet floor, and still extends itself, for up to seven days. Each turn reads a board that cannot change.

The ledger cannot tell these cases apart. `work_report` carries `status` (`blocked` or `question`) and free-text `summary`. A worker whose `git` call timed out on an approval reported `blocked` with a sentence; no code can read a sentence.

## 2. Goals and non-goals

Goals:

- A worker can say, as data, that its blocked item waits on a person: on an approval, or on a person's answer or act.
- A `work-ledger` watch rests while every open item waits on a person and nothing on the ledger has changed since the conductor last looked. It fires no turn, takes no floor turn, and is not extended.
- A worker's new report ends the rest with no re-arm by anyone.
- The rest is visible in `monitor_inspect`, in the goal popover, and as a WARNING in the gateway log.

Non-goals:

- Changing the quiet floor or the probe gate in `autonudge_service/gate.py`. The hold sits in front of them.
- A notification when the hold starts. The conductor's own last turn is the one that reports the wait to the user.
- Holding any loop other than a `work-ledger` watch.
- Inferring a reason from the summary text.

## 3. Design

### 3.1 The `reason` field

`work_report` takes an optional `reason`. Allowed values:

| `reason` | Means |
|---|---|
| `approval` | a tool approval timed out or is waiting |
| `needs_human` | only a person can answer or act |

Both mean "only a person can move this item", and both count toward the hold. A wait on a build, service or other item carries no reason: no code reads a third value, so none is offered.

- It is accepted only with `status` `blocked` or `question`. With `progress` or `done` the report is refused (`invalid_value`, field `reason`).
- It is a worker-owned field, like `summary`. Every report replaces it, so a report without `reason` clears it.
- It is stored on the item, carried in the item's `work/recorded` crew-log entry, and rebuilt by the `work` fold, so a rebuilt ledger keeps it.
- `work_ledger_read` shows it on every row, full and compact.

### 3.2 The hold

On each tick of a `work-ledger` watch, before the cycle cap is checked, the timer reads the ledger once (off the event loop) and asks two questions:

1. **Is everyone waiting on a person?** True when the ledger has at least one open item, every item file is readable, every open item has `status` `blocked` or `question` with `reason` `approval` or `needs_human`, and no open item's worker session is gone. A worker whose session closed leaves nobody to answer its approval, so its item does not count as a wait on a person, and the probe's stall wake still reaches the conductor.
2. **Has any worker said anything since the conductor last looked?** The loop records a fingerprint of the ledger's worker-owned fields (each item's `status`, `reason`, `summary`, `artifacts`, `pr` and `last_report_at`) when it delivers a turn. The tick compares the ledger's current fingerprint with that one. The conductor's own writes (decide, verdict, accept, close, goal) are left out: the conductor already knows what it wrote, and counting them would let a conductor that writes the ledger every turn never stay held. This is the same rule the quiet floor's ledger revision already uses (#18112), which counts worker reports only.

When both are true, the loop **holds**:

- It fires no turn. The probe gate is not consulted, so no quiet-floor turn fires either.
- It is not extended and not stopped by a spent bound: the hold is checked before both.
- It re-arms at its own interval, so the next check costs one ledger read and no model call.
- It sets `waiting_on_person` on the loop record and logs one WARNING when the hold starts.

**Release.** A worker's new report changes the fingerprint, even one that stays blocked (its `last_report_at` moves). A conductor write does not. A new item the conductor creates has no report yet, so the ledger no longer waits only on people and the next tick fires. The next tick finds the fingerprints different, clears `waiting_on_person`, logs a WARNING, and runs as a normal tick. A worker's report also pulls that tick forward through the existing crew-log wake, so release is immediate. No person or agent has to re-arm anything.

The turn that delivers after a release records the new fingerprint. If every open item still waits on a person, the tick after it holds again. So the conductor gets exactly one turn per worker report while everyone waits. A person's Nudge now still fires a turn whenever they want one.

**Approval hold first.** A loop already held by the approval hold ([rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md)) is left to it: that hold arms nothing, and this one's interval re-arm must not wake it on a schedule.

**Doubt fires.** An unreadable ledger, a torn item file, a read that raises, or a loop with no recorded fingerprint (a fresh loop) does not hold. That keeps the shipped behaviour whenever the hold cannot prove itself.

**Restart.** The recorded fingerprint and `waiting_on_person` persist with the loop. The boot replay tick reads the ledger and holds again if nothing changed while the gateway was down.

### 3.3 Interaction with the bound extension

This amends [rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md) section 3.2. A held loop never reaches the bound checks, so it is not extended while held. On release it reaches them as before: a spent bound with an open item is extended if the loop is younger than the backstop, and stops the loop otherwise.

Held time **is** credited back to the runtime clock, the same way the approval hold does it. The loop records when the hold began (`waiting_on_person_at`), and on release adds the held time to `created_ts`. Without the credit, a hold longer than the backstop would end at release: the tick carrying the news would stop the loop instead of delivering the turn. The backstop still bounds the loop's unheld life, and held time fires nothing.

### 3.4 Visible

- `monitor_inspect` reports `waiting_on_person`.
- The REST loop row and the `autonudge_state` frame carry it, so the goal popover shows the loop as **Waiting on you** with a line naming why: every open task needs a person.
- The hold's start and its release each log one WARNING naming the loop.

### 3.5 Guidance

- The worker prompt and the goal-conductor skill tell a worker: when a tool approval times out, report `blocked` with `reason=approval`; when only a person can unblock you, use `needs_human`; for a build or service you do not control, leave `reason` unset.
- The goal-conductor skill tells the conductor that a ledger waiting only on people holds its patrol, and that its job on that turn is to put the ask in front of the user.

## 4. Risks

- **A wrong reason holds a patrol.** A worker that says `needs_human` while it could move on its own stops being patrolled until something on the ledger changes. Its own next report releases the hold, and a stalled worker shows in `work_ledger_read`.
- **A hold nobody ends.** If the person never acts, the loop stays held. It costs one file read per interval, no turns. It shows in the popover and in `monitor_inspect`.
- **Held-time credit stretches the wall clock.** A loop held for a week runs a week later than its wall clock suggests. The credit covers only held time, and held time fires nothing.
- **A conductor write does not wake its own patrol.** Closing or re-planning items is the conductor's own act inside a turn, so it already read what it wrote. If the write leaves an item that no longer waits on a person (a new item, say), the next tick fires because the all-wait condition fails, not because the fingerprint moved.

## 5. Security

No new authority. `reason` is one more worker-owned field on the worker's own item, behind the same binding and validation as `status`. The hold only withholds fires; the release only lets an already-armed loop run. Nothing a worker writes can make a conductor's loop fire more than it does today.

## 6. Alternatives considered

- **Stop the loop instead of holding it.** A person would then have to re-arm the patrol after answering. Rejected: that is the failure the approval hold already removed.
- **Hold on the reason alone, with no fingerprint.** A worker's new report that stays blocked would not reach the conductor. Rejected: any report is news.
- **Count the conductor's own writes as a change.** A conductor that writes the ledger on every turn (a decide, a goal) would change the fingerprint every turn and never stay held. Rejected; only worker-owned fields count.
- **Record the fingerprint at turn end instead.** It would also skip the conductor's in-turn writes, but a worker report that lands mid-turn would then be counted as seen. Rejected in favour of leaving conductor writes out.
- **Let held time spend the runtime clock.** A hold past the backstop would then end at release, answering the news with a stop instead of a turn. Rejected.
- **Infer the reason from the summary.** Free text is not a contract. Rejected.
- **A third value, `external`, for a build or service.** It would change nothing: the hold treats it exactly like no reason, so it is only stored and shown. Rejected; a wait on something that moves on its own carries no reason.

## 7. Open questions

None open.

## 8. Rollout

One implementation PR, after this document lands. Exit criteria, each pinned by a test there:

- A ledger whose open items all wait on a person, unchanged since the last delivered turn, holds the loop: no fire, no extension, `waiting_on_person` set.
- A new worker report releases the hold, and the next tick fires.
- While held, a spent bound runs no extension and does not stop the loop.
- A mixed ledger (one item still progressing, or one blocked with no reason) does not hold.
- `reason` is refused with `progress` or `done`, stored, cleared by a later report, rebuilt by the fold, and shown by `work_ledger_read`.

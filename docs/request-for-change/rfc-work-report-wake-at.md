---
title: Work-report wake_at -- a worker that waits on a time says when, and the conductor is woken then
status: accepted
author: iamwhatever, with kirocrew-worker
created: 2026-11-03
last-audited: 2026-11-03
audited-at: 41bade9256
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Work-report `wake_at`

- Status: accepted. The repository owner chose this design on 2026-11-03, over the two alternatives in section 6. Nothing is built. This document lands on its own first, as GOVERNANCE.md asks of an RFC, because it adds a field to the worker contract and to a crew-log entry. The implementation follows in its own pull request and references this one.
- Amends [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md) (the `work_report` fields) and [rfc-crew-log-wake.md](rfc-crew-log-wake.md) (which reports wake the conductor) in the one place section 3 names: a `progress` report that carries `wake_at` wakes the conductor once, at that time.
- Related: [rfc-work-ledger-person-wait-hold.md](rfc-work-ledger-person-wait-hold.md) added `reason` to `work_report` the same way this adds `wake_at`; [rfc-conductor-default-patrol.md](rfc-conductor-default-patrol.md) owns the patrol text this changes.
- Measured at `41bade9256`.

## 1. Problem

A worker often has to wait on a time. The common case is GitHub's hourly API rate limit: a review lane fails with "rate limit exceeded", and rerunning it before the window resets only fails again. The worker knows the moment ("after 00:03Z"). The ledger has no field for it.

Workers must not use the `wait` tool. A `wait` holds the worker's turn open, and the worker's turn runs on the shared kirocrew-core pool, so a long `wait` blocks other sessions. The worker prompt tells it instead to report `progress`, name the wait in `summary`, and end its turn (`_WORKER_SYSTEM_PROMPT` in `src/kiro_crew/agent_parts/spec_prompts.py`, around line 584).

Nothing then wakes anyone at the right time:

1. `progress` never wakes the conductor. `WORK_WAKE_STATUSES` in `src/kiro_crew/work_vocab.py` is `done`, `blocked`, `question`, `reply`, and `is_actionable_event` in `src/kiro_crew/ledger_wake.py` refuses `progress` by status alone. That is deliberate: most `progress` reports need nothing.
2. The stall wake fires once per report. In `observe` in `src/kiro_crew/probes/work_ledger.py`, a stale item spends the token `stall:<last_report_at>` through `_spend`, and a spent token never wakes again while the item is open. The stale window is 900 s (`DEFAULT_STALE_WINDOW_SECS` in `src/kiro_crew/work_ledger_records.py`). A wait longer than 15 minutes gets its one stall wake before the moment it waits for.
3. The conductor's patrol text says "Ignore progress." (`PATROL_MESSAGE` in `src/kiro_crew/conductor_patrol.py`), and the goal-conductor skill's status table says `progress` means "nothing" (`src/kiro_crew/builtin_skills/goal-conductor/SKILL.md`, step 2). So the one stall wake does nothing either.
4. After that the ledger reads idle, and an idle work-ledger watch waits `_IDLE_LEDGER_QUIET_FLOOR` (40) quiet ticks before its floor turn (`src/kiro_crew/autonudge_service/gate.py`). At the 600 s patrol interval (`PATROL_INTERVAL_SECS` in `conductor_patrol.py`) that is about 6.7 hours.

On 2026-11-02 this left a worker idle for about 6.7 hours. It reported `progress` at 23:18 UTC, waiting for a rate-limit reset at 00:03. The single stall wake reached the conductor at 23:38, 25 minutes too early, and the conductor correctly found nothing to do. The next conductor turn was the floor turn, after 05:20 the next morning.

The gap is old, not a regression. The work-ledger wake shipped with the one-shot stall token in #12781 (2026-10-02). #18112 (2026-10-08) raised the idle floor from 10 to 40 ticks to save cost, which made the worst case four times longer. The research behind this section, with file and line references, is summarised in this document's pull request description.

## 2. Goals and non-goals

Goals:

- A worker that stops to wait on a time says when, as data, on its `progress` report.
- The conductor is woken once when that time comes, with no model turn and no network call before it.
- The wait shows in `work_ledger_read` and on the goal board, so a person can see "waiting until 00:05" instead of reading prose.
- A waiting worker is not flagged stale before its time.

Non-goals:

- **Event waits** ("when CI finishes", "when the PR merges"). The worker turns an event wait into a recheck time (section 5).
- Waking the worker directly. The conductor already owns `session_send` to its workers; this keeps one path.
- Changing which statuses wake the conductor, the stall token, or the idle floor.
- Any change to `wait`, `monitor_start` or cron.

## 3. Design

### 3.1 The field

`work_report` takes an optional `wake_at`: an ISO-8601 UTC timestamp.

| Rule | Refusal |
|---|---|
| Legal only with `status: progress` | `wake_at_requires_progress` |
| Must be in the future | `wake_at_in_past` |
| At most 24 h ahead | `wake_at_too_far` |
| Must parse as an ISO-8601 instant with a zone | `wake_at_invalid` |

A refusal writes nothing, like every other `work_report` refusal. The 24 h cap matches `pending_overdue`'s day and keeps a typo from parking an item for a week. A wait longer than a day is a `blocked` report, not a timer.

Like `reason`, `wake_at` is a worker-owned field that every report replaces. A later report without `wake_at` clears it. A later report with a new `wake_at` replaces it.

### 3.2 Where it is stored and shown

- The ledger item (`WorkItem` in `src/kiro_crew/work_ledger_records.py`) gains `wake_at: str | None`. `apply_worker_report` in `src/kiro_crew/work_ledger_writes.py` writes it with the other worker fields.
- The crew-log report entry (`src/kiro_crew/crew_log/entry_types_ledgers.py`) gains an optional `wake_at` string, and the work fold (`src/kiro_crew/crew_log/projection_work.py`) carries it, so `work_ledger_rebuild` restores it. An old entry without it folds to `None`.
- `work_ledger_read` returns it, in the full read and in the compact columns (`_COMPACT_ROW_FIELDS` in `src/kiro_crew/dashboard/handlers/work_ledger_read.py`).
- A report that travels from a remote instance (`src/kiro_crew/work_ledger_remote.py`, `src/kiro_crew/dashboard/handlers/remote_work_item.py`) carries it the same way it carries `reason`.
- The goal board shows a `progress` item with a future `wake_at` in its waiting row as "waiting until HH:MM" (`src/kiro_crew/dashboard/crew_work.py`, which already sorts items into waiting and needs-you).

### 3.3 The wake

The work-ledger probe (`observe` in `src/kiro_crew/probes/work_ledger.py`) does one more check per open item per tick: if `status == "progress"`, `wake_at` is set, and `now >= wake_at`, it raises one WAKE observation with `reason=wait_due`.

- It spends the token `wait:<last_report_at>:<wake_at>` through the existing `_spend`, with `replaces="wait:"`. So one report wakes once, a re-read never wakes twice, and the per-item budget (`MAX_WAKES_PER_ITEM_PER_HOUR`, 12) still applies.
- The check is a timestamp compare on data the probe already reads. No network call, no model turn, nothing until the time comes.
- A WAKE observation is delivered like a stall wake today, so the idle floor does not hold it back.
- This is the amendment to rfc-crew-log-wake.md: `progress` still does not wake on arrival. It wakes once, later, when it said it would.

A new `wait_brief` in `src/kiro_crew/ledger_wake.py`, next to `stall_brief`, writes the wake text: the item id, that its wait is due, and the instruction in section 3.5.

### 3.4 Staleness

`is_stale` in `src/kiro_crew/work_ledger_rules.py` returns false for an item whose `wake_at` is still in the future. The worker said when it would be back, so silence before then is expected. Once `wake_at` passes, the quiet window is measured from the later of `last_report_at` and `wake_at`. So the `wait_due` wake comes first, and if the conductor's nudge does not get the worker moving, the stall wake still fires one window later. This uses the same exemption shape as `worker_looping`.

### 3.5 Conductor and worker text

- `PATROL_MESSAGE` in `conductor_patrol.py`: "Ignore progress" becomes "Ignore progress, except a `wait_due` wake: read that item, then session_send its worker to continue."
- goal-conductor `SKILL.md`, step 2 table: the `progress` row gains "on a `wait_due` wake, read the item and `session_send` the worker to continue its next step".
- The worker prompt (`_WORKER_SYSTEM_PROMPT` in `spec_prompts.py`) and the `work_report` tool description (`src/kiro_crew/mcp_work.py`): when you stop to wait on a time, report `progress` with `wake_at` set to when you should be resumed, then end your turn. When you wait on an event, pick a recheck time.
- The `work_report` schema (`WORK_REPORT_SCHEMA` in `src/kiro_crew/validation_parts/opt_in_server_schemas.py`) gains the field and its rules.

## 4. Behaviour on the 2026-11-02 case

| Time (UTC) | Today | With `wake_at` |
|---|---|---|
| 23:18 | `progress`, prose "after ~00:03Z" | `progress`, `wake_at=00:05Z` |
| 23:33 | stale | not stale (wake_at is ahead) |
| 23:38 | one stall wake, nothing to do | nothing |
| 00:05 | nothing | one `wait_due` wake; conductor tells the worker to rerun |
| ~05:20 | floor turn | (worker long since moved on) |

## 5. Why event waits are out of scope

A wake on "CI finished" or "PR merged" needs the gateway to poll GitHub for each waiting item. That poll spends the same per-installation API budget whose exhaustion caused the wait in section 1, so it can make the problem it is meant to solve. The `pr_checks` and `pr_merged` acceptance kinds are only evaluated by the conductor's `accept_eval.py`, in a model turn; nothing in the gateway polls them today. A worker waiting on an event picks a recheck time instead ("CI usually takes 40 minutes, so `wake_at` is 40 minutes from now"). If that proves too coarse, an event wait is its own RFC.

## 6. Alternatives considered

| Option | Why not chosen |
|---|---|
| The worker arms its own one-shot `monitor_start` before it stops | Smallest change (prompt text only), but the wait stays invisible on the ledger and the board, and it fails silently when the worker forgets or already holds a loop (one automation per session). |
| Re-wake the conductor on stale, with a time-bucketed stall token | Costs a conductor model turn per re-wake on every long wait, even before the wait is over, and undoes the saving #18112 chose on purpose. |

## 7. Size and files

About 80 to 120 lines of non-test code, plus tests.

| File | Change |
|---|---|
| `src/kiro_crew/validation_parts/opt_in_server_schemas.py` | schema field and the four refusals |
| `src/kiro_crew/mcp_work.py` | tool description; pass the field through |
| `src/kiro_crew/work_ledger_records.py` | `WorkItem.wake_at`, serialise and load |
| `src/kiro_crew/work_ledger_writes.py`, `work_ledger_remote.py` | write it in `apply_worker_report` and the remote report path |
| `src/kiro_crew/crew_log/entry_types_ledgers.py`, `projection_work.py` | entry field and fold |
| `src/kiro_crew/dashboard/handlers/work_ledger_read.py` | compact column |
| `src/kiro_crew/dashboard/handlers/remote_work_item.py` | carry it on remote reports |
| `src/kiro_crew/work_ledger_rules.py` | `is_stale` exemption |
| `src/kiro_crew/probes/work_ledger.py`, `ledger_wake.py` | `wait_due` wake and its brief |
| `src/kiro_crew/conductor_patrol.py`, `builtin_skills/goal-conductor/SKILL.md`, `agent_parts/spec_prompts.py` | text |
| `src/kiro_crew/dashboard/crew_work.py` | "waiting until" on the board |
| `src/kiro_crew/docs/work-ledger.md` | user doc |

## 8. Tests the implementation must carry

- Each refusal in section 3.1, and that a refused report writes nothing.
- A report without `wake_at` clears an earlier one; a new one replaces it.
- The probe raises exactly one `wait_due` wake when `now >= wake_at`, none before, and none on a re-read.
- A new report with a new `wake_at` can wake again.
- `is_stale` is false before `wake_at` and follows the normal rule after it.
- A crew-log rebuild restores `wake_at`; an old entry without it folds to `None`.

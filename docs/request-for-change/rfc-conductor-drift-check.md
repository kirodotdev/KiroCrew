---
title: The goal conductor reads each open worker's new transcript lines on every patrol turn and steers drift
status: accepted
author: kirocrew agent session, directed by the repository owner
created: 2026-11-13
last-audited: 2026-11-13
audited-at: 1bfb59ea14
doc-pr: null
implementation-prs: [36302]
tracking-issues: [36299]
supersedes: []
superseded-by: []
---

# RFC: The goal conductor checks each worker for drift on every patrol turn

Status: accepted. The repository owner asked for this in the conductor chat on
2026-11-13 and merges this document; it takes effect when it merges. Nothing is
built by this document and it changes no code. The skill and prompt change is
[#36302](https://github.com/kirodotdev/KiroCrew/pull/36302), which rebases onto
the base that carries this file. Every code reference below was read at
`27e9359419`.

This document lands on its own because it changes what the goal conductor's
default patrol does, and the First Principles lane reads that decision from the
base branch. It amends
[`rfc-conductor-work-ledger.md`](rfc-conductor-work-ledger.md), which replaced
the transcript-reading patrol with a ledger read.

## 1. Problem

The owner's ask, verbatim: "类似的问题，conductor没有办法主动对worker的工作纠错。我们目前其实有N轮一定苏醒，但是苏醒后它应该主动检查下worker的工作，是否偏离方向"
(the conductor has no way to correct a worker's work; it already wakes at
least every N quiet rounds, and on waking it should check whether each worker
has drifted).

Today `goal-conductor/SKILL.md` patrol step 5 allows `session_read_message`
"for detail the record does not carry ... never as the routine cycle read", and
its Cost discipline section repeats the limit. The conductor therefore sees
only each worker's own `work_report` claims. A worker that builds something its
item does not name, widens scope or redoes finished work looks on track until
its `done` fails acceptance, after the whole item's run is spent.

The patrol already wakes at the right moments: a `blocked`, `question` or
`done` report, a `stale` or `orphaned` flag, and the floor turn the gate
delivers after a run of quiet ticks (`_MAX_QUIET_STREAK` in
`src/kiro_crew/autonudge_service/gate.py`). Nothing on that turn looks at what
the worker is doing.

## 2. Decision

On every patrol turn, the floor turn included, the conductor reads each open
local item's worker with `session_read_message`, `since` = the cursor it stored for
that item and `limit: 10`. It compares the new assistant lines with the item's
`title`, its acceptance and the goal. Off direction, it sends a correction with
`session_send` `steer: true` and records it with `work_ledger_record`
`action=decide`. On track, it says nothing. It keeps each item's cursor in its
session ledger's `next`.

A remote-dispatched item (`work_ledger.remote_dispatch`) is skipped on a patrol
wake: `session_read_message` and `session_send` with `crew=` need an owner
turn, as the goal-conductor skill's remote-dispatch section already says. Its
drift is checked in the next owner turn, under that section's existing rule.

### 2.1 What this read is not

It is not a liveness check. `stale` and `orphaned` on the ledger stay the only
liveness signal, as `rfc-conductor-work-ledger.md` decided (its acceptance
list: "The skill no longer instructs `session_read_message` for liveness").

It is not a verdict. Acceptance stays `accept_eval`'s answer over the item's
bar; a transcript never passes or fails an item. The read only decides whether
to send a steer, which the worker reads as an ordinary correction.

`rfc-conductor-work-ledger.md` (line 722) already keeps the
conversation: "When the conductor needs detail the summary does not carry, it
uses `session_send` to ask and `session_read_message` to read the answer ...
The ledger removes the polling, not the conversation." The drift read is that
conversation, bounded, on turns the conductor is already taking.

### 2.2 Cost

- `since` returns only rows after the stored cursor, so an item whose
  transcript did not move costs one call and no rows.
- `limit: 10` caps each read. When a worker produced more than 10 rows (the
  reply's `total` is past `next_since`, or the cursor is refused
  `cursor_unavailable`), the conductor reads the newest 10 once without `since`
  instead of paging the backlog.
- At most two reads per open item per patrol turn: the `since` read, plus the
  newest-10 read only when the worker overflowed. So an item costs at most 20
  rows, and a turn at the default concurrency cap of 6 open items reads at most
  12 calls and 120 rows, each row capped at `MAX_READ_CONTENT_CHARS` = 4000 in
  `session_control_parts/session_status.py`.
- Patrol turns come only from a report, a flag or the quiet floor. A
  `watch="work-ledger"` tick with nothing new still costs no turn, so the read
  adds no turns of its own.

## 3. Why a monitor judge cannot replace it

`monitor_start`/`monitor_update` accept a `judge` whose `targets` may be worker
chat keys and whose evidence includes "a watched session's new assistant
lines". A `wake_when` like "a worker's new lines show it working outside its
item" looks like the same check. It is not enough:

1. **It screens quiet ticks only.** On a monitor-backed loop the judge is asked
   only when the probe reads QUIET (the `irq.Outcome.QUIET` branch of
   `_monitor_tick_is_quiet` in `autonudge_service/gate.py`); a report wake is delivered
   without it. The turns the owner named (a wake) are exactly the ones it never
   screens.
2. **It cannot see the bar.** The judge reads two criteria of at most
   `MAX_JUDGE_CRITERION_CHARS` = 500 characters and up to `MAX_JUDGE_TARGETS`
   = 8 targets (both in `validation_parts/core_agent_schemas.py`).
   Six items' titles and acceptance conditions do not fit, so it would judge
   drift against nothing specific.
3. **It cannot steer.** It answers wake or quiet. A wake still needs the
   conductor to read the lines to say what is wrong.
4. **Its targets move.** Every dispatch changes the worker set, so the brief
   would need a `monitor_update` per dispatch, and an update resets the judge's
   cursors and quiet streak.

The judge stays available as an optional earlier wake. It is not adopted as the
check.

## 4. Non-goals

- No new code, tool, field, schema or stored format.
- No change to `accept_eval` or verdict recording. The acceptance bar was a
  non-goal here too; the amendment below lets the conductor correct one item's
  bar mid-round, never weaker than the goal asks.
- No change to liveness: `stale` / `orphaned` are untouched.
- No change to the crewmate conductor's grants; it reads the same shared rule
  (`read_message_detail` in `src/kiro_crew/agent_parts/spec_prompts.py`).

## 5. Rollout

One phase: [#36302](https://github.com/kirodotdev/KiroCrew/pull/36302) edits
`goal-conductor/SKILL.md` (patrol step 5, the round-close `next` record, Cost
discipline) and the shared `read_message_detail` rule. Exit criteria: the
skill's step 5 names the `since` cursor, `limit: 10`, the at-most-two-reads
bound, the remote-item skip and the steer; the parity test (`test/test_mate_conductor_parity.py`) passes with the shared rule; the
agent spec byte goldens move only for `kirocrew-conductor.json` and
`kirocrew-ledger-conductor.json`.

## 6. Backward compatibility

Compatible. A conductor that ran the old skill keeps working; the new step
only adds reads it was already granted (`session_read_message` is
auto-approved for the conductor). Sessions with no stored cursor read without
`since` once and store one.

## 7. Security considerations

The read goes through `session_control.read_messages`, which authorizes with
`authorize_target` and redacts each row before returning it. The conductor can
read only sessions it could already read by hand. No new egress: the rows stay
in the conductor's own turn.

## Amendment 2026-11-13: re-plan one item mid-round on a drift finding

The owner's ask, verbatim: "看一看我们是不是做了drfit检查，能够中途换方案"
(check whether we have a drift check that can change the plan midway).
Every code reference in this section was read at `1bfb59ea14`.

### A.1 Problem

The drift read in section 2 can show that the item itself is wrong, not the
worker. Four cases come up: the work is already on main, the item is
mis-scoped, it needs a split, or its bar cannot be met. Section 2 only lets the
conductor steer the worker back toward the item as written. And
`goal-conductor/SKILL.md` (lines 576-577) says "Re-planning between rounds is
expected ... Re-planning mid-round is not: let the round finish." So the
worker keeps going on a wrong item until the round ends. Today the skill
re-plans a running item only when the person's own message invalidates it
(section "Goal changes mid-flight"), never on the conductor's own finding.

### A.2 Decision

When a worker's new lines show the item itself is wrong, the conductor
re-plans that one item in the same patrol turn. It picks one of three moves:

| Case | Move |
|---|---|
| Already done on main, or no longer needed | `work_ledger_record` `action=close` `state=abandoned` with the reason |
| Mis-scoped or needs a split | close it `abandoned` with the reason, then `create` the replacement item(s) at once |
| Bar is wrong or cannot be met | `action=accept` with the corrected bar |

Then it `session_send`s the worker what changed and records the instruction
with `action=decide` on the item that is still open: the re-barred item, or the
replacement. A closed item refuses `decide` (`item_closed`), so the reason for a
close goes in that `close` call's own `decision` field. A replacement item may
be bound to the same worker session after the close, as the skill's close step
already describes; `create` still counts against the open-item cap.

The tools already do this. `action=close` stores its `decision` text as the
reason on a `close` event. `action=accept` replaces an open item's bar and
appends a `decision` event (`apply_acceptance_update`). Both are in
`src/kiro_crew/work_ledger_writes.py`. Today the skill teaches `accept` only
for promoting a `TBD` PR number. No code, tool or stored format changes.

### A.3 Safety

- **Never weaker.** A corrected bar may never be weaker than the goal asks. It
  may name the right PR, the right check or a narrower item whose sibling
  carries the rest. It may not drop a check, lower a count or swap a
  machine-checked bar for a looser one to make an item pass.
- **Shown to the owner.** Every bar change is in the ledger as a `decision`
  event, and every item closed this way as a `close` event carrying the
  reason. The conductor names each one in its next report to the owner: the
  item, the old bar or the replacement, and why.
- **Item-level only.** This reverses SKILL:577 for one item at a time. A
  round-level re-plan (changing the round's plan, cancelling the round, or
  applying a changed goal) still waits for the round boundary.
- The verdict stays `accept_eval`'s answer over the item's bar, as section 2.1
  says. The transcript still never passes or fails an item.

### A.4 Who gets it

Both conductors. Ultra Mode reads it from `goal-conductor/SKILL.md`; the Bot
(crewmate) conductor reads it from the shared rule in
`src/kiro_crew/agent_parts/spec_prompts.py`, which
`test/test_mate_conductor_parity.py` keeps the same for both.

### A.5 Rollout

This extends the drift check of
[#36302](https://github.com/kirodotdev/KiroCrew/pull/36302), still open when
this was written. One follow-up prompt PR, after this amendment merges and
rebased on #36302 if that has landed: one paragraph after SKILL patrol step 5,
one sentence in the shared `read_message_detail` rule, and SKILL:577 reworded
to allow item-level re-plans. Exit criteria: the parity test passes, and the
spec byte goldens move only for the two conductor specs.

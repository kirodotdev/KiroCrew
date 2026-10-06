---
title: Goal popover Pause/Play controls — a readable loop state, one press to resume, a spent bound resets only its own counter
status: in-progress
author: pepmach
created: 2026-10-01
last-audited: 2026-10-02
audited-at: bc80d8d8a2
doc-pr: 16030
implementation-prs: [16030]
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Goal popover Pause/Play controls

- Status: in-progress. The design was decided by the product owner (pepmach / Stan) on 2026-10-01 on the thread of the superseded [#11478](https://github.com/kirodotdev/KiroCrew/pull/11478) and recorded here; the implementation is [#16030](https://github.com/kirodotdev/KiroCrew/pull/16030), which carries this document. The First Principles lane reads an RFC's status off the base branch, so until this document is on main it reads as `draft` to that lane; clearing the lane is a maintainer's call (an override on the final head, or merging this document first), not this PR's.
- Author: pepmach (product owner); written up by the crew worker that implements it.
- Created: 2026-10-01
- Related: [`../system-specs/modules/learn-cron-dashboard.md`](../system-specs/modules/learn-cron-dashboard.md) (AutoNudge: the loop service, `stopped_reason`, the runtime budget and the revival rule), [`../system-specs/modules/monitor-architecture.md`](../system-specs/modules/monitor-architecture.md) (the loop paradigm), [rfc-perpetual-agent.md](rfc-perpetual-agent.md) (the sibling decision on an uncapped loop), [`../../website/AUTOSDE.yaml`](../../website/AUTOSDE.yaml) (`max-two-buttons-per-row`, `icon-buttons-need-labels`).

## Summary

The goal popover (the composer's `Set a goal` chip) gets two icon-only controls while a loop exists — Pause, and a fire control that is a lightning glyph (nudge now) while the loop runs and a play glyph (resume and nudge now) while it is paused — plus `Clear stopped goal` as a text link that is the only way to delete a goal, behind its existing confirm. The loop's state is one plain-English line under a state-naming title: the countdown while running, `Paused · <why>` otherwise, with the reason read off the persisted `stopped_reason`. Resuming resets only the counter behind a spent bound: a loop that stopped on its cycle limit, or was paused with its cap reached, gets its cycle count restarted; one that stopped on its time limit, or whose time budget elapsed while paused, gets its clock re-anchored; each on its own, so it comes back with one press instead of being refused until a limit is raised. Everything else resumes from the cycle it stopped at: a loop paused by hand at 7 of 24 and resumed the next morning comes back at 7 of 24 on a fresh clock. The help lines that named `max_runtime_secs`, `monitor_start` and `monitor_update` are gone.

## Motivation

Measured at `bc80d8d8a2` on `website/src/components/AutoNudgePopover.tsx`:

- While a loop runs, the popover offers two text buttons, `Stop loop` and `Save`, and a `Trigger nudge` text button on the schedule line. `Stop loop` is a `DELETE`: the only way to halt a running loop is to remove the goal for good, and the text has to be retyped to bring it back.
- An inactive loop reads `Stopped` with a `Start loop` button whatever stopped it. A loop a person paused, a loop the agent stopped, and a loop that hit its cycle cap all look the same, so a reader cannot tell which one is safe to bring back, and nothing on the surface says why it ended.
- `Start loop` on an inactive loop sends `active: true`, but the timer re-checks the cycle cap and the runtime budget before every fire (`src/kiro_crew/autonudge_service/firing.py`), so a loop stopped on either is re-stopped on its first tick unless the bound was raised first. The one way out from the popover was to raise `Max cycles`; the runtime budget has no field there at all.

A goal loop is the one feature a person leaves running unattended. Its controls have to say what state the loop is in and what one press will do.

## Goals

- A loop can be paused and resumed in place without losing the goal text.
- The surface says why a loop is not running, in plain words, without naming a tool or a config key.
- One press resumes a loop that stopped on a limit.
- Every control is a glyph with an accessible name and a hover title; no visible label text (the dashboard's toolbar convention).

## Non-goals

- Restarting a finished goal from Done, and what the agent's own `autonudge_stop` call does to a loop. The Done state itself is in scope (see *Done*), added by the follow-up PR.
- A separate save-without-firing control: the fire control applies pending edits.
- Any change to how the stop file, the cycle cap or the runtime budget END a running loop. Only resume changes.
- Any change to the structured-monitor view of the same popover.

## Design

### State model

| State | Title | Status line under the title | Controls |
|---|---|---|---|
| No goal | `Set a goal` | none | Play alone: creates and starts the loop from the form, no fire |
| Running | `Goal active (cycle N/M)` | the countdown (`Next cycle in …` / `Next cycle due, fires after the current turn` / `Next cycle not yet scheduled`), boxed in the ok tone | Pause · Lightning (`Nudge now`, or `Save edits and nudge now` when the form is dirty) |
| Paused | `Paused` | `Paused · <why>`, boxed in the warn tone | Pause (disabled) · Play (`Resume loop and nudge now`, or `Save edits, resume loop and nudge now`), and `Clear stopped goal` as a text link at the left of the same row |
| Done | `Done` | `Done · <how it finished>`, boxed in the info tone, with a check-mark glyph before the text when the goal was reached or the pull request merged | Pause and Play, both disabled, with the Paused-state glyphs; `Clear finished goal` as a text link at the left of the same row |

The paused reason comes from the persisted `stopped_reason`: `manual` → you paused it; `autonudge_stop` → the agent stopped it; `cycle_cap` → cycle limit reached (N of M), Play resumes it with a fresh budget; `runtime_budget` → time limit reached, same clause; `approval_stalled` → waiting for your approval; `structural_terminal` and `session_start_failures` → the last cycle could not run; anything else → bare `Paused`. A limit stop therefore reads `Paused`, never `Stopped`: it is resumable in place, and the word `Stopped` leaves the surface.

The capability note of a crew or member session (writes unavailable) and the Clear confirm are not loop states and never share the status line's tone: while writes are disabled the running loop's countdown keeps its words but drops the ok tone, so it does not assert a fire beside a note saying the session cannot act.

### Controls

- Exactly two icon-only controls in every state while a loop exists, in one row at the right: Pause and the fire control. Names live in `aria-label` and `title`; a pending edit marks the fire control with a dot.
- Lightning while running means nudge now, saving pending edits first. A play triangle beside `Goal active` had no cold reading, so the running state does not use it.
- Play while paused resumes and fires, saving pending edits first — one request path (`PATCH {…edited fields, active: true}` then `POST …/fire`). The user never has to raise `Max cycles` first.
- `Clear stopped goal` is the only delete. It is a text link, not a button, at the left of the same row (the two-buttons-per-row rule counts the two icons; this placement is the product owner's call). Its confirm replaces the row with an accented box: the question in the primary text colour, a filled danger `Clear`, a plain `Cancel`; the status line stays above it so the confirm never reads as the loop's state.
- A running loop cannot be deleted directly: pause first, then clear.

### Done

A loop whose goal is reached is finished, not paused, and the surface says so. Two stops are finishes: the agent created the loop's stop file (`stopped_reason: stop_sentinel`, which now keeps the record instead of removing it — a removed row left the popover on its empty form with nothing saying the goal was met), and the watched pull request merged or closed (`monitor_terminal`). The title reads `Done`; the status line is boxed in the info tone and reads, by case, `Done · the agent created the stop file`, `Done · the watched pull request merged` or `Done · the watched pull request was closed without merging`, with a check-mark glyph before the first two and none before the third, which ended on a question rather than a success. Merged versus closed is read off the settled watch outcome the `autonudge_state` frame carries (`monitor_outcome`) — and, on a cold read after a reload, off the nested monitor record the REST row of a gated loop ships, which the dashboard now reads as the goal loop it is rather than as a bounded monitor — never off prose, and the pull-request wording applies only to a pull or merge request (`monitor_kind`): a finished watch of any other kind — a conductor's work ledger, which reaches the same reason when its board closes — reads `Done · the watched subject finished` with the glyph when it finished well, and `Done · the watched subject ended without finishing` without it otherwise: two sentences, not one sentence and a glyph, because a screen reader hears the sentence. The goal text and the two number fields stay visible, read-only and dimmed. The row keeps its shape — Pause and Play at the right, both disabled, with the Paused-state glyphs — and `Clear finished goal` replaces `Clear stopped goal` at the left, behind the same confirm. Done is a dead end: no Play, no edits; the service refuses to revive a finished loop whoever asks (the popover's Play, `monitor_update`, an app reconciler). To run again the person clears the goal and sets a new one. One exception lives below the popover: Issue Radar and Research Lab arm the same stop file as an operator's kill switch on a crew or campaign, so a loop its stop file finished may be re-armed by its reconciler once the file is gone — deleting the file is the operator saying run again. The popover never deletes the file, so a goal's Done keeps its shape and its Play stays disabled.

### Resume resets only the counter behind a spent bound

The user's resume — `update(active=True, fresh_run=True)` reaching an inactive loop, keyed on the loop having been inactive and not on any `active: true` — resets each counter on its own, and only when the bound behind it is spent (`src/kiro_crew/autonudge_service/mutations.py`). **A spent cycle cap zeroes `cycle_count`**: the persisted `stopped_reason` is `cycle_cap`, or `cycle_count >= max_cycles` with a non-zero cap as the bounds stand after the same update (`cap_reached` in `model.py`). **A spent time budget re-anchors `created_ts`**: the reason is `runtime_budget`, or `now - created_ts >= max_runtime_secs` with a non-zero budget (`budget_elapsed`). The at-the-press readings exist because the wall clock keeps running through a pause, and a cap raised in the same save reads as raised. Both predicates read the bounds type-safely, because the store is agent-writable. A counter whose bound is not spent is kept, so a loop paused by hand at cycle 7 of 24 with an hour of budget and resumed the next morning comes back at cycle 7 of 24 on a fresh clock, and the popover reads `Goal active (cycle 7)`. Max cycles is a lifetime limit a person typed; a pause must not quietly turn it into a fresh allowance, and the cycle readout after Play must match what actually ran. No time-spent-paused bookkeeping is kept; the budget stays a wall-clock bound measured from `created_ts`. A loop paused with both allowances left — `manual`, `autonudge_stop`, `approval_stalled`, `structural_terminal`, `session_start_failures`, or no recorded reason, with cycles and time remaining — resets nothing. `created_ts` is the clock's anchor because every reader of the runtime left already measures from it (the timer's budget predicate, the patrol-budget header line, the restore-after-failed-close remainder, `monitor_update`'s spent-budget guard), so one move keeps them consistent; its cost is that `created_ts` reads as the start of the current clock for a loop resumed from a spent budget. A still-active loop's settings save resets nothing. The reset is the resume's alone: `fresh_run` is passed only by the dashboard `PATCH` route behind the popover's Play. A revival without it — a `monitor_update` that raises the stopping bound, and the Research Lab and Issue Radar reconcilers, which re-arm any inactive loop of a live campaign or crew every few seconds — keeps both counters in every case, so a raise buys its increment and a reconciler cannot turn the user's cap into a per-run allowance (a cap-stopped campaign loop is re-stopped unfired on its first tick, as before). Because a resume now preserves the counters, `_load` repairs a malformed `cycle_count` or `created_ts` at the store boundary (to 0) the way it already repairs `idle_secs` and the start-failure streak; the bounds are deliberately left as stored, since repairing a malformed cap or budget to 0 would quietly remove a cost limit.

For a spent bound this is the Hermes rule: its `/goal resume` "resets the turn counter back to zero". The alternative — keep the counts and make the user raise the bound — is what forced the old surface to hold its resume control behind the bound and to explain `max_runtime_secs` in a help line. For a bound with allowance left the Hermes rule does not apply: nothing was spent, so there is nothing to reset.

### Help lines removed

The lines that named `max_runtime_secs`, `monitor_start` and `monitor_update` are retired with their catalog keys in every locale. The single status line replaces them. No user-facing string names an MCP tool or a config key.

## Migration plan

One PR, two commits, both in [#16030](https://github.com/kirodotdev/KiroCrew/pull/16030):

1. `feat(autonudge): resuming a loop resets its budget` — the opt-in resume reset (`fresh_run`, passed by the dashboard route alone), the frame fields the popover reads (`stopped_reason`, `next_due_ts` on every loop), the bound-keeps rule for a pause landing after a bound, the spec text. Exit: a cap-stopped and a budget-stopped loop each fire again after the route's `active: true`; a bare revival keeps the count and is re-stopped unfired; a running loop's save keeps its count and clock.
2. `feat(autonudge): pause and play icon controls in the goal popover` — the surface above, the catalogs, this document. Exit: the popover renders the table above in every state, with the DOM assertions in `website/src/test/AutoNudgePopover.test.tsx`.

## Backward compatibility

No route, field or key the wire accepts is rejected, renamed or removed. The websocket frame gains two fields on plain loops. Revival behaviour changes only for the dashboard `PATCH` route's `update(active=True, fresh_run=True)`: a spent cycle cap (stopped on `cycle_cap`, or the cap reached) zeroes the count, a spent time budget (stopped on `runtime_budget`, or elapsed) re-anchors the clock, and every other counter and every other revival keeps the loop's count and clock as it always did. The retired catalog keys were referenced only by this popover.

## Security considerations

None new. The popover's writes go through the existing authorized `PATCH` / `POST` / `DELETE` routes; the DELETE carries the `clear` intent it already carried.

## Alternatives considered

- A red-square Stop that deletes the goal (the shipped shape, and the shape of the superseded #11478's first rounds). Rejected: a running loop's only halt was an erase, and a reader took `Paused` beside an erase control as a mixed message.
- A separate Save button beside Play. Rejected: two write controls side by side left a reader unable to tell the two writes apart; the fire control applies pending edits, and a dot says when it will.
- Stop as a restart ("Stop loop" that keeps the record, `Start loop` that revives it). Rejected: it kept the word `Stopped` on a resumable record and still refused a limit-stopped loop until a bound was raised.
- Resume without a budget reset for a limit-stopped loop, routing the user to raise the bound. Rejected: see *Resume resets only the counter behind a spent bound*.
- Resume resets the budget for every paused loop (the shape #16030 first shipped). Rejected: a loop a person paused at cycle 7 of 24 came back as cycle 1 of 24, turning the lifetime cap into a fresh allowance and making the cycle readout after Play disagree with what had run.
- Reset keyed on the stored stop reason alone. Rejected: a loop paused by hand whose budget elapsed while paused, or paused at exactly its cap, was re-stopped unfired on the first Play and reset only on the second press.
- Stop the budget clock while a loop is paused (a time-spent-paused field). Rejected for this change: the budget stays a wall-clock bound measured from `created_ts`; a spent budget is re-anchored on resume instead.
- Reset both counters whenever either bound is spent (one resume, one fresh run). Rejected: the time budget keeps running through a pause, so the most common pause — by hand, overnight, on a loop with a time budget — came back as cycle 1 of 24 with a fresh cycle allowance, the symptom this change exists to remove; resetting each counter only when its own bound is spent keeps the one-press resume (each pre-fire check passes) and the lifetime cap.

## Open questions

- Whether a finished goal should be restartable from Done (today: clear it and set a new goal), and whether the stop file should be removed once the finish is recorded (today it is left where the agent wrote it and unlinked before the next arm).
- Whether the Clear link's placement in the icon row stands against the `max-two-buttons-per-row` lane reading is the product owner's override to record on the PR, not a change to this document.

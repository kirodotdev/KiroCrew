---
title: Goal popover Pause/Play controls — a readable loop state, one press to resume, resume resets the budget
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

The goal popover (the composer's `Set a goal` chip) gets two icon-only controls while a loop exists — Pause, and a fire control that is a lightning glyph (nudge now) while the loop runs and a play glyph (resume and nudge now) while it is paused — plus `Clear stopped goal` as a text link that is the only way to delete a goal, behind its existing confirm. The loop's state is one plain-English line under a state-naming title: the countdown while running, `Paused · <why>` otherwise, with the reason read off the persisted `stopped_reason`. Resuming a loop runs it on a fresh budget: the cycle count restarts and the wall-clock budget re-anchors, so a loop that stopped on its cycle or time limit comes back with one press instead of being refused until a limit is raised. The help lines that named `max_runtime_secs`, `monitor_start` and `monitor_update` are gone.

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

- A "Done" state for a loop whose stop file was created or whose watched pull request merged. Follow-up.
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
| Done | — | — | follow-up (see Non-goals) |

The paused reason comes from the persisted `stopped_reason`: `manual` → you paused it; `autonudge_stop` → the agent stopped it; `cycle_cap` → cycle limit reached (N of M), Play resumes it with a fresh budget; `runtime_budget` → time limit reached, same clause; `approval_stalled` → waiting for your approval; `structural_terminal` and `session_start_failures` → the last cycle could not run; anything else → bare `Paused`. A limit stop therefore reads `Paused`, never `Stopped`: it is resumable in place, and the word `Stopped` leaves the surface.

The capability note of a crew or member session (writes unavailable) and the Clear confirm are not loop states and never share the status line's tone: while writes are disabled the running loop's countdown keeps its words but drops the ok tone, so it does not assert a fire beside a note saying the session cannot act.

### Controls

- Exactly two icon-only controls in every state while a loop exists, in one row at the right: Pause and the fire control. Names live in `aria-label` and `title`; a pending edit marks the fire control with a dot.
- Lightning while running means nudge now, saving pending edits first. A play triangle beside `Goal active` had no cold reading, so the running state does not use it.
- Play while paused resumes and fires, saving pending edits first — one request path (`PATCH {…edited fields, active: true}` then `POST …/fire`). The user never has to raise `Max cycles` first.
- `Clear stopped goal` is the only delete. It is a text link, not a button, at the left of the same row (the two-buttons-per-row rule counts the two icons; this placement is the product owner's call). Its confirm replaces the row with an accented box: the question in the primary text colour, a filled danger `Clear`, a plain `Cancel`; the status line stays above it so the confirm never reads as the loop's state.
- A running loop cannot be deleted directly: pause first, then clear.

### Resume resets the budget

The user's resume — `update(active=True, fresh_run=True)` reaching an inactive loop, keyed on the loop having been inactive and not on any `active: true` — zeroes `cycle_count` and re-anchors `created_ts` to the revival (`src/kiro_crew/autonudge_service/mutations.py`). `created_ts` is the anchor because every reader of the runtime left already measures from it (the timer's budget predicate, the patrol-budget header line, the restore-after-failed-close remainder, `monitor_update`'s spent-budget guard), so one move keeps them consistent; its cost is that `created_ts` now reads as the start of the current run. A still-active loop's settings save resets nothing. The reset is the resume's alone: `fresh_run` is passed only by the dashboard `PATCH` route behind the popover's Play. A revival without it — a `monitor_update` that raises the stopping bound, and the Research Lab and Issue Radar reconcilers, which re-arm any inactive loop of a live campaign or crew every few seconds — keeps the count and the clock, so a raise buys its increment and a reconciler cannot turn the user's cap into a per-run allowance (a cap-stopped campaign loop is re-stopped unfired on its first tick, as before).

This is the Hermes rule: its `/goal resume` "resets the turn counter back to zero". The alternative — keep the counts and make the user raise the bound — is what forced the old surface to hold its resume control behind the bound and to explain `max_runtime_secs` in a help line.

### Help lines removed

The lines that named `max_runtime_secs`, `monitor_start` and `monitor_update` are retired with their catalog keys in every locale. The single status line replaces them. No user-facing string names an MCP tool or a config key.

## Migration plan

One PR, two commits, both in [#16030](https://github.com/kirodotdev/KiroCrew/pull/16030):

1. `feat(autonudge): resuming a loop resets its budget` — the opt-in resume reset (`fresh_run`, passed by the dashboard route alone), the frame fields the popover reads (`stopped_reason`, `next_due_ts` on every loop), the bound-keeps rule for a pause landing after a bound, the spec text. Exit: a cap-stopped and a budget-stopped loop each fire again after the route's `active: true`; a bare revival keeps the count and is re-stopped unfired; a running loop's save keeps its count and clock.
2. `feat(autonudge): pause and play icon controls in the goal popover` — the surface above, the catalogs, this document. Exit: the popover renders the table above in every state, with the DOM assertions in `website/src/test/AutoNudgePopover.test.tsx`.

## Backward compatibility

No route, field or key the wire accepts is rejected, renamed or removed. The websocket frame gains two fields on plain loops. Revival behaviour changes for every caller of `update(active=True)` on an inactive loop; each of them asks for a fresh run. The retired catalog keys were referenced only by this popover.

## Security considerations

None new. The popover's writes go through the existing authorized `PATCH` / `POST` / `DELETE` routes; the DELETE carries the `clear` intent it already carried.

## Alternatives considered

- A red-square Stop that deletes the goal (the shipped shape, and the shape of the superseded #11478's first rounds). Rejected: a running loop's only halt was an erase, and a reader took `Paused` beside an erase control as a mixed message.
- A separate Save button beside Play. Rejected: two write controls side by side left a reader unable to tell the two writes apart; the fire control applies pending edits, and a dot says when it will.
- Stop as a restart ("Stop loop" that keeps the record, `Start loop` that revives it). Rejected: it kept the word `Stopped` on a resumable record and still refused a limit-stopped loop until a bound was raised.
- Resume without a budget reset, routing the user to raise the bound. Rejected: see *Resume resets the budget*.

## Open questions

- A "Done" state for a loop whose stop file was created or whose watched pull request merged: a follow-up decision, not this one.
- Whether the Clear link's placement in the icon row stands against the `max-two-buttons-per-row` lane reading is the product owner's override to record on the PR, not a change to this document.

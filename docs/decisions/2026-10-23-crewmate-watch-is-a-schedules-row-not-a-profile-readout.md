# What a crewmate watches on its own is one row on its Schedules tab, not a readout on its Profile tab

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-23

## Decision

The crewmate Profile card's Profile tab carries no monitor section. The patrol a crewmate runs over the tasks it handed out (its `work-ledger` loop) is one row on the Schedules tab under an **On its own** header, in the schedule row's grammar, with Pause / Resume as its one control; the loop's budget bookkeeping (check count and cap, runtime clock and budget, next-check timestamp) is shown nowhere and surfaces only as a one-phrase stop reason when a budget ended the loop.

## Why

- The nine-row Monitor section shipped in #20175 named an internal watch kind ("Task list"), left half its rows as dashes for the life of an active loop, listed budget counters a person can act on in no way, and had no control: after reading it the one thing a user wants, to pause it, was elsewhere. The maintainer called it confusing and without meaning.
- A crewmate's self-driven wakes were described by four words on four surfaces (Monitor, Heartbeat, Scheduled check-ins, Patrolling / On watch). To the user they are one question, when does this mate act without me, and the Schedules tab is the surface that already answers it; #23262 moves the Heartbeat row there for the same reason.
- The patrol is not a schedule: it fires on a worker's report, with a check at most every interval. Its row says so ("Wakes when a task reports back, or every 15m") instead of reading as "Every 15m".

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/23558 -- the issue recording the decision, the four real-SPA mockups compared, and the chosen one.
- https://github.com/kirodotdev/KiroCrew/pull/20175 -- the change that added the Profile monitor section this decision removes.
- https://github.com/kirodotdev/KiroCrew/issues/23262 -- the sibling decision moving the Heartbeat row to the Schedules tab.

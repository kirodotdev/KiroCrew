# M1: an iCalendar feed drops cancelled events and keeps the nearest under the cap

Tier: medium (two items in one round). Source: Kiro Crew issues #28572 and
#28652, fixed by PRs #28651 and #28655 (merged 2026-11).

## Where it comes from

The Meetings app's `.ics` parser never read `STATUS`, so cancelled meetings
were listed; and it stopped reading at the 500-event cap before sorting, so an
out-of-order feed could hide the nearest meetings. The seed paraphrases the two
issues as they were filed, before any PR opened.

## How the case is built

`base/ics_feed.py` is the parser from
`apps/builtins/meetings/backend/providers/calendar.py` at the first PR's base,
cut down to the parsing functions (no network fetch, no URL vetting) with the
app's constants inlined and its secret masker replaced by a no-op. `ref/` holds
it after both merged changes. The case builds offline in a second.

## The hidden check

`hidden/check.py` judges origin `trunk`: a cancelled event is dropped (status
trimmed, any case); tentative and confirmed stay; with more than 500 events in
any order the 500 nearest come back sorted by start; the visible tests still
pass. Its feed builders and the 500-event cap live in the check itself, so
editing the visible tests or the module's constant cannot change what it
measures. It fails on `base/` and passes on `ref/`.

## Traps

- Fixing one bug and not the other.
- Breaking out of the loop at the cap after sorting a partial list.
- Comparing `STATUS` without trimming or case folding.

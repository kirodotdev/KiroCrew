# A full-key chat link to a closed session offers Resume instead of "not found"

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-12

## Decision

When a `/chat?sid=<full key>` link, or a tab reloading one, names a session that
is not open but still has a transcript on disk, the page offers to resume it
instead of answering `Session "<key>" not found`; the fix stays in the page.

## Why

- The tab was left showing a dead "not found" with no way back to a
  conversation that still exists.
- Rolling the session map back after a handover, or widening the
  `session_not_found` recovery ladder, rests on a premise that does not hold:
  `projections/` is a savepoint written only past 256 log entries, so the
  mapped unit was a normal session, and the ladder is scoped to live backends
  on purpose.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/29460#issuecomment-6816926086 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/29460 -- the issue.

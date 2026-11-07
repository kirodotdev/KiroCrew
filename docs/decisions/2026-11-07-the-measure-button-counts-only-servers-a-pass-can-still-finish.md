# The Measure button counts only servers a measure pass can still finish

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-07

## Decision

The Sharing assessment's Measure button counts only the servers a measure pass
would still probe or measure under two callers, as marked by the gateway. With
none left it reads "Nothing left this button can measure", and the note above it
names the servers it does not count.

## Why

- After a pass, the button kept offering servers that only another agent
  declares. The pass never probes those, so pressing it again measured nothing.
- Measuring every listed row instead would start servers no pass started
  before, and a note alone would leave the wrong count in place.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/30353#issuecomment-6704964226 --
  the maintainer's ruling (option A).
- https://github.com/kirodotdev/KiroCrew/issues/30353 -- the issue and the
  triage that set out options A, B and C.

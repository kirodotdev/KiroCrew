# The prompt as sent is an owner-only read in the Context tab

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-06

## Decision

The text a turn sent the agent shows in the side panel's Context tab under the
selected turn, with no switch to turn it on; only the dashboard owner can read
it, an app caller gets a 404, secrets are masked when it is read, and nothing of
it is written to disk.

## Why

- Developer Mode was taken out of Logs, Context and Crew log on purpose, so a new
  switch for this view would bring back what that removal took away.
- The prompt carries the conversation and a crew member's own memory, so it stays
  with the one person who can already read both, by the same server-side owner
  check the Crew log tab uses.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/16613#issuecomment-6696937217 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/pull/24810 -- removed Developer Mode
  from Logs, Context and Crew log.
- https://github.com/kirodotdev/KiroCrew/issues/16613 -- the issue.

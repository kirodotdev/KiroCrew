# The terminal usage report counts the whole install's spend and prints no unreported figure as zero

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-14

## Decision

`kirocrew usage` counts the whole install's spend, every billed turn with
background and subagent turns included, and prints no figure a cell's harness
does not report as zero, applying the Daily History table's per-day rule to
every cell.

## Why

- A person on a browserless host and a cron script need the same spend the
  "Spend over time" chart shows, read from the one reader that chart uses, so
  the two figures reconcile.
- kiro-cli and KAS write cost and tokens as a hard zero, and a dollar-billed
  harness writes zero credits, so a zero in those fields means "not
  reported"; printing it would read as "nothing was spent".
- The rule is decided from the rows, never from config, because a crew member
  on another harness writes to the same store.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/35821#issuecomment-6872784090 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/35821 -- the issue.

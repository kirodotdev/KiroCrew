# The Storage report shows Claude Code's own store but never removes it

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-13

## Decision

The Storage report shows, as a separate read-only lower bound, how much Claude
Code keeps for the Claude chats Kiro Crew can still resume, counts it in no
session's size or the reclaimable total, and never removes Claude Code's files.

## Why

- It makes the disk use a claude install pays for visible.
- Reclaiming would be a new deletion path over a directory the operator's own
  Claude Code also writes.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32622#issuecomment-6860865698 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/32622#issuecomment-6717402391 --
  the recommendation it follows.
- https://github.com/kirodotdev/KiroCrew/issues/32622 -- the issue.

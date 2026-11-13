# The first Mate's welcome asks for its name, explains what a mate is and tours its tabs, instead of offering setup help

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-29

## Decision

The first Mate's welcome asks for its name first, explains what a mate is and tours its Profile, Schedules and Goals tabs, instead of offering to set up or fix Kiro Crew.

## Why

- The first conversation is where a user decides whether a mate is worth keeping. A welcome that sounds like a setup wizard and never shows where the goal or schedules live leaves Mate as a chat box rather than a teammate (the motivation on #25476).

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/25476#issuecomment-6485903226 -- the maintainer's owner decision on the pull request that implements it, merged as `ca8f35673b`.
- https://github.com/kirodotdev/KiroCrew/issues/25528 -- the follow-up that asks for this record.

This supersedes the first-welcome paragraph of `docs/request-for-change/rfc-crewmate-guides-and-mate.md` (§ "Part 2", "The first welcome": help set up Kiro Crew, fix problems in it, or show the user around), which had no entry in this directory.

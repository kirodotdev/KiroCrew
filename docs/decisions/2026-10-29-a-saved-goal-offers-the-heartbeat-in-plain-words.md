# A mate offers the Heartbeat in plain words when a goal is saved, not after milestones are confirmed

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-29

## Decision

The Heartbeat is offered in plain words when a goal is saved, instead of after milestones are confirmed.

## Why

- The Heartbeat is the switch that makes a mate work on its own. Offered only after milestones, and by a name the user does not know yet, it was never offered when the milestone research stalled (the motivation on #25476).

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/25476#issuecomment-6485903226 -- the maintainer's owner decision on the pull request that implements it, merged as `ca8f35673b`.
- https://github.com/kirodotdev/KiroCrew/issues/25528 -- the follow-up that asks for this record.

This supersedes the offer after the milestones are confirmed in `docs/request-for-change/rfc-crewmate-heartbeat.md` (§ 3.6 "Where it is said", § 7), which had no entry in this directory. The Heartbeat stays off until the user says yes.

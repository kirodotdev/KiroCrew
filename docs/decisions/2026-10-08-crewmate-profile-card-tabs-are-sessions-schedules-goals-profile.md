# The crewmate Profile card's tabs are Sessions, Schedules, Goals, Profile, and it opens on Sessions

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-08

## Decision

The crewmate Profile card's tab strip reads Sessions, Schedules, Goals, Profile, in that order: the crewmate's work first, its identity card last. The card opens on Sessions unless the opener names a tab.

## Why

- The card shipped in https://github.com/kirodotdev/KiroCrew/pull/16317 as Profile, Schedules, Sessions, Goals, opening on Profile, following the launch RFC's draft order.
- On https://github.com/kirodotdev/KiroCrew/pull/18245 the review lane flagged the reorder as an undeclared product change; the maintainer settled it on record: "the crewmate Profile card's tab order is Sessions, Schedules, Goals, Profile and it opens on Sessions".

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/18245#issuecomment-6071212582 -- the maintainer's on-record decision.
- https://github.com/kirodotdev/KiroCrew/pull/18245 -- the change that applies it (`PROFILE_TABS` in `CrewProfilePanel.tsx`).
- https://github.com/kirodotdev/KiroCrew/pull/16317 -- the card's first order.
- https://github.com/kirodotdev/KiroCrew/pull/18409 -- the pull request that adds this entry; the maintainer restates the decision in a comment there.

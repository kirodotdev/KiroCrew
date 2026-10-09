# Every sidebar lane ends with an in-flow "Show all older sessions" row, kept alongside the Older Sessions footer

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-09-05

## Decision

In the Sessions sidebar, the list-view root lane and the flat view each end with an in-flow "Show all older sessions" row after their last session row, hidden only while the Older Sessions pane is open. The Older Sessions footer stays as well. The two are not duplicates and are not folded into one.

## Why

- https://github.com/kirodotdev/KiroCrew/pull/1211 removed the in-flow row from both lanes as a duplicate of the footer. The maintainer restored it: "The removal treated the row as a duplicate control. It was a **discovery / guidance affordance**; the footer is the steady-state control. De-duplicating them removed the guidance."

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/8710 -- the maintainer's issue stating the decision.
- https://github.com/kirodotdev/KiroCrew/pull/8720 -- the maintainer's restoration, with `ChatSidebar.olderSessionsHint.test.tsx` as the regression guard.
- https://github.com/kirodotdev/KiroCrew/pull/1211 -- the removal this entry records as a regression.
- https://github.com/kirodotdev/KiroCrew/pull/18409 -- the pull request that adds this entry; the maintainer restates the decision in a comment there.

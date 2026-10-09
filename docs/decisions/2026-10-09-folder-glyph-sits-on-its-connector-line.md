# A sidebar folder's glyph sits on its own connector line, and folder rows share the session rows' left pad

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-09

## Decision

In the Sessions sidebar, the `border-l` connector line that runs down under an open folder starts at the x of that folder's glyph, so the glyph reads as the head of its own subtree; a folder header uses the same left pad as a session row (`ROW_BOX_CLS`, 14px), a folder name shares one left edge with the agent label, title and tool-call subtitle of every session filed inside it, and a nested folder's glyph sits on the content column of the sessions beside it. With `D` the nested body's own inset, `P` the header pad, `G` the glyph, `g` the glyph-to-name gap, `M` the body margin, `B` its border and `p` its pad, and `R` the session row pad, the three guides are `P = D + M`, `P + G + g = D + M + B + p + R` and `P = R`; today that is 14 = 2 + 12, 33 = 33 and 14 = 14, at every depth, with no per-depth term. Width for session titles is never bought by moving the glyph off its line or the folder header off the row pad.

## Why

- The maintainer tuned this geometry by hand and measured it on real renders in https://github.com/kirodotdev/KiroCrew/pull/3905, after three earlier attempts had each landed 1-4px out.
- https://github.com/kirodotdev/KiroCrew/pull/14717 later outdented the glyph off its connector line and cut the folder header pad to 3px to tighten the per-level indent from 19px to 10px. The maintainer saw the result on `main` and asked for the #3905 geometry back, with the alignment of the glyph and the line below it measured, and for the decision to be recorded so no later pull request changes it again.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/3905 -- the maintainer's own alignment pull request that set the geometry.
- https://github.com/kirodotdev/KiroCrew/issues/18359 -- the issue recording the regression and the maintainer's direction.
- https://github.com/kirodotdev/KiroCrew/pull/18362 -- the pull request that restores the geometry and adds this entry; the maintainer restates the decision in a comment there.
- https://github.com/kirodotdev/KiroCrew/pull/14717 -- the change this decision reverts.

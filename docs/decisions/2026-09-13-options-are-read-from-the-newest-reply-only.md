# An `[OPTIONS:]` ask is read from the newest assistant reply only; the dashboard never walks back to resurrect an older one

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-09-13

## Decision

The composer chips on the newest assistant reply are the only surface for an `[OPTIONS:]` ask, and a slot's `has_options` / `options` are computed from that newest reply alone. The dashboard never scans back through the transcript to re-surface an older `[OPTIONS:]` turn as a card, a sidebar status or a lane. A loop that is still blocked on the user re-states its options in its newest turn, and the chips come back on their own.

## Why

- https://github.com/kirodotdev/KiroCrew/pull/10173 added a backward scan over the last 150 rows, a "Waiting on you" card above the composer, a sidebar "Pending your response" status and a Waiting lane. The maintainer removed it the next day: "the 'Waiting on you' card re-surfaces a *stale* options turn in a question-card shape. Users read it as a pending question and cannot tell it from a real one; the composer chips on the newest assistant turn are the intended and only surface for `[OPTIONS:]`."
- The later Dynamic Dashboard Questions tab (https://github.com/kirodotdev/KiroCrew/pull/15945) lists an idle session's trailing ask but reads it from the slot's newest-reply-only `has_options`, naming the backward scan as a non-goal.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/10173#issuecomment-5656392231 -- the maintainer's verdict.
- https://github.com/kirodotdev/KiroCrew/pull/10615 -- the revert that removed the scan, the card, the sidebar branch and the lane.
- https://github.com/kirodotdev/KiroCrew/pull/15945 -- the Questions tab, built on the newest-reply-only rule.
- https://github.com/kirodotdev/KiroCrew/pull/18409 -- the pull request that adds this entry; the maintainer restates the decision in a comment there.

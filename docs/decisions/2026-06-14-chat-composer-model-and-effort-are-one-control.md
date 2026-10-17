# The chat composer offers model and reasoning effort as one control, never two

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-06-14

## Decision

The chat composer has one model picker, and reasoning effort lives inside it,
below the searchable model list. There is no separate effort button next to the
model name. A backend that reports effort support, or advertises its own effort
levels, feeds those into the same picker; a backend that reports none shows no
effort row inside it. Which backends support effort and which levels they offer
is a data question the picker answers from the ACP capability response; it is
never a reason to add a second control.

## Why

- Model and effort are one choice to the user: "how much brain, how hard it
  thinks". Splitting them doubles the composer chrome for a decision most users
  make once.
- The combined picker was designed this way on purpose when the effort slider
  first shipped (commit 612a1d63f2, 2026-06-14) and already carried the hooks a
  capability-driven backend needs (`hasEffort`, `slot`, `currentEffort`; the
  per-session `effortLevelsOverride` followed in #7693). Pull
  request #13637 replaced it with a separate `Effort: Default` button on
  2026-09-26 while adding a backend capability endpoint; the maintainer restated
  on 2026-09-27 that the backend change is welcome and the split is not.

## Evidence

- https://github.com/kirodotdev/KiroCrew/commit/612a1d63f2 -- the change that
  introduced the combined model + effort picker.
- https://github.com/kirodotdev/KiroCrew/pull/13637 -- the pull request that
  split the control, without a decision record.
- https://github.com/kirodotdev/KiroCrew/pull/14368#issuecomment-5853790305 -- the maintainer's own on-record restatement of the
  decision on the pull request that adds this entry.

# A pull-request watch can also wake on a few other pull requests

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

A structured pull-request watch can name a few extra pull requests it waits on (`also_wake_on` on
`monitor_watch` and `monitor_update`). These rulings hold for every version of it:

- An extra only ever wakes the session. An extra's state (merged, closed, unreadable) never ends the
  watch and never changes the watched pull request's completion rule.
- Extras share the watch's one runtime, agent-turn and token budget; there is no separate runtime, turn,
  token or probe budget for them.
- An extra that keeps failing to read is retired on its own, with one wake, and never stops the watch.
- While an extra is still open, a quiet watched pull request is not treated as stalled.
- An extra is never read under a credential grant made for the watched pull request.

The first version covers GitHub pull requests and wakes at most once when an extra merges, is closed, or
is retired. Other providers, and wakes for other changes on an extra such as review activity, are not
ruled out by this entry.

## Why

- A session waiting on a second pull request missed its merge until the watched one moved, and the side
  cron it used instead had its own budget and stop rules and was invisible to `monitor_inspect`.
- A second completion rule would make the watch end for a reason the watched pull request never reached.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/40070#issuecomment-7135711606 -- the maintainer's ruling.

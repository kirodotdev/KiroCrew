# A channel names its owner only when it has exactly one configured DM target, and only while that target is available

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-08

## Decision

`messaging.transport.sole_direct_target`, the one owner rule for every channel,
counts each configured `user:` target a channel advertises, available or not
(learned peers, threads and rooms are not counted), and names the owner only
when there is exactly one and it is available.

## Why

- On WeCom and Teams a configured person's target is available only after they
  have written to the bot, so with two configured people the one who had not
  written was skipped and the other was named the owner. Availability decided
  who the owner was.
- The helper's own description and the session-control spec already say an
  allow-list of two people names nobody, and the cross-channel owner notice
  already counts unavailable entries on the same principle.
- A WeCom-only owner answer was the other option offered; it would put owner
  inference in a second place, which the shared helper exists to prevent.
- On Teams, an owner DM with two or more allow-listed people falls back to the
  dashboard notification instead of reaching whoever wrote first.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/33035 -- the report and the two
  options.
- https://github.com/kirodotdev/KiroCrew/issues/33035#issuecomment-6722168873 --
  the maintainer decision on the issue.

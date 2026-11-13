# A silent parent's sub-agent completions never ring the bell

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

A silent cron's sub-agents — and any they spawn — never ring the dashboard
bell when they finish, failed and cancelled included. `spawn_run` and
`spawn_sub_agents` each take an optional call-level `silent` boolean (default
`false`), passed to the admission gate's existing `silent` parameter. `silent`
drops only completion notifications: the bell note on every route that rings
one and, under a cron, the channel post of the cron's reply, as today. It
changes no delivery and no approval alert, and a report that could not reach
its cron is still announced at the next gateway start, as for any silent run
today.

## Why

- A silent cron's own failures are already recorded but not broadcast, and the
  channel leg for these completions already honours the cron's flag, so the
  bell should match.
- The requesting workflow (waves, `spawn_status`) is `spawn_run`'s, which
  sends the same spawn request as `spawn_sub_agents`, so both tools carry the
  flag.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34157#issuecomment-6761048669 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/34157 -- the issue: cron-spawned
  sub-agents filled the bell with machine-to-machine payloads, with no opt-out
  even when the parent reaps results itself.

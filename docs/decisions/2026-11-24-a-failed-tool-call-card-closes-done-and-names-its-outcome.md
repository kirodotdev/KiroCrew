# A failed tool call's card in a Slack reply closes as done and names its outcome

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

In a Slack reply's tool-call plan, a tool call that fails, is refused, is
cancelled or is rejected closes its task card `complete` and writes its exact
outcome (`failed`, `refused`, `cancelled`, `canceled`, `rejected`) in the card's
details; no tool-call card is sent with the `error` status. This covers the
tool-call cards of the transport renderer and of the native stream's rejection;
the code-channel board's own plan is not covered.

## Why

- Replies stream with `task_display_mode: "plan"`, and one `error` task makes
  Slack title the whole plan "Something went wrong" in red while the turn keeps
  running and the other calls succeeded.
- The outcome stays readable on the call's own card.
- A count of succeeded and failed calls in the plan's header is not part of this
  decision; it stays open on the issue.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/21596#issuecomment-7135537959 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/21596#issuecomment-6891811449 --
  the live probe: one `error` task among three turns the plan red.
- https://github.com/kirodotdev/KiroCrew/pull/37568 -- the change this builds on.

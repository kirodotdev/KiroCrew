# First-run setup says why the configured agent is not ready, with no Sign in button of its own

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-14

## Decision

When first-run setup's "Use other coding agents" section says the configured
agent "isn't ready on this host yet", it also names the reason the setup check
found, in the wording the agent list already uses for the same reason, and it
carries no Sign in button of its own.

## Why

- The notice named the agent but not the cause, and Kiro Agent has no row in
  the agent list, so nothing on the screen said why it was not ready.
- Being signed out is not what keeps this notice up on a host that can run
  Kiro Agent, so a Sign in button there would not clear it; the first-run
  sign-in step is tracked in #32058.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34676#issuecomment-6872106550 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/34676 -- the issue.

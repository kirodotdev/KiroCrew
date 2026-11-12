# The policy-floor pip upgrade says to run it from your own terminal

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-08

## Decision

When a plain `pip` install is below the policy minimum version, the gateway's
version-compliance log line says to run its in-place upgrade and the
`kirocrew restart` after it from your own terminal; the `kirocrew update` print
and the extras install commands keep their text.

## Why

- An agent can read that log line, and both steps are the user's to run: the
  upgrade replaces the package the running gateway is loaded from, and the
  restart after it is refused to agents.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/25156#issuecomment-6716782682 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/25156 -- the issue.

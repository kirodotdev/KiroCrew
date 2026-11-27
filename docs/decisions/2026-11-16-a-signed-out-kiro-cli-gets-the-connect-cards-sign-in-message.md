# A signed-out kiro-cli gets the Connect card's sign-in message

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-16

## Decision

A Connect that fails because kiro-cli is signed out, as kiro-cli's own `whoami`
confirms, ends on the card with the existing "Connect needs kiro-cli installed
and signed in" message. No new string is added. Connecting without kiro-cli on
another backend stays with the "MCP sign-in by Kiro Crew" feature preview.

## Why

- The card says what to do: sign in to kiro-cli, then connect again.
- The verdict comes from kiro-cli, never from the error's prose.
- Reusing the sentence costs no new translation.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/35282#issuecomment-6908829585 --
  the decision.
- https://github.com/kirodotdev/KiroCrew/issues/35282 -- the report.

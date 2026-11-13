# An interrupted cloud launch names the stack it may leave and never deletes it for you

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-09

## Decision

`kirocrew cloud launch` records a new stack before provisioning starts, so every `cloud`
command finds it whatever ends the launch, and a Ctrl+C during provisioning names the
stack, says AWS may still be creating it and what it bills, and prints
`kirocrew cloud status --tag <tag>` and `kirocrew cloud destroy --tag <tag>` with the
launch's Region and profile; it neither deletes the stack nor asks to.

## Why

- Every interrupted launch in the report left a stack billing in AWS that no command named.
- Deleting is irreversible, and a user often presses Ctrl+C only because the install looked
  stuck; a message leaves both choices to them.
- Resuming a stack still being created, the gateway restart and the dashboard's Cancel are
  left to their own pull requests.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32100#issuecomment-6730537566 -- the
  maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/32100 -- the issue.

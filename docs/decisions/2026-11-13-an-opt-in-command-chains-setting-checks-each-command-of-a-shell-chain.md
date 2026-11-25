# An opt-in `command_chains` setting checks each command of a shell chain

Decided by: @bolichen97 (maintainer)
Date: 2026-11-13

## Decision

`"command_chains": "each"` in a policy or profile (an ordinal, `whole` < `each`,
strictest wins, unnamed means `whole`) asks the `commands` scope about each
command of a shell chain, at the tool-call gate for every shell call and for
every cron `command` body, and only ever refuses more than `whole` does.

## Why

- The issue asks for an opt-in: under an allow-mode `commands` list, `git *`
  admits `git status && python3 -c x`, and an operator who wants the list to
  mean only those commands had no way to say so.
- It composes like `sandbox.min_level`: either level can turn it on and neither
  can turn the other's off.
- Without the cron `command` body, a scheduled job could run a chain the gate
  refuses.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/35404#issuecomment-6867793819 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/35404 -- the issue.

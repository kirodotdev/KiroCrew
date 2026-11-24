# A spec hook may not use the engine's PermissionRequest trigger

Decided by: Bolin Chen (bolichen97), Kiro Crew maintainer
Date: 2026-11-24

## Decision

A hook document on the engine's `PermissionRequest` trigger (spelled `PermissionRequest` or `permissionRequest`), in an agent spec or in a `~/.kiro/hooks/*.json` file Kiro Crew reads, stays unsupported: Kiro Crew skips it and keeps both spellings out of its trigger alias table, and a permission request is answered only through Kiro Crew's own approval flow.

## Why

- On the engine this trigger is a decision hook: a hook on it can allow or deny the permission request itself, before anyone is asked.
- Accepting it with that power would let a spec answer permission requests, which widens what a spec can do inside the approval flow.
- Accepting it as observe-only is more than one table row: no hook event Kiro Crew fires matches it, and a hook written to the engine's allow or deny contract would have its answer ignored without a sign.
- Where Kiro Crew runs spec hooks, a `PreToolUse` hook already lets a spec refuse a tool call, so no supported use is lost.
- Kiro Crew does not ask the engine to run hooks itself; a change that did would reopen this decision.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/41937#issuecomment-7141188189 -- the decision, as a maintainer listed in `MAINTAINERS.md`.
- https://github.com/kirodotdev/KiroCrew/issues/41937 -- the question.

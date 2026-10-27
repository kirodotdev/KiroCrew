# On upgrade a crewmate moves onto its conductor and its own file even when its worker would lose a grant or its agent is unreadable

Decided by: Ray (project owner, @buluoray)
Date: 2026-10-27

## Decision

The startup migration moves a crewmate whose worker would lose a grant its old agent had, telling its user once what it lost, and moves a crewmate whose agent cannot be read onto the default template with the same notice; a grant the worker would gain beyond the old agent is never written and is offered only through the capability card, and a change that would loosen a limit (a gain, a switched-off server starting, a hook or a `permissions` / `toolsSettings` restriction not kept) still keeps the crewmate where it is.

## Why

- Every old crewmate the user actually uses should end up on the conductor chat with its own crewmate file; a crewmate left on its old agent keeps doing the work in its chat, the opposite of what the upgrade notice describes.
- A loss leaves the worker able to do less than before, so it can be undone by the user and is safe to move with as long as it is said; a gain or a dropped restriction lets the worker do more than the user ever allowed, so it is never applied without the user's own approval.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/24954 -- the pull request that adds this entry and implements it.

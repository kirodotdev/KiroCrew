# A shared MCP server keeps its approval when only an edition-folded version folder moves

Decided by: Ray Xu (maintainer, @buluoray)
Date: 2026-11-26
Supersedes: 2026-11-20-the-gateways-own-path-is-not-a-launch-change.md (in part)

## Decision

A shared MCP server keeps its launch approval when the only change to its launch
is a path segment that the edition's own launch normalizer folds as a version
folder, even when the owner did not tick "Always trust changes to this server".
Core ships no normalizer; an edition registers one in code, for the install
layout of a package manager it distributes itself, such as one that puts each
update under a new versioned folder. Any other change to the command, arguments or env still asks again,
as the 2026-11-20 entry says, and that entry stands for everything except this
case.

## Why

- A package manager that installs each update under a new versioned folder moves
  one argument on every update, while the program stays the same, so the owner
  is asked to approve the same program again after each update.
- The normalizer is edition code for a layout that edition owns, not something an
  agent can register.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/43251#issuecomment-7201342680 --
  Ray Xu restating this decision on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/43244#issuecomment-7201135089 --
  Ray Xu's comment stating this decision on the pull request that builds it.
- https://github.com/kirodotdev/KiroCrew/issues/43225 -- the design.

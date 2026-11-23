# The gateway's own inherited PATH and package-installer updates to it are not an MCP launch change

Decided by: Ray Xu (maintainer, @buluoray)
Date: 2026-11-20

## Decision

The part of a declared MCP spec `PATH` that the gateway itself appends (its own
`PATH` plus the well-known MCP directories, where a package installer adds or
moves version directories) does not count toward a stubbed server's launch
approval, so a change there pauses no shared server; the spec's own `PATH`
entries, the command and the arguments still count, and a moved version
directory in the resolved command path is still a changed launch.

## Why

- A re-rendered service unit changed the gateway's `PATH`, and five shared
  servers asked for approval at once although none of them had changed.
- The appended part comes from the gateway's own environment, which no agent
  writes, so asking about it carries no information for the person.
- A changed command or argument stays held until agents can no longer write
  the MCP registry; that is a separate change.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/40276 -- the report: five
  shared servers paused after the gateway's `PATH` changed.
- https://github.com/kirodotdev/KiroCrew/pull/40280#issuecomment-7057186737 --
  Ray Xu's decision comment, posted 2026-11-21.

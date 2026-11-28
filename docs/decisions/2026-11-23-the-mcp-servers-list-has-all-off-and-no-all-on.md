# The MCP servers list has an All off button and no All on

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

The Customize → MCP Servers tab has one bulk button, **All off**, which stages
the Kiro Crew switch off for every listed server whose switch is live, for the
usual Apply (a built-in server that is on, a row the shared config disables and
a row staged for uninstall keep their state); there is no All on, and nothing
writes `~/.kiro/settings/mcp.json`.

## Why

- Going from a full set of servers to a minimal one took one click per server.
- A registry install or a custom add starts switched off, and switching it on
  in this list is where the user confirms it. The list cannot tell such a server
  from one the user switched off, so an All on would also confirm servers nobody
  has reviewed.
- The Kiro Crew switch never edits the shared Kiro MCP config, so
  `/api/mcp/toggle-all`, which writes that file, keeps no UI caller.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/39883#issuecomment-7097302533 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/39883 -- the issue.

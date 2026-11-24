# Agents also ask before changing kiro-cli's MCP file, a project's MCP files and app manifests

Decided by: Ray Xu (maintainer, @buluoray)
Date: 2026-11-23

## Decision

The 2026-11-20 entry
(`2026-11-20-agents-ask-before-changing-gateway-trusted-files.md`) extends to the other
MCP config files an agent can write, sealed the same way #40308 sealed Kiro Crew's own
`mcp.json`: kiro-cli's `~/.kiro/settings/mcp.json`, a project's `.kiro/settings/mcp.json`
and `.mcp.json`, and an installed app's `app.json`. Agents change them only through a
`request_server` card, or Kiro Crew's own app install and update. A seal never stops a
session from starting: where a platform cannot seal a path, the write asks and is never
approved on its own.

## Why

These files decide what a later session launches, as Kiro Crew's own `mcp.json` does.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/41629#issuecomment-7119698863 -- Ray Xu's
  comment stating this decision.
- https://github.com/kirodotdev/KiroCrew/pull/40308#issuecomment-7057913394 -- the
  decision this entry extends.

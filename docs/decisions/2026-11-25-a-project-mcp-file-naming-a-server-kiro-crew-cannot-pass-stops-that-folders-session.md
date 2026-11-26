# A project MCP file naming a server Kiro Crew cannot pass on stops that folder's session

Decided by: Ray Xu (maintainer, @buluoray)
Date: 2026-11-25
Supersedes: 2026-11-23-agents-ask-before-changing-kiro-cli-project-and-app-mcp-files.md (in part: only "a seal never stops a session from starting", for the one case below)

## Decision

A session still always starts when Kiro Crew holds a folder's project MCP servers. One
narrow exception: when that folder's project MCP file names a server Kiro Crew cannot
pass to the agent host, the session in that folder does not start, and the chat says to
rename that server in the file and then start a new chat. Every other case is unchanged,
and every other rule of the 2026-11-23 entry stands.

## Why

Kiro Crew holds a project server by naming it to the host. A name Kiro Crew cannot pass
on cannot be held that way, and the host would otherwise start it unapproved.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/42182#issuecomment-7173302235 -- Ray Xu's
  comment stating this decision.
- https://github.com/kirodotdev/KiroCrew/pull/41629#issuecomment-7119698863 -- the
  decision this entry narrows.

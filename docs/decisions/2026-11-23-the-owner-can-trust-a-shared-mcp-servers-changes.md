# The owner can trust one shared MCP server's launch changes, from any source

Decided by: Ray Xu (maintainer, @buluoray)
Date: 2026-11-23
Supersedes: 2026-11-20-the-gateways-own-path-is-not-a-launch-change.md (in part)

## Decision

"Always trust changes to this server" is offered for every shared MCP server,
whatever file declares it, opt-in per server and unchecked by default, and only
the owner's click in the dashboard sets it; once set, later command, argument or
env changes to that server, including changes an agent makes, stay shared without
asking again. For servers the owner marked trusted this replaces the 2026-11-20
line that a changed command or argument stays held until agents can no longer
write the MCP registry; every other server keeps that rule, and the rest of the
2026-11-20 entry stands.

The trust choice does not check how a change reached the file. Which agent writes
reach it at all is decided by
`2026-11-23-agents-ask-before-changing-kiro-cli-project-and-app-mcp-files.md`: an
agent changes an MCP file only through a `request_server` card the owner applies,
and where a platform cannot seal a path, the write asks first. So "changes an
agent makes" means changes that already passed that card or that ask, plus a
write to any declaring file that no seal covers yet.

## Why

- Servers that update themselves re-ask after every update, and people approve
  them without reading. For example, a server whose resolved command path moves to
  a new version folder on each install is a changed launch under the 2026-11-20
  entry.
- The owner makes the choice per server, so nobody is opted in by default.
- The cost: once a server is trusted, a changed launch line for it, an agent's
  included, runs outside the chat sandbox without the owner seeing it first.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/41444#issuecomment-7114051793 --
  Ray Xu's comment stating this decision on the pull request that builds it.

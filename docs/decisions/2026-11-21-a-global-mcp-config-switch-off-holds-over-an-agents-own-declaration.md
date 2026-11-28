# A global MCP config's switch-off holds over an agent's own declaration on KAS

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-21

## Decision

On the KAS backend, Kiro Crew keeps a server off when a global MCP config file
such as `~/.kiro/settings/mcp.json` switches it off, even when an agent's own
file declares it, and the chat's start notice names that server for an agent
whose `tools` asks for it.

## Why

- The MCP Servers switch stores its "off" as `disabled: true` in that same file,
  so Kiro Crew cannot tell a hand-written entry from the switch.
- Letting the agent's file win, even with an explicit `"disabled": false`, would
  make the switch stop working for any agent file Kiro Crew does not manage that
  declares the server itself.
- The reported problem was that nothing said why the server's tools were
  missing, so the start notice names it and says a global MCP config switches
  it off.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/38359 -- the report.
- https://github.com/kirodotdev/KiroCrew/issues/38359#issuecomment-7071222137
  -- Bolin Chen's comment on that issue stating this decision.
- https://github.com/kirodotdev/KiroCrew/pull/30924 and
  https://github.com/kirodotdev/KiroCrew/pull/36391 -- the pull requests that
  made a config scope's switch-off reach an agent-declared server.

# A custom agent that grants tool search gets every enabled installed app's MCP servers by default

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

A Kiro Crew session on a user's own agent whose `tools` grants `tool_search`
gets the MCP servers of every enabled installed app, the entries Kiro Crew's
default agent mounts, without their `autoApprove`, except a server that a
wildcard in the agent's own `allowedTools` would cover (`"*"`, `@*`,
`@<app>:*`), even beside an entry naming it; the Crew Mode conductor (the
agent `agent.crew_mode_agent` names, or a shipped conductor) gets none,
`excludedTools` or `managedToolPolicy.excludeServers` withholds one, and an
agent that does not grant `tool_search` is unchanged.

## Why

- The report: a user who runs their own agent got nothing from installing an
  app that ships MCP tools, and nothing said why.
- Installing and enabling the app is the user's choice, and the existing
  exclusion fields are the opt-out per agent, so no new setting is added.
- Kiro Crew auto-approves no app tool. A call runs unasked only when the
  agent's own `allowedTools` names that server or tool; a wildcard there could
  run a third-party server's tools without the approval prompt or Kiro Crew's
  tool gate, so a server it covers is kept out, failing closed, and the user
  replaces the wildcard with the entries they approve to get it.
- A conductor mounts only Kiro Crew's own servers
  (`2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md`), so
  the agent `agent.crew_mode_agent` names gets no app server.
- It extends what a custom agent already gets from Kiro Crew's own servers
  (`kirocrew-core`, `kirocrew-cron`), through the same helper on both kiro
  backends. This entry is the record for that changed default.
- Not decided here: an app server whose declared name has a `/` (left out, as
  #37400 leaves it open), and an agent that exists only in a project checkout
  (unchanged: it gets no servers).

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/36782 -- the report.
- https://github.com/kirodotdev/KiroCrew/pull/40525#issuecomment-7074802285 -- the decision, as a maintainer listed in `MAINTAINERS.md`.
- https://github.com/kirodotdev/KiroCrew/pull/40525 -- the pull request that
  ships it and adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/28815 -- the custom-agent default
  servers it extends.

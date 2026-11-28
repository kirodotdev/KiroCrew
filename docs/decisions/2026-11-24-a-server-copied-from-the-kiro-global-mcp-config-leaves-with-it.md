# A server copied from the Kiro global MCP config leaves the agent spec with it

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

An entry Kiro Crew copies into the default agent spec from
`~/.kiro/settings/mcp.json` is stamped `x-kirocrew-copied`, and the next rebuild
removes a stamped entry once no MCP config launches that server, edits made to
the copy included. An entry the user writes into the spec carries no stamp and
always stays, nothing is removed while one of the MCP config files cannot be
read, and a copy an older build made carries no stamp, so it stays until it is
deleted by hand.

## Why

- The spec is rebuilt on top of itself, so a copy outlived the server it came
  from: a renamed or removed server kept failing to start in every session.
- Only a record written with the copy tells it apart from a server the user
  wrote. A list of names kept beside the spec would remove a user's server that
  reuses a name, or every copy when the global file has a typo.
- Stamping an existing entry by its content would also claim servers the user
  wrote, so an older build's copy is left alone.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/41777 -- the report.
- https://github.com/kirodotdev/KiroCrew/issues/41777#issuecomment-7121489255
  -- Bolin Chen's comment on that issue stating this decision.

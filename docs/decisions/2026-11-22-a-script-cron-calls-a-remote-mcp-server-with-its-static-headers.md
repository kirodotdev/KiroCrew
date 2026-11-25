# A script cron calls a remote MCP server with its static headers

Decided by: Bolin Chen (bolichen97), Kiro Crew maintainer, 2026-11-22
Date: 2026-11-22

## Decision

A script cron's `ctx.call_tool` and `kirocrew cron preview` call a remote (URL) MCP server that speaks streamable HTTP, sending the entry's static `headers` with `${VAR}` filled in the way a local server's `env` is.

## Why

- More MCP servers are hosted than local, and a script cron could reach only local ones.
- A static header (an API key or token) is resolved from the same view a local server's `env` already is, and the request leaves from the script's own process, so a script gains no credential or connection it did not already have.
- Options not chosen: a stdio-to-remote proxy declared as a local server (a third-party program and a second entry for every remote server); handing a script cron OAuth tokens Kiro Crew or kiro-cli hold.
- Not decided here: OAuth sign-in for a script cron, and routing a script cron through the MCP gateway (#32996).

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/38479#issuecomment-7075920716 -- the decision, as a maintainer listed in `MAINTAINERS.md`.
- https://github.com/kirodotdev/KiroCrew/issues/38479 -- the request.

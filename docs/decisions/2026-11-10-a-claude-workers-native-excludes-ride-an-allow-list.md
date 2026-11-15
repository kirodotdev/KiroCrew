# A claude Bot worker's native-tool excludes ride an allow-list of the built-ins it keeps

Decided by: @bolichen97 (maintainer)
Date: 2026-11-10

## Decision

On `agent.acp_backend: claude`, a Bot worker whose file excludes a native tool runs with claude's `tools` option listing only the built-ins of the native tools it keeps, instead of being refused, and a native entry no claude built-in covers is still refused before spawn.

## Why

- A Bot on claude could not use the common "no shell" or "no file writes" worker profile: the worker was refused before it started.
- A claude session's built-ins depend on the user's own Claude Code release, so the fix names the tools a worker keeps rather than the ones it drops.
- It lands only after a live capture on the pinned adapter shows the list holds for the worker, for its MCP tools and for any subagent it starts.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32621#issuecomment-6757831843 -- the maintainer's statement of this decision.
- https://github.com/kirodotdev/KiroCrew/issues/32621 -- the issue that set out the gap and the capture it needed.

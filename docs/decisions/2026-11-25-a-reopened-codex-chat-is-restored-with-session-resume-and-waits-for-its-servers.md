# A re-opened Codex chat is restored with session/resume and waits for its MCP servers like a new one

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

On the Codex harness a re-opened chat is restored with `session/resume`, carrying the same MCP startup wait (`_meta.mcpStartupAwaitTimeoutMs`) a new Codex chat's `session/new` carries, instead of `session/load`, so its first message is offered the tools of every server that is ready within that wait.

## Why

- Of the two restore requests, codex-acp holds the reply for that wait only on `session/resume`; a `session/load` answers before a slow server is up, and Codex fixes a turn's tools when the turn starts.
- `session/resume` kept the earlier conversation in every measurement, after a restart and on a process already serving another chat, on codex-acp 2.3.0 and 2.12.0, and the six releases checked (1.0.0 to 2.12.0) all advertise it.
- Accepted cost: a re-open onto a process already serving another chat had the slow server's tool on main too in the measured setup, so there the change only adds the wait a new Codex chat is given (up to 30 seconds for a server that never answers); what made the tool appear there without a wait is not established.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/41912#issuecomment-7169613266
- Measurements: https://github.com/kirodotdev/KiroCrew/issues/41912#issuecomment-7158794171 and the decision comment above.
- Issue: https://github.com/kirodotdev/KiroCrew/issues/41912

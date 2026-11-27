# One harness can run sandboxed while the sandbox is off, through an opt-in per-harness floor

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

`agent.harness_sandbox` maps a harness id to `auto` or `strict` (the `agent.sandbox` values; `off` or no entry is no floor), empty by default: a session on a listed harness starts at the stricter of `agent.sandbox` and its floor with its credential mask applied, only the harnesses refused while the sandbox is off read it (not Kiro CLI, KAS or Claude Code), Settings > Agent Harness, saving the harness, a crewmate's harness pin and first-run setup read the same floor, the refusal text is unchanged when no floor applies, and a floor change reaches sessions started after it.

## Why

- An operator who keeps `agent.sandbox: "off"` on purpose, for example for crons that write outside the sandbox, had no way to use one enforced harness: the only remedy was to sandbox the whole gateway.
- The floor can only raise a tier, and the harness then runs under the same mask a sandbox-on gateway already gives it, so nothing is loosened.
- A default floor would override an `agent.sandbox: "off"` the operator chose, so there is none.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/39721
- https://github.com/kirodotdev/KiroCrew/issues/39721#issuecomment-7091177311

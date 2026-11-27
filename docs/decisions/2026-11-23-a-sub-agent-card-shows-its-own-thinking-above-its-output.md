# A managed sub-agent's card shows its own thinking above its Output

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

A `spawn_run` sub-agent's thinking streams to its card as `subagent_thinking`, drawn as a thinking block above the card's Output (collapsed by default, like the chat's own thinking), and `GET /api/spawn/{id}` returns it as an optional `thinking` field while the run is live; it never becomes part of the result or the parent's report.

## Why

- A long sub-agent looked silent between tool calls, so a person watching it could not tell slow progress from a stuck run.
- The ruling built this on main instead of waiting for or stacking on the open pull request that adds the same frame for harness-native children, and kept that frame's name, `{id, slot, text}` payload and card block, so there is one definition.
- A run that never thinks answers as before, and the thinking reaches only sub-agent subscribers, never a channel gateway or the parent model.

## Evidence

- The ruling: https://github.com/kirodotdev/KiroCrew/issues/39612#issuecomment-7092612298
- The request: https://github.com/kirodotdev/KiroCrew/issues/39612
- The related open pull request: https://github.com/kirodotdev/KiroCrew/pull/37870

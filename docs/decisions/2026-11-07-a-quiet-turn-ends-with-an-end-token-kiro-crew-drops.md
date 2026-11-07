# After `nothing_to_do` the model writes only a fixed end token, which Kiro Crew drops where the model's text first arrives

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-07
Supersedes: 2026-10-03-every-agent-turn-ends-with-text-or-nothing-to-do.md (in part: "a `nothing_to_do` call with nothing after it")

## Decision

After `nothing_to_do` the model writes only a fixed end token, which Kiro Crew drops where the model's text first arrives, so no chat, channel, cron or sub-agent result shows it; every other rule of the superseded entry stands, including the recovery ladder for a bare stop after an ordinary tool.

## Why

- A quiet end with nothing after the tool call leaves the backend's history ending on a tool result. The backend fills that gap with its own assistant placeholder, and the model reads it back and writes it again after later quiet ends: billed output on every quiet cycle that Kiro Crew then throws away. One crewmate DM on a KAS backend counted 190 copies in its backend session history, and 323 `Turn-end contract violation` warnings in under a day.
- A fixed token gives the backend a real assistant message to end on. Dropping that exact token where the model's text first arrives keeps every surface as it is today when the model writes nothing, and the token never reaches the `Turn-end contract violation` count, so the warning stays honest.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/19276#issuecomment-6705496717 -- the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/19276 -- the request, by the maintainer who decided the superseded entry.
- https://github.com/kirodotdev/KiroCrew/issues/27655 -- the counts from an affected crewmate DM.
- https://github.com/kirodotdev/KiroCrew/pull/19265 -- the earlier change that stopped the dashboard from showing text after an applied `nothing_to_do`.

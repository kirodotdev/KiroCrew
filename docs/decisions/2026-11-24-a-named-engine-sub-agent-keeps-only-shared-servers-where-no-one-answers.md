# A named engine sub-agent keeps only the chat agent's servers where no person may answer its ask

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24
Supersedes: 2026-11-24-conductors-hand-work-to-sessions-and-engine-sub-agents-replace-spawn-run.md (in part, for its "with that agent's own tools and third-party MCP servers" clause only, as it applies to a named agent outside a person's own dashboard chat on the default agent and the crew member named `default`'s chat on it; every other rule of that entry stands)

## Decision

Behind the default-off `kas_subagents` feature preview, a named agent (one the parent's `availableAgents` allows) run as an engine sub-agent outside a person's own dashboard chat on the default agent and the crew member named `default`'s chat on it keeps only the MCP servers it declares exactly as the chat's agent does; its other servers are left out. In those two chats it keeps its own third-party MCP servers, as the 2026-11-24 entry says. The chat's own agent run as an engine sub-agent is unchanged. With the preview off or never set, nothing changes.

## Why

- A named agent's server that the chat's agent lacks is held only by an approval prompt. In a cron run on `auto`, a trusted chat, a messaging channel or a dispatched worker no person may be there to answer it, so the prompt would be answered without anyone approving that reach.
- In a person's own dashboard chat on the default agent the person answers that prompt, as before the engine's tool replaced `spawn_run`.

## Evidence

- The maintainer's statement of this rule: https://github.com/kirodotdev/KiroCrew/pull/42047#issuecomment-7153298506
- The entry this supersedes in part, landed by https://github.com/kirodotdev/KiroCrew/pull/42218
- The code change it authorizes: https://github.com/kirodotdev/KiroCrew/pull/42108

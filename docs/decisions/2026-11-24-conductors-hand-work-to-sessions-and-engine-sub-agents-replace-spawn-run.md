# Conductors hand work to sessions; elsewhere the engine's sub-agent runs the agent spawn_run would

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24
Supersedes: 2026-11-24-a-conductor-gets-the-engines-sub-agents-under-the-kas-subagents-preview.md (whole: a conductor's own chat and a Bot's own thread get no engine sub-agent tool and draw no engine sub-agent card; the 2026-10-17 crewmate-chat entry and the 2026-11-04 conductor entry hold unchanged with the preview on, as they do with it off)

## Decision

Behind the default-off `kas_subagents` feature preview:

- A conductor's own chat (a Bot's conductor, the Ultra Mode conductor and every other conductor agent Ultra Mode runs) and a Bot's own thread mount no engine sub-agent tool. They hand work to separate sessions (`session_create`, `work_dispatch`) exactly as with the preview off, with their tool set unchanged.
- In every other KAS chat on an agent Kiro Crew ships or builds, the sessions a conductor or a Bot creates (a dispatched worker, a Bot's worker chat) and ordinary chats alike, the engine's sub-agent tool takes the place of `spawn_run` and `spawn_sub_agents`. It runs the agent `spawn_run` would run: the chat's own agent when none is named, or a named agent the parent's `availableAgents` allows, with that agent's own tools and third-party MCP servers, under the parent's denies.
- An engine sub-agent has no Kiro Crew tool (memory, cron, messaging, spawn), starts no sub-agent of its own, and takes none of `spawn_run`'s per-run options (model, backend, folder, turn limit). Its prompt says so. The built-in flows that pin a model on each `spawn_run` (the local review lanes, llm-council) run those tasks on the chat's own model in these chats.
- With the preview off or never set, nothing changes.

## Why

- The maintainer's decision: conductor and Bot sessions create sessions, they do not start sub-agents; what is managed is the sub-agent inside the sessions they create.
- An engine sub-agent should be the same agent a `spawn_run` child is, so the model has one tool with one meaning instead of two tools that run different agents.
- Kiro Crew's own servers are launched once per session with that session's identity, and an engine sub-agent runs inside its parent's turn with no session of its own, so those servers cannot be carried; the maintainer accepted that gap.
- With the preview off or never set, no engine tool is mounted and every chat is as it was.

## Evidence

- The maintainer's restatement of this rule: https://github.com/kirodotdev/KiroCrew/pull/42047#issuecomment-7151107738 (it supersedes the earlier addenda https://github.com/kirodotdev/KiroCrew/pull/42047#issuecomment-7148043887 and https://github.com/kirodotdev/KiroCrew/pull/42047#issuecomment-7148492346).
- The entry this supersedes, landed by https://github.com/kirodotdev/KiroCrew/pull/42047
- The issue this follows: https://github.com/kirodotdev/KiroCrew/issues/42042

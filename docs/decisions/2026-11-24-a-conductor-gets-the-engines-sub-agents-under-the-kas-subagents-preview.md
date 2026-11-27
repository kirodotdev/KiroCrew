# A conductor gets the engine's in-turn sub-agents behind the kas_subagents preview

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24
Supersedes: 2026-10-17-crewmate-chat-hands-off-to-worker-sessions.md (in part, for the `kas_subagents` preview only: "has no ... other sub-agent tool" no longer holds for the engine's own in-turn sub-agent tool while the preview is on, and "draws no tool row at all" no longer holds for the engine sub-agent card that tool draws; the chat still has no `spawn_run` or `spawn_sub_agents` and still hands work to worker sessions)
Supersedes: 2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md (in part, for the `kas_subagents` preview only: the engine's sub-agent tool is the one capability a conductor gains; it mounts no new MCP server and loads no new skill)

## Decision

Behind the default-off `kas_subagents` feature preview, a conductor (a Bot's conductor, the Ultra Mode conductor, and every other conductor agent Ultra Mode runs) gets the engine's in-turn sub-agent tool like every other KAS chat; its children run on the conductor's own spec and limits (its denies, asks, tools and MCP servers), never more; with the preview off or never set, both earlier entries stand unchanged.

With the preview on, that conductor chat, a Bot's own chat included, draws the engine sub-agent card like every other KAS chat; with it off or never set, the 2026-10-17 entry's "draws no tool row" wording holds unchanged.

This entry adds no MCP server, no skill and no `spawn_run` or `spawn_sub_agents` to any conductor, and a conductor still hands its work to worker sessions. Every other rule of the 2026-10-17 entry and of the 2026-11-04 entry, each as the later entries that name it amended it, stands.

## Why

- The maintainer's decision: every KAS chat kind gets the engine's sub-agents when the preview is on, and a conductor is one of those chat kinds.
- The preview is off by default. A person who turns it on is trying the engine's sub-agents, and a conductor left out is the one chat kind where that trial cannot happen.
- A child cannot do more than the conductor that started it, so the conductor gains no reach it did not already have; it gains a way to do a short look-up inside its own turn instead of opening a worker session for it.
- With the preview off or never set, no conductor mounts the tool, and both earlier entries read as written.

## Evidence

- The maintainer's decision, on the pull request that adds this entry: https://github.com/kirodotdev/KiroCrew/pull/42047#issuecomment-7139302818
- The maintainer's addendum on the engine sub-agent card in a conductor's chat, on the same pull request: https://github.com/kirodotdev/KiroCrew/pull/42047#issuecomment-7146172588
- The pull request that adds this entry: https://github.com/kirodotdev/KiroCrew/pull/42047
- The issue this entry and the change it covers follow: https://github.com/kirodotdev/KiroCrew/issues/42042
- https://github.com/kirodotdev/KiroCrew/pull/41972 -- gives every other KAS chat kind the engine's sub-agents under the same preview and keeps conductors off pending this entry.

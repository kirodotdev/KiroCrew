# Conductors stay simple: new MCP servers and skills go to workers, never to conductors

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-04

## Decision
A conductor, the Ultra Mode conductor or a mate's, mounts only Crew's own MCP servers and loads only the skills it names itself; new MCP servers and skills go to workers, and a change that adds capability to a conductor or binds Ultra Mode or mates back to custom agents needs an explicit owner decision. Rollout for mate conductors: scoped skills are the intended direction; this release still adds the user's `~/.kiro/skills` and workspace `.kiro/skills` to a mate conductor's mapping and the mate tells the person in its chat, and the next release removes that inheritance.

## Why
- Ultra Mode and mates replace custom agents and move to self-evolution.
- A mate copies a custom agent's MCP servers, skills, tools and prompt once, as its starting point. After that it grows through its own memory and its own abilities: chatting with the mate, or the mate's Abilities settings.
- Ultra Mode wants general ability, so its worker gets every MCP server and skill from the start.
- A conductor only analyzes the problem, dispatches tasks and organizes their execution. Keeping it simple means it gets no user servers or user skills.

## Evidence
- The pull request that adds this entry and starts that rollout: https://github.com/kirodotdev/KiroCrew/pull/29926
- The maintainer's restatement on that pull request, the on-record artifact this entry rests on: https://github.com/kirodotdev/KiroCrew/pull/29926#issuecomment-6645315433
- The review that asked for a one-release deprecation instead of removing the inheritance at once: https://github.com/kirodotdev/KiroCrew/pull/29926#pullrequestreview-5778297941
- The design it builds on: `docs/request-for-change/rfc-mate-owned-agent-config.md` §3 (the conductor's prompt, tools, MCP servers and skills are source-defined).

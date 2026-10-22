# A crewmate's chat starts no sub-agents and draws no spawn_run card; it hands work to worker sessions

Decided by: Ray Xu (owner of the crewmate design, @buluoray)
Date: 2026-10-17
Supersedes: 2026-10-16-crewmate-chat-draws-the-spawn-run-card.md

## Decision

A crewmate's own chat runs on its conductor, which only dispatches: it hands work to worker sessions (`session_create`, `session_send`, `session_read_message`) and has no `spawn_run`, `spawn_sub_agents` or other sub-agent tool, so the chat draws no tool row at all, the `spawn_run` launch card included; every other rule of the superseded entry stands.

## Why

- The conductor dispatches and the worker does the work. A worker session runs on the crewmate's own memory and workspace with the worker's tools plus the crewmate's own additions, appears in the sidebar, and can be read and taken over by the person; a sub-agent is none of those.
- With no sub-agents there is no launch to draw: the one tool row the superseded entry let through has nothing left to show, and the chat goes back to the person's rows and the crewmate's speech.
- Delegated work stays visible as the worker sessions themselves in the sidebar, which outlive the crewmate's turn the way the launch card did.

## Evidence

- `docs/request-for-change/rfc-mate-owned-agent-config.md` -- the conductor/worker design this applies.
- [#22443](https://github.com/kirodotdev/KiroCrew/pull/22443) -- accepts that RFC as the product owner's decision of 2026-10-20.
- [The owner's restatement](https://github.com/kirodotdev/KiroCrew/pull/22444#issuecomment-6286548646) on the pull request that adds this entry. A maintainer listed in `MAINTAINERS.md` restates it there too; that comment is the other on-record artifact this entry rests on.

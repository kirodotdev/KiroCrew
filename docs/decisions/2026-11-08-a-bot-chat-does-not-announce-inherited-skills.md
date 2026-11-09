# A bot's chat does not announce that it still loads the person's own skills

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-08
Supersedes: 2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md

## Decision

A new bot chat session shows no notice, and the bot says nothing in its reply, about the person's own skills that its conductor still loads for this release; a skill to keep is added in the bot's Abilities.

## Why

- The notice asked for no action now and repeated on every new session, so the maintainer judged it noise.
- This replaces only the clause "the mate tells the person in its chat" of `2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md`. The rest of that entry stands: conductors stay simple, new MCP servers and skills go to workers, this release still adds the user's `~/.kiro/skills` and workspace `.kiro/skills` to a mate conductor's mapping, and the next release removes that inheritance.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/33038#issuecomment-6719687998 -- the maintainer's decision, stated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/issues/33037 -- the issue recording the request.

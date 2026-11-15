# On claude, the `/` menu lists the commands each chat's own session advertises

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

On a claude chat, the `/` menu adds the commands that chat's own live claude session last advertised (its project and user `.claude/commands` files and skills) to the list every chat gets; a chat with no live session or no update yet keeps that list, and what a typed command runs does not change.

## Why

- Claude Code builds the command list per session, from that session's working directory and settings, so a per-session list is exact and needs no shared cache.
- A per-directory list would still have to know which chat is asking, so it is the per-session list plus a cache.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32619#issuecomment-6757703895 -- the maintainer's decision, on the issue that asked for it.
- https://github.com/kirodotdev/KiroCrew/issues/32619 -- the issue.

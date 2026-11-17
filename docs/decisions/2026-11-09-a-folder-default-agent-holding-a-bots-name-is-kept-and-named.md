# A folder Default agent that holds a bot's name is kept as saved and named once in a notice, never rewritten

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-09

## Decision

The one-time folder pass leaves every folder whose Default agent is exactly one bot's label as saved and posts one bell notice naming each such folder and the name it stores, so the person picks the bot in that folder's settings where the bot was meant; folders the earlier rewrite already moved to the bot, which shipped in v0.12.1 and lts-v0.1.0, keep that change.

## Why

- Since the folder picker lists a bot and a same-named custom agent as two rows, it saves the custom agent's row as that same name, so a deliberate custom-agent pick and an older label meant for the bot are the same stored string.
- No released build records which one a folder holds, so a rewrite would silently move deliberate picks onto the bot, its model, workspace and memory; leaving the value and asking moves nothing and still tells the person.
- The rewrite recorded no folder's earlier value, so a folder it moved stores exactly what a folder set to the bot on purpose stores; a notice or a rewrite on those homes would have to guess, so the moved folders are accepted as they are, and chats already run there stay in the bot's history and memory. The person picks the custom agent again in the folder's settings.
- This was first decided believing the rewrite had not shipped; it shipped in v0.12.1 and lts-v0.1.0, and on 2026-11-11 the acceptance was confirmed for those homes too.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32568#issuecomment-6730740584 -- the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/32568 -- the report that the rewrite moves deliberate custom-agent picks.
- https://github.com/kirodotdev/KiroCrew/issues/32568#issuecomment-6791291135 -- the maintainer's correction: the rewrite shipped, and the decision stands.
- https://github.com/kirodotdev/KiroCrew/pull/32299 -- the rewrite this replaces, first released in v0.12.1 and lts-v0.1.0.
- https://github.com/kirodotdev/KiroCrew/issues/35186 -- pointing out, in the product, the folders the shipped rewrite moved.
- https://github.com/kirodotdev/KiroCrew/issues/22713 -- the original report of a folder bound to a custom agent instead of the bot.

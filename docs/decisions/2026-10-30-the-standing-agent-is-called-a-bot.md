# The standing goal-driven agent is called a Bot, not a Mate

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-30
Supersedes: 2026-10-15-the-standing-teammate-agent-is-called-a-mate.md

## Decision

Everywhere a person or the model reads it (dashboard copy, every locale, user-facing docs, CLI text and agent prompts), the always-on, goal-driven agent is a **Bot** (plural **Bots**), and the name "Bot" stays untranslated in every locale; code identifiers, i18n keys, API paths, the `kirocrew-mate` MCP server, data-file names and the `.crewjam` share-file extension ([2026-10-31](2026-10-31-the-share-file-is-a-crewjam-file.md)) keep their spelling.

## Why

- The maintainer renamed the product concept from Mate / Crewmate to Bot.
- The scope is what a user sees and what the prompts say; renaming identifiers and persisted state is not part of it.
- The maintainer chose to keep the word "Bot" as is in every language instead of translating it.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/26417 -- the issue recording the scope.
- https://github.com/kirodotdev/KiroCrew/issues/26417#issuecomment-6533446671 -- the decision to keep the name untranslated.
- https://github.com/kirodotdev/KiroCrew/issues/26417#issuecomment-6617445636 -- the name became "Bot", capitalized and untranslated.

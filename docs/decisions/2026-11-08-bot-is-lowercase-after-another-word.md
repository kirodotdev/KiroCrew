# In English copy, "Bot" is lowercase after another word and capitalized only alone or at a sentence start

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-08
Supersedes: 2026-10-30-the-standing-agent-is-called-a-bot.md

## Decision

In English copy, the always-on, goal-driven agent is written "Bot" (plural "Bots") only when the word stands alone as a label or starts a sentence, and "bot" after any other word ("New bot", "Search bots…", "this bot's chat"); other products' own terms keep their casing ("Azure Bot", "Bot Framework", "Bot API", "Bot ID").

## Why

- Marketing asked for sentence case in English copy, so the noun is capitalized only where it is the whole label or starts a sentence.
- This replaces only the casing of `2026-10-30-the-standing-agent-is-called-a-bot.md`. The rest of that entry stands: the concept is a Bot everywhere a person or the model reads it, the name stays untranslated in every locale, and code identifiers, i18n keys, API paths and data-file names keep their spelling.
- Other locales do not change.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/30697#issuecomment-6716195749 -- the maintainer's decision, stated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/issues/30649 -- the issue recording the request.

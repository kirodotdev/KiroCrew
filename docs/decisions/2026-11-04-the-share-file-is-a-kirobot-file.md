# The Bot share file is a `.kirobot` file; old `.crewjam` and `.crewmate` files still import

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-11-04
Supersedes: 2026-10-31-the-share-file-is-a-crewjam-file.md

## Decision

Export writes the share file as `<name>.kirobot`, with `"format": "kirocrew.kirobot"` and the manifest entry `kirobot.json`; import keeps accepting the two older forms, `.crewjam` (`kirocrew.crewjam`, `crewjam.json`) and `.crewmate` (`kirocrew.crewmate`, `crewmate.json`), through the same reader checks.

## Why

- The product name is now Bot, not Jam, so the share file follows it.
- This also replaces the one point of `2026-10-30-the-standing-agent-is-called-a-bot.md` that kept the `.crewjam` extension; the rest of that entry stands.
- Files people already shared must keep importing, so the reader takes all three forms. A file holding more than one manifest entry is refused.
- Code identifiers, i18n keys, API paths and every other data-file name keep their spelling.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/26417#issuecomment-6617445636 -- the name became "Bot".
- https://github.com/kirodotdev/KiroCrew/pull/29962#issuecomment-6645112004 -- the maintainer's decision to rename the share file to `.kirobot`, restated on the pull request that adds this entry.
